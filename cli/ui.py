"""All Rich rendering functions for the MAK CLI.

Design language (benchmarked against Claude Code / Codex CLI):
  - one accent color, everything else default or dim
  - a single compact welcome box; no ASCII banners, no startup command dumps
  - flat indented lists instead of nested panels
  - live session state lives in the prompt's bottom toolbar, not in scrollback
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.box import ROUNDED
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from cli.core.state import CliState, mode_summary

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mak.execution_result import ExecutionResult
from mak._version import __version_display__
from mak.teardown import SuiteOutcome, TeardownResult

ACCENT = "#bd93f9"
DIM = "#6e7681"

_TAGLINE = "A kernel for concurrent multi-agent software development"


# ── Welcome ────────────────────────────────────────────────────────────────────

def print_banner(console: Console, state: CliState) -> None:
    """Print the startup banner with the current version and working directory."""
    body = Text()
    body.append("✻ ", style=f"bold {ACCENT}")
    body.append("MAK", style="bold")
    body.append(" — Multi-Agent Kernel  ", style="")
    body.append(f"v{__version_display__.lower()}", style="dim")
    body.append(f"\n\n  {_TAGLINE}", style="dim")
    body.append("\n  ", style="")
    body.append("Run ", style="dim")
    body.append("mak update", style="")
    body.append(" in the shell to update", style="dim")
    body.append("\n\n  ", style="")
    body.append("/help", style=ACCENT)
    body.append(" for commands · ", style="dim")
    body.append("/status", style=ACCENT)
    body.append(" for session info", style="dim")
    body.append(f"\n\n  cwd: {state.work_dir_display()}", style="dim")
    # The byline rides the bottom border rather than taking a line inside the
    # box: it stays visible without adding to the block of text above it.
    byline = Text("made by Seungjoon Cha", style=ACCENT)
    console.print()
    console.print(
        Panel(
            body,
            border_style=ACCENT,
            box=ROUNDED,
            padding=(0, 1),
            expand=False,
            subtitle=byline,
            subtitle_align="right",
        )
    )
    console.print()


# ── Session status (printed by /status) ────────────────────────────────────────

def _catalog_status() -> str:
    """Summarise the model catalog: how many models, last refreshed when."""
    from cli.core.models import all_models, registry

    count = len(all_models())
    stamp = registry().last_refresh
    when = stamp.strftime("%Y-%m-%d") if stamp else "never refreshed"
    return f"{count} models · {when}"


def print_status(console: Console, state: CliState) -> None:
    """Print the live session status block: models, planner, agents, workdir."""
    rows = [
        ("mode", f"{state.mode_display()} — {mode_summary(state.mode)}"),
        ("models", state.models_display()),
        ("planner", _planner_display(state)),
        ("agents", str(state.max_agents)),
        ("workdir", state.work_dir_display()),
        ("config", state.config_display()),
        ("approval", "off — plans run immediately" if state.no_review else "on"),
        ("catalog", _catalog_status()),
    ]
    if state.uses_local_agents():
        # A local run's endpoint is the thing most likely to be wrong, and it is
        # invisible everywhere else.
        rows.insert(1, ("runtime", state.local_display()))
    if state.endpoint_ids:
        rows.insert(1, ("endpoints", _endpoints_display(state)))
    console.print()
    for label, value in rows:
        console.print(f"  [dim]{label:>9}[/dim]  {value}")
    console.print()


def _planner_display(state: CliState) -> str:
    """Return the planner line: model, and the route when one is explicit."""
    if state.planner_endpoint_id:
        return f"{state.planner_endpoint_id}:{state.planner_model}"
    return state.planner_model


def _endpoints_display(state: CliState) -> str:
    """Return each known endpoint with where its traffic actually goes.

    Hosted, private and local are labelled distinctly rather than collapsed into
    "not local": a LAN gateway does leave this machine, and a user checking this
    line before a run is checking exactly that.
    """
    try:
        from cli.endpoints.commands import all_endpoints
        from cli.endpoints.render import location_label

        configured = {e.id: e for e in all_endpoints()}
    except Exception:  # noqa: BLE001 - status must never fail on a broken store
        return "  ".join(state.endpoint_ids)
    parts: list[str] = []
    for endpoint_id in state.endpoint_ids:
        endpoint = configured.get(endpoint_id)
        if endpoint is None:
            parts.append(f"{endpoint_id} (missing)")
            continue
        # Parentheses, not brackets: rich reads "[hosted]" as a style tag
        # and swallows it.
        parts.append(f"{endpoint_id} ({location_label(endpoint.location)})")
    return "  ".join(parts)


# ── One-line feedback for slash commands ──────────────────────────────────────

def print_ok(console: Console, message: str) -> None:
    """Print a one-line success confirmation for a slash command."""
    console.print(f"  [green]✓[/green] {message}")


def print_warn(console: Console, message: str) -> None:
    """Print a one-line warning for a slash command."""
    console.print(f"  [yellow]⚠[/yellow] {message}")


def print_error(console: Console, message: str) -> None:
    """Print a one-line error for a slash command."""
    console.print(f"  [red]✗[/red] {message}")


# ── Plan display ───────────────────────────────────────────────────────────────

def _compute_waves(subtasks: list[Any]) -> list[list[Any]]:
    completed: set[str] = set()
    remaining = list(subtasks)
    waves: list[list[Any]] = []
    while remaining:
        wave = [t for t in remaining if all(d in completed for d in t.depends_on)]
        if not wave:
            waves.append(remaining)
            break
        waves.append(wave)
        completed.update(t.task_id for t in wave)
        remaining = [t for t in remaining if t not in wave]
    return waves


def show_plan(console: Console, subtasks: list[Any]) -> None:
    """Render a planner plan as a numbered task list with targets and deps."""
    waves = _compute_waves(subtasks)
    n, w = len(subtasks), len(waves)

    console.print()
    header = Text()
    header.append("  Plan", style=f"bold {ACCENT}")
    header.append(
        f"  ·  {n} task{'s' if n != 1 else ''}"
        f"  ·  {w} wave{'s' if w != 1 else ''}",
        style="dim",
    )
    console.print(header)
    console.print()

    for i, wave in enumerate(waves, 1):
        note = f" · {len(wave)} in parallel" if len(wave) > 1 else ""
        console.print(f"  [dim]wave {i}{note}[/dim]")
        for st in wave:
            line = Text("    ")
            line.append("●", style=ACCENT)
            line.append(f" {st.task_id}", style="bold")
            line.append(f"  {st.description}")
            console.print(line)

            meta = Text("      ")
            parts: list[tuple[str, str]] = []
            for target in st.target_nodes:
                parts.append(("target ", str(target)))
            if st.agent_type:
                parts.append(("agent ", st.agent_type))
            if st.depends_on:
                parts.append(("after ", ", ".join(st.depends_on)))
            for j, (label, value) in enumerate(parts):
                if j > 0:
                    meta.append(" · ", style="dim")
                meta.append(label, style="dim")
                meta.append(value, style=DIM)
            if parts:
                console.print(meta)
        console.print()


# ── Result summary ─────────────────────────────────────────────────────────────

def show_results(
    console: Console, execution: ExecutionResult, teardown: TeardownResult
) -> None:
    """Print a finished run's tallies — across every wave, not just the last.

    Takes the aggregate rather than one ``SessionResult`` because the TUI used to
    be handed the *cascade's* result in place of the original, and reported an
    initial wave's failures as a clean success. The headline symbol now follows
    :attr:`ExecutionResult.request_satisfied` — did the user get what they asked
    for — with the completed count kept beside it as the separate statistic it is.
    """
    ok = execution.tasks_completed
    bad = len(execution.failed)
    skp = len(execution.skipped)
    blk = len(execution.blocked)

    ok_flag = execution.request_satisfied and teardown.ok
    sym, style = ("✓", "bold green") if ok_flag else ("✗", "bold red")

    line = Text("  ")
    line.append(sym, style=style)
    line.append(f" {ok} completed", style="green" if ok else "dim")
    # A task that reported "nothing needed changing" completed without changing a
    # line; folding it into the headline number overstates what the run did.
    noop = len(execution.noop)
    if noop:
        line.append(f" ({noop} no-op)", style="dim")
    if bad:
        line.append(f" · {bad} failed", style="red")
    if skp or blk:
        line.append(f" · {skp} skipped · {blk} blocked", style="dim")
    if execution.wave_count > 1:
        line.append(f" · {execution.wave_count} waves", style="dim")
    console.print()
    console.print(line)

    _show_test_outcome(console, teardown)
    # A run the kernel itself halted (today: the token budget) strands tasks that
    # have no failure of their own, so nothing below would explain them.
    for index, stopped in execution.stopped_reasons:
        console.print(f"  [yellow]⚠ Wave {index + 1} stopped — {stopped}.[/yellow]")
    for key in execution.failed:
        reason = execution.failure_reasons.get(key, "")
        wave, task_id = key
        label = task_id if wave == 0 else f"{task_id} (wave {wave + 1})"
        console.print(f"    [red]✗ {label}[/red]  [dim]{reason}[/dim]")
    _show_cascade_state(console, execution)
    console.print()


def _show_test_outcome(console: Console, teardown: TeardownResult) -> None:
    """Name the test outcome rather than implying one from a tick.

    "Tests passed" used to be a bool that started ``True``, so a project with no
    suite and a teardown that raised both read as green. Each of the four
    outcomes now says what it is.
    """
    if teardown.outcome is SuiteOutcome.FAILED:
        console.print("  [yellow]⚠ Test suite did not pass after changes.[/yellow]")
    elif teardown.outcome is SuiteOutcome.SKIPPED:
        console.print(
            "  [dim]No test_command configured — no suite ran.[/dim]"
        )
    elif teardown.outcome is SuiteOutcome.ERROR:
        console.print(
            f"  [red]✗ Test runner errored:[/red] [dim]{teardown.output}[/dim]"
        )
    if teardown.push_skipped_reason:
        console.print(f"  [dim]{teardown.push_skipped_reason}.[/dim]")


def _show_cascade_state(console: Console, execution: ExecutionResult) -> None:
    """Say how the fix-up loop stopped, when it stopped short of finishing."""
    cascade = execution.cascade
    if cascade.declined:
        console.print(
            "  [yellow]⚠ A cascade wave was declined — callers may still be "
            "broken.[/yellow]"
        )
    if cascade.limit_reached:
        console.print(
            "  [yellow]⚠ Cascade stopped at its wave limit with defects "
            "remaining.[/yellow]"
        )
    if cascade.stalled:
        console.print(
            f"  [yellow]⚠ Cascade stopped without progress — "
            f"{cascade.stop_reason}.[/yellow]"
        )
    if cascade.oscillating:
        console.print(
            f"  [yellow]⚠ Cascade stopped on an oscillation — "
            f"{cascade.stop_reason}.[/yellow]"
        )
    if cascade.unrepairable:
        console.print(
            f"  [yellow]⚠ Cascade plan cannot satisfy its repair contract — "
            f"{cascade.stop_reason}.[/yellow]"
        )
    if cascade.unresolved:
        console.print(
            f"  [dim]Unresolved cascade defects: "
            f"{', '.join(cascade.unresolved)}[/dim]"
        )


# ── Git diff — one summary line per file, git-stat style ──────────────────────

def show_diff(console: Console, diff: str) -> None:
    """Print a per-file summary of a unified diff, git-stat style."""
    files = _split_diff_by_file(diff)
    if not files:
        return

    rows: list[tuple[str, int, int]] = []
    for filename, hunks in files:
        added = sum(1 for h in hunks for ln in h
                    if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for h in hunks for ln in h
                      if ln.startswith("-") and not ln.startswith("---"))
        if added or removed:
            rows.append((filename, added, removed))

    if not rows:
        return

    max_fn = max(len(f) for f, _, _ in rows)
    max_total = max(a + r for _, a, r in rows) or 1
    BAR_W = 20

    console.print("  [dim]changes[/dim]")
    for filename, added, removed in rows:
        add_bars = round(added / max_total * BAR_W)
        rem_bars = round(removed / max_total * BAR_W)
        line = Text(f"    {filename.ljust(max_fn + 2)}")
        line.append(f"+{added:<4}", style="green")
        line.append(f"-{removed:<4}", style="red")
        line.append(" ")
        line.append("+" * add_bars, style="green")
        line.append("-" * rem_bars, style="red")
        console.print(line)
    console.print()


def _split_diff_by_file(diff: str) -> list[tuple[str, list[list[str]]]]:
    files: list[tuple[str, list[list[str]]]] = []
    cur_file: str | None = None
    cur_hunks: list[list[str]] = []
    cur_hunk:  list[str]       = []

    for line in diff.splitlines():
        if line.startswith("diff --git "):
            if cur_file is not None:
                if cur_hunk:
                    cur_hunks.append(cur_hunk)
                files.append((cur_file, cur_hunks))
            cur_file  = line.split(" b/", 1)[-1].strip()
            cur_hunks = []
            cur_hunk  = []
        elif line.startswith("@@"):
            if cur_hunk:
                cur_hunks.append(cur_hunk)
            cur_hunk = [line]
        elif cur_file is not None:
            cur_hunk.append(line)

    if cur_file is not None:
        if cur_hunk:
            cur_hunks.append(cur_hunk)
        files.append((cur_file, cur_hunks))
    return files
