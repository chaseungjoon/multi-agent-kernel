"""Output-token budgets resolved from the model catalog.

Both the planner and the agent adapters have to tell a provider how many output
tokens a reply may use, and both get it wrong in the same way when the number is
a constant: a budget smaller than the reply truncates it, and — because an
identical request produces an identically-cut reply — the truncation then repeats
on every retry until the task fails. The fix in both places is to ask the model
catalog what the model actually documents.

The two callers want different clamps (a plan spans a whole repo; an agent result
is a few nodes, but a whole-file node is one long string), so the range is a
parameter and this module owns only the lookup and the clamping rule. It lives in
``mak.core`` because an adapter importing from ``mak.planner`` would be the wrong
dependency direction.
"""

from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=1)
def documented_output_limits() -> dict[str, int]:
    """Return ``{model_id: max_output}`` from the model catalog.

    The catalog is an optimisation, not a dependency: a missing manifest or an
    unreadable cache yields an empty mapping, and every caller falls back to a
    constant rather than failing.
    """
    try:
        from mak.models.registry import ModelRegistry

        return {
            entry.model_id: entry.max_output
            for entry in ModelRegistry().all_models()
            if entry.max_output
        }
    except Exception:  # noqa: BLE001 - callers' fallbacks are safe
        return {}


def resolve_output_budget(
    model: str, *, fallback: int, minimum: int, maximum: int
) -> int:
    """Return the output-token budget to request for ``model``.

    Uses the model's documented output limit when the catalog knows it, clamped
    to ``[minimum, maximum]``; falls back to ``fallback`` for an unknown model.

    The floor wins over a documented limit *below* it: a catalog entry that small
    is almost certainly stale or wrong, and a budget under the floor truncates
    every real reply. A caller genuinely on such a model sets the budget
    explicitly (``agents[].max_tokens`` in the config) rather than inheriting it.
    """
    documented = documented_output_limits().get(model)
    if documented is None:
        return fallback
    return max(minimum, min(documented, maximum))
