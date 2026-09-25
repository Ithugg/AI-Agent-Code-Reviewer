"""Agent commands: estimate, run, judge, score, judge-agreement, sandbox-check."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from catchrate.agent.execution import DockerSandbox, docker_available
from catchrate.agent.findings import Finding
from catchrate.agent.versions import resolve
from catchrate.cli.common import open_repo, read_jsonl, spec_of
from catchrate.harness.estimate import estimate
from catchrate.harness.runner import RunSettings, ToolboxProvider, run_cases, session_factory
from catchrate.harness.score import aggregate, scorecard
from catchrate.harness.store import Store
from catchrate.judge.judge import LLMJudge, match_review
from catchrate.judge.validate import JUDGE_TASK, judge_agreement
from catchrate.schema import BugLabel, Case
from catchrate.verify.envs import EnvManager


def register(commands: argparse._SubParsersAction) -> None:
    est = commands.add_parser("estimate", help="estimate what a run would cost, without calling any API")
    _selection(est)
    est.add_argument("--batch", action="store_true", help="price at Batch API rates (single-call versions)")
    est.set_defaults(func=cmd_estimate)

    run = commands.add_parser("run", help="review benchmark cases with an agent version (spends money)")
    _selection(run)
    run.add_argument("--max-usd", type=float, required=True, help="hard spending cap for this run")
    run.add_argument("--seed", type=int, default=0, help="repeat index; run each configuration 3 times")
    run.add_argument("--run-id", help="resume this run instead of starting a new one")
    run.add_argument("--workers", type=int, default=4)
    run.set_defaults(func=cmd_run)

    judge = commands.add_parser("judge", help="match a run's findings to known bugs with an LLM judge (spends money)")
    judge.add_argument("--run", required=True, dest="run_id")
    judge.add_argument("--judge-model", default="claude-opus-5-5")
    judge.add_argument("--window", type=int, default=5, help="line window for the location check")
    judge.set_defaults(func=cmd_judge)

    score = commands.add_parser("score", help="print scorecards for judged runs")
    score.add_argument("--run", action="append", dest="run_ids", default=[], help="runs to score (default: all)")
    score.set_defaults(func=cmd_score)

    agree = commands.add_parser("judge-agreement", help="how often the judge agrees with your hand labels")
    agree.set_defaults(func=cmd_agreement)

    check = commands.add_parser("sandbox-check", help="check Docker and build the sandbox image for agent-written tests")
    check.add_argument("--python", default="3.12")
    check.set_defaults(func=cmd_sandbox_check)

    pr = commands.add_parser("review-pr", help="review a live GitHub PR (spends money); --post publishes the review")
    pr.add_argument("target", help="owner/repo#number")
    pr.add_argument("--version", default="v1", help="v0 or v1 (read-only)")
    pr.add_argument("--model", default="claude-sonnet-5")
    pr.add_argument("--project", default="", help="project name for the prompt (default: the repo name)")
    pr.add_argument("--min-confidence", type=float, default=0.5, help="only post findings at least this confident")
    pr.add_argument("--post", action="store_true", help="publish the review on the PR (needs a token with write access)")
    pr.set_defaults(func=cmd_review_pr)


def _selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--version", required=True, help='v0-v4, optionally with knobs: "v1:calls=10,effort=low"')
    parser.add_argument("--model", required=True, help="claude-opus-5-5, claude-sonnet-5, ... or openai:<name>")
    parser.add_argument("--split", choices=["dev", "ablation", "test"], help="benchmark split (default: all cases)")
    parser.add_argument("--limit", type=int, help="only the first N cases of the split")


def _benchmark(args: argparse.Namespace) -> tuple[Path, list[Case]]:
    path = args.data_dir / "benchmark" / f"{spec_of(args).key}.jsonl"
    if not path.exists():
        sys.exit(f"{path} is missing: run `catchrate verify` and `catchrate freeze` first")
    cases = [c for c in read_jsonl(path, Case) if args.split is None or c.split == args.split]
    return path, cases[: args.limit] if args.limit else cases


def _store(args: argparse.Namespace) -> Store:
    return Store(args.data_dir / "results.db")


def cmd_estimate(args: argparse.Namespace) -> int:
    _, cases = _benchmark(args)
    version = resolve(args.version)
    est = estimate(cases, version, args.model, spec_of(args).project, batch=args.batch)
    u = est.usage
    print(f"{version.name} on {args.model}, {est.cases} cases ({args.split or 'all splits'}):")
    print(f"  ~{u.input_tokens:,} input, {u.cache_write_tokens:,} cache-write, {u.cache_read_tokens:,} cache-read, {u.output_tokens:,} output tokens")
    if est.cost_usd is None:
        print("  no price known for this model")
    else:
        print(f"  ~${est.cost_usd:,.2f} total, ~${est.per_case_usd:,.3f} per review (rough; smoke-test a few cases first)")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    if args.model.startswith("claude-") and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set: put ANTHROPIC_API_KEY=... in .env in the project folder")
    spec, repo = spec_of(args), open_repo(args)
    bench_path, cases = _benchmark(args)
    version = resolve(args.version)
    store = _store(args)
    run_id = args.run_id or _run_id(version.name, args.model, args.split, args.seed)
    if store.run(run_id) is None:
        store.create_run(
            run_id, repo=spec.key, version=version.name, model=args.model, split=args.split, seed=args.seed,
            config={"version": vars(version), "max_usd": args.max_usd, "limit": args.limit},
            benchmark_sha=hashlib.sha256(bench_path.read_bytes()).hexdigest(),
        )  # fmt: skip
    envs = EnvManager.for_data_dir(args.data_dir) if "run_tests" in version.tools else None
    toolboxes = ToolboxProvider(repo, version, spec=spec, envs=envs, work_dir=args.data_dir / "work", workers=args.workers)
    settings = RunSettings(version, args.model, spec.project, max_usd=args.max_usd, workers=args.workers)
    print(f"Run {run_id}: {len(cases)} cases, spending cap ${args.max_usd:.2f}")
    summary = run_cases(run_id, cases, settings, store, repo, session_factory(args.model), toolboxes)
    print(f"Done: {summary}. Next: catchrate judge --run {run_id}")
    return 0


def cmd_judge(args: argparse.Namespace) -> int:
    if args.judge_model.startswith("claude-") and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set: put ANTHROPIC_API_KEY=... in .env in the project folder")
    spec, repo, store = spec_of(args), open_repo(args), _store(args)
    cases = {c.case_id: c for c in read_jsonl(args.data_dir / "benchmark" / f"{spec.key}.jsonl", Case)}

    def fix_diff(bug: BugLabel) -> str:
        parent = repo.commits([bug.fix_commit])[bug.fix_commit].parents[0]
        text = repo.run("diff", "--no-ext-diff", parent, bug.fix_commit, "--", *spec.source_prefixes)
        return text[:8000] + ("\n[diff truncated]" if len(text) > 8000 else "")

    def code_at(case: Case):
        def lines(path: str, start: int, end: int) -> str:
            text = repo.show_file(case.review.head_commit, path) or ""
            return "\n".join(text.splitlines()[max(start - 1, 0) : end][:40])

        return lines

    judge = LLMJudge(session_factory(args.judge_model, max_tokens=4000), args.judge_model, fix_diff, code_at)
    judged = 0
    for review in store.reviews(args.run_id):
        case = cases.get(review["case_id"])
        if case is None or review["error"] or case.kind == "clean":
            continue
        findings = [Finding.model_validate(f) for f in review["findings"]]
        for m in match_review(case, findings, judge, window=args.window):
            store.save_match(args.run_id, case.case_id, m.fix_commit, m.finding_index, location_ok=m.location_ok,
                             same_bug=m.same_bug, reason=m.reason, judge_model=args.judge_model)  # fmt: skip
            judged += 1
    cost = judge.cost_usd
    print(f"Judged {judged} (bug, finding) pairs" + (f" for ${cost:.2f}" if cost is not None else "") + f". Next: catchrate score --run {args.run_id}")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    store, spec = _store(args), spec_of(args)
    cases = {c.case_id: c for c in read_jsonl(args.data_dir / "benchmark" / f"{spec.key}.jsonl", Case)}
    runs = [r for r in store.runs() if not args.run_ids or r["run_id"] in args.run_ids]
    if not runs:
        print("No runs yet. Start one with `catchrate run`.")
        return 0
    cards = [scorecard(run, store.reviews(run["run_id"]), store.matches(run["run_id"]), cases) for run in runs]
    print(f"{'run':44} {'catch':>6} {'verif':>6} {'prec':>6} {'FA/clean':>8} {'$/review':>8} {'sec':>6} {'errors':>6}")
    for c in cards:
        cost = f"{c.mean_cost_usd:.3f}" if c.mean_cost_usd is not None else "-"
        print(f"{c.run_id[:44]:44} {c.catch_rate:6.1%} {c.verified_catch_rate:6.1%} {c.precision:6.1%} "
              f"{c.false_alarms_per_clean_pr:8.2f} {cost:>8} {c.mean_seconds:6.0f} {c.errors:6d}")  # fmt: skip
    groups = aggregate(cards)
    if any(stats.get("catch_rate", (0, 0, 0))[2] > 1 for stats in groups.values()):
        print("\nAcross seeds (mean ± sd):")
        for (version, model, split), stats in groups.items():
            mean, sd, n = stats["catch_rate"]
            print(f"  {version} / {model} / {split or 'all'}: catch rate {mean:.1%} ± {sd:.1%} over {n} runs")
    return 0


def cmd_agreement(args: argparse.Namespace) -> int:
    store = _store(args)
    matches = [m for run in store.runs() for m in store.matches(run["run_id"])]
    result = judge_agreement(matches, store.labels(JUDGE_TASK))
    if result.n == 0:
        print("No hand labels yet: label judge decisions in the dashboard (Label tab) first.")
        return 0
    print(f"Judge vs you on {result.n} pairs: {result.accuracy:.1%} agreement, Cohen's kappa {result.kappa:.2f}")
    print(f"  both same {result.both_yes}, only judge said same {result.judge_only}, "
          f"only you said same {result.human_only}, both different {result.both_no}")  # fmt: skip
    return 0


def cmd_sandbox_check(args: argparse.Namespace) -> int:
    if not docker_available():
        print("Docker is not available. Install Docker Desktop (with the WSL2 backend), start it, then re-run this.")
        return 1
    sandbox = DockerSandbox(args.data_dir / "work", args.python)
    sandbox.build_image()
    print(f"Docker works and {sandbox.image} is built. v3 and v4 can now write and run tests.")
    return 0


def cmd_review_pr(args: argparse.Namespace) -> int:
    from catchrate.agent.pricing import cost_usd
    from catchrate.cli.common import github
    from catchrate.review_pr import build_review, fetch_head, fetch_pull_request, parse_target, post_review, review_pull_request

    if args.model.startswith("claude-") and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set: put ANTHROPIC_API_KEY=... in .env or the environment")
    owner, name, number = parse_target(args.target)
    version = resolve(args.version)
    with github(args) as gh:
        pr = fetch_pull_request(gh, owner, name, number)
        repo = fetch_head(args.data_dir / "repos" / f"{owner}__{name}.pr.git", pr)
        result = review_pull_request(pr, repo, version, session_factory(args.model), args.project or name)
        cost = cost_usd(args.model, result.usage)
        signature = f"Catch Rate {version.name} · {args.model}" + (f" · ${cost:.2f}" if cost is not None else "")
        payload = build_review(result.findings, pr.review.diff, signature=signature, min_confidence=args.min_confidence)
        print(payload["body"])
        for c in payload["comments"]:
            print(f"\n{c['path']}:{c['line']}\n{c['body']}")
        if args.post:
            print(f"\nPosted: {post_review(gh, pr, payload)}")
        else:
            print("\n(dry run: add --post to publish this review on the PR)")
    return 0


def _run_id(version: str, model: str, split: str | None, seed: int) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return re.sub(r"[^\w.-]+", "_", f"{version}-{model}-{split or 'all'}-s{seed}-{stamp}")
