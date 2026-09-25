import argparse
from pathlib import Path

import pytest

from catchrate.cli.common import read_jsonl, write_jsonl
from catchrate.cli.data import cmd_verify
from catchrate.mining.cases import MineOptions, mine_regression_cases
from catchrate.repos import RepoSpec
from catchrate.schema import StateResult, Verification
from catchrate.verify.checkout import Checkout
from catchrate.verify.envs import python_for
from catchrate.verify.testrun import TestRun
from catchrate.verify.verify import Verifier, classify, state_status

from conftest import FakeGitHub

LABELS = ["app.tests.T.test_x"]


def _run(outcomes, signatures=None, load_errors=(), code=0):
    return TestRun(outcomes, signatures or {}, tuple(load_errors), code, 1.0, "")


def _state(status, signatures=None):
    return StateResult(
        commit="c", django="5.2", python="3.12", patch="applied", status=status,
        outcomes={}, signatures=signatures or {}, load_errors=[], seconds=1.0,
    )


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        (_run({"app.tests.T.test_x": "ok", "other.tests.U.test_y": "fail"}), "pass"),
        (_run({"app.tests.T.test_x": "fail"}, {"app.tests.T.test_x": "AssertionError"}), "fail"),
        (_run({"app.tests.T.test_x": "error"}), "fail"),
        (_run({"app.tests.T.test_x": "skip"}), "skipped"),
        (_run({}, load_errors=["app.tests"]), "load_error"),
        (_run({}, load_errors=["test_x"]), "load_error"),
        (_run({}, load_errors=["app.tests.T"]), "load_error"),
        (_run({}, load_errors=["tests"]), "load_error"),  # unittest's name for "app.tests" failing to import
        (_run({}, load_errors=["other"]), "no_tests"),
        (_run({}), "no_tests"),
        (_run({}, code=1), "env_error"),  # the runner died before running anything
        (_run({}, code=None), "timeout"),
    ],
)
def test_state_status(run, expected):
    assert state_status(run, LABELS)[0] == expected


def test_a_run_with_an_unloaded_label_never_passes():
    run = _run({"b.tests.U.test_y": "ok"}, load_errors=["tests"])
    assert state_status(run, [*LABELS, "b.tests.U.test_y"])[0] == "load_error"


def test_class_labels_select_their_methods():
    status, outcomes, _ = state_status(_run({"app.tests.T.test_a": "ok", "app.tests.T.test_b": "fail"}), ["app.tests.T"])
    assert (status, set(outcomes)) == ("fail", {"app.tests.T.test_a", "app.tests.T.test_b"})


A = "AssertionError: 0 != 2"
I = "AttributeError: Instance only"
MISSING = "AttributeError: 'Options' object has no attribute '_property_names'"  # a name the change added


def _st(status, sigs=None, outcomes=None, skips=None):
    state = _state(status, sigs)
    return state.model_copy(update={"outcomes": outcomes or {t: "fail" for t in (sigs or {})}, "skip_reasons": skips or {}})


@pytest.mark.parametrize(
    ("fix", "parent", "head", "base", "verdict"),
    [
        (_st("pass"), _st("fail", {"t": A}), _st("fail", {"t": A}), _st("pass", outcomes={"t": "ok"}), "verified"),
        (_st("pass"), _st("fail", {"t": A}), _st("fail", {"t": A}), _st("load_error"), "verified_new"),
        (_st("pass"), _st("fail", {"t": A}), _st("fail", {"t": A}), _st("fail", {"t": "NameError: name 'alogin' is not defined"}), "verified_new"),
        (_st("pass"), _st("fail", {"t": A}), _st("fail", {"t": A}), _st("fail", {"t": "TypeError: x"}), "verified_differs"),
        (_st("pass"), _st("fail", {"t": A}), _st("fail", {"t": A}), _st("fail", {"t": "AssertionError: 1 != 2"}), "predates"),
        (_st("pass"), _st("fail", {"t": I}), _st("fail", {"t": I}), _st("fail", {"t": I}), "predates"),
        (_st("pass"), _st("fail", {"t": I}), _st("fail", {"t": I}), _st("fail", {"t": MISSING}), "verified_new"),
        (_st("pass"), _st("fail", {"t": A}), _st("fail", {"t": "FieldError: new_field"}), None, "head_unclear"),
        (_st("pass"), _st("fail", {"t": A}), _st("load_error"), None, "head_unclear"),
        (_st("pass"), _st("fail", {"t": A}), _st("pass"), None, "not_reproduced"),
        (_st("pass"), _st("pass"), None, None, "not_fail_to_pass"),
        (_st("skipped", skips={"t": "PostgreSQL specific tests"}), None, None, None, "unverifiable"),
        (_st("fail", {"t": A}), None, None, None, "broken"),
        (_st("pass"), _st("fail", {"t": A}), _st("env_error"), None, "broken"),
    ],
)
def test_classify(fix, parent, head, base, verdict):
    assert classify(fix, parent, head, base)[0] == verdict


def test_python_for_django_versions():
    versions = [(6, 2), (5, 0), (4, 2), (4, 0), (3, 2), (2, 2), (1, 11), (1, 9), (1, 8), (1, 6)]
    assert [python_for(v) for v in versions] == ["3.12", "3.12", "3.11", "3.10", "3.9", "3.8", "3.6", "3.5", "2.7", "2.7"]


