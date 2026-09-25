"""MAK's ``.env`` files: read them as data, or load them into the process.

Two front ends, two needs. ``mak run`` is a one-shot process whose adapters read
``os.environ``, so it loads the files into it (:func:`load_env_file`). The
interactive app is long-lived and holds its keys in ``CliState``; it reads the
files as a mapping (:func:`read_env_files`) and passes the merged result down as
an explicit ``env``, so a session built there never changes the process
environment another session — or the user's shell tooling — later sees.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

from mak.config import user_config_dir


def _legacy_env_path() -> Path:
    """Return the deprecated in-package ``.env`` location (``mak/.env``)."""
    return Path(__file__).resolve().parent.parent / ".env"


def _parse_env_file(env_path: Path) -> dict[str, str] | None:
    """Return one file's ``KEY=VALUE`` pairs, or None if the file is absent."""
    if not env_path.exists():
        return None
    values: dict[str, str] = {}
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values.setdefault(key.strip(), value.strip())
    return values


def _warn_legacy(legacy: Path) -> None:
    """Tell the user a key came from the deprecated in-package file."""
    print(
        f"mak: warning: read API keys from the legacy {legacy} (inside the "
        f"package directory). Move them to {user_config_dir() / '.env'} — "
        "MAK writes there with owner-only permissions. The legacy location "
        "is removed in the next release.",
        file=sys.stderr,
    )


def read_env_files(
    *, warn: Callable[[Path], None] = _warn_legacy
) -> dict[str, str]:
    """Return the variables MAK's ``.env`` files define, earlier files winning.

    1. ``<user config dir>/.env`` (e.g. ``~/.config/mak/.env``) — where an
       installed MAK's ``/apikey`` setup stores keys.
    2. ``mak/.env`` next to the package — the legacy source-checkout location,
       **deprecated**: it sits inside the package directory and nothing
       enforces its mode, so a working copy is routinely left world-readable
       with live keys in it. Reading it calls ``warn``.

    Pure with respect to the environment: nothing is exported.
    """
    merged: dict[str, str] = dict(_parse_env_file(user_config_dir() / ".env") or {})
    legacy = _legacy_env_path()
    legacy_values = _parse_env_file(legacy)
    if legacy_values is not None:
        warn(legacy)
        for key, value in legacy_values.items():
            merged.setdefault(key, value)
    return merged


def load_env_file(path: Path | None = None) -> None:
    """Load MAK ``.env`` files (``KEY=VALUE`` lines) into ``os.environ``.

    No external dependency. Already-exported variables win (``setdefault``), so
    an explicit ``export`` overrides any file. With an explicit ``path`` only
    that file is read; otherwise the files :func:`read_env_files` names are.

    For the one-shot ``mak run`` process only — the interactive app passes an
    explicit ``env`` instead of mutating the process environment.
    """
    values = (
        _parse_env_file(path) or {} if path is not None else read_env_files()
    )
    for key, value in values.items():
        os.environ.setdefault(key, value)
