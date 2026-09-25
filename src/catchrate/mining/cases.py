"""Turn developer-labeled regression fixes into benchmark cases.

A case is one reviewable change (a PR, or a single commit when the PR can't be
used) plus every bug the developers traced back to it.
"""

from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from catchrate.diffparse import FileDiff, parse_diff, to_ranges
from catchrate.github import GitHubClient
from catchrate.gitrepo import GitError, GitRepo
from catchrate.mining.bugfixes import find_changelog_fixes, szz_label
from catchrate.mining.fixtests import extract_fix_tests
from catchrate.mining.pr_range import choose_pr, landed_range
from catchrate.mining.regressions import RegressionFix, find_regression_fixes
from catchrate.repos import RepoSpec
from catchrate.schema import BugLabel, BugLine, Case, PRInfo, Rejection, ReviewInput
from catchrate.szz.blame import BlamedLine, blame_changes, select_sources

log = logging.getLogger(__name__)

_TICKET_ROLE_RE = re.compile(r":ticket:`(\d+)`")
_ROLE_RE = re.compile(r":[\w:.-]+:`(~?)([^`<]+?)\s*(?:<[^>]*>)?`")


@dataclass(frozen=True)
class MineOptions:
    max_changed_lines: int = 500
    ignore_revs_file: Path | None = None
    workers: int = 6


@dataclass(frozen=True)
class _Traced:
    review: ReviewInput
    review_flags: tuple[str, ...]
    bug: BugLabel


def find_labeled_fixes(repo: GitRepo, spec: RepoSpec, since: date | None, opts: MineOptions) -> list[RegressionFix]:
    """Fixes with a named introducing commit: from developers' messages, or proposed by SZZ."""
    if spec.fix_source == "changelog_bugfix":
        found = find_changelog_fixes(repo, spec, since)
        return szz_label(repo, spec, found, ignore_revs_file=opts.ignore_revs_file, workers=opts.workers)
    return [f for f in find_regression_fixes(repo, spec.default_branch, since) if f.named]


def mine_regression_cases(
    repo: GitRepo, gh: GitHubClient, spec: RepoSpec, *, since: date | None = None, opts: MineOptions = MineOptions()
) -> tuple[list[Case], list[Rejection]]:
    fixes = find_labeled_fixes(repo, spec, since, opts)
    prs = gh.commit_pull_requests(spec.owner, spec.name, sorted({sha for f in fixes for sha in f.named}))
    jobs = [(fix, named) for fix in fixes for named in fix.named]
    log.info("Tracing %d (fix, introducing commit) pairs from %d labeled fixes", len(jobs), len(fixes))
    done, lock = [0], threading.Lock()

    def trace(job: tuple[RegressionFix, str]) -> _Traced | Rejection:
        fix, named = job
        try:
            result = _trace_bug(repo, spec, opts, fix, named, prs.get(named, []))
        except GitError as exc:
            result = Rejection(repo=spec.key, fix_commit=fix.sha, introducing_commit=named, reasons=[f"git_error: {exc}"])
        with lock:
            done[0] += 1
            if done[0] % 100 == 0:
                log.info("  %d/%d", done[0], len(jobs))
        return result

    with ThreadPoolExecutor(opts.workers) as pool:
        results = list(pool.map(trace, jobs))

    by_review: dict[tuple[str, str], list[_Traced]] = {}
    for r in results:
        if isinstance(r, _Traced):
            by_review.setdefault((r.review.base_commit, r.review.head_commit), []).append(r)
    cases = [_make_case(spec, traced) for traced in by_review.values()]
    cases.sort(key=lambda c: (max(b.fix_date for b in c.bugs), c.case_id), reverse=True)
    return cases, [r for r in results if isinstance(r, Rejection)]


def _make_case(spec: RepoSpec, traced: list[_Traced]) -> Case:
    chosen = next((t for t in traced if t.review.review_unit == "pr"), traced[0])
    bugs: dict[str, BugLabel] = {}
    for t in sorted(traced, key=lambda t: (t.bug.fix_date, t.bug.fix_commit, t.bug.introducing_commit)):
        bugs.setdefault(t.bug.fix_commit, t.bug)  # a fix naming two commits of one PR is still one bug
    review = chosen.review
    case_id = f"{spec.key}-pr{review.pr_number}" if review.review_unit == "pr" else f"{spec.key}-c{review.head_commit[:10]}"
    return Case(case_id=case_id, repo=spec.key, review=review, bugs=list(bugs.values()), flags=list(chosen.review_flags))


