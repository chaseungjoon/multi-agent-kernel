"""Read and persist API keys to/from the user config dir (``~/.config/mak/.env``).

Keys are stored per-user, outside the package, so an installed MAK (``uv tool
install`` / ``pipx``) keeps them across upgrades. A legacy source-checkout
``mak/.env`` is still read (lowest precedence) so existing dev setups keep
working; exported environment variables always win.
"""
from __future__ import annotations

import os
from pathlib import Path

from mak.config import user_config_dir

KEY_NAMES = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY")
_LEGACY_ENV_PATH = Path(__file__).resolve().parent.parent.parent / "mak" / ".env"


def _env_path() -> Path:
    return user_config_dir() / ".env"


def _read_env_file(path: Path, keys: dict[str, str]) -> None:
    if not path.exists():
        return
    for raw in path.read_text("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() in keys and v.strip():
            keys[k.strip()] = v.strip()


def load_keys() -> dict[str, str]:
    keys: dict[str, str] = {k: "" for k in KEY_NAMES}
    _read_env_file(_LEGACY_ENV_PATH, keys)
    _read_env_file(_env_path(), keys)
    for name in KEY_NAMES:
        if val := os.environ.get(name, ""):
            keys[name] = val
    return keys


def save_keys(keys: dict[str, str]) -> None:
    path = _env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(f"{n}={keys.get(n, '')}" for n in KEY_NAMES) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(path, 0o600)  # keys file: owner read/write only
    except OSError:
        pass
    for name, value in keys.items():
        if value:
            os.environ[name] = value


def any_key_set(keys: dict[str, str]) -> bool:
    return any(bool(v.strip()) for v in keys.values())
