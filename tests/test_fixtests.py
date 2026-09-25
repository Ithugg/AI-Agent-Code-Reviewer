import pytest

from catchrate.diffparse import parse_diff
from catchrate.mining.fixtests import extract_fix_tests, module_label, touched_tests

from conftest import TOY_SPEC

SOURCE = '''\
import unittest


class HelperMixin:
    def helper(self):
        return 1


class QueryTests(unittest.TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.x = 1

    def test_one(self):
        self.assertEqual(1, 1)

    @unittest.skip("flaky")
    def test_two(self):
        self.assertEqual(2, 2)

    class Nested(unittest.TestCase):
        def test_inner(self):
            pass


def test_module_level():
    assert True
'''


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        ({15}, ["QueryTests.test_one"]),
        ({17}, ["QueryTests.test_two"]),  # a decorator line belongs to its test
        ({15, 19}, ["QueryTests.test_one", "QueryTests.test_two"]),
        ({12}, ["QueryTests"]),  # setUpTestData changed: run the whole class
        ({23}, ["QueryTests.Nested.test_inner"]),
        ({27}, ["test_module_level"]),
        ({6}, []),  # a mixin without tests is not a test
        ({1}, []),
    ],
)
def test_touched_tests(changed, expected):
    assert touched_tests(SOURCE, changed) == expected


MIXINS = '''\
from django.test import TestCase
from .base import OperationTestBase


class SerializersTestBase:
    def test_round_trip(self):
        pass


class JsonTests(SerializersTestBase, TestCase):
    pass


class OperationTests(OperationTestBase):
    def test_add_field(self):
        pass
'''


def test_mixin_methods_are_labeled_through_their_subclasses():
    assert touched_tests(MIXINS, {7}) == ["JsonTests.test_round_trip"]
    assert touched_tests(MIXINS, {16}) == ["OperationTests.test_add_field"]  # an imported base still runs


def test_mixin_subclasses_in_other_modules_are_found(toy):
    base = "class SerializersTestBase:\n    def test_round_trip(self):\n        assert True\n"
    json_tests = "from django.test import TestCase\nfrom .tests import SerializersTestBase\n\n\nclass JsonSerializerTestCase(SerializersTestBase, TestCase):\n    pass\n"
    toy.commit("Initial", {"tests/serializers/tests.py": base, "tests/serializers/test_json.py": json_tests})
    fix = toy.commit("Fixed #5 -- X.", {"tests/serializers/tests.py": base.replace("assert True", "assert 1 == 1")})
    parent = toy.repo.commits([fix])[fix].parents[0]
    diffs = parse_diff(toy.repo.diff(parent, fix, unified=0))

    assert extract_fix_tests(toy.repo, TOY_SPEC, fix, diffs) == ["serializers.test_json.JsonSerializerTestCase.test_round_trip"]


def test_unparseable_source_is_not_mistaken_for_no_match():
    assert touched_tests("def broken(:\n", {1}) is None


def test_unparseable_test_module_yields_no_labels(toy):
    py2 = "class T:\n    def test_x(self):\n        print 'hi'\n"
    toy.commit("Initial", {"tests/old/tests.py": py2})
    fix = toy.commit("Fixed #4 -- X.", {"tests/old/tests.py": py2.replace("'hi'", "'ho'")})
    parent = toy.repo.commits([fix])[fix].parents[0]
    diffs = parse_diff(toy.repo.diff(parent, fix, unified=0))

    assert extract_fix_tests(toy.repo, TOY_SPEC, fix, diffs) == []


def test_module_label():
    assert module_label("tests/queries/test_q.py", "tests/") == "queries.test_q"
    assert module_label("tests/model_fields/tests.py", "tests/") == "model_fields.tests"


def test_extract_fix_tests_labels_changed_tests(toy):
    toy.commit("Initial", {"tests/app/tests.py": SOURCE, "tests/app/models.py": "x = 1\n", "pkg/a.py": "a = 1\n"})
    changed = SOURCE.replace("self.assertEqual(2, 2)", "self.assertEqual(2, 3)")
    fix = toy.commit(
        "Fixed #1 -- X.",
        {"tests/app/tests.py": changed, "tests/app/models.py": "x = 2\n", "pkg/a.py": "a = 2\n"},
    )
    parent = toy.repo.commits([fix])[fix].parents[0]
    diffs = parse_diff(toy.repo.diff(parent, fix, unified=0))

    assert extract_fix_tests(toy.repo, TOY_SPEC, fix, diffs) == ["app.tests.QueryTests.test_two"]


def test_removing_an_expected_failure_decorator_counts(toy):
    marked = SOURCE.replace("    def test_one(self):", "    @unittest.expectedFailure\n    def test_one(self):")
    toy.commit("Initial", {"tests/app/tests.py": marked})
    fix = toy.commit("Fixed #2 -- X.", {"tests/app/tests.py": SOURCE})
    parent = toy.repo.commits([fix])[fix].parents[0]
    diffs = parse_diff(toy.repo.diff(parent, fix, unified=0))

    assert extract_fix_tests(toy.repo, TOY_SPEC, fix, diffs) == ["app.tests.QueryTests.test_one"]


def test_module_level_changes_label_the_whole_module(toy):
    toy.commit("Initial", {"tests/data/test_cases.py": "CASES = [1, 2]\n\n\ndef make_tests():\n    pass\n"})
    fix = toy.commit("Fixed #3 -- X.", {"tests/data/test_cases.py": "CASES = [1, 2, 3]\n\n\ndef make_tests():\n    pass\n"})
    parent = toy.repo.commits([fix])[fix].parents[0]
    diffs = parse_diff(toy.repo.diff(parent, fix, unified=0))

    assert extract_fix_tests(toy.repo, TOY_SPEC, fix, diffs) == ["data.test_cases"]
