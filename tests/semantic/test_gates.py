"""Wave 20 optional gates: import smoke, impact tests, type diff, adjudicator."""

from __future__ import annotations

import subprocess
from pathlib import Path

from mak.config import SemanticConfig
from mak.core.logging import EventType
from mak.core.types import NodeId
from tests.semantic.helpers import (
    ScriptRunner,
    committed,
    events,
    make_session,
    task,
)


def _write(tmp_path: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class TestImportSmoke:
    def test_a_module_that_stops_importing_is_reported(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            {"a.py": "X = 1\n", "broken.py": "raise RuntimeError('old')\n"},
        )
        runner = ScriptRunner({
            "t": [{"a.py": "import missing_dependency_xyz\n\nX = 1\n"}],
            "u": [{"broken.py": "raise RuntimeError('still')\n"}],
        })
        session, _, logger = make_session(
            tmp_path, runner, semantic=SemanticConfig(import_smoke=True)
        )
        session.initialize()
        session.install_plan([task("t", ["a.py"]), task("u", ["broken.py"])])
        assert session.run().ok
        tasks = session.detect_cascade_tasks()
        smoke = [t for t in tasks if t.task_id.startswith("import_smoke_fix")]
        assert [t.target_nodes for t in smoke] == [[NodeId("a.py")]]
        assert "missing_dependency_xyz" in smoke[0].description
        # broken.py failed before the wave too: not this wave's defect.
        assert all("broken.py" not in str(t.target_nodes) for t in smoke)


class TestImpactTests:
    USERS = (
        "_DB = {1: 'ann'}\n\n\n"
        "def get_user(uid):\n    return _DB[uid]\n"
    )

    def test_pairwise_semantic_conflict_is_attributed(self, tmp_path: Path) -> None:
        _write(tmp_path, {"users.py": self.USERS})
        runner = ScriptRunner({
            # A: a behaviour change with an identical signature (shape 3).
            "a": [{"users.py::function::get_user":
                   "def get_user(uid):\n    return _DB.get(uid)\n"}],
            # B: a caller written against the old contract, with its test.
            "b": [{
                "exists.py": "from users import get_user\n\n\n"
                             "def user_exists(uid):\n    try:\n        get_user(uid)\n"
                "    except KeyError:\n        return False\n    return True\n",
                "test_exists.py": "from exists import user_exists\n\n\n"
                "def test_missing_user():\n    assert not user_exists(99)\n",
            }],
        })
        session, _, logger = make_session(
            tmp_path, runner, semantic=SemanticConfig(impact_tests=True)
        )
        session.initialize()
        session.install_plan([
            task("a", ["users.py::function::get_user"]),
            task("b", ["exists.py", "test_exists.py"]),
        ])
        assert session.run().ok
        tasks = session.detect_cascade_tasks()
        impact = [t for t in tasks if t.task_id.startswith("impact_tests_fix")]
        assert len(impact) == 1
        assert "a semantic conflict between them" in impact[0].description
        (finding,) = [
            e for e in events(logger, EventType.GATE_FINDING) if "detail" in e.payload
        ]
        assert finding.payload["tasks"] == ["a", "b"]

    def test_subset_rebuilds_one_tasks_state(self, tmp_path: Path) -> None:
        _write(
            tmp_path, {"m.py": "def f():\n    return 1\n\n\ndef g():\n    return 2\n"}
        )
        runner = ScriptRunner({
            "a": [{"m.py::function::f": "def f():\n    return 10\n"}],
            "b": [{"m.py::function::g": "def g():\n    return 20\n"}],
        })
        session, store, _ = make_session(tmp_path, runner)
        runner._hold["b"] = committed(store, "m.py::function::f", "10")
        session.initialize()
        session.install_plan([task("a", ["m.py::function::f"]),
                              task("b", ["m.py::function::g"])])
        assert session.run().ok
        only_b = session._subset_files(frozenset({"b"}))["m.py"]
        assert only_b is not None
        assert "return 1\n" in only_b and "return 20" in only_b
        assert "return 10" not in only_b
        both = session._subset_files(frozenset({"a", "b"}))["m.py"]
        assert both is not None and "return 10" in both and "return 20" in both


class TestTypeGate:
    def test_only_new_diagnostics_are_reported(self, tmp_path: Path) -> None:
        _write(tmp_path, {"m.py": "def f() -> int:\n    return 1\n"})
        outputs = iter([
            "m.py:1: error: old problem  [misc]\n",
            "m.py:3: error: old problem  [misc]\n"
            "m.py:4: error: Incompatible return value type  [return-value]\n",
        ])
        calls: list[list[str]] = []

        def fake(
            argv: list[str], cwd: Path, timeout: float
        ) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 1, next(outputs), "")

        runner = ScriptRunner({"t": [{"m.py::function::f":
                                      "def f() -> int:\n    return 'x'\n"}]})
        session, _, logger = make_session(
            tmp_path, runner, semantic=SemanticConfig(type_check="mypy")
        )
        session._gates._runner = fake  # type: ignore[attr-defined]
        session.initialize()
        session.install_plan([task("t", ["m.py::function::f"])])
        assert session.run().ok
        tasks = [t for t in session.detect_cascade_tasks()
                 if t.task_id.startswith("type_check_fix")]
        assert len(tasks) == 1
        assert "return-value" in tasks[0].description
        assert "old problem" not in tasks[0].description
        assert len(calls) == 2  # baseline at initialize, then one wave-end run

    def test_a_missing_tool_is_logged_not_fatal(self, tmp_path: Path) -> None:
        _write(tmp_path, {"m.py": "X = 1\n"})

        def boom(
            argv: list[str], cwd: Path, timeout: float
        ) -> subprocess.CompletedProcess[str]:
            raise OSError("no such tool")

        runner = ScriptRunner({"t": [{"m.py": "X = 2\n"}]})
        session, _, logger = make_session(
            tmp_path, runner, semantic=SemanticConfig(type_check="mypy")
        )
        session._gates._runner = boom  # type: ignore[attr-defined]
        session.initialize()
        session.install_plan([task("t", ["m.py"])])
        assert session.run().ok
        assert session.detect_cascade_tasks() == []
        findings = events(logger, EventType.GATE_FINDING)
        errors = [e for e in findings if "error" in e.payload]
        assert errors and errors[0].payload["gate"] == "type_check"


