"""Virtualenvs for running Django's own test suite at historical commits.

Interpreters come from uv (python-build-standalone) and live inside the data
directory, so nothing is installed system-wide.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from catchrate.gitrepo import GitRepo

# Packages Django's test suite imports; without the optional ones some tests skip.
TEST_DEPS = (
    "asgiref",
    "sqlparse",
    "tzdata",
    "pytz",
    "pillow",
    "pyyaml",
    "jinja2",
    "docutils",
    "argon2-cffi",
    "bcrypt",
    "tblib",
)

# Django 1.x/2.0/2.1 need these (mock for Python 2; six for 1.10-era commits that imported it by
# mistake); the rest of TEST_DEPS is optional and installed best-effort.
LEGACY_CORE_DEPS = ("pytz", "sqlparse", "six", "mock")

# (oldest Django version, Python to test it with), newest first. uv has no Python older than 3.8 on
# Windows, so 3.6, 3.5 and 2.7 come from the Python Software Foundation's portable NuGet builds
# (packages "python" and "python2"). Django before 1.9 predates Python 3.5, which broke it.
_PYTHON_FOR_DJANGO = (
    ((5, 0), "3.12"),
    ((4, 1), "3.11"),
    ((4, 0), "3.10"),
    ((3, 2), "3.9"),
    ((2, 2), "3.8"),
    ((1, 11), "3.6"),
    ((1, 9), "3.5"),
    ((0, 0), "2.7"),
)
_VERSION_RE = re.compile(r"^VERSION\s*=\s*(?:VersionTuple)?\(\s*(\d+)\s*,\s*(\d+)", re.MULTILINE)


def django_version(repo: GitRepo, commit: str) -> tuple[int, int] | None:
    match = _VERSION_RE.search(repo.show_file(commit, "django/__init__.py") or "")
    return (int(match[1]), int(match[2])) if match else None


def python_for(version: tuple[int, int]) -> str:
    return next(py for oldest, py in _PYTHON_FOR_DJANGO if version >= oldest)


def find_uv() -> Path:
    """uv from this interpreter's environment, else from PATH."""
    scripts = Path(sys.executable).parent
    found = shutil.which("uv", path=str(scripts)) or shutil.which("uv")
    if found is None:
        raise RuntimeError("uv not found: pip install uv")
    return Path(found)


def find_legacy_pythons(tools_dir: Path) -> dict[str, Path]:
    """Portable interpreters extracted to tools_dir/python-X.Y.Z/, keyed by "X.Y"."""
    found: dict[str, Path] = {}
    for exe in sorted(tools_dir.glob("python-*/python.exe")):
        major_minor = ".".join(exe.parent.name.removeprefix("python-").split(".")[:2])
        found[major_minor] = exe.resolve()
    return found


@dataclass
class EnvManager:
    root: Path  # python/ holds uv's interpreters; py312/ etc. are venvs
    uv: Path
    legacy: dict[str, Path] = field(default_factory=dict)  # "3.6" -> a portable python.exe uv can't provide
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> EnvManager:
        return cls(data_dir / "envs", find_uv(), find_legacy_pythons(data_dir / "tools" / "nuget"))

    def python(self, version: str) -> Path:
        """The venv interpreter for `version`, created with TEST_DEPS on first use."""
        if version.startswith("2."):
            return self._python2(version)
        venv = (self.root / f"py{version.replace('.', '')}").resolve()
        exe = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        with self._lock:
            if not exe.exists():
                if version in self.legacy:
                    self._legacy_venv(self.legacy[version], venv, exe)
                else:
                    self._install_python(version)
                    self._uv("venv", "--quiet", "--python", version, str(venv))
                    self._uv("pip", "install", "--quiet", "--python", str(exe), *TEST_DEPS)
        return exe

    def bare_venv(self, name: str, version: str, *, fresh: bool = False) -> Path:
        """An empty venv (no packages) for suites that install the project under test themselves;
        `fresh` empties it first, so nothing from an earlier install lingers."""
        venv = (self.root / name).resolve()
        exe = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        with self._lock:
            if not exe.exists():
                self._install_python(version)
            if fresh or not exe.exists():
                self._uv("venv", "--quiet", "--clear", "--python", version, str(venv))
        return exe

    def _install_python(self, version: str) -> None:
        # Only when missing: `uv python install` relinks an installed version too, which fails on Windows
        # while another process (another verify run) is using that Python.
        if not any((self.root / "python").glob(f"cpython-{version}.*")):
            self._uv("python", "install", "--no-bin", version)

    def _python2(self, version: str) -> Path:
        """Python 2 has no venv module, so the private portable copy itself gets the test dependencies."""
        if version not in self.legacy:
            raise RuntimeError(f"no Python {version}: extract the python2 NuGet package into tools/nuget/python-{version}.x")
        exe = self.legacy[version]
        marker = exe.parent / ".catchrate-deps"
        with self._lock:
            if not marker.exists():
                self._install_legacy_deps(exe)
                marker.write_text("installed\n", encoding="utf-8")
        return exe

    def _legacy_venv(self, python: Path, venv: Path, exe: Path) -> None:
        _check([str(python), "-m", "venv", str(venv)], "venv")
        self._install_legacy_deps(exe)

    def _install_legacy_deps(self, exe: Path) -> None:
        pip = [str(exe), "-m", "pip", "install", "--quiet", "--disable-pip-version-check"]
        _check([*pip, *LEGACY_CORE_DEPS], "pip install")
        for dep in TEST_DEPS:  # old interpreters lack wheels for some optional packages; skip those
            if dep not in LEGACY_CORE_DEPS:
                subprocess.run([*pip, dep], capture_output=True)

    def _uv(self, *args: str) -> None:
        env = {
            **os.environ,
            "UV_PYTHON_INSTALL_DIR": str((self.root / "python").resolve()),
            "UV_PYTHON_PREFERENCE": "only-managed",
        }
        _check([str(self.uv), *args], f"uv {' '.join(args[:2])}", env)


def _check(cmd: list[str], what: str, env: dict[str, str] | None = None) -> None:
    proc = subprocess.run(cmd, capture_output=True, env=env)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()[-800:]
        raise RuntimeError(f"{what} failed: {err}")
