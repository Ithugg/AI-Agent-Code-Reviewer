"""Loading helpers for the dashboard: tolerant of any of these files being missing."""

from __future__ import annotations

import json
import os
from pathlib import Path

from catchrate.cli.common import read_jsonl
from catchrate.harness.store import Store
from catchrate.repos import REPOS
from catchrate.schema import Case, Verification

DEFAULT_REPO = "django"


def data_dir() -> Path:
    return Path(os.environ.get("CATCHRATE_DATA", "data"))


def available_repos(root: Path) -> list[str]:
    """Repos with mined or frozen data in `root`; the default one when there is none yet."""
    found = [key for key in REPOS if cases_path(root, key).exists() or benchmark_path(root, key).exists()]
    return found or [DEFAULT_REPO]


def project(repo: str) -> str:
    return REPOS[repo].project


def cases_path(root: Path, repo: str) -> Path:
    return root / "cases" / f"{repo}.jsonl"


def clean_cases_path(root: Path, repo: str) -> Path:
    return root / "cases" / f"{repo}.clean.jsonl"


def summary_path(root: Path, repo: str) -> Path:
    return root / "cases" / f"{repo}.summary.json"


def verified_path(root: Path, repo: str) -> Path:
    return root / "verified" / f"{repo}.jsonl"


def szz_path(root: Path, repo: str) -> Path:
    return root / "reports" / f"szz_{repo}.json"


def benchmark_path(root: Path, repo: str) -> Path:
    return root / "benchmark" / f"{repo}.jsonl"


def store_path(root: Path) -> Path:
    return root / "results.db"


def load_cases(root: Path, repo: str) -> list[Case]:
    return read_jsonl(cases_path(root, repo), Case)


def load_clean_cases(root: Path, repo: str) -> list[Case]:
    return read_jsonl(clean_cases_path(root, repo), Case)


def load_summary(root: Path, repo: str) -> dict | None:
    path = summary_path(root, repo)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def load_verified(root: Path, repo: str) -> dict[tuple[str, str], Verification]:
    """Latest verification per (case_id, fix_commit): later lines in the file win."""
    latest: dict[tuple[str, str], Verification] = {}
    for v in read_jsonl(verified_path(root, repo), Verification):
        latest[(v.case_id, v.fix_commit)] = v
    return latest


def load_szz(root: Path, repo: str) -> dict | None:
    path = szz_path(root, repo)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def load_benchmark(root: Path, repo: str) -> list[Case]:
    return read_jsonl(benchmark_path(root, repo), Case)


def open_store(root: Path) -> Store | None:
    """None when no run has ever written to the store, so the dashboard never creates it just by browsing."""
    path = store_path(root)
    return Store(path) if path.exists() else None
