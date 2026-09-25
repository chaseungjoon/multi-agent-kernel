"""Main CLI loop — the inline, Claude Code-style entry point for MAK."""
from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable
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
from cli.core.models import default_planner_route, providers_with_keys
from cli.core.state import CliState
from cli.local import restore_saved_hosts
from cli.project_config import offer_project_config
from cli.runner import (
    build_session,
    get_git_diff,
    get_pre_task_hash,
    plan_in_thread,
    run_session_in_thread,
    session_tokens,
)
from cli.setup import run_setup
from cli.ui import ACCENT, print_banner, show_diff, show_plan, show_results
from mak.cascade import run_cascade_waves
from mak.execution_result import ExecutionResult
from mak.teardown import SuiteOutcome, TeardownResult

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


_LOG = logging.getLogger(__name__)


def _auto_refresh_enabled(state: CliState) -> bool:
    """Read ``models.auto_refresh`` from the session's config (default on).

    Any config problem falls back to enabled — the registry still applies its own
    due/cooldown/opt-out checks, so this never forces an unwanted fetch.
    """
    try:
        from mak.config import load_config
        return bool(load_config(state.config_file()).models.auto_refresh)
    except Exception:  # noqa: BLE001 - config trouble must not block startup
        return True


class MakCli:
    """The interactive MAK terminal app: prompt, commands, and task runs."""

    def __init__(self) -> None:
        self.console         = Console(highlight=False)
        self.state           = self._init_state()
        self._history        = InMemoryHistory()
        self._session_tokens = 0
        self._prompt_session = self._build_session()

    # ── Entry point ────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Run the interactive loop until the user exits."""
        if not any_key_set(self.state.api_keys) and not self.state.has_local_runtime():
            # Setup can now end successfully with zero keys — a fully-local user
            # has none, and exiting here is what used to make MAK unreachable
            # for them. Only an explicitly cancelled setup still exits.
            if not run_setup(self.state, self.console):
                sys.exit(1)

        print_banner(self.console, self.state)
        offer_project_config(self.state, self.console)

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
                action = self._dispatch_command(text)
                if action == "exit":
                    self._print_session_end()
                    break
                if action == "clear":
                    self.console.clear()
                    print_banner(self.console, self.state)
                if action == "work_dir":
                    offer_project_config(self.state, self.console)
            else:
                self._execute_task(text)

    def _dispatch_command(self, text: str) -> str | None:
        """Run one slash command; an unexpected error costs the command only.

        The session — work dir, planner, roster, mode, keys — lives in this
        loop, so an exception escaping a handler used to end all of it behind
        a raw traceback. ``KeyboardInterrupt`` and ``EOFError`` are not caught
        here: they are how the user leaves, and the loop handles them.
        """
        try:
            return handle_command(text, self.state, self.console)
        except (KeyboardInterrupt, EOFError):
            raise
        except Exception as exc:  # noqa: BLE001 - contained by design, see above
            command = text.split(maxsplit=1)[0]
            _LOG.debug("slash command %s failed", command, exc_info=True)
            self.console.print(
                f"  [red]✗[/red] {command} failed: {type(exc).__name__}: {exc}"
            )
            return None

    # ── Task execution ─────────────────────────────────────────────────────────

    def _execute_task(self, task: str) -> None:
        console = self.console
        state   = self.state

        console.print()
        console.print(Rule(f"[bold {ACCENT}]{task}[/bold {ACCENT}]", style="dim"))

        # Capture pre-task HEAD so the diff covers every commit MAK makes.
        pre_hash = get_pre_task_hash(state.work_dir)

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
        mak_session.install_plan(subtasks, objective=task)

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

        # ── 6b. Cascade waves ──────────────────────────────────────────────────
        # The same loop `mak run` drives (mak.cascade): a wave can leave callers
        # of a changed signature broken, or two new modules disagreeing about
        # each other's API. This used to run only from the command line, so the
        # same defect was reported or not depending on which front end you
        # launched.
        cascade = run_cascade_waves(
            mak_session, self._cascade_approval(), announce=self._announce_cascade
        )
        # The aggregate, not the last wave — see mak.execution_result. Replacing
        # run_result with the cascade's result reported an initial wave's
        # failures as though a later successful wave had answered them.
        execution = ExecutionResult(initial=run_result, cascade=cascade)

        # ── 7. Teardown ────────────────────────────────────────────────────────
        with console.status("[dim]Running tests…[/dim]", spinner="dots"):
            try:
                teardown = mak_session.teardown(execution)
            except Exception as exc:  # noqa: BLE001
                # An exception here is an *error* outcome, not a warning over a
                # run still reporting that its tests passed.
                teardown = TeardownResult(
                    outcome=SuiteOutcome.ERROR,
                    output=f"teardown raised: {exc}",
                )

        # ── 8. Results + diff ──────────────────────────────────────────────────
        self._session_tokens += session_tokens(mak_session)

        show_results(console, execution, teardown)

        diff = get_git_diff(state.work_dir, pre_hash)
        if diff.strip():
            show_diff(console, diff)

    def _announce_cascade(self, tasks: list[Any]) -> None:
        """Say what the previous wave left behind, then show the fix-up plan."""
        n = len(tasks)
        self.console.print()
        self.console.print(
            f"  [yellow]⚠[/yellow] {n} cascade task{'s' if n != 1 else ''} — the "
            "files below call or import something that no longer matches."
        )
        show_plan(self.console, tasks)

    def _cascade_approval(self) -> Callable[[list[Any]], list[Any] | None]:
        """Approve a cascade wave with the same y/N prompt the first plan uses."""
        def approve(tasks: list[Any]) -> list[Any] | None:
            if self.state.no_review:
                self.console.print(
                    "  [dim]no-review is on; skipping the cascade wave — "
                    "callers may be broken.[/dim]"
                )
                return None
            if not self._confirm_plan():
                self.console.print(
                    "  [dim]Cascade declined; callers may still be broken.[/dim]"
                )
                return None
            self.console.print()
            return tasks

        return approve

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

        # Mode leads: a user must never be unsure whether the next task costs
        # money.
        item("mode", state.mode_display())
        item("model", state.models_display())
        item("planner", state.planner_spec())
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
        from cli.core.api_keys import key_names_for
        from cli.endpoints.commands import all_endpoints

        endpoints = all_endpoints()
        keys = load_keys(key_names_for(endpoints))
        state = CliState(
            api_keys=keys,
            endpoint_ids=[endpoint.id for endpoint in endpoints],
        )
        # Kick off a scheduled model-catalog refresh (1st/15th) in the
        # background. Returns immediately when not due, offline, keyless, or
        # opted out; it never prints — results show up in /models and /status.
        state.models().maybe_auto_refresh(
            keys, enabled=_auto_refresh_enabled(state)
        )
        # Reconnect to the hosts a previous session used (``/local url``);
        # offline, from the cached model lists.
        restore_saved_hosts(state)
        if providers_with_keys(keys):
            state.planner = default_planner_route(state.models(), keys)
            state.selected_models = [state.planner.spec()]
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
