"""Tests for the public API digest (Wave 13, step 3)."""

from __future__ import annotations

from mak.node_store.api_digest import public_api_digest


def test_function_signature_without_body() -> None:
    digest = public_api_digest(
        "def build(name: str, size: int = 1) -> str:\n"
        "    secret = name * size\n"
        "    return secret\n"
    )
    assert digest == "def build(name: str, size: int=1) -> str: ..."


def test_class_members_and_decorators() -> None:
    digest = public_api_digest(
        "class Box:\n"
        "    limit: int = 3\n\n"
        "    def __init__(self, size):\n"
        "        self._size = size\n\n"
        "    @property\n"
        "    def size(self):\n"
        "        return self._size\n\n"
        "    def _hidden(self):\n"
        "        return 1\n"
    )
    assert "class Box:" in digest
    assert "    limit: int" in digest
    assert "    def __init__(self, size): ..." in digest
    assert "    @property" in digest
    assert "_hidden" not in digest


def test_private_names_are_omitted_but_dunders_kept() -> None:
    digest = public_api_digest(
        "_SECRET = 1\n"
        "LIMIT = 2\n"
        "def _helper():\n    return 1\n"
        "def helper():\n    return 1\n"
    )
    assert "LIMIT = ..." in digest
    assert "_SECRET" not in digest
    assert "def helper(): ..." in digest
    assert "_helper" not in digest


def test_bodies_never_leak() -> None:
    digest = public_api_digest("def f():\n    return 'MAGIC'\n")
    assert "MAGIC" not in digest


def test_unparseable_source_yields_empty() -> None:
    assert public_api_digest("def (:\n") == ""


def test_empty_class_keeps_a_placeholder_body() -> None:
    assert public_api_digest("class A:\n    pass\n") == "class A:\n    ..."


class TestNodeSignature:
    """The one-line shape the planner's inventory shows beside a node id."""

    def test_defaults_are_elided(self) -> None:
        from mak.node_store.api_digest import node_signature

        shape = node_signature(
            'def f(a: int = 5, *, key: str = "sk-live-123") -> int:\n    return a\n',
            "function",
        )
        assert shape == "def f(a: int=..., *, key: str=...) -> int"

    def test_async_and_decorated(self) -> None:
        from mak.node_store.api_digest import node_signature

        source = '@app.route("/x", methods=["GET"])\nasync def g(q):\n    pass\n'
        assert node_signature(source, "function") == "@app.route async def g(q)"

    def test_a_method_fragment(self) -> None:
        from mak.node_store.api_digest import node_signature

        source = "@property\ndef size(self) -> int:\n    return 1\n"
        assert node_signature(source, "method") == "@property def size(self) -> int"
        indented = "    def m(self, x=[1]):\n        pass\n"
        assert node_signature(indented, "method") == "def m(self, x=...)"

    def test_a_class_shell(self) -> None:
        from mak.node_store.api_digest import node_signature

        # The ingestion class fragment has no body after its attributes.
        source = (
            "@dataclass(frozen=True)\nclass A(Base, metaclass=Meta):\n"
            '    """Doc."""\n    x: int = 3\n'
        )
        assert node_signature(source, "class") == (
            "@dataclass class A(Base, metaclass=...)"
        )
        assert node_signature("class B:\n", "class") == "class B"

    def test_capped_at_160_characters(self) -> None:
        from mak.node_store.api_digest import SIGNATURE_MAX_CHARS, node_signature

        params = ", ".join(f"p{i}: int = {i}" for i in range(40))
        shape = node_signature(f"def h({params}):\n    pass\n", "function")
        assert shape is not None
        assert len(shape) == SIGNATURE_MAX_CHARS
        assert shape.endswith("…")

    def test_no_shape_for_module_fragments_or_bad_source(self) -> None:
        from mak.node_store.api_digest import node_signature

        assert node_signature("import os\n", "module_header") is None
        assert node_signature("x = 1\n", "module_body") is None
        assert node_signature("def broken(:\n", "function") is None
