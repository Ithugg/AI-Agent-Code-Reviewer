"""Run each bug's fix tests at four commits and decide whether its label holds.

1. At the fix the tests must pass, or this machine can't judge the case.
2. Just before the fix they must fail: that failure is the bug's signature.
3. Right after the reviewed change they must fail the same way (same exception
   type): the bug is there. Failing some other way usually means the test needs
   something added later, which proves nothing.
4. Before the reviewed change they should pass, or be unable to run because
   the code under test didn't exist. Failing the bug's way means the bug is older.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from catchrate.diffparse import parse_diff
from catchrate.gitrepo import GitRepo
from catchrate.repos import RepoSpec
from catchrate.schema import BugLabel, Case, StateResult, Verification
from catchrate.verify.checkout import Checkout
from catchrate.verify.envs import EnvManager
from catchrate.verify.suites import DjangoSuite, TestRunner, TestSuite, suite_for
from catchrate.verify.testrun import TestRun, run_django_tests

log = logging.getLogger(__name__)
_CANT_RUN = frozenset({"load_error", "no_tests", "skipped"})
_MISSING_NAME = frozenset({"NameError", "ImportError", "ModuleNotFoundError", "AttributeError"})
_MISSING_API_RE = re.compile(r"has no attribute|cannot import name|is not defined|No module named|unexpected keyword argument")
# runtests.py aborts before running anything when a test needs a backend SQLite can't stand in for.
_NEEDS_BACKEND_RE = re.compile(r"Aborting: (A [\w ]+ database backend is required[^\n]*)")


def state_status(run: TestRun, labels: list[str], sep: str = ".") -> tuple[str, dict[str, str], dict[str, str]]:
    """(status, outcomes, failure signatures) of the tests selected by `labels` in one run.

    `sep` joins a label to what it contains: "." for Django labels, "::" for pytest node ids.
    """

    def selected(test_id: str) -> bool:
        return any(test_id == label or test_id.startswith((label + sep, label + "[")) for label in labels)

    outcomes = {t: o for t, o in run.outcomes.items() if selected(t)}
    signatures = {t: s for t, s in run.signatures.items() if t in outcomes}
    failing = any(o in ("fail", "error", "xpass") for o in outcomes.values())
    blocked = any(
        label == err or label.startswith(err + sep) or err.startswith(label + sep)
        or err in label.split(sep)  # unittest names a module that did not import by its last part, e.g. "tests"
        for label in labels
        for err in run.load_errors
    )  # fmt: skip
    if run.returncode is None:
        status = "timeout"
    elif failing:
        status = "fail"
    elif blocked:
        status = "load_error"
    elif not run.outcomes and _NEEDS_BACKEND_RE.search(run.tail):
        status = "skipped"
    elif not run.outcomes and not run.load_errors and run.returncode != 0:
        status = "env_error"
    elif not outcomes:
        status = "no_tests"
    elif all(o == "skip" for o in outcomes.values()):
        status = "skipped"
    else:
        status = "pass"
    return status, outcomes, signatures


def failure_kinds(state: StateResult) -> dict[str, str]:
    """Failing test id -> exception type, e.g. "AssertionError" or "django.core.exceptions.FieldError"."""
    return {
        t: state.signatures.get(t, "").split(":", 1)[0].strip()
        for t, outcome in state.outcomes.items()
        if outcome in ("fail", "error", "xpass")
    }


def _short(kind: str) -> str:
    return kind.rsplit(".", 1)[-1]


def _missing_name_only_before(base: StateResult, head: StateResult, test: str) -> bool:
    """The test fails before the change on a name that didn't exist yet, which the bug's failure doesn't
    share: the same exception type (AttributeError, say) then isn't the bug's way of failing."""
    before, after = base.signatures.get(test, ""), head.signatures.get(test, "")
    return bool(_MISSING_API_RE.search(before)) and before != after


def classify(
    fix: StateResult, fix_parent: StateResult | None, head: StateResult | None, base: StateResult | None
) -> tuple[str, str]:
    """(verdict, reason) from the four runs; later runs are None when an earlier one settled it."""
    if fix.status == "skipped":
        reason = next(iter(fix.skip_reasons.values()), "no reason given")
        return "unverifiable", f"the fix's tests skip on this machine ({reason})"
    for name, state in (("the fix", fix), ("just before the fix", fix_parent), ("the reviewed change", head), ("the base", base)):
        if state is not None and state.status == "env_error":
            return "broken", f"environment: the test suite could not start on Python {state.python} at {name}"
    if fix.status != "pass":
        return "broken", f"the fix's tests {fix.status} at the fix itself"
    if fix_parent is None or fix_parent.status != "fail":
        status = fix_parent.status if fix_parent else "were not run"
        return "not_fail_to_pass", f"the fix's tests {status} right before the fix, so they don't catch the bug"
    bug = failure_kinds(fix_parent)
    if head is None:
        return "broken", "the reviewed change was not run"
    if head.status == "pass":
        return "not_reproduced", "the fix's tests pass right after the reviewed change"
    if head.status != "fail":
        return "head_unclear", f"the fix's tests {head.status} right after the reviewed change"
    at_head = failure_kinds(head)
    matched = {t for t, kind in at_head.items() if bug.get(t) == kind}
    if not matched:
        return "head_unclear", "fails right after the change, but not the way the bug fails"
    if base is None:
        return "broken", "the base was not run"
    if base.status == "pass":
        return "verified", "passes before the change, fails the bug's way after it, passes at the fix"
    if base.status in _CANT_RUN:
        return "verified_new", f"fails the bug's way after the change; before it the tests {base.status.replace('_', ' ')}"
    if base.status != "fail":
        return "broken", f"the fix's tests {base.status} before the reviewed change"
    at_base = failure_kinds(base)
    same = {t for t in matched if at_base.get(t) == at_head[t] and not _missing_name_only_before(base, head, t)}
    if same == matched:
        return "predates", "fails the bug's way before the change too, so the bug is older"
    if same:
        return "verified_differs", "some of the bug's tests already fail the bug's way before the change"
    passed = {t for t in matched if base.outcomes.get(t) in ("ok", "skip", "xfail")}
    missing = {t for t in matched if t not in base.outcomes or _short(at_base.get(t, "")) in _MISSING_NAME}
    if passed | missing == matched:
        if passed:
            return "verified", "the bug's tests pass before the change and fail its way after it"
        return "verified_new", "before the change the tests fail on names that didn't exist yet"
    return "verified_differs", "before the change the tests fail too, but not the way the bug fails"


@dataclass
class Verifier:
    repo: GitRepo
    spec: RepoSpec
    envs: EnvManager
    work_dir: Path
    workers: int = 4
    runner: TestRunner = run_django_tests
    suite: TestSuite | None = None  # default: the repo's own suite (Django's runtests.py or pytest)

    def __post_init__(self) -> None:
        if self.suite is None:
            is_django = self.spec.test_style == "django"
            self.suite = DjangoSuite(self.envs, self.runner) if is_django else suite_for(self.spec, self.envs)

    def python_versions(self, cases: Iterable[Case]) -> set[str]:
        """Pythons needed to run every state of every bug."""
        assert self.suite is not None
        commits = set()
        for case in cases:
            for bug in case.bugs:
                fix_parent = self.repo.commits([bug.fix_commit])[bug.fix_commit].parents[0]
                commits |= {bug.fix_commit, fix_parent, case.review.head_commit, case.review.base_commit}
        return {py for c in commits if (py := self.suite.python(self.repo, c))}

    def verify_all(self, cases: list[Case], sink: Callable[[Verification], None]) -> Counter[str]:
        jobs = [(case, bug) for case in cases for bug in case.bugs]
        assert self.suite is not None
        for version in sorted(self.python_versions(cases)):
            log.info("Preparing Python %s test environment", version)
            self.suite.env(0, version)
        checkouts: queue.Queue[Checkout] = queue.Queue()
        start = self.repo.head_of(self.spec.default_branch)
        prefix = "w" if self.spec.key == "django" else f"{self.spec.key}-w"  # Django's worktrees predate other repos
        for i in range(min(self.workers, len(jobs)) or 1):
            checkouts.put(Checkout(self.repo, self.work_dir / f"{prefix}{i}", start, worker=i))
        verdicts: Counter[str] = Counter()
        lock = threading.Lock()

        def job(pair: tuple[Case, BugLabel]) -> None:
            case, bug = pair
            checkout = checkouts.get()
            try:
                result = self.verify_bug(checkout, case, bug)
            except Exception as exc:  # one broken case must not end a multi-hour run
                result = Verification(
                    case_id=case.case_id,
                    fix_commit=bug.fix_commit,
                    introducing_commit=bug.introducing_commit,
                    verdict="broken",
                    reason=f"error: {type(exc).__name__}: {exc}"[:500],
                )
            finally:
                checkouts.put(checkout)
            with lock:
                sink(result)
                verdicts[result.verdict] += 1
                log.info("[%d/%d] %s %s: %s", sum(verdicts.values()), len(jobs), case.case_id, bug.fix_commit[:8], result.verdict)

        with ThreadPoolExecutor(self.workers) as pool:
            list(pool.map(job, jobs))
        return verdicts

    def verify_bug(self, checkout: Checkout, case: Case, bug: BugLabel) -> Verification:
        fix = self.repo.commits([bug.fix_commit])[bug.fix_commit]
        parent = fix.parents[0]
        paths = [
            p
            for fd in parse_diff(self.repo.diff(parent, fix.sha, unified=0))
            if (p := fd.new_path or fd.old_path)
            and p.startswith(self.spec.test_prefix)
            and not p.startswith(self.spec.test_infrastructure)  # the runner must match the checkout
        ]
        patch = self.repo.run("diff", "--binary", "--no-ext-diff", parent, fix.sha, "--", *paths) if paths else ""
        labels = bug.fix_tests

        def run(commit: str, with_patch: bool) -> StateResult:
            return self._run(checkout, commit, labels, patch if with_patch else None, fix.sha, paths)

        fix_state = run(fix.sha, with_patch=False)
        fix_parent = head = base = None
        if fix_state.status == "pass":
            fix_parent = run(parent, with_patch=True)
            if fix_parent.status == "fail":
                head = run(case.review.head_commit, with_patch=True)
                if head.status == "fail" and set(failure_kinds(head).items()) & set(failure_kinds(fix_parent).items()):
                    base = run(case.review.base_commit, with_patch=True)
        verdict, reason = classify(fix_state, fix_parent, head, base)
        return Verification(
            case_id=case.case_id,
            fix_commit=bug.fix_commit,
            introducing_commit=bug.introducing_commit,
            verdict=verdict,  # type: ignore[arg-type]
            reason=reason,
            fix=fix_state,
            fix_parent=fix_parent,
            head=head,
            base=base,
        )

    def _run(
        self, checkout: Checkout, commit: str, labels: list[str], patch: str | None, source: str, paths: list[str]
    ) -> StateResult:
        assert self.suite is not None
        checkout.switch(commit)
        method = checkout.apply_tests(patch, source, paths) if patch is not None else "none"
        python = self.suite.python(self.repo, commit)
        version = self.suite.version(self.repo, commit)
        empty = dict(outcomes={}, signatures={}, load_errors=[], seconds=0.0)
        if python is None:
            return StateResult(commit=commit, version=version, python=None, patch=method, status="no_tests", **empty)
        if method == "failed":
            return StateResult(commit=commit, version=version, python=python, patch=method, status="load_error", **empty)
        env = self.suite.env(checkout.worker, python)
        error = self.suite.setup(env, checkout.path, self.repo, commit)
        if error is not None:
            return StateResult(commit=commit, version=version, python=python, patch=method, status="env_error",
                               tail=error[-2000:], **empty)  # fmt: skip
        run = self.suite.run(env, checkout.path, labels)
        status, outcomes, signatures = state_status(run, labels, self.suite.label_sep)
        skip_reasons = {t: r for t, r in run.skip_reasons.items() if t in outcomes}
        if status == "skipped" and not skip_reasons and (backend := _NEEDS_BACKEND_RE.search(run.tail)):
            skip_reasons = {"*": backend[1]}
        return StateResult(
            commit=commit,
            version=version,
            python=python,
            patch=method,
            status=status,  # type: ignore[arg-type]
            outcomes=outcomes,
            signatures=signatures,
            load_errors=list(run.load_errors),
            seconds=round(run.seconds, 1),
            skip_reasons=skip_reasons,
            tail="" if status in ("pass", "fail") else run.tail[-2000:],
        )
