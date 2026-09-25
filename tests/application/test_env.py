"""``.env`` files as data (the app) versus loaded into the process (``mak run``)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import mak.application.env as app_env
from mak.config import user_config_dir


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TestReadEnvFiles:
    def test_reads_the_user_file_without_exporting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MAK_TEST_ONLY_KEY", raising=False)
        _write(user_config_dir() / ".env", "# comment\nMAK_TEST_ONLY_KEY=v1\n")
        assert app_env.read_env_files()["MAK_TEST_ONLY_KEY"] == "v1"
        assert "MAK_TEST_ONLY_KEY" not in os.environ

    def test_the_user_file_beats_the_legacy_one_and_warns(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        legacy = tmp_path / "legacy.env"
        monkeypatch.setattr(app_env, "_legacy_env_path", lambda: legacy)
        _write(user_config_dir() / ".env", "K=user\n")
        _write(legacy, "K=legacy\nL=legacy-only\n")
        warned: list[Path] = []
        values = app_env.read_env_files(warn=warned.append)
        assert values == {"K": "user", "L": "legacy-only"}
        assert warned == [legacy]


class TestLoadEnvFile:
    def test_exports_but_an_existing_export_wins(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("MAK_TEST_A", "exported")
        monkeypatch.delenv("MAK_TEST_B", raising=False)
        path = tmp_path / "x.env"
        _write(path, "MAK_TEST_A=file\nMAK_TEST_B=file\n")
        app_env.load_env_file(path)
        assert os.environ["MAK_TEST_A"] == "exported"
        assert os.environ["MAK_TEST_B"] == "file"
        monkeypatch.delenv("MAK_TEST_B")
