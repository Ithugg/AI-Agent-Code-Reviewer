"""Where agent tools execute code.

`SuiteTests` runs the project's *existing* tests natively: the agent only picks
test labels, so only the project's own code runs. `DockerSandbox` runs tests the
agent wrote, which is arbitrary model-written code, so it only ever runs in a
throwaway container with no network. Nothing here runs agent code on the host.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from catchrate.gitrepo import GitRepo
from catchrate.verify.checkout import Checkout
from catchrate.verify.envs import TEST_DEPS
from catchrate.verify.suites import TestSuite
from catchrate.verify.testrun import TestRun, parse_output

SANDBOX_IMAGE = "catchrate-sandbox"
_DOCKERFILE = """ARG PYTHON=3.12
FROM python:${PYTHON}-slim
RUN pip install --no-cache-dir """ + " ".join(TEST_DEPS) + "\n"


def summarize(run: TestRun, max_tail: int = 3000) -> str:
    """A compact report of a test run for the model."""
    counts: dict[str, int] = {}
    for outcome in run.outcomes.values():
        counts[outcome] = counts.get(outcome, 0) + 1
    if run.returncode is None:
        head = "The test run timed out."
    else:
        head = f"Ran {len(run.outcomes)} tests: " + (", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "none")
    lines = [head]
    lines += [f"FAILED {t}: {sig}" for t, sig in run.signatures.items()]
    lines += [f"Could not load {name}" for name in run.load_errors]
    if run.signatures or run.load_errors or run.returncode is None:
        lines.append("Output tail:\n" + run.tail[-max_tail:])
    return "\n".join(lines)


class SuiteTests:
    """Runs the project's existing tests at one commit, in a dedicated worktree, through its test suite."""

    def __init__(self, repo: GitRepo, suite: TestSuite, checkout: Checkout, commit: str):
        python = suite.python(repo, commit)
        if python is None:
            raise ValueError(f"no usable Python for {commit[:10]}")
        checkout.switch(commit)
        self.suite = suite
        self.checkout = checkout
        self.python = python
        self.env = suite.env(checkout.worker, python)
        error = suite.setup(self.env, checkout.path, repo, commit)
        if error is not None:
            raise RuntimeError(f"could not prepare the test environment: {error[-500:]}")

    def run_tests(self, labels: list[str]) -> str:
        return summarize(self.suite.run(self.env, self.checkout.path, labels))


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode == 0


class DockerSandbox:
    """Runs an agent-written Django test module against a checkout, inside Docker, offline.

    Untested until Docker is installed on this machine: run `catchrate sandbox-check` first.
    """

    def __init__(self, checkout: Path, python: str, *, timeout: float = 600):
        self.checkout = checkout.resolve()
        self.python = python
        self.timeout = timeout
        self.image = f"{SANDBOX_IMAGE}:py{python}"

    def build_image(self) -> None:
        exists = subprocess.run(["docker", "image", "inspect", self.image], capture_output=True).returncode == 0
        if not exists:
            proc = subprocess.run(
                ["docker", "build", "--build-arg", f"PYTHON={self.python}", "-t", self.image, "-"],
                input=_DOCKERFILE.encode(),
                capture_output=True,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"docker build failed: {proc.stderr.decode('utf-8', 'replace')[-800:]}")

    def run_new_test(self, test_code: str) -> tuple[str, bool]:
        with tempfile.TemporaryDirectory(dir=self.checkout.parent, prefix="agent-test-") as tmp:
            app = Path(tmp)
            (app / "__init__.py").write_text("", encoding="utf-8")
            (app / "tests.py").write_text(test_code, encoding="utf-8")
            name = f"catchrate-{uuid.uuid4().hex[:12]}"
            cmd = [
                "docker", "run", "--rm", "--name", name, "--network", "none", "--memory", "2g", "--cpus", "2",
                "--pids-limit", "256", "-v", f"{self.checkout}:/repo:ro", "-v", f"{app}:/repo/tests/catchrate_agent:ro",
                "-w", "/repo/tests", "-e", "PYTHONPATH=/repo", "-e", "PYTHONDONTWRITEBYTECODE=1",
                self.image, "python", "runtests.py", "--settings=test_sqlite", "--parallel=1", "--noinput", "-v", "2",
                "catchrate_agent",
            ]  # fmt: skip
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=self.timeout)
                output, code = proc.stdout + b"\n" + proc.stderr, proc.returncode  # results and tracebacks last
            except subprocess.TimeoutExpired as exc:
                subprocess.run(["docker", "kill", name], capture_output=True)  # stopping the client leaves it running
                output, code = (exc.stdout or b"") + b"\n" + (exc.stderr or b""), None
        run = parse_output(output.decode("utf-8", "replace"), code, 0.0)
        if run.load_errors:  # an import error is the agent's bug, not evidence of one in the code
            return summarize(run) + "\nThe test module did not load, so this run is not evidence.", False
        failed = any(o in ("fail", "error") for o in run.outcomes.values())
        return summarize(run), failed


def related_files(repo: GitRepo, commit: str, paths: list[str], max_chars: int = 60_000) -> dict[str, str]:
    """Full text of the changed Python files at `commit`, for the diff+files context strategy."""
    out: dict[str, str] = {}
    total = 0
    for path in paths:
        if not path.endswith(".py"):
            continue
        text = repo.show_file(commit, path)
        if text is None or total + len(text) > max_chars:
            continue
        out[path] = text
        total += len(text)
    return out
