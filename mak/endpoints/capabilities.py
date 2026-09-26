"""Session-lifetime memory of what each endpoint/model pair actually supports.

"OpenAI-compatible" is a family resemblance, not a contract. One service
implements strict JSON schema, another only ``json_object``, a third rejects
``response_format`` entirely and has to be asked in the prompt. MAK discovers
which by trying, stepping down a rung on a verified rejection.

Without a memory, every task pays that discovery again: a run of forty tasks
against a server with no structured-output support makes forty wasted requests
before forty successful ones. The adapter cannot hold the memory itself, because
the registry builds a fresh adapter per dispatch — anything kept on the instance
dies with it.

So the cache is an object the **composition root** creates once and closes every
adapter factory over. That is not global mutable state (AGENTS.md): there is no
module-level dict, nothing is importable-and-mutable, and two sessions in one
process each get their own.

Keyed by ``(endpoint id, model id)`` because the same model id served by two
endpoints is two different implementations with two different capability sets —
``anthropic/claude-opus-5`` through OpenRouter is not the same server as
Anthropic's own. The model id is kept **whole**: in the live catalog,
``inclusionai/ling-3.0-flash-vl`` reports full structured-output support while
``inclusionai/ling-3.0-flash-vl:free`` reports none. Stripping a ``:free``
suffix to "canonicalize" would cache one product's capabilities under the
other's name.

**Two kinds of evidence, kept apart.**

*Reported* parameters come from the endpoint's ``/models`` listing. They are a
claim, not a proof: they choose where the ladder *starts* and descent below that
remains available. *Proven* modes come from a real successful call and pin the
mode exactly. Storing them in one field would let a catalog claim masquerade as
a verified fact, and a stale catalog would then be unfixable within the session.

**Single-flight discovery.** Four agents starting together used to
each see "unknown" and each walk the same failing ladder, paying four times for
one predictable mismatch. The first caller now owns discovery for a key and the
rest wait on it, then start from what it learned. No network call happens while
the cache's lock is held, waiters are released on success *and* on exception, a
failed owner does not poison the key, and the wait is bounded so a wedged owner
cannot stall the pool.

Thread-safe by construction: agents run concurrently in scheduler threads, so
every read and write takes the lock. The critical sections are dictionary
operations, so the contention cost is nil next to an HTTP round trip.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

# The descent order. A rejection at one rung moves to the next; reaching the end
# means the endpoint gets a bare prompt-only request, which is all a server with
# no structured-output support can be asked for.
STRUCTURED_OUTPUT_LADDER: tuple[str, ...] = ("json_schema", "json_object", "none")

# The bottom rung: a prompt-only request, available from every server.
PROMPT_ONLY = "none"

# Which reported request parameter authorizes which rung.
#
# These two names are **not** synonyms, as measured against the live
# OpenRouter catalog: ``google/gemma-4-31b-it:free`` reports ``response_format``
# and not ``structured_outputs``, and it accepts ``{"type": "json_object"}``
# while refusing a strict ``json_schema``. Across the 446-model catalog, 30
# models sit in exactly that gap. Gating both rungs on ``response_format``
# alone — the obvious reading — would keep sending those 30 a schema they
# cannot honor, one wasted call per task.
#
# ``none`` is deliberately absent: it requires no parameter, so no catalog
# report can rule it out.
RUNG_PARAMETER: dict[str, str] = {
    "json_schema": "structured_outputs",
    "json_object": "response_format",
}

# How long a waiter blocks on another thread's discovery before giving up and
# probing for itself. Bounded on purpose: a wedged owner — a provider that
# accepts a connection and never answers — must cost one agent its timeout, not
# the whole pool's. Generous enough that a normal ladder (two rejections and a
# completion) finishes well inside it.
DISCOVERY_WAIT_SECONDS = 120.0


def rungs_from(mode: str) -> tuple[str, ...]:
    """Return the descent path starting at ``mode``, inclusive.

    ``json_schema`` yields all three rungs, so a server rejecting both schema
    *and* object still reaches prompt-only JSON. The pre-Wave-22 code allowed a
    single downgrade, which made that final rung unreachable from the top.
    """
    try:
        index = STRUCTURED_OUTPUT_LADDER.index(mode)
    except ValueError:
        return (mode,)
    return STRUCTURED_OUTPUT_LADDER[index:]


def start_rung_for(parameters: frozenset[str] | None) -> str | None:
    """Return the highest rung a reported parameter set authorizes.

    ``None`` means *nothing is ruled out* — keep whatever ladder the user and
    the endpoint configured. A string means "start no higher than this", with
    descent below it still available, because a report is a claim and not a
    proof.

    Two states both mean unknown, and conflating them with "unsupported" is a
    real bug in both directions:

    * ``None`` — the service published no capability metadata at all. Most
      OpenAI-compatible ``/models`` implementations return bare ids, and
      treating that silence as "no structured output" would disable schema
      enforcement for every one of them.
    * ``frozenset()`` — the service published the field and it was empty.
      OpenRouter's catalog has published three such models
      (``openrouter/fusion`` and two siblings, all auto-routers), and probing
      confirmed ``openrouter/fusion`` *succeeds* with a strict ``json_schema``
      request. An empty list is a service declining to enumerate, not a service
      declaring nothing works.
    """
    if not parameters:
        return None
    if RUNG_PARAMETER["json_schema"] in parameters:
        return None
    if RUNG_PARAMETER["json_object"] in parameters:
        return "json_object"
    return PROMPT_ONLY


def rung_is_reported(mode: str, parameters: frozenset[str] | None) -> bool:
    """Whether a report positively confirms the parameter backing ``mode``.

    Positive confirmation only — an unknown or empty report answers ``False``.
    This is what gates OpenRouter's ``provider.require_parameters`` routing
    guard: that guard answers 404 when it filters every route away, so sending
    it on a guess converts a recoverable provider rejection into a hard routing
    failure. Sent only where the catalog says it will hold, it does its job and
    cannot manufacture that 404.
    """
    parameter = RUNG_PARAMETER.get(mode)
    if parameter is None:
        # ``none`` needs no parameter, so there is nothing for a routing filter
        # to require and nothing to confirm.
        return False
    if not parameters:
        # Unknown, or reported empty. Neither is positive confirmation.
        return False
    return parameter in parameters


@dataclass(frozen=True, slots=True)
class CapabilityKey:
    """What a cached capability belongs to: one model at one endpoint."""

    endpoint_id: str
    model_id: str


@dataclass(frozen=True, slots=True)
class Discovery:
    """One caller's standing with respect to discovering a key's capability.

    ``owned`` is True for the single caller that must do the probing.
    ``mode`` is a mode already known — proven by an earlier call, or learned by
    the owner this caller waited on — and is ``None`` when nothing is known.
    ``reported`` is the endpoint's published parameter set, for choosing the
    starting rung and the routing guard.
    """

    owned: bool
    mode: str | None = None
    reported: frozenset[str] | None = None


class CapabilityCache:
    """Remembers the structured-output mode that worked, per endpoint and model.

    Created once per session by the composition root and passed explicitly to
    every adapter factory. Never a module-level singleton.
    """

    def __init__(self, *, wait_seconds: float = DISCOVERY_WAIT_SECONDS) -> None:
        self._lock = threading.Lock()
        # Proven by a real successful call. Pins the mode exactly.
        self._structured_output: dict[CapabilityKey, str] = {}
        # Claimed by the endpoint's /models listing. Chooses a starting rung.
        self._reported: dict[CapabilityKey, frozenset[str]] = {}
        # Which (endpoint, model) pairs have already logged their selected rung,
        # so a forty-task run reports the discovery once rather than forty times.
        self._announced: set[CapabilityKey] = set()
        # Keys currently being discovered, each with the event its waiters
        # block on. Removed on completion, so the cache stays bounded.
        self._in_flight: dict[CapabilityKey, threading.Event] = {}
        self._wait_seconds = wait_seconds

    def structured_output(self, endpoint_id: str, model_id: str) -> str | None:
        """Return the mode *proven* to work for this pair, or None if untried."""
        with self._lock:
            return self._structured_output.get(CapabilityKey(endpoint_id, model_id))

    def record_structured_output(
        self, endpoint_id: str, model_id: str, mode: str
    ) -> None:
        """Remember that ``mode`` produced a usable reply for this pair.

        Runtime proof, so it supersedes anything the catalog claimed for the
        rest of the session. Kept session-local deliberately: provider support
        is dynamic, and an indefinite persisted "none" would go stale silently.
        """
        with self._lock:
            self._structured_output[CapabilityKey(endpoint_id, model_id)] = mode

    def record_reported_parameters(
        self, endpoint_id: str, model_id: str, parameters: Iterable[str] | None
    ) -> None:
        """Seed the parameters an endpoint's catalog reports for this pair.

        Called by the composition root before any dispatch, from the model
        manifest. ``None`` records nothing, preserving "unknown" — the whole
        point of the tri-state is that a service which never published
        capability metadata is not the same as one that published an absence.
        """
        if parameters is None:
            return
        with self._lock:
            self._reported[CapabilityKey(endpoint_id, model_id)] = frozenset(
                parameters
            )

    def reported_parameters(
        self, endpoint_id: str, model_id: str
    ) -> frozenset[str] | None:
        """Return the catalog-reported parameters for this pair, if any."""
        with self._lock:
            return self._reported.get(CapabilityKey(endpoint_id, model_id))

    def should_announce(self, endpoint_id: str, model_id: str) -> bool:
        """Return True the first time this pair's selected rung is reported.

        Claiming the announcement and testing it are one atomic step, so two
        threads discovering the same limitation at once still log once.
        """
        key = CapabilityKey(endpoint_id, model_id)
        with self._lock:
            if key in self._announced:
                return False
            self._announced.add(key)
            return True

    @contextmanager
    def discovering(self, endpoint_id: str, model_id: str) -> Iterator[Discovery]:
        """Coordinate discovery for one pair, yielding this caller's standing.

        The first caller for an unknown key is handed ``owned=True`` and walks
        the ladder. Concurrent callers block until it finishes — outside this
        cache's lock, so nothing else is held up — and are then handed
        ``owned=False`` with whatever it proved.

        On exit the event is signalled and the key removed **whether or not the
        body raised**. An owner that learned nothing therefore releases its
        waiters to try for themselves rather than leaving the key owned
        forever; the alternative is a single provider hiccup disabling
        structured output for the session.

        A caller that already has a proven mode is handed it immediately and
        takes no ownership: there is nothing left to discover.
        """
        key = CapabilityKey(endpoint_id, model_id)
        event: threading.Event | None = None

        with self._lock:
            proven = self._structured_output.get(key)
            reported = self._reported.get(key)
            if proven is not None:
                yielded = Discovery(owned=False, mode=proven, reported=reported)
            else:
                waiting = self._in_flight.get(key)
                if waiting is None:
                    event = threading.Event()
                    self._in_flight[key] = event
                    yielded = Discovery(owned=True, mode=None, reported=reported)
                else:
                    yielded = Discovery(owned=False, reported=reported)

        if yielded.owned:
            assert event is not None
            try:
                yield yielded
            finally:
                with self._lock:
                    self._in_flight.pop(key, None)
                # Signalled after the key is released, so a waiter that wakes
                # and immediately re-enters can take ownership if nothing was
                # learned.
                event.set()
            return

        if yielded.mode is None:
            # Wait outside the lock: an HTTP ladder is in flight and holding the
            # cache's lock across it would serialize every other key too.
            waiting = self._pending(key)
            if waiting is not None:
                waiting.wait(self._wait_seconds)
            with self._lock:
                yielded = Discovery(
                    owned=False,
                    mode=self._structured_output.get(key),
                    reported=self._reported.get(key),
                )
        yield yielded

    def _pending(self, key: CapabilityKey) -> threading.Event | None:
        with self._lock:
            return self._in_flight.get(key)

    def in_flight(self) -> int:
        """Return how many keys are mid-discovery, for tests and diagnostics."""
        with self._lock:
            return len(self._in_flight)

    def snapshot(self) -> dict[CapabilityKey, str]:
        """Return a copy of what has been proven, for status and tests."""
        with self._lock:
            return dict(self._structured_output)
