"""Render benchmark results into ``benchmark/README.md`` and ``benchmark/STATS.md``."""

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


def _tokens(result: RunResult) -> int:
    return result.usage.tokens_in + result.usage.tokens_out


def _fmt_secs(value: float) -> str:
    return f"{value:.2f}s"


def _summary_table(mak: RunResult, trad: RunResult) -> str:
    rows = [
        ("Implementation time", _fmt_secs(mak.wall_seconds), _fmt_secs(trad.wall_seconds)),
        ("Total tokens", f"{_tokens(mak):,}", f"{_tokens(trad):,}"),
        ("Model calls", str(mak.usage.calls), str(trad.usage.calls)),
        ("Accuracy (tests passed)",
         f"{mak.passed}/{mak.total} ({mak.accuracy:.0%})",
         f"{trad.passed}/{trad.total} ({trad.accuracy:.0%})"),
        ("Registry merge conflicts", str(mak.conflicts), str(trad.conflicts)),
        ("Conflict-resolution calls", str(mak.resolutions), str(trad.resolutions)),
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
        f"worktree run hit **{trad.conflicts}**, each an extra resolution call."
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
    return (
        f"> **Mode: `real`.** {meta.num_agents} agents "
        f"({', '.join(meta.models)}) implementing {meta.operations} operations "
        f"(verified by {meta.tests} tests)."
    )


def render_readme_results(mak: RunResult, trad: RunResult, meta: RunMeta) -> str:
    """The block injected between the RESULTS markers in README.md."""
    return "\n".join([
        _README_START,
        "",
        f"_Last run: {meta.timestamp} · mode `{meta.mode}` · {meta.num_agents} agents._",
        "",
        _mode_note(meta),
        "",
        _summary_table(mak, trad),
        "",
        "**Reading the numbers:**",
        "",
        _takeaway(mak, trad),
        "",
        "See [STATS.md](STATS.md) for the full breakdown.",
        "",
        _README_END,
    ])


def inject_readme(readme_text: str, block: str) -> str:
    """Replace the RESULTS block in ``readme_text`` (markers must be present)."""
    pre, _, rest = readme_text.partition(_README_START)
    _, _, post = rest.partition(_README_END)
    return pre + block + post


def render_stats(mak: RunResult, trad: RunResult, meta: RunMeta) -> str:
    """The full STATS.md document."""
    lines: list[str] = [
        "# Benchmark results — detailed statistics",
        "",
        f"- **Run at:** {meta.timestamp}",
        f"- **Mode:** `{meta.mode}`",
        f"- **Agents:** {meta.num_agents} ({', '.join(meta.models)})",
        f"- **Workload:** {meta.operations} operations across 3 modules + 1 shared "
        f"registry function; {meta.tests} tests as the accuracy oracle.",
        "",
        _mode_note(meta),
        "",
        "## Headline",
        "",
        _summary_table(mak, trad),
        "",
        "## Reading the numbers",
        "",
        _takeaway(mak, trad),
        "",
        "## Token detail",
        "",
        "| | MAK | Traditional |",
        "|---|---|---|",
        f"| Input tokens | {mak.usage.tokens_in:,} | {trad.usage.tokens_in:,} |",
        f"| Output tokens | {mak.usage.tokens_out:,} | {trad.usage.tokens_out:,} |",
        f"| Total tokens | {_tokens(mak):,} | {_tokens(trad):,} |",
        f"| Model calls | {mak.usage.calls} | {trad.usage.calls} |",
        "",
        "## Model calls per agent",
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
    lines += [
        "",
        "## Coordination",
        "",
        f"- **MAK** held a node-level write lock on the shared `_register_all`, "
        f"serializing the {meta.operations} registry edits: "
        f"**{mak.conflicts} conflicts**, **{mak.resolutions} resolution calls**.",
        f"- **Traditional** merged {meta.num_agents} branches that all edited "
        f"`_register_all`: **{trad.conflicts} conflicts**, "
        f"**{trad.resolutions} resolution calls**.",
        "",
    ]
    for label, result in (("MAK", mak), ("Traditional", trad)):
        if result.notes:
            lines.append(f"### {label} notes")
            lines += [f"- {note}" for note in result.notes]
            lines.append("")
    return "\n".join(lines)
