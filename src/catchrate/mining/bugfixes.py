"""Bug fixes found through changelog fragments, with SZZ proposing the commit that caused each bug.

Projects that use towncrier add a fragment such as changelog/1234.bugfix.rst in
the commit that fixes a bug. Nobody names the commit that introduced it, so
B-SZZ proposes one: the commit most of the fix's changed lines blame to. The
four-point test verification later keeps only the proposals that hold up.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from catchrate.diffparse import parse_diff
from catchrate.gitrepo import Commit, GitRepo
from catchrate.mining.regressions import RegressionFix
from catchrate.repos import RepoSpec
from catchrate.szz.blame import blame_changes, select_sources, votes

log = logging.getLogger(__name__)


def find_changelog_fixes(repo: GitRepo, spec: RepoSpec, since: date | None = None) -> list[tuple[Commit, list[str]]]:
    """Commits on the default branch (newest first) that add a bug-fix changelog fragment."""
    args = ["log", spec.default_branch, "--first-parent", "--diff-filter=A", "--name-only", "--format=%x1e%H"]
    if since:
        args.append(f"--since={since.isoformat()}")
    out = repo.run(*args, "--", *spec.changelog_globs)
    fragments: dict[str, list[str]] = {}
    for record in out.split("\x1e"):
        lines = [line.strip() for line in record.strip().splitlines() if line.strip()]
        if lines:
            fragments[lines[0]] = lines[1:]
    commits = repo.commits(fragments)
    return [(commits[sha], paths) for sha, paths in fragments.items() if sha in commits]


def _tickets(paths: list[str]) -> tuple[int, ...]:
    numbers = (re.match(r"(\d+)", path.rsplit("/", 1)[-1]) for path in paths)
    return tuple(dict.fromkeys(int(m[1]) for m in numbers if m))


def szz_label(
    repo: GitRepo,
    spec: RepoSpec,
    fixes: list[tuple[Commit, list[str]]],
    *,
    ignore_revs_file: Path | None = None,
    max_age_days: int = 3 * 365,
    workers: int = 6,
) -> list[RegressionFix]:
    """Name the most-blamed commit behind each fix's changed source lines (anchors when it only adds code)."""

    def label(item: tuple[Commit, list[str]]) -> RegressionFix | None:
        commit, paths = item
        if not commit.parents:
            return None
        parent = commit.parents[0]
        diffs = select_sources(spec, parse_diff(repo.diff(parent, commit.sha, unified=0)))
        if not diffs:
            return None  # a fix outside the source tree (docs, tests, packaging)
        blamed = blame_changes(repo, parent, diffs, ignore_revs_file=ignore_revs_file)
        candidates = votes(blamed) or votes(blamed, ("deleted", "anchor"))
        if not candidates:
            return None
        dates = {sha: c.committed for sha, c in repo.commits(candidates).items()}
        top = max(candidates, key=lambda sha: (candidates[sha], dates.get(sha, commit.committed)))
        if top not in dates or (commit.committed - dates[top]).days > max_age_days:
            return None  # older than the window we can build environments for
        return RegressionFix(commit, _tickets(paths), (top,), (), label_source="szz")

    with ThreadPoolExecutor(workers) as pool:
        labeled = [fix for fix in pool.map(label, fixes) if fix is not None]
    log.info("SZZ proposed an introducing commit for %d of %d changelog bug fixes", len(labeled), len(fixes))
    return labeled
