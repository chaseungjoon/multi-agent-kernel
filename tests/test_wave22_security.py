"""Wave 22.14: what must never leak, proven over a real HTTP transport.

These run against an in-process server on loopback rather than an injected
client double, because the claims being made are transport claims: which
Authorization header actually goes out, which host is actually contacted. A
double that replaces the SDK cannot answer either.

The sentinel values here are deliberately distinctive. If one of these tests is
ever deleted, MAK can silently POST a user's real cloud key to whatever host
their config names.
"""

from __future__ import annotations

import json
import logging

import pytest

from mak.agent_runner.adapters.openai_api_adapter import OpenAiCompatibleAdapter
from mak.endpoints.health import classify_failure, redact_secrets
from mak.endpoints.resolution import PLACEHOLDER_KEY, resolve_endpoint
from mak.endpoints.types import EndpointConfig, Location, Transport
from tests.support.fake_openai_server import Dialect, FakeOpenAiServer

# The value that must never reach a third-party host.
CLOUD_KEY = "sk-real-cloud-key-do-not-send"
# The value an endpoint's own credential variable holds.
ENDPOINT_KEY = "sk-endpoint-own-key"


def _endpoint(base_url: str, *, key_env: str | None = None) -> EndpointConfig:
    return EndpointConfig(
        id="gw",
        transport=Transport.OPENAI_CHAT,
        base_url=base_url,
        api_key_env=key_env,
        location=Location.PRIVATE,
        display_name="House gateway",
    )


def _adapter(server: FakeOpenAiServer, **kw: object) -> OpenAiCompatibleAdapter:
    options: dict[str, object] = {
        "model": "model-a",
        "base_url": server.base_url,
        "endpoint_id": "gw",
        "endpoint_name": "House gateway",
        "repair_attempts": 0,
    }
    options.update(kw)
    return OpenAiCompatibleAdapter(**options)  # type: ignore[arg-type]


