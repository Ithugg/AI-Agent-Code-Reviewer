"""Freeze verified cases into the benchmark file that every agent run uses."""

from __future__ import annotations

import hashlib
from typing import Iterable

from catchrate.schema import VERIFIED, Case, Verification


def assign_splits(cases: list[Case], *, dev: int, ablation: int) -> list[Case]:
    """Deterministic splits by hashed case id: the first `dev` are dev, the next `ablation` ablation."""
    ordered = sorted(cases, key=lambda c: hashlib.sha256(c.case_id.encode()).hexdigest())
    out = []
    for i, case in enumerate(ordered):
        split = "dev" if i < dev else "ablation" if i < dev + ablation else "test"
        out.append(case.model_copy(update={"split": split}))
    return out


def freeze(
    cases: Iterable[Case],
    verifications: Iterable[Verification],
    clean: Iterable[Case] = (),
    *,
    dev: int = 30,
    ablation: int = 60,
) -> list[Case]:
    """Keep only bugs whose label survived verification, then split buggy and clean cases alike."""
    verdicts = {(v.case_id, v.fix_commit): v.verdict for v in verifications}
    buggy = []
    for case in cases:
        bugs = [b.model_copy(update={"verdict": verdicts.get((case.case_id, b.fix_commit))}) for b in case.bugs]
        kept = [b for b in bugs if b.verdict in VERIFIED]
        if kept:
            buggy.append(case.model_copy(update={"bugs": kept}))
    clean = list(clean)
    share = (dev / len(buggy), ablation / len(buggy)) if buggy else (0.0, 0.0)
    frozen = assign_splits(buggy, dev=dev, ablation=ablation) + assign_splits(
        clean, dev=round(len(clean) * share[0]), ablation=round(len(clean) * share[1])
    )
    return sorted(frozen, key=lambda c: (c.kind, c.case_id))


SPLITS = ("dev", "ablation", "test")


def hf_configs(splits: dict[str, list[str]]) -> str:
    """The `configs:` block of a Hugging Face dataset card: a config per repository, a file per split it has."""
    lines = ["configs:"]
    for key, names in splits.items():
        lines += [f"  - config_name: {key}", "    data_files:"]
        for split in names:
            lines += [f"      - split: {split}", f"        path: data/{key}/{split}.jsonl"]
    return "\n".join(lines)


def with_configs(card: str, configs: str) -> str:
    """The dataset card with `configs` added to its YAML front matter, or given front matter holding them."""
    if card.startswith("---\n"):
        end = card.index("\n---", 3)
        return f"{card[:end]}\n{configs}{card[end:]}"
    return f"---\n{configs}\n---\n\n{card}"
