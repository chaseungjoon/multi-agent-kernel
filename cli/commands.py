"""Slash-command handlers for the MAK CLI.

``handle_command`` returns an action for the main loop: ``"exit"`` to quit,
``"clear"`` to clear the screen, or ``None`` to keep prompting. State feedback
is a single ✓/✗ line — live settings are always visible in the prompt toolbar.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from rich.console import Console

from cli.completer import COMMANDS
from cli.config_sync import sync_with_config
from cli.core.models import (
    PROVIDER_DISPLAY,
    PROVIDER_ORDER,
    all_models,
    models_for_provider,
)
from cli.core.state import (
    MODE_CLOUD,
    MODE_HYBRID,
    MODE_LOCAL,
    MODES,
    CliState,
    LocalHost,
    mode_summary,
)
from cli.endpoints import cmd_endpoint
from cli.local import (
    activate_host,
    apply_cloud_planner,
    apply_local_planner,
    cmd_local,
    refresh_local_models,
    refresh_saved_host,
)
from cli.ui import ACCENT, print_error, print_ok, print_status, print_warn
from mak.config import model_caveat
from mak.endpoints.types import EndpointConfig, Location
from mak.local import OllamaError
from mak.local.runtime import KIND_OLLAMA, KIND_OPENAI_COMPATIBLE
from mak.models.refresh import RefreshReport

_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai":    "OPENAI_API_KEY",
    "gemini":    "GEMINI_API_KEY",
}


def handle_command(text: str, state: CliState, console: Console) -> str | None:
    """Execute a ``/command`` line and return the action the main loop takes.

    ``"exit"``, ``"clear"``, ``"work_dir"`` (the working directory changed), or
    None to keep prompting.
    """
    parts = text.strip().split()
    if not parts:
        return None
    cmd  = parts[0].lower()
    args = parts[1:]

    if cmd == "/models":
        _cmd_models(args, state, console)
    elif cmd == "/max-agents":
        _cmd_max_agents(args, state, console)
    elif cmd == "/work-dir":
        # The app, which owns the terminal, then offers a project config.
        return "work_dir" if _cmd_work_dir(args, state, console) else None
    elif cmd == "/apikey":
        _cmd_apikey(state, console)
    elif cmd == "/config":
        _cmd_config(args, state, console)
    elif cmd == "/no-review":
        _cmd_no_review(args, state, console)
    elif cmd == "/planner":
        _cmd_planner(args, state, console)
    elif cmd == "/refresh-models":
        _cmd_refresh_models(state, console)
    elif cmd == "/local":
        cmd_local(args, state, console)
    elif cmd == "/endpoint":
        cmd_endpoint(args, state, console)
    elif cmd == "/mode":
        _cmd_mode(args, state, console)
    elif cmd == "/status":
        print_status(console, state)
    elif cmd == "/help":
        _cmd_help(console)
    elif cmd == "/clear":
        return "clear"
    elif cmd in ("/exit", "/quit"):
        return "exit"
    else:
        print_error(
            console, f"Unknown command: {cmd}  [dim]— /help lists commands[/dim]"
        )
    return None


# ── Handlers ──────────────────────────────────────────────────────────────────

def _cmd_help(console: Console) -> None:
    console.print()
    width = max(len(name) for name, _ in COMMANDS)
    for name, desc in COMMANDS:
        padded = name.ljust(width)
        console.print(f"  [bold {ACCENT}]{padded}[/bold {ACCENT}]  [dim]{desc}[/dim]")
    console.print()
    console.print(
        "  [dim]Type [/dim][bold]/[/bold][dim] to browse commands with descriptions,"
        " Tab to complete.[/dim]"
    )
    console.print(
        "  [dim]Enter runs a task · Ctrl+J inserts a newline · Ctrl+C quits.[/dim]"
    )
    console.print()


# Providers whose models live on this machine. They have no API-key env var by
# construction, so the key check below must not be applied to them: rejecting a
# keyless provider for having no key is exactly the bug that would make local
# mode unusable from ``/models``.
_LOCAL_PROVIDERS = ("local", "ollama")

# The same two names as spec prefixes. Tested against a whole spec rather than a
# bare provider, so the colon matters: without it a hosted model id that merely
# began with "local" would be read as a local one.
_LOCAL_SPEC_PREFIXES = tuple(f"{name}:" for name in _LOCAL_PROVIDERS)


def _cmd_models(args: list[str], state: CliState, console: Console) -> None:
    if not args:
        if state.uses_local_agents() and state.has_local_runtime():
            # The running server is the authority on what is installed, so a
            # local session lists it live; otherwise the cached list is shown.
            try:
                refresh_local_models(state)
            except OllamaError as exc:
                print_error(console, f"{exc} — showing the cached local list")
        _list_models(state, console)
        return

    configured = _configured_endpoints(state)
    valid: list[str] = []
    for spec in args:
        provider = spec.split(":")[0].lower()
        # A configured endpoint wins over every legacy rule. Reserved ids can
        # never reach here (the parser refuses them), so there is no case where
        # this shadows a built-in provider.
        endpoint = configured.get(provider)
        if endpoint is not None:
            resolved = _endpoint_spec(spec, endpoint, state, console)
            if resolved is None:
                return
            valid.append(resolved)
            continue
        if provider in _LOCAL_PROVIDERS:
            resolved = _local_spec(spec, state, console)
            if resolved is None:
                return
            valid.append(resolved)
            continue
        key_env = _KEY_ENV.get(provider)
        if key_env is None:
            known = ", ".join(sorted({*_KEY_ENV, *_LOCAL_PROVIDERS, *configured}))
            print_error(
                console,
                f"Unknown endpoint or provider: {provider}  "
                f"[dim]— known: {known}. '/endpoint add' sets up a new one.[/dim]",
            )
            return
        if not state.api_keys.get(key_env, "").strip():
            print_error(
                console,
                f"No API key for {provider} — run [bold]/apikey[/bold] to add one.",
            )
            return
        valid.append(spec)

    if state.max_agents < len(valid):
        print_error(
            console,
            f"max-agents ({state.max_agents}) < number of models ({len(valid)}) — "
            f"run [bold]/max-agents {len(valid)}[/bold] first.",
        )
        return

    state.selected_models = valid
    # Only a genuinely local runtime flips the mode. A hosted compatible
    # endpoint is cloud work with a different URL, and switching to local mode
    # for one would launch the local wizard at a user who has no local runtime.
    if any(_spec_is_local(spec, state) for spec in valid) and (
        state.mode == MODE_CLOUD
    ):
        state.mode = MODE_LOCAL
    print_ok(console, f"Models: {', '.join(valid)}")
    for spec in valid:
        caveat = model_caveat(spec.partition(":")[2].partition("@")[0])
        if caveat:
            print_warn(console, caveat)


def _configured_endpoints(state: CliState) -> dict[str, EndpointConfig]:
    """Return every configured endpoint by id, or an empty map if none load.

    Total by construction: ``/models`` must stay usable when the endpoint store
    or the project config is broken, and ``/endpoint list`` is where that
    problem gets reported.
    """
    try:
        from cli.endpoints.commands import all_endpoints

        return {e.id: e for e in all_endpoints(state.config_file())}
    except Exception:  # noqa: BLE001 - a broken store must not break /models
        return {}


def _endpoint_spec(
    spec: str, endpoint: EndpointConfig, state: CliState, console: Console
) -> str | None:
    """Validate an ``endpoint:model`` spec, or report why and return None.

    The model id keeps **everything** after the first colon, so a slug like
    ``meta/llama-3.3-70b-instruct`` and an Ollama tag like
    ``qwen2.5-coder:14b`` both survive.
    """
    _, _, model = spec.partition(":")
    if not model:
        print_error(
            console,
            f"'{spec}' names no model — write {endpoint.id}:<model>, or list "
            f"what it offers with [bold]/endpoint models {endpoint.id}[/bold].",
        )
        return None
    if endpoint.api_key_env and not _endpoint_key_present(endpoint, state):
        print_error(
            console,
            f"No key for {endpoint.display_name} — set {endpoint.api_key_env} "
            "or run [bold]/apikey[/bold].",
        )
        return None
    if endpoint.id not in state.endpoint_ids:
        state.endpoint_ids.append(endpoint.id)
    return spec


def _endpoint_key_present(endpoint: EndpointConfig, state: CliState) -> bool:
    """Whether this endpoint's credential is available from any source."""
    import os

    name = endpoint.api_key_env or ""
    if state.api_keys.get(name, "").strip() or os.environ.get(name, "").strip():
        return True
    from cli.core.api_keys import load_all_stored

    return bool(load_all_stored().get(name, "").strip())


