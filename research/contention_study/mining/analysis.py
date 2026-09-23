"""Stage 23.7 — build the RQ1-RQ6 tables and the combined results document.

Reads each repository's cache and profile, assembles every table the write-up
quotes, and writes ``data/results.json`` plus ``data/RESULTS.md``. Nothing here
recomputes a measurement: this stage only aggregates and formats, so the numbers
in the report and the numbers in the caches cannot drift apart.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from mining.cache import CacheHandle, get_meta, open_cache
from mining.cli import chosen, repo_parser
from mining.config import RepoSpec, StudyConfig, load_config
from mining.jsonval import (
    as_float,
    as_int,
    as_mapping,
    as_sequence,
    as_text,
)
from mining.stats import wilson_interval


@dataclass(frozen=True, slots=True)
class RepoResults:
    """Everything one repository contributes to the report."""

    slug: str
    profile: dict[str, object]
    two_by_two: dict[str, int]
    semantic: dict[str, object]
    validation: dict[str, object]
    non_python: dict[str, object]


def _two_by_two(handle: CacheHandle) -> dict[str, int]:
    """Count the cells of the conflict x shared-node contingency table."""
    rows = handle.conn.execute(
        "SELECT merge_verdict, nodes_shared > 0 AS shared,"
        "       py_nodes_shared > 0 AS py_shared,"
        "       shared_all_append, COUNT(*) AS n"
        " FROM pair GROUP BY merge_verdict, shared, py_shared, shared_all_append"
    ).fetchall()
    table = {
        "conflict_shared": 0, "conflict_not_shared": 0,
        "clean_shared": 0, "clean_not_shared": 0,
        "clean_shared_all_append": 0,
        "conflict_py_shared": 0, "conflict_py_not_shared": 0,
        "rebase_conflict": 0, "error": 0,
    }
    for row in rows:
        verdict = str(row["merge_verdict"])
        shared, py_shared = bool(row["shared"]), bool(row["py_shared"])
        n = int(row["n"])
        if verdict == "conflict":
            table["conflict_shared" if shared else "conflict_not_shared"] += n
            table["conflict_py_shared" if py_shared else "conflict_py_not_shared"] += n
        elif verdict == "clean":
            table["clean_shared" if shared else "clean_not_shared"] += n
            if shared and bool(row["shared_all_append"]):
                table["clean_shared_all_append"] += n
        else:
            table[verdict if verdict in table else "error"] += n
    return table


def _semantic(handle: CacheHandle) -> dict[str, object]:
    """RQ6 aggregates for one repository."""
    rows = handle.conn.execute(
        "SELECT COUNT(*) AS probed,"
        "       SUM(new_defects > 0) AS with_defects,"
        "       SUM(new_defects) AS total_defects"
        " FROM semantic_row WHERE status = 'ok'"
    ).fetchone()
    kinds = handle.conn.execute(
        "SELECT new_kinds, COUNT(*) AS n FROM semantic_row"
        " WHERE status = 'ok' AND new_defects > 0 GROUP BY new_kinds ORDER BY n DESC"
    ).fetchall()
    probed = int(rows["probed"] or 0)
    with_defects = int(rows["with_defects"] or 0)
    low, high = wilson_interval(with_defects, probed)
    return {
        "probed": probed,
        "with_new_defects": with_defects,
        "total_new_defects": int(rows["total_defects"] or 0),
        "share": with_defects / probed if probed else 0.0,
        "ci95": [low, high],
        "kinds": [
            {"kinds": str(row["new_kinds"]), "pairs": int(row["n"])} for row in kinds
        ],
    }


def _non_python(handle: CacheHandle) -> dict[str, object]:
    """How much of the measured contention MAK cannot represent as nodes today."""
    touches = handle.conn.execute(
        "SELECT SUM(nd.is_python = 0) AS non_py, COUNT(*) AS total"
        " FROM pr_node nd JOIN pr_change ch ON ch.number = nd.number"
        " WHERE ch.bucket = 'kept'"
    ).fetchone()
    conflicts = handle.conn.execute(
        "SELECT SUM(conflict_files - conflict_py_files) AS non_py,"
        "       SUM(conflict_files) AS total"
        " FROM pair WHERE merge_verdict = 'conflict'"
    ).fetchone()
    total_touches = int(touches["total"] or 0)
    total_conflicts = int(conflicts["total"] or 0)
    return {
        "non_python_touches": int(touches["non_py"] or 0),
        "total_touches": total_touches,
        "non_python_touch_share": (
            int(touches["non_py"] or 0) / total_touches if total_touches else 0.0
        ),
        "non_python_conflict_files": int(conflicts["non_py"] or 0),
        "total_conflict_files": total_conflicts,
        "non_python_conflict_share": (
            int(conflicts["non_py"] or 0) / total_conflicts if total_conflicts else 0.0
        ),
    }


def collect(config: StudyConfig, spec: RepoSpec) -> RepoResults | None:
    """Gather one repository's results, or None when it has not been analysed."""
    profile_path = config.repo_data_dir(spec) / "profile.json"
    if not profile_path.exists():
        return None
    handle = open_cache(config, spec)
    results = RepoResults(
        slug=spec.slug,
        profile=json.loads(profile_path.read_text()),
        two_by_two=_two_by_two(handle),
        semantic=_semantic(handle),
        validation={
            "mapper": json.loads(get_meta(handle, "mapper_audit", "{}")),
            "merge": json.loads(get_meta(handle, "merge_audit", "{}")),
            "naive_bias": json.loads(get_meta(handle, "naive_bias", "{}")),
            "survivorship": json.loads(get_meta(handle, "survivorship", "{}")),
            "pair_sampling_rate": get_meta(handle, "pair_sampling_rate", "0"),
        },
        non_python=_non_python(handle),
    )
    handle.conn.close()
    return results


