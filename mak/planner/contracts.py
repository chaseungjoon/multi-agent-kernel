"""Declared API contracts, and holding an implementation to one.

A contract is the planner's statement of the signature a task will give a node —
``"def get_user(uid: int) -> User | None"`` — written *before* the code exists.
It does two jobs:

- **Dependents build against it.** It is shipped to them as a fixed interface
  (layer 0 of the bundle) and used as the signature authority when their calls
  are checked, so a dependent can be written while its provider is still being
  implemented.
- **The provider is held to it.** At commit, the implementation's signature must
  equal the declared one exactly. A contract that the implementation is allowed
  to drift from is not a contract; it is a guess the dependents trusted.

Only the *signature* is compared — name, parameters (with annotations and
defaults), return annotation, async-ness; for a class, its bases. Bodies,
docstrings and decorators are the implementation's business.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from mak.core.exceptions import ContractError

_Def = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


@dataclass(frozen=True, slots=True)
class Contract:
    """A parsed contract: the declared name and its normalized signature."""

    name: str
    signature: str  # canonical ``def f(a: int) -> R`` / ``class C(Base)``
    is_class: bool


def parse_contract(text: str) -> Contract:
    """Parse ``text`` as a single function or class signature.

    Accepts the forms a planner naturally writes: with or without a trailing
    colon, with or without ``...``, ``def``/``async def``/``class``. Raises
    :class:`ContractError` for anything that is not exactly one signature.
    """
    header = text.strip()
    if not header:
        raise ContractError("a contract must not be empty")
    for suffix in ("...", ":"):
        header = header.removesuffix(suffix).rstrip()
    try:
        tree = ast.parse(f"{header}: ...\n")
    except SyntaxError as exc:
        raise ContractError(
            f"contract {text!r} is not a Python signature ({exc.msg})"
        ) from exc
    if len(tree.body) != 1 or not isinstance(tree.body[0], _Def):
        raise ContractError(
            f"contract {text!r} must be exactly one 'def' or 'class' signature"
        )
    return _contract_of(tree.body[0])


def _contract_of(node: _Def) -> Contract:
    """Build the canonical : class:`Contract` for a parsed definition."""
    return Contract(node.name, _signature_of(node), isinstance(node, ast.ClassDef))


def _signature_of(node: _Def) -> str:
    """Render a definition's signature with no body and no decorators."""
    if isinstance(node, ast.ClassDef):
        bases = ", ".join(
            [*(ast.unparse(b) for b in node.bases),
             *(ast.unparse(k) for k in node.keywords)]
        )
        return f"class {node.name}({bases})"
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    return f"{prefix} {node.name}({ast.unparse(node.args)}){returns}"


def normalize_contract(text: str) -> str:
    """Return the canonical signature for a contract string."""
    return parse_contract(text).signature


def contract_stub(text: str) -> str:
    """Return a parseable stub for a contract, e.g. ``def f(a) -> R: ...``.

    Used as a *definition* in signature checks, so a dependent's calls are
    judged against the declared shape before the implementation exists.
    """
    return f"{parse_contract(text).signature}: ...\n"


def implementation_mismatch(contract_text: str, source: str) -> str | None:
    """Return why ``source`` does not implement ``contract_text``, or None.

    ``source`` is a node's committed source: a function, a method fragment
    (dedented, so it reads as a top-level ``def``), a class, or a whole file.
    The definition with the contract's name is found at the top level first,
    then one class level down (a method contract against a whole-file or class
    source). A source that never defines the name does not implement it.
    """
    contract = parse_contract(contract_text)
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return f"the implementation does not parse ({exc.msg})"
    found = _find_definition(tree, contract.name, is_class=contract.is_class)
    if found is None:
        return f"the implementation defines no '{contract.name}'"
    actual = _signature_of(found)
    if actual == contract.signature:
        return None
    return f"declared `{contract.signature}`, implemented `{actual}`"


def _find_definition(tree: ast.Module, name: str, *, is_class: bool) -> _Def | None:
    """Return the definition of ``name`` at module level or one class down."""
    candidates: list[ast.stmt] = list(tree.body)
    for stmt in tree.body:
        if isinstance(stmt, ast.ClassDef):
            candidates.extend(stmt.body)
    for stmt in candidates:
        if is_class and isinstance(stmt, ast.ClassDef) and stmt.name == name:
            return stmt
        if (
            not is_class
            and isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef)
            and stmt.name == name
        ):
            return stmt
    return None


def symbol_of_node(node_id: str) -> str | None:
    """Return the short symbol a ``file: :kind::name`` id names, else None."""
    parts = node_id.split("::")
    if len(parts) < 3:
        return None
    return parts[2].split("#", 1)[0].rsplit(".", 1)[-1] or None
