"""Synthetic repositories for planner-input tests and the offline benchmark.

``build_synthetic_store(tmp_path, files=N)`` writes ``N`` real Python files —
packages three levels deep, about twelve nodes each (an import header, seven
functions, a class shell with three methods) — with **real cross-file calls**
through ``from … import …`` lines, so the dependency graph and its reverse
index have edges to show. It then ingests them into a ``NodeStore``.

Two files are always present with recognisable names, so retrieval can be
tested against a known answer: ``synth/billing/invoice.py`` defines
``invoice_total`` and ``synth/billing/report.py`` calls it. Everything else is
generated from a seeded RNG: the same ``files`` and ``seed`` give byte-identical
files.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from pathlib import Path

from mak.node_store.store import NodeStore

_VERBS = ("load", "save", "parse", "render", "build", "check", "merge", "split",
          "apply", "fetch", "store", "scan", "sync", "emit", "clean", "rank")
_NOUNS = ("record", "entry", "table", "batch", "token", "frame", "event",
          "index", "block", "queue", "cache", "graph", "shard", "chunk", "page")

INVOICE_FILE = "synth/billing/invoice.py"
REPORT_FILE = "synth/billing/report.py"

_INVOICE = '''"""Invoices."""

from __future__ import annotations


def invoice_total(lines: list[float], tax: float = 0.2) -> float:
    return sum(lines) * (1 + tax)


def invoice_lines(raw: str) -> list[float]:
    return [float(x) for x in raw.split(",")]
'''

_REPORT = '''"""Reports."""

from __future__ import annotations

from synth.billing.invoice import invoice_lines, invoice_total


def monthly_report(raw: str) -> str:
    return f"total={invoice_total(invoice_lines(raw))}"


def yearly_report(raws: list[str]) -> float:
    return sum(invoice_total(invoice_lines(r)) for r in raws)
'''


@dataclass(frozen=True, slots=True)
class SyntheticRepo:
    """A generated repository: its root, its files, and the ingested store."""

    root: Path
    files: dict[str, str]
    store: NodeStore


def _module(path: str) -> str:
    return path.removesuffix(".py").replace("/", ".")


def _layout(count: int) -> list[str]:
    """Return ``count`` generated file paths, three package levels deep."""
    branch = max(2, math.ceil(count ** (1 / 3)))
    paths = []
    for index in range(count):
        top = index // (branch * branch)
        mid = (index // branch) % branch
        paths.append(f"synth/p{top:02d}/q{mid:02d}/m{index % branch:02d}.py")
    return paths


def _names(rng: random.Random, index: int) -> list[str]:
    """Seven function names for file ``index`` (unique repo-wide)."""
    return [
        f"{rng.choice(_VERBS)}_{rng.choice(_NOUNS)}_{index}_{n}" for n in range(7)
    ]


def _source(
    index: int, names: list[str], imports: list[tuple[str, str]]
) -> str:
    """Render one generated file: header, functions, a class with methods."""
    header = ['"""Generated module."""', "", "from __future__ import annotations"]
    header.append("")
    header += [f"from {module} import {name}" for module, name in imports]
    body: list[str] = []
    for position, name in enumerate(names):
        call = ""
        if imports and position % 2 == 0:
            call = f"    {imports[position % len(imports)][1]}(value)\n"
        elif position:
            call = f"    {names[position - 1]}(value)\n"
        body.append(f"\n\ndef {name}(value: int, scale: int = 2) -> int:\n"
                    f"{call}    return value * scale\n")
    body.append(
        f"\n\nclass Worker{index}:\n    limit = 3\n\n"
        f"    def start(self) -> int:\n        return {names[0]}(1)\n\n"
        f"    def step(self, count: int) -> int:\n        return count + 1\n\n"
        f"    def stop(self) -> None:\n        return None\n"
    )
    return "\n".join(header) + "".join(body)


def generate_files(count: int, seed: int = 0) -> dict[str, str]:
    """Return ``{path: source}`` for a ``count``-file synthetic repository."""
    rng = random.Random(seed)
    generated = max(0, count - 2)
    paths = _layout(generated)
    names = [_names(rng, index) for index in range(generated)]
    files: dict[str, str] = {INVOICE_FILE: _INVOICE, REPORT_FILE: _REPORT}
    for index, path in enumerate(paths):
        earlier = rng.sample(range(index), k=min(2, index)) if index else []
        imports = [(_module(paths[e]), names[e][rng.randrange(7)]) for e in earlier]
        files[path] = _source(index, names[index], imports)
    return dict(sorted(files.items()))


def write_files(root: Path, files: dict[str, str]) -> None:
    """Write ``files`` under ``root``, with package ``__init__`` markers omitted."""
    for path, source in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")


def ingest(store: NodeStore, files: dict[str, str]) -> NodeStore:
    """Ingest ``files`` into ``store`` and return it."""
    for path, source in files.items():
        store.sync_file(path, source)
    return store


def build_synthetic_store(
    tmp_path: Path, files: int, seed: int = 0
) -> SyntheticRepo:
    """Write a ``files``-file repository under ``tmp_path`` and ingest it."""
    sources = generate_files(files, seed)
    root = tmp_path / "repo"
    write_files(root, sources)
    store = ingest(NodeStore(tmp_path / "store"), sources)
    return SyntheticRepo(root=root, files=sources, store=store)
