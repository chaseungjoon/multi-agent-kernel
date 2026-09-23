"""Stage 23.5 (RQ6) — static defects a clean merge introduces that neither side had.

Git's merge is textual: two changes that never touch the same line merge without
complaint even when the result is broken. This stage takes pairs that merged
**cleanly**, materialises four variants of every Python file either side touched
— base, A-only, B-only and the merge — and runs MAK's own cross-node checks
(Wave 20's signature, import, name-collision and registry-key checks) over each
variant.

A defect is only counted when it is present in the merge and absent from base,
A-alone and B-alone. Differencing against the three controls is what makes the
result trustworthy: any false positive the checks produce from being run over a
whole file set rather than one agent's edit appears in all four variants and
cancels out.
"""

from __future__ import annotations

import random
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from mak.conflict_detector.detector import ConflictDetector, EditRound

from mining.cache import CacheHandle, open_cache, set_meta, table_count
from mining.cli import chosen, repo_parser
from mining.config import RepoSpec, StudyConfig, load_config
from mining.footprints import Footprint, load_footprints
from mining.git_repo import GitRepo
from mining.node_map import index_file
from mining.rebase import VERDICT_CLEAN, ChangeRef, merge_on_common_base
from mining.records import SemanticRow

# A pair touching more Python files than this is a sweep; four full variants of
# it would dominate the probe's runtime without changing the answer.
MAX_PROBE_FILES = 40
# The detector's signature check is quadratic in the node count of what it is
# given, so a single enormous module is skipped rather than allowed to dominate.
MAX_NODES_PER_FILE = 400


@dataclass(frozen=True, slots=True)
class Variant:
    """One version of the touched file set, keyed by path."""

    name: str
    sources: dict[str, str]


def _read_variant(
    repo: GitRepo, tree: str, paths: list[str], name: str
) -> Variant:
    """Read every path out of one tree, skipping the ones it does not contain."""
    sources: dict[str, str] = {}
    for path in paths:
        blob = repo.blob(tree, path)
        if blob is not None:
            sources[path] = blob
    return Variant(name=name, sources=sources)


def _file_edit_round(path: str, source: str) -> EditRound | None:
    """Build an :class:`EditRound` describing one file of a variant.

    Every node of the file is offered as both a definition and a caller, which
    asks the detector "is this file internally consistent?". The detector is run
    per file rather than over the whole touched set for two reasons: its import
    and name-collision checks are already scoped per file when the keys carry a
    file component, so nothing is lost; and its signature check re-parses the
    joined definitions once per caller, which is quadratic in the number of
    nodes and becomes unusable on a repository of large modules.
    """
    index = index_file(path, source)
    if not index.parsed or len(index.spans) > MAX_NODES_PER_FILE:
        return None
    lines = source.splitlines(keepends=True)
    definitions: dict[str, str] = {}
    headers: dict[str, str] = {}
    registries: dict[str, str] = {}
    for span in index.spans:
        fragment = "".join(lines[span.start - 1 : span.end])
        definitions[span.node_id] = fragment
        if span.kind == "module_header":
            headers[span.node_id] = fragment
        else:
            registries[span.node_id] = fragment
    return EditRound(
        definitions=definitions,
        callers=dict(definitions),
        header_edits=headers,
        registry_edits=registries,
    )


def _findings(variant: Variant) -> set[str]:
    """Run MAK's cross-node checks over a variant and return its finding set."""
    detector = ConflictDetector()
    found: set[str] = set()
    for path, source in variant.sources.items():
        edits = _file_edit_round(path, source)
        if edits is None:
            continue
        report = detector.detect(edits)
        found.update(
            f"{conflict.check}|{conflict.message}" for conflict in report.conflicts
        )
    return found


@dataclass(frozen=True, slots=True)
class ProbePayload:
    """Everything a worker needs to probe one pair."""

    a: int
    b: int
    ref_a: ChangeRef
    ref_b: ChangeRef
    paths: tuple[str, ...]


_WORKER_REPO: GitRepo | None = None
_WORKER_CACHE: dict[str, str] = {}


def _worker_init(git_dir: str) -> None:
    """Open the bare clone once per pool worker."""
    global _WORKER_REPO
    _WORKER_REPO = GitRepo(Path(git_dir))
    _WORKER_CACHE.clear()


