"""What a review produces: typed findings, plus the JSON schemas the model fills in."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

CATEGORIES = (
    "logic",
    "edge_case",
    "api_misuse",
    "error_handling",
    "concurrency",
    "performance",
    "security",
    "compatibility",
    "other",
)


class Evidence(BaseModel):
    """A test the agent wrote and ran against the reviewed code."""

    test_id: str
    test_code: str
    output: str
    failed: bool  # failing on the reviewed code is what makes a finding "verified"


class Finding(BaseModel):
    file: str
    start_line: int
    end_line: int
    severity: Literal["critical", "major", "minor"]
    category: str
    explanation: str
    confidence: float
    evidence: Evidence | None = None

    @property
    def verified(self) -> bool:
        return self.evidence is not None and self.evidence.failed


# Strict-mode schemas: every property required, no extra keys, no numeric bounds.
_FINDING_PROPERTIES: dict[str, Any] = {
    "file": {"type": "string", "description": "Repository-relative path of the file with the bug."},
    "start_line": {"type": "integer", "description": "First line of the buggy code in the file after the change."},
    "end_line": {"type": "integer", "description": "Last line of the buggy code (same as start_line for one line)."},
    "severity": {"type": "string", "enum": ["critical", "major", "minor"]},
    "category": {"type": "string", "enum": list(CATEGORIES)},
    "explanation": {
        "type": "string",
        "description": "The failing scenario: the input or state, what happens, and what should happen instead.",
    },
    "confidence": {"type": "number", "description": "How sure you are that this is a real bug, from 0 to 1."},
}
FINDING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": _FINDING_PROPERTIES,
    "required": list(_FINDING_PROPERTIES),
    "additionalProperties": False,
}
REPORT_FINDING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **_FINDING_PROPERTIES,
        "evidence_test_id": {
            "type": "string",
            "description": "Id of a write_and_run_test run that demonstrates the bug, or an empty string.",
        },
    },
    "required": [*_FINDING_PROPERTIES, "evidence_test_id"],
    "additionalProperties": False,
}
REVIEW_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"findings": {"type": "array", "items": FINDING_SCHEMA}},
    "required": ["findings"],
    "additionalProperties": False,
}


def finding_from(data: dict[str, Any], evidence: Evidence | None = None) -> Finding:
    """Validate model-produced finding fields, tolerating small slips (swapped lines, odd confidence)."""
    start, end = int(data["start_line"]), int(data["end_line"])
    category = data.get("category") if data.get("category") in CATEGORIES else "other"
    return Finding(
        file=str(data["file"]).strip().replace("\\", "/").removeprefix("./").lstrip("/"),
        start_line=min(start, end),
        end_line=max(start, end),
        severity=data["severity"] if data.get("severity") in ("critical", "major", "minor") else "minor",
        category=category,
        explanation=str(data["explanation"]).strip(),
        confidence=min(max(float(data.get("confidence", 0.5)), 0.0), 1.0),
        evidence=evidence,
    )
