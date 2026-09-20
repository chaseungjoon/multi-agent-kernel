"""Tests for endpoint resolution: precedence, credentials, header safety."""

from __future__ import annotations

import pytest

from mak.core.exceptions import ConfigError
from mak.endpoints.profiles import profile_for
from mak.endpoints.resolution import (
    PLACEHOLDER_KEY,
    require_endpoint,
    resolve_endpoint,
    resolve_endpoints,
)
from mak.endpoints.types import (
    EndpointConfig,
    EndpointHeaderConfig,
    HealthPolicy,
    Location,
    ModelDiscovery,
    StructuredOutput,
    TokenParameter,
    Transport,
)


def _endpoint(**kw: object) -> EndpointConfig:
    base: dict[str, object] = {
        "id": "gw",
        "transport": Transport.OPENAI_CHAT,
        "base_url": "https://gw.example/v1",
    }
    base.update(kw)
    return EndpointConfig(**base)  # type: ignore[arg-type]


class TestPrecedence:
    def test_an_unset_field_falls_through_to_the_transport_default(self) -> None:
        resolved = resolve_endpoint(_endpoint(), env={})
        assert resolved.structured_output is StructuredOutput.AUTO
        assert resolved.token_parameter is TokenParameter.AUTO

    def test_a_profile_default_beats_the_transport_default(self) -> None:
        """DeepSeek documents json_object; the transport default is auto."""
        profile = profile_for("deepseek")
        assert profile is not None
        resolved = resolve_endpoint(profile.to_endpoint(), env={})
        assert resolved.structured_output is StructuredOutput.JSON_OBJECT

    def test_an_explicit_field_beats_the_profile_default(self) -> None:
        profile = profile_for("deepseek")
        assert profile is not None
        endpoint = profile.to_endpoint()
        overridden = EndpointConfig(
            id=endpoint.id,
            transport=endpoint.transport,
            base_url=endpoint.base_url,
            api_key_env=endpoint.api_key_env,
            profile=endpoint.profile,
            structured_output=StructuredOutput.JSON_SCHEMA,
        )
        resolved = resolve_endpoint(overridden, env={})
        assert resolved.structured_output is StructuredOutput.JSON_SCHEMA

    def test_an_explicit_none_stops_the_walk(self) -> None:
        """The tri-state rule: 'none' is a decision, not an absence.

        Without it, a profile that defaults to json_object could never be
        turned off for one endpoint — the None would fall through and pick the
        profile's value straight back up.
        """
        profile = profile_for("deepseek")
        assert profile is not None
        endpoint = EndpointConfig(
            id="ds",
            transport=Transport.OPENAI_CHAT,
            base_url="https://api.deepseek.com",
            profile="deepseek",
            structured_output=StructuredOutput.NONE,
            health_check=HealthPolicy.NONE,
        )
        resolved = resolve_endpoint(endpoint, env={})
        assert resolved.structured_output is StructuredOutput.NONE
        assert resolved.health_check is HealthPolicy.NONE

    def test_each_transport_has_its_own_defaults(self) -> None:
        ollama = resolve_endpoint(
            _endpoint(transport=Transport.OLLAMA_NATIVE), env={}
        )
        assert ollama.structured_output is StructuredOutput.JSON_SCHEMA
        anthropic = resolve_endpoint(
            _endpoint(transport=Transport.ANTHROPIC), env={}
        )
        assert anthropic.token_parameter is TokenParameter.MAX_TOKENS