def _local_spec(spec: str, state: CliState, console: Console) -> str | None:
    """Return ``spec`` with an endpoint attached, or None after reporting why.

    ``/models ollama:qwen2.5-coder:14b`` is accepted with **no** key check; the
    endpoint comes from the spec's own ``@url`` when given, else from the
    runtime ``/local`` configured.
    """
    if "@" in spec:
        return spec
    if not state.has_local_runtime():
        print_error(
            console,
            f"{spec} names no endpoint and no local runtime is configured — "
            "run [bold]/local[/bold], or write "
            "[bold]provider:model@http://host:port[/bold].",
        )
        return None
    return f"{spec}@{state.local_base_url}"


def _print_local_group(
    state: CliState, console: Console, is_active: Callable[[LocalHost, str], bool]
) -> None:
    """Print every known host's models as the "Local" group, active host first.

    Shown in every mode, from the lists ``/local`` or ``/refresh-models`` last
    fetched — listing never waits on the network. Models on the active host
    read ``provider:model``; on any other host the spec carries its ``@url``.
    """
    hosts = state.all_local_hosts()
    if not hosts:
        return
    for host in hosts:
        is_current = host.url == state.local_base_url
        tag = "" if is_current else "  [dim](not active — use the full spec)[/dim]"
        console.print(f"\n  [bold]Local[/bold]  [dim]{host.host_display()}[/dim]{tag}")
        if not host.models:
            console.print("    [dim]no models known — /refresh-models[/dim]")
        for name in host.models:
            active = "[green]●[/green]" if is_active(host, name) else "[dim]○[/dim]"
            suffix = "" if is_current else f"@{host.url}"
            console.print(f"    {active} {host.provider()}:{name}{suffix}")
    console.print("\n  [bold]Cloud[/bold]")


