"""Run the semantic corpus through MAK and through a worktree baseline.

For each scenario:

- **MAK, full** — A and B planned together, B held until A commits when the
  kernel lets them run side by side. Classified by the earliest mechanism that
  handled the conflict: ``prevented`` (the kernel serialized the pair and the
  oracle passes), ``detected (plan)``, ``detected (commit)``, ``detected (wave
  end)``, or ``missed``. ``resolved`` says whether the oracle passes after the
  wave itself (before any fix-up wave).
- **MAK, A alone / B alone** — every rejection, stale-read re-dispatch or fix-up
  task in a single-edit run is a false positive; those runs also produce the
  "A only" and "B only" project states.
- **Worktrees** — three-way ``git merge-file`` of the two single-edit states
  against the base: a textual conflict is ``detected (textual conflict)``; a
  clean merge whose project tests fail is ``detected (CI tests)``; a clean merge
  that breaks the oracle is ``missed``.

``validate`` checks the corpus contract: the oracle passes on the base, A-only
and B-only states and fails on their naive combination (B's side first).
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from mak.config import (
    GitConfig,
    MakConfig,
    NodeStoreConfig,
    SemanticConfig,
    SessionConfig,
)
from mak.core.logging import EventType, LogEntry, SessionLogger
from mak.core.types import NodeId, SubTask, TaskBundle
from mak.lock_manager.lock_table import LockTable
from mak.node_store.store import NodeStore
from mak.session import Session
from semantic.scenarios import Edit, Scenario
from semantic.scripted import ScriptedAgent, wait_for_peer

_ORACLE_MODULE = "_semantic_oracle"
_PLAN_KINDS = frozenset(
    {"declared_api_dep", "shared_structure", "ordered_table", "registry_key_collision"}
)


@dataclass(frozen=True)
class MakRun:
    """What one MAK run did, and what it left on disk."""

    outcome: str
    resolved: bool
    mechanisms: tuple[str, ...]
    calls: int
    fixups: int
    false_positive_signals: int
    files: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class WorktreeRun:
    """What the worktree baseline did with the same two edits."""

    outcome: str
    textual_conflict: bool
    oracle_passes: bool


@dataclass(frozen=True)
class ScenarioResult:
    """One row of the semantic table."""

    scenario: Scenario
    mak: MakRun
    mak_gated: MakRun | None
    worktree: WorktreeRun
    false_positives: int
    extra_calls: int


class _Adapter:
    agent_type = "scripted"


class _Registry:
    def get(self, agent_type: str) -> _Adapter:
        return _Adapter()


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _read_tree(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*.py"))
        if ".mak" not in path.parts and "__pycache__" not in path.parts
    }


def run_oracle(files: dict[str, str], oracle: str) -> bool:
    """Whether ``check()`` passes against the project ``files``."""
    with tempfile.TemporaryDirectory(prefix="mak-oracle-") as tmp:
        root = Path(tmp)
        _write_tree(root, {**files, f"{_ORACLE_MODULE}.py": oracle})
        done = subprocess.run(
            [sys.executable, "-c", f"import {_ORACLE_MODULE} as o; o.check()"],
            cwd=root, capture_output=True, text=True, timeout=60, check=False,
            env={"PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1"},
        )
        return done.returncode == 0


def run_project_tests(files: dict[str, str]) -> bool:
    """Whether the project's own tests pass (True when it has none)."""
    tests = [p for p in files if p.rsplit("/", 1)[-1].startswith("test_")]
    if not tests:
        return True
    with tempfile.TemporaryDirectory(prefix="mak-ci-") as tmp:
        root = Path(tmp)
        _write_tree(root, files)
        done = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *tests],
            cwd=root, capture_output=True, text=True, timeout=120, check=False,
            env={"PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1"},
        )
        return done.returncode == 0


