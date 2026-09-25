import sys
import types

import pytest

from catchrate.verify.graft import graft

CURRENT = '''"""Tests for calc."""
import pytest

from pkg.calc import inc


def test_inc():
    assert inc(1) == 2


class TestCalc:
    def test_zero(self):
        assert inc(0) == 1

    def test_one(self):
        assert inc(1) == 2
'''

FIX = '''"""Tests for calc."""
import pytest

from pkg.calc import inc
from pkg.later import something_new
from pkg.calc import MAX


def test_inc():
    assert inc(1) == 2


def test_unrelated_later_change():
    assert something_new()


class TestCalc:
    def test_zero(self):
        assert inc(0) == 1, "zero"

    def test_one(self):
        assert inc(1) == 2

    @pytest.mark.parametrize("x", [MAX])
    def test_max(self, x):
        assert inc(x) == x + 1


@pytest.fixture
def big():
    return MAX


def test_big(big):
    assert inc(big) > big
'''


def _lines_of(text: str, *snippets: str) -> set[int]:
    numbered = list(enumerate(text.splitlines(), start=1))
    return {n for snippet in snippets for n, line in numbered if snippet in line}


def test_only_the_definitions_the_fix_changed_come_over():
    changed = _lines_of(FIX, "from pkg.calc import MAX", '"zero"', "parametrize", "def test_max", "x + 1",
                        "@pytest.fixture", "def big", "return MAX", "def test_big", "> big")  # fmt: skip

    out = graft(CURRENT, FIX, changed)

    assert "something_new" not in out  # an unrelated later change the fix didn't make
    assert "from pkg.calc import MAX\n" in out and out.index("import MAX") < out.index("def test_inc")
    assert 'assert inc(0) == 1, "zero"' in out
    assert out.index("def test_one") < out.index('@pytest.mark.parametrize("x", [MAX])') < out.index("@pytest.fixture")
    assert out.rstrip().endswith("assert inc(big) > big")
    compile(out, "test_calc.py", "exec")


def test_what_the_grafted_tests_need_comes_along():
    current = "from .models import Author\n\n\ndef test_old():\n    assert Author\n"
    fix = (
        "from .models import Author, Product, NewThing\n\n\n"
        "def make_product():\n    return Product()\n\n\n"
        "def test_old():\n    assert Author\n\n\n"
        "def test_new():\n    assert make_product() and Author\n"
    )

    out = graft(current, fix, _lines_of(fix, "def test_new", "make_product() and"))

    assert out.startswith("from .models import Author\nfrom .models import Product\n")  # only the missing name
    assert "NewThing" not in out
    assert out.index("def test_old") < out.index("def make_product") < out.index("def test_new")
    compile(out, "tests.py", "exec")


@pytest.fixture
def models(monkeypatch):
    module = types.ModuleType("models")
    module.Model = object
    module.CharField = module.ManyToManyField = module.UUIDField = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "models", module)
    return module


def test_members_grafted_into_a_class_get_what_they_use_defined_first(models):
    current = "import models\n\n\nclass Article(models.Model):\n    title = models.CharField()\n"
    fix = (
        "import uuid\n\nimport models\n\n\nclass Topic(models.Model):\n    pass\n\n\n"
        "class Article(models.Model):\n    title = models.CharField()\n    topics = models.ManyToManyField(Topic)\n"
        "    uuid = models.UUIDField(default=uuid.uuid4)\n"
    )

    out = graft(current, fix, _lines_of(fix, "topics =", "uuid = models"))

    assert out.index("import uuid") < out.index("class Topic") < out.index("class Article") < out.index("topics =")
    exec(compile(out, "models.py", "exec"), {})  # class bodies run at import: nothing may come too late


def test_a_definition_that_comes_too_late_for_a_grafted_class_body_moves_up(models):
    current = (
        "import models\n\n\nclass Article(models.Model):\n    title = models.CharField()\n\n\n"
        "class Topic(models.Model):\n    name = models.CharField()\n"
    )
    fix = (
        "import models\n\n\nclass Topic(models.Model):\n    name = models.CharField()\n\n\n"
        "class Article(models.Model):\n    title = models.CharField()\n    topics = models.ManyToManyField(Topic)\n"
    )

    out = graft(current, fix, _lines_of(fix, "topics ="))

    assert out.count("class Topic") == 1 and out.index("class Topic") < out.index("class Article")
    exec(compile(out, "models.py", "exec"), {})


def test_an_unchanged_module_stays_as_it_was():
    assert graft(CURRENT, FIX, set()) == CURRENT


def test_unparseable_versions_do_not_graft():
    assert graft("def broken(:\n", FIX, {1}) is None
