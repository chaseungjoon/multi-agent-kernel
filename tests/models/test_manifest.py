from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mak.models.catalog import ModelEntry
from mak.models.manifest import (
    SCHEMA_VERSION,
    Manifest,
    ProviderBlock,
    is_in_cooldown,
    is_refresh_due,
    last_scheduled_tick,
    load_manifest,
    save_manifest,
)


def _dt(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


class TestPersistence:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "models.json"
        manifest = Manifest(
            last_refresh=_dt(2026, 7, 15),
            last_attempt=_dt(2026, 7, 15),
            providers={
                "anthropic": ProviderBlock(
                    fetched_at=_dt(2026, 7, 15),
                    models=(
                        ModelEntry(
                            provider="anthropic",
                            model_id="claude-opus-5",
                            display_name="Claude Opus 5",
                            context_window=1_000_000,
                        ),
                    ),
                )
            },
        )
        save_manifest(manifest, path)
        loaded = load_manifest(path)

        assert loaded.last_refresh == manifest.last_refresh
        assert loaded.models_for("anthropic")[0].model_id == "claude-opus-5"
        assert loaded.models_for("anthropic")[0].context_window == 1_000_000

    def test_missing_file_is_empty(self, tmp_path: Path) -> None:
        loaded = load_manifest(tmp_path / "nope.json")
        assert loaded == Manifest()
        assert loaded.models_for("anthropic") == ()

    def test_corrupt_json_is_empty_not_raise(self, tmp_path: Path) -> None:
        path = tmp_path / "models.json"
        path.write_text("{not json at all", encoding="utf-8")
        assert load_manifest(path) == Manifest()

    def test_non_object_json_is_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "models.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        assert load_manifest(path) == Manifest()

    def test_wrong_schema_version_is_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "models.json"
        path.write_text(
            f'{{"schema_version": {SCHEMA_VERSION + 99}, "providers": {{}}}}',
            encoding="utf-8",
        )
        assert load_manifest(path) == Manifest()

    def test_save_is_atomic_and_leaves_no_temp(self, tmp_path: Path) -> None:
        path = tmp_path / "models.json"
        save_manifest(Manifest(last_refresh=_dt(2026, 7, 1)), path)
        save_manifest(Manifest(last_refresh=_dt(2026, 7, 15)), path)
        assert list(tmp_path.iterdir()) == [path]
        assert load_manifest(path).last_refresh == _dt(2026, 7, 15)

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "nested" / "models.json"
        save_manifest(Manifest(), path)
        assert path.is_file()

    def test_naive_timestamps_are_treated_as_utc(self, tmp_path: Path) -> None:
        path = tmp_path / "models.json"
        path.write_text(
            f'{{"schema_version": {SCHEMA_VERSION}, '
            '"last_refresh": "2026-07-15T00:00:00", "providers": {}}',
            encoding="utf-8",
        )
        loaded = load_manifest(path)
        assert loaded.last_refresh is not None
        assert loaded.last_refresh.tzinfo is not None

    def test_garbage_model_entries_are_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "models.json"
        path.write_text(
            f'{{"schema_version": {SCHEMA_VERSION}, "providers": '
            '{"openai": {"models": [{"provider": "openai", "model_id": "ok"}, '
            '"junk", {"no_id": true}]}}}',
            encoding="utf-8",
        )
        models = load_manifest(path).models_for("openai")
        assert [m.model_id for m in models] == ["ok"]

    def test_has_reports_cached_providers(self, tmp_path: Path) -> None:
        path = tmp_path / "models.json"
        save_manifest(
            Manifest(providers={"openai": ProviderBlock(models=())}), path
        )
        loaded = load_manifest(path)
        assert loaded.has("openai") is True
        assert loaded.has("gemini") is False


class TestSchedule:
    @pytest.mark.parametrize(
        ("now", "expected_day", "expected_month"),
        [
            (_dt(2026, 7, 1), 1, 7),
            (_dt(2026, 7, 14), 1, 7),
            (_dt(2026, 7, 15), 15, 7),
            (_dt(2026, 7, 31), 15, 7),
        ],
    )
    def test_last_tick(
        self, now: datetime, expected_day: int, expected_month: int
    ) -> None:
        tick = last_scheduled_tick(now)
        assert (tick.day, tick.month) == (expected_day, expected_month)

    def test_tick_rolls_into_previous_month(self) -> None:
        # Midnight on the 1st has already passed at 12:00 on the 1st, but a
        # timestamp before that rolls back to the previous month's 15th.
        tick = last_scheduled_tick(_dt(2026, 7, 1, hour=0))
        assert (tick.month, tick.day) == (7, 1)

    def test_never_refreshed_is_due(self) -> None:
        assert is_refresh_due(None, _dt(2026, 7, 20)) is True

    def test_refreshed_today_not_due(self) -> None:
        assert is_refresh_due(_dt(2026, 7, 1, 9), _dt(2026, 7, 1, 10)) is False

    def test_refreshed_first_now_second_not_due(self) -> None:
        assert is_refresh_due(_dt(2026, 7, 1), _dt(2026, 7, 2)) is False

    def test_refreshed_first_now_fifteenth_is_due(self) -> None:
        assert is_refresh_due(_dt(2026, 7, 1), _dt(2026, 7, 15)) is True

    def test_refreshed_fifteenth_now_sixteenth_not_due(self) -> None:
        assert is_refresh_due(_dt(2026, 7, 15), _dt(2026, 7, 16)) is False

    def test_missed_tick_catches_up_late(self) -> None:
        """Skipping the 15th means refreshing on the 19th, not waiting for the 1st."""
        assert is_refresh_due(_dt(2026, 7, 1), _dt(2026, 7, 19)) is True

    def test_month_rollover_is_due(self) -> None:
        assert is_refresh_due(_dt(2026, 6, 20), _dt(2026, 7, 3)) is True

    def test_year_rollover_is_due(self) -> None:
        assert is_refresh_due(_dt(2025, 12, 20), _dt(2026, 1, 4)) is True

    def test_seventeenth_release_is_not_covered_by_schedule(self) -> None:
        """The 17th case is /refresh-models' job, not the schedule's."""
        assert is_refresh_due(_dt(2026, 7, 15), _dt(2026, 7, 17)) is False


class TestCooldown:
    def test_no_previous_attempt_is_not_cooling_down(self) -> None:
        assert is_in_cooldown(None, _dt(2026, 7, 20)) is False

    def test_recent_attempt_blocks(self) -> None:
        now = _dt(2026, 7, 20, 12)
        assert is_in_cooldown(now - timedelta(hours=1), now) is True

    def test_old_attempt_expires(self) -> None:
        now = _dt(2026, 7, 20, 12)
        assert is_in_cooldown(now - timedelta(hours=7), now) is False
