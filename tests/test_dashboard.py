"""Headless tests for the Streamlit dashboard, via streamlit.testing.v1.AppTest."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from streamlit.testing.v1 import AppTest

from catchrate.dashboard import data
from catchrate.harness.store import Store
from catchrate.judge.validate import JUDGE_TASK, item_id
from catchrate.schema import BugLabel, BugLine, Case, ReviewInput, StateResult, Verification

APP = str(Path(__file__).parent.parent / "src" / "catchrate" / "dashboard" / "app.py")
FIX_COMMIT = "f" * 40


def _review() -> ReviewInput:
    return ReviewInput(
        review_unit="pr", base_commit="b" * 40, head_commit="h" * 40, commits=["h" * 40], pr_number=1,
        pr_url="https://example.com/pr/1", title="Fix off-by-one in calc", description="Some description.",
        diff="--- a/pkg/calc.py\n+++ b/pkg/calc.py\n@@ -1,2 +1,2 @@\n-    return x - 1\n+    return x + 1\n",
        changed_files=["pkg/calc.py"], changed_lines=2,
    )  # fmt: skip


def _bug() -> BugLabel:
    return BugLabel(
        label_source="developer", introducing_commit="i" * 40, fix_commit=FIX_COMMIT,
        fix_date=datetime(2025, 1, 1, tzinfo=timezone.utc), fix_subject="Fixed the bug", tickets=[1],
        bug_summary="inc() subtracts instead of adding", bug_summary_source="fix_subject", locality="in_diff",
        bug_files=["pkg/calc.py"], bug_lines=[BugLine(path="pkg/calc.py", line=2, origin="deleted", in_diff=True)],
        unlocated_lines=0, fix_tests=["calc.tests.T.test_inc"],
    )  # fmt: skip


def _verification() -> Verification:
    state = StateResult(commit="x" * 40, django="5.2", python="3.12", patch="applied", status="pass",
                        outcomes={"calc.tests.T.test_inc": "ok"}, signatures={}, load_errors=[], seconds=1.0)  # fmt: skip
    return Verification(case_id="c1", fix_commit=FIX_COMMIT, introducing_commit="i" * 40, verdict="verified",
                        reason="fails before the change, passes at the fix", fix=state, fix_parent=state,
                        head=state, base=state)  # fmt: skip


def _write_jsonl(path: Path, records: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(r.model_dump_json() + "\n")


def _write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _build_data_dir(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    case = Case(case_id="c1", repo="django", review=_review(), bugs=[_bug()])
    _write_jsonl(data.cases_path(root, "django"), [case])
    _write_jsonl(data.verified_path(root, "django"), [_verification()])
    _write_json(data.summary_path(root, "django"), {"cases": 1, "bugs": 1})
    _write_json(data.szz_path(root, "django"), {
        "variants": {"bszz": {"commit": {"recall": 0.5, "precision": 0.4, "top1": 0.3,
                                          "mean_candidates": 1.2, "fixes": 10, "with_candidates": 5}}}
    })  # fmt: skip
    frozen = case.model_copy(update={"split": "dev", "bugs": [b.model_copy(update={"verdict": "verified"}) for b in case.bugs]})
    _write_jsonl(data.benchmark_path(root, "django"), [frozen])

    store = Store(data.store_path(root))
    store.create_run("run1", repo="django", version="v1", model="claude-sonnet-5", split="dev", seed=0,
                     config={}, benchmark_sha=None)  # fmt: skip
    store.save_review("run1", "c1", "buggy", {
        "findings": [{"file": "pkg/calc.py", "start_line": 2, "end_line": 2, "severity": "major", "category": "logic",
                      "explanation": "inc(1) returns 0.", "confidence": 0.9, "evidence": None}],
        "input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 0, "cache_write_tokens": 0,
        "cost_usd": 0.01, "seconds": 1.5, "tool_calls": 0, "turns": 1, "stop_reason": "end_turn", "error": None,
        "trace": [{"text": "Looking at calc.py", "tool_calls": [], "stop_reason": "end_turn", "usage": {}}],
    })  # fmt: skip
    store.save_match("run1", "c1", FIX_COMMIT, 0, location_ok=True, same_bug=True, reason="same defect",
                     judge_model="claude-opus-5-5")  # fmt: skip
    store.close()
    return root


def test_dashboard_renders_all_tabs_with_data(tmp_path, monkeypatch):
    root = _build_data_dir(tmp_path)
    monkeypatch.setenv("CATCHRATE_DATA", str(root))
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    # 4 top-level pages plus the "Label" page's 2 nested tabs.
    assert {t.label for t in at.tabs} == {
        "Overview", "Runs", "Case browser", "Label", "Judge decisions", "Verified bugs",
    }


def test_dashboard_renders_with_empty_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CATCHRATE_DATA", str(tmp_path / "nowhere"))
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception


def test_label_judge_decision_saves_to_store(tmp_path, monkeypatch):
    root = _build_data_dir(tmp_path)
    monkeypatch.setenv("CATCHRATE_DATA", str(root))
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception

    same_bug = [b for b in at.button if b.label == "same bug"]
    assert len(same_bug) == 1
    same_bug[0].click().run()
    assert not at.exception

    store = Store(data.store_path(root))
    labels = store.labels(JUDGE_TASK)
    store.close()
    iid = item_id("run1", "c1", FIX_COMMIT, 0)
    assert labels[iid]["label"] == "same"


def test_label_case_decision_saves_to_store(tmp_path, monkeypatch):
    root = _build_data_dir(tmp_path)
    monkeypatch.setenv("CATCHRATE_DATA", str(root))
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception

    correct = [b for b in at.button if b.label == "label correct"]
    assert len(correct) == 1
    correct[0].click().run()
    assert not at.exception

    store = Store(data.store_path(root))
    labels = store.labels("case")  # matches CASE_TASK in dashboard/app.py
    store.close()
    assert labels[f"c1|{FIX_COMMIT}"]["label"] == "correct"


def test_repository_picker_switches_every_page_to_that_repo(tmp_path, monkeypatch):
    root = _build_data_dir(tmp_path)
    other = Case(case_id="pytest-pr9", repo="pytest", review=_review(), bugs=[_bug()])
    _write_jsonl(data.cases_path(root, "pytest"), [other])
    monkeypatch.setenv("CATCHRATE_DATA", str(root))
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert at.title[0].value == "Catch Rate — Django"

    at.sidebar.selectbox[0].select("pytest").run()

    assert not at.exception
    assert at.title[0].value == "Catch Rate — pytest"
    assert any("No runs recorded for pytest" in i.value for i in at.info)  # django's run1 is not shown
    assert not [b for b in at.button if b.label == "same bug"]