def _subtask(edit: Edit) -> SubTask:
    return SubTask(
        task_id=edit.task_id,
        description=edit.description,
        target_nodes=[NodeId(n) for n in edit.targets],
        context_nodes=[NodeId(n) for n in edit.context],
        **cast(dict[str, object], dict(edit.declarations)),  # type: ignore[arg-type]
    )


def run_mak(
    scenario: Scenario,
    edits: tuple[Edit, ...],
    workdir: Path,
    semantic: SemanticConfig | None = None,
) -> MakRun:
    """Run ``edits`` of ``scenario`` through a real MAK session."""
    project = workdir / "project"
    _write_tree(project, dict(scenario.files))
    logger = SessionLogger(workdir / "log.jsonl")
    agent = ScriptedAgent(
        {e.task_id: e.first for e in edits},
        {e.task_id: (e.retry or e.first) for e in edits},
    )
    if len(edits) == 2:
        agent.hold(edits[1].task_id, wait_for_peer(logger, edits[0].task_id))
    config = MakConfig(
        session=SessionConfig(
            work_dir=str(project), mak_dir=str(workdir / ".mak"),
            max_concurrent_agents=4,
        ),
        git=GitConfig(auto_commit=False, auto_push=False),
        node_store=NodeStoreConfig(),
        semantic=semantic or SemanticConfig(),
    )
    session = Session(
        session_id=f"semantic-{scenario.shape}",
        config=config,
        node_store=NodeStore(workdir / ".mak" / "node_store"),
        lock_table=LockTable(),
        registry=_Registry(),  # type: ignore[arg-type]
        agent_runner=agent,
        logger=logger,
        collect_timeout_s=60.0,
    )
    session.initialize()
    session.install_plan([_subtask(e) for e in edits])
    session.run()
    fixups = session.detect_cascade_tasks()
    files = _read_tree(project)
    log = logger.read_log()
    ids = [e.task_id for e in edits]
    mechanisms = _mechanisms(log, session, ids, fixups_found=bool(fixups))
    resolved = run_oracle(files, scenario.oracle)
    serialized = len(ids) == 2 and _serialized(log, ids[0], ids[1])
    return MakRun(
        outcome=_outcome(mechanisms, serialized=serialized, resolved=resolved),
        resolved=resolved,
        mechanisms=tuple(mechanisms),
        calls=sum(agent.calls.values()),
        fixups=len(fixups),
        false_positive_signals=_signal_count(log) + len(fixups),
        files=files,
    )


def _mechanisms(
    log: list[LogEntry], session: Session, ids: list[str], *, fixups_found: bool
) -> list[str]:
    found: list[str] = []
    plan = [
        f for f in session.last_plan_findings
        if f.kind in _PLAN_KINDS and f.task_id in ids
    ]
    if plan:
        found.append("plan:" + ",".join(sorted({f.kind for f in plan})))
    commit = sorted(_commit_signals(log))
    if commit:
        found.append("commit:" + ",".join(commit))
    if fixups_found:
        found.append("wave_end")
    return found


def _commit_signals(log: list[LogEntry]) -> set[str]:
    signals: set[str] = set()
    for entry in log:
        payload = entry.payload
        if entry.event_type is EventType.STALE_READ and payload.get("verdict") != "accept":
            signals.add("stale_read")
        elif entry.event_type is EventType.CONFLICT_DETECTED and "task_id" in payload:
            signals.add("rejected")
        elif entry.event_type is EventType.CONTRACT_VIOLATION:
            signals.add("contract")
        elif entry.event_type is EventType.API_ESCALATED and str(
            payload.get("outcome", "")
        ).startswith("refused"):
            signals.add("api_lock")
    return signals


def _signal_count(log: list[LogEntry]) -> int:
    """Kernel interventions in a run (each one in a single-edit run is an FP)."""
    return sum(
        1
        for entry in log
        if (entry.event_type is EventType.STALE_READ
            and entry.payload.get("verdict") != "accept")
        or (entry.event_type is EventType.CONFLICT_DETECTED and "task_id" in entry.payload)
        or entry.event_type is EventType.CONTRACT_VIOLATION
    )


