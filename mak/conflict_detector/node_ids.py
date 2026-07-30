"""Reading structure out of the edit keys the conflict detector is handed.

An ``EditRound`` is keyed by MAK node ids (``file.py::kind::name``) whenever the
session builds it, but the checks also accept plain agent ids (``agent_a``) — the
form the unit tests and older callers use. These helpers extract what the checks
need from a key, and answer "nothing" for the plain form, so every check scopes
itself the same way instead of re-parsing ids inline.
"""

from __future__ import annotations

# Fragment kinds (see ``mak.node_store.ingestion``) whose stored source is the
# *inside* of a class, dedented to column 0 by the node store.
_CLASS_SCOPED_KINDS = frozenset({"method", "class_body"})


def file_scope_of(edit_key: str) -> str:
    """Return the file component of a node id, or ``""`` for a plain agent id.

    Checks that are only meaningful within one file (imports, name collisions)
    use this as their comparison scope: the same name in two different files is
    not a conflict. Plain agent ids share the single ``""`` scope, preserving the
    original per-agent semantics.

    A **whole-file** node id (``editor/motions.py``) carries no ``::`` and so
    would otherwise be indistinguishable from an agent id — which would put two
    freshly created files into one scope and report their identically named
    ``main`` functions as colliding. A key whose last segment has an extension is
    therefore read as a file path and scopes to itself.
    """
    if "::" in edit_key:
        return edit_key.split("::", 1)[0]
    return edit_key if "." in edit_key.rsplit("/", 1)[-1] else ""


def class_scope_of(edit_key: str) -> str | None:
    """Return the class a class-scoped fragment belongs to, else None.

    ``file.py::method::Buffer.get`` and ``file.py::class_body::Buffer#2`` both
    hold source that lives *inside* ``class Buffer`` but is stored dedented, so
    their symbols and signatures must be attributed to ``Buffer`` rather than to
    the module.
    """
    parts = edit_key.split("::")
    if len(parts) < 3 or parts[1] not in _CLASS_SCOPED_KINDS:
        return None
    name = parts[2].split(".", 1)[0].split("#", 1)[0]
    return name if name.isidentifier() else None
