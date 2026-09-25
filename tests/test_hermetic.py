"""The suite never reads the developer's real configuration (D25.4).

``tests/conftest.py`` moves ``HOME`` and ``XDG_CONFIG_HOME`` before any test
module is imported. This guards that: every per-user path MAK derives must land
in a directory the test run owns, never in the real home.
"""
from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest
from cli.core.api_keys import env_path
from cli.core.local_hosts import hosts_path

from mak.config import user_config_dir, user_config_path
from mak.endpoints.store import endpoints_path
from mak.models.manifest import manifest_path


def _owned_roots(
    hermetic_root: Path, tmp_path_factory: pytest.TempPathFactory
) -> tuple[Path, ...]:
    return (hermetic_root.resolve(), tmp_path_factory.getbasetemp().resolve())


@pytest.mark.parametrize(
    "path_fn",
    [
        manifest_path,
        endpoints_path,
        hosts_path,
        env_path,
        user_config_dir,
    ],
)
def test_every_user_path_is_owned_by_the_run(
    path_fn: object,
    hermetic_root: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    assert callable(path_fn)
    path = Path(path_fn()).resolve()
    assert any(path.is_relative_to(root) for root in _owned_roots(
        hermetic_root, tmp_path_factory
    )), path


def test_the_real_home_is_not_home(hermetic_root: Path) -> None:
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    assert Path.home().resolve() != real_home
    assert Path.home().resolve().is_relative_to(hermetic_root.resolve())


def test_no_user_config_is_found_by_default() -> None:
    # A developer's ~/.config/mak/config.yaml must not
    # leak into discovery.
    assert user_config_path() is None
