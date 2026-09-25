"""Minimal GitHub GraphQL client with an on-disk cache for commit -> PR lookups."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

import httpx

from catchrate.schema import PRInfo

log = logging.getLogger(__name__)

API_URL = "https://api.github.com/graphql"
REST_URL = "https://api.github.com"
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
_PR_FIELDS = """
number title body url merged mergedAt baseRefName additions deletions changedFiles
mergeCommit { oid }
commits(first: 100) { totalCount nodes { commit { messageHeadline } } }
"""


class GitHubError(RuntimeError):
    pass


def discover_token() -> str | None:
    """A token from GITHUB_TOKEN / GH_TOKEN, or from a logged-in `gh` CLI."""
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    try:
        proc = subprocess.run(["gh", "auth", "token"], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    token = proc.stdout.decode("utf-8", "replace").strip()
    return token if proc.returncode == 0 and token else None


class GitHubClient:
    def __init__(
        self,
        token: str,
        cache_dir: Path,
        *,
        transport: httpx.BaseTransport | None = None,
        batch_size: int = 25,
        max_retries: int = 5,
    ):
        self._http = httpx.Client(
            headers={"Authorization": f"bearer {token}", "User-Agent": "catchrate-miner"},
            timeout=60.0,
            transport=transport,
        )
        self.cache_dir = cache_dir
        self.batch_size = batch_size
        self.max_retries = max_retries

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(self.max_retries):
            resp = self._http.post(API_URL, json={"query": query, "variables": variables})
            if resp.status_code in (403, 429) or resp.status_code >= 500:
                wait = _retry_delay(resp, attempt)
                log.warning("GitHub returned %s; retrying in %.0fs", resp.status_code, wait)
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                raise GitHubError(f"GitHub GraphQL returned {resp.status_code}: {resp.text[:300]}")
            payload = resp.json()
            if payload.get("data") is None:
                raise GitHubError(f"GitHub GraphQL errors: {payload.get('errors')}")
            if payload.get("errors"):
                log.debug("GitHub GraphQL partial errors: %s", payload["errors"])
            return payload["data"]
        raise GitHubError(f"GitHub GraphQL still failing after {self.max_retries} attempts")

    def rest(self, method: str, path: str, *, accept: str = "application/vnd.github+json",
             payload: dict[str, Any] | None = None) -> httpx.Response:  # fmt: skip
        """One REST call, retrying rate limits and server errors; 4xx errors raise GitHubError."""
        if not path.startswith("/"):
            raise ValueError(f"REST path must start with '/': {path!r}")
        for attempt in range(self.max_retries):
            resp = self._http.request(method, REST_URL + path, headers={"Accept": accept}, json=payload)
            limited = resp.status_code == 429 or (resp.status_code == 403 and "rate limit" in resp.text.lower())
            if limited or resp.status_code >= 500:
                time.sleep(_retry_delay(resp, attempt))
                continue
            if resp.status_code >= 400:
                raise GitHubError(f"{method} {path} returned {resp.status_code}: {resp.text[:300]}")
            return resp
        raise GitHubError(f"{method} {path} still failing after {self.max_retries} attempts")

    def commit_pull_requests(self, owner: str, name: str, shas: Iterable[str]) -> dict[str, list[PRInfo]]:
        """Pull requests associated with each commit (cached on disk per commit)."""
        if not (_NAME_RE.fullmatch(owner) and _NAME_RE.fullmatch(name)):
            raise ValueError(f"bad repository name: {owner}/{name}")
        cache = self.cache_dir / f"{owner}__{name}"
        result: dict[str, list[PRInfo]] = {}
        missing: list[str] = []
        for sha in dict.fromkeys(shas):
            if not _SHA_RE.fullmatch(sha):
                raise ValueError(f"not a full lowercase commit SHA: {sha!r}")
            cached = _read_cache(cache / f"{sha}.json")
            if cached is None:
                missing.append(sha)
            else:
                result[sha] = cached
        for start in range(0, len(missing), self.batch_size):
            batch = missing[start : start + self.batch_size]
            variables: dict[str, Any] = {"owner": owner, "name": name}
            variables.update({f"s{i}": sha for i, sha in enumerate(batch)})
            repository = self.graphql(_commit_prs_query(len(batch)), variables).get("repository") or {}
            for i, sha in enumerate(batch):
                node = repository.get(f"c{i}") or {}
                nodes = (node.get("associatedPullRequests") or {}).get("nodes") or []
                prs = [_pr_from_node(n) for n in nodes]
                _write_cache(cache / f"{sha}.json", prs)
                result[sha] = prs
            log.info("GitHub: looked up %d/%d commits", start + len(batch), len(missing))
        return result


def _commit_prs_query(n: int) -> str:
    params = ", ".join(f"$s{i}: GitObjectID!" for i in range(n))
    fields = " ".join(
        f"c{i}: object(oid: $s{i}) {{ ... on Commit {{ associatedPullRequests(first: 5) {{ nodes {{ {_PR_FIELDS} }} }} }} }}"
        for i in range(n)
    )
    return f"query($owner: String!, $name: String!, {params}) {{ repository(owner: $owner, name: $name) {{ {fields} }} }}"


def _pr_from_node(node: dict[str, Any]) -> PRInfo:
    commits = node.get("commits") or {}
    return PRInfo(
        number=node["number"],
        title=node.get("title") or "",
        body=node.get("body") or "",
        url=node.get("url") or "",
        merged=bool(node.get("merged")),
        merged_at=node.get("mergedAt"),
        base_ref=node.get("baseRefName") or "",
        merge_commit=(node.get("mergeCommit") or {}).get("oid"),
        commit_count=commits.get("totalCount") or 0,
        commit_headlines=[((n or {}).get("commit") or {}).get("messageHeadline") or "" for n in commits.get("nodes") or []],
        additions=node.get("additions") or 0,
        deletions=node.get("deletions") or 0,
        changed_files=node.get("changedFiles") or 0,
    )


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    if retry_after := resp.headers.get("retry-after"):
        return min(float(retry_after), 900.0)
    if resp.headers.get("x-ratelimit-remaining") == "0" and (reset := resp.headers.get("x-ratelimit-reset")):
        return min(max(float(reset) - time.time(), 0.0) + 1.0, 900.0)
    return 2.0 * 2**attempt


def _read_cache(path: Path) -> list[PRInfo] | None:
    try:
        return [PRInfo.model_validate(item) for item in json.loads(path.read_text(encoding="utf-8"))]
    except FileNotFoundError:
        return None


def _write_cache(path: Path, prs: list[PRInfo]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A unique temp file per writer keeps concurrent runs from clobbering each other.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps([p.model_dump(mode="json") for p in prs]))
    os.replace(tmp, path)
