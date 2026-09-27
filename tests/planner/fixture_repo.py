"""A small hand-written repository whose inventory renderings are pinned.

Small enough to read in full, varied enough to exercise every rendering rule:
nested packages, a decorated dataclass, a method with keyword-only defaults
(one of them a URL-like literal that must never reach a prompt), an async
function, cross-file callers, a same-file caller and a file nobody calls.
"""

from __future__ import annotations

from pathlib import Path

from mak.node_store.store import NodeStore
from mak.planner.depgraph import build_dep_graph, referrers, store_sources
from mak.planner.inventory import InventoryView

SECRET_DEFAULT = "internal://billing-secret"

FILES: dict[str, str] = {
    "app/models.py": (
        '"""Models."""\n\nfrom dataclasses import dataclass\n\n\n'
        "@dataclass(frozen=True)\nclass Invoice:\n    total: float\n"
        '    currency: str = "EUR"\n\n'
        "    def with_tax(self, rate: float = 0.2, *, "
        f'note: str = "{SECRET_DEFAULT}") -> "Invoice":\n'
        "        return Invoice(self.total * (1 + rate), self.currency)\n"
    ),
    "app/billing/rules.py": (
        "def rounding(value: float, digits: int = 2) -> float:\n"
        "    return round(value, digits)\n\n\n"
        "def strict(value: float) -> float:\n    return rounding(value, 4)\n"
    ),
    "app/billing/service.py": (
        "from app.billing.rules import rounding\nfrom app.models import Invoice\n\n\n"
        "def invoice_total(items: list[float]) -> Invoice:\n"
        "    return Invoice(rounding(sum(items)))\n\n\n"
        "async def refresh(client, *, timeout: float = 3.0) -> None:\n"
        "    await client.get(invoice_total([1.0]))\n"
    ),
    "app/api/routes.py": (
        "from app.billing.service import invoice_total\n\n\n"
        '@route("/total")\ndef total(items):\n    return invoice_total(items)\n'
    ),
    "tools/cli.py": "import sys\n\n\ndef main() -> int:\n    return len(sys.argv)\n",
}


def fixture_store(tmp_path: Path) -> NodeStore:
    """Ingest :data:`FILES` into a fresh store under ``tmp_path``."""
    store = NodeStore(tmp_path / "fixture-store")
    for path, source in FILES.items():
        store.sync_file(path, source)
    return store


def view_of(store: NodeStore) -> InventoryView:
    """Build the planner's inventory view of ``store`` (as the session does)."""
    sources = store_sources(store)
    graph = build_dep_graph(sources)
    return InventoryView(
        list(sources), sources=sources, graph=graph, referrers=referrers(graph)
    )
