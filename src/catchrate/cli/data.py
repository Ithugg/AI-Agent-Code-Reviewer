"""Data commands: clone, mine, szz-eval, verify, freeze."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

from catchrate.benchmark import SPLITS, freeze, hf_configs, with_configs
from catchrate.cli.common import fmt_counts, github, open_repo, read_jsonl, spec_of, write_jsonl
from catchrate.mining.cases import MineOptions, find_labeled_fixes, mine_regression_cases
from catchrate.mining.clean import mine_clean_cases
from catchrate.repos import REPOS
from catchrate.schema import VERIFIED, Case, Verification
from catchrate.szz.blame import write_ignore_revs
from catchrate.szz.run import evaluate_szz
from catchrate.verify.envs import EnvManager
from catchrate.verify.verify import Verifier, classify


def register(commands: argparse._SubParsersAction) -> None:
    clone = commands.add_parser("clone", help="clone the repository (resumable); --update fetches new commits")
    clone.add_argument("--update", action="store_true")
    clone.set_defaults(func=cmd_clone)

    mine = commands.add_parser("mine", help="build cases from labeled bug fixes (named regressions, or changelog fixes + SZZ)")
    mine.add_argument("--since", type=date.fromisoformat, help="only fixes committed on or after this date")
    mine.add_argument("--max-lines", type=int, default=500, help="largest reviewable change (added + deleted lines)")
    mine.add_argument("--workers", type=int, default=6)
    mine.set_defaults(func=cmd_mine)

    szz = commands.add_parser("szz-eval", help="score B-SZZ against developer-labeled regressions")
    szz.add_argument("--since", type=date.fromisoformat, help="only fixes committed on or after this date")
    szz.add_argument("--workers", type=int, default=6)
    szz.set_defaults(func=cmd_szz_eval)

    verify = commands.add_parser("verify", help="run each bug's fix tests before the change, after it, and at the fix")
    verify.add_argument("--since", type=date.fromisoformat, help="only bugs fixed on or after this date")
    verify.add_argument("--case", action="append", default=[], help="only these case ids (repeatable)")
    verify.add_argument("--limit", type=int, help="stop after this many bugs")
    verify.add_argument("--workers", type=int, default=4)
    verify.add_argument("--redo", action="store_true", help="re-verify bugs that already have a verdict")
    verify.add_argument("--redo-verdict", action="append", default=[], help="re-verify bugs with this verdict (repeatable)")
    verify.add_argument("--reclassify", action="store_true", help="re-derive verdicts from the stored runs; runs no tests")
    verify.set_defaults(func=cmd_verify)

    clean = commands.add_parser("mine-clean", help="sample merged PRs with no known bug, for measuring false alarms")
    clean.add_argument("--count", type=int, default=40)
    clean.add_argument("--since", type=date.fromisoformat, default=date(2022, 3, 1), help="after the 2022 Black reformat")
    clean.add_argument("--until", type=date.fromisoformat, default=date(2025, 6, 30), help="leave a year to check survival")
    clean.add_argument("--survival", type=float, default=0.9, help="share of added lines still untouched a year later")
    clean.set_defaults(func=cmd_mine_clean)

    frz = commands.add_parser("freeze", help="write the benchmark: verified bugs, clean cases, and splits")
    frz.add_argument("--dev", type=int, default=30, help="buggy cases in the dev split")
    frz.add_argument("--ablation", type=int, default=60, help="buggy cases in the ablation split")
    frz.set_defaults(func=cmd_freeze)

    export = commands.add_parser("export", help="write every frozen repo as one Hugging Face dataset folder (no upload)")
    export.add_argument("--out", type=Path, default=Path("build/hf"))
    export.set_defaults(func=cmd_export)


def cmd_clone(args: argparse.Namespace) -> int:
    spec = spec_of(args)
    repo = open_repo(args, update=args.update)
    print(f"{spec.key}: {spec.default_branch} at {repo.head_of(spec.default_branch)}")
    return 0


def cmd_mine(args: argparse.Namespace) -> int:
    spec, repo = spec_of(args), open_repo(args)
    ignore_revs = write_ignore_revs(repo, spec.default_branch, args.data_dir / "cache" / spec.key / "ignore-revs")
    opts = MineOptions(max_changed_lines=args.max_lines, ignore_revs_file=ignore_revs, workers=args.workers)
    with github(args) as gh:
        cases, rejections = mine_regression_cases(repo, gh, spec, since=args.since, opts=opts)
    out_dir = args.data_dir / "cases"
    write_jsonl(out_dir / f"{spec.key}.jsonl", cases)
    write_jsonl(out_dir / f"{spec.key}.rejected.jsonl", rejections)
    bugs = [b for c in cases for b in c.bugs]
    summary = {
        "repo": spec.key,
        "head": repo.head_of(spec.default_branch),
        "since": args.since.isoformat() if args.since else None,
        "max_changed_lines": args.max_lines,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cases": len(cases),
        "bugs": len(bugs),
        "rejected_pairs": len(rejections),
        "review_units": Counter(c.review.review_unit for c in cases),
        "bug_locality": Counter(b.locality for b in bugs),
        "bugs_per_case": Counter(str(len(c.bugs)) for c in cases),
        "case_flags": Counter(f for c in cases for f in c.flags),
        "bug_flags": Counter(f for b in bugs for f in b.flags),
        "rejection_reasons": Counter(r.split(":", 1)[0] for rej in rejections for r in rej.reasons),
        "bug_summary_sources": Counter(b.bug_summary_source for b in bugs),
        "bugs_by_fix_year": dict(sorted(Counter(str(b.fix_date.year) for b in bugs).items(), reverse=True)),
    }
    (out_dir / f"{spec.key}.summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(
        f"{spec.key} @ {summary['head'][:10]}: {len(cases)} cases holding {len(bugs)} bugs, "
        f"{len(rejections)} (fix, commit) pairs rejected -> {out_dir / spec.key}.jsonl"
    )
    for label in ("review_units", "bug_locality", "bugs_per_case", "case_flags", "bug_flags", "rejection_reasons", "bugs_by_fix_year"):
        print(f"  {label.replace('_', ' ')}: {fmt_counts(summary[label])}")
    return 0


def cmd_szz_eval(args: argparse.Namespace) -> int:
    spec = spec_of(args)
    if spec.fix_source != "regression_message":
        print(f"szz-eval scores SZZ against developer-named commits; {spec.key} has none (its labels come from SZZ).")
        return 1
    repo = open_repo(args)
    ignore_revs = write_ignore_revs(repo, spec.default_branch, args.data_dir / "cache" / spec.key / "ignore-revs")
    with github(args) as gh:
        report = evaluate_szz(repo, gh, spec, ignore_revs_file=ignore_revs, since=args.since, workers=args.workers)
    out = args.data_dir / "reports" / f"szz_{spec.key}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        f"B-SZZ vs developer labels on {report['fixes']} {spec.key} regression fixes "
        f"({report['add_only_fixes']} add-only, invisible to plain B-SZZ)"
    )
    print(f"  {'variant':26} {'level':6} {'recall':>7} {'precision':>9} {'top-1':>6} {'cands/fix':>9}")
    for name, levels in report["variants"].items():
        for level, s in levels.items():
            print(f"  {name:26} {level:6} {s['recall']:7.1%} {s['precision']:9.1%} {s['top1']:6.1%} {s['mean_candidates']:9.2f}")
    print(f"-> {out}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    spec = spec_of(args)
    out = args.data_dir / "verified" / f"{spec.key}.jsonl"
    latest = {(v.case_id, v.fix_commit): v for v in read_jsonl(out, Verification)}
    if args.reclassify:
        return _reclassify(latest.values(), out)
    repo = open_repo(args)
    cases = read_jsonl(args.data_dir / "cases" / f"{spec.key}.jsonl", Case)
    # Infrastructure errors ("error: ...") are retried on every run; real verdicts only with --redo(-verdict).
    done = set() if args.redo else {
        key
        for key, v in latest.items()
        if not v.reason.startswith("error:") and v.verdict not in args.redo_verdict
    }
    todo, count = [], 0
    for case in cases:
        bugs = [
            b
            for b in case.bugs
            if (case.case_id, b.fix_commit) not in done
            and (not args.since or b.fix_date.date() >= args.since)
            and (not args.case or case.case_id in args.case)
        ]
        if args.limit is not None:
            bugs = bugs[: max(args.limit - count, 0)]
        if bugs:
            todo.append(case.model_copy(update={"bugs": bugs}))
            count += len(bugs)
    print(f"Verifying {count} bugs in {len(todo)} cases ({len(done)} already done) -> {out}")
    envs = EnvManager.for_data_dir(args.data_dir)
    verifier = Verifier(repo, spec, envs, args.data_dir / "work", workers=args.workers)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8", newline="\n") as f:

        def sink(v: Verification) -> None:
            f.write(v.model_dump_json() + "\n")
            f.flush()

        verifier.verify_all(todo, sink)
    latest = {(v.case_id, v.fix_commit): v for v in read_jsonl(out, Verification)}
    verdicts = Counter(v.verdict for v in latest.values())
    print(f"All verdicts so far ({len(latest)} bugs): {fmt_counts(dict(verdicts.most_common()))}")
    return 0


def _reclassify(verifications: Iterable[Verification], out: Path) -> int:
    """Apply the current verdict rules to the stored runs, appending only the verdicts that change."""
    verifications = list(verifications)
    changed = []
    for v in verifications:
        if v.fix is None:  # an infrastructure error: nothing ran
            continue
        verdict, reason = classify(v.fix, v.fix_parent, v.head, v.base)
        if (verdict, reason) != (v.verdict, v.reason):
            changed.append((v.verdict, v.model_copy(update={"verdict": verdict, "reason": reason})))
    with out.open("a", encoding="utf-8", newline="\n") as f:
        for _, v in changed:
            f.write(v.model_dump_json() + "\n")
    moves = Counter(f"{old} -> {v.verdict}" for old, v in changed if old != v.verdict)
    print(f"Reclassified {len(verifications)} bugs from their stored runs: {len(changed)} changed. {fmt_counts(dict(moves))}")
    return 0


def cmd_mine_clean(args: argparse.Namespace) -> int:
    spec, repo = spec_of(args), open_repo(args)
    ignore_revs = write_ignore_revs(repo, spec.default_branch, args.data_dir / "cache" / spec.key / "ignore-revs")
    fixes = find_labeled_fixes(repo, spec, None, MineOptions(ignore_revs_file=ignore_revs))
    labeled = {sha for fix in fixes for sha in fix.named}
    with github(args) as gh:
        cases = mine_clean_cases(
            repo, gh, spec, labeled=labeled, since=args.since, until=args.until, count=args.count, survival=args.survival
        )
    out = args.data_dir / "cases" / f"{spec.key}.clean.jsonl"
    write_jsonl(out, cases)
    lines = sorted(c.review.changed_lines for c in cases)
    median = lines[len(lines) // 2] if lines else 0
    print(f"{len(cases)} clean cases (median {median} changed lines) -> {out}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """One dataset holding every frozen repository, a config each."""
    frozen = {key: read_jsonl(args.data_dir / "benchmark" / f"{key}.jsonl", Case) for key in REPOS}
    frozen = {key: cases for key, cases in frozen.items() if cases}
    if not frozen:
        print("No benchmark yet: run `catchrate freeze` first.")
        return 1
    out = args.out / "catch-rate"
    splits: dict[str, list[str]] = {}
    for key, cases in frozen.items():
        for split in SPLITS:  # only the splits a repo has: empty split files trip dataset loaders
            if chosen := [c for c in cases if c.split == split]:
                write_jsonl(out / "data" / key / f"{split}.jsonl", chosen)
                splits.setdefault(key, []).append(split)
    card_path = Path(__file__).resolve().parents[3] / "docs" / "data-card.md"
    card = card_path.read_text(encoding="utf-8") if card_path.exists() else ""
    (out / "README.md").write_text(with_configs(card, hf_configs(splits)), encoding="utf-8")
    print(f"Wrote {sum(len(c) for c in frozen.values())} cases ({', '.join(frozen)}) to {out}. To publish (your call):")
    print(f"  huggingface-cli upload <your-user>/catch-rate {out} --repo-type dataset")
    return 0


def cmd_freeze(args: argparse.Namespace) -> int:
    spec = spec_of(args)
    cases = read_jsonl(args.data_dir / "cases" / f"{spec.key}.jsonl", Case)
    latest = {(v.case_id, v.fix_commit): v for v in read_jsonl(args.data_dir / "verified" / f"{spec.key}.jsonl", Verification)}
    clean = read_jsonl(args.data_dir / "cases" / f"{spec.key}.clean.jsonl", Case)
    frozen = freeze(cases, latest.values(), clean, dev=args.dev, ablation=args.ablation)
    out = args.data_dir / "benchmark" / f"{spec.key}.jsonl"
    write_jsonl(out, frozen)
    splits = Counter(f"{c.kind}/{c.split}" for c in frozen)
    bugs = sum(len(c.bugs) for c in frozen)
    verified = sum(1 for v in latest.values() if v.verdict in VERIFIED)
    print(f"Benchmark: {len(frozen)} cases ({bugs} verified bugs of {verified} verified labels) -> {out}")
    print(f"  splits: {fmt_counts(dict(sorted(splits.items())))}")
    return 0
