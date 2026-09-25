"""Run a pytest-based project's own tests and read the results from JUnit XML."""

from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from catchrate.verify.limits import run_bounded
from catchrate.verify.testrun import Outcome, TestRun

NODE_ID_RE = re.compile(r"[\w./-]+\.py(::\w+)*")
_EXCEPTION_RE = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Failure|Exit|Interrupt|Iteration|Warning)\b")
_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]+")
_NOT_FOUND = ("file or directory not found", "not found:", "no match in any of")


def run_pytest(
    python: Path,
    checkout: Path,
    node_ids: list[str],
    *,
    args: tuple[str, ...] = (),
    extra_env: dict[str, str] | None = None,
    timeout: float = 900,
) -> TestRun:
    bad = [n for n in node_ids if not NODE_ID_RE.fullmatch(n)]
    if bad or not node_ids:
        raise ValueError(f"invalid pytest node ids: {bad or node_ids}")
    report = checkout.parent / f"{checkout.name}.junit.xml"
    report.unlink(missing_ok=True)
    cmd = [str(python), "-m", "pytest", "-p", "no:cacheprovider", "-q", *args, "-o", "junit_family=xunit2",
           f"--junitxml={report}", *node_ids]  # fmt: skip
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env.update(extra_env or {})
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    start = time.monotonic()
    done = run_bounded(cmd, cwd=checkout, env=env, timeout=timeout)
    xml = report.read_text(encoding="utf-8", errors="replace") if report.exists() and not done.stopped else ""
    return parse_junit(xml, checkout, done.returncode, time.monotonic() - start, done.output(), node_ids)


def parse_junit(xml: str, checkout: Path, returncode: int | None, seconds: float, output: str, node_ids: list[str]) -> TestRun:
    outcomes: dict[str, Outcome] = {}
    signatures: dict[str, str] = {}
    skips: dict[str, str] = {}
    load_errors: list[str] = []
    for case in ET.fromstring(xml).iter("testcase") if xml.strip() else ():
        classname, name = case.get("classname", ""), case.get("name", "")
        failure, error, skipped = case.find("failure"), case.find("error"), case.find("skipped")
        if not classname and error is not None:  # a module that failed to collect
            load_errors.append(_module_path(name.split("."), checkout) or name)
            continue
        test_id = _node_id(classname, name, checkout)
        if failure is not None or error is not None:
            node = failure if failure is not None else error
            outcomes[test_id] = "fail" if failure is not None else "error"
            signatures[test_id] = _signature(node.get("message", ""), node.text or "")
        elif skipped is not None:
            xfail = skipped.get("type", "") == "pytest.xfail"
            outcomes[test_id] = "xfail" if xfail else "skip"
            if not xfail:
                skips[test_id] = skipped.get("message", "")[:200]
        else:
            outcomes[test_id] = "ok"
    if returncode == 5:  # pytest's "no tests collected"
        returncode = 0
    if not outcomes and returncode == 4 and any(marker in output for marker in _NOT_FOUND):
        load_errors += node_ids  # the tests don't exist at this commit
    lines = output.replace("\r\n", "\n").split("\n")
    return TestRun(outcomes, signatures, tuple(dict.fromkeys(load_errors)), returncode, seconds, "\n".join(lines[-40:]), skips)


def _module_path(parts: list[str], checkout: Path) -> str | None:
    for i in range(len(parts), 0, -1):
        candidate = "/".join(parts[:i]) + ".py"
        if (checkout / candidate).exists():
            return candidate
    return None


def _node_id(classname: str, name: str, checkout: Path) -> str:
    """"testing.test_x.TestC" + "test_m[1]" -> "testing/test_x.py::TestC::test_m[1]"."""
    parts = classname.split(".")
    for i in range(len(parts), 0, -1):
        candidate = "/".join(parts[:i]) + ".py"
        if (checkout / candidate).exists():
            return "::".join([candidate, *parts[i:], name])
    return f"{classname}::{name}"


def _signature(message: str, text: str) -> str:
    first = message.strip().splitlines()[0] if message.strip() else ""
    if not _EXCEPTION_RE.match(first):
        found = [line.strip().lstrip("E ").strip() for line in text.splitlines() if _EXCEPTION_RE.match(line.strip().lstrip("E ").strip())]
        first = found[-1] if found else first
    return _ADDRESS_RE.sub("0x?", first)[:300]
