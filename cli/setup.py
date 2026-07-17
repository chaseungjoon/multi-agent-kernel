"""Interactive API key + planner model setup (inline, no screen-switching)."""
from __future__ import annotations

from rich.console import Console
from rich.rule import Rule

from cli.core.api_keys import any_key_set, save_keys
from cli.core.models import (
    PROVIDER_DISPLAY,
    PROVIDER_ORDER,
    models_for_provider,
    providers_with_keys,
    recommended_planner_for_provider,
)
from cli.core.state import CliState
from cli.ui import ACCENT, print_error, print_ok


def run_setup(state: CliState, console: Console, *, editing: bool = False) -> bool:
    """Collect API keys interactively, then select a planner model.

    Returns True if at least one key was saved; False if the user provided
    nothing (which means the caller should exit or retry).
    """
    from prompt_toolkit import prompt as pt_prompt
    from prompt_toolkit.styles import Style

    pt_style = Style.from_dict({"": "#c9d1d9", "prompt": f"{ACCENT} bold"})

    title = "API keys" if editing else "Welcome to MAK — set up your API keys"
    console.print()
    console.print(Rule(f"[bold]{title}[/bold]", style="dim"))
    console.print()
    console.print(
        "  [dim]Press Enter to keep an existing value. "
        "Leave blank to skip a provider.[/dim]"
    )
    console.print()

    provider_meta = [
        ("ANTHROPIC_API_KEY", "Anthropic", "claude-sonnet-5 recommended"),
        ("OPENAI_API_KEY",    "OpenAI", "gpt-5.6-sol"),
        ("GEMINI_API_KEY",    "Google Gemini", "gemini-3.5-flash"),
    ]

    for env_name, label, hint in provider_meta:
        existing = state.api_keys.get(env_name, "")
        status = " [green]●[/green] [dim]key set[/dim]" if existing else ""

        console.print(f"  [bold]{label}[/bold]  [dim]{hint}[/dim]{status}")
        try:
            value = pt_prompt(
                f"  {env_name}: ",
                default=existing,
                is_password=True,
                style=pt_style,
            )
        except (KeyboardInterrupt, EOFError):
            console.print("\n  [dim]Setup cancelled.[/dim]")
            return False
        state.api_keys[env_name] = value.strip()
        console.print()

    if not any_key_set(state.api_keys):
        print_error(
            console,
            "At least one API key is required — run [bold]/apikey[/bold] to set one.",
        )
        return False

    save_keys(state.api_keys)

    # ── Planner selection ────────────────────────────────────────────────────
    available = providers_with_keys(state.api_keys)
    if len(available) == 1:
        rec = recommended_planner_for_provider(available[0])
        state.planner_model = rec
        print_ok(
            console,
            f"Planner: [bold]{rec}[/bold] [dim](auto-selected — only "
            f"{PROVIDER_DISPLAY[available[0]]} key set)[/dim]",
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

    pt_style = Style.from_dict({"prompt": f"{ACCENT} bold"})

    console.print(Rule("[bold]Planner model[/bold]", style="dim"))
    console.print()
    console.print(
        "  [dim]The planner decomposes your task into parallel sub-tasks.\n"
        "  Recommended: claude-sonnet-4-6-class capability or higher.[/dim]"
    )
    console.print()

    options: list[tuple[str, str]] = []
    for provider in PROVIDER_ORDER:
        if provider not in available:
            continue
        for m in models_for_provider(provider):
            # Planner bar: sonnet-4-6-class capability and up is recommended;
            # anything below gets an explicit warning.
            tag = (
                "  [dim]★ recommended[/dim]" if m.planner_ok
                else "  [yellow]⚠ not recommended[/yellow]"
            )
            display = f"{PROVIDER_DISPLAY[provider]} · {m.display_name}{tag}"
            options.append((f"{provider}:{m.model_id}", display))

    for i, (_spec, display) in enumerate(options, 1):
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
                console.print()
                print_ok(console, f"Planner: [bold]{full_spec}[/bold]")
                return
        except ValueError:
            pass
        console.print(
            f"  [red]Please enter a number between 1 and {len(options)}.[/red]"
        )
