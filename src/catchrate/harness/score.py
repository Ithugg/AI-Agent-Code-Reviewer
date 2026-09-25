"""Scorecards: catch rate, verified catch rate, false alarms, precision, cost and latency per run."""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from typing import Any

from catchrate.schema import Case

# Bugs fixed on or after these dates cannot have been in the model's training data.
# Training-data cutoffs from platform.claude.com/docs/en/about-claude/models/overview (checked 2026-09-23).
CLEAN_AFTER: dict[str, str] = {
    "claude-fable-5-1": "2026-07-01",
    "claude-opus-5-5": "2026-07-01",
    "claude-sonnet-5": "2026-02-01",
    "claude-haiku-4-5": "2025-08-01",
}


@dataclass
class Scorecard:
    run_id: str
    version: str
    model: str
    split: str | None
    reviews: int
    errors: int
    buggy_cases: int
    clean_cases: int
    bugs: int
    caught: int
    catch_rate: float  # caught bugs / bugs
    case_catch_rate: float  # buggy cases with at least one bug caught / buggy cases
    verified_catch_rate: float  # bugs caught by a finding backed by a failing test / bugs
    precision: float  # findings that match a bug / all findings
    false_alarms_per_clean_pr: float
    false_alarms_per_buggy_pr: float  # findings on buggy cases that match none of their bugs
    mean_cost_usd: float | None
    total_cost_usd: float | None
    mean_seconds: float
    p90_seconds: float
    mean_tool_calls: float
    by_locality: dict[str, float] = field(default_factory=dict)  # in_diff / outside_diff -> catch rate
    after_training_cutoff: dict[str, float] = field(default_factory=dict)  # "bugs", "catch_rate"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _rate(num: int, den: int) -> float:
    return num / den if den else 0.0


def scorecard(run: dict[str, Any], reviews: list[dict[str, Any]], matches: list[dict[str, Any]], cases: dict[str, Case]) -> Scorecard:
    ok = [r for r in reviews if not r["error"] and r["case_id"] in cases]
    caught_by: dict[tuple[str, str], list[int]] = {}
    for m in matches:
        if m["location_ok"] and m["same_bug"]:
            caught_by.setdefault((m["case_id"], m["fix_commit"]), []).append(m["finding_index"])
    matched_findings = {(case_id, i) for (case_id, _), idxs in caught_by.items() for i in idxs}

    bugs = caught = verified = buggy = buggy_caught = clean = 0
    clean_findings = unmatched_buggy = all_findings = 0
    by_loc: dict[str, list[int]] = {}
    cutoff = CLEAN_AFTER.get(run["model"])
    late: list[int] = []
    for review in ok:
        case = cases[review["case_id"]]
        findings = review["findings"]
        all_findings += len(findings)
        if case.kind == "clean":
            clean += 1
            clean_findings += len(findings)
            continue
        buggy += 1
        unmatched_buggy += sum(1 for i in range(len(findings)) if (case.case_id, i) not in matched_findings)
        any_caught = False
        for bug in case.bugs:
            bugs += 1
            idxs = caught_by.get((case.case_id, bug.fix_commit), [])
            hit = bool(idxs)
            caught += hit
            any_caught |= hit
            verified += any(_verified(findings[i]) for i in idxs if i < len(findings))
            by_loc.setdefault(bug.locality, []).append(int(hit))
            if cutoff and bug.fix_date.date().isoformat() >= cutoff:
                late.append(int(hit))
        buggy_caught += any_caught

    costs = [r["cost_usd"] for r in ok if r["cost_usd"] is not None]
    seconds = sorted(r["seconds"] or 0.0 for r in ok)
    return Scorecard(
        run_id=run["run_id"],
        version=run["version"],
        model=run["model"],
        split=run["split"],
        reviews=len(reviews),
        errors=len(reviews) - len(ok),
        buggy_cases=buggy,
        clean_cases=clean,
        bugs=bugs,
        caught=caught,
        catch_rate=_rate(caught, bugs),
        case_catch_rate=_rate(buggy_caught, buggy),
        verified_catch_rate=_rate(verified, bugs),
        precision=_rate(len(matched_findings), all_findings),
        false_alarms_per_clean_pr=_rate(clean_findings, clean),
        false_alarms_per_buggy_pr=_rate(unmatched_buggy, buggy),
        mean_cost_usd=statistics.fmean(costs) if costs else None,
        total_cost_usd=sum(costs) if costs else None,
        mean_seconds=statistics.fmean(seconds) if seconds else 0.0,
        p90_seconds=seconds[int(0.9 * (len(seconds) - 1))] if seconds else 0.0,
        mean_tool_calls=statistics.fmean(r["tool_calls"] or 0 for r in ok) if ok else 0.0,
        by_locality={loc: _rate(sum(hits), len(hits)) for loc, hits in sorted(by_loc.items())},
        after_training_cutoff={"bugs": len(late), "catch_rate": _rate(sum(late), len(late))} if cutoff else {},
    )


def _verified(finding: dict[str, Any]) -> bool:
    evidence = finding.get("evidence")
    return bool(evidence and evidence.get("failed"))


def aggregate(cards: list[Scorecard]) -> dict[tuple[str, str, str | None], dict[str, tuple[float, float, int]]]:
    """(version, model, split) -> metric -> (mean, standard deviation, runs), across seeds."""
    groups: dict[tuple[str, str, str | None], list[Scorecard]] = {}
    for card in cards:
        groups.setdefault((card.version, card.model, card.split), []).append(card)
    metrics = ("catch_rate", "verified_catch_rate", "precision", "false_alarms_per_clean_pr", "mean_cost_usd", "mean_seconds")
    out = {}
    for key, group in groups.items():
        stats = {}
        for metric in metrics:
            values = [v for c in group if (v := getattr(c, metric)) is not None]
            if values:
                stats[metric] = (statistics.fmean(values), statistics.stdev(values) if len(values) > 1 else 0.0, len(values))
        out[key] = stats
    return out
