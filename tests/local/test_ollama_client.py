"""Tests for the stdlib Ollama client. No test here opens a socket."""

from __future__ import annotations

import io
import json
import urllib.error
from collections.abc import Callable
from typing import Any

import pytest

from mak.local.ollama_client import OllamaClient, OllamaError

_BASE = "http://localhost:11434"


class FakeResponse(io.BytesIO):
    """A urlopen result: readable, iterable by line, and a context manager."""

    def __init__(self, body: bytes) -> None:
        super().__init__(body)
        self.closed_count = 0

    def __enter__(self) -> FakeResponse:
        """Enter the context, as urlopen's result does."""
        return self

    def __exit__(self, *_: object) -> None:
        """Close on exit, as urlopen's result does."""
        self.close()

    def close(self) -> None:
        self.closed_count += 1
        super().close()


def _install(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[Any, Any], Any]
) -> list[tuple[str, str, dict[str, Any] | None]]:
    """Route ``urlopen`` to ``handler``; return the log of requests it saw."""
    seen: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake_urlopen(request: Any, timeout: float | None = None) -> Any:
        body = None if request.data is None else json.loads(request.data)
        seen.append((request.full_url, request.get_method(), body))
        return handler(request, timeout)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return seen


def _json_handler(payload: object) -> Callable[[Any, Any], Any]:
    def handler(_request: Any, _timeout: Any) -> FakeResponse:
        return FakeResponse(json.dumps(payload).encode())

    return handler


def _raising_handler(exc: BaseException) -> Callable[[Any, Any], Any]:
    def handler(_request: Any, _timeout: Any) -> FakeResponse:
        raise exc

    return handler


