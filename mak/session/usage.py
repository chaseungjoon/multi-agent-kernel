"""What a session has spent, in the tokens each provider reported."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping

# The two directional counters. A provider's own "total" field is never
# summed alongside them, so a backend that reports all three is not counted
# twice.
_DIRECTIONAL = ("input_tokens", "output_tokens")


def combined_usage(agents: Mapping[str, int], planner: object) -> dict[str, int]:
    """Every agent call's usage plus the planner's, when it reports any.

    The planner's share is included because a run's cost is not only its
    agents — decomposition, its retries, and the optional critique pass are
    all billed.
    """
    total = Counter(agents)
    planner_usage = getattr(planner, "token_usage", None)
    if isinstance(planner_usage, dict):
        total.update({k: v for k, v in planner_usage.items() if isinstance(v, int)})
    return dict(total)


def total_tokens(usage: Mapping[str, int]) -> int:
    """Input + output tokens in ``usage``."""
    return sum(value for key, value in usage.items() if key in _DIRECTIONAL)
