from catchrate.szz.blame import write_ignore_revs
from catchrate.szz.run import evaluate_szz

from conftest import TOY_SPEC, FakeGitHub, make_pr


def test_evaluate_szz_scores_every_variant(toy):
    toy.commit("Initial", {"pkg/q.py": "a = 1\nb = 'x'\n\n\ndef f():\n    return 1\n"})
    bug = toy.commit("Refs #1 -- Changed b.", {"pkg/q.py": "a = 1\nb = 'y'\n\n\ndef f():\n    return 1\n"})
    reformat = toy.commit("Reformatted.", {"pkg/q.py": 'a = 1\nb = "y"\n\n\ndef f():\n    return 1\n'})
    toy.commit("Added ignore-revs.", {".git-blame-ignore-revs": f"{reformat}\n"})
    toy.commit(f"Fixed #2 -- Restored b.\n\nRegression in {bug[:10]}.", {"pkg/q.py": 'a = 1\nb = "x"\n\n\ndef f():\n    return 1\n'})
    # An add-only fix: plain B-SZZ has nothing to blame, anchors do.
    toy.commit(
        f"Fixed #3 -- Guarded f().\n\nRegression in {bug[:10]}.",
        {"pkg/q.py": 'a = 1\nb = "x"\n\n\ndef f():\n    if a:\n        return 0\n    return 1\n'},
    )
    ignore = write_ignore_revs(toy.repo, "main", toy.root.parent / "ignore-revs")
    gh = FakeGitHub({bug: [make_pr(5, bug, ["Refs #1 -- Changed b."])]})

    report = evaluate_szz(toy.repo, gh, TOY_SPEC, ignore_revs_file=ignore, workers=1)

    assert (report["fixes"], report["add_only_fixes"]) == (2, 1)
    variants = report["variants"]
    # Plain blame lands on the reformat commit for the first fix and finds nothing for the second.
    assert variants["bszz"]["commit"]["recall"] == 0.0
    assert variants["bszz+ignore_revs"]["commit"]["recall"] == 0.5
    assert variants["bszz+ignore_revs"]["commit"]["precision"] == 1.0
    anchors = variants["bszz+ignore_revs+anchors"]["commit"]
    assert anchors["with_candidates"] == 2
    assert anchors["recall"] == 0.5  # the anchors around f()'s body predate the bug commit
