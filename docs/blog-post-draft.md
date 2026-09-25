# How many real bugs can an AI code reviewer catch? (draft)

> Draft. The dataset and SZZ sections use measured numbers. Every agent result is a placeholder marked TODO until
> the runs happen; do not publish before replacing them.

## The question

Every bug fix in a mature project points back to a pull request that introduced the bug and got approved anyway.
If those pull requests can be recovered, each one becomes a test case with a known answer: here is a change a
careful human reviewer accepted; can an AI reviewer spot what they missed, and at what cost?

## Labels you can trust

The textbook way to trace a bug to its origin is SZZ: blame the lines the fix changed and assume whoever wrote them
introduced the bug. I didn't want to build a benchmark on that assumption without measuring it first.

Django makes that possible. When its committers fix a regression they write "Regression in <sha>." in the commit
message, naming the commit that caused it. That gave me 633 fixes where the answer is written down by the people
who know the code best, so I could score SZZ against them:

| SZZ variant | Finds the named commit | Candidates that are right |
|---|---|---|
| Plain B-SZZ | 54.0% | 43.7% |
| Ignoring Django's bulk-reformat commits | 54.8% | 44.9% |
| Also blaming lines next to inserted code | 60.5% | 34.4% |

A benchmark mined with SZZ alone would have mislabeled about half its cases. 144 of the 633 fixes (23%) only add
code, a missing check, and plain SZZ has nothing to blame for those at all.

## Bugs outside the diff

Once each bug was located at the moment of review, a second surprise: in 115 of the 395 bugs I could trace (52 of
the 228 that survived verification), the fix never touched a line the pull request wrote. The PR changed one thing;
the fix had to change older code that the change broke. `django-pr16703` ("Made SELECT respect the order specified
by values()/values_list()") caused six regressions in Django 5.2, fixed over 17 months; four of them were fixed in
code the PR never touched.

These are exactly the bugs a reviewer can only catch by reasoning past the diff, which is why the benchmark tags
them (`in_diff` vs `outside_diff`) instead of dropping them, and why the agent gets tools to explore the codebase.

## Verification, and the bug in my verifier

Each label is checked by running the fix's own tests on SQLite. My first design ran them at three points: before
the PR, after the PR, and at the fix. It was wrong in an instructive way: "fails after the PR" doesn't prove the
bug is what failed. When a test needs something added later (a test helper, a model field), it fails before and
after the PR alike, and my verifier called those bugs older than the PR.

The fix was a fourth run just before the fix commit, where the test fails *because of the bug*. That failure is
the bug's signature, and the PR only counts as guilty if it fails the same way.

Of 395 traced bugs, 228 (58%) survived all four runs, with each commit tested on the Python of its day, from 2.7
for 2014's Django to 3.12 for today's. 34 labels turned out to point at the wrong change: the test fails the bug's
way before the pull request too. Most of the rest couldn't be judged on one Windows laptop (67 tests need
PostgreSQL, MySQL, GIS or a browser), which is a limitation of the verifier, not evidence against the labels. The
benchmark keeps only the verified bugs, plus 40 clean pull requests whose added lines survived a year untouched,
for measuring false alarms.

## Beyond Django

No other project I checked names the commit behind a regression, so for pytest and aiohttp I went the other way
round: every commit that adds a bug-fix changelog fragment is a fix, SZZ proposes the change that caused it, and
the four-run test check decides. The check refuted 32 of pytest's 100 proposals and 30 of aiohttp's 111, and
confirmed 31 of each.

Running a 2021 commit's tests in 2026 took more than checking it out:

- **Today's dependencies break old code.** pytest 7.4 crashed with internal errors under current plugins. Each
  commit now gets the dependency versions released before it (`uv pip install --exclude-newer <commit date>`).
- **So do the build tools of the day.** 2021's setuptools can't do editable installs, so the checkout is installed
  normally and rebuilt for every commit (uv would otherwise reuse a wheel built from a different commit).
- **Some packages no longer exist as they were.** `atomicwrites` was deleted from PyPI in 2022 and re-uploaded
  later, so no version predates the cutoff; such packages are exempted from it one by one.
- **A fix's test file doesn't fit older code.** Copying the whole file brings years of unrelated test changes that
  fail to import. Grafting only the tests, fixtures and imports the fix changed onto the older file turned several
  "unclear" results into verified bugs.

## The agent

TODO after runs: v0 (diff only, one call), v1 (reads files, searches, finds callers), v2 (runs existing tests),
v3 (writes a failing test as evidence), v4 (tuned). Report catch rate, verified catch rate, false alarms per clean
PR, precision, dollars and seconds per review, each over three runs, split by in-diff vs outside-diff bugs and by
whether the fix came after the model's training cutoff.

## What didn't work

- The first verification design (above).
- A full `git clone` of Django kept dying on a flaky connection; cloning shallow and deepening in retryable chunks
  finished in five minutes.
- Dating dependencies with editable installs: it made pytest's verified count drop from 30 to 18 before I noticed
  2021's setuptools couldn't build them.
- TODO: surprises from the agent runs.
