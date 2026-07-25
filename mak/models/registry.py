"""Runtime model registry: the single object the CLI reads models from.

Load order on every (re)build:

1. the packaged seed (the offline floor),
2. overlaid with the manifest's per-provider cached facts,
3. joined with the curated judgment table.

Step 3 runs on *every* build, not just after a fetch, so editing
``mak.models.curation.CURATED`` takes effect immediately without a refetch.

**Concurrency.** The catalog is an immutable ``tuple`` held in one attribute. The
background refresh thread builds a new tuple and assigns it in a single
statement; readers take the attribute once. A lone assignment of an
already-constructed immutable object needs no lock, so there is none — and the
tuple is never mutated in place.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from mak.models.catalog import (
    PROVIDER_KEY_ENV,
    PROVIDER_ORDER,
    ModelEntry,
    load_seed,
    with_judgment,
)
from mak.models.curation import judgment_for
from mak.models.manifest import (
    Manifest,
    is_in_cooldown,
    is_refresh_due,
    load_manifest,
    manifest_path,
    save_manifest,
)
from mak.models.providers import ModelSource, default_sources
from mak.models.refresh import RefreshReport, refresh

# Escape hatch for users who do not want MAK making network calls at startup.
NO_REFRESH_ENV = "MAK_NO_MODEL_REFRESH"
_TRUE_STRINGS = {"1", "true", "yes", "on"}


def _now() -> datetime:
    return datetime.now(UTC)


def refresh_disabled_by_env() -> bool:
    """Return True when ``MAK_NO_MODEL_REFRESH`` opts out of auto refresh."""
    return os.environ.get(NO_REFRESH_ENV, "").strip().lower() in _TRUE_STRINGS


class ModelRegistry:
    """Holds the effective model catalog and drives refreshes."""

    def __init__(
        self,
        *,
        manifest_path_: Path | None = None,
        sources: Sequence[ModelSource] | None = None,
    ) -> None:
        self._path = manifest_path_ if manifest_path_ is not None else manifest_path()
        self._sources = tuple(sources) if sources is not None else default_sources()
        self._manifest = load_manifest(self._path)
        self._entries: tuple[ModelEntry, ...] = _build(self._manifest)
        self._refreshing = False

    # ── Reads ────────────────────────────────────────────────────────────────

    def all_models(self) -> tuple[ModelEntry, ...]:
        """Return the current catalog snapshot."""
        return self._entries

    def for_provider(self, provider: str) -> tuple[ModelEntry, ...]:
        """Return the catalog entries belonging to ``provider``."""
        return tuple(e for e in self._entries if e.provider == provider)

    def find(self, model_id: str) -> ModelEntry | None:
        """Return the entry with ``model_id``, or None when unknown."""
        return next((e for e in self._entries if e.model_id == model_id), None)

    def recommended_planner(self, provider: str) -> str:
        """Return the planner model to auto-select for ``provider``.

        Falls back down the chain — curated planner pick, curated agent pick,
        first unwarned model, first model — so removing a curated favourite
        degrades gracefully instead of raising.

        Retired models are skipped at every level: a model the provider no
        longer offers stays *selectable* (the user may have reasons) but is
        never what MAK auto-selects. Only if every candidate is retired does one
        get picked, since returning nothing would be worse.
        """
        candidates = self.for_provider(provider)
        if not candidates:
            return ""
        live = [m for m in candidates if not m.retired] or list(candidates)
        preferences: tuple[Callable[[ModelEntry], bool], ...] = (
            lambda m: m.planner_recommended,
            lambda m: m.recommended,
            lambda m: m.planner_ok,
        )
        for predicate in preferences:
            match = next((m for m in live if predicate(m)), None)
            if match is not None:
                return match.model_id
        return live[0].model_id

    @property
    def last_refresh(self) -> datetime | None:
        """When the catalog was last successfully refreshed (None = never)."""
        return self._manifest.last_refresh

    # ── Refresh ──────────────────────────────────────────────────────────────

    def refresh_now(self, api_keys: Mapping[str, str]) -> RefreshReport:
        """Fetch every provider synchronously and swap in the result.

        Ignores the schedule and the cooldown entirely — that is the whole point
        of the manual ``/refresh-models`` path: a model released on the 17th is
        usable on the 17th.
        """
        manifest, report = refresh(
            sources=self._sources,
            api_keys=api_keys,
            manifest=self._manifest,
            now=_now(),
        )
        self._apply(manifest)
        return report

    def maybe_auto_refresh(
        self, api_keys: Mapping[str, str], *, enabled: bool = True
    ) -> bool:
        """Start a background refresh when one is due. Returns whether it started.

        Returns immediately (doing nothing) when refresh is disabled, no API key
        is present, the schedule has not come due, or the cooldown is active.
        """
        if not enabled or refresh_disabled_by_env() or self._refreshing:
            return False
        if not any(
            api_keys.get(env, "").strip() for env in PROVIDER_KEY_ENV.values()
        ):
            return False
        now = _now()
        if not is_refresh_due(self._manifest.last_refresh, now):
            return False
        if is_in_cooldown(self._manifest.last_attempt, now):
            return False

        self._refreshing = True
        thread = threading.Thread(
            target=self._background_refresh,
            args=(dict(api_keys),),
            name="mak-model-refresh",
            daemon=True,
        )
        thread.start()
        return True

    def _background_refresh(self, api_keys: dict[str, str]) -> None:
        # A cache refresh may never take down the CLI, and this thread has no
        # console to report to (printing into a live prompt_toolkit session from
        # a background thread corrupts the prompt). Swallow everything.
        try:
            manifest, _report = refresh(
                sources=self._sources,
                api_keys=api_keys,
                manifest=self._manifest,
                now=_now(),
            )
            self._apply(manifest)
        except Exception:  # noqa: BLE001 - background cache refresh, never fatal
            pass
        finally:
            self._refreshing = False

    def _apply(self, manifest: Manifest) -> None:
        # Build the new snapshot first, then publish both fields. Readers see
        # either the old or the new tuple, never a partially-built one.
        entries = _build(manifest)
        self._manifest = manifest
        self._entries = entries
        try:
            save_manifest(manifest, self._path)
        except OSError:
            # An unwritable cache is not worth failing a run over; the refreshed
            # catalog still applies for this session.
            pass


def _build(manifest: Manifest) -> tuple[ModelEntry, ...]:
    """Compose seed + manifest facts + curated judgment into a sorted catalog."""
    seed = load_seed()
    seed_order = {e.model_id: i for i, e in enumerate(seed)}
    by_provider: dict[str, list[ModelEntry]] = {}

    for provider in PROVIDER_ORDER:
        seed_entries = [e for e in seed if e.provider == provider]
        if not manifest.has(provider):
            by_provider[provider] = list(seed_entries)
            continue

        fetched = list(manifest.models_for(provider))
        fetched_ids = {e.model_id for e in fetched}
        # A seed model the provider no longer offers stays visible but marked,
        # so a model the user has configured never silently disappears.
        retired = [
            ModelEntry(
                provider=e.provider,
                model_id=e.model_id,
                display_name=e.display_name,
                context_window=e.context_window,
                max_output=e.max_output,
                source=e.source,
                retired=True,
            )
            for e in seed_entries
            if e.model_id not in fetched_ids
        ]
        by_provider[provider] = fetched + retired

    ordered: list[ModelEntry] = []
    for provider in PROVIDER_ORDER:
        entries = by_provider.get(provider, [])
        entries.sort(
            key=lambda e: (
                e.retired,
                seed_order.get(e.model_id, len(seed_order)),
                e.model_id,
            )
        )
        ordered.extend(with_judgment(e, judgment_for(e.model_id)) for e in entries)
    return tuple(ordered)
