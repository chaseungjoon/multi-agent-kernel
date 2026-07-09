"""Slash-command handlers for the MAK CLI.

``handle_command`` returns an action for the main loop: ``"exit"`` to quit,
``"clear"`` to clear the screen, or ``None`` to keep prompting. State feedback
is a single ✓/✗ line — live settings are always visible in the prompt toolbar.
"""
from __future__ import annotations

from pathlib import Path

from rich.console import Console

from cli.completer import COMMANDS
from cli.core.models import (
    ALL_MODELS,
    PROVIDER_DISPLAY,
    PROVIDER_ORDER,
    models_for_provider,
)
from cli.core.state import CliState
from cli.ui import ACCENT, print_error, print_ok, print_status, print_warn

_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai":    "OPENAI_API_KEY",
    "gemini":    "GEMINI_API_KEY",
}


def handle_command(text: str, state: CliState, console: Console) -> str | None:
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


def _cmd_models(args: list[str], state: CliState, console: Console) -> None:
    if not args:
        _list_models(state, console)
        return

    valid: list[str] = []
    for spec in args:
        provider = spec.split(":")[0].lower()
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
    print_ok(console, f"Models: {', '.join(valid)}")


def _list_models(state: CliState, console: Console) -> None:
    console.print("\n  [dim]Usage: /models provider:model \\[provider:model ...][/dim]")
    for provider in PROVIDER_ORDER:
        has_key = bool(state.api_keys.get(_KEY_ENV[provider], "").strip())
        console.print(
            f"\n  [bold]{PROVIDER_DISPLAY[provider]}[/bold]"
            + ("" if has_key else " [dim]— no API key[/dim]")
        )
        for m in models_for_provider(provider):
            rec    = " [dim]★ recommended[/dim]" if m.recommended else ""
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
        state.config_path = "mak/config.yaml"
        print_ok(console, "Config reset to mak/config.yaml")
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

    model_id = args[0]
    if ":" in model_id:
        model_id = model_id.split(":", 1)[1]

    model_info = next((m for m in ALL_MODELS if m.model_id == model_id), None)
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

    state.planner_model = model_id
    if not model_info.planner_ok:
        print_warn(
            console,
            f"Planner: {model_id} — may struggle with complex task decomposition.",
        )
    else:
        print_ok(console, f"Planner: {model_id}")


def _list_planner_models(state: CliState, console: Console) -> None:
    console.print(
        "\n  [dim]Usage: /planner <model>  —  models below claude-sonnet-4-6 capability"
        " are not recommended.[/dim]"
    )
    for provider in PROVIDER_ORDER:
        has_key = bool(state.api_keys.get(_KEY_ENV[provider], "").strip())
        console.print(
            f"\n  [bold]{PROVIDER_DISPLAY[provider]}[/bold]"
            + ("" if has_key else " [dim]— no API key[/dim]")
        )
        for m in models_for_provider(provider):
            is_planner = m.model_id == state.planner_model
            active  = "[green]●[/green]" if is_planner else "[dim]○[/dim]"
            warning = "  [yellow]⚠ not recommended[/yellow]" if not m.planner_ok else ""
            if has_key:
                console.print(f"    {active} {m.model_id}{warning}")
            else:
                console.print(f"    [dim]○ {m.model_id}[/dim]")
    console.print()


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
