"""How often the LLM judge agrees with a human, on hand-labeled (bug, finding) pairs."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

JUDGE_TASK = "judge"  # label task name in the store


def item_id(run_id: str, case_id: str, fix_commit: str, finding_index: int) -> str:
    return f"{run_id}|{case_id}|{fix_commit}|{finding_index}"


@dataclass(frozen=True)
class Agreement:
    n: int
    accuracy: float
    kappa: float  # Cohen's kappa: agreement beyond chance
    both_yes: int
    judge_only: int  # judge said same bug, human said no
    human_only: int
    both_no: int


def agreement(pairs: list[tuple[bool, bool]]) -> Agreement:
    """`pairs` are (judge says same bug, human says same bug)."""
    n = len(pairs)
    both_yes = sum(1 for j, h in pairs if j and h)
    judge_only = sum(1 for j, h in pairs if j and not h)
    human_only = sum(1 for j, h in pairs if h and not j)
    both_no = n - both_yes - judge_only - human_only
    if n == 0:
        return Agreement(0, 0.0, 0.0, 0, 0, 0, 0)
    observed = (both_yes + both_no) / n
    judge_yes, human_yes = (both_yes + judge_only) / n, (both_yes + human_only) / n
    chance = judge_yes * human_yes + (1 - judge_yes) * (1 - human_yes)
    kappa = 1.0 if chance == 1 else (observed - chance) / (1 - chance)
    return Agreement(n, observed, kappa, both_yes, judge_only, human_only, both_no)


def sample_for_labeling(matches: list[dict[str, Any]], k: int = 50, seed: int = 0) -> list[dict[str, Any]]:
    """Up to k judged pairs, half the judge's yeses and half its noes where possible."""
    judged = [m for m in matches if m["same_bug"] is not None]
    yes = [m for m in judged if m["same_bug"]]
    no = [m for m in judged if not m["same_bug"]]
    rng = random.Random(seed)
    rng.shuffle(yes)
    rng.shuffle(no)
    take_yes = min(len(yes), max(k // 2, k - len(no)))
    picked = yes[:take_yes] + no[: k - take_yes]
    return sorted(picked, key=lambda m: (m["case_id"], m["fix_commit"], m["finding_index"]))


def judge_agreement(matches: list[dict[str, Any]], labels: dict[str, dict[str, Any]]) -> Agreement:
    pairs = []
    for m in matches:
        label = labels.get(item_id(m["run_id"], m["case_id"], m["fix_commit"], m["finding_index"]))
        if label and label["label"] in ("same", "different") and m["same_bug"] is not None:
            pairs.append((bool(m["same_bug"]), label["label"] == "same"))
    return agreement(pairs)