class TestCredentialResolution:
    def test_the_key_is_read_from_the_named_variable(self) -> None:
        resolved = resolve_endpoint(
            _endpoint(api_key_env="GW_KEY"), env={"GW_KEY": "sk-gw"}
        )
        assert resolved.api_key == "sk-gw"
        assert resolved.has_key
        assert resolved.effective_key() == "sk-gw"

    def test_an_unset_variable_resolves_to_the_placeholder(self) -> None:
        resolved = resolve_endpoint(_endpoint(api_key_env="GW_KEY"), env={})
        assert resolved.api_key is None
        assert not resolved.has_key
        assert resolved.effective_key() == PLACEHOLDER_KEY

    def test_a_keyless_endpoint_still_sends_the_placeholder(self) -> None:
        """A local vLLM needs no key, but the SDK must never pick one itself."""
        resolved = resolve_endpoint(
            _endpoint(api_key_env=None, location=Location.LOCAL), env={}
        )
        assert resolved.effective_key() == PLACEHOLDER_KEY

    def test_an_ambient_openai_key_is_never_adopted(self) -> None:
        """The wave's core security property, asserted at the resolution layer.

        An endpoint that names no credential variable must not inherit
        OPENAI_API_KEY from the environment just because the SDK would.
        """
        resolved = resolve_endpoint(
            _endpoint(api_key_env=None),
            env={"OPENAI_API_KEY": "sk-real-cloud-key"},
        )
        assert resolved.api_key is None
        assert resolved.effective_key() == PLACEHOLDER_KEY
        assert "sk-real-cloud-key" not in str(resolved)

    def test_a_blank_variable_counts_as_unset(self) -> None:
        resolved = resolve_endpoint(
            _endpoint(api_key_env="GW_KEY"), env={"GW_KEY": "   "}
        )
        assert resolved.api_key is None

    def test_an_endpoint_without_a_base_url_sends_no_placeholder(self) -> None:
        """Cloud OpenAI: the SDK's own default base and its own key handling."""
        resolved = resolve_endpoint(
            _endpoint(base_url=None, api_key_env=None), env={}
        )
        assert resolved.effective_key() is None


class TestHeaderResolution:
    def test_a_public_literal_is_passed_through(self) -> None:
        resolved = resolve_endpoint(
            _endpoint(headers=(EndpointHeaderConfig(name="X-Title", value="MAK"),)),
            env={},
        )
        assert resolved.headers == (("X-Title", "MAK"),)

    def test_a_secret_header_is_read_from_the_environment(self) -> None:
        resolved = resolve_endpoint(
            _endpoint(
                headers=(EndpointHeaderConfig(name="X-Token", value_env="GW_TOK"),)
            ),
            env={"GW_TOK": "tok-123"},
        )
        assert resolved.headers == (("X-Token", "tok-123"),)

    def test_an_unset_secret_header_is_dropped_not_sent_empty(self) -> None:
        """An empty auth header looks authenticated and is not.

        The resulting 401 is far harder to read than the header being absent.
        """
        resolved = resolve_endpoint(
            _endpoint(
                headers=(EndpointHeaderConfig(name="X-Token", value_env="GW_TOK"),)
            ),
            env={},
        )
        assert resolved.headers == ()


class TestDisplaySafety:
    def test_describe_carries_no_credential(self) -> None:
        resolved = resolve_endpoint(
            _endpoint(api_key_env="GW_KEY"), env={"GW_KEY": "sk-secret"}
        )
        assert "sk-secret" not in resolved.describe()

    def test_describe_strips_a_query_string(self) -> None:
        resolved = resolve_endpoint(
            _endpoint(base_url="https://gw.example/v1?token=sekret"), env={}
        )
        assert "sekret" not in resolved.describe()

    def test_describe_names_the_location(self) -> None:
        resolved = resolve_endpoint(_endpoint(location=Location.PRIVATE), env={})
        assert "private" in resolved.describe()


class TestLookup:
    def test_resolve_endpoints_keys_by_id(self) -> None:
        resolved = resolve_endpoints(
            (_endpoint(id="a"), _endpoint(id="b")), env={}
        )
        assert sorted(resolved) == ["a", "b"]

    def test_require_endpoint_returns_the_match(self) -> None:
        resolved = resolve_endpoints((_endpoint(id="a"),), env={})
        assert require_endpoint(resolved, "a", where="agent 'x'").id == "a"

    def test_a_missing_endpoint_names_what_exists_and_how_to_add_one(self) -> None:
        resolved = resolve_endpoints((_endpoint(id="a"),), env={})
        with pytest.raises(ConfigError) as exc:
            require_endpoint(resolved, "nope", where="agent 'x'")
        message = str(exc.value)
        assert "agent 'x'" in message
        assert "known endpoints: a" in message
        assert "/endpoint add" in message


class TestAdapterSelection:
    def test_the_compatible_transport_selects_the_openai_adapter(self) -> None:
        assert resolve_endpoint(_endpoint(), env={}).adapter_type == "openai_api"

    def test_discovery_defaults_carry_through(self) -> None:
        resolved = resolve_endpoint(
            _endpoint(model_discovery=ModelDiscovery.MANUAL), env={}
        )
        assert resolved.model_discovery is ModelDiscovery.MANUAL