def _list_models(state: CliState, console: Console) -> None:
    console.print("\n  [dim]Usage: /models provider:model \\[provider:model ...][/dim]")
    chosen = set(state.selected_models)
    _print_local_group(
        state,
        console,
        lambda host, name: f"{host.provider()}:{name}@{host.url}" in chosen,
    )
    for provider in PROVIDER_ORDER:
        has_key = bool(state.api_keys.get(_KEY_ENV[provider], "").strip())
        console.print(
            f"\n  [bold]{PROVIDER_DISPLAY[provider]}[/bold]"
            + ("" if has_key else " [dim]— no API key[/dim]")
        )
        for m in models_for_provider(state.models(), provider):
            if m.retired:
                rec = " [yellow]⚠ no longer offered[/yellow]"
            elif m.recommended:
                rec = " [dim]★ recommended[/dim]"
            else:
                rec = ""
            spec   = f"{provider}:{m.model_id}"
            selected = spec in state.selected_models
            active = "[green]●[/green]" if selected else "[dim]○[/dim]"
            if has_key:
                console.print(f"    {active} {spec}{rec}")
            else:
                console.print(f"    [dim]○ {spec}[/dim]")
    _print_endpoint_model_groups(state, console, for_planner=False)
    console.print()


def _endpoint_model_rows(
    endpoint: EndpointConfig, state: CliState, *, for_planner: bool
) -> list[tuple[str, str]]:
    """Return cached and manually selected models for one endpoint."""
    rows = [
        (entry.model_id, entry.planner_note())
        for entry in state.models().for_endpoint(endpoint.id)
    ]
    known = {model_id for model_id, _note in rows}
    prefix = f"{endpoint.id}:"
    selected = (
        []
        if for_planner
        else [
            spec[len(prefix) :]
            for spec in state.selected_models
            if spec.startswith(prefix)
        ]
    )
    if for_planner and state.planner_endpoint_id == endpoint.id:
        selected.append(state.planner_model)
    rows.extend((model_id, "") for model_id in selected if model_id not in known)
    return rows


def _print_endpoint_model_groups(
    state: CliState, console: Console, *, for_planner: bool
) -> None:
    """Show configured endpoints beside the built-in model providers."""
    for endpoint in _configured_endpoints(state).values():
        has_key = not endpoint.api_key_env or _endpoint_key_present(endpoint, state)
        identity = (
            endpoint.display_name
            if endpoint.display_name == endpoint.id
            else f"{endpoint.display_name} [dim]({endpoint.id})[/dim]"
        )
        console.print(
            f"\n  [bold]{identity}[/bold]"
            + ("" if has_key else " [dim]— no API key[/dim]")
        )
        rows = _endpoint_model_rows(endpoint, state, for_planner=for_planner)
        if not rows:
            console.print("    [dim]no models cached — /refresh-models[/dim]")
            continue
        for model_id, note in rows:
            spec = f"{endpoint.id}:{model_id}"
            selected = (
                state.planner_endpoint_id == endpoint.id
                and state.planner_model == model_id
                if for_planner
                else spec in state.selected_models
            )
            active = "[green]●[/green]" if selected else "[dim]○[/dim]"
            suffix = f"  [dim]({note})[/dim]" if note else ""
            console.print(f"    {active} {spec}{suffix}")


def _cmd_max_agents(args: list[str], state: CliState, console: Console) -> None:
    if not args:
        console.print("  [dim]Usage: /max-agents N[/dim]")
        return
    try:
        n = int(args[0])
        if n < 1:
            raise ValueError
    except ValueError:
        print_error(console, "/max-agents requires a positive integer.")
        return
    n_models = len(state.selected_models)
    if n < n_models:
        print_error(console, f"{n} < number of selected models ({n_models}).")
        return
    state.max_agents = n
    print_ok(console, f"Max agents: {n}")


