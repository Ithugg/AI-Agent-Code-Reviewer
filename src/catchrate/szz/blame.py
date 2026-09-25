"""Blame the lines a fix changed: the core of B-SZZ.

Deleted or replaced lines are blamed at the fix's parent. A fix that only adds
code (a missing check) has nothing to blame, which is SZZ's classic blind spot;
with anchors on, the existing lines on either side of each insertion are blamed
instead, so those fixes still yield candidates.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

from catchrate.diffparse import FileDiff, to_ranges
from catchrate.gitrepo import GitError, GitRepo
from catchrate.repos import RepoSpec

log = logging.getLogger(__name__)

Origin = Literal["deleted", "anchor"]
_COMMENT_PREFIXES = ("#", "//", "/*", "*", "{#", "<!--", '"""', "'''")


@dataclass(frozen=True)
class BlamedLine:
    path: str  # path at the fix's parent
    line: int  # line number at the fix's parent
    origin: Origin
    commit: str  # commit the line is attributed to
    orig_path: str  # path in that commit
    orig_line: int  # line number in that commit's version of the file


def is_meaningful(content: str) -> bool:
    """False for blank and comment-only lines, which SZZ conventionally skips."""
    stripped = content.strip()
    return bool(stripped) and not stripped.startswith(_COMMENT_PREFIXES)


def select_sources(spec: RepoSpec, diffs: list[FileDiff]) -> list[FileDiff]:
    """Text diffs of source files that existed before the change."""
    return [fd for fd in diffs if fd.old_path and not fd.binary and spec.is_source(fd.old_path)]


def _anchor_lines(repo: GitRepo, parent: str, fd: FileDiff) -> set[int]:
    points = fd.insertion_points()
    if not points or fd.old_path is None:
        return set()
    total = len((repo.show_file(parent, fd.old_path) or "").splitlines())
    return {n for p in points for n in (p, p + 1) if 1 <= n <= total}


def blame_changes(
    repo: GitRepo,
    parent: str,
    diffs: list[FileDiff],
    *,
    ignore_revs_file: Path | None = None,
    include_anchors: bool = True,
) -> list[BlamedLine]:
    blamed: list[BlamedLine] = []
    for fd in diffs:
        assert fd.old_path is not None
        deleted = set(fd.removed_lines())
        anchors = _anchor_lines(repo, parent, fd) - deleted if include_anchors else set()
        ranges = to_ranges(deleted | anchors)
        if not ranges:
            continue
        try:
            entries = repo.blame(parent, fd.old_path, ranges, ignore_revs_file=ignore_revs_file)
        except GitError as exc:
            log.warning("blame failed for %s at %s: %s", fd.old_path, parent[:10], exc)
            continue
        for e in entries:
            if is_meaningful(e.content):
                origin: Origin = "deleted" if e.line in deleted else "anchor"
                blamed.append(BlamedLine(fd.old_path, e.line, origin, e.commit, e.orig_path, e.orig_line))
    return blamed


def votes(blamed: Iterable[BlamedLine], origins: tuple[Origin, ...] = ("deleted",)) -> Counter[str]:
    """Candidate bug-introducing commits, weighted by how many blamed lines point at them."""
    return Counter(b.commit for b in blamed if b.origin in origins)


def write_ignore_revs(repo: GitRepo, rev: str, dest: Path) -> Path | None:
    """Save the repo's .git-blame-ignore-revs as of `rev` (bulk reformat commits); None if absent."""
    content = repo.show_file(rev, ".git-blame-ignore-revs")
    if content is None:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    return dest.resolve()
