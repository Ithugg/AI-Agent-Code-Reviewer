"""Reusable git worktrees of a bare repository, for running tests at any commit."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Literal

from catchrate.diffparse import parse_diff
from catchrate.gitrepo import GitError, GitRepo
from catchrate.verify.graft import graft

PatchMethod = Literal["none", "applied", "3way", "graft", "overlay", "failed"]


class Checkout:
    def __init__(self, repo: GitRepo, path: Path, start: str, worker: int = 0):
        self.repo = repo
        self.path = path.resolve()
        self.worker = worker
        if not (self.path / ".git").exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            repo.run("worktree", "prune")
            repo.run("worktree", "add", "--force", "--detach", str(self.path), start)
        # A run that was killed mid-checkout leaves index.lock behind, which blocks every
        # later git command here. Each worktree has a single owner, so it is safe to clear.
        git_dir = self._git("rev-parse", "--absolute-git-dir").stdout.decode("utf-8", "replace").strip()
        if git_dir:
            Path(git_dir, "index.lock").unlink(missing_ok=True)

    def _git(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[bytes]:
        cmd = ["git", "-C", str(self.path), "-c", "core.autocrlf=false", "-c", "core.longpaths=true", *args]
        return subprocess.run(
            cmd,
            input=stdin.encode("utf-8") if stdin is not None else None,
            capture_output=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )

    def _must(self, *args: str) -> None:
        proc = self._git(*args)
        if proc.returncode != 0:
            raise GitError(f"git {' '.join(args[:2])} in {self.path.name}: {proc.stderr.decode('utf-8', 'replace').strip()}")

    def switch(self, commit: str) -> None:
        """Check out `commit` with a pristine tree (no leftovers from earlier runs)."""
        self._must("checkout", "--quiet", "--force", "--detach", commit)
        self._must("clean", "-fdxq")

    def apply_tests(self, patch: str, source: str, paths: list[str]) -> PatchMethod:
        """Bring a fix's test changes onto the current commit.

        Tries the patch as is, then a three-way apply, then grafts the definitions
        the fix changed into this commit's test modules, and last copies the files
        wholesale from `source` (the fix commit), which may drag in unrelated
        later test changes.
        """
        if not patch.strip():
            return "none"
        for method, flags in (("applied", ()), ("3way", ("--3way",))):
            proc = self._git("apply", "--whitespace=nowarn", *flags, "-", stdin=patch)
            if proc.returncode == 0:
                return method  # type: ignore[return-value]
            self._must("checkout", "--quiet", "--force", "HEAD")
            self._must("clean", "-fdxq")
        if self._graft(patch, source, paths):
            return "graft"
        present = [p for p in paths if self.repo.show_file(source, p) is not None]
        if present and self._git("checkout", source, "--", *present).returncode == 0:
            return "overlay"
        return "failed"

    def _graft(self, patch: str, source: str, paths: list[str]) -> bool:
        """Swap the definitions the fix changed into each test module here; new files come over whole."""
        diffs = {fd.new_path: fd for fd in parse_diff(patch) if fd.new_path}
        writes: dict[Path, str] = {}
        for path in paths:
            fixed = self.repo.show_file(source, path)
            if fixed is None:
                continue  # the fix deleted it
            target = self.path / path
            if not path.endswith(".py") or not target.exists():
                writes[target] = fixed
                continue
            fd = diffs.get(path)
            changed = set(fd.added_lines()) | set(fd.deletion_points()) if fd else set()
            try:
                merged = graft(target.read_text(encoding="utf-8"), fixed, changed)
            except UnicodeDecodeError:
                merged = None
            if merged is None:
                return False
            writes[target] = merged
        for target, text in writes.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8", newline="\n")
        return bool(writes)
