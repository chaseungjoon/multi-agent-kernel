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
from cli.core.models import (
    PROVIDER_DISPLAY,
    PROVIDER_ORDER,
    all_models,
    models_for_provider,
    registry,
)
from cli.core.state import (
    MODE_CLOUD,
    MODE_HYBRID,
    MODE_LOCAL,
    MODES,
    CliState,
    mode_summary,
)
from cli.local import (
    apply_cloud_planner,
    apply_local_planner,
    cmd_local,
    refresh_local_models,
    spec_for,
)
from cli.ui import ACCENT, print_error, print_ok, print_status, print_warn
from mak.config import model_caveat
from mak.local import OllamaError
from mak.local.runtime import KIND_OLLAMA, KIND_OPENAI_COMPATIBLE

_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai":    "OPENAI_API_KEY",
    "gemini":    "GEMINI_API_KEY",
}


def handle_command(text: str, state: CliState, console: Console) -> str | None:
    """Execute a ``/command`` line and return whether the app should keep running."""
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
        _cmd_work_dir(args, state, console)
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

    valid: list[str] = []
    for spec in args:
        provider = spec.split(":")[0].lower()
        if provider in _LOCAL_PROVIDERS:
            resolved = _local_spec(spec, state, console)
            if resolved is None:
                return
            valid.append(resolved)
            continue
        key_env = _KEY_ENV.get(provider)
        if key_env is None:
            print_error(console, f"Unknown provider: {provider}")
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
    if any(spec.startswith(_LOCAL_SPEC_PREFIXES) for spec in valid) and (
        state.mode == MODE_CLOUD
    ):
        state.mode = MODE_LOCAL
    print_ok(console, f"Models: {', '.join(valid)}")
    for spec in valid:
        caveat = model_caveat(spec.partition(":")[2].partition("@")[0])
        if caveat:
            print_warn(console, caveat)


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
    state: CliState, console: Console, is_active: Callable[[str], bool]
) -> None:
    """Print the configured runtime's models as the "Local" group.

    Shown in every mode, from the list ``/local url`` or ``/refresh-models``
    last fetched — listing never waits on the network.
    """
    if not state.has_local_runtime():
        return
    console.print(
        f"\n  [bold]Local[/bold]  [dim]{state.local_host_display()}[/dim]"
    )
    if not state.local_models:
        console.print("    [dim]no models installed — /local pull <model>[/dim]")
    for name in state.local_models:
        active = "[green]●[/green]" if is_active(name) else "[dim]○[/dim]"
        console.print(f"    {active} {state.local_provider()}:{name}")
    console.print("\n  [bold]Cloud[/bold]")


def _list_models(state: CliState, console: Console) -> None:
    console.print("\n  [dim]Usage: /models provider:model \\[provider:model ...][/dim]")
    chosen = set(state.selected_models)
    _print_local_group(
        state, console, lambda name: spec_for(state, name) in chosen
    )
    for provider in PROVIDER_ORDER:
        has_key = bool(state.api_keys.get(_KEY_ENV[provider], "").strip())
        console.print(
            f"\n  [bold]{PROVIDER_DISPLAY[provider]}[/bold]"
            + ("" if has_key else " [dim]— no API key[/dim]")
        )
        for m in models_for_provider(provider):
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
    console.print()


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


def _cmd_work_dir(args: list[str], state: CliState, console: Console) -> None:
    if not args:
        console.print("  [dim]Usage: /work-dir /path/to/dir[/dim]")
        return
    p = Path(" ".join(args)).expanduser().resolve()
    if not p.is_dir():
        print_error(console, f"Directory not found: {p}")
        return
    state.work_dir = str(p)
    print_ok(console, f"Working directory: {state.work_dir_display()}")


def _cmd_apikey(state: CliState, console: Console) -> None:
    from cli.setup import run_setup
    run_setup(state, console, editing=True)


def _cmd_config(args: list[str], state: CliState, console: Console) -> None:
    if not args:
        state.config_path = ""
        print_ok(
            console,
            "Config: auto  [dim]— ./mak.yaml → ~/.config/mak/config.yaml → "
            "built-in default[/dim]",
        )
    else:
        p = Path(args[0]).expanduser().resolve()
        if not p.exists():
            print_error(console, f"Config file not found: {p}")
            return
        state.config_path = str(p)
        print_ok(console, f"Config: {p}")


