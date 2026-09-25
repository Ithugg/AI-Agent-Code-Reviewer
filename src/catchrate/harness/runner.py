"""Run an agent version over benchmark cases and record every review in the store."""

from __future__ import annotations

import logging
import os
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ContextManager, Iterator

from catchrate.agent.execution import DockerSandbox, SuiteTests, docker_available, related_files
from catchrate.agent.findings import REVIEW_OUTPUT_SCHEMA
from catchrate.agent.llm import AnthropicSession, OpenAICompatSession, Session, ToolSpec, Usage
from catchrate.agent.loop import ReviewResult, run_review
from catchrate.agent.pricing import cost_usd
from catchrate.agent.prompts import system_prompt, user_prompt
from catchrate.agent.tools import ToolBox
from catchrate.agent.versions import AgentVersion
from catchrate.gitrepo import GitRepo
from catchrate.harness.store import Store
from catchrate.repos import RepoSpec
from catchrate.schema import Case
from catchrate.verify.checkout import Checkout
from catchrate.verify.envs import EnvManager
from catchrate.verify.suites import suite_for

log = logging.getLogger(__name__)

# (system prompt, tools, structured-output schema or None, effort or None) -> a fresh conversation
SessionFactory = Callable[[str, list[ToolSpec], "dict[str, Any] | None", "str | None"], Session]


def session_factory(model: str, *, max_tokens: int = 16000) -> SessionFactory:
    """Sessions for "claude-..." models, or "openai:<name>" via OPENAI_COMPAT_BASE_URL (and OPENAI_COMPAT_API_KEY)."""
    if model.startswith("claude-"):
        import anthropic  # imported lazily so the rest of the harness runs without it

        client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        return lambda system, tools, schema, effort: AnthropicSession(
            client, model, system, tools, effort=effort, output_schema=schema, max_tokens=max_tokens
        )
    if model.startswith("openai:"):
        import httpx

        base_url = os.environ.get("OPENAI_COMPAT_BASE_URL")
        if not base_url:
            raise RuntimeError("set OPENAI_COMPAT_BASE_URL (and OPENAI_COMPAT_API_KEY if needed) for openai: models")
        key = os.environ.get("OPENAI_COMPAT_API_KEY", "")
        http = httpx.Client(headers={"Authorization": f"Bearer {key}"} if key else {}, timeout=900)
        name = model.split(":", 1)[1]
        return lambda system, tools, schema, effort: OpenAICompatSession(
            http, base_url, name, system, tools, output_schema=schema, max_tokens=max_tokens
        )
    raise ValueError(f"unsupported model {model!r}: use a claude-... id or openai:<name>")


class ToolboxProvider:
    """Builds each case's toolbox; versions that run tests borrow a worktree from a pool."""

    def __init__(self, repo: GitRepo, version: AgentVersion, *, spec: RepoSpec | None = None,
                 envs: EnvManager | None = None, work_dir: Path | None = None, workers: int = 4):  # fmt: skip
        self.repo = repo
        self.version = version
        self.spec = spec
        self.envs = envs
        self.runs_tests = "run_tests" in version.tools or "write_and_run_test" in version.tools
        self.sandboxed = "write_and_run_test" in version.tools
        if self.sandboxed and not docker_available():
            raise RuntimeError(f"{version.name} writes and runs tests, which needs Docker; install Docker Desktop first")
        self.checkouts: queue.Queue[Checkout] = queue.Queue()
        if self.runs_tests:
            if envs is None or work_dir is None or spec is None:
                raise ValueError("versions that run tests need the repo spec, test environments and a work directory")
            if self.sandboxed and spec.test_style != "django":
                raise NotImplementedError("the Docker sandbox only runs Django test modules so far")
            start = repo.head_of(spec.default_branch)
            for i in range(workers):
                self.checkouts.put(Checkout(repo, work_dir / f"{spec.key}-agent{i}", start, worker=100 + i))

    @contextmanager
    def __call__(self, case: Case) -> Iterator[ToolBox]:
        if not self.runs_tests:
            yield ToolBox(self.repo, case, self.version.tools)
            return
        checkout = self.checkouts.get()
        try:
            assert self.envs is not None and self.spec is not None
            tests = SuiteTests(self.repo, suite_for(self.spec, self.envs), checkout, case.review.head_commit)
            sandbox = None
            if self.sandboxed:
                sandbox = DockerSandbox(checkout.path, tests.python)
                sandbox.build_image()
            yield ToolBox(self.repo, case, self.version.tools, tests=tests, sandbox=sandbox, test_style=self.spec.test_style)
        finally:
            self.checkouts.put(checkout)


