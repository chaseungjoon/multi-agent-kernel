"""On-disk model manifest: the cached provider catalogs and refresh schedule.

The manifest lives in the per-user config dir (``~/.config/mak/models.json``),
never inside the package: an installed MAK (``uv tool install`` / ``pipx``) has a
read-only package directory, and a per-user cache survives upgrades — the same
reasoning as ``~/.config/mak/.env``.

Every read path is **total**: a missing, unreadable, corrupt, or
wrong-schema file yields an empty manifest rather than raising. A broken cache
must degrade to the packaged seed, never break MAK's startup.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mak.config import user_config_dir
from mak.models.catalog import ModelEntry

SCHEMA_VERSION = 1

# Scheduled refresh ticks: the 1st and the 15th of each month.
REFRESH_DAYS: tuple[int, ...] = (1, 15)

# Minimum gap between *attempts* after a failure, so a permanently-offline user
# does not pay a network timeout on every single start.
COOLDOWN_HOURS = 6


@dataclass(frozen=True, slots=True)
class ProviderBlock:
    """One provider's cached model facts and the time they were fetched."""

    fetched_at: datetime | None = None
    models: tuple[ModelEntry, ...] = ()


@dataclass(frozen=True, slots=True)
class Manifest:
    """The full cache: per-provider blocks plus refresh bookkeeping.

    ``last_refresh`` advances only when at least one provider fetch succeeded;
    ``last_attempt`` advances on every attempt. Keeping them separate is what
    makes a fully-offline attempt retry later instead of silently consuming the
    scheduled tick.
    """

    last_refresh: datetime | None = None
    last_attempt: datetime | None = None
    providers: dict[str, ProviderBlock] = field(default_factory=dict)

    def models_for(self, provider: str) -> tuple[ModelEntry, ...]:
        """Return cached entries for ``provider`` (empty when never fetched)."""
        block = self.providers.get(provider)
        return block.models if block is not None else ()

    def has(self, provider: str) -> bool:
        """Return True when ``provider`` has a block from a successful fetch."""
        return provider in self.providers


def manifest_path() -> Path:
    """Return the per-user manifest location."""
    return user_config_dir() / "models.json"


def _parse_dt(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def load_manifest(path: Path | None = None) -> Manifest:
    """Load the manifest, returning an empty one on any problem."""
    target = path if path is not None else manifest_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Manifest()
    if not isinstance(raw, dict):
        return Manifest()
    if raw.get("schema_version") != SCHEMA_VERSION:
        # A future or unknown schema is treated as no cache: the seed still
        # works, and the next refresh rewrites the file in the current shape.
        return Manifest()

    providers: dict[str, ProviderBlock] = {}
    raw_providers = raw.get("providers")
    if isinstance(raw_providers, dict):
        for provider, block in raw_providers.items():
            if not isinstance(block, dict):
                continue
            entries: list[ModelEntry] = []
            for item in block.get("models", []):
                if not isinstance(item, dict):
                    continue
                try:
                    entries.append(
                        ModelEntry.from_dict({**item, "source": "api"})
                    )
                except (KeyError, TypeError):
                    continue
            providers[str(provider)] = ProviderBlock(
                fetched_at=_parse_dt(block.get("fetched_at")),
                models=tuple(entries),
            )

    return Manifest(
        last_refresh=_parse_dt(raw.get("last_refresh")),
        last_attempt=_parse_dt(raw.get("last_attempt")),
        providers=providers,
    )


def save_manifest(manifest: Manifest, path: Path | None = None) -> None:
    """Write the manifest atomically (temp file in the same dir + replace)."""
    target = path if path is not None else manifest_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "last_refresh": (
            manifest.last_refresh.isoformat() if manifest.last_refresh else None
        ),
        "last_attempt": (
            manifest.last_attempt.isoformat() if manifest.last_attempt else None
        ),
        "providers": {
            provider: {
                "fetched_at": (
                    block.fetched_at.isoformat() if block.fetched_at else None
                ),
                "models": [entry.to_dict() for entry in block.models],
            }
            for provider, block in manifest.providers.items()
        },
    }
    tmp = target.with_name(f"{target.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def last_scheduled_tick(
    now: datetime, *, days: tuple[int, ...] = REFRESH_DAYS
) -> datetime:
    """Return the most recent scheduled refresh moment at or before ``now``.

    Ticks are midnight on each day in ``days``. When ``now`` precedes every tick
    in its own month, the answer rolls back into the previous month.
    """
    candidates = [
        now.replace(
            day=day, hour=0, minute=0, second=0, microsecond=0
        )
        for day in sorted(days)
        if day <= now.day
    ]
    if candidates:
        return max(candidates)
    # Before the first tick of this month -> the last tick of the previous month.
    first_of_month = now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    prev_month_end = first_of_month - timedelta(days=1)
    return prev_month_end.replace(
        day=max(days), hour=0, minute=0, second=0, microsecond=0
    )


def is_refresh_due(
    last_refresh: datetime | None,
    now: datetime,
    *,
    days: tuple[int, ...] = REFRESH_DAYS,
) -> bool:
    """Return True when a scheduled refresh has come due.

    This is catch-up, not calendar-exact: a user who skips the 15th and starts
    MAK on the 19th refreshes on the 19th, because the 15th's tick is still
    newer than their last refresh.
    """
    if last_refresh is None:
        return True
    return last_scheduled_tick(now, days=days) > last_refresh


def is_in_cooldown(
    last_attempt: datetime | None, now: datetime, *, hours: int = COOLDOWN_HOURS
) -> bool:
    """Return True when the previous attempt is too recent to retry."""
    if last_attempt is None:
        return False
    return now - last_attempt < timedelta(hours=hours)