def test_checkout_applies_a_fix_patch_three_ways(toy):
    toy.commit("Initial", {"tests/app/tests.py": "a = 1\nb = 2\nc = 3\n", "tests/app/models.py": "m = 1\n"})
    drifted = toy.commit("Drift", {"tests/app/tests.py": "a = 1\nb = 2\nc = 3\nd = 4\n", "tests/app/models.py": "m = 2\n"})
    parent = toy.commit("More drift", {"tests/app/tests.py": "a = 0\nb = 2\nc = 3\nd = 4\n", "tests/app/models.py": "m = 3\n"})
    fix = toy.commit("Fix", {"tests/app/tests.py": "a = 0\nb = 2\nc = 3\nd = 4\ne = 5\n", "tests/app/models.py": "m = 4\n"})
    paths = ["tests/app/models.py", "tests/app/tests.py"]
    patch = toy.repo.run("diff", "--binary", parent, fix, "--", *paths)
    checkout = Checkout(toy.repo, toy.root.parent / "work" / "w0", parent)

    checkout.switch(parent)
    assert checkout.apply_tests(patch, fix, paths) == "applied"
    assert (checkout.path / "tests/app/tests.py").read_text().endswith("e = 5\n")

    checkout.switch(drifted)  # "m = 2" no longer matches the patch's "m = 3": the fix's definitions are grafted
    assert checkout.apply_tests(patch, fix, paths) == "graft"
    assert (checkout.path / "tests/app/models.py").read_text() == "m = 4\n"
    tests = (checkout.path / "tests/app/tests.py").read_text()
    assert tests.startswith("a = 1\n") and tests.endswith("e = 5\n")  # its own "a = 1" stays; the fix's "e" comes over

    checkout.switch(drifted)
    assert (checkout.path / "tests/app/models.py").read_text() == "m = 2\n"  # switching resets everything
    assert checkout.apply_tests("", fix, paths) == "none"

    broken = toy.commit("Unparseable", {"tests/app/tests.py": "def broken(:\n"})
    checkout.switch(broken)  # nothing to graft into: only the file copy works
    assert checkout.apply_tests(patch, fix, paths) == "overlay"
    assert (checkout.path / "tests/app/tests.py").read_text() == "a = 0\nb = 2\nc = 3\nd = 4\ne = 5\n"


class FakeEnvs:
    def python(self, version):
        return Path(f"python{version}")


def test_verifier_runs_three_states_and_classifies(toy):
    django = "VERSION = (5, 2, 0, 'alpha', 0)\n"
    spec = RepoSpec(key="toy", owner="toy", name="toy", default_branch="main", source_prefixes=("django/",), test_prefix="tests/")
    toy.commit("Initial", {"django/__init__.py": django, "django/calc.py": "def inc(x):\n    return x + 1\n"})
    bug = toy.commit("Refs #1 -- Simplified inc().", {"django/calc.py": "def inc(x):\n    return x - 1\n"})
    test = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        pass\n"
    toy.commit(
        f"Fixed #2 -- Fixed inc().\n\nRegression in {bug[:10]}.",
        {"django/calc.py": "def inc(x):\n    return x + 1\n", "tests/app/tests.py": test},
    )
    (case,), _ = mine_regression_cases(toy.repo, FakeGitHub(), spec, opts=MineOptions(workers=1))

    def runner(python: Path, checkout: Path, labels: list[str]) -> TestRun:
        # Stand-in for Django's runner: the test passes unless inc() subtracts.
        assert labels == ["app.tests.T.test_x"] and (checkout / "tests/app/tests.py").exists()
        broken = "x - 1" in (checkout / "django/calc.py").read_text()
        test_id = "app.tests.T.test_x"
        if broken:
            return TestRun({test_id: "fail"}, {test_id: "AssertionError: 0 != 2"}, (), 1, 0.1, "")
        return TestRun({test_id: "ok"}, {}, (), 0, 0.1, "")

    verifier = Verifier(toy.repo, spec, FakeEnvs(), toy.root.parent / "work", workers=1, runner=runner)
    results = []
    verdicts = verifier.verify_all([case], results.append)

    assert dict(verdicts) == {"verified": 1}
    (result,) = results
    assert (result.fix.status, result.fix_parent.status, result.head.status, result.base.status) == ("pass", "fail", "fail", "pass")
    assert (result.head.patch, result.base.patch, result.fix.python) == ("applied", "applied", "3.12")


def test_reclassify_applies_new_rules_to_stored_runs_without_running_tests(tmp_path, capsys):
    stored = Verification(case_id="c1", fix_commit="f" * 40, introducing_commit="i" * 40, verdict="predates",
                          reason="old rules", fix=_st("pass"), fix_parent=_st("fail", {"t": I}),
                          head=_st("fail", {"t": I}), base=_st("fail", {"t": MISSING}))  # fmt: skip
    errored = Verification(case_id="c2", fix_commit="e" * 40, introducing_commit="i" * 40, verdict="broken",
                           reason="error: GitError")  # fmt: skip
    write_jsonl(tmp_path / "verified" / "django.jsonl", [stored, errored])
    args = argparse.Namespace(repo="django", data_dir=tmp_path, reclassify=True)

    assert cmd_verify(args) == 0

    latest = {v.case_id: v for v in read_jsonl(tmp_path / "verified" / "django.jsonl", Verification)}
    assert (latest["c1"].verdict, latest["c2"].verdict) == ("verified_new", "broken")
    assert "predates -> verified_new" in capsys.readouterr().out
