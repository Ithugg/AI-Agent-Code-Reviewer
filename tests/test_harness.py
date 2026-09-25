import json
from datetime import datetime, timezone

import pytest

from catchrate.agent.findings import Finding
from catchrate.agent.llm import ScriptedSession, Turn, Usage
from catchrate.agent.versions import VERSIONS, resolve
from catchrate.harness.estimate import estimate
from catchrate.harness.runner import RunSettings, ToolboxProvider, run_cases
from catchrate.harness.score import scorecard
from catchrate.harness.store import Store
from catchrate.judge.judge import Decision, location_ok, match_review
from catchrate.judge.validate import agreement, item_id, judge_agreement, sample_for_labeling
from catchrate.schema import BugLabel, BugLine, Case, ReviewInput

FINDING = {"file": "pkg/calc.py", "start_line": 2, "end_line": 2, "severity": "major", "category": "logic",
           "explanation": "inc(1) returns 0.", "confidence": 0.9}  # fmt: skip


def _review(head: str, files=("pkg/calc.py",)) -> ReviewInput:
    return ReviewInput(review_unit="pr", base_commit="b" * 40, head_commit=head, commits=[head], pr_number=1,
                       pr_url=None, title="Changed inc", description="", diff="--- a\n+++ b\n", changed_files=list(files),
                       changed_lines=2)  # fmt: skip


def _bug(fix: str, locality="in_diff", path="pkg/calc.py", line=2, date="2025-01-01") -> BugLabel:
    return BugLabel(label_source="developer", introducing_commit="i" * 40, fix_commit=fix,
                    fix_date=datetime.fromisoformat(date).replace(tzinfo=timezone.utc), fix_subject="Fixed #1",
                    tickets=[1], bug_summary="inc() subtracts", bug_summary_source="fix_subject", locality=locality,
                    bug_files=[path], bug_lines=[BugLine(path=path, line=line, origin="deleted", in_diff=locality == "in_diff")],
                    unlocated_lines=0, fix_tests=["calc.tests.T.test_inc"], verdict="verified")  # fmt: skip


def test_run_cases_records_reviews_and_stops_at_the_spend_cap(toy, tmp_path):
    head = toy.commit("Initial", {"pkg/calc.py": "def inc(x):\n    return x - 1\n"})
    cases = [Case(case_id=f"c{i}", repo="toy", review=_review(head), bugs=[_bug("f" * 40)]) for i in range(3)]
    store = Store(tmp_path / "results.db")
    store.create_run("r1", repo="toy", version="v0", model="claude-sonnet-5", split=None, seed=0, config={}, benchmark_sha=None)
    # Each review "costs" $3 on Sonnet 5 prices, so a $5 cap stops after the second.
    reply = Turn(json.dumps({"findings": [FINDING]}), [], "end_turn", Usage(input_tokens=1_000_000, output_tokens=100_000))
    sessions = lambda system, tools, schema, effort: ScriptedSession([reply], model="claude-sonnet-5")  # noqa: E731
    settings = RunSettings(VERSIONS["v0"], "claude-sonnet-5", "Toy", max_usd=5.0, workers=1)

    summary = run_cases("r1", cases, settings, store, toy.repo, sessions, ToolboxProvider(toy.repo, VERSIONS["v0"]))

    assert summary == {"reviewed": 2, "errors": 0, "spent_usd": pytest.approx(6.0), "stopped_early": True}
    (first, _) = store.reviews("r1")
    assert first["findings"][0]["explanation"] == "inc(1) returns 0." and first["cost_usd"] == pytest.approx(3.0)


def test_run_cases_records_session_errors_and_keeps_going(toy, tmp_path):
    head = toy.commit("Initial", {"pkg/calc.py": "x = 1\n"})
    cases = [Case(case_id=f"c{i}", repo="toy", review=_review(head), bugs=[]) for i in range(2)]
    store = Store(tmp_path / "results.db")
    store.create_run("r2", repo="toy", version="v1", model="claude-sonnet-5", split=None, seed=0, config={}, benchmark_sha=None)

    def sessions(system, tools, schema, effort):
        raise ConnectionError("API unreachable")

    summary = run_cases("r2", cases, RunSettings(VERSIONS["v1"], "claude-sonnet-5", "Toy", workers=1), store, toy.repo,
                        sessions, ToolboxProvider(toy.repo, VERSIONS["v1"]))  # fmt: skip

    assert summary["errors"] == 2
    assert store.reviewed_cases("r2") == set()  # errored cases are retried when the run resumes


