"""Review a live GitHub pull request with the agent and, only when asked, post the findings (Week 7).

Findings on lines the diff shows become inline comments; the rest go in the
review body, because GitHub only accepts comments on lines inside the diff.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from catchrate.agent.findings import Finding
from catchrate.agent.loop import ReviewResult, run_review
from catchrate.agent.prompts import system_prompt, user_prompt
from catchrate.agent.tools import ToolBox
from catchrate.agent.versions import AgentVersion
from catchrate.diffparse import parse_diff
from catchrate.github import GitHubClient
from catchrate.gitrepo import GitError, GitRepo
from catchrate.harness.runner import SessionFactory
from catchrate.schema import Case, ReviewInput

_TARGET_RE = re.compile(r"([\w.-]+)/([\w.-]+)#(\d+)")
_SHA_RE = re.compile(r"[0-9a-f]{40}")
MARKER = "<!-- catch-rate-review -->"


@dataclass(frozen=True)
class PullRequest:
    owner: str
    name: str
    number: int
    review: ReviewInput


def parse_target(text: str) -> tuple[str, str, int]:
    match = _TARGET_RE.fullmatch(text.strip())
    if not match:
        raise ValueError(f"expected owner/repo#number, got {text!r}")
    return match[1], match[2], int(match[3])


def fetch_pull_request(gh: GitHubClient, owner: str, name: str, number: int) -> PullRequest:
    base = f"/repos/{owner}/{name}/pulls/{number}"
    meta = gh.rest("GET", base).json()
    diff = gh.rest("GET", base, accept="application/vnd.github.diff").text
    files: list[str] = []
    for page in range(1, 31):  # GitHub caps the list at 3,000 files
        batch = gh.rest("GET", f"{base}/files?per_page=100&page={page}").json()
        files += [f["filename"] for f in batch]
        if len(batch) < 100:
            break
    head, base_sha = meta["head"]["sha"], meta["base"]["sha"]
    if not (_SHA_RE.fullmatch(head) and _SHA_RE.fullmatch(base_sha)):
        raise ValueError("unexpected commit ids from GitHub")
    review = ReviewInput(
        review_unit="pr",
        base_commit=base_sha,
        head_commit=head,
        commits=[head],
        pr_number=number,
        pr_url=meta.get("html_url"),
        title=meta.get("title") or "",
        description=meta.get("body") or "",
        diff=diff,
        changed_files=files,
        changed_lines=int(meta.get("additions", 0)) + int(meta.get("deletions", 0)),
    )
    return PullRequest(owner, name, number, review)


def fetch_head(repo_dir: Path, pr: PullRequest) -> GitRepo:
    """A bare repository holding just the PR's head tree (depth 1), for the read-only tools."""
    if not repo_dir.exists():
        repo_dir.mkdir(parents=True)
        subprocess.run(["git", "init", "--bare", "--quiet", str(repo_dir)], check=True, capture_output=True)
    repo = GitRepo(repo_dir)
    url = f"https://github.com/{pr.owner}/{pr.name}.git"
    repo.run("fetch", "--quiet", "--depth=1", url, f"pull/{pr.number}/head")
    fetched = repo.run("rev-parse", "FETCH_HEAD").strip()
    if fetched != pr.review.head_commit:
        raise GitError(f"PR head moved while reviewing ({pr.review.head_commit[:10]} -> {fetched[:10]}); run again")
    return repo


def review_pull_request(pr: PullRequest, repo: GitRepo, version: AgentVersion, sessions: SessionFactory, project: str) -> ReviewResult:
    if any(tool in version.tools for tool in ("run_tests", "write_and_run_test")):
        raise ValueError("live PR reviews use read-only versions (v0 or v1); tests would run the PR's code")
    case = Case(case_id=f"{pr.owner}/{pr.name}#{pr.number}", repo=pr.name, review=pr.review, bugs=[])
    toolbox = ToolBox(repo, case, version.tools) if version.agentic else None
    tools = toolbox.specs() if toolbox else []
    from catchrate.agent.findings import REVIEW_OUTPUT_SCHEMA

    session = sessions(system_prompt(version, project), tools, None if version.agentic else REVIEW_OUTPUT_SCHEMA, version.effort)
    return run_review(case.case_id, version, session, user_prompt(pr.review), toolbox)


def commentable_lines(diff: str) -> dict[str, set[int]]:
    """New-side lines GitHub accepts comments on: every line inside a hunk."""
    lines: dict[str, set[int]] = {}
    for fd in parse_diff(diff):
        if fd.new_path:
            lines[fd.new_path] = {n for h in fd.hunks for n in range(h.new_start, h.new_start + h.new_count)}
    return lines


def build_review(findings: list[Finding], diff: str, *, signature: str, min_confidence: float = 0.0) -> dict[str, Any]:
    allowed = commentable_lines(diff)
    shown = [f for f in findings if f.confidence >= min_confidence]
    comments, elsewhere = [], []
    for f in shown:
        body = _comment_body(f, signature)
        if f.end_line in allowed.get(f.file, set()):
            comments.append({"path": f.file, "line": f.end_line, "side": "RIGHT", "body": body})
        else:
            elsewhere.append(f"- `{f.file}:{f.start_line}-{f.end_line}`: {body}")
    if not shown:
        summary = "No likely bugs found."
    else:
        summary = f"{len(shown)} possible bug{'s' if len(shown) != 1 else ''} found."
    body = "\n\n".join([MARKER, f"**Catch Rate review**: {summary}", *(["Outside the diff:\n" + "\n".join(elsewhere)] if elsewhere else []), f"_{signature}_"])
    return {"event": "COMMENT", "body": body, "comments": comments}


def post_review(gh: GitHubClient, pr: PullRequest, payload: dict[str, Any]) -> str:
    resp = gh.rest("POST", f"/repos/{pr.owner}/{pr.name}/pulls/{pr.number}/reviews",
                   payload={**payload, "commit_id": pr.review.head_commit})  # fmt: skip
    return str(resp.json().get("html_url", ""))


def _comment_body(f: Finding, signature: str) -> str:
    text = f"**{f.severity} · {f.category.replace('_', ' ')}** (confidence {f.confidence:.1f}): {f.explanation}"
    if f.verified and f.evidence is not None:
        text += f"\n\nA test fails on this code:\n```\n{f.evidence.output[:1500]}\n```"
    return text
