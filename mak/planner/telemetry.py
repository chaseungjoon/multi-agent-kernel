"""What each planner call cost, recorded as it happens.

The planner's input is the largest prompt a run sends, and before this module
nothing logged its size: ``Planner.token_usage`` summed a total, but no record
said how big the inventory was, how many rounds a plan took, or how much of a
prompt a provider served from its cache. A :class:`PlannerCall` is one LLM
call's measurements — **sizes and counts only**: no prompt text, no source and
no file names, so the event log never becomes a second copy of the repository.

Calls are reported to an observer *live*, one by one, rather than returned at
the end: a plan that ends in ``PlannerFailedError`` still logs every call it
paid for.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from mak.core.types import SubTask

# Values of ``PlannerCall.phase``.
PHASES = ("plan", "outline", "detail", "critique", "expand", "verify")
# Values of ``PlannerCall.outcome``.
OUTCOMES = ("plan", "expand", "rejected", "call_failed", "truncated")


@dataclass(frozen=True, slots=True)
class InventoryStats:
    """How much of the repository one prompt showed, and what it left out.

    ``files_shown`` counts files whose node ids are all visible in the prompt:
    every file for the flat ``oneshot`` listing, the files shown at level 1 for
    ``full`` and ``retrieval``. ``symbols_truncated`` counts what the budget
    left out: nodes of large files cut short, plus expansion requests that did
    not fit.
    """

    files_total: int = 0
    files_shown: int = 0
    nodes_shown: int = 0
    chars: int = 0
    collapsed_dirs: int = 0
    symbols_truncated: int = 0
    seed_files: int = 0
    expanded_paths: int = 0


@dataclass(frozen=True, slots=True)
class PlannerCall:
    """One planner LLM call: where it sat in the plan, its size, its usage."""

    phase: str
    round: int
    attempt: int
    strategy: str
    prompt_chars: int
    stable_chars: int
    inventory_files_total: int
    inventory_files_shown: int
    inventory_nodes_shown: int
    inventory_chars: int
    collapsed_dirs: int
    symbols_truncated: int
    seed_files: int
    expanded_paths: int
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    outcome: str
    duration_ms: float


@dataclass(frozen=True, slots=True)
class CallSite:
    """Everything about a call that is known before it is made."""

    phase: str
    round: int
    strategy: str
    inventory: InventoryStats


def make_call(
    site: CallSite,
    *,
    attempt: int,
    prompt_chars: int,
    stable_chars: int,
    usage: Mapping[str, int],
    outcome: str,
    duration_ms: float,
) -> PlannerCall:
    """Build the record of one finished attempt at ``site``."""
    stats = site.inventory
    return PlannerCall(
        phase=site.phase,
        round=site.round,
        attempt=attempt,
        strategy=site.strategy,
        prompt_chars=prompt_chars,
        stable_chars=stable_chars,
        inventory_files_total=stats.files_total,
        inventory_files_shown=stats.files_shown,
        inventory_nodes_shown=stats.nodes_shown,
        inventory_chars=stats.chars,
        collapsed_dirs=stats.collapsed_dirs,
        symbols_truncated=stats.symbols_truncated,
        seed_files=stats.seed_files,
        expanded_paths=stats.expanded_paths,
        input_tokens=int(usage.get("input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
        cached_input_tokens=int(usage.get("cached_input_tokens", 0)),
        cache_write_tokens=int(usage.get("cache_write_tokens", 0)),
        outcome=outcome,
        duration_ms=round(duration_ms, 3),
    )


@dataclass(frozen=True, slots=True)
class PlanningSummary:
    """A plan's calls folded into the numbers ``plan_metrics`` reports.

    ``rounds`` counts the plan's main rounds (one for ``oneshot`` and ``full``,
    one per expansion or verification for ``retrieval``, the outline plus each
    detail step for ``outline``); a critique pass is a call but not a round.
    """

    calls: int = 0
    rounds: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0

    @classmethod
    def of(cls, calls: Sequence[PlannerCall], rounds: int) -> PlanningSummary:
        """Summarize ``calls``, a plan that took ``rounds`` rounds."""
        return cls(
            calls=len(calls),
            rounds=rounds,
            input_tokens=sum(c.input_tokens for c in calls),
            cached_tokens=sum(c.cached_input_tokens for c in calls),
            output_tokens=sum(c.output_tokens for c in calls),
        )

    def as_metrics(self) -> dict[str, float]:
        """Return the ``planner_*`` entries of ``SessionResult.metrics``."""
        return {
            "planner_calls": float(self.calls),
            "planner_rounds": float(self.rounds),
            "planner_input_tokens": float(self.input_tokens),
            "planner_cached_tokens": float(self.cached_tokens),
            "planner_output_tokens": float(self.output_tokens),
        }


# A wave that was not planned by the planner (a cascade wave, a plan installed
# directly) reports zeros rather than the previous plan's numbers.
NO_PLANNING = PlanningSummary()

PlannerObserver = Callable[[PlannerCall], None]


class CallRecorder:
    """Collect one plan's calls and hand each to the observer as it finishes."""

    def __init__(self, observer: PlannerObserver | None) -> None:
        self._observer = observer
        self.calls: list[PlannerCall] = []

    def record(self, call: PlannerCall) -> None:
        """Keep ``call`` and report it."""
        self.calls.append(call)
        if self._observer is not None:
            self._observer(call)


@dataclass(frozen=True, slots=True)
class PlanOutcome:
    """What ``Planner.plan`` produced and what producing it cost.

    ``seen_files`` is the set of files the model was shown at level 1 when the
    strategy was ``retrieval``, and ``None`` for every strategy that shows the
    whole inventory — validation reads ``None`` as "no retrieval".
    """

    plan: list[SubTask]
    strategy: str
    seen_files: frozenset[str] | None
    calls: tuple[PlannerCall, ...]
    summary: PlanningSummary
