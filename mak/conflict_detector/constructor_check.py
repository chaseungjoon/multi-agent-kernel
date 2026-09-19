"""Calls to an in-repo class that its constructor rejects (shape 8).

Task A adds a required field to ``@dataclass class Order`` (or a required
``__init__`` parameter); task B, in the same wave, writes ``Order(1, 100)``
somewhere else. Both parse, the classes are not functions so the arity checks
never look, and the call raises ``TypeError`` the first time it runs.

The constructor's shape is derived from the class itself:

- an explicit ``__init__`` — on the class, or the nearest in-repo base that
  defines one — with ``self`` stripped;
- otherwise a ``@dataclass``'s fields, bases' fields first: annotated class
  attributes, minus ``ClassVar`` and ``field(init=False)``, optional when they
  have a default (``= x``, ``field(default=…)``, ``field(default_factory=…)``),
  keyword-only under ``kw_only=True`` or after a ``KW_ONLY`` marker;
- otherwise, for a class whose every base is in-repo (or none), no parameters.

Skipped (precision over recall): any class with a ``__new__``, a metaclass,
an unrecognised decorator, or a base that does not resolve in the repo — each
can reshape construction in ways the AST does not show — plus calls that splat
arguments, which the shared call check already treats as unknowable.
"""

from __future__ import annotations

import ast

from mak.conflict_detector.cross_module_check import CrossModuleDefect
from mak.conflict_detector.module_index import ModuleIndex, rebound_names
from mak.conflict_detector.signature_check import (
    CallSite,
    Receiver,
    Signature,
    check_call,
    signature_for,
)

_DATACLASS_NAMES = frozenset({"dataclass"})
_MAX_DEPTH = 8


def check_constructors(
    index: ModuleIndex, scope: frozenset[str]
) -> list[CrossModuleDefect]:
    """Report constructor calls the called in-repo class cannot accept.

    A call is judged when the calling file or the class's defining file is in
    ``scope``, so a class changed this wave is checked against every caller.
    """
    defects: list[CrossModuleDefect] = []
    shapes: dict[tuple[str, str], Signature | None] = {}
    for path in sorted(index.files):
        tree = index.tree(path)
        if tree is None:
            continue
        shadowed = rebound_names(tree) - set(index.classes(path))
        for call in _calls(tree):
            if isinstance(call.func, ast.Name) and call.func.id in shadowed:
                continue
            resolved = index.resolve_class(path, call.func)
            if resolved is None:
                continue
            defining, cls = resolved
            if path not in scope and defining not in scope:
                continue
            key = (defining, cls.name)
            if key not in shapes:
                shapes[key] = constructor_signature(index, defining, cls, depth=0)
            signature = shapes[key]
            if signature is None:
                continue
            reason = check_call(signature, _site(call, cls.name))
            if reason is None:
                continue
            defects.append(CrossModuleDefect(
                kind="constructor_mismatch",
                file=path,
                defining_file=defining,
                detail=f"'{path}' constructs '{cls.name}' from '{defining}': {reason}",
            ))
    return defects


def _calls(tree: ast.Module) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def _site(call: ast.Call, name: str) -> CallSite:
    positional = [a for a in call.args if not isinstance(a, ast.Starred)]
    return CallSite(
        func_name=name,
        positional_count=len(positional),
        has_star_args=any(isinstance(a, ast.Starred) for a in call.args),
        keywords=tuple(k.arg for k in call.keywords if k.arg is not None),
        has_double_star=any(k.arg is None for k in call.keywords),
        is_attribute=False,
    )


def constructor_signature(
    index: ModuleIndex, path: str, cls: ast.ClassDef, *, depth: int
) -> Signature | None:
    """Return the parameter shape ``cls(...)`` accepts, or None if unknowable."""
    if depth > _MAX_DEPTH or not _plain_class(cls):
        return None
    init = _own_method(cls, "__init__")
    if init is not None:
        signature = signature_for(init, in_class=True)
        return _renamed(signature, cls.name) if signature else None
    bases = _resolved_bases(index, path, cls)
    if bases is None:
        return None
    if _is_dataclass(cls):
        return _dataclass_signature(index, cls, bases, depth=depth)
    if len(bases) > 1:
        return None  # which base's __init__ wins is an MRO question; not guessed
    if bases:
        base_file, base = bases[0]
        inherited = constructor_signature(index, base_file, base, depth=depth + 1)
        return _renamed(inherited, cls.name) if inherited else None
    return Signature(
        name=cls.name, positional=(), required_positional=0, has_vararg=False,
        keyword_only_required=(), keyword_only_optional=(), has_kwarg=False,
        is_method=False,
    )


def _plain_class(cls: ast.ClassDef) -> bool:
    """No metaclass, no ``__new__``, and only understood decorators."""
    if cls.keywords or _own_method(cls, "__new__") is not None:
        return False
    return all(_decorator_name(d) in _DATACLASS_NAMES for d in cls.decorator_list)


def _decorator_name(node: ast.expr) -> str | None:
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _is_dataclass(cls: ast.ClassDef) -> bool:
    return any(_decorator_name(d) in _DATACLASS_NAMES for d in cls.decorator_list)


