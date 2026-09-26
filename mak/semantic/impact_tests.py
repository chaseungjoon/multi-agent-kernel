"""Impacted tests with pairwise attribution.

The research definition of a semantic merge conflict is a test that passes with
change A alone and with change B alone, and fails with A and B together. Git
cannot evaluate that cheaply; MAK can, because any subset of a wave's commits
can be materialized from the node store (:mod:`mak.semantic.overlay`).

At wave end:

1. **select** the tests that import — directly or transitively — a module the
   wave touched (static import-graph selection; no coverage run needed);
2. run them on the wave's end state and on the pre-wave state; a test that
   fails now and passed before is a **new failure**;
3. **attribute** each new failure: first to a single task (it also fails with
   only that task's commits applied), then to a *pair* (it passes with each
   alone and fails with both — the semantic conflict proper). Overlays are
   budgeted by ``semantic.impact_max_overlays``; what the budget cannot
   attribute is reported against every task that touched an imported module.
"""

from __future__ import annotations

import itertools
import tempfile
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from mak.core.exceptions import SemanticGateError
from mak.semantic.gate_types import (
    GateFinding,
    ProcessRunner,
    WaveView,
    python,
    run_process,
)
from mak.semantic.overlay import overlay
from mak.semantic.project_files import importers_of, is_test_file, python_sources


def select_tests(sources: dict[str, str], touched: list[str]) -> list[str]:
    """Test files whose import closure reaches a touched module."""
    reached = importers_of(sources, touched) | set(touched)
    return sorted(p for p in reached if is_test_file(p) and p in sources)


def run_tests(
    root: Path, tests: list[str], runner: ProcessRunner, timeout_s: float
) -> set[str]:
    """Run ``tests`` under pytest in ``root``; return the ids that failed.

    Tests absent from this state (a test the wave itself added, in the
    pre-wave or a single-task overlay) are simply not run.
    """
    tests = [t for t in tests if (root / t).exists()]
    if not tests:
        return set()
    with tempfile.TemporaryDirectory(prefix="mak-junit-") as tmp:
        report = Path(tmp) / "junit.xml"
        try:
            runner(
                [
                    python(), "-m", "pytest", "-q", "-p", "no:cacheprovider",
                    f"--junitxml={report}", "--rootdir", str(root), *tests,
                ],
                root,
                timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 - reported, never raised past the gate
            raise SemanticGateError(f"pytest could not run: {exc}") from exc
        if not report.exists():
            raise SemanticGateError("pytest produced no report (is pytest installed?)")
        return _failed_ids(report)


def _failed_ids(report: Path) -> set[str]:
    try:
        tree = ElementTree.parse(report)
    except ElementTree.ParseError as exc:
        raise SemanticGateError(f"unreadable pytest report: {exc}") from exc
    failed: set[str] = set()
    for case in tree.iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            failed.add(f"{case.get('classname', '')}::{case.get('name', '')}")
    return failed


def impact_tests(
    view: WaveView, runner: ProcessRunner = run_process
) -> list[GateFinding]:
    """Report tests the wave broke, attributed to a task or a task pair."""
    touched = [p for p in sorted(view.before) if view.current(p) is not None]
    with overlay(view.work_dir, view.after()) as root:
        tests = select_tests(python_sources(root), touched)
        if not tests:
            return []
        failing_now = run_tests(root, tests, runner, view.timeout_s)
    if not failing_now:
        return []
    with overlay(view.work_dir, dict(view.before)) as root:
        failing_before = run_tests(root, tests, runner, view.timeout_s)
    new = failing_now - failing_before
    if not new:
        return []
    blame = _attribute(view, tests, new, runner)
    return [_finding(view, test, culprits) for test, culprits in sorted(blame.items())]


def _attribute(
    view: WaveView, tests: list[str], new: set[str], runner: ProcessRunner
) -> dict[str, tuple[str, ...]]:
    """Map each new failure to the smallest task set reproducing it."""
    blame: dict[str, tuple[str, ...]] = {}
    budget = view.max_overlays
    for task in view.tasks:
        if budget <= 0 or len(blame) == len(new):
            break
        budget -= 1
        failing = _run_subset(view, tests, frozenset({task}), runner)
        for test in (new & failing) - set(blame):
            blame[test] = (task,)
    for first, second in itertools.combinations(view.tasks, 2):
        remaining = new - set(blame)
        if budget <= 0 or not remaining:
            break
        budget -= 1
        failing = _run_subset(view, tests, frozenset({first, second}), runner)
        for test in remaining & failing:
            blame[test] = (first, second)
    for test in new - set(blame):
        blame[test] = view.tasks
    return blame


def _run_subset(
    view: WaveView, tests: list[str], tasks: frozenset[str], runner: ProcessRunner
) -> set[str]:
    with overlay(view.work_dir, view.subset(tasks)) as root:
        return run_tests(root, tests, runner, view.timeout_s)


def _finding(view: WaveView, test: str, culprits: tuple[str, ...]) -> GateFinding:
    if len(culprits) == 1:
        how = f"fails with task {culprits[0]}'s commits alone"
    elif len(culprits) == 2:
        how = (
            f"passes with task {culprits[0]} alone and with task {culprits[1]} "
            "alone, and fails with both — a semantic conflict between them"
        )
    else:
        how = f"could not be narrowed below tasks {', '.join(culprits)}"
    targets = tuple(dict.fromkeys(n for n in view.task_nodes(culprits[-1])))
    context = tuple(
        dict.fromkeys(n for t in culprits[:-1] for n in view.task_nodes(t))
    )
    return GateFinding(
        gate="impact_tests",
        file=test.split("::", 1)[0],
        detail=f"test '{test}' passed before this wave and fails now; it {how}",
        targets=targets,
        context=context,
        tasks=culprits,
    )
