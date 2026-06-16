"""All Rich rendering functions for the MAK CLI."""
from __future__ import annotations

from typing import Any

from rich.box import HEAVY, ROUNDED
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

from cli.core.state import CliState

# ── ASCII logo (pyfiglet doom, "Multi Agent" + "Kernel" side-by-side) ──────────
_LOGO_LINES = [
    r"___  ___      _ _   _    ___                   _      _   __                     _ ",
    r"|  \/  |     | | | (_)  / _ \                 | |    | | / /                    | |",
    r"| .  . |_   _| | |_ _  / /_\ \ __ _  ___ _ __ | |_   | |/ /  ___ _ __ _ __   ___| |",
    r"| |\/| | | | | | __| | |  _  |/ _` |/ _ \ '_ \| __|  |    \ / _ \ '__| '_ \ / _ \ |",
    r"| |  | | |_| | | |_| | | | | | (_| |  __/ | | | |_   | |\  \  __/ |  | | | |  __/ |",
    r"\_|  |_/\__,_|_|\__|_| \_| |_/\__, |\___|_| |_|\__|  \_| \_/\___|_|  |_| |_|\___|_|",
    r"                               __/ |                 ",
    r"                              |___/                  ",
]

_TAGLINE = "A kernel for concurrent multi agent software development"
_VERSION = "0.2.0 Beta"

# ── Command hints ──────────────────────────────────────────────────────────────
_HINTS: list[tuple[str, str, str | None]] = [
    ("/models [provider:model ...]",
     "Choose which AI models to use as agents.",
     "example: /models anthropic:claude-sonnet-4-6 openai:gpt-5.5"),
    ("/max-agents <N>",
     "Set how many agents may run in parallel.",
     "example: /max-agents 3"),
    ("/work-dir <path>",
     "Set the working directory that MAK will edit.",
     "example: /work-dir ~/projects/myapp"),
    ("/apikey",
     "Add or update API keys for any provider.",
     None),
    ("/config [path]",
     "Use the built-in default config, or load a custom YAML file.",
     "example: /config  or  /config /path/to/config.yaml"),
    ("/no-review [true|false]",
     "Control whether MAK waits for your approval before running a plan.",
     "example: /no-review true  (skip approval)  or  /no-review false  (require approval)"),
]


def print_logo(console: Console) -> None:
    console.print()
    for line in _LOGO_LINES:
        console.print(f"[bold #ff8c00]{line}[/bold #ff8c00]")
    console.print(f"  [dim]{_TAGLINE}[/dim]     [dim]{_VERSION}[/dim]")
    console.print()


def make_status_text(state: CliState) -> Text:
    t = Text()
    t.append("  models: ",   style="dim")
    t.append(state.models_display(), style="bold cyan")
    t.append("   agents: ", style="dim")
    t.append(str(state.max_agents), style="bold")
    t.append("   workdir: ", style="dim")
    t.append(state.work_dir_display(), style="bold")
    t.append("   planner: ", style="dim")
    t.append(state.planner_model, style="bold magenta")
    t.append("   approval: ", style="dim")
    t.append("off" if state.no_review else "on",
             style="bold red" if state.no_review else "bold green")
    t.append("  ", style="")
    return t


def print_status_capsule(console: Console, state: CliState) -> None:
    console.print(
        Panel(make_status_text(state), border_style="bold blue", box=HEAVY, padding=(0, 0))
    )


def print_hints(console: Console) -> None:
    console.print()
    console.print(Rule(style="dim"))
    console.print()
    for cmd, desc, example in _HINTS:
        body = Text()
        body.append(cmd, style="bold cyan")
        body.append(f"\n  {desc}", style="dim")
        if example:
            body.append(f"\n  {example}", style="dim italic")
        console.print(Panel(body, border_style="dim", box=ROUNDED, padding=(0, 1)))
    console.print()
    console.print(Rule(style="dim"))
    console.print()


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


def _task_node(st: Any) -> Panel:
    body = Text()
    body.append(f"  {st.task_id}", style="bold green")
    body.append("  ", style="")
    body.append(st.description, style="bold white")
    for target in st.target_nodes:
        body.append("\n    target  ", style="dim")
        body.append(str(target), style="cyan")
    if st.agent_type:
        body.append("\n    agent   ", style="dim")
        body.append(st.agent_type, style="dim magenta")
    if st.depends_on:
        body.append("\n    after   ", style="dim")
        body.append(", ".join(st.depends_on), style="yellow")
    return Panel(body, border_style="green", box=ROUNDED, padding=(0, 1))


