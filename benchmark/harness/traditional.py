"""Run the workload the *traditional* way: one git worktree per agent, then merge.

Each agent gets its own branch + worktree, implements its assigned module(s) and
appends its ``register(...)`` lines to ``_register_all`` there, and commits. The
branches are then merged one by one. Module files merge cleanly (each is owned by a
single agent), but ``_register_all`` was edited in every branch, so every merge
after the first collides there and must be resolved — the cost MAK does not pay.

Timing model: the agents implement in parallel, so the implementation phase is
charged as ``max`` over agents of that agent's total call time (not the sum); the
sequential merge+resolve phase is charged as measured wall-clock. This is the fair
parallel model — see ``benchmark/README.md``.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import time
from pathlib import Path

from harness.accuracy import measure
from harness.agents import Backend, Usage, _union_registry
from harness.metrics import RunResult
from harness.workload import Workload, add_registration


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=check, capture_output=True, text=True
    )


def _func_span(source: str, name: str) -> tuple[int, int]:
    """Return the 0-based [start, end) line span of top-level function ``name``."""
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            start = min([d.lineno for d in node.decorator_list], default=node.lineno) - 1
            assert node.end_lineno is not None
            return start, node.end_lineno
    raise KeyError(name)


def _extract_function(source: str, name: str) -> str:
    start, end = _func_span(source, name)
    return "".join(source.splitlines(keepends=True)[start:end])


def _splice_function(source: str, name: str, new_source: str) -> str:
    start, end = _func_span(source, name)
    lines = source.splitlines(keepends=True)
    body = new_source if new_source.endswith("\n") else new_source + "\n"
    return "".join(lines[:start]) + body + "".join(lines[end:])


def run_traditional(
    project_dir: Path,
    worktree_root: Path,
    backends: list[Backend],
    assignment: list[int],
    workload: Workload,
) -> RunResult:
    """Implement the workload via git worktrees + merge; return measured results."""
    operations = workload.operations
    _git(["init", "-q"], project_dir)
    _git(["config", "user.email", "benchmark@mak.local"], project_dir)
    _git(["config", "user.name", "MAK Benchmark"], project_dir)
    _git(["add", "-A"], project_dir)
    _git(["commit", "-q", "-m", "base: stubs"], project_dir)
    base = _git(["branch", "--show-current"], project_dir).stdout.strip()

    ops_for = {i: [operations[k] for k in range(len(operations)) if assignment[k] == i]
               for i in range(len(backends))}

    # -- implementation phase (agents work in parallel, in their own worktrees) --
    agent_seconds: dict[str, float] = {}
    calls_by_agent: dict[str, int] = {}
    total_usage = Usage()
    branches: list[str] = []
    notes: list[str] = []

    for i, backend in enumerate(backends):
        if not ops_for[i]:
            continue
        branch = f"agent-{i}"
        worktree = worktree_root / branch
        _git(["worktree", "add", "-q", "-b", branch, str(worktree), base], project_dir)
        branches.append(branch)
        elapsed = 0.0
        for op in ops_for[i]:
            module_path = worktree / f"toolkit/{op.module}.py"
            module_src = module_path.read_text()
            stub = _extract_function(module_src, op.func)
            start = time.monotonic()
            try:
                func_source, usage = backend.implement(op, stub)
            except Exception as exc:  # a failed agent call leaves the stub in place
                elapsed += time.monotonic() - start
                notes.append(f"{backend.name} failed to implement {op.name}: {exc}")
                continue
            elapsed += time.monotonic() - start
            # The call happened and cost tokens regardless of whether its output is
            # usable, so count it before deciding whether to keep the result.
            total_usage = total_usage + usage
            calls_by_agent[backend.name] = calls_by_agent.get(backend.name, 0) + usage.calls

            # Reject a malformed implementation rather than write unparseable Python
            # into the module (which would crash every later step). The stub stays in
            # place, so only this op's tests fail — the same way MAK isolates a bad
            # node instead of corrupting the whole file.
            try:
                new_module = _splice_function(module_src, op.func, func_source)
                ast.parse(new_module)
            except SyntaxError as exc:
                notes.append(f"{backend.name} produced unparseable {op.name}: {exc}")
                continue
            module_path.write_text(new_module)

            reg_path = worktree / "toolkit/registry.py"
            reg_src = reg_path.read_text()
            current = _extract_function(reg_src, "_register_all")
            updated = add_registration(current, op.register_line)
            reg_path.write_text(_splice_function(reg_src, "_register_all", updated))
        _git(["add", "-A"], worktree)
        _git(["commit", "-q", "-m", branch], worktree)
        agent_seconds[backend.name] = elapsed

    parallel_seconds = max(agent_seconds.values(), default=0.0)

    # -- merge phase (sequential; _register_all collides every time but the first) --
    base_registry = _git(["show", f"{base}:toolkit/registry.py"], project_dir).stdout
    resolver = backends[0]
    conflicts = 0
    resolutions = 0
    merge_start = time.monotonic()
    for branch in branches:
        merged = _git(["merge", "--no-edit", branch], project_dir, check=False)
        if merged.returncode == 0:
            continue
        conflicted = _git(
            ["diff", "--name-only", "--diff-filter=U"], project_dir
        ).stdout.split()
        conflicts += 1
        if conflicted != ["toolkit/registry.py"]:
            notes.append(f"unexpected conflicts: {conflicted}")
        reg_path = project_dir / "toolkit" / "registry.py"
        try:
            merged_register_all, usage = resolver.resolve([reg_path.read_text()])
        except Exception as exc:  # fall back to a deterministic union so the run finishes
            notes.append(f"{resolver.name} failed to resolve conflict: {exc}")
            merged_register_all = _union_registry([reg_path.read_text()])
            usage = Usage(calls=1)
        resolutions += 1
        total_usage = total_usage + usage
        calls_by_agent[resolver.name] = calls_by_agent.get(resolver.name, 0) + usage.calls
        reg_path.write_text(
            _splice_function(base_registry, "_register_all", merged_register_all)
        )
        _git(["add", "toolkit/registry.py"], project_dir)
        # If other files conflicted, take the merged-in side to avoid wedging.
        for path in conflicted:
            if path != "toolkit/registry.py":
                _git(["checkout", "--theirs", path], project_dir, check=False)
                _git(["add", path], project_dir)
        _git(["commit", "-q", "--no-edit"], project_dir)
    merge_seconds = time.monotonic() - merge_start

    for branch in branches:
        _git(["worktree", "remove", "--force", str(worktree_root / branch)],
             project_dir, check=False)

    print("[traditional] merge done; measuring accuracy (pytest) ...",
          file=sys.stderr, flush=True)
    passed = measure(project_dir)
    return RunResult(
        label="Traditional (git worktrees)",
        wall_seconds=parallel_seconds + merge_seconds,
        usage=total_usage,
        passed=passed,
        total=workload.expected_tests,
        conflicts=conflicts,
        resolutions=resolutions,
        per_agent_calls=calls_by_agent,
        notes=notes,
    )
