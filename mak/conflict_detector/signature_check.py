"""Signature compatibility between call sites and a function's new signature.

When agent A rewrites ``func_b`` and agent B's fragment calls ``func_b``, the call
sites in B's fragment must still be compatible with A's *new* signature (PLANS.md
§5.1). This is a deliberately shallow static check (§5.2): it extracts each
definition's parameter shape, parses the call expressions, and compares arity and
keyword-argument names. It is not a type checker — argument *types* are never
inspected.

The check is conservative about uncertainty: a call that splats ``*args`` or
``**kwargs`` suppresses the arity / missing-argument checks for the dimension it
makes unknowable, so the detector never reports a false conflict it cannot prove.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Signature:
    """The parameter shape of a function, with method ``self``/``cls`` stripped."""

    name: str
    positional: tuple[str, ...]  # positional-or-keyword + positional-only, in order
    required_positional: int  # count of leading positional params without a default
    has_vararg: bool  # def f(*args)
    keyword_only_required: tuple[str, ...]
    keyword_only_optional: tuple[str, ...]
    has_kwarg: bool  # def f(**kwargs)

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


def _build_signature(
    node: ast.FunctionDef | ast.AsyncFunctionDef, *, is_method: bool
) -> Signature:
    args = node.args
    positional = [a.arg for a in (*args.posonlyargs, *args.args)]
    if is_method and positional:
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
    )


def extract_signatures(source: str) -> dict[str, Signature]:
    """Extract a ``Signature`` per function/method defined in ``source``.

    Top-level functions are keyed by bare name; methods are keyed by both
    ``method`` and ``Class.method`` so a call written either way can resolve.
    Later definitions of the same key win (matches "the new committed version").
    """
    tree = ast.parse(source)
    signatures: dict[str, Signature] = {}

    def visit(body: list[ast.stmt], class_name: str | None) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                visit(node.body, node.name)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                sig = _build_signature(node, is_method=class_name is not None)
                signatures[node.name] = sig
                if class_name is not None:
                    signatures[f"{class_name}.{node.name}"] = sig

    visit(tree.body, None)
    return signatures


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def extract_calls(source: str) -> list[CallSite]:
    """Extract every call expression in ``source`` as a ``CallSite``."""
    tree = ast.parse(source)
    calls: list[CallSite] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name is None:
            continue
        positional = [a for a in node.args if not isinstance(a, ast.Starred)]
        has_star = any(isinstance(a, ast.Starred) for a in node.args)
        keywords = tuple(kw.arg for kw in node.keywords if kw.arg is not None)
        has_double_star = any(kw.arg is None for kw in node.keywords)
        calls.append(
            CallSite(
                func_name=name,
                positional_count=len(positional),
                has_star_args=has_star,
                keywords=keywords,
                has_double_star=has_double_star,
            )
        )
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


def check_signature_compatibility(
    defining_source: str, calling_source: str
) -> list[str]:
    """Check every call in ``calling_source`` against signatures in ``defining_source``.

    Returns a list of human-readable incompatibility reasons (empty if compatible).
    Call sites whose target is not defined in ``defining_source`` are ignored — the
    check only reasons about functions whose signature it actually knows.
    """
    signatures = extract_signatures(defining_source)
    reasons: list[str] = []
    for call in extract_calls(calling_source):
        signature = signatures.get(call.func_name)
        if signature is None:
            continue
        reason = check_call(signature, call)
        if reason is not None:
            reasons.append(reason)
    return reasons
