"""Tests for the endpoint domain types: validation, tri-state, redaction."""

from __future__ import annotations

import pytest

from mak.core.exceptions import ConfigError
from mak.endpoints.types import (
    EndpointConfig,
    EndpointHeaderConfig,
    HealthPolicy,
    Location,
    StructuredOutput,
    Transport,
    validate_agent_id,
    validate_endpoint_id,
    validate_env_name,
)


class TestIdValidation:
    @pytest.mark.parametrize(
        "raw", ["nvidia", "zai-coding", "my_gateway", "a", "a" * 64]
    )
    def test_accepts_a_slug(self, raw: str) -> None:
        assert validate_endpoint_id(raw) == raw

    def test_normalizes_case_and_whitespace(self) -> None:
        assert validate_endpoint_id("  NVIDIA  ") == "nvidia"

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "1nvidia",          # must start with a letter
            "-leading",
            "has space",
            "has:colon",        # the spec grammar splits on ':'
            "has@at",           # the spec grammar splits on '@'
            "has.dot",
            "a" * 65,
        ],
    )
    def test_rejects_anything_else(self, raw: str) -> None:
        with pytest.raises(ConfigError, match="not a valid id"):
            validate_endpoint_id(raw)

    def test_the_message_names_which_kind_of_id_failed(self) -> None:
        with pytest.raises(ConfigError, match="agent id"):
            validate_agent_id("Bad Id")


class TestEnvNameValidation:
    @pytest.mark.parametrize(
        "raw", ["NVIDIA_API_KEY", "_PRIVATE", "A1", "OPENROUTER_API_KEY"]
    )
    def test_accepts_a_posix_name(self, raw: str) -> None:
        assert validate_env_name(raw, where="x") == raw

    @pytest.mark.parametrize(
        "raw", ["", "lowercase", "1LEADING", "HAS-DASH", "HAS SPACE", "sk-real-key"]
    )
    def test_rejects_anything_else(self, raw: str) -> None:
        with pytest.raises(ConfigError, match="environment variable NAME"):
            validate_env_name(raw, where="x")

    def test_the_message_explains_that_a_key_is_not_wanted(self) -> None:
        """A user pasting the key itself is the mistake this guards."""
        with pytest.raises(ConfigError, match="never stores the key"):
            validate_env_name("sk-abc123", where="endpoint 'x' 'api_key_env'")


class TestHeaderConfig:
    def test_a_public_literal_is_allowed(self) -> None:
        header = EndpointHeaderConfig(name="X-Title", value="MAK")
        assert not header.is_secret

    def test_a_secret_uses_an_env_name(self) -> None:
        header = EndpointHeaderConfig(name="X-Token", value_env="GATEWAY_TOKEN")
        assert header.is_secret

    def test_both_sources_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="exactly one"):
            EndpointHeaderConfig(name="X", value="a", value_env="B")

    def test_neither_source_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="exactly one"):
            EndpointHeaderConfig(name="X")

    @pytest.mark.parametrize(
        "name", ["Authorization", "authorization", "Content-Type", "Host", "User-Agent"]
    )
    def test_headers_mak_owns_cannot_be_overridden(self, name: str) -> None:
        with pytest.raises(ConfigError, match="set by MAK"):
            EndpointHeaderConfig(name=name, value="x")

    def test_a_blank_name_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="must have a 'name'"):
            EndpointHeaderConfig(name="  ", value="x")

    def test_a_secret_header_validates_its_env_name(self) -> None:
        with pytest.raises(ConfigError, match="environment variable NAME"):
            EndpointHeaderConfig(name="X-Token", value_env="not a name")


class TestEndpointConfig:
    def _endpoint(self, **kw: object) -> EndpointConfig:
        base = {
            "id": "nvidia",
            "transport": Transport.OPENAI_CHAT,
            "base_url": "https://integrate.api.nvidia.com/v1",
            "api_key_env": "NVIDIA_API_KEY",
        }
        base.update(kw)
        return EndpointConfig(**base)  # type: ignore[arg-type]

    def test_the_id_is_normalized(self) -> None:
        assert self._endpoint(id="NVIDIA").id == "nvidia"

    def test_display_name_defaults_to_the_id(self) -> None:
        assert self._endpoint().display_name == "nvidia"

    def test_a_bad_key_env_is_rejected_naming_the_endpoint(self) -> None:
        with pytest.raises(ConfigError, match="endpoint 'nvidia'"):
            self._endpoint(api_key_env="lowercase")

    def test_capability_fields_default_to_unset_not_none(self) -> None:
        """``None`` means 'defer'; ``StructuredOutput.NONE`` means 'do not try'.

        Collapsing the two would make a profile default impossible to override
        downward, so the distinction is asserted at the type level.
        """
        endpoint = self._endpoint()
        assert endpoint.structured_output is None
        assert endpoint.health_check is None
        explicit = self._endpoint(
            structured_output=StructuredOutput.NONE, health_check=HealthPolicy.NONE
        )
        assert explicit.structured_output is StructuredOutput.NONE
        assert explicit.structured_output is not None

    def test_duplicate_headers_are_rejected(self) -> None:
        with pytest.raises(ConfigError, match="twice"):
            self._endpoint(
                headers=(
                    EndpointHeaderConfig(name="X-Title", value="a"),
                    EndpointHeaderConfig(name="x-title", value="b"),
                )
            )

    def test_secret_env_names_lists_key_then_headers(self) -> None:
        endpoint = self._endpoint(
            headers=(
                EndpointHeaderConfig(name="X-Public", value="mak"),
                EndpointHeaderConfig(name="X-Token", value_env="GATEWAY_TOKEN"),
            )
        )
        assert endpoint.secret_env_names() == ("NVIDIA_API_KEY", "GATEWAY_TOKEN")

    def test_a_query_string_is_stripped_for_display(self) -> None:
        """Some gateways carry a token in the query string."""
        endpoint = self._endpoint(base_url="https://gw.example/v1?token=sekret")
        assert endpoint.sanitized_base_url() == "https://gw.example/v1"
        assert "sekret" not in endpoint.sanitized_base_url()

    def test_is_hosted_reads_the_explicit_location(self) -> None:
        assert self._endpoint().is_hosted
        assert not self._endpoint(location=Location.LOCAL).is_hosted
        assert not self._endpoint(location=Location.PRIVATE).is_hosted

    def test_a_base_url_alone_never_implies_local(self) -> None:
        """The heuristic Wave 22 deletes: hosted services all have a base_url."""
        assert self._endpoint(base_url="https://openrouter.ai/api/v1").is_hosted
