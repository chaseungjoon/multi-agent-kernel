"""Suggested local coding models — a hand-maintained table, nothing inferred.

This mirrors ``mak/models/curation.py``'s discipline exactly, and for the same
reason: MAK's model catalog separates **fact** (what a provider says exists,
fetched) from **judgment** (which of those are worth suggesting, written by a
person). ``mak/local`` keeps that split with the fact side moved: what models a
runtime has is asked of the running server, live, every time — a local model
list is authoritative, instant, and changes the moment the user pulls something,
so there is nothing to cache and nothing to retire.

**MAK does not benchmark or rank local models. A human edits this list.** It is
consulted in exactly one situation: the user has no model installed and asked
the wizard what to get. Entries are exact Ollama tags, because a suggestion that
cannot be pasted into ``ollama pull`` is not a suggestion.

Ordered **smallest first** on purpose: the wizard's default is the first entry,
and the machine most in need of a suggestion is the one least able to run a
large model.
"""

from __future__ import annotations

from dataclasses import dataclass

# Below this parameter count a model is small enough that MAK recommends a cloud
# planner beside it (hybrid mode) rather than asking it to plan a whole repo.
# A judgment, stated once, here — not a heuristic computed at the call site.
SMALL_MODEL_PARAMS_B = 10.0


@dataclass(frozen=True, slots=True)
class RecommendedModel:
    """One suggested local coding model."""

    tag: str
    params_b: float
    download_gb: float
    summary: str

    def is_small(self) -> bool:
        """Whether MAK suggests pairing this model with a cloud planner."""
        return self.params_b < SMALL_MODEL_PARAMS_B

    def describe(self) -> str:
        """Return the one-line form the wizard prints."""
        return f"{self.tag}  ({self.download_gb:g} GB) — {self.summary}"


RECOMMENDED: tuple[RecommendedModel, ...] = (
    RecommendedModel(
        tag="qwen2.5-coder:7b",
        params_b=7.6,
        download_gb=4.7,
        summary="the smallest model worth pointing at real code; runs on 8 GB",
    ),
    RecommendedModel(
        tag="qwen2.5-coder:14b",
        params_b=14.8,
        download_gb=9.0,
        summary="the default suggestion — clearly better edits, needs ~12 GB",
    ),
    RecommendedModel(
        tag="deepseek-coder-v2:16b",
        params_b=15.7,
        download_gb=8.9,
        summary="a mixture-of-experts alternative; fast for its size",
    ),
    RecommendedModel(
        tag="qwen2.5-coder:32b",
        params_b=32.8,
        download_gb=20.0,
        summary="the strongest of these; wants a 24 GB card or an M-series Mac",
    ),
)


def recommended_for(tag: str) -> RecommendedModel | None:
    """Return the table entry for an exact tag, or None when it is not listed.

    An unlisted model is not a judgment against it — the table is short by
    design. Callers must treat None as "no opinion", never as "not suitable".
    """
    for entry in RECOMMENDED:
        if entry.tag == tag:
            return entry
    return None


def default_suggestion() -> RecommendedModel:
    """Return the model the wizard offers first (the smallest listed)."""
    return RECOMMENDED[0]
