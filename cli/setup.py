"""Interactive API key + planner model setup (inline, no screen-switching)."""
from __future__ import annotations

import threading
from collections.abc import Callable

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
from cli.core.state import (
    MODE_CLOUD,
    MODE_HYBRID,
    MODE_LOCAL,
    MODES,
    CliState,
    mode_summary,
)
from cli.ui import ACCENT, print_error, print_ok, print_warn
from mak.config import model_caveat
from mak.local import LocalRuntime


def run_setup(state: CliState, console: Console, *, editing: bool = False) -> bool:
    """Set the session up: cloud keys, a local runtime, or both.

    ``editing=True`` (what ``/apikey`` passes) goes straight to the key wizard;
    it is the "change my keys" path and has nothing to ask about mode.

    A first run asks the question first, before any API-key prompt, so a
    fully-offline user can reach it at all: setup can end successfully with
    **zero** keys.

    Returns whether the session is usable afterwards.
    """
    if editing:
        return run_key_setup(state, console, editing=True)
    return _run_first_run(state, console)


def _run_first_run(state: CliState, console: Console) -> bool:
    """Ask cloud / local / hybrid, then run the matching wizard."""
    from cli.local import run_wizard

    # Detection runs while the question is being read, so the menu can be
    # honest about what is actually on this machine rather than describing
    # local mode in the abstract.
    detected = _detect_in_background(state.local_seams.discover)

    console.print()
    console.print(Rule("[bold]Welcome to MAK[/bold]", style="dim"))
    console.print()
    console.print("  How do you want to run models?")
    console.print()
    runtimes = detected()
    keys_set = sum(1 for value in state.api_keys.values() if value.strip())
    notes = {
        MODE_CLOUD: (
            f"[green]●[/green] {keys_set} key(s) set" if keys_set
            else "[dim]● no keys set yet[/dim]"
        ),
        MODE_LOCAL: (
            f"[green]●[/green] {runtimes[0].describe()}" if runtimes
            else "[dim]— no local runtime detected[/dim]"
        ),
        MODE_HYBRID: "[dim]recommended when the local model is small[/dim]",
    }
    labels = {MODE_CLOUD: "Cloud ", MODE_LOCAL: "Local ", MODE_HYBRID: "Hybrid"}
    for index, mode in enumerate(MODES, 1):
        console.print(
            f"    [dim]{index})[/dim]  [bold]{labels[mode]}[/bold]  "
            f"[dim]{mode_summary(mode):<42}[/dim]{notes[mode]}"
        )
    console.print()
    choice = _ask_choice(console, len(MODES))
    mode = MODES[choice]

    if mode == MODE_CLOUD:
        return run_key_setup(state, console)
    if mode == MODE_HYBRID:
        if not run_key_setup(state, console, planner_only=True):
            return False
        return run_wizard(state, console) or True
    if not runtimes:
        # Choosing local with nothing running is not a failure — it is a user
        # who needs install instructions, which the wizard prints. They still
        # reach the prompt.
        run_wizard(state, console)
        return True
    state.mode = MODE_LOCAL
    run_wizard(state, console)
    return True


def _detect_in_background(
    discover: Callable[[], list[LocalRuntime]],
) -> Callable[[], list[LocalRuntime]]:
    """Start a discovery scan now; return a function that waits for its result."""
    found: list[LocalRuntime] = []

    def scan() -> None:
        found.extend(discover())

    thread = threading.Thread(target=scan, daemon=True)
    thread.start()

    def result() -> list[LocalRuntime]:
        thread.join(timeout=3.0)
        return list(found)

    return result


def _ask_choice(console: Console, count: int) -> int:
    """Ask for a 1-based menu choice; return a 0-based index (0 on anything else)."""
    from prompt_toolkit import prompt as pt_prompt
    from prompt_toolkit.styles import Style

    pt_style = Style.from_dict({"prompt": f"{ACCENT} bold"})
    try:
        raw = pt_prompt(f"  Select (1–{count}): ", style=pt_style)
    except (KeyboardInterrupt, EOFError):
        console.print()
        return 0
    try:
        index = int(raw.strip()) - 1
    except ValueError:
        return 0
    return index if 0 <= index < count else 0


def run_key_setup(
    state: CliState,
    console: Console,
    *,
    editing: bool = False,
    planner_only: bool = False,
) -> bool:
    """Collect API keys interactively, then select a planner model.

    Returns True if at least one key was saved; False if the user provided
    nothing (which means the caller should exit or retry).
    """
    from prompt_toolkit import prompt as pt_prompt
    from prompt_toolkit.styles import Style

    pt_style = Style.from_dict({"": "#c9d1d9", "prompt": f"{ACCENT} bold"})

    if editing:
        title = "API keys"
    elif planner_only:
        title = "Planner API key — your agents will run locally"
    else:
        title = "Set up your API keys"
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
        rec = recommended_planner_for_provider(state.models(), available[0])
        state.set_cloud_planner(available[0], rec)
        print_ok(
            console,
            f"Planner: [bold]{state.planner_spec()}[/bold] [dim](auto-selected — only "
            f"{PROVIDER_DISPLAY[available[0]]} key set)[/dim]",
        )
    else:
        _select_planner(state, console, available)

    # ── Default model roster if none chosen ──────────────────────────────────
    if not state.selected_models and available:
        first = available[0]
        rec = recommended_planner_for_provider(state.models(), first)
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
        for m in models_for_provider(state.models(), provider):
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
            state.set_cloud_planner(
                first_provider,
                recommended_planner_for_provider(state.models(), first_provider),
            )
            return
        try:
            idx = int(raw.strip()) - 1
            if 0 <= idx < len(options):
                full_spec = options[idx][0]
                provider, _, model = full_spec.partition(":")
                state.set_cloud_planner(provider, model)
                console.print()
                print_ok(console, f"Planner: [bold]{full_spec}[/bold]")
                caveat = model_caveat(state.planner_model)
                if caveat:
                    print_warn(console, caveat)
                return
        except ValueError:
            pass
        console.print(
            f"  [red]Please enter a number between 1 and {len(options)}.[/red]"
        )
