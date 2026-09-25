"""The ``/endpoint add`` and ``/endpoint edit`` flow.

**Gather everything, then commit.** Every answer is collected and validated into
a local draft; only once the user confirms does anything touch ``CliState``, the
endpoint store or ``.env``. Cancelling at any step — Ctrl-C, EOF, or declining
the confirmation — leaves all three byte-identical. A wizard that writes as it
goes leaves a user with half an endpoint and no way to tell which half.

The step order is fixed and deliberate: identity before address, address before
credential, credential before any network call, and the network call before the
model choice that depends on it. Presets **pre-fill** rather than hide, so the
user sees the URL and credential variable they are agreeing to.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

from rich.console import Console

from cli.core.api_keys import load_all_stored, save_keys
from cli.endpoints.prompts import CANCELLED, ask, ask_secret, choose, confirm
from cli.endpoints.render import print_show
from cli.ui import print_error, print_ok, print_warn
from mak.core.exceptions import ConfigError
from mak.endpoints.parse import (
    is_loopback,
    is_private_host,
    validate_endpoint_url,
)
from mak.endpoints.profiles import BUILTIN_PROFILES, is_reserved, profile_for
from mak.endpoints.store import (
    load_user_endpoints,
    merge_endpoints,
    save_user_endpoints,
)
from mak.endpoints.types import (
    EndpointConfig,
    HealthPolicy,
    Location,
    ModelDiscovery,
    StructuredOutput,
    TokenParameter,
    Transport,
    validate_endpoint_id,
    validate_env_name,
)

# What the wizard offers to do with the endpoint once it exists.
_USE_CHOICES = (
    "Use it for agents",
    "Use it for the planner",
    "Use it for both",
    "Just save it for now",
)

_LOCATION_CHOICES = (
    ("hosted", Location.HOSTED, "a service on the internet, billed to an account"),
    ("local", Location.LOCAL, "a server on this machine"),
    ("private", Location.PRIVATE, "a server on your own network"),
)


@dataclass(slots=True)
class Draft:
    """Everything the wizard has gathered, before any of it is committed.

    Mutable on purpose — it is scratch space owned by one function call and
    never shared. What it produces (an ``EndpointConfig``) is frozen.
    """

    endpoint_id: str = ""
    profile: str | None = None
    display_name: str = ""
    location: Location = Location.HOSTED
    base_url: str = ""
    api_key_env: str = ""
    # The key itself, held only until the commit step writes it to the 0600
    # ``.env``. Never echoed, never logged, never put in the endpoint record.
    api_key: str = ""
    save_key: bool = False
    model: str = ""
    use_for_agents: bool = False
    use_for_planner: bool = False
    allow_insecure: bool = False
    headers: list[tuple[str, str]] = field(default_factory=list)

    def to_endpoint(self) -> EndpointConfig:
        """Return the frozen endpoint record this draft describes."""
        from mak.endpoints.types import EndpointHeaderConfig

        return EndpointConfig(
            id=self.endpoint_id,
            transport=Transport.OPENAI_CHAT,
            base_url=self.base_url or None,
            api_key_env=self.api_key_env or None,
            location=self.location,
            profile=self.profile,
            display_name=self.display_name,
            headers=tuple(
                EndpointHeaderConfig(name=name, value=value)
                for name, value in self.headers
            ),
        )


def run_add(
    console: Console, profile_id: str = "", *, existing: tuple[str, ...] = ()
) -> tuple[EndpointConfig, Draft] | None:
    """Run the add wizard. Returns None when the user cancels at any step."""
    draft = Draft()

    profile = _pick_profile(console, profile_id)
    if profile is None:
        _cancel(console)
        return None
    draft.profile = None if profile == "custom" else profile
    preset = profile_for(profile)
    if preset is not None:
        draft.base_url = preset.base_url or ""
        draft.api_key_env = preset.api_key_env or ""
        draft.display_name = preset.display_name
        draft.location = preset.location
        if preset.note:
            console.print(f"\n  [dim]{preset.note}[/dim]")

    if not _pick_identity(console, draft, profile, existing):
        _cancel(console)
        return None
    if not _pick_location(console, draft):
        _cancel(console)
        return None
    if not _pick_url(console, draft):
        _cancel(console)
        return None
    if not _pick_credential(console, draft):
        _cancel(console)
        return None

    try:
        endpoint = draft.to_endpoint()
    except ConfigError as exc:
        print_error(console, str(exc))
        return None

    if not _pick_model(console, draft, endpoint):
        _cancel(console)
        return None
    if not _pick_use(console, draft):
        _cancel(console)
        return None

    print_show(console, endpoint)
    _print_capability_summary(console, draft)
    answer = confirm(console, f"Save endpoint '{draft.endpoint_id}'?", default=True)
    if not answer:
        _cancel(console)
        return None
    return endpoint, draft


def _pick_model(
    console: Console, draft: Draft, endpoint: EndpointConfig
) -> bool:
    """Probe for models and let the user choose one. False on cancel.

    A failed or unsupported listing is **not** a dead end: several compatible
    services expose no ``/models`` route at all, and the user usually knows the
    exact id they want. Failure drops straight to typing one.
    """
    listed = _list_models(console, endpoint)
    if listed:
        console.print("\n  [bold]Which model?[/bold]\n")
        options = [*listed[:20], "Type an exact model id instead"]
        index = choose(console, options, "Model")
        if index < 0:
            return False
        if index < len(listed[:20]):
            draft.model = listed[index]
            return True
    raw = ask(console, "Model id: ")
    if raw == CANCELLED:
        return False
    draft.model = raw
    if not draft.model:
        print_warn(
            console, "No model chosen — pick one later with /models."
        )
    return True


def _list_models(console: Console, endpoint: EndpointConfig) -> list[str]:
    """Return the endpoint's model ids, or an empty list with an explanation."""
    from mak.endpoints.resolution import resolve_endpoint
    from mak.models.providers import ModelFetchError, OpenAiCompatibleSource

    resolved = resolve_endpoint(endpoint)
    if resolved.model_discovery is ModelDiscovery.MANUAL:
        return []
    console.print("\n  [dim]Asking the endpoint what it offers…[/dim]")
    try:
        fetched = OpenAiCompatibleSource(resolved).fetch(
            resolved.effective_key() or ""
        )
    except ModelFetchError as exc:
        print_warn(
            console,
            f"Could not list models ({exc}). You can still enter an id by hand.",
        )
        return []
    return [m.model_id for m in fetched]