def _pct(value: float) -> str:
    """Format a share as a percentage with two decimals."""
    return f"{100 * value:.2f}%"


def render_markdown(results: list[RepoResults]) -> str:
    """Render every RQ table as one markdown document."""
    out: list[str] = ["# Contention study — results tables", ""]
    out.append(
        "Generated by `mining/analysis.py`. Every number is read back from the "
        "per-repository caches; this file is not edited by hand.\n"
    )

    out.append("## Corpus and filters\n")
    out.append(
        "| repo | PRs kept (all statuses) | bot | oversize | whitespace sweep "
        "| unusable | no footprint |"
    )
    out.append("|---|---|---|---|---|---|---|")
    for r in results:
        f = as_mapping(r.profile.get("filters"))
        out.append(
            f"| `{r.slug}` | {as_int(f.get('kept'))} | {as_int(f.get('bot'))} "
            f"| {as_int(f.get('oversize'))} "
            f"| {as_int(f.get('whitespace_sweep'))} "
            f"| {as_int(f.get('unusable'))} | {as_int(f.get('no_footprint'))} |"
        )

    out.append(
        "\n## RQ1 — file-level versus node-level collision, per concurrent pair\n"
    )
    out.append(
        "*Lifetime overlap* means both PRs were open at once and defines the "
        "initial sampling frame. The table reports the stricter *base overlap*: "
        "neither change was already contained in the other's base. See "
        "`pair_analysis.py` for why merge tests require this definition.\n"
    )
    out.append(
        "| repo | definition | pairs | same file | same node "
        "| same file, different node |"
    )
    out.append("|---|---|---|---|---|---|")
    for r in results:
        pair_probs = as_mapping(r.profile.get("pair_probabilities"))
        base_pairs = as_int(pair_probs.get("population_pairs"))
        out.append(
            f"| `{r.slug}` | base overlap | {base_pairs} "
            f"| {_pct(as_float(pair_probs.get('same_file')))} "
            f"| {_pct(as_float(pair_probs.get('same_node')))} "
            f"| {_pct(as_float(pair_probs.get('same_file_different_node')))} |"
        )

    out.append("\n### Python-only view\n")
    out.append("| repo | same Python file | same Python AST node | reduction factor |")
    out.append("|---|---|---|---|")
    for r in results:
        probs = as_mapping(r.profile.get("pair_probabilities"))
        file_share = as_float(probs.get("same_python_file"))
        node_share = as_float(probs.get("same_python_node"))
        factor = f"{file_share / node_share:.1f}x" if node_share else "n/a"
        out.append(
            f"| `{r.slug}` | {_pct(file_share)} | {_pct(node_share)} | {factor} |"
        )

    out.append("\n## RQ2 / RQ3 — the 2x2\n")
    out.append(
        "| repo | conflict & shared node | conflict, no shared node (RQ2) | "
        "clean & shared node (RQ3) | clean, no shared node | rebase conflict | error |"
    )
    out.append("|---|---|---|---|---|---|---|")
    for r in results:
        t = r.two_by_two
        out.append(
            f"| `{r.slug}` | {t['conflict_shared']} | {t['conflict_not_shared']} "
            f"| {t['clean_shared']} | {t['clean_not_shared']} "
            f"| {t['rebase_conflict']} | {t['error']} |"
        )

    out.append("\n### RQ2 and RQ3 as rates\n")
    out.append(
        "| repo | conflicts MAK could not have | shared-node pairs git merges cleanly "
        "| of those, commutative appends |"
    )
    out.append("|---|---|---|---|")
    for r in results:
        t = r.two_by_two
        conflicts = t["conflict_shared"] + t["conflict_not_shared"]
        shared = t["conflict_shared"] + t["clean_shared"]
        clean_shared = t["clean_shared"]
        rq3 = clean_shared / shared if shared else 0.0
        appends = t["clean_shared_all_append"] / clean_shared if clean_shared else 0.0
        if conflicts:
            low, high = wilson_interval(t["conflict_not_shared"], conflicts)
            rq2_text = (
                f"{_pct(t['conflict_not_shared'] / conflicts)} of {conflicts} "
                f"[{_pct(low)}, {_pct(high)}]"
            )
        else:
            rq2_text = "n/a — no textual conflict in the sample"
        rq3_text = f"{_pct(rq3)} of {shared}" if shared else "n/a — no shared-node pair"
        out.append(
            f"| `{r.slug}` | {rq2_text} | {rq3_text} | {_pct(appends)} |"
        )

    out.append("\n## RQ4 — concentration of node-level contention\n")
    out.append(
        "| repo | distinct nodes | Zipf alpha | x_min | log-log R^2 | Gini "
        "| top category by writes |"
    )
    out.append("|---|---|---|---|---|---|---|")
    for r in results:
        pop = as_mapping(r.profile.get("node_popularity"))
        zipf = as_mapping(pop.get("zipf"))
        cats = as_sequence(r.profile.get("hot_node_categories"))
        top = as_text(as_mapping(cats[0]).get("category")) if cats else "n/a"
        out.append(
            f"| `{r.slug}` | {as_int(pop.get('distinct_nodes'))} "
            f"| {as_float(zipf.get('alpha'), float('nan')):.2f} "
            f"| {as_int(zipf.get('x_min'))} "
            f"| {as_float(zipf.get('r_squared'), float('nan')):.3f} "
            f"| {as_float(zipf.get('gini')):.3f} | {top} |"
        )

    out.append("\n## RQ5 — collision probability versus concurrency k\n")
    for r in results:
        out.append(f"\n### `{r.slug}`\n")
        out.append(
            "| k | windows | P(any-path collision) | P(Python file) "
            "| P(Python node) | mean Python file lock chain "
            "| mean Python node lock chain | P(git conflict) |"
        )
        out.append("|---|---|---|---|---|---|---|---|")
        for raw in as_sequence(r.profile.get("window_curves")):
            point = as_mapping(raw)
            git_share = point.get("p_git_conflict")
            git_text = _pct(as_float(git_share)) if git_share is not None else "n/a"
            out.append(
                f"| {as_int(point['k'])} | {as_int(point['windows'])} "
                f"| {_pct(as_float(point['p_file_collision']))} "
                f"| {_pct(as_float(point.get('p_py_file_collision')))} "
                f"| {_pct(as_float(point.get('p_py_node_collision')))} "
                f"| {as_float(point.get('mean_max_py_file_chain')):.2f} "
                f"| {as_float(point.get('mean_max_py_node_chain')):.2f} "
                f"| {git_text} |"
            )

    out.append("\n## RQ6 — static defects a clean merge introduced\n")
    out.append(
        "| repo | pairs probed | with a new defect | share | 95% CI | defect kinds |"
    )
    out.append("|---|---|---|---|---|---|")
    for r in results:
        probe = r.semantic
        ci = as_sequence(probe.get("ci95")) or [0.0, 0.0]
        kinds = ", ".join(
            f"{as_text(as_mapping(item).get('kinds'))} "
            f"({as_int(as_mapping(item).get('pairs'))})"
            for item in as_sequence(probe.get("kinds"))[:3]
        ) or "none"
        out.append(
            f"| `{r.slug}` | {as_int(probe['probed'])} "
            f"| {as_int(probe['with_new_defects'])} "
            f"| {_pct(as_float(probe['share']))} "
            f"| [{_pct(as_float(ci[0]))}, {_pct(as_float(ci[1]))}] | {kinds} |"
        )

    out.append("\n## Non-Python accounting\n")
    out.append(
        "MAK decomposes Python only. Everything else is locked whole-file today, "
        "so it is reported rather than dropped.\n"
    )
    out.append(
        "| repo | non-Python share of node touches "
        "| non-Python share of conflicted files |"
    )
    out.append("|---|---|---|")
    for r in results:
        n = r.non_python
        out.append(
            f"| `{r.slug}` | {_pct(as_float(n['non_python_touch_share']))} "
            f"| {_pct(as_float(n['non_python_conflict_share']))} |"
        )

    out.append("\n## Survivorship — do abandoned changes collide more?\n")
    out.append(
        "Every headline number is computed over changes that merged. If authors "
        "abandon a change because it became impossible to land beside someone "
        "else's, the measured contention is biased downward. The comparison "
        "below is against the same merged stream in both rows.\n"
    )
    out.append(
        "| repo | population | changes | concurrent pairs | share sharing a path "
        "| share sharing a node |"
    )
    out.append("|---|---|---|---|---|---|")
    for r in results:
        survivor = as_mapping(r.validation.get("survivorship"))
        for name in ("merged", "abandoned"):
            row = as_mapping(survivor.get(name))
            if not row:
                continue
            out.append(
                f"| `{r.slug}` | {name} | {as_int(row.get('changes'))} "
                f"| {as_int(row.get('pairs'))} "
                f"| {_pct(as_float(row.get('file_rate')))} "
                f"| {_pct(as_float(row.get('node_rate')))} |"
            )

    out.append("\n## Why the naive merge test had to be abandoned\n")
    out.append(
        "A bare `git merge-tree A B` uses the *earlier* of the two fork points "
        "as its merge base, so every mainline commit between the two forks is "
        "charged to one side. The table shows the conflict rate that method "
        "reports, the rate once both changes are expressed against a shared "
        "base, and how much of the difference is mainline progress.\n"
    )
    out.append(
        "| repo | naive: pairs | naive conflict rate | shared base: pairs "
        "| shared-base conflict rate | naive conflicts whose diffs share no file |"
    )
    out.append("|---|---|---|---|---|---|")
    for r in results:
        bias = as_mapping(r.validation.get("naive_bias"))
        if not bias:
            continue
        out.append(
            f"| `{r.slug}` | {as_int(bias.get('naive_checked'))} "
            f"| {_pct(as_float(bias.get('naive_rate')))} "
            f"| {as_int(bias.get('corrected_checked'))} "
            f"| {_pct(as_float(bias.get('corrected_rate')))} "
            f"| {_pct(as_float(bias.get('provably_spurious')))} |"
        )

    out.append("\n## Validation\n")
    out.append(
        "| repo | mapper: files fully covered | mapper: symbol recall "
        "| merge-tree vs real `git merge` | pair sampling rate |"
    )
    out.append("|---|---|---|---|---|")
    for r in results:
        m = as_mapping(r.validation.get("mapper"))
        g = as_mapping(r.validation.get("merge"))
        out.append(
            f"| `{r.slug}` | {_pct(as_float(m.get('file_accuracy')))} "
            f"of {as_int(m.get('files_checked'))} "
            f"| {_pct(as_float(m.get('symbol_recall')))} "
            f"| {_pct(as_float(g.get('accuracy')))} "
            f"of {as_int(g.get('checked'))} "
            f"| {as_text(r.validation.get('pair_sampling_rate'), '0')} |"
        )
    out.append("")
    return "\n".join(out)


def run(config: StudyConfig, specs: list[RepoSpec]) -> Path:
    """Collect every repository's results and write the combined documents."""
    results = [r for r in (collect(config, spec) for spec in specs) if r is not None]
    if not results:
        raise SystemExit("no repository has a profile.json yet; run the pipeline first")

    config.data_root.mkdir(parents=True, exist_ok=True)
    payload = {
        "repos": [
            {
                "slug": r.slug, "profile": r.profile, "two_by_two": r.two_by_two,
                "semantic": r.semantic, "validation": r.validation,
                "non_python": r.non_python,
            }
            for r in results
        ]
    }
    (config.data_root / "results.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    markdown = config.data_root / "RESULTS.md"
    markdown.write_text(render_markdown(results), encoding="utf-8")
    return markdown


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m mining.analysis [owner/repo ...]``."""
    parser = repo_parser(__doc__)
    args = parser.parse_args(argv)

    config = load_config()
    specs = chosen(config, args.repos)
    path = run(config, specs)
    print(f"wrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
