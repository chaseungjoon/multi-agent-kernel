"""Where a session reads and writes: the working tree and MAK's own directory."""

from __future__ import annotations

from pathlib import Path

from mak.config import SessionConfig
from mak.core.exceptions import UnsafeNodeIdError
from mak.core.paths import DEFAULT_MAK_DIR_NAME, check_node_id, safe_path_under


class Workspace:
    """The project's working directory and MAK's persistence directory."""

    def __init__(self, config: SessionConfig) -> None:
        self._config = config

    @property
    def work_dir(self) -> Path:
        """The project tree the session reconciles and writes."""
        return Path(self._config.work_dir)

    @property
    def mak_dir(self) -> Path:
        """MAK's own persistence directory (node store, task graph, journal)."""
        return Path(self._config.mak_dir)

    @property
    def mak_dir_name(self) -> str:
        """The persistence directory's name, as node ids must never contain it."""
        return self.mak_dir.name or DEFAULT_MAK_DIR_NAME

    @property
    def journal_dir(self) -> Path:
        """Where a commit-in-flight records what it is about to overwrite.

        One directory per project, not per transaction: only one commit is ever
        in flight at a time (batch commits are applied serially under the store
        lock), and a fixed location is what lets the *next process* find the
        journal a killed one left behind.
        """
        return self.mak_dir / "journal"

    @property
    def task_graph_path(self) -> Path:
        """Where the scheduler persists the current wave's task graph."""
        return self.mak_dir / "task_graph.json"

    def mak_roots(self) -> tuple[Path, ...]:
        """Return the absolute location of MAK's own persistence directory.

        ``config.anchor_mak_dir`` resolves a relative ``mak_dir`` against the
        work dir before a session is built, which leaves exactly one directory
        to exclude.
        """
        mak_dir = self.mak_dir
        candidate = mak_dir if mak_dir.is_absolute() else self.work_dir / mak_dir
        try:
            return (candidate.resolve(),)
        except OSError:
            return ()

    def is_store_path(self, path: Path) -> bool:
        """Whether ``path`` lives inside MAK's own persistence directory.

        Deliberately independent of ``exclude_patterns``: the node store writes
        fragments as ``.py`` files, so ingesting it feeds MAK its own previous
        output back as "source" — a defect that compounds by hundreds of nodes
        per run. A user config that overrides the pattern list must not be able
        to switch this off, because the store is never project source under any
        configuration.
        """
        roots = self.mak_roots()
        if not roots:
            return False
        try:
            resolved = path.resolve()
        except OSError:
            return False
        return any(resolved.is_relative_to(root) for root in roots)

    def safe_output_path(self, file_path: str) -> Path:
        """Resolve a file this wave may write, refusing anything outside the tree.

        The last gate before content reaches the filesystem. ``parse_plan``
        already refuses an escaping target, but it is not the only way a plan
        arrives: ``install_plan`` is called directly by the interactive app and
        by every cascade wave, and neither goes through the planner's parser. A
        guard that only one of three entry points passes through is not a guard.

        Resolution (not just the lexical check) because this is the layer that
        can see the filesystem: a ``vendor/`` symlink pointing at ``/etc`` is
        invisible to a string check and obvious to ``resolve()``.
        """
        check_node_id(file_path, mak_dir_name=self.mak_dir_name)
        resolved = safe_path_under(self.work_dir, file_path, label="output file")
        if self.is_store_path(resolved):
            raise UnsafeNodeIdError(
                f"refusing output file '{file_path}': it resolves inside MAK's own "
                f"{self.mak_dir} directory, which is never project source"
            )
        return resolved