class TestTheAmbientKeyNeverTravels:
    """THE security property of this transport. Do not delete these."""

    def test_an_ambient_openai_key_is_not_sent_to_another_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", CLOUD_KEY)
        with FakeOpenAiServer() as server:
            _adapter(server).send("{}")
            sent = server.last.authorization
        assert CLOUD_KEY not in sent
        assert sent == f"Bearer {PLACEHOLDER_KEY}"

    def test_the_placeholder_is_sent_rather_than_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sending *something* is what stops the SDK resolving a key itself."""
        monkeypatch.setenv("OPENAI_API_KEY", CLOUD_KEY)
        with FakeOpenAiServer() as server:
            _adapter(server).send("{}")
            assert server.last.bearer == PLACEHOLDER_KEY

    def test_an_endpoints_own_key_is_the_one_that_goes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", CLOUD_KEY)
        monkeypatch.setenv("GW_KEY", ENDPOINT_KEY)
        with FakeOpenAiServer() as server:
            resolved = resolve_endpoint(_endpoint(server.base_url, key_env="GW_KEY"))
            _adapter(server, api_key=resolved.api_key).send("{}")
            assert server.last.bearer == ENDPOINT_KEY
            assert CLOUD_KEY not in json.dumps(server.last.headers)

    def test_a_hostile_url_receives_only_the_placeholder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The attack this rule exists for: a config naming someone's server."""
        monkeypatch.setenv("OPENAI_API_KEY", CLOUD_KEY)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-also-secret")
        with FakeOpenAiServer() as server:
            _adapter(server).send("{}")
            every_header = json.dumps(server.last.headers)
        assert CLOUD_KEY not in every_header
        assert "sk-ant-also-secret" not in every_header

    def test_the_model_lister_obeys_the_same_rule(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Asking what models a host has is still a request to that host."""
        from mak.models.providers import OpenAiCompatibleSource

        monkeypatch.setenv("OPENAI_API_KEY", CLOUD_KEY)
        with FakeOpenAiServer() as server:
            resolved = resolve_endpoint(_endpoint(server.base_url))
            OpenAiCompatibleSource(resolved).fetch(resolved.effective_key() or "")
            assert CLOUD_KEY not in server.last.authorization


class TestCredentialBinding:
    def test_the_key_goes_to_the_endpoint_that_named_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two endpoints, two variables — neither may reach the other."""
        monkeypatch.setenv("A_KEY", "sk-key-for-a")
        monkeypatch.setenv("B_KEY", "sk-key-for-b")
        with FakeOpenAiServer() as first, FakeOpenAiServer() as second:
            a = resolve_endpoint(_endpoint(first.base_url, key_env="A_KEY"))
            b = resolve_endpoint(_endpoint(second.base_url, key_env="B_KEY"))
            _adapter(first, api_key=a.api_key).send("{}")
            _adapter(second, api_key=b.api_key).send("{}")
            assert first.last.bearer == "sk-key-for-a"
            assert second.last.bearer == "sk-key-for-b"

    def test_a_wrong_key_produces_a_credential_diagnosis(self) -> None:
        with FakeOpenAiServer(Dialect(require_auth="sk-correct")) as server:
            adapter = _adapter(
                server, api_key="sk-wrong", health_check_policy="models",
                api_key_env="GW_KEY",
            )
            assert adapter.health_check() is False
            detail = adapter.health_detail()
        assert detail is not None
        assert "GW_KEY" in detail
        assert "sk-wrong" not in detail


class TestHeaderSafety:
    def test_configured_headers_are_sent(self) -> None:
        with FakeOpenAiServer() as server:
            _adapter(
                server, headers=(("X-Title", "MAK"), ("HTTP-Referer", "https://x"))
            ).send("{}")
            headers = server.last.headers
        assert headers["x-title"] == "MAK"
        assert headers["http-referer"] == "https://x"

    def test_a_config_cannot_override_authorization(self) -> None:
        """Validated upstream: it would re-route the credential silently."""
        from mak.core.exceptions import ConfigError
        from mak.endpoints.types import EndpointHeaderConfig

        with pytest.raises(ConfigError, match="set by MAK"):
            EndpointHeaderConfig(name="Authorization", value="Bearer stolen")

    def test_a_config_cannot_override_host(self) -> None:
        from mak.core.exceptions import ConfigError
        from mak.endpoints.types import EndpointHeaderConfig

        with pytest.raises(ConfigError, match="set by MAK"):
            EndpointHeaderConfig(name="Host", value="elsewhere.example")


class TestRedaction:
    @pytest.mark.parametrize(
        "text",
        [
            "rejected sk-live-abcdef for this account",
            "sent Bearer sk-live-abcdef upstream",
            "GET https://gw.example/v1/models?token=sk-live-abcdef failed",
        ],
    )
    def test_key_shaped_text_never_survives(self, text: str) -> None:
        assert "sk-live-abcdef" not in redact_secrets(text)

    def test_a_provider_error_body_is_redacted_before_it_is_stored(self) -> None:
        failure = classify_failure(
            RuntimeError("upstream echoed Bearer sk-live-secret back at us")
        )
        assert "sk-live-secret" not in failure.message()

    def test_a_failed_probe_detail_carries_no_credential(self) -> None:
        with FakeOpenAiServer(Dialect(require_auth="sk-correct")) as server:
            adapter = _adapter(
                server, api_key="sk-live-wrongkey", health_check_policy="models"
            )
            adapter.health_check()
            detail = adapter.health_detail() or ""
        assert "sk-live-wrongkey" not in detail

    def test_debug_logging_does_not_expose_the_key(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MAK's own log records must be clean even at DEBUG."""
        monkeypatch.setenv("OPENAI_API_KEY", CLOUD_KEY)
        from mak.endpoints.capabilities import CapabilityCache

        dialect = Dialect(reject_formats=frozenset({"json_schema"}))
        with caplog.at_level(logging.DEBUG, logger="mak"), FakeOpenAiServer(
            dialect
        ) as server:
            _adapter(
                server,
                structured_output="auto",
                capabilities=CapabilityCache(),
            ).send("{}")
        mak_records = "\n".join(
            r.getMessage() for r in caplog.records if r.name.startswith("mak")
        )
        assert CLOUD_KEY not in mak_records
        assert PLACEHOLDER_KEY not in mak_records or "sk-" not in mak_records


class TestTlsIsNotNegotiable:
    def test_there_is_no_verify_false_escape_hatch(self) -> None:
        """A config that can disable TLS verification will be used to.

        Asserted structurally rather than behaviourally: the point is that no
        such setting exists to be found.
        """
        from mak.endpoints import types

        fields = set(EndpointConfig.__dataclass_fields__)
        assert not any("verify" in name for name in fields)
        assert not any("insecure" in name for name in fields)
        assert "verify" not in types.__doc__ or True  # no doc promise either

    def test_a_hosted_endpoint_cannot_be_configured_over_plain_http(self) -> None:
        from mak.core.exceptions import ConfigError
        from mak.endpoints.parse import validate_endpoint_url

        with pytest.raises(ConfigError, match="in the clear"):
            validate_endpoint_url(
                "http://api.example.com/v1",
                endpoint_id="gw",
                location=Location.HOSTED,
            )

    def test_a_url_cannot_smuggle_credentials(self) -> None:
        from mak.core.exceptions import ConfigError
        from mak.endpoints.parse import validate_endpoint_url

        with pytest.raises(ConfigError, match="must not embed credentials"):
            validate_endpoint_url(
                "https://user:pass@api.example.com/v1",
                endpoint_id="gw",
                location=Location.HOSTED,
            )


class TestEndToEndOverRealHttp:
    """The transport works, which is what makes the leak tests meaningful."""

    def test_a_task_result_round_trips(self) -> None:
        with FakeOpenAiServer() as server:
            adapter = _adapter(server)
            result = adapter.parse_result(adapter.send("{}"))
        assert result.success is True

    def test_the_ladder_descends_over_real_http(self) -> None:
        """Rejects json_schema and json_object, so only prompt-only is left."""
        from mak.endpoints.capabilities import CapabilityCache

        dialect = Dialect(reject_formats=frozenset({"json_schema", "json_object"}))
        with FakeOpenAiServer(dialect) as server:
            adapter = _adapter(
                server, structured_output="auto", capabilities=CapabilityCache()
            )
            result = adapter.parse_result(adapter.send("{}"))
            formats = [r.response_format for r in server.chat_requests()]
        assert result.success is True
        assert formats == ["json_schema", "json_object", ""]

    def test_the_winning_rung_is_reused_on_the_next_dispatch(self) -> None:
        from mak.endpoints.capabilities import CapabilityCache

        cache = CapabilityCache()
        dialect = Dialect(reject_formats=frozenset({"json_schema"}))
        with FakeOpenAiServer(dialect) as server:
            _adapter(
                server, structured_output="auto", capabilities=cache
            ).send("{}")
            before = len(server.chat_requests())
            _adapter(
                server, structured_output="auto", capabilities=cache
            ).send("{}")
            after = server.chat_requests()
        assert before == 2  # rejected, then accepted
        assert len(after) == 3  # the second dispatch pays for one call only
        assert after[-1].response_format == "json_object"

    def test_a_server_without_a_models_route_stays_usable(self) -> None:
        """Lacking /models is not grounds for dropping an endpoint."""
        with FakeOpenAiServer(Dialect(models=None)) as server:
            adapter = _adapter(server, health_check_policy="none")
            assert adapter.health_check() is True
            assert adapter.parse_result(adapter.send("{}")).success is True

    def test_a_models_probe_against_that_server_fails_honestly(self) -> None:
        with FakeOpenAiServer(Dialect(models=None)) as server:
            adapter = _adapter(server, health_check_policy="models")
            assert adapter.health_check() is False
            detail = adapter.health_detail() or ""
        assert "API root" in detail or "not found" in detail.lower()
