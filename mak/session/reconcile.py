"""Bring the node store into agreement with the working tree before a run."""

from __future__ import annotations

import fnmatch
import sys

from mak.config import MakConfig
from mak.core.exceptions import GitIntegrationError, WorkTreeConflictError
from mak.core.logging import EventType
from mak.git_integration.git import GitHelper
from mak.node_store.ingestion import iter_source_files
from mak.node_store.makignore import MakIgnore, ensure_makignore, load_makignore
from mak.node_store.store import FileSyncReport, NodeStore
from mak.session.events import EventLog
from mak.session.workspace import Workspace


class WorkTreeReconciler:
    """Reconcile the store with disk, prune what is no longer ingestable.

    Owns the project's ``.makignore``: read by :meth:`load_ignore_rules` at
    ``initialize`` and empty until then, so a session driven without
    ``initialize`` ignores nothing extra.
    """

    def __init__(
        self,
        *,
        config: MakConfig,
        store: NodeStore,
        workspace: Workspace,
        git: GitHelper | None,
        log: EventLog,
    ) -> None:
        self._config = config
        self._store = store
        self._workspace = workspace
        self._git = git
        self._log = log
        self._makignore = MakIgnore()

    def load_ignore_rules(self) -> None:
        """Read ``.makignore`` (its defaults, in memory, when there is none yet)."""
        self._makignore = load_makignore(self._workspace.work_dir)

    def write_ignore_file(self) -> None:
        """Create ``.makignore`` with its defaults when the project has none."""
        ensure_makignore(self._workspace.work_dir)

    def require_clean_tree(self) -> None:
        """Refuse to start on a dirty tree, when the project asks for that.

        Off by default. A clean-tree precondition is a legitimate product policy
        — it makes ``git diff`` after a run mean exactly "what MAK did" — but it
        is the project's call to make, not one MAK imposes, so it is opt-in via
        ``git.require_clean_tree`` and this is the only thing that enforces it.
        """
        if self._git is None or not self._config.git.require_clean_tree:
            return
        if not self._git.validate_clean_state():
            raise GitIntegrationError(
                "the working tree has uncommitted changes and "
                "git.require_clean_tree is on; commit or stash them first"
            )

    def ensure_audit_repo(self) -> None:
        """Give the work dir its own repository for MAK's audit commits.

        If the work dir is nested in an outer repo (e.g. a home directory) or in
        none at all, audit commits would leak into the surrounding one.
        """
        if self._git is None or not self._config.git.auto_commit:
            return
        if self._git.ensure_initialized():
            print(
                "mak: initialized a git repo in "
                f"{self._workspace.work_dir} for MAK's audit log (it was not "
                "its own repository).",
                file=sys.stderr,
            )

    def reconcile(self) -> None:
        """Make the store agree with the working tree before anything runs.

        Reconciliation runs in both directions, so an edit a human made between
        two sessions is never silently discarded and a symbol they deleted is
        never reconstructed:

        * every included file on disk is synchronized into the store, and
        * every file the store still holds live nodes for that is **gone** from
          disk has those nodes retired.

        A file whose content differs from what MAK last materialized was edited
        by someone else. Under the default ``on_external_edit="adopt"`` the disk
        wins — it is the newer truth, and the store's job is to record it, not
        overrule it. Under ``"conflict"`` the divergence raises *here*, before
        planning, so no agent can be handed content the tree no longer holds.

        The file list comes from a walk that refuses to *descend* into an
        excluded directory: enumerating ``.venv``, ``node_modules`` or
        ``__pycache__`` only to discard every path would be the bulk of
        ``initialize()``'s cost on a repo with a populated virtualenv.
        """
        ns_cfg = self._config.node_store
        work_dir = self._workspace.work_dir
        seen: set[str] = set()
        adopted: list[str] = []
        reports: list[FileSyncReport] = []
        for path in iter_source_files(
            work_dir,
            ns_cfg.include_patterns,
            ns_cfg.exclude_patterns,
            skip=self._workspace.is_store_path,
            ignore=self._makignore.matches,
        ):
            rel = str(path.relative_to(work_dir))
            try:
                source = path.read_text(encoding="utf-8")
            except OSError:
                continue
            seen.add(rel)
            if self._is_external_edit(rel, source):
                self._on_external_edit(rel, source)
                adopted.append(rel)
            try:
                reports.append(self._store.sync_file(rel, source))
            except (SyntaxError, OSError):
                continue
        reports.extend(self._retire_missing_files(seen))
        self._report(reports, adopted)

    def prune_excluded_nodes(self) -> int:
        """Drop stored nodes whose file is no longer ingestable; return the count.

        The migration path for a store that ingested MAK's own ``.mak/``
        directory, an excluded path, or one the user has since added to
        ``.makignore``: a fix-forward run would otherwise keep carrying every
        such fragment. Deleting ``.mak/`` by hand is the blunt alternative; this
        is the one that preserves real work.
        """
        patterns = self._config.node_store.exclude_patterns
        doomed = [
            node_id
            for node_id in self._store.list_all_nodes()
            if self._is_excluded_node(str(node_id), patterns)
        ]
        for node_id in doomed:
            self._store.remove_node(node_id)
        if doomed:
            print(
                f"mak: pruned {len(doomed)} node(s) that are no longer ingestable "
                "(MAK's own .mak/ store, an excluded path, or one in .makignore).",
                file=sys.stderr,
            )
        return len(doomed)

    def _is_excluded_node(self, node_id: str, patterns: tuple[str, ...]) -> bool:
        """Whether a node's file component is excluded from ingestion."""
        file_path = node_id.split("::", 1)[0]
        return (
            self._workspace.is_store_path(self._workspace.work_dir / file_path)
            or is_excluded(file_path, patterns)
            or self._makignore.is_ignored(file_path)
        )

    def _is_external_edit(self, file_path: str, source: str) -> bool:
        """Whether ``source`` differs from the content MAK last wrote there.

        A file MAK has never materialized is not an external edit — it is simply
        a file, and every file is one on the first run.
        """
        recorded = self._store.materialized_digest(file_path)
        if recorded is None:
            return False
        return recorded != NodeStore.content_digest(source)

    def _on_external_edit(self, file_path: str, source: str) -> None:
        """Apply the configured policy to a file someone edited outside MAK."""
        if self._config.session.on_external_edit == "conflict":
            raise WorkTreeConflictError(
                f"'{file_path}' has changed since MAK last wrote it "
                f"(recorded {self._store.materialized_digest(file_path)}, "
                f"found {NodeStore.content_digest(source)}); "
                "session.on_external_edit is 'conflict'. Review the file, then "
                "re-run with 'adopt' to take the working tree as authoritative."
            )
        self._log(EventType.SESSION_STARTED, adopted_external_edit=file_path)

    def _retire_missing_files(self, seen: set[str]) -> list[FileSyncReport]:
        """Retire the nodes of every file the store holds that disk no longer has."""
        known = {
            str(node_id).split("::", 1)[0] for node_id in self._store.list_all_nodes()
        }
        reports: list[FileSyncReport] = []
        for file_path in sorted(known - seen):
            if (self._workspace.work_dir / file_path).exists():
                # Present but not walked: excluded, unreadable, or not a source
                # file under the current patterns. Not a deletion — exclusion
                # pruning is a separate, deliberate operation.
                continue
            reports.append(self._store.sync_file(file_path, None))
        return reports

    def _report(self, reports: list[FileSyncReport], adopted: list[str]) -> None:
        """Say what reconciliation changed, when it changed anything."""
        updated = sum(len(r.updated) for r in reports)
        retired = sum(len(r.retired) for r in reports)
        if not adopted and not retired and not updated:
            return
        self._log(
            EventType.SESSION_STARTED,
            reconciled_files=len(adopted),
            reconciled_updated=updated,
            reconciled_retired=retired,
        )
        if adopted or retired:
            print(
                f"mak: reconciled the node store with the working tree — "
                f"{len(adopted)} file(s) edited outside MAK, "
                f"{retired} node(s) retired.",
                file=sys.stderr,
            )


def is_excluded(rel: str, exclude_patterns: tuple[str, ...]) -> bool:
    """Whether a path (relative to the work dir) matches any exclude glob."""
    return any(
        fnmatch.fnmatch(rel, pattern)
        or (pattern.startswith("**/") and fnmatch.fnmatch(rel, pattern[3:]))
        for pattern in exclude_patterns
    )
