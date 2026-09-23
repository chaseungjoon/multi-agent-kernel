"""A dependency-free HTTP client for Ollama's native API.

Deliberately **not** the ``ollama`` SDK, for three reasons in order of weight:

1. A fully-local MAK install then needs no third-party provider SDK at all —
   which is the strongest possible form of this wave's promise, and the reason
   the ``[local]`` packaging extra is empty rather than a dependency list.
2. The surface MAK needs is five endpoints of plain JSON. ``urllib.request`` and
   ``json`` cover it in one small module.
3. The SDK pins ``httpx`` versions that would have to be reconciled against
   ``openai`` and ``anthropic``, for no gain.

The client holds no socket state — one ``Request`` per call — so it is trivially
safe to share across the session's worker pool, and it is injectable everywhere
it is used, so no test opens a socket.

Every transport, HTTP, timeout, and JSON failure becomes an :class:`OllamaError`
naming the endpoint and the underlying reason. Nothing else escapes: a local
runtime that is simply not running is the most common failure there is, and it
must read as one clear line rather than a ``URLError`` traceback out of a worker
thread.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from mak.core.exceptions import MakError

# Ollama's own default listen address. Named here because it is a fact about the
# runtime, not a MAK policy — the composition root re-exports it as the default
# ``base_url`` for the ``ollama_api`` agent type.
DEFAULT_BASE_URL = "http://localhost:11434"

_DEFAULT_TIMEOUT_S = 60.0

# ``/api/show`` reports the context window under ``model_info`` keyed by
# architecture — ``llama.context_length``, ``qwen2.context_length``,
# ``gemma3.context_length``. The architecture is not knowable up front, so the
# key is matched by suffix rather than by name.
_CONTEXT_LENGTH_SUFFIX = ".context_length"


class OllamaError(MakError):
    """Raised when a call to an Ollama server fails, for any reason."""


@dataclass(frozen=True, slots=True)
class OllamaModel:
    """One model as the server reports it.

    ``context_length`` is ``None`` until :meth:`OllamaClient.show` is asked for
    it — ``/api/tags`` does not carry it, and the context window is exactly the
    number the adapter needs before it can size a request safely.
    """

    name: str
    size_bytes: int | None = None
    parameter_size: str | None = None
    quantization: str | None = None
    context_length: int | None = None


@dataclass(frozen=True, slots=True)
class OllamaChatResponse:
    """One ``/api/chat`` reply, reduced to what an adapter reads.

    ``done_reason`` is Ollama's stop signal (``"stop"``, ``"length"``, …) and
    goes through the same :func:`~mak.agent_runner.stop_signals.check_stop_reason`
    as every other backend's, so a cut local reply is rejected by one rule.
    """

    content: str
    done_reason: str | None = None
    prompt_eval_count: int | None = None
    eval_count: int | None = None


@dataclass(frozen=True, slots=True)
class PullProgress:
    """One line of a streaming model download."""

    status: str
    digest: str | None = None
    total: int | None = None
    completed: int | None = None

    def fraction(self) -> float | None:
        """Return the fraction downloaded, or None when the line has no sizes."""
        if not self.total or self.completed is None:
            return None
        return min(1.0, self.completed / self.total)


def _opt_int(raw: object) -> int | None:
    """Return ``raw`` as an int when it plainly is one, else None."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw


def _opt_str(raw: object) -> str | None:
    """Return ``raw`` as a string when it is one and non-empty, else None."""
    return raw if isinstance(raw, str) and raw else None


def _context_length(model_info: object) -> int | None:
    """Find the context window in ``/api/show``'s architecture-keyed model_info."""
    if not isinstance(model_info, dict):
        return None
    for key, value in model_info.items():
        if isinstance(key, str) and key.endswith(_CONTEXT_LENGTH_SUFFIX):
            length = _opt_int(value)
            if length is not None:
                return length
    return None


def _model_from_tags(entry: object) -> OllamaModel | None:
    """Build an :class:`OllamaModel` from one ``/api/tags`` entry."""
    if not isinstance(entry, dict):
        return None
    name = _opt_str(entry.get("name")) or _opt_str(entry.get("model"))
    if name is None:
        return None
    details = entry.get("details")
    details = details if isinstance(details, dict) else {}
    return OllamaModel(
        name=name,
        size_bytes=_opt_int(entry.get("size")),
        parameter_size=_opt_str(details.get("parameter_size")),
        quantization=_opt_str(details.get("quantization_level")),
    )


