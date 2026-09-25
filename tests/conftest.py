"""Suite-wide isolation that keeps tests independent of the developer's machine."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

_HERMETIC_ROOT = pytest.StashKey[Path]()


def pytest_configure(config: pytest.Config) -> None:
    """Point ``HOME`` and ``XDG_CONFIG_HOME`` at a throwaway dir for the whole run.

    Runs before any test module is imported, which a fixture cannot: an
    import-time read of ``~/.config/mak/models.json`` once made a test pass or
    fail depending on whose machine ran it. From here on, every per-user path
    MAK derives — the model manifest, the endpoint store, ``local_hosts.json``,
    the user ``.env`` and ``config.yaml`` — lands under this directory.
    """
    root = Path(tempfile.mkdtemp(prefix="mak-tests-home-"))
    (root / "home").mkdir()
    os.environ["HOME"] = str(root / "home")
    os.environ["XDG_CONFIG_HOME"] = str(root / "config")
    config.stash[_HERMETIC_ROOT] = root


def pytest_unconfigure(config: pytest.Config) -> None:
    """Remove the throwaway home ``pytest_configure`` created."""
    root = config.stash.get(_HERMETIC_ROOT, None)
    if root is not None:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="session")
def hermetic_root(pytestconfig: pytest.Config) -> Path:
    """Return the directory ``pytest_configure`` moved ``HOME`` under."""
    return pytestconfig.stash[_HERMETIC_ROOT]


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
    import cli.core.api_keys as api_keys

    import mak.application.env as app_env

    root = tmp_path_factory.mktemp("env-isolation")
    monkeypatch.setattr(api_keys, "_LEGACY_ENV_PATH", root / "legacy.env")
    monkeypatch.setattr(app_env, "_legacy_env_path", lambda: root / "legacy.env")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "config"))
    yield root
