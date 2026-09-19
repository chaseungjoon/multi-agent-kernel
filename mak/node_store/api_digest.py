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


# -- the interface fingerprint (Wave 20) ------------------------------------
#
# ``public_api_digest`` answers "what should a dependent be *shown*?" and so
# hides private names. ``api_fingerprint`` answers a different question — "did
# this node's interface change?" — and must not: a private helper's callers are
# broken by its signature change exactly as a public one's are. It is the
# identity behind the ``#api`` lock resource and the body-only / API-change
# split of a stale read.


def api_fingerprint(source: str, *, parameters: bool = True) -> str | None:
    """Return a canonical rendering of everything in ``source`` but code bodies.

    Two sources with equal fingerprints differ only inside function bodies (and
    docstrings), which is the definition of a *body-only* change. Included:
    every function and method signature with its decorators and return
    annotation, class headers (bases, keywords, decorators), class attributes
    **with their values** (a dataclass field's default decides whether it is
    required), imports, and module-level statements verbatim — a constant's
    value *is* its contract.

    ``parameters=False`` renders every parameter list as ``(...)``: two sources
    whose fingerprints agree that way differ at most in parameter shapes, the
    one kind of API change the static signature checks can fully re-verify.

    Returns None when the source does not parse, even as a class shell (the
    ingestion ``class`` fragment is the ``class`` line plus members up to the
    first method, which is not always a complete statement on its own). None is
    "unknown", and callers must treat it as a change.
    """
    tree = _parse_lenient(source)
    if tree is None:
        return None
    lines: list[str] = []
    for stmt in _without_docstring(tree.body):
        lines.extend(_fingerprint_statement(stmt, "", parameters=parameters))
    return "\n".join(lines)


def _parse_lenient(source: str) -> ast.Module | None:
    """Parse ``source``, retrying as a class shell with an empty body."""
    try:
        return ast.parse(source)
    except SyntaxError:
        pass
    try:
        return ast.parse(source.rstrip() + "\n" + _INDENT + "pass\n")
    except SyntaxError:
        return None


def _without_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    """Drop a leading docstring: documentation is not interface."""
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def _fingerprint_statement(
    stmt: ast.stmt, indent: str, *, parameters: bool
) -> list[str]:
    """Render one statement's interface (a function's body is never rendered)."""
    if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
        decorators = [f"{indent}@{ast.unparse(d)}" for d in stmt.decorator_list]
        prefix = "async def" if isinstance(stmt, ast.AsyncFunctionDef) else "def"
        args = ast.unparse(stmt.args) if parameters else "..."
        returns = f" -> {ast.unparse(stmt.returns)}" if stmt.returns else ""
        return [*decorators, f"{indent}{prefix} {stmt.name}({args}){returns}"]
    if isinstance(stmt, ast.ClassDef):
        decorators = [f"{indent}@{ast.unparse(d)}" for d in stmt.decorator_list]
        bases = ", ".join(
            [*(ast.unparse(b) for b in stmt.bases),
             *(ast.unparse(k) for k in stmt.keywords)]
        )
        lines = [*decorators, f"{indent}class {stmt.name}({bases}):"]
        for member in _without_docstring(stmt.body):
            lines.extend(
                _fingerprint_statement(
                    member, indent + _INDENT, parameters=parameters
                )
            )
        return lines
    if isinstance(stmt, ast.Pass):
        return []
    return [f"{indent}{line}" for line in ast.unparse(stmt).splitlines()]
