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
