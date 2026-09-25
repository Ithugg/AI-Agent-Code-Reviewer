"""Work out which commits a merged pull request put on the target branch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

from catchrate.github import GitHubClient
from catchrate.gitrepo import GitError, GitRepo
from catchrate.repos import RepoSpec
from catchrate.schema import PRInfo

Method = Literal["merge", "rebase", "squash", "single"]


@dataclass(frozen=True)
class LandedRange:
    base: str
    head: str
    commits: tuple[str, ...]  # oldest first
    method: Method


def choose_pr(prs: list[PRInfo], branches: tuple[str, ...]) -> PRInfo | None:
    """The lowest-numbered merged PR that targeted one of `branches`."""
    merged = [p for p in prs if p.merged and p.merge_commit and p.base_ref in branches]
    return min(merged, key=lambda p: p.number) if merged else None


def pr_groups(gh: GitHubClient, spec: RepoSpec, shas: Iterable[str]) -> dict[str, str]:
    """Commit -> "pr:<number>" for commits that landed through a PR, else the SHA itself."""
    groups = {}
    for sha, prs in gh.commit_pull_requests(spec.owner, spec.name, sorted(set(shas))).items():
        pr = choose_pr(prs, spec.merge_branches)
        groups[sha] = f"pr:{pr.number}" if pr else sha
    return groups


def _norm(headline: str) -> str:
    return " ".join(headline.split())[:60]


def landed_range(repo: GitRepo, pr: PRInfo) -> LandedRange | None:
    """The base, head, and commits `pr` landed, or None if its merge commit isn't in `repo`."""
    if not pr.merge_commit:
        return None
    try:
        head = repo.commits([pr.merge_commit]).get(pr.merge_commit)
    except GitError:
        return None
    if head is None or not head.parents:
        return None
    if len(head.parents) > 1:  # a real merge commit: the PR is everything on the second parent
        base = head.parents[0]
        return LandedRange(base, head.sha, tuple(repo.rev_list(base, head.parents[1])), "merge")
    if pr.commit_count <= 1:
        return LandedRange(head.parents[0], head.sha, (head.sha,), "single")
    # Rebase-merged PRs land their commits (with new SHAs) as a run ending at the
    # merge commit; squash-merged ones land a single commit.
    landed = list(reversed(repo.first_parent_chain(head.sha, pr.commit_count)))
    # GitHub returns at most the first 100 headlines, so compare that prefix.
    headlines = [_norm(h) for h in pr.commit_headlines]
    if (
        len(landed) == pr.commit_count
        and landed[0].parents
        and headlines
        and [_norm(c.subject) for c in landed[: len(headlines)]] == headlines
    ):
        return LandedRange(landed[0].parents[0], head.sha, tuple(c.sha for c in landed), "rebase")
    return LandedRange(head.parents[0], head.sha, (head.sha,), "squash")
