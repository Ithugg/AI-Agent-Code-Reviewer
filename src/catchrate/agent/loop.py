"""Run one review: a single structured call (v0) or a budgeted tool loop (v1+)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from catchrate.agent.findings import Finding, finding_from
from catchrate.agent.llm import Session, ToolResult, Turn, Usage
from catchrate.agent.tools import ToolBox, truncate
from catchrate.agent.versions import AgentVersion

EXTRA_TURNS_AFTER_BUDGET = 3  # turns left to report findings once the tool budget is spent
_TRACE_TEXT = 2000
_TRACE_RESULT = 1500


@dataclass
class ReviewResult:
    case_id: str
    version: str
    model: str
    findings: list[Finding]
    usage: Usage
    seconds: float
    tool_calls: int
    turns: int
    stop_reason: str
    error: str | None = None
    trace: list[dict[str, Any]] = field(default_factory=list)


def run_review(
    case_id: str,
    version: AgentVersion,
    session: Session,
    user_message: str,
    toolbox: ToolBox | None = None,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> ReviewResult:
    start = clock()
    usage = Usage()
    trace: list[dict[str, Any]] = []

    def finish(findings: list[Finding], turns: int, calls: int, stop: str, error: str | None = None) -> ReviewResult:
        return ReviewResult(case_id, version.name, session.model, findings, usage, clock() - start, calls, turns, stop, error, trace)

    turn = session.send(user=user_message)
    usage.add(turn.usage)
    trace.append(_trace(turn))
    if not version.agentic:
        findings, error = parse_findings(turn.text)
        return finish(findings, 1, 0, turn.stop_reason, error)

    assert toolbox is not None, "agentic versions need a toolbox"
    turns, calls, over_budget_turns = 1, 0, 0
    while turn.tool_calls and turn.stop_reason not in ("refusal", "max_tokens"):
        results: list[ToolResult] = []
        for call in turn.tool_calls:
            if call.name != "report_finding" and calls >= version.max_tool_calls:
                results.append(
                    ToolResult(call.id, "Tool budget used up. Report any bugs you found with report_finding, then stop.", True)
                )
                continue
            calls += call.name != "report_finding"
            results.append(toolbox.run(call))
        trace[-1]["results"] = [{"ok": not r.is_error, "content": truncate(r.content, _TRACE_RESULT)} for r in results]
        if calls >= version.max_tool_calls:
            over_budget_turns += 1
            if over_budget_turns > EXTRA_TURNS_AFTER_BUDGET:
                return finish(toolbox.findings, turns, calls, "tool_budget")
        if turns >= version.max_tool_calls + 20:  # e.g. a model that only ever calls report_finding
            return finish(toolbox.findings, turns, calls, "turn_limit")
        turn = session.send(results=results)
        usage.add(turn.usage)
        trace.append(_trace(turn))
        turns += 1
    return finish(toolbox.findings, turns, calls, turn.stop_reason)


def parse_findings(text: str) -> tuple[list[Finding], str | None]:
    """Findings from a structured-output reply ({"findings": [...]}), skipping malformed items."""
    try:
        items = json.loads(text)["findings"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        return [], f"unparseable output: {type(exc).__name__}"
    findings, bad = [], 0
    for item in items if isinstance(items, list) else []:
        try:
            findings.append(finding_from(item))
        except (KeyError, TypeError, ValueError):
            bad += 1
    return findings, f"{bad} malformed findings skipped" if bad else None


def _trace(turn: Turn) -> dict[str, Any]:
    return {
        "text": truncate(turn.text, _TRACE_TEXT),
        "tool_calls": [{"name": c.name, "input": c.input} for c in turn.tool_calls],
        "stop_reason": turn.stop_reason,
        "usage": vars(turn.usage).copy(),
    }