def _pick_use(console: Console, draft: Draft) -> bool:
    """Ask what this endpoint should be used for. False on cancel."""
    console.print("\n  [bold]Use it for what?[/bold]\n")
    index = choose(console, list(_USE_CHOICES), "Use")
    if index < 0:
        return False
    draft.use_for_agents = index in (0, 2)
    draft.use_for_planner = index in (1, 2)
    return True


def _print_capability_summary(console: Console, draft: Draft) -> None:
    """Show the capability defaults this endpoint will run with.

    Shown rather than asked: these are protocol capabilities MAK negotiates at
    runtime, and asking a user to predict them before the first request would
    be asking them to guess. ``/endpoint edit`` overrides any of them.
    """
    discovery, health, structured, token = default_capabilities(draft.profile)
    console.print(
        "    [dim]capabilities[/dim]  "
        f"discovery={_name(discovery)}  health={_name(health)}  "
        f"output={_name(structured)}  cap={_name(token)}"
    )
    console.print(
        "    [dim]MAK negotiates these on the first request and remembers "
        "what worked.[/dim]"
    )


def _name(value: object) -> str:
    """Render a capability default, or 'auto' when the profile states none."""
    return str(getattr(value, "value", "auto"))


def _cancel(console: Console) -> None:
    """Report that nothing was changed.

    Every caller returns ``None`` immediately after; keeping the return out of
    this helper is what lets mypy see that each exit is a real ``None`` rather
    than a value flowing through.
    """
    print_warn(console, "Cancelled — nothing was changed.")


def _pick_profile(console: Console, requested: str) -> str | None:
    """Return the chosen profile id, or None on cancel."""
    if requested:
        if profile_for(requested) is not None:
            return requested.strip().lower()
        known = ", ".join(p.id for p in BUILTIN_PROFILES)
        print_error(console, f"Unknown profile '{requested}' — known: {known}")
        return None
    console.print("\n  [bold]Which service?[/bold]\n")
    options = [f"{p.display_name}  [dim]({p.id})[/dim]" for p in BUILTIN_PROFILES]
    index = choose(console, options, "Service")
    if index < 0:
        return None
    return BUILTIN_PROFILES[index].id


