"""One owner per project: an OS-backed exclusive lease over a project's state.

MAK's :class:`~mak.lock_manager.lock_table.LockTable` guards its state with a
``threading.RLock``. That is the right primitive for what it actually protects —
a session's own worker threads racing over one in-memory table — and it says
*nothing* about a second process. Two ``mak`` runs over one project therefore
each built their own table over the same persistence file and both granted a
write lock on ``a.py`` to different holders; worse, each startup called
``clear()`` on the persisted table without establishing anything about whether
the prior owner was alive, so a second run stripped the leases of a first that
was still working.

The fix is a **single-owner policy**, not a distributed lock table. One process
holds an exclusive lease on the project's ``.mak/`` for as long as it runs;
everyone else fails fast with :class:`~mak.core.exceptions.ProjectBusyError`.
That is a smaller guarantee than distributed locking and the right one for what
MAK is: a tool a person runs on their own checkout, where two concurrent runs
are a mistake to report rather than a workload to schedule.

**Why ``flock`` and not a lock file.** A lock *file* has to answer "is the owner
still alive?" from data the dead owner wrote, which is unanswerable in general —
a pid can be recycled, and a heartbeat threshold either strands live sessions or
lets dead ones block for minutes. ``flock`` moves that question to the kernel,
which releases the lock when the holding process dies **however** it dies,
including ``SIGKILL``. Abrupt-owner recovery therefore needs no timeout and no
heuristic: the next acquire simply succeeds.

The JSON record written inside the file is diagnostics — who to name in the error
— plus the staleness signal for the platform below where the kernel cannot help.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any

from mak.core.exceptions import ProjectBusyError

_logger = logging.getLogger(__name__)

LEASE_FILENAME = "owner.lock"

# How long after its last heartbeat an owner record is treated as abandoned. Only
# consulted on the Windows path, where a byte-range lock can outlive the process
# that took it; on POSIX the kernel's own release makes this unnecessary.
DEFAULT_STALE_AFTER_S = 90.0


@dataclasses.dataclass(frozen=True, slots=True)
class LeaseOwner:
    """Who holds a project lease, for the error message that names them."""

    pid: int
    hostname: str
    session_id: str
    acquired_at: float
    heartbeat_at: float

    def to_json(self) -> dict[str, Any]:
        """Serialize the record for the lease file."""
        return dataclasses.asdict(self)

    @staticmethod
    def from_json(raw: dict[str, Any]) -> LeaseOwner:
        """Rebuild a record from the lease file, defaulting anything missing."""
        return LeaseOwner(
            pid=int(raw.get("pid", 0)),
            hostname=str(raw.get("hostname", "?")),
            session_id=str(raw.get("session_id", "?")),
            acquired_at=float(raw.get("acquired_at", 0.0)),
            heartbeat_at=float(raw.get("heartbeat_at", 0.0)),
        )

    def describe(self, *, now: float | None = None) -> str:
        """Describe the holder in one line an operator can act on."""
        age = (now if now is not None else time.time()) - self.heartbeat_at
        return (
            f"pid {self.pid} on {self.hostname} (session {self.session_id}), "
            f"last seen {age:.0f}s ago"
        )


class ProjectLease:
    """Exclusive ownership of one project's MAK state, held for a session.

    Acquired before ingestion, recovery, or any other state mutation, and
    released on :meth:`release` or process exit. Re-entrant only in the trivial
    sense: acquiring a lease this object already holds is a no-op, so a session
    that initializes and then recovers does not deadlock against itself.
    """

    def __init__(
        self,
        mak_dir: Path,
        session_id: str,
        *,
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
    ) -> None:
        self._path = mak_dir / LEASE_FILENAME
        self._session_id = session_id
        self._stale_after = stale_after_s
        self._fd: int | None = None
        self._acquired_at = 0.0

    @property
    def path(self) -> Path:
        """Where the lease file lives."""
        return self._path

    @property
    def held(self) -> bool:
        """Whether this object currently holds the lease."""
        return self._fd is not None

    def acquire(self) -> None:
        """Take the project's exclusive lease, or raise ``ProjectBusyError``."""
        if self._fd is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            self._lock_exclusive(fd)
        except BlockingIOError as exc:
            owner = self._read_owner(fd)
            os.close(fd)
            where = f" — {owner.describe()}" if owner is not None else ""
            raise ProjectBusyError(
                f"another MAK session already owns {self._path.parent}{where}. "
                "Wait for it to finish, or stop it before running here again."
            ) from exc
        except OSError:
            os.close(fd)
            raise
        self._fd = fd
        self._acquired_at = time.time()
        self._write_owner()

    def heartbeat(self) -> None:
        """Refresh the owner record's timestamp. Cheap, and never fails a run."""
        if self._fd is None:
            return
        try:
            self._write_owner()
        except OSError as exc:  # pragma: no cover - unwritable mak dir
            _logger.warning("could not refresh the project lease record: %s", exc)

    def release(self) -> None:
        """Release the lease. Idempotent, and safe to call from a finally block."""
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            self._unlock(fd)
        except OSError as exc:  # pragma: no cover - fd already invalid
            _logger.warning("could not release the project lease: %s", exc)
        finally:
            try:
                os.close(fd)
            except OSError:  # pragma: no cover - fd already closed
                pass

    def __enter__(self) -> ProjectLease:
        """Acquire the lease on entry."""
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Release the lease on exit, however the block ended."""
        self.release()

    # -- platform specifics -------------------------------------------------

    def _lock_exclusive(self, fd: int) -> None:
        """Take the OS lock non-blockingly, raising ``BlockingIOError`` if taken."""
        if sys.platform == "win32":  # pragma: no cover - POSIX CI
            self._lock_windows(fd)
            return
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(self, fd: int) -> None:
        if sys.platform == "win32":  # pragma: no cover - POSIX CI
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            return
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)

    def _lock_windows(self, fd: int) -> None:  # pragma: no cover - POSIX CI
        """Windows fallback: a byte-range lock, plus a staleness check.

        Weaker than the POSIX path on purpose-of-record: a byte-range lock can
        outlive the process that took it, so an owner record older than
        ``stale_after_s`` is treated as abandoned and the lock is broken. That
        heuristic is exactly what ``flock`` lets the POSIX path avoid.
        """
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        except OSError as exc:
            owner = self._read_owner(fd)
            if owner is not None and (
                time.time() - owner.heartbeat_at > self._stale_after
            ):
                _logger.warning(
                    "breaking an abandoned project lease held by %s",
                    owner.describe(),
                )
                return
            raise BlockingIOError(str(exc)) from exc

    # -- the owner record ---------------------------------------------------

    def _write_owner(self) -> None:
        """Record who holds the lease, in the locked file itself.

        Written through the held descriptor rather than via an atomic replace: a
        rename would swap out the very inode the lock is taken on, and the next
        process would lock a different file and find the project unguarded.
        """
        if self._fd is None:  # pragma: no cover - guarded by callers
            return
        owner = LeaseOwner(
            pid=os.getpid(),
            hostname=socket.gethostname(),
            session_id=self._session_id,
            acquired_at=self._acquired_at,
            heartbeat_at=time.time(),
        )
        payload = json.dumps(owner.to_json(), indent=2).encode("utf-8")
        os.lseek(self._fd, 0, os.SEEK_SET)
        os.ftruncate(self._fd, 0)
        os.write(self._fd, payload)
        os.fsync(self._fd)

    def _read_owner(self, fd: int) -> LeaseOwner | None:
        """Read the owner record from an open descriptor, tolerating garbage."""
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 8192).decode("utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                return None
            return LeaseOwner.from_json(data)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            # A lease held by a process that has not written its record yet is a
            # real state, not an error: the caller still reports "busy", just
            # without a name to put on it.
            return None
