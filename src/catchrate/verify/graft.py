"""Bring a fix's test changes onto an older test module one definition at a time.

When the fix's test patch no longer applies, copying the fix's whole test file
drags in years of unrelated test changes that often fail to import against old
code. Grafting takes the older module and swaps in only the definitions the fix
changed (top-level tests, fixtures, helpers and models, and members of classes
both versions share), plus what they need that the older module lacks: the names
they import and the module-level definitions they use, placed before their first
use. The fix's tests then run as written, next to the rest of the module as it was.
"""

from __future__ import annotations

import ast
import builtins

_Edit = tuple[int, int, int, list[str]]  # (start, end, order, lines): current lines [start, end) become lines
_BUILTINS = frozenset(dir(builtins))
_END = 1 << 30  # anchor for what goes at the end of the module


def graft(current: str, fix: str, changed: set[int]) -> str | None:
    """`current` with every definition overlapping the fix's `changed` lines taken from `fix`.

    `changed` holds line numbers in `fix`. None when either version does not parse.
    """
    try:
        current_tree, fix_tree = ast.parse(current), ast.parse(fix)
    except SyntaxError:
        return None
    lines, fix_lines = _lines(current), _lines(fix)

    def touched(node: ast.stmt) -> bool:
        start, end = _span(node)
        return any(start <= n <= end for n in changed)

    def source(node: ast.stmt) -> list[str]:
        start, end = _span(node)
        return fix_lines[start - 1 : end]

    edits: list[_Edit] = []
    grafted: list[tuple[ast.stmt, int]] = []  # (fix node, where what it needs must already exist)
    replaced: dict[str, int] = {}  # top-level name -> its replacing edit
    top = {name: node for node in current_tree.body if (name := _name(node))}
    for node in fix_tree.body:
        name = _name(node)
        if name is None or not touched(node):
            continue  # imports come over below, only as far as the grafted code needs them
        old = top.get(name)
        if old is None:
            grafted.append((node, _END))
            continue
        before_old = _span(old)[0] - 1
        if isinstance(node, ast.ClassDef) and isinstance(old, ast.ClassDef):
            members = {n: m for m in old.body if (n := _name(m))}
            for member in node.body:
                member_name = _name(member)
                if member_name is None or not touched(member):
                    continue
                if member_name in members:
                    start, end = _span(members[member_name])
                    edits.append((start - 1, end, len(edits), source(member)))
                else:  # a new member goes at the end of the class
                    end = old.end_lineno or old.lineno
                    edits.append((end, end, len(edits), ["\n", *source(member)]))
                grafted.append((member, before_old))
        else:
            start, end = _span(old)
            replaced[name] = len(edits)
            edits.append((start - 1, end, len(edits), source(node)))
            grafted.append((node, before_old))

    placed, moved, imports = _dependencies(current_tree, fix_tree, grafted)
    for name in moved:  # the fix's definition now comes earlier, so the later one goes
        start, end = _span(top[name])
        if name in replaced:
            edits[replaced[name]] = (start - 1, end, edits[replaced[name]][2], [])
        else:
            edits.append((start - 1, end, len(edits), []))
    if imports:
        at = _import_insertion_line(current_tree)
        edits.append((at, at, -2, imports))
    by_anchor: dict[int, list[ast.stmt]] = {}
    for node, anchor in placed.items():
        by_anchor.setdefault(anchor, []).append(node)
    appended: list[str] = []
    for anchor, nodes in by_anchor.items():
        block = [line for node in sorted(nodes, key=lambda n: n.lineno) for line in ["\n", "\n", *source(node)]]
        if anchor == _END:
            appended = block
        else:  # before the definition that first needs them, after any edit to that definition itself
            edits.append((anchor, anchor, -1, [*block[2:], "\n", "\n"]))
    for start, end, _, replacement in sorted(edits, key=lambda e: (e[0], e[2]), reverse=True):
        lines[start:end] = replacement
    return "".join(lines + appended)


def _dependencies(
    current: ast.Module, fix: ast.Module, grafted: list[tuple[ast.stmt, int]]
) -> tuple[dict[ast.stmt, int], set[str], list[str]]:
    """What the grafted code needs that `current` lacks: the fix's module-level definitions to bring along,
    each with the line it must precede; the names `current` defines too late for a class body that reads
    them at import (their fix definitions move up and the later ones go); and import lines for the names
    taken from the fix's imports."""
    definitions = {name: node for node in fix.body if (name := _name(node))}
    imported = _imported(fix.body)
    have = _bound(current.body)
    starts = {name: _span(node)[0] - 1 for node in current.body if (name := _name(node))}
    # New top-level definitions the fix added go at the end unless something needs them earlier.
    placed: dict[ast.stmt, int] = {node: anchor for node, anchor in grafted if node in fix.body and anchor == _END}
    moved: set[str] = set()
    wanted_imports: dict[str, ast.stmt] = {}
    pending = list(grafted)

    def bring(name: str, anchor: int) -> None:
        dependency = definitions[name]
        if placed.get(dependency, _END + 1) > anchor:  # new, or needed earlier than where it sits
            placed[dependency] = anchor
            pending.append((dependency, anchor))

    while pending:
        node, anchor = pending.pop()
        for name in _free_names(node) - have - _BUILTINS:
            if name in imported:
                wanted_imports[name] = imported[name]
            elif name in definitions:
                bring(name, anchor)
        for name in _import_time_names(node) & have:
            if anchor < starts.get(name, -1) and name in definitions:  # defined here, but too late
                moved.add(name)
                bring(name, anchor)
    return placed, moved, _import_lines(wanted_imports)


