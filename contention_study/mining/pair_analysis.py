"""Stage 23.3 — classify every concurrent PR pair into the study's 2x2.

Two definitions of "concurrent" are reported, because they answer different
questions and they do not agree.

*Lifetime overlap* — ``[created_at, merged_at]`` intervals intersect — is how
human collaboration is usually measured, and it is what a reader expects. It is
**not** a sound basis for a merge test: GitHub reports a PR's *final* head, after
every rebase and force-push, so a long-lived PR's effective base is often newer
than another PR that was merged while it was open. Merging such a pair is
vacuous, because one change is already contained in the other's base.

*Base overlap* — neither change's fork point postdates the other's merge — is
the definition the merge test needs and the one that matches the agent setting:
two changes were written against views of the codebase that did not contain each
other. Every merge verdict in this study uses it.

Set overlap is computed for the **whole** population under both definitions,
which is cheap. The three-way merge is run on a uniform random sample of
base-overlapping pairs, because ``git merge-tree`` is the hot path; the sampling
rate is recorded so the sampled cells carry an honest confidence interval.
"""

from __future__ import annotations

import json
import random
import sys
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

from mining.cache import CacheHandle, open_cache, set_meta, table_count
from mining.cli import chosen, repo_parser
from mining.config import RepoSpec, StudyConfig, load_config
from mining.footprints import Footprint, load_footprints
from mining.git_repo import GitRepo
from mining.rebase import VERDICT_CONFLICT, ChangeRef, merge_on_common_base
from mining.records import PairRow

_MAX_STORED_CONFLICT_PATHS = 20


@dataclass(frozen=True, slots=True)
class OverlapCounts:
    """Set-overlap counts under one concurrency definition (RQ1)."""

    definition: str
    pairs: int
    file_overlap: int
    node_overlap: int
    py_file_overlap: int
    py_node_overlap: int
    same_file_different_node: int
    shared_all_append: int


@dataclass(frozen=True, slots=True)
class PairPopulation:
    """Exact RQ1 counts under both concurrency definitions."""

    total_prs: int
    total_pairs: int
    lifetime: OverlapCounts
    base: OverlapCounts

    def as_json(self) -> str:
        """Serialise for ``run_meta``."""
        return json.dumps(asdict(self))


def lifetime_overlap(first: Footprint, second: Footprint) -> bool:
    """Whether two PRs were open at the same time."""
    return (
        first.created_ts <= second.merged_ts and second.created_ts <= first.merged_ts
    )


def base_overlap(first: Footprint, second: Footprint) -> bool:
    """Whether each change was written against a base not containing the other.

    A fork point that postdates the other PR's merge means the author already
    had that work in their tree, so the two were never independent however long
    their pull requests happened to stay open.
    """
    if not first.fork_date or not second.fork_date:
        return False
    return first.fork_ts < second.merged_ts and second.fork_ts < first.merged_ts


def concurrent_pairs(footprints: list[Footprint]) -> Iterator[tuple[int, int]]:
    """Yield index pairs whose lifetimes overlap.

    The scan is ordered by creation time, so it can stop as soon as a later PR
    was created after the current one was merged.
    """
    ordered = sorted(range(len(footprints)), key=lambda i: footprints[i].created_ts)
    for position, i in enumerate(ordered):
        first = footprints[i]
        for j in ordered[position + 1 :]:
            if footprints[j].created_ts > first.merged_ts:
                break
            yield i, j


