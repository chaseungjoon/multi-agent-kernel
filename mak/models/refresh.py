"""Refresh orchestration: fetch provider catalogs and fold them into a manifest.

Pure with respect to I/O — ``refresh`` takes sources plus the current manifest and
returns a *new* manifest; persisting it is the caller's job. That keeps the
add/remove/keep policy fully unit-testable without touching disk or network.

The policy that matters, in one sentence: **a provider's entries are replaced only
when that provider's fetch succeeds.** No key, an outage, a timeout, or a garbage
response all mean "keep what we had". A network blip must never empty the list.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from mak.models.catalog import PROVIDER_KEY_ENV, ModelEntry, load_seed
from mak.models.curation import display_name_for, filter_ids
from mak.models.manifest import Manifest, ProviderBlock
from mak.models.providers import (
    DEFAULT_TIMEOUT,
    FetchedModel,
    ModelFetchError,
    ModelSource,
)


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """Outcome of one provider's refresh attempt."""

    provider: str
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    total: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True when the fetch succeeded (entries were replaced)."""
        return self.error is None

    @property
    def changed(self) -> bool:
        """True when this provider's model set actually moved."""
        return bool(self.added or self.removed)


@dataclass(frozen=True, slots=True)
class RefreshReport:
    """Per-provider outcomes of one refresh pass."""

    results: tuple[ProviderResult, ...] = ()

    @property
    def ok(self) -> bool:
        """True when at least one provider fetch succeeded."""
        return any(r.ok for r in self.results)

    @property
    def changed(self) -> bool:
        """True when any provider's model set moved."""
        return any(r.changed for r in self.results)

    @property
    def errors(self) -> tuple[ProviderResult, ...]:
        """Results that failed, in provider order."""
        return tuple(r for r in self.results if not r.ok)


def _entry_for(
    provider: str, model_id: str, fetched: Mapping[str, FetchedModel]
) -> ModelEntry:
    """Build a catalog entry, resolving facts through a canonicalized alias.

    ``model_id`` may be an alias the provider did not return verbatim (it
    returned the dated snapshot instead), so fall back to the dated variant's
    facts rather than losing the context window.
    """
    facts = fetched.get(model_id)
    if facts is None:
        facts = next(
            (
                f
                for fetched_id, f in fetched.items()
                if fetched_id.startswith(f"{model_id}-")
            ),
            FetchedModel(model_id=model_id),
        )
    return ModelEntry(
        provider=provider,
        model_id=model_id,
        display_name=display_name_for(model_id, facts.display_name),
        context_window=facts.context_window,
        max_output=facts.max_output,
        source="api",
    )


def refresh(
    *,
    sources: Sequence[ModelSource],
    api_keys: Mapping[str, str],
    manifest: Manifest,
    now: datetime,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[Manifest, RefreshReport]:
    """Fetch every source and fold successful results into a new manifest.

    ``api_keys`` is keyed by environment-variable name (as
    ``cli.core.api_keys.load_keys`` returns it), so a provider with no key is
    reported as an error rather than attempted.
    """
    providers = dict(manifest.providers)
    seed = load_seed()
    results: list[ProviderResult] = []
    any_success = False

    for source in sources:
        provider = source.provider
        previous = tuple(e.model_id for e in manifest.models_for(provider))
        key = api_keys.get(PROVIDER_KEY_ENV.get(provider, ""), "").strip()

        if not key:
            results.append(
                ProviderResult(
                    provider=provider,
                    total=len(previous),
                    error="no API key",
                )
            )
            continue

        try:
            fetched = source.fetch(key, timeout=timeout)
        except ModelFetchError as exc:
            results.append(
                ProviderResult(
                    provider=provider, total=len(previous), error=str(exc)
                )
            )
            continue
        except Exception as exc:  # noqa: BLE001 - a source must never take MAK down
            results.append(
                ProviderResult(
                    provider=provider, total=len(previous), error=str(exc)
                )
            )
            continue

        by_id = {m.model_id: m for m in fetched}
        # The seed's ids are known-valid aliases, so a dated snapshot the
        # provider returns collapses onto the alias the user actually types.
        kept_ids = filter_ids(
            provider,
            list(by_id),
            known_aliases=[e.model_id for e in seed if e.provider == provider],
        )
        entries = tuple(
            _entry_for(provider, model_id, by_id) for model_id in kept_ids
        )

        previous_set, current_set = set(previous), set(kept_ids)
        results.append(
            ProviderResult(
                provider=provider,
                added=tuple(m for m in kept_ids if m not in previous_set),
                removed=tuple(m for m in previous if m not in current_set),
                total=len(entries),
            )
        )
        providers[provider] = ProviderBlock(fetched_at=now, models=entries)
        any_success = True

    updated = replace(
        manifest,
        providers=providers,
        last_attempt=now,
        # Only a successful pass consumes the scheduled tick; a fully-failed
        # attempt stays "due" so it retries once the cooldown expires.
        last_refresh=now if any_success else manifest.last_refresh,
    )
    return updated, RefreshReport(results=tuple(results))
