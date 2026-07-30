"""Signature compatibility between call sites and a function's new signature.

When agent A rewrites ``func_b`` and agent B's fragment calls ``func_b``, the call
sites in B's fragment must still be compatible with A's *new* signature. This is a
deliberately shallow static check: it extracts each definition's parameter shape,
parses the call expressions, and compares arity and keyword-argument names. It is
not a type checker — argument *types* are never inspected.

**Precision over recall (Wave 11).** A false conflict costs a whole task and its
dependent subtree; a missed conflict costs nothing the test suite would not also
catch. Three rules keep the check from judging what it cannot know:

- **Decorators are read.** ``@staticmethod`` has no implicit receiver, so the
  first parameter must *not* be stripped; ``@classmethod`` does. A decorator that
  is neither recognised nor known signature-preserving (``@lru_cache()``,
  ``@app.route(...)``, ``@x.setter``) can reshape the wrapped callable
  arbitrarily, so the definition is dropped from the table entirely rather than
  checked against a shape we are guessing at.
- **Attribute calls are resolved by receiver, not by bare name.** ``obj.get(k)``
  is only matched against a local ``get`` when the receiver is ``self`` / ``cls``
  or the owning class name. Without this, ``self._data.get(name, "")`` — a
  *dict* ``.get`` — resolves to the file's own ``Registers.get`` and reports a
  conflict. The deliberate cost is that a call on an untyped receiver
  (``svc.run(1)``) is no longer checked at all.
- **Methods are keyed ``Class.method`` only.** A flat bare-name table let two
  classes in one file silently shadow each other's methods ("later wins").

The check is also conservative about uncertainty at the call site: a call that
splats ``*args`` or ``**kwargs`` suppresses the arity / missing-argument checks
for the dimension it makes unknowable.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import StrEnum


class Receiver(StrEnum):
    """How a definition's implicit first parameter is supplied by the caller."""

    NONE = "none"  # module-level def, or @staticmethod — nothing is implicit
    INSTANCE = "instance"  # plain method — obj.m(...) supplies ``self``
    CLASS = "class"  # @classmethod — C.m(...) / obj.m(...) supplies ``cls``


_STATIC_DECORATORS = frozenset({"staticmethod"})
_CLASS_DECORATORS = frozenset({"classmethod"})
# Decorators that provably leave the wrapped callable's parameter shape alone.
# Anything outside these three sets makes the definition unresolvable.
_TRANSPARENT_DECORATORS = frozenset(
    {"abstractmethod", "override", "final", "no_type_check", "runtime_checkable"}
)


@dataclass(frozen=True, slots=True)
class Signature:
    """The parameter shape of a function, with an implicit receiver stripped."""

    name: str
    positional: tuple[str, ...]  # positional-or-keyword + positional-only, in order
    required_positional: int  # count of leading positional params without a default
    has_vararg: bool  # def f(*args)
    keyword_only_required: tuple[str, ...]
    keyword_only_optional: tuple[str, ...]
    has_kwarg: bool  # def f(**kwargs)
    is_method: bool  # defined inside a class (reachable only as ``Owner.name``)
    receiver: Receiver = Receiver.NONE

    @property
    def accepted_keywords(self) -> frozenset[str]:
        """Names that may legally be passed by keyword."""
        return frozenset(
            (*self.positional, *self.keyword_only_required, *self.keyword_only_optional)
        )


@dataclass(frozen=True, slots=True)
class CallSite:
    """A single call expression, reduced to the dimensions the check cares about."""

    func_name: str
    positional_count: int
    has_star_args: bool  # foo(*xs)
    keywords: tuple[str, ...]  # explicit keyword names (excludes **kwargs)
    has_double_star: bool  # foo(**kw)
    is_attribute: bool  # obj.foo(...) rather than a bare foo(...)
    receiver: str | None = None  # the receiver's name when it is a plain name
    enclosing_class: str | None = None  # class body the call is written inside


