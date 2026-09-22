"""Put two changes on a *common base* before merging them.

``git merge-tree A B`` alone is not a test of whether two changes conflict. Its
implicit merge base is ``merge-base(A, B)`` — the *earlier* of the two fork
points — so every commit that landed on the integration branch between the two
forks is counted as one side's change. Measured on django that inflated the
conflict rate by roughly an order of magnitude: pairs whose diffs shared no file
at all were reported as conflicting, purely because of mainline progress.

This module removes mainline from the comparison. The shared base is the
**later** of the two fork points, and the earlier change is forward-ported onto
it — which is what a developer does when they rebase before merging, and what
"N agents dispatched from one base" means. Porting in this direction never has
to revert mainline history, so it succeeds far more often than backporting to
the earlier fork; when it does fail, that is a change-versus-mainline conflict
and is reported as ``rebase_conflict`` rather than being miscounted as a
change-versus-change conflict.
"""

from __future__ import annotations

from dataclasses import dataclass

from mining.git_repo import GitRepo

VERDICT_CLEAN = "clean"
VERDICT_CONFLICT = "conflict"
VERDICT_REBASE_CONFLICT = "rebase_conflict"
VERDICT_ERROR = "error"


@dataclass(frozen=True, slots=True)
class ChangeRef:
    """The two commits that define one change: where it forked, and its tip."""

    number: int
    head: str
    fork: str


@dataclass(frozen=True, slots=True)
class MergeOutcome:
    """The verdict of merging two changes on a common base, plus the trees."""

    verdict: str
    conflicted_paths: tuple[str, ...] = ()
    base: str = ""
    detail: str = ""
    base_tree: str = ""
    tree_a: str = ""
    tree_b: str = ""
    merged_tree: str = ""


def tree_of(repo: GitRepo, commitish: str, cache: dict[str, str]) -> str | None:
    """Resolve a commit or tree-ish to its tree oid, memoised per worker."""
    key = f"tree:{commitish}"
    cached = cache.get(key)
    if cached is not None:
        return cached or None
    completed = repo.run("rev-parse", f"{commitish}^{{tree}}", check=False)
    if completed.returncode != 0:
        cache[key] = ""
        return None
    tree = completed.stdout.decode().strip()
    cache[key] = tree
    return tree


def shared_base(
    repo: GitRepo, fork_a: str, fork_b: str, cache: dict[str, str]
) -> str | None:
    """Return the later of two fork points: the base both changes share.

    Returns None when neither fork is an ancestor of the other — mainline
    diverged between them (a release branch, a force-push), and there is no
    single base that represents "both agents started here".
    """
    if fork_a == fork_b:
        return fork_a
    key = f"base:{fork_a}:{fork_b}"
    cached = cache.get(key)
    if cached is not None:
        return cached or None
    merge_base = repo.merge_base(fork_a, fork_b)
    if merge_base == fork_a:
        result = fork_b
    elif merge_base == fork_b:
        result = fork_a
    else:
        result = ""
    cache[key] = result
    return result or None


def port_to_base(
    repo: GitRepo, change: ChangeRef, base: str, cache: dict[str, str]
) -> tuple[str | None, str]:
    """Return ``base`` plus only ``change``'s own edits, as a tree oid.

    When the change already forked at ``base`` this is just its head tree.
    Otherwise it is the three-way merge a rebase performs: the change's fork
    point is the merge base, ``base`` carries mainline's progress, and the
    change's head carries the edits.
    """
    if change.fork == base:
        return tree_of(repo, change.head, cache), ""
    key = f"port:{change.head}:{base}"
    cached = cache.get(key)
    if cached is not None:
        return (None, cached[1:]) if cached.startswith("!") else (cached, "")
    completed = repo.run(
        "merge-tree", "--write-tree", "--name-only",
        f"--merge-base={change.fork}", base, change.head,
        check=False,
    )
    if completed.returncode == 0:
        lines = completed.stdout.decode().strip().splitlines()
        if lines:
            cache[key] = lines[0]
            return lines[0], ""
        cache[key] = "!port-empty"
        return None, "port-empty"
    reason = "rebase-conflict" if completed.returncode == 1 else "port-failed"
    cache[key] = f"!{reason}"
    return None, reason


def merge_on_common_base(
    repo: GitRepo, first: ChangeRef, second: ChangeRef, cache: dict[str, str]
) -> MergeOutcome:
    """Merge two changes after expressing both against their shared base."""
    base = shared_base(repo, first.fork, second.fork, cache)
    if base is None:
        return MergeOutcome(VERDICT_ERROR, detail="no-linear-common-base")

    base_tree = tree_of(repo, base, cache)
    if base_tree is None:
        return MergeOutcome(VERDICT_ERROR, base=base, detail="missing-base-tree")

    tree_a, reason_a = port_to_base(repo, first, base, cache)
    tree_b, reason_b = port_to_base(repo, second, base, cache)
    if tree_a is None or tree_b is None:
        reason = reason_a or reason_b
        failed = (
            VERDICT_REBASE_CONFLICT if reason == "rebase-conflict" else VERDICT_ERROR
        )
        return MergeOutcome(failed, base=base, detail=reason, base_tree=base_tree)

    merged = repo.merge_tree(tree_a, tree_b, base=base_tree)
    if merged.failed:
        return MergeOutcome(
            VERDICT_ERROR, base=base, detail=merged.reason,
            base_tree=base_tree, tree_a=tree_a, tree_b=tree_b,
        )
    if merged.clean:
        return MergeOutcome(
            VERDICT_CLEAN, base=base, merged_tree=merged.tree,
            base_tree=base_tree, tree_a=tree_a, tree_b=tree_b,
        )
    return MergeOutcome(
        VERDICT_CONFLICT, conflicted_paths=merged.conflicted_paths, base=base,
        base_tree=base_tree, tree_a=tree_a, tree_b=tree_b,
    )


def _depth(repo: GitRepo, fork: str, cache: dict[str, str]) -> int | None:
    """Count the ancestors of a mainline commit, memoised per worker."""
    key = f"depth:{fork}"
    cached = cache.get(key)
    if cached is None:
        completed = repo.run("rev-list", "--count", fork, check=False)
        cached = (
            completed.stdout.decode().strip() if completed.returncode == 0 else ""
        )
        cache[key] = cached
    return int(cached) if cached else None


def _extreme_fork(
    repo: GitRepo, forks: tuple[str, ...], cache: dict[str, str], *, newest: bool
) -> str | None:
    """Return the newest or oldest fork point in a set, by ancestor count."""
    unique = sorted(set(forks))
    if not unique:
        return None
    if len(unique) == 1:
        return unique[0]
    best: tuple[int, str] | None = None
    for fork in unique:
        depth = _depth(repo, fork, cache)
        if depth is None:
            continue
        if best is None or (depth > best[0] if newest else depth < best[0]):
            best = (depth, fork)
    return best[1] if best else None


def latest_fork(
    repo: GitRepo, forks: tuple[str, ...], cache: dict[str, str]
) -> str | None:
    """Return the most recent fork point in a window, by ancestor count."""
    return _extreme_fork(repo, forks, cache, newest=True)


def earliest_fork(
    repo: GitRepo, forks: tuple[str, ...], cache: dict[str, str]
) -> str | None:
    """Return the oldest fork point in a window, by ancestor count.

    This is the base the *k*-window replay dispatches from: every change in the
    window forked at or after it, so none of them is already contained in it and
    each one has to be genuinely re-applied.
    """
    return _extreme_fork(repo, forks, cache, newest=False)
