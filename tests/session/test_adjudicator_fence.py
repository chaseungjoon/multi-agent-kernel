"""The LLM adjudicator is fenced: configured only, logged and counted.

It is also refused where the stale-read policy would never consult it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mak.bootstrap import validate_config
from mak.config import AgentConfig, MakConfig, SemanticConfig
from mak.core.exceptions import ConfigError
from mak.core.logging import EventType
from mak.execution_result import ExecutionResult
from mak.session import SessionResult
from tests.semantic.helpers import (
    ScriptRunner,
    committed,
    events,
    make_session,
    task,
)

_BASE = "def load(uid):\n    return {'name': 'x'}\n\n\ndef show(uid):\n    return 1\n"


class _FakeLLM:
    def __init__(self, answer: str) -> None:
        self._answer = answer
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._answer


def _race(
    tmp_path: Path, llm: _FakeLLM, semantic: SemanticConfig
) -> tuple[SessionResult, ScriptRunner, object]:
    """B reads load(), then A changes its return type before B commits."""
    (tmp_path / "m.py").write_text(_BASE)
    runner = ScriptRunner({
        "a": [{
            "m.py::function::load": "def load(uid) -> 'User':\n    return User()\n"
        }],
        "b": [{"m.py::function::show": "def show(uid):\n    return load(uid)\n"}],
    })
    session, store, logger = make_session(
        tmp_path, runner, semantic=semantic, adjudicator_llm=llm
    )
    runner._hold["b"] = committed(store, "m.py::function::load", "User")
    session.initialize()
    session.install_plan([task("a", ["m.py::function::load"]),
                          task("b", ["m.py::function::show"])])
    return session.run(), runner, logger


class TestConfigFence:
    @pytest.mark.parametrize("policy", ["reject", "redispatch", "accept_if_api_stable"])
    def test_an_adjudicator_the_policy_never_consults_is_refused(
        self, policy: str
    ) -> None:
        config = MakConfig(
            agents=(AgentConfig(type="anthropic_api"),),
            semantic=SemanticConfig(stale_read=policy, adjudicator="anthropic:m"),
        )
        with pytest.raises(ConfigError, match="never consults it"):
            validate_config(config)

    def test_revalidate_with_an_adjudicator_is_accepted(self) -> None:
        config = MakConfig(
            agents=(AgentConfig(type="anthropic_api"),),
            semantic=SemanticConfig(stale_read="revalidate", adjudicator="anthropic:m"),
        )
        validate_config(config)

    def test_an_injected_model_alone_does_not_switch_it_on(
        self, tmp_path: Path
    ) -> None:
        llm = _FakeLLM("YES")
        result, runner, logger = _race(tmp_path, llm, SemanticConfig())
        assert result.ok
        assert llm.prompts == []  # never consulted
        assert runner.calls["b"] == 2  # the uncertain read was re-dispatched
        assert events(logger, EventType.ADJUDICATION) == []  # type: ignore[arg-type]
        assert result.metrics["adjudicated_accepts"] == 0.0


class TestAdjudicatedAccept:
    def test_it_is_logged_nondeterministic_and_counted(self, tmp_path: Path) -> None:
        llm = _FakeLLM("YES — it only passes the value through.")
        result, runner, logger = _race(
            tmp_path, llm, SemanticConfig(adjudicator="anthropic:fake")
        )
        assert result.ok and runner.calls["b"] == 1

        (consulted,) = events(logger, EventType.ADJUDICATION)  # type: ignore[arg-type]
        assert consulted.payload["answer"] == "yes"
        assert consulted.payload["nondeterministic"] is True
        accepted = [
            e for e in events(logger, EventType.STALE_READ)  # type: ignore[arg-type]
            if e.payload.get("task_id") == "b"
        ]
        assert accepted and all(e.payload["verdict"] == "accept" for e in accepted)
        assert any(e.payload.get("nondeterministic") is True for e in accepted)

        assert result.metrics["adjudicated_accepts"] == 1.0
        summary = ExecutionResult(initial=result).summary_line()
        assert "1 stale read(s) accepted by the LLM adjudicator" in summary

    def test_a_no_is_consulted_but_not_counted(self, tmp_path: Path) -> None:
        result, runner, logger = _race(
            tmp_path, _FakeLLM("No."), SemanticConfig(adjudicator="anthropic:fake")
        )
        assert result.ok and runner.calls["b"] == 2
        (consulted,) = events(logger, EventType.ADJUDICATION)  # type: ignore[arg-type]
        assert consulted.payload["nondeterministic"] is True
        assert result.metrics["adjudicated_accepts"] == 0.0
        assert "adjudicator" not in ExecutionResult(initial=result).summary_line()
