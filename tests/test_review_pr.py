import json

import httpx
import pytest

from catchrate.agent.findings import Finding
from catchrate.agent.llm import ScriptedSession, ToolCall, Turn, Usage
from catchrate.agent.versions import VERSIONS
from catchrate.github import GitHubClient
from catchrate.review_pr import (
    MARKER,
    build_review,
    commentable_lines,
    fetch_pull_request,
    parse_target,
    post_review,
    review_pull_request,
)

DIFF = """diff --git a/pkg/calc.py b/pkg/calc.py
--- a/pkg/calc.py
+++ b/pkg/calc.py
@@ -1,3 +1,3 @@
 def inc(x):
-    return x + 1
+    return x - 1

"""


def _finding(**kw):
    base = dict(file="pkg/calc.py", start_line=2, end_line=2, severity="major", category="logic",
                explanation="inc(1) returns 0.", confidence=0.9)  # fmt: skip
    return Finding(**{**base, **kw})


def test_parse_target():
    assert parse_target("django/django#20009") == ("django", "django", 20009)
    with pytest.raises(ValueError):
        parse_target("django/django 20009")


def test_findings_on_diff_lines_become_inline_comments():
    findings = [_finding(), _finding(file="pkg/other.py", start_line=9, end_line=9), _finding(confidence=0.2)]

    payload = build_review(findings, DIFF, signature="Catch Rate v1", min_confidence=0.5)

    assert commentable_lines(DIFF) == {"pkg/calc.py": {1, 2, 3}}
    assert [(c["path"], c["line"], c["side"]) for c in payload["comments"]] == [("pkg/calc.py", 2, "RIGHT")]
    assert payload["body"].startswith(MARKER) and "2 possible bugs found." in payload["body"]
    assert "`pkg/other.py:9-9`" in payload["body"]  # not in the diff, so it goes in the body
    assert build_review([], DIFF, signature="s")["comments"] == []


def test_fetch_review_and_post_against_a_fake_github(toy, tmp_path):
    base = toy.commit("Initial", {"pkg/calc.py": "def inc(x):\n    return x + 1\n"})
    head = toy.commit("Changed inc", {"pkg/calc.py": "def inc(x):\n    return x - 1\n"})
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST":
            posted.append(json.loads(request.content))
            return httpx.Response(200, json={"html_url": "https://github.com/o/r/pull/5#review-1"})
        if path.endswith("/files"):
            return httpx.Response(200, json=[{"filename": "pkg/calc.py"}])
        if request.headers["Accept"] == "application/vnd.github.diff":
            return httpx.Response(200, text=DIFF)
        return httpx.Response(200, json={"title": "Change inc", "body": "Simpler.", "html_url": "u",
                                         "head": {"sha": head}, "base": {"sha": base}, "additions": 1, "deletions": 1})  # fmt: skip

    with GitHubClient("t", tmp_path, transport=httpx.MockTransport(handler)) as gh:
        pr = fetch_pull_request(gh, "o", "r", 5)
        assert (pr.review.head_commit, pr.review.changed_files, pr.review.changed_lines) == (head, ["pkg/calc.py"], 2)

        report = {k: v for k, v in _finding().model_dump().items() if k != "evidence"} | {"evidence_test_id": ""}
        script = [Turn("", [ToolCall("c1", "report_finding", report)], "tool_use", Usage()), Turn("Done.", [], "end_turn", Usage())]
        sessions = lambda *args: ScriptedSession(list(script))  # noqa: E731
        result = review_pull_request(pr, toy.repo, VERSIONS["v1"], sessions, "Toy")
        assert len(result.findings) == 1

        url = post_review(gh, pr, build_review(result.findings, pr.review.diff, signature="s"))

    assert url.endswith("#review-1")
    assert posted[0]["commit_id"] == head and posted[0]["comments"][0]["line"] == 2
    with pytest.raises(ValueError):
        review_pull_request(pr, toy.repo, VERSIONS["v2"], sessions, "Toy")  # no running a PR's code
