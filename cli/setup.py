"""Interactive API key + planner model setup (inline, no screen-switching)."""
from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule

from cli.core.api_keys import KEY_NAMES, any_key_set, save_keys
from cli.core.models import (
    PROVIDER_DISPLAY,
    PROVIDER_ORDER,
    models_for_provider,
    providers_with_keys,
    recommended_planner_for_provider,
)
from cli.core.state import CliState


def run_setup(state: CliState, console: Console, *, editing: bool = False) -> bool:
    """Collect API keys interactively, then select a planner model.

    Returns True if at least one key was saved; False if the user provided
    nothing (which means the caller should exit or retry).
    """
    from prompt_toolkit import prompt as pt_prompt
    from prompt_toolkit.styles import Style

    pt_style = Style.from_dict({"": "#c9d1d9", "prompt": "#58a6ff bold"})

    title = "Edit API Keys" if editing else "Welcome to MAK  —  Set up your API keys"
    console.print()
    console.print(Rule(title, style="cyan"))
    console.print()
    console.print(
        "  [dim]Press Enter to keep an existing value.  "
        "Leave blank to skip a provider.[/dim]"
    )
    console.print()

    provider_meta = [
        ("ANTHROPIC_API_KEY", "Anthropic API Key", "claude-sonnet-4-6 recommended"),
        ("OPENAI_API_KEY",    "OpenAI API Key",    "gpt-4o"),
        ("GEMINI_API_KEY",    "Google Gemini API Key", "gemini-2.0-flash"),
    ]

    for env_name, label, hint in provider_meta:
        existing = state.api_keys.get(env_name, "")
        filled = "●●●●●●●●" if existing else ""

        console.print(f"  [bold]{label}[/bold]  [dim]{hint}[/dim]")
        try:
            value = pt_prompt(
                f"  {env_name}: ",
                default=existing,
                is_password=True,
                style=pt_style,
            )
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Setup cancelled.[/dim]")
            return False
        state.api_keys[env_name] = value.strip()
        console.print()

    if not any_key_set(state.api_keys):
        console.print(
            Panel(
                "[bold red]✗  At least one API key is required.[/bold red]\n"
                "Run [bold]/apikey[/bold] to set one.",
                border_style="red",
            )
        )
        return False

    save_keys(state.api_keys)

    # ── Planner selection ────────────────────────────────────────────────────
    available = providers_with_keys(state.api_keys)
    if len(available) == 1:
        rec = recommended_planner_for_provider(available[0])
        state.planner_model = rec
        console.print(
            f"  [green]✓  Planner:[/green] [bold magenta]{rec}[/bold magenta]  "
            f"[dim](auto-selected — only {PROVIDER_DISPLAY[available[0]]} key set)[/dim]"
        )
    else:
        _select_planner(state, console, available)

    # ── Default model roster if none chosen ──────────────────────────────────
    if not state.selected_models and available:
        first = available[0]
        rec = recommended_planner_for_provider(first)
        state.selected_models = [f"{first}:{rec}"]

    console.print()
    return True


def _select_planner(state: CliState, console: Console, available: list[str]) -> None:
    from prompt_toolkit import prompt as pt_prompt
    from prompt_toolkit.styles import Style

    pt_style = Style.from_dict({"prompt": "#58a6ff bold"})

    console.print(Rule("Choose Planner Model", style="dim"))
    console.print()
    console.print(
        "  [dim]The planner is the 'brain' that decomposes your task into parallel "
        "sub-tasks.[/dim]\n"
        "  [bold cyan]Recommended:[/bold cyan] [dim]Anthropic · claude-sonnet-4-6 or higher[/dim]"
    )
    console.print()

    options: list[tuple[str, str]] = []
    for provider in PROVIDER_ORDER:
        if provider not in available:
            continue
        for m in models_for_provider(provider):
            rec_tag = "  (recommended)" if m.recommended else ""
            display = f"{PROVIDER_DISPLAY[provider]}  ·  {m.display_name}{rec_tag}"
            options.append((f"{provider}:{m.model_id}", display))

    for i, (spec, display) in enumerate(options, 1):
        console.print(f"  [dim]{i:>2})[/dim]  {display}")

    console.print()
    while True:
        try:
            raw = pt_prompt(
                f"  Select (1–{len(options)}): ",
                style=pt_style,
            )
        except (KeyboardInterrupt, EOFError):
            # Default to first recommended
            first_provider = available[0]
            state.planner_model = recommended_planner_for_provider(first_provider)
            return
        try:
            idx = int(raw.strip()) - 1
            if 0 <= idx < len(options):
                full_spec = options[idx][0]
                state.planner_model = full_spec.split(":")[1]
                console.print(
                    f"\n  [green]✓  Planner set to:[/green] "
                    f"[bold magenta]{full_spec}[/bold magenta]"
                )
                return
        except ValueError:
            pass
        console.print(f"  [red]Please enter a number between 1 and {len(options)}.[/red]")