class OllamaClient:
    """Five endpoints of Ollama's REST API, over the standard library."""

    def __init__(self, base_url: str, *, timeout: float = _DEFAULT_TIMEOUT_S) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # -- transport ---------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _open(
        self, path: str, *, body: dict[str, Any] | None, timeout: float | None
    ) -> Any:
        """Open one request, translating every transport failure to OllamaError."""
        url = self._url(path)
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - scheme is validated config
            url,
            data=data,
            method="GET" if body is None else "POST",
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            return urllib.request.urlopen(  # noqa: S310 - same
                request, timeout=self.timeout if timeout is None else timeout
            )
        except urllib.error.HTTPError as exc:
            raise OllamaError(f"{url} returned HTTP {exc.code} ({exc.reason})") from exc
        except urllib.error.URLError as exc:
            raise OllamaError(f"cannot reach {url}: {exc.reason}") from exc
        except OSError as exc:  # timeouts and socket errors
            raise OllamaError(f"cannot reach {url}: {exc}") from exc

    def _request(
        self,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Make one request and return its decoded JSON object."""
        url = self._url(path)
        with self._open(path, body=body, timeout=timeout) as response:
            raw = response.read()
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise OllamaError(f"{url} returned a body that is not JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise OllamaError(
                f"{url} returned {type(parsed).__name__}, expected a JSON object"
            )
        return parsed

    # -- endpoints ---------------------------------------------------------

    def version(self) -> str:
        """Return the server's version — the cheapest proof it is reachable."""
        return _opt_str(self._request("/api/version").get("version")) or ""

    def list_models(self, *, timeout: float | None = None) -> list[OllamaModel]:
        """Return every model installed on the server (``GET /api/tags``).

        ``timeout`` overrides the client's default for this one call, so a UI
        path can ask an unreachable host and get an answer in seconds.
        """
        raw = self._request("/api/tags", timeout=timeout).get("models")
        entries = raw if isinstance(raw, list) else []
        models = [_model_from_tags(entry) for entry in entries]
        return [model for model in models if model is not None]

    def show(self, model: str) -> OllamaModel:
        """Return one model's details, context window included (``POST /api/show``)."""
        data = self._request("/api/show", body={"model": model})
        details = data.get("details")
        details = details if isinstance(details, dict) else {}
        return OllamaModel(
            name=model,
            size_bytes=_opt_int(data.get("size")),
            parameter_size=_opt_str(details.get("parameter_size")),
            quantization=_opt_str(details.get("quantization_level")),
            context_length=_context_length(data.get("model_info")),
        )

    def running(self) -> list[str]:
        """Return the names of the models loaded right now (``GET /api/ps``)."""
        raw = self._request("/api/ps").get("models")
        entries = raw if isinstance(raw, list) else []
        names = [
            _opt_str(entry.get("name")) or _opt_str(entry.get("model"))
            for entry in entries
            if isinstance(entry, dict)
        ]
        return [name for name in names if name is not None]

    def chat(
        self,
        *,
        model: str,
        messages: Sequence[dict[str, Any]],
        format: dict[str, Any] | str | None = None,  # noqa: A002 - Ollama's own name
        options: dict[str, Any] | None = None,
        keep_alive: str | None = None,
        timeout: float | None = None,
    ) -> OllamaChatResponse:
        """Complete one chat turn (``POST /api/chat``), without streaming.

        ``stream: false`` on purpose. Streaming exists to keep a long-lived
        cloud connection from being dropped by an idle proxy; this endpoint is
        on localhost, where there is no proxy and no idle timeout to defeat, so
        one blocking POST under the per-agent timeout is simpler and loses
        nothing. (The Anthropic adapter streams for the opposite reason, which
        is documented there — the two choices should not look arbitrary.)

        ``format`` is Ollama's constrained-decoding lever: a JSON schema, the
        literal ``"json"``, or None for unconstrained text.
        """
        body: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "stream": False,
        }
        if format is not None:
            body["format"] = format
        if options:
            body["options"] = options
        if keep_alive is not None:
            body["keep_alive"] = keep_alive
        data = self._request("/api/chat", body=body, timeout=timeout)
        message = data.get("message")
        message = message if isinstance(message, dict) else {}
        return OllamaChatResponse(
            content=_opt_str(message.get("content")) or "",
            done_reason=_opt_str(data.get("done_reason")),
            prompt_eval_count=_opt_int(data.get("prompt_eval_count")),
            eval_count=_opt_int(data.get("eval_count")),
        )

    def pull(self, model: str) -> Iterator[PullProgress]:
        """Download a model, yielding one progress record per NDJSON line.

        Interruptible: a ``KeyboardInterrupt`` in the caller closes the response
        and leaves Ollama's partial blob alone, so the next pull resumes rather
        than restarting. Never buffers the whole stream — a model is gigabytes.
        """
        url = self._url("/api/pull")
        response = self._open("/api/pull", body={"model": model}, timeout=None)
        try:
            for raw_line in response:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    # A truncated final line is the normal end of an interrupted
                    # stream, not a failure worth raising over.
                    break
                if not isinstance(parsed, dict):
                    continue
                error = _opt_str(parsed.get("error"))
                if error is not None:
                    raise OllamaError(f"{url} failed to pull {model!r}: {error}")
                yield PullProgress(
                    status=_opt_str(parsed.get("status")) or "",
                    digest=_opt_str(parsed.get("digest")),
                    total=_opt_int(parsed.get("total")),
                    completed=_opt_int(parsed.get("completed")),
                )
        finally:
            response.close()
