"""Read and persist API keys to/from the user config dir (``~/.config/mak/.env``).

Keys are stored per-user, outside the package, so an installed MAK (``uv tool
install`` / ``pipx``) keeps them across upgrades. A legacy source-checkout
``mak/.env`` is still read (lowest precedence) so existing dev setups keep
working; exported environment variables always win.

The legacy path is **deprecated**. It lives inside the package directory, is
created with no mode enforcement (observed at 0644 with live keys), and only
survives an upgrade by accident. Reading it warns and names the replacement;
the next release drops it.
"""
from __future__ import annotations

import os
import warnings
from pathlib import Path

from mak.config import user_config_dir

KEY_NAMES = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY")
_LEGACY_ENV_PATH = Path(__file__).resolve().parent.parent.parent / "mak" / ".env"

# Owner read/write only. A key file readable by every account on the machine is
# a credential leak whether or not anyone reads it.
_KEY_FILE_MODE = 0o600

LEGACY_ENV_DEPRECATION = (
    "reading API keys from the legacy '{path}' (inside the package directory). "
    "Move them to '{replacement}' — MAK's /apikey setup writes there, with "
    "owner-only permissions. Support for the legacy location is removed in the "
    "next release."
)


def _env_path() -> Path:
    return user_config_dir() / ".env"


def _warn_legacy(path: Path) -> None:
    """Warn that the deprecated in-package ``.env`` supplied a key."""
    warnings.warn(
        LEGACY_ENV_DEPRECATION.format(path=path, replacement=_env_path()),
        DeprecationWarning,
        stacklevel=3,
    )


def _read_env_file(path: Path, keys: dict[str, str]) -> bool:
    """Merge ``KEY=VALUE`` lines from ``path`` into ``keys``; report if any hit."""
    if not path.exists():
        return False
    found = False
    for raw in path.read_text("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() in keys and v.strip():
            keys[k.strip()] = v.strip()
            found = True
    return found


def load_keys() -> dict[str, str]:
    """Return every provider key, later sources winning over earlier ones."""
    keys: dict[str, str] = {k: "" for k in KEY_NAMES}
    if _read_env_file(_LEGACY_ENV_PATH, keys):
        _warn_legacy(_LEGACY_ENV_PATH)
    _read_env_file(_env_path(), keys)
    for name in KEY_NAMES:
        if val := os.environ.get(name, ""):
            keys[name] = val
    return keys


def save_keys(keys: dict[str, str]) -> None:
    """Persist keys to the user config dir and export them into this process.

    Created at 0600 by ``os.open`` rather than written and then ``chmod``-ed: the
    old order left the file at the process umask (0644 on a default macOS or
    Linux account) for the window between the two calls, with the keys already
    in it. The ``chmod`` stays as a repair for a file an older MAK left behind.
    """
    path = _env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"{n}={keys.get(n, '')}" for n in KEY_NAMES) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _KEY_FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)
    try:
        os.chmod(path, _KEY_FILE_MODE)
    except OSError:
        pass
    for name, value in keys.items():
        if value:
            os.environ[name] = value


def any_key_set(keys: dict[str, str]) -> bool:
    """Return whether at least one provider key has a non-blank value."""
    return any(bool(v.strip()) for v in keys.values())