def test_estimate_grows_with_tool_rounds():
    cases = [Case(case_id="c", repo="toy", review=_review("h" * 40), bugs=[])]
    single = estimate(cases, VERSIONS["v0"], "claude-sonnet-5", "Toy")
    agentic = estimate(cases, VERSIONS["v1"], "claude-sonnet-5", "Toy")
    short = estimate(cases, resolve("v1:calls=5"), "claude-sonnet-5", "Toy")
    assert single.cost_usd < short.cost_usd < agentic.cost_usd
    assert estimate(cases, VERSIONS["v0"], "claude-sonnet-5", "Toy", batch=True).cost_usd == pytest.approx(single.cost_usd / 2)


def test_location_check_and_matching():
    case = Case(case_id="c", repo="toy", review=_review("h" * 40, files=("pkg/calc.py", "pkg/use.py")),
                bugs=[_bug("a" * 40), _bug("b" * 40, locality="outside_diff", path="pkg/old.py", line=50)])  # fmt: skip
    near = Finding(**{**FINDING, "start_line": 6, "end_line": 7})
    far = Finding(**{**FINDING, "start_line": 9, "end_line": 9})
    in_changed_file = Finding(**{**FINDING, "file": "pkg/use.py", "start_line": 1, "end_line": 1})
    assert location_ok(near, case.bugs[0], case) and not location_ok(far, case.bugs[0], case)
    assert location_ok(in_changed_file, case.bugs[1], case) and not location_ok(in_changed_file, case.bugs[0], case)

    class Judge:
        model = "fake"

        def decide(self, case, bug, finding):
            return Decision("inc" in finding.explanation, "same function")

    records = match_review(case, [near, far, in_changed_file], Judge())
    assert [(r.fix_commit[0], r.finding_index, r.same_bug) for r in records] == [("a", 0, True), ("b", 0, True), ("b", 1, True), ("b", 2, True)]


def test_scorecard_metrics():
    bugs = {"A": _bug("A" * 40), "B": _bug("B" * 40, locality="outside_diff", date="2026-08-01"), "C": _bug("C" * 40)}
    cases = {
        "c1": Case(case_id="c1", repo="toy", review=_review("1" * 40), bugs=[bugs["A"], bugs["B"]]),
        "c2": Case(case_id="c2", repo="toy", review=_review("2" * 40), bugs=[bugs["C"]]),
        "k1": Case(case_id="k1", repo="toy", kind="clean", review=_review("3" * 40), bugs=[]),
    }
    verified = {**FINDING, "evidence": {"test_id": "t1", "test_code": "...", "output": "FAIL", "failed": True}}

    def review(case_id, findings, cost):
        return {"case_id": case_id, "findings": findings, "error": None, "cost_usd": cost, "seconds": 10.0, "tool_calls": 4}

    reviews = [review("c1", [verified, FINDING], 0.5), review("c2", [FINDING], 0.3), review("k1", [FINDING, FINDING], 0.2)]
    matches = [
        {"case_id": "c1", "fix_commit": "A" * 40, "finding_index": 0, "location_ok": 1, "same_bug": 1},
        {"case_id": "c1", "fix_commit": "B" * 40, "finding_index": 1, "location_ok": 1, "same_bug": 0},
        {"case_id": "c2", "fix_commit": "C" * 40, "finding_index": 0, "location_ok": 1, "same_bug": 1},
    ]
    run = {"run_id": "r", "version": "v1", "model": "claude-sonnet-5", "split": "dev"}

    card = scorecard(run, reviews, matches, cases)

    assert (card.bugs, card.caught, card.buggy_cases, card.clean_cases) == (3, 2, 2, 1)
    assert card.catch_rate == pytest.approx(2 / 3) and card.case_catch_rate == 1.0
    assert card.verified_catch_rate == pytest.approx(1 / 3)
    assert card.precision == pytest.approx(2 / 5)
    assert (card.false_alarms_per_clean_pr, card.false_alarms_per_buggy_pr) == (2.0, 0.5)
    assert card.by_locality == {"in_diff": 1.0, "outside_diff": 0.0}
    assert card.after_training_cutoff == {"bugs": 1, "catch_rate": 0.0}  # B was fixed after Sonnet 5's cutoff
    assert card.mean_cost_usd == pytest.approx(1.0 / 3)


def test_agreement_and_labeling_sample(tmp_path):
    result = agreement([(True, True), (True, False), (False, False), (False, False)])
    assert (result.n, result.accuracy, result.judge_only) == (4, 0.75, 1)
    assert result.kappa == pytest.approx(0.5)

    matches = [{"run_id": "r", "case_id": f"c{i}", "fix_commit": "f", "finding_index": 0, "same_bug": i % 3 == 0} for i in range(30)]
    sample = sample_for_labeling(matches, k=10)
    assert len(sample) == 10 and sum(m["same_bug"] for m in sample) == 5

    store = Store(tmp_path / "db.sqlite")
    store.save_label("judge", item_id("r", "c0", "f", 0), "same")
    store.save_label("judge", item_id("r", "c3", "f", 0), "different")
    assert judge_agreement(matches, store.labels("judge")).n == 2
