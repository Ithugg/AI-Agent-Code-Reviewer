import time

import pytest

from catchrate.gitrepo import GitError, GitRepo, _parse_numstat_z


def test_parse_numstat_z_handles_renames_and_binaries():
    out = "3\t1\tpkg/a.py\0-\t-\tlogo.png\0" "2\t0\t\0old/b.py\0new/b.py\0"
    stats = _parse_numstat_z(out)
    assert [(s.added, s.deleted, s.path) for s in stats] == [
        (3, 1, "pkg/a.py"),
        (None, None, "logo.png"),
        (2, 0, "new/b.py"),
    ]


def test_resolve_commits_and_numstat(toy):
    a = toy.commit("Initial", {"pkg/a.py": "x = 1\n"})
    b = toy.commit("Second", {"pkg/a.py": "x = 2\ny = 3\n"})

    got = toy.repo.resolve_commits([a[:8], b, "0" * 12, "not a sha"])

    assert got == {a[:8]: a, b: b, "0" * 12: None, "not a sha": None}
    assert [(s.added, s.deleted, s.path) for s in toy.repo.numstat(a, b)] == [(2, 1, "pkg/a.py")]


def test_blame_reports_origin_line_numbers(toy):
    a = toy.commit("Initial", {"f.py": "one\ntwo\nthree\n"})
    b = toy.commit("Insert", {"f.py": "zero\none\ntwo\nthree\n"})

    entries = toy.repo.blame(b, "f.py", [(1, 1), (3, 4)])

    assert [(e.line, e.commit, e.orig_line, e.content) for e in entries] == [
        (1, b, 1, "zero"),
        (3, a, 2, "two"),
        (4, a, 3, "three"),
    ]


def test_log_reads_messages_and_parents(toy):
    a = toy.commit("Initial", {"f.py": "1\n"})
    b = toy.commit("Fixed #1 -- Subject line.\n\nBody text.\n\nRegression in abc.", {"f.py": "2\n"})

    newest, oldest = toy.repo.log("main")

    assert (newest.sha, newest.parents, newest.subject) == (b, (a,), "Fixed #1 -- Subject line.")
    assert newest.message.endswith("Regression in abc.")
    assert oldest.parents == ()
    assert [c.sha for c in toy.repo.first_parent_chain(b, 1)] == [b]
    assert toy.repo.is_ancestor(a, b) and not toy.repo.is_ancestor(b, a)


def test_a_missing_branch_fails_at_once_instead_of_retrying(toy, tmp_path, monkeypatch):
    toy.commit("Initial", {"a.txt": "a\n"})
    monkeypatch.setattr(time, "sleep", lambda seconds: pytest.fail("retried a permanent failure"))

    with pytest.raises(GitError, match="not found in upstream"):
        GitRepo.ensure_clone(toy.root.as_uri(), tmp_path / "clone.git", "no-such-branch")
