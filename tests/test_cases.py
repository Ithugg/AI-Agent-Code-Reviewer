from catchrate.diffparse import parse_diff
from catchrate.mining.cases import MineOptions, carry_back, mine_regression_cases

from conftest import TOY_SPEC, FakeGitHub
from conftest import make_pr as _pr

CALC_OK = "def inc(x):\n    return x + 1\n"
CALC_BUG = "def inc(x):\n    return x - 1\n"
TEST = "class IncTests(TestCase):\n    def test_inc(self):\n        assert inc(1) == 2\n"
NOTE = "* Fixed a regression in Toy 1.1 where :func:`~pkg.calc.inc` decremented\n  instead (:ticket:`2`).\n"


def _mine(toy, prs, **opts):
    return mine_regression_cases(toy.repo, FakeGitHub(prs), TOY_SPEC, opts=MineOptions(workers=1, **opts))


def test_mines_a_case_from_a_labeled_fix(toy):
    base = toy.commit("Initial", {"pkg/calc.py": CALC_OK})
    bug = toy.commit("Refs #1 -- Simplified inc().", {"pkg/calc.py": CALC_BUG})
    toy.commit("Unrelated.", {"pkg/other.py": "y = 1\n"})
    fix = toy.commit(
        f"Fixed #2 -- Fixed inc().\n\nRegression in {bug[:10]}.",
        {"pkg/calc.py": CALC_OK, "tests/calc/tests.py": TEST, "docs/releases/1.1.1.txt": NOTE},
    )
    pr = _pr(7, bug, ["Refs #1 -- Simplified inc()."], body="Makes inc() shorter.", url="https://example.com/pull/7")

    cases, rejected = _mine(toy, {bug: [pr]})

    assert rejected == []
    (case,) = cases
    assert (case.case_id, case.flags) == ("toy-pr7", [])
    review = case.review
    assert (review.review_unit, review.base_commit, review.head_commit, review.commits) == ("pr", base, bug, [bug])
    assert (review.pr_number, review.title, review.description) == (7, pr.title, "Makes inc() shorter.")
    assert (review.changed_files, review.changed_lines) == (["pkg/calc.py"], 2)
    assert "-    return x + 1\n+    return x - 1" in review.diff
    (truth,) = case.bugs
    assert (truth.introducing_commit, truth.fix_commit, truth.tickets) == (bug, fix, [2])
    assert (truth.locality, truth.bug_files, truth.unlocated_lines) == ("in_diff", ["pkg/calc.py"], 0)
    assert [(b.path, b.line, b.origin, b.in_diff) for b in truth.bug_lines] == [("pkg/calc.py", 2, "deleted", True)]
    assert truth.fix_tests == ["calc.tests.IncTests.test_inc"]
    assert truth.bug_summary == "Fixed a regression in Toy 1.1 where inc decremented instead (#2)."
    assert truth.bug_summary_source == "release_note"


def test_bugs_from_the_same_change_share_one_case(toy):
    toy.commit("Initial", {"pkg/calc.py": CALC_OK, "pkg/neg.py": "def neg(x):\n    return -x\n"})
    bug = toy.commit(
        "Refs #1 -- Rewrote helpers.",
        {"pkg/calc.py": CALC_BUG, "pkg/neg.py": "def neg(x):\n    return x\n"},
    )
    toy.commit(f"Fixed #2 -- Fixed inc().\n\nRegression in {bug[:10]}.", {"pkg/calc.py": CALC_OK, "tests/calc/tests.py": TEST})
    toy.commit(
        f"Fixed #3 -- Fixed neg().\n\nRegression in {bug[:10]}.",
        {"pkg/neg.py": "def neg(x):\n    return -x\n", "tests/neg/tests.py": "def test_neg():\n    assert neg(1) == -1\n"},
    )

    (case,), rejected = _mine(toy, {bug: [_pr(9, bug, ["Refs #1 -- Rewrote helpers."])]})

    assert rejected == []
    assert case.case_id == "toy-pr9"
    assert [b.tickets for b in case.bugs] == [[2], [3]]
    assert [b.bug_files for b in case.bugs] == [["pkg/calc.py"], ["pkg/neg.py"]]


