"""Find fix commits whose message names the commit that introduced the bug.

Django's committing guidelines ask for "Regression in <sha>." in the message
of a commit that fixes a regression, so the bug-introducing commit is labeled
by the developers themselves rather than guessed by blame heuristics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from catchrate.gitrepo import Commit, GitRepo

# "Regression in <sha>" is the documented form; older commits also say "regression
# from <sha>", "regression introduced in/by <sha>" or "the regression was caused by <sha>".
_REGRESSION_RE = re.compile(
    r"\bregression\s+(?:was\s+)?(?:(?:introduced\s+)?(?:in|from)|(?:caused|introduced)\s+by)\s+", re.IGNORECASE
)
_SENTENCE_END_RE = re.compile(r"\.(?=\s|$)|\n\s*\n")
_SHA_TOKEN_RE = re.compile(r"(?<![#\w])([0-9a-fA-F]{7,40})(?!\w)")


@dataclass(frozen=True)
class RegressionFix:
    commit: Commit
    tickets: tuple[int, ...]
    named: tuple[str, ...]  # full SHAs of named commits that precede the fix
    unresolved: tuple[str, ...]  # named tokens that are not commits in the fix's history
    label_source: str = "developer"  # "szz" when SZZ proposed the named commit

    @property
    def sha(self) -> str:
        return self.commit.sha


def parse_regression_tokens(message: str) -> list[str]:
    """Commit-like tokens in "Regression in <sha>[ and <sha>]." sentences."""
    if message.startswith('Revert "'):  # quotes the message of the fix it undoes
        return []
    tokens: list[str] = []
    for match in _REGRESSION_RE.finditer(message):
        tail = message[match.end() : match.end() + 300]
        sentence = _SENTENCE_END_RE.split(tail, maxsplit=1)[0]
        for token in _SHA_TOKEN_RE.findall(sentence):
            token = token.lower()
            if token not in tokens:
                tokens.append(token)
    return tokens


def parse_tickets(subject: str) -> tuple[int, ...]:
    """Ticket numbers before the " -- " in a subject like "Fixed #123, Refs #456 -- ..."."""
    head = subject.split(" -- ", 1)[0]
    return tuple(dict.fromkeys(int(t) for t in re.findall(r"#(\d+)", head)))


def find_regression_fixes(repo: GitRepo, branch: str, since: date | None = None) -> list[RegressionFix]:
    """Commits on `branch` (newest first) whose message names a regressing commit."""
    commits = repo.log(
        branch,
        grep="regression",
        ignore_case=True,
        first_parent=True,
        since=since.isoformat() if since else None,
    )
    candidates = [(c, parse_regression_tokens(c.message)) for c in commits]
    candidates = [(c, tokens) for c, tokens in candidates if tokens]
    resolved = repo.resolve_commits(t for _, tokens in candidates for t in tokens)
    fixes = []
    for commit, tokens in candidates:
        named: list[str] = []
        unresolved: list[str] = []
        for token in tokens:
            sha = resolved.get(token)
            if sha is None or sha == commit.sha or not repo.is_ancestor(sha, commit.sha):
                unresolved.append(token)
            elif sha not in named:
                named.append(sha)
        fixes.append(RegressionFix(commit, parse_tickets(commit.subject), tuple(named), tuple(unresolved)))
    return fixes
