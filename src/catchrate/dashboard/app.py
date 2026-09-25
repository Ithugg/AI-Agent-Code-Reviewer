"""Catch Rate dashboard. Launch with `streamlit run src/catchrate/dashboard/app.py`."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pandas as pd
import streamlit as st

from catchrate.dashboard import data
from catchrate.harness.score import scorecard
from catchrate.harness.store import Store
from catchrate.judge.validate import JUDGE_TASK, item_id, judge_agreement, sample_for_labeling
from catchrate.schema import VERIFIED

CASE_TASK = "case"  # label task name for hand-checked verified bugs


def _runs(store: Store | None, repo: str) -> list[dict]:
    return [r for r in store.runs() if r["repo"] == repo] if store else []


def page_overview(root: Path, repo: str) -> None:
    st.subheader("Mining")
    cases = data.load_cases(root, repo)
    if not cases:
        st.info(f"No mined cases yet. Run `catchrate mine` to produce `{data.cases_path(root, repo)}`.")
    else:
        bugs = [b for c in cases for b in c.bugs]
        clean = data.load_clean_cases(root, repo)
        loc = Counter(b.locality for b in bugs)
        cols = st.columns(4)
        cols[0].metric("Cases mined", len(cases))
        cols[1].metric("Bugs mined", len(bugs))
        cols[2].metric("Clean cases", len(clean))
        cols[3].metric("In diff / outside diff", f"{loc.get('in_diff', 0)} / {loc.get('outside_diff', 0)}")
        if not clean:
            st.caption(f"No clean cases yet. Run `catchrate mine-clean` to produce `{data.clean_cases_path(root, repo)}`.")

    st.subheader("Verification")
    verified = data.load_verified(root, repo)
    if not verified:
        st.info(f"No verification data yet. Run `catchrate verify` to produce `{data.verified_path(root, repo)}`.")
    else:
        counts = Counter(v.verdict for v in verified.values())
        st.caption("Verdict counts, latest per bug")
        st.bar_chart(pd.Series(counts, name="bugs").sort_index())

    st.subheader("Benchmark")
    bench = data.load_benchmark(root, repo)
    if not bench:
        st.info(f"No frozen benchmark yet. Run `catchrate freeze` to produce `{data.benchmark_path(root, repo)}`.")
    else:
        counts = Counter(f"{c.kind}/{c.split}" for c in bench)
        st.dataframe(pd.DataFrame({"split": list(counts), "cases": list(counts.values())}), hide_index=True)

    st.subheader("SZZ evaluation")
    szz = data.load_szz(root, repo)
    if not szz:
        st.info(f"No SZZ evaluation yet. Run `catchrate szz-eval` to produce `{data.szz_path(root, repo)}`.")
    else:
        rows = [{"variant": name, "level": level, **stats} for name, levels in szz["variants"].items() for level, stats in levels.items()]
        st.dataframe(pd.DataFrame(rows), hide_index=True)


def page_runs(root: Path, repo: str, store: Store | None) -> None:
    if store is None:
        st.info(f"No results yet. Run `catchrate run` to produce `{data.store_path(root)}`.")
        return
    runs = _runs(store, repo)
    if not runs:
        st.info(f"No runs recorded for {repo} yet. Run `catchrate --repo {repo} run` to start one.")
        return

    cases = {c.case_id: c for c in data.load_benchmark(root, repo)}
    cards = [scorecard(run, store.reviews(run["run_id"]), store.matches(run["run_id"]), cases) for run in runs]
    rows = [
        {
            "run_id": c.run_id, "version": c.version, "model": c.model, "split": c.split,
            "catch_rate": c.catch_rate, "verified_catch_rate": c.verified_catch_rate, "precision": c.precision,
            "false_alarms_per_clean_pr": c.false_alarms_per_clean_pr, "mean_cost_usd": c.mean_cost_usd,
            "mean_seconds": c.mean_seconds, "errors": c.errors,
        }
        for c in cards
    ]  # fmt: skip
    df = pd.DataFrame(rows)
    st.dataframe(df, hide_index=True)

    st.subheader("Catch rate vs. mean cost per review")
    cost_df = df.dropna(subset=["mean_cost_usd"])
    if cost_df.empty:
        st.caption("No cost data yet.")
    else:
        st.scatter_chart(cost_df, x="mean_cost_usd", y="catch_rate", color="run_id")


def page_case_browser(root: Path, repo: str, store: Store | None) -> None:
    cases = data.load_cases(root, repo)
    if not cases:
        st.info(f"No mined cases yet. Run `catchrate mine` to produce `{data.cases_path(root, repo)}`.")
        return
    verified = data.load_verified(root, repo)
    options = {f"{c.case_id} — {c.review.title}": c for c in cases}
    case = options[st.selectbox("Case", sorted(options), key="case_browser_select")]

    st.markdown(f"#### {case.review.title}")
    st.caption(f"{case.case_id} · {case.review.pr_url or case.review.head_commit}")
    st.text(case.review.description or "(no description)")
    st.code(case.review.diff, language="diff")

    st.markdown("##### Bugs")
    for bug in case.bugs:
        v = verified.get((case.case_id, bug.fix_commit))
        verdict = v.verdict if v else (bug.verdict or "not verified yet")
        with st.expander(f"{bug.bug_summary} ({verdict})"):
            st.write(f"Locality: {bug.locality}")
            st.write(f"Fix commit: {bug.fix_commit}")
            st.write(f"Fix tests: {', '.join(bug.fix_tests) or 'none'}")
            if bug.bug_lines:
                st.dataframe(pd.DataFrame(bl.model_dump() for bl in bug.bug_lines), hide_index=True)
            else:
                st.caption(f"No located bug lines ({bug.unlocated_lines} unlocated).")

    runs = _runs(store, repo)
    if store is None or not runs:
        st.caption("No runs yet: run `catchrate run` to see agent findings for this case.")
        return

    run_id = st.selectbox("Run", [r["run_id"] for r in runs], key="case_browser_run")
    review = next((r for r in store.reviews(run_id) if r["case_id"] == case.case_id), None)
    if review is None:
        st.caption("This run has no review recorded for this case.")
        return

    st.markdown("##### Findings")
    if not review["findings"]:
        st.caption("No findings.")
    for i, f in enumerate(review["findings"]):
        st.write(f"**{i}.** `{f['file']}:{f['start_line']}-{f['end_line']}` [{f['severity']}] {f['explanation']}")
        if f.get("evidence"):
            st.caption(f"Evidence ({'failed' if f['evidence']['failed'] else 'passed'}): {f['evidence']['test_id']}")

    st.markdown("##### Which findings matched which bug")
    matches = [m for m in store.matches(run_id) if m["case_id"] == case.case_id]
    if matches:
        st.dataframe(pd.DataFrame(matches)[["fix_commit", "finding_index", "location_ok", "same_bug", "reason"]], hide_index=True)
    else:
        st.caption("No judged matches for this case yet. Run `catchrate judge`.")

    with st.expander("Trace"):
        for i, turn in enumerate(review["trace"]):
            st.write(f"Turn {i} ({turn.get('stop_reason')}): {turn.get('text') or '(no text)'}")
            for call in turn.get("tool_calls", []):
                st.code(json.dumps(call, indent=2), language="json")
            for r in turn.get("results", []):
                st.caption(f"tool result ({'ok' if r['ok'] else 'error'}): {r['content'][:200]}")


def page_label(root: Path, repo: str, store: Store | None) -> None:
    # Hand-checking verified bugs needs no agent runs, so labeling may create the store.
    store = store or Store(data.store_path(root))
    tab_judge, tab_case = st.tabs(["Judge decisions", "Verified bugs"])
    with tab_judge:
        _label_judge(root, repo, store)
    with tab_case:
        _label_case(root, repo, store)


def _label_judge(root: Path, repo: str, store: Store) -> None:
    all_matches = [m for run in _runs(store, repo) for m in store.matches(run["run_id"])]
    sample = sample_for_labeling(all_matches)
    if not sample:
        st.info("No judged matches yet. Run `catchrate judge --run <run_id>` first.")
        return
    labels = store.labels(JUDGE_TASK)
    st.write(f"Progress: {len(labels)}/{len(sample)} labeled")
    agree = judge_agreement(all_matches, labels)
    if agree.n:
        st.write(f"Judge agreement: {agree.accuracy:.1%} (kappa {agree.kappa:.2f}) over {agree.n} labeled pairs")

    cases = {c.case_id: c for c in data.load_cases(root, repo)}
    reviews_by_run: dict[str, dict[str, dict]] = {}
    buttons = (("same bug", "same"), ("different", "different"), ("unsure", "unsure"))
    for m in sample:
        iid = item_id(m["run_id"], m["case_id"], m["fix_commit"], m["finding_index"])
        if m["run_id"] not in reviews_by_run:
            reviews_by_run[m["run_id"]] = {r["case_id"]: r for r in store.reviews(m["run_id"])}
        review = reviews_by_run[m["run_id"]].get(m["case_id"])
        findings = review["findings"] if review else []
        finding = findings[m["finding_index"]] if m["finding_index"] < len(findings) else None
        case = cases.get(m["case_id"])
        bug = next((b for b in case.bugs if b.fix_commit == m["fix_commit"]), None) if case else None

        title = f"{m['case_id']} · finding {m['finding_index']}" + (" (labeled)" if iid in labels else "")
        with st.expander(title):
            if bug:
                st.write(f"Known bug: {bug.bug_summary}")
                st.caption("Bug lines: " + ", ".join(f"{bl.path}:{bl.line}" for bl in bug.bug_lines))
            if finding:
                st.write(f"Finding: `{finding['file']}:{finding['start_line']}-{finding['end_line']}` — {finding['explanation']}")
            st.caption(f"Judge said same_bug={m['same_bug']}: {m['reason']}")
            cols = st.columns(3)
            for col, (btn_label, label_value) in zip(cols, buttons):
                if col.button(btn_label, key=f"judge-{label_value}-{iid}"):
                    store.save_label(JUDGE_TASK, iid, label_value)


def _label_case(root: Path, repo: str, store: Store) -> None:
    verified = data.load_verified(root, repo)
    entries = sorted((key, v) for key, v in verified.items() if v.verdict in VERIFIED)
    if not entries:
        st.info(f"No verified bugs yet. Run `catchrate verify` to produce `{data.verified_path(root, repo)}`.")
        return
    labels = store.labels(CASE_TASK)
    st.write(f"Progress: {len(labels)}/{len(entries)} labeled")

    cases = {c.case_id: c for c in data.load_cases(root, repo)}
    buttons = (("label correct", "correct"), ("label wrong", "wrong"), ("unsure", "unsure"))
    for (case_id, fix_commit), v in entries:
        label_key = f"{case_id}|{fix_commit}"
        case = cases.get(case_id)
        bug = next((b for b in case.bugs if b.fix_commit == fix_commit), None) if case else None

        title = f"{case_id} · {fix_commit[:10]} ({v.verdict})" + (" (labeled)" if label_key in labels else "")
        with st.expander(title):
            if case:
                st.code(case.review.diff, language="diff")
            else:
                st.caption("Case not found in mined cases.")
            if bug:
                st.caption("Bug lines: " + ", ".join(f"{bl.path}:{bl.line}" for bl in bug.bug_lines))
            for name, state in (("fix", v.fix), ("fix_parent", v.fix_parent), ("head", v.head), ("base", v.base)):
                if state is None:
                    st.write(f"{name}: (not run)")
                else:
                    sigs = "; ".join(f"{k}: {val}" for k, val in state.signatures.items())
                    st.write(f"{name}: {state.status}" + (f" — {sigs}" if sigs else ""))
            cols = st.columns(3)
            for col, (btn_label, label_value) in zip(cols, buttons):
                if col.button(btn_label, key=f"case-{label_value}-{label_key}"):
                    store.save_label(CASE_TASK, label_key, label_value)


def main() -> None:
    st.set_page_config(page_title="Catch Rate", layout="wide")
    root = data.data_dir()
    repos = data.available_repos(root)
    repo = st.sidebar.selectbox("Repository", repos, format_func=data.project) if len(repos) > 1 else repos[0]
    st.title(f"Catch Rate — {data.project(repo)}")
    store = data.open_store(root)

    tab_overview, tab_runs, tab_cases, tab_label = st.tabs(["Overview", "Runs", "Case browser", "Label"])
    with tab_overview:
        page_overview(root, repo)
    with tab_runs:
        page_runs(root, repo, store)
    with tab_cases:
        page_case_browser(root, repo, store)
    with tab_label:
        page_label(root, repo, store)


main()
