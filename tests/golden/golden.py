"""Golden event logs: prove a refactor of the kernel changed no behaviour.

Each scenario drives a real :class:`~mak.session.Session` over a fixed project and
records what it did: per task, the ordered ``(event type, payload)`` sequence the
session logged, plus the final result. Timings, durations and anything else that
depends on thread scheduling rather than on the kernel's decisions are removed,
so two runs of the same tree produce the same record.

    python -m tests.golden.golden record     # rewrite tests/golden/data/
    python -m tests.golden.golden compare    # exit 1 and describe any difference

Scenarios:

- ``concurrency/*`` — the overlapping corpus of
  ``tests/test_concurrency_integration.py`` (18 contending tasks; two tasks on one
  node);
- ``semantic/<shape>/<arm>`` — every seeded semantic-conflict scenario of
  ``benchmark/semantic`` through the MAK arms: A alone, B alone, both, and the
  gated run where the scenario needs one;
- ``benchmark/<template>`` — the MAK arm of every benchmark template in mock
  mode, Template 4 with its mock plan.

Events are grouped by ``task_id`` because tasks run concurrently: the order in
which two tasks' events interleave is a property of the thread pool, while the
order of one task's own events is a property of the kernel.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

from mak.config import GitConfig, MakConfig, NodeStoreConfig, SessionConfig
from mak.core.logging import EventType, LogEntry, SessionLogger
from mak.lock_manager.lock_table import LockTable
from mak.node_store.store import NodeStore
from mak.session import Session, SessionResult

REPO = Path(__file__).resolve().parents[2]
BENCH = REPO / "benchmark"
DATA = Path(__file__).resolve().parent / "data"

# Payload keys whose values are wall-clock measurements or identifiers of the run.
_VOLATILE_KEYS = frozenset({"duration_seconds", "session_id"})
# Plan metrics sampled from the thread pool's instantaneous occupancy.
_VOLATILE_METRICS = frozenset({"max_concurrency", "mean_concurrency"})
# The overlap corpus races sibling commits against each dispatch on purpose, so
# what a bundle carried and which siblings moved under it before its commit are
# properties of the thread schedule there. Its record keeps the kernel's
# decisions (dispatch, result, commit, completion) and drops those measurements.
_RACY_EVENTS = frozenset({EventType.STALE_READ})
_RACY_KEYS = frozenset({"context_bytes", "layers", "read_sources"})
_RACY_METRICS = frozenset({"context_bytes_total", "mean_context_bytes", "stale_reads"})
# Benchmark result fields that are timings or byte counts of on-disk state.
_BENCH_FIELDS = (
    "passed",
    "total",
    "conflicts",
    "resolutions",
    "notes",
    "registration_expected",
    "registration_survived",
    "registration_dropped",
    "registration_duplicates",
)

Record = dict[str, object]


@dataclass(frozen=True)
class Scenario:
    """One recorded run: a name and a function producing its record."""

    name: str
    run: Callable[[Path], Record]


def normalize_log(
    entries: list[LogEntry], roots: tuple[str, ...], *, racy: bool = False
) -> Record:
    """Group events by task, drop volatile keys, and scrub temporary paths."""
    dropped_keys = _VOLATILE_KEYS | (_RACY_KEYS if racy else frozenset())
    dropped_metrics = _VOLATILE_METRICS | (_RACY_METRICS if racy else frozenset())
    groups: dict[str, list[object]] = {}
    for entry in entries:
        if racy and entry.event_type in _RACY_EVENTS:
            continue
        payload = {
            key: _scrub(value, roots)
            for key, value in entry.payload.items()
            if key not in dropped_keys
        }
        if entry.event_type is EventType.PLAN_METRICS:
            payload = {k: v for k, v in payload.items() if k not in dropped_metrics}
        owner = str(payload.get("task_id", "_session"))
        groups.setdefault(owner, []).append([entry.event_type.value, payload])
    return dict(sorted(groups.items()))


def normalize_result(
    result: SessionResult, roots: tuple[str, ...], *, racy: bool = False
) -> Record:
    """Return a session result without its timing-dependent parts."""
    dropped_metrics = _VOLATILE_METRICS | (_RACY_METRICS if racy else frozenset())
    return {
        "state": result.state.value,
        "completed": sorted(result.completed),
        "failed": sorted(result.failed),
        "blocked": sorted(result.blocked),
        "skipped": sorted(result.skipped),
        "noop": sorted(result.noop),
        "failure_reasons": _scrub(dict(sorted(result.failure_reasons.items())), roots),
        "stopped_reason": result.stopped_reason,
        "metrics": {
            k: v for k, v in sorted(result.metrics.items())
            if k not in dropped_metrics
        },
    }


def _scrub(value: object, roots: tuple[str, ...]) -> object:
    """Replace every temporary root inside ``value`` with a fixed placeholder."""
    if isinstance(value, str):
        for root in roots:
            value = value.replace(root, "<ROOT>")
        return value
    if isinstance(value, list | tuple):
        return [_scrub(v, roots) for v in value]
    if isinstance(value, dict):
        return {str(k): _scrub(v, roots) for k, v in value.items()}
    return value


def _roots(path: Path) -> tuple[str, ...]:
    """Both spellings of a temporary directory (macOS links /var to /private/var)."""
    return tuple(sorted({str(path.resolve()), str(path)}, key=len, reverse=True))


# -- concurrency corpus ------------------------------------------------------------


def _concurrency_config(root: Path) -> MakConfig:
    return MakConfig(
        session=SessionConfig(
            work_dir=str(root),
            mak_dir=str(root / ".mak"),
            max_concurrent_agents=4,
            deadlock_check_interval_s=0.0,
        ),
        git=GitConfig(auto_commit=False, auto_push=False),
        node_store=NodeStoreConfig(),
    )


def _run_concurrency(root: Path, *, solo: bool) -> Record:
    from mak.core.types import NodeId, SubTask
    from tests import test_concurrency_integration as corpus

    if solo:
        (root / "solo.py").write_text("def only():\n    return 0\n")
        node = NodeId("solo.py::function::only")
        plan = [
            SubTask(task_id=t, description="d", target_nodes=[node], agent_type="fake")
            for t in ("w1", "w2")
        ]
    else:
        for file in corpus._FILES:
            (root / file).write_text(corpus._file_source())
        plan = corpus._overlapping_plan()
    store = NodeStore(root / ".mak" / "node_store")
    lock_table = LockTable()
    runner = corpus._ContendedRunner(store, lock_table, [], {}, threading.Lock())
    logger = SessionLogger(root / ".mak" / "log.jsonl")
    session = Session(
        session_id="golden-concurrency",
        config=_concurrency_config(root),
        node_store=store,
        lock_table=lock_table,
        registry=corpus._Registry(),  # type: ignore[arg-type]
        agent_runner=runner,  # type: ignore[arg-type]
        logger=logger,
    )
    session.initialize()
    session.install_plan(plan)
    result = session.run()
    roots = _roots(root)
    racy = not solo
    return {
        "result": normalize_result(result, roots, racy=racy),
        "events": normalize_log(logger.read_log(), roots, racy=racy),
    }


# -- semantic corpus ---------------------------------------------------------------


def _semantic_scenarios() -> list[Scenario]:
    sys.path.insert(0, str(BENCH))
    from semantic.scenarios import SCENARIOS

    found: list[Scenario] = []
    for scenario in SCENARIOS:
        arms: list[tuple[str, tuple[object, ...], bool]] = [
            ("a", (scenario.a,), False),
            ("b", (scenario.b,), False),
            ("full", (scenario.a, scenario.b), False),
        ]
        if scenario.needs:
            arms.append(("gated", (scenario.a, scenario.b), True))
        for arm, edits, gated in arms:
            found.append(Scenario(
                f"semantic/{scenario.shape}/{arm}",
                _semantic_runner(scenario, edits, gated),
            ))
    return found


def _semantic_runner(
    scenario: object, edits: tuple[object, ...], gated: bool
) -> Callable[[Path], Record]:
    def run(root: Path) -> Record:
        from semantic.evaluate import run_mak

        from mak.config import SemanticConfig

        semantic = None
        if gated:
            needs = scenario.needs  # type: ignore[attr-defined]
            semantic = SemanticConfig(impact_tests="impact_tests" in needs)
        outcome = run_mak(scenario, edits, root, semantic)  # type: ignore[arg-type]
        log = SessionLogger(root / "log.jsonl").read_log()
        roots = _roots(root)
        return {
            "outcome": outcome.outcome,
            "resolved": outcome.resolved,
            "mechanisms": list(outcome.mechanisms),
            "calls": outcome.calls,
            "fixups": outcome.fixups,
            "files": outcome.files,
            "events": normalize_log(log, roots),
        }

    return run


# -- benchmark templates (mock mode) -----------------------------------------------


def _benchmark_runner(project: str) -> Callable[[Path], Record]:
    def run(root: Path) -> Record:
        import shutil

        sys.path.insert(0, str(BENCH))
        import run_benchmark as cli
        from harness.agents import make_backends
        from harness.mak_runner import run_mak
        from harness.planner import apply_plan, make_plan
        from harness.workload import WORKLOADS, assign

        workload = WORKLOADS[project]
        specs = cli._parse_specs(None, 3, project)
        assignment = assign(workload, len(specs))
        if project == "4":
            plan = make_plan(
                workload, BENCH / workload.template, len(specs), specs[0], mock=True
            )
            workload, assignment = apply_plan(workload, plan)
        project_dir = root / "project"
        shutil.copytree(
            BENCH / workload.template,
            project_dir,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        mak_dir = root / "mak_state"
        result = run_mak(
            project_dir, mak_dir, make_backends(specs, mock=True), assignment, workload
        )
        roots = _roots(root)
        return {
            "result": {name: getattr(result, name) for name in _BENCH_FIELDS},
            "events": normalize_log(
                SessionLogger(mak_dir / "events.jsonl").read_log(), roots
            ),
        }

    return run


def scenarios() -> list[Scenario]:
    """Every golden scenario, in a stable order."""
    return [
        Scenario("concurrency/overlap", lambda r: _run_concurrency(r, solo=False)),
        Scenario("concurrency/solo", lambda r: _run_concurrency(r, solo=True)),
        *_semantic_scenarios(),
        *(
            Scenario(f"benchmark/{p}", _benchmark_runner(p))
            for p in ("basic", "2", "3", "4")
        ),
    ]


# -- record / compare --------------------------------------------------------------


@contextmanager
def _quiet() -> Iterator[None]:
    """Silence the harness's progress output; the record is what matters."""
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        yield


