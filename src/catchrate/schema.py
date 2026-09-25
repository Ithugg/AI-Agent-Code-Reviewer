"""Typed records for benchmark cases.

A case is split into what the reviewer may see (`ReviewInput`) and the hidden
ground truth (`BugLabel`), so a harness can hand over `case.review` alone.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field


class PRInfo(BaseModel):
    number: int
    title: str
    body: str = ""
    url: str = ""
    merged: bool
    merged_at: datetime | None = None
    base_ref: str
    merge_commit: str | None = None
    commit_count: int
    commit_headlines: list[str] = Field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0


class BugLine(BaseModel):
    path: str  # path at the review head
    line: int  # line number at the review head
    # "deleted": the fix changed this line; "anchor": the fix inserted code next to it.
    origin: Literal["deleted", "anchor"]
    in_diff: bool  # the reviewed change introduced this line


class ReviewInput(BaseModel):
    """Everything a reviewer is allowed to see."""

    review_unit: Literal["pr", "commit"]
    base_commit: str
    head_commit: str
    commits: list[str]
    pr_number: int | None
    pr_url: str | None
    title: str
    description: str
    diff: str
    changed_files: list[str]
    changed_lines: int


class BugLabel(BaseModel):
    """Ground truth for one bug. Never shown to the reviewer."""

    label_source: Literal["developer", "szz"]
    introducing_commit: str
    fix_commit: str
    fix_date: datetime
    fix_subject: str
    tickets: list[int]
    bug_summary: str
    bug_summary_source: Literal["release_note", "fix_subject"]
    # "in_diff": the fix changed lines the reviewed change introduced.
    # "outside_diff": the fix only changed older code, so spotting the bug means
    # reasoning about how the change interacts with code the diff doesn't show.
    locality: Literal["in_diff", "outside_diff"]
    bug_files: list[str]
    bug_lines: list[BugLine]
    # Fix-touched source lines that did not exist yet at the review head.
    unlocated_lines: int
    fix_tests: list[str]
    flags: list[str] = Field(default_factory=list)
    verdict: str | None = None  # set by `catchrate freeze` from the Week 2 verification
    bug_category: str | None = None
    difficulty: Literal["easy", "medium", "hard"] | None = None


class Case(BaseModel):
    """One reviewable change and every known bug it introduced (none for a clean case)."""

    schema_version: Literal[2] = 2
    case_id: str
    repo: str
    kind: Literal["buggy", "clean"] = "buggy"
    # Set by `catchrate freeze`: dev for prompt work, ablation for experiments, test for headline numbers.
    split: Literal["dev", "ablation", "test"] | None = None
    review: ReviewInput
    bugs: list[BugLabel]
    flags: list[str] = Field(default_factory=list)  # how the review unit was chosen


# env_error: the test runner died before running anything (e.g. that Django doesn't import on this Python).
StateStatus = Literal["pass", "fail", "skipped", "load_error", "no_tests", "env_error", "timeout"]
Verdict = Literal[
    "verified",  # passes before the change, fails after it (the bug's way), passes at the fix
    "verified_new",  # same, except before the change the tests can't run: the code under test didn't exist
    "verified_differs",  # same, except before the change the tests fail in some other way
    "predates",  # fails the bug's way before the change too: the label points at the wrong change
    "not_reproduced",  # passes right after the change
    "head_unclear",  # fails right after the change, but not the way the bug fails
    "not_fail_to_pass",  # the fix's tests don't fail right before the fix, so they can't confirm anything
    "unverifiable",  # the tests skip here (another database backend or an optional dependency)
    "broken",  # the fix's tests don't pass at the fix, or something else went wrong
]
VERIFIED: frozenset[str] = frozenset({"verified", "verified_new", "verified_differs"})


class StateResult(BaseModel):
    """The fix's tests run at one commit."""

    commit: str
    # The project's version at the commit ("5.2"); files written before multi-repo support call it "django".
    version: str | None = Field(default=None, validation_alias=AliasChoices("version", "django"))
    python: str | None  # "3.12"
    patch: Literal["none", "applied", "3way", "graft", "overlay", "failed"]
    status: StateStatus
    outcomes: dict[str, str]  # selected test id -> ok / fail / error / skip / xfail / xpass
    signatures: dict[str, str]  # failing test id -> final exception line
    load_errors: list[str]
    seconds: float
    skip_reasons: dict[str, str] = Field(default_factory=dict)
    tail: str = ""  # end of the runner output when something went wrong


class Verification(BaseModel):
    """Whether one bug label survives running its fix's tests (Week 2)."""

    case_id: str
    fix_commit: str
    introducing_commit: str
    verdict: Verdict
    reason: str
    fix: StateResult | None = None  # at the fix: tests should pass
    fix_parent: StateResult | None = None  # just before the fix: tests should fail; this is the bug's signature
    head: StateResult | None = None  # right after the reviewed change: tests should fail the same way
    base: StateResult | None = None  # before it: tests should pass, or not run at all


class Rejection(BaseModel):
    repo: str
    fix_commit: str
    introducing_commit: str | None
    reasons: list[str]