def _cmd_work_dir(args: list[str], state: CliState, console: Console) -> bool:
    """Change the working directory; return whether it changed."""
    if not args:
        console.print("  [dim]Usage: /work-dir /path/to/dir[/dim]")
        return False
    p = Path(" ".join(args)).expanduser().resolve()
    if not p.is_dir():
        print_error(console, f"Directory not found: {p}")
        return False
    state.work_dir = str(p)
    sync_with_config(state)  # the new work dir may bring its own config
    print_ok(console, f"Working directory: {state.work_dir_display()}")
    return True


def _cmd_apikey(state: CliState, console: Console) -> None:
    from cli.setup import run_setup
    run_setup(state, console, editing=True)


def _cmd_config(args: list[str], state: CliState, console: Console) -> None:
    if not args:
        state.config_path = ""
        print_ok(
            console,
            "Config: auto  [dim]— <work dir>/.mak/config.yaml → "
            "~/.config/mak/config.yaml → built-in default[/dim]",
        )
    else:
        p = Path(args[0]).expanduser().resolve()
        if not p.exists():
            print_error(console, f"Config file not found: {p}")
            return
        state.config_path = str(p)
        print_ok(console, f"Config: {p}")
    sync_with_config(state)


def _cmd_planner(args: list[str], state: CliState, console: Console) -> None:
    """Set the planner from a ``provider:model[@url]`` spec — ``/models``' grammar.

    The provider is required, never inferred from the model id: one model can
    be offered by several providers (``anthropic:`` and an ``openrouter:``
    endpoint), and the planner must go where the user said.
    """
    if not args:
        _list_planner_models(state, console)
        return

    raw = args[0]
    provider, _, model = raw.partition(":")
    provider = provider.lower()
    configured = _configured_endpoints(state)
    endpoint = configured.get(provider)
    if endpoint is not None:
        _set_endpoint_planner(raw, endpoint, state, console)
        return
    # A local model is not in the catalog by construction (mak/local's whole
    # premise is that the running server is the authority), so the catalog
    # lookup below must be skipped for one rather than rejecting it.
    if provider in _LOCAL_PROVIDERS:
        _set_local_planner(raw, state, console)
        return
    if provider in _KEY_ENV:
        _set_cloud_planner(provider, model, state, console)
        return
    # Not a provider. Checked against the whole argument, not the text before
    # its first colon: an Ollama tag (``qwen2.5-coder:14b``) has one of its own.
    candidates = _planner_specs_for_model(raw, state)
    if candidates:
        print_error(
            console,
            f"/planner takes provider:model, got '{raw}' — did you mean "
            f"{' or '.join(candidates)}?",
        )
        return
    known = ", ".join(sorted({*_KEY_ENV, *_LOCAL_PROVIDERS, *configured}))
    print_error(
        console,
        f"Unknown endpoint or provider: {provider}  "
        f"[dim]— /planner takes provider:model; known: {known}. "
        "'/endpoint add' sets up a new one.[/dim]",
    )


def _planner_specs_for_model(model_id: str, state: CliState) -> list[str]:
    """Return every ``provider:model`` spec that offers ``model_id``."""
    specs = [
        f"{m.provider}:{m.model_id}"
        for m in all_models(state.models())
        if m.model_id == model_id
    ]
    if model_id in state.local_models:
        specs.append(f"{state.local_provider()}:{model_id}")
    return specs


def _set_cloud_planner(
    provider: str, model_id: str, state: CliState, console: Console
) -> None:
    """Point the planner at a built-in hosted provider's model."""
    spec = f"{provider}:{model_id}"
    if not model_id:
        print_error(
            console, f"'{provider}:' names no model — write {provider}:<model>."
        )
        return
    offered = models_for_provider(state.models(), provider)
    model_info = next((m for m in offered if m.model_id == model_id), None)
    if model_info is None:
        print_error(
            console,
            f"Unknown model: {spec} — run [bold]/planner[/bold] to list models.",
        )
        return

    if not state.api_keys.get(_KEY_ENV[provider], "").strip():
        print_error(
            console,
            f"No API key for {provider} — run [bold]/apikey[/bold] to add one.",
        )
        return

    if state.mode == MODE_LOCAL:
        # A hosted planner beside local agents is hybrid, by definition.
        state.mode = MODE_HYBRID
    apply_cloud_planner(state, provider, model_id)
    if not model_info.planner_ok:
        print_warn(
            console,
            f"Planner: {spec} — may struggle with complex task decomposition.",
        )
    else:
        print_ok(console, f"Planner: {spec}")
    caveat = model_caveat(model_id)
    if caveat:
        print_warn(console, caveat)


