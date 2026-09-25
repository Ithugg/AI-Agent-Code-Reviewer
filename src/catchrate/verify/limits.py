"""Run a test command within a time and memory budget that covers its whole process tree.

On Windows a venv's python.exe is a launcher that starts the real interpreter as a
child, so stopping only the direct child on a timeout leaves the tests running. And a
test written for a resource-limit bug can grow without bound on the buggy commit.
"""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

MAX_MEMORY = 4 << 30  # bytes of resident memory, summed over the process tree


@dataclass(frozen=True)
class Finished:
    stdout: bytes
    stderr: bytes
    returncode: int | None  # None when the run was stopped
    stopped: str | None = None  # "timeout" or "memory"

    def output(self) -> str:
        """stdout, then stderr (results and tracebacks last, so a kept tail ends with them)."""
        text = (self.stdout + b"\n" + self.stderr).decode("utf-8", "replace")
        if self.stopped:
            text += f"\ncatchrate: stopped the run ({'over the time limit' if self.stopped == 'timeout' else 'over the memory limit'})\n"
        return text


def run_bounded(
    cmd: list[str], *, cwd: Path, env: dict[str, str], timeout: float, max_memory: int = MAX_MEMORY, poll: float = 0.5
) -> Finished:
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    chunks: dict[str, bytes] = {}
    readers = [
        threading.Thread(target=lambda name=name, stream=stream: chunks.__setitem__(name, stream.read()), daemon=True)
        for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr))
    ]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout
    stopped = None
    while True:
        try:
            proc.wait(timeout=poll)
            break
        except subprocess.TimeoutExpired:
            pass
        if time.monotonic() > deadline:
            stopped = "timeout"
        elif _tree_memory(proc.pid) > max_memory:
            stopped = "memory"
        if stopped:
            _kill_tree(proc.pid)
            proc.wait()
            break
    for reader in readers:
        reader.join(timeout=30)
    return Finished(chunks.get("stdout", b""), chunks.get("stderr", b""), None if stopped else proc.returncode, stopped)


def _tree(pid: int) -> list[psutil.Process]:
    try:
        root = psutil.Process(pid)
        return [root, *root.children(recursive=True)]
    except psutil.Error:
        return []


def _tree_memory(pid: int) -> int:
    total = 0
    for process in _tree(pid):
        try:
            total += process.memory_info().rss
        except psutil.Error:
            pass
    return total


def _kill_tree(pid: int) -> None:
    processes = _tree(pid)
    for process in reversed(processes):  # children first, so none is left orphaned
        try:
            process.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(processes, timeout=10)