def _serialized(log: list[LogEntry], first: str, second: str) -> bool:
    """Whether one task was dispatched only after the other completed."""

    def index(event: EventType, task_id: str) -> int | None:
        for i, entry in enumerate(log):
            if entry.event_type is event and entry.payload.get("task_id") == task_id:
                return i
        return None

    for early, late in ((first, second), (second, first)):
        done = index(EventType.TASK_COMPLETED, early)
        started = index(EventType.TASK_DISPATCHED, late)
        if done is not None and started is not None and started > done:
            return True
    return False


def _outcome(mechanisms: list[str], *, serialized: bool, resolved: bool) -> str:
    if serialized and resolved and not any(m.startswith("commit") for m in mechanisms):
        return "prevented"
    for prefix, label in (
        ("plan", "detected (plan)"),
        ("commit", "detected (commit)"),
        ("wave_end", "detected (wave end)"),
    ):
        if any(m.startswith(prefix) for m in mechanisms):
            return label
    return "clean" if resolved else "missed"


def merge_states(
    base: dict[str, str], a_only: dict[str, str], b_only: dict[str, str]
) -> tuple[dict[str, str], bool]:
    """Three-way merge per file (``git merge-file``); returns (union merge, conflict?).

    The returned merge resolves conflicts with ``--union``, B's side first —
    the uncoordinated combination the oracle must reject.
    """
    merged: dict[str, str] = {}
    conflict = False
    with tempfile.TemporaryDirectory(prefix="mak-merge-") as tmp:
        root = Path(tmp)
        for rel in sorted(set(base) | set(a_only) | set(b_only)):
            sides = [b_only.get(rel), base.get(rel, ""), a_only.get(rel)]
            if sides[0] is None and sides[2] is None:
                continue
            if sides[0] is None or sides[2] is None:
                merged[rel] = sides[0] if sides[0] is not None else cast(str, sides[2])
                continue
            paths = []
            for name, text in zip(("b", "base", "a"), sides, strict=True):
                path = root / name
                path.write_text(text or "", encoding="utf-8")
                paths.append(str(path))
            plain = subprocess.run(
                ["git", "merge-file", "-p", *paths], capture_output=True, text=True,
                check=False,
            )
            conflict = conflict or plain.returncode > 0
            union = subprocess.run(
                ["git", "merge-file", "-p", "--union", *paths], capture_output=True,
                text=True, check=False,
            )
            merged[rel] = union.stdout
    return merged, conflict


def worktree_state(scenario: Scenario, edit: Edit) -> dict[str, str]:
    """Return the project after ``edit`` alone, as a worktree would leave it.

    Text edits spliced into the base files by symbol span — exactly what the
    traditional benchmark runner does — so the three-way merge sees only the
    lines the edit really changed, not a reformatting of the whole file.
    """
    files = dict(scenario.files)
    context = {f"write_source:{n}": _node_source(files, n) or "" for n in edit.targets}
    bundle = TaskBundle(
        task_id=edit.task_id, description=edit.description,
        target_nodes=[NodeId(n) for n in edit.targets], context=context,
    )
    for node, answer in edit.first.items():
        text = answer(bundle) if callable(answer) else answer
        file_path, _, rest = node.partition("::")
        if not rest:
            files[file_path] = text
            continue
        files[file_path] = _splice(
            files.get(file_path, ""), rest.split("::", 1)[1], text
        )
    return files


def _span(tree: ast.Module, qualname: str) -> tuple[ast.stmt, int] | None:
    """Return the definition of ``qualname`` and its indentation, if present."""
    owner, _, member = qualname.partition(".")
    for stmt in tree.body:
        if not isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        if stmt.name != owner:
            continue
        if not member:
            return stmt, 0
        if isinstance(stmt, ast.ClassDef):
            for inner in stmt.body:
                if (
                    isinstance(inner, ast.FunctionDef | ast.AsyncFunctionDef)
                    and inner.name == member
                ):
                    return inner, inner.col_offset
    return None


