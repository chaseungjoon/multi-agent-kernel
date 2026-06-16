"""Slash-command handlers for the MAK CLI."""
from __future__ import annotations

from pathlib import Path

from rich.console import Console

from cli.core.models import (
    ALL_MODELS,
    PROVIDER_DISPLAY,
    PROVIDER_ORDER,
    models_for_provider,
)
from cli.core.state import CliState
from cli.ui import print_status_capsule


def handle_command(text: str, state: CliState, console: Console) -> None:
    parts = text.strip().split()
    if not parts:
        return
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
    else:
        console.print(f"[red]x  Unknown command:[/red] {cmd}")


# ── Handlers ──────────────────────────────────────────────────────────────────

def _cmd_models(args: list[str], state: CliState, console: Console) -> None:
    if not args:
        console.print("[dim]Usage: /models [provider[:model] ...][/dim]")
        _list_models(state, console)
        return

    valid: list[str] = []
    for spec in args:
        provider = spec.split(":")[0].lower()
        key_env = {
            "anthropic": "ANTHROPIC_API_KEY",
            "openai":    "OPENAI_API_KEY",
            "gemini":    "GEMINI_API_KEY",
        }.get(provider)
        if key_env is None:
            console.print(f"[red]x  Unknown provider:[/red] {provider}")
            return
        if not state.api_keys.get(key_env, "").strip():
            console.print(
                f"[bold red]x  No API key for {provider}.[/bold red]  "
                f"Run [bold]/apikey[/bold] to add one."
            )
            return
        valid.append(spec)

    if state.max_agents < len(valid):
        console.print(
            f"[red]x  max-agents ({state.max_agents}) < number of models ({len(valid)}).  "
            f"Run[/red] [bold]/max-agents {len(valid)}[/bold] [red]first.[/red]"
        )
        return

    state.selected_models = valid
    console.print(f"[green]ok  Models set to:[/green] {', '.join(valid)}")
    print_status_capsule(console, state)


def _list_models(state: CliState, console: Console) -> None:
    for provider in PROVIDER_ORDER:
        key_env = {
            "anthropic": "ANTHROPIC_API_KEY",
            "openai":    "OPENAI_API_KEY",
            "gemini":    "GEMINI_API_KEY",
        }[provider]
        has_key = bool(state.api_keys.get(key_env, "").strip())
        console.print(
            f"\n  [bold]{PROVIDER_DISPLAY[provider]}[/bold]"
            + ("" if has_key else " [dim](no API key)[/dim]")
        )
        for m in models_for_provider(provider):
            rec    = " [dim](recommended)[/dim]" if m.recommended else ""
            spec   = f"{provider}:{m.model_id}"
            active = " [green]ok[/green]" if spec in state.selected_models else "   "
            if has_key:
                console.print(f"  {active}  {spec}{rec}")
            else:
                console.print(f"  [dim]    {spec}{rec}[/dim]")
    console.print()


def _cmd_max_agents(args: list[str], state: CliState, console: Console) -> None:
    if not args:
        console.print("[dim]Usage: /max-agents N[/dim]")
        return
    try:
        n = int(args[0])
        if n < 1:
            raise ValueError
    except ValueError:
        console.print("[red]x  /max-agents requires a positive integer.[/red]")
        return
    if n < len(state.selected_models):
        console.print(
            f"[red]x  {n} < number of selected models ({len(state.selected_models)}).[/red]"
        )
        return
    state.max_agents = n
    console.print(f"[green]ok  Max agents set to {n}.[/green]")
    print_status_capsule(console, state)


def _cmd_work_dir(args: list[str], state: CliState, console: Console) -> None:
    if not args:
        console.print("[dim]Usage: /work-dir /path/to/dir[/dim]")
        return
    p = Path(" ".join(args)).expanduser().resolve()
    if not p.is_dir():
        console.print(f"[red]x  Directory not found:[/red] {p}")
        return
    state.work_dir = str(p)
    console.print(f"[green]ok  Working directory set to[/green] {p}")
    print_status_capsule(console, state)


def _cmd_apikey(state: CliState, console: Console) -> None:
    from cli.setup import run_setup
    run_setup(state, console, editing=True)
    # setup already prints its own summary; reprint capsule so state is visible
    print_status_capsule(console, state)


def _cmd_config(args: list[str], state: CliState, console: Console) -> None:
    if not args:
        state.config_path = "mak/config.yaml"
        console.print("[green]ok  Config reset to mak/config.yaml[/green]")
    else:
        p = Path(args[0]).expanduser().resolve()
        if not p.exists():
            console.print(f"[red]x  Config file not found:[/red] {p}")
            return
        state.config_path = str(p)
        console.print(f"[green]ok  Config set to[/green] {p}")
    print_status_capsule(console, state)


def _cmd_planner(args: list[str], state: CliState, console: Console) -> None:
    if not args:
        _list_planner_models(state, console)
        return

    model_id = args[0]
    if ":" in model_id:
        model_id = model_id.split(":", 1)[1]

    model_info = next((m for m in ALL_MODELS if m.model_id == model_id), None)
    if model_info is None:
        console.print(f"[red]x  Unknown model:[/red] {model_id}")
        console.print("  Run [bold]/planner[/bold] to see available models.")
        return

    if not state.api_keys.get(model_info.api_key_env, "").strip():
        console.print(
            f"[bold red]x  No API key for {model_info.provider}.[/bold red]  "
            f"Run [bold]/apikey[/bold] to add one."
        )
        return

    state.planner_model = model_id
    if not model_info.planner_ok:
        console.print(
            f"[yellow]ok  Planner set to {model_id}.[/yellow]  "
            "[yellow]⚠ This model may struggle with complex task decomposition.[/yellow]"
        )
    else:
        console.print(f"[green]ok  Planner set to {model_id}.[/green]")
    print_status_capsule(console, state)


def _list_planner_models(state: CliState, console: Console) -> None:
    console.print(
        "\n  [yellow]⚠  Models below [bold]claude-sonnet-4-6[/bold] capability are not"
        " recommended as planners — complex task decomposition may produce malformed"
        " or incomplete plans.[/yellow]"
    )
    for provider in PROVIDER_ORDER:
        key_env = {
            "anthropic": "ANTHROPIC_API_KEY",
            "openai":    "OPENAI_API_KEY",
            "gemini":    "GEMINI_API_KEY",
        }[provider]
        has_key = bool(state.api_keys.get(key_env, "").strip())
        console.print(
            f"\n  [bold]{PROVIDER_DISPLAY[provider]}[/bold]"
            + ("" if has_key else " [dim](no API key)[/dim]")
        )
        for m in models_for_provider(provider):
            active  = " [green]ok[/green]" if m.model_id == state.planner_model else "   "
            warning = "  [yellow]⚠ not recommended[/yellow]" if not m.planner_ok else ""
            if has_key:
                console.print(f"  {active}  {m.model_id}{warning}")
            else:
                console.print(f"  [dim]    {m.model_id}[/dim]")
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
            console.print(
                f"[red]x  /no-review expects true or false, got:[/red] {args[0]}\n"
                "  example: /no-review true   (skip human approval)\n"
                "           /no-review false  (require approval — default)"
            )
            return
    if state.no_review:
        console.print(
            "[yellow]ok  Human approval disabled.[/yellow]  "
            "MAK will run plans immediately.  Use [bold]/no-review false[/bold] to re-enable."
        )
    else:
        console.print(
            "[green]ok  Human approval enabled.[/green]  "
            "MAK will show the plan and wait before running."
        )
    print_status_capsule(console, state)
