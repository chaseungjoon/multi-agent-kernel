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


@dataclass
class Dialect:
    """How this fake server behaves, so one class covers every real shape.

    ``reject_formats`` names the ``response_format`` types this server refuses
    with a 400 — the ladder's whole reason for existing. ``models`` of ``None``
    means the ``/models`` route does not exist, which is a real configuration
    and must not disqualify an endpoint from being used.
    """

    models: list[str] | None = field(default_factory=lambda: ["model-a", "model-b"])
    reject_formats: frozenset[str] = frozenset()
    require_auth: str | None = None
    chat_status: int = 200
    result: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_RESULT))


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


class FakeOpenAiServer:
    """A loopback HTTP server speaking enough of the OpenAI protocol to test.

    Use as a context manager; ``base_url`` is what an endpoint should point at.
    """

    def __init__(self, dialect: Dialect | None = None) -> None:
        self.dialect = dialect or Dialect()
        self.requests: list[Request] = []
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
                        "data": [
                            {"id": m, "object": "model"}
                            for m in owner.dialect.models
                        ],
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
                if requested in owner.dialect.reject_formats:
                    owner._send(
                        self,
                        400,
                        {
                            "error": {
                                "message": (
                                    f"response_format '{requested}' is not "
                                    "supported by this model"
                                )
                            }
                        },
                    )
                    return
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
                                    "content": json.dumps(owner.dialect.result),
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
