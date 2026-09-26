"""The optional LLM adjudicator, fenced off from the deterministic commit path."""

from __future__ import annotations

from collections.abc import Callable

from mak.config import SemanticConfig
from mak.core.exceptions import PlannerFailedError
from mak.core.logging import EventType
from mak.core.types import NodeId
from mak.planner.llm import build_planner_llm
from mak.planner.planner import PlannerLLM
from mak.semantic.adjudicator import Adjudicator
from mak.semantic.stale import StaleRead
from mak.session.events import EventLog
from mak.session.wave import WaveState


class AdjudicatorFence:
    """Builds the adjudicator once, re-budgets it per wave, and binds it per commit.

    The one model call on the commit path, and so the one nondeterministic
    decision in it. It is fenced three ways:

    - it exists only when ``semantic.adjudicator`` is configured — an injected
      model (``Session(adjudicator_llm=...)``) only replaces how that configured
      model is built, it never switches the adjudicator on by itself;
    - it can only turn a re-dispatch into an accept (see
      :mod:`mak.semantic.adjudicator`), for the stale reads the static checks
      cannot settle, under the ``revalidate`` policy (``validate_config``
      refuses it with any other);
    - every consultation is logged with ``nondeterministic: true`` and every
      accept is counted on the wave (``adjudicated_accepts``).
    """

    def __init__(
        self, *, config: SemanticConfig, llm: PlannerLLM | None, log: EventLog
    ) -> None:
        self._config = config
        self._llm = llm
        self._log = log
        self._instance: Adjudicator | None = None
        self._active: Adjudicator | None = None

    @property
    def configured(self) -> bool:
        """Whether the project turned the adjudicator on at all."""
        return self._config.adjudicator is not None

    def start_wave(self) -> None:
        """Build the adjudicator on first use and give it a fresh wave budget."""
        if not self.configured:
            self._active = None
            return
        if self._instance is None:
            llm = self._llm or self._configured_llm()
            if llm is None:
                self._active = None
                return
            self._instance = Adjudicator(
                llm,
                max_calls=self._config.adjudicator_max_calls,
                log=lambda **p: self._log(
                    EventType.ADJUDICATION, nondeterministic=True, **p
                ),
            )
        self._instance.reset()
        self._active = self._instance

    def bind(
        self, wave: WaveState, staged: dict[NodeId, str], accepted: set[NodeId]
    ) -> Callable[[StaleRead], bool | None] | None:
        """Return the adjudicator for one commit's staged code, or None when off.

        Every stale read it accepts is added to ``accepted`` and counted on the
        wave, so the commit can mark those decisions and the run can report them.
        """
        active = self._active
        if active is None:
            return None

        def adjudicate(stale: StaleRead) -> bool | None:
            answer = active(stale, staged)
            if answer is True:
                accepted.add(stale.node_id)
                wave.adjudicated_accepts += 1
            return answer

        return adjudicate

    def _configured_llm(self) -> PlannerLLM | None:
        """Build the configured adjudicator model, or None when it cannot be."""
        spec = self._config.adjudicator
        if spec is None:
            return None
        backend, _, model = spec.partition(":")
        try:
            return build_planner_llm(model, backend=backend)
        except PlannerFailedError as exc:
            self._log(EventType.GATE_FINDING, gate="adjudicator", error=str(exc))
            return None