def _trace_bug(
    repo: GitRepo, spec: RepoSpec, opts: MineOptions, fix: RegressionFix, named: str, prs: list[PRInfo]
) -> _Traced | Rejection:
    review, review_flags = _review_unit(repo, spec, opts, named, prs)
    bug_flags = ["multi_origin"] if len(fix.named) > 1 else []
    if review is None:
        return Rejection(repo=spec.key, fix_commit=fix.sha, introducing_commit=named, reasons=review_flags + bug_flags)

    parent = fix.commit.parents[0]
    fix_diff = parse_diff(repo.diff(parent, fix.sha, unified=0))
    blamed = blame_changes(repo, parent, select_sources(spec, fix_diff), ignore_revs_file=opts.ignore_revs_file)
    lines, unlocated = _locate(repo, review, parent, blamed, opts.ignore_revs_file)
    tests = extract_fix_tests(repo, spec, fix.sha, fix_diff)

    reasons = [r for r, bad in (("no_fix_test", not tests), ("no_bug_lines", not lines)) if bad]
    if reasons:
        return Rejection(
            repo=spec.key, fix_commit=fix.sha, introducing_commit=named, reasons=review_flags + bug_flags + reasons
        )
    if all(line.origin == "anchor" for line in lines):
        bug_flags.append("anchor_only")

    note = _release_note(spec, fix_diff)
    bug = BugLabel(
        label_source=fix.label_source,  # type: ignore[arg-type]
        introducing_commit=named,
        fix_commit=fix.sha,
        fix_date=fix.commit.committed,
        fix_subject=fix.commit.subject,
        tickets=list(fix.tickets),
        bug_summary=note or fix.commit.subject,
        bug_summary_source="release_note" if note else "fix_subject",
        locality="in_diff" if any(line.in_diff for line in lines) else "outside_diff",
        bug_files=sorted({line.path for line in lines}),
        bug_lines=lines,
        unlocated_lines=unlocated,
        fix_tests=tests,
        flags=bug_flags,
    )
    return _Traced(review, tuple(review_flags), bug)


def _review_unit(
    repo: GitRepo, spec: RepoSpec, opts: MineOptions, named: str, prs: list[PRInfo]
) -> tuple[ReviewInput | None, list[str]]:
    """The PR that landed `named`, or just `named` itself when the PR can't be used."""
    flags: list[str] = []
    pr = choose_pr(prs, spec.merge_branches)
    if pr is None:
        flags.append("no_pr")
    else:
        landed = landed_range(repo, pr)
        if landed is None or named not in landed.commits:
            flags.append("pr_range_mismatch")
        else:
            review = make_review(repo, "pr", landed.base, landed.head, list(landed.commits), pr)
            if review.changed_lines <= opts.max_changed_lines:
                return review, flags
            flags.append("pr_too_large")
    commit = repo.commits([named])[named]
    if not commit.parents:
        return None, flags + ["root_commit"]
    review = make_review(repo, "commit", commit.parents[0], named, [named], pr, message=commit.message)
    if review.changed_lines > opts.max_changed_lines:
        return None, flags + ["commit_too_large"]
    return review, flags


def make_review(
    repo: GitRepo, unit: str, base: str, head: str, commits: list[str], pr: PRInfo | None, message: str = ""
) -> ReviewInput:
    stats = repo.numstat(base, head)
    if unit == "pr" and pr is not None:
        title, description = pr.title, pr.body
    else:
        title, _, description = message.partition("\n")
    return ReviewInput(
        review_unit=unit,  # type: ignore[arg-type]
        base_commit=base,
        head_commit=head,
        commits=commits,
        pr_number=pr.number if pr else None,
        pr_url=pr.url if pr else None,
        title=title.strip(),
        description=description.strip(),
        diff=repo.diff(base, head),
        changed_files=[s.path for s in stats],
        changed_lines=sum((s.added or 0) + (s.deleted or 0) for s in stats),
    )


