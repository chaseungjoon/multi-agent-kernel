"""An LLM adjudicator for the stale reads the static checks cannot settle (D7).

A stale read the kernel cannot classify — a return type changed under code that
uses the result, a class's fields changed under code that reads them — is sent
back for a re-dispatch by default. That is always safe and sometimes a whole
agent call spent on nothing. When configured (``semantic.adjudicator``), a cheap
model is asked the one question that matters: *does B's use of X still hold
under A's change?*

It can only ever **accept**: a "yes" turns a re-dispatch into an accept, while
"no", "unsure", an error, or an exhausted budget leave the re-dispatch in
place. It is never the only gate — the static checks have already run, and a
defect they found is never overridden (see :func:`mak.semantic.stale.decide`).
"""

from __future__ import annotations

from collections.abc import Callable

from mak.core.types import NodeId
from mak.planner.planner import PlannerLLM
from mak.semantic.stale import StaleRead

_PROMPT = """\
You are checking one concurrent edit for a semantic conflict. Task B wrote the \
code below while node {node} was being changed by another task. B saw the OLD \
version of {node}. Decide whether B's use of {node} is still correct under the \
NEW version.

OLD {node}:
{old}

NEW {node}:
{new}

B's code:
{code}

Answer with exactly one word: YES (B's use still holds), NO (it does not), or \
UNSURE."""

_MAX_SECTION = 4000


class Adjudicator:
    """Budgeted yes/no/unsure calls to a cheap model; logs every one."""

    def __init__(
        self,
        llm: PlannerLLM,
        *,
        max_calls: int,
        log: Callable[..., None],
    ) -> None:
        self._llm = llm
        self._max_calls = max_calls
        self._log = log
        self.calls = 0

    def reset(self) -> None:
        """Start a new wave's budget."""
        self.calls = 0

    def __call__(self, stale: StaleRead, staged: dict[NodeId, str]) -> bool | None:
        """Return True when the model says B's use still holds; None otherwise."""
        if self.calls >= self._max_calls:
            self._log(node_id=str(stale.node_id), answer="skipped", reason="budget")
            return None
        self.calls += 1
        prompt = _PROMPT.format(
            node=stale.node_id,
            old=_clip(stale.mark.source or "(not recorded)"),
            new=_clip(stale.current_source or "(deleted)"),
            code=_clip("\n\n".join(staged.values())),
        )
        try:
            reply = self._llm.complete(prompt)
        except Exception as exc:  # noqa: BLE001 - an adjudicator failure is "unsure"
            self._log(node_id=str(stale.node_id), answer="error", reason=str(exc))
            return None
        answer = _answer(reply)
        self._log(node_id=str(stale.node_id), answer=answer or "unparsed")
        return True if answer == "yes" else None


def _answer(reply: str) -> str | None:
    words = reply.strip().split()
    if not words:
        return None
    first = words[0].strip(".,!:;*`\"'").lower()
    return first if first in ("yes", "no", "unsure") else None


def _clip(text: str) -> str:
    if len(text) <= _MAX_SECTION:
        return text
    return text[:_MAX_SECTION] + "\n… (truncated)"
