"""Match findings to known bugs: a location check, then an LLM judge for "same problem?".

A finding catches a bug when it points at the bug's lines (within a small
window) and describes the same defect. For bugs outside the diff, pointing at
any file the change touched also counts as a location match, because a reviewer
would flag the change that causes the problem, not the old code it breaks.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from catchrate.agent.findings import Finding
from catchrate.agent.llm import Usage
from catchrate.agent.pricing import cost_usd
from catchrate.harness.runner import SessionFactory
from catchrate.schema import BugLabel, Case

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"reason": {"type": "string"}, "same_bug": {"type": "boolean"}},
    "required": ["reason", "same_bug"],
    "additionalProperties": False,
}

JUDGE_SYSTEM = """You grade an automated code reviewer. You will see a bug that is known to be in a pull request \
(taken from the later commit that fixed it) and one finding the reviewer reported on that pull request.

Decide whether the finding identifies the same defect as the known bug. It does if a maintainer who read the \
finding would be led to the problem the fix addresses, even if the wording, the exact lines, or the suggested fix \
differ. It does not if it describes a different problem, a vague concern, or only a symptom that points somewhere \
else. Give a one or two sentence reason first, then the decision."""


@dataclass(frozen=True)
class Decision:
    same_bug: bool
    reason: str


class Judge(Protocol):
    model: str

    def decide(self, case: Case, bug: BugLabel, finding: Finding) -> Decision: ...


def location_ok(finding: Finding, bug: BugLabel, case: Case, window: int = 5) -> bool:
    for line in bug.bug_lines:
        if line.path == finding.file and finding.start_line - window <= line.line <= finding.end_line + window:
            return True
    return bug.locality == "outside_diff" and finding.file in case.review.changed_files


def judge_prompt(case: Case, bug: BugLabel, finding: Finding, fix_diff: str, code_at: Callable[[str, int, int], str]) -> str:
    lines = sorted({(b.path, b.line) for b in bug.bug_lines})[:12]
    located = "\n".join(f"{path}:{n}: {code_at(path, n, n).strip()}" for path, n in lines)
    return f"""<known_bug>
<summary>{bug.bug_summary}</summary>
<fix_commit_subject>{bug.fix_subject}</fix_commit_subject>
<fix_diff>
{fix_diff}
</fix_diff>
<buggy_lines_in_the_pull_request>
{located}
</buggy_lines_in_the_pull_request>
</known_bug>
<pull_request_title>{case.review.title}</pull_request_title>
<finding>
<location>{finding.file}:{finding.start_line}-{finding.end_line}</location>
<code>
{code_at(finding.file, finding.start_line, finding.end_line)}
</code>
<severity>{finding.severity}</severity>
<explanation>{finding.explanation}</explanation>
</finding>
Does the finding identify the same defect as the known bug?"""


class LLMJudge:
    """Asks a strong model for a structured yes/no. Build one per worker thread or share: it is thread-safe."""

    def __init__(
        self,
        sessions: SessionFactory,
        model: str,
        fix_diff: Callable[[BugLabel], str],
        code_at: Callable[[Case], Callable[[str, int, int], str]],
    ):
        self.sessions = sessions
        self.model = model
        self.fix_diff = fix_diff
        self.code_at = code_at
        self.usage = Usage()
        self._lock = threading.Lock()

    def decide(self, case: Case, bug: BugLabel, finding: Finding) -> Decision:
        session = self.sessions(JUDGE_SYSTEM, [], JUDGE_SCHEMA, None)
        turn = session.send(user=judge_prompt(case, bug, finding, self.fix_diff(bug), self.code_at(case)))
        with self._lock:
            self.usage.add(turn.usage)
        try:
            data = json.loads(turn.text)
            return Decision(bool(data["same_bug"]), str(data["reason"]))
        except (json.JSONDecodeError, KeyError, TypeError):
            return Decision(False, f"unparseable judge output: {turn.text[:200]}")

    @property
    def cost_usd(self) -> float | None:
        return cost_usd(self.model, self.usage)


@dataclass(frozen=True)
class MatchRecord:
    fix_commit: str
    finding_index: int
    location_ok: bool
    same_bug: bool | None  # None when the location check already ruled it out
    reason: str


def match_review(case: Case, findings: list[Finding], judge: Judge, *, window: int = 5) -> list[MatchRecord]:
    """Judge every (bug, finding) pair that passes the location check."""
    records = []
    for bug in case.bugs:
        for i, finding in enumerate(findings):
            if not location_ok(finding, bug, case, window):
                continue
            decision = judge.decide(case, bug, finding)
            records.append(MatchRecord(bug.fix_commit, i, True, decision.same_bug, decision.reason))
    return records
