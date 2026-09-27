"""Measure what MAK's real planner sends, offline: no model calls, no cost.

For each input repository and each strategy (``oneshot``, ``auto``) this runs
MAK's production ``Planner`` — the same inventory view, retrieval, expansion
rounds and prompt layout a session uses — against a **recording fake LLM**. The
fake answers with one expansion request when it is shown a repository tree and
with a fixed one-task plan otherwise, so a ``retrieval`` run takes the round it
would take in real use. Every call is recorded as a ``PlannerCall``.

Inputs: MAK's own ``mak/`` package, the four ``benchmark/project_template*``
projects, and synthetic repositories of 10, 100 and 1,000 files
(``tests/planner/synthetic_repo.py``).

It reports, per input and strategy: the strategy chosen, the first call's
prompt size (characters and estimated tokens), its inventory section, the
largest inventory section of any call, rounds, the stable (cacheable) share of
the first prompt, collapsed directories and the index build time. Output is a
JSON file under ``benchmark/results/`` and a Markdown table on stdout.

What this does **not** measure is plan quality — whether a smaller prompt still
yields a correct plan, covers callers, avoids cascade waves. That needs real
models and is Wave 33's ``mak-e2e`` arm.

    python benchmark/tools/planner_input.py            # everything
    python benchmark/tools/planner_input.py --only synthetic-10 --no-write
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.planner.synthetic_repo import generate_files, ingest  # noqa: E402

from mak.agent_runner.adapters.ollama_api_adapter import estimate_tokens  # noqa: E402
from mak.core.types import NodeId  # noqa: E402
from mak.node_store.store import NodeStore  # noqa: E402
from mak.planner.depgraph import (  # noqa: E402
    build_dep_graph,
    referrers,
    store_sources,
)
from mak.planner.inventory import InventoryView  # noqa: E402
from mak.planner.planner import DEFAULT_INVENTORY_TOKEN_BUDGET, Planner  # noqa: E402
from mak.planner.telemetry import PlannerCall  # noqa: E402

RESULTS = ROOT / "benchmark" / "results" / "planner_input.json"
_DESCRIPTION = "Measure what MAK's real planner sends, offline."
STRATEGIES = ("oneshot", "auto")
TASK = "Add a `timeout` parameter to the main entry point and update its callers."
_SKIP_DIRS = {"__pycache__", ".mak", ".runs", "tests"}


@dataclass(frozen=True, slots=True)
class Row:
    """One input × strategy measurement."""

    input: str
    files: int
    nodes: int
    requested: str
    chosen: str
    first_prompt_chars: int
    first_prompt_tokens: int
    first_inventory_tokens: int
    max_inventory_tokens: int
    rounds: int
    calls: int
    stable_share: float
    collapsed_dirs: int
    index_build_ms: float


class RecordingLLM:
    """A fake planner LLM: one expansion when shown a tree, then a plan."""

    def __init__(self, target: str, expand: str | None) -> None:
        self._plan = json.dumps([{
            "task_id": "t", "description": "do it", "target_nodes": [target],
        }])
        self._expand = expand
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        """Answer like a model that asks for one file before planning."""
        self.prompts.append(prompt)
        if self._expand and "REPOSITORY TREE" in prompt and len(self.prompts) == 1:
            return json.dumps({"expand": [self._expand], "why": "benchmark"})
        return self._plan


def _python_files(root: Path, prefix: str) -> dict[str, str]:
    """Return ``{prefix/relative path: source}`` for every ``.py`` under ``root``."""
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if _SKIP_DIRS & set(relative.parts):
            continue
        files[f"{prefix}/{relative.as_posix()}".lstrip("/")] = path.read_text(
            encoding="utf-8"
        )
    return files


def inputs(only: set[str] | None = None) -> dict[str, dict[str, str]]:
    """Return the benchmark inputs as ``{name: {path: source}}``."""
    candidates = {
        "mak": lambda: _python_files(ROOT / "mak", "mak"),
        **{
            name: (lambda d=d: _python_files(ROOT / "benchmark" / d, ""))
            for name, d in (
                ("template-1", "project_template"),
                ("template-2", "project_template_2"),
                ("template-3", "project_template_3"),
                ("template-4", "project_template_4"),
            )
        },
        **{
            f"synthetic-{n}": (lambda n=n: generate_files(n))
            for n in (10, 100, 1000)
        },
    }
    return {
        name: build() for name, build in candidates.items()
        if only is None or name in only
    }


def _index(files: dict[str, str], workdir: Path) -> tuple[InventoryView, float]:
    """Ingest ``files`` and build the planning index, timing the build."""
    store = ingest(NodeStore(workdir / "store"), files)
    started = time.perf_counter()
    sources = store_sources(store)
    graph = build_dep_graph(sources)
    reverse = referrers(graph)
    view = InventoryView(list(sources), sources=sources, graph=graph,
                         referrers=reverse)
    view.full_tokens()  # what ``auto`` computes to choose a strategy
    return view, (time.perf_counter() - started) * 1000.0


def measure(name: str, files: dict[str, str], strategy: str, workdir: Path) -> Row:
    """Run the planner once over ``files`` and summarize its calls."""
    view, build_ms = _index(files, workdir)
    target = str(next((n for n in view.inventory if "::function::" in str(n)),
                      NodeId("new_module.py")))
    expand = view.files[len(view.files) // 2] if view.files else None
    calls: list[PlannerCall] = []
    planner = Planner(RecordingLLM(target, expand), strategy=strategy)
    outcome = planner.plan(TASK, view.inventory, view=view, observer=calls.append)
    first = calls[0]
    return Row(
        input=name,
        files=len(view.files),
        nodes=view.node_count,
        requested=strategy,
        chosen=outcome.strategy,
        first_prompt_chars=first.prompt_chars,
        first_prompt_tokens=estimate_tokens("x" * first.prompt_chars),
        first_inventory_tokens=estimate_tokens("x" * max(1, first.inventory_chars)),
        max_inventory_tokens=max(
            estimate_tokens("x" * max(1, c.inventory_chars)) for c in calls
        ),
        rounds=outcome.summary.rounds,
        calls=len(calls),
        stable_share=round(first.stable_chars / max(1, first.prompt_chars), 3),
        collapsed_dirs=first.collapsed_dirs,
        index_build_ms=round(build_ms, 1),
    )


def run(only: set[str] | None = None) -> list[Row]:
    """Measure every input under every strategy."""
    rows: list[Row] = []
    for name, files in inputs(only).items():
        for strategy in STRATEGIES:
            with tempfile.TemporaryDirectory(prefix="mak-planner-input-") as tmp:
                rows.append(measure(name, files, strategy, Path(tmp)))
    return rows


def markdown(rows: list[Row]) -> str:
    """Render ``rows`` as the table the documentation quotes."""
    head = (
        "| Input | Files | Nodes | Strategy | First prompt (tokens) | "
        "Inventory (first / max) | Rounds | Stable share | Collapsed dirs | "
        "Index build (ms) |\n|---|--:|--:|---|--:|--:|--:|--:|--:|--:|"
    )
    lines = [head]
    for r in rows:
        chosen = r.chosen if r.chosen == r.requested else f"{r.requested} → {r.chosen}"
        lines.append(
            f"| {r.input} | {r.files:,} | {r.nodes:,} | {chosen} | "
            f"{r.first_prompt_tokens:,} | {r.first_inventory_tokens:,} / "
            f"{r.max_inventory_tokens:,} | {r.rounds} | {r.stable_share:.0%} | "
            f"{r.collapsed_dirs} | {r.index_build_ms:,.0f} |"
        )
    return "\n".join(lines)


def main() -> None:
    """Parse arguments, measure, write the JSON and print the table."""
    parser = argparse.ArgumentParser(description=_DESCRIPTION)
    parser.add_argument("--only", action="append", help="measure only this input")
    parser.add_argument("--output", type=Path, default=RESULTS)
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args()
    rows = run(set(args.only) if args.only else None)
    if not args.no_write:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "task": TASK,
            "inventory_token_budget": DEFAULT_INVENTORY_TOKEN_BUDGET,
            "rows": [asdict(row) for row in rows],
        }
        args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(markdown(rows))


if __name__ == "__main__":
    main()
