from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from catchrate.gitrepo import GitRepo
from catchrate.repos import RepoSpec
from catchrate.schema import PRInfo

TOY_SPEC = RepoSpec(
    key="toy",
    owner="toy",
    name="toy",
    default_branch="main",
    source_prefixes=("pkg/",),
    test_prefix="tests/",
    release_notes_prefix="docs/releases/",
)


class ToyRepo:
    """A throwaway git repository with helpers to write files and commit."""

    def __init__(self, root: Path):
        self.root = root
        self._hour = 0
        root.mkdir(parents=True)
        self.git("init", "--quiet", "--initial-branch=main")
        self.git("config", "core.autocrlf", "false")
        self.repo = GitRepo(root)

    def git(self, *args: str) -> str:
        stamp = (datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(hours=self._hour)).isoformat()
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Toy",
            "GIT_AUTHOR_EMAIL": "toy@example.com",
            "GIT_COMMITTER_NAME": "Toy",
            "GIT_COMMITTER_EMAIL": "toy@example.com",
            "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_DATE": stamp,
        }
        proc = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, env=env, check=True)
        return proc.stdout.decode("utf-8").strip()

    def commit(self, message: str, files: dict[str, str | None]) -> str:
        """Write (or delete, for None) files and commit them; returns the new SHA."""
        for path, text in files.items():
            target = self.root / path
            if text is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8", newline="\n")
        self.git("add", "--all")
        self._hour += 1
        self.git("commit", "--quiet", "-m", message)
        return self.git("rev-parse", "HEAD")


class FakeGitHub:
    """Stands in for GitHubClient: a fixed commit -> pull requests mapping."""

    def __init__(self, prs: dict[str, list[PRInfo]] | None = None):
        self.prs = prs or {}

    def commit_pull_requests(self, owner, name, shas):
        return {sha: self.prs.get(sha, []) for sha in shas}


def make_pr(number: int, merge_commit: str, headlines: list[str], **extra) -> PRInfo:
    return PRInfo(
        number=number,
        title=extra.pop("title", headlines[-1]),
        merged=True,
        base_ref="main",
        merge_commit=merge_commit,
        commit_count=len(headlines),
        commit_headlines=headlines,
        **extra,
    )


@pytest.fixture
def toy(tmp_path: Path) -> ToyRepo:
    return ToyRepo(tmp_path / "toy")
