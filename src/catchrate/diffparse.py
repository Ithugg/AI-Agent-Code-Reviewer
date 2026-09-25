"""Parse `git diff` unified output into per-file hunks with line numbers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

_HUNK_RE = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    removed: tuple[tuple[int, str], ...] = ()  # (old-side line number, text)
    added: tuple[tuple[int, str], ...] = ()  # (new-side line number, text)
    # Old-side line after which a run of added lines replaced nothing (0 = start of file).
    insertions: tuple[int, ...] = ()
    # New-side line after which a run of removed lines was replaced by nothing.
    deletions: tuple[int, ...] = ()


@dataclass
class FileDiff:
    old_path: str | None  # None when the file was added
    new_path: str | None  # None when the file was deleted
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""

    def removed_lines(self) -> list[int]:
        """Old-side line numbers the diff deleted or replaced."""
        return [n for h in self.hunks for n, _ in h.removed]

    def added_lines(self) -> list[int]:
        """New-side line numbers the diff added or replaced."""
        return [n for h in self.hunks for n, _ in h.added]

    def insertion_points(self) -> list[int]:
        """Old-side lines after which pure insertions happened (0 = start of file)."""
        return [p for h in self.hunks for p in h.insertions]

    def deletion_points(self) -> list[int]:
        """New-side lines after which pure deletions happened (0 = start of file)."""
        return [p for h in self.hunks for p in h.deletions]


@dataclass
class _HunkBuilder:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    removed: list[tuple[int, str]] = field(default_factory=list)
    added: list[tuple[int, str]] = field(default_factory=list)
    insertions: list[int] = field(default_factory=list)
    deletions: list[int] = field(default_factory=list)
    seen_old: int = 0
    seen_new: int = 0
    last_tag: str = " "
    deleting_after: int | None = None  # start of a run of removals not (yet) replaced

    def __post_init__(self) -> None:
        # With a zero count, git reports the line *before* the (empty) range.
        self.old_pos = self.old_start if self.old_count else self.old_start + 1
        self.new_pos = self.new_start if self.new_count else self.new_start + 1

    @property
    def open(self) -> bool:
        return self.seen_old < self.old_count or self.seen_new < self.new_count

    def feed(self, line: str) -> None:
        tag, body = line[:1], line[1:]
        if tag == "-":
            if self.last_tag != "-":
                self.deleting_after = self.new_pos - 1
            self.removed.append((self.old_pos, body))
            self.old_pos += 1
            self.seen_old += 1
        elif tag == "+":
            if self.last_tag == " ":
                self.insertions.append(self.old_pos - 1)
            self.deleting_after = None  # the removals were replaced, not deleted
            self.added.append((self.new_pos, body))
            self.new_pos += 1
            self.seen_new += 1
        else:  # context line
            tag = " "
            self._close_deletion()
            self.old_pos += 1
            self.new_pos += 1
            self.seen_old += 1
            self.seen_new += 1
        self.last_tag = tag

    def _close_deletion(self) -> None:
        if self.deleting_after is not None:
            self.deletions.append(self.deleting_after)
            self.deleting_after = None

    def build(self) -> Hunk:
        self._close_deletion()
        return Hunk(
            self.old_start,
            self.old_count,
            self.new_start,
            self.new_count,
            tuple(self.removed),
            tuple(self.added),
            tuple(self.insertions),
            tuple(self.deletions),
        )


def to_ranges(lines: Iterable[int]) -> list[tuple[int, int]]:
    """Collapse line numbers into sorted, inclusive (start, end) ranges."""
    ranges: list[tuple[int, int]] = []
    for n in sorted(set(lines)):
        if ranges and n == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], n)
        else:
            ranges.append((n, n))
    return ranges


def parse_diff(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    current: FileDiff | None = None
    pending: _HunkBuilder | None = None

    def close_hunk() -> None:
        nonlocal pending
        if pending is not None and current is not None:
            current.hunks.append(pending.build())
        pending = None

    for line in text.split("\n"):
        if pending is not None and pending.open:
            if not line.startswith("\\"):  # skip "\ No newline at end of file"
                pending.feed(line)
            continue
        if line.startswith("diff --git "):
            close_hunk()
            current = FileDiff(*_paths_from_header(line[len("diff --git ") :]))
            files.append(current)
        elif current is None or line.startswith("\\"):
            continue
        elif match := _HUNK_RE.match(line):
            close_hunk()
            old_count = int(match[2]) if match[2] is not None else 1
            new_count = int(match[4]) if match[4] is not None else 1
            pending = _HunkBuilder(int(match[1]), old_count, int(match[3]), new_count)
        elif line.startswith("--- "):
            current.old_path = _clean_path(line[4:], "a/")
        elif line.startswith("+++ "):
            current.new_path = _clean_path(line[4:], "b/")
        elif line.startswith("rename from "):
            current.old_path = line[len("rename from ") :]
        elif line.startswith("rename to "):
            current.new_path = line[len("rename to ") :]
        elif line.startswith("new file mode"):
            current.old_path = None
        elif line.startswith("deleted file mode"):
            current.new_path = None
        elif line.startswith("Binary files "):
            current.binary = True
    close_hunk()
    return files


def _clean_path(raw: str, prefix: str) -> str | None:
    raw = raw.rstrip("\t")
    if raw == "/dev/null":
        return None
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        raw = raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return raw[len(prefix) :] if raw.startswith(prefix) else raw


def _paths_from_header(rest: str) -> tuple[str | None, str | None]:
    """Best-effort paths from "a/X b/Y"; ---/+++/rename lines override these."""
    half = (len(rest) - 1) // 2
    if rest[half : half + 1] == " " and rest[:half].startswith("a/") and rest[half + 1 :].startswith("b/"):
        return rest[2:half], rest[half + 3 :]
    old, sep, new = rest.rpartition(" b/")
    if sep:
        return _clean_path(old, "a/"), new
    return None, None
