"""Parse the ``endpoints:`` YAML section into validated ``EndpointConfig``s.

Kept out of ``mak/config.py`` deliberately: that module already parses eight
sections and is the file most likely to accumulate unrelated logic. Endpoint
parsing is one concern with its own validation surface, so it gets its own file
and ``mak.config`` calls into it.

Every error names the exact endpoint id and setting, because these messages are
read by someone editing a YAML file and "invalid value" tells them nothing
about which of six endpoints to look at.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, TypeVar
from urllib.parse import urlsplit

from mak.core.exceptions import ConfigError
from mak.endpoints.profiles import is_reserved, profile_for
from mak.endpoints.types import (
    EndpointConfig,
    EndpointHeaderConfig,
    HealthPolicy,
    Location,
    ModelDiscovery,
    StructuredOutput,
    TokenParameter,
    Transport,
    validate_endpoint_id,
)

# Hostnames that mean "this machine". A URL on one of these may use plain HTTP
# without a confirmation, because the traffic never reaches a network.
LOOPBACK_HOSTS: frozenset[str] = frozenset(
    {"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"}
)


_EnumT = TypeVar("_EnumT", bound=StrEnum)


def _enum(
    raw: dict[str, Any],
    key: str,
    enum_cls: type[_EnumT],
    *,
    where: str,
) -> _EnumT | None:
    """Return an optional enum setting, or None when the key is absent.

    Absent is **not** the same as an explicit value equal to the enum's
    ``none`` member: the first defers to the profile, the second is a statement
    that the service does not support the feature.
    """
    if key not in raw or raw[key] is None:
        return None
    text = str(raw[key]).strip().lower()
    try:
        return enum_cls(text)
    except ValueError:
        allowed = ", ".join(m.value for m in enum_cls)
        raise ConfigError(
            f"{where} '{key}' must be one of {allowed}, got {raw[key]!r}"
        ) from None


def is_loopback(url: str) -> bool:
    """Return whether ``url``'s host is the loopback interface."""
    host = (urlsplit(url).hostname or "").lower()
    return host in LOOPBACK_HOSTS


def is_private_host(url: str) -> bool:
    """Return whether ``url``'s host is on a private/LAN address range.

    Used only to *suggest* a location in the wizard. The stored value is always
    explicit — guessing is how a LAN gateway ends up labelled "local" and the
    privacy note lies.
    """
    import ipaddress

    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # A bare hostname with no dots is almost always an internal name.
        return "." not in host and host not in LOOPBACK_HOSTS
    return bool(address.is_private and not address.is_loopback)


def validate_endpoint_url(
    url: str, *, endpoint_id: str, location: Location, allow_insecure: bool = False
) -> str:
    """Return a validated endpoint base URL, or raise ``ConfigError``.

    Beyond ``mak.config.normalize_base_url``'s scheme/host checks this adds the
    rules an endpoint needs and a bare local URL did not:

    * **no fragment** — an endpoint is a request target, not a document anchor,
      and a ``#`` is almost always a mis-paste;
    * **no userinfo** — ``https://user:pass@host/v1`` puts a credential in a
      file MAK writes to disk and prints in status output, which is exactly what
      ``api_key_env`` exists to prevent;
    * **HTTPS for hosted** — plaintext to a public host sends the key in the
      clear. Loopback and private hosts may use HTTP, with ``allow_insecure``
      recording that the user was told.
    """
    parts = urlsplit(url)
    where = f"endpoint '{endpoint_id}' 'base_url'"
    if parts.fragment:
        raise ConfigError(
            f"{where} must not contain a '#' fragment, got {url!r}"
        )
    if parts.username or parts.password:
        raise ConfigError(
            f"{where} must not embed credentials (user:password@host). Name the "
            "variable holding the key in 'api_key_env' instead — MAK never "
            "stores a secret in config or endpoint metadata."
        )
    if parts.scheme == "http" and location is Location.HOSTED:
        raise ConfigError(
            f"{where} uses plain http:// for a hosted endpoint, which sends the "
            "API key in the clear. Use https://, or set 'location' to local or "
            "private if this really is a server on your own network."
        )
    if (
        parts.scheme == "http"
        and not allow_insecure
        and not is_loopback(url)
        and location is Location.LOCAL
    ):
        raise ConfigError(
            f"{where} uses plain http:// but is not on this machine; set "
            "'location: private' for a server on your network, and confirm the "
            "insecure transport."
        )
    return url


