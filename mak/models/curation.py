"""Hand-maintained model curation: judgment table and eligibility filters.

Two concerns live here and **conflating them is the mistake this module exists to
prevent**:

1. **Eligibility** (``DENY``, ``filter_ids``) — factual and automatic. "Is this a
   text-chat model at all?" ``dall-e-3`` and ``text-embedding-3-large`` are not
   opinions about quality; they are the wrong *kind* of endpoint and a provider's
   list endpoint returns them alongside chat models.

2. **Judgment** (``CURATED``, ``judgment_for``) — never automatic. One
   hand-maintained table keyed by **exact model id**. There are no heuristics, no
   capability thresholds, no id-pattern inference, and no scoring. MAK does not
   decide whether a model is good; a human does, by editing ``CURATED``.

A model that ships tomorrow is immediately usable and immediately selectable — it
simply carries neutral judgment (no ★, no warning) until someone curates it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase


@dataclass(frozen=True, slots=True)
class Judgment:
    """Curated opinion about a model. Defaults are deliberately neutral.

    ``planner_ok=True`` by default means "no opinion" — the CLI shows no warning.
    Only an explicit ``CURATED`` entry can set it False, because a warning is an
    assertion about the model that MAK is not entitled to make on its own.
    """

    recommended: bool = False
    planner_ok: bool = True
    planner_recommended: bool = False


# ── Judgment ──────────────────────────────────────────────────────────────────
# HAND-MAINTAINED, EXACT MODEL IDS ONLY.
#
# Adding a model here is a human decision. Nothing infers entries into this
# table, and no refresh ever writes to it. A model absent from this table gets
# Judgment() — no star, no warning, fully usable. When a new flagship ships, the
# ★ moves late and on purpose: a wrong automated judgment is worse than a
# missing one.
#
#   recommended          -> ★ default agent model for that provider
#   planner_recommended  -> the planner MAK auto-selects for that provider
#   planner_ok=False     -> "may struggle with complex task decomposition"
CURATED: dict[str, Judgment] = {
    # Anthropic
    "claude-opus-5": Judgment(planner_recommended=True),
    "claude-sonnet-5": Judgment(recommended=True),
    "claude-haiku-4-5": Judgment(planner_ok=False),
    # OpenAI
    "gpt-5.6-sol": Judgment(recommended=True),
    "gpt-5.6-luna": Judgment(planner_ok=False),
    # Google Gemini
    "gemini-3.5-flash": Judgment(recommended=True),
    "gemini-3.1-flash-lite": Judgment(planner_ok=False),
}


# ── Eligibility ───────────────────────────────────────────────────────────────
# NOT judgment: these patterns exclude endpoints that are not text-chat models
# at all (images, audio, embeddings, moderation, fine-tuning). A provider's
# list-models endpoint returns them mixed in with chat models.
DENY: dict[str, tuple[str, ...]] = {
    "anthropic": (),
    "openai": (
        "dall-e*",
        "whisper*",
        "*tts*",          # text-to-speech, incl. dated variants
        "text-embedding*",
        "*embedding*",
        "*moderation*",
        "babbage*",
        "davinci*",
        "sora*",
        "*-audio*",
        "*-realtime*",
        "*-transcribe*",
        "*-search*",
        "*instruct*",     # completion-style, not the chat surface
        "*image*",
    ),
    "gemini": (
        "*embedding*",
        "aqa",
        "imagen*",
        "veo*",
        "lyria*",         # music generation
        "*image*",        # image generation
        "nano-banana*",   # Google's image-generation family codename
        "*tts*",          # text-to-speech
        "*-tuning*",
        "learnlm*",
    ),
}

# A dated snapshot suffix: "-2025-10-01" or "-20251001".
_DATED_SUFFIX = re.compile(r"^(?P<base>.+?)-(?:\d{4}-\d{2}-\d{2}|\d{8})$")


def judgment_for(model_id: str) -> Judgment:
    """Return the curated judgment for ``model_id``, or a neutral default.

    This is the **entire** judgment logic. Do not add a fallback ladder,
    pattern matching, or capability heuristics here — see the module docstring.
    """
    return CURATED.get(model_id, Judgment())


def is_denied(provider: str, model_id: str) -> bool:
    """Return True when ``model_id`` is the wrong kind of endpoint here."""
    return any(
        fnmatchcase(model_id, pattern) for pattern in DENY.get(provider, ())
    )


def filter_ids(
    provider: str,
    ids: Sequence[str],
    *,
    known_aliases: Sequence[str] = (),
) -> list[str]:
    """Drop non-chat endpoints and canonicalize dated snapshots to their alias.

    ``claude-haiku-4-5-20251001`` collapses to ``claude-haiku-4-5`` when that
    undated alias is known — either because the provider also returned it, or
    because ``known_aliases`` (in practice, the packaged seed) says the alias
    exists. Anthropic's list endpoint returns the dated form for older models
    while the alias remains valid, so without this the catalog would show the
    snapshot *and* mark the alias retired.

    This is canonicalization, not judgment: an alias and its snapshot are the
    same model, which is a fact about the provider's naming, not an opinion
    about quality. A model that only ever ships dated (no known alias) survives
    verbatim — dropping it would make it unselectable.

    Order is preserved so listings stay stable across refreshes.
    """
    allowed = [
        model_id
        for model_id in ids
        if model_id and not is_denied(provider, model_id)
    ]
    bases = set(allowed) | set(known_aliases)
    kept: list[str] = []
    seen: set[str] = set()
    for model_id in allowed:
        match = _DATED_SUFFIX.match(model_id)
        canonical = model_id
        if match is not None and match.group("base") in bases:
            canonical = match.group("base")
        if canonical in seen:
            continue
        seen.add(canonical)
        kept.append(canonical)
    return kept


def display_name_for(model_id: str, api_display_name: str = "") -> str:
    """Prefer the provider's own display name; fall back to the id verbatim.

    No title-casing or prettifying — a guessed label is a small lie about what
    the provider calls the model.
    """
    name = api_display_name.strip()
    return name or model_id
