"""Main CLI loop — the inline, Claude Code-style entry point for MAK."""
from __future__ import annotations

import sys
import threading
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.rule import Rule

from cli.commands import handle_command
from cli.completer import MakCompleter
from cli.core.api_keys import any_key_set, load_keys
from cli.core.models import providers_with_keys, recommended_planner_for_provider
from cli.core.state import CliState
from cli.runner import (
    build_session,
    get_git_diff,
    get_pre_task_hash,
    install_token_counter,
    plan_in_thread,
    read_token_counter,
    reset_token_counter,
    run_session_in_thread,
)
from cli.setup import run_setup
from cli.ui import ACCENT, print_banner, show_diff, show_plan, show_results

_STYLE = Style.from_dict({
    "prompt":                                  f"{ACCENT} bold",
    "placeholder":                             "#484f58",
    "completion-menu":                         "bg:#1c2128 #c9d1d9",
    "completion-menu.completion":              "bg:#1c2128 #c9d1d9",
    "completion-menu.completion.current":      f"bg:#30363d {ACCENT} bold",
    "completion-menu.meta.completion":         "bg:#1c2128 #6e7681",
    "completion-menu.meta.completion.current": "bg:#30363d #8b949e",
    "scrollbar.background":                    "bg:#1c2128",
    "scrollbar.button":                        "bg:#30363d",
    "auto-suggestion":                         "#484f58",
    "bottom-toolbar":                          "noreverse bg:default #6e7681",
    "bottom-toolbar.accent":                   f"noreverse bg:default {ACCENT}",
    "bottom-toolbar.value":                    "noreverse bg:default #8b949e",
})

_PLACEHOLDER = FormattedText(
    [("class:placeholder", "Describe a task…  (/ for commands)")]
)


def _key_bindings() -> KeyBindings:
    kb = KeyBindings()

    @kb.add("c-j")
    def _newline(event: Any) -> None:
        """Ctrl+J inserts a newline for multi-line tasks."""
        event.current_buffer.insert_text("\n")

    return kb