def _set_endpoint_planner(
    raw: str, endpoint: EndpointConfig, state: CliState, console: Console
) -> None:
    """Point the planner at ``endpoint:model``, making the endpoint the route.

    The model and the route are one ``PlannerRoute``, so no earlier backend or
    base URL can linger beside the endpoint and give the planner two answers to
    "where does this go".
    """
    _, _, model = raw.partition(":")
    if not model:
        print_error(
            console,
            f"'{raw}' names no model — write {endpoint.id}:<model>.",
        )
        return
    if endpoint.api_key_env and not _endpoint_key_present(endpoint, state):
        print_error(
            console,
            f"No key for {endpoint.display_name} — set {endpoint.api_key_env} "
            "or run [bold]/apikey[/bold].",
        )
        return
    state.set_endpoint_planner(endpoint.id, model)
    if endpoint.id not in state.endpoint_ids:
        state.endpoint_ids.append(endpoint.id)
    print_ok(console, f"Planner: {endpoint.id}:{model}")

    entry = state.models().find(model, endpoint.id)
    note = entry.planner_note() if entry is not None else "not evaluated"
    if note:
        # "Not evaluated" is a third state, distinct from "fine" and from
        # "known to struggle"; conflating any two of them misleads.
        print_warn(console, f"Planner quality for {model}: {note}.")


def _set_local_planner(raw: str, state: CliState, console: Console) -> None:
    """Point the planner at a local model, taking the endpoint from state/spec."""
    spec, _, url = raw.partition("@")
    provider, _, model = spec.partition(":")
    provider = provider.lower()
    if not model:
        print_error(console, f"'{raw}' names no model — write {provider}:<model>.")
        return
    if url:
        activate_host(
            state,
            url.rstrip("/"),
            KIND_OLLAMA if provider == "ollama" else KIND_OPENAI_COMPATIBLE,
        )
    if not state.has_local_runtime():
        print_error(
            console,
            f"{raw} names no endpoint and no local runtime is configured — "
            "run [bold]/local[/bold] first.",
        )
        return
    if provider != state.local_provider():
        # The prefix picks the wire protocol; honouring the runtime's kind over
        # what the user wrote would silently plan through a different client.
        print_error(
            console,
            f"{raw} names {provider}, but the runtime at {state.local_base_url} "
            f"is {state.local_provider()} — write {state.local_provider()}:{model}.",
        )
        return
    apply_local_planner(state, model)
    print_ok(
        console,
        f"Planner: {state.planner_spec()}  [dim]at {state.local_base_url}[/dim]",
    )


def _cmd_mode(args: list[str], state: CliState, console: Console) -> None:
    """Show or switch how this session gets its models."""
    if not args:
        console.print()
        for mode in MODES:
            active = "[green]●[/green]" if mode == state.mode else "[dim]○[/dim]"
            console.print(
                f"    {active} [bold]{mode}[/bold]  [dim]{mode_summary(mode)} — "
                f"{_mode_requirement(mode)}[/dim]"
            )
        console.print()
        return

    target = args[0].lower()
    if target not in MODES:
        print_error(console, f"/mode expects one of {', '.join(MODES)}, got: {args[0]}")
        return
    problem = _mode_blocker(target, state)
    if problem:
        # Refuse with the command that fixes it, not with "cannot".
        print_error(console, problem)
        return
    state.mode = target
    print_ok(console, f"Mode: {target}  [dim]{mode_summary(target)}[/dim]")
    _reconcile_with_mode(target, state, console)


# What each mode expects of (planner, agents): True = local, False = cloud.
_MODE_EXPECTS: dict[str, tuple[bool, bool]] = {
    MODE_CLOUD:  (False, False),
    MODE_LOCAL:  (True, True),
    MODE_HYBRID: (False, True),
}


def _where(is_local: bool) -> str:
    return "local" if is_local else "cloud"


def _planner_is_local(state: CliState) -> bool:
    """Whether the planner's traffic stays on this machine or network.

    A selected endpoint answers by its explicit ``location``; a local route
    is local by definition, and a hosted one (even through a gateway) is not.
    """
    if state.planner.kind == "endpoint":
        return _endpoint_is_local(state.planner.endpoint_id, state)
    return state.planner.kind == "local"


def _endpoint_is_local(endpoint_id: str, state: CliState) -> bool:
    """Whether an endpoint's traffic stays off the public internet.

    Reads the endpoint's stated ``location``. The old test — "does it have a
    base_url" — called NVIDIA, OpenRouter, DeepSeek and Z.ai local, because
    every one of them has one.

    A ``private`` endpoint counts as local for *mode* purposes (it is not a
    hosted provider) while still being reported distinctly wherever privacy is
    described, because its traffic does leave this machine.
    """
    endpoint = _configured_endpoints(state).get(endpoint_id)
    return endpoint is not None and endpoint.location is not Location.HOSTED


def _spec_is_local(spec: str, state: CliState) -> bool:
    """Whether one selected model spec runs off the public internet."""
    prefix = spec.split(":")[0].lower()
    if prefix in _configured_endpoints(state):
        return _endpoint_is_local(prefix, state)
    return spec.startswith(_LOCAL_SPEC_PREFIXES)


