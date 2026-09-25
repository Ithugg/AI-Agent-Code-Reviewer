"""Find the tests a fix commit added or changed: Django runtests.py labels or pytest node ids."""

from __future__ import annotations

import ast
import re
import warnings
from dataclasses import dataclass, field

from catchrate.diffparse import FileDiff
from catchrate.gitrepo import GitRepo
from catchrate.repos import RepoSpec


def module_label(path: str, test_prefix: str) -> str:
    """tests/queries/test_q.py -> queries.test_q"""
    return path[len(test_prefix) : -len(".py")].replace("/", ".")


def is_test_module(path: str) -> bool:
    return path.rsplit("/", 1)[-1].startswith("test")


@dataclass
class Touched:
    names: list[str] = field(default_factory=list)  # runnable here: "Class.method", "Class" or "function"
    # (mixin class, method or "") changed in a mixin whose tests only run through subclasses
    mixins: list[tuple[str, str]] = field(default_factory=list)


def _base_names(node: ast.ClassDef) -> list[str]:
    return [b.id if isinstance(b, ast.Name) else b.attr for b in node.bases if isinstance(b, (ast.Name, ast.Attribute))]


def _is_mixin(name: str, classes: dict[str, ast.ClassDef], seen: frozenset[str] = frozenset()) -> bool:
    """A class whose bases are only object, *Mixin names, or other local mixins; it can't run as a test itself.

    Classes with any other base (TestCase, or an imported base like OperationTestBase) count as runnable.
    """
    if name in seen:
        return False
    return all(
        base == "object" or base.endswith("Mixin") or (base in classes and _is_mixin(base, classes, seen | {name}))
        for base in _base_names(classes[name])
    )


def _local_subclasses(mixin: str, classes: dict[str, ast.ClassDef], mixins: set[str]) -> list[str]:
    def inherits(name: str, seen: frozenset[str] = frozenset()) -> bool:
        bases = _base_names(classes[name]) if name in classes else []
        return mixin in bases or any(b in classes and b not in seen and inherits(b, seen | {name}) for b in bases)

    return [name for name in classes if name not in mixins and inherits(name)]


def analyze(source: str, changed: set[int], style: str = "django") -> Touched | None:
    """Tests whose source spans a changed line; None if the source doesn't parse.

    With style "pytest", classes named Test* are runnable whatever their bases,
    and any other class holding tests is a mixin.

    A test class with changes outside any test method (setUp, a helper, a class
    attribute) comes back as "Class". Classes without test methods are ignored.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # old code trips SyntaxWarnings on newer Pythons
        try:
            tree = ast.parse(source)
        except SyntaxError:  # e.g. Python 2 era test modules
            return None

    def span(node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> range:
        start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
        return range(start, (node.end_lineno or node.lineno) + 1)

    def is_test(node: ast.AST) -> bool:
        return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test")

    def in_class(node: ast.ClassDef, prefix: str) -> list[str]:
        found: list[str] = []
        for child in node.body:
            if isinstance(child, ast.ClassDef):
                found += in_class(child, f"{prefix}{child.name}.")
            elif is_test(child) and changed.intersection(span(child)):  # type: ignore[arg-type]
                found.append(f"{prefix}{child.name}")  # type: ignore[attr-defined]
        if not found and any(is_test(n) for n in ast.walk(node)) and changed.intersection(span(node)):
            found.append(prefix.rstrip("."))
        return found

    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    if style == "pytest":
        mixins = {name for name in classes if not name.startswith("Test")}
    else:
        mixins = {name for name in classes if _is_mixin(name, classes)}
    touched = Touched()
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            found = in_class(node, f"{node.name}.")
            if node.name not in mixins:
                touched.names += found
                continue
            subclasses = _local_subclasses(node.name, classes, mixins)
            if style == "pytest":
                subclasses = [sub for sub in subclasses if sub.startswith("Test")]
            for name in found:
                rest = name.split(".", 1)[1] if "." in name else ""
                touched.mixins.append((node.name, rest))
                touched.names += [f"{sub}.{rest}" if rest else sub for sub in subclasses]
        elif is_test(node) and changed.intersection(span(node)):  # type: ignore[arg-type]
            touched.names.append(node.name)  # type: ignore[attr-defined]
    touched.names = list(dict.fromkeys(touched.names))
    return touched


def touched_tests(source: str, changed: set[int]) -> list[str] | None:
    """Runnable names of tests whose source spans a changed line; None if the source doesn't parse."""
    result = analyze(source, changed)
    return None if result is None else result.names


def _subclasses_elsewhere(repo: GitRepo, spec: RepoSpec, commit: str, mixin: str, directory: str) -> list[tuple[str, str]]:
    """(test module path, class) for classes in `directory` that list `mixin` among their bases."""
    out = repo.grep(commit, ["-n", "-E", "-e", rf"^class \w+\([^)]*\b{re.escape(mixin)}\b"], [directory])
    found = []
    for line in out.splitlines():
        _, path, _, text = line.split(":", 3)
        name = re.match(r"class (\w+)", text)
        if name and spec.is_test(path) and is_test_module(path):
            found.append((path, name[1]))
    return found


def extract_fix_tests(repo: GitRepo, spec: RepoSpec, fix_sha: str, diffs: list[FileDiff]) -> list[str]:
    """Labels for tests the fix added or changed: "queries.tests.QuerySetTests.test_x" for Django,
    "testing/test_x.py::TestC::test_m" for pytest.

    A pure deletion counts for the lines on either side of it, which catches a
    fix that only removed an @expectedFailure or @skip decorator. A test module
    whose changes sit outside any test (module-level data for generated tests)
    is labeled as a whole module. Test methods on mixins are labeled through the
    concrete test classes that run them, in any module of the same test package.
    """
    pytest_style = spec.test_style == "pytest"

    def label(path: str, name: str = "") -> str:
        if pytest_style:
            return f"{path}::{name.replace('.', '::')}" if name else path
        module = module_label(path, spec.test_prefix)
        return f"{module}.{name}" if name else module

    labels: list[str] = []
    for fd in diffs:
        path = fd.new_path
        if path is None or fd.binary or not spec.is_test(path) or not is_test_module(path):
            continue
        changed = set(fd.added_lines()) | {n for p in fd.deletion_points() for n in (p, p + 1) if n >= 1}
        source = repo.show_file(fix_sha, path) if changed else None
        if source is None:
            continue
        result = analyze(source, changed, spec.test_style)
        if result is None:
            continue  # can't tell which tests changed, so claim none
        found = [label(path, name) for name in result.names]
        directory = path.rsplit("/", 1)[0] + "/"
        for mixin, rest in result.mixins:
            for other_path, cls in _subclasses_elsewhere(repo, spec, fix_sha, mixin, directory):
                if other_path != path and (not pytest_style or cls.startswith("Test")):
                    found.append(label(other_path, f"{cls}.{rest}" if rest else cls))
        labels += found if found or result.mixins else [label(path)]
    return list(dict.fromkeys(labels))
