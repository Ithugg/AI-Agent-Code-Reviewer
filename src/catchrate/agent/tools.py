"""Tools the review agent can call, bound to one case's review head.

The read-only tools look at the repository through git objects at the review
head, so the agent sees the code exactly as the reviewer did and nothing that
came later: there is no shell, no history, and no network.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Callable, Protocol

from catchrate.agent.findings import REPORT_FINDING_SCHEMA, Evidence, Finding, finding_from
from catchrate.agent.llm import ToolCall, ToolResult, ToolSpec
from catchrate.gitrepo import GitError, GitRepo
from catchrate.schema import Case

MAX_OUTPUT_CHARS = 12_000
MAX_READ_LINES = 400
MAX_HITS = 60
_IDENT_RE = re.compile(r"[A-Za-z_]\w*")
_LABEL_RE = re.compile(r"[\w./:\[\]-]+")  # Django labels or pytest node ids


class ToolError(Exception):
    """Shown to the model as an error result; the review continues."""


class TestExecutor(Protocol):
    def run_tests(self, labels: list[str]) -> str: ...


class Sandbox(Protocol):
    def run_new_test(self, test_code: str) -> tuple[str, bool]:
        """(output, whether the test failed) for an agent-written test module."""
        ...


def _obj(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


SPECS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in [
        ToolSpec("read_diff", "Show the pull request's title, description and full diff again.", _obj({})),
        ToolSpec(
            "read_file",
            f"Read lines of a file as it is after the pull request, with line numbers (at most {MAX_READ_LINES} lines per call). "
            "Use end_line 0 to read to the end of the file.",
            _obj({"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}),
        ),
        ToolSpec(
            "search_code",
            "Search the repository after the pull request with an extended regular expression (like grep -E). "
            "Returns path:line:text. Limit the search to a directory or file with path, or pass an empty string.",
            _obj({"pattern": {"type": "string"}, "path": {"type": "string"}}),
        ),
        ToolSpec(
            "find_callers",
            "Find every line in Python files that mentions an identifier as a whole word: its definition, calls, and references.",
            _obj({"name": {"type": "string"}}),
        ),
        ToolSpec(
            "list_dir",
            "List a directory of the repository after the pull request (empty string for the root).",
            _obj({"path": {"type": "string"}}),
        ),
        ToolSpec(
            "run_tests",
            "Run existing tests from the project's test suite against the code after the pull request.",
            _obj({"labels": {"type": "array", "items": {"type": "string"}}}),
        ),
        ToolSpec(
            "write_and_run_test",
            "Write a new Django test module (a complete tests.py using django.test.TestCase or SimpleTestCase and existing test "
            "apps' models) and run it against the code after the pull request. Returns an id to cite as evidence.",
            _obj({"test_code": {"type": "string"}}),
        ),
        ToolSpec(
            "report_finding",
            "Report one bug introduced by this pull request. Call once per distinct bug.",
            REPORT_FINDING_SCHEMA,
        ),
    ]
}
TOOL_NAMES = tuple(SPECS)
_LABEL_HINTS = {
    "django": " Labels are Django test labels such as 'lookup.tests' or 'lookup.tests.LookupTests.test_in'.",
    "pytest": " Labels are pytest node ids such as 'tests/test_client.py' or 'tests/test_client.py::TestSession::test_close'.",
}


class ToolBox:
    def __init__(
        self,
        repo: GitRepo,
        case: Case,
        enabled: tuple[str, ...],
        *,
        tests: TestExecutor | None = None,
        sandbox: Sandbox | None = None,
        test_style: str = "django",
    ):
        unknown = set(enabled) - set(SPECS)
        if unknown:
            raise ValueError(f"unknown tools: {sorted(unknown)}")
        if "run_tests" in enabled and tests is None:
            raise ValueError("run_tests needs a test executor")
        if "write_and_run_test" in enabled and sandbox is None:
            raise ValueError("write_and_run_test needs a sandbox (Docker)")
        self.repo = repo
        self.case = case
        self.head = case.review.head_commit
        self.enabled = tuple(enabled)
        self.tests = tests
        self.sandbox = sandbox
        self.test_style = test_style
        self.findings: list[Finding] = []
        self.evidence: dict[str, Evidence] = {}
        self._handlers: dict[str, Callable[..., str]] = {
            "read_diff": self._read_diff,
            "read_file": self._read_file,
            "search_code": self._search_code,
            "find_callers": self._find_callers,
            "list_dir": self._list_dir,
            "run_tests": self._run_tests,
            "write_and_run_test": self._write_and_run_test,
            "report_finding": self._report_finding,
        }

    def specs(self) -> list[ToolSpec]:
        specs = [SPECS[name] for name in self.enabled]
        hint = _LABEL_HINTS[self.test_style]
        return [replace(s, description=s.description + hint) if s.name == "run_tests" else s for s in specs]

    def run(self, call: ToolCall) -> ToolResult:
        if call.name not in self.enabled:
            return ToolResult(call.id, f"Unknown tool {call.name!r}.", is_error=True)
        try:
            text = self._handlers[call.name](**call.input)
        except ToolError as exc:
            return ToolResult(call.id, str(exc), is_error=True)
        except (TypeError, ValueError, KeyError) as exc:
            return ToolResult(call.id, f"Bad arguments for {call.name}: {exc}", is_error=True)
        return ToolResult(call.id, truncate(text))

    # -- read-only tools ---------------------------------------------------

    def _read_diff(self) -> str:
        r = self.case.review
        return f"Title: {r.title}\n\nDescription:\n{r.description or '(none)'}\n\nDiff:\n{r.diff}"

    def _read_file(self, path: str, start_line: int, end_line: int) -> str:
        path = clean_path(path)
        text = self.repo.show_file(self.head, path)
        if text is None:
            raise ToolError(f"{path} does not exist after this pull request; use list_dir or search_code to find the path.")
        lines = text.splitlines()
        start = max(1, int(start_line))
        end = len(lines) if int(end_line) <= 0 else min(int(end_line), len(lines))
        if start > len(lines):
            raise ToolError(f"{path} has only {len(lines)} lines.")
        clipped = end - start + 1 > MAX_READ_LINES
        end = min(end, start + MAX_READ_LINES - 1)
        body = "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(start, end + 1))
        more = f"\n(showing {MAX_READ_LINES} lines; read on from line {end + 1})" if clipped else ""
        return f"{path}, lines {start}-{end} of {len(lines)}:\n{body}{more}"

    def _search_code(self, pattern: str, path: str) -> str:
        if not pattern or len(pattern) > 200:
            raise ToolError("pattern must be 1-200 characters")
        paths = [clean_path(path)] if path.strip() else []
        try:
            out = self.repo.grep(self.head, ["-n", "-I", "-E", "-e", pattern], paths)
        except GitError as exc:
            raise ToolError(f"search failed (is the pattern a valid extended regex?): {exc}") from exc
        return _format_hits(_strip_commit(out))

    def _find_callers(self, name: str) -> str:
        if not _IDENT_RE.fullmatch(name):
            raise ToolError("name must be a single identifier, such as get_prep_lookup")
        hits = _strip_commit(self.repo.grep(self.head, ["-n", "-I", "-w", "-e", name], ["*.py"]))
        definitions = [h for h in hits if re.search(rf"\b(def|class)\s+{re.escape(name)}\b", h)]
        others = [h for h in hits if h not in definitions]
        return _format_hits(definitions + others, header=f"{len(definitions)} definition(s) first")

    def _list_dir(self, path: str) -> str:
        entries = self.repo.ls_tree(self.head, clean_path(path) if path.strip() else "")
        if entries is None:
            raise ToolError(f"{path} is not a directory after this pull request.")
        return "\n".join(name + ("/" if kind == "tree" else "") for kind, name in entries) or "(empty)"

    # -- execution tools -----------------------------------------------------

    def _run_tests(self, labels: list[str]) -> str:
        if not labels or len(labels) > 20 or not all(_LABEL_RE.fullmatch(label) for label in labels):
            raise ToolError("labels must be 1-20 test labels, such as 'lookup.tests.LookupTests' or 'testing/test_x.py::test_y'")
        assert self.tests is not None
        return self.tests.run_tests(list(labels))

    def _write_and_run_test(self, test_code: str) -> str:
        if not test_code.strip() or len(test_code) > 20_000:
            raise ToolError("test_code must be a non-empty test module under 20,000 characters")
        assert self.sandbox is not None
        output, failed = self.sandbox.run_new_test(test_code)
        test_id = f"t{len(self.evidence) + 1}"
        self.evidence[test_id] = Evidence(test_id=test_id, test_code=test_code, output=truncate(output), failed=failed)
        return f"Test run {test_id}: {'FAILED on the reviewed code' if failed else 'passed'}.\n{output}"

    def _report_finding(self, **fields: Any) -> str:
        evidence_id = str(fields.pop("evidence_test_id", "") or "").strip()
        if evidence_id and evidence_id not in self.evidence:
            raise ToolError(f"unknown evidence_test_id {evidence_id!r}; use an id returned by write_and_run_test or ''")
        finding = finding_from(fields, self.evidence.get(evidence_id))
        self.findings.append(finding)
        return f"Recorded finding {len(self.findings)} ({finding.file}:{finding.start_line}-{finding.end_line})."


def clean_path(path: str) -> str:
    cleaned = path.strip().replace("\\", "/").removeprefix("./").strip("/")
    if not cleaned or ".." in cleaned.split("/") or ":" in cleaned:
        raise ToolError(f"invalid repository path {path!r}")
    return cleaned


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [{len(text) - limit} characters cut] ...\n{text[-half:]}"


def _strip_commit(grep_output: str) -> list[str]:
    """git grep over a commit prints "<commit>:path:line:text"; keep "path:line:text"."""
    return [line.split(":", 1)[1] for line in grep_output.splitlines() if ":" in line]


def _format_hits(hits: list[str], header: str = "") -> str:
    if not hits:
        return "No matches."
    shown = "\n".join(hits[:MAX_HITS])
    more = f"\n... {len(hits) - MAX_HITS} more matches; narrow the search" if len(hits) > MAX_HITS else ""
    return f"{len(hits)} matches{f' ({header})' if header else ''}:\n{shown}{more}"
