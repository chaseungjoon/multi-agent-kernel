"""Tests for mak.conflict_detector.signature_check."""

from __future__ import annotations

from mak.conflict_detector.signature_check import (
    Receiver,
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
        # Wave 11: reachable ONLY by qualified name; self is stripped. The bare
        # name is deliberately absent so two classes cannot shadow each other.
        assert sigs["C.m"].positional == ("x", "y")
        assert "m" not in sigs

    def test_positional_only(self) -> None:
        sig = extract_signatures("def f(a, b, /, c): pass")["f"]
        assert sig.positional == ("a", "b", "c")
        assert sig.required_positional == 3


class TestDecoratorAwareReceivers:
    """11.1a: a @staticmethod has no implicit receiver to strip."""

    def test_staticmethod_keeps_every_parameter(self) -> None:
        src = "class C:\n    @staticmethod\n    def norm(name): pass\n"
        sig = extract_signatures(src)["C.norm"]
        assert sig.positional == ("name",)
        assert sig.receiver is Receiver.NONE

    def test_classmethod_strips_cls(self) -> None:
        src = "class C:\n    @classmethod\n    def make(cls, name): pass\n"
        sig = extract_signatures(src)["C.make"]
        assert sig.positional == ("name",)
        assert sig.receiver is Receiver.CLASS

    def test_dotted_staticmethod_is_recognised(self) -> None:
        src = "import builtins\n\n\nclass C:\n"
        src += "    @builtins.staticmethod\n    def norm(name): pass\n"
        assert extract_signatures(src)["C.norm"].positional == ("name",)

    def test_transparent_decorator_is_still_a_method(self) -> None:
        src = "class C:\n    @abstractmethod\n    def run(self, x): pass\n"
        assert extract_signatures(src)["C.run"].positional == ("x",)

    def test_unknown_decorator_drops_the_definition(self) -> None:
        # A decorator MAK cannot recognise may reshape the callable arbitrarily.
        # Guessing either way produces false conflicts, so it is not checked.
        src = "class C:\n    @property\n    def value(self): pass\n"
        assert extract_signatures(src) == {}

    def test_decorator_factory_drops_the_definition(self) -> None:
        src = "@lru_cache(maxsize=8)\ndef fetch(url): pass\n"
        assert extract_signatures(src) == {}

    def test_unknown_decorator_clears_an_earlier_definition(self) -> None:
        src = "def f(a): pass\n\n\n@some_wrapper\ndef f(a, b): pass\n"
        assert "f" not in extract_signatures(src)


class TestBareNameShadowing:
    """11.1c: two classes in one file must not overwrite each other."""

    def test_same_method_name_in_two_classes(self) -> None:
        src = (
            "class A:\n    def get(self, x): pass\n\n\n"
            "class B:\n    def get(self, x, y): pass\n"
        )
        sigs = extract_signatures(src)
        assert sigs["A.get"].positional == ("x",)
        assert sigs["B.get"].positional == ("x", "y")
        assert "get" not in sigs


class TestCallScopes:
    def test_call_records_receiver_and_enclosing_class(self) -> None:
        src = "class C:\n    def go(self):\n        return self.run(1)\n"
        (call,) = extract_calls(src)
        assert call.receiver == "self"
        assert call.enclosing_class == "C"

    def test_bare_call_has_no_receiver(self) -> None:
        (call,) = extract_calls("f(1)")
        assert call.receiver is None
        assert call.enclosing_class is None

    def test_nested_class_wins(self) -> None:
        src = (
            "class Outer:\n"
            "    class Inner:\n"
            "        def go(self):\n"
            "            return self.run(1)\n"
        )
        (call,) = extract_calls(src)
        assert call.enclosing_class == "Inner"


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

    def test_self_call_against_class_definition(self) -> None:
        # ``self.run(...)`` is the one method receiver whose type is certain.
        defining = "class Svc:\n    def run(self, x, y): pass\n"
        calling = (
            "class Svc:\n"
            "    def go(self):\n"
            "        return self.run(1)\n"
        )
        reasons = check_signature_compatibility(defining, calling)
        assert reasons and "run" in reasons[0]

    def test_untyped_receiver_is_not_resolved(self) -> None:
        # Wave 11, deliberate recall loss: the type of ``svc`` is unknown, so a
        # local ``Svc.run`` must not be assumed to be its target. Resolving by
        # bare method name is what made ``self._data.get(k, "")`` — a dict call —
        # report a conflict against the file's own ``get``.
        defining = "class Svc:\n    def run(self, x, y): pass\n"
        calling = "def caller(svc):\n    return svc.run(1)\n"
        assert check_signature_compatibility(defining, calling) == []

    def test_method_call_does_not_match_top_level_function(self) -> None:
        # A bare ``def upper(s)`` must not be matched against the *method* call
        # ``s.upper()`` (the str method) just because the names coincide.
        source = "def upper(s):\n    return s.upper()\n"
        assert check_signature_compatibility(source, source) == []

    def test_bare_call_does_not_match_method_of_same_name(self) -> None:
        # ``cleanup()`` (a bare call) must not resolve to the method ``X.cleanup``.
        defining = "class X:\n    def cleanup(self, path): pass\n"
        calling = "def caller():\n    return cleanup()\n"
        assert check_signature_compatibility(defining, calling) == []

    def test_stdlib_call_does_not_resolve_to_a_same_named_method(self) -> None:
        # 11.1b, the defect that failed `registers_module`: ``self._data.get`` is
        # a *dict* method; it must not be matched against the file's own ``get``.
        source = (
            "class Registers:\n"
            "    def get(self, name):\n"
            '        return self._data.get(name, "")\n'
        )
        assert check_signature_compatibility(source, source) == []

    def test_self_call_to_staticmethod_keeps_its_parameter(self) -> None:
        # 11.1a: ``self._normalise(name)`` on a @staticmethod is one argument to
        # a one-parameter function, not one too many.
        source = (
            "class Registers:\n"
            "    @staticmethod\n"
            "    def _normalise(name):\n"
            "        return name.lower()\n"
            "\n"
            "    def get(self, name):\n"
            "        return self._normalise(name)\n"
        )
        assert check_signature_compatibility(source, source) == []

    def test_two_classes_do_not_shadow_each_other_at_call_sites(self) -> None:
        # ``Marks.lookup`` calls ``self.get(name, None)``; ``Registers.get`` takes
        # one argument and must not be what that call resolves to.
        source = (
            "class Registers:\n"
            "    def get(self, name):\n"
            "        return name\n"
            "\n"
            "\n"
            "class Marks:\n"
            "    def get(self, name, default):\n"
            "        return default\n"
            "\n"
            "    def lookup(self, name):\n"
            "        return self.get(name, None)\n"
        )
        assert check_signature_compatibility(source, source) == []

    def test_unbound_call_through_the_class_name_is_not_judged(self) -> None:
        # ``Base.__init__(self, x)`` passes the receiver explicitly; a bound and
        # an unbound call are indistinguishable here, so neither is reported.
        defining = "class Base:\n    def setup(self, x): pass\n"
        calling = "def caller(obj):\n    return Base.setup(obj, 1)\n"
        assert check_signature_compatibility(defining, calling) == []

    def test_classmethod_call_through_the_class_name_is_checked(self) -> None:
        defining = "class Base:\n    @classmethod\n    def make(cls, x): pass\n"
        calling = "def caller():\n    return Base.make(1, 2)\n"
        reasons = check_signature_compatibility(defining, calling)
        assert reasons and "make" in reasons[0]
