"""Install a wave's output files as one all-or-nothing step.

Rendering and writing each affected file in turn with ``Path.write_text`` has
two failure modes this module exists to rule out. The first is partial
installation: a task touching two files could write the first, fail rendering
the second, and leave disk holding half a change whose store commits were then
reverted. The second is that ``write_text`` truncates
before it writes, so an interruption did not merely fail to update a file — it
destroyed the version that was there.

This module inverts the order. **Render everything first, write nothing until
all of it is known good**, and journal the destinations' prior content before
touching any of them so a failure — or a kill, recovered by a later process —
can put them all back. The store's own transaction wraps this one, and the
metadata save at its end is the commit point for both.

Rollback here is deliberately narrow: it restores files. Restoring the *store*
is :meth:`NodeStore.transaction`'s job, and the two are composed by the caller
rather than entangled, because a file this cannot restore must still not stop
the store from rolling back.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from pathlib import Path

from mak.core.atomic import write_text_atomic
from mak.core.types import NodeId
from mak.node_store import journal as journal_mod
from mak.node_store.journal import CommitJournal, JournalEntry
from mak.node_store.reconstruction import render_file
from mak.node_store.store import NodeStore

_logger = logging.getLogger(__name__)

# work-dir-relative file path -> the absolute path it may be written to.
PathResolver = Callable[[str], Path]


@dataclasses.dataclass(frozen=True, slots=True)
class InstalledFiles:
    """What an installation put on disk, and the journal covering it."""

    files: tuple[str, ...]
    journal: CommitJournal
    journal_dir: Path
    # Rendered content per file, so the caller can record what it materialized
    # without reading the files back off disk.
    contents: dict[str, str]


def render_affected(
    store: NodeStore, nodes: list[NodeId], resolve: PathResolver
) -> tuple[list[str], dict[str, str], dict[str, Path]]:
    """Render every file ``nodes`` touches, without writing any of them.

    Raises before any file is opened if a file has no committed fragments: a
    committed node that yields nothing to write would leave the destination
    untouched while the run reported it as written, which is a divergence that
    only surfaces later, at ``git add``.
    """
    files = sorted({str(n).split("::", 1)[0] for n in nodes})
    contents: dict[str, str] = {}
    destinations: dict[str, Path] = {}
    for file_path in files:
        destinations[file_path] = resolve(file_path)
        fragments = store.get_committed_fragments(file_path)
        if not fragments:
            raise OSError(
                f"no committed fragments for '{file_path}'; nothing to write"
            )
        contents[file_path] = render_file(fragments)
    return files, contents, destinations


def install_files(
    store: NodeStore,
    nodes: list[NodeId],
    *,
    resolve: PathResolver,
    journal_dir: Path,
    session_id: str,
    task_id: str,
) -> InstalledFiles:
    """Render, journal, and atomically write every file ``nodes`` touches.

    Call inside an open :meth:`NodeStore.transaction`, after the nodes have been
    committed in memory: the versions recorded in the journal are read from the
    store, and they are what recovery later compares against to decide whether
    the commit point was reached.

    On any failure every destination is restored from its backup and the journal
    is discarded before the exception propagates — so the enclosing store
    transaction rolls back onto a working tree that already matches it.
    """
    files, contents, destinations = render_affected(store, nodes, resolve)

    node_versions: dict[str, int] = {}
    for node_id in nodes:
        version = _committed_version(store, node_id)
        if version is not None:
            node_versions[str(node_id)] = version

    entries: list[JournalEntry] = []
    for index, file_path in enumerate(files):
        entries.append(
            JournalEntry(
                file_path=file_path,
                backup=journal_mod.back_up(
                    journal_dir, index, destinations[file_path]
                ),
            )
        )
    record = CommitJournal(
        txn_id=journal_mod.new_txn_id(),
        session_id=session_id,
        task_id=task_id,
        phase=journal_mod.PHASE_INSTALLING,
        entries=tuple(entries),
        node_versions=node_versions,
    )
    journal_mod.write(record, journal_dir)

    try:
        for file_path in files:
            write_text_atomic(destinations[file_path], contents[file_path])
    except BaseException:
        journal_mod.restore(record, journal_dir, resolve)
        journal_mod.discard(journal_dir)
        raise
    return InstalledFiles(
        files=tuple(files),
        journal=record,
        journal_dir=journal_dir,
        contents=contents,
    )


def mark_installed(installed: InstalledFiles) -> None:
    """Advance the journal past the commit point, before the Git audit.

    Between this call and :func:`finish` the only thing outstanding is the audit
    commit, and recovery handles that by re-running it — a retry ``git`` itself
    makes idempotent by reporting an empty diff.
    """
    journal_mod.write(
        dataclasses.replace(installed.journal, phase=journal_mod.PHASE_INSTALLED),
        installed.journal_dir,
    )


def finish(installed: InstalledFiles) -> None:
    """Discard the journal and its backups; the transaction is fully done."""
    journal_mod.discard(installed.journal_dir)


def _committed_version(store: NodeStore, node_id: NodeId) -> int | None:
    """Return the node's committed version, or ``None`` if it has none."""
    from mak.core.exceptions import NodeStoreError

    try:
        return store.get_node(node_id).version
    except NodeStoreError:
        return None


def recover(
    journal_dir: Path,
    store: NodeStore,
    *,
    resolve: PathResolver,
    reaudit: Callable[[str, list[str]], None] | None = None,
) -> str | None:
    """Resolve a commit journal left behind by an interrupted run.

    Returns the branch taken (``"rolled-back"``, ``"rolled-forward"``,
    ``"reaudited"``) or ``None`` when there was nothing in flight. Runs before
    anything else touches state, so whichever way it resolves, the session that
    follows starts from a consistent picture.
    """
    record = journal_mod.read(journal_dir)
    if record is None:
        return None

    if record.phase == journal_mod.PHASE_INSTALLED:
        # Files and store are both durable; only the audit commit is in doubt.
        if reaudit is not None:
            try:
                reaudit(record.task_id, [e.file_path for e in record.entries])
            except Exception as exc:  # noqa: BLE001 - git failure must not block startup
                _logger.warning(
                    "could not re-run the interrupted audit commit for task "
                    "'%s': %s. The change itself is intact and committed to the "
                    "store; only its audit-log entry may be missing.",
                    record.task_id,
                    exc,
                )
        journal_mod.discard(journal_dir)
        _logger.info(
            "recovered commit %s: files and store were durable, re-ran the audit.",
            record.txn_id,
        )
        return "reaudited"

    if journal_mod.commit_point_passed(
        record, lambda nid: _committed_version(store, nid)
    ):
        # The metadata save landed, so the transaction committed. Roll forward:
        # the files may be half-written, and the store is the authority for them.
        _, contents, destinations = render_affected(
            store, [NodeId(e.file_path) for e in record.entries], resolve
        )
        for file_path, content in contents.items():
            write_text_atomic(destinations[file_path], content)
            store.record_materialized(file_path, content)
        journal_mod.discard(journal_dir)
        _logger.info(
            "recovered commit %s: the store had committed, rewrote %d file(s).",
            record.txn_id,
            len(contents),
        )
        return "rolled-forward"

    journal_mod.restore(record, journal_dir, resolve)
    journal_mod.discard(journal_dir)
    _logger.info(
        "recovered commit %s: the store had not committed, restored %d file(s).",
        record.txn_id,
        len(record.entries),
    )
    return "rolled-back"
