"""Rough cost estimates for a run, computed offline (no API calls).

Token counts are approximated from characters, and agentic runs assume a
typical number of tool rounds. Treat the result as an order of magnitude; the
first real smoke test on a few cases is the number to trust.
"""

from __future__ import annotations

from dataclasses import dataclass

from catchrate.agent.llm import Usage
from catchrate.agent.pricing import cost_usd
from catchrate.agent.prompts import system_prompt, user_prompt
from catchrate.agent.tools import SPECS
from catchrate.agent.versions import AgentVersion
from catchrate.schema import Case

CHARS_PER_TOKEN = 3.2  # code and diffs; Claude's current tokenizer runs ~30% denser than older ones
TYPICAL_TOOL_ROUNDS = 15  # tool rounds in an agentic review, capped by the version's budget
TOKENS_ADDED_PER_ROUND = 2500  # tool result + the assistant turn that asked for it
OUTPUT_PER_ROUND = 700  # thinking and the tool call
SINGLE_CALL_OUTPUT = 2500  # thinking plus a JSON list of findings


@dataclass(frozen=True)
class Estimate:
    cases: int
    usage: Usage
    cost_usd: float | None
    per_case_usd: float | None


def _tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN)


def estimate(cases: list[Case], version: AgentVersion, model: str, project: str, *, batch: bool = False) -> Estimate:
    total = Usage()
    system = _tokens(system_prompt(version, project))
    tools = sum(_tokens(str(SPECS[name].input_schema) + SPECS[name].description) for name in version.tools)
    for case in cases:
        prefix = system + tools + _tokens(user_prompt(case.review))
        if not version.agentic:
            # The system prompt is shared across cases (cached); the diff is not.
            total.add(Usage(input_tokens=prefix - system, output_tokens=SINGLE_CALL_OUTPUT, cache_read_tokens=system))
            continue
        rounds = min(version.max_tool_calls, TYPICAL_TOOL_ROUNDS) + 1
        usage = Usage(cache_write_tokens=prefix)
        for turn in range(1, rounds):
            context = prefix + turn * TOKENS_ADDED_PER_ROUND
            usage.add(Usage(cache_read_tokens=context - TOKENS_ADDED_PER_ROUND, cache_write_tokens=TOKENS_ADDED_PER_ROUND))
        usage.output_tokens = rounds * OUTPUT_PER_ROUND
        total.add(usage)
    cost = cost_usd(model, total, batch=batch)
    return Estimate(len(cases), total, cost, cost / len(cases) if cost is not None and cases else None)
