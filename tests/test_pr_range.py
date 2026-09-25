from catchrate.mining.pr_range import choose_pr, landed_range
from catchrate.schema import PRInfo


def _pr(number, merge_commit, headlines, *, merged=True, base="main"):
    return PRInfo(
        number=number,
        title=f"PR {number}",
        merged=merged,
        base_ref=base,
        merge_commit=merge_commit,
        commit_count=len(headlines),
        commit_headlines=headlines,
    )


def test_single_commit_pr(toy):
    base = toy.commit("Initial", {"a.txt": "1\n"})
    head = toy.commit("Fixed #1 -- One.", {"a.txt": "2\n"})

    r = landed_range(toy.repo, _pr(1, head, ["Fixed #1 -- One (as pushed)."]))

    assert (r.base, r.head, r.commits, r.method) == (base, head, (head,), "single")


def test_rebase_merged_pr_lands_each_commit(toy):
    base = toy.commit("Initial", {"a.txt": "1\n"})
    c1 = toy.commit("Refs #1 -- Step one.", {"a.txt": "2\n"})
    c2 = toy.commit("Fixed #1 -- Step two.", {"a.txt": "3\n"})

    r = landed_range(toy.repo, _pr(2, c2, ["Refs #1 -- Step one.", "Fixed #1 -- Step two."]))

    assert (r.base, r.head, r.commits, r.method) == (base, c2, (c1, c2), "rebase")


def test_rebase_detection_uses_the_headlines_github_returned(toy):
    base = toy.commit("Initial", {"a.txt": "1\n"})
    commits = [toy.commit(f"Step {i}.", {"a.txt": f"{i}\n"}) for i in range(3)]
    pr = _pr(6, commits[-1], ["Step 0.", "Step 1."])  # as if GitHub truncated the list
    pr = pr.model_copy(update={"commit_count": 3})

    r = landed_range(toy.repo, pr)

    assert (r.base, r.commits, r.method) == (base, tuple(commits), "rebase")


def test_squash_merged_pr_lands_one_commit(toy):
    toy.commit("Initial", {"a.txt": "1\n"})
    previous = toy.commit("Someone else's change.", {"b.txt": "1\n"})
    squashed = toy.commit("Fixed #1 -- Everything (#3).", {"a.txt": "3\n"})

    r = landed_range(toy.repo, _pr(3, squashed, ["WIP", "Address review"]))

    assert (r.base, r.head, r.commits, r.method) == (previous, squashed, (squashed,), "squash")


def test_merge_commit_pr(toy):
    toy.commit("Initial", {"a.txt": "1\n"})
    toy.git("checkout", "--quiet", "-b", "feature")
    f1 = toy.commit("Feature one.", {"f.txt": "1\n"})
    f2 = toy.commit("Feature two.", {"f.txt": "2\n"})
    toy.git("checkout", "--quiet", "main")
    other = toy.commit("Main moved on.", {"a.txt": "2\n"})
    toy.git("merge", "--quiet", "--no-ff", "-m", "Merge feature", "feature")
    merge = toy.git("rev-parse", "HEAD")

    r = landed_range(toy.repo, _pr(4, merge, ["Feature one.", "Feature two."]))

    assert (r.base, r.head, r.commits, r.method) == (other, merge, (f1, f2), "merge")


def test_unknown_merge_commit(toy):
    toy.commit("Initial", {"a.txt": "1\n"})
    assert landed_range(toy.repo, _pr(5, "0" * 40, ["x"])) is None


def test_choose_pr_takes_merged_prs_into_target_branches():
    prs = [
        _pr(9, "a" * 40, ["x"], base="stable/5.2.x"),
        _pr(7, "b" * 40, ["x"], merged=False),
        _pr(8, "c" * 40, ["x"]),
    ]
    assert choose_pr(prs, ("main", "master")).number == 8
    assert choose_pr([], ("main",)) is None