def _import_time_names(node: ast.stmt) -> set[str]:
    """Names `node` reads while its module is imported (class bodies, bases, decorators, defaults), which must
    be defined above it; names read inside function bodies only need to exist by the time they run."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        parts: list[ast.AST] = [*node.decorator_list, *node.args.defaults, *filter(None, node.args.kw_defaults)]
    elif isinstance(node, ast.ClassDef):
        parts = [*node.decorator_list, *node.bases, *(k.value for k in node.keywords)]
        for stmt in node.body:
            parts += _import_time_parts(stmt)
    else:
        parts = [node]
    return {n.id for part in parts for n in ast.walk(part) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def _import_time_parts(stmt: ast.stmt) -> list[ast.AST]:
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return [*stmt.decorator_list, *stmt.args.defaults, *filter(None, stmt.args.kw_defaults)]
    return [stmt]


def _import_lines(wanted: dict[str, ast.stmt]) -> list[str]:
    """One import line per statement, naming only the wanted names."""
    by_statement: dict[int, tuple[ast.stmt, list[ast.alias]]] = {}
    for name, statement in wanted.items():
        alias = next(a for a in statement.names if _alias_name(a) == name)  # type: ignore[attr-defined]
        by_statement.setdefault(id(statement), (statement, []))[1].append(alias)
    lines = []
    for statement, aliases in by_statement.values():
        if isinstance(statement, ast.ImportFrom):
            node: ast.stmt = ast.ImportFrom(module=statement.module, names=aliases, level=statement.level)
        else:
            node = ast.Import(names=aliases)
        lines.append(ast.unparse(node) + "\n")
    return sorted(lines)


def _free_names(node: ast.stmt) -> set[str]:
    """Names `node` reads from its module. Only function scopes count as local: a class body or a
    module-level assignment like `uuid = UUIDField(default=uuid.uuid4)` reads the module's `uuid`."""
    loaded, local = set(), set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            loaded.add(child.id)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            local |= {arg.arg for arg in ast.walk(child.args) if isinstance(arg, ast.arg)}
            body = child.body if isinstance(child.body, list) else [child.body]
            local |= {n.id for stmt in body for n in ast.walk(stmt) if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load)}
            local |= {n.name for stmt in body for n in ast.walk(stmt) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        local.add(node.name)
    return loaded - local


def _bound(body: list[ast.stmt]) -> set[str]:
    """Names a module binds at top level, including inside top-level if/try blocks."""
    names: set[str] = set()
    for node in body:
        if name := _name(node):
            names.add(name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= {_alias_name(a) for a in node.names}
        elif isinstance(node, ast.Assign):
            names |= {n.id for t in node.targets for n in ast.walk(t) if isinstance(n, ast.Name)}
        elif isinstance(node, (ast.If, ast.Try)):
            names |= _bound([*node.body, *node.orelse, *getattr(node, "finalbody", [])])
            for handler in getattr(node, "handlers", []):
                names |= _bound(handler.body)
    return names


def _imported(body: list[ast.stmt]) -> dict[str, ast.stmt]:
    """Name -> the top-level import statement that binds it (star imports bind nothing we can name)."""
    return {
        _alias_name(alias): node
        for node in body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
        if alias.name != "*"
    }


def _alias_name(alias: ast.alias) -> str:
    return alias.asname or alias.name.split(".")[0]


def _span(node: ast.stmt) -> tuple[int, int]:
    decorators = getattr(node, "decorator_list", [])
    return min([node.lineno, *(d.lineno for d in decorators)]), node.end_lineno or node.lineno


def _lines(text: str) -> list[str]:
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    return lines


def _name(node: ast.stmt) -> str | None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        return node.targets[0].id
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    return None


def _import_insertion_line(tree: ast.Module) -> int:
    """0-based line after the module's last top-level import (or its docstring), where new imports go."""
    at = 0
    for i, node in enumerate(tree.body):
        is_docstring = i == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        if isinstance(node, (ast.Import, ast.ImportFrom)) or is_docstring:
            at = node.end_lineno or node.lineno
    return at
