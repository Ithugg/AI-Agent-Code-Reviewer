"""Score SZZ candidates against developer-named bug-introducing commits."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class FixOutcome:
    fix: str
    truth: frozenset[str]  # commits the developers named
    votes: Counter[str]  # SZZ candidate commit -> number of blamed lines
    # commit -> group key (e.g. "pr:123"); commits missing here are their own group
    groups: dict[str, str] = field(default_factory=dict)
    # commit -> commit timestamp, used to break vote ties toward the newer commit
    recency: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Scores:
    fixes: int
    with_candidates: int  # fixes where SZZ proposed at least one commit
    mean_candidates: float  # over fixes with candidates
    recall: float  # fixes whose candidates include the truth / all fixes
    precision: float  # correct candidates / all candidates
    top1: float  # fixes whose most-voted candidate is the truth / all fixes

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def score(outcomes: list[FixOutcome], *, by_group: bool = False) -> Scores:
    """Commit-level scores, or group-level (PR-level) with `by_group`."""
    with_candidates = hits = top_hits = correct = total = 0
    for o in outcomes:
        key = (lambda c: o.groups.get(c, c)) if by_group else (lambda c: c)  # noqa: E731
        truth = {key(c) for c in o.truth}
        grouped: Counter[str] = Counter()
        newest: dict[str, float] = {}
        for commit, n in o.votes.items():
            grouped[key(commit)] += n
            newest[key(commit)] = max(newest.get(key(commit), 0.0), o.recency.get(commit, 0.0))
        if not grouped:
            continue
        with_candidates += 1
        total += len(grouped)
        correct += len(grouped.keys() & truth)
        hits += bool(grouped.keys() & truth)
        top = max(grouped, key=lambda g: (grouped[g], newest[g]))
        top_hits += top in truth
    fixes = len(outcomes)
    return Scores(
        fixes=fixes,
        with_candidates=with_candidates,
        mean_candidates=total / with_candidates if with_candidates else 0.0,
        recall=hits / fixes if fixes else 0.0,
        precision=correct / total if total else 0.0,
        top1=top_hits / fixes if fixes else 0.0,
    )
