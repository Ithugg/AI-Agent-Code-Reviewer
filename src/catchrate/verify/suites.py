"""How to run one project's own tests at an arbitrary commit.

The verifier drives either suite the same way: pick a Python for the commit, get
an environment, prepare it for the checkout, run labels, read outcomes.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol

from catchrate.gitrepo import GitRepo
from catchrate.repos import PytestSetup, RepoSpec
from catchrate.verify.envs import EnvManager, django_version, python_for
from catchrate.verify.pytestrun import run_pytest
from catchrate.verify.testrun import TestRun, run_django_tests

TestRunner = Callable[[Path, Path, list[str]], TestRun]  # (python, checkout, labels) -> TestRun


class TestSuite(Protocol):
    label_sep: str  # "." for Django labels, "::" for pytest node ids

    def python(self, repo: GitRepo, commit: str) -> str | None: ...

    def version(self, repo: GitRepo, commit: str) -> str | None: ...

    def env(self, worker: int, python: str) -> Path: ...

    def setup(self, env: Path, checkout: Path, repo: GitRepo, commit: str) -> str | None:
        """Prepare `env` for `checkout`; an error message when that fails."""
        ...

    def run(self, env: Path, checkout: Path, labels: list[str]) -> TestRun: ...


@dataclass
class DjangoSuite:
    envs: EnvManager
    runner: TestRunner = run_django_tests
    label_sep: str = "."

    def python(self, repo: GitRepo, commit: str) -> str | None:
        version = django_version(repo, commit)
        return python_for(version) if version else None

    def version(self, repo: GitRepo, commit: str) -> str | None:
        version = django_version(repo, commit)
        return f"{version[0]}.{version[1]}" if version else None

    def env(self, worker: int, python: str) -> Path:
        return self.envs.python(python)  # shared by all workers: Django runs from the checkout itself

    def setup(self, env: Path, checkout: Path, repo: GitRepo, commit: str) -> str | None:
        return None

    def run(self, env: Path, checkout: Path, labels: list[str]) -> TestRun:
        return self.runner(env, checkout, labels)


_REQUIRES_RE = re.compile(r"""(?:requires-python|python_requires)\s*=\s*["']?\s*>=\s*(\d+)\.(\d+)""")
_AVAILABLE = ((3, 8), (3, 9), (3, 10), (3, 11), (3, 12))
_RELEASE_RE = re.compile(r"\d+(?:\.\d+)+")  # "v8.1.0.dev0" -> "8.1.0": dev versions trip pre-release rules
_FILTERED_RE = re.compile(r"`([\w.-]+)` was filtered by `exclude-newer`")  # uv's hint when a cutoff leaves nothing


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class PytestSuite:
    """Installs the checkout and its test dependencies into a fresh per-worker venv, then runs pytest."""

    envs: EnvManager
    key: str
    package: str = ""  # the distribution the checkout builds; defaults to key
    config: PytestSetup = PytestSetup()
    runner: Callable[..., TestRun] = run_pytest
    label_sep: str = "::"
    _versions: dict[str, str | None] = field(default_factory=dict, repr=False)

    def python(self, repo: GitRepo, commit: str) -> str | None:
        """The oldest available Python the commit supports: old code breaks least on old Pythons."""
        declared = None
        for path in ("pyproject.toml", "setup.cfg", "setup.py"):
            match = _REQUIRES_RE.search(repo.show_file(commit, path) or "")
            if match:
                declared = (int(match[1]), int(match[2]))
                break
        floor = declared or _AVAILABLE[0]
        choice = next((v for v in _AVAILABLE if v >= floor), None)
        return f"{choice[0]}.{choice[1]}" if choice else None

    def version(self, repo: GitRepo, commit: str) -> str | None:
        """The release part of the nearest tag, e.g. "9.2.0" for a commit after the 9.2.0.dev0 tag."""
        if commit not in self._versions:
            proc = subprocess.run(["git", "-C", str(repo.path), "describe", "--tags", "--abbrev=0", commit], capture_output=True)
            match = _RELEASE_RE.search(proc.stdout.decode()) if proc.returncode == 0 else None
            self._versions[commit] = match[0] if match else None
        return self._versions[commit]

    def env(self, worker: int, python: str) -> Path:
        return self.envs.bare_venv(f"{self.key}-w{worker}-py{python.replace('.', '')}", python, fresh=True)

    def setup(self, env: Path, checkout: Path, repo: GitRepo, commit: str) -> str | None:
        """Install the test requirements, then the checkout, with the dependency versions of its day: today's
        releases break old commits.

        A regular install, not an editable one: the build tools of an old commit's day may predate editable
        installs (PEP 660). uv caches local builds by their setup files' timestamps, which often stay the same
        between two commits, so the checkout is always rebuilt.
        """
        pretend = self.version(repo, commit)
        if pretend is None:  # setuptools_scm would call it 0.0.0, which the test dependencies reject
            return f"no release tag reaches {commit[:10]}: fetch the repository's tags"
        for placeholder in self.config.placeholders:
            (checkout / placeholder).parent.mkdir(parents=True, exist_ok=True)
            (checkout / placeholder).touch()
        steps: list[list[str]] = []
        if self.config.requirements:
            found = next((r for r in self.config.requirements if (checkout / r).exists()), None)
            if found is None:
                return f"none of the test requirement files {', '.join(self.config.requirements)} exists here"
            steps.append(["-r", found])
        steps.append(["--reinstall-package", self.package or self.key, *self.config.install])
        cutoff = repo.commits([commit])[commit].committed.astimezone(timezone.utc) + timedelta(days=1)
        environ = {**os.environ, **dict(self.config.env), "SETUPTOOLS_SCM_PRETEND_VERSION": pretend}
        exempt: list[str] = []  # packages deleted from PyPI and uploaded again later have no copy from before
        for step in steps:
            if error := self._install(env, checkout, step, cutoff, environ, exempt):
                return error
        return None

    def _install(self, env: Path, checkout: Path, args: list[str], cutoff: datetime, environ: dict[str, str],
                 exempt: list[str]) -> str | None:  # fmt: skip
        while True:
            overrides = [arg for name in exempt for arg in ("--exclude-newer-package", f"{name}={_stamp(_now())}")]
            cmd = [str(self.envs.uv), "pip", "install", "--quiet", "--python", str(env), "--exclude-newer",
                   _stamp(cutoff), *overrides, *args]  # fmt: skip
            proc = subprocess.run(cmd, cwd=checkout, capture_output=True, env=environ, timeout=900)
            if proc.returncode == 0:
                return None
            error = ((proc.stdout or b"") + b"\n" + (proc.stderr or b"")).decode("utf-8", "replace").strip()
            missing = [name for name in dict.fromkeys(_FILTERED_RE.findall(error)) if name not in exempt]
            if not missing or len(exempt) >= 5:
                return f"uv pip install exited with {proc.returncode}: {error[-1500:]}"
            exempt += missing

    def run(self, env: Path, checkout: Path, labels: list[str]) -> TestRun:
        return self.runner(env, checkout, labels, args=self.config.args, extra_env=dict(self.config.env))


def suite_for(spec: RepoSpec, envs: EnvManager) -> TestSuite:
    if spec.test_style == "pytest":
        return PytestSuite(envs, spec.key, spec.name, spec.pytest_setup)
    return DjangoSuite(envs)
