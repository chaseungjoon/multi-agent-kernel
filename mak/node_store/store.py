"""NodeStore: versioned AST fragment storage with commit/rollback semantics.

The store owns version assignment: ``put_node`` ignores any version on
the incoming fragment and stamps it ``current_committed + 1``, so callers never
have to guess the next version. Prior versions are retained on disk, enabling
``revert_node`` (roll a committed node back to its previous version). Fragment
order is preserved as ``order`` metadata so reconstruction emits source in its
original order. All mutations are guarded by a re-entrant lock.

**Retention (Wave 18).** "Prior versions are retained" used to mean *all* of
them: every commit wrote a ``v{n}.py`` and nothing ever removed one, so
``.mak/node_store/`` grew monotonically for the life of a project. A commit now
prunes back to ``version_retention`` versions of that node (the committed one
plus ``N-1`` prior; the floor is 2 because ``revert_node`` needs one prior
version, and ``-1`` restores the old unbounded behaviour). :meth:`gc` applies
the same policy to the whole store and additionally removes fragment
directories no live node id addresses any more.

**Ordering (Wave 18).** ``order`` is assigned 0..n *per file*, so a global sort
on ``order`` alone interleaved every file's node 0, then every file's node 1 —
harmless for reconstruction, which filters by file first, but it presented the
planner's inventory prompt shuffled. Listings sort by ``(file_path, order)`` and
the ordering is memoized behind the store lock, invalidated whenever the
committed set changes; :attr:`generation` exposes that same invalidation signal
so callers can cache derived state of their own.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import logging
import shutil
import textwrap
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from mak.core.atomic import write_text_atomic
from mak.core.exceptions import NodeStoreError, UnsafeNodeIdError
from mak.core.paths import check_node_id, safe_path_under
from mak.core.types import NodeFragment, NodeId
from mak.node_store.ingestion import parse_file_into_fragments

_logger = logging.getLogger(__name__)

# How many on-disk versions of a node the store keeps by default: the committed
# one plus four prior. ``UNBOUNDED_VERSION_RETENTION`` restores the pre-Wave-18
# "keep everything" behaviour for anyone who wants the full history.
DEFAULT_VERSION_RETENTION = 5
# ``revert_node`` rolls back to ``version - 1``, so a store that kept only the
# committed version could never revert. Two is the floor for that reason.
MIN_VERSION_RETENTION = 2
UNBOUNDED_VERSION_RETENTION = -1


def normalize_retention(value: int) -> int:
    """Coerce a retention setting to a value the store can honour.

    Anything negative means unbounded; anything below the floor is raised to it
    rather than rejected, because a store that cannot revert is a worse outcome
    than a config value being quietly widened, and the configuration layer
    already refuses out-of-range values before they reach here.
    """
    if value < 0:
        return UNBOUNDED_VERSION_RETENTION
    return max(MIN_VERSION_RETENTION, value)


def _extract_indent(source: str) -> tuple[str, str]:
    """Return ``(dedented_source, indent_prefix)`` for a node fragment.

    ``textwrap.dedent`` strips the common leading whitespace from every
    non-empty line.  We recover the stripped prefix by comparing the first
    non-empty line before and after dedenting, so the store can re-apply it
    during file reconstruction without losing any relative indentation inside
    the fragment itself.
    """
    dedented = textwrap.dedent(source)
    for orig, ded in zip(source.splitlines(), dedented.splitlines(), strict=False):
        if orig.strip():
            return dedented, orig[: len(orig) - len(ded)]
    return dedented, ""


@dataclasses.dataclass(frozen=True, slots=True)
class FileSyncReport:
    """What :meth:`NodeStore.sync_file` changed for one file. Wave 19.

    Reconciliation is the one startup step that can silently discard a human's
    work, so it says what it did rather than returning a bare id list: an
    operator seeing ``retired`` non-empty is being told a symbol disappeared, and
    a test can assert the *kind* of change rather than inferring it from counts.
    """

    file_path: str
    added: tuple[NodeId, ...] = ()
    updated: tuple[NodeId, ...] = ()
    retired: tuple[NodeId, ...] = ()
    unchanged: tuple[NodeId, ...] = ()

    @property
    def live(self) -> list[NodeId]:
        """Every id this file still has, in the order the parse produced them."""
        return [*self.added, *self.updated, *self.unchanged]

    @property
    def changed(self) -> bool:
        """Whether the store's picture of the file moved at all."""
        return bool(self.added or self.updated or self.retired)


@dataclasses.dataclass(slots=True)
class _StoreSnapshot:
    """Everything a transaction must be able to put back (Wave 19).

    Shallow copies of the three index dicts plus the generation counter. The
    per-node metadata dicts are copied one level down as well, because
    ``commit_node`` replaces them wholesale but ``sync_file`` mutates keys in
    place — a snapshot that shared them would restore the mutation it exists to
    undo.
    """

    nodes: dict[NodeId, NodeFragment]
    pending: dict[NodeId, NodeFragment]
    metadata: dict[NodeId, dict[str, object]]
    generation: int