def _node_source(files: dict[str, str], node: str) -> str | None:
    file_path, _, rest = node.partition("::")
    source = files.get(file_path)
    if source is None:
        return None
    if not rest:
        return source
    found = _span(ast.parse(source), rest.split("::", 1)[1])
    if found is None:
        return None
    stmt, _ = found
    lines = source.splitlines(keepends=True)
    start = min(
        [d.lineno for d in getattr(stmt, "decorator_list", [])], default=stmt.lineno
    )
    return textwrap.dedent("".join(lines[start - 1 : stmt.end_lineno]))


def _splice(source: str, qualname: str, new: str) -> str:
    found = _span(ast.parse(source), qualname)
    lines = source.splitlines(keepends=True)
    text = textwrap.indent(
        new if new.endswith("\n") else new + "\n", " " * (found[1] if found else 0)
    )
    if found is None:
        return source + ("\n\n" if source.strip() else "") + text
    stmt, _ = found
    start = min(
        [d.lineno for d in getattr(stmt, "decorator_list", [])], default=stmt.lineno
    )
    return "".join(lines[: start - 1]) + text + "".join(lines[stmt.end_lineno :])


def run_worktrees(
    scenario: Scenario, a_only: dict[str, str], b_only: dict[str, str]
) -> WorktreeRun:
    """Run the worktree baseline: merge the two single-edit states, then CI."""
    merged, conflict = merge_states(dict(scenario.files), a_only, b_only)
    oracle_passes = run_oracle(merged, scenario.oracle)
    if conflict:
        outcome = "detected (textual conflict)"
    elif not run_project_tests(merged):
        outcome = "detected (CI tests)"
    elif not oracle_passes:
        outcome = "missed"
    else:
        outcome = "clean"
    return WorktreeRun(outcome, conflict, oracle_passes)


def validate(scenario: Scenario, a_only: dict[str, str], b_only: dict[str, str]) -> list[str]:
    """Problems with the scenario itself (empty when it is a valid test case)."""
    problems: list[str] = []
    base = dict(scenario.files)
    for label, files in (("base", base), ("A only", a_only), ("B only", b_only)):
        if not run_oracle(files, scenario.oracle):
            problems.append(f"oracle fails on {label}")
    merged, _ = merge_states(base, a_only, b_only)
    if run_oracle(merged, scenario.oracle):
        problems.append("oracle passes on the naive A+B combination")
    return problems


def evaluate(scenario: Scenario, *, with_gates: bool = True) -> ScenarioResult:
    """Run one scenario through every arm."""
    with tempfile.TemporaryDirectory(prefix=f"mak-sem-{scenario.shape}-") as tmp:
        root = Path(tmp)
        a_alone = run_mak(scenario, (scenario.a,), root / "a")
        b_alone = run_mak(scenario, (scenario.b,), root / "b")
        full = run_mak(scenario, (scenario.a, scenario.b), root / "full")
        gated = None
        if with_gates and scenario.needs:
            gated = run_mak(
                scenario, (scenario.a, scenario.b), root / "gated",
                SemanticConfig(impact_tests="impact_tests" in scenario.needs),
            )
        worktree = run_worktrees(
            scenario, worktree_state(scenario, scenario.a),
            worktree_state(scenario, scenario.b),
        )
        shutil.rmtree(root, ignore_errors=True)
    return ScenarioResult(
        scenario=scenario,
        mak=full,
        mak_gated=gated,
        worktree=worktree,
        false_positives=a_alone.false_positive_signals + b_alone.false_positive_signals,
        extra_calls=full.calls - 2,
    )


def single_states(scenario: Scenario) -> tuple[dict[str, str], dict[str, str]]:
    """Return the A-only and B-only project states (text edits on the base)."""
    return worktree_state(scenario, scenario.a), worktree_state(scenario, scenario.b)
