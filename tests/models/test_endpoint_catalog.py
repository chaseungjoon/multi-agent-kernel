"""Wave 22.7: per-endpoint model discovery, manifest v2, and judgment honesty."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from mak.endpoints.resolution import ResolvedEndpoint
from mak.endpoints.types import (
    HealthPolicy,
    Location,
    ModelDiscovery,
    StructuredOutput,
    TokenParameter,
    Transport,
)
from mak.models.catalog import ModelEntry
from mak.models.manifest import (
    PREVIOUS_SCHEMA_VERSION,
    SCHEMA_VERSION,
    Manifest,
    ProviderBlock,
    load_manifest,
    save_manifest,
)
from mak.models.providers import (
    FetchedModel,
    ModelFetchError,
    OpenAiCompatibleSource,
    sources_for_endpoints,
)
from mak.models.refresh import refresh
from mak.models.registry import ModelRegistry

_NOW = datetime(2026, 9, 20, tzinfo=UTC)


def _endpoint(
    endpoint_id: str = "nvidia",
    *,
    discovery: ModelDiscovery = ModelDiscovery.MODELS,
    key: str | None = "sk-nv",
    headers: tuple[tuple[str, str], ...] = (),
) -> ResolvedEndpoint:
    return ResolvedEndpoint(
        id=endpoint_id,
        display_name=endpoint_id,
        transport=Transport.OPENAI_CHAT,
        base_url="https://nv.example/v1",
        location=Location.HOSTED,
        model_discovery=discovery,
        health_check=HealthPolicy.MODELS,
        structured_output=StructuredOutput.AUTO,
        token_parameter=TokenParameter.MAX_TOKENS,
        api_key_env="NVIDIA_API_KEY",
        api_key=key,
        headers=headers,
    )


class _Source:
    """A model source that answers from a script instead of the network."""

    def __init__(
        self, provider: str, result: list[FetchedModel] | Exception
    ) -> None:
        self.provider = provider
        self._result = result
        self.calls = 0

    def fetch(self, api_key: str, *, timeout: float = 10.0) -> list[FetchedModel]:
        self.calls += 1
        if isinstance(self._result, Exception):
            raise self._result
        return list(self._result)


class TestManifestMigration:
    def test_a_v1_cache_survives_the_upgrade(self, tmp_path: Path) -> None:
        """The user keeps every cached model instead of paying a refetch."""
        path = tmp_path / "models.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": PREVIOUS_SCHEMA_VERSION,
                    "last_refresh": _NOW.isoformat(),
                    "last_attempt": _NOW.isoformat(),
                    "providers": {
                        "anthropic": {
                            "fetched_at": _NOW.isoformat(),
                            "models": [
                                {
                                    "provider": "anthropic",
                                    "model_id": "claude-opus-5",
                                    "display_name": "Claude Opus 5",
                                    "context_window": 200000,
                                    "max_output": 64000,
                                }
                            ],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        manifest = load_manifest(path)
        (entry,) = manifest.models_for("anthropic")
        assert entry.model_id == "claude-opus-5"
        assert entry.context_window == 200000
        # v1 carried no endpoint id; the block key was the provider name, which
        # *is* the built-in endpoint id. Nothing is guessed.
        assert entry.endpoint_id == "anthropic"

    def test_a_v1_cache_is_rewritten_as_v2(self, tmp_path: Path) -> None:
        path = tmp_path / "models.json"
        save_manifest(
            Manifest(
                providers={
                    "anthropic": ProviderBlock(
                        fetched_at=_NOW,
                        models=(
                            ModelEntry(
                                provider="anthropic",
                                model_id="claude-opus-5",
                                display_name="Claude Opus 5",
                            ),
                        ),
                    )
                }
            ),
            path,
        )
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["schema_version"] == SCHEMA_VERSION

    def test_a_future_schema_still_degrades_to_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "models.json"
        path.write_text(
            json.dumps({"schema_version": 99, "providers": {}}), encoding="utf-8"
        )
        assert load_manifest(path).providers == {}

    def test_a_third_party_block_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / "models.json"
        save_manifest(
            Manifest(
                providers={
                    "nvidia": ProviderBlock(
                        fetched_at=_NOW,
                        models=(
                            ModelEntry(
                                provider="nvidia",
                                endpoint_id="nvidia",
                                model_id="meta/llama-3.3-70b-instruct",
                                display_name="meta/llama-3.3-70b-instruct",
                                evaluated=False,
                            ),
                        ),
                    )
                }
            ),
            path,
        )
        (entry,) = load_manifest(path).models_for("nvidia")
        assert entry.endpoint_id == "nvidia"
        assert entry.evaluated is False


class TestEndpointKeying:
    def test_the_same_model_id_at_two_endpoints_is_two_entries(self) -> None:
        direct = ModelEntry(
            provider="anthropic",
            model_id="claude-opus-5",
            display_name="Claude Opus 5",
        )
        proxied = ModelEntry(
            provider="openrouter",
            endpoint_id="openrouter",
            model_id="claude-opus-5",
            display_name="claude-opus-5",
        )
        assert direct.key != proxied.key

    def test_a_built_in_entry_defaults_its_endpoint_to_the_provider(self) -> None:
        entry = ModelEntry(
            provider="openai", model_id="gpt-5.6-sol", display_name="x"
        )
        assert entry.endpoint_id == "openai"

    def test_the_spec_names_the_endpoint(self) -> None:
        entry = ModelEntry(
            provider="nvidia",
            endpoint_id="nvidia",
            model_id="meta/llama",
            display_name="x",
        )
        assert entry.spec == "nvidia:meta/llama"


class TestPerEndpointIsolation:
    def _manifest(self) -> Manifest:
        return Manifest(
            providers={
                "anthropic": ProviderBlock(
                    fetched_at=_NOW,
                    models=(
                        ModelEntry(
                            provider="anthropic",
                            model_id="claude-opus-5",
                            display_name="Claude Opus 5",
                        ),
                    ),
                ),
                "nvidia": ProviderBlock(
                    fetched_at=_NOW,
                    models=(
                        ModelEntry(
                            provider="nvidia",
                            endpoint_id="nvidia",
                            model_id="old/model",
                            display_name="old/model",
                        ),
                    ),
                ),
            }
        )

    def test_one_endpoints_failure_keeps_its_own_previous_cache(self) -> None:
        manifest, report = refresh(
            sources=[_Source("nvidia", ModelFetchError("nvidia: boom"))],
            api_keys={"NVIDIA_API_KEY": "sk"},
            manifest=self._manifest(),
            now=_NOW,
            key_envs={"nvidia": "NVIDIA_API_KEY"},
        )
        assert [e.model_id for e in manifest.models_for("nvidia")] == ["old/model"]
        assert report.errors

    def test_one_endpoints_failure_does_not_touch_another(self) -> None:
        manifest, _ = refresh(
            sources=[
                _Source("nvidia", ModelFetchError("boom")),
                _Source("anthropic", [FetchedModel(model_id="claude-opus-5")]),
            ],
            api_keys={"NVIDIA_API_KEY": "sk", "ANTHROPIC_API_KEY": "sk"},
            manifest=self._manifest(),
            now=_NOW,
            key_envs={"nvidia": "NVIDIA_API_KEY"},
        )
        assert manifest.models_for("anthropic")
        assert [e.model_id for e in manifest.models_for("nvidia")] == ["old/model"]

    def test_a_missing_key_is_reported_not_attempted(self) -> None:
        source = _Source("nvidia", [FetchedModel(model_id="x")])
        _, report = refresh(
            sources=[source],
            api_keys={},
            manifest=self._manifest(),
            now=_NOW,
            key_envs={"nvidia": "NVIDIA_API_KEY"},
        )
        assert source.calls == 0
        assert any("no API key" in (r.error or "") for r in report.errors)

    def test_a_keyless_endpoint_still_lists(self) -> None:
        """A local vLLM needs no credential and must not be skipped for that."""
        source = _Source("vllm", [FetchedModel(model_id="local-model")])
        manifest, _ = refresh(
            sources=[source],
            api_keys={},
            manifest=Manifest(),
            now=_NOW,
            key_envs={},
        )
        assert source.calls == 1
        assert [e.model_id for e in manifest.models_for("vllm")] == ["local-model"]


class TestCurationScope:
    def test_third_party_ids_are_not_filtered_or_rewritten(self) -> None:
        """MAK's deny patterns and date-collapsing target three known catalogs.

        Applying them to an arbitrary service would drop real models whose ids
        happen to match, and rewrite ids that are not dated snapshots at all.
        """
        manifest, _ = refresh(
            sources=[
                _Source(
                    "nvidia",
                    [
                        FetchedModel(model_id="meta/llama-3.3-70b-instruct"),
                        FetchedModel(model_id="nvidia/embed-qa-4"),
                        FetchedModel(model_id="some/model-2025-01-01"),
                    ],
                )
            ],
            api_keys={"NVIDIA_API_KEY": "sk"},
            manifest=Manifest(),
            now=_NOW,
            key_envs={"nvidia": "NVIDIA_API_KEY"},
        )
        ids = [e.model_id for e in manifest.models_for("nvidia")]
        assert ids == [
            "meta/llama-3.3-70b-instruct",
            "nvidia/embed-qa-4",
            "some/model-2025-01-01",
        ]

    def test_built_in_providers_keep_their_curation(self) -> None:
        manifest, _ = refresh(
            sources=[
                _Source(
                    "openai",
                    [
                        FetchedModel(model_id="gpt-5.6-sol"),
                        FetchedModel(model_id="text-embedding-3-large"),
                    ],
                )
            ],
            api_keys={"OPENAI_API_KEY": "sk"},
            manifest=Manifest(),
            now=_NOW,
        )
        ids = [e.model_id for e in manifest.models_for("openai")]
        assert "gpt-5.6-sol" in ids
        assert "text-embedding-3-large" not in ids

    def test_a_third_party_model_is_marked_unevaluated(self) -> None:
        manifest, _ = refresh(
            sources=[_Source("nvidia", [FetchedModel(model_id="meta/llama")])],
            api_keys={"NVIDIA_API_KEY": "sk"},
            manifest=Manifest(),
            now=_NOW,
            key_envs={"nvidia": "NVIDIA_API_KEY"},
        )
        (entry,) = manifest.models_for("nvidia")
        assert entry.evaluated is False
        assert entry.planner_note() == "not evaluated"

    def test_a_built_in_model_stays_evaluated(self) -> None:
        manifest, _ = refresh(
            sources=[_Source("openai", [FetchedModel(model_id="gpt-5.6-sol")])],
            api_keys={"OPENAI_API_KEY": "sk"},
            manifest=Manifest(),
            now=_NOW,
        )
        (entry,) = manifest.models_for("openai")
        assert entry.evaluated is True
        assert entry.planner_note() == ""

    def test_an_unevaluated_model_is_never_silently_planner_safe(self) -> None:
        entry = ModelEntry(
            provider="nvidia",
            endpoint_id="nvidia",
            model_id="meta/llama",
            display_name="x",
            evaluated=False,
        )
        # planner_ok defaults True (neutral), but the note makes the state clear
        # rather than presenting that default as an endorsement.
        assert entry.planner_ok is True
        assert entry.planner_note() == "not evaluated"


class TestSourceConstruction:
    def test_a_source_is_built_per_compatible_endpoint(self) -> None:
        sources = sources_for_endpoints(
            [_endpoint("nvidia"), _endpoint("openrouter")]
        )
        assert [s.provider for s in sources] == ["nvidia", "openrouter"]

    def test_manual_discovery_produces_no_source_at_all(self) -> None:
        """Visible in the source list, not buried in a branch."""
        assert (
            sources_for_endpoints([_endpoint(discovery=ModelDiscovery.MANUAL)]) == ()
        )

    def test_a_non_compatible_transport_is_skipped(self) -> None:
        anthropic = ResolvedEndpoint(
            id="anthropic",
            display_name="Anthropic",
            transport=Transport.ANTHROPIC,
            base_url=None,
            location=Location.HOSTED,
            model_discovery=ModelDiscovery.MODELS,
            health_check=HealthPolicy.NONE,
            structured_output=StructuredOutput.NONE,
            token_parameter=TokenParameter.MAX_TOKENS,
        )
        assert sources_for_endpoints([anthropic]) == ()

    def test_the_source_reports_the_endpoint_id_as_its_provider(self) -> None:
        assert OpenAiCompatibleSource(_endpoint("zai-coding")).provider == (
            "zai-coding"
        )

    def test_a_fetch_error_is_redacted_before_it_is_stored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys
        import types

        module = types.ModuleType("openai")

        def _boom(**_: Any) -> Any:
            raise RuntimeError("rejected sk-live-supersecret at the gateway")

        module.OpenAI = _boom  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "openai", module)
        with pytest.raises(ModelFetchError) as exc:
            OpenAiCompatibleSource(_endpoint()).fetch("sk-live-supersecret")
        assert "sk-live-supersecret" not in str(exc.value)


class TestCatalogComposition:
    def test_third_party_models_appear_after_the_built_ins(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "models.json"
        save_manifest(
            Manifest(
                providers={
                    "nvidia": ProviderBlock(
                        fetched_at=_NOW,
                        models=(
                            ModelEntry(
                                provider="nvidia",
                                endpoint_id="nvidia",
                                model_id="meta/llama",
                                display_name="meta/llama",
                                evaluated=False,
                            ),
                        ),
                    )
                }
            ),
            path,
        )
        registry = ModelRegistry(manifest_path_=path, sources=())
        endpoint_ids = registry.endpoint_ids()
        assert endpoint_ids[-1] == "nvidia"
        assert "anthropic" in endpoint_ids

    def test_for_endpoint_selects_by_service_not_by_maker(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "models.json"
        save_manifest(
            Manifest(
                providers={
                    "openrouter": ProviderBlock(
                        fetched_at=_NOW,
                        models=(
                            ModelEntry(
                                provider="openrouter",
                                endpoint_id="openrouter",
                                model_id="anthropic/claude-opus-5",
                                display_name="anthropic/claude-opus-5",
                                evaluated=False,
                            ),
                        ),
                    )
                }
            ),
            path,
        )
        registry = ModelRegistry(manifest_path_=path, sources=())
        assert len(registry.for_endpoint("openrouter")) == 1
        assert registry.for_endpoint("anthropic")

    def test_find_can_be_scoped_to_an_endpoint(self, tmp_path: Path) -> None:
        path = tmp_path / "models.json"
        save_manifest(
            Manifest(
                providers={
                    "openrouter": ProviderBlock(
                        fetched_at=_NOW,
                        models=(
                            ModelEntry(
                                provider="openrouter",
                                endpoint_id="openrouter",
                                model_id="claude-opus-5",
                                display_name="claude-opus-5",
                                evaluated=False,
                            ),
                        ),
                    )
                }
            ),
            path,
        )
        registry = ModelRegistry(manifest_path_=path, sources=())
        proxied = registry.find("claude-opus-5", "openrouter")
        assert proxied is not None and proxied.evaluated is False
        assert registry.find("claude-opus-5", "nowhere") is None