def _pick_identity(
    console: Console, draft: Draft, profile: str, existing: tuple[str, ...]
) -> bool:
    """Ask for the endpoint id and display name. Returns False on cancel."""
    suggested = profile if profile != "custom" else ""
    while True:
        raw = ask(console, f"Endpoint id [{suggested}]: ", default="")
        if raw == CANCELLED:
            return False
        candidate = raw or suggested
        if not candidate:
            print_error(console, "An id is required.")
            continue
        try:
            endpoint_id = validate_endpoint_id(candidate)
        except ConfigError as exc:
            print_error(console, str(exc))
            continue
        if is_reserved(endpoint_id):
            print_error(
                console,
                f"'{endpoint_id}' is reserved for MAK's built-in provider — "
                f"try '{endpoint_id}-gateway'.",
            )
            continue
        if endpoint_id in existing:
            print_error(console, f"'{endpoint_id}' is already configured.")
            continue
        draft.endpoint_id = endpoint_id
        break

    name = ask(
        console, f"Display name [{draft.display_name or draft.endpoint_id}]: "
    )
    if name == CANCELLED:
        return False
    draft.display_name = name or draft.display_name or draft.endpoint_id
    return True


def _pick_location(console: Console, draft: Draft) -> bool:
    """Ask where the endpoint lives. Returns False on cancel."""
    console.print("\n  [bold]Where does this endpoint run?[/bold]\n")
    default = next(
        (i for i, (_, loc, _) in enumerate(_LOCATION_CHOICES) if loc == draft.location),
        0,
    )
    options = [f"{label} [dim]— {note}[/dim]" for label, _, note in _LOCATION_CHOICES]
    index = choose(console, options, "Location", default)
    if index < 0:
        return False
    draft.location = _LOCATION_CHOICES[index][1]
    return True


def _pick_url(console: Console, draft: Draft) -> bool:
    """Ask for and validate the base URL. Returns False on cancel."""
    while True:
        raw = ask(console, f"Base URL [{draft.base_url}]: ")
        if raw == CANCELLED:
            return False
        url = raw or draft.base_url
        if not url:
            print_error(console, "A base URL is required.")
            continue
        try:
            from mak.config import normalize_base_url

            url = normalize_base_url(url, where="base URL")
            url = validate_endpoint_url(
                url,
                endpoint_id=draft.endpoint_id,
                location=draft.location,
                allow_insecure=draft.allow_insecure,
            )
        except ConfigError as exc:
            # Plain HTTP off-machine is recoverable with an explicit yes; a
            # hosted HTTP URL is not, because that ships the key in the clear.
            if url.startswith("http://") and not is_loopback(url) and (
                draft.location is not Location.HOSTED
            ):
                answer = confirm(
                    console,
                    "That sends requests unencrypted over your network. Continue?",
                )
                if answer is None:
                    return False
                if answer:
                    draft.allow_insecure = True
                    draft.base_url = url
                    return True
            print_error(console, str(exc))
            continue
        if url.startswith("http://") and is_private_host(url):
            console.print(
                "  [yellow]Note:[/yellow] this endpoint is on your network, "
                "not this machine — its traffic leaves this computer."
            )
        draft.base_url = url
        return True


def _pick_credential(console: Console, draft: Draft) -> bool:
    """Ask for the credential variable and, optionally, the key. False on cancel."""
    prompt = f"API key variable name [{draft.api_key_env or 'none'}]: "
    while True:
        raw = ask(console, prompt)
        if raw == CANCELLED:
            return False
        name = raw or draft.api_key_env
        if not name or name.lower() == "none":
            console.print(
                "  [dim]No credential — MAK will send a non-secret placeholder, "
                "and never your OpenAI key.[/dim]"
            )
            draft.api_key_env = ""
            return True
        try:
            draft.api_key_env = validate_env_name(name, where="API key variable")
        except ConfigError as exc:
            print_error(console, str(exc))
            continue
        break

    already_set = draft.api_key_env in load_all_stored()
    hint = " (leave blank to keep the stored one)" if already_set else ""
    secret = ask_secret(console, f"Paste the key{hint}: ")
    if secret == CANCELLED:
        return False
    if not secret:
        if not already_set:
            print_warn(
                console,
                f"No key entered. Export {draft.api_key_env} before running, or "
                "add it later with /apikey.",
            )
        return True
    draft.api_key = secret
    answer = confirm(
        console,
        "Save it to your key file so it survives a restart?",
        default=True,
    )
    if answer is None:
        return False
    draft.save_key = answer
    return True


