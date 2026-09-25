from collections import Counter

import pytest

from catchrate.szz.evaluate import FixOutcome, score


def test_commit_level_scores():
    outcomes = [
        FixOutcome("f1", frozenset({"a"}), Counter({"a": 3, "b": 1})),  # hit, top-1 right
        FixOutcome("f2", frozenset({"c"}), Counter({"d": 2, "c": 2}), recency={"c": 1.0, "d": 2.0}),  # hit, tie -> newer d
        FixOutcome("f3", frozenset({"e"}), Counter({"x": 1})),  # miss
        FixOutcome("f4", frozenset({"g"}), Counter()),  # no candidates
    ]

    s = score(outcomes)

    assert s.fixes == 4
    assert s.with_candidates == 3
    assert s.mean_candidates == pytest.approx(5 / 3)
    assert s.recall == pytest.approx(2 / 4)
    assert s.precision == pytest.approx(2 / 5)
    assert s.top1 == pytest.approx(1 / 4)


def test_group_level_merges_commits_of_the_same_pr():
    groups = {"a1": "pr:1", "a2": "pr:1", "b": "pr:2"}
    outcomes = [
        # SZZ blamed a sibling commit of the PR that holds the named commit.
        FixOutcome("f1", frozenset({"a1"}), Counter({"a2": 2, "b": 1}), groups),
    ]

    assert score(outcomes).recall == 0
    by_pr = score(outcomes, by_group=True)
    assert by_pr.recall == 1
    assert by_pr.precision == pytest.approx(1 / 2)
    assert by_pr.top1 == 1


def test_empty():
    s = score([])
    assert (s.fixes, s.recall, s.precision, s.top1) == (0, 0.0, 0.0, 0.0)