def show_plan(console: Console, subtasks: list[Any], task: str) -> None:
    waves = _compute_waves(subtasks)
    n, w = len(subtasks), len(waves)

    hdr = Text()
    hdr.append("  PLANNER", style="bold cyan")
    hdr.append(f"  ──  {n} subtask{'s' if n != 1 else ''}   {w} wave{'s' if w != 1 else ''}  ",
               style="dim")
    console.print()
    console.print(Panel(hdr, border_style="cyan", box=HEAVY, padding=(0, 0)))
    console.print()
    console.print(f"  [bold]Task:[/bold] [italic]{task}[/italic]")
    console.print()

    for i, wave in enumerate(waves, 1):
        note = f"  ({len(wave)} in parallel)" if len(wave) > 1 else ""
        console.print(
            Rule(
                Text.assemble(
                    ("  WAVE ", "dim"),
                    (str(i), "bold yellow"),
                    (note, "dim italic"),
                    ("  ", ""),
                ),
                style="dim",
            )
        )
        console.print()
        for st in wave:
            console.print(_task_node(st))
        console.print()

    # Dependency graph below all waves
    edges = [(dep, st.task_id) for st in subtasks for dep in st.depends_on]
    if edges:
        console.print(Rule(Text.assemble(("  dependencies  ", "dim")), style="dim"))
        console.print()
        # Build forward adjacency: upstream → [downstream, ...]
        adj: dict[str, list[str]] = {}
        for up, down in edges:
            adj.setdefault(up, []).append(down)
        for up in sorted(adj):
            line = Text()
            line.append(f"  {up}", style="bold green")
            line.append("  →  ", style="dim")
            line.append(",  ".join(adj[up]), style="cyan")
            console.print(line)
        console.print()


# ── Result summary ─────────────────────────────────────────────────────────────

def show_results(console: Console, result: Any, tests_passed: bool) -> None:
    ok  = len(result.completed)
    bad = len(result.failed)
    skp = len(result.skipped)
    blk = len(result.blocked)

    ok_flag = result.ok and tests_passed
    sym   = "+" if ok_flag else "!"
    style = "bold green" if ok_flag else "bold red"

    console.print(Rule(style="dim"))
    console.print(
        f"  [{style}]{sym}[/{style}]  "
        f"[green]{ok} completed[/green]  "
        f"[red]{bad} failed[/red]  "
        f"[dim]{skp} skipped  {blk} blocked[/dim]"
    )
    if not tests_passed:
        console.print("  [yellow]  Test suite did not pass after changes.[/yellow]")
    for task_id in result.failed:
        reason = result.failure_reasons.get(task_id, "")
        console.print(f"  [red]  - {task_id}:[/red] [dim]{reason}[/dim]")
    console.print()


# ── Git diff — one summary line per file, git-stat style ──────────────────────

def show_diff(console: Console, diff: str) -> None:
    files = _split_diff_by_file(diff)
    if not files:
        return

    rows: list[tuple[str, int, int]] = []
    for filename, hunks in files:
        added   = sum(1 for h in hunks for ln in h
                      if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for h in hunks for ln in h
                      if ln.startswith("-") and not ln.startswith("---"))
        if added or removed:
            rows.append((filename, added, removed))

    if not rows:
        return

    max_fn    = max(len(f) for f, _, _ in rows)
    max_total = max(a + r for _, a, r in rows) or 1
    BAR_W     = 24

    body = Text()
    for i, (filename, added, removed) in enumerate(rows):
        if i > 0:
            body.append("\n")
        add_bars = round(added   / max_total * BAR_W)
        rem_bars = round(removed / max_total * BAR_W)
        body.append(f"  {filename.ljust(max_fn + 2)}")
        body.append(f"+{added:<4}", style="bold green")
        body.append(f"-{removed:<4}", style="bold red")
        body.append("  ")
        body.append("+" * add_bars, style="green")
        body.append("-" * rem_bars, style="red")

    console.print(
        Panel(
            body,
            title="[dim]changes[/dim]",
            border_style="dim",
            box=ROUNDED,
            padding=(0, 1),
        )
    )
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