def commit(
    console: Console, endpoint: EndpointConfig, draft: Draft
) -> EndpointConfig | None:
    """Write the endpoint and its key, atomically as far as each store allows.

    Order matters: the key first, then the metadata. A saved key with no
    endpoint is inert and invisible; an endpoint whose key never landed looks
    configured and fails at the first dispatch.
    """
    if draft.api_key and draft.save_key and draft.api_key_env:
        try:
            save_keys({draft.api_key_env: draft.api_key})
        except ConfigError as exc:
            print_error(console, f"Could not save the key: {exc}")
            return None
    elif draft.api_key and draft.api_key_env:
        # Session-only: exported into this process so the endpoint works now,
        # deliberately not written to disk.
        import os

        os.environ[draft.api_key_env] = draft.api_key

    saved, _diagnostic = load_user_endpoints()
    try:
        save_user_endpoints((*saved, endpoint))
    except ConfigError as exc:
        print_error(console, str(exc))
        return None
    print_ok(console, f"Endpoint '{endpoint.id}' saved.")
    return endpoint


def existing_ids(config_file: Path | None = None) -> tuple[str, ...]:
    """Return every endpoint id already configured, from both sources.

    ``config_file`` is the session's config; without one, the config
    discovered from the current directory is read.
    """
    from mak.config import discover_config_path, load_config

    saved, _ = load_user_endpoints()
    try:
        project = load_config(config_file or discover_config_path()).endpoints
    except ConfigError:
        project = ()
    try:
        merged = merge_endpoints(project, saved)
    except ConfigError:
        # A clash is reported by the command that surfaces it; for the purpose
        # of "which ids are taken", both sources count.
        merged = (*project, *saved)
    return tuple(e.id for e in merged)


def default_capabilities(profile: str | None) -> tuple[
    ModelDiscovery | None,
    HealthPolicy | None,
    StructuredOutput | None,
    TokenParameter | None,
]:
    """Return the capability defaults a profile contributes, for the summary."""
    preset = profile_for(profile) if profile else None
    if preset is None:
        return (None, None, None, None)
    return (
        preset.model_discovery,
        preset.health_check,
        preset.structured_output,
        preset.token_parameter,
    )


def apply_edit(
    console: Console, endpoint: EndpointConfig
) -> EndpointConfig | None:
    """Edit an endpoint's mutable settings. The id is immutable.

    Changing an id would orphan every selected agent spec that names it, so the
    wizard does not offer it: remove and re-add is the honest path, and it makes
    the consequence visible.
    """
    draft = Draft(
        endpoint_id=endpoint.id,
        profile=endpoint.profile,
        display_name=endpoint.display_name,
        location=endpoint.location,
        base_url=endpoint.base_url or "",
        api_key_env=endpoint.api_key_env or "",
    )
    console.print(
        f"\n  Editing [bold]{endpoint.id}[/bold] "
        "[dim](the id cannot change — remove and re-add for that)[/dim]"
    )
    if not _pick_location(console, draft):
        _cancel(console)
        return None
    if not _pick_url(console, draft):
        _cancel(console)
        return None
    if not _pick_credential(console, draft):
        _cancel(console)
        return None

    updated = replace(
        endpoint,
        base_url=draft.base_url or None,
        api_key_env=draft.api_key_env or None,
        location=draft.location,
        display_name=draft.display_name,
    )
    print_show(console, updated)
    answer = confirm(console, "Apply these changes?", default=True)
    if not answer:
        _cancel(console)
        return None

    if draft.api_key and draft.save_key and draft.api_key_env:
        try:
            save_keys({draft.api_key_env: draft.api_key})
        except ConfigError as exc:
            print_error(console, f"Could not save the key: {exc}")
            return None

    saved, _ = load_user_endpoints()
    try:
        save_user_endpoints(
            tuple(updated if e.id == endpoint.id else e for e in saved)
        )
    except ConfigError as exc:
        print_error(console, str(exc))
        return None
    print_ok(console, f"Endpoint '{endpoint.id}' updated.")
    return updated
