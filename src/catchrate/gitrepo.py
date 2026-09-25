"""Thin wrapper around the git CLI.

Output is always decoded as UTF-8 (never the Windows locale codepage) and
paths come back unquoted (core.quotePath=false). Works on bare repositories:
nothing here needs a checkout.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence

log = logging.getLogger(__name__)

SHA_RE = re.compile(r"[0-9a-f]{40}")
# HTTP/1.1 sidesteps the HTTP/2 stream resets behind many "curl 56 / early EOF"
# failures; the low-speed limit turns a stalled transfer into a retryable error.
_NETWORK_CONFIG = ("-c", "http.version=HTTP/1.1", "-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=60")
_REVISION_RE = re.compile(r"[0-9A-Za-z._/^~-]+")
# Failures that no retry fixes: a wrong branch or repository name, or no access to it.
_PERMANENT_RE = re.compile(r"not found in upstream|[Rr]epository not found|couldn't find remote ref|Authentication failed")
_FIELD, _RECORD = "\x1f", "\x1e"
_LOG_FORMAT = "%H%x1f%P%x1f%aI%x1f%cI%x1f%B%x1e"
_BLAME_HEADER = re.compile(r"([0-9a-f]{40}) (\d+) (\d+)(?: \d+)?")


class GitError(RuntimeError):
    """A git command exited with an unexpected status."""


@dataclass(frozen=True)
class Commit:
    sha: str
    parents: tuple[str, ...]
    authored: datetime
    committed: datetime
    message: str

    @property
    def subject(self) -> str:
        return self.message.split("\n", 1)[0].strip()


@dataclass(frozen=True)
class BlameEntry:
    line: int  # line number in the blamed revision
    commit: str  # commit the line is attributed to
    orig_path: str  # path of the file in that commit
    orig_line: int  # line number in that commit's version of the file
    content: str


@dataclass(frozen=True)
class NumStat:
    added: int | None  # None for binary files
    deleted: int | None
    path: str  # path on the new side


class GitRepo:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def _exec(self, args: Sequence[str], stdin: str | None = None) -> subprocess.CompletedProcess[bytes]:
        cmd = ["git", "-C", str(self.path), "-c", "core.quotePath=false", "-c", "color.ui=never", *args]
        return subprocess.run(
            cmd,
            input=stdin.encode("utf-8") if stdin is not None else None,
            capture_output=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )

    def run(self, *args: str, stdin: str | None = None) -> str:
        proc = self._exec(args, stdin)
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()
            raise GitError(f"git {' '.join(args[:2])} exited {proc.returncode}: {err}")
        return proc.stdout.decode("utf-8", "replace")

    @classmethod
    def ensure_clone(
        cls, url: str, dest: Path, branch: str, *, update: bool = False, tags: bool = False, step: int = 2000,
        attempts: int = 5,
    ) -> GitRepo:
        """Bare-clone `branch` of `url` into `dest` in resumable steps, plus every tag when `tags`.

        A full clone is one long transfer that restarts from zero when the
        connection drops, so start shallow and deepen `step` commits at a time,
        retrying each step. Re-running resumes an interrupted clone.
        """
        repo = cls(dest)
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)

            def clone() -> None:
                shutil.rmtree(dest, ignore_errors=True)
                cmd = ["git", *_NETWORK_CONFIG, "clone", "--bare", "--quiet", "--depth=1", "--single-branch"]
                proc = subprocess.run([*cmd, f"--branch={branch}", url, str(dest)], capture_output=True)
                if proc.returncode != 0:
                    raise GitError(f"git clone {url} failed: {proc.stderr.decode('utf-8', 'replace').strip()}")

            _retry(clone, attempts)
        elif update:
            _retry(lambda: repo.run(*_NETWORK_CONFIG, "fetch", "--quiet", "origin", f"+{branch}:{branch}"), attempts)
        while repo.run("rev-parse", "--is-shallow-repository").strip() == "true":
            _retry(lambda: repo.run(*_NETWORK_CONFIG, "fetch", "--quiet", f"--deepen={step}", "origin", branch), attempts)
            log.info("%s: %s commits fetched", dest.name, repo.run("rev-list", "--count", branch).strip())
        if tags and (update or not repo.run("for-each-ref", "--count=1", "refs/tags").strip()):
            _retry(lambda: repo.run(*_NETWORK_CONFIG, "fetch", "--quiet", "--tags", "origin"), attempts)
        return repo

    def resolve_commits(self, names: Iterable[str]) -> dict[str, str | None]:
        """Map abbreviated or full commit names to full SHAs (None if missing or ambiguous)."""
        unique = [n for n in dict.fromkeys(names)]
        valid = [n for n in unique if _REVISION_RE.fullmatch(n)]
        resolved: dict[str, str | None] = {n: None for n in unique}
        if not valid:
            return resolved
        out = self.run("cat-file", "--batch-check", stdin="".join(f"{n}^{{commit}}\n" for n in valid))
        for name, line in zip(valid, out.splitlines()):
            parts = line.split()
            if len(parts) == 3 and parts[1] == "commit" and SHA_RE.fullmatch(parts[0]):
                resolved[name] = parts[0]
        return resolved

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        proc = self._exec(["merge-base", "--is-ancestor", ancestor, descendant])
        if proc.returncode not in (0, 1):
            raise GitError(f"merge-base --is-ancestor failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
        return proc.returncode == 0

    def head_of(self, branch: str) -> str:
        return self.run("rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}").strip()

    def log(
        self,
        rev: str,
        *,
        grep: str | None = None,
        ignore_case: bool = False,
        since: str | None = None,
        max_count: int | None = None,
        first_parent: bool = False,
    ) -> list[Commit]:
        args = ["log", f"--format={_LOG_FORMAT}"]
        if grep:
            args.append(f"--grep={grep}")
        if ignore_case:
            args.append("--regexp-ignore-case")
        if since:
            args.append(f"--since={since}")
        if max_count is not None:
            args.append(f"--max-count={max_count}")
        if first_parent:
            args.append("--first-parent")
        return _parse_log(self.run(*args, rev, "--"))

    def commits(self, shas: Iterable[str]) -> dict[str, Commit]:
        """Commit metadata for full SHAs; unknown SHAs raise GitError."""
        unique = list(dict.fromkeys(shas))
        if not unique:
            return {}
        out = self.run("log", "--no-walk=unsorted", "--stdin", f"--format={_LOG_FORMAT}", stdin="\n".join(unique) + "\n")
        return {c.sha: c for c in _parse_log(out)}

    def first_parent_chain(self, head: str, count: int) -> list[Commit]:
        """`head` and its first-parent ancestors, newest first, at most `count` commits."""
        return self.log(head, max_count=count, first_parent=True)

    def rev_list(self, exclude: str, include: str) -> list[str]:
        """Commits reachable from `include` but not from `exclude`, oldest first."""
        return self.run("rev-list", "--reverse", f"{exclude}..{include}").split()

    def diff(
        self, base: str, head: str, *, unified: int = 3, ignore_whitespace: bool = False, paths: Sequence[str] = ()
    ) -> str:
        args = ["diff", "--no-ext-diff", "--find-renames", f"--unified={unified}"]
        if ignore_whitespace:
            args.append("--ignore-all-space")
        return self.run(*args, base, head, "--", *paths)

    def renames(self, base: str, head: str) -> dict[str, str]:
        """New path -> old path for files renamed between `base` and `head`."""
        tokens = self.run("diff", "--no-ext-diff", "--find-renames", "--name-status", "-z", base, head, "--").split("\0")
        renamed: dict[str, str] = {}
        i = 0
        while i < len(tokens):
            status = tokens[i]
            if not status:
                i += 1
            elif status[0] in "RC":  # rename or copy: old and new paths follow
                if status[0] == "R":
                    renamed[tokens[i + 2]] = tokens[i + 1]
                i += 3
            else:
                i += 2
        return renamed

    def numstat(self, base: str, head: str) -> list[NumStat]:
        return _parse_numstat_z(self.run("diff", "--no-ext-diff", "--find-renames", "--numstat", "-z", base, head, "--"))

    def grep(self, commit: str, args: Sequence[str], paths: Sequence[str] = ()) -> str:
        """`git grep` over one commit's tree; empty output when nothing matches."""
        proc = self._exec(["grep", *args, commit, "--", *paths])
        if proc.returncode not in (0, 1):
            raise GitError(f"git grep exited {proc.returncode}: {proc.stderr.decode('utf-8', 'replace').strip()}")
        return proc.stdout.decode("utf-8", "replace")

    def ls_tree(self, commit: str, path: str = "") -> list[tuple[str, str]] | None:
        """(kind, name) entries of a directory at `commit` ("tree" or "blob"), or None if it isn't one."""
        proc = self._exec(["ls-tree", f"{commit}:{path}" if path else commit])
        if proc.returncode != 0:
            return None
        entries = []
        for line in proc.stdout.decode("utf-8", "replace").splitlines():
            meta, _, name = line.partition("\t")
            entries.append((meta.split()[1], name))
        return entries

    def show_file(self, rev: str, path: str) -> str | None:
        proc = self._exec(["show", f"{rev}:{path}"])
        return proc.stdout.decode("utf-8", "replace") if proc.returncode == 0 else None

    def blame(
        self,
        rev: str,
        path: str,
        ranges: Sequence[tuple[int, int]],
        *,
        ignore_whitespace: bool = True,
        ignore_revs_file: Path | None = None,
        reverse: bool = False,
    ) -> list[BlameEntry]:
        """Blame the inclusive line `ranges` of `path` as of `rev`.

        With `reverse`, `rev` is "start..end" and each line of `start` is attributed
        to the last commit in which it still existed (`end` itself if it survived).
        """
        if not ranges:
            return []
        args = ["blame", "--line-porcelain"]
        if reverse:
            args.append("--reverse")
        if ignore_whitespace:
            args.append("-w")
        if ignore_revs_file is not None:
            # git -C changes directory, so the path must be absolute.
            args += ["--ignore-revs-file", str(Path(ignore_revs_file).resolve())]
        for start, end in ranges:
            args += ["-L", f"{start},{end}"]
        return _parse_line_porcelain(self.run(*args, rev, "--", path))


