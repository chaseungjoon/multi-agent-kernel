"""Main CLI loop — the inline, Claude Code-style entry point for MAK."""
from __future__ import annotations

import sys
import threading
from typing import Any

from prompt_toolkit import PromptSession, prompt as pt_prompt
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.formatted_text import HTML, FormattedText
from prompt_toolkit.history import InMemoryHistory
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
from cli.ui import (
    print_hints,
    print_logo,
    print_status_capsule,
    show_diff,
    show_plan,
    show_results,
)

_STYLE = Style.from_dict({
    "prompt":                                  "#ff8c00 bold",
    "placeholder":                             "#484f58",
    "completion-menu.completion":              "bg:#1c2128 #c9d1d9",
    "completion-menu.completion.current":      "bg:#0d1117 #58a6ff bold",
    "completion-menu.meta.completion":         "bg:#1c2128 #6e7681",
    "completion-menu.meta.completion.current": "bg:#0d1117 #6e7681",
    "auto-suggestion":                         "#484f58",
})

_PLACEHOLDER = FormattedText([("class:placeholder", "  What should the agents do?")])


class MakCli:
    def __init__(self) -> None:
        self.console         = Console()
        self.state           = self._init_state()
        self._history        = InMemoryHistory()
        self._session_tokens = 0
        install_token_counter()

    # ── Entry point ────────────────────────────────────────────────────────────

    def run(self) -> None:
        if not any_key_set(self.state.api_keys):
            ok = run_setup(self.state, self.console)
            if not ok:
                sys.exit(1)

        print_logo(self.console)
        print_status_capsule(self.console, self.state)
        print_hints(self.console)

        while True:
            # Rebuild each iteration so completions always reflect current state.
            session = self._build_session()
            try:
                raw = session.prompt(
                    FormattedText([("class:prompt", "❯ ")]),
                    placeholder=_PLACEHOLDER,
                )
            except KeyboardInterrupt:
                self._print_session_end()
                break
            except EOFError:
                self._print_session_end()
                break

            text = raw.strip()
            if not text:
                continue

            if text.startswith("/"):
                handle_command(text, self.state, self.console)
            else:
                self._execute_task(text)

    # ── Task execution ─────────────────────────────────────────────────────────

    def _execute_task(self, task: str) -> None:
        console = self.console
        state   = self.state

        console.print()
        console.print(Rule(f"[bold #ff8c00]{task}[/bold #ff8c00]", style="dim"))
        console.print()

        # Capture pre-task HEAD so the diff covers every commit MAK makes.
        pre_hash = get_pre_task_hash(state.work_dir)
        reset_token_counter()

        # ── 1. Build MAK session ───────────────────────────────────────────────
        try:
            mak_session = build_session(task, state)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[bold red]x  Configuration error:[/bold red] {exc}")
            return

        # ── 2. Initialize ──────────────────────────────────────────────────────
        with console.status("[dim]Initializing...[/dim]", spinner="dots"):
            try:
                mak_session.initialize()
            except Exception as exc:  # noqa: BLE001
                console.print(f"[bold red]x  Initialization failed:[/bold red] {exc}")
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

        with console.status("[bold blue]Planning...[/bold blue]", spinner="aesthetic"):
            plan_done.wait()

        if plan_error is not None:
            console.print(f"[bold red]x  Planning failed:[/bold red] {plan_error}")
            return

        if not subtasks:
            console.print("[yellow]  Planner produced an empty plan.[/yellow]")
            return

        # ── 4. Show plan ───────────────────────────────────────────────────────
        show_plan(console, subtasks, task)

        # ── 5. Human approval ──────────────────────────────────────────────────
        if not state.no_review:
            console.print("[dim]  Review the plan above.  Ctrl+C to cancel.[/dim]")
            console.print()
            try:
                ans = pt_prompt(
                    HTML(
                        "<ansiyellow><b>  Proceed with this plan?</b></ansiyellow>"
                        " [<ansigreen>y</ansigreen>/N]  "
                        "<ansiyellow><b>❯</b></ansiyellow> "
                    ),
                    style=_STYLE,
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "n"
            if ans not in ("y", "yes"):
                console.print("\n[dim]  Cancelled.[/dim]\n")
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
            SpinnerColumn("aesthetic"),
            TextColumn("[bold cyan]Working...[/bold cyan]"),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        ) as progress:
            progress.add_task("", total=None)
            run_done.wait()

        if run_error is not None:
            console.print(f"[bold red]x  Execution error:[/bold red] {run_error}")
            return

        # ── 7. Teardown ────────────────────────────────────────────────────────
        tests_passed = True
        with console.status("[dim]Running tests...[/dim]", spinner="dots"):
            try:
                tests_passed = mak_session.teardown()
            except Exception as exc:  # noqa: BLE001
                console.print(f"[yellow]  Teardown error:[/yellow] {exc}")

        # ── 8. Results + diff ──────────────────────────────────────────────────
        self._session_tokens += read_token_counter()

        show_results(console, run_result, tests_passed)

        diff = get_git_diff(state.work_dir, pre_hash)
        if diff.strip():
            show_diff(console, diff)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _print_session_end(self) -> None:
        tokens = self._session_tokens
        if tokens > 0:
            self.console.print(
                f"\n[dim]Session ended.  {tokens:,} tokens used.[/dim]\n"
            )
        else:
            self.console.print("\n[dim]Session ended.[/dim]\n")

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
        )
