import argparse
from datetime import datetime, timezone

from catchrate.benchmark import freeze, hf_configs, with_configs
from catchrate.cli.common import read_jsonl, write_jsonl
from catchrate.cli.data import cmd_export
from catchrate.schema import BugLabel, BugLine, Case, ReviewInput, Verification


def _review(head: str) -> ReviewInput:
    return ReviewInput(
        review_unit="pr", base_commit="b" * 40, head_commit=head, commits=[head], pr_number=1, pr_url=None,
        title="t", description="", diff="", changed_files=["f.py"], changed_lines=2,
    )


def _bug(fix: str) -> BugLabel:
    return BugLabel(
        label_source="developer", introducing_commit="i" * 40, fix_commit=fix, fix_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
        fix_subject="Fixed #1", tickets=[1], bug_summary="s", bug_summary_source="fix_subject", locality="in_diff",
        bug_files=["f.py"], bug_lines=[BugLine(path="f.py", line=1, origin="deleted", in_diff=True)],
        unlocated_lines=0, fix_tests=["app.tests.T.test_x"],
    )


def _verification(case_id: str, fix: str, verdict: str) -> Verification:
    return Verification(case_id=case_id, fix_commit=fix, introducing_commit="i" * 40, verdict=verdict, reason="")


def test_freeze_keeps_verified_bugs_and_splits_deterministically():
    cases = [Case(case_id=f"c{i}", repo="toy", review=_review(f"{i:040d}"), bugs=[_bug(f"{i:039d}a"), _bug(f"{i:039d}b")]) for i in range(10)]
    verifications = [_verification(f"c{i}", f"{i:039d}a", "verified" if i % 3 else "predates") for i in range(10)]
    verifications += [_verification("c1", f"{1:039d}b", "verified_new")]
    clean = [Case(case_id=f"k{i}", repo="toy", kind="clean", review=_review(f"{i + 50:040d}"), bugs=[]) for i in range(6)]

    frozen = freeze(cases, verifications, clean, dev=2, ablation=3)

    buggy = [c for c in frozen if c.kind == "buggy"]
    assert len(buggy) == 6  # c0, c3, c6, c9 had only a "predates" or unverified bug
    assert next(c for c in buggy if c.case_id == "c1").bugs[1].verdict == "verified_new"
    assert all(b.verdict in ("verified", "verified_new") for c in buggy for b in c.bugs)
    assert sorted(c.split for c in buggy) == ["ablation"] * 3 + ["dev"] * 2 + ["test"]
    assert sorted(c.split for c in frozen if c.kind == "clean") == ["ablation"] * 3 + ["dev"] * 2 + ["test"]
    assert freeze(cases, verifications, clean, dev=2, ablation=3) == frozen  # deterministic


def test_export_card_gets_a_config_per_repo():
    card = "---\nlicense: bsd-3-clause\n---\n\n# Catch Rate\n"

    out = with_configs(card, hf_configs({"django": ["dev", "ablation", "test"], "pytest": ["test"]}))

    front = out.split("---\n")[1]
    assert front.startswith("license: bsd-3-clause\nconfigs:\n  - config_name: django\n")
    assert "path: data/pytest/test.jsonl" in front and "data/pytest/dev.jsonl" not in front
    assert out.endswith("---\n\n# Catch Rate\n")
    assert with_configs("# Card\n", "configs: []").startswith("---\nconfigs: []\n---\n\n# Card")


def test_export_writes_every_frozen_repo_as_one_dataset(tmp_path):
    for key in ("django", "pytest"):
        cases = [Case(case_id=f"{key}-{i}", repo=key, split=split, review=_review(f"{i:040d}"), bugs=[_bug(f"{i:040d}")])
                 for i, split in enumerate(["dev", "test", "test"])]  # fmt: skip
        write_jsonl(tmp_path / "data" / "benchmark" / f"{key}.jsonl", cases)

    assert cmd_export(argparse.Namespace(data_dir=tmp_path / "data", out=tmp_path / "hf")) == 0

    out = tmp_path / "hf" / "catch-rate"
    assert len(read_jsonl(out / "data" / "pytest" / "test.jsonl", Case)) == 2
    assert not (out / "data" / "django" / "ablation.jsonl").exists()  # an empty split is left out, not written
    card = (out / "README.md").read_text(encoding="utf-8")
    assert "config_name: django" in card and "config_name: pytest" in card and "config_name: aiohttp" not in card
    assert "data/django/ablation.jsonl" not in card
