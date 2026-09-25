from pathlib import Path
from types import SimpleNamespace

from catchrate.gitrepo import GitRepo
from catchrate.mining.cases import MineOptions, mine_regression_cases
from catchrate.repos import PytestSetup, RepoSpec
from catchrate.verify import suites
from catchrate.verify.pytestrun import parse_junit
from catchrate.verify.suites import PytestSuite
from catchrate.verify.testrun import TestRun
from catchrate.verify.verify import Verifier, state_status

from conftest import FakeGitHub

SPEC = RepoSpec(
    key="toypy", owner="toy", name="toypy", default_branch="main", source_prefixes=("src/",), test_prefix="testing/",
    release_notes_prefix="changelog/", fix_source="changelog_bugfix", changelog_globs=("changelog/*.bugfix.rst",),
    test_style="pytest",
)  # fmt: skip
TESTS = """from pkg.calc import inc


def test_inc():
    assert inc(1) == 2


class TestCalc:
    def test_zero(self):
        assert inc(0) == 1
"""


def _toy_pytest_repo(toy):
    toy.commit("Initial", {"src/pkg/__init__.py": "", "src/pkg/calc.py": "def inc(x):\n    return x + 1\n",
                           "pyproject.toml": 'requires-python = ">=3.9"\n'})  # fmt: skip
    bug = toy.commit("Simplify inc (#5)", {"src/pkg/calc.py": "def inc(x):\n    return x - 1\n"})
    toy.commit("Docs", {"README.rst": "toy\n"})
    fix = toy.commit(
        "Fix inc (#12)",
        {"src/pkg/calc.py": "def inc(x):\n    return x + 1\n", "testing/test_calc.py": TESTS,
         "changelog/12.bugfix.rst": "Fixed :func:`inc` subtracting\ninstead of adding.\n"},
    )  # fmt: skip
    return bug, fix


def test_changelog_fixes_get_szz_labels_and_node_ids(toy):
    bug, fix = _toy_pytest_repo(toy)

    (case,), rejected = mine_regression_cases(toy.repo, FakeGitHub(), SPEC, opts=MineOptions(workers=1))

    assert rejected == []
    (truth,) = case.bugs
    assert (truth.label_source, truth.introducing_commit, truth.fix_commit, truth.tickets) == ("szz", bug, fix, [12])
    assert truth.fix_tests == ["testing/test_calc.py::test_inc", "testing/test_calc.py::TestCalc::test_zero"]
    assert truth.bug_summary == "Fixed inc subtracting instead of adding."
    assert [(b.path, b.line, b.in_diff) for b in truth.bug_lines] == [("src/pkg/calc.py", 2, True)]


def test_pytest_suite_picks_the_oldest_python_the_commit_supports(toy, tmp_path):
    first = toy.commit("Initial", {"pyproject.toml": '[project]\nrequires-python = ">=3.10"\n'})
    second = toy.commit("Older floor", {"pyproject.toml": "", "setup.cfg": "[options]\npython_requires = >=3.6\n"})
    third = toy.commit("Too new", {"setup.cfg": "[options]\npython_requires = >=3.14\n"})
    suite = PytestSuite(envs=None, key="toypy")  # type: ignore[arg-type]

    assert [suite.python(toy.repo, c) for c in (first, second, third)] == ["3.10", "3.8", None]


