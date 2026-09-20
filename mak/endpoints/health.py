"""Classify why an endpoint probe failed, and say so honestly.

Two problems this fixes.

**The generic message.** A failed preflight used to report "missing API key/SDK,
or CLI not on PATH" for every cause, which sends a user to fix the wrong thing
roughly as often as the right one. A wrong base URL, an expired key, a quota
breach and an unreachable host need four different actions, and the provider
already told us which it was — MAK was discarding that.

**The dishonest pass.** An endpoint whose health policy makes no network call
used to be reported as "healthy". It is not: nothing has been verified. The
honest answer is *not probed*, and this module keeps the distinction available
so the UI never claims more than MAK checked.

Classification reads HTTP status first, because that is the provider's own
structured answer, and falls back to narrow message markers only where the SDK
gives no status. It never guesses from a substring alone in a way that would
change what MAK *does* — the worst outcome here is a less specific message.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FailureKind(StrEnum):
    """What went wrong when MAK tried to reach an endpoint."""

    UNREACHABLE = "unreachable"
    CREDENTIALS = "credentials"
    NOT_FOUND = "not_found"
    RATE_LIMIT = "rate_limit"
    MODEL_MISSING = "model_missing"
    INCOMPATIBLE = "incompatible"
    SDK_MISSING = "sdk_missing"
    UNKNOWN = "unknown"


# What the user should actually do about each kind. Written as the next action,
# not as a restatement of the error — "check the key" is useful, "401
# Unauthorized" is not.
_REMEDY: dict[FailureKind, str] = {
    FailureKind.UNREACHABLE: (
        "nothing answered at that address — check the base URL, and that the "
        "server is running and reachable from here"
    ),
    FailureKind.CREDENTIALS: (
        "the endpoint rejected the credential — check the key in {env}, that it "
        "is still valid, and that it belongs to this service"
    ),
    FailureKind.NOT_FOUND: (
        "the endpoint answered, but that path does not exist — the base URL is "
        "probably wrong (it should be the API root, not a /chat/completions URL)"
    ),
    FailureKind.RATE_LIMIT: (
        "the endpoint is rate-limiting or out of quota — wait, or check the "
        "balance and plan on your account"
    ),
    FailureKind.MODEL_MISSING: (
        "the endpoint does not offer that model — list what it has with "
        "'/endpoint models {endpoint}'"
    ),
    FailureKind.INCOMPATIBLE: (
        "the endpoint answered with something MAK could not read — it may not "
        "be OpenAI-compatible at this URL"
    ),
    FailureKind.SDK_MISSING: "an SDK MAK needs is not installed",
    FailureKind.UNKNOWN: "MAK could not tell why; the endpoint's own words are above",
}

# Status codes whose meaning is unambiguous across providers.
_BY_STATUS: dict[int, FailureKind] = {
    401: FailureKind.CREDENTIALS,
    403: FailureKind.CREDENTIALS,
    404: FailureKind.NOT_FOUND,
    429: FailureKind.RATE_LIMIT,
}

# Fallback markers, used only when no status is available. Kept narrow and
# specific: a wrong guess here costs a worse message, never a wrong action.
_BY_MARKER: tuple[tuple[str, FailureKind], ...] = (
    ("no module named", FailureKind.SDK_MISSING),
    ("sdk not installed", FailureKind.SDK_MISSING),
    ("connection refused", FailureKind.UNREACHABLE),
    ("connection error", FailureKind.UNREACHABLE),
    ("name or service not known", FailureKind.UNREACHABLE),
    ("nodename nor servname", FailureKind.UNREACHABLE),
    ("failed to resolve", FailureKind.UNREACHABLE),
    ("timed out", FailureKind.UNREACHABLE),
    ("certificate verify failed", FailureKind.UNREACHABLE),
    ("ssl", FailureKind.UNREACHABLE),
    ("api key", FailureKind.CREDENTIALS),
    ("unauthorized", FailureKind.CREDENTIALS),
    ("model not found", FailureKind.MODEL_MISSING),
    ("does not exist", FailureKind.MODEL_MISSING),
)


# Kinds where naming the address is the most useful thing MAK can say: the
# user's next move is to look at the URL itself.
_ADDRESS_KINDS = frozenset(
    {FailureKind.UNREACHABLE, FailureKind.NOT_FOUND, FailureKind.INCOMPATIBLE}
)


@dataclass(frozen=True, slots=True)
class HealthFailure:
    """One classified probe failure, ready to show without further shaping."""

    kind: FailureKind
    detail: str
    endpoint_id: str = ""
    api_key_env: str | None = None
    base_url: str | None = None

    def message(self) -> str:
        """Return the user-facing line: where, what happened, then what to do."""
        remedy = _REMEDY[self.kind].format(
            env=self.api_key_env or "the configured variable",
            endpoint=self.endpoint_id or "<id>",
        )
        where = ""
        if self.base_url and self.kind in _ADDRESS_KINDS:
            # Sanitized: a gateway URL can carry a token in its query string.
            where = f"{self.base_url.split('?', 1)[0]}: "
        return f"{where}{self.detail} — {remedy}"


def status_of(exc: BaseException) -> int | None:
    """Return the HTTP status carried by an SDK exception, if it has one."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def classify_failure(
    exc: BaseException,
    *,
    endpoint_id: str = "",
    api_key_env: str | None = None,
    base_url: str | None = None,
) -> HealthFailure:
    """Classify a probe failure into an actionable :class:`HealthFailure`.

    ``endpoint_id``, ``api_key_env`` and ``base_url`` only shape the message;
    they are never used to decide the kind.
    """
    detail = _redact(str(exc)) or exc.__class__.__name__
    status = status_of(exc)
    if status is not None:
        kind = _BY_STATUS.get(status)
        if kind is None:
            kind = (
                FailureKind.INCOMPATIBLE if status < 500 else FailureKind.UNREACHABLE
            )
        return HealthFailure(kind, detail, endpoint_id, api_key_env, base_url)

    lowered = detail.lower()
    for marker, kind in _BY_MARKER:
        if marker in lowered:
            return HealthFailure(kind, detail, endpoint_id, api_key_env, base_url)
    return HealthFailure(
        FailureKind.UNKNOWN, detail, endpoint_id, api_key_env, base_url
    )


# Anything that looks like a bearer token or an API key in an error string.
# Provider errors do sometimes echo the request, and this text reaches log files
# and terminal scrollback that users paste into issue reports.
_SECRET_PREFIXES = ("sk-", "bearer ", "api-key ", "token ")


def _redact(text: str) -> str:
    """Return ``text`` with anything key-shaped replaced by a marker.

    Conservative by design: it is better to redact a harmless string than to
    print a live credential into a log the user is about to share.
    """
    out: list[str] = []
    for word in text.split(" "):
        lowered = word.lower()
        if any(lowered.startswith(p.strip()) for p in _SECRET_PREFIXES if p.strip()):
            out.append("[redacted]")
            continue
        out.append(word)
    joined = " ".join(out)
    # A query string can carry a token; the path alone is enough to diagnose.
    return joined.split("?", 1)[0] if "?" in joined and "://" in joined else joined


# What ``health_check`` reports when the policy is ``none``. Not "healthy" — MAK
# verified the configuration and nothing else, and saying otherwise would be a
# claim it has not earned.
NOT_PROBED = "not probed (health_check: none) — configuration looks valid"