@dataclass
class RunSettings:
    version: AgentVersion
    model: str
    project: str  # e.g. "Django", used in the system prompt
    max_usd: float | None = None  # stop starting reviews once this much is spent
    workers: int = 4


def record(result: ReviewResult, cost: float | None) -> dict[str, Any]:
    return {
        "findings": [f.model_dump(mode="json") for f in result.findings],
        "input_tokens": result.usage.input_tokens,
        "output_tokens": result.usage.output_tokens,
        "cache_read_tokens": result.usage.cache_read_tokens,
        "cache_write_tokens": result.usage.cache_write_tokens,
        "cost_usd": cost,
        "seconds": round(result.seconds, 2),
        "tool_calls": result.tool_calls,
        "turns": result.turns,
        "stop_reason": result.stop_reason,
        "error": result.error if result.stop_reason == "error" else None,
        "trace": result.trace,
    }


def run_cases(
    run_id: str,
    cases: list[Case],
    settings: RunSettings,
    store: Store,
    repo: GitRepo,
    sessions: SessionFactory,
    toolboxes: Callable[[Case], ContextManager[ToolBox]],
) -> dict[str, Any]:
    """Review every case not yet reviewed in `run_id`; failures are recorded and the run continues."""
    done = store.reviewed_cases(run_id)
    todo = [c for c in cases if c.case_id not in done]
    system = system_prompt(settings.version, settings.project)
    stop = threading.Event()
    counter = {"n": 0}
    lock = threading.Lock()

    def review(case: Case) -> None:
        if stop.is_set():
            return
        related = None
        if settings.version.context == "diff+files":
            related = related_files(repo, case.review.head_commit, case.review.changed_files)
        user = user_prompt(case.review, related)
        try:
            with toolboxes(case) as toolbox:
                tools = toolbox.specs() if settings.version.agentic else []
                schema = None if settings.version.agentic else REVIEW_OUTPUT_SCHEMA
                session = sessions(system, tools, schema, settings.version.effort)
                result = run_review(case.case_id, settings.version, session, user, toolbox if tools else None)
        except Exception as exc:  # API or tool infrastructure errors: record them and keep going
            result = ReviewResult(case.case_id, settings.version.name, settings.model, [], Usage(), 0.0, 0, 0, "error",
                                  f"{type(exc).__name__}: {exc}"[:1000])  # fmt: skip
        store.save_review(run_id, case.case_id, case.kind, record(result, cost_usd(settings.model, result.usage)))
        with lock:
            counter["n"] += 1
            spent = store.spent(run_id)
            log.info("[%d/%d] %s: %d findings, $%.3f so far", counter["n"], len(todo), case.case_id, len(result.findings), spent)
            if settings.max_usd is not None and spent >= settings.max_usd:
                log.warning("Spend cap of $%.2f reached; not starting more reviews", settings.max_usd)
                stop.set()

    with ThreadPoolExecutor(settings.workers) as pool:
        list(pool.map(review, todo))
    reviews = store.reviews(run_id)
    return {
        "reviewed": len(reviews),
        "errors": sum(1 for r in reviews if r["error"]),
        "spent_usd": store.spent(run_id),
        "stopped_early": stop.is_set(),
    }
