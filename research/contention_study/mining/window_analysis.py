"""Stage 23.4 — the k-window replay (RQ5): how contention grows with concurrency.

Pairwise lifetime overlap measures how humans actually worked. The agent setting
is different: *k* tasks are dispatched from one base at the same instant. The
window replay models that by taking *k* consecutive merged PRs, treating them as
one wave off a single base, and asking how much of the wave collides.

Two curves come out of it.

The **set curve** — what fraction of windows contain at least one file-level or
node-level collision, and the longest chain MAK would serialise behind one lock —
is computed for every window. It asks "if these *k* changes were dispatched at
once, how many would contend?", which needs only their write sets.

The **git curve** is computed on a sample and needs more care, because the *k*
changes have to be expressed against one base before they can be landed one
after another. That base is the **earliest** fork point in the window: every
change forked at or after it, so none of them is already contained in it and each
one has to be genuinely re-applied. (The pair analysis uses the *later* fork
instead, because there the question is whether two specific changes were
independent; here the question is what happens when *k* changes are dispatched
from one base, which is a different question and takes a different base.)

Backporting a change across the mainline commits that landed after it forked can
itself fail. That is a change-versus-mainline conflict, not a
change-versus-change one, so such a change is left out and counted separately;
the number that survived is reported as the *effective* k.
"""

from __future__ import annotations

import json
import random
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

from mining.cache import CacheHandle, open_cache, set_meta, table_count
from mining.cli import chosen, repo_parser
from mining.config import RepoSpec, StudyConfig, load_config
from mining.footprints import Footprint, load_footprints
from mining.git_repo import GitRepo
from mining.rebase import ChangeRef, earliest_fork, port_to_base, tree_of
from mining.records import WindowRow

# How many windows per k get a real sequential merge. The set statistics are
# computed for every window; only the git replay is sampled.
MERGE_SAMPLE_PER_K = 150


@dataclass(frozen=True, slots=True)
class MergeCurvePoint:
    """Sequential-merge outcome for one nominal k."""

    k: int
    windows: int
    windows_with_conflict: int
    conflicting_steps: int
    total_steps: int
    failed_windows: int
    mean_effective_k: float


def _contention(counts: Counter[str]) -> tuple[int, int, int]:
    """``(distinct, contended, longest chain)`` for one resource's write counts."""
    contended = sum(1 for count in counts.values() if count > 1)
    return len(counts), contended, max(counts.values(), default=0)


def window_row(index: int, k: int, group: list[Footprint]) -> WindowRow:
    """Compute the contention statistics for one window of k changes.

    Both granularities are reported twice: over every path the changes write to,
    and over Python only. The two views differ because a non-Python file *is* one
    node — MAK cannot decompose it — so file-level and node-level contention
    coincide there by construction, and mixing them would hide the effect the
    study is trying to measure.
    """
    files: Counter[str] = Counter()
    nodes: Counter[str] = Counter()
    py_files: Counter[str] = Counter()
    py_nodes: Counter[str] = Counter()
    for footprint in group:
        files.update(footprint.paths)
        nodes.update(footprint.nodes)
        py_files.update(footprint.paths_py)
        py_nodes.update(footprint.nodes_py)

    distinct_files, contended_files, file_chain = _contention(files)
    distinct_nodes, contended_nodes, node_chain = _contention(nodes)
    distinct_py_files, contended_py_files, py_file_chain = _contention(py_files)
    distinct_py_nodes, contended_py_nodes, py_node_chain = _contention(py_nodes)
    return WindowRow(
        k=k,
        window_index=index,
        first_pr=group[0].number,
        distinct_files=distinct_files,
        contended_files=contended_files,
        distinct_nodes=distinct_nodes,
        contended_nodes=contended_nodes,
        max_file_chain=file_chain,
        max_node_chain=node_chain,
        any_file_collision=contended_files > 0,
        any_node_collision=contended_nodes > 0,
        distinct_py_files=distinct_py_files,
        contended_py_files=contended_py_files,
        distinct_py_nodes=distinct_py_nodes,
        contended_py_nodes=contended_py_nodes,
        max_py_file_chain=py_file_chain,
        max_py_node_chain=py_node_chain,
        any_py_file_collision=contended_py_files > 0,
        any_py_node_collision=contended_py_nodes > 0,
    )