def _locate(
    repo: GitRepo, review: ReviewInput, parent: str, blamed: list[BlamedLine], ignore_revs_file: Path | None
) -> tuple[list[BugLine], int]:
    """Place fix-touched lines at the review head; also count those that didn't exist there yet.

    Lines the reviewed change introduced are matched by blaming the head, which
    sees through ignored reformat commits. Older lines are carried back through
    the diff between the review head and the fix's parent.
    """
    in_review = set(review.commits)
    ours = [b for b in blamed if b.commit in in_review]
    positions = _review_positions(repo, review, ignore_revs_file) if ours else {}
    older = {b.commit for b in blamed if b.commit not in in_review}
    existed = {c for c in older if repo.is_ancestor(c, review.head_commit)}
    paths = {b.path for b in blamed if b.commit in existed}
    later = {}
    if paths:
        # git only pairs a rename when both of its paths are in the pathspec.
        renamed = repo.renames(review.head_commit, parent)
        pathspec = sorted(paths | {renamed[p] for p in paths if p in renamed})
        diff = repo.diff(review.head_commit, parent, unified=0, ignore_whitespace=True, paths=pathspec)
        later = {fd.new_path: fd for fd in parse_diff(diff) if fd.new_path}

    found: dict[tuple[str, int], BugLine] = {}
    unlocated = 0
    for b in blamed:
        if b.commit in in_review:
            pos = positions.get((b.commit, b.orig_path, b.orig_line))
        elif b.commit in existed:
            pos = carry_back(later.get(b.path), b.path, b.line)
        else:
            pos = None
        if pos is None:
            unlocated += 1
            continue
        line = BugLine(path=pos[0], line=pos[1], origin=b.origin, in_diff=b.commit in in_review)
        kept = found.get(pos)
        if kept is None or (line.origin == "deleted" and kept.origin == "anchor"):
            found[pos] = line
    return sorted(found.values(), key=lambda line: (line.path, line.line)), unlocated


def carry_back(later: FileDiff | None, path: str, line: int) -> tuple[str, int] | None:
    """Where `line` of `path` sat before the changes in `later` (a -U0 diff), or None if they touched it."""
    if later is None:
        return path, line  # file unchanged in between
    if later.old_path is None:
        return None  # file created in between
    shift = 0
    for h in later.hunks:
        if h.new_count and h.new_start <= line < h.new_start + h.new_count:
            return None
        last = h.new_start + h.new_count - 1 if h.new_count else h.new_start
        if last < line:
            shift += h.new_count - h.old_count
    return later.old_path, line - shift


def _review_positions(
    repo: GitRepo, review: ReviewInput, ignore_revs_file: Path | None
) -> dict[tuple[str, str, int], tuple[str, int]]:
    """(commit, path in commit, line in commit) -> (path, line) at the review head, for lines the review added."""
    in_review = set(review.commits)
    positions: dict[tuple[str, str, int], tuple[str, int]] = {}
    for fd in parse_diff(repo.diff(review.base_commit, review.head_commit, unified=0)):
        ranges = to_ranges(fd.added_lines())
        if fd.new_path is None or fd.binary or not ranges:
            continue
        try:
            entries = repo.blame(review.head_commit, fd.new_path, ranges, ignore_revs_file=ignore_revs_file)
        except GitError as exc:
            log.warning("blame failed for %s at %s: %s", fd.new_path, review.head_commit[:10], exc)
            continue
        for e in entries:
            if e.commit in in_review:
                positions[(e.commit, e.orig_path, e.orig_line)] = (fd.new_path, e.line)
    return positions


def _release_note(spec: RepoSpec, diffs: list[FileDiff]) -> str | None:
    """The first release-note bullet (or changelog-fragment paragraph) the fix added, as plain text."""
    for fd in diffs:
        if fd.new_path and spec.is_release_note(fd.new_path):
            added = [text for h in fd.hunks for _, text in h.added]
            # A new changelog fragment is plain prose; an edited release-notes file only counts as bullets.
            note = _first_bullet(added) or (_first_paragraph(added) if fd.old_path is None else None)
            if note:
                return note
    return None


def _first_bullet(lines: list[str]) -> str | None:
    bullet: list[str] = []
    for line in lines:
        if line.startswith("* "):
            if bullet:
                break
            bullet.append(line[2:])
        elif bullet and line.startswith("  ") and line.strip():
            bullet.append(line.strip())
        elif bullet:
            break
    return _plain(" ".join(bullet)) if bullet else None


def _first_paragraph(lines: list[str]) -> str | None:
    paragraph: list[str] = []
    for line in lines:
        if line.strip():
            paragraph.append(line.strip())
        elif paragraph:
            break
    return _plain(" ".join(paragraph)) if paragraph else None


def _plain(rst: str) -> str:
    text = _TICKET_ROLE_RE.sub(r"#\1", " ".join(rst.split()))
    text = _ROLE_RE.sub(lambda m: m[2].rsplit(".", 1)[-1] if m[1] else m[2], text)
    return text.replace("``", "")