def test_large_pr_falls_back_to_the_named_commit(toy):
    toy.commit("Initial", {"pkg/calc.py": CALC_OK})
    refactor = toy.commit("Refs #1 -- Big refactor.", {"pkg/big.py": "x = 1\n" * 30})
    bug = toy.commit("Refs #1 -- Simplified inc().", {"pkg/calc.py": CALC_BUG})
    toy.commit(f"Fixed #2 -- Fixed inc().\n\nRegression in {bug[:10]}.", {"pkg/calc.py": CALC_OK, "tests/calc/tests.py": TEST})
    pr = _pr(8, bug, ["Refs #1 -- Big refactor.", "Refs #1 -- Simplified inc()."], title="Refactoring")

    (case,), _ = _mine(toy, {bug: [pr]}, max_changed_lines=10)

    assert (case.case_id, case.flags) == (f"toy-c{bug[:10]}", ["pr_too_large"])
    review = case.review
    assert (review.review_unit, review.base_commit, review.head_commit, review.changed_lines) == ("commit", refactor, bug, 2)
    assert (review.title, review.pr_number) == ("Refs #1 -- Simplified inc().", 8)
    assert case.bugs[0].bug_summary_source == "fix_subject"


def test_fix_without_tests_is_rejected(toy):
    toy.commit("Initial", {"pkg/calc.py": CALC_OK})
    bug = toy.commit("Simplified inc().", {"pkg/calc.py": CALC_BUG})
    fix = toy.commit(f"Fixed #3 -- Fixed inc().\n\nRegression in {bug[:10]}.", {"pkg/calc.py": CALC_OK})

    cases, (rejection,) = _mine(toy, {})

    assert cases == []
    assert (rejection.fix_commit, rejection.introducing_commit) == (fix, bug)
    assert rejection.reasons == ["no_pr", "no_fix_test"]


def test_fix_in_older_code_is_kept_as_an_outside_diff_bug(toy):
    toy.commit("Initial", {"pkg/calc.py": "# calculator\n" + CALC_BUG, "pkg/other.py": "y = 1\n"})
    named = toy.commit("Touched other.", {"pkg/other.py": "y = 2\n"})
    toy.commit("Added a header line.", {"pkg/calc.py": "# calculator\n# more\n" + CALC_BUG})
    toy.commit(
        f"Fixed #4 -- Fixed inc().\n\nRegression in {named[:10]}.",
        {"pkg/calc.py": "# calculator\n# more\n" + CALC_OK, "tests/calc/tests.py": TEST},
    )

    (case,), rejected = _mine(toy, {})

    assert rejected == []
    (truth,) = case.bugs
    assert truth.locality == "outside_diff"
    # Line 4 at the fix's parent sat at line 3 at the review head, before the header line was added.
    assert [(b.path, b.line, b.in_diff) for b in truth.bug_lines] == [("pkg/calc.py", 3, False)]


def test_outside_diff_lines_are_found_across_a_later_rename(toy):
    toy.commit("Initial", {"pkg/calc.py": CALC_BUG, "pkg/other.py": "y = 1\n"})
    named = toy.commit("Touched other.", {"pkg/other.py": "y = 2\n"})
    toy.commit("Renamed calc.", {"pkg/calc.py": None, "pkg/arith.py": CALC_BUG})
    toy.commit(
        f"Fixed #5 -- Fixed inc().\n\nRegression in {named[:10]}.",
        {"pkg/arith.py": CALC_OK, "tests/calc/tests.py": TEST},
    )

    (case,), rejected = _mine(toy, {})

    assert rejected == []
    (truth,) = case.bugs
    assert (truth.unlocated_lines, truth.bug_files) == (0, ["pkg/calc.py"])
    assert [(b.path, b.line, b.in_diff) for b in truth.bug_lines] == [("pkg/calc.py", 2, False)]


def test_carry_back_maps_lines_through_later_hunks():
    (later,) = parse_diff(
        "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n"
        "@@ -2,0 +3,2 @@\n+new1\n+new2\n"  # 2 lines inserted after old line 2
        "@@ -5,2 +7 @@\n-old5\n-old6\n+merged\n"  # old 5-6 replaced by new 7
    )
    assert carry_back(later, "f.py", 2) == ("f.py", 2)
    assert carry_back(later, "f.py", 3) is None  # added later
    assert carry_back(later, "f.py", 5) == ("f.py", 3)
    assert carry_back(later, "f.py", 7) is None
    assert carry_back(later, "f.py", 8) == ("f.py", 7)
    assert carry_back(None, "g.py", 10) == ("g.py", 10)
