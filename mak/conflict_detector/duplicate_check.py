"""The same function implemented twice by two tasks in one wave (shape 9).

Two agents each write a private ``_normalize_email`` in different modules.
Both are correct; nothing fails; the codebase has two copies that will drift.
No lock can prevent it — the tasks touch different nodes by construction — but
the kernel knows which symbols each task *created* this wave, which is exactly
what is needed to notice it the moment it happens.

Reported: two functions **created this wave by different tasks**, in different
files, with the same name and an equivalent body — identical once docstrings
are dropped, or (for bodies of three statements or more) at least 85% similar.
Pre-existing functions, same-task duplicates, dunders, ``main``, ``setup`` and
tests are never reported: none of those is two agents duplicating each other.
"""

from __future__ import annotations

import ast
import difflib
from dataclasses import dataclass

from mak.conflict_detector.cross_module_check import CrossModuleDefect

_SIMILARITY = 0.85
_IGNORED = frozenset({"main", "setup", "teardown", "run"})


@dataclass(frozen=True, slots=True)
class CreatedFunction:
    """A top-level function a task created this wave."""

    file: str
    name: str
    source: str
    task_id: str


def check_duplicates(created: list[CreatedFunction]) -> list[CrossModuleDefect]:
    """Report pairs of equivalent same-name functions from different tasks."""
    by_name: dict[str, list[CreatedFunction]] = {}
    for fn in created:
        if _ignored(fn.name):
            continue
        by_name.setdefault(fn.name, []).append(fn)
    defects: list[CrossModuleDefect] = []
    for name, group in sorted(by_name.items()):
        group = sorted(group, key=lambda f: (f.file, f.task_id))
        for i, first in enumerate(group):
            for second in group[i + 1:]:
                if first.file == second.file or first.task_id == second.task_id:
                    continue
                if not _equivalent(first.source, second.source):
                    continue
                defects.append(CrossModuleDefect(
                    kind="duplicate_implementation",
                    file=second.file,
                    defining_file=first.file,
                    detail=(
                        f"'{name}' was implemented twice this wave: in "
                        f"'{first.file}' (task {first.task_id}) and in "
                        f"'{second.file}' (task {second.task_id}). Keep one "
                        f"definition and import it where the other was."
                    ),
                    subject=name,
                    site=f"duplicate:{name}",
                ))
    return defects


def _ignored(name: str) -> bool:
    return (
        name in _IGNORED
        or name.startswith("test")
        or (name.startswith("__") and name.endswith("__"))
    )


def _equivalent(a: str, b: str) -> bool:
    body_a, body_b = _body(a), _body(b)
    if body_a is None or body_b is None:
        return False
    if body_a == body_b:
        return True
    if min(body_a.count("\n"), body_b.count("\n")) + 1 < 3:
        return False
    return difflib.SequenceMatcher(None, body_a, body_b).ratio() >= _SIMILARITY


def _body(source: str) -> str | None:
    """Return a function's body, canonicalised, without its docstring."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    funcs = [
        s for s in tree.body
        if isinstance(s, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    if len(funcs) != 1:
        return None
    body = list(funcs[0].body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return "\n".join(ast.unparse(stmt) for stmt in body)
