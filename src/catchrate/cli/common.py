"""Helpers shared by CLI commands."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Iterable, TypeVar

from pydantic import BaseModel

from catchrate.github import GitHubClient, discover_token
from catchrate.gitrepo import GitRepo
from catchrate.repos import REPOS, RepoSpec

M = TypeVar("M", bound=BaseModel)


def spec_of(args: argparse.Namespace) -> RepoSpec:
    return REPOS[args.repo]


def open_repo(args: argparse.Namespace, *, update: bool = False) -> GitRepo:
    spec = spec_of(args)
    dest = args.data_dir / "repos" / f"{spec.key}.git"
    # pytest-style projects take their version from git tags (setuptools_scm), which installing them needs
    return GitRepo.ensure_clone(spec.url, dest, spec.default_branch, update=update, tags=spec.test_style == "pytest")


def github(args: argparse.Namespace) -> GitHubClient:
    token = discover_token()
    if token is None:
        sys.exit("no GitHub token: set GITHUB_TOKEN or run `gh auth login`")
    return GitHubClient(token, args.data_dir / "cache" / "github")


def write_jsonl(path: Path, records: Iterable[BaseModel]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for record in records:
            f.write(record.model_dump_json() + "\n")


def read_jsonl(path: Path, model: type[M]) -> list[M]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [model.model_validate_json(line) for line in f if line.strip()]


def fmt_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in counts.items()) or "none"


def load_dotenv(path: Path) -> None:
    """Read KEY=VALUE lines into the environment without overriding what is already set. Values are never printed."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().removeprefix("export ").strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value
