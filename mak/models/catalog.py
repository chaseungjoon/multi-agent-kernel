"""Model catalog types: the entry record and the provider lookup tables.

This is a **leaf module** — it deliberately does not import ``mak.bootstrap``
(which pulls in every agent adapter at import time) even though that module owns
the same provider maps. The duplication is pinned by a consistency test in
``tests/models/test_catalog.py`` so the two cannot drift apart silently.

A ``ModelEntry`` carries two kinds of field, and the distinction is load-bearing:

* **Facts** (``display_name``, ``context_window``, ``max_output``) are fetched
  from the provider and refreshed by ``mak.models.refresh``.
* **Judgment** (``recommended``, ``planner_ok``, ``planner_recommended``) is
  *never* inferred by MAK. It comes from the hand-maintained table in
  ``mak.models.curation`` and nowhere else.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from mak.models.curation import Judgment

# Friendly provider name -> adapter type registered in ``mak.bootstrap``.
PROVIDER_ADAPTER: dict[str, str] = {
    "anthropic": "anthropic_api",
    "openai": "openai_api",
    "gemini": "gemini_api",
}

# Friendly provider name -> conventional API-key environment variable.
PROVIDER_KEY_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}

# Display order used by every listing (``/models``, ``/planner``, completions).
PROVIDER_ORDER: tuple[str, ...] = ("anthropic", "openai", "gemini")

PROVIDER_DISPLAY: dict[str, str] = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "gemini": "Google Gemini",
}

KEY_ENV_TO_PROVIDER: dict[str, str] = {
    env: provider for provider, env in PROVIDER_KEY_ENV.items()
}


@dataclass(frozen=True, slots=True)
class ModelEntry:
    """One selectable model: endpoint facts joined with curated judgment.

    ``source`` records provenance (``"seed"`` = shipped default, ``"api"`` =
    seen in a fetch). ``retired`` marks an entry that a previous fetch offered
    but the latest one did not — kept visible so a model the user has
    configured never silently disappears mid-session.

    ``endpoint_id`` is what makes two identically-named models distinguishable.
    ``anthropic/claude-opus-5`` served by OpenRouter and ``claude-opus-5``
    served by Anthropic are different services with different capabilities,
    pricing and availability, so they are different *entries*. For the three
    built-in providers the endpoint id equals the provider name, which is why
    every pre-Wave-22 cache migrates without losing anything.

    ``evaluated`` says whether the judgment fields mean anything. MAK's curated
    table covers models a human has actually assessed; a model from a
    third-party catalog has not been, and the UI must say "not evaluated"
    rather than silently presenting the neutral defaults as an endorsement.
    """

    provider: str
    model_id: str
    display_name: str
    context_window: int | None = None
    max_output: int | None = None
    recommended: bool = False
    planner_ok: bool = True
    planner_recommended: bool = False
    source: str = "seed"
    retired: bool = False
    endpoint_id: str = ""
    evaluated: bool = True

    def __post_init__(self) -> None:
        """Default the endpoint id to the provider name for built-in entries."""
        if not self.endpoint_id:
            object.__setattr__(self, "endpoint_id", self.provider)

    @property
    def api_key_env(self) -> str:
        """Environment variable holding this model's provider API key."""
        try:
            return PROVIDER_KEY_ENV[self.provider]
        except KeyError:
            raise ValueError(f"unknown provider: {self.provider!r}") from None

    @property
    def adapter_type(self) -> str:
        """Agent adapter type that drives this model."""
        try:
            return PROVIDER_ADAPTER[self.provider]
        except KeyError:
            raise ValueError(f"unknown provider: {self.provider!r}") from None

    @property
    def key(self) -> tuple[str, str]:
        """Return the identity of this entry: its endpoint and model id."""
        return (self.endpoint_id, self.model_id)

    @property
    def spec(self) -> str:
        """Return the ``endpoint:model`` spec that selects this entry."""
        return f"{self.endpoint_id}:{self.model_id}"

    def planner_note(self) -> str:
        """Return the planner-quality note for listings, or an empty string.

        Three distinct states, and conflating any two of them misleads:
        unevaluated (MAK has no opinion), known-weak (a human said so), and
        fine (nothing to say).
        """
        if not self.evaluated:
            return "not evaluated"
        return "" if self.planner_ok else "may struggle with decomposition"

    def to_dict(self) -> dict[str, Any]:
        """Serialize the **facts** only — judgment is re-joined on load.

        ``evaluated`` is deliberately *not* written: it is derived from the
        provider on load (see :meth:`from_dict`), so a provider later added to
        the curated set upgrades every cached entry it already has instead of
        keeping a stale ``false`` until the next refetch.
        """
        return {
            "provider": self.provider,
            "endpoint_id": self.endpoint_id,
            "model_id": self.model_id,
            "display_name": self.display_name,
            "context_window": self.context_window,
            "max_output": self.max_output,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ModelEntry:
        """Build an entry from a manifest/seed mapping (judgment defaults).

        Whether the entry has been evaluated follows from its provider: MAK's
        curated table covers the built-in three and nothing else, so an entry
        from any other catalog has not been assessed by anyone.
        """
        provider = str(raw["provider"])
        return cls(
            provider=provider,
            model_id=str(raw["model_id"]),
            display_name=str(raw.get("display_name") or raw["model_id"]),
            context_window=_opt_int(raw.get("context_window")),
            max_output=_opt_int(raw.get("max_output")),
            source=str(raw.get("source", "seed")),
            endpoint_id=str(raw.get("endpoint_id") or provider),
            evaluated=provider in PROVIDER_KEY_ENV,
        )


def _opt_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def seed_path() -> Path:
    """Return the path to the packaged seed catalog."""
    return Path(__file__).resolve().parent / "seed.json"


def load_seed() -> tuple[ModelEntry, ...]:
    """Load the packaged seed catalog.

    The seed is the offline floor: a user with no API keys and no network still
    gets a usable model list. A missing or malformed seed yields an empty tuple
    rather than raising — a broken cache file must never break MAK's startup.
    """
    path = seed_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    if not isinstance(raw, list):
        return ()
    entries: list[ModelEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            entries.append(ModelEntry.from_dict(item))
        except (KeyError, TypeError):
            continue
    return tuple(entries)


def with_judgment(entry: ModelEntry, judgment: Judgment) -> ModelEntry:
    """Return ``entry`` with the curated judgment flags applied.

    Kept here (rather than in ``curation``) so the join direction is explicit:
    facts are the base record, judgment is layered on top and can be re-applied
    at any time without a refetch.

    ``evaluated`` is preserved, not set: whether a human has assessed this model
    is a property of the entry's origin, and applying a neutral default
    judgment to an unreviewed third-party model must not make it look reviewed.
    """
    return replace(
        entry,
        recommended=judgment.recommended,
        planner_ok=judgment.planner_ok,
        planner_recommended=judgment.planner_recommended,
    )
