"""The output-token budget an API agent adapter requests, and how a cut is read.

An agent returns a node's **whole new source** in one structured response, so its
output budget is not a tuning knob — it is the largest file the agent can ever
write. A budget slightly under that size is the worst case: most nodes fit, the
occasional large one is cut off mid-array, and because an identical request is
cut at an identical point the failure repeats on every retry. That is exactly how
``_DEFAULT_MAX_TOKENS = 8192`` behaved against real modules of 17–26 KB.

The budget therefore comes from the model catalog (the same resolver the planner
uses), and every adapter turns a provider-signalled cut into a typed error rather
than a result. The clamp here is agent-shaped: a floor that fits a large
single-file rewrite, and the same ceiling the planner uses.
"""

from __future__ import annotations

from mak.core.budget import resolve_output_budget

# A whole-file node of ~26 KB costs roughly 7 900 output tokens once JSON-escaped,
# and a task may return more than one node — so the floor sits above the largest
# single file this codebase's agents have been asked to emit.
_MIN_MAX_TOKENS = 8192
# Matches the planner's ceiling: past this, the request is better narrowed than
# enlarged, and the adapters stream so the SDK's non-streaming window does not bite.
_MAX_MAX_TOKENS = 32000
# Used when the catalog knows nothing about the model.
_DEFAULT_MAX_TOKENS = 16384

# Provider stop signals that mean "the reply was cut off at the output cap".
TRUNCATION_STOP_REASONS = frozenset({"max_tokens", "length", "MAX_TOKENS"})
# ...and those that mean "the model declined", which no retry can fix.
REFUSAL_STOP_REASONS = frozenset(
    {
        "refusal",
        "content_filter",
        "SAFETY",
        "RECITATION",
        "PROHIBITED_CONTENT",
        "BLOCKLIST",
    }
)


def resolve_agent_max_tokens(model: str) -> int:
    """Return the output-token budget an agent adapter should request."""
    return resolve_output_budget(
        model,
        fallback=_DEFAULT_MAX_TOKENS,
        minimum=_MIN_MAX_TOKENS,
        maximum=_MAX_MAX_TOKENS,
    )
