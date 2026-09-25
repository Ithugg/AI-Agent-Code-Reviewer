"""Agent versions v0-v4 and ablation variants like "v1:calls=10" or "v0:context=diff+files"."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

Context = Literal["diff", "diff+files", "agentic"]
READ_TOOLS = ("read_diff", "read_file", "search_code", "find_callers", "list_dir", "report_finding")


@dataclass(frozen=True)
class AgentVersion:
    name: str
    tools: tuple[str, ...]  # empty: one structured-output call, no tool loop
    context: Context
    max_tool_calls: int = 30
    evidence_required: bool = False  # v3+: only report bugs proven by a failing test
    effort: str | None = None  # None keeps the model's default

    @property
    def agentic(self) -> bool:
        return bool(self.tools)


VERSIONS: dict[str, AgentVersion] = {
    # v0 baseline: the diff and one prompt.
    "v0": AgentVersion("v0", tools=(), context="diff", max_tool_calls=0),
    # v1 explorer: read files, search, find callers.
    "v1": AgentVersion("v1", tools=READ_TOOLS, context="agentic"),
    # v2 tester: also run the existing test suite.
    "v2": AgentVersion("v2", tools=READ_TOOLS + ("run_tests",), context="agentic"),
    # v3 prover: also write new tests, and back each finding with one. Needs Docker.
    "v3": AgentVersion("v3", tools=READ_TOOLS + ("run_tests", "write_and_run_test"), context="agentic", evidence_required=True),
    # v4 tuned: placeholder until the ablations pick the best settings; starts as v3 with more room.
    "v4": AgentVersion(
        "v4", tools=READ_TOOLS + ("run_tests", "write_and_run_test"), context="agentic", max_tool_calls=40, evidence_required=True
    ),
}

_KNOBS = {"calls": "max_tool_calls", "context": "context", "evidence": "evidence_required", "effort": "effort"}


def resolve(spec: str) -> AgentVersion:
    """"v1" or "v1:calls=10,effort=low" -> an AgentVersion named after the full spec."""
    name, _, knobs = spec.partition(":")
    if name not in VERSIONS:
        raise ValueError(f"unknown version {name!r}; choose from {sorted(VERSIONS)}")
    version = replace(VERSIONS[name], name=spec)
    for knob in filter(None, knobs.split(",")):
        key, _, value = knob.partition("=")
        if key not in _KNOBS:
            raise ValueError(f"unknown knob {key!r}; choose from {sorted(_KNOBS)}")
        field = _KNOBS[key]
        if field == "max_tool_calls":
            version = replace(version, max_tool_calls=int(value))
        elif field == "evidence_required":
            version = replace(version, evidence_required=value in ("on", "true", "1"))
        elif field == "context":
            if value not in ("diff", "diff+files", "agentic"):
                raise ValueError(f"context must be diff, diff+files or agentic, not {value!r}")
            tools = version.tools if value == "agentic" else ()
            version = replace(version, context=value, tools=tools)  # type: ignore[arg-type]
        else:
            version = replace(version, effort=value)
    if version.context == "agentic" and not version.tools:
        raise ValueError("an agentic context needs tools")
    return version
