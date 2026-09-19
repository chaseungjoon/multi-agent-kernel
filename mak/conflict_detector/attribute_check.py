"""Attribute access on an in-repo module that no longer binds the name (shape 4).

Task A deletes or renames ``helpers.slugify``; task B, in the same wave, adds
``helpers.slugify(title)``. Both parse, the cross-module check only looks at
*from-imports*, and cascade detection skips deleted nodes — so the first anyone
hears of it is an ``AttributeError`` at runtime. This check reads every
``mod.name`` where ``mod`` is bound to an in-repo module and reports the ones
that module does not bind.

Precision over recall, like every check in this package. Skipped:

- a local name rebound anywhere in the file (a parameter, an assignment) — it
  may no longer be the module;
- a module that binds names dynamically (module ``__getattr__``, a star import,
  ``globals()``/``exec``) or does not parse;
- ``pkg.sub`` where ``sub`` is a submodule of the package;
- anything not in ``Load`` context (``mod.x = 1`` *creates* the attribute).
"""

from __future__ import annotations

import ast

from mak.conflict_detector.cross_module_check import CrossModuleDefect
from mak.conflict_detector.module_index import ModuleIndex, rebound_names


def check_module_attributes(
    index: ModuleIndex, scope: frozenset[str]
) -> list[CrossModuleDefect]:
    """Report ``mod. name`` uses in ``scope`` whose module does not bind ``name``."""
    defects: list[CrossModuleDefect] = []
    for path in sorted(scope & index.files):
        tree = index.tree(path)
        if tree is None:
            continue
        modules = index.module_bindings(path)
        if not modules:
            continue
        shadowed = rebound_names(tree)
        seen: set[tuple[str, str]] = set()
        for node in ast.walk(tree):
            found = _unbound_use(node, index, modules, shadowed)
            if found is None or found in seen:
                continue
            seen.add(found)
            local, attr = found
            defining = modules[local]
            defects.append(CrossModuleDefect(
                kind="unresolved_attribute",
                file=path,
                defining_file=defining,
                detail=(
                    f"'{path}' uses '{local}.{attr}', but '{defining}' does not "
                    f"define '{attr}'"
                ),
            ))
    return defects


def _unbound_use(
    node: ast.AST,
    index: ModuleIndex,
    modules: dict[str, str],
    shadowed: frozenset[str],
) -> tuple[str, str] | None:
    """``(local, attr)`` when ``node`` reads an attribute its module lacks."""
    if not (
        isinstance(node, ast.Attribute)
        and isinstance(node.ctx, ast.Load)
        and isinstance(node.value, ast.Name)
    ):
        return None
    local, attr = node.value.id, node.attr
    defining = modules.get(local)
    if defining is None or local in shadowed:
        return None
    names = index.top_level_names(defining)
    if names is None or attr in names or index.is_submodule(defining, attr):
        return None
    if attr.startswith("__") and attr.endswith("__"):
        return None  # module dunders (__name__, __file__, …) always exist
    return local, attr