def parse_endpoint(raw: dict[str, Any]) -> EndpointConfig:
    """Build one ``EndpointConfig`` from a YAML mapping.

    Applies the profile's defaults first, then the entry's explicit settings, so
    a preset can be adopted wholesale (``{id: nvidia, profile: nvidia}``) or
    overridden field by field.
    """
    if "id" not in raw:
        raise ConfigError("each endpoint entry must have an 'id' field")
    endpoint_id = validate_endpoint_id(str(raw["id"]))
    if is_reserved(endpoint_id):
        raise ConfigError(
            f"endpoint id '{endpoint_id}' is reserved for MAK's built-in "
            "provider; choose another id (e.g. "
            f"'{endpoint_id}-gateway') so '--models {endpoint_id}:<model>' keeps "
            "one unambiguous meaning"
        )

    profile_id = raw.get("profile")
    profile = profile_for(str(profile_id)) if profile_id else None
    if profile_id and profile is None:
        from mak.endpoints.profiles import profile_ids

        raise ConfigError(
            f"endpoint '{endpoint_id}' names unknown profile {profile_id!r}; "
            f"known profiles: {', '.join(profile_ids())}"
        )

    base = profile.to_endpoint(endpoint_id) if profile else None
    where = f"endpoint '{endpoint_id}'"

    transport = _enum(raw, "transport", Transport, where=where)
    if transport is None:
        transport = base.transport if base else Transport.OPENAI_CHAT

    location = _enum(raw, "location", Location, where=where)
    if location is None:
        location = base.location if base else Location.HOSTED

    base_url = raw.get("base_url")
    url = str(base_url).strip() if base_url else (base.base_url if base else None)
    if url:
        from mak.config import normalize_base_url

        url = normalize_base_url(url, where=f"{where} 'base_url'")
        url = validate_endpoint_url(url, endpoint_id=endpoint_id, location=location)

    key_env = raw.get("api_key_env")
    api_key_env = (
        str(key_env).strip() if key_env else (base.api_key_env if base else None)
    )

    return EndpointConfig(
        id=endpoint_id,
        transport=transport,
        base_url=url,
        api_key_env=api_key_env,
        location=location,
        profile=profile.id if profile else None,
        display_name=str(
            raw.get("display_name") or (base.display_name if base else "") or ""
        ),
        model_discovery=_enum(raw, "model_discovery", ModelDiscovery, where=where)
        or (base.model_discovery if base else None),
        health_check=_enum(raw, "health_check", HealthPolicy, where=where)
        or (base.health_check if base else None),
        structured_output=_enum(
            raw, "structured_output", StructuredOutput, where=where
        )
        or (base.structured_output if base else None),
        token_parameter=_enum(raw, "token_parameter", TokenParameter, where=where)
        or (base.token_parameter if base else None),
        headers=_parse_headers(raw.get("headers"), endpoint_id=endpoint_id),
    )


def _parse_headers(
    raw: object, *, endpoint_id: str
) -> tuple[EndpointHeaderConfig, ...]:
    """Parse the optional ``headers:`` list of ``{name, value|value_env}``."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(
            f"endpoint '{endpoint_id}' 'headers' must be a list of "
            "{name, value} or {name, value_env} entries"
        )
    headers: list[EndpointHeaderConfig] = []
    for entry in raw:
        if not isinstance(entry, dict) or "name" not in entry:
            raise ConfigError(
                f"endpoint '{endpoint_id}' has a header entry with no 'name'"
            )
        value = entry.get("value")
        value_env = entry.get("value_env")
        headers.append(
            EndpointHeaderConfig(
                name=str(entry["name"]),
                value=None if value is None else str(value),
                value_env=None if value_env is None else str(value_env),
            )
        )
    return tuple(headers)


def parse_endpoints(raw: object) -> tuple[EndpointConfig, ...]:
    """Parse the whole ``endpoints:`` section, rejecting duplicate ids."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("'endpoints' must be a list of endpoint entries")
    endpoints: list[EndpointConfig] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise ConfigError("each endpoint entry must be a mapping")
        endpoint = parse_endpoint(entry)
        if endpoint.id in seen:
            raise ConfigError(
                f"endpoint id '{endpoint.id}' is defined more than once; ids "
                "must be unique because CLI specs and the planner reference them"
            )
        seen.add(endpoint.id)
        endpoints.append(endpoint)
    return tuple(endpoints)
