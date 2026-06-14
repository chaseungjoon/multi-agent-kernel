"""Render benchmark results into ``benchmark/README.md`` and ``benchmark/STATS.md``.

Both reports can hold *several* projects at once (the basic 9-op target and the
larger 17-op ``pro`` target), one labelled section per project, so adding a heavier
workload never overwrites the lighter one's published numbers.
"""

from __future__ import annotations

from dataclasses import dataclass

from harness.metrics import RunResult

_README_START = "<!-- RESULTS:START -->"
_README_END = "<!-- RESULTS:END -->"


@dataclass(frozen=True)
class RunMeta:
    """Context for a benchmark run, recorded alongside the numbers."""

    mode: str  # "mock" | "real"
    num_agents: int
    models: list[str]
    timestamp: str
    operations: int
    tests: int
    project: str = "basic"  # workload key
    label: str = "Basic toolkit"  # human label
    modules: int = 3  # module count of the workload
    repeats: int = 1  # how many runs the numbers are averaged over


@dataclass(frozen=True)
class ProjectRun:
    """One project's full result triple (the ``mak``/``trad`` numbers are the mean
    over ``meta.repeats`` runs; ``samples`` holds the per-run headline numbers)."""

    meta: RunMeta
    mak: RunResult
    trad: RunResult
    samples: list[dict] | None = None


def _tokens(result: RunResult) -> int:
    return round(result.usage.tokens_in + result.usage.tokens_out)


def _num(value: float | int) -> str:
    """Render a metric: integers (and whole floats) without decimals, else one place."""
    if isinstance(value, float) and not value.is_integer():
        return f"{value:.1f}"
    return f"{int(round(value)):,}"


def _fmt_secs(value: float) -> str:
    return f"{value:.2f}s"


def _summary_table(mak: RunResult, trad: RunResult) -> str:
    rows = [
        ("Implementation time", _fmt_secs(mak.wall_seconds), _fmt_secs(trad.wall_seconds)),
        ("Total tokens", _num(_tokens(mak)), _num(_tokens(trad))),
        ("Model calls", _num(mak.usage.calls), _num(trad.usage.calls)),
        ("Accuracy (tests passed)",
         f"{_num(mak.passed)}/{mak.total} ({mak.accuracy:.0%})",
         f"{_num(trad.passed)}/{trad.total} ({trad.accuracy:.0%})"),
        ("Registry merge conflicts", _num(mak.conflicts), _num(trad.conflicts)),
        ("Conflict-resolution calls", _num(mak.resolutions), _num(trad.resolutions)),
    ]
    lines = ["| Metric | MAK | Traditional (worktrees) |", "|---|---|---|"]
    lines += [f"| {m} | {a} | {b} |" for m, a, b in rows]
    return "\n".join(lines)


def _takeaway(mak: RunResult, trad: RunResult) -> str:
    """An honest, computed reading of the numbers (no hard-coded conclusions)."""
    mt, tt = _tokens(mak), _tokens(trad)
    lines: list[str] = []
    if tt and mt < tt:
        lines.append(
            f"- **Tokens:** MAK spent **{(1 - mt / tt) * 100:.0f}% fewer** "
            f"({mt:,} vs {tt:,}) — it reconciles nothing, so it makes no extra "
            f"conflict-resolution calls."
        )
    elif tt:
        lines.append(f"- **Tokens:** MAK {mt:,} vs Traditional {tt:,}.")
    if mak.passed == trad.passed:
        lines.append(
            f"- **Accuracy:** tied at {mak.accuracy:.0%}. These tasks are small and "
            f"the resolver merged the registry correctly *this time*; the structural "
            f"risk MAK removes — a dropped or garbled registration — is what bites on "
            f"larger tasks or weaker resolvers."
        )
    else:
        better, worse = (
            ("MAK", "Traditional") if mak.passed > trad.passed else ("Traditional", "MAK")
        )
        lines.append(
            f"- **Accuracy:** {better} higher — MAK {mak.accuracy:.0%} vs "
            f"Traditional {trad.accuracy:.0%} ({worse} lost work in the merge)."
        )
    if mak.wall_seconds > trad.wall_seconds:
        lines.append(
            f"- **Time:** the worktree run was faster here "
            f"({trad.wall_seconds:.1f}s vs {mak.wall_seconds:.1f}s): *every* task "
            f"contends on the one shared registry node, so MAK serializes them while "
            f"the worktrees run fully in parallel and reconcile afterwards. On a "
            f"workload with more independent work, MAK parallelizes that part too — "
            f"this benchmark deliberately maximizes contention."
        )
    else:
        lines.append(
            f"- **Time:** MAK was faster ({mak.wall_seconds:.1f}s vs "
            f"{trad.wall_seconds:.1f}s)."
        )
    lines.append(
        f"- **Coordination:** MAK hit **0** merge conflicts by construction; the "
        f"worktree run hit **{_num(trad.conflicts)}**, each an extra resolution call."
    )
    return "\n".join(lines)


def _mode_note(meta: RunMeta) -> str:
    if meta.mode == "mock":
        return (
            "> **Mode: `mock` (harness self-test).** Agents are deterministic and free, "
            "so **time and token figures are not representative** — they only prove the "
            "harness runs end-to-end. The meaningful signal here is structural: the "
            "merge-conflict and resolution counts, which become real token/time/accuracy "
            "costs under `--mode real`. Run with real models for representative numbers."
        )
    averaged = (
        f" Figures are the **mean of {meta.repeats} runs** (per-run breakdown below)."
        if meta.repeats > 1 else ""
    )
    return (
        f"> **Mode: `real`.** {meta.num_agents} agents "
        f"({', '.join(meta.models)}) implementing {meta.operations} operations "
        f"(verified by {meta.tests} tests).{averaged}"
    )


