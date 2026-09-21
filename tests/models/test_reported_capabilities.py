"""Wave 24: capability facts survive the trip from ``/models`` to the cache.

The chain under test, end to end:

.. code-block:: text

    SDK model row -> FetchedModel -> ModelEntry -> manifest JSON
                  -> ModelEntry -> ReportedCapabilities -> CapabilityCache

Every hop must preserve the tri-state — unknown, reported-empty, reported — and
the exact model id. Wave 22 dropped this field on the floor at the first hop,
which is why MAK could only ever learn a model's limits by being refused.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mak.models.catalog import ModelEntry
from mak.models.manifest import (
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    Manifest,
    ProviderBlock,
    load_manifest,
    save_manifest,
)
from mak.models.providers import FetchedModel, reported_parameters
from mak.models.registry import ReportedCapabilities

FREE_PARAMS = frozenset({"max_tokens", "temperature", "tools"})
PAID_PARAMS = FREE_PARAMS | {"response_format", "structured_outputs"}
MODEL = "inclusionai/ling-3.0-flash-vl:free"
PAID = "inclusionai/ling-3.0-flash-vl"


class _Row:
    """A model row shaped like the openai SDK's ``Model`` with extras.

    The real SDK types ``Model`` with four fields and keeps everything else as
    pydantic extras reachable through ``model_extra``; this mirrors that, so
    the extraction helper is tested against the shape it will actually meet.
    """

    def __init__(self, model_id: str, **extra: object) -> None:
        self.id = model_id
        self.model_extra = dict(extra)
        for key, value in extra.items():
            setattr(self, key, value)


class TestExtraction:
    """Reading the field off whatever the SDK hands back."""

    def test_a_published_list_is_read(self) -> None:
        row = _Row(MODEL, supported_parameters=["max_tokens", "tools"])
        assert reported_parameters(row) == frozenset({"max_tokens", "tools"})

    def test_an_absent_field_is_unknown(self) -> None:
        """Cloud OpenAI, vLLM and llama.cpp all return bare ids."""
        assert reported_parameters(_Row(MODEL)) is None

    def test_an_empty_list_is_an_empty_report_not_unknown(self) -> None:
        """Three live OpenRouter models do this, and they *do* serve schemas."""
        assert reported_parameters(_Row(MODEL, supported_parameters=[])) == (
            frozenset()
        )

    def test_model_extra_is_preferred_over_the_attribute(self) -> None:
        """Insulates MAK from a future SDK adding a real field of this name."""
        row = _Row(MODEL, supported_parameters=["from_extra"])
        # Shadow the attribute; ``model_extra`` still holds the server's value.
        row.supported_parameters = ["from_attribute"]  # type: ignore[assignment]
        assert reported_parameters(row) == frozenset({"from_extra"})

    def test_the_attribute_is_the_fallback(self) -> None:
        """An SDK or fake with no ``model_extra`` must still work."""

        class Bare:
            id = MODEL
            supported_parameters = ["response_format"]

        assert reported_parameters(Bare()) == frozenset({"response_format"})

    @pytest.mark.parametrize("value", [None, "response_format", 7, object()])
    def test_a_non_list_value_is_unknown(self, value: object) -> None:
        """A malformed field must degrade to "discover it", never to a claim.

        A bare string is iterable, so without the explicit guard it would
        decompose into a set of single characters and quietly claim nothing is
        supported.
        """
        assert reported_parameters(_Row(MODEL, supported_parameters=value)) is None

    def test_non_string_members_are_dropped(self) -> None:
        row = _Row(MODEL, supported_parameters=["tools", 5, None, "stop"])
        assert reported_parameters(row) == frozenset({"tools", "stop"})


class TestFetchedModel:
    """The field defaults to unknown, so no existing fetcher asserts anything."""

    def test_the_default_is_unknown(self) -> None:
        assert FetchedModel(model_id=MODEL).supported_parameters is None

    def test_a_report_round_trips(self) -> None:
        fetched = FetchedModel(model_id=MODEL, supported_parameters=FREE_PARAMS)
        assert fetched.supported_parameters == FREE_PARAMS


class TestSerialization:
    """Deterministic on disk, and lossless across the three states."""

    def test_the_list_is_written_sorted(self) -> None:
        """A byte-stable manifest, so an unchanged catalog produces no diff."""
        entry = ModelEntry(
            provider="openrouter",
            model_id=MODEL,
            display_name="Ling",
            supported_parameters=frozenset({"tools", "max_tokens", "stop"}),
        )
        assert entry.to_dict()["supported_parameters"] == [
            "max_tokens",
            "stop",
            "tools",
        ]

    def test_unknown_is_omitted_entirely(self) -> None:
        """So a fresh write and an older record agree on what absence means."""
        entry = ModelEntry(
            provider="openai", model_id="gpt-5.6-sol", display_name="GPT"
        )
        assert "supported_parameters" not in entry.to_dict()

    @pytest.mark.parametrize(
        "value", [None, frozenset(), frozenset({"response_format"})]
    )
    def test_all_three_states_round_trip(
        self, value: frozenset[str] | None
    ) -> None:
        entry = ModelEntry(
            provider="openrouter",
            model_id=MODEL,
            display_name="Ling",
            supported_parameters=value,
        )
        restored = ModelEntry.from_dict(json.loads(json.dumps(entry.to_dict())))
        assert restored.supported_parameters == value

    def test_an_explicit_null_reads_as_unknown(self) -> None:
        """A hand-edited or older-writer manifest must not crash or lie."""
        restored = ModelEntry.from_dict(
            {"provider": "p", "model_id": "m", "supported_parameters": None}
        )
        assert restored.supported_parameters is None

    def test_a_garbage_value_reads_as_unknown(self) -> None:
        restored = ModelEntry.from_dict(
            {"provider": "p", "model_id": "m", "supported_parameters": "nope"}
        )
        assert restored.supported_parameters is None


class TestManifestMigration:
    """v1 and v2 caches load without loss; a future one still degrades safely."""

    def test_the_schema_version_advanced(self) -> None:
        assert SCHEMA_VERSION == 3
        assert SUPPORTED_SCHEMA_VERSIONS == frozenset({1, 2, 3})

    @pytest.mark.parametrize("version", [1, 2])
    def test_an_older_cache_keeps_every_model(
        self, version: int, tmp_path: Path
    ) -> None:
        """The migration hazard that matters: nobody pays a refetch to upgrade.

        An older record has no capability key at all, which reads as *unknown* —
        the tri-state's whole purpose. So the models survive and MAK simply
        negotiates at runtime until the next refresh fills the field in.
        """
        path = tmp_path / "models.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": version,
                    "last_refresh": "2026-09-01T00:00:00+00:00",
                    "last_attempt": "2026-09-01T00:00:00+00:00",
                    "providers": {
                        "anthropic": {
                            "fetched_at": "2026-09-01T00:00:00+00:00",
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
        entries = manifest.models_for("anthropic")
        assert [e.model_id for e in entries] == ["claude-opus-5"]
        assert entries[0].context_window == 200000, "facts are not lost"
        assert entries[0].supported_parameters is None, "unknown, not empty"

    def test_a_future_schema_degrades_to_the_seed(self, tmp_path: Path) -> None:
        """A newer MAK's cache must not break an older one's startup."""
        path = tmp_path / "models.json"
        path.write_text(
            json.dumps({"schema_version": 99, "providers": {"x": {}}}),
            encoding="utf-8",
        )
        assert load_manifest(path).providers == {}

    def test_a_v3_round_trip_preserves_unknown_and_empty(
        self, tmp_path: Path
    ) -> None:
        """The distinction has to survive the disk, not just the dataclass."""
        path = tmp_path / "models.json"
        save_manifest(
            Manifest(
                providers={
                    "openrouter": ProviderBlock(
                        fetched_at=datetime(2026, 9, 21, tzinfo=UTC),
                        models=(
                            ModelEntry(
                                provider="openrouter",
                                model_id="known",
                                display_name="Known",
                                supported_parameters=PAID_PARAMS,
                            ),
                            ModelEntry(
                                provider="openrouter",
                                model_id="empty",
                                display_name="Empty",
                                supported_parameters=frozenset(),
                            ),
                            ModelEntry(
                                provider="openrouter",
                                model_id="unknown",
                                display_name="Unknown",
                            ),
                        ),
                    )
                }
            ),
            path,
        )
        loaded = {
            e.model_id: e.supported_parameters
            for e in load_manifest(path).models_for("openrouter")
        }
        assert loaded == {
            "known": PAID_PARAMS,
            "empty": frozenset(),
            "unknown": None,
        }

    def test_a_corrupt_cache_still_yields_an_empty_manifest(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "models.json"
        path.write_text("{not json", encoding="utf-8")
        assert load_manifest(path).providers == {}


class TestReportedCapabilitiesLookup:
    """The narrow, read-only view the composition root hands to bootstrap."""

    def test_a_pair_is_found_by_endpoint_and_exact_model_id(self) -> None:
        lookup = ReportedCapabilities.from_entries(
            [
                ModelEntry(
                    provider="openrouter",
                    model_id=MODEL,
                    display_name="free",
                    supported_parameters=FREE_PARAMS,
                )
            ]
        )
        assert lookup.for_model("openrouter", MODEL) == FREE_PARAMS

    def test_an_unknown_pair_is_none(self) -> None:
        assert ReportedCapabilities().for_model("openrouter", MODEL) is None

    def test_variants_are_separate_rows(self) -> None:
        """The capability inversion this whole wave exists for."""
        lookup = ReportedCapabilities.from_entries(
            [
                ModelEntry(
                    provider="openrouter",
                    model_id=MODEL,
                    display_name="free",
                    supported_parameters=FREE_PARAMS,
                ),
                ModelEntry(
                    provider="openrouter",
                    model_id=PAID,
                    display_name="paid",
                    supported_parameters=PAID_PARAMS,
                ),
            ]
        )
        assert lookup.for_model("openrouter", MODEL) == FREE_PARAMS
        assert lookup.for_model("openrouter", PAID) == PAID_PARAMS

    def test_two_endpoints_offering_one_id_are_separate_rows(self) -> None:
        lookup = ReportedCapabilities.from_entries(
            [
                ModelEntry(
                    provider="openrouter",
                    model_id="shared",
                    display_name="a",
                    supported_parameters=FREE_PARAMS,
                ),
                ModelEntry(
                    provider="other",
                    model_id="shared",
                    display_name="b",
                    endpoint_id="other",
                    supported_parameters=PAID_PARAMS,
                ),
            ]
        )
        assert lookup.for_model("openrouter", "shared") == FREE_PARAMS
        assert lookup.for_model("other", "shared") == PAID_PARAMS

    def test_load_is_total(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Seeding is an optimisation; it must never break startup."""
        import mak.models.registry as registry_module

        def explode(*_: object, **__: object) -> object:
            raise OSError("disk on fire")

        monkeypatch.setattr(registry_module, "ModelRegistry", explode)
        assert ReportedCapabilities.load().by_model == {}
