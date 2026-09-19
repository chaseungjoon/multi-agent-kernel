"""The repository as a file -> source mapping, assembled on demand.

Post-wave and commit-time checks need "the whole repository as MAK holds it",
and eagerly assembling every file for every check is the O(repo) cost the
checks can least afford at commit time. This mapping knows every file path up
front (import resolution needs the full set) but assembles a file's source only
when a check reads it, then caches it.

``overrides`` substitutes a file's content — a prospective version with a
task's staged fragments, or the file as it was before the wave — and ``None``
removes a file (it did not exist yet).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping

from mak.node_store.reconstruction import assemble_fragments
from mak.node_store.store import NodeStore


class StoreSources(Mapping[str, str]):
    """A lazy ``{file path: source}`` view of the node store."""

    def __init__(
        self, store: NodeStore, overrides: Mapping[str, str | None] | None = None
    ) -> None:
        self._store = store
        self._overrides = dict(overrides or {})
        committed = {str(n).split("::", 1)[0] for n in store.list_nodes()}
        present = {f for f, s in self._overrides.items() if s is not None}
        removed = {f for f, s in self._overrides.items() if s is None}
        self._files = sorted((committed | present) - removed)
        self._keys = frozenset(self._files)
        self._cache: dict[str, str] = {}

    def __getitem__(self, path: str) -> str:
        """Return a file's source, assembled from fragments on first use."""
        if path not in self._keys:
            raise KeyError(path)
        if path not in self._cache:
            override = self._overrides.get(path)
            if override is not None:
                self._cache[path] = override
            else:
                fragments = self._store.get_committed_fragments(path)
                self._cache[path] = assemble_fragments(fragments) if fragments else ""
        return self._cache[path]

    def __iter__(self) -> Iterator[str]:
        """Iterate over every file path this view holds."""
        return iter(self._files)

    def __len__(self) -> int:
        """Return how many files this view holds."""
        return len(self._files)

    def __contains__(self, path: object) -> bool:
        """Whether this view holds ``path`` (nothing is assembled)."""
        return path in self._keys

    def with_overrides(self, overrides: Mapping[str, str | None]) -> StoreSources:
        """Return a new view with more files substituted (or removed)."""
        return StoreSources(self._store, {**self._overrides, **overrides})
