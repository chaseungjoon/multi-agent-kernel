"""Stage 23.6 — export each repository's contention profile as JSON.

The profile is the study's reusable output: the distributions a synthetic
workload generator needs in order to produce traces that look like real history
instead of uniform random noise. TASKS.md names Wave 21.10's
``gen_synthetic.py --from-profile`` as the consumer; that flag does not exist on
``main`` yet, so the schema is defined and versioned here and the file documents
its own fields.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime

from mining.cache import CacheHandle, get_meta, open_cache
from mining.cli import chosen, repo_parser
from mining.config import RepoSpec, StudyConfig, load_config
from mining.exceptions import ProfileSchemaError
from mining.footprints import load_footprints
from mining.hot_nodes import category_shares, fit_power_law, hot_nodes, write_counts
from mining.jsonval import as_int, as_mapping
from mining.stats import histogram, summarise

SCHEMA_VERSION = 1

_REQUIRED_TOP_LEVEL = (
    "schema_version", "repo", "window", "changes", "node_popularity",
    "pair_probabilities", "window_curves", "hot_node_categories",
)


def build_profile(config: StudyConfig, spec: RepoSpec) -> dict[str, object]:
    """Assemble one repository's profile document."""
    handle = open_cache(config, spec)
    footprints = load_footprints(handle)
    counts_all = write_counts(handle, python_only=False)
    counts_py = write_counts(handle, python_only=True)
    top = hot_nodes(handle, 200, python_only=False)

    files_per_change = [len(f.paths) for f in footprints]
    nodes_per_change = [len(f.nodes) for f in footprints]
    py_nodes_per_change = [len(f.nodes_py) for f in footprints]
    append_share = _append_share(handle)

    population = json.loads(get_meta(handle, "pair_population", "{}"))
    base_counts = as_mapping(population.get("base"))
    pairs = as_int(base_counts.get("pairs"))
    fit = fit_power_law(list(counts_all.values()))

    profile: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "repo": spec.slug,
        "generated_at": datetime.now(UTC).isoformat(),
        "window": {
            "since": get_meta(handle, "window_since", config.since),
            "until": get_meta(handle, "window_until", config.until),
            "clone_main_branch": get_meta(
                handle, "clone_main_branch", spec.main_branch
            ),
            "clone_main_sha": get_meta(handle, "clone_main_sha"),
        },
        "filters": json.loads(get_meta(handle, "filter_counts", "{}")),
        "changes": {
            "count": len(footprints),
            "files_per_change": summarise(files_per_change).as_dict(),
            "nodes_per_change": summarise(nodes_per_change).as_dict(),
            "python_nodes_per_change": summarise(py_nodes_per_change).as_dict(),
            "files_histogram": histogram(files_per_change),
            "nodes_histogram": histogram(nodes_per_change),
            "append_only_touch_share": append_share,
        },
        "node_popularity": {
            "distinct_nodes": len(counts_all),
            "distinct_python_nodes": len(counts_py),
            "writes_histogram": histogram(list(counts_all.values()), limit=128),
            "zipf": {
                "alpha": fit.alpha,
                "x_min": fit.x_min,
                "tail_size": fit.tail_size,
                "r_squared": fit.r_squared,
                "gini": fit.gini,
            },
            "top_nodes": [
                {
                    "node_id": node.node_id,
                    "writes": node.writes,
                    "category": node.category,
                }
                for node in top[:50]
            ],
        },
        "pair_probabilities": _pair_probabilities(handle, base_counts, pairs),
        "window_curves": _window_curves(handle, config),
        "hot_node_categories": [
            {"category": category, "nodes": nodes, "writes": writes}
            for category, nodes, writes in category_shares(top)
        ],
    }
    handle.conn.close()
    _validate(profile)
    return profile


def _append_share(handle: CacheHandle) -> float:
    """Share of node touches that only add lines to an existing node."""
    row = handle.conn.execute(
        "SELECT SUM(nd.append_only) AS appended, COUNT(*) AS total"
        " FROM pr_node nd JOIN pr_change ch ON ch.number = nd.number"
        " WHERE ch.bucket = 'kept'"
    ).fetchone()
    total = int(row["total"] or 0)
    return (int(row["appended"] or 0) / total) if total else 0.0


def _pair_probabilities(
    handle: CacheHandle, base_counts: dict[str, object], pairs: int
) -> dict[str, object]:
    """Per-pair collision and conflict probabilities under base overlap."""
    def share(key: str) -> float:
        return as_int(base_counts.get(key)) / pairs if pairs else 0.0

    row = handle.conn.execute(
        "SELECT merge_verdict, COUNT(*) AS n FROM pair GROUP BY merge_verdict"
    ).fetchall()
    verdicts = {str(item["merge_verdict"]): int(item["n"]) for item in row}
    resolvable = verdicts.get("clean", 0) + verdicts.get("conflict", 0)
    conflict_no_node = handle.conn.execute(
        "SELECT COUNT(*) AS n FROM pair"
        " WHERE merge_verdict = 'conflict' AND nodes_shared = 0"
    ).fetchone()["n"]
    return {
        "definition": "base_overlap",
        "population_pairs": pairs,
        "same_file": share("file_overlap"),
        "same_node": share("node_overlap"),
        "same_file_different_node": share("same_file_different_node"),
        "same_python_file": share("py_file_overlap"),
        "same_python_node": share("py_node_overlap"),
        "shared_node_all_append": share("shared_all_append"),
        "merge_sample": resolvable,
        "textual_conflict": (
            verdicts.get("conflict", 0) / resolvable if resolvable else 0.0
        ),
        "textual_conflict_without_shared_node": (
            int(conflict_no_node) / verdicts["conflict"]
            if verdicts.get("conflict")
            else 0.0
        ),
        "verdict_counts": verdicts,
        "co_flight_hours": _co_flight_hours(handle),
    }


