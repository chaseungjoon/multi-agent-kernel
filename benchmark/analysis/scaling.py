"""Generate scaling tables, SVG figures, analytic overlays, and H1-H5 verdicts."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AnalysisRecord:
    """Normalized fields used by the scaling analysis."""

    agents: int
    tasks: int
    popularity: str
    arm: str
    seed: int
    makespan: float
    conflicts: float
    accuracy: float
    dropped: int
    kernel_p95: float
    kernel_seconds: float
    agent_seconds: float


@dataclass(frozen=True, slots=True)
class Verdict:
    """One preregistered hypothesis verdict and its evidence."""

    hypothesis: str
    status: str
    evidence: str


def read_records(path: Path) -> list[AnalysisRecord]:
    """Read the JSONL result stream into typed analysis rows."""
    records: list[AnalysisRecord] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        params = raw["params"]
        metrics = raw["metrics"]
        records.append(
            AnalysisRecord(
                agents=int(params["agents"]),
                tasks=int(params["tasks"]),
                popularity=str(params["popularity"]),
                arm=str(params["arm"]),
                seed=int(params["seed"]),
                makespan=float(metrics["makespan_seconds"]),
                conflicts=float(metrics["conflicts"]),
                accuracy=float(metrics["accuracy"]),
                dropped=int(metrics["registrations_dropped"]),
                kernel_p95=float(metrics["kernel_commit_p95_seconds"]),
                kernel_seconds=float(metrics["kernel_seconds"]),
                agent_seconds=float(metrics["modeled_agent_seconds"]),
            )
        )
    return records


def analytic_prediction(record: AnalysisRecord) -> float:
    """Evaluate the explanatory model using measured work and coordination terms."""
    parallel_work = record.agent_seconds / max(1, record.agents)
    if record.arm.startswith("mak_"):
        hot_fraction = 0.65 if record.popularity == "zipf" else 0.2
        critical_hot = record.agent_seconds * hot_fraction
        return max(parallel_work, critical_hot) + record.kernel_seconds
    resolver_cost = record.conflicts * 15.0
    git_cost = record.conflicts * 0.02
    return parallel_work + resolver_cost + git_cost


def verdicts(records: list[AnalysisRecord]) -> list[Verdict]:
    """Evaluate H1-H5 conservatively from available paired records."""
    if not records:
        return [
            Verdict(f"H{index}", "inconclusive", "No completed records.")
            for index in range(1, 6)
        ]
    pairs = _pair_arms(records, "mak_node", "worktree_end")
    low = [pair for pair in pairs if pair[0].popularity == "uniform"]
    high = [pair for pair in pairs if pair[0].popularity == "zipf"]
    low_margin = _mean([worktree.makespan - mak.makespan for mak, worktree in low])
    high_margin = _mean([worktree.makespan - mak.makespan for mak, worktree in high])
    h1_status = (
        "supported"
        if low and high and low_margin > high_margin
        else "refuted"
        if low and high
        else "inconclusive"
    )
    h1 = Verdict(
        "H1 crossover",
        h1_status,
        f"Mean MAK margin: uniform={low_margin:.3f}s, Zipf={high_margin:.3f}s.",
    )

    worktrees = sorted(
        (record for record in records if record.arm == "worktree_end"),
        key=lambda record: record.agents,
    )
    if len({record.agents for record in worktrees}) >= 2:
        first, last = worktrees[0], worktrees[-1]
        agent_ratio = last.agents / max(1, first.agents)
        conflict_ratio = (last.conflicts + 1) / (first.conflicts + 1)
        h2_status = "supported" if conflict_ratio > agent_ratio else "refuted"
        h2_evidence = (
            f"Conflict growth={conflict_ratio:.2f}x versus agent "
            f"growth={agent_ratio:.2f}x."
        )
    else:
        h2_status = "inconclusive"
        h2_evidence = "At least two agent counts are required."
    h2 = Verdict("H2 merge cost is superlinear", h2_status, h2_evidence)

    mak_drops = sum(record.dropped for record in records if record.arm == "mak_node")
    worktree_drops = sum(
        record.dropped for record in records if record.arm.startswith("worktree_")
    )
    h3_status = (
        "supported"
        if worktree_drops > 0 and mak_drops == 0
        else "refuted"
        if mak_drops > 0
        else "inconclusive"
    )
    h3 = Verdict(
        "H3 worktree correctness decay",
        h3_status,
        f"Observed registration drops: MAK={mak_drops}, worktrees={worktree_drops}.",
    )

    ablations = _pair_arms(records, "mak_node", "mak_file")
    node_mean = _mean([node.makespan for node, _file in ablations])
    file_mean = _mean([file.makespan for _node, file in ablations])
    h4_status = (
        "supported"
        if ablations and file_mean > node_mean * 1.05
        else "refuted"
        if ablations
        else "inconclusive"
    )
    h4 = Verdict(
        "H4 node granularity matters",
        h4_status,
        f"Mean makespan: node={node_mean:.3f}s, file={file_mean:.3f}s.",
    )

    commit_p95 = max(
        (record.kernel_p95 for record in records if record.arm.startswith("mak_")),
        default=0.0,
    )
    h5_status = (
        "supported"
        if 0 < commit_p95 < 7.5
        else "refuted"
        if commit_p95
        else "inconclusive"
    )
    h5 = Verdict(
        "H5 kernel overhead stays small",
        h5_status,
        f"Maximum measured commit p95={commit_p95:.6f}s; default shortest "
        "agent sample=7.5s.",
    )
    return [h1, h2, h3, h4, h5]


def write_analysis(
    records: list[AnalysisRecord], output_dir: Path, results_markdown: Path
) -> None:
    """Write tabular data, a compact SVG, analytic overlays, and verdict prose."""
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = sorted(
        records, key=lambda record: (record.popularity, record.arm, record.agents)
    )
    with (output_dir / "scaling.csv").open("w", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(
            ["agents", "tasks", "popularity", "arm", "measured", "analytic"]
        )
        for record in rows:
            writer.writerow(
                [
                    record.agents,
                    record.tasks,
                    record.popularity,
                    record.arm,
                    record.makespan,
                    analytic_prediction(record),
                ]
            )
    (output_dir / "crossover.json").write_text(
        json.dumps(_crossover(rows), indent=2, sort_keys=True) + "\n"
    )
    (output_dir / "makespan.svg").write_text(_render_svg(rows))
    findings = verdicts(records)
    results_markdown.write_text(_render_results(findings, len(records)))


def _pair_arms(
    records: list[AnalysisRecord], left: str, right: str
) -> list[tuple[AnalysisRecord, AnalysisRecord]]:
    index = {
        (
            record.agents,
            record.tasks,
            record.popularity,
            record.seed,
            record.arm,
        ): record
        for record in records
    }
    pairs: list[tuple[AnalysisRecord, AnalysisRecord]] = []
    for record in records:
        if record.arm != left:
            continue
        other = index.get(
            (record.agents, record.tasks, record.popularity, record.seed, right)
        )
        if other is not None:
            pairs.append((record, other))
    return pairs


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _crossover(records: list[AnalysisRecord]) -> list[dict[str, object]]:
    cells: list[dict[str, object]] = []
    for mak, worktree in _pair_arms(records, "mak_node", "worktree_end"):
        margin = worktree.makespan - mak.makespan
        cells.append(
            {
                "agents": mak.agents,
                "contention": mak.popularity,
                "winner": "mak" if margin > 0 else "worktree",
                "margin_seconds": abs(margin),
            }
        )
    return cells


def _render_svg(records: list[AnalysisRecord]) -> str:
    selected = [
        record for record in records if record.arm in {"mak_node", "worktree_end"}
    ]
    width, height = 760, 420
    if not selected:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
            f'height="{height}" />\n'
        )
    max_agents = max(record.agents for record in selected)
    max_time = max(record.makespan for record in selected) or 1.0
    colors = {"mak_node": "#2563eb", "worktree_end": "#dc2626"}
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<text x="20" y="28" font-family="sans-serif" font-size="18">'
        "Makespan vs agents</text>",
        '<line x1="55" y1="375" x2="735" y2="375" stroke="black"/>',
        '<line x1="55" y1="45" x2="55" y2="375" stroke="black"/>',
    ]
    for record in selected:
        x = 55 + 680 * record.agents / max_agents
        y = 375 - 330 * record.makespan / max_time
        parts.append(
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="4" fill="{colors[record.arm]}">'
            f"<title>{record.arm}: N={record.agents}, "
            f"{record.makespan:.3f}s</title></circle>"
        )
    parts.append("</svg>\n")
    return "\n".join(parts)


def _render_results(findings: list[Verdict], record_count: int) -> str:
    lines = [
        "# Simulated agent scaling 1 — Results",
        "",
        f"Analysis covers {record_count} completed arm runs.",
        "",
        "The MAK kernel, node store, scheduler, transactions, reconstruction and git",
        "operations are real. Agent latency/tokens/correctness and conflict-resolver",
        "latency/line drops are modeled from the selected profile.",
        "",
        "## Preregistered hypotheses",
        "",
    ]
    for finding in findings:
        lines.extend(
            [
                f"- **{finding.hypothesis}: {finding.status}.** {finding.evidence}",
                "",
            ]
        )
    return "\n".join(lines)


def main() -> None:
    """Analyze one sweep result stream."""
    benchmark = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path, default=benchmark / "figures")
    parser.add_argument(
        "--results", type=Path, default=benchmark / "sim" / "RESULTS.md"
    )
    args = parser.parse_args()
    records = read_records(args.input)
    write_analysis(records, args.output_dir, args.results)
    print(f"[analysis] wrote {args.output_dir} and {args.results}")


if __name__ == "__main__":
    main()
