"""Import every touched module in a fresh interpreter (Wave 20, D6).

A parse check proves a module is valid Python; it does not prove the module can
be *imported* — a name that fails at import time, a module-level call into code
another task changed, a circular import that only bites on first import. Each
touched module is imported in its own subprocess, in the wave's end state and
in the pre-wave state, and only failures the wave introduced are reported.
"""

from __future__ import annotations

from pathlib import Path

from mak.semantic.gate_types import (
    GateFinding,
    ProcessRunner,
    WaveView,
    python,
    run_process,
)
from mak.semantic.overlay import overlay
from mak.semantic.project_files import module_name


def import_smoke(
    view: WaveView, runner: ProcessRunner = run_process
) -> list[GateFinding]:
    """Report touched modules that import cleanly before the wave and not after."""
    modules = {
        path: name
        for path in sorted(view.before)
        if view.current(path) is not None
        and (name := module_name(path, view.work_dir)) is not None
    }
    if not modules:
        return []
    with overlay(view.work_dir, view.after()) as root:
        after = _failures(root, modules, runner, view.timeout_s)
    if not after:
        return []
    with overlay(view.work_dir, dict(view.before)) as root:
        before = _failures(root, modules, runner, view.timeout_s)
    return [
        GateFinding(
            gate="import_smoke",
            file=path,
            detail=f"'{path}' no longer imports: {error}",
            targets=tuple(view.file_nodes(path)),
            tasks=tuple(view.writers.get(path, ())),
        )
        for path, error in sorted(after.items())
        if path not in before
    ]


def _failures(
    root: Path, modules: dict[str, str], runner: ProcessRunner, timeout_s: float
) -> dict[str, str]:
    """Import each module in its own interpreter; ``{path: last error line}``."""
    failures: dict[str, str] = {}
    for path, name in modules.items():
        try:
            done = runner(
                [
                    python(), "-c",
                    f"import importlib; importlib.import_module({name!r})",
                ],
                root,
                timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 - a hung import is a failure
            failures[path] = f"{type(exc).__name__}: {exc}"
            continue
        if done.returncode != 0:
            lines = [ln for ln in done.stderr.strip().splitlines() if ln.strip()]
            failures[path] = lines[-1] if lines else f"exit {done.returncode}"
    return failures
