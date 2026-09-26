"""Decide *why* a structured-output request was refused, from the error itself.

Searching ``str(exc)`` for a few literal substrings is wrong in both
directions, and one real incident proved it:
OpenRouter routing to Novita refused the same model twice with two spellings of
one sentence —

.. code-block:: text

    model features structured outputs not support
    model: inclusionai/ling-3.0-flash-vl does not support feature: structured-outputs

The 0.8.1 hotfix added the literal ``"structured outputs"``, which matches the
first and misses the second, so the ladder never descended and every agent task
failed. Extending a tuple of spellings cannot be the design: the sentence is
written by whichever upstream provider OpenRouter picked, and MAK does not get
to enumerate them.

So classification works on **structure, then normalized language**:

1. the HTTP status, which is the provider's own machine-readable answer;
2. the SDK's parsed error body (``exc.body``), then ``response.json()``;
3. OpenRouter's outer ``error.message`` / ``error.code`` / ``error.metadata``;
4. ``error.metadata.raw`` — the upstream payload, JSON-decoded when it is a
   JSON string, which is where the useful sentence actually lives;
5. ``str(exc)`` last, purely for compatibility with SDKs and fakes that expose
   nothing else.

Text is normalized (NFKC, casefolded, every run of non-alphanumerics collapsed
to one space) so ``structured outputs``, ``structured-outputs`` and
``structured_outputs`` become one token sequence and one marker matches all
three. That is the property the literal-substring approach lacked.

**Two rejections, two recoveries.** A provider saying "I cannot do that reply
shape" means *descend a rung*. OpenRouter answering 404 "no endpoints found
that can handle the requested parameters" means MAK's own
``provider.require_parameters`` routing guard filtered every route away — the
recovery there is to *drop the guard and retry the same rung*, because the
model may well support the rung even though the routing filter disagreed. They
are different outcomes and the caller must be able to tell them apart.

**Everything else propagates.** A capability claim requires a status in
400/422 *and* a marker naming the reply format *and* language describing an
unsupported capability. An invalid JSON Schema authored by MAK names the format
but claims no missing capability, so it propagates — silently dropping schema
enforcement would hide a MAK defect. Authentication, missing model, quota,
context limit, safety, timeout and 5xx all propagate untouched.

**Bodies are read but never logged.** A provider error body can echo request
headers and user content. ``reason`` carries a short, redacted phrase for logs;
the raw body never leaves this module.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from mak.endpoints.health import redact_secrets, status_of

# A format rejection is a client error about the request body. A 5xx is the
# server failing and a 429 is quota; neither is answered by asking for a looser
# reply shape.
FORMAT_REJECTION_STATUSES: frozenset[int] = frozenset({400, 422})

# OpenRouter answers a routing-filter miss with 404: there is no eligible
# upstream, which is not a client error about the body.
ROUTING_REJECTION_STATUSES: frozenset[int] = frozenset({404})

# How deep into a nested error body to look. OpenRouter nests two levels
# (``error.metadata.raw``, itself JSON); the bound stops a hostile or looping
# structure from costing unbounded work.
_MAX_DEPTH = 6

# Total characters of extracted text to classify. Provider bodies can echo the
# whole request, and the useful sentence is always near the front.
_MAX_TEXT = 8000

_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def normalize(text: str) -> str:
    """Return ``text`` casefolded with every non-alphanumeric run as one space.

    This is what makes ``structured-outputs``, ``structured_outputs`` and
    ``structured outputs`` a single token sequence, so one marker matches all
    three spellings instead of the caller enumerating them. NFKC first, because
    a provider echoing user content can return full-width or composed forms.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    return _NON_ALNUM.sub(" ", folded).strip()


# Markers naming the *reply format* specifically, already normalized.
# Deliberately narrow: an earlier version matched bare "unsupported", which
# fires on an unsupported model, an unsupported parameter and an unsupported
# region — and every one of those was being answered by silently retrying with
# a weaker output contract instead of surfacing the provider's real error.
#
# "structured output" is a prefix, so it covers both "structured output" and
# "structured outputs" without a second entry.
_FORMAT_MARKERS: tuple[str, ...] = (
    "response format",
    "json schema",
    "json mode",
    "json object",
    "structured output",
)

# Language asserting a *capability* is absent, as opposed to a request being
# malformed. Both observed Novita spellings satisfy this: "does not support"
# contains "not support", and "structured outputs not support" ends with it.
_CAPABILITY_MARKERS: tuple[str, ...] = (
    "not support",
    "unsupported",
    "not available",
    "unavailable",
    "no support",
    "not implemented",
    "not enabled",
    "not allowed",
    "cannot be used",
    "does not accept",
    "not accepted",
    "not capable",
)

