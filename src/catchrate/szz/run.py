"""Run B-SZZ variants on developer-labeled regression fixes and score them."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

from catchrate.diffparse import parse_diff
from catchrate.github import GitHubClient
from catchrate.gitrepo import GitRepo
from catchrate.mining.pr_range import pr_groups
from catchrate.mining.regressions import RegressionFix, find_regression_fixes
from catchrate.repos import RepoSpec
from catchrate.szz.blame import BlamedLine, Origin, blame_changes, select_sources, votes
from catchrate.szz.evaluate import FixOutcome, score

log = logging.getLogger(__name__)

# name -> (use the repo's .git-blame-ignore-revs, which blamed lines vote)
VARIANTS: dict[str, tuple[bool, tuple[Origin, ...]]] = {
    "bszz": (False, ("deleted",)),
    "bszz+ignore_revs": (True, ("deleted",)),
    "bszz+ignore_revs+anchors": (True, ("deleted", "anchor")),
}


def evaluate_szz(
    repo: GitRepo,
    gh: GitHubClient,
    spec: RepoSpec,
    *,
    ignore_revs_file: Path | None,
    since: date | None = None,
    workers: int = 6,
) -> dict[str, Any]:
    fixes = [f for f in find_regression_fixes(repo, spec.default_branch, since) if f.named]
    log.info("SZZ: blaming %d labeled fixes", len(fixes))

    def blame(fix: RegressionFix) -> tuple[list[BlamedLine], list[BlamedLine]]:
        parent = fix.commit.parents[0]
        diffs = select_sources(spec, parse_diff(repo.diff(parent, fix.sha, unified=0)))
        plain = blame_changes(repo, parent, diffs)
        if ignore_revs_file is None:
            return plain, plain
        return plain, blame_changes(repo, parent, diffs, ignore_revs_file=ignore_revs_file)

    with ThreadPoolExecutor(workers) as pool:
        blamed = list(pool.map(blame, fixes))

    involved = {c for f in fixes for c in f.named} | {b.commit for pair in blamed for side in pair for b in side}
    groups = pr_groups(gh, spec, involved)
    recency = {sha: c.committed.timestamp() for sha, c in repo.commits(involved).items()}

    variants: dict[str, Any] = {}
    for name, (use_ignore_revs, origins) in VARIANTS.items():
        outcomes = [
            FixOutcome(fix.sha, frozenset(fix.named), votes(ignored if use_ignore_revs else plain, origins), groups, recency)
            for fix, (plain, ignored) in zip(fixes, blamed)
        ]
        variants[name] = {"commit": score(outcomes).as_dict(), "pr": score(outcomes, by_group=True).as_dict()}

    return {
        "repo": spec.key,
        "head": repo.head_of(spec.default_branch),
        "since": since.isoformat() if since else None,
        "fixes": len(fixes),
        # Fixes whose source changes delete or replace no meaningful line: invisible to plain B-SZZ.
        "add_only_fixes": sum(1 for plain, _ in blamed if not any(b.origin == "deleted" for b in plain)),
        "variants": variants,
    }