class MakCli:
    def __init__(self) -> None:
        self.console         = Console(highlight=False)
        self.state           = self._init_state()
        self._history        = InMemoryHistory()
        self._session_tokens = 0
        self._prompt_session = self._build_session()
        install_token_counter()

    # ── Entry point ────────────────────────────────────────────────────────────

    def run(self) -> None:
        if not any_key_set(self.state.api_keys):
            ok = run_setup(self.state, self.console)
            if not ok:
                sys.exit(1)

        print_banner(self.console, self.state)

        while True:
            try:
                raw = self._prompt_session.prompt(
                    FormattedText([("class:prompt", "❯ ")]),
                    placeholder=_PLACEHOLDER,
                )
            except (KeyboardInterrupt, EOFError):
                self._print_session_end()
                break

            text = raw.strip()
            if not text:
                continue

            if text.startswith("/"):
                action = handle_command(text, self.state, self.console)
                if action == "exit":
                    self._print_session_end()
                    break
                if action == "clear":
                    self.console.clear()
                    print_banner(self.console, self.state)
            else:
                self._execute_task(text)

    # ── Task execution ─────────────────────────────────────────────────────────

    def _execute_task(self, task: str) -> None:
        console = self.console
        state   = self.state

        console.print()
        console.print(Rule(f"[bold {ACCENT}]{task}[/bold {ACCENT}]", style="dim"))

        # Capture pre-task HEAD so the diff covers every commit MAK makes.
        pre_hash = get_pre_task_hash(state.work_dir)
        reset_token_counter()

        # ── 1. Build MAK session ───────────────────────────────────────────────
        try:
            mak_session = build_session(task, state)
        except Exception as exc:  # noqa: BLE001
            console.print(f"  [red]✗[/red] Configuration error: {exc}")
            return

        # ── 2. Initialize ──────────────────────────────────────────────────────
        with console.status("[dim]Initializing…[/dim]", spinner="dots"):
            try:
                mak_session.initialize()
            except Exception as exc:  # noqa: BLE001
                console.print(f"  [red]✗[/red] Initialization failed: {exc}")
                return

        # ── 3. Plan ────────────────────────────────────────────────────────────
        subtasks: list[Any]          = []
        plan_error: Exception | None = None
        plan_done = threading.Event()

        def _plan() -> None:
            nonlocal subtasks, plan_error
            subtasks, plan_error = plan_in_thread(mak_session, task)
            plan_done.set()

        threading.Thread(target=_plan, daemon=True).start()

        with console.status(f"[{ACCENT}]Planning…[/{ACCENT}]", spinner="dots"):
            plan_done.wait()

        if plan_error is not None:
            console.print(f"  [red]✗[/red] Planning failed: {plan_error}")
            return

        if not subtasks:
            console.print("  [yellow]⚠[/yellow] Planner produced an empty plan.")
            return

        # ── 4. Show plan ───────────────────────────────────────────────────────
        show_plan(console, subtasks)

        # ── 5. Human approval ──────────────────────────────────────────────────
        if not state.no_review:
            if not self._confirm_plan():
                console.print("  [dim]Cancelled.[/dim]\n")
                return
            console.print()

        # ── 6. Run ─────────────────────────────────────────────────────────────
        mak_session.install_plan(subtasks)

        run_result: Any               = None
        run_error:  Exception | None  = None
        run_done = threading.Event()

        def _run() -> None:
            nonlocal run_result, run_error
            run_result, run_error = run_session_in_thread(mak_session)
            run_done.set()

        threading.Thread(target=_run, daemon=True).start()

        with Progress(
            SpinnerColumn("dots", style=ACCENT),
            TextColumn(f"[{ACCENT}]Working…[/{ACCENT}]"),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        ) as progress:
            progress.add_task("", total=None)
            run_done.wait()

        if run_error is not None:
            console.print(f"  [red]✗[/red] Execution error: {run_error}")
            return

        # ── 7. Teardown ────────────────────────────────────────────────────────
        tests_passed = True
        with console.status("[dim]Running tests…[/dim]", spinner="dots"):
            try:
                tests_passed = mak_session.teardown()
            except Exception as exc:  # noqa: BLE001
                console.print(f"  [yellow]⚠[/yellow] Teardown error: {exc}")

        # ── 8. Results + diff ──────────────────────────────────────────────────
        self._session_tokens += read_token_counter()

        show_results(console, run_result, tests_passed)

        diff = get_git_diff(state.work_dir, pre_hash)
        if diff.strip():
            show_diff(console, diff)

    def _confirm_plan(self) -> bool:
        """Single-line plan approval: Enter/y runs, anything else cancels."""
        from prompt_toolkit import prompt as pt_prompt

        try:
            ans = pt_prompt(
                FormattedText([
                    ("", "  "),
                    ("bold", "Run this plan?"),
                    ("class:placeholder", "  y/N · Ctrl+C cancels  "),
                    ("class:prompt", "❯ "),
                ]),
                style=_STYLE,
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return ans in ("y", "yes")

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _toolbar(self) -> FormattedText:
        """Live session state, rendered under the prompt on every keystroke."""
        state = self.state
        fragments: list[tuple[str, str]] = [("class:bottom-toolbar", "  ")]

        def item(
            label: str, value: str, style: str = "class:bottom-toolbar.value"
        ) -> None:
            if len(fragments) > 1:
                fragments.append(("class:bottom-toolbar", "  ·  "))
            fragments.append(("class:bottom-toolbar", f"{label} "))
            fragments.append((style, value))

        item("model", state.models_display())
        item("planner", state.planner_model)
        item("agents", str(state.max_agents))
        item("dir", state.work_dir_display())
        approval_style = (
            "class:bottom-toolbar.accent" if state.no_review
            else "class:bottom-toolbar.value"
        )
        item("approval", "off" if state.no_review else "on", approval_style)
        if self._session_tokens:
            item("tokens", f"{self._session_tokens:,}")
        return FormattedText(fragments)

    def _print_session_end(self) -> None:
        tokens = self._session_tokens
        suffix = f"  ·  {tokens:,} tokens used" if tokens > 0 else ""
        self.console.print(f"\n  [dim]Session ended{suffix}.[/dim]\n")

    def _init_state(self) -> CliState:
        keys  = load_keys()
        state = CliState(api_keys=keys)
        avail = providers_with_keys(keys)
        if avail:
            first = avail[0]
            rec   = recommended_planner_for_provider(first)
            state.planner_model   = rec
            state.selected_models = [f"{first}:{rec}"]
        return state

    def _build_session(self) -> PromptSession[str]:
        return PromptSession(
            style=_STYLE,
            completer=MakCompleter(self.state),
            auto_suggest=AutoSuggestFromHistory(),
            history=self._history,
            complete_while_typing=True,
            enable_open_in_editor=False,
            key_bindings=_key_bindings(),
            bottom_toolbar=self._toolbar,
            reserve_space_for_menu=7,
        )
