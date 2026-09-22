"""Validation for the two measurements the whole study rests on.

Two things could be quietly wrong and would invalidate every number: the
hunk-to-node mapper, and the in-memory merge verdict. This module checks both
against independent oracles and reports the agreement rate rather than asserting
it.

- **Mapper.** Every *removed* line is re-attributed with a plain ``ast`` walk
  that knows nothing about MAK's fragment tiling, and the two symbol sets are
  compared per file. The oracle deliberately covers only the base side, so the
  mapper is expected to report additional symbols for newly added nodes; that
  surplus is reported separately rather than counted as error.
- **Merge verdict.** A sample of pairs is merged for real — ``git worktree`` plus
  ``git merge`` — and the outcome is compared with what ``git merge-tree`` said.
- **Naive-merge bias.** The same pairs are also merged the obvious way, with a
  bare ``git merge-tree A B``, to measure how much of the conflict rate that
  method reports is mainline progress rather than a conflict between the two
  changes.
- **Manual audit.** A stratified sample across the 2x2 is written out in a form a
  person can read, so the cells can be checked by hand.
"""

from __future__ import annotations

import ast
import json
import random
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from mining.cache import CacheHandle, get_meta, open_cache, set_meta
from mining.cli import chosen, repo_parser
from mining.config import RepoSpec, StudyConfig, load_config
from mining.diff_parse import parse_diff
from mining.footprints import load_footprints
from mining.git_repo import GitRepo
from mining.hunks_to_nodes import map_file_diff
from mining.pair_analysis import base_overlap, concurrent_pairs
from mining.rebase import (
    VERDICT_CLEAN,
    VERDICT_CONFLICT,
    ChangeRef,
    merge_on_common_base,
)

MAPPER_SAMPLE = 150
MERGE_SAMPLE = 40
NAIVE_SAMPLE = 1200


@dataclass(frozen=True, slots=True)
class MapperAudit:
    """Agreement between the mapper and an independent AST oracle."""

    files_checked: int
    files_agreeing: int
    symbols_expected: int
    symbols_matched: int
    symbols_extra: int

    @property
    def file_accuracy(self) -> float:
        """Share of files whose symbol set matched exactly."""
        return self.files_agreeing / self.files_checked if self.files_checked else 0.0

    @property
    def symbol_recall(self) -> float:
        """Share of oracle symbols the mapper also found."""
        expected = self.symbols_expected
        return self.symbols_matched / expected if expected else 0.0

    @property
    def extra_ratio(self) -> float:
        """Extra symbols per oracle symbol.

        These are not errors. The oracle only attributes *removed* lines against
        the base file, because that is all a simple ``ast`` walk can do; the
        mapper also attributes *added* lines against the head file, so a change
        that introduces a new function legitimately reports a symbol the oracle
        cannot see. The ratio is reported so a reader can judge the gap rather
        than having to trust it.
        """
        expected = self.symbols_expected
        return self.symbols_extra / expected if expected else 0.0


@dataclass(frozen=True, slots=True)
class NaiveBiasAudit:
    """What the obvious way to measure conflicts reports, versus the careful way.

    The naive method is what a reader would reach for first: take every pair of
    pull requests whose lifetimes overlapped and run ``git merge-tree A B``. Its
    merge base is the *earlier* of the two fork points, so every mainline commit
    between the two forks is charged to one side.

    ``naive_conflicts_sharing_no_file`` is the diagnostic: a pair whose two
    diffs have no path in common cannot conflict with each other, so every such
    "conflict" is mainline progress and nothing else.
    """

    naive_checked: int
    naive_conflicts: int
    naive_conflicts_sharing_no_file: int
    corrected_checked: int
    corrected_conflicts: int

    @property
    def naive_rate(self) -> float:
        """Conflict rate a bare ``git merge-tree A B`` reports."""
        return self.naive_conflicts / self.naive_checked if self.naive_checked else 0.0

    @property
    def corrected_rate(self) -> float:
        """Conflict rate once both changes are expressed against a shared base."""
        return (
            self.corrected_conflicts / self.corrected_checked
            if self.corrected_checked
            else 0.0
        )

    @property
    def provably_spurious(self) -> float:
        """Share of the naive method's conflicts whose diffs share no path."""
        return (
            self.naive_conflicts_sharing_no_file / self.naive_conflicts
            if self.naive_conflicts
            else 0.0
        )


