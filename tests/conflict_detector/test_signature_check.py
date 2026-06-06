"""Tests for mak.conflict_detector.signature_check."""

from __future__ import annotations

from mak.conflict_detector.signature_check import (
    check_call,
    check_signature_compatibility,
    extract_calls,
    extract_signatures,
)


class TestExtractSignatures:
    def test_simple_function(self) -> None:
        sigs = extract_signatures("def f(a, b, c=1): pass")
        sig = sigs["f"]
        assert sig.positional == ("a", "b", "c")
        assert sig.required_positional == 2
        assert not sig.has_vararg
        assert not sig.has_kwarg

    def test_vararg_and_kwarg(self) -> None:
        sig = extract_signatures("def f(a, *args, **kwargs): pass")["f"]
        assert sig.has_vararg
        assert sig.has_kwarg
        assert sig.positional == ("a",)

    def test_keyword_only(self) -> None:
        sig = extract_signatures("def f(a, *, b, c=3): pass")["f"]
        assert sig.keyword_only_required == ("b",)
        assert sig.keyword_only_optional == ("c",)
        assert "b" in sig.accepted_keywords

    def test_method_drops_self(self) -> None:
        src = "class C:\n    def m(self, x, y): pass\n"
        sigs = extract_signatures(src)
        # Reachable by bare name and by qualified name; self is stripped.
        assert sigs["m"].positional == ("x", "y")
        assert sigs["C.m"].positional == ("x", "y")

    def test_positional_only(self) -> None:
        sig = extract_signatures("def f(a, b, /, c): pass")["f"]
        assert sig.positional == ("a", "b", "c")
        assert sig.required_positional == 3


class TestExtractCalls:
    def test_plain_call(self) -> None:
        (call,) = extract_calls("f(1, 2, key=3)")
        assert call.func_name == "f"
        assert call.positional_count == 2
        assert call.keywords == ("key",)

    def test_attribute_call(self) -> None:
        (call,) = extract_calls("obj.method(1)")
        assert call.func_name == "method"
        assert call.positional_count == 1

    def test_star_args(self) -> None:
        (call,) = extract_calls("f(*xs, **kw)")
        assert call.has_star_args
        assert call.has_double_star


class TestCheckCall:
    def test_compatible_call_passes(self) -> None:
        sig = extract_signatures("def f(a, b): pass")["f"]
        (call,) = extract_calls("f(1, 2)")
        assert check_call(sig, call) is None

    def test_too_many_positional(self) -> None:
        sig = extract_signatures("def f(a, b): pass")["f"]
        (call,) = extract_calls("f(1, 2, 3)")
        assert "at most 2" in (check_call(sig, call) or "")

    def test_vararg_absorbs_extra_positional(self) -> None:
        sig = extract_signatures("def f(a, *args): pass")["f"]
        (call,) = extract_calls("f(1, 2, 3, 4)")
        assert check_call(sig, call) is None

    def test_unknown_keyword(self) -> None:
        sig = extract_signatures("def f(a): pass")["f"]
        (call,) = extract_calls("f(1, bogus=2)")
        assert "unknown keyword argument 'bogus'" in (check_call(sig, call) or "")

    def test_kwarg_absorbs_unknown_keyword(self) -> None:
        sig = extract_signatures("def f(a, **kwargs): pass")["f"]
        (call,) = extract_calls("f(1, anything=2)")
        assert check_call(sig, call) is None

    def test_missing_required_positional(self) -> None:
        sig = extract_signatures("def f(a, b): pass")["f"]
        (call,) = extract_calls("f(1)")
        assert "missing required argument 'b'" in (check_call(sig, call) or "")

    def test_required_filled_by_keyword(self) -> None:
        sig = extract_signatures("def f(a, b): pass")["f"]
        (call,) = extract_calls("f(1, b=2)")
        assert check_call(sig, call) is None

    def test_missing_required_keyword_only(self) -> None:
        sig = extract_signatures("def f(a, *, b): pass")["f"]
        (call,) = extract_calls("f(1)")
        assert "missing required keyword argument 'b'" in (check_call(sig, call) or "")

    def test_double_star_suppresses_missing(self) -> None:
        sig = extract_signatures("def f(a, b): pass")["f"]
        (call,) = extract_calls("f(**kw)")
        assert check_call(sig, call) is None

    def test_star_suppresses_missing_positional(self) -> None:
        sig = extract_signatures("def f(a, b, c): pass")["f"]
        (call,) = extract_calls("f(*args)")
        assert check_call(sig, call) is None

    def test_default_makes_positional_optional(self) -> None:
        sig = extract_signatures("def f(a, b=2): pass")["f"]
        (call,) = extract_calls("f(1)")
        assert check_call(sig, call) is None


class TestCheckSignatureCompatibility:
    def test_incompatible_call_site_detected(self) -> None:
        defining = "def func_b(a, b, c): pass"
        calling = "def caller():\n    return func_b(1)\n"
        reasons = check_signature_compatibility(defining, calling)
        assert len(reasons) == 1
        assert "func_b" in reasons[0]

    def test_compatible_returns_empty(self) -> None:
        defining = "def func_b(a, b): pass"
        calling = "def caller():\n    return func_b(1, 2)\n"
        assert check_signature_compatibility(defining, calling) == []

    def test_unknown_callee_ignored(self) -> None:
        # Calls to functions not in the defining source are not the detector's
        # concern — it only reasons about signatures it actually knows.
        defining = "def func_b(a): pass"
        calling = "def caller():\n    return some_other(1, 2, 3)\n"
        assert check_signature_compatibility(defining, calling) == []

    def test_method_call_against_class_definition(self) -> None:
        defining = "class Svc:\n    def run(self, x, y): pass\n"
        calling = "def caller(svc):\n    return svc.run(1)\n"
        reasons = check_signature_compatibility(defining, calling)
        assert reasons and "run" in reasons[0]
