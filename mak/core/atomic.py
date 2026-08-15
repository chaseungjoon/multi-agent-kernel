"""Crash-safe file writes for MAK's persisted state.

Three files carry everything a run can be resumed from — the lock table, the task
graph, and the node store's metadata — and each was written with a plain
``Path.write_text``. That truncates the target *before* writing it, so a kill or a
full disk between the two leaves a half-written file where valid JSON used to be.
The next run then fails to parse it, which for the task graph means ``--recover``
breaks on exactly the crash it exists to recover from.

:func:`write_text_atomic` closes that window: the content is written to a
temporary file in the same directory, flushed to the platter, and moved into place
with ``os.replace``. A same-directory rename is atomic on POSIX and on Windows, so
a reader sees either the whole old file or the whole new one — never a truncation.

The ``fsync`` is what makes the *content* durable rather than merely the rename:
without it the rename can land while the data is still in the page cache, and a
power loss leaves an intact directory entry pointing at zeros.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_text_atomic(
    path: Path, text: str, *, encoding: str = "utf-8"
) -> None:
    """Write ``text`` to ``path`` atomically, creating parents as needed.

    The temporary file is created in ``path``'s own directory because
    ``os.replace`` is only atomic within a single filesystem; a temp dir
    elsewhere would silently degrade to a copy. It is removed on any failure, so
    a crashed write leaves no debris beside the real file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(handle_fd, "w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        # Includes KeyboardInterrupt/SystemExit on purpose: an interrupted write
        # must not leave a stray temp file next to the state it failed to update.
        tmp_path.unlink(missing_ok=True)
        raise