def _own_method(
    cls: ast.ClassDef, name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for member in cls.body:
        if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef) and (
            member.name == name
        ):
            return member
    return None


def _resolved_bases(
    index: ModuleIndex, path: str, cls: ast.ClassDef
) -> list[tuple[str, ast.ClassDef]] | None:
    """Every base resolved in-repo, or None if any is not (``object`` is fine)."""
    bases: list[tuple[str, ast.ClassDef]] = []
    for base in cls.bases:
        if isinstance(base, ast.Name) and base.id == "object":
            continue
        resolved = index.resolve_class(path, base)
        if resolved is None:
            return None
        bases.append(resolved)
    return bases


def _dataclass_signature(
    index: ModuleIndex,
    cls: ast.ClassDef,
    bases: list[tuple[str, ast.ClassDef]],
    *,
    depth: int,
) -> Signature | None:
    """Fields of a dataclass (bases first), as its generated ``__init__``."""
    fields: dict[str, tuple[bool, bool]] = {}  # name -> (has_default, kw_only)
    for base_file, base in reversed(bases):
        if not _is_dataclass(base):
            if _own_method(base, "__init__") is not None or base.body and any(
                isinstance(s, ast.AnnAssign) for s in base.body
            ):
                return None
            continue
        inherited = _dataclass_fields(index, base_file, base, depth=depth + 1)
        if inherited is None:
            return None
        fields.update(inherited)
    own = _own_fields(cls)
    if own is None:
        return None
    fields.update(own)
    positional = [n for n, (_, kw) in fields.items() if not kw]
    required = 0
    for name in positional:
        if fields[name][0]:
            break
        required += 1
    return Signature(
        name=cls.name,
        positional=tuple(positional),
        required_positional=required,
        has_vararg=False,
        keyword_only_required=tuple(
            n for n, (default, kw) in fields.items() if kw and not default
        ),
        keyword_only_optional=tuple(
            n for n, (default, kw) in fields.items() if kw and default
        ),
        has_kwarg=False,
        is_method=False,
    )


def _dataclass_fields(
    index: ModuleIndex, path: str, cls: ast.ClassDef, *, depth: int
) -> dict[str, tuple[bool, bool]] | None:
    if depth > _MAX_DEPTH or not _plain_class(cls):
        return None
    bases = _resolved_bases(index, path, cls)
    if bases is None:
        return None
    fields: dict[str, tuple[bool, bool]] = {}
    for base_file, base in reversed(bases):
        if _is_dataclass(base):
            inherited = _dataclass_fields(index, base_file, base, depth=depth + 1)
            if inherited is None:
                return None
            fields.update(inherited)
    own = _own_fields(cls)
    if own is None:
        return None
    fields.update(own)
    return fields


def _own_fields(cls: ast.ClassDef) -> dict[str, tuple[bool, bool]] | None:
    """``{field: (has_default, kw_only)}`` declared directly on a dataclass."""
    kw_only = _decorator_kw_only(cls)
    fields: dict[str, tuple[bool, bool]] = {}
    for stmt in cls.body:
        if not isinstance(stmt, ast.AnnAssign) or not isinstance(
            stmt.target, ast.Name
        ):
            continue
        annotation = ast.unparse(stmt.annotation)
        if "ClassVar" in annotation:
            continue
        if annotation.endswith("KW_ONLY"):
            kw_only = True
            continue
        spec = _field_spec(stmt.value)
        if spec is None:
            return None
        has_default, in_init, field_kw = spec
        if not in_init:
            continue
        fields[stmt.target.id] = (has_default, kw_only or field_kw)
    return fields


def _decorator_kw_only(cls: ast.ClassDef) -> bool:
    for decorator in cls.decorator_list:
        if isinstance(decorator, ast.Call):
            for keyword in decorator.keywords:
                if (
                    keyword.arg == "kw_only"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                ):
                    return True
    return False


def _field_spec(value: ast.expr | None) -> tuple[bool, bool, bool] | None:
    """``(has_default, in_init, kw_only)`` for a field's value; None if unknowable."""
    if value is None:
        return False, True, False
    if not (isinstance(value, ast.Call) and _decorator_name(value) == "field"):
        return True, True, False
    has_default = False
    in_init = True
    kw_only = False
    for keyword in value.keywords:
        if keyword.arg in ("default", "default_factory"):
            has_default = True
        elif keyword.arg == "init":
            if not isinstance(keyword.value, ast.Constant):
                return None
            in_init = bool(keyword.value.value)
        elif keyword.arg == "kw_only":
            if not isinstance(keyword.value, ast.Constant):
                return None
            kw_only = bool(keyword.value.value)
        elif keyword.arg is None:
            return None
    return has_default, in_init, kw_only


def _renamed(signature: Signature, name: str) -> Signature:
    """Return a constructor is called by its class's name, whatever defined it."""
    return Signature(
        name=name,
        positional=signature.positional,
        required_positional=signature.required_positional,
        has_vararg=signature.has_vararg,
        keyword_only_required=signature.keyword_only_required,
        keyword_only_optional=signature.keyword_only_optional,
        has_kwarg=signature.has_kwarg,
        is_method=False,
        receiver=Receiver.NONE,
    )
