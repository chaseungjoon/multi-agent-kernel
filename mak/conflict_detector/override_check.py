"""An override that cannot accept what its base method accepts (shape 5).

Task A changes ``Base.save(self)`` to ``Base.save(self, x)`` and updates every
``obj.save(x)`` call it can see; task B, in the same wave, writes
``Child(Base).save(self)`` against the old base. Every call now passes ``x`` —
and a ``Child`` raises ``TypeError``. No call-site check can see this: the calls
go through untyped receivers, which the signature check deliberately skips.
What *can* be checked is the class hierarchy itself: an override must accept
every call its base accepts (Liskov's rule, for parameter shapes only).

Checked, for a class whose base resolves to an in-repo class:

- **positional capacity** — the override takes at least as many positional
  arguments as the base, or ``*args`` (and ``*args`` when the base has it);
- **required positional** — it requires no more positional arguments than
  the base;
- **keywords** — every *keyword-only* parameter of the base is accepted, no
  new keyword-only parameter is required, and ``**kwargs`` is kept when the
  base has it.

Skipped, deliberately (precision over recall): constructors and other
dunders whose override rules are not Liskov's (``__init__``, ``__new__``,
``__init_subclass__``, ``__post_init__``, ``__class_getitem__``); a method or
base with an unknowable decorator; a static/class/instance mismatch; renamed
positional-or-keyword parameters (common, usually called positionally); and
any base that does not resolve in the repo.
"""

from __future__ import annotations

import ast

from mak.conflict_detector.cross_module_check import CrossModuleDefect
from mak.conflict_detector.module_index import ModuleIndex
from mak.conflict_detector.signature_check import Signature, signature_for

_NOT_LISKOV = frozenset(
    {"__init__", "__new__", "__init_subclass__", "__post_init__",
     "__class_getitem__"}
)
_MAX_DEPTH = 8


def check_overrides(
    index: ModuleIndex, scope: frozenset[str]
) -> list[CrossModuleDefect]:
    """Report overrides incompatible with an in-repo base method.

    A pair is judged when the child's file or the base's file is in ``scope``,
    so a base changed this wave is checked against every subclass in the repo.
    """
    defects: list[CrossModuleDefect] = []
    for path in sorted(index.files):
        for cls in index.classes(path).values():
            defects.extend(_check_class(index, scope, path, cls))
    return defects


def _check_class(
    index: ModuleIndex, scope: frozenset[str], path: str, cls: ast.ClassDef
) -> list[CrossModuleDefect]:
    defects: list[CrossModuleDefect] = []
    for method in cls.body:
        if not isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if method.name in _NOT_LISKOV:
            continue
        found = _base_method(index, path, cls, method.name, depth=0)
        if found is None:
            continue
        base_file, base_cls, base_method = found
        if path not in scope and base_file not in scope:
            continue
        reason = _incompatibility(method, base_method)
        if reason is None:
            continue
        defects.append(CrossModuleDefect(
            kind="override_mismatch",
            file=path,
            defining_file=base_file,
            detail=(
                f"'{path}': {cls.name}.{method.name} overrides "
                f"{base_cls.name}.{method.name} from '{base_file}' but {reason}"
            ),
        ))
    return defects


def _base_method(
    index: ModuleIndex, path: str, cls: ast.ClassDef, name: str, *, depth: int
) -> tuple[str, ast.ClassDef, ast.FunctionDef | ast.AsyncFunctionDef] | None:
    """Return the nearest in-repo base definition of ``name``, left-to-right."""
    if depth > _MAX_DEPTH:
        return None
    for base in cls.bases:
        resolved = index.resolve_class(path, base)
        if resolved is None:
            continue
        base_file, base_cls = resolved
        for member in base_cls.body:
            if (
                isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef)
                and member.name == name
            ):
                return base_file, base_cls, member
        deeper = _base_method(index, base_file, base_cls, name, depth=depth + 1)
        if deeper is not None:
            return deeper
    return None


def _incompatibility(
    override: ast.FunctionDef | ast.AsyncFunctionDef,
    base: ast.FunctionDef | ast.AsyncFunctionDef,
) -> str | None:
    """Why ``override`` rejects a call ``base`` accepts, or None."""
    o = signature_for(override, in_class=True)
    b = signature_for(base, in_class=True)
    if o is None or b is None or o.receiver is not b.receiver:
        return None
    return _positional_gap(o, b) or _keyword_gap(o, b)


def _positional_gap(o: Signature, b: Signature) -> str | None:
    if b.has_vararg and not o.has_vararg:
        return "the base accepts *args and the override does not"
    if not o.has_vararg and len(o.positional) < len(b.positional):
        return (
            f"it accepts {len(o.positional)} positional argument(s) where the "
            f"base accepts {len(b.positional)}"
        )
    if o.required_positional > b.required_positional:
        return (
            f"it requires {o.required_positional} positional argument(s) where "
            f"the base requires {b.required_positional}"
        )
    return None


def _keyword_gap(o: Signature, b: Signature) -> str | None:
    if b.has_kwarg and not o.has_kwarg:
        return "the base accepts **kwargs and the override does not"
    if not o.has_kwarg:
        for name in (*b.keyword_only_required, *b.keyword_only_optional):
            if name not in o.accepted_keywords:
                return f"it does not accept the base's keyword argument '{name}'"
    extra = set(o.keyword_only_required) - set(b.keyword_only_required)
    if extra:
        return (
            "it requires keyword argument(s) the base does not: "
            + ", ".join(sorted(extra))
        )
    return None
