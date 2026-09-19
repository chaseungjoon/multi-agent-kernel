"""Diff a type checker's diagnostics against a baseline (Wave 20, D3).

Most codebases were never type-clean, so "the type checker reports errors" says
nothing about a wave. What does is a diagnostic that was *not there before*: a
return type changed under a caller, a field removed that a method reads. The
gate takes a baseline once (at ``initialize``), then at wave end checks the
touched files plus every file that imports them and reports only diagnostics
the baseline did not have. Diagnostics are compared without line numbers, so
code moving within a file does not read as new errors.

Tool discovery mirrors the venv-``ruff`` rule: the binary next to the running
interpreter first, then ``PATH``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

from mak.core.exceptions import SemanticGateError
from mak.semantic.gate_types import GateFinding, ProcessRunner, WaveView, run_process
from mak.semantic.project_files import importers_of, python_sources

# (file, rule/code, message) — deliberately line-independent.
Diagnostic = tuple[str, str, str]

_MYPY_LINE = re.compile(
    r"^(?P<file>[^:\n]+\.pyi?):\d+(?::\d+)?: (?P<severity>error|warning): "
    r"(?P<message>.*?)(?:  \[(?P<code>[\w-]+)\])?$"
)


def discover(tool: str) -> list[str]:
    """Return the command that runs ``tool``; raises when it is not installed."""
    beside = Path(sys.executable).parent / tool
    if beside.exists():
        return [str(beside)]
    found = shutil.which(tool)
    if found is not None:
        return [found]
    raise SemanticGateError(
        f"semantic.type_check is '{tool}' but {tool} is not installed "
        "(looked beside the running interpreter and on PATH)"
    )


def run_checker(
    tool: str,
    work_dir: Path,
    files: list[str],
    runner: ProcessRunner = run_process,
    timeout_s: float = 300.0,
) -> Counter[Diagnostic]:
    """Run ``tool`` over ``files`` and return its diagnostics in those files."""
    if not files:
        return Counter()
    command = discover(tool)
    if tool == "pyright":
        argv = [*command, "--outputjson", *files]
    else:
        argv = [
            *command, "--no-error-summary", "--show-error-codes",
            "--no-pretty", "--hide-error-context", "--no-color-output", *files,
        ]
    try:
        done = runner(argv, work_dir, timeout_s)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SemanticGateError(f"{tool} could not run: {exc}") from exc
    wanted = set(files)
    parsed = (
        _parse_pyright(done.stdout, work_dir) if tool == "pyright"
        else _parse_mypy(done.stdout)
    )
    return Counter(d for d in parsed if d[0] in wanted)


def _parse_mypy(output: str) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    for line in output.splitlines():
        match = _MYPY_LINE.match(line.strip())
        if match is None:
            continue
        found.append((
            match.group("file").replace("\\", "/"),
            match.group("code") or match.group("severity"),
            match.group("message").strip(),
        ))
    return found


def _parse_pyright(output: str, work_dir: Path) -> list[Diagnostic]:
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise SemanticGateError(f"pyright output was not JSON: {exc}") from exc
    found: list[Diagnostic] = []
    for item in data.get("generalDiagnostics", []):
        if item.get("severity") not in ("error", "warning"):
            continue
        path = Path(str(item.get("file", "")))
        try:
            rel = path.resolve().relative_to(work_dir.resolve()).as_posix()
        except ValueError:
            continue
        code = str(item.get("rule") or item["severity"])
        found.append((rel, code, str(item["message"])))
    return found


def type_gate(
    tool: str,
    view: WaveView,
    baseline: Counter[Diagnostic],
    runner: ProcessRunner = run_process,
) -> list[GateFinding]:
    """Report diagnostics in touched files and their importers that are new."""
    touched = [p for p in sorted(view.before) if view.current(p) is not None]
    if not touched:
        return []
    sources = python_sources(view.work_dir)
    files = sorted(set(touched) | (importers_of(sources, touched) & set(sources)))
    now = run_checker(tool, view.work_dir, files, runner, view.timeout_s)
    new = now - baseline
    by_file: dict[str, list[Diagnostic]] = {}
    for diagnostic in sorted(new):
        by_file.setdefault(diagnostic[0], []).append(diagnostic)
    findings: list[GateFinding] = []
    for path, diagnostics in by_file.items():
        listed = "; ".join(
            f"[{code}] {message}" for _, code, message in diagnostics[:5]
        )
        more = f" (+{len(diagnostics) - 5} more)" if len(diagnostics) > 5 else ""
        writers = view.writers.get(path) or sorted(
            {t for p in touched for t in view.writers.get(p, ())}
        )
        findings.append(GateFinding(
            gate="type_check",
            file=path,
            detail=f"{tool} reports new diagnostics in '{path}': {listed}{more}",
            targets=tuple(view.file_nodes(path)),
            context=tuple(n for p in touched if p != path for n in view.file_nodes(p)),
            tasks=tuple(writers),
        ))
    return findings
