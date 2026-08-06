"""Agreement between modules a single wave created, checked after the wave.

Every gate MAK runs is *within* a task: the conflict detector validates one
agent's staged fragments, and the parse gate proves each file compiles. Nothing
checked whether the new modules agree with **each other**. A real run produced
this, from a task that could not see the module it was importing::

    try:
        from editor.homeart import pick_banner
    except Exception:            # never taken — the import succeeds
        def pick_banner(width: int) -> List[str]: ...
    ...
    pick_banner(width)           # TypeError: missing 'height'

Both files parse. Both tasks reported complete. The same file also imported a
``load_recent`` its target module never defined, wrapped in a bare ``except``, so
the feature was silently dead. This module is the check that catches both:

- **unresolved_import** — importing a name the defining module does not bind.
- **signature_mismatch** — calling an imported function with an argument shape
  its definition rejects.

Scope is the files the wave touched, judged against the store as it now stands.
It inherits :mod:`mak.conflict_detector.signature_check`'s precision-over-recall
contract: a call is judged only when the definition it reaches is *provable* —
resolvable import, no local shadow, not a method — because a false report here
costs a whole extra wave.

That contract is also why import resolution runs in **strict** mode here while
plan validation uses the loose one. Validation's wrong guess is a finding a human
reads; this check's wrong guess is a fix-up task that tells an agent to change
working code. Loose resolution matches on a unique *last segment*, which sent
``from PyInstaller.__main__ import run`` to a repo's own ``editor/__main__.py`` and
reported the correct import as a defect.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass

from mak.conflict_detector.signature_check import (
    CallSite,
    Signature,
    check_call,
    extract_calls,
    extract_signatures,
)
from mak.planner.depgraph import resolve_module_file


@dataclass(frozen=True, slots=True)
class CrossModuleDefect:
    """One provable disagreement between a calling file and a defining file."""

    # unresolved_import | signature_mismatch
    kind: str
    file: str
    defining_file: str
    detail: str


@dataclass(frozen=True, slots=True)
class _Imported:
    """A name bound by ``from <module> import <name>``, resolved to its file."""

    defining_file: str
    original_name: str


def check_cross_module_api(
    file_sources: Mapping[str, str], scope: frozenset[str]
) -> list[CrossModuleDefect]:
    """Report imports and calls in ``scope`` that the defining files contradict.

    ``file_sources`` is the whole repository as MAK currently holds it, keyed by
    path; ``scope`` names the files to judge (the ones this wave wrote). A file
    that does not parse yields nothing — the parse gate already owns that failure.
    """
    files = frozenset(file_sources)
    defects: list[CrossModuleDefect] = []
    for path in sorted(scope & set(file_sources)):
        try:
            tree = ast.parse(file_sources[path])
        except SyntaxError:
            continue
        imported = _resolve_from_imports(tree, path, files)
        modules = _resolve_module_aliases(tree, path, files)
        defects.extend(_check_imports(path, imported, file_sources))
        defects.extend(
            _check_calls(path, tree, imported, modules, file_sources)
        )
    return defects


# -- import resolution ----------------------------------------------------


def _resolve_from_imports(
    tree: ast.Module, path: str, files: frozenset[str]
) -> dict[str, _Imported]:
    """Map each locally bound name to the in-repo file its from-import names.

    Star imports and imports of a *submodule* (``from pkg import mod``, where
    ``pkg/mod.py`` exists) are skipped: neither binds a symbol whose definition
    this check could look up.
    """
    bindings: dict[str, _Imported] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        defining = resolve_module_file(
            module, files, from_file=path, level=node.level, strict=True
        )
        if defining is None or defining == path:
            continue
        for alias in node.names:
            if alias.name == "*":
                continue
            submodule = resolve_module_file(
                f"{module}.{alias.name}" if module else alias.name,
                files,
                from_file=path,
                level=node.level,
                strict=True,
            )
            if submodule is not None:
                continue  # the alias is a module, not a symbol in ``defining``
            bindings[alias.asname or alias.name] = _Imported(defining, alias.name)
    return bindings


def _resolve_module_aliases(
    tree: ast.Module, path: str, files: frozenset[str]
) -> dict[str, str]:
    """Map each name bound by ``import x``/``import x as y`` to its in-repo file."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import):
            continue
        for alias in node.names:
            binding = alias.asname or alias.name
            if "." in binding:
                continue  # ``import pkg.mod`` binds ``pkg``, not the module
            resolved = resolve_module_file(alias.name, files, strict=True)
            if resolved is not None and resolved != path:
                aliases[binding] = resolved
    return aliases


