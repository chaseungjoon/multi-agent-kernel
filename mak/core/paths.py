"""Containment for the paths carried by node ids: where MAK may write.

Every node id MAK acts on originates with a model. The planner names the write
targets of each sub-task; an agent names the nodes it rewrote. A node id's file
component is then joined to a root and written to — twice, on two different
roots:

- ``NodeStore._fragment_dir`` joins the id (``::`` → ``/``) to the store root and
  writes ``v<n>.py`` under it;
- ``Session._reconstruct_affected`` joins the id's file component to the work dir
  and writes the reconstructed file there.

Neither join is safe on its own. ``Path("/work") / "/etc/x.py"`` is ``/etc/x.py``
— an absolute component discards everything before it — and ``..`` walks out of
any root. Both forms are ordinary Python paths, so the ``.py`` check the planner
already applies passes them.

This module is the gate. It offers two checks because the callers genuinely
differ, and neither subsumes the other:

- :func:`unsafe_node_id_reason` / :func:`check_node_id` are **lexical**: no
  filesystem access, no root required. ``parse_plan`` validates a plan long
  before a work dir is in scope, so this is the only check available there — and
  it is the one that catches a bad plan *before* any lock is taken or any agent
  is paid.
- :func:`safe_path_under` **resolves**. A lexical check cannot see that
  ``project/vendor`` is a symlink to ``/etc``; resolution can. This is what the
  two write paths use, where a root is known.

The store already had the right instinct in one place: ``_delete_fragment_dir``
guards with ``is_relative_to(root)``. The delete path was contained and the write
path was not. This generalizes that guard so both are.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath

from mak.core.exceptions import UnsafeNodeIdError

# The conventional mak-dir name, used by the lexical check. The *authoritative*
# exclusion is the resolved one in ``Session`` (a config can rename the dir);
# this catches the common shape at plan time, before a root is available.
DEFAULT_MAK_DIR_NAME = ".mak"

_PARENT = ".."
_CURRENT = "."


def node_file_path(node_id: str) -> str:
    """Return a node id's file component (``a.py::function::f`` → ``a.py``)."""
    return node_id.split("::", 1)[0]


def _path_components(file_path: str) -> list[str]:
    """Split a path on either separator, dropping empty and ``.`` segments.

    Both separators, because a node id minted on Windows carries backslashes and
    an id crafted to escape may use whichever the reader does not split on.
    """
    normalized = file_path.replace("\\", "/")
    return [part for part in normalized.split("/") if part and part != _CURRENT]


def _is_absolute(file_path: str) -> bool:
    """Whether ``file_path`` is absolute under POSIX *or* Windows rules.

    ``PurePosixPath("C:/x.py").is_absolute()`` is False and
    ``PureWindowsPath("/etc/x.py").is_absolute()`` is False, so a single flavour
    misses half the cases. A bare drive-relative ``C:x.py`` has no leading
    separator yet still escapes the root, hence the ``drive`` test.
    """
    if PurePosixPath(file_path).is_absolute():
        return True
    windows = PureWindowsPath(file_path)
    return windows.is_absolute() or bool(windows.drive)


def unsafe_node_id_reason(
    node_id: str, *, mak_dir_name: str | None = DEFAULT_MAK_DIR_NAME
) -> str | None:
    """Return why ``node_id`` may not be written, or ``None`` when it is fine.

    Purely lexical — safe to call anywhere, including before a work dir exists.
    Returns a reason rather than raising so a caller collecting several bad ids
    (``parse_plan``) can report them together.

    ``mak_dir_name=None`` drops the mak-dir rule and checks containment only.
    That is not a loosening for convenience: the two rules answer different
    questions. *Containment* ("does this escape its root?") is a property of the
    path and holds everywhere. *"Is this project source?"* is a policy about what
    may be planned and reconstructed, and the node store must not enforce it —
    the Wave 11 prune has to address the ``.mak/…`` nodes an older MAK ingested
    in order to delete them, and a store that refuses to name them can never
    clean them up.
    """
    if not node_id or not node_id.strip():
        return "node id is empty"
    file_path = node_file_path(node_id)
    if not file_path or not file_path.strip():
        return "node id has no file component"
    if _is_absolute(file_path):
        return (
            f"'{file_path}' is an absolute path; a target must be relative to the "
            "working directory (joining an absolute path to the work dir discards "
            "the work dir entirely)"
        )
    if file_path.startswith("~"):
        return f"'{file_path}' references a home directory"
    components = _path_components(file_path)
    if _PARENT in components:
        return (
            f"'{file_path}' escapes the working directory with '..'; a target must "
            "stay inside the project"
        )
    if mak_dir_name and mak_dir_name in components:
        return (
            f"'{file_path}' is inside MAK's own '{mak_dir_name}' directory, which "
            "is never project source"
        )
    return None


def check_node_id(
    node_id: str, *, mak_dir_name: str | None = DEFAULT_MAK_DIR_NAME
) -> None:
    """Raise :class:`UnsafeNodeIdError` if ``node_id`` may not be written."""
    reason = unsafe_node_id_reason(node_id, mak_dir_name=mak_dir_name)
    if reason is not None:
        raise UnsafeNodeIdError(f"refusing node id '{node_id}': {reason}")


def safe_path_under(root: Path, relative: str, *, label: str = "path") -> Path:
    """Join ``relative`` to ``root`` and return it, or raise if it escapes.

    Resolution is the point: this catches what the lexical check cannot — a
    symlinked directory inside the tree pointing out of it. Returns the
    *resolved* path, so the caller writes to the location that was actually
    checked rather than re-deriving it.

    ``root`` need not exist yet; ``Path.resolve`` is non-strict.
    """
    try:
        root_resolved = root.resolve()
        resolved = (root / relative).resolve()
    except OSError as exc:  # pragma: no cover - platform-specific resolve failure
        raise UnsafeNodeIdError(
            f"refusing {label} '{relative}': cannot resolve it under {root} ({exc})"
        ) from exc
    if resolved != root_resolved and not resolved.is_relative_to(root_resolved):
        raise UnsafeNodeIdError(
            f"refusing {label} '{relative}': it resolves to {resolved}, outside "
            f"{root_resolved}"
        )
    return resolved
