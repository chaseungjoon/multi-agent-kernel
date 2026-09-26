"""Commit an accepted edit: one store transaction, journaled files, audit commit."""

from __future__ import annotations

from mak.config import MakConfig
from mak.core.atomic import write_text_atomic
from mak.core.exceptions import NodeStoreError, UnsafeNodeIdError
from mak.core.logging import EventType
from mak.core.types import NodeId
from mak.git_integration.git import GitHelper
from mak.node_store.reconstruction import assemble_fragments
from mak.node_store.transaction import (
    finish,
    install_files,
    mark_installed,
    render_affected,
)
from mak.session.events import EventLog, timed_phase
from mak.session.failures import record_failure
from mak.session.store_view import StoreView, file_of
from mak.session.wave import WaveState
from mak.session.workspace import Workspace


class CommitApplier:
    """Applies a validated commit and records it as this wave's work."""

    def __init__(
        self,
        *,
        config: MakConfig,
        view: StoreView,
        workspace: Workspace,
        git: GitHelper | None,
        session_id: str,
        log: EventLog,
    ) -> None:
        self._config = config
        self._view = view
        self._workspace = workspace
        self._git = git
        self._session_id = session_id
        self._log = log

    def apply(
        self, wave: WaveState, task_id: str, staged: list[NodeId]
    ) -> list[NodeId]:
        """Commit ``staged`` and install its files; return what was committed.

        Everything here is one transaction. The store's snapshot covers the node
        versions, the superseded fragments, and the metadata; the journal covers
        the output files. The metadata save at the end of the ``with`` block is
        the commit point for both — before it, nothing durable has changed;
        after it, the change is recoverable in full. A failure before it leaves
        store and disk where they started and returns ``[]``.
        """
        store = self._view.store
        wave_entries: dict[NodeId, tuple[str | None, str | None]] = {}
        before_files = self._files_before(wave, staged)
        try:
            with store.transaction():
                for node_id in staged:
                    old_source = self._view.source(node_id)  # before commit
                    superseded = self._superseded_by(node_id)
                    store.commit_node(node_id)
                    new_source = self._view.source(node_id)  # after commit
                    if new_source is not None:
                        wave_entries[node_id] = (old_source, new_source)
                    for gone, gone_source in superseded.items():
                        if self._view.source(gone) is None:
                            wave_entries[gone] = (gone_source, None)
                installed = install_files(
                    store,
                    staged,
                    resolve=self._workspace.safe_output_path,
                    journal_dir=self._workspace.journal_dir,
                    session_id=self._session_id,
                    task_id=task_id,
                )
        except (SyntaxError, OSError, UnsafeNodeIdError, NodeStoreError) as exc:
            # Store and files are both back where they started; the pending
            # fragments are the only thing left to discard.
            for node_id in staged:
                store.rollback_node(node_id)
            self._log(
                EventType.CONFLICT_DETECTED,
                task_id=task_id,
                reasons=[f"commit transaction rolled back: {exc}"],
                files=sorted({file_of(str(n)) for n in staged}),
            )
            record_failure(wave, task_id, f"commit transaction rolled back: {exc}")
            return []

        # Past the commit point. Only now is any of this recorded as work that
        # happened, so a rolled-back transaction leaves nothing behind for the
        # wave's cascade analysis to mistake for real work.
        wave.committed.update(wave_entries)
        self._record_wave_writes(wave, task_id, staged, before_files)
        for file_path, content in installed.contents.items():
            store.record_materialized(file_path, content)
        mark_installed(installed)
        self._audit_commit(wave, task_id, staged)
        finish(installed)
        return list(staged)

    @timed_phase("reconstruct")
    def reconstruct_affected(self, nodes: list[NodeId]) -> list[str]:
        """Rewrite each file touched by ``nodes`` from its committed fragments.

        The *non-transactional* materialization path: bringing an already-committed
        file back into agreement with the store, as the no-op policy does when a
        node's on-disk file pre-dates its committed version. The edit path does
        not come through here — :meth:`apply` goes through ``install_files``,
        which journals what it is about to overwrite. Nothing is committed by
        this call, so there is nothing for a journal to roll back; every write is
        still atomic.
        """
        store = self._view.store
        files, contents, destinations = render_affected(
            store, nodes, self._workspace.safe_output_path
        )
        for file_path in files:
            write_text_atomic(destinations[file_path], contents[file_path])
            store.record_materialized(file_path, contents[file_path])
        return files

    def _files_before(
        self, wave: WaveState, staged: list[NodeId]
    ) -> dict[str, str | None]:
        """Each staged file's committed source, for files first touched now."""
        store = self._view.store
        before: dict[str, str | None] = {}
        for file_path in sorted({file_of(str(n)) for n in staged}):
            if file_path in wave.file_before:
                continue
            fragments = store.get_committed_fragments(file_path)
            before[file_path] = assemble_fragments(fragments) if fragments else None
            wave.fragments_before[file_path] = [
                (f.node_id, store.node_order(f.node_id), f.source) for f in fragments
            ]
        return before

    def _superseded_by(self, node_id: NodeId) -> dict[NodeId, str]:
        """Return the fragments a whole-file commit of ``node_id`` will remove."""
        if "::" in str(node_id):
            return {}
        return {
            fragment: source
            for fragment in self._view.file_fragment_ids(node_id)
            if (source := self._view.source(fragment)) is not None
        }

    def _record_wave_writes(
        self,
        wave: WaveState,
        task_id: str,
        staged: list[NodeId],
        before: dict[str, str | None],
    ) -> None:
        """Remember what this commit touched, once it is past the commit point."""
        store = self._view.store
        for file_path, source in before.items():
            wave.file_before.setdefault(file_path, source)
        for node_id in staged:
            wave.node_writer[node_id] = task_id
            writers = wave.file_writers.setdefault(file_of(str(node_id)), [])
            if task_id not in writers:
                writers.append(task_id)
        for file_path in sorted({file_of(str(n)) for n in staged}):
            for fragment in store.get_committed_fragments(file_path):
                if fragment.node_id in staged:
                    wave.commit_log.append((
                        task_id,
                        fragment.node_id,
                        store.node_order(fragment.node_id),
                        fragment.source,
                    ))

    def _audit_commit(self, wave: WaveState, task_id: str, nodes: list[NodeId]) -> None:
        """Record a git audit commit for the task's files, if git is enabled."""
        if self._git is None or not self._config.git.auto_commit:
            return
        files = sorted({file_of(str(n)) for n in nodes})
        task = wave.task(task_id)
        self._git.commit_task(
            task_id=task_id,
            files=files,
            description=task.description,
            agent_id=task.agent_type or "unknown",
            session_id=self._session_id,
        )