# Language asserting the *schema MAK sent* is wrong. This vetoes a capability
# claim even when the capability markers would otherwise match, because a
# malformed schema is a MAK defect and must surface rather than be answered by
# a quieter rung.
_INVALID_SCHEMA_MARKERS: tuple[str, ...] = (
    "invalid schema",
    "schema is invalid",
    "invalid json schema",
    "is required to be supplied",
    "unknown keyword",
    "additionalproperties",
    "failed to parse schema",
    "schema must",
)

# OpenRouter's own wording for "your provider filter excluded everything", plus
# the machine-readable step name it reports alongside it.
_ROUTING_MARKERS: tuple[str, ...] = (
    "no endpoints found that can handle the requested parameters",
    "no allowed providers are available",
)
_ROUTING_STEP_MARKERS: tuple[str, ...] = ("parameter",)


class RejectionKind(StrEnum):
    """What a failed structured-output request actually was.

    ``NOT_A_REJECTION`` is the common case and the safe default: the error is
    the provider's real answer and belongs to the caller unchanged.
    """

    NOT_A_REJECTION = "not_a_rejection"
    # The provider cannot produce this reply shape. Recovery: descend a rung.
    FORMAT_UNSUPPORTED = "format_unsupported"
    # MAK's own routing guard left no eligible provider. Recovery: drop the
    # guard and retry the *same* rung — the model may support it regardless.
    ROUTING_PARAMETERS = "routing_parameters"


@dataclass(frozen=True, slots=True)
class RejectionAnalysis:
    """The typed verdict on one SDK exception.

    ``reason`` is a short redacted phrase safe to log; it is never the raw
    provider body. ``provider_name`` is OpenRouter's upstream attribution when
    it reported one, which is the single most useful field for diagnosing "why
    does this model work for me and not for you".
    """

    kind: RejectionKind
    status: int | None = None
    reason: str = ""
    provider_name: str = ""

    @property
    def is_format_rejection(self) -> bool:
        """Whether the provider refused this reply shape (descend a rung)."""
        return self.kind is RejectionKind.FORMAT_UNSUPPORTED

    @property
    def is_routing_rejection(self) -> bool:
        """Whether MAK's routing guard excluded every provider (drop it)."""
        return self.kind is RejectionKind.ROUTING_PARAMETERS


def _error_body(exc: BaseException) -> object:
    """Return the SDK's parsed error body, preferring what it already decoded.

    ``exc.body`` is what the openai SDK attaches and is already a ``dict``, so
    it is tried first. ``response.json()`` is the fallback for an SDK that
    exposes only the raw response, and any failure there is ignored: a body
    that will not parse simply contributes no structured evidence.
    """
    body = getattr(exc, "body", None)
    if body is not None:
        return body
    response = getattr(exc, "response", None)
    if response is None:
        return None
    reader = getattr(response, "json", None)
    if not callable(reader):
        return None
    try:
        return reader()
    except Exception:  # noqa: BLE001 - an unparseable body is simply no evidence
        return None


def _decoded(value: str) -> object:
    """Return ``value`` parsed as JSON when it is JSON, else the string itself.

    OpenRouter delivers the upstream provider's payload as
    ``error.metadata.raw``, and it is *usually* a JSON-encoded string rather
    than a nested object. Both forms occur — a plain sentence is what the quota
    error carries — so this returns whichever it got and lets the walk below
    handle it uniformly.
    """
    text = value.strip()
    if not text or text[0] not in "{[":
        return value
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return value


def _collect(node: object, out: list[str], depth: int = 0) -> None:
    """Walk an error body, appending every string it carries, bounded.

    A bounded walk rather than a fixed field list on purpose: the priority
    order in the module docstring says *where the useful sentence usually is*,
    but OpenRouter has reshaped this envelope before and a provider is free to
    nest its message one level deeper. Collecting every string and classifying
    the whole is robust to that, and the marker gate is what keeps it from
    over-matching.
    """
    if depth > _MAX_DEPTH:
        return
    if isinstance(node, str):
        decoded = _decoded(node)
        if isinstance(decoded, str):
            out.append(decoded)
        else:
            _collect(decoded, out, depth + 1)
        return
    if isinstance(node, dict):
        for value in node.values():
            _collect(value, out, depth + 1)
        return
    if isinstance(node, (list, tuple)):
        for value in node:
            _collect(value, out, depth + 1)