class _Tally:
    """Running RQ1 counters for one concurrency definition."""

    def __init__(self, definition: str) -> None:
        self.definition = definition
        self.pairs = 0
        self.file_overlap = 0
        self.node_overlap = 0
        self.py_file_overlap = 0
        self.py_node_overlap = 0
        self.same_file_different_node = 0
        self.shared_all_append = 0

    def add(self, first: Footprint, second: Footprint) -> None:
        """Fold one pair into the counters."""
        self.pairs += 1
        files_shared = first.paths & second.paths
        nodes_shared = first.nodes & second.nodes
        if files_shared:
            self.file_overlap += 1
        if nodes_shared:
            self.node_overlap += 1
            if nodes_shared <= (first.append_nodes & second.append_nodes):
                self.shared_all_append += 1
        elif files_shared:
            self.same_file_different_node += 1
        if first.paths_py & second.paths_py:
            self.py_file_overlap += 1
        if first.nodes_py & second.nodes_py:
            self.py_node_overlap += 1

    def freeze(self) -> OverlapCounts:
        """Snapshot the counters as an immutable record."""
        return OverlapCounts(
            definition=self.definition,
            pairs=self.pairs,
            file_overlap=self.file_overlap,
            node_overlap=self.node_overlap,
            py_file_overlap=self.py_file_overlap,
            py_node_overlap=self.py_node_overlap,
            same_file_different_node=self.same_file_different_node,
            shared_all_append=self.shared_all_append,
        )


def _population_and_sample(
    footprints: list[Footprint], config: StudyConfig
) -> tuple[PairPopulation, list[tuple[int, int]]]:
    """Single pass: exact RQ1 counts plus a reservoir sample of merge candidates."""
    rng = random.Random(config.random_seed)
    reservoir: list[tuple[int, int]] = []
    limit = config.max_pairs_per_repo
    lifetime = _Tally("lifetime_overlap")
    base = _Tally("base_overlap")

    for i, j in concurrent_pairs(footprints):
        first, second = footprints[i], footprints[j]
        lifetime.add(first, second)
        if not base_overlap(first, second):
            continue
        base.add(first, second)
        index = base.pairs - 1
        if len(reservoir) < limit:
            reservoir.append((i, j))
        else:
            slot = rng.randint(0, index)
            if slot < limit:
                reservoir[slot] = (i, j)

    total = len(footprints)
    population = PairPopulation(
        total_prs=total,
        total_pairs=total * (total - 1) // 2,
        lifetime=lifetime.freeze(),
        base=base.freeze(),
    )
    return population, reservoir


_WORKER_REPO: GitRepo | None = None
_WORKER_CACHE: dict[str, str] = {}


def _worker_init(git_dir: str) -> None:
    """Open the bare clone once per pool worker and reset its tree cache."""
    global _WORKER_REPO
    _WORKER_REPO = GitRepo(Path(git_dir))
    _WORKER_CACHE.clear()


def _worker_merge(
    payload: tuple[int, int, ChangeRef, ChangeRef]
) -> tuple[int, int, str, str]:
    """Pool entry point: merge one pair on its common base, return the verdict."""
    a, b, first, second = payload
    if _WORKER_REPO is None:
        raise RuntimeError("worker was not initialised with a repository")
    outcome = merge_on_common_base(_WORKER_REPO, first, second, _WORKER_CACHE)
    detail = "\n".join(outcome.conflicted_paths[:_MAX_STORED_CONFLICT_PATHS])
    return a, b, outcome.verdict, detail or outcome.detail