def _agents_are_local(state: CliState) -> bool | None:
    """Return True/False for an all-local/all-cloud roster, None otherwise.

    An empty roster (the config file's agents) or a mixed one has no single
    answer, and is left alone rather than second-guessed.
    """
    if not state.selected_models:
        return None
    local = [_spec_is_local(spec, state) for spec in state.selected_models]
    if all(local):
        return True
    if not any(local):
        return False
    return None


def _mode_mismatches(mode: str, state: CliState) -> tuple[bool, bool]:
    """Return ``(planner_wrong, agents_wrong)`` for ``mode``."""
    want_planner, want_agents = _MODE_EXPECTS[mode]
    agents = _agents_are_local(state)
    return (
        _planner_is_local(state) != want_planner,
        agents is not None and agents != want_agents,
    )


def _reconcile_with_mode(mode: str, state: CliState, console: Console) -> None:
    """Offer to replace whichever of planner/agents does not fit ``mode``.

    Never forced: declining (or skipping a pick) keeps the current models, so
    an unusual combination such as a local planner with cloud agents stays
    possible. The mode is set either way.
    """
    import cli.local as local_mod

    planner_wrong, agents_wrong = _mode_mismatches(mode, state)
    if not (planner_wrong or agents_wrong):
        return
    want_planner, want_agents = _MODE_EXPECTS[mode]
    console.print(
        f"\n  [dim]{mode} mode expects a {_where(want_planner)} planner and "
        f"{_where(want_agents)} agents. Currently:[/dim]"
    )
    console.print(
        f"    [dim]planner[/dim] {state.planner_spec()}  "
        f"[dim]({_where(_planner_is_local(state))})[/dim]"
    )
    console.print(
        f"    [dim]agents [/dim] {state.models_display()}  "
        f"[dim]({_where(not want_agents) if agents_wrong else _where(want_agents)})"
        "[/dim]"
    )
    parts = " and ".join(
        name
        for name, wrong in (("planner", planner_wrong), ("agents", agents_wrong))
        if wrong
    )
    answer = local_mod._ask(console, f"Choose new {parts} now? [Y/n]: ").lower()
    if answer in ("n", "no"):
        print_warn(
            console,
            f"Keeping the current models. [dim]{mode} mode is set; "
            "/models and /planner change them anytime.[/dim]",
        )
        return

    if agents_wrong:
        _pick_agents(want_agents, state, console)
    if planner_wrong:
        _pick_planner(want_planner, state, console)
    # Applying a planner re-derives the mode (apply_cloud_planner → hybrid,
    # apply_local_planner → local); the user asked for this one explicitly.
    state.mode = mode
    console.print(
        f"\n  [dim]planner[/dim] {state.planner_spec()}   "
        f"[dim]agents[/dim] {state.models_display()}"
    )


def _numbered(console: Console, options: list[str]) -> None:
    console.print()
    for index, option in enumerate(options, 1):
        console.print(f"    [dim]{index:>3})[/dim]  {option}")
    console.print()


def _parse_picks(raw: str, count: int) -> list[int] | None:
    """Parse ``"1"`` / ``"1,3"`` / ``"1 3"`` into 0-based indexes; None if invalid."""
    picks: list[int] = []
    for token in raw.replace(",", " ").split():
        if not token.isdigit() or not 1 <= int(token) <= count:
            return None
        if int(token) - 1 not in picks:
            picks.append(int(token) - 1)
    return picks or None


def _local_choices(state: CliState) -> list[tuple[LocalHost, str]]:
    return [(host, name) for host in state.all_local_hosts() for name in host.models]


def _cloud_choices(state: CliState, *, for_planner: bool) -> list[tuple[str, str]]:
    """Return ``(provider, model_id)`` for every offered model with a key."""
    choices: list[tuple[str, str]] = []
    for provider in PROVIDER_ORDER:
        if not state.api_keys.get(_KEY_ENV[provider], "").strip():
            continue
        entries = [
            m for m in models_for_provider(state.models(), provider) if not m.retired
        ]
        entries.sort(
            key=lambda m: not (m.planner_ok if for_planner else m.recommended)
        )
        choices += [(provider, m.model_id) for m in entries]
    return choices


def _pick_agents(local: bool, state: CliState, console: Console) -> None:
    import cli.local as local_mod

    if local:
        local_opts = _local_choices(state)
        specs = [f"{h.provider()}:{name}@{h.url}" for h, name in local_opts]
        labels = [f"{h.provider()}:{name}  [dim]{h.host_display()}[/dim]"
                  for h, name in local_opts]
        empty = "No local models known — /local url or /refresh-models first."
    else:
        cloud_opts = _cloud_choices(state, for_planner=False)
        specs = labels = [f"{provider}:{model}" for provider, model in cloud_opts]
        empty = "No cloud models available — /apikey adds a provider key."
    if not specs:
        print_error(console, f"{empty} Agents unchanged.")
        return
    console.print(f"\n  [bold]Agent model(s)[/bold] [dim]({_where(local)})[/dim]")
    _numbered(console, labels)
    raw = local_mod._ask(console, "Agents (e.g. 1 or 1,3; Enter keeps current): ")
    if not raw:
        return
    picks = _parse_picks(raw, len(specs))
    if picks is None:
        print_error(console, f"Not a choice: {raw} — agents unchanged.")
        return
    if len(picks) > state.max_agents:
        print_error(
            console,
            f"max-agents ({state.max_agents}) < {len(picks)} models — "
            "agents unchanged; run /max-agents first.",
        )
        return
    state.selected_models = [specs[i] for i in picks]