@dataclass(frozen=True, slots=True)
class MergeAudit:
    """Agreement between ``git merge-tree`` and a real ``git merge``."""

    checked: int
    agreeing: int
    disagreements: tuple[str, ...]

    @property
    def accuracy(self) -> float:
        """Share of sampled pairs where both methods agreed."""
        return self.agreeing / self.checked if self.checked else 0.0


def oracle_symbols(source: str, lines: set[int]) -> set[str]:
    """Enclosing top-level symbols of a set of 1-indexed lines, via plain ``ast``.

    Deliberately simple and independent of ``mak.node_store.ingestion``: a line
    inside a method is attributed to ``Class.method``, a line inside a top-level
    function to that function, a line inside a class but outside any method to
    the class, and anything else to ``__module__``.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return set()

    found: set[str] = set()
    remaining = set(lines)
    for node in tree.body:
        start, end = _span(node)
        covered = {line for line in remaining if start <= line <= end}
        if not covered:
            continue
        remaining -= covered
        if isinstance(node, ast.ClassDef):
            found |= _class_symbols(node, covered)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            found.add(node.name)
        else:
            found.add("__module__")
    if remaining:
        found.add("__module__")
    return found


def _class_symbols(node: ast.ClassDef, lines: set[int]) -> set[str]:
    """Attribute lines inside a class to ``Class.method`` or to the class itself."""
    found: set[str] = set()
    remaining = set(lines)
    for member in node.body:
        if not isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        start, end = _span(member)
        covered = {line for line in remaining if start <= line <= end}
        if covered:
            remaining -= covered
            found.add(f"{node.name}.{member.name}")
    if remaining:
        found.add(node.name)
    return found


def _span(node: ast.stmt) -> tuple[int, int]:
    """1-indexed inclusive line span of a statement, decorators included."""
    start = node.lineno
    decorators = getattr(node, "decorator_list", [])
    if decorators:
        start = min(start, min(d.lineno for d in decorators))
    return start, node.end_lineno or node.lineno


def audit_mapper(
    config: StudyConfig, spec: RepoSpec, sample_size: int = MAPPER_SAMPLE
) -> MapperAudit:
    """Compare mapper output against the AST oracle over a sample of PRs."""
    handle = open_cache(config, spec)
    repo = GitRepo(config.clone_dir(spec))
    rows = handle.conn.execute(
        "SELECT number, fork_point, head_sha FROM pr_change"
        " WHERE bucket = 'kept' AND status = 'ok'"
    ).fetchall()
    rng = random.Random(config.random_seed + 3)
    chosen = rng.sample(list(rows), min(sample_size, len(rows)))

    files = agreeing = expected = matched = extra = 0
    for row in chosen:
        fork, head = str(row["fork_point"]), str(row["head_sha"])
        for diff in parse_diff(repo.diff_u0(fork, head)):
            if diff.binary or not diff.path.endswith(".py") or diff.old_path is None:
                continue
            base_source = repo.blob(fork, diff.old_path)
            if base_source is None:
                continue
            changed_lines = {
                line
                for hunk in diff.hunks
                if hunk.old_count > 0
                for line in range(hunk.old_start, hunk.old_start + hunk.old_count)
            }
            if not changed_lines:
                continue
            truth = oracle_symbols(base_source, changed_lines)
            if not truth:
                continue
            touches, _failed = map_file_diff(repo, fork, head, diff)
            mapped = {_symbol_of(touch.node_id) for touch in touches}
            files += 1
            expected += len(truth)
            matched += len(truth & mapped)
            extra += len(mapped - truth)
            agreeing += int(truth <= mapped)

    audit = MapperAudit(
        files_checked=files, files_agreeing=agreeing, symbols_expected=expected,
        symbols_matched=matched, symbols_extra=extra,
    )
    set_meta(handle, "mapper_audit", json.dumps({
        "files_checked": files, "files_agreeing": agreeing,
        "symbols_expected": expected, "symbols_matched": matched,
        "symbols_extra": extra,
        "file_accuracy": audit.file_accuracy, "symbol_recall": audit.symbol_recall,
        "extra_ratio": audit.extra_ratio,
    }))
    handle.conn.close()
    return audit


def _symbol_of(node_id: str) -> str:
    """Reduce a MAK node id to the symbol name the oracle would produce."""
    parts = node_id.split("::")
    if len(parts) < 3:
        return "__module__"
    kind, name = parts[1], parts[2]
    if kind in ("module_header", "module_body", "file"):
        return "__module__"
    return name.split("#", 1)[0]


def audit_merge_verdicts(
    config: StudyConfig, spec: RepoSpec, sample_size: int = MERGE_SAMPLE
) -> MergeAudit:
    """Re-merge a sample of pairs for real and compare with the in-memory verdict."""
    handle = open_cache(config, spec)
    repo = GitRepo(config.clone_dir(spec))
    footprints = {f.number: f for f in load_footprints(handle)}
    # Stratified: a uniform sample of a corpus this clean would be almost
    # entirely disjoint pairs, which prove nothing. Half the budget goes to
    # pairs that share a node and half to pairs that conflict — the two cells
    # where a wrong verdict would actually change a conclusion.
    rng = random.Random(config.random_seed + 4)
    strata = (
        "nodes_shared > 0",
        "merge_verdict = 'conflict'",
        "nodes_shared = 0 AND merge_verdict = 'clean'",
    )
    chosen = []
    seen: set[tuple[int, int]] = set()
    per_stratum = max(1, sample_size // len(strata))
    for predicate in strata:
        rows = handle.conn.execute(
            "SELECT a, b, merge_verdict FROM pair"
            f" WHERE merge_verdict IN ('clean', 'conflict') AND {predicate}"
        ).fetchall()
        picked = rng.sample(list(rows), min(per_stratum, len(rows)))
        for row in picked:
            key = (int(row["a"]), int(row["b"]))
            if key not in seen:
                seen.add(key)
                chosen.append(row)

    checked = agreeing = 0
    disagreements: list[str] = []
    cache: dict[str, str] = {}
    for row in chosen:
        a, b = int(row["a"]), int(row["b"])
        if a not in footprints or b not in footprints:
            continue
        first, second = footprints[a], footprints[b]
        outcome = merge_on_common_base(
            repo,
            ChangeRef(a, first.head_sha, first.fork_point),
            ChangeRef(b, second.head_sha, second.fork_point),
            cache,
        )
        if outcome.verdict not in (VERDICT_CLEAN, VERDICT_CONFLICT):
            continue
        real = _real_merge(repo, outcome.base_tree, outcome.tree_a, outcome.tree_b)
        if real is None:
            continue
        checked += 1
        if real == (outcome.verdict == VERDICT_CLEAN):
            agreeing += 1
        else:
            disagreements.append(
                f"#{a} x #{b}: merge-tree={outcome.verdict}, git merge="
                f"{'clean' if real else 'conflict'}"
            )

    audit = MergeAudit(
        checked=checked, agreeing=agreeing, disagreements=tuple(disagreements)
    )
    set_meta(handle, "merge_audit", json.dumps({
        "checked": checked, "agreeing": agreeing, "accuracy": audit.accuracy,
        "disagreements": list(audit.disagreements),
    }))
    handle.conn.close()
    return audit


def _changed_paths(repo: GitRepo, base_tree: str, *trees: str) -> list[str]:
    """Paths that differ from the base in any of the given trees."""
    paths: set[str] = set()
    for tree in trees:
        listing = repo.text(
            "-c", "core.quotePath=false", "diff", "--name-only", base_tree, tree
        )
        paths.update(line for line in listing.splitlines() if line)
    return sorted(paths)


def _real_merge(repo: GitRepo, base_tree: str, tree_a: str, tree_b: str) -> bool | None:
    """Perform an actual ``git merge`` in a throwaway worktree; True when clean.

    The worktree is sparse, covering only the paths either side changed. A full
    checkout of a repository this size costs seconds per pair and nothing outside
    those paths can conflict, since both sides match the base there.
    """
    if not (base_tree and tree_a and tree_b):
        return None
    base_commit = _commit_tree(repo, base_tree, None)
    if base_commit is None:
        return None
    commit_a = _commit_tree(repo, tree_a, base_commit)
    commit_b = _commit_tree(repo, tree_b, base_commit)
    if commit_a is None or commit_b is None:
        return None
    paths = _changed_paths(repo, base_tree, tree_a, tree_b)
    if not paths:
        return True

    worktree = Path(tempfile.mkdtemp(prefix="mak-audit-"))
    try:
        added = repo.run(
            "worktree", "add", "--detach", "--no-checkout", "--quiet",
            str(worktree), commit_a, check=False,
        )
        if added.returncode != 0:
            return None
        for args in (
            ["sparse-checkout", "set", "--no-cone", *paths],
            ["checkout", "--quiet"],
        ):
            step = subprocess.run(
                ["git", "-C", str(worktree), *args], capture_output=True, check=False
            )
            if step.returncode != 0:
                return None
        merged = subprocess.run(
            ["git", "-C", str(worktree), "merge", "--no-commit", "--no-ff", commit_b],
            capture_output=True, check=False,
        )
        return merged.returncode == 0
    finally:
        repo.run("worktree", "remove", "--force", str(worktree), check=False)
        shutil.rmtree(worktree, ignore_errors=True)
        repo.run("worktree", "prune", check=False)


def _commit_tree(repo: GitRepo, tree: str, parent: str | None) -> str | None:
    """Wrap a tree in a commit so ``git merge`` has real ancestry to work with."""
    args = ["commit-tree", tree, "-m", "mak-audit"]
    if parent is not None:
        args.extend(["-p", parent])
    completed = repo.run(*args, check=False)
    if completed.returncode != 0:
        return None
    return completed.stdout.decode().strip()


def audit_naive_bias(
    config: StudyConfig, spec: RepoSpec, sample_size: int = NAIVE_SAMPLE
) -> NaiveBiasAudit:
    """Measure the naive conflict rate against the shared-base one.

    The naive side is sampled from *lifetime-overlapping* pairs, because that is
    the population a study using the obvious definitions would work with. The
    corrected side is the base-overlapping subset of the same sample, merged the
    way the rest of this study merges.
    """
    handle = open_cache(config, spec)
    repo = GitRepo(config.clone_dir(spec))
    footprints = load_footprints(handle)
    pairs = list(concurrent_pairs(footprints))
    rng = random.Random(config.random_seed + 5)
    chosen_pairs = rng.sample(pairs, min(sample_size, len(pairs)))

    cache: dict[str, str] = {}
    naive_checked = naive_conflicts = no_shared_file = 0
    corrected_checked = corrected_conflicts = 0
    for i, j in chosen_pairs:
        first, second = footprints[i], footprints[j]
        if not (first.head_sha and second.head_sha):
            continue
        bare = repo.merge_tree(first.head_sha, second.head_sha)
        if bare.failed:
            continue
        naive_checked += 1
        if not bare.clean:
            naive_conflicts += 1
            if not (first.paths & second.paths):
                no_shared_file += 1
        if not base_overlap(first, second):
            continue
        outcome = merge_on_common_base(
            repo,
            ChangeRef(first.number, first.head_sha, first.fork_point),
            ChangeRef(second.number, second.head_sha, second.fork_point),
            cache,
        )
        if outcome.verdict not in (VERDICT_CLEAN, VERDICT_CONFLICT):
            continue
        corrected_checked += 1
        corrected_conflicts += int(outcome.verdict == VERDICT_CONFLICT)

    audit = NaiveBiasAudit(
        naive_checked=naive_checked,
        naive_conflicts=naive_conflicts,
        naive_conflicts_sharing_no_file=no_shared_file,
        corrected_checked=corrected_checked,
        corrected_conflicts=corrected_conflicts,
    )
    set_meta(handle, "naive_bias", json.dumps({
        "naive_checked": naive_checked, "naive_conflicts": naive_conflicts,
        "naive_conflicts_sharing_no_file": no_shared_file,
        "corrected_checked": corrected_checked,
        "corrected_conflicts": corrected_conflicts,
        "naive_rate": audit.naive_rate, "corrected_rate": audit.corrected_rate,
        "provably_spurious": audit.provably_spurious,
    }))
    handle.conn.close()
    return audit


def write_manual_sample(
    config: StudyConfig, spec: RepoSpec, per_cell: int | None = None
) -> Path:
    """Write a stratified 2x2 sample of pairs for a human to check by hand."""
    handle = open_cache(config, spec)
    size = per_cell if per_cell is not None else config.audit_sample_per_cell
    cells = {
        "conflict_shared_node": "merge_verdict = 'conflict' AND nodes_shared > 0",
        "conflict_no_shared_node": "merge_verdict = 'conflict' AND nodes_shared = 0",
        "clean_shared_node": "merge_verdict = 'clean' AND nodes_shared > 0",
        "clean_no_shared_node": "merge_verdict = 'clean' AND nodes_shared = 0",
    }
    lines = [
        f"# Manual audit sample — {spec.slug}",
        "",
        "Each row is a concurrent pair. `nodes_shared` is what MAK would serialise;",
        "`merge_verdict` is what git's three-way merge did on a common base.",
        "",
        "A cell with no rows is not an omission: in this corpus some cells are",
        "genuinely empty, and that is itself a result.",
        "",
    ]
    stored: list[tuple[str, int, int]] = []
    for cell, predicate in cells.items():
        rows = handle.conn.execute(
            f"SELECT a, b, files_shared, nodes_shared, py_nodes_shared,"
            f" shared_all_append, conflict_files, conflict_paths"
            f" FROM pair WHERE {predicate} ORDER BY a, b LIMIT ?",
            (size,),
        ).fetchall()
        lines.append(f"## {cell} ({len(rows)} sampled)")
        lines.append("")
        lines.append(
            "| PR A | PR B | files shared | nodes shared | py nodes "
            "| all-append | conflicted paths |"
        )
        lines.append("|---|---|---|---|---|---|---|")
        for row in rows:
            paths = str(row["conflict_paths"]).replace("\n", "<br>")[:160]
            lines.append(
                f"| {row['a']} | {row['b']} | {row['files_shared']} "
                f"| {row['nodes_shared']} | {row['py_nodes_shared']} "
                f"| {bool(row['shared_all_append'])} | {paths} |"
            )
            stored.append((cell, int(row["a"]), int(row["b"])))
        lines.append("")

    handle.conn.executemany(
        "INSERT OR REPLACE INTO audit_row (cell, a, b, verdict, detail)"
        " VALUES (?,?,?,'unreviewed','')",
        stored,
    )
    handle.conn.commit()
    lines.extend(_shared_node_detail(handle, size))
    path = config.repo_data_dir(spec) / "audit_sample.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    handle.conn.close()
    return path


def _shared_node_detail(handle: CacheHandle, limit: int) -> list[str]:
    """Per-node detail for shared-node pairs, so the mapping can be checked by eye.

    Counts alone are not auditable. For each pair this prints the node both
    changes wrote to and what each side did to it, which is enough to open the
    two pull requests and confirm the attribution.
    """
    lines = [
        "## Shared-node pairs, node by node",
        "",
        "For each pair, the nodes both sides wrote to and what each side did.",
        "`append` means the side only added lines to that node and removed none,",
        "so the two edits commute.",
        "",
    ]
    pairs = handle.conn.execute(
        "SELECT a, b, merge_verdict FROM pair WHERE py_nodes_shared > 0"
        " ORDER BY a, b LIMIT ?",
        (limit,),
    ).fetchall()
    if not pairs:
        lines.append("_No pair in the sample shares a Python AST node._")
        lines.append("")
        return lines

    for pair in pairs:
        a, b = int(pair["a"]), int(pair["b"])
        shared = handle.conn.execute(
            "SELECT node_id FROM pr_node WHERE number = ? AND is_python = 1"
            "   AND kind != 'file'"
            " INTERSECT"
            " SELECT node_id FROM pr_node WHERE number = ? AND is_python = 1"
            "   AND kind != 'file'"
            " ORDER BY node_id",
            (a, b),
        ).fetchall()
        lines.append(f"### #{a} x #{b} — git said `{pair['merge_verdict']}`")
        lines.append("")
        lines.append("| node | side | change | +lines | -lines | append |")
        lines.append("|---|---|---|---|---|---|")
        for row in shared:
            node_id = str(row["node_id"])
            for number in (a, b):
                touch = handle.conn.execute(
                    "SELECT change_type, lines_added, lines_removed, append_only"
                    " FROM pr_node WHERE number = ? AND node_id = ?",
                    (number, node_id),
                ).fetchone()
                if touch is None:
                    continue
                lines.append(
                    f"| `{node_id}` | #{number} | {touch['change_type']} "
                    f"| {touch['lines_added']} | {touch['lines_removed']} "
                    f"| {bool(touch['append_only'])} |"
                )
        lines.append("")
    return lines


def already_audited(config: StudyConfig, spec: RepoSpec) -> bool:
    """Whether every audit result for this repository is already recorded."""
    handle = open_cache(config, spec)
    done = all(
        get_meta(handle, key) for key in ("mapper_audit", "merge_audit", "naive_bias")
    )
    handle.conn.close()
    return done


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m mining.audit [owner/repo ...]``."""
    parser = repo_parser(__doc__)
    parser.add_argument(
        "--force", action="store_true",
        help="re-audit even when results are already recorded",
    )
    args = parser.parse_args(argv)

    config = load_config()
    specs = chosen(config, args.repos)
    for spec in specs:
        if already_audited(config, spec) and not args.force:
            print(f"auditing {spec.slug} ... cached, skipping", flush=True)
            continue
        print(f"auditing {spec.slug} ...", flush=True)
        mapper = audit_mapper(config, spec)
        print(
            f"  mapper: {mapper.file_accuracy:.2%} of {mapper.files_checked} "
            f"files fully covered;"
            f" symbol recall {mapper.symbol_recall:.2%}"
            f"({mapper.extra_ratio:.2f} extra symbols per oracle symbol, "
            f"mostly newly added nodes)",
            flush=True,
        )
        merge = audit_merge_verdicts(config, spec)
        print(
            f"  merge: {merge.accuracy:.2%} agreement with a real git merge"
            f" over {merge.checked} pairs",
            flush=True,
        )
        for line in merge.disagreements[:5]:
            print(f"    {line}", flush=True)
        naive = audit_naive_bias(config, spec)
        print(
            f"  naive merge-tree: {naive.naive_rate:.2%} conflict rate over "
            f"{naive.naive_checked} lifetime-overlapping pairs vs "
            f"{naive.corrected_rate:.2%} on a shared base; "
            f"{naive.provably_spurious:.1%} of naive conflicts share no file at all",
            flush=True,
        )
        path = write_manual_sample(config, spec)
        print(f"  wrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
