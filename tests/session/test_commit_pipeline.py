"""The commit pipeline is an ordered list of checks, and a new one is a plug-in."""

from __future__ import annotations

from pathlib import Path

from mak.config import SemanticConfig
from mak.conflict_detector.detector import ConflictDetector
from mak.core.logging import EventType
from mak.core.types import NodeId
from mak.session.commit.checks import PreviewCompiles
from mak.session.commit.pipeline import DEFAULT_CHECKS, default_checks
from mak.session.commit.verdict import (
    ACCEPT,
    CommitCheck,
    CommitContext,
    Verdict,
    reject,
    resend,
)
from tests.semantic.helpers import ScriptRunner, events, make_session, task

_ORDER = [
    "providers_committed",
    "registrar_merge",
    "read_set_current",
    "structural_conflicts",
    "contracts_hold",
    "interface_granted",
    "preview_compiles",
    "prospective_semantics",
    "lease_still_held",
]


class TestDefaultOrder:
    def test_default_checks_are_in_the_documented_order(self) -> None:
        assert [check.name for check in DEFAULT_CHECKS] == _ORDER

    def test_the_instantiated_pipeline_keeps_that_order(self) -> None:
        checks = default_checks(semantic=SemanticConfig(), detector=ConflictDetector())
        assert [check.name for check in checks] == _ORDER

    def test_a_session_runs_the_default_pipeline(self, tmp_path: Path) -> None:
        session, _, _ = make_session(tmp_path, ScriptRunner({}))
        assert [check.name for check in session.pipeline.checks] == _ORDER


class _Recorder:
    """Wrap a check and note, in a shared list, that it ran."""

    def __init__(self, inner: CommitCheck, seen: list[str]) -> None:
        self.name = inner.name
        self._inner = inner
        self._seen = seen

    def check(self, ctx: CommitContext) -> Verdict:
        self._seen.append(self.name)
        return self._inner.check(ctx)


class _Veto:
    """A new commit-time rule: task ``vetoed`` may not commit, everyone else may."""

    name = "veto"

    def __init__(self, verdict: Verdict, seen: list[str]) -> None:
        self._verdict = verdict
        self._seen = seen

    def check(self, ctx: CommitContext) -> Verdict:
        self._seen.append(self.name)
        return self._verdict if ctx.task_id == "vetoed" else ACCEPT


def _insert_before(
    checks: tuple[CommitCheck, ...], new: CommitCheck, name: str
) -> tuple[CommitCheck, ...]:
    index = [c.name for c in checks].index(name)
    return (*checks[:index], new, *checks[index:])


def _project(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "def f():\n    return 1\n\n\ndef g():\n    return 2\n"
    )


class TestInsertedCheck:
    def test_it_runs_in_position_and_its_reject_is_honoured(
        self, tmp_path: Path
    ) -> None:
        _project(tmp_path)
        runner = ScriptRunner({
            "fine": [{"m.py::function::f": "def f():\n    return 10\n"}],
            "vetoed": [{"m.py::function::g": "def g():\n    return 20\n"}],
        })
        session, store, logger = make_session(tmp_path, runner, max_attempts=1)
        seen: list[str] = []
        pipeline = session.pipeline
        wrapped = tuple(_Recorder(check, seen) for check in pipeline.checks)
        veto = _Veto(reject(["vetoed by policy"]), seen)
        pipeline.checks = _insert_before(wrapped, veto, PreviewCompiles.name)
        session.initialize()
        session.install_plan([
            task("fine", ["m.py::function::f"]),
            task("vetoed", ["m.py::function::g"]),
        ])
        result = session.run()

        expected = _ORDER[:6] + ["veto"] + _ORDER[6:]
        assert seen[: len(expected)] == expected
        assert result.completed == ("fine",)
        assert result.failed == ("vetoed",)
        assert "vetoed by policy" in result.failure_reasons["vetoed"]
        assert result.metrics["conflict_rejections"] == 1.0
        # Rejected means rolled back: the store never advanced for "vetoed".
        g = store.get_node(NodeId("m.py::function::g"))
        assert g.version == 1 and "return 20" not in g.source
        assert "return 20" not in (tmp_path / "m.py").read_text()
        rejected = [
            e for e in events(logger, EventType.CONFLICT_DETECTED)
            if e.payload.get("task_id") == "vetoed"
        ]
        assert rejected and rejected[0].payload["reasons"] == ["vetoed by policy"]

    def test_a_resend_verdict_carries_its_note_to_the_next_attempt(
        self, tmp_path: Path
    ) -> None:
        _project(tmp_path)
        runner = ScriptRunner({
            "vetoed": [{"m.py::function::g": "def g():\n    return 20\n"}],
        })
        session, _, _ = make_session(tmp_path, runner, max_attempts=2)
        note = "Return 21, not 20."
        veto = _Veto(
            resend(["policy wants 21"], note=note, error_kind="policy"), []
        )
        session.pipeline.checks = (*session.pipeline.checks, veto)
        session.initialize()
        session.install_plan([task("vetoed", ["m.py::function::g"])])
        result = session.run()

        assert result.failed == ("vetoed",)
        bundles = runner.bundles_for("vetoed")
        assert len(bundles) == 2
        assert bundles[1].retry_note == note
        assert "policy wants 21" in result.failure_reasons["vetoed"]
