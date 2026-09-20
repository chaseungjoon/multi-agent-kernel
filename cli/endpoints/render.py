"""Turn endpoints into the lines a terminal shows.

Separated from the command handlers so that "what an endpoint looks like" is
decided in one place and tested without driving a command. Everything here is
secret-free by construction: it renders *names* of credential variables and
whether they are set, never values, and it sanitizes URLs before printing them.
"""

from __future__ import annotations

from rich.console import Console

from cli.core.api_keys import load_all_stored
from mak.endpoints.resolution import ResolvedEndpoint
from mak.endpoints.types import EndpointConfig, Location

# How many models a bare ``/endpoint models <id>`` will print. OpenRouter alone
# serves several hundred; dumping them into scrollback destroys the session's
# history and helps nobody. Past this the user is asked to filter.
MODEL_LIST_CAP = 50

_LOCATION_LABEL: dict[Location, str] = {
    Location.HOSTED: "hosted",
    Location.LOCAL: "local",
    Location.PRIVATE: "private",
}

# Colour by where the traffic goes, because that is the property a user scanning
# this list is actually checking.
_LOCATION_STYLE: dict[Location, str] = {
    Location.HOSTED: "yellow",
    Location.LOCAL: "green",
    Location.PRIVATE: "cyan",
}


def location_label(location: Location) -> str:
    """Return the short word for an endpoint's location."""
    return _LOCATION_LABEL.get(location, str(location))


def key_state(endpoint: EndpointConfig, env: dict[str, str] | None = None) -> str:
    """Return whether this endpoint's credential is set, never what it is."""
    if not endpoint.api_key_env:
        return "no key needed"
    stored = env if env is not None else load_all_stored()
    import os

    present = bool(os.environ.get(endpoint.api_key_env) or stored.get(
        endpoint.api_key_env
    ))
    return f"{endpoint.api_key_env}: {'set' if present else 'unset'}"


def endpoint_line(
    endpoint: EndpointConfig, *, models: int = 0, probe: str = ""
) -> str:
    """Return one ``/endpoint list`` row."""
    style = _LOCATION_STYLE.get(endpoint.location, "white")
    where = location_label(endpoint.location)
    url = endpoint.sanitized_base_url() or "(sdk default)"
    parts = [
        f"[bold]{endpoint.id}[/bold]",
        f"[{style}]{where}[/{style}]",
        f"[dim]{url}[/dim]",
        key_state(endpoint),
    ]
    if models:
        parts.append(f"{models} model{'' if models == 1 else 's'}")
    if probe:
        parts.append(f"[dim]{probe}[/dim]")
    return "  ·  ".join(parts)


def print_list(
    console: Console,
    endpoints: tuple[EndpointConfig, ...],
    *,
    model_counts: dict[str, int] | None = None,
    probes: dict[str, str] | None = None,
) -> None:
    """Print every configured endpoint, or a line explaining there are none."""
    if not endpoints:
        console.print(
            "\n  [dim]No endpoints configured. "
            "'/endpoint add' sets one up.[/dim]\n"
        )
        return
    counts = model_counts or {}
    states = probes or {}
    console.print()
    for endpoint in endpoints:
        console.print(
            "  "
            + endpoint_line(
                endpoint,
                models=counts.get(endpoint.id, 0),
                probe=states.get(endpoint.id, ""),
            )
        )
    console.print()


def print_show(console: Console, endpoint: EndpointConfig) -> None:
    """Print every non-secret setting of one endpoint, plus header names."""
    console.print(
        f"\n  [bold]{endpoint.display_name}[/bold]  [dim]({endpoint.id})[/dim]"
    )
    rows: list[tuple[str, str]] = [
        ("profile", endpoint.profile or "(none)"),
        ("transport", endpoint.transport.value),
        ("location", location_label(endpoint.location)),
        ("base URL", endpoint.sanitized_base_url() or "(sdk default)"),
        ("credential", key_state(endpoint)),
        ("model discovery", _unset(endpoint.model_discovery)),
        ("health check", _unset(endpoint.health_check)),
        ("structured output", _unset(endpoint.structured_output)),
        ("token parameter", _unset(endpoint.token_parameter)),
    ]
    width = max(len(name) for name, _ in rows)
    for name, value in rows:
        console.print(f"    [dim]{name.rjust(width)}[/dim]  {value}")
    if endpoint.headers:
        console.print(f"    [dim]{'headers'.rjust(width)}[/dim]  ", end="")
        # Names only. A literal header value may be public, but a user reading
        # this over someone's shoulder should not learn which ones are secret
        # by seeing one of them printed.
        console.print(
            ", ".join(
                f"{h.name}{' (from env)' if h.is_secret else ''}"
                for h in endpoint.headers
            )
        )
    console.print()


def _unset(value: object) -> str:
    """Render a tri-state capability: an explicit value, or where it comes from."""
    return str(value.value) if hasattr(value, "value") else "(profile default)"


def export_yaml(endpoint: EndpointConfig) -> str:
    """Return a pasteable, secret-free ``endpoints:`` entry for this endpoint.

    What it deliberately contains: the URL, the credential *variable name*, the
    location and any capability the user set explicitly. What it deliberately
    omits: the key, any secret header value, and capabilities left to the
    profile — writing those would freeze today's defaults into a file that
    outlives them.
    """
    lines = [
        "endpoints:",
        f"  - id: {endpoint.id!r}",
    ]
    if endpoint.profile:
        lines.append(f"    profile: {endpoint.profile!r}")
    if endpoint.display_name and endpoint.display_name != endpoint.id:
        lines.append(f"    display_name: {endpoint.display_name!r}")
    lines.append(f"    transport: {endpoint.transport.value!r}")
    if endpoint.base_url:
        lines.append(f"    base_url: {endpoint.base_url!r}")
    if endpoint.api_key_env:
        lines.append(f"    api_key_env: {endpoint.api_key_env!r}")
    lines.append(f"    location: {endpoint.location.value!r}")
    for name in (
        "model_discovery",
        "health_check",
        "structured_output",
        "token_parameter",
    ):
        value = getattr(endpoint, name)
        if value is not None:
            lines.append(f"    {name}: {value.value!r}")
    if endpoint.headers:
        lines.append("    headers:")
        for header in endpoint.headers:
            lines.append(f"      - name: {header.name!r}")
            if header.value is not None:
                lines.append(f"        value: {header.value!r}")
            else:
                lines.append(f"        value_env: {header.value_env!r}")
    return "\n".join(lines)


def describe_resolved(endpoint: ResolvedEndpoint) -> str:
    """Return the one-line, secret-free summary used by ``/status``."""
    return endpoint.describe()