@dataclasses.dataclass(slots=True)
class _Transaction:
    """Side effects a store transaction holds back until its commit point.

    Three effects are destructive and therefore deferred: the metadata save
    (which is the commit point itself, and runs exactly once no matter how many
    nodes the transaction commits), superseded fragment-directory removals, and
    version pruning. Pruning in particular *must* wait — it deletes the very
    version files a rollback restores the in-memory index to.
    """

    snapshot: _StoreSnapshot
    doomed_dirs: list[NodeId] = dataclasses.field(default_factory=list)
    prune_ids: list[NodeId] = dataclasses.field(default_factory=list)
    # Version files written while this transaction was open. Every one is newer
    # than anything the snapshot holds — the store only ever stamps
    # ``current + 1`` — so a rollback removes all of them and leaves no debris
    # beside the state it restored.
    written: list[tuple[NodeId, int]] = dataclasses.field(default_factory=list)
    depth: int = 0


class NodeStore:
    """Versioned fragment store backed by ``.mak/node_store/`` on disk."""

    def __init__(
        self,
        store_root: Path,
        *,
        version_retention: int = DEFAULT_VERSION_RETENTION,
    ) -> None:
        self._root = store_root
        self._root.mkdir(parents=True, exist_ok=True)

        self._nodes: dict[NodeId, NodeFragment] = {}
        self._pending: dict[NodeId, NodeFragment] = {}
        self._metadata: dict[NodeId, dict[str, object]] = {}
        self._lock = threading.RLock()
        self._retention = normalize_retention(version_retention)
        # Bumped whenever the *committed* set changes. Staging and rollback do
        # not touch it: they leave ``_nodes`` alone, so anything derived from the
        # committed set is still valid and need not be rebuilt.
        self._generation = 0
        self._sorted_cache: list[NodeId] | None = None
        # The open transaction, if any. Re-entrant by depth: ``sync_file`` opens
        # one and calls ``commit_node``, which opens another; only the outermost
        # one commits or rolls back.
        self._txn: _Transaction | None = None
        # file path -> digest of the content MAK last materialized for it. The
        # startup reconciliation's "did a human edit this?" question is answered
        # against this, not against the store's own fragments.
        self._file_state: dict[str, dict[str, object]] = {}

        self._load_from_disk()
        self._load_file_state()
        self._invalidate()

    @property
    def generation(self) -> int:
        """Monotonic counter bumped on every change to the committed node set.

        A caller that derives an index from the store (the session's cross-file
        symbol index) caches it against this value and rebuilds only when the
        number moves, without having to learn *what* changed.
        """
        with self._lock:
            return self._generation

    @property
    def version_retention(self) -> int:
        """How many on-disk versions of a node are kept (``-1`` = unbounded)."""
        return self._retention

    def _invalidate(self) -> None:
        """Drop memoized ordering and bump the generation. Call under the lock."""
        self._generation += 1
        self._sorted_cache = None

    def _sort_key(self, node_id: NodeId) -> tuple[str, int]:
        """Sort key grouping a file's nodes together, in source order within it."""
        return (str(node_id).split("::", 1)[0], self._order(node_id))

    def _sorted_node_ids(self) -> list[NodeId]:
        """Return committed ids in ``(file, order)``, memoized until a change.

        The sort itself is O(n log n) over the whole store and ``list_nodes`` is
        called once per enrichment layer per dispatch *and* per retry, so
        re-sorting on each call was a measurable share of a wide plan's cost. The
        returned list is the cache: callers copy or filter it, never mutate it.
        """
        if self._sorted_cache is None:
            self._sorted_cache = sorted(self._nodes, key=self._sort_key)
        return self._sorted_cache

    def _fragment_dir(self, node_id: NodeId) -> Path:
        """Return a node's on-disk version directory, refusing an escaping id.

        The single choke point for every fragment read, write, and delete, which
        is why containment is asserted *here* rather than in ``put_node``: an id
        that must not be written must not be probed or removed either, and one
        guard covers all three. ``_delete_fragment_dir`` already checked
        containment before deleting; this extends the same rule to the write
        path, which had none.

        Containment **only** — ``mak_dir_name=None``. Whether an id names
        legitimate project source is a question for the planner and the
        reconstructor, not for the store: the Wave 11 prune exists precisely to
        remove the ``.mak/…`` nodes an older MAK ingested, and it cannot delete
        what it cannot address.
        """
        check_node_id(str(node_id), mak_dir_name=None)
        relative = str(node_id).replace("::", "/")
        return safe_path_under(self._root, relative, label="node id")

    def _load_from_disk(self) -> None:
        meta_path = self._root / "metadata.json"
        if not meta_path.exists():
            return
        data = self._read_metadata(meta_path)
        for nid_str, meta in data.items():
            nid = NodeId(nid_str)
            self._metadata[nid] = meta
            # A retired node keeps its metadata and its versions but is not live:
            # loading it back into ``_nodes`` would resurrect, on the next run,
            # exactly the symbol the previous run recorded as deleted.
            if meta.get("retired", False):
                continue
            version = int(meta.get("version", 1))
            frag_file = self._fragment_dir(nid) / f"v{version}.py"
            if frag_file.exists():
                self._nodes[nid] = NodeFragment(
                    node_id=nid,
                    kind=str(meta.get("kind", "unknown")),
                    source=frag_file.read_text("utf-8"),
                    version=version,
                )

    @staticmethod
    def _read_metadata(meta_path: Path) -> dict[str, Any]:
        """Load the metadata index, quarantining it if it cannot be read.

        A truncated or malformed index used to raise straight out of the
        constructor, which made the store *unopenable*: every subsequent run died
        before it could do anything about it, and the only recovery was deleting
        ``.mak/`` — i.e. discarding the work the store existed to protect. The
        file is moved aside instead and the store starts clean, so the fragments
        on disk survive and the operator has the bad index to inspect.
        """
        try:
            data = json.loads(meta_path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            quarantine = meta_path.with_suffix(".json.corrupt")
            try:
                meta_path.replace(quarantine)
            except OSError:  # pragma: no cover - unwritable store dir
                quarantine = meta_path
            _logger.warning(
                "node store metadata at %s is unreadable (%s); moved it to %s and "
                "started with an empty index. Stored fragments are untouched.",
                meta_path,
                exc,
                quarantine,
            )
            return {}
        if not isinstance(data, dict):
            _logger.warning(
                "node store metadata at %s is not a JSON object; ignoring it.",
                meta_path,
            )
            return {}
        return {str(k): v for k, v in data.items() if isinstance(v, dict)}

    def _save_metadata(self) -> None:
        meta_path = self._root / "metadata.json"
        data = {str(k): v for k, v in self._metadata.items()}
        # Atomic: this file is rewritten after every commit, and a truncation
        # here is what makes the whole store unreadable on the next run.
        write_text_atomic(meta_path, json.dumps(data, indent=2))

    # -- what MAK last put on disk (Wave 19) -------------------------------

    def _file_state_path(self) -> Path:
        return self._root / "file_state.json"

    def _load_file_state(self) -> None:
        """Load the last-materialized digests, starting empty if unreadable.

        A missing or corrupt sidecar is not a failure: every file simply reads as
        "MAK has never materialized this", which makes the startup reconciliation
        treat the working tree as authoritative. That is the safe direction to be
        wrong in — it adopts the human's content rather than overwriting it.
        """
        path = self._file_state_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            _logger.warning(
                "could not read %s (%s); treating every file as unseen.", path, exc
            )
            return
        if not isinstance(data, dict):
            return
        self._file_state = {
            str(k): v for k, v in data.items() if isinstance(v, dict)
        }

    def _save_file_state(self) -> None:
        write_text_atomic(
            self._file_state_path(), json.dumps(self._file_state, indent=2)
        )

    @staticmethod
    def content_digest(content: str) -> str:
        """Return the digest the store records for materialized file content."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def record_materialized(self, file_path: str, content: str) -> None:
        """Record that MAK just wrote ``content`` to ``file_path``.

        This is the reference point the next session compares the working tree
        against. Without it "has a human edited this file?" is unanswerable — the
        store's own fragments cannot distinguish *the edit MAK made* from *an
        edit someone made afterwards*, which is why re-ingestion used to discard
        human work without noticing.
        """
        with self._lock:
            self._file_state[file_path] = {
                "sha256": self.content_digest(content),
                "at": time.time(),
            }
            self._save_file_state()

    def forget_materialized(self, file_path: str) -> None:
        """Drop a file's materialization record (it no longer exists)."""
        with self._lock:
            if self._file_state.pop(file_path, None) is not None:
                self._save_file_state()

    def materialized_digest(self, file_path: str) -> str | None:
        """Digest of the content MAK last wrote for ``file_path``, if known."""
        with self._lock:
            value = self._file_state.get(file_path, {}).get("sha256")
            return value if isinstance(value, str) else None

    # -- transactions (Wave 19) -------------------------------------------

    def _snapshot(self) -> _StoreSnapshot:
        """Capture the in-memory index. Call under the lock."""
        return _StoreSnapshot(
            nodes=dict(self._nodes),
            pending=dict(self._pending),
            metadata={nid: dict(meta) for nid, meta in self._metadata.items()},
            generation=self._generation,
        )

    def _restore(self, snapshot: _StoreSnapshot) -> None:
        """Put a snapshot back. Call under the lock."""
        self._nodes = snapshot.nodes
        self._pending = snapshot.pending
        self._metadata = snapshot.metadata
        self._generation = snapshot.generation
        self._sorted_cache = None

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        """Advance several nodes together, or not at all.

        Without this, ``commit_node`` was its own commit point: it mutated the
        index, deleted superseded fragment directories, pruned old versions, and
        *then* saved the metadata — so a failure anywhere after the first of
        those left the store's four representations disagreeing, with the files a
        rollback needed already deleted. Inside a transaction those three
        destructive effects are held back and :meth:`_save_metadata` runs exactly
        once, at the end: **that save is the commit point.** Before it, nothing
        durable has changed and the in-memory index can be put back verbatim;
        after it, the deferred deletions are drained and the transaction is done.

        Re-entrant by depth, because the operations that need it nest —
        ``sync_file`` opens one and calls ``commit_node``, which opens another.
        Only the outermost block commits or rolls back.

        Held under the store lock for its whole duration, so no other thread can
        observe the half-advanced index. The lock is re-entrant, so a caller
        already holding it (every public method does) nests fine.
        """
        with self._lock:
            if self._txn is not None:
                self._txn.depth += 1
                try:
                    yield
                finally:
                    self._txn.depth -= 1
                return
            txn = _Transaction(snapshot=self._snapshot())
            self._txn = txn
            try:
                yield
                self._save_metadata()
            except BaseException:
                # Includes KeyboardInterrupt: an interrupted transaction must not
                # leave the index ahead of the metadata on disk either.
                self._restore(txn.snapshot)
                self._discard_written(txn)
                raise
            finally:
                self._txn = None
            # Past the commit point: the deletions can no longer cost us a
            # rollback target, so drain them. Each stays best-effort.
            for node_id in txn.doomed_dirs:
                try:
                    self._delete_fragment_dir(node_id)
                except NodeStoreError as exc:
                    _logger.warning(
                        "could not remove superseded fragment directory: %s", exc
                    )
            for node_id in txn.prune_ids:
                self._prune_versions(node_id)

    def _discard_written(self, txn: _Transaction) -> None:
        """Remove version files this transaction wrote. Call under the lock.

        Only ever files newer than the restored state — the store stamps
        ``current + 1`` and never reuses a number — so this cannot delete
        anything the snapshot depends on. Best-effort: a file that will not
        unlink is disk hygiene, and the rollback itself has already succeeded.
        """
        for node_id, version in txn.written:
            try:
                (self._fragment_dir(node_id) / f"v{version}.py").unlink(
                    missing_ok=True
                )
            except (OSError, UnsafeNodeIdError) as exc:  # pragma: no cover
                _logger.warning(
                    "could not discard rolled-back version %d of '%s': %s",
                    version,
                    node_id,
                    exc,
                )

    def _write_fragment_to_disk(self, fragment: NodeFragment) -> None:
        frag_dir = self._fragment_dir(fragment.node_id)
        frag_dir.mkdir(parents=True, exist_ok=True)
        (frag_dir / f"v{fragment.version}.py").write_text(
            fragment.source, encoding="utf-8"
        )
        if self._txn is not None:
            self._txn.written.append((fragment.node_id, fragment.version))

    def _next_version(self, node_id: NodeId) -> int:
        committed = self._nodes.get(node_id)
        return committed.version + 1 if committed is not None else 1

    def _order(self, node_id: NodeId) -> int:
        value = self._metadata.get(node_id, {}).get("order", 0)
        return value if isinstance(value, int) else 0

    def node_order(self, node_id: NodeId) -> int | None:
        """Return a node's source-order index, or ``None`` if the store has none.

        ``None`` is a real answer, not a zero: an id the store has never seen has
        no position, and callers ordering a mix of known and unknown ids
        (``map_returned_sources`` folding an agent's fragments back into a
        whole-file grant) must be able to tell "first" from "unplaced".
        """
        with self._lock:
            value = self._metadata.get(node_id, {}).get("order")
            return value if isinstance(value, int) else None

    def get_node(self, node_id: NodeId, version: int | None = None) -> NodeFragment:
        """Return the latest committed fragment, or a specific prior version.

        A **retired** node answers only for an explicit ``version``. Retiring
        records that a symbol was deleted while keeping its history addressable,
        so refusing a versioned read of one would make "retired, not destroyed"
        an empty promise; but it has no *current* version to return, and handing
        one back would resurrect a symbol the working tree no longer has.
        """
        with self._lock:
            if node_id not in self._nodes:
                if version is not None and node_id in self._metadata:
                    return self._historical(node_id, version)
                raise NodeStoreError(f"node not found: {node_id}")
            latest = self._nodes[node_id]
            if version is None or version == latest.version:
                return latest
            return self._historical(node_id, version, kind=latest.kind)

    def _historical(
        self, node_id: NodeId, version: int, *, kind: str | None = None
    ) -> NodeFragment:
        """Read one on-disk version of a node. Call under the lock."""
        frag_file = self._fragment_dir(node_id) / f"v{version}.py"
        if not frag_file.exists():
            raise NodeStoreError(f"version {version} not found for node: {node_id}")
        if kind is None:
            kind = str(self._metadata.get(node_id, {}).get("kind", "unknown"))
        return NodeFragment(
            node_id=node_id,
            kind=kind,
            source=frag_file.read_text("utf-8"),
            version=version,
        )

    def put_node(self, node_id: NodeId, fragment: NodeFragment) -> NodeFragment:
        """Stage a new version of a node (uncommitted). Returns the versioned fragment.

        The store assigns the version (``current + 1``); the incoming fragment's
        ``version`` field is ignored so callers cannot collide or skip versions.

        Incoming source is dedented unconditionally: agents receive dedented
        fragments and should return them at column 0, but if an agent writes
        code at class-body indentation (the natural mental model), the strip
        here makes validation and reconstruction correct either way.
        """
        with self._lock:
            dedented_source = textwrap.dedent(fragment.source)
            staged = dataclasses.replace(
                fragment, source=dedented_source, version=self._next_version(node_id)
            )
            self._pending[node_id] = staged
            self._write_fragment_to_disk(staged)
            return staged

    def commit_node(self, node_id: NodeId) -> None:
        """Promote a pending fragment to committed.

        Committing a *whole-file* node (a bare ``path.py`` with no ``::kind::name``)
        supersedes that file's fragment nodes: the whole file now defines the file,
        so any previously-ingested ``path.py::…`` fragments are dropped. Otherwise
        reconstruction would concatenate the whole file *and* its old fragments and
        emit every top-level symbol twice.

        Inside a :meth:`transaction` the destructive half of that — deleting the
        superseded directories, and pruning this node's old versions — is deferred
        to the transaction's commit point, and the metadata save happens once for
        the whole transaction rather than once per node.

        Outside one, the metadata save is still this method's own commit point,
        but it is no longer allowed to leave memory ahead of disk: a failure
        there restores the entries this call changed before re-raising. That
        window is the third of Wave 19's confirmed 19.1 failure modes.
        """
        with self._lock:
            if node_id not in self._pending:
                raise NodeStoreError(f"no pending version for node: {node_id}")
            snapshot = None if self._txn is not None else self._snapshot()
            fragment = self._pending.pop(node_id)
            self._nodes[node_id] = fragment
            existing = self._metadata.get(node_id, {})
            order = existing.get("order", len(self._metadata))
            self._metadata[node_id] = {
                "kind": fragment.kind,
                "version": fragment.version,
                "order": order,
                "indent_prefix": existing.get("indent_prefix", ""),
            }
            if "::" not in str(node_id):
                self._supersede_fragments(str(node_id))
            self._invalidate()
            if self._txn is not None:
                self._txn.prune_ids.append(node_id)
                return
            self._prune_versions(node_id)
            try:
                self._save_metadata()
            except OSError:
                if snapshot is not None:
                    self._restore(snapshot)
                raise

    def uncommit_node(self, node_id: NodeId) -> None:
        """Return a node to *absent* — the undo :meth:`revert_node` cannot express.

        ``revert_node`` rolls back to ``version - 1``, so it has no answer for a
        node whose committed version is 1: there is no version 0, and the node's
        correct prior state is not an older version but no node at all. That gap
        is why a failed first-version commit used to leave the store holding work
        the caller had already rejected.

        The on-disk version directory is left alone: a rollback that deleted it
        would make the failure destructive, and the id stays addressable for the
        next attempt.
        """
        with self._lock:
            self._nodes.pop(node_id, None)
            self._pending.pop(node_id, None)
            self._metadata.pop(node_id, None)
            self._invalidate()
            if self._txn is None:
                self._save_metadata()

    def _supersede_fragments(self, file_path: str) -> None:
        """Drop a file's ``path::…`` fragments — a whole-file node now defines it.

        In-memory *and* on disk. Dropping only the former left every superseded
        fragment's version directory under ``.mak/node_store/`` forever, with
        nothing left in the store that could ever address it again — the second
        half of Wave 18's node-store growth finding. Removal is best-effort: a
        directory that will not delete is a disk-hygiene problem, never a reason
        to fail the commit that supersedes it.

        Inside a transaction the removal is *queued* instead of performed. The
        whole-file commit that supersedes a file's fragments is exactly the case
        where a later failure has to put those fragments back, and it cannot put
        back a directory this method already deleted.
        """
        prefix = f"{file_path}::"
        superseded = {
            nid
            for group in (self._nodes, self._pending, self._metadata)
            for nid in group
            if str(nid).startswith(prefix)
        }
        for nid in superseded:
            self._nodes.pop(nid, None)
            self._pending.pop(nid, None)
            self._metadata.pop(nid, None)
            if self._txn is not None:
                self._txn.doomed_dirs.append(nid)
                continue
            try:
                self._delete_fragment_dir(nid)
            except NodeStoreError as exc:
                _logger.warning(
                    "could not remove superseded fragment directory: %s", exc
                )
        if superseded:
            self._invalidate()

    def rollback_node(self, node_id: NodeId) -> None:
        """Discard a pending (uncommitted) fragment."""
        with self._lock:
            if node_id not in self._pending:
                return
            frag = self._pending.pop(node_id)
            frag_file = self._fragment_dir(node_id) / f"v{frag.version}.py"
            # Only delete the file if this version was never committed.
            committed = self._nodes.get(node_id)
            never_committed = committed is None or committed.version != frag.version
            if frag_file.exists() and never_committed:
                frag_file.unlink()

    def revert_node(self, node_id: NodeId) -> NodeFragment:
        """Roll a committed node back to its immediately previous version.

        This is a *version* operation, not the undo for an arbitrary commit: a
        node whose committed version is 1 has no previous version and raises.
        Rolling back a whole commit — including its first-version nodes, its
        superseded fragments, and its metadata — is what :meth:`transaction`
        does, and :meth:`uncommit_node` is the piece of it this method cannot
        express.
        """
        with self._lock:
            if node_id not in self._nodes:
                raise NodeStoreError(f"node not found: {node_id}")
            current = self._nodes[node_id]
            previous = self.get_node(node_id, version=current.version - 1)
            self._nodes[node_id] = previous
            self._metadata[node_id] = {
                **self._metadata.get(node_id, {}),
                "kind": previous.kind,
                "version": previous.version,
            }
            self._invalidate()
            self._save_metadata()
            return previous

    def retire_node(self, node_id: NodeId) -> bool:
        """Retire a node whose symbol no longer exists in the source. Wave 19.

        Deleting a symbol is not the same operation as never having ingested it.
        :meth:`remove_node` is the maintenance hard delete — it destroys the
        node's whole version history — and using it to record "the human deleted
        this function" would throw away exactly the history the store exists to
        keep. A retired node is instead dropped from the *live* set (no listing,
        no reconstruction, no planner inventory) while its metadata entry and its
        on-disk versions stay: ``get_node(nid, version=n)`` still answers, and
        because the id remains in ``_metadata`` the :meth:`gc` orphan sweep —
        which maps forward from known ids — still protects its directory.

        Returns whether anything was retired.
        """
        with self._lock:
            if node_id not in self._nodes and node_id not in self._metadata:
                return False
            fragment = self._nodes.pop(node_id, None)
            self._pending.pop(node_id, None)
            meta = dict(self._metadata.get(node_id, {}))
            meta["retired"] = True
            if fragment is not None:
                meta.setdefault("kind", fragment.kind)
                meta["version"] = fragment.version
            self._metadata[node_id] = meta
            self._invalidate()
            if self._txn is None:
                self._save_metadata()
            return True

    def is_retired(self, node_id: NodeId) -> bool:
        """Whether the store holds this id only as retired history."""
        with self._lock:
            return bool(self._metadata.get(node_id, {}).get("retired", False))

    def list_versions(self, node_id: NodeId) -> list[int]:
        """List all on-disk version numbers for a node, ascending."""
        with self._lock:
            frag_dir = self._fragment_dir(node_id)
            if not frag_dir.exists():
                return []
            versions = [
                int(p.stem[1:])
                for p in frag_dir.glob("v*.py")
                if p.stem[1:].isdigit()
            ]
            return sorted(versions)

    def _prune_versions(self, node_id: NodeId) -> int:
        """Delete a node's oldest on-disk versions past the retention policy.

        Called after every commit, so growth is bounded as it happens rather than
        needing a periodic sweep. The *newest* ``retention`` versions survive,
        which always includes the committed one (a commit assigns the highest
        version number there is) and — for any retention at or above the floor of
        2 — the one ``revert_node`` would roll back to.

        Best-effort: an unlink that fails leaves a stale file behind, which is
        exactly the condition before this existed, and is never worth failing a
        commit over.
        """
        if self._retention < 0:
            return 0
        versions = self.list_versions(node_id)
        doomed = versions[: max(0, len(versions) - self._retention)]
        if not doomed:
            return 0
        frag_dir = self._fragment_dir(node_id)
        removed = 0
        for version in doomed:
            try:
                (frag_dir / f"v{version}.py").unlink()
                removed += 1
            except OSError as exc:  # pragma: no cover - unwritable store dir
                _logger.warning(
                    "could not prune version %d of '%s': %s", version, node_id, exc
                )
        return removed

    def gc(self) -> dict[str, int]:
        """Apply the retention policy to the whole store; return what it removed.

        Two kinds of garbage, both of which accumulate only in a store written by
        an older MAK: versions beyond the retention policy (every commit now
        prunes its own), and fragment directories no live node id addresses any
        more (superseded fragments now delete theirs). Returns
        ``{"versions": n, "directories": n}``.
        """
        with self._lock:
            versions = sum(self._prune_versions(nid) for nid in self._nodes)
            directories = 0
            for orphan in self._orphan_fragment_dirs():
                try:
                    shutil.rmtree(orphan)
                    directories += 1
                except OSError as exc:  # pragma: no cover - unwritable store dir
                    _logger.warning("could not remove orphan '%s': %s", orphan, exc)
            return {"versions": versions, "directories": directories}

    def _orphan_fragment_dirs(self) -> list[Path]:
        """Directories under the store root that no live node id addresses.

        Derived by *forward* mapping — every known id to its directory — never by
        parsing a path back into an id, which is ambiguous: a file path contains
        the same ``/`` separator that ``::`` becomes on disk, so ``a/b.py`` and
        ``a/b.py::function::f`` nest inside one another. A directory that is an
        ancestor of a live node's directory is therefore kept even when nothing
        addresses it directly, and only the topmost orphan of a subtree is
        returned so the caller removes each one exactly once.
        """
        root = self._root.resolve()
        known: set[Path] = set()
        for nid in {*self._nodes, *self._pending, *self._metadata}:
            try:
                known.add(self._fragment_dir(nid).resolve())
            except (UnsafeNodeIdError, OSError):
                continue
        orphans: list[Path] = []
        for path in sorted(root.rglob("*")):
            if not path.is_dir():
                continue
            try:
                resolved = path.resolve()
            except OSError:  # pragma: no cover - unreadable store dir
                continue
            if any(resolved.is_relative_to(found) for found in orphans):
                continue  # already covered by the subtree root above it
            if resolved in known or any(k.is_relative_to(resolved) for k in known):
                continue
            orphans.append(resolved)
        return orphans

    def list_nodes(self, file_path: str | None = None) -> list[NodeId]:
        """List committed node IDs in source order, optionally filtered by file.

        Ordering is ``(file_path, order)``: ``order`` is assigned per file, so
        sorting on it alone grouped every file's node 0 together and shuffled the
        planner's inventory prompt into an order no file actually has.

        A *whole-file* node — a bare path id equal to ``file_path`` with no
        ``::kind::name`` suffix — supersedes all fragment nodes for that file.
        When one exists, only that node is returned for the file; stale
        ``path::kind::name`` fragments are ignored so reconstruction uses the
        authoritative whole-file content instead of concatenating both.

        Without ``file_path``, fragment nodes for files that have a whole-file
        node are omitted so the planner does not offer them as write targets.
        """
        with self._lock:
            nodes = self._sorted_node_ids()
            if file_path is None:
                whole_file_paths = {
                    str(nid) for nid in nodes if "::" not in str(nid)
                }
                return [
                    nid for nid in nodes
                    if "::" not in str(nid)
                    or str(nid).split("::", 1)[0] not in whole_file_paths
                ]
            whole_file_nid = NodeId(file_path)
            if whole_file_nid in self._nodes:
                return [whole_file_nid]
            prefix = f"{file_path}::"
            return [nid for nid in nodes if str(nid).startswith(prefix)]

    def list_all_nodes(self) -> list[NodeId]:
        """Every committed node id in source order, superseded fragments included.

        ``list_nodes`` hides a file's ``path::…`` fragments once a whole-file node
        exists, which is right for planning and reconstruction but wrong for
        maintenance: a prune must be able to see — and evict — every id the store
        actually holds.
        """
        with self._lock:
            return list(self._sorted_node_ids())

    def remove_node(self, node_id: NodeId) -> bool:
        """Delete a node outright: committed and pending state, metadata, files.

        Returns whether anything was removed. This is a *maintenance* operation
        (the Wave 11 prune of nodes that should never have been ingested), not
        part of the edit path — an ordinary rejected edit is rolled back or
        reverted, never removed. On-disk versions are deleted only when the
        fragment directory really lies inside the store root.
        """
        with self._lock:
            existed = (
                node_id in self._nodes
                or node_id in self._pending
                or node_id in self._metadata
            )
            if not existed:
                return False
            self._delete_fragment_dir(node_id)
            self._nodes.pop(node_id, None)
            self._pending.pop(node_id, None)
            self._metadata.pop(node_id, None)
            self._invalidate()
            self._save_metadata()
            return True

    def _delete_fragment_dir(self, node_id: NodeId) -> None:
        """Remove a node's on-disk version directory, if it is under the root."""
        root = self._root.resolve()
        frag_dir = self._fragment_dir(node_id)
        try:
            resolved = frag_dir.resolve()
            if not resolved.is_dir() or resolved == root:
                return
            if not resolved.is_relative_to(root):
                return
            shutil.rmtree(resolved)
        except OSError as exc:
            raise NodeStoreError(
                f"cannot remove stored versions of '{node_id}': {exc}"
            ) from exc

    def parse_file_into_nodes(
        self, file_path: str, source: str | None = None
    ) -> list[NodeId]:
        """Parse a file into the store and return its live node IDs.

        A thin wrapper over :meth:`sync_file`, kept because it is the name every
        caller already uses. It used to *skip* a file that already had a
        whole-file node — ignoring the ``source`` it was handed — which is how a
        session could hand an agent content the working tree had not held for
        days. Synchronizing is now the behaviour; see :meth:`sync_file`.
        """
        return self.sync_file(file_path, source).live

    def sync_file(self, file_path: str, source: str | None) -> FileSyncReport:
        r"""Make the store's picture of ``file_path`` match ``source``. Wave 19.

        Re-ingestion used to be write-only and version-blind: it wrote whatever
        fragments the current source produced, never removed a symbol that had
        disappeared, stamped every fragment version 1 (discarding the node's edit
        history), and — for a file already held as one whole-file node — returned
        without looking at ``source`` at all. The three observable consequences
        were a deleted function that kept reconstructing, an edit history that
        reset on every run, and a human's change to a whole-file node silently
        reverted.

        The four cases, all inside **one transaction** so a mid-file failure
        leaves the store exactly as the previous session left it:

        * **file unknown** — fresh ingestion, versions start at 1 (unchanged).
        * **whole-file node** — if ``source`` differs, commit it as the next
          version of that node. The node stays whole-file: re-fragmenting it
          would create the stale siblings the old early return was avoiding.
        * **fragment nodes** — diff the new parse against the committed set.
          Changed fragments advance to ``_next_version``; identical ones are left
          untouched (no version churn for an unedited file); committed ids the
          new parse no longer contains are :meth:`retire_node`\\ d.
        * **``source is None``** — the file is gone from disk; retire every live
          node it had.

        ``source`` is never re-read from disk here: the caller has already read
        it, and a second read is a second answer.
        """
        with self._lock, self.transaction():
            if source is None:
                return self._sync_deleted_file(file_path)
            whole_file_nid = NodeId(file_path)
            if whole_file_nid in self._nodes:
                return self._sync_whole_file(whole_file_nid, source)
            if self._live_ids_for(file_path):
                return self._sync_fragments(file_path, source)
            return self._ingest_new_file(file_path, source)

    def _live_ids_for(self, file_path: str) -> list[NodeId]:
        """Return committed, non-retired ids for ``file_path``. Under the lock."""
        prefix = f"{file_path}::"
        return [
            nid
            for nid in self._sorted_node_ids()
            if str(nid) == file_path or str(nid).startswith(prefix)
        ]

    def _sync_deleted_file(self, file_path: str) -> FileSyncReport:
        """Retire every live node of a file that no longer exists on disk."""
        retired = tuple(self._live_ids_for(file_path))
        for nid in retired:
            self.retire_node(nid)
        if retired:
            self.forget_materialized(file_path)
        return FileSyncReport(file_path=file_path, retired=retired)

    def _sync_whole_file(
        self, node_id: NodeId, source: str
    ) -> FileSyncReport:
        """Advance a whole-file node to the working tree's content."""
        current = self._nodes[node_id]
        if current.source == textwrap.dedent(source):
            return FileSyncReport(
                file_path=str(node_id), unchanged=(node_id,)
            )
        self.put_node(
            node_id,
            NodeFragment(
                node_id=node_id,
                kind=current.kind,
                source=source,
                version=current.version,
            ),
        )
        self.commit_node(node_id)
        return FileSyncReport(file_path=str(node_id), updated=(node_id,))

    def _sync_fragments(self, file_path: str, source: str) -> FileSyncReport:
        """Diff a file's committed fragments against a fresh parse."""
        fragments = parse_file_into_fragments(file_path, source)
        seen: set[NodeId] = set()
        added: list[NodeId] = []
        updated: list[NodeId] = []
        unchanged: list[NodeId] = []
        for order, frag in enumerate(fragments):
            dedented_source, prefix = _extract_indent(frag.source)
            seen.add(frag.node_id)
            committed = self._nodes.get(frag.node_id)
            meta = dict(self._metadata.get(frag.node_id, {}))
            meta["order"] = order
            meta["indent_prefix"] = prefix
            meta.pop("retired", None)
            self._metadata[frag.node_id] = meta
            if committed is not None and committed.source == dedented_source:
                unchanged.append(frag.node_id)
                continue
            self.put_node(
                frag.node_id,
                dataclasses.replace(frag, source=dedented_source),
            )
            self.commit_node(frag.node_id)
            # commit_node rewrites the metadata entry from the fragment, so the
            # order/indent this parse observed are re-applied on top of it.
            self._metadata[frag.node_id]["order"] = order
            self._metadata[frag.node_id]["indent_prefix"] = prefix
            (updated if committed is not None else added).append(frag.node_id)
        retired = tuple(
            nid for nid in self._live_ids_for(file_path) if nid not in seen
        )
        for nid in retired:
            self.retire_node(nid)
        return FileSyncReport(
            file_path=file_path,
            added=tuple(added),
            updated=tuple(updated),
            retired=retired,
            unchanged=tuple(unchanged),
        )

    def _ingest_new_file(self, file_path: str, source: str) -> FileSyncReport:
        """First ingestion of a file the store has never held."""
        fragments = parse_file_into_fragments(file_path, source)
        added: list[NodeId] = []
        for order, frag in enumerate(fragments):
            dedented_source, prefix = _extract_indent(frag.source)
            staged = dataclasses.replace(frag, source=dedented_source, version=1)
            self._pending[frag.node_id] = staged
            self._write_fragment_to_disk(staged)
            self._metadata[frag.node_id] = {
                "order": order,
                "indent_prefix": prefix,
            }
            self.commit_node(frag.node_id)
            self._metadata[frag.node_id]["order"] = order
            self._metadata[frag.node_id]["indent_prefix"] = prefix
            added.append(frag.node_id)
        return FileSyncReport(file_path=file_path, added=tuple(added))

    def get_committed_fragments(self, file_path: str) -> list[NodeFragment]:
        """Return all committed fragments for a file, in original source order.

        Fragments are re-indented to their original column position before
        being returned so that concatenating them reconstructs valid Python.
        Agents store and receive source at column 0 (dedented); only the
        reconstruction path needs the original indentation back.
        """
        with self._lock:
            result: list[NodeFragment] = []
            for nid in self.list_nodes(file_path):
                frag = self._nodes[nid]
                prefix = str(self._metadata.get(nid, {}).get("indent_prefix", ""))
                if prefix:
                    frag = dataclasses.replace(
                        frag, source=textwrap.indent(frag.source, prefix)
                    )
                result.append(frag)
            return result

    def get_preview_fragments(
        self,
        file_path: str,
        staged_overrides: dict[NodeId, NodeFragment],
    ) -> list[NodeFragment]:
        """Like ``get_committed_fragments`` but substitutes staged sources.

        Used by ``_preview_is_valid`` to assemble the prospective file before
        committing.  The staged sources are dedented (stored at column 0) so
        this method re-applies the original ``indent_prefix`` — exactly as
        ``get_committed_fragments`` does — so that class methods and other
        indented fragments appear at the correct column in the preview.  The
        caller therefore gets a correctly-indented source that ``compile()``
        can validate, rather than a flat (dedented) concatenation that would
        always fail for any file containing class methods.

        A **staged whole-file node supersedes the file's fragments**, mirroring
        what ``commit_node`` does via ``_supersede_fragments``.  The preview must
        model the state the commit would actually produce: without this, a
        whole-file rewrite of a file still stored as fragments previews as the
        old fragments *plus* the entire new file concatenated after them, which
        duplicates every symbol and — for any module opening with
        ``from __future__ import …`` — cannot compile at all.  The gate then
        rejects a state that would never have been committed, deterministically,
        discarding the agent's work on every attempt.
        """
        with self._lock:
            staged_whole_file = staged_overrides.get(NodeId(file_path))
            if staged_whole_file is not None:
                return [staged_whole_file]

            result: list[NodeFragment] = []
            seen: set[NodeId] = set()
            for nid in self.list_nodes(file_path):
                seen.add(nid)
                frag = staged_overrides.get(nid) or self._nodes.get(nid)
                if frag is None:
                    continue
                prefix = str(self._metadata.get(nid, {}).get("indent_prefix", ""))
                if prefix:
                    frag = dataclasses.replace(
                        frag, source=textwrap.indent(frag.source, prefix)
                    )
                result.append(frag)
            # Brand-new staged nodes have no committed slot yet — append as-is.
            # Skip when a whole-file node owns the file: staged fragments would
            # be appended after the whole-file content and corrupt the preview.
            if NodeId(file_path) not in self._nodes:
                for nid, frag in staged_overrides.items():
                    if nid not in seen and str(nid).split("::", 1)[0] == file_path:
                        result.append(frag)
            return result

    def get_staged(self, node_id: NodeId) -> NodeFragment | None:
        """Return the pending (staged, uncommitted) fragment for a node, if any.

        Lets a caller inspect a new fragment version *before* committing it — e.g.
        the conflict detector, which must validate the proposed source before it is
        promoted during the collection phase.
        """
        with self._lock:
            return self._pending.get(node_id)