def _error_section(body: object) -> dict[str, object]:
    """Return the ``error`` mapping, accepting a body that *is* the error.

    The openai SDK hands back ``{"error": {...}}`` for cloud OpenAI, while
    OpenRouter's ``exc.body`` is the error object itself (``message``, ``code``,
    ``metadata`` at the top level). Both were observed live, so both are read.
    """
    if not isinstance(body, dict):
        return {}
    section = body.get("error")
    if isinstance(section, dict):
        return section
    return body


def _metadata(body: object) -> dict[str, object]:
    """Return ``error.metadata`` when present, else an empty mapping."""
    metadata = _error_section(body).get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _provider_name(body: object) -> str:
    """Return OpenRouter's upstream provider attribution, or an empty string."""
    name = _metadata(body).get("provider_name")
    return str(name) if isinstance(name, (str, int)) else ""


def _routing_step(body: object) -> str:
    """Return the routing step OpenRouter says failed, normalized."""
    step = _metadata(body).get("failed_routing_step")
    return normalize(str(step)) if isinstance(step, str) else ""


def _text_of(exc: BaseException, body: object) -> str:
    """Return the normalized text to classify, from the body then the string.

    ``str(exc)`` is appended rather than preferred: for the openai SDK it is a
    rendering of the same body, but for a bare test double or a different SDK
    it is the only evidence there is.
    """
    fragments: list[str] = []
    if body is not None:
        _collect(body, fragments)
    fragments.append(str(exc))
    joined = " ".join(f for f in fragments if f)[:_MAX_TEXT]
    return normalize(joined)


def _reason_of(body: object, exc: BaseException) -> str:
    """Return a short, redacted phrase naming the refusal, safe for a log.

    Prefers the upstream provider's own sentence, because that is the one that
    tells a user which model/provider pair refused. Never the whole body: a
    provider error can echo request headers and user content.
    """
    metadata = _metadata(body)
    raw = metadata.get("raw")
    if isinstance(raw, str):
        decoded = _decoded(raw)
        if isinstance(decoded, dict):
            message = decoded.get("message")
            if isinstance(message, str) and message.strip():
                return redact_secrets(message.strip())[:200]
        elif isinstance(decoded, str) and decoded.strip():
            return redact_secrets(decoded.strip())[:200]
    message = _error_section(body).get("message")
    if isinstance(message, str) and message.strip():
        return redact_secrets(message.strip())[:200]
    return redact_secrets(str(exc))[:200]


def _has(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def classify_rejection(exc: BaseException) -> RejectionAnalysis:
    """Return the typed verdict on one SDK exception.

    The default is :attr:`RejectionKind.NOT_A_REJECTION`. Nothing is inferred
    from a message alone: an exception carrying no HTTP status is never a
    rejection, because a transport error has no opinion about the request body.
    """
    status = status_of(exc)
    body = _error_body(exc)

    if status is None:
        # A timeout, a DNS failure, a dropped connection. It says nothing about
        # whether the model supports the reply format MAK asked for.
        return RejectionAnalysis(RejectionKind.NOT_A_REJECTION)

    text = _text_of(exc, body)
    provider = _provider_name(body)

    if status in ROUTING_REJECTION_STATUSES and (
        _has(text, _ROUTING_MARKERS)
        or _has(_routing_step(body), _ROUTING_STEP_MARKERS)
    ):
        return RejectionAnalysis(
            RejectionKind.ROUTING_PARAMETERS,
            status=status,
            reason=_reason_of(body, exc),
            provider_name=provider,
        )

    if status not in FORMAT_REJECTION_STATUSES:
        return RejectionAnalysis(RejectionKind.NOT_A_REJECTION, status=status)

    if _has(text, _INVALID_SCHEMA_MARKERS):
        # MAK authored a schema the provider will not accept. That is MAK's bug
        # and must surface; answering it with a weaker rung would hide it.
        return RejectionAnalysis(RejectionKind.NOT_A_REJECTION, status=status)

    if _has(text, _FORMAT_MARKERS) and _has(text, _CAPABILITY_MARKERS):
        return RejectionAnalysis(
            RejectionKind.FORMAT_UNSUPPORTED,
            status=status,
            reason=_reason_of(body, exc),
            provider_name=provider,
        )

    return RejectionAnalysis(RejectionKind.NOT_A_REJECTION, status=status)
