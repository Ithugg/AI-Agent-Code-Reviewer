"""Clean cases: merged changes with no known bug, for measuring false alarms.

A change counts as clean when no regression fix names any of its commits and
at least `survival` of the source lines it added are still untouched a year
later, so no later fix went through them. "No known bug" is not "no bug": check
the most confident false alarms on clean cases by hand before trusting them.
"""

from __future__ import annotations

import logging
import random
from datetime import date, timedelta

from catchrate.diffparse import parse_diff, to_ranges
from catchrate.github import GitHubClient
from catchrate.gitrepo import GitError, GitRepo
from catchrate.mining.cases import make_review
from catchrate.mining.pr_range import choose_pr, landed_range
from catchrate.repos import RepoSpec
from catchrate.schema import Case, ReviewInput

log = logging.getLogger(__name__)


def survival_rate(repo: GitRepo, spec: RepoSpec, review: ReviewInput, horizon_days: int = 365) -> float | None:
    """Share of the source lines a change added that still exist unchanged `horizon_days` later."""
    head = repo.commits([review.head_commit])[review.head_commit]
    later_date = (head.committed + timedelta(days=horizon_days)).isoformat()
    later = repo.run("rev-list", "-1", "--first-parent", f"--before={later_date}", spec.default_branch).strip()
    if not later or later == head.sha:
        return None
    added = survived = 0
    for fd in parse_diff(repo.diff(review.base_commit, review.head_commit, unified=0)):
        if not fd.new_path or fd.binary or not spec.is_source(fd.new_path):
            continue
        ranges = to_ranges(fd.added_lines())
        if not ranges:
            continue
        added += len(fd.added_lines())
        try:
            entries = repo.blame(f"{head.sha}..{later}", fd.new_path, ranges, reverse=True)
        except GitError:
            continue  # counted as not surviving
        survived += sum(1 for e in entries if e.commit == later)
    return survived / added if added else None


def mine_clean_cases(
    repo: GitRepo,
    gh: GitHubClient,
    spec: RepoSpec,
    *,
    labeled: set[str],
    since: date,
    until: date,
    count: int = 40,
    min_lines: int = 20,
    max_lines: int = 500,
    survival: float = 0.9,
    seed: int = 0,
) -> list[Case]:
    """Sample merged PRs from [since, until] (ticketed ones where the repo marks them) until `count` pass every check."""
    commits = [
        c
        for c in repo.log(spec.default_branch, first_parent=True, since=since.isoformat())
        if c.committed.date() <= until
        and (not spec.clean_subject_prefixes or c.subject.startswith(spec.clean_subject_prefixes))
        and c.sha not in labeled
    ]
    random.Random(seed).shuffle(commits)
    pool = commits[: count * 6]
    prs = gh.commit_pull_requests(spec.owner, spec.name, [c.sha for c in pool])
    cases: list[Case] = []
    seen: set[int] = set()
    checked = 0
    for commit in pool:
        if len(cases) >= count:
            break
        pr = choose_pr(prs.get(commit.sha, []), spec.merge_branches)
        if pr is None or pr.number in seen:
            continue
        seen.add(pr.number)
        landed = landed_range(repo, pr)
        if landed is None or commit.sha not in landed.commits or labeled & set(landed.commits):
            continue
        review = make_review(repo, "pr", landed.base, landed.head, list(landed.commits), pr)
        if not min_lines <= review.changed_lines <= max_lines or not any(spec.is_source(f) for f in review.changed_files):
            continue
        checked += 1
        rate = survival_rate(repo, spec, review)
        if rate is None or rate < survival:
            continue
        cases.append(
            Case(
                case_id=f"{spec.key}-clean-pr{pr.number}",
                repo=spec.key,
                kind="clean",
                review=review,
                bugs=[],
                flags=[f"survival={rate:.2f}"],
            )
        )
        log.info("clean %d/%d: PR #%d (%d lines, %.0f%% survived)", len(cases), count, pr.number, review.changed_lines, rate * 100)
    log.info("Checked survival for %d candidate PRs", checked)
    return sorted(cases, key=lambda c: c.case_id)
