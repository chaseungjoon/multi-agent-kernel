"""Read and persist API keys to/from ``mak/.env``."""
from __future__ import annotations

import os
from pathlib import Path

KEY_NAMES = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY")
_ENV_PATH = Path(__file__).resolve().parent.parent.parent / "mak" / ".env"


def load_keys() -> dict[str, str]:
    keys: dict[str, str] = {k: "" for k in KEY_NAMES}
    if _ENV_PATH.exists():
        for raw in _ENV_PATH.read_text("utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() in keys:
                keys[k.strip()] = v.strip()
    for name in KEY_NAMES:
        if val := os.environ.get(name, ""):
            keys[name] = val
    return keys


def save_keys(keys: dict[str, str]) -> None:
    _ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    _ENV_PATH.write_text(
        "\n".join(f"{n}={keys.get(n, '')}" for n in KEY_NAMES) + "\n",
        encoding="utf-8",
    )
    for name, value in keys.items():
        if value:
            os.environ[name] = value


def any_key_set(keys: dict[str, str]) -> bool:
    return any(bool(v.strip()) for v in keys.values())