# -- the two checks -------------------------------------------------------


def _check_imports(
    path: str,
    imported: dict[str, _Imported],
    file_sources: Mapping[str, str],
) -> list[CrossModuleDefect]:
    """Report each imported name its defining module does not actually bind."""
    defects: list[CrossModuleDefect] = []
    surface: dict[str, frozenset[str] | None] = {}
    for local_name, origin in sorted(imported.items()):
        defining = origin.defining_file
        if defining not in surface:
            surface[defining] = _exported_names(file_sources[defining])
        exported = surface[defining]
        if exported is None or origin.original_name in exported:
            continue
        defects.append(CrossModuleDefect(
            kind="unresolved_import",
            file=path,
            defining_file=origin.defining_file,
            detail=(
                f"'{path}' imports '{origin.original_name}' from "
                f"'{origin.defining_file}', which does not define it"
                + (f" (bound locally as '{local_name}')"
                   if local_name != origin.original_name else "")
            ),
        ))
    return defects


def _check_calls(
    path: str,
    tree: ast.Module,
    imported: dict[str, _Imported],
    modules: dict[str, str],
    file_sources: Mapping[str, str],
) -> list[CrossModuleDefect]:
    """Report calls to imported functions whose argument shape is incompatible."""
    shadowed = _local_definitions(tree)
    defects: list[CrossModuleDefect] = []
    tables: dict[str, dict[str, Signature]] = {}
    for call in extract_calls(file_sources[path]):
        target = _call_target(call, imported, modules, shadowed)
        if target is None:
            continue
        defining_file, name = target
        if defining_file not in tables:
            tables[defining_file] = _signatures_of(file_sources[defining_file])
        signature = tables[defining_file].get(name)
        if signature is None or signature.is_method:
            continue
        reason = check_call(signature, call)
        if reason is None:
            continue
        defects.append(CrossModuleDefect(
            kind="signature_mismatch",
            file=path,
            defining_file=defining_file,
            detail=(
                f"'{path}' calls '{name}' from '{defining_file}': {reason}"
            ),
        ))
    return defects


def _call_target(
    call: CallSite,
    imported: dict[str, _Imported],
    modules: dict[str, str],
    shadowed: frozenset[str],
) -> tuple[str, str] | None:
    """Return ``(defining_file, name)`` when a call provably reaches one, else None.

    Two shapes are provable: a bare call to a from-imported name, and an
    attribute call on a name bound to an in-repo module. Anything else — a local
    definition shadowing the import, a method, an unknown receiver — is left
    alone, matching ``signature_check.resolve_signature``'s rules.
    """
    if not call.is_attribute:
        origin = imported.get(call.func_name)
        if origin is None or call.func_name in shadowed:
            return None
        return origin.defining_file, origin.original_name
    if call.receiver is None or call.receiver in shadowed:
        return None
    defining_file = modules.get(call.receiver)
    return (defining_file, call.func_name) if defining_file else None


# -- module surface -------------------------------------------------------


def _signatures_of(source: str) -> dict[str, Signature]:
    """Extract a defining module's signatures, or nothing when it does not parse.

    The defining module need not be in scope, so unlike the caller it has not
    been proven parseable — and a raised ``SyntaxError`` here would abort the
    whole post-wave check over one unrelated broken file.
    """
    try:
        return extract_signatures(source)
    except SyntaxError:
        return {}


def _exported_names(source: str) -> frozenset[str] | None:
    """Every name a module binds at top level, or None when it does not parse.

    Re-exports count: a module that imports a name and passes it on genuinely
    provides it, so import bindings are included alongside its own definitions.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    return _local_definitions(tree) | _import_bindings(tree)


def _local_definitions(tree: ast.Module) -> frozenset[str]:
    """Names a module defines itself at top level (not the ones it imports)."""
    names: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(stmt.name)
        elif isinstance(stmt, ast.Assign):
            names.update(t.id for t in stmt.targets if isinstance(t, ast.Name))
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            names.add(stmt.target.id)
    return frozenset(names)


def _import_bindings(tree: ast.Module) -> frozenset[str]:
    """Every name any import statement in a module binds."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            names.update(
                alias.asname or alias.name.split(".")[0] for alias in node.names
            )
    return frozenset(names)
