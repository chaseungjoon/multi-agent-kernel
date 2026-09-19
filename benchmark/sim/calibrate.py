"""Compare simulator predictions with affordable real-model calibration points."""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CalibrationPoint:
    """Comparable metrics at one agent count and contention level."""

    agents: int
    contention: str
    makespan: float
    conflicts: float
    tokens: int


def read_points(path: Path) -> list[CalibrationPoint]:
    """Read sweep-like JSONL rows into calibration points."""
    points: list[CalibrationPoint] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        params = row["params"]
        metrics = row["metrics"]
        points.append(
            CalibrationPoint(
                agents=int(params["agents"]),
                contention=str(params.get("popularity", params.get("contention"))),
                makespan=float(metrics["makespan_seconds"]),
                conflicts=float(metrics["conflicts"]),
                tokens=int(metrics["tokens_in"]) + int(metrics["tokens_out"]),
            )
        )
    return points


def percentage_error(predicted: float, actual: float) -> float:
    """Return absolute percentage error, with a zero-safe absolute fallback."""
    return abs(predicted - actual) / abs(actual) * 100.0 if actual else abs(predicted)


def _metric(row: dict[str, object], name: str) -> float:
    value = row[name]
    if isinstance(value, int | float):
        return float(value)
    raise ValueError(f"calibration metric {name!r} is not numeric")


def compare(
    simulated: list[CalibrationPoint], real: list[CalibrationPoint]
) -> dict[str, object]:
    """Pair points and report per-metric and mean absolute percentage errors."""
    real_index = {(point.agents, point.contention): point for point in real}
    rows: list[dict[str, object]] = []
    for prediction in simulated:
        actual = real_index.get((prediction.agents, prediction.contention))
        if actual is None:
            continue
        rows.append(
            {
                "agents": prediction.agents,
                "contention": prediction.contention,
                "makespan_error_percent": percentage_error(
                    prediction.makespan, actual.makespan
                ),
                "conflicts_error_percent": percentage_error(
                    prediction.conflicts, actual.conflicts
                ),
                "tokens_error_percent": percentage_error(
                    float(prediction.tokens), float(actual.tokens)
                ),
            }
        )
    if not rows:
        raise ValueError("no matching calibration points")
    summary = {
        metric: statistics.fmean(_metric(row, metric) for row in rows)
        for metric in (
            "makespan_error_percent",
            "conflicts_error_percent",
            "tokens_error_percent",
        )
    }
    return {"points": rows, "mean_absolute_percentage_error": summary}


def main() -> None:
    """Compare two JSONL files and write the calibration report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulated", required=True, type=Path)
    parser.add_argument("--real", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = compare(read_points(args.simulated), read_points(args.real))
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"[calibration] wrote {args.output}")


if __name__ == "__main__":
    main()
