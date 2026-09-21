"""An in-process OpenAI-compatible HTTP server for end-to-end tests.

Why a real socket rather than an injected client double: the things Wave 22
most needs to prove are *transport* facts — which Authorization header actually
goes out, which URL is actually contacted, whether an ambient key can leak —
and a double that replaces the SDK cannot answer any of them. This serves real
HTTP on an ephemeral loopback port, so the openai SDK does its real work.

Built on ``http.server`` from the standard library: no new dependency, and
nothing here ever leaves the machine.

The dialects exist because "OpenAI-compatible" is a family resemblance. A
server that rejects ``json_schema``, one that rejects every ``response_format``,
one with no ``/models`` route at all, and one that 401s are all real services
someone will point MAK at.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

# The reply body a happy server returns: a minimal, valid ``TaskResult``.
DEFAULT_RESULT: dict[str, Any] = {
    "task_id": "t1",
    "success": True,
    "modified_fragments": [],
    "no_changes_required": True,
    "error": None,
}


# The exact upstream sentences OpenRouter relayed from Novita during the Wave 24
# incident, one per rung. Two spellings of one refusal — a space and a hyphen —
# which is precisely what defeated the literal-substring matching in 0.8.1.
NOVITA_REFUSALS: dict[str, str] = {
    "json_schema": "model features structured outputs not support",
    "json_object": (
        "model: inclusionai/ling-3.0-flash-vl does not support feature: "
        "structured-outputs"
    ),
}


@dataclass
class Dialect:
    """How this fake server behaves, so one class covers every real shape.

    ``reject_formats`` names the ``response_format`` types this server refuses
    with a 400 — the ladder's whole reason for existing. ``models`` of ``None``
    means the ``/models`` route does not exist, which is a real configuration
    and must not disqualify an endpoint from being used.

    The OpenRouter fields (Wave 24) reproduce facts measured against the live
    service, so a test can exercise them over real HTTP and the real SDK rather
    than against a hand-written double of the SDK:

    ``supported_parameters``
        Published per model id on the ``/models`` route, exactly as OpenRouter
        does. ``None`` omits the field, which is what every other compatible
        service does and must keep meaning *unknown*.
    ``openrouter_errors``
        Wrap refusals in OpenRouter's real envelope — an outer "Provider
        returned error" with the upstream sentence nested inside
        ``error.metadata.raw`` as a JSON **string**. That nesting is the reason
        reading the outer message was never enough.
    ``require_parameters_404``
        Answer a request carrying ``provider.require_parameters`` with the real
        404 routing body when the model does not publish the parameter the
        requested rung needs. Measured live: the guard fails by *routing*, with
        a status outside the format window.
    ``delay_seconds``
        Hold the completion open, so a concurrency test can prove that three
        waiting agents really did wait on one discovery rather than racing.
    """

    models: list[str] | None = field(default_factory=lambda: ["model-a", "model-b"])
    reject_formats: frozenset[str] = frozenset()
    require_auth: str | None = None
    chat_status: int = 200
    result: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_RESULT))
    supported_parameters: dict[str, list[str]] | None = None
    openrouter_errors: bool = False
    require_parameters_404: bool = False
    delay_seconds: float = 0.0
    # Literal message contents to return, one per successful completion, in
    # order; the last is reused once exhausted. This is how a test produces a
    # reply that is *not* valid JSON — the failure mode prompt-only mode has to
    # survive, since no server-side grammar is constraining the output there.
    raw_contents: list[str] | None = None

    def rung_parameter(self, response_format: str) -> str | None:
        """Return the reported parameter a ``response_format`` type needs.

        Mirrors ``mak.endpoints.capabilities.RUNG_PARAMETER``. Kept as a literal
        here rather than imported, so the fake describes the *service's*
        behaviour and a bug in MAK's table cannot make the fake agree with it.
        """
        return {
            "json_schema": "structured_outputs",
            "json_object": "response_format",
        }.get(response_format)


@dataclass
class Request:
    """One recorded request, so a test can assert what actually went out."""

    path: str
    headers: dict[str, str]
    body: dict[str, Any]

    @property
    def authorization(self) -> str:
        """Return the Authorization header exactly as it was sent."""
        return self.headers.get("authorization", "")

    @property
    def bearer(self) -> str:
        """Return the bearer token that was sent, or an empty string."""
        value = self.authorization
        return value[7:] if value.lower().startswith("bearer ") else ""

    @property
    def response_format(self) -> str:
        """Return the requested response_format type, or an empty string."""
        fmt = self.body.get("response_format")
        return str(fmt.get("type", "")) if isinstance(fmt, dict) else ""

    @property
    def provider_object(self) -> dict[str, Any]:
        """Return the OpenRouter ``provider`` object sent, or an empty mapping.

        The SDK folds ``extra_body`` into the top level of the JSON body, so
        this is where a routing extension actually lands on the wire — which is
        the only place worth asserting it, since "never sent to another
        endpoint" is a claim about the request, not about MAK's intent.
        """
        provider = self.body.get("provider")
        return provider if isinstance(provider, dict) else {}

    @property
    def require_parameters(self) -> bool:
        """Whether this request asked for parameter-compatible routing."""
        return self.provider_object.get("require_parameters") is True


class FakeOpenAiServer:
    """A loopback HTTP server speaking enough of the OpenAI protocol to test.

    Use as a context manager; ``base_url`` is what an endpoint should point at.
    """

    def __init__(self, dialect: Dialect | None = None) -> None:
        self.dialect = dialect or Dialect()
        self.requests: list[Request] = []
        self._content_index = 0
        self._lock = threading.Lock()
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> FakeOpenAiServer:
        """Start serving on an ephemeral loopback port."""
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                """Silence the default stderr access log."""

            def do_GET(self) -> None:  # noqa: N802 - http.server's naming
                owner._record(self, {})
                if not self.path.endswith("/models"):
                    owner._send(self, 404, {"error": {"message": "not found"}})
                    return
                if owner.dialect.models is None:
                    owner._send(
                        self, 404, {"error": {"message": "no such route"}}
                    )
                    return
                if not owner._authorized(self):
                    return
                owner._send(
                    self,
                    200,
                    {
                        "object": "list",
                        "data": [owner._model_row(m) for m in owner.dialect.models],
                    },
                )

            def do_POST(self) -> None:  # noqa: N802 - http.server's naming
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw)
                except json.JSONDecodeError:
                    body = {}
                owner._record(self, body)
                if not owner._authorized(self):
                    return
                fmt = body.get("response_format") or {}
                requested = fmt.get("type") if isinstance(fmt, dict) else None
                model = str(body.get("model") or "")
                provider = body.get("provider")
                guarded = (
                    isinstance(provider, dict)
                    and provider.get("require_parameters") is True
                )
                # Routing runs *before* the provider sees anything, so a guard
                # that excludes every route answers 404 and the upstream model
                # is never consulted. Measured live against OpenRouter.
                if (
                    guarded
                    and owner.dialect.require_parameters_404
                    and not owner._publishes_rung(model, requested)
                ):
                    owner._send(self, 404, owner._routing_error())
                    return
                if requested in owner.dialect.reject_formats:
                    owner._send(self, 400, owner._format_error(requested))
                    return
                if owner.dialect.delay_seconds:
                    time.sleep(owner.dialect.delay_seconds)
                if owner.dialect.chat_status != 200:
                    owner._send(
                        self,
                        owner.dialect.chat_status,
                        {"error": {"message": "upstream said no"}},
                    )
                    return
                owner._send(
                    self,
                    200,
                    {
                        "id": "chatcmpl-fake",
                        "object": "chat.completion",
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {
                                    "role": "assistant",
                                    "content": owner._next_content(),
                                },
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": 5,
                            "total_tokens": 15,
                        },
                    },
                )

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="fake-openai", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        """Stop serving and join the thread."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        """Return the SDK base URL for this server."""
        assert self._server is not None, "server is not running"
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    @property
    def last(self) -> Request:
        """Return the most recent request, for assertions."""
        with self._lock:
            return self.requests[-1]

    def chat_requests(self) -> list[Request]:
        """Return only the completion requests, in order."""
        with self._lock:
            return [r for r in self.requests if r.path.endswith("/chat/completions")]

    def _next_content(self) -> str:
        """Return the content for this completion, advancing any script.

        Without a script this is the dialect's happy ``TaskResult``. With one,
        each entry is returned in turn and the final entry repeats — so a test
        can say "malformed, then valid" or "always malformed" without counting
        the adapter's internal repair turns.
        """
        with self._lock:
            script = self.dialect.raw_contents
            if not script:
                return json.dumps(self.dialect.result)
            index = min(self._content_index, len(script) - 1)
            self._content_index += 1
            return script[index]

    def _model_row(self, model_id: str) -> dict[str, Any]:
        """Return one ``/models`` row, publishing capabilities when configured.

        The field is **omitted** rather than sent empty when this dialect has
        nothing to say about a model, because an absent field and an empty list
        are different claims and MAK is required to treat them differently.
        """
        row: dict[str, Any] = {"id": model_id, "object": "model"}
        published = self.dialect.supported_parameters
        if published is not None and model_id in published:
            row["supported_parameters"] = list(published[model_id])
        return row

    def _publishes_rung(self, model_id: str, response_format: str | None) -> bool:
        """Whether this model publishes the parameter the rung needs.

        A model with nothing published satisfies no routing filter, which is
        what makes the guard's 404 reachable in a test.
        """
        if response_format is None:
            return True
        needed = self.dialect.rung_parameter(response_format)
        if needed is None:
            return True
        published = (self.dialect.supported_parameters or {}).get(model_id)
        return bool(published) and needed in published

    def _routing_error(self) -> dict[str, Any]:
        """Return OpenRouter's real "no eligible provider" body, verbatim.

        Including ``failed_routing_step``, which is the machine-readable field
        MAK classifies on — the prose is OpenRouter's to reword.
        """
        return {
            "error": {
                "message": (
                    "No endpoints found that can handle the requested "
                    "parameters. To learn more about provider routing, visit: "
                    "https://openrouter.ai/docs/guides/routing/provider-selection"
                ),
                "code": 404,
                "metadata": {
                    "routing_funnel": [
                        {"step": "Initial Endpoints", "endpoint_count": 1}
                    ],
                    "failed_routing_step": "Filter by Parameters",
                },
            }
        }

    def _format_error(self, requested: str | None) -> dict[str, Any]:
        """Return a refusal of ``requested``, in this dialect's envelope.

        An ordinary compatible server says so plainly. OpenRouter wraps the
        upstream provider's sentence two levels down, inside a JSON *string* —
        and that sentence differs per rung, which is the shape that broke the
        literal matching this fake now guards against.
        """
        if not self.dialect.openrouter_errors:
            return {
                "error": {
                    "message": (
                        f"response_format '{requested}' is not supported by "
                        "this model"
                    )
                }
            }
        upstream = NOVITA_REFUSALS.get(
            str(requested), "model does not support that reply format"
        )
        return {
            "error": {
                "message": "Provider returned error",
                "code": 400,
                "metadata": {
                    "raw": json.dumps(
                        {
                            "code": 400,
                            "reason": "INVALID_REQUEST_BODY",
                            "message": upstream,
                            "metadata": {},
                        }
                    ),
                    "provider_name": "Novita",
                    "is_byok": False,
                },
            }
        }

    def _record(self, handler: BaseHTTPRequestHandler, body: dict[str, Any]) -> None:
        with self._lock:
            self.requests.append(
                Request(
                    path=handler.path,
                    headers={k.lower(): v for k, v in handler.headers.items()},
                    body=body,
                )
            )

    def _authorized(self, handler: BaseHTTPRequestHandler) -> bool:
        """Enforce the dialect's expected credential, answering 401 if wrong."""
        expected = self.dialect.require_auth
        if expected is None:
            return True
        sent = handler.headers.get("Authorization", "")
        if sent == f"Bearer {expected}":
            return True
        self._send(
            handler, 401, {"error": {"message": "invalid api key"}}
        )
        return False

    def _send(
        self, handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