_WORKER_REPO: GitRepo | None = None
_WORKER_CACHE: dict[str, str] = {}


def _worker_init(git_dir: str) -> None:
    """Open the bare clone once per pool worker."""
    global _WORKER_REPO
    _WORKER_REPO = GitRepo(Path(git_dir))
    _WORKER_CACHE.clear()


def _worker_sequence(
    payload: tuple[int, tuple[ChangeRef, ...]]
) -> tuple[int, int, int, int, bool]:
    """Land a window one change at a time on one base.

    Returns ``(k, conflicting, steps, effective_k, failed)``. Every change is
    first ported onto the window's shared base, then folded into an accumulating
    tree with that base as the merge base — which is how a branch-per-agent
    workflow lands a wave: the first change is free, and each later one meets
    everything already landed. A change that conflicts is counted and left out,
    so the rest of the window still lands.
    """
    k, refs = payload
    if _WORKER_REPO is None:
        raise RuntimeError("worker was not initialised with a repository")
    repo = _WORKER_REPO
    base = earliest_fork(repo, tuple(ref.fork for ref in refs), _WORKER_CACHE)
    if base is None:
        return k, 0, 0, 0, True
    root = tree_of(repo, base, _WORKER_CACHE)
    if root is None:
        return k, 0, 0, 0, True

    ported = [
        tree
        for tree, _reason in (
            port_to_base(repo, ref, base, _WORKER_CACHE) for ref in refs
        )
        if tree is not None and tree != root
    ]

    if len(ported) < 2:
        return k, 0, 0, len(ported), True

    accumulated = ported[0]
    conflicting = 0
    for tree in ported[1:]:
        verdict = repo.merge_tree(accumulated, tree, base=root)
        if verdict.failed:
            # Infrastructure failures (missing objects, permissions, disk
            # errors) are not evidence of source-level contention. Exclude the
            # whole replay window instead of turning them into false conflicts.
            return k, 0, 0, len(ported), True
        if not verdict.clean:
            conflicting += 1
            continue
        if verdict.tree:
            accumulated = verdict.tree
    return k, conflicting, len(ported) - 1, len(ported), False


def _window_refs(group: list[Footprint]) -> tuple[ChangeRef, ...]:
    """Every member of a window that has a resolvable fork point and head."""
    return tuple(
        ChangeRef(number=f.number, head=f.head_sha, fork=f.fork_point)
        for f in group
        if f.head_sha and f.fork_point and f.fork_date
    )


def _merge_curve(
    config: StudyConfig, spec: RepoSpec, footprints: list[Footprint]
) -> list[MergeCurvePoint]:
    """Run the sampled sequential-merge replay for every k."""
    rng = random.Random(config.random_seed + 1)
    payloads: list[tuple[int, tuple[ChangeRef, ...]]] = []
    for k in config.window_sizes:
        if len(footprints) < k:
            continue
        starts = list(range(0, len(footprints) - k + 1))
        if len(starts) > MERGE_SAMPLE_PER_K:
            starts = rng.sample(starts, MERGE_SAMPLE_PER_K)
        for start in starts:
            group = footprints[start : start + k]
            refs = _window_refs(group)
            if len(refs) >= 2:
                payloads.append((k, refs))

    tallies: dict[int, list[int]] = {k: [0, 0, 0, 0, 0, 0] for k in config.window_sizes}
    if payloads:
        with ProcessPoolExecutor(
            max_workers=config.workers,
            initializer=_worker_init,
            initargs=(str(config.clone_dir(spec)),),
        ) as pool:
            for k, conflicting, steps, effective, failed in pool.map(
                _worker_sequence, payloads, chunksize=1
            ):
                tally = tallies[k]
                if failed:
                    tally[4] += 1
                    continue
                tally[0] += 1
                tally[1] += int(conflicting > 0)
                tally[2] += conflicting
                tally[3] += steps
                tally[5] += effective
    return [
        MergeCurvePoint(
            k=k,
            windows=tally[0],
            windows_with_conflict=tally[1],
            conflicting_steps=tally[2],
            total_steps=tally[3],
            failed_windows=tally[4],
            mean_effective_k=tally[5] / tally[0] if tally[0] else 0.0,
        )
        for k, tally in sorted(tallies.items())
        if tally[0] > 0
    ]


