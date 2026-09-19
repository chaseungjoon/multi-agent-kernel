"""Wave 20 registrar detection, pure-append diffing and commutative merge."""

from __future__ import annotations

import pytest

from mak.node_store.registrar import (
    RegistrarKind,
    appended_entries,
    classify,
    duplicate_keys,
    merge_append,
    parse_registrar,
)

STUB = (
    "def _register_all() -> None:\n"
    '    """Register every route."""\n'
    "    raise NotImplementedError\n"
)


def table(*lines: str, doc: str = "Register every route.") -> str:
    body = "".join(f"    {line}\n" for line in lines) or "    pass\n"
    return f'def _register_all() -> None:\n    """{doc}"""\n{body}'


LOCAL = (
    "def _register_all() -> dict[str, object]:\n"
    '    """Register handlers."""\n'
    "    entries: dict[str, object] = {}\n"
    "    register = entries.__setitem__\n"
    "    register('a', fa)\n"
    "    return entries\n"
)


class TestDetection:
    def test_stub_is_an_empty_registrar(self) -> None:
        assert classify(STUB) is RegistrarKind.EMPTY

    def test_literal_keys_make_it_keyed(self) -> None:
        source = table('register("/a", a)', 'register("/b", b)')
        registrar = parse_registrar(source)
        assert registrar is not None
        assert registrar.kind is RegistrarKind.KEYED
        assert registrar.keys() == ["/a", "/b"]
        assert registrar.callee == "register"

    def test_unkeyed_calls_are_ordered(self) -> None:
        assert classify(table("use(auth)", "use(audit)")) is RegistrarKind.ORDERED

    def test_mixed_keys_are_ordered(self) -> None:
        assert classify(table('use("x", a)', "use(b)")) is RegistrarKind.ORDERED

    def test_prelude_and_return_are_allowed(self) -> None:
        assert classify(LOCAL) is RegistrarKind.KEYED

    def test_attribute_callee(self) -> None:
        assert classify(table('app.route("/a", a)')) is RegistrarKind.KEYED

    @pytest.mark.parametrize(
        "source",
        [
            table('register("/a", a)', 'add("/b", b)'),  # two callees
            table('register("/a", a)', "x = compute()"),  # non-call after entries
            table('register("/a", a)', 'if x:\n        register("/b", b)'),
            "def a():\n    pass\n\ndef b():\n    pass\n",  # two functions
            "async def f():\n    register('a', a)\n",  # async
            "x = 1\n",
            "def f(:\n",
            table("self.helpers[0].register('a', a)"),  # complex receiver
            "def f():\n    return 1\n",  # no entries and no placeholder
            "def f():\n    x = 1\n    return x\n",
        ],
    )
    def test_other_shapes_are_not_registrars(self, source: str) -> None:
        assert parse_registrar(source) is None


class TestAppendedEntries:
    def test_pure_append_from_a_stub(self) -> None:
        added = appended_entries(STUB, table('register("/a", a)'))
        assert added is not None
        assert [e.key for e in added] == ["/a"]

    def test_pure_append_after_entries(self) -> None:
        before = table('register("/a", a)')
        after = table('register("/a", a)', 'register("/b", b)')
        added = appended_entries(before, after)
        assert added is not None and [e.key for e in added] == ["/b"]

    @pytest.mark.parametrize(
        "after",
        [
            table('register("/b", b)'),  # removed /a
            table('register("/a", other)', 'register("/b", b)'),  # edited /a
            table('register("/a", a)', 'register("/b", b)', doc="Changed."),
            "def other() -> None:\n    register('/a', a)\n",
            table('register("/a", a)', 'add("/b", b)'),
        ],
    )
    def test_anything_else_is_not_a_pure_append(self, after: str) -> None:
        assert appended_entries(table('register("/a", a)'), after) is None


class TestMergeAppend:
    def test_two_appenders_merge_in_either_order(self) -> None:
        a = appended_entries(STUB, table('register("/a", a)'))
        b = appended_entries(STUB, table('register("/b", b)'))
        assert a is not None and b is not None
        first = merge_append(STUB, a)
        assert first is not None
        merged = merge_append(first, b)
        assert merged is not None
        registrar = parse_registrar(merged)
        assert registrar is not None and registrar.keys() == ["/a", "/b"]
        assert "NotImplementedError" not in merged

    def test_merge_keeps_prelude_and_return(self) -> None:
        added = appended_entries(LOCAL, LOCAL.replace(
            "    register('a', fa)\n", "    register('a', fa)\n    register('b', fb)\n"
        ))
        assert added is not None
        merged = merge_append(LOCAL, added)
        assert merged is not None
        assert merged.rstrip().endswith("return entries")
        assert parse_registrar(merged) is not None

    def test_identical_line_is_one_registration(self) -> None:
        current = table('register("/a", a)')
        added = appended_entries(STUB, table('register("/a", a)'))
        assert added is not None
        assert merge_append(current, added) == current

    def test_same_key_different_handler_is_a_duplicate(self) -> None:
        current = table('register("/a", a)')
        added = appended_entries(STUB, table('register("/a", z)'))
        assert added is not None
        merged = merge_append(current, added)
        assert merged is not None
        assert duplicate_keys(merged) == ["/a"]

    def test_a_different_callee_is_refused(self) -> None:
        added = appended_entries(STUB, table('add("/a", a)'))
        assert added is not None
        assert merge_append(table('register("/b", b)'), added) is None

    def test_merge_into_a_non_registrar_is_refused(self) -> None:
        added = appended_entries(STUB, table('register("/a", a)'))
        assert added is not None
        assert merge_append("def f():\n    return 1\n", added) is None
        assert merge_append("def f():\n    x = 1\n    return x\n", added) is None


class TestDuplicateKeys:
    def test_reports_each_duplicate_once(self) -> None:
        source = table('r("/a", 1)', 'r("/b", 2)', 'r("/a", 3)', 'r("/a", 4)')
        assert duplicate_keys(source) == ["/a"]

    def test_ordered_and_non_registrars_report_nothing(self) -> None:
        assert duplicate_keys(table("use(a)", "use(a)")) == []
        assert duplicate_keys('def f():\n    print("x")\n    y = 1\n') == []
