import pytest

from catchrate.mining.regressions import find_regression_fixes, parse_regression_tokens, parse_tickets

FULL = "821619aa8771ef211c4c4922001efdf914201ca3"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (f"Fixed #1 -- Fixed a crash.\n\nRegression in {FULL}.", [FULL]),
        ("Fixed #2 -- X.\n\nRegression in abc1234 and def5678.", ["abc1234", "def5678"]),
        ("Fixed #3 -- X.\n\nThanks Y for the report.\n\nRegression in\n0a1b2c3d.", ["0a1b2c3d"]),
        ("Fixed #4 -- Fixed a regression in QuerySet.update() for 5.0.", []),
        ("Refs #5 -- Regression in 1234567 (#18000).", ["1234567"]),
        ("Fixed #6 -- regression in ABCDEF0.", ["abcdef0"]),
        ("Fixed #7 -- Regression in abc1234.\n\nSee deadbeef00 for context.", ["abc1234"]),
        ("Fixed #8 -- Regression from 0f956085", ["0f956085"]),
        ("Fixed a regression introduced in 59a655988e.", ["59a655988e"]),
        ("Fixed #9 -- X.\n\nThe regression was caused by 2f35c6f10f.", ["2f35c6f10f"]),
        ("This fixes a regression caused by 0707b82.", ["0707b82"]),
        ("Fixed a regression introduced by 3a37a50b.", ["3a37a50b"]),
        ("Cleaned up tests introduced in f572ee0c65.", []),
        ("Fix test failure caused by 3149cdce32.", []),
        ('Revert "Fixed regression from a2dd618e3b."\n\nThis reverts commit abcdef1.', []),
    ],
)
def test_parse_regression_tokens(message, expected):
    assert parse_regression_tokens(message) == expected


def test_parse_tickets():
    assert parse_tickets("Fixed #37300 -- Preserved hints.") == (37300,)
    assert parse_tickets("Fixed #1, Refs #2 -- Did things for #3.") == (1, 2)
    assert parse_tickets("Corrected a typo.") == ()


def test_find_regression_fixes_resolves_named_commits(toy):
    base = toy.commit("Initial", {"pkg/a.py": "x = 1\n"})
    bug = toy.commit("Refs #1 -- Changed x.", {"pkg/a.py": "x = 2\n"})
    toy.git("checkout", "--quiet", "-b", "side")
    side = toy.commit("Side work.", {"pkg/b.py": "y = 1\n"})
    toy.git("checkout", "--quiet", "main")
    fix = toy.commit(
        f"Fixed #2 -- Restored x.\n\nRegression in {bug[:9]} and {side[:9]}. Also mentions {base[:9]}.",
        {"pkg/a.py": "x = 1\n"},
    )
    toy.commit("Unrelated.", {"pkg/c.py": "z = 1\n"})

    (found,) = find_regression_fixes(toy.repo, "main")

    assert found.sha == fix
    assert found.tickets == (2,)
    assert found.named == (bug,)  # the side-branch commit is not in the fix's history
    assert found.unresolved == (side[:9],)
