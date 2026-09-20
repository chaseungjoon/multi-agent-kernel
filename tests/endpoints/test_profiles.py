"""Tests for the built-in provider profiles.

The consistency test at the bottom is the load-bearing one: it is what stops the
CLI, the config parser and the documentation from each carrying their own copy
of a provider URL that then drifts.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mak.endpoints.profiles import (
    BUILTIN_PROFILES,
    RESERVED_ENDPOINT_IDS,
    TRANSPORT_ADAPTER_TYPE,
    adapter_type_for,
    is_reserved,
    profile_for,
    profile_ids,
)
from mak.endpoints.types import Location, Transport, validate_endpoint_id

_REPO_ROOT = Path(__file__).resolve().parents[2]


class TestTheTable:
    def test_every_profile_id_is_a_valid_slug(self) -> None:
        for profile in BUILTIN_PROFILES:
            assert validate_endpoint_id(profile.id) == profile.id

    def test_profile_ids_are_unique(self) -> None:
        ids = [p.id for p in BUILTIN_PROFILES]
        assert len(ids) == len(set(ids))

    def test_the_wave_ships_the_six_documented_profiles(self) -> None:
        assert set(profile_ids()) == {
            "nvidia",
            "openrouter",
            "deepseek",
            "zai-general",
            "zai-coding",
            "custom",
        }

    def test_every_hosted_profile_uses_https(self) -> None:
        for profile in BUILTIN_PROFILES:
            if profile.location is Location.HOSTED:
                assert profile.base_url is not None
                assert profile.base_url.startswith("https://"), profile.id

    def test_every_hosted_profile_cites_its_documentation(self) -> None:
        """These rows are external facts; the source must travel with them."""
        for profile in BUILTIN_PROFILES:
            if profile.location is Location.HOSTED:
                assert profile.docs_url.startswith("https://"), profile.id

    def test_no_profile_ships_a_model_id(self) -> None:
        """Live model availability belongs to /models or to the user, not here."""
        for profile in BUILTIN_PROFILES:
            fields = (profile.base_url or "", profile.display_name, profile.note)
            for text in fields:
                assert "llama-3" not in text, profile.id
                assert "deepseek-chat" not in text, profile.id

    def test_no_profile_carries_a_credential_value(self) -> None:
        for profile in BUILTIN_PROFILES:
            assert profile.api_key_env is None or profile.api_key_env.isupper()
            for header in profile.headers:
                assert header.value_env is not None or header.is_secret is False

    def test_deepseek_keeps_its_non_v1_root(self) -> None:
        """MAK must never 'helpfully' append /v1 — DeepSeek documents the host."""
        deepseek = profile_for("deepseek")
        assert deepseek is not None
        assert deepseek.base_url == "https://api.deepseek.com"

    def test_the_two_zai_plans_have_different_urls(self) -> None:
        """Picking the wrong one charges the wrong balance."""
        general = profile_for("zai-general")
        coding = profile_for("zai-coding")
        assert general is not None and coding is not None
        assert general.base_url != coding.base_url
        assert general.api_key_env == coding.api_key_env
        assert "quota" in coding.note or "Coding Plan" in coding.note
        assert "prepaid" in general.note or "balance" in general.note

    def test_custom_assumes_no_url_or_credential(self) -> None:
        custom = profile_for("custom")
        assert custom is not None
        assert custom.base_url is None
        assert custom.api_key_env is None

    def test_every_profile_speaks_the_compatible_transport(self) -> None:
        """No vendor SDK: the openai package drives every compatible profile."""
        for profile in BUILTIN_PROFILES:
            assert profile.transport is Transport.OPENAI_CHAT, profile.id


class TestLookup:
    def test_profile_for_is_case_insensitive(self) -> None:
        assert profile_for("NVIDIA") is not None

    def test_an_unknown_profile_returns_none(self) -> None:
        assert profile_for("mistral") is None

    def test_to_endpoint_prefills_the_documented_values(self) -> None:
        profile = profile_for("openrouter")
        assert profile is not None
        endpoint = profile.to_endpoint()
        assert endpoint.id == "openrouter"
        assert endpoint.profile == "openrouter"
        assert endpoint.base_url == "https://openrouter.ai/api/v1"
        assert endpoint.api_key_env == "OPENROUTER_API_KEY"

    def test_to_endpoint_accepts_a_different_id(self) -> None:
        profile = profile_for("nvidia")
        assert profile is not None
        endpoint = profile.to_endpoint("nvidia-work")
        assert endpoint.id == "nvidia-work"
        assert endpoint.profile == "nvidia"


class TestReservedIds:
    def test_the_legacy_provider_prefixes_are_reserved(self) -> None:
        assert RESERVED_ENDPOINT_IDS == {
            "anthropic",
            "openai",
            "gemini",
            "google",
            "local",
            "ollama",
        }

    @pytest.mark.parametrize("raw", ["openai", "OpenAI", " ollama "])
    def test_is_reserved_normalizes(self, raw: str) -> None:
        assert is_reserved(raw)

    def test_no_builtin_profile_claims_a_reserved_id(self) -> None:
        """A profile id doubles as the default endpoint id in the wizard."""
        for profile in BUILTIN_PROFILES:
            assert not is_reserved(profile.id), profile.id


class TestTransportMapping:
    def test_every_non_cli_transport_maps_to_an_adapter_type(self) -> None:
        for transport in Transport:
            if transport is Transport.CLI:
                continue
            assert transport in TRANSPORT_ADAPTER_TYPE

    def test_the_compatible_transport_uses_the_openai_adapter(self) -> None:
        assert adapter_type_for(Transport.OPENAI_CHAT) == "openai_api"

    def test_cli_has_no_endpoint_adapter(self) -> None:
        with pytest.raises(KeyError):
            adapter_type_for(Transport.CLI)


class TestOneSourceOfTruth:
    """The presets must appear in exactly one place in the tree.

    A URL copied into the CLI, the config parser or an example config is a URL
    that drifts from the table the next time a provider changes it.
    """

    @pytest.mark.parametrize(
        "url",
        [p.base_url for p in BUILTIN_PROFILES if p.base_url],
    )
    def test_a_preset_url_appears_only_in_the_profile_table(self, url: str) -> None:
        hits = _grep(url, ("mak", "cli"))
        assert hits == {"mak/endpoints/profiles.py"}, (
            f"{url} also appears in {sorted(hits - {'mak/endpoints/profiles.py'})}"
        )

    @pytest.mark.parametrize(
        "env",
        sorted({p.api_key_env for p in BUILTIN_PROFILES if p.api_key_env}),
    )
    def test_a_preset_key_env_appears_only_in_the_profile_table(
        self, env: str
    ) -> None:
        hits = _grep(env, ("mak", "cli"))
        assert hits == {"mak/endpoints/profiles.py"}, (
            f"{env} also appears in {sorted(hits - {'mak/endpoints/profiles.py'})}"
        )


def _grep(needle: str, roots: tuple[str, ...]) -> set[str]:
    """Return repo-relative paths of source files containing ``needle``."""
    pattern = re.compile(re.escape(needle))
    found: set[str] = set()
    for root in roots:
        for path in (_REPO_ROOT / root).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            if pattern.search(path.read_text(encoding="utf-8")):
                found.add(str(path.relative_to(_REPO_ROOT)))
    return found
