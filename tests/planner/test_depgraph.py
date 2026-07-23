"""Tests for the static dependency-graph extractor (Wave 10, Step 1)."""

from __future__ import annotations

from mak.core.types import NodeId
from mak.planner.depgraph import DepGraph, build_dep_graph


def _graph(sources: dict[str, str]) -> DepGraph:
    return build_dep_graph({NodeId(k): v for k, v in sources.items()})


class TestSameFile:
    def test_same_file_call_edge(self) -> None:
        graph = _graph({
            "m.py::function::helper": "def helper():\n    return 1\n",
            "m.py::function::caller": "def caller():\n    return helper()\n",
        })
        assert graph.references[NodeId("m.py::function::caller")] == frozenset(
            {NodeId("m.py::function::helper")}
        )

    def test_value_reference_not_called(self) -> None:
        # Passing a function as an argument (not calling it) is still a dependency.
        graph = _graph({
            "m.py::function::helper": "def helper():\n    return 1\n",
            "m.py::function::caller": "def caller():\n    return apply(helper)\n",
        })
        assert NodeId("m.py::function::helper") in graph.references[
            NodeId("m.py::function::caller")
        ]

    def test_self_reference_dropped(self) -> None:
        graph = _graph({
            "m.py::function::rec": "def rec(n):\n    return rec(n - 1)\n",
        })
        assert graph.references[NodeId("m.py::function::rec")] == frozenset()


class TestCrossFile:
    def test_from_package_import_module_then_attr(self) -> None:
        graph = _graph({
            "app/cart.py::module_header::__header__": "",
            "app/cart.py::function::add_item": "def add_item(x):\n    return x\n",
            "app/orders.py::module_header::__header__": "from app import cart\n",
            "app/orders.py::function::place": (
                "def place():\n    return cart.add_item(1)\n"
            ),
        })
        assert NodeId("app/cart.py::function::add_item") in graph.references[
            NodeId("app/orders.py::function::place")
        ]

    def test_from_module_import_symbol_bare_call(self) -> None:
        graph = _graph({
            "app/cart.py::module_header::__header__": "",
            "app/cart.py::function::add_item": "def add_item(x):\n    return x\n",
            "app/orders.py::module_header::__header__": (
                "from app.cart import add_item\n"
            ),
            "app/orders.py::function::place": "def place():\n    return add_item(1)\n",
        })
        assert NodeId("app/cart.py::function::add_item") in graph.references[
            NodeId("app/orders.py::function::place")
        ]

    def test_relative_import(self) -> None:
        graph = _graph({
            "pkg/util.py::module_header::__header__": "",
            "pkg/util.py::function::parse": "def parse(s):\n    return s\n",
            "pkg/app.py::module_header::__header__": "from . import util\n",
            "pkg/app.py::function::load": "def load():\n    return util.parse('x')\n",
        })
        assert NodeId("pkg/util.py::function::parse") in graph.references[
            NodeId("pkg/app.py::function::load")
        ]

    def test_unresolvable_module_no_edge(self) -> None:
        # A definer named 'add_item' exists in two files -> module suffix ambiguous.
        graph = _graph({
            "a/cart.py::module_header::__header__": "",
            "a/cart.py::function::add_item": "def add_item(x):\n    return x\n",
            "b/cart.py::module_header::__header__": "",
            "b/cart.py::function::add_item": "def add_item(x):\n    return x\n",
            "c/orders.py::module_header::__header__": "from x import cart\n",
            "c/orders.py::function::place": (
                "def place():\n    return cart.add_item(1)\n"
            ),
        })
        assert graph.references[NodeId("c/orders.py::function::place")] == frozenset()


class TestMethods:
    def test_class_attr_resolves_to_method_node(self) -> None:
        graph = _graph({
            "m.py::class::Cart": "class Cart:\n    x = 1\n",
            "m.py::method::Cart.total": "def total(self):\n    return 0\n",
            "m.py::function::report": "def report():\n    return Cart.total(None)\n",
        })
        assert NodeId("m.py::method::Cart.total") in graph.references[
            NodeId("m.py::function::report")
        ]

    def test_unknown_receiver_no_edge(self) -> None:
        # self.foo() has receiver 'self' which is neither an import nor a class name.
        graph = _graph({
            "m.py::method::Cart.foo": "def foo(self):\n    return 1\n",
            "m.py::method::Cart.bar": "def bar(self):\n    return self.foo()\n",
        })
        assert graph.references[NodeId("m.py::method::Cart.bar")] == frozenset()

    def test_methods_double_keyed_in_definers(self) -> None:
        graph = _graph({
            "m.py::method::Cart.total": "def total(self):\n    return 0\n",
        })
        node = NodeId("m.py::method::Cart.total")
        assert graph.definers["total"] == (node,)
        assert graph.definers["Cart.total"] == (node,)


class TestRobustness:
    def test_class_shell_unparseable_is_skipped(self) -> None:
        # A bare class shell ('class Cart:') is not valid standalone Python; the
        # extractor must skip it as a reference source rather than crash.
        graph = _graph({
            "m.py::class::Cart": "class Cart:",
            "m.py::function::f": "def f():\n    return 1\n",
        })
        assert graph.references[NodeId("m.py::class::Cart")] == frozenset()

    def test_whole_file_node_indexes_its_symbols(self) -> None:
        graph = _graph({
            "lib.py": "def a():\n    return 1\n\n\ndef b():\n    return a()\n",
            "use.py::module_header::__header__": "from lib import a\n",
            "use.py::function::c": "def c():\n    return a()\n",
        })
        # 'a' from the whole-file lib.py is referenced by use.py::c.
        assert NodeId("lib.py") in graph.references[NodeId("use.py::function::c")]

    def test_empty_sources(self) -> None:
        graph = _graph({})
        assert graph.references == {}
        assert graph.definers == {}