def _pick_planner(local: bool, state: CliState, console: Console) -> None:
    import cli.local as local_mod

    local_opts = _local_choices(state) if local else []
    cloud_opts = [] if local else _cloud_choices(state, for_planner=True)
    if local:
        labels = [f"{name}  [dim]{h.host_display()}[/dim]" for h, name in local_opts]
    else:
        labels = [f"{provider}:{model}" for provider, model in cloud_opts]
    if not labels:
        where = (
            "No local models known — /local url or /refresh-models first."
            if local else "No cloud models available — /apikey adds a provider key."
        )
        print_error(console, f"{where} Planner unchanged.")
        return
    console.print(f"\n  [bold]Planner model[/bold] [dim]({_where(local)})[/dim]")
    _numbered(console, labels)
    raw = local_mod._ask(console, "Planner (number; Enter keeps current): ")
    if not raw:
        return
    picks = _parse_picks(raw, len(labels))
    if picks is None or len(picks) != 1:
        print_error(console, f"Not a choice: {raw} — planner unchanged.")
        return
    if local:
        host, name = local_opts[picks[0]]
        activate_host(state, host.url, host.kind)
        apply_local_planner(state, name)
    else:
        provider, model = cloud_opts[picks[0]]
        apply_cloud_planner(state, provider, model)


def _mode_requirement(mode: str) -> str:
    """Return what a mode needs to be usable."""
    if mode == MODE_CLOUD:
        return "needs an API key"
    if mode == MODE_LOCAL:
        return "needs a local runtime"
    return "needs both"


def _mode_blocker(mode: str, state: CliState) -> str | None:
    """Return why ``mode`` cannot be selected yet, or None when it can."""
    has_key = any(value.strip() for value in state.api_keys.values())
    needs_key = mode in (MODE_CLOUD, MODE_HYBRID)
    needs_local = mode in (MODE_LOCAL, MODE_HYBRID)
    if needs_local and not state.has_local_runtime():
        return "No local runtime configured — run [bold]/local[/bold]."
    if needs_key and not has_key:
        return "No API key set — run [bold]/apikey[/bold]."
    return None


def _list_planner_models(state: CliState, console: Console) -> None:
    console.print(
        "\n  [dim]Usage: /planner provider:model  —  models below claude-sonnet-4-6"
        " capability"
        " are not recommended.[/dim]"
    )
    _print_local_group(
        state,
        console,
        lambda host, name: (
            host.url == state.planner_base_url and name == state.planner_model
        ),
    )
    for provider in PROVIDER_ORDER:
        has_key = bool(state.api_keys.get(_KEY_ENV[provider], "").strip())
        console.print(
            f"\n  [bold]{PROVIDER_DISPLAY[provider]}[/bold]"
            + ("" if has_key else " [dim]— no API key[/dim]")
        )
        for m in models_for_provider(state.models(), provider):
            spec = f"{provider}:{m.model_id}"
            active = (
                "[green]●[/green]" if spec == state.planner_spec() else "[dim]○[/dim]"
            )
            if m.retired:
                tag = "  [yellow]⚠ no longer offered[/yellow]"
            elif m.planner_ok:
                tag = " [dim]★ recommended[/dim]"
            else:
                tag = "  [yellow]⚠ not recommended[/yellow]"
            if has_key:
                console.print(f"    {active} {spec}{tag}")
            else:
                console.print(f"    [dim]○ {spec}[/dim]")
    _print_endpoint_model_groups(state, console, for_planner=True)
    console.print()


