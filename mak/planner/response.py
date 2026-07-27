"""Turn a raw LLM response into JSON, and tell malformed apart from truncated.

The planner asks for "ONLY a JSON array", but a real model reply may arrive
wrapped in a code fence, prefixed with a sentence of prose, or — the failure this
module exists for — **cut off part-way through** because the response hit the
provider's output-token ceiling.

Truncation and malformation need different recoveries, so they must not share an
error type. A malformed reply is a one-off the model can usually fix if asked
again; a truncated reply is *deterministic* — re-asking the same question yields
the same over-long plan and the same cut, which is why a truncated plan burns
every retry and fails the run. Only a prompt that asks for a **smaller** plan (or
a larger output budget) recovers it, so ``TruncatedResponseError`` is raised
separately for ``Planner`` to react to.

Detection is exact rather than heuristic: a payload is truncated when appending
the delimiters it left open — after discarding a trailing incomplete element —
yields JSON that parses. Anything else is genuinely malformed.

A repaired payload is deliberately **never returned as a plan**. It is a partial
plan: the sub-tasks past the cut are missing, and running one would edit half the
codebase and call it done. Repair here answers "was this truncated, and how much
did the model get through" — a diagnostic, not a result.
"""

from __future__ import annotations

import json
import re

# A fenced block anywhere in the reply, not just at position 0 — models often
# open with "Here's the plan:" before the fence. The closing fence is optional so
# a response truncated inside the fence is still extracted (and then reported as
# truncated rather than as missing JSON).
_FENCE = re.compile(r"```[A-Za-z0-9_+-]*[ \t]*\r?\n(?P<body>.*?)(?:```|\Z)", re.DOTALL)

_OPEN_TO_CLOSE = {"[": "]", "{": "}"}


class ResponseError(ValueError):
    """Base for LLM-response problems the planner can retry on.

    Subclasses ``ValueError`` because the planner's retry loop treats a rejected
    response as a value error; existing ``except ValueError`` callers keep working.
    """


class EmptyResponseError(ResponseError):
    """Raised when the model returned no text at all."""


class TruncatedResponseError(ResponseError):
    """Raised when the response was cut off before its JSON was complete.

    ``complete_elements`` is how many whole items the model emitted before the
    cut, which tells the user whether the plan was slightly or wildly too big.
    """

    def __init__(self, message: str, *, complete_elements: int = 0) -> None:
        super().__init__(message)
        self.complete_elements = complete_elements


def extract_json_payload(raw: str) -> str:
    """Return the JSON substring of ``raw``, dropping fences and framing prose.

    Raises ``EmptyResponseError`` when nothing that could start a JSON value is
    present. The returned text may still be invalid JSON — this only locates it.
    """
    text = raw.strip()
    if not text:
        raise EmptyResponseError(
            "model returned an empty response (no text content)"
        )

    fenced = _FENCE.search(text)
    if fenced is not None:
        body = fenced.group("body").strip()
        if body:
            text = body

    start = min(
        (i for i in (text.find("["), text.find("{")) if i != -1),
        default=-1,
    )
    if start == -1:
        raise ResponseError(
            "response contained no JSON value (expected an array or object)"
        )
    return text[start:]


def loads_json(raw: str) -> object:
    """Parse ``raw`` into a JSON value, tolerating fences and surrounding prose.

    Raises ``TruncatedResponseError`` when the response was cut short,
    ``EmptyResponseError`` when it was blank, and plain ``ResponseError`` when it
    is malformed for any other reason.
    """
    payload = extract_json_payload(raw)
    try:
        # raw_decode stops at the end of the first value, so trailing commentary
        # ("...that's the plan!") does not fail an otherwise-good response.
        value, _end = json.JSONDecoder().raw_decode(payload)
    except json.JSONDecodeError as exc:
        repaired = repair_truncated(payload)
        if repaired is not None:
            _recovered, count = repaired
            raise TruncatedResponseError(
                "response was not valid JSON: it was truncated after "
                f"{len(payload)} characters and {count} complete "
                f"{'item' if count == 1 else 'items'}, so the model hit its "
                f"output-token limit before finishing ({exc})",
                complete_elements=count,
            ) from exc
        raise ResponseError(f"response was not valid JSON: {exc}") from exc
    return value


def repair_truncated(payload: str) -> tuple[str, int] | None:
    """Close a truncated ``payload`` into valid JSON, or None if not truncated.

    Returns ``(repaired_json, complete_element_count)``. Used to classify a parse
    failure and report how far the model got — never to accept a partial plan,
    which would silently drop sub-tasks (see the module docstring).
    """
    stack, in_string, escaped = _scan(payload)
    if not stack and not in_string:
        return None

    points = _cut_points(payload)
    candidates: list[tuple[int, str]] = []
    if in_string:
        # The cut landed inside a string literal. Closing it recovers the whole
        # value when that string was a value; when it was a key, this candidate
        # fails to parse and the cut points below drop the half-written pair.
        head = payload[:-1] if escaped else payload
        candidates.append((len(payload), head + '"' + _closers(stack)))
    candidates.extend(
        (cut, payload[:cut] + _closers(open_stack))
        for cut, open_stack in reversed(points)
    )

    for cut, candidate in candidates:
        try:
            json.JSONDecoder().raw_decode(candidate)
        except json.JSONDecodeError:
            continue
        # A cut point left with only the root container open sits just past a
        # complete top-level element, so counting those is the element count.
        complete = sum(1 for at, s in points if len(s) == 1 and at <= cut)
        return candidate, complete
    return None


def _closers(stack: list[str]) -> str:
    """Return the delimiters that close ``stack``, innermost first."""
    return "".join(_OPEN_TO_CLOSE[opener] for opener in reversed(stack))


def _scan(payload: str) -> tuple[list[str], bool, bool]:
    """Return the open-delimiter stack, in-string and trailing-escape state at EOF."""
    stack: list[str] = []
    in_string = False
    escaped = False
    for char in payload:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in _OPEN_TO_CLOSE:
            stack.append(char)
        elif char in ("]", "}") and stack:
            stack.pop()
    return stack, in_string, escaped


def _cut_points(payload: str) -> list[tuple[int, list[str]]]:
    """Return ``(end_index, open_stack)`` for every position a value can end at.

    Only positions still nested inside a container qualify: closing at depth 0
    would mean the value was already complete, which is not a truncation.
    """
    points: list[tuple[int, list[str]]] = []
    stack: list[str] = []
    in_string = False
    escaped = False
    for index, char in enumerate(payload):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
                if stack:
                    points.append((index + 1, list(stack)))
            continue
        if char == '"':
            in_string = True
        elif char in _OPEN_TO_CLOSE:
            stack.append(char)
        elif char in ("]", "}"):
            if stack:
                stack.pop()
            if stack:
                points.append((index + 1, list(stack)))
    return points
