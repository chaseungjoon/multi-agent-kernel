"""Public-API digest of a Python source: declarations without bodies.

A task that depends on another task's output usually needs that output's *API*,
not its implementation — a test writer needs to know that ``pick_banner(width,
height)`` exists and what it returns, not how it draws. The digest is what
``Session._enrich_bundle`` attaches when a dependency's full source would exceed
the bundle's context budget: degrading to the API keeps the dependent task
informed at a fraction of the tokens, where dropping the entry is what left agents
inventing signatures for code they could not see.

Only public names are emitted. A leading underscore means "not part of the
contract", with the dunder exception (``__init__`` is very much part of it).
"""

from __future__ import annotations

import ast

_INDENT = "    "


def public_api_digest(source: str) -> str:
    """Return the public declarations in ``source``, without any function bodies.

    Top-level functions, classes (with their public methods and annotated
    attributes), and module-level bindings, in declaration order. Returns ``""``
    when the source does not parse — a digest is a convenience, never a gate.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ""
    lines: list[str] = []
    for stmt in tree.body:
        lines.extend(_digest_statement(stmt, indent=""))
    return "\n".join(lines)


def _is_public(name: str) -> bool:
    """Whether a bound name is part of the module's contract."""
    return not name.startswith("_") or name.endswith("__")


def _digest_statement(stmt: ast.stmt, *, indent: str) -> list[str]:
    """Digest one statement of a module or class body (empty when not public)."""
    if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
        if not _is_public(stmt.name):
            return []
        decorators = [f"{indent}@{ast.unparse(d)}" for d in stmt.decorator_list]
        return [*decorators, f"{indent}{_signature(stmt)} ..."]
    if isinstance(stmt, ast.ClassDef):
        return _digest_class(stmt, indent=indent) if _is_public(stmt.name) else []
    return [
        f"{indent}{decl}" for name, decl in _bindings(stmt) if _is_public(name)
    ]


def _digest_class(node: ast.ClassDef, *, indent: str) -> list[str]:
    """Digest a class: its header plus the public members of its body."""
    bases = ", ".join(
        [*(ast.unparse(b) for b in node.bases),
         *(f"{kw.arg}={ast.unparse(kw.value)}" for kw in node.keywords if kw.arg)]
    )
    header = f"{indent}class {node.name}({bases}):" if bases else (
        f"{indent}class {node.name}:"
    )
    body: list[str] = []
    for stmt in node.body:
        body.extend(_digest_statement(stmt, indent=indent + _INDENT))
    return [header, *(body or [f"{indent}{_INDENT}..."])]


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Render a ``def name(args) -> ret:`` line for a function definition."""
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    return f"{prefix} {node.name}({ast.unparse(node.args)}){returns}:"


def _bindings(stmt: ast.stmt) -> list[tuple[str, str]]:
    """Return ``(name, declaration)`` for each name an assignment binds.

    The declaration drops the value: a dependency's *contract* is that the name
    exists with that annotation, not what it was initialized to.
    """
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        name = stmt.target.id
        return [(name, f"{name}: {ast.unparse(stmt.annotation)}")]
    if isinstance(stmt, ast.Assign):
        return [
            (t.id, f"{t.id} = ...") for t in stmt.targets if isinstance(t, ast.Name)
        ]
    return []