def _decorator_name(node: ast.expr) -> str | None:
    """Return a decorator's rightmost name, or None when it is not a plain name.

    ``@staticmethod`` -> ``staticmethod``; ``@abc.abstractmethod`` ->
    ``abstractmethod``. A decorator *factory* (``@lru_cache(maxsize=1)``) or any
    other expression returns None, which the caller reads as "unknowable".
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _receiver_of(
    node: ast.FunctionDef | ast.AsyncFunctionDef, *, in_class: bool
) -> Receiver | None:
    """Return how ``node``'s first parameter is bound, or None if unknowable.

    Returning None means the definition is dropped rather than mis-read. Note
    that *not* stripping the receiver is not the safe default on its own: keeping
    ``self`` in the positional list turns every correct ``obj.m(x)`` into a
    "missing required argument 'self'" — one false conflict traded for another.
    """
    names = [_decorator_name(d) for d in node.decorator_list]
    if any(name is None for name in names):
        return None
    resolved = {name for name in names if name is not None}
    if resolved & _STATIC_DECORATORS:
        return Receiver.NONE
    if resolved & _CLASS_DECORATORS:
        return Receiver.CLASS
    if resolved - _TRANSPARENT_DECORATORS:
        return None
    return Receiver.INSTANCE if in_class else Receiver.NONE


def _build_signature(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    receiver: Receiver,
    is_method: bool,
) -> Signature:
    args = node.args
    positional = [a.arg for a in (*args.posonlyargs, *args.args)]
    if receiver is not Receiver.NONE and positional:
        # Drop the implicit receiver — call sites write obj.method(x), not (self, x).
        positional = positional[1:]
    num_defaults = len(args.defaults)
    required_positional = max(0, len(positional) - num_defaults)

    kw_required: list[str] = []
    kw_optional: list[str] = []
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        if default is None:
            kw_required.append(arg.arg)
        else:
            kw_optional.append(arg.arg)

    return Signature(
        name=node.name,
        positional=tuple(positional),
        required_positional=required_positional,
        has_vararg=args.vararg is not None,
        keyword_only_required=tuple(kw_required),
        keyword_only_optional=tuple(kw_optional),
        has_kwarg=args.kwarg is not None,
        is_method=is_method,
        receiver=receiver,
    )


def extract_signatures(source: str) -> dict[str, Signature]:
    """Extract a ``Signature`` per function/method defined in ``source``.

    Top-level functions are keyed by bare name; methods are keyed **only** as
    ``Class.method`` (the immediately enclosing class), so two classes defining
    the same method name in one file no longer shadow each other. Later
    definitions of the same key win (matches "the new committed version").

    A definition whose decorators make its parameter shape unknowable is omitted
    — and clears any earlier definition of the same key, so nothing is ever
    checked against a stale shape.
    """
    tree = ast.parse(source)
    signatures: dict[str, Signature] = {}

    def visit(body: list[ast.stmt], class_name: str | None) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                visit(node.body, node.name)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                key = (
                    f"{class_name}.{node.name}"
                    if class_name is not None
                    else node.name
                )
                receiver = _receiver_of(node, in_class=class_name is not None)
                if receiver is None:
                    signatures.pop(key, None)
                    continue
                signatures[key] = _build_signature(
                    node, receiver=receiver, is_method=class_name is not None
                )

    visit(tree.body, None)
    return signatures


def _call_site(node: ast.Call, enclosing_class: str | None) -> CallSite | None:
    """Reduce a call expression to a ``CallSite``, or None if it has no name."""
    func = node.func
    receiver: str | None = None
    if isinstance(func, ast.Name):
        name, is_attribute = func.id, False
    elif isinstance(func, ast.Attribute):
        name, is_attribute = func.attr, True
        if isinstance(func.value, ast.Name):
            receiver = func.value.id
    else:
        return None
    positional = [a for a in node.args if not isinstance(a, ast.Starred)]
    return CallSite(
        func_name=name,
        positional_count=len(positional),
        has_star_args=any(isinstance(a, ast.Starred) for a in node.args),
        keywords=tuple(kw.arg for kw in node.keywords if kw.arg is not None),
        has_double_star=any(kw.arg is None for kw in node.keywords),
        is_attribute=is_attribute,
        receiver=receiver,
        enclosing_class=enclosing_class,
    )


def _collect_calls(
    node: ast.AST, class_name: str | None, calls: list[CallSite]
) -> None:
    """Walk ``node`` in source order, tracking which class body each call is in."""
    if isinstance(node, ast.ClassDef):
        # Decorators, bases and keywords are evaluated in the *outer* scope; only
        # the body belongs to the class. The innermost class wins for nested ones.
        for outer in (*node.decorator_list, *node.bases, *node.keywords):
            _collect_calls(outer, class_name, calls)
        for stmt in node.body:
            _collect_calls(stmt, node.name, calls)
        return
    if isinstance(node, ast.Call):
        site = _call_site(node, class_name)
        if site is not None:
            calls.append(site)
    for child in ast.iter_child_nodes(node):
        _collect_calls(child, class_name, calls)


def extract_calls(source: str) -> list[CallSite]:
    """Extract every call expression in ``source`` as a ``CallSite``."""
    calls: list[CallSite] = []
    _collect_calls(ast.parse(source), None, calls)
    return calls


def check_call(signature: Signature, call: CallSite) -> str | None:
    """Return a reason if ``call`` is incompatible with ``signature``, else None."""
    max_positional = len(signature.positional)

    # Too many positional arguments (only provable without a *splat or *args).
    if (
        not call.has_star_args
        and not signature.has_vararg
        and call.positional_count > max_positional
    ):
        return (
            f"passes {call.positional_count} positional args but "
            f"'{signature.name}' accepts at most {max_positional}"
        )

    # Unknown keyword argument (only provable when the callee has no **kwargs).
    if not signature.has_kwarg:
        for kw in call.keywords:
            if kw not in signature.accepted_keywords:
                return f"unknown keyword argument '{kw}' for '{signature.name}'"

    # Missing required arguments — suppressed if the call splats *args/**kwargs,
    # which could supply them in ways we cannot statically see.
    if not call.has_star_args and not call.has_double_star:
        covered = set(call.keywords)
        for index in range(signature.required_positional):
            name = signature.positional[index]
            if index < call.positional_count or name in covered:
                continue
            return f"missing required argument '{name}' for '{signature.name}'"
        for name in signature.keyword_only_required:
            if name not in covered:
                return (
                    f"missing required keyword argument '{name}' "
                    f"for '{signature.name}'"
                )

    return None


def resolve_signature(
    signatures: dict[str, Signature], call: CallSite
) -> Signature | None:
    """Return the definition ``call`` provably targets, or None if unknowable.

    The resolution rules, and why each one is this narrow:

    - ``foo(...)`` — a bare call reaches only a module-level function. It can
      never be a method: methods are keyed ``Class.method``.
    - ``self.foo(...)`` — resolves within the class body the call is written in.
      This is the only receiver whose type is certain.
    - ``cls.foo(...)`` / ``Owner.foo(...)`` — resolves to ``Owner.foo``, but only
      when that definition is a ``classmethod`` or ``staticmethod``. An unbound
      ``Owner.method(obj, x)`` passes the receiver explicitly and is
      indistinguishable here from a bound call, so plain methods are skipped.
    - anything else (``obj.foo(...)``, ``self._data.get(...)``) — the receiver's
      type is unknown, so the call is not judged.
    """
    if not call.is_attribute:
        signature = signatures.get(call.func_name)
        return None if signature is None or signature.is_method else signature
    if call.receiver == "self" and call.enclosing_class is not None:
        return signatures.get(f"{call.enclosing_class}.{call.func_name}")
    owner = call.enclosing_class if call.receiver == "cls" else call.receiver
    if owner is None:
        return None
    signature = signatures.get(f"{owner}.{call.func_name}")
    if signature is None or signature.receiver is Receiver.INSTANCE:
        return None
    return signature


def check_signature_compatibility(
    defining_source: str, calling_source: str
) -> list[str]:
    """Check every call in ``calling_source`` against signatures in ``defining_source``.

    Returns a list of human-readable incompatibility reasons (empty if compatible).
    Call sites whose target cannot be *proven* to be a definition in
    ``defining_source`` are ignored — see ``resolve_signature`` for the rules.
    """
    signatures = extract_signatures(defining_source)
    reasons: list[str] = []
    for call in extract_calls(calling_source):
        signature = resolve_signature(signatures, call)
        if signature is None:
            continue
        reason = check_call(signature, call)
        if reason is not None:
            reasons.append(reason)
    return reasons
