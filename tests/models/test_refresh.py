from __future__ import annotations

from datetime import UTC, datetime

from mak.models.catalog import ModelEntry
from mak.models.manifest import Manifest, ProviderBlock
from mak.models.providers import FetchedModel, ModelFetchError
from mak.models.refresh import refresh

NOW = datetime(2026, 7, 15, tzinfo=UTC)
EARLIER = datetime(2026, 7, 1, tzinfo=UTC)
KEYS = {
    "ANTHROPIC_API_KEY": "a",
    "OPENAI_API_KEY": "o",
    "GEMINI_API_KEY": "g",
}


class FakeSource:
    """A ModelSource that returns canned models or raises."""

    def __init__(
        self,
        provider: str,
        models: list[str] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.provider = provider
        self._models = models or []
        self._raises = raises

    def fetch(self, api_key: str, *, timeout: float = 10.0) -> list[FetchedModel]:
        if self._raises is not None:
            raise self._raises
        return [FetchedModel(model_id=m, display_name=m.title()) for m in self._models]


def _manifest_with(provider: str, ids: list[str]) -> Manifest:
    return Manifest(
        last_refresh=EARLIER,
        last_attempt=EARLIER,
        providers={
            provider: ProviderBlock(
                fetched_at=EARLIER,
                models=tuple(
                    ModelEntry(provider=provider, model_id=m, display_name=m)
                    for m in ids
                ),
            )
        },
    )


class TestAddRemove:
    def test_new_model_is_added(self) -> None:
        manifest, report = refresh(
            sources=[FakeSource("anthropic", ["claude-opus-5", "claude-opus-6"])],
            api_keys=KEYS,
            manifest=_manifest_with("anthropic", ["claude-opus-5"]),
            now=NOW,
        )
        assert report.results[0].added == ("claude-opus-6",)
        assert "claude-opus-6" in {
            m.model_id for m in manifest.models_for("anthropic")
        }

    def test_missing_model_is_removed(self) -> None:
        manifest, report = refresh(
            sources=[FakeSource("anthropic", ["claude-opus-5"])],
            api_keys=KEYS,
            manifest=_manifest_with(
                "anthropic", ["claude-opus-5", "claude-opus-3"]
            ),
            now=NOW,
        )
        assert report.results[0].removed == ("claude-opus-3",)
        assert "claude-opus-3" not in {
            m.model_id for m in manifest.models_for("anthropic")
        }

    def test_unchanged_reports_no_delta(self) -> None:
        _manifest, report = refresh(
            sources=[FakeSource("anthropic", ["claude-opus-5"])],
            api_keys=KEYS,
            manifest=_manifest_with("anthropic", ["claude-opus-5"]),
            now=NOW,
        )
        assert report.results[0].changed is False
        assert report.changed is False

    def test_junk_is_filtered_out(self) -> None:
        manifest, _report = refresh(
            sources=[
                FakeSource("openai", ["gpt-5.5", "dall-e-3", "text-embedding-3"])
            ],
            api_keys=KEYS,
            manifest=Manifest(),
            now=NOW,
        )
        assert [m.model_id for m in manifest.models_for("openai")] == ["gpt-5.5"]

    def test_dated_only_fetch_canonicalizes_to_seed_alias(self) -> None:
        """A seed alias keeps its id (and its curated flags) across a refresh."""
        manifest, _report = refresh(
            sources=[FakeSource("anthropic", ["claude-haiku-4-5-20251001"])],
            api_keys=KEYS,
            manifest=Manifest(),
            now=NOW,
        )
        ids = [m.model_id for m in manifest.models_for("anthropic")]
        assert ids == ["claude-haiku-4-5"]

    def test_canonicalized_alias_keeps_the_snapshot_facts(self) -> None:
        manifest, _report = refresh(
            sources=[FakeSource("anthropic", ["claude-haiku-4-5-20251001"])],
            api_keys=KEYS,
            manifest=Manifest(),
            now=NOW,
        )
        entry = manifest.models_for("anthropic")[0]
        assert entry.display_name == "Claude-Haiku-4-5-20251001"

    def test_dated_snapshot_collapses(self) -> None:
        manifest, _report = refresh(
            sources=[
                FakeSource(
                    "anthropic",
                    ["claude-haiku-4-5", "claude-haiku-4-5-20251001"],
                )
            ],
            api_keys=KEYS,
            manifest=Manifest(),
            now=NOW,
        )
        assert [m.model_id for m in manifest.models_for("anthropic")] == [
            "claude-haiku-4-5"
        ]


class TestFailureIsolation:
    def test_failed_provider_keeps_previous_entries(self) -> None:
        before = _manifest_with("anthropic", ["claude-opus-5"])
        manifest, report = refresh(
            sources=[FakeSource("anthropic", raises=ModelFetchError("offline"))],
            api_keys=KEYS,
            manifest=before,
            now=NOW,
        )
        assert report.results[0].ok is False
        assert [m.model_id for m in manifest.models_for("anthropic")] == [
            "claude-opus-5"
        ]

    def test_one_failure_does_not_block_others(self) -> None:
        before = _manifest_with("anthropic", ["claude-opus-5"])
        manifest, report = refresh(
            sources=[
                FakeSource("anthropic", raises=ModelFetchError("offline")),
                FakeSource("openai", ["gpt-5.5"]),
            ],
            api_keys=KEYS,
            manifest=before,
            now=NOW,
        )
        assert report.results[0].ok is False
        assert report.results[1].ok is True
        assert [m.model_id for m in manifest.models_for("anthropic")] == [
            "claude-opus-5"
        ]
        assert [m.model_id for m in manifest.models_for("openai")] == ["gpt-5.5"]

    def test_total_failure_does_not_advance_last_refresh(self) -> None:
        before = _manifest_with("anthropic", ["claude-opus-5"])
        manifest, report = refresh(
            sources=[FakeSource("anthropic", raises=ModelFetchError("offline"))],
            api_keys=KEYS,
            manifest=before,
            now=NOW,
        )
        assert report.ok is False
        assert manifest.last_refresh == EARLIER  # still due, will retry
        assert manifest.last_attempt == NOW

    def test_success_advances_last_refresh(self) -> None:
        manifest, report = refresh(
            sources=[FakeSource("anthropic", ["claude-opus-5"])],
            api_keys=KEYS,
            manifest=_manifest_with("anthropic", []),
            now=NOW,
        )
        assert report.ok is True
        assert manifest.last_refresh == NOW

    def test_missing_key_is_reported_not_fatal(self) -> None:
        before = _manifest_with("anthropic", ["claude-opus-5"])
        manifest, report = refresh(
            sources=[FakeSource("anthropic", ["claude-opus-9"])],
            api_keys={"ANTHROPIC_API_KEY": "  "},
            manifest=before,
            now=NOW,
        )
        assert report.results[0].error == "no API key"
        assert [m.model_id for m in manifest.models_for("anthropic")] == [
            "claude-opus-5"
        ]

    def test_unexpected_exception_is_contained(self) -> None:
        _manifest, report = refresh(
            sources=[FakeSource("anthropic", raises=RuntimeError("boom"))],
            api_keys=KEYS,
            manifest=Manifest(),
            now=NOW,
        )
        assert report.results[0].ok is False
        assert "boom" in (report.results[0].error or "")


class TestReport:
    def test_errors_property(self) -> None:
        _manifest, report = refresh(
            sources=[
                FakeSource("anthropic", raises=ModelFetchError("x")),
                FakeSource("openai", ["gpt-5.5"]),
            ],
            api_keys=KEYS,
            manifest=Manifest(),
            now=NOW,
        )
        assert [r.provider for r in report.errors] == ["anthropic"]

    def test_total_counts_entries(self) -> None:
        _manifest, report = refresh(
            sources=[FakeSource("openai", ["gpt-5.5", "gpt-5.6-sol"])],
            api_keys=KEYS,
            manifest=Manifest(),
            now=NOW,
        )
        assert report.results[0].total == 2