def _worker_probe(payload: ProbePayload) -> SemanticRow:
    """Pool entry point: probe one cleanly merged pair."""
    if _WORKER_REPO is None:
        raise RuntimeError("worker was not initialised with a repository")
    repo = _WORKER_REPO
    outcome = merge_on_common_base(repo, payload.ref_a, payload.ref_b, _WORKER_CACHE)
    if outcome.verdict != VERDICT_CLEAN or not outcome.merged_tree:
        return SemanticRow(
            a=payload.a, b=payload.b, status=f"skipped:{outcome.verdict}",
            base_defects=0, a_defects=0, b_defects=0, merge_defects=0,
            new_defects=0, new_kinds="", detail="",
        )

    paths = list(payload.paths)
    variants = {
        "base": _read_variant(repo, outcome.base_tree, paths, "base"),
        "a": _read_variant(repo, outcome.tree_a, paths, "a"),
        "b": _read_variant(repo, outcome.tree_b, paths, "b"),
        "merge": _read_variant(repo, outcome.merged_tree, paths, "merge"),
    }
    found = {name: _findings(variant) for name, variant in variants.items()}
    introduced = found["merge"] - found["base"] - found["a"] - found["b"]
    kinds = sorted({item.split("|", 1)[0] for item in introduced})
    return SemanticRow(
        a=payload.a,
        b=payload.b,
        status="ok",
        base_defects=len(found["base"]),
        a_defects=len(found["a"]),
        b_defects=len(found["b"]),
        merge_defects=len(found["merge"]),
        new_defects=len(introduced),
        new_kinds=",".join(kinds),
        detail="\n".join(sorted(introduced)[:5]),
    )


def _payloads(
    handle: CacheHandle, footprints: list[Footprint], sample_size: int, seed: int
) -> list[ProbePayload]:
    """Sample cleanly merged pairs and collect the Python paths they touch."""
    by_number = {footprint.number: footprint for footprint in footprints}
    rows = handle.conn.execute(
        "SELECT a, b FROM pair WHERE merge_verdict = 'clean'"
    ).fetchall()
    candidates = [
        (int(row["a"]), int(row["b"]))
        for row in rows
        if int(row["a"]) in by_number and int(row["b"]) in by_number
    ]
    rng = random.Random(seed)
    rng.shuffle(candidates)

    payloads: list[ProbePayload] = []
    for a, b in candidates:
        first, second = by_number[a], by_number[b]
        paths = sorted(first.paths_py | second.paths_py)
        if not paths or len(paths) > MAX_PROBE_FILES:
            continue
        payloads.append(
            ProbePayload(
                a=a, b=b,
                ref_a=ChangeRef(a, first.head_sha, first.fork_point),
                ref_b=ChangeRef(b, second.head_sha, second.fork_point),
                paths=tuple(paths),
            )
        )
        if len(payloads) >= sample_size:
            break
    return payloads


def analyze(
    config: StudyConfig, spec: RepoSpec, *, force: bool = False, verbose: bool = True
) -> int:
    """Probe a sample of cleanly merged pairs; return how many were probed."""
    handle = open_cache(config, spec)
    cached = table_count(handle, "semantic_row")
    if cached and not force:
        if verbose:
            print(f"  {spec.slug}: {cached} probe rows cached, skipping", flush=True)
        handle.conn.close()
        return cached
    footprints = load_footprints(handle)
    payloads = _payloads(
        handle, footprints, config.semantic_probe_sample, config.random_seed + 2
    )
    if verbose:
        print(
            f"  {spec.slug}: probing {len(payloads)} cleanly merged pairs", flush=True
        )
    if not payloads:
        handle.conn.close()
        return 0

    rows: list[SemanticRow] = []
    with ProcessPoolExecutor(
        max_workers=config.workers,
        initializer=_worker_init,
        initargs=(str(config.clone_dir(spec)),),
    ) as pool:
        rows.extend(pool.map(_worker_probe, payloads, chunksize=2))
    _store(handle, rows)

    probed = sum(1 for row in rows if row.status == "ok")
    with_defects = sum(1 for row in rows if row.new_defects > 0)
    set_meta(handle, "semantic_probed", str(probed))
    set_meta(handle, "semantic_with_new_defects", str(with_defects))
    if verbose:
        share = with_defects / probed if probed else 0.0
        print(
            f"    {with_defects}/{probed} clean merges introduced a static defect "
            f"({share:.2%})",
            flush=True,
        )
    handle.conn.close()
    return probed


def _store(handle: CacheHandle, rows: list[SemanticRow]) -> None:
    """Persist semantic-probe rows."""
    handle.conn.executemany(
        "INSERT OR REPLACE INTO semantic_row (a, b, status, base_defects, a_defects,"
        " b_defects, merge_defects, new_defects, new_kinds, detail)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (
                r.a, r.b, r.status, r.base_defects, r.a_defects, r.b_defects,
                r.merge_defects, r.new_defects, r.new_kinds, r.detail,
            )
            for r in rows
        ],
    )
    handle.conn.commit()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m mining.semantic_probe [owner/repo ...]``."""
    parser = repo_parser(__doc__)
    parser.add_argument(
        "--force", action="store_true",
        help="recompute even when this stage's rows are already cached",
    )
    args = parser.parse_args(argv)

    config = load_config()
    specs = chosen(config, args.repos)
    for spec in specs:
        print(f"probing {spec.slug} ...", flush=True)
        analyze(config, spec, force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