def test_parse_junit_maps_classnames_to_node_ids(tmp_path):
    (tmp_path / "testing").mkdir()
    (tmp_path / "testing" / "test_calc.py").write_text("", encoding="utf-8")
    xml = """<testsuites><testsuite>
      <testcase classname="testing.test_calc" name="test_inc"><failure message="AssertionError: assert 0 == 2">...</failure></testcase>
      <testcase classname="testing.test_calc.TestCalc" name="test_zero[1]"/>
      <testcase classname="testing.test_calc" name="test_skip"><skipped type="pytest.skip" message="needs posix"/></testcase>
      <testcase classname="testing.test_calc" name="test_x"><skipped type="pytest.xfail" message="known"/></testcase>
      <testcase classname="testing.test_calc" name="test_err"><error message="setup failed">E   ValueError: boom</error></testcase>
      <testcase classname="" name="testing.test_calc"><error message="collection failure">ImportError</error></testcase>
    </testsuite></testsuites>"""

    run = parse_junit(xml, tmp_path, 1, 2.0, "", ["testing/test_calc.py"])

    assert run.outcomes == {
        "testing/test_calc.py::test_inc": "fail",
        "testing/test_calc.py::TestCalc::test_zero[1]": "ok",
        "testing/test_calc.py::test_skip": "skip",
        "testing/test_calc.py::test_x": "xfail",
        "testing/test_calc.py::test_err": "error",
    }
    assert run.signatures["testing/test_calc.py::test_inc"] == "AssertionError: assert 0 == 2"
    assert run.signatures["testing/test_calc.py::test_err"] == "ValueError: boom"
    assert run.skip_reasons == {"testing/test_calc.py::test_skip": "needs posix"}
    assert run.load_errors == ("testing/test_calc.py",)


def test_missing_node_ids_count_as_load_errors(tmp_path):
    run = parse_junit("", tmp_path, 4, 0.5, "ERROR: file or directory not found: testing/test_new.py", ["testing/test_new.py::test_a"])
    assert state_status(run, ["testing/test_new.py::test_a"], "::")[0] == "load_error"


def test_node_ids_select_parametrized_and_class_members():
    run = TestRun({"testing/t.py::test_a[1]": "fail", "testing/t.py::TestC::test_b": "ok", "testing/t.py::test_c": "fail"},
                  {"testing/t.py::test_a[1]": "AssertionError: x"}, (), 1, 1.0, "")  # fmt: skip
    status, outcomes, _ = state_status(run, ["testing/t.py::test_a", "testing/t.py::TestC"], "::")
    assert (status, set(outcomes)) == ("fail", {"testing/t.py::test_a[1]", "testing/t.py::TestC::test_b"})


def test_a_failed_setup_is_an_environment_problem(toy):
    _, fix = _toy_pytest_repo(toy)
    (case,), _ = mine_regression_cases(toy.repo, FakeGitHub(), SPEC, opts=MineOptions(workers=1))

    class BrokenSuite:
        label_sep = "::"

        def python(self, repo, commit):
            return "3.12"

        def version(self, repo, commit):
            return None

        def env(self, worker, python):
            return Path("python")

        def setup(self, env, checkout, repo, commit):
            return "error: could not build wheel"

        def run(self, env, checkout, labels):
            raise AssertionError("setup failed, so nothing should run")

    verifier = Verifier(toy.repo, SPEC, envs=None, work_dir=toy.root.parent / "work", workers=1, suite=BrokenSuite())  # type: ignore[arg-type]
    results = []
    verifier.verify_all([case], results.append)

    (result,) = results
    assert (result.verdict, result.fix.status) == ("broken", "env_error")
    assert "could not start" in result.reason and "could not build wheel" in result.fix.tail


def test_clone_fetches_tags_and_the_version_comes_from_the_nearest_one(toy, tmp_path):
    first = toy.commit("Initial", {"setup.cfg": "[options]\npython_requires = >=3.8\n"})
    toy.git("tag", "7.1.0.dev0", first)
    second = toy.commit("Work", {"src/pkg/a.py": "x = 1\n"})
    suite = PytestSuite(envs=None, key="toypy")  # type: ignore[arg-type]
    untagged = GitRepo.ensure_clone(toy.root.as_uri(), tmp_path / "plain.git", "main")
    assert suite.version(untagged, second) is None
    assert suite.setup(Path("unused"), Path("unused"), untagged, second).startswith("no release tag")

    clone = GitRepo.ensure_clone(toy.root.as_uri(), tmp_path / "tagged.git", "main", tags=True)

    assert PytestSuite(envs=None, key="toypy").version(clone, second) == "7.1.0"  # type: ignore[arg-type]


