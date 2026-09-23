"""Sensitivity analysis: whether abandoned pull requests are the contended ones.

Every headline number in this study is computed over PRs that **merged**. If
authors abandon a change precisely because it became impossible to land next to
someone else's, those changes are missing from the record and the measured
contention is biased downward.

The closed-but-unmerged PRs overlapping the study slice are already fetched and
mapped, so the bias is measurable rather than merely acknowledged: this stage
computes, for both populations, how often a change collides with the concurrent
merged changes around it. If the unmerged population collides more, the
direction and size of the bias are known.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass

from mining.cache import CacheHandle, get_meta, open_cache, set_meta
from mining.cli import chosen, repo_parser
from mining.config import RepoSpec, StudyConfig, load_config
from mining.footprints import Footprint, load_footprints
from mining.pair_analysis import base_overlap, lifetime_overlap


@dataclass(frozen=True, slots=True)
class CollisionRates:
    """How often one population of changes collides with the merged stream."""

    population: str
    changes: int
    pairs: int
    file_collisions: int
    node_collisions: int
    py_file_collisions: int
    py_node_collisions: int

    @property
    def file_rate(self) -> float:
        """Share of concurrent pairs sharing a path."""
        return self.file_collisions / self.pairs if self.pairs else 0.0

    @property
    def node_rate(self) -> float:
        """Share of concurrent pairs sharing a node."""
        return self.node_collisions / self.pairs if self.pairs else 0.0

    @property
    def py_node_rate(self) -> float:
        """Share of concurrent pairs sharing a Python AST node."""
        return self.py_node_collisions / self.pairs if self.pairs else 0.0


def _rates(
    population: str, subjects: list[Footprint], merged: list[Footprint]
) -> CollisionRates:
    """Collision rates of ``subjects`` against the concurrent merged changes."""
    pairs = files = nodes = py_files = py_nodes = 0
    for subject in subjects:
        for other in merged:
            if subject.number == other.number:
                continue
            if not lifetime_overlap(subject, other):
                continue
            if not base_overlap(subject, other):
                continue
            pairs += 1
            files += int(bool(subject.paths & other.paths))
            nodes += int(bool(subject.nodes & other.nodes))
            py_files += int(bool(subject.paths_py & other.paths_py))
            py_nodes += int(bool(subject.nodes_py & other.nodes_py))
    return CollisionRates(
        population=population,
        changes=len(subjects),
        pairs=pairs,
        file_collisions=files,
        node_collisions=nodes,
        py_file_collisions=py_files,
        py_node_collisions=py_nodes,
    )


def is_cached(config: StudyConfig, spec: RepoSpec) -> bool:
    """Whether this repository's survivorship comparison is already recorded."""
    handle = open_cache(config, spec)
    done = bool(get_meta(handle, "survivorship"))
    handle.conn.close()
    return done


def analyze(
    config: StudyConfig, spec: RepoSpec, *, verbose: bool = True
) -> tuple[CollisionRates, CollisionRates]:
    """Compare merged and abandoned changes on how much they collide."""
    handle: CacheHandle = open_cache(config, spec)
    merged = load_footprints(handle, merged_only=True)
    abandoned = [
        footprint
        for footprint in load_footprints(handle, merged_only=False)
        if footprint.number not in {m.number for m in merged}
    ]
    merged_rates = _rates("merged", merged, merged)
    abandoned_rates = _rates("abandoned", abandoned, merged)

    set_meta(handle, "survivorship", json.dumps({
        "merged": asdict(merged_rates) | {
            "file_rate": merged_rates.file_rate,
            "node_rate": merged_rates.node_rate,
            "py_node_rate": merged_rates.py_node_rate,
        },
        "abandoned": asdict(abandoned_rates) | {
            "file_rate": abandoned_rates.file_rate,
            "node_rate": abandoned_rates.node_rate,
            "py_node_rate": abandoned_rates.py_node_rate,
        },
    }))
    if verbose:
        print(
            f"  {spec.slug}: merged {merged_rates.changes} changes "
            f"(node collision {merged_rates.node_rate:.3%}), "
            f"abandoned {abandoned_rates.changes} "
            f"(node collision {abandoned_rates.node_rate:.3%})",
            flush=True,
        )
    handle.conn.close()
    return merged_rates, abandoned_rates


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m mining.survivorship [owner/repo ...]``."""
    parser = repo_parser(__doc__)
    parser.add_argument(
        "--force", action="store_true",
        help="recompute even when the comparison is already recorded",
    )
    args = parser.parse_args(argv)

    config = load_config()
    specs = chosen(config, args.repos)
    for spec in specs:
        if is_cached(config, spec) and not args.force:
            print(f"survivorship {spec.slug} ... cached, skipping", flush=True)
            continue
        print(f"survivorship {spec.slug} ...", flush=True)
        analyze(config, spec)
    return 0


if __name__ == "__main__":
    sys.exit(main())