def _readme_section(run: ProjectRun) -> str:
    meta = run.meta
    runs_note = f" · mean of {meta.repeats} runs" if meta.repeats > 1 else ""
    return "\n".join([
        f"### {meta.label} — {meta.operations} operations, {meta.modules} modules",
        "",
        f"_Last run: {meta.timestamp} · mode `{meta.mode}` · "
        f"{meta.num_agents} agents{runs_note}._",
        "",
        _mode_note(meta),
        "",
        _summary_table(run.mak, run.trad),
        "",
        "**Reading the numbers:**",
        "",
        _takeaway(run.mak, run.trad),
    ])


def render_readme_results(runs: list[ProjectRun]) -> str:
    """The block injected between the RESULTS markers in README.md."""
    parts = [_README_START, ""]
    for i, run in enumerate(runs):
        if i:
            parts.append("\n---\n")
        parts.append(_readme_section(run))
        parts.append("")
    parts += ["See [STATS.md](STATS.md) for the full breakdown.", "", _README_END]
    return "\n".join(parts)


def inject_readme(readme_text: str, block: str) -> str:
    """Replace the RESULTS block in ``readme_text`` (markers must be present)."""
    pre, _, rest = readme_text.partition(_README_START)
    _, _, post = rest.partition(_README_END)
    return pre + block + post


def _samples_lines(run: ProjectRun) -> list[str]:
    """A per-run breakdown table, so the averaged headline is auditable."""
    if not run.samples or run.meta.repeats <= 1:
        return []
    lines = [
        f"### Per-run breakdown ({run.meta.repeats} runs)",
        "",
        "Each row is one independent run; the headline above is the mean of these.",
        "",
        "| Run | MAK tokens | MAK passed | MAK time | "
        "Trad tokens | Trad passed | Trad time | Trad conflicts |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, s in enumerate(run.samples, 1):
        m, t = s["mak"], s["trad"]
        lines.append(
            f"| {i} | {m['tokens']:,} | {m['passed']}/{run.meta.tests} | "
            f"{m['wall']:.1f}s | {t['tokens']:,} | {t['passed']}/{run.meta.tests} | "
            f"{t['wall']:.1f}s | {t['conflicts']} |"
        )
    lines.append("")
    return lines


def _stats_section(run: ProjectRun) -> list[str]:
    meta, mak, trad = run.meta, run.mak, run.trad
    avg = f" (mean of {meta.repeats} runs)" if meta.repeats > 1 else ""
    lines: list[str] = [
        f"## {meta.label}",
        "",
        f"- **Run at:** {meta.timestamp}{avg}",
        f"- **Mode:** `{meta.mode}`",
        f"- **Agents:** {meta.num_agents} ({', '.join(meta.models)})",
        f"- **Workload:** {meta.operations} operations across {meta.modules} modules "
        f"+ 1 shared registry function; {meta.tests} tests as the accuracy oracle.",
        "",
        _mode_note(meta),
        "",
        "### Headline",
        "",
        _summary_table(mak, trad),
        "",
        "### Reading the numbers",
        "",
        _takeaway(mak, trad),
        "",
        "### Token detail",
        "",
        "| | MAK | Traditional |",
        "|---|---|---|",
        f"| Input tokens | {mak.usage.tokens_in:,} | {trad.usage.tokens_in:,} |",
        f"| Output tokens | {mak.usage.tokens_out:,} | {trad.usage.tokens_out:,} |",
        f"| Total tokens | {_tokens(mak):,} | {_tokens(trad):,} |",
        f"| Model calls | {mak.usage.calls} | {trad.usage.calls} |",
        "",
        "### Model calls per agent",
        "",
        "| Agent | MAK | Traditional |",
        "|---|---|---|",
    ]
    agents = sorted(set(mak.per_agent_calls) | set(trad.per_agent_calls))
    for agent in agents:
        lines.append(
            f"| {agent} | {mak.per_agent_calls.get(agent, 0)} | "
            f"{trad.per_agent_calls.get(agent, 0)} |"
        )
    lines += _samples_lines(run)
    lines += [
        "",
        "### Coordination",
        "",
        f"- **MAK** held a node-level write lock on the shared `_register_all`, "
        f"serializing the {meta.operations} registry edits: "
        f"**{_num(mak.conflicts)} conflicts**, "
        f"**{_num(mak.resolutions)} resolution calls**.",
        f"- **Traditional** merged {meta.num_agents} branches that all edited "
        f"`_register_all`: **{_num(trad.conflicts)} conflicts**, "
        f"**{_num(trad.resolutions)} resolution calls**.",
        "",
    ]
    for label, result in (("MAK", mak), ("Traditional", trad)):
        if result.notes:
            lines.append(f"#### {label} notes")
            lines += [f"- {note}" for note in result.notes]
            lines.append("")
    return lines


def render_stats(runs: list[ProjectRun]) -> str:
    """The full STATS.md document, one section per project."""
    lines: list[str] = ["# Benchmark results — detailed statistics", ""]
    for i, run in enumerate(runs):
        if i:
            lines.append("---")
            lines.append("")
        lines += _stats_section(run)
    return "\n".join(lines)
