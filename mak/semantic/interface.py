"""What a node's interface *is*, binding by binding (Wave 20).

``api_fingerprint`` answers "is this source's interface text identical?" — a
fine identity for a node, and too blunt a question for enforcement. Adding a
new helper beside a function, a new import to a header, or a new method to a
class changes the text and breaks nobody: nothing that already exists could
depend on a name that did not. What breaks callers is an **existing** binding
removed or re-bound — a parameter list, a return annotation, a base class, a
field set, an import pointing somewhere else.

So this module maps each name a source binds to the interface it binds it to,
qualified (``C`` for a class's header and fields, ``C.m`` for its members),
and :func:`changed_bindings` reports only names whose binding changed or
disappeared. It is what "the interface changed" means for a body-only promise,
for escalating an ``#api`` lock, and for deciding whether a stale read touched
anything the task uses.
"""

from __future__ import annotations

import ast

from mak.node_store.api_digest import api_fingerprint

_FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)


def interface_map(source: str) -> dict[str, str] | None:
    """``{qualified name: rendered interface}``; None when it does not parse."""
    tree = _parse_lenient(source)
    if tree is None:
        return None
    rendered: dict[str, str] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.ClassDef):
            _bind_class(stmt, rendered)
        else:
            _bind(stmt, rendered, prefix="")
    return rendered


def changed_bindings(old: str, new: str | None) -> set[str] | None:
    """Qualified names ``old`` bound that ``new`` removes or binds differently.

    None when either side cannot be read (callers must treat that as "changed").
    Names only ``new`` binds are not changes: nothing could depend on them yet.
    """
    before = interface_map(old)
    if before is None:
        return None
    if new is None:
        return set(before)
    after = interface_map(new)
    if after is None:
        return None
    return {name for name, shape in before.items() if after.get(name) != shape}


def short_names(qualified: set[str]) -> set[str]:
    """Return the identifiers code would use to reach each qualified name."""
    names: set[str] = set()
    for name in qualified:
        names.add(name.rsplit(".", 1)[-1])
        names.add(name.split(".", 1)[0])
    return {n for n in names if n and not (n.startswith("__") and n.endswith("__"))}


def _bind_class(cls: ast.ClassDef, rendered: dict[str, str]) -> None:
    """Bind a class under its name (header + fields), its members as ``C.m``."""
    header = [f"@{ast.unparse(d)}" for d in cls.decorator_list]
    bases = ", ".join(
        [*(ast.unparse(b) for b in cls.bases), *(ast.unparse(k) for k in cls.keywords)]
    )
    header.append(f"class {cls.name}({bases})")
    for member in cls.body:
        if isinstance(member, ast.AnnAssign | ast.Assign):
            header.append(ast.unparse(member))
        elif isinstance(member, (*_FUNC, ast.ClassDef)):
            _bind(member, rendered, prefix=f"{cls.name}.")
    rendered[cls.name] = "\n".join(header)


def _bind(stmt: ast.stmt, rendered: dict[str, str], *, prefix: str) -> None:
    """Record the names ``stmt`` binds with the interface it binds them to."""
    if isinstance(stmt, ast.Import | ast.ImportFrom):
        module = stmt.module if isinstance(stmt, ast.ImportFrom) else ""
        level = stmt.level if isinstance(stmt, ast.ImportFrom) else 0
        for alias in stmt.names:
            if alias.name == "*":
                continue
            binding = alias.asname or alias.name.split(".")[0]
            dotted = f"{'.' * level}{module or ''}"
            rendered[prefix + binding] = f"import {dotted}:{alias.name}"
        return
    if isinstance(stmt, (*_FUNC, ast.ClassDef)):
        rendered_text = ast.unparse(stmt)
        rendered[prefix + stmt.name] = api_fingerprint(rendered_text) or rendered_text
        return
    targets: list[str] = []
    if isinstance(stmt, ast.Assign):
        targets = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
    elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        targets = [stmt.target.id]
    for name in targets:
        rendered[prefix + name] = ast.unparse(stmt)


def _parse_lenient(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)
    except SyntaxError:
        pass
    try:
        return ast.parse(source.rstrip() + "\n    pass\n")
    except SyntaxError:
        return None