def _cmd_planner(args: list[str], state: CliState, console: Console) -> None:
    if not args:
        _list_planner_models(state, console)
        return

    raw = args[0]
    # A local model is not in the catalog by construction (mak/local's whole
    # premise is that the running server is the authority), so the catalog
    # lookup below must be skipped for one rather than rejecting it.
    if raw.startswith(_LOCAL_SPEC_PREFIXES) or _is_installed_locally(raw, state):
        _set_local_planner(raw, state, console)
        return

    model_id = raw
    if ":" in model_id:
        model_id = model_id.split(":", 1)[1]

    model_info = next((m for m in all_models() if m.model_id == model_id), None)
    if model_info is None:
        print_error(
            console,
            f"Unknown model: {model_id} — run [bold]/planner[/bold] to list models.",
        )
        return

    if not state.api_keys.get(model_info.api_key_env, "").strip():
        print_error(
            console,
            f"No API key for {model_info.provider} — "
            "run [bold]/apikey[/bold] to add one.",
        )
        return

    if state.mode == MODE_LOCAL:
        # A hosted planner beside local agents is hybrid, by definition.
        state.mode = MODE_HYBRID
    apply_cloud_planner(state, model_id)
    if not model_info.planner_ok:
        print_warn(
            console,
            f"Planner: {model_id} — may struggle with complex task decomposition.",
        )
    else:
        print_ok(console, f"Planner: {model_id}")
    caveat = model_caveat(model_id)
    if caveat:
        print_warn(console, caveat)


def _is_installed_locally(model: str, state: CliState) -> bool:
    """Whether ``model`` is one the configured local runtime reported."""
    return state.has_local_runtime() and model in state.local_models


def _set_local_planner(raw: str, state: CliState, console: Console) -> None:
    """Point the planner at a local model, taking the endpoint from state/spec."""
    spec, _, url = raw.partition("@")
    is_prefixed = spec.startswith(_LOCAL_SPEC_PREFIXES)
    model = spec.partition(":")[2] if is_prefixed else spec
    if url:
        state.local_base_url = url
        if not state.local_kind:
            state.local_kind = (
                KIND_OLLAMA
                if spec.startswith("ollama:")
                else KIND_OPENAI_COMPATIBLE
            )
    if not state.has_local_runtime():
        print_error(
            console,
            f"{raw} names no endpoint and no local runtime is configured — "
            "run [bold]/local[/bold] first.",
        )
        return
    apply_local_planner(state, model)
    print_ok(console, f"Planner: {model}  [dim]at {state.local_base_url}[/dim]")


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
        "\n  [dim]Usage: /planner <model>  —  models below claude-sonnet-4-6 capability"
        " are not recommended.[/dim]"
    )
    _print_local_group(
        state,
        console,
        lambda name: bool(state.planner_base_url) and name == state.planner_model,
    )
    for provider in PROVIDER_ORDER:
        has_key = bool(state.api_keys.get(_KEY_ENV[provider], "").strip())
        console.print(
            f"\n  [bold]{PROVIDER_DISPLAY[provider]}[/bold]"
            + ("" if has_key else " [dim]— no API key[/dim]")
        )
        for m in models_for_provider(provider):
            is_planner = m.model_id == state.planner_model
            active = "[green]●[/green]" if is_planner else "[dim]○[/dim]"
            if m.retired:
                tag = "  [yellow]⚠ no longer offered[/yellow]"
            elif m.planner_ok:
                tag = " [dim]★ recommended[/dim]"
            else:
                tag = "  [yellow]⚠ not recommended[/yellow]"
            if has_key:
                console.print(f"    {active} {m.model_id}{tag}")
            else:
                console.print(f"    [dim]○ {m.model_id}[/dim]")
    console.print()


def _cmd_refresh_models(state: CliState, console: Console) -> None:
    """Re-fetch every provider's model list now, ignoring the refresh schedule.

    This is the escape hatch for a model that ships between scheduled refreshes:
    the catalog updates in-session, so the new model is immediately selectable.
    """
    console.print("\n  [dim]Fetching model lists…[/dim]")
    if state.has_local_runtime():
        _refresh_local(state, console)
    try:
        report = registry().refresh_now(state.api_keys)
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


def _refresh_local(state: CliState, console: Console) -> None:
    """Re-list the connected runtime's models, reporting like a cloud provider."""
    label = f"Local [dim]({state.local_host_display()})[/dim]"
    try:
        added, removed = refresh_local_models(state)
    except OllamaError as exc:
        console.print(
            f"    [dim]○[/dim] [bold]{label}[/bold]  "
            f"[dim]{exc} — keeping cached list[/dim]"
        )
        return
    delta = f"  [dim](+{len(added)} −{len(removed)})[/dim]" if added or removed else ""
    console.print(
        f"    [green]●[/green] [bold]{label}[/bold]  "
        f"[dim]{len(state.local_models)} models[/dim]{delta}"
    )
    for model_id in added:
        console.print(f"        [green]+ {model_id}[/green]")
    for model_id in removed:
        console.print(f"        [red]- {model_id}[/red]")


def _warn_retired_selections(state: CliState, console: Console) -> None:
    retired = {m.model_id for m in all_models() if m.retired}
    in_use = [
        spec for spec in state.selected_models
        if spec.partition(":")[2] in retired
    ]
    if state.planner_model in retired:
        print_warn(
            console,
            f"Planner {state.planner_model} is no longer offered by its provider — "
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
