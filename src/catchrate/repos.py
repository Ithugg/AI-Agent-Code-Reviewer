"""Repositories the miner knows how to handle."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Literal


@dataclass(frozen=True)
class PytestSetup:
    """How to install and run a pytest-style project's own tests at any commit."""

    install: tuple[str, ...] = (".[dev,testing]",)  # `uv pip install` arguments for the checkout itself
    # Test requirement files, installed first: the first one a commit has (their names change over the years).
    requirements: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()  # extra environment for installing and for running tests
    placeholders: tuple[str, ...] = ()  # empty files a plain checkout needs to build, e.g. submodule markers
    args: tuple[str, ...] = ()  # extra pytest arguments, e.g. to override the project's addopts


@dataclass(frozen=True)
class RepoSpec:
    key: str
    owner: str
    name: str
    default_branch: str
    source_prefixes: tuple[str, ...]
    test_prefix: str
    exclude_prefixes: tuple[str, ...] = ()
    test_excludes: tuple[str, ...] = ()  # under test_prefix but not collected as tests, e.g. example data
    release_notes_prefix: str | None = None
    # Branches a PR may have targeted; merged PRs keep their base name after a rename.
    merge_branches: tuple[str, ...] = ("main", "master")
    project: str = ""  # display name used in prompts, e.g. "Django"
    # Test-suite machinery under test_prefix that must never be copied between versions.
    test_infrastructure: tuple[str, ...] = ()
    # How bug fixes are found: commit messages naming the regressing commit (developer labels),
    # or changelog fragments marking a bug fix, whose introducing commit SZZ proposes.
    fix_source: Literal["regression_message", "changelog_bugfix"] = "regression_message"
    changelog_globs: tuple[str, ...] = ()  # e.g. ("changelog/*.bugfix.rst",)
    test_style: Literal["django", "pytest"] = "django"
    pytest_setup: PytestSetup = PytestSetup()
    # Subjects of the ticketed commits clean PRs are sampled from (empty: any commit).
    clean_subject_prefixes: tuple[str, ...] = ()

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.name}.git"

    def is_source(self, path: str) -> bool:
        return path.startswith(self.source_prefixes) and not path.startswith(self.exclude_prefixes)

    def is_test(self, path: str) -> bool:
        return path.startswith(self.test_prefix) and path.endswith(".py") and not path.startswith(self.test_excludes)

    def is_release_note(self, path: str) -> bool:
        prefix = self.release_notes_prefix
        if prefix is None or not path.startswith(prefix):
            return False
        return path.endswith((".txt", ".rst")) or any(fnmatch(path, glob) for glob in self.changelog_globs)


REPOS: dict[str, RepoSpec] = {
    "django": RepoSpec(
        key="django",
        owner="django",
        name="django",
        default_branch="main",
        source_prefixes=("django/",),
        exclude_prefixes=("django/conf/locale/",),
        test_prefix="tests/",
        release_notes_prefix="docs/releases/",
        project="Django",
        test_infrastructure=("tests/runtests.py", "tests/test_sqlite.py", "tests/requirements/"),
        clean_subject_prefixes=("Fixed #", "Refs #"),
    ),
    "pytest": RepoSpec(
        key="pytest",
        owner="pytest-dev",
        name="pytest",
        default_branch="main",
        source_prefixes=("src/",),
        test_prefix="testing/",
        test_excludes=("testing/example_scripts/", "testing/plugins_integration/"),  # pytest's norecursedirs
        release_notes_prefix="changelog/",
        project="pytest",
        fix_source="changelog_bugfix",
        changelog_globs=("changelog/*.bugfix.rst",),
        test_style="pytest",
    ),
    "aiohttp": RepoSpec(
        key="aiohttp",
        owner="aio-libs",
        name="aiohttp",
        default_branch="master",
        source_prefixes=("aiohttp/",),
        test_prefix="tests/",
        test_excludes=("tests/autobahn/",),  # needs Docker
        release_notes_prefix="CHANGES/",
        project="aiohttp",
        fix_source="changelog_bugfix",
        changelog_globs=("CHANGES/*.bugfix", "CHANGES/*.bugfix.rst"),
        test_style="pytest",
        pytest_setup=PytestSetup(
            install=(".",),
            # They pin a released aiohttp (for pytest-aiohttp), so they go in first and the checkout over them.
            requirements=("requirements/test.txt", "requirements/ci-wheel.txt"),
            # The pure-Python build: the C extensions need the vendored llhttp submodule and a compiler.
            env=(("AIOHTTP_NO_EXTENSIONS", "1"),),
            # setup.py refuses a git checkout without its parser submodule; the markers it looks for over time.
            placeholders=("vendor/http-parser/README.md", "vendor/llhttp/README.md"),
            # Its addopts can spread tests over every core (xdist) and add coverage; keep the one that stops
            # pytest-aiohttp's entry point loading the plugin its conftest already loads.
            args=("-o", "addopts=", "-p", "no:aiohttp"),
        ),
    ),
}
