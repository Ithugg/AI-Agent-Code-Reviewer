import sys
from pathlib import Path

from catchrate.verify.testrun import parse_output, run_django_tests
from catchrate.verify.verify import state_status

MODERN = r"""Found 4 test(s).
Creating test database for alias 'default'...
System check identified no issues (0 silenced).
test_in_iterator_rhs (lookup.tests.LookupTests.test_in_iterator_rhs) ... ok
test_range_iterator_rhs (lookup.tests.LookupTests.test_range_iterator_rhs) ... FAIL
test_skipped (lookup.tests.LookupTests.test_skipped) ... skipped "Database doesn't support feature(s): x"
test_documented (lookup.tests.LookupTests.test_documented)
A docstring line ... ERROR

======================================================================
ERROR: test_documented (lookup.tests.LookupTests.test_documented)
A docstring line
----------------------------------------------------------------------
Traceback (most recent call last):
  File "C:\x\tests\lookup\tests.py", line 10, in test_documented
    list(Author.objects.filter(pk__range=iter([1, 2])))
TypeError: 'list_iterator' object at 0x0000021F is not subscriptable

======================================================================
FAIL: test_range_iterator_rhs (lookup.tests.LookupTests.test_range_iterator_rhs)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "C:\x\tests\lookup\tests.py", line 20, in test_range_iterator_rhs
    self.assertEqual(len(qs), 2)
AssertionError: 0 != 2

----------------------------------------------------------------------
Ran 4 tests in 0.020s

FAILED (failures=1, errors=1, skipped=1)
"""

LEGACY = """test_x (queries.tests.Queries1Tests) ... ok
queries.test_new (unittest.loader._FailedTest) ... ERROR
setUpClass (admin_views.tests.AdminViewTests) ... ERROR

======================================================================
ERROR: queries.test_new (unittest.loader._FailedTest)
----------------------------------------------------------------------
ImportError: Failed to import test module: queries.test_new
Traceback (most recent call last):
ImportError: cannot import name 'NewThing' from 'django.db.models'

======================================================================
ERROR: setUpClass (admin_views.tests.AdminViewTests)
----------------------------------------------------------------------
django.core.exceptions.FieldError: Unknown field(s) (new_field) specified for Book
"""


def test_parses_modern_output_with_docstrings_and_failures():
    run = parse_output(MODERN, 1, 2.5)

    assert run.outcomes == {
        "lookup.tests.LookupTests.test_in_iterator_rhs": "ok",
        "lookup.tests.LookupTests.test_range_iterator_rhs": "fail",
        "lookup.tests.LookupTests.test_skipped": "skip",
        "lookup.tests.LookupTests.test_documented": "error",
    }
    assert run.signatures == {
        "lookup.tests.LookupTests.test_documented": "TypeError: 'list_iterator' object at 0x? is not subscriptable",
        "lookup.tests.LookupTests.test_range_iterator_rhs": "AssertionError: 0 != 2",
    }
    assert run.load_errors == ()
    assert (run.returncode, run.seconds) == (1, 2.5)


def test_parses_legacy_names_import_failures_and_class_setup_errors():
    run = parse_output(LEGACY, 1, 1.0)

    assert run.outcomes == {"queries.tests.Queries1Tests.test_x": "ok"}
    assert run.load_errors == ("queries.test_new", "admin_views.tests.AdminViewTests")


PY2_IMPORT_FAILURE = """test_ok (basic.tests.ModelTests) ... ok
test_models (unittest.loader.ModuleImportFailure) ... ERROR

======================================================================
ERROR: test_models (unittest.loader.ModuleImportFailure)
----------------------------------------------------------------------
ImportError: Failed to import test module: test_models
"""

FAKE_RUNTESTS = r"""import sys
sys.stderr.write("argv=" + " ".join(sys.argv[1:]) + "\n")
sys.stderr.write('Traceback (most recent call last):\n  File "runtests.py", line 9, in <module>\n')
sys.stderr.write('  File "C:/py27/lib/unittest/loader.py", line 91, in loadTestsFromName\n')
sys.stderr.write("    parent, obj = obj, getattr(obj, part)\n")
sys.stderr.write("AttributeError: type object 'T' has no attribute 'test_new'\n")
sys.exit(1)
"""


def test_python2_module_import_failures_are_load_errors():
    run = parse_output(PY2_IMPORT_FAILURE, 1, 1.0)
    assert run.outcomes == {"basic.tests.ModelTests.test_ok": "ok"}
    assert run.load_errors == ("test_models",)


def test_old_runtests_gets_no_parallel_flag_and_a_loader_crash_blocks_every_label(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "runtests.py").write_text(FAKE_RUNTESTS, encoding="utf-8")

    run = run_django_tests(Path(sys.executable), tmp_path, ["app.tests.T.test_new", "app.tests.T.test_old"])

    assert "--parallel" not in run.tail and "argv=--settings=test_sqlite --noinput" in run.tail
    assert run.outcomes == {}
    assert run.load_errors == ("app.tests.T.test_new", "app.tests.T.test_old")
    assert state_status(run, ["app.tests.T.test_new"])[0] == "load_error"