def analyze(
    config: StudyConfig, spec: RepoSpec, *, force: bool = False, verbose: bool = True
) -> int:
    """Enumerate, classify and store concurrent pairs for one repository."""
    handle = open_cache(config, spec)
    cached = table_count(handle, "pair")
    if cached and not force:
        if verbose:
            print(f"  {spec.slug}: {cached} pair rows cached, skipping", flush=True)
        handle.conn.close()
        return cached
    footprints = load_footprints(handle)
    if verbose:
        print(f"  {spec.slug}: {len(footprints)} kept merged PRs", flush=True)
    if len(footprints) < 2:
        handle.conn.close()
        return 0

    population, sample = _population_and_sample(footprints, config)
    if verbose:
        print(
            f"    {population.lifetime.pairs} lifetime-overlapping pairs, "
            f"{population.base.pairs} base-overlapping; "
            f"merging a sample of {len(sample)}",
            flush=True,
        )
    refs = {
        index: ChangeRef(
            number=footprint.number,
            head=footprint.head_sha,
            fork=footprint.fork_point,
        )
        for index, footprint in enumerate(footprints)
    }
    payloads = [
        (i, j, refs[i], refs[j])
        for i, j in sample
        if refs[i].head and refs[i].fork and refs[j].head and refs[j].fork
    ]
    # Group pairs that share fork points so each worker's tree cache hits: the
    # shared base and the ported trees are then computed once per distinct fork
    # pair instead of once per pair, which is most of this stage's cost.
    payloads.sort(key=lambda item: (item[2].fork, item[3].fork))

    verdicts: dict[tuple[int, int], tuple[str, str]] = {}
    with ProcessPoolExecutor(
        max_workers=config.workers,
        initializer=_worker_init,
        initargs=(str(config.clone_dir(spec)),),
    ) as pool:
        done = 0
        for a, b, verdict, detail in pool.map(_worker_merge, payloads, chunksize=256):
            verdicts[(a, b)] = (verdict, detail)
            done += 1
            if verbose and done % 10000 == 0:
                print(f"    merged {done}/{len(payloads)}", flush=True)

    rows = [
        _pair_row(footprints[i], footprints[j], *verdicts[(i, j)])
        for i, j in sample
        if (i, j) in verdicts
    ]
    _store(handle, rows)
    set_meta(handle, "pair_population", population.as_json())
    set_meta(handle, "pair_sample_size", str(len(rows)))
    set_meta(
        handle, "pair_sampling_rate",
        f"{len(rows) / population.base.pairs:.6f}" if population.base.pairs else "0",
    )
    handle.conn.close()
    return len(rows)


def _pair_row(
    first: Footprint, second: Footprint, verdict: str, detail: str
) -> PairRow:
    """Assemble one stored pair row from two footprints and a merge verdict."""
    files_shared = first.paths & second.paths
    nodes_shared = first.nodes & second.nodes
    # ``detail`` carries conflicted paths only for a real conflict; for the
    # other verdicts it carries a reason string, which is not a path.
    conflict_paths = (
        [line for line in detail.split("\n") if line]
        if verdict == VERDICT_CONFLICT
        else []
    )
    return PairRow(
        a=first.number,
        b=second.number,
        overlap_seconds=int(
            max(
                0.0,
                min(first.merged_ts, second.merged_ts)
                - max(first.fork_ts, second.fork_ts),
            )
        ),
        files_a=len(first.paths),
        files_b=len(second.paths),
        files_shared=len(files_shared),
        py_files_shared=len(first.paths_py & second.paths_py),
        nodes_shared=len(nodes_shared),
        py_nodes_shared=len(first.nodes_py & second.nodes_py),
        shared_all_append=bool(nodes_shared)
        and nodes_shared <= (first.append_nodes & second.append_nodes),
        merge_verdict=verdict,
        conflict_files=len(conflict_paths),
        conflict_py_files=sum(1 for p in conflict_paths if p.endswith(".py")),
        conflict_paths=detail,
    )


def _store(handle: CacheHandle, rows: list[PairRow]) -> None:
    """Persist pair rows."""
    handle.conn.executemany(
        "INSERT OR REPLACE INTO pair (a, b, overlap_seconds, files_a, files_b,"
        " files_shared, py_files_shared, nodes_shared, py_nodes_shared,"
        " shared_all_append, merge_verdict, conflict_files, conflict_py_files,"
        " conflict_paths) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                r.a, r.b, r.overlap_seconds, r.files_a, r.files_b, r.files_shared,
                r.py_files_shared, r.nodes_shared, r.py_nodes_shared,
                int(r.shared_all_append), r.merge_verdict, r.conflict_files,
                r.conflict_py_files, r.conflict_paths,
            )
            for r in rows
        ],
    )
    handle.conn.commit()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m mining.pair_analysis [owner/repo ...]``."""
    parser = repo_parser(__doc__)
    parser.add_argument(
        "--force", action="store_true",
        help="recompute even when this stage's rows are already cached",
    )
    args = parser.parse_args(argv)

    config = load_config()
    specs = chosen(config, args.repos)
    for spec in specs:
        print(f"pairing {spec.slug} ...", flush=True)
        count = analyze(config, spec, force=args.force)
        print(f"  -> {count} pair rows", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