class TestEndpoints:
    def test_version(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = _install(monkeypatch, _json_handler({"version": "0.5.7"}))
        assert OllamaClient(_BASE).version() == "0.5.7"
        assert seen[0][0] == f"{_BASE}/api/version"
        assert seen[0][1] == "GET"

    def test_trailing_slash_in_base_url_is_stripped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = _install(monkeypatch, _json_handler({"version": "1"}))
        OllamaClient(f"{_BASE}/").version()
        assert seen[0][0] == f"{_BASE}/api/version"

    def test_list_models(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(
            monkeypatch,
            _json_handler(
                {
                    "models": [
                        {
                            "name": "qwen2.5-coder:14b",
                            "size": 9_000_000_000,
                            "details": {
                                "parameter_size": "14.8B",
                                "quantization_level": "Q4_K_M",
                            },
                        },
                        {"not": "a model"},
                    ]
                }
            ),
        )
        (model,) = OllamaClient(_BASE).list_models()
        assert model.name == "qwen2.5-coder:14b"
        assert model.size_bytes == 9_000_000_000
        assert model.parameter_size == "14.8B"
        assert model.quantization == "Q4_K_M"

    def test_running(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = _install(
            monkeypatch, _json_handler({"models": [{"name": "llama3.1:8b"}]})
        )
        assert OllamaClient(_BASE).running() == ["llama3.1:8b"]
        assert seen[0][0] == f"{_BASE}/api/ps"

    def test_chat_posts_a_non_streaming_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = _install(
            monkeypatch,
            _json_handler(
                {
                    "message": {"content": '{"task_id": "t"}'},
                    "done_reason": "stop",
                    "prompt_eval_count": 512,
                    "eval_count": 64,
                }
            ),
        )
        reply = OllamaClient(_BASE).chat(
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            format="json",
            options={"num_ctx": 8192},
            keep_alive="30m",
        )
        assert reply.content == '{"task_id": "t"}'
        assert reply.done_reason == "stop"
        assert reply.prompt_eval_count == 512
        assert reply.eval_count == 64
        url, method, body = seen[0]
        assert url == f"{_BASE}/api/chat"
        assert method == "POST"
        assert body is not None
        assert body["stream"] is False
        assert body["format"] == "json"
        assert body["options"] == {"num_ctx": 8192}
        assert body["keep_alive"] == "30m"

    def test_chat_omits_unset_options(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = _install(monkeypatch, _json_handler({"message": {"content": "{}"}}))
        OllamaClient(_BASE).chat(model="m", messages=[])
        _url, _method, body = seen[0]
        assert body is not None
        assert "format" not in body
        assert "options" not in body
        assert "keep_alive" not in body


class TestShowContextLength:
    @pytest.mark.parametrize(
        "arch_key", ["llama.context_length", "qwen2.context_length"]
    )
    def test_context_length_is_found_by_key_suffix(
        self, monkeypatch: pytest.MonkeyPatch, arch_key: str
    ) -> None:
        # The key is architecture-prefixed and the architecture is not knowable
        # up front, so it must be matched by suffix, never by a fixed name.
        _install(
            monkeypatch,
            _json_handler(
                {
                    "details": {"parameter_size": "14.8B"},
                    "model_info": {"general.architecture": "x", arch_key: 32768},
                }
            ),
        )
        model = OllamaClient(_BASE).show("qwen2.5-coder:14b")
        assert model.context_length == 32768
        assert model.name == "qwen2.5-coder:14b"

    def test_missing_context_length_is_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, _json_handler({"model_info": {"general.arch": "x"}}))
        assert OllamaClient(_BASE).show("m").context_length is None


class TestFailuresBecomeOllamaError:
    def test_connection_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(
            monkeypatch,
            _raising_handler(urllib.error.URLError("Connection refused")),
        )
        with pytest.raises(OllamaError, match=r"cannot reach .*/api/version"):
            OllamaClient(_BASE).version()

    def test_http_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(
            monkeypatch,
            _raising_handler(
                urllib.error.HTTPError(
                    f"{_BASE}/api/show", 404, "Not Found", {}, None  # type: ignore[arg-type]
                )
            ),
        )
        with pytest.raises(OllamaError, match="HTTP 404"):
            OllamaClient(_BASE).show("nope")

    def test_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, _raising_handler(TimeoutError("timed out")))
        with pytest.raises(OllamaError, match=r"cannot reach .*/api/tags"):
            OllamaClient(_BASE).list_models()

    def test_malformed_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(_request: Any, _timeout: Any) -> FakeResponse:
            return FakeResponse(b"<html>not json</html>")

        _install(monkeypatch, handler)
        with pytest.raises(OllamaError, match="not JSON"):
            OllamaClient(_BASE).version()

    def test_non_object_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, _json_handler([1, 2, 3]))
        with pytest.raises(OllamaError, match="expected a JSON object"):
            OllamaClient(_BASE).version()


def _ndjson(*lines: dict[str, Any]) -> bytes:
    return b"".join(json.dumps(line).encode() + b"\n" for line in lines)


class TestPull:
    def test_yields_one_record_per_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = _ndjson(
            {"status": "pulling manifest"},
            {"status": "pulling", "digest": "sha:a", "total": 100, "completed": 40},
            {"status": "success"},
        )
        _install(monkeypatch, lambda *_: FakeResponse(body))
        records = list(OllamaClient(_BASE).pull("qwen2.5-coder:7b"))
        assert [r.status for r in records] == [
            "pulling manifest",
            "pulling",
            "success",
        ]
        assert records[1].fraction() == pytest.approx(0.4)
        assert records[0].fraction() is None

    def test_a_truncated_stream_ends_cleanly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An interrupted download ends mid-line; that is the normal shape of a
        # Ctrl-C, not a failure worth raising over.
        body = _ndjson({"status": "pulling"}) + b'{"status": "pul'
        _install(monkeypatch, lambda *_: FakeResponse(body))
        records = list(OllamaClient(_BASE).pull("m"))
        assert [r.status for r in records] == ["pulling"]

    def test_a_reported_error_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = _ndjson({"error": "model 'nope' not found"})
        _install(monkeypatch, lambda *_: FakeResponse(body))
        with pytest.raises(OllamaError, match="not found"):
            list(OllamaClient(_BASE).pull("nope"))

    def test_the_response_is_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        response = FakeResponse(_ndjson({"status": "success"}))
        _install(monkeypatch, lambda *_: response)
        list(OllamaClient(_BASE).pull("m"))
        assert response.closed_count >= 1
