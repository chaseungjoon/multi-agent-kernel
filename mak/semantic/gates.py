"""Run the optional heavy gates at wave end.

Every gate is off by default and none of them can fail a wave: each finding
becomes a fix-up task through the same review flow as the cascade, and a gate
whose tool is missing or times out is logged and skipped — its infrastructure
failing says nothing about the wave's code.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from pathlib import Path

from mak.config import SemanticConfig
from mak.core.exceptions import SemanticGateError
from mak.semantic.gate_types import GateFinding, ProcessRunner, WaveView, run_process
from mak.semantic.impact_tests import impact_tests
from mak.semantic.import_smoke import import_smoke
from mak.semantic.project_files import python_sources
from mak.semantic.type_gate import Diagnostic, run_checker, type_gate


class GateSuite:
    """The configured gates, plus the type-check baseline they diff against."""

    def __init__(
        self, config: SemanticConfig, runner: ProcessRunner = run_process
    ) -> None:
        self._config = config
        self._runner = runner
        self._type_baseline: Counter[Diagnostic] | None = None

    @property
    def enabled(self) -> bool:
        """Whether any gate is switched on."""
        return (
            self._config.type_check != "off"
            or self._config.impact_tests
            or self._config.import_smoke
        )

    def take_baseline(self, work_dir: Path) -> None:
        """Record the type checker's pre-existing diagnostics (``initialize``).

        Raises :class:`SemanticGateError` when the tool cannot run; the caller
        logs it, and the type gate then stays silent rather than reporting
        every pre-existing diagnostic as new.
        """
        if self._config.type_check == "off":
            return
        files = sorted(python_sources(work_dir))
        self._type_baseline = run_checker(
            self._config.type_check, work_dir, files, self._runner,
            self._config.gate_timeout_s,
        )

    def run(
        self, view: WaveView, log: Callable[..., None]
    ) -> list[GateFinding]:
        """Run every enabled gate; log (and skip) any that cannot run."""
        findings: list[GateFinding] = []
        for name, gate in self._gates():
            try:
                findings.extend(gate(view))
            except SemanticGateError as exc:
                log(gate=name, error=str(exc))
        return findings

    def _gates(self) -> list[tuple[str, Callable[[WaveView], list[GateFinding]]]]:
        gates: list[tuple[str, Callable[[WaveView], list[GateFinding]]]] = []
        if self._config.import_smoke:
            gates.append(("import_smoke", lambda v: import_smoke(v, self._runner)))
        if self._config.type_check != "off" and self._type_baseline is not None:
            tool, baseline = self._config.type_check, self._type_baseline
            gates.append(
                ("type_check", lambda v: type_gate(tool, v, baseline, self._runner))
            )
        if self._config.impact_tests:
            gates.append(("impact_tests", lambda v: impact_tests(v, self._runner)))
        return gates