def _co_flight_hours(handle: CacheHandle) -> dict[str, float | int]:
    """How long the two changes of a sampled pair were in flight together.

    Reported because "concurrent" is the study's central definition and a reader
    should be able to see how much of a window the pairs actually share, rather
    than taking the predicate on trust.
    """
    rows = handle.conn.execute(
        "SELECT overlap_seconds FROM pair WHERE merge_verdict IN ('clean', 'conflict')"
    ).fetchall()
    hours = [max(0, int(row["overlap_seconds"])) // 3600 for row in rows]
    return summarise(hours).as_dict()


def _window_curves(handle: CacheHandle, config: StudyConfig) -> list[dict[str, object]]:
    """Per-k collision probabilities and serialisation depth."""
    merge_curve = {
        int(point["k"]): point
        for point in json.loads(get_meta(handle, "window_merge_curve", "[]"))
    }
    rows = handle.conn.execute(
        "SELECT k, COUNT(*) AS windows,"
        "       AVG(any_file_collision) AS p_file,"
        "       AVG(any_node_collision) AS p_node,"
        "       AVG(any_py_file_collision) AS p_py_file,"
        "       AVG(any_py_node_collision) AS p_py_node,"
        "       AVG(contended_files) AS mean_contended_files,"
        "       AVG(contended_nodes) AS mean_contended_nodes,"
        "       AVG(contended_py_files) AS mean_contended_py_files,"
        "       AVG(contended_py_nodes) AS mean_contended_py_nodes,"
        "       AVG(max_file_chain) AS mean_max_file_chain,"
        "       AVG(max_node_chain) AS mean_max_node_chain,"
        "       AVG(max_py_file_chain) AS mean_max_py_file_chain,"
        "       AVG(max_py_node_chain) AS mean_max_py_node_chain,"
        "       AVG(distinct_nodes) AS mean_distinct_nodes,"
        "       AVG(distinct_py_nodes) AS mean_distinct_py_nodes"
        " FROM window_row GROUP BY k ORDER BY k"
    ).fetchall()
    curves: list[dict[str, object]] = []
    for row in rows:
        k = int(row["k"])
        point = merge_curve.get(k, {})
        windows = int(point.get("windows", 0))
        curves.append(
            {
                "k": k,
                "windows": int(row["windows"]),
                "p_file_collision": float(row["p_file"]),
                "p_node_collision": float(row["p_node"]),
                "p_py_file_collision": float(row["p_py_file"]),
                "p_py_node_collision": float(row["p_py_node"]),
                "mean_contended_files": float(row["mean_contended_files"]),
                "mean_contended_nodes": float(row["mean_contended_nodes"]),
                "mean_contended_py_files": float(row["mean_contended_py_files"]),
                "mean_contended_py_nodes": float(row["mean_contended_py_nodes"]),
                "mean_max_file_chain": float(row["mean_max_file_chain"]),
                "mean_max_node_chain": float(row["mean_max_node_chain"]),
                "mean_max_py_file_chain": float(row["mean_max_py_file_chain"]),
                "mean_max_py_node_chain": float(row["mean_max_py_node_chain"]),
                "mean_distinct_nodes": float(row["mean_distinct_nodes"]),
                "mean_distinct_py_nodes": float(row["mean_distinct_py_nodes"]),
                "mean_effective_k": float(point.get("mean_effective_k", 0.0)) or None,
                "p_git_conflict": (
                    int(point.get("windows_with_conflict", 0)) / windows
                    if windows
                    else None
                ),
                "git_conflict_step_share": (
                    int(point.get("conflicting_steps", 0)) / int(point["total_steps"])
                    if point.get("total_steps")
                    else None
                ),
            }
        )
    return curves


def _validate(profile: dict[str, object]) -> None:
    """Fail loudly if the exported document is missing a required section."""
    missing = [key for key in _REQUIRED_TOP_LEVEL if key not in profile]
    if missing:
        raise ProfileSchemaError(f"profile is missing required keys: {missing}")


def export(config: StudyConfig, spec: RepoSpec, *, verbose: bool = True) -> None:
    """Write ``data/<repo>/profile.json`` for one repository."""
    profile = build_profile(config, spec)
    path = config.repo_data_dir(spec) / "profile.json"
    path.write_text(
        json.dumps(profile, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if verbose:
        print(f"  wrote {path}", flush=True)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m mining.profile_export [owner/repo ...]``."""
    parser = repo_parser(__doc__)
    args = parser.parse_args(argv)

    config = load_config()
    specs = chosen(config, args.repos)
    for spec in specs:
        print(f"exporting {spec.slug} ...", flush=True)
        export(config, spec)
    return 0


if __name__ == "__main__":
    sys.exit(main())
