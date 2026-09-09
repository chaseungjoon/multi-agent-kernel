"""The write-ahead journal for a commit that spans the store and the work tree.

A MAK commit changes two durable things: the node store's metadata, and the
source files reconstructed from it. Those cannot be made atomic together — they
are different files, and on some filesystems different directories — so instead
one of them is designated the **commit point** (the store's ``metadata.json``,
written with :func:`~mak.core.atomic.write_text_atomic`) and this journal makes
everything on either side of it recoverable.

The journal is written *before* the first output file is touched and carries a
backup of every destination's prior content. That makes the whole installation
reversible up to the commit point, and — crucially — reversible **by a later
process**, which is what a crash leaves behind. Without it, a kill between two
files of a two-file change left no record that anything had been in flight: the
next run opened a store whose metadata disagreed with a working tree nobody
could explain.

Recovery reads the journal and asks one question: *did the commit point pass?*
It answers it by comparing the versions the journal says the transaction was
committing against the versions the reopened store actually holds.

* every version matches → the metadata save completed, the transaction is
  **committed**, and recovery rolls *forward* (rewrite the files from committed
  fragments, then discard the journal);
* any version differs → the metadata save never happened, the transaction is
  **not** committed, and recovery rolls *back* (restore every destination from
  its backup, then discard);
* ``phase == "installed"`` → files and store are both durable and only the Git
  audit commit is in doubt; recovery re-runs it, which is a no-op when it already
  landed, then discards.

That third case is the defined recovery of an interrupted Git audit: ``git``'s
own emptiness check makes the retry idempotent, so recovery never has to decide
whether a commit exists.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mak.core.atomic import write_text_atomic
from mak.core.types import NodeId

_logger = logging.getLogger(__name__)

JOURNAL_NAME = "journal.json"

# The journal's two phases. "installing" means output files are being written and
# the commit point has not been reached; "installed" means both the files and the
# store metadata are durable and only the audit commit remains.
PHASE_INSTALLING = "installing"
PHASE_INSTALLED = "installed"


@dataclasses.dataclass(frozen=True, slots=True)
class JournalEntry:
    """One output file in flight, and how to put it back.

    ``backup`` names a file beside the journal holding the destination's prior
    content. ``None`` means the destination did not exist, and rolling back means
    *deleting* it rather than restoring anything — a distinction a plain
    "restore the backup" scheme cannot make, and the one that matters for a task
    that creates a new module.
    """

    file_path: str
    backup: str | None

    def to_json(self) -> dict[str, Any]:
        """Serialize the entry for the journal file."""
        return {"file_path": self.file_path, "backup": self.backup}

    @staticmethod
    def from_json(raw: dict[str, Any]) -> JournalEntry:
        """Rebuild one entry from the journal file."""
        backup = raw.get("backup")
        return JournalEntry(
            file_path=str(raw["file_path"]),
            backup=str(backup) if backup is not None else None,
        )


@dataclasses.dataclass(frozen=True, slots=True)
class CommitJournal:
    """A commit transaction's write-ahead record."""

    txn_id: str
    session_id: str
    task_id: str
    phase: str
    entries: tuple[JournalEntry, ...]
    # node id -> the version this transaction is committing. Recovery compares
    # these against the reopened store to decide whether the commit point passed.
    node_versions: dict[str, int]

    def to_json(self) -> dict[str, Any]:
        """Serialize the whole journal record."""
        return {
            "txn_id": self.txn_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "phase": self.phase,
            "entries": [e.to_json() for e in self.entries],
            "node_versions": dict(self.node_versions),
        }

    @staticmethod
    def from_json(raw: dict[str, Any]) -> CommitJournal:
        """Rebuild a journal record, defaulting anything an older one lacks."""
        return CommitJournal(
            txn_id=str(raw["txn_id"]),
            session_id=str(raw.get("session_id", "")),
            task_id=str(raw.get("task_id", "")),
            phase=str(raw.get("phase", PHASE_INSTALLING)),
            entries=tuple(
                JournalEntry.from_json(e) for e in raw.get("entries", [])
            ),
            node_versions={
                str(k): int(v) for k, v in raw.get("node_versions", {}).items()
            },
        )


