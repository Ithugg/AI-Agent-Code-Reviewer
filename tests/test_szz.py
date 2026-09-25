from collections import Counter

from catchrate.diffparse import parse_diff
from catchrate.szz.blame import blame_changes, is_meaningful, select_sources, votes, write_ignore_revs

from conftest import TOY_SPEC

CALC = "def inc(x):\n    return {op}\n\n\ndef dec(x):\n{extra}    return x - 1\n"


def _blame_fix(toy, fix, **kwargs):
    parent = toy.repo.commits([fix])[fix].parents[0]
    diffs = select_sources(TOY_SPEC, parse_diff(toy.repo.diff(parent, fix, unified=0)))
    return blame_changes(toy.repo, parent, diffs, **kwargs)


def test_blames_the_commit_that_last_touched_the_fixed_line(toy):
    toy.commit("Initial", {"pkg/calc.py": CALC.format(op="x + 1", extra="")})
    bug = toy.commit("Refs #1 -- Simplified inc().", {"pkg/calc.py": CALC.format(op="x - 1", extra="")})
    toy.commit("Commented dec().", {"pkg/calc.py": CALC.format(op="x - 1", extra="    # decrement\n")})
    fix = toy.commit(
        "Fixed #2 -- Fixed inc().",
        {
            "pkg/calc.py": CALC.format(op="x + 1", extra="    # decrement\n"),
            "tests/calc/tests.py": "def test_inc():\n    assert inc(1) == 2\n",
        },
    )

    blamed = _blame_fix(toy, fix, include_anchors=False)

    assert votes(blamed) == Counter({bug: 1})
    (line,) = blamed
    assert (line.path, line.line, line.orig_line, line.origin) == ("pkg/calc.py", 2, 2, "deleted")


def test_add_only_fix_is_invisible_without_anchors(toy):
    toy.commit("Initial", {"pkg/v.py": "def check(x):\n    return x\n"})
    bug = toy.commit("Refs #1 -- Rewrote check().", {"pkg/v.py": "def check(value):\n    return value\n"})
    fix = toy.commit(
        "Fixed #2 -- Handled None.",
        {"pkg/v.py": "def check(value):\n    if value is None:\n        return 0\n    return value\n"},
    )

    assert _blame_fix(toy, fix, include_anchors=False) == []
    anchored = _blame_fix(toy, fix, include_anchors=True)
    assert {b.origin for b in anchored} == {"anchor"}
    assert votes(anchored, ("deleted", "anchor")) == Counter({bug: 2})


def test_ignore_revs_skips_bulk_reformat_commits(toy):
    toy.commit("Initial", {"pkg/q.py": "a = 1\nb = 'x'\n"})
    bug = toy.commit("Refs #1 -- Changed b.", {"pkg/q.py": "a = 1\nb = 'y'\n"})
    reformat = toy.commit("Reformatted with Black.", {"pkg/q.py": 'a = 1\nb = "y"\n'})
    toy.commit("Added ignore-revs.", {".git-blame-ignore-revs": f"# Black\n{reformat}\n"})
    fix = toy.commit("Fixed #2 -- Restored b.", {"pkg/q.py": 'a = 1\nb = "x"\n'})

    assert votes(_blame_fix(toy, fix)) == Counter({reformat: 1})

    ignore = write_ignore_revs(toy.repo, "main", toy.root.parent / "cache" / "ignore-revs")
    assert ignore is not None
    assert votes(_blame_fix(toy, fix, ignore_revs_file=ignore)) == Counter({bug: 1})


def test_blank_and_comment_lines_do_not_vote():
    assert is_meaningful("    return x")
    assert not is_meaningful("   ")
    assert not is_meaningful("    # a comment")
    assert not is_meaningful('    """')
