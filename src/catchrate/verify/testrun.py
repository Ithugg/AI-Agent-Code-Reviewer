"""Run Django's tests/runtests.py and parse per-test outcomes from its verbose output."""

from __future__ import annotations

import dataclasses
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from catchrate.verify.limits import run_bounded

Outcome = Literal["ok", "fail", "error", "skip", "xfail", "xpass"]
_LABEL_RE = re.compile(r"[\w.]+")
_TEST_LINE_RE = re.compile(r"^(test\w*) \(([\w.]+)\)(.*)$")
_STATUS_RE = re.compile(r" \.\.\. (ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)$")
_BLOCK_RE = re.compile(r"^(FAIL|ERROR): (\S+) \(([\w.]+)\)")
_EXCEPTION_RE = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Failure|Exit|Interrupt|Iteration|DoesNotExist|MultipleObjectsReturned)\b")
_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]+")
_FIXTURE_HOOKS = frozenset({"setUpClass", "tearDownClass", "setUpModule", "tearDownModule"})
# How unittest reports a module that did not import: _FailedTest on Python 3.5+, ModuleImportFailure before.
_FAILED_LOAD = ("unittest.loader._FailedTest", "unittest.loader.ModuleImportFailure")
_LOADER_CRASH_RE = re.compile(r", in loadTestsFromNames?\s*$", re.MULTILINE)
_STATUS_MAP: dict[str, Outcome] = {
    "ok": "ok",
    "FAIL": "fail",
    "ERROR": "error",
    "expected failure": "xfail",
    "unexpected success": "xpass",
}


@dataclass(frozen=True)
class TestRun:
    __test__ = False  # not a pytest test class

    outcomes: dict[str, Outcome]  # test id ("app.module.Class.test_x") -> outcome
    signatures: dict[str, str]  # failed or errored test id -> its final exception line
    load_errors: tuple[str, ...]  # test modules or labels that failed to import
    returncode: int | None  # None when the run timed out
    seconds: float
    tail: str  # end of the output, for debugging
    skip_reasons: dict[str, str] = field(default_factory=dict)  # skipped test id -> reason


def run_django_tests(python: Path, checkout: Path, labels: list[str], *, timeout: float = 900) -> TestRun:
    """Run `labels` with the checkout's own runtests.py (SQLite, one process)."""
    bad = [label for label in labels if not _LABEL_RE.fullmatch(label)]
    if bad or not labels:
        raise ValueError(f"invalid test labels: {bad or labels}")
    runtests = (checkout / "tests" / "runtests.py").read_text(encoding="utf-8", errors="replace")
    parallel = ["--parallel=1"] if "--parallel" in runtests else []  # the option arrived in Django 1.9
    cmd = [str(python), "runtests.py", "--settings=test_sqlite", *parallel, "--noinput", "-v", "2", *labels]
    env = {
        **os.environ,
        "PYTHONPATH": str(checkout.resolve()),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }
    start = time.monotonic()
    done = run_bounded(cmd, cwd=checkout / "tests", env=env, timeout=timeout)
    run = parse_output(done.output(), done.returncode, time.monotonic() - start)
    if not run.outcomes and _LOADER_CRASH_RE.search(run.tail):
        # Before Python 3.5 a label that does not load aborts the whole run instead of reporting a _FailedTest.
        run = dataclasses.replace(run, load_errors=tuple(dict.fromkeys([*run.load_errors, *labels])))
    return run


def parse_output(text: str, returncode: int | None, seconds: float) -> TestRun:
    lines = text.replace("\r\n", "\n").split("\n")
    outcomes: dict[str, Outcome] = {}
    signatures: dict[str, str] = {}
    skips: dict[str, str] = {}
    load_errors: list[str] = []
    pending: str | None = None  # a test line whose status comes after its docstring line

    def record(test_id: str, status: str) -> None:
        outcomes[test_id] = _outcome(status)
        if status.startswith("skipped"):
            skips[test_id] = status[len("skipped") :].strip().strip("'\"")[:200]

    for i, line in enumerate(lines):
        if (match := _TEST_LINE_RE.match(line)) and match[2].startswith(_FAILED_LOAD):
            continue  # Python 2 names its import-failure placeholder after the module, e.g. "tests"; see the block below
        if match:
            test_id = _test_id(match[1], match[2])
            status = _STATUS_RE.search(match[3])
            if status:
                record(test_id, status[1])
                pending = None
            else:
                pending = test_id
        elif pending and (status := _STATUS_RE.search(line)):
            record(pending, status[1])
            pending = None
        elif block := _BLOCK_RE.match(line):
            kind, name, where = block[1], block[2], block[3]
            if where.startswith(_FAILED_LOAD):  # module or test failed to import
                load_errors.append(name)
                continue
            if name in _FIXTURE_HOOKS:  # the whole class or module could not be set up
                load_errors.append(where)
                continue
            test_id = _test_id(name, where)
            outcomes[test_id] = "fail" if kind == "FAIL" else "error"
            signatures[test_id] = _signature(lines, i + 1)
    return TestRun(
        outcomes, signatures, tuple(dict.fromkeys(load_errors)), returncode, seconds, "\n".join(lines[-40:]), skips
    )


def _test_id(name: str, where: str) -> str:
    # Python 3.11+ prints "(module.Class.test_x)", older versions "(module.Class)".
    return where if where.endswith(f".{name}") else f"{where}.{name}"


def _outcome(status: str) -> Outcome:
    return "skip" if status.startswith("skipped") else _STATUS_MAP[status]


def _signature(lines: list[str], start: int) -> str:
    """The last exception line of the traceback block starting at `start`."""
    found = ""
    for line in lines[start:]:
        if line.startswith(("=" * 20, "-" * 20)) and found:
            break
        if _EXCEPTION_RE.match(line):
            found = line
    return _ADDRESS_RE.sub("0x?", found.strip())[:300]
