"""Read and persist API keys to/from the user config dir (``~/.config/mak/.env``).

Keys are stored per-user, outside the package, so an installed MAK (``uv tool
install`` / ``pipx``) keeps them across upgrades. A legacy source-checkout
``mak/.env`` is still read (lowest precedence) so existing dev setups keep
working; exported environment variables always win.

The legacy path is **deprecated**. It lives inside the package directory, is
created with no mode enforcement (observed at 0644 with live keys), and only
survives an upgrade by accident. Reading it warns and names the replacement;
the next release drops it.

**Any variable name, not three.** The name set used to be a fixed
tuple of the three built-in providers. Every custom endpoint brings its own
credential variable, and a gateway may need a secret header as well, so the set
is now whatever the caller asks for.

That change made a latent bug fatal. ``save_keys`` used to render the whole file
from the names it knew, so anything else in it — a comment, an unrelated
variable, another endpoint's key — was destroyed on the next save. With one
fixed set of three that was merely rude; with per-endpoint credentials it would
mean saving one endpoint's key deletes another's. Writes are now
parse-merge-render: every line MAK did not set is preserved verbatim, in place.
"""
from __future__ import annotations

import os
import warnings
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from mak.config import user_config_dir
from mak.core.exceptions import ConfigError
from mak.endpoints.types import EndpointConfig, validate_env_name

# The built-in providers' conventional variables. No longer the *limit* of what
# can be stored — just the names MAK knows about without being told.
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


@dataclass(frozen=True, slots=True)
class EnvLine:
    """One line of the ``.env`` file, remembered well enough to rewrite it.

    ``name`` is empty for a blank line or a comment, whose ``raw`` text is
    reproduced exactly. That is what lets a user keep their own notes in the
    file without MAK eating them on the next save.
    """

    raw: str
    name: str = ""
    value: str = ""


def _env_path() -> Path:
    return user_config_dir() / ".env"


def env_path() -> Path:
    """Return the per-user ``.env`` location (public for status and messages)."""
    return _env_path()


def _warn_legacy(path: Path) -> None:
    """Warn that the deprecated in-package ``.env`` supplied a key."""
    warnings.warn(
        LEGACY_ENV_DEPRECATION.format(path=path, replacement=_env_path()),
        DeprecationWarning,
        stacklevel=3,
    )


def parse_env_file(path: Path) -> list[EnvLine]:
    """Parse ``path`` into lines, keeping comments and unknown entries intact."""
    if not path.exists():
        return []
    lines: list[EnvLine] = []
    try:
        text = path.read_text("utf-8")
    except OSError:
        return []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            lines.append(EnvLine(raw=raw))
            continue
        name, _, value = stripped.partition("=")
        lines.append(EnvLine(raw=raw, name=name.strip(), value=value.strip()))
    return lines


def _read_env_file(path: Path, keys: dict[str, str]) -> bool:
    """Merge ``KEY=VALUE`` lines from ``path`` into ``keys``; report if any hit."""
    found = False
    for line in parse_env_file(path):
        if line.name and line.name in keys and line.value:
            keys[line.name] = line.value
            found = True
    return found


def load_keys(names: tuple[str, ...] = KEY_NAMES) -> dict[str, str]:
    """Return every requested key, later sources winning over earlier ones.

    Order is legacy file, then the user file, then the environment — so an
    exported variable always beats a stored one, which is what lets a user
    override a saved key for a single shell without editing anything.
    """
    keys: dict[str, str] = dict.fromkeys(names, "")
    if _read_env_file(_LEGACY_ENV_PATH, keys):
        _warn_legacy(_LEGACY_ENV_PATH)
    _read_env_file(_env_path(), keys)
    for name in names:
        if val := os.environ.get(name, ""):
            keys[name] = val
    return keys


def load_all_stored() -> dict[str, str]:
    """Return every variable the user file currently holds, whatever its name.

    Used by ``/apikey`` and the endpoint wizard to show what is already set for
    endpoints MAK only learns about at runtime.
    """
    return {
        line.name: line.value
        for line in parse_env_file(_env_path())
        if line.name and line.value
    }


def save_keys(keys: dict[str, str]) -> None:
    """Merge ``keys`` into the user ``.env``, preserving everything else.

    Parse, merge, render — never truncate. A name present in ``keys`` is
    updated in place (or appended if new); a name whose value is empty is
    **removed**; every other line, including comments and variables MAK knows
    nothing about, is reproduced verbatim.

    Created at 0600 by ``os.open`` rather than written and then ``chmod``-ed:
    the old order left the file at the process umask (0644 on a default macOS or
    Linux account) for the window between the two calls, with the keys already
    in it. The ``chmod`` stays as a repair for a file an older MAK left behind.
    Written to a temp file and moved into place, so a crash mid-write cannot
    leave a user with half a credential file.
    """
    for name in keys:
        validate_env_name(name, where="API key name")

    path = _env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = parse_env_file(path)

    rendered: list[str] = []
    written: set[str] = set()
    for line in existing:
        if not line.name or line.name not in keys:
            rendered.append(line.raw)
            continue
        value = keys[line.name].strip()
        written.add(line.name)
        if value:
            rendered.append(f"{line.name}={value}")
        # An empty value removes the line rather than storing a blank, so
        # "remove this key" and "store an empty key" stay distinguishable.
    for name, value in keys.items():
        if name not in written and value.strip():
            rendered.append(f"{name}={value.strip()}")

    body = "\n".join(rendered).rstrip("\n") + "\n"
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _KEY_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.chmod(tmp, _KEY_FILE_MODE)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise ConfigError(f"cannot write API keys to {path}: {exc}") from exc
    try:
        os.chmod(path, _KEY_FILE_MODE)
    except OSError:
        pass

    for name, value in keys.items():
        if value:
            os.environ[name] = value
        else:
            os.environ.pop(name, None)


def any_key_set(keys: dict[str, str]) -> bool:
    """Return whether at least one provider key has a non-blank value."""
    return any(bool(v.strip()) for v in keys.values())


def key_names_for(
    endpoints: Iterable[EndpointConfig] = (),
) -> tuple[str, ...]:
    """Return the built-in key names plus every configured endpoint's.

    The order matters to ``/apikey``, which groups the official providers first
    and custom endpoints after them, in the order they were configured.
    """
    names: list[str] = list(KEY_NAMES)
    for endpoint in endpoints:
        for name in endpoint.secret_env_names():
            if name not in names:
                names.append(name)
    return tuple(names)
