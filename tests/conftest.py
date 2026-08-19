"""Suite-wide fixtures that keep tests independent of the developer's machine."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import cli.core.api_keys as api_keys
import pytest

import mak.__main__ as mak_main


@pytest.fixture(autouse=True)
def _isolate_env_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Path]:
    """Point both ``.env`` lookups at an empty temp dir for every test.

    MAK reads a user-level ``~/.config/mak/.env`` and a deprecated in-package
    ``mak/.env``. A developer checkout normally has the latter — with live keys —
    so without this the suite's assertions about stderr, warnings, and which keys
    are configured depend on whose machine it runs on.
    """
    root = tmp_path_factory.mktemp("env-isolation")
    monkeypatch.setattr(api_keys, "_LEGACY_ENV_PATH", root / "legacy.env")
    monkeypatch.setattr(mak_main, "_legacy_env_path", lambda: root / "legacy.env")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "config"))
    yield root
