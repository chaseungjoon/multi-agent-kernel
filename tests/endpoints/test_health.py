"""Tests for health classification and the three health policies."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from mak.agent_runner.adapters.openai_api_adapter import OpenAiApiAdapter
from mak.endpoints.health import (
    NOT_PROBED,
    FailureKind,
    classify_failure,
    status_of,
)


class _StatusError(Exception):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status_code = status


class TestClassification:
    @pytest.mark.parametrize(
        ("status", "kind"),
        [
            (401, FailureKind.CREDENTIALS),
            (403, FailureKind.CREDENTIALS),
            (404, FailureKind.NOT_FOUND),
            (429, FailureKind.RATE_LIMIT),
            (400, FailureKind.INCOMPATIBLE),
            (500, FailureKind.UNREACHABLE),
            (503, FailureKind.UNREACHABLE),
        ],
    )
    def test_status_decides_the_kind(self, status: int, kind: FailureKind) -> None:
        assert classify_failure(_StatusError("boom", status)).kind is kind

    @pytest.mark.parametrize(
        ("message", "kind"),
        [
            ("Connection refused", FailureKind.UNREACHABLE),
            ("Name or service not known", FailureKind.UNREACHABLE),
            ("request timed out", FailureKind.UNREACHABLE),
            ("certificate verify failed", FailureKind.UNREACHABLE),
            ("No module named 'openai'", FailureKind.SDK_MISSING),
            ("model not found", FailureKind.MODEL_MISSING),
        ],
    )
    def test_markers_classify_a_statusless_error(
        self, message: str, kind: FailureKind
    ) -> None:
        assert classify_failure(RuntimeError(message)).kind is kind

    def test_an_unrecognized_error_is_unknown_not_misattributed(self) -> None:
        failure = classify_failure(RuntimeError("something odd happened"))
        assert failure.kind is FailureKind.UNKNOWN
        assert "something odd happened" in failure.message()

    def test_a_status_on_a_response_attribute_is_read(self) -> None:
        exc = RuntimeError("nope")
        exc.response = SimpleNamespace(status_code=404)  # type: ignore[attr-defined]
        assert status_of(exc) == 404
        assert classify_failure(exc).kind is FailureKind.NOT_FOUND

    def test_an_error_with_no_status_reports_none(self) -> None:
        assert status_of(RuntimeError("x")) is None


class TestMessages:
    def test_a_credential_failure_names_the_variable_to_check(self) -> None:
        failure = classify_failure(
            _StatusError("Unauthorized", 401), api_key_env="NVIDIA_API_KEY"
        )
        assert "NVIDIA_API_KEY" in failure.message()

    def test_a_missing_model_points_at_the_listing_command(self) -> None:
        failure = classify_failure(
            RuntimeError("model not found"), endpoint_id="nvidia"
        )
        assert "/endpoint models nvidia" in failure.message()

    def test_an_unreachable_endpoint_names_the_address(self) -> None:
        failure = classify_failure(
            RuntimeError("Connection refused"), base_url="http://localhost:8000/v1"
        )
        assert "http://localhost:8000/v1" in failure.message()

    def test_a_wrong_path_says_the_base_url_is_probably_wrong(self) -> None:
        failure = classify_failure(_StatusError("Not Found", 404))
        assert "API root" in failure.message()

    def test_a_credential_failure_does_not_name_the_address(self) -> None:
        """The key is the thing to look at, and the URL adds noise."""
        failure = classify_failure(
            _StatusError("Unauthorized", 401), base_url="https://gw.example/v1"
        )
        assert "gw.example" not in failure.message()

    def test_each_kind_has_a_remedy(self) -> None:
        for kind in FailureKind:
            failure = classify_failure(_StatusError("x", 401))
            object.__setattr__(failure, "kind", kind)
            assert failure.message().strip()


class TestRedaction:
    def test_a_key_shaped_token_is_removed(self) -> None:
        failure = classify_failure(
            RuntimeError("rejected key sk-live-abcdef123 for this account")
        )
        assert "sk-live-abcdef123" not in failure.message()
        assert "[redacted]" in failure.message()

    def test_a_bearer_header_echo_is_removed(self) -> None:
        failure = classify_failure(RuntimeError("sent Bearer sk-secret-xyz"))
        assert "sk-secret-xyz" not in failure.message()

    def test_a_query_string_is_dropped_from_an_echoed_url(self) -> None:
        failure = classify_failure(
            RuntimeError("POST https://gw.example/v1/chat?token=sekret failed")
        )
        assert "sekret" not in failure.message()

    def test_ordinary_text_survives(self) -> None:
        failure = classify_failure(RuntimeError("Connection refused"))
        assert "Connection refused" in failure.message()


class _Models:
    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail
        self.listed = 0

    def list(self) -> list[Any]:
        self.listed += 1
        if self.fail is not None:
            raise self.fail
        return []


class _Completions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="ok"), finish_reason="stop"
                )
            ],
            usage=None,
        )


class _Client:
    def __init__(self, fail: Exception | None = None) -> None:
        self.models = _Models(fail)
        self.chat = SimpleNamespace(completions=_Completions())

    def with_options(self, **_: Any) -> _Client:
        return self


class TestHealthPolicies:
    def test_models_lists_once(self) -> None:
        client = _Client()
        adapter = OpenAiApiAdapter(
            client=client, base_url="https://h/v1", health_check_policy="models"
        )
        assert adapter.health_check() is True
        assert client.models.listed == 1

    def test_none_makes_no_network_call_and_says_not_probed(self) -> None:
        client = _Client()
        adapter = OpenAiApiAdapter(
            client=client, base_url="https://h/v1", health_check_policy="none"
        )
        assert adapter.health_check() is True
        assert client.models.listed == 0
        assert not client.chat.completions.calls
        assert adapter.health_status() == NOT_PROBED
        assert "healthy" not in adapter.health_status()

    def test_a_service_without_a_listing_stays_usable_under_none(self) -> None:
        """Lacking /models is not a reason to drop an endpoint from the pool."""
        client = _Client(fail=RuntimeError("404 no such route"))
        adapter = OpenAiApiAdapter(
            client=client, base_url="https://h/v1", health_check_policy="none"
        )
        assert adapter.health_check() is True

    def test_chat_without_acceptance_does_not_spend(self) -> None:
        client = _Client()
        adapter = OpenAiApiAdapter(
            client=client,
            base_url="https://h/v1",
            health_check_policy="chat",
            chat_probe_ok=False,
        )
        assert adapter.health_check() is True
        assert not client.chat.completions.calls
        assert adapter.health_status() == NOT_PROBED

    def test_chat_with_acceptance_sends_one_tiny_request(self) -> None:
        client = _Client()
        adapter = OpenAiApiAdapter(
            client=client,
            model="m",
            base_url="https://h/v1",
            health_check_policy="chat",
            chat_probe_ok=True,
        )
        assert adapter.health_check() is True
        (call,) = client.chat.completions.calls
        assert call["model"] == "m"
        assert call["max_tokens"] == 1
        # Not structured: this asks whether the model answers at all, and a
        # server that rejects response_format would fail a probe it should pass.
        assert "response_format" not in call

    def test_a_failed_probe_reports_the_classified_reason(self) -> None:
        adapter = OpenAiApiAdapter(
            client=_Client(fail=_StatusError("Unauthorized", 401)),
            base_url="https://h/v1",
            health_check_policy="models",
            api_key_env="GW_KEY",
        )
        assert adapter.health_check() is False
        detail = adapter.health_detail()
        assert detail is not None and "GW_KEY" in detail

    def test_auto_probes_only_when_there_is_an_address(self) -> None:
        """A registry build must not make a network call for the SDK default."""
        cloud = _Client()
        assert OpenAiApiAdapter(client=cloud).health_check() is True
        assert cloud.models.listed == 0

        compat = _Client()
        assert (
            OpenAiApiAdapter(client=compat, base_url="https://h/v1").health_check()
            is True
        )
        assert compat.models.listed == 1

    def test_health_status_reports_healthy_only_after_a_real_probe(self) -> None:
        adapter = OpenAiApiAdapter(
            client=_Client(), base_url="https://h/v1", health_check_policy="models"
        )
        adapter.health_check()
        assert adapter.health_status() == "healthy"


def test_the_adapter_and_the_endpoint_enum_agree_on_health_names() -> None:
    from mak.agent_runner.adapters import openai_api_adapter as adapter
    from mak.endpoints.types import HealthPolicy

    assert adapter.HEALTH_MODELS == HealthPolicy.MODELS.value
    assert adapter.HEALTH_CHAT == HealthPolicy.CHAT.value
    assert adapter.HEALTH_NONE == HealthPolicy.NONE.value
    # The adapter's own default is deliberately not an endpoint policy.
    assert adapter.HEALTH_AUTO not in {p.value for p in HealthPolicy}


def test_a_builtin_cloud_endpoint_is_not_probed_at_startup() -> None:
    from mak.config import AgentConfig
    from mak.endpoints.builtin import builtin_endpoint_for
    from mak.endpoints.types import HealthPolicy

    cloud = builtin_endpoint_for(AgentConfig(type="openai_api"))
    assert cloud.health_check is HealthPolicy.NONE

    local = builtin_endpoint_for(
        AgentConfig(type="local_api", base_url="http://localhost:8000/v1")
    )
    assert local.health_check is HealthPolicy.MODELS