def test_setup_installs_requirements_then_the_checkout_with_the_dependencies_of_its_day(toy, monkeypatch, tmp_path):
    commit = toy.commit("Initial", {"setup.cfg": "[options]\npython_requires = >=3.8\n"})  # 2024-01-01 01:00 UTC
    config = PytestSetup(install=(".",), requirements=("requirements/test.txt", "requirements/ci.txt"),
                         env=(("TOY_NO_EXT", "1"),), placeholders=("vendor/parser/README.md",))  # fmt: skip
    suite = PytestSuite(envs=SimpleNamespace(uv=Path("uv")), key="toypy", config=config)  # type: ignore[arg-type]
    suite._versions[commit] = "7.1.0"
    calls = []
    fake = SimpleNamespace(run=lambda cmd, **kw: calls.append((cmd, kw)) or SimpleNamespace(returncode=0))
    monkeypatch.setattr(suites, "subprocess", fake)  # only the suite's calls: git itself still runs
    assert suite.setup(Path("env/python"), tmp_path, toy.repo, commit).startswith("none of the test requirement files")
    (tmp_path / "requirements").mkdir()
    (tmp_path / "requirements" / "ci.txt").write_text("pytest\n", encoding="utf-8")  # an older name

    assert suite.setup(Path("env/python"), tmp_path, toy.repo, commit) is None

    (requirements, _), (checkout, kw) = calls
    assert requirements[-2:] == ["-r", "requirements/ci.txt"] and "--reinstall-package" not in requirements
    assert checkout[checkout.index("--exclude-newer") + 1] == "2024-01-02T01:00:00Z"
    assert checkout[checkout.index("--reinstall-package") + 1] == "toypy"  # never a wheel cached from another commit
    assert checkout[-1] == "." and "-e" not in checkout
    assert (kw["env"]["SETUPTOOLS_SCM_PRETEND_VERSION"], kw["env"]["TOY_NO_EXT"]) == ("7.1.0", "1")
    assert (tmp_path / "vendor" / "parser" / "README.md").exists()


def test_packages_with_no_copy_before_the_cutoff_are_exempted_from_it(toy, monkeypatch, tmp_path):
    commit = toy.commit("Initial", {"setup.cfg": "[options]\npython_requires = >=3.8\n"})
    suite = PytestSuite(envs=SimpleNamespace(uv=Path("uv")), key="toypy")  # type: ignore[arg-type]
    suite._versions[commit] = "7.1.0"
    hint = b"hint: `atomicwrites` was filtered by `exclude-newer` to only include packages uploaded before 2022"
    results = [SimpleNamespace(returncode=1, stdout=b"", stderr=hint), SimpleNamespace(returncode=0, stdout=b"", stderr=b"")]
    calls = []
    fake = SimpleNamespace(run=lambda cmd, **kw: calls.append(cmd) or results.pop(0))
    monkeypatch.setattr(suites, "subprocess", fake)

    assert suite.setup(Path("env/python"), tmp_path, toy.repo, commit) is None

    assert "--exclude-newer-package" not in calls[0]
    assert calls[1][calls[1].index("--exclude-newer-package") + 1].startswith("atomicwrites=")


def test_changelog_fragments_without_an_extension_are_release_notes():
    spec = RepoSpec(**{**SPEC.__dict__, "release_notes_prefix": "CHANGES/", "changelog_globs": ("CHANGES/*.bugfix",)})
    assert spec.is_release_note("CHANGES/7302.bugfix") and spec.is_release_note("CHANGES/7302.feature.rst")
    assert not spec.is_release_note("CHANGES/README")


def test_example_scripts_are_not_fix_tests():
    assert SPEC.is_test("testing/test_calc.py")
    spec = RepoSpec(**{**SPEC.__dict__, "test_excludes": ("testing/example_scripts/",)})
    assert not spec.is_test("testing/example_scripts/fixtures/test_getfixturevalue_dynamic.py")
