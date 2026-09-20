"""Per-user endpoint metadata: ``~/.config/mak/endpoints.json``.

The interactive CLI has to be able to add an endpoint that survives a restart,
and neither existing home works:

* rewriting the *packaged* ``config.yaml`` is impossible for an installed MAK
  (``uv tool install`` leaves the package directory read-only) and would be lost
  on upgrade anyway;
* round-tripping the user's ``mak.yaml`` through PyYAML would strip every
  comment and reorder every key in a file they hand-wrote.

So endpoints created in the TUI live here, beside ``.env`` and ``models.json``,
in the same per-user directory and with the same discipline:

**Reads are total.** A missing, unreadable, corrupt or future-schema file yields
an *empty* store plus a :class:`StoreDiagnostic`, never an exception. A broken
cache must degrade to "you have no saved endpoints", never break startup — the
user still has to be able to launch MAK to fix it, and ``/endpoint`` is what
surfaces the diagnostic.

**Writes are atomic and owner-only.** Temp file in the same directory, then
``os.replace``. Mode 0600 at ``os.open`` rather than a later ``chmod``, because
the gap between the two leaves the file at the process umask with its contents
already written. This file holds no credential *values*, but it does hold
internal hostnames and public header values, which are not for every account on
a shared machine.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mak.config import user_config_dir
from mak.core.exceptions import ConfigError
from mak.endpoints.parse import parse_endpoint
from mak.endpoints.types import EndpointConfig

SCHEMA_VERSION = 1

# Owner read/write only. See the module docstring.
_STORE_FILE_MODE = 0o600


@dataclass(frozen=True, slots=True)
class StoreDiagnostic:
    """Why a read returned an empty store, for ``/endpoint`` to surface.

    Carried rather than raised: the CLI must still start. ``path`` is included
    because the fix is almost always "delete or repair that file", and the user
    cannot do that without being told where it is.
    """

    path: Path
    reason: str

    def message(self) -> str:
        """Return the one-line warning shown by ``/endpoint``."""
        return (
            f"saved endpoints could not be read from {self.path}: {self.reason}. "
            "MAK is running with no user endpoints; fix or delete that file to "
            "restore them."
        )


def endpoints_path() -> Path:
    """Return the per-user endpoint store location."""
    return user_config_dir() / "endpoints.json"


def load_user_endpoints(
    path: Path | None = None,
) -> tuple[tuple[EndpointConfig, ...], StoreDiagnostic | None]:
    """Load saved endpoints, returning ``(endpoints, diagnostic)``.

    Never raises. An entry that fails validation is skipped rather than
    poisoning the whole file — one endpoint edited by hand into an invalid
    state must not hide the other five.
    """
    target = path if path is not None else endpoints_path()
    try:
        raw: Any = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return (), None
    except OSError as exc:
        return (), StoreDiagnostic(target, f"cannot read the file ({exc})")
    except json.JSONDecodeError as exc:
        return (), StoreDiagnostic(target, f"not valid JSON ({exc})")

    if not isinstance(raw, dict):
        return (), StoreDiagnostic(target, "the file is not a JSON object")
    version = raw.get("schema_version")
    if version != SCHEMA_VERSION:
        return (), StoreDiagnostic(
            target,
            f"schema version {version!r} is not {SCHEMA_VERSION} (written by a "
            "different version of MAK)",
        )

    entries = raw.get("endpoints")
    if not isinstance(entries, list):
        return (), StoreDiagnostic(target, "'endpoints' is not a list")

    endpoints: list[EndpointConfig] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            skipped.append("(non-mapping entry)")
            continue
        try:
            endpoint = parse_endpoint(entry)
        except ConfigError as exc:
            skipped.append(f"{entry.get('id', '?')} ({exc})")
            continue
        if endpoint.id in seen:
            skipped.append(f"{endpoint.id} (duplicate id)")
            continue
        seen.add(endpoint.id)
        endpoints.append(endpoint)

    diagnostic = (
        StoreDiagnostic(target, f"skipped invalid entries: {'; '.join(skipped)}")
        if skipped
        else None
    )
    return tuple(endpoints), diagnostic


def save_user_endpoints(
    endpoints: tuple[EndpointConfig, ...], path: Path | None = None
) -> None:
    """Write the endpoint store atomically with owner-only permissions.

    Raises ``ConfigError`` on an I/O failure rather than failing silently: this
    is called from a wizard that has just told the user their endpoint is
    saved, and a silent failure would make that a lie.
    """
    target = path if path is not None else endpoints_path()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "endpoints": [to_dict(e) for e in endpoints],
    }
    body = json.dumps(payload, indent=2) + "\n"
    tmp = target.with_name(f"{target.name}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _STORE_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
        # Repair the mode on a temp file an older MAK may have left behind with
        # a looser one; ``os.open`` above only sets it on creation.
        os.chmod(tmp, _STORE_FILE_MODE)
        os.replace(tmp, target)
    except OSError as exc:
        # The cleanup must not mask the real failure: when the parent is not a
        # directory at all, ``unlink`` raises NotADirectoryError rather than the
        # FileNotFoundError ``missing_ok`` covers, and that would surface
        # instead of the message naming what actually went wrong.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise ConfigError(f"cannot write saved endpoints to {target}: {exc}") from exc


def to_dict(endpoint: EndpointConfig) -> dict[str, Any]:
    """Serialize an endpoint to the store's JSON shape.

    Only settings are written. There is no branch here that could emit a
    credential, because :class:`EndpointConfig` never holds one — it carries the
    *name* of the variable, which is the whole point of that design.
    """
    data: dict[str, Any] = {
        "id": endpoint.id,
        "transport": endpoint.transport.value,
        "location": endpoint.location.value,
    }
    if endpoint.base_url:
        data["base_url"] = endpoint.base_url
    if endpoint.api_key_env:
        data["api_key_env"] = endpoint.api_key_env
    if endpoint.profile:
        data["profile"] = endpoint.profile
    if endpoint.display_name and endpoint.display_name != endpoint.id:
        data["display_name"] = endpoint.display_name
    for name in (
        "model_discovery",
        "health_check",
        "structured_output",
        "token_parameter",
    ):
        value = getattr(endpoint, name)
        # An unset capability is *omitted*, not written as null: writing it
        # would freeze today's profile default into the user's file, so a later
        # correction to the profile would never reach them.
        if value is not None:
            data[name] = value.value
    if endpoint.headers:
        data["headers"] = [
            {"name": h.name, **({"value": h.value} if h.value is not None else {}),
             **({"value_env": h.value_env} if h.value_env is not None else {})}
            for h in endpoint.headers
        ]
    return data


def merge_endpoints(
    project: tuple[EndpointConfig, ...], user: tuple[EndpointConfig, ...]
) -> tuple[EndpointConfig, ...]:
    """Combine project-declared and user-saved endpoints, refusing a clash.

    A duplicate id across the two sources is a ``ConfigError`` naming both — not
    an implicit precedence rule. Silently preferring one source would let a
    project file redirect a user's saved credential variable to a different
    host, or the reverse, and neither is something a user can see happening.
    """
    by_id = {e.id: e for e in project}
    clashes = sorted(e.id for e in user if e.id in by_id)
    if clashes:
        raise ConfigError(
            f"endpoint id(s) {', '.join(clashes)} are defined both in this "
            f"project's config and in your saved endpoints ({endpoints_path()}). "
            "MAK will not choose between them, because the two may point at "
            "different hosts. Rename one, or remove it with "
            f"'/endpoint remove {clashes[0]}'."
        )
    return (*project, *user)