def _retry(action: Callable[[], object], attempts: int) -> None:
    for attempt in range(1, attempts + 1):
        try:
            action()
            return
        except GitError as exc:
            if attempt == attempts or _PERMANENT_RE.search(str(exc)):
                raise
            wait = 5 * 2 ** (attempt - 1)
            log.warning("%s (attempt %d/%d, retrying in %ds)", exc, attempt, attempts, wait)
            time.sleep(wait)


def _parse_log(out: str) -> list[Commit]:
    commits = []
    for record in out.split(_RECORD):
        record = record.lstrip("\n")
        if not record:
            continue
        sha, parents, authored, committed, message = record.split(_FIELD, 4)
        commits.append(
            Commit(
                sha=sha,
                parents=tuple(parents.split()),
                authored=datetime.fromisoformat(authored),
                committed=datetime.fromisoformat(committed),
                message=message.rstrip("\n"),
            )
        )
    return commits


def _parse_numstat_z(out: str) -> list[NumStat]:
    stats = []
    tokens = out.split("\0")
    i = 0
    while i < len(tokens):
        if not tokens[i]:
            i += 1
            continue
        added, deleted, path = tokens[i].split("\t", 2)
        if path:
            i += 1
        else:  # rename or copy: old and new paths follow as their own tokens
            path = tokens[i + 2]
            i += 3
        stats.append(NumStat(None if added == "-" else int(added), None if deleted == "-" else int(deleted), path))
    return stats


def _parse_line_porcelain(out: str) -> list[BlameEntry]:
    entries = []
    commit, orig_line, final_line, orig_path = "", 0, 0, ""
    for raw in out.split("\n"):
        if raw.startswith("\t"):
            entries.append(BlameEntry(final_line, commit, orig_path, orig_line, raw[1:].rstrip("\r")))
        elif match := _BLAME_HEADER.fullmatch(raw):
            commit, orig_line, final_line = match[1], int(match[2]), int(match[3])
        elif raw.startswith("filename "):
            orig_path = raw[len("filename ") :]
    return entries
