"""Tests for the ``endpoints:`` YAML section and the new agent/planner fields."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mak.config import AgentConfig, load_config
from mak.core.exceptions import ConfigError
from mak.endpoints.parse import (
    is_loopback,
    is_private_host,
    parse_endpoint,
    parse_endpoints,
    validate_endpoint_url,
)
from mak.endpoints.types import Location, Transport


def _write(tmp_path: Path, data: dict[str, object]) -> Path:
    path = tmp_path / "mak.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


_AGENTS: list[dict[str, object]] = [{"type": "anthropic_api"}]


class TestParseEndpoint:
    def test_a_preset_entry_inherits_the_profile(self) -> None:
        endpoint = parse_endpoint({"id": "nvidia-work", "profile": "nvidia"})
        assert endpoint.base_url == "https://integrate.api.nvidia.com/v1"
        assert endpoint.api_key_env == "NVIDIA_API_KEY"
        assert endpoint.location is Location.HOSTED
        assert endpoint.transport is Transport.OPENAI_CHAT

    def test_an_explicit_field_overrides_the_profile(self) -> None:
        endpoint = parse_endpoint(
            {
                "id": "nvidia-proxy",
                "profile": "nvidia",
                "base_url": "https://proxy.internal/v1",
                "api_key_env": "PROXY_KEY",
            }
        )
        assert endpoint.base_url == "https://proxy.internal/v1"
        assert endpoint.api_key_env == "PROXY_KEY"
        assert endpoint.profile == "nvidia"

    def test_an_id_is_required(self) -> None:
        with pytest.raises(ConfigError, match="must have an 'id'"):
            parse_endpoint({"profile": "nvidia"})

    def test_an_unknown_profile_lists_the_known_ones(self) -> None:
        with pytest.raises(ConfigError, match="known profiles"):
            parse_endpoint({"id": "x", "profile": "mistral"})

    def test_a_reserved_id_is_rejected_with_a_suggestion(self) -> None:
        """One namespace: 'openai' must keep exactly one meaning in a spec."""
        with pytest.raises(ConfigError, match="reserved"):
            parse_endpoint({"id": "openai", "base_url": "https://h/v1"})

    def test_every_enum_typo_is_caught_at_load(self) -> None:
        for key in (
            "transport",
            "location",
            "model_discovery",
            "health_check",
            "structured_output",
            "token_parameter",
        ):
            with pytest.raises(ConfigError, match=f"'{key}' must be one of"):
                parse_endpoint(
                    {"id": "x", "base_url": "https://h/v1", key: "nonsense"}
                )

    def test_the_error_names_the_endpoint(self) -> None:
        with pytest.raises(ConfigError, match="endpoint 'gw'"):
            parse_endpoint(
                {"id": "gw", "base_url": "https://h/v1", "health_check": "wat"}
            )

    def test_a_trailing_slash_is_stripped_but_the_path_is_kept(self) -> None:
        endpoint = parse_endpoint(
            {"id": "ds", "base_url": "https://api.deepseek.com/"}
        )
        assert endpoint.base_url == "https://api.deepseek.com"

    def test_a_non_v1_root_is_left_alone(self) -> None:
        """Several providers do not use /v1; MAK must not 'fix' that."""
        endpoint = parse_endpoint(
            {"id": "z", "base_url": "https://api.z.ai/api/coding/paas/v4"}
        )
        assert endpoint.base_url == "https://api.z.ai/api/coding/paas/v4"

    def test_headers_parse_into_typed_records(self) -> None:
        endpoint = parse_endpoint(
            {
                "id": "or",
                "base_url": "https://h/v1",
                "headers": [
                    {"name": "X-Title", "value": "MAK"},
                    {"name": "X-Token", "value_env": "OR_TOKEN"},
                ],
            }
        )
        assert len(endpoint.headers) == 2
        assert endpoint.headers[1].is_secret

    def test_a_header_without_a_name_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="header entry with no 'name'"):
            parse_endpoint(
                {"id": "x", "base_url": "https://h/v1", "headers": [{"value": "v"}]}
            )


class TestUrlSafety:
    def test_a_fragment_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="fragment"):
            validate_endpoint_url(
                "https://h/v1#frag", endpoint_id="x", location=Location.HOSTED
            )

    def test_embedded_credentials_are_rejected(self) -> None:
        """user:pass@host would put a secret in a file MAK writes and prints."""
        with pytest.raises(ConfigError, match="must not embed credentials"):
            validate_endpoint_url(
                "https://user:pw@h/v1", endpoint_id="x", location=Location.HOSTED
            )

    def test_plain_http_to_a_hosted_endpoint_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="in the clear"):
            validate_endpoint_url(
                "http://api.example.com/v1",
                endpoint_id="x",
                location=Location.HOSTED,
            )

    def test_plain_http_on_loopback_is_allowed(self) -> None:
        url = validate_endpoint_url(
            "http://localhost:8000/v1", endpoint_id="x", location=Location.LOCAL
        )
        assert url == "http://localhost:8000/v1"

    def test_plain_http_to_a_lan_host_needs_the_private_location(self) -> None:
        with pytest.raises(ConfigError, match="not on this machine"):
            validate_endpoint_url(
                "http://192.168.1.9:8000/v1",
                endpoint_id="x",
                location=Location.LOCAL,
            )

    def test_https_is_always_fine(self) -> None:
        for location in Location:
            assert validate_endpoint_url(
                "https://h/v1", endpoint_id="x", location=location
            )

    @pytest.mark.parametrize(
        "url", ["http://localhost:1", "http://127.0.0.1:1", "http://[::1]:1"]
    )
    def test_loopback_detection(self, url: str) -> None:
        assert is_loopback(url)

    def test_a_public_host_is_not_loopback_or_private(self) -> None:
        assert not is_loopback("https://api.example.com/v1")
        assert not is_private_host("https://api.example.com/v1")

    @pytest.mark.parametrize(
        "url", ["http://192.168.1.9:8000", "http://10.0.0.4:8000", "http://gpu-box:8000"]
    )
    def test_private_host_detection(self, url: str) -> None:
        assert is_private_host(url)


class TestParseEndpointsSection:
    def test_an_absent_section_is_empty(self) -> None:
        assert parse_endpoints(None) == ()

    def test_a_non_list_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="must be a list"):
            parse_endpoints({"id": "x"})

    def test_duplicate_ids_are_rejected(self) -> None:
        with pytest.raises(ConfigError, match="more than once"):
            parse_endpoints(
                [
                    {"id": "gw", "base_url": "https://a/v1"},
                    {"id": "gw", "base_url": "https://b/v1"},
                ]
            )


class TestConfigIntegration:
    def test_an_endpoint_backed_agent_round_trips(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            {
                "endpoints": [{"id": "nvidia-work", "profile": "nvidia"}],
                "agents": [
                    {
                        "id": "nvidia-llama",
                        "endpoint": "nvidia-work",
                        "model": "meta/llama-3.3-70b-instruct",
                        "max_instances": 2,
                    }
                ],
                "planner": {
                    "model": "meta/llama-3.3-70b-instruct",
                    "endpoint": "nvidia-work",
                },
            },
        )
        config = load_config(path)
        assert len(config.endpoints) == 1
        agent = config.agents[0]
        assert agent.id == "nvidia-llama"
        assert agent.endpoint == "nvidia-work"
        assert agent.routing_id() == "nvidia-llama"
        assert config.planner.endpoint == "nvidia-work"

    def test_an_agent_naming_both_an_endpoint_and_a_type_is_rejected(
        self, tmp_path: Path
    ) -> None:
        path = _write(
            tmp_path,
            {
                "endpoints": [{"id": "gw", "base_url": "https://h/v1"}],
                "agents": [{"endpoint": "gw", "type": "openai_api"}],
            },
        )
        with pytest.raises(ConfigError, match="will not guess which host"):
            load_config(path)

    def test_an_agent_naming_an_endpoint_and_a_base_url_is_rejected(
        self, tmp_path: Path
    ) -> None:
        path = _write(
            tmp_path,
            {
                "endpoints": [{"id": "gw", "base_url": "https://h/v1"}],
                "agents": [{"endpoint": "gw", "base_url": "https://other/v1"}],
            },
        )
        with pytest.raises(ConfigError, match="'base_url'"):
            load_config(path)

    def test_a_planner_naming_an_endpoint_and_a_backend_is_rejected(
        self, tmp_path: Path
    ) -> None:
        path = _write(
            tmp_path,
            {
                "endpoints": [{"id": "gw", "base_url": "https://h/v1"}],
                "agents": _AGENTS,
                "planner": {"endpoint": "gw", "backend": "openai"},
            },
        )
        with pytest.raises(ConfigError, match="repository inventory"):
            load_config(path)

    def test_an_unknown_endpoint_reference_is_caught_at_load(
        self, tmp_path: Path
    ) -> None:
        path = _write(
            tmp_path,
            {
                "endpoints": [{"id": "gw", "base_url": "https://h/v1"}],
                "agents": [{"endpoint": "typo", "model": "m"}],
            },
        )
        with pytest.raises(ConfigError, match="does not declare"):
            load_config(path)

    def test_a_reference_is_not_checked_when_the_file_declares_none(
        self, tmp_path: Path
    ) -> None:
        """The user endpoint store may supply it; the composition root rechecks."""
        path = _write(
            tmp_path, {"agents": [{"endpoint": "from-user-store", "model": "m"}]}
        )
        config = load_config(path)
        assert config.agents[0].endpoint == "from-user-store"

    def test_a_legacy_config_is_unchanged(self, tmp_path: Path) -> None:
        """The whole point: nothing about an existing file behaves differently."""
        path = _write(
            tmp_path,
            {
                "agents": [
                    {"type": "anthropic_api", "model": "claude-opus-5"},
                    {"type": "local_api", "base_url": "http://localhost:8000/v1"},
                ]
            },
        )
        config = load_config(path)
        assert config.endpoints == ()
        assert config.agents == (
            AgentConfig(type="anthropic_api", model="claude-opus-5"),
            AgentConfig(type="local_api", base_url="http://localhost:8000/v1"),
        )
        assert [a.routing_id() for a in config.agents] == [
            "anthropic_api",
            "local_api",
        ]

    def test_an_invalid_agent_id_is_rejected(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path, {"agents": [{"type": "openai_api", "id": "Has Space"}]}
        )
        with pytest.raises(ConfigError, match="agent id"):
            load_config(path)

    def test_a_hosted_endpoint_over_http_is_rejected_from_yaml(
        self, tmp_path: Path
    ) -> None:
        path = _write(
            tmp_path,
            {
                "endpoints": [
                    {
                        "id": "gw",
                        "base_url": "http://api.example.com/v1",
                        "location": "hosted",
                    }
                ],
                "agents": _AGENTS,
            },
        )
        with pytest.raises(ConfigError, match="in the clear"):
            load_config(path)
