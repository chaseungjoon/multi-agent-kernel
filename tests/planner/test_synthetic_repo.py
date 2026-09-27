"""The synthetic repositories the planner-input tests and benchmark run on."""

from __future__ import annotations

from mak.planner.depgraph import dep_graph_from_store, referrers
from tests.planner.synthetic_repo import (
    INVOICE_FILE,
    REPORT_FILE,
    SyntheticRepo,
    generate_files,
)


def test_generation_is_deterministic() -> None:
    assert generate_files(40, seed=3) == generate_files(40, seed=3)
    assert generate_files(40, seed=3) != generate_files(40, seed=4)


def test_sizes_and_depth(synthetic_100: SyntheticRepo) -> None:
    assert len(synthetic_100.files) == 100
    generated = [p for p in synthetic_100.files if p.startswith("synth/p")]
    assert all(p.count("/") == 3 for p in generated)
    nodes = synthetic_100.store.list_nodes()
    assert 11 * 100 <= len(nodes) <= 13 * 100
    assert (synthetic_100.root / INVOICE_FILE).is_file()


def test_real_cross_file_edges(synthetic_100: SyntheticRepo) -> None:
    graph = dep_graph_from_store(synthetic_100.store)
    reverse = referrers(graph)
    cross = [
        (source, target)
        for source, targets in graph.references.items()
        for target in targets
        if str(source).split("::", 1)[0] != str(target).split("::", 1)[0]
    ]
    assert len(cross) > 100
    callers = reverse[f"{INVOICE_FILE}::function::invoice_total"]  # type: ignore[index]
    assert {str(c).split("::", 1)[0] for c in callers} == {REPORT_FILE}
