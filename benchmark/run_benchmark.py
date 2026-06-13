"""Run the MAK-vs-worktree benchmark and write the results.

Usage (from the repository root)::

    python benchmark/run_benchmark.py --mode mock                 # keyless self-test (all projects)
    python benchmark/run_benchmark.py --mode real                 # real models, all projects
    python benchmark/run_benchmark.py --mode real --project 2      # just the heavy project
    python benchmark/run_benchmark.py --mode real --models anthropic:claude-sonnet-4-6 \\
        openai:gpt-4o gemini:gemini-3-pro

There are two workloads: ``basic`` (9 ops, 3 modules) and ``2`` (90 ops, 9 modules of
real-utility-style functions). Each runs both coordination models — MAK and git
worktrees — on a fresh copy with the *same* agents and assignment; only coordination
differs.
Every project's last run is saved separately, so the reports
(``benchmark/README.md`` summary + ``benchmark/STATS.md`` detail) show one labelled
section per project and a heavier run never overwrites a lighter one's numbers.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import os
import shutil
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent
for path in (str(BENCH), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)


def _load_env() -> None:
    """Load ``benchmark/.env`` (KEY=VALUE lines) into the environment, if present.

    No external dependency; existing environment variables win, so an exported key
    overrides the file.
    """
    env_path = BENCH / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_env()

from harness.agents import AgentSpec, Usage, make_backends  # noqa: E402
from harness.mak_runner import run_mak  # noqa: E402
from harness.metrics import RunResult  # noqa: E402
from harness.report import (  # noqa: E402
    ProjectRun,
    RunMeta,
    inject_readme,
    render_readme_results,
    render_stats,
)
from harness.traditional import run_traditional  # noqa: E402
from harness.workload import WORKLOADS, Workload, assign  # noqa: E402

# Render order: lighter project first, heavier second.
_PROJECT_ORDER = ["basic", "2"]
_LEGACY_RUN = BENCH / ".last_run.json"


def _run_path(project: str) -> Path:
    return BENCH / f".last_run.{project}.json"


def _save_run(project: str, mak: RunResult, trad: RunResult, meta: RunMeta) -> None:
    _run_path(project).write_text(json.dumps({
        "meta": dataclasses.asdict(meta),
        "mak": dataclasses.asdict(mak),
        "trad": dataclasses.asdict(trad),
    }, indent=2))


def _load_run(path: Path) -> ProjectRun:
    data = json.loads(path.read_text())
    meta = RunMeta(**data["meta"])

    def _result(d: dict) -> RunResult:
        d = dict(d)
        d["usage"] = Usage(**d.pop("usage"))
        d.setdefault("total", meta.tests)  # legacy runs stored no per-result total
        d.pop("accuracy", None)
        return RunResult(**d)

    return ProjectRun(meta=meta, mak=_result(data["mak"]), trad=_result(data["trad"]))


def _migrate_legacy() -> None:
    """Seed ``.last_run.basic.json`` from the pre-multiproject ``.last_run.json``."""
    if _legacy_basic_missing():
        shutil.copyfile(_LEGACY_RUN, _run_path("basic"))


def _legacy_basic_missing() -> bool:
    return _LEGACY_RUN.exists() and not _run_path("basic").exists()


def _collect_runs() -> list[ProjectRun]:
    """Load every saved project run, in render order."""
    _migrate_legacy()
    runs: list[ProjectRun] = []
    for project in _PROJECT_ORDER:
        path = _run_path(project)
        if path.exists():
            runs.append(_load_run(path))
    return runs


def _write_reports() -> None:
    runs = _collect_runs()
    if not runs:
        raise SystemExit("no saved runs to render")
    readme_path = BENCH / "README.md"
    readme_path.write_text(
        inject_readme(readme_path.read_text(), render_readme_results(runs))
    )
    (BENCH / "STATS.md").write_text(render_stats(runs))
    print(f"[benchmark] wrote {readme_path.name} and STATS.md "
          f"({len(runs)} project section(s))")


_DEFAULT_SPECS = [
    AgentSpec("claude", "anthropic", "claude-sonnet-4-6"),
    AgentSpec("gpt", "openai", "gpt-4o"),
    AgentSpec("gemini", "gemini", "gemini-3-pro"),
]


def _parse_specs(raw: list[str] | None, num_agents: int) -> list[AgentSpec]:
    if not raw:
        pairs = [(s.provider, s.model) for s in _DEFAULT_SPECS[:num_agents]]
    else:
        pairs = []
        for item in raw:
            provider, _, model = item.partition(":")
            if not model:
                raise SystemExit(f"--models entry must be provider:model, got {item!r}")
            pairs.append((provider, model))
    # Index the name so two agents on the *same* model stay distinct.
    return [
        AgentSpec(f"agent{i}-{model}", provider, model)
        for i, (provider, model) in enumerate(pairs)
    ]


def _fresh_copy(template: Path, dest: Path) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(template, dest, ignore=shutil.ignore_patterns("__pycache__"))
    return dest


def _run_project(
    workload: Workload, specs: list[AgentSpec], mode: str, runs_root: Path
) -> None:
    num_agents = len(specs)
    assignment = assign(workload, num_agents)
    mock = mode == "mock"
    template = BENCH / workload.template

    print(f"\n[benchmark] === project '{workload.name}' ({workload.label}) ===")
    print(f"[benchmark] mode={mode} agents={num_agents} "
          f"models={[s.model for s in specs]}")

    print("[benchmark] running MAK (shared-memory kernel) ...")
    mak_result = run_mak(
        _fresh_copy(template, runs_root / workload.name / "mak" / "project"),
        runs_root / workload.name / "mak" / "mak_state",
        make_backends(specs, mock=mock),
        assignment,
        workload,
    )
    print(f"[benchmark]   MAK: {mak_result.accuracy:.0%} accuracy, "
          f"{mak_result.usage.calls} calls, {mak_result.wall_seconds:.2f}s")

    print("[benchmark] running Traditional (git worktrees) ...")
    trad_result = run_traditional(
        _fresh_copy(template, runs_root / workload.name / "traditional" / "project"),
        runs_root / workload.name / "traditional" / "worktrees",
        make_backends(specs, mock=mock),
        assignment,
        workload,
    )
    print(f"[benchmark]   Traditional: {trad_result.accuracy:.0%} accuracy, "
          f"{trad_result.usage.calls} calls, {trad_result.conflicts} conflicts")

    meta = RunMeta(
        mode=mode,
        num_agents=num_agents,
        models=[s.model for s in specs],
        timestamp=datetime.datetime.now().isoformat(timespec="seconds"),
        operations=len(workload.operations),
        tests=workload.expected_tests,
        project=workload.name,
        label=workload.label,
        modules=len(workload.modules),
    )
    _save_run(workload.name, mak_result, trad_result, meta)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MAK vs git-worktree benchmark")
    parser.add_argument("--mode", choices=("mock", "real"), default="mock")
    parser.add_argument("--agents", type=int, default=3)
    parser.add_argument("--project", choices=("basic", "2", "all"), default="all",
                        help="which workload to run (default: all)")
    parser.add_argument("--models", nargs="*", default=None,
                        help="provider:model entries; overrides the defaults")
    parser.add_argument("--keep", action="store_true",
                        help="keep the .runs working copies for inspection")
    parser.add_argument("--render-only", action="store_true",
                        help="re-render the reports from the last runs (no model calls)")
    args = parser.parse_args(argv)

    if args.render_only:
        _write_reports()
        return 0

    specs = _parse_specs(args.models, args.agents)
    projects = _PROJECT_ORDER if args.project == "all" else [args.project]
    runs_root = BENCH / ".runs"
    shutil.rmtree(runs_root, ignore_errors=True)  # clear any stale copies from a crash

    for project in projects:
        _run_project(WORKLOADS[project], specs, args.mode, runs_root)

    _write_reports()

    if not args.keep:
        shutil.rmtree(runs_root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