def _cmd_refresh_models(state: CliState, console: Console) -> None:
    """Re-fetch every provider's model list now, ignoring the refresh schedule.

    This is the escape hatch for a model that ships between scheduled refreshes:
    the catalog updates in-session, so the new model is immediately selectable.
    """
    console.print("\n  [dim]Fetching model lists…[/dim]")
    _refresh_local(state, console)
    try:
        report = _refresh_catalog(state)
    except Exception as exc:  # noqa: BLE001 - a refresh must never kill the prompt
        print_error(console, f"Refresh failed: {exc}")
        return

    for result in report.results:
        label = PROVIDER_DISPLAY.get(result.provider, result.provider)
        if not result.ok:
            note = (
                "no API key" if result.error == "no API key"
                else f"{result.error} [dim]— keeping cached list[/dim]"
            )
            console.print(f"    [dim]○[/dim] [bold]{label}[/bold]  [dim]{note}[/dim]")
            continue
        delta = ""
        if result.changed:
            delta = f"  [dim](+{len(result.added)} −{len(result.removed)})[/dim]"
        console.print(
            f"    [green]●[/green] [bold]{label}[/bold]  "
            f"[dim]{result.total} models[/dim]{delta}"
        )
        for model_id in result.added:
            console.print(f"        [green]+ {model_id}[/green]")
        for model_id in result.removed:
            console.print(f"        [red]- {model_id}[/red]")

    console.print()
    if not report.changed:
        print_ok(console, "Model list is up to date.")
    else:
        print_ok(console, "Model list refreshed.")

    # A model the user is actively using may have just been retired. Say so —
    # but change nothing: MAK never re-picks a model on the user's behalf.
    _warn_retired_selections(state, console)


def _refresh_catalog(state: CliState) -> RefreshReport:
    """Refresh built-in providers plus endpoints discovered at runtime."""
    endpoints = tuple(_configured_endpoints(state).values())
    if not endpoints:
        return state.models().refresh_now(state.api_keys)

    from cli.core.api_keys import key_names_for, load_keys
    from mak.endpoints.resolution import resolve_endpoints
    from mak.models.providers import default_sources, sources_for_endpoints

    keys = load_keys(key_names_for(endpoints))
    keys.update({name: value for name, value in state.api_keys.items() if value})
    resolved = resolve_endpoints(endpoints, env=keys)
    sources = (
        *default_sources(),
        *sources_for_endpoints(tuple(resolved.values())),
    )
    key_envs = {endpoint.id: endpoint.api_key_env or "" for endpoint in endpoints}
    return state.models().refresh_now(keys, sources=sources, key_envs=key_envs)


def _refresh_local(state: CliState, console: Console) -> None:
    """Re-list every known host's models, reporting like a cloud provider."""
    for host in state.all_local_hosts():
        label = f"Local [dim]({host.host_display()})[/dim]"
        if host.url == state.local_base_url:
            try:
                changes: tuple[list[str], list[str]] | None = (
                    refresh_local_models(state)
                )
                note = ""
            except OllamaError as exc:
                changes, note = None, str(exc)
            total = len(state.local_models)
        else:
            changes = refresh_saved_host(state, host)
            note = f"cannot reach {host.url}"
            total = len(host.models)
        if changes is None:
            console.print(
                f"    [dim]○[/dim] [bold]{label}[/bold]  "
                f"[dim]{note} — keeping cached list[/dim]"
            )
            continue
        added, removed = changes
        delta = ""
        if added or removed:
            delta = f"  [dim](+{len(added)} −{len(removed)})[/dim]"
        console.print(
            f"    [green]●[/green] [bold]{label}[/bold]  "
            f"[dim]{total} models[/dim]{delta}"
        )
        for model_id in added:
            console.print(f"        [green]+ {model_id}[/green]")
        for model_id in removed:
            console.print(f"        [red]- {model_id}[/red]")


def _warn_retired_selections(state: CliState, console: Console) -> None:
    # Keyed by provider as well as model: the same id retired by one provider
    # may still be offered by another.
    retired = {
        f"{m.provider}:{m.model_id}"
        for m in all_models(state.models())
        if m.retired
    }
    in_use = [
        spec for spec in state.selected_models
        if spec.partition("@")[0] in retired
    ]
    planner = state.planner_spec()
    if ":" not in planner:
        # A planner with no recorded route (set before providers were recorded)
        # can only be matched by model id.
        planner_retired = any(spec.endswith(f":{planner}") for spec in retired)
    else:
        planner_retired = planner in retired
    if planner_retired:
        print_warn(
            console,
            f"Planner {state.planner_spec()} is no longer offered by its provider — "
            "still selected; use [bold]/planner[/bold] to change it.",
        )
    for spec in in_use:
        print_warn(
            console,
            f"Agent model {spec} is no longer offered by its provider — "
            "still selected; use [bold]/models[/bold] to change it.",
        )


def _cmd_no_review(args: list[str], state: CliState, console: Console) -> None:
    if not args:
        state.no_review = not state.no_review
    else:
        flag = args[0].lower()
        if flag in ("true", "on", "1", "yes"):
            state.no_review = True
        elif flag in ("false", "off", "0", "no"):
            state.no_review = False
        else:
            print_error(console, f"/no-review expects true or false, got: {args[0]}")
            return
    if state.no_review:
        print_warn(
            console,
            "Approval off — plans run immediately."
            " [dim]/no-review false re-enables.[/dim]",
        )
    else:
        print_ok(console, "Approval on — MAK shows the plan and waits before running.")