def analyze(
    config: StudyConfig,
    spec: RepoSpec,
    *,
    with_merges: bool = True,
    force: bool = False,
    verbose: bool = True,
) -> int:
    """Compute and store the window rows and the sequential-merge curve."""
    handle = open_cache(config, spec)
    cached = table_count(handle, "window_row")
    if cached and not force:
        if verbose:
            print(f"  {spec.slug}: {cached} window rows cached, skipping", flush=True)
        handle.conn.close()
        return cached
    footprints = load_footprints(handle)
    if verbose:
        print(f"  {spec.slug}: {len(footprints)} kept merged PRs", flush=True)

    rng = random.Random(config.random_seed)
    rows: list[WindowRow] = []
    for k in config.window_sizes:
        if len(footprints) < k:
            continue
        starts = list(range(0, len(footprints) - k + 1))
        if len(starts) > config.max_windows_per_k:
            starts = sorted(rng.sample(starts, config.max_windows_per_k))
        rows.extend(
            window_row(start, k, footprints[start : start + k]) for start in starts
        )
    _store(handle, rows)

    if with_merges:
        curve = _merge_curve(config, spec, footprints)
        set_meta(handle, "window_merge_curve", json.dumps([asdict(p) for p in curve]))
        if verbose:
            for point in curve:
                share = point.windows_with_conflict / max(point.windows, 1)
                print(
                    f"    k={point.k:3d}: {share:6.1%} of windows hit a git conflict "
                    f"({point.conflicting_steps}/{point.total_steps} steps)",
                    flush=True,
                )
    handle.conn.close()
    return len(rows)


def _store(handle: CacheHandle, rows: list[WindowRow]) -> None:
    """Persist window rows."""
    handle.conn.executemany(
        "INSERT OR REPLACE INTO window_row (k, window_index, first_pr,"
        " distinct_files, contended_files, distinct_nodes, contended_nodes,"
        " max_file_chain, max_node_chain, any_file_collision, any_node_collision,"
        " distinct_py_files, contended_py_files, distinct_py_nodes,"
        " contended_py_nodes, max_py_file_chain, max_py_node_chain,"
        " any_py_file_collision, any_py_node_collision)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                r.k, r.window_index, r.first_pr, r.distinct_files, r.contended_files,
                r.distinct_nodes, r.contended_nodes, r.max_file_chain,
                r.max_node_chain, int(r.any_file_collision),
                int(r.any_node_collision), r.distinct_py_files,
                r.contended_py_files, r.distinct_py_nodes, r.contended_py_nodes,
                r.max_py_file_chain, r.max_py_node_chain,
                int(r.any_py_file_collision), int(r.any_py_node_collision),
            )
            for r in rows
        ],
    )
    handle.conn.commit()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m mining.window_analysis [owner/repo ...]``."""
    parser = repo_parser(__doc__)
    parser.add_argument(
        "--no-merges", action="store_true",
        help="skip the sampled sequential-merge replay (set statistics only)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="recompute even when this stage's rows are already cached",
    )
    args = parser.parse_args(argv)

    config = load_config()
    specs = chosen(config, args.repos)
    for spec in specs:
        print(f"windowing {spec.slug} ...", flush=True)
        count = analyze(
            config, spec, with_merges=not args.no_merges, force=args.force
        )
        print(f"  -> {count} window rows", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
