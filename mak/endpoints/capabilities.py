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
Anthropic's own.

Thread-safe by construction: agents run concurrently in scheduler threads, so
every read and write takes the lock. The critical sections are dictionary
operations, so the contention cost is nil next to an HTTP round trip.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

# The descent order. A rejection at one rung moves to the next; reaching the end
# means the endpoint gets a bare prompt-only request, which is all a server with
# no structured-output support can be asked for.
STRUCTURED_OUTPUT_LADDER: tuple[str, ...] = ("json_schema", "json_object", "none")


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


@dataclass(frozen=True, slots=True)
class CapabilityKey:
    """What a cached capability belongs to: one model at one endpoint."""

    endpoint_id: str
    model_id: str


class CapabilityCache:
    """Remembers the structured-output mode that worked, per endpoint and model.

    Created once per session by the composition root and passed explicitly to
    every adapter factory. Never a module-level singleton.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._structured_output: dict[CapabilityKey, str] = {}
        # Which (endpoint, model) pairs have already logged a downgrade, so a
        # forty-task run reports the discovery once rather than forty times.
        self._announced: set[CapabilityKey] = set()

    def structured_output(self, endpoint_id: str, model_id: str) -> str | None:
        """Return the mode known to work for this pair, or None if untried."""
        with self._lock:
            return self._structured_output.get(CapabilityKey(endpoint_id, model_id))

    def record_structured_output(
        self, endpoint_id: str, model_id: str, mode: str
    ) -> None:
        """Remember that ``mode`` produced a usable reply for this pair."""
        with self._lock:
            self._structured_output[CapabilityKey(endpoint_id, model_id)] = mode

    def should_announce(self, endpoint_id: str, model_id: str) -> bool:
        """Return True the first time a downgrade for this pair is reported.

        Claiming the announcement and testing it are one atomic step, so two
        threads discovering the same limitation at once still log once.
        """
        key = CapabilityKey(endpoint_id, model_id)
        with self._lock:
            if key in self._announced:
                return False
            self._announced.add(key)
            return True

    def snapshot(self) -> dict[CapabilityKey, str]:
        """Return a copy of what has been learned, for status and tests."""
        with self._lock:
            return dict(self._structured_output)