def new_txn_id() -> str:
    """Generate a fresh transaction id (also its backup directory's name)."""
    return uuid.uuid4().hex


def journal_path(journal_dir: Path) -> Path:
    """Return the journal file inside ``journal_dir``."""
    return journal_dir / JOURNAL_NAME


def write(journal: CommitJournal, journal_dir: Path) -> None:
    """Persist the journal atomically, creating its directory if needed."""
    write_text_atomic(
        journal_path(journal_dir), json.dumps(journal.to_json(), indent=2)
    )


def read(journal_dir: Path) -> CommitJournal | None:
    """Load the journal, or ``None`` when there is nothing in flight.

    A journal that cannot be parsed is quarantined rather than raised past: an
    unreadable recovery record must not be able to stop the session that needs to
    recover, and the operator still gets the file to inspect. There is nothing to
    roll back or forward in that case, because the entries are what said what to
    do and they are the part that is gone.
    """
    path = journal_path(journal_dir)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"expected a JSON object, got {type(raw).__name__}")
        return CommitJournal.from_json(raw)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, KeyError,
            TypeError, ValueError) as exc:
        quarantine = path.with_suffix(".json.corrupt")
        try:
            path.replace(quarantine)
        except OSError:  # pragma: no cover - unwritable mak dir
            quarantine = path
        _logger.warning(
            "commit journal at %s is unreadable (%s); moved it to %s. Nothing "
            "could be rolled back or forward from it.",
            path,
            exc,
            quarantine,
        )
        return None


def discard(journal_dir: Path) -> None:
    """Remove the journal and every backup it holds. Best-effort."""
    try:
        shutil.rmtree(journal_dir)
    except FileNotFoundError:
        return
    except OSError as exc:  # pragma: no cover - unwritable mak dir
        _logger.warning(
            "could not remove the commit journal at %s: %s", journal_dir, exc
        )


def back_up(journal_dir: Path, index: int, destination: Path) -> str | None:
    """Copy ``destination`` beside the journal; return the backup's name.

    ``None`` when the destination does not exist yet — recorded as such so a
    rollback deletes the file instead of restoring one that was never there.
    """
    if not destination.exists():
        return None
    journal_dir.mkdir(parents=True, exist_ok=True)
    name = f"{index}.bak"
    shutil.copy2(destination, journal_dir / name)
    return name


def restore(
    journal: CommitJournal, journal_dir: Path, resolve: Callable[[str], Path]
) -> None:
    """Put every destination back the way the journal found it.

    Best-effort per file and deliberately so: one unrestorable path must not stop
    the other three from being restored. Each failure is logged loudly, because a
    file this could not put back is precisely the divergence the operator has to
    know about.
    """
    for entry in journal.entries:
        try:
            destination = resolve(entry.file_path)
        except Exception as exc:  # noqa: BLE001 - path policy varies by caller
            _logger.warning(
                "cannot resolve '%s' to roll it back: %s", entry.file_path, exc
            )
            continue
        try:
            if entry.backup is None:
                destination.unlink(missing_ok=True)
            else:
                source = journal_dir / entry.backup
                if source.exists():
                    write_text_atomic(
                        destination, source.read_text(encoding="utf-8")
                    )
        except OSError as exc:
            _logger.warning(
                "could not roll '%s' back to its previous content: %s",
                entry.file_path,
                exc,
            )


def commit_point_passed(
    journal: CommitJournal, committed_version: Callable[[NodeId], int | None]
) -> bool:
    """Whether the store already holds every version the journal was committing.

    This is the whole recovery decision. The store's metadata save is the commit
    point, so the versions it reports *are* the record of whether that save
    happened — no separate "committed" flag is needed, and none could be written
    atomically with the thing it describes anyway.
    """
    for node_id, version in journal.node_versions.items():
        if committed_version(NodeId(node_id)) != version:
            return False
    return True
