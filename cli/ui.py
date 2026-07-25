"""All Rich rendering functions for the MAK CLI.

Design language (benchmarked against Claude Code / Codex CLI):
  - one accent color, everything else default or dim
  - a single compact welcome box; no ASCII banners, no startup command dumps
  - flat indented lists instead of nested panels
  - live session state lives in the prompt's bottom toolbar, not in scrollback
"""
from __future__ import annotations

from typing import Any

from rich.box import ROUNDED
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from cli.core.state import CliState
from mak._version import __version_display__

ACCENT = "#bd93f9"
DIM = "#6e7681"

_TAGLINE = "A kernel for concurrent multi-agent software development"


# ── Welcome ────────────────────────────────────────────────────────────────────

def print_banner(console: Console, state: CliState) -> None:
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
    console.print()
    console.print(
        Panel(body, border_style=ACCENT, box=ROUNDED, padding=(0, 1), expand=False)
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
    rows = [
        ("models", state.models_display()),
        ("planner", state.planner_model),
        ("agents", str(state.max_agents)),
        ("workdir", state.work_dir_display()),
        ("config", state.config_display()),
        ("approval", "off — plans run immediately" if state.no_review else "on"),
        ("catalog", _catalog_status()),
    ]
    console.print()
    for label, value in rows:
        console.print(f"  [dim]{label:>9}[/dim]  {value}")
    console.print()


# ── One-line feedback for slash commands ──────────────────────────────────────

def print_ok(console: Console, message: str) -> None:
    console.print(f"  [green]✓[/green] {message}")


def print_warn(console: Console, message: str) -> None:
    console.print(f"  [yellow]⚠[/yellow] {message}")


def print_error(console: Console, message: str) -> None:
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

def show_results(console: Console, result: Any, tests_passed: bool) -> None:
    ok = len(result.completed)
    bad = len(result.failed)
    skp = len(result.skipped)
    blk = len(result.blocked)

    ok_flag = result.ok and tests_passed
    sym, style = ("✓", "bold green") if ok_flag else ("✗", "bold red")

    line = Text("  ")
    line.append(sym, style=style)
    line.append(f" {ok} completed", style="green" if ok else "dim")
    if bad:
        line.append(f" · {bad} failed", style="red")
    if skp or blk:
        line.append(f" · {skp} skipped · {blk} blocked", style="dim")
    console.print()
    console.print(line)

    if not tests_passed:
        console.print("  [yellow]⚠ Test suite did not pass after changes.[/yellow]")
    for task_id in result.failed:
        reason = result.failure_reasons.get(task_id, "")
        console.print(f"    [red]✗ {task_id}[/red]  [dim]{reason}[/dim]")
    console.print()


# ── Git diff — one summary line per file, git-stat style ──────────────────────

def show_diff(console: Console, diff: str) -> None:
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
