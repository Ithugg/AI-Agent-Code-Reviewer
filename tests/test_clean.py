from datetime import date

from catchrate.mining.clean import mine_clean_cases, survival_rate

from conftest import TOY_SPEC, FakeGitHub, make_pr

BODY = "".join(f"line_{i} = {i}\n" for i in range(30))


def test_clean_cases_need_surviving_lines_and_no_regression_label(toy):
    toy.commit("Initial", {"pkg/a.py": "a = 0\n", "pkg/b.py": "b = 0\n", "pkg/c.py": "c = 0\n"})
    kept = toy.commit("Fixed #1 -- Added a feature.", {"pkg/a.py": "a = 0\n" + BODY})
    churned = toy.commit("Fixed #2 -- Added b.", {"pkg/b.py": "b = 0\n" + BODY})
    labeled = toy.commit("Refs #3 -- Added c.", {"pkg/c.py": "c = 0\n" + BODY})
    toy.commit("Reworked b.", {"pkg/b.py": "b = 0\n" + BODY.replace("= ", "== ")})  # rewrites every line b added
    toy.commit("Later work.", {"pkg/d.py": "d = 0\n"})
    prs = {sha: [make_pr(n, sha, ["x"])] for n, sha in enumerate([kept, churned, labeled], start=10)}

    cases = mine_clean_cases(
        toy.repo, FakeGitHub(prs), TOY_SPEC, labeled={labeled}, since=date(2023, 1, 1), until=date(2030, 1, 1), count=5
    )

    assert [c.case_id for c in cases] == ["toy-clean-pr10"]
    (case,) = cases
    assert (case.kind, case.bugs, case.flags) == ("clean", [], ["survival=1.00"])
    assert survival_rate(toy.repo, TOY_SPEC, case.review) == 1.0
