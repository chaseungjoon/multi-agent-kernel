"""Wave 28.9: realized concurrency when every task needs an import in one file.

N tasks each rewrite their own function in ``shared.py`` and each needs a
different stdlib import there. Two arms, same agents, same latency:

- ``header`` — the pre-Wave-28 way: a task that needs an import is granted the
  module header and rewrites it, so every task holds WRITE on one node and the
  scheduler can only run them one at a time.
- ``channel`` — each task targets only its function and returns the import in
  ``imports``; the kernel merges it under a shared ``#imports`` lock.

python benchmark/growth_bench.py                  # markdown table
python benchmark/growth_bench.py --tasks 12 --latency 0.5 --json
"""

from __future__ import annotations

import argparse
import json
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from mak.config import GitConfig, MakConfig, NodeStoreConfig, SessionConfig
from mak.core.logging import SessionLogger
from mak.core.types import NodeId, SubTask, TaskBundle, TaskResult
from mak.lock_manager.lock_table import LockTable
from mak.node_store.store import NodeStore
from mak.session import Session

FILE = "shared.py"
HEADER = f"{FILE}::module_header::__header__"
MODULES = (
    "json", "re", "csv", "math", "heapq", "bisect", "string", "textwrap",
    "decimal", "fractions", "statistics", "itertools", "functools", "operator",
    "zlib", "base64",
)
ARMS = ("header", "channel")


@dataclass(frozen=True)
class ArmResult:
    """One arm's run: how parallel it was, what it cost, and what it left."""

    arm: str
    tasks: int
    ok: bool
    max_concurrency: float
    mean_concurrency: float
    wall_s: float
    agent_calls: int
    imports_once: int
    imports_lost: int


def _function(i: int) -> str:
    return f"{FILE}::function::f{i}"


def _project(n: int) -> str:
    body = "".join(f"\n\ndef f{i}():\n    return {i}\n" for i in range(n))
    return '"""Shared module."""\nimport os\n' + body


class _Agent:
    """Answers after ``latency`` seconds, counting how many are answering."""

    def __init__(self, arm: str, latency: float) -> None:
        self._arm = arm
        self._latency = latency
        self._lock = threading.Lock()
        self.calls = 0

    def assign(self, adapter: object, bundle: TaskBundle) -> TaskResult:
        """Rewrite the task's function and ask for its module's import."""
        with self._lock:
            self.calls += 1
        time.sleep(self._latency)
        i = int(bundle.task_id[1:])
        module = MODULES[i]
        fn = NodeId(_function(i))
        sources = {fn: f"def f{i}():\n    return {module}.__name__\n"}
        imports: list[str] = []
        if self._arm == "header":
            header = bundle.context.get(f"write_source:{HEADER}", "")
            sources[NodeId(HEADER)] = header.rstrip("\n") + f"\nimport {module}\n"
        else:
            imports.append(f"import {module}")
        return TaskResult(
            task_id=bundle.task_id,
            success=True,
            modified_nodes=list(sources),
            new_sources=sources,
            imports=imports,
        )

    def shutdown(self) -> None:
        """Nothing to shut down."""


class _Adapter:
    agent_type = "scripted"


class _Registry:
    def get(self, agent_type: str) -> _Adapter:
        return _Adapter()


def _plan(arm: str, n: int) -> list[SubTask]:
    tasks = []
    for i in range(n):
        targets = [NodeId(_function(i))]
        if arm == "header":
            targets.append(NodeId(HEADER))
        tasks.append(SubTask(f"t{i}", f"use {MODULES[i]} in f{i}", targets))
    return tasks


def run_arm(arm: str, n: int, latency: float, workdir: Path) -> ArmResult:
    """Run ``n`` import-needing tasks through one arm and measure it."""
    if arm not in ARMS:
        raise ValueError(f"unknown arm {arm!r}")
    if not 1 <= n <= len(MODULES):
        raise ValueError(f"tasks must be 1..{len(MODULES)}")
    project = workdir / "project"
    project.mkdir(parents=True)
    (project / FILE).write_text(_project(n), encoding="utf-8")
    agent = _Agent(arm, latency)
    config = MakConfig(
        session=SessionConfig(
            work_dir=str(project), mak_dir=str(workdir / ".mak"),
            max_concurrent_agents=n,
        ),
        git=GitConfig(auto_commit=False, auto_push=False),
        node_store=NodeStoreConfig(),
    )
    session = Session(
        session_id=f"growth-{arm}",
        config=config,
        node_store=NodeStore(workdir / ".mak" / "node_store"),
        lock_table=LockTable(),
        registry=_Registry(),  # type: ignore[arg-type]
        agent_runner=agent,
        logger=SessionLogger(workdir / "log.jsonl"),
        collect_timeout_s=120.0,
    )
    session.initialize()
    session.install_plan(_plan(arm, n))
    start = time.monotonic()
    result = session.run()
    wall = time.monotonic() - start
    text = (project / FILE).read_text(encoding="utf-8")
    counts = [text.count(f"import {MODULES[i]}\n") for i in range(n)]
    return ArmResult(
        arm=arm,
        tasks=n,
        ok=result.ok,
        max_concurrency=result.metrics["max_concurrency"],
        mean_concurrency=round(result.metrics["mean_concurrency"], 2),
        wall_s=round(wall, 2),
        agent_calls=agent.calls,
        imports_once=sum(1 for c in counts if c == 1),
        imports_lost=sum(1 for c in counts if c == 0),
    )


def run(n: int, latency: float) -> list[ArmResult]:
    """Run both arms on the same workload."""
    results = []
    for arm in ARMS:
        with tempfile.TemporaryDirectory(prefix=f"mak-growth-{arm}-") as tmp:
            results.append(run_arm(arm, n, latency, Path(tmp)))
    return results


def main(argv: list[str] | None = None) -> int:
    """Run the benchmark and print a table (or JSON lines)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=int, default=8)
    parser.add_argument("--latency", type=float, default=0.3, help="seconds per call")
    parser.add_argument("--json", action="store_true", help="emit JSON lines")
    args = parser.parse_args(argv)
    results = run(args.tasks, args.latency)
    if args.json:
        for r in results:
            print(json.dumps(asdict(r)))
        return 0
    print(
        "| arm | tasks | max in flight | mean in flight | wall (s) | agent calls "
        "| imports once | imports lost |"
    )
    print("|---|---|---|---|---|---|---|---|")
    for r in results:
        print(
            f"| {r.arm} | {r.tasks} | {r.max_concurrency:g} | {r.mean_concurrency} "
            f"| {r.wall_s} | {r.agent_calls} | {r.imports_once} | {r.imports_lost} |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