class _FakeLLM:
    def __init__(self, answers: list[str]) -> None:
        self._answers = answers
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._answers[min(len(self.prompts) - 1, len(self._answers) - 1)]


class TestAdjudicator:
    BASE = (
        "def load(uid):\n    return {'name': 'x'}\n\n\n"
        "def show(uid):\n    return 1\n"
    )

    def _race(
        self, tmp_path: Path, llm: _FakeLLM, **semantic: object
    ) -> tuple[object, ScriptRunner, object]:
        from mak.session import Session  # noqa: F401 - type only

        _write(tmp_path, {"m.py": self.BASE})
        runner = ScriptRunner({
            "a": [{
                "m.py::function::load":
                    "def load(uid) -> 'User':\n    return User()\n",
            }],
            "b": [{"m.py::function::show": "def show(uid):\n    return load(uid)\n"}],
        })
        session, store, logger = make_session(
            tmp_path, runner, semantic=SemanticConfig(**semantic)  # type: ignore[arg-type]
        )
        session._adjudicator_llm = llm  # type: ignore[attr-defined]
        runner._hold["b"] = committed(store, "m.py::function::load", "User")
        session.initialize()
        session.install_plan([task("a", ["m.py::function::load"]),
                              task("b", ["m.py::function::show"])])
        return session.run(), runner, logger

    def test_yes_turns_a_redispatch_into_an_accept(self, tmp_path: Path) -> None:
        llm = _FakeLLM(["YES — it only passes the value through."])
        result, runner, logger = self._race(tmp_path, llm)
        assert result.ok and runner.calls["b"] == 1
        (event,) = events(logger, EventType.ADJUDICATION)
        assert event.payload["answer"] == "yes"
        assert "OLD m.py::function::load" in llm.prompts[0]

    def test_no_keeps_the_redispatch(self, tmp_path: Path) -> None:
        result, runner, _ = self._race(tmp_path, _FakeLLM(["No."]))
        assert result.ok and runner.calls["b"] == 2

    def test_budget_exhaustion_falls_back_to_redispatch(self, tmp_path: Path) -> None:
        result, runner, logger = self._race(
            tmp_path, _FakeLLM(["YES"]), adjudicator_max_calls=0
        )
        assert result.ok and runner.calls["b"] == 2
        (event,) = events(logger, EventType.ADJUDICATION)
        assert event.payload["answer"] == "skipped"