def _golden_path(name: str) -> Path:
    return DATA / (name.replace("/", "__") + ".json")


def produce(scenario: Scenario) -> Record:
    """Run one scenario in a fresh temporary directory and return its record."""
    with tempfile.TemporaryDirectory(prefix="mak-golden-") as tmp, _quiet():
        return scenario.run(Path(tmp))


def _dump(record: Record) -> str:
    return json.dumps(record, indent=1, sort_keys=True) + "\n"


def record(selected: list[Scenario]) -> None:
    """Write the golden record of every selected scenario."""
    DATA.mkdir(parents=True, exist_ok=True)
    for scenario in selected:
        _golden_path(scenario.name).write_text(_dump(produce(scenario)))
        print(f"recorded {scenario.name}")


def compare(selected: list[Scenario]) -> list[str]:
    """Return one description per scenario whose run differs from its golden."""
    problems: list[str] = []
    for scenario in selected:
        path = _golden_path(scenario.name)
        if not path.exists():
            problems.append(f"{scenario.name}: no golden recorded")
            continue
        expected = path.read_text()
        actual = _dump(produce(scenario))
        if actual == expected:
            print(f"ok       {scenario.name}")
            continue
        diff = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=f"{scenario.name} (golden)",
                tofile=f"{scenario.name} (now)",
                n=2,
            )
        )
        print(f"CHANGED  {scenario.name}")
        problems.append(f"{scenario.name}:\n{diff[:6000]}")
    return problems


def main(argv: list[str] | None = None) -> int:
    """Record or compare the golden event logs."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("command", choices=("record", "compare"))
    parser.add_argument(
        "--only", default="", help="run only scenarios whose name starts with this"
    )
    args = parser.parse_args(argv)
    selected = [s for s in scenarios() if s.name.startswith(args.only)]
    if args.command == "record":
        record(selected)
        return 0
    problems = compare(selected)
    for problem in problems:
        print(problem)
    print(f"{len(selected) - len(problems)}/{len(selected)} scenarios reproduce")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
