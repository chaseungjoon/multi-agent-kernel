"""Run the MAK-vs-worktree benchmark and write the results.

Usage (from the repository root)::

    python benchmark/run_benchmark.py --mode mock          # keyless self-test
    python benchmark/run_benchmark.py --mode real          # real models (needs keys)
    python benchmark/run_benchmark.py --mode real --models anthropic:claude-sonnet-4-6 \\
        openai:gpt-4o gemini:gemini-3-pro

Both runners get a fresh copy of ``project_template`` and the *same* agents and
assignment; only the coordination model differs. Results are written into
``benchmark/README.md`` (summary) and ``benchmark/STATS.md`` (detail).
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

from harness.accuracy import EXPECTED_TESTS  # noqa: E402
from harness.agents import AgentSpec, Usage, make_backends  # noqa: E402
from harness.mak_runner import run_mak  # noqa: E402
from harness.metrics import RunResult  # noqa: E402
from harness.report import (  # noqa: E402
    RunMeta,
    inject_readme,
    render_readme_results,
    render_stats,
)
from harness.traditional import run_traditional  # noqa: E402
from harness.workload import OPERATIONS, assign  # noqa: E402

_LAST_RUN = BENCH / ".last_run.json"


def _save_run(mak: RunResult, trad: RunResult, meta: RunMeta) -> None:
    _LAST_RUN.write_text(json.dumps({
        "meta": dataclasses.asdict(meta),
        "mak": dataclasses.asdict(mak),
        "trad": dataclasses.asdict(trad),
    }, indent=2))


def _load_run() -> tuple[RunResult, RunResult, RunMeta]:
    data = json.loads(_LAST_RUN.read_text())

    def _result(d: dict) -> RunResult:
        d = dict(d)
        d["usage"] = Usage(**d.pop("usage"))
        d.pop("total", None)
        d.pop("accuracy", None)
        return RunResult(**d)

    return _result(data["mak"]), _result(data["trad"]), RunMeta(**data["meta"])


def _write_reports(mak: RunResult, trad: RunResult, meta: RunMeta) -> None:
    readme_path = BENCH / "README.md"
    readme_path.write_text(
        inject_readme(
            readme_path.read_text(),
            render_readme_results(mak, trad, meta),
        )
    )
    (BENCH / "STATS.md").write_text(render_stats(mak, trad, meta))
    print(f"[benchmark] wrote {readme_path.name} and STATS.md")

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MAK vs git-worktree benchmark")
    parser.add_argument("--mode", choices=("mock", "real"), default="mock")
    parser.add_argument("--agents", type=int, default=3)
    parser.add_argument("--models", nargs="*", default=None,
                        help="provider:model entries; overrides the defaults")
    parser.add_argument("--keep", action="store_true",
                        help="keep the .runs working copies for inspection")
    parser.add_argument("--render-only", action="store_true",
                        help="re-render the reports from the last run (no model calls)")
    args = parser.parse_args(argv)

    if args.render_only:
        if not _LAST_RUN.exists():
            raise SystemExit("no previous run to render; run without --render-only first")
        _write_reports(*_load_run())
        return 0

    specs = _parse_specs(args.models, args.agents)
    num_agents = len(specs)
    assignment = assign(num_agents)
    mock = args.mode == "mock"
    template = BENCH / "project_template"
    runs = BENCH / ".runs"

    print(f"[benchmark] mode={args.mode} agents={num_agents} "
          f"models={[s.model for s in specs]}")

    print("[benchmark] running MAK (shared-memory kernel) ...")
    mak_result = run_mak(
        _fresh_copy(template, runs / "mak" / "project"),
        runs / "mak" / "mak_state",
        make_backends(specs, mock=mock),
        assignment,
    )
    print(f"[benchmark]   MAK: {mak_result.accuracy:.0%} accuracy, "
          f"{mak_result.usage.calls} calls, {mak_result.wall_seconds:.2f}s")

    print("[benchmark] running Traditional (git worktrees) ...")
    trad_result = run_traditional(
        _fresh_copy(template, runs / "traditional" / "project"),
        runs / "traditional" / "worktrees",
        make_backends(specs, mock=mock),
        assignment,
    )
    print(f"[benchmark]   Traditional: {trad_result.accuracy:.0%} accuracy, "
          f"{trad_result.usage.calls} calls, {trad_result.conflicts} conflicts")

    meta = RunMeta(
        mode=args.mode,
        num_agents=num_agents,
        models=[s.model for s in specs],
        timestamp=datetime.datetime.now().isoformat(timespec="seconds"),
        operations=len(OPERATIONS),
        tests=EXPECTED_TESTS,
    )

    _save_run(mak_result, trad_result, meta)
    _write_reports(mak_result, trad_result, meta)

    if not args.keep:
        shutil.rmtree(runs, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
