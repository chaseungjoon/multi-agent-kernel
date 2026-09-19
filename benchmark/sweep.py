"""Run a resumable, process-isolated Wave 21 scaling sweep."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import itertools
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

BENCHMARK = Path(__file__).resolve().parent
ROOT = BENCHMARK.parent
for candidate in (BENCHMARK, ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from sim.profile import load_profile, profile_hash  # noqa: E402
from sim.runtime import RunCase, git_sha, output_to_dict, run_case  # noqa: E402


@dataclass(frozen=True, slots=True)
class SweepConfig:
    """Validated grid and output settings loaded from YAML."""

    name: str
    agents: tuple[int, ...]
    tasks: tuple[int, ...]
    modules: tuple[int, ...]
    shared_tables: tuple[int, ...]
    popularity: tuple[str, ...]
    zipf_exponent: tuple[float, ...]
    assignment: tuple[str, ...]
    contention_kinds: tuple[tuple[str, ...], ...]
    arms: tuple[str, ...]
    seeds: tuple[int, ...]
    time_scale: float
    failure_rate: float
    kernel_only: bool
    profile: Path


def load_config(path: Path) -> SweepConfig:
    """Load a sweep grid relative to the benchmark directory."""
    data = yaml.safe_load(path.read_text())
    grid = data["grid"]
    profile = Path(data.get("profile", "sim/profiles/default.json"))
    if not profile.is_absolute():
        profile = BENCHMARK / profile
    contention = tuple(
        (value,) if isinstance(value, str) else tuple(str(kind) for kind in value)
        for value in grid.get("contention_kinds", ["registry_append"])
    )
    return SweepConfig(
        name=str(data["name"]),
        agents=tuple(int(value) for value in grid["agents"]),
        tasks=tuple(int(value) for value in grid["tasks"]),
        modules=tuple(int(value) for value in grid["modules"]),
        shared_tables=tuple(int(value) for value in grid["shared_tables"]),
        popularity=tuple(str(value) for value in grid["popularity"]),
        zipf_exponent=tuple(float(value) for value in grid.get("zipf_exponent", [1.2])),
        assignment=tuple(str(value) for value in grid["assignment"]),
        contention_kinds=contention,
        arms=tuple(str(value) for value in grid["arms"]),
        seeds=tuple(int(value) for value in grid["seeds"]),
        time_scale=float(data.get("time_scale", 0.02)),
        failure_rate=float(data.get("failure_rate", 0.0)),
        kernel_only=bool(data.get("kernel_only", False)),
        profile=profile,
    )


def expand_cases(config: SweepConfig) -> list[RunCase]:
    """Expand the Cartesian grid, preserving configured arm order."""
    return [
        RunCase(
            agents=agents,
            tasks=tasks,
            modules=modules,
            shared_tables=tables,
            popularity=popularity,
            zipf_exponent=zipf,
            assignment=assignment,
            contention_kinds=kinds,
            arm=arm,
            seed=seed,
            time_scale=config.time_scale,
            failure_rate=config.failure_rate,
            kernel_only=config.kernel_only,
        )
        for (
            agents,
            tasks,
            modules,
            tables,
            popularity,
            zipf,
            assignment,
            kinds,
            seed,
        ) in itertools.product(
            config.agents,
            config.tasks,
            config.modules,
            config.shared_tables,
            config.popularity,
            config.zipf_exponent,
            config.assignment,
            config.contention_kinds,
            config.seeds,
        )
        for arm in config.arms
    ]


def case_key(case: RunCase) -> str:
    """Return the resumable identity of one grid point."""
    encoded = json.dumps(
        dataclasses.asdict(case), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode()).hexdigest()[:20]


def _read_records(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return {str(key): item for key, item in value.items()}


def _number(value: object, label: str) -> float:
    if isinstance(value, int | float):
        return float(value)
    raise ValueError(f"{label} must be numeric")


def _run_child(case: RunCase, config: SweepConfig, record_path: Path) -> None:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--case-json",
        json.dumps(dataclasses.asdict(case), separators=(",", ":")),
        "--profile",
        str(config.profile),
        "--record-path",
        str(record_path),
    ]
    subprocess.run(command, cwd=ROOT, check=True)


def _baseline_key(record: dict[str, object]) -> tuple[object, ...]:
    params = _mapping(record["params"], "params")
    return tuple(
        json.dumps(params[key], sort_keys=True)
        if isinstance(params[key], list | dict)
        else params[key]
        for key in (
            "agents",
            "tasks",
            "modules",
            "shared_tables",
            "popularity",
            "zipf_exponent",
            "assignment",
            "contention_kinds",
            "seed",
        )
    )


def run_sweep(config: SweepConfig, *, fresh: bool = False) -> Path:
    """Run missing cases, append JSONL records, and write consolidated JSON."""
    results_path = BENCHMARK / "results" / f"{config.name}.jsonl"
    results_path.parent.mkdir(parents=True, exist_ok=True)
    if fresh and results_path.exists():
        results_path.unlink()
    existing = _read_records(results_path)
    completed = {str(record["key"]) for record in existing}
    baselines = {
        _baseline_key(record): _number(
            _mapping(record["metrics"], "metrics")["makespan_seconds"],
            "makespan_seconds",
        )
        for record in existing
        if _mapping(record["params"], "params")["arm"] == "sequential"
    }
    cases = expand_cases(config)
    for number, case in enumerate(cases, start=1):
        key = case_key(case)
        if key in completed:
            print(f"[sweep {number}/{len(cases)}] resume skip {key} {case.arm}")
            continue
        temp_record = results_path.with_suffix(f".{key}.tmp")
        print(
            f"[sweep {number}/{len(cases)}] {case.arm}: "
            f"agents={case.agents} tasks={case.tasks} seed={case.seed}",
            flush=True,
        )
        _run_child(case, config, temp_record)
        record = json.loads(temp_record.read_text())
        temp_record.unlink()
        baseline_identity = _baseline_key(record)
        metrics = _mapping(record["metrics"], "metrics")
        makespan = _number(metrics["makespan_seconds"], "makespan_seconds")
        if case.arm == "sequential":
            baselines[baseline_identity] = makespan
        baseline = baselines.get(baseline_identity, makespan)
        metrics["speedup_vs_sequential"] = baseline / makespan if makespan else 0.0
        record["metrics"] = metrics
        with results_path.open("a") as output:
            output.write(json.dumps(record, sort_keys=True) + "\n")
        existing.append(record)
        completed.add(key)
    consolidated = BENCHMARK / "simulated_agent_scaling_1_result.json"
    payload = {
        "benchmark": "simulated_agent_scaling_1",
        "sweep": config.name,
        "real_components": [
            "mak.session.Session",
            "LockTable and node store transactions",
            "git worktrees, commits, merges, and conflict detection",
        ],
        "modeled_components": [
            "agent latency and tokens",
            "agent correctness",
            "worktree conflict resolution and line drops",
        ],
        "profile": str(config.profile.relative_to(BENCHMARK)),
        "profile_hash": profile_hash(config.profile),
        "records": existing,
    }
    consolidated.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"[sweep] wrote {results_path}")
    print(f"[sweep] wrote {consolidated}")
    return consolidated


def _case_from_json(raw: str) -> RunCase:
    data = json.loads(raw)
    data["contention_kinds"] = tuple(data["contention_kinds"])
    return RunCase(**data)


def _run_one(case: RunCase, profile_path: Path, record_path: Path) -> None:
    output = run_case(
        case,
        load_profile(profile_path),
        BENCHMARK / ".runs" / "simulated_agent_scaling_1" / case_key(case),
    )
    record = {
        "key": case_key(case),
        "params": dataclasses.asdict(case),
        "metrics": dataclasses.asdict(output.metrics),
        "modeled_samples": output_to_dict(output)["modeled_samples"],
        "git_sha": git_sha(ROOT),
        "profile_hash": profile_hash(profile_path),
    }
    record_path.write_text(json.dumps(record, sort_keys=True) + "\n")


def main() -> None:
    """Run a complete sweep or one private child-process case."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="replace this sweep's existing JSONL instead of resuming it",
    )
    parser.add_argument("--case-json", help=argparse.SUPPRESS)
    parser.add_argument("--profile", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--record-path", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.case_json:
        if args.profile is None or args.record_path is None:
            parser.error("internal case mode requires --profile and --record-path")
        _run_one(_case_from_json(args.case_json), args.profile, args.record_path)
        return
    if args.config is None:
        parser.error("--config is required")
    run_sweep(load_config(args.config), fresh=args.fresh)


if __name__ == "__main__":
    main()
