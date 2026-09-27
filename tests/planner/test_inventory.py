"""Tests for mak.planner.inventory: the planner's hierarchical inventory view."""

from __future__ import annotations

from pathlib import Path

import pytest

from mak.agent_runner.adapters.ollama_api_adapter import estimate_tokens
from mak.core.types import NodeId
from mak.planner.inventory import COLLAPSED_SUFFIX, InventoryView
from tests.planner.fixture_repo import SECRET_DEFAULT, fixture_store, view_of
from tests.planner.synthetic_repo import SyntheticRepo

GOLDEN = Path(__file__).parent / "golden"


def _golden(name: str) -> str:
    return (GOLDEN / name).read_text(encoding="utf-8").removesuffix("\n")


@pytest.fixture
def view(tmp_path: Path) -> InventoryView:
    return view_of(fixture_store(tmp_path))


class TestGoldenRenderings:
    """The rendered format is a prompt contract: pinned byte for byte."""

    def test_tree(self, view: InventoryView) -> None:
        assert view.render_tree(1000).text == _golden("inventory_tree.txt")

    def test_collapsed_tree(self, view: InventoryView) -> None:
        tree = view.render_tree(40)
        assert tree.text == _golden("inventory_tree_collapsed.txt")
        assert tree.collapsed == 1
        assert tree.expanded == frozenset({"", "tools/"})

    def test_level_one_file(self, view: InventoryView) -> None:
        rendered = view.render_file("app/billing/service.py")
        assert rendered.text == _golden("inventory_file.txt")
        assert (rendered.nodes_shown, rendered.nodes_hidden) == (3, 0)

    def test_large_file_truncation(self, view: InventoryView) -> None:
        rendered = view.render_file("app/billing/service.py", 45)
        assert rendered.text == _golden("inventory_file_truncated.txt")
        assert (rendered.nodes_shown, rendered.nodes_hidden) == (1, 2)
        assert estimate_tokens(rendered.text) <= 45

    def test_full_view(self, view: InventoryView) -> None:
        assert view.render_full() == _golden("inventory_full.txt")


class TestDeterminism:
    def test_two_builds_render_byte_identically(self, tmp_path: Path) -> None:
        first = view_of(fixture_store(tmp_path / "a"))
        second = view_of(fixture_store(tmp_path / "b"))
        assert first.render_full() == second.render_full()
        assert first.render_tree(40).text == second.render_tree(40).text
        assert first.render_flat() == second.render_flat()

    def test_synthetic_builds_are_identical(self, synthetic_100: SyntheticRepo) -> None:
        a = view_of(synthetic_100.store)
        b = view_of(synthetic_100.store)
        assert a.render_tree(3000).text == b.render_tree(3000).text
        assert a.render_full() == b.render_full()


class TestShapesAndReferences:
    def test_a_string_default_never_reaches_the_rendering(
        self, view: InventoryView
    ) -> None:
        full = view.render_full()
        assert SECRET_DEFAULT not in full
        assert "note: str=..." in full

    def test_incoming_references_name_their_files(self, view: InventoryView) -> None:
        line = next(
            line for line in view.render_full().splitlines()
            if "::function::invoice_total" in line
        )
        assert "← 2 refs · 2 files (app/api/routes.py, this file)" in line

    def test_a_same_file_only_caller_says_this_file(self) -> None:
        sources = {
            NodeId("m.py::function::a"): "def a():\n    return 1\n",
            NodeId("m.py::function::b"): "def b():\n    return a()\n",
        }
        from mak.planner.depgraph import build_dep_graph

        view = InventoryView(list(sources), sources=sources,
                             graph=build_dep_graph(sources))
        assert "← 1 ref · this file" in view.render_file("m.py").text

    def test_ids_only_view_has_no_shapes_or_refs(self) -> None:
        view = InventoryView([NodeId("m.py::function::f"), NodeId("n.py")])
        assert view.render_full() == (
            "m.py  (1 node)\n  ::function::f\n"
            "n.py  (1 node)\n  (whole-file node: target the file path itself)"
        )

    def test_flat_rendering_is_the_oneshot_listing(self, view: InventoryView) -> None:
        assert view.render_flat() == "\n".join(f"  - {n}" for n in view.inventory)
        assert InventoryView([]).render_flat() == "  (empty)"

    def test_neighbours_are_referrers_and_referents(self, view: InventoryView) -> None:
        assert view.neighbour_files("app/billing/service.py") == frozenset(
            {"app/api/routes.py", "app/billing/rules.py", "app/models.py"}
        )


class TestTreeCollapse:
    def test_a_tree_larger_than_its_share_collapses_breadth_first(
        self, synthetic_1000: SyntheticRepo
    ) -> None:
        view = view_of(synthetic_1000.store)
        tree = view.render_tree(3000)
        assert estimate_tokens(tree.text) <= 3000
        assert tree.collapsed > 0
        assert tree.text.count(COLLAPSED_SUFFIX) == tree.collapsed
        # Breadth-first: every top-level directory is at least listed.
        assert "synth/" in tree.expanded

    def test_top_level_that_does_not_fit_is_cut_and_counted(
        self, view: InventoryView
    ) -> None:
        tree = view.render_tree(20)
        assert tree.omitted == 2
        assert "more top-level entries not shown (budget)" in tree.text

    def test_render_dir_opens_one_level_at_a_time(self, view: InventoryView) -> None:
        shut = frozenset({""})
        first = view.render_dir("app/", shut)
        assert first is not None
        text, opened = first
        assert opened == frozenset({"", "app/"})
        assert text.startswith("app/ (4 files, 10 nodes)")
        assert "api/ (1 file, 2 nodes) [collapsed" in text
        second = view.render_dir("app/", opened)
        assert second is not None
        assert second[1] == frozenset({"", "app/", "app/api/", "app/billing/"})
        assert view.render_dir("app/", second[1]) is None

    def test_collapsed_count_tracks_open_directories(self, view: InventoryView) -> None:
        assert view.collapsed_count(frozenset({""})) == 2
        assert view.collapsed_count(frozenset({"", "app/"})) == 3
        assert view.collapsed_count(
            frozenset({"", "app/", "app/api/", "app/billing/", "tools/"})
        ) == 0

    def test_dir_key_accepts_either_spelling(self, view: InventoryView) -> None:
        assert view.dir_key("app/billing") == "app/billing/"
        assert view.dir_key("app/billing/") == "app/billing/"
        assert view.dir_key("nope/") is None
        assert view.dir_key("") is None
