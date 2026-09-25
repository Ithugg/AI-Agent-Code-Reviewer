import json

import httpx
import pytest

from catchrate.github import GitHubClient

SHA_A = "a" * 40
SHA_B = "b" * 40
NODE = {
    "number": 17554,
    "title": "Fixed #28586 -- Added model field fetch modes.",
    "body": "Trac ticket: 28586",
    "url": "https://github.com/django/django/pull/17554",
    "merged": True,
    "mergedAt": "2025-10-16T12:00:00Z",
    "baseRefName": "main",
    "additions": 900,
    "deletions": 100,
    "changedFiles": 20,
    "mergeCommit": {"oid": "e" * 40},
    "commits": {
        "totalCount": 2,
        "nodes": [
            {"commit": {"messageHeadline": "Refs #28586 -- One."}},
            {"commit": {"messageHeadline": "Fixed #28586 -- Two."}},
        ],
    },
}


def test_looks_up_commits_in_one_batch_and_caches_them(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        assert request.headers["Authorization"] == "bearer t0k"
        data = {"repository": {"c0": {"associatedPullRequests": {"nodes": [NODE]}}, "c1": None}}
        return httpx.Response(200, json={"data": data})

    with GitHubClient("t0k", tmp_path, transport=httpx.MockTransport(handler)) as gh:
        first = gh.commit_pull_requests("django", "django", [SHA_A, SHA_B, SHA_A])
        again = gh.commit_pull_requests("django", "django", [SHA_B, SHA_A])

    assert len(seen) == 1
    assert seen[0]["variables"] == {"owner": "django", "name": "django", "s0": SHA_A, "s1": SHA_B}
    (pr,) = first[SHA_A]
    assert (pr.number, pr.merge_commit, pr.commit_count, pr.base_ref, pr.merged) == (17554, "e" * 40, 2, "main", True)
    assert pr.commit_headlines == ["Refs #28586 -- One.", "Fixed #28586 -- Two."]
    assert first[SHA_B] == []
    assert again == first


def test_rejects_input_that_is_not_a_sha_or_repo_name(tmp_path):
    with GitHubClient("t", tmp_path, transport=httpx.MockTransport(lambda r: httpx.Response(500))) as gh:
        with pytest.raises(ValueError):
            gh.commit_pull_requests("django", "django", ["abc1234"])
        with pytest.raises(ValueError):
            gh.commit_pull_requests("django", 'x") {', [SHA_A])


def test_retries_server_errors(tmp_path, monkeypatch):
    monkeypatch.setattr("catchrate.github.time.sleep", lambda seconds: None)
    responses = iter([httpx.Response(502), httpx.Response(200, json={"data": {"repository": {"c0": None}}})])

    with GitHubClient("t", tmp_path, transport=httpx.MockTransport(lambda r: next(responses))) as gh:
        assert gh.commit_pull_requests("django", "django", [SHA_A]) == {SHA_A: []}
