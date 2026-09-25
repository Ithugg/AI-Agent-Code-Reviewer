---
license:
  - bsd-3-clause
  - mit
  - apache-2.0
language:
  - en
  - code
pretty_name: Catch Rate
task_categories:
  - text-generation
tags:
  - code-review
  - bug-detection
  - software-engineering
  - benchmark
size_categories:
  - n<1K
---

# Catch Rate: real bugs that got past code review

Each case is a merged pull request to Django, pytest or aiohttp (or, when the PR was too large, the single commit
inside it) that introduced at least one bug a human reviewer missed. The task: review the change and find the bug,
with no hint that one exists. Clean cases (merged changes with no known bug) measure false alarms. Each repository
is its own config: `django` has `dev`, `ablation` and `test` splits; `pytest` and `aiohttp` are held out entirely as
`test`, to check that what works on Django generalizes.

This is a detection benchmark, not a repair benchmark: SWE-bench gives an agent a known issue and asks for a fix;
Catch Rate gives it a pull request and asks whether anything is wrong.

## How the cases were built

1. **Where each bug came from.**
   - *Django: developer labels.* Django's committers write "Regression in <sha>." in the message of a commit that
     fixes a regression, naming the commit that caused it. 633 fixes on `main` name a commit in the fix's own
     history (older phrasings "regression from/introduced in/caused by <sha>" included).
   - *pytest and aiohttp: changelog fragments plus SZZ.* Every commit on the main branch since 2021 that adds a
     bug-fix fragment (`changelog/*.bugfix.rst`, `CHANGES/*.bugfix[.rst]`) is a fix. Nobody names the change that
     caused it, so B-SZZ proposes one: the commit most of the fix's changed source lines blame to (or the lines
     next to inserted code, when a fix only adds code). SZZ finds the right commit only about half the time on
     Django's labeled regressions, so these proposals count only once the tests confirm them (step 4).
2. **The change the reviewer saw.** Each named commit is mapped to its merged GitHub pull request, and the PR's
   landed commits are recovered (merge, squash and rebase merges alike). PRs over 500 changed lines fall back to the
   single named commit.
3. **Where the bug is.** The fix's changed lines are blamed at the fix's parent and carried back to their position
   at the review head. A bug is `in_diff` when the fix changed lines the PR wrote, and `outside_diff` when it only
   changed older code the PR's change broke.
4. **Test verification.** The fix's own tests run at four commits: passing at the fix, failing just before it (the
   bug's failure signature), failing the same way (same exception type) right after the reviewed change, and
   passing (or unable to run) before it. Only bugs that survive this are in the benchmark. Django runs on SQLite
   with the Python of its day (2.7 to 3.12); pytest and aiohttp are installed from the checkout with the dependency
   versions available on the commit's date (aiohttp without its C extensions). When the fix's test patch no longer
   applies to an older commit, the definitions it changed are grafted into that commit's test modules, and failing
   that the fix's test files are copied whole.
5. **Clean cases.** Merged PRs that no known fix points at, whose added source lines were at least 90% untouched a
   year later (Django: ticketed PRs from 2022-03 to 2025-06).

## Fields

- `case_id`, `repo`, `kind` (`buggy` or `clean`), `split` (`dev`, `ablation`, `test`)
- `review`: what a reviewer may see: `title`, `description`, `diff`, `changed_files`, `changed_lines`,
  `base_commit`, `head_commit`, `commits`, `pr_number`, `pr_url`, `review_unit` (`pr` or `commit`)
- `bugs[]`: hidden ground truth. Never show these to the system under test:
  `bug_summary` (usually the project's own release note), `locality`, `bug_files`, `bug_lines` (path, line at the
  review head, `origin` deleted/anchor, `in_diff`), `fix_commit`, `fix_date`, `fix_tests` (Django runtests.py
  labels or pytest node ids), `introducing_commit`, `label_source` (`developer` or `szz`), `tickets`, `verdict`

## Statistics

| | Django | pytest | aiohttp |
|---|---:|---:|---:|
| Bug fixes found | 633 named regressions | 282 changelog fixes | 345 changelog fixes |
| Cause named by | committers | SZZ (for 175) | SZZ (for 180) |
| Bugs traced to a reviewable change | 395 | 100 | 111 |
| Test-verified bugs | 228 | 31 | 31 |
| Refuted: the bug predates the labeled change | 34 | 32 | 30 |
| Buggy cases (dev / ablation / test) | 30 / 60 / 112 | 0 / 0 / 28 | 0 / 0 / 29 |
| Clean cases | 40 | 10 | 10 |
| Bugs `in_diff` / `outside_diff` | 176 / 52 | 31 / 0 | 31 / 0 |
| Median changed lines, buggy / clean | 75 / 50 | 110.5 / 61.5 | 133 / 67 |
| Bugs fixed on or after 2026-02-01 | 11 | 6 | 7 |

- **Django verification funnel:** 633 labeled fixes → 395 bugs traced to a reviewable change → 228 test-verified
  (149 `verified`, 31 `verified_new`, 48 `verified_differs`). Of the rest: 34 `predates` (the bug is older than
  the labeled change), 24 `head_unclear`, 33 `not_fail_to_pass`, 67 `unverifiable` (the tests need PostgreSQL,
  MySQL, Oracle, GIS or a browser), 8 that could not run here (Django 1.5 and early 1.6's test runner, two
  Windows-only failures, one crash), 1 `not_reproduced`.
- **pytest and aiohttp:** 282 and 345 bug-fix commits since 2021 → SZZ proposed a cause for 175 and 180 → 100
  and 111 bugs traced to a reviewable change (most of the others sat in PRs over 500 changed lines or had no
  fix test) → 31 and 31 test-verified, 32 and 30 refuted (`predates`). The rest were unclear, mostly because
  the fix's tests could not run against code years older than the fix.
- **Django bugs by fix year:** 2013-2016: 10, 2017: 19, 2018: 19, 2019: 16, 2020: 24, 2021: 23, 2022: 24, 2023: 30,
  2024: 16, 2025: 35, 2026: 12. Seven were fixed on or after 2026-07-01, eleven on or after 2026-02-01.
- **Summaries:** 135 of Django's 228 come from its release notes, the rest from the fix's commit subject; every
  pytest and aiohttp summary is the fix's changelog fragment.

## Known limitations

- **Three Python projects.** Results may not transfer to other codebases or languages.
- **SZZ labels.** For pytest and aiohttp the introducing change is SZZ's proposal, kept only when the fix's tests
  pass before it and fail the bug's way after it. That can still be a change that exposed an older latent bug.
  SZZ names the change that wrote the lines the fix touched, so these bugs are all `in_diff` by construction.
- **Environments of one machine.** Tests ran on Windows. Django bugs whose tests need PostgreSQL, MySQL, Oracle,
  GIS or a browser were not verifiable, and neither were aiohttp bugs in its C extensions only.
- **Verification is per exception type.** "Fails the same way" compares exception types, not messages.
- **Clean does not mean bug-free.** A finding on a clean case may be a real, undiscovered bug. Check confident false
  alarms by hand.
- **Clean cases are smaller** than buggy ones (see the medians above), which can flatter false-alarm rates.
- **Contamination.** Most fixes predate current models' training cutoffs; only a small slice was fixed after them.
  Report that slice separately.
- **PR text can change.** Titles and descriptions come from GitHub today, not from the day of review.

## Licenses

Code and commit messages come from Django (BSD-3-Clause), pytest (MIT) and aiohttp (Apache-2.0, with MIT parts).
Pull-request titles and descriptions are public GitHub content written by their authors.
