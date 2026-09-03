"""Tests for mak.__main__: argument parsing, lifecycle wiring, error handling."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

import mak.__main__ as cli
from mak.__main__ import load_env_file, main, parse_args
from mak.core.exceptions import PlannerFailedError, PlanReviewAborted, SessionError
from mak.session import SessionResult, SessionState

_MIN_CONFIG = "agents:\n  - type: anthropic_api\n"


def _config_file(tmp_path: Path, body: str = _MIN_CONFIG) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(body)
    return path


class FakeSession:
    """Records lifecycle calls; configurable to raise or to report an outcome."""

    def __init__(
        self,
        *,
        result: SessionResult | None = None,
        tests_passed: bool = True,
        plan_error: Exception | None = None,
        recover_state: SessionState = SessionState.PLANNED,
    ) -> None:
        self._result = result or SessionResult(
            state=SessionState.COMPLETED, completed=("a",), failed=(), blocked=()
        )
        self._tests_passed = tests_passed
        self._plan_error = plan_error
        self._recover_state = recover_state
        self.state = SessionState.CREATED
        self.calls: list[str] = []

    def recover(self) -> int:
        self.calls.append("recover")
        self.state = self._recover_state  # PLANNED = found a graph; else nothing
        return 0

    def initialize(self) -> list[str]:
        self.calls.append("initialize")
        return []

    def plan(self, task: str, *, review: bool = True) -> object:
        self.calls.append(f"plan(review={review})")
        if self._plan_error is not None:
            raise self._plan_error
        return []

    def run(self) -> SessionResult:
        self.calls.append("run")
        return self._result

    def detect_cascade_tasks(self) -> list[object]:
        self.calls.append("detect_cascade_tasks")
        return []  # no cascades in the happy-path fake

    def teardown(self) -> bool:
        self.calls.append("teardown")
        return self._tests_passed


def _builder(session: FakeSession) -> cli.SessionBuilder:
    def build(args: argparse.Namespace, config: object, sandbox: object) -> object:
        return session

    return build  # type: ignore[return-value]


class TestParseArgs:
    def test_task_optional_at_parse_time(self) -> None:
        # --task is no longer argparse-required (a --recover run has no task);
        # main() enforces "task required unless --recover" instead.
        args = parse_args([])
        assert args.task is None
        assert args.recover is False

    def test_defaults(self) -> None:
        args = parse_args(["--task", "do it"])
        assert args.task == "do it"
        assert args.config is None  # None = auto-discover at load time
        assert args.no_review is False
        assert args.sandbox is False
        assert args.recover is False

    def test_flags(self) -> None:
        args = parse_args(
            ["--task", "t", "--no-review", "--sandbox", "--agent", "openai_api", "-vv"]
        )
        assert args.no_review is True
        assert args.sandbox is True
        assert args.agent == "openai_api"
        assert args.verbose == 2

    def test_models_and_max_agents(self) -> None:
        args = parse_args(
            ["--task", "t", "--models", "anthropic:claude-opus-4-8", "openai",
             "--max-agents", "5"]
        )
        assert args.models == ["anthropic:claude-opus-4-8", "openai"]
        assert args.max_agents == 5

    def test_models_default_none(self) -> None:
        args = parse_args(["--task", "t"])
        assert args.models is None
        assert args.max_agents is None


class TestLoadEnvFile:
    def test_loads_keys_from_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        env = tmp_path / ".env"
        env.write_text("# comment\nANTHROPIC_API_KEY=sk-from-file\n\nNOPE\n")
        load_env_file(env)
        assert os.environ["ANTHROPIC_API_KEY"] == "sk-from-file"

    def test_exported_var_wins_over_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-exported")
        env = tmp_path / ".env"
        env.write_text("OPENAI_API_KEY=sk-from-file\n")
        load_env_file(env)
        assert os.environ["OPENAI_API_KEY"] == "sk-exported"

    def test_missing_file_is_a_noop(self, tmp_path: Path) -> None:
        load_env_file(tmp_path / "absent.env")  # must not raise


class TestMain:
    def test_happy_path_returns_zero(self, tmp_path: Path) -> None:
        session = FakeSession()
        code = main(
            ["--task", "t", "--config", str(_config_file(tmp_path))],
            session_builder=_builder(session),
        )
        assert code == 0
        assert session.calls == [
            "initialize", "plan(review=True)", "run",
            "detect_cascade_tasks",  # cascade check after each wave
            "teardown",
        ]

    def test_recover_resumes_without_planning(self, tmp_path: Path) -> None:
        session = FakeSession(recover_state=SessionState.PLANNED)
        code = main(
            ["--recover", "--config", str(_config_file(tmp_path))],
            session_builder=_builder(session),
        )
        assert code == 0
        # recover replaces initialize+plan; run/teardown still happen.
        assert "recover" in session.calls
        assert "initialize" not in session.calls
        assert not any(c.startswith("plan") for c in session.calls)
        assert "run" in session.calls

    def test_recover_with_nothing_to_resume_returns_one(
        self, tmp_path: Path
    ) -> None:
        session = FakeSession(recover_state=SessionState.CREATED)
        code = main(
            ["--recover", "--config", str(_config_file(tmp_path))],
            session_builder=_builder(session),
        )
        assert code == 1
        assert "run" not in session.calls

    def test_task_required_without_recover(self, tmp_path: Path) -> None:
        code = main(
            ["--config", str(_config_file(tmp_path))],
            session_builder=_builder(FakeSession()),
        )
        assert code == 2  # config error: --task required

    def test_models_and_max_agents_override_config(self, tmp_path: Path) -> None:
        captured: dict[str, object] = {}

        def build(args: argparse.Namespace, config: object, sandbox: object) -> object:
            captured["config"] = config
            return FakeSession()

        code = main(
            ["--task", "t", "--config", str(_config_file(tmp_path)),
             "--models", "openai", "gemini", "--max-agents", "4"],
            session_builder=build,  # type: ignore[arg-type]
        )
        assert code == 0
        config = captured["config"]
        assert [a.type for a in config.agents] == ["openai_api", "gemini_api"]  # type: ignore[attr-defined]
        assert config.session.max_concurrent_agents == 4  # type: ignore[attr-defined]

    def test_bad_models_provider_returns_two(self, tmp_path: Path) -> None:
        code = main(
            ["--task", "t", "--config", str(_config_file(tmp_path)),
             "--models", "mistral"],
            session_builder=_builder(FakeSession()),
        )
        assert code == 2

    def test_max_agents_below_one_returns_two(self, tmp_path: Path) -> None:
        code = main(
            ["--task", "t", "--config", str(_config_file(tmp_path)),
             "--max-agents", "0"],
            session_builder=_builder(FakeSession()),
        )
        assert code == 2

    def test_no_review_flag_skips_review(self, tmp_path: Path) -> None:
        session = FakeSession()
        main(
            ["--task", "t", "--no-review", "--config", str(_config_file(tmp_path))],
            session_builder=_builder(session),
        )
        assert "plan(review=False)" in session.calls

    def test_missing_config_returns_two(self, tmp_path: Path) -> None:
        code = main(
            ["--task", "t", "--config", str(tmp_path / "nope.yaml")],
            session_builder=_builder(FakeSession()),
        )
        assert code == 2

    def test_unknown_agent_type_returns_two(self, tmp_path: Path) -> None:
        cfg = _config_file(tmp_path, "agents:\n  - type: bogus_type\n")
        code = main(
            ["--task", "t", "--config", str(cfg)],
            session_builder=_builder(FakeSession()),
        )
        assert code == 2

    def test_plan_aborted_returns_one(self, tmp_path: Path) -> None:
        session = FakeSession(plan_error=PlanReviewAborted("user aborted"))
        code = main(
            ["--task", "t", "--config", str(_config_file(tmp_path))],
            session_builder=_builder(session),
        )
        assert code == 1
        assert "run" not in session.calls

    def test_planner_failure_returns_one(self, tmp_path: Path) -> None:
        session = FakeSession(plan_error=PlannerFailedError("bad plan"))
        code = main(
            ["--task", "t", "--config", str(_config_file(tmp_path))],
            session_builder=_builder(session),
        )
        assert code == 1

    def test_generic_mak_error_returns_one(self, tmp_path: Path) -> None:
        session = FakeSession(plan_error=SessionError("boom"))
        code = main(
            ["--task", "t", "--config", str(_config_file(tmp_path))],
            session_builder=_builder(session),
        )
        assert code == 1

    def test_blocked_run_returns_one(self, tmp_path: Path) -> None:
        session = FakeSession(
            result=SessionResult(
                state=SessionState.FAILED, completed=(), failed=(), blocked=("a",)
            )
        )
        code = main(
            ["--task", "t", "--config", str(_config_file(tmp_path))],
            session_builder=_builder(session),
        )
        assert code == 1

    def test_failing_tests_returns_one(self, tmp_path: Path) -> None:
        session = FakeSession(tests_passed=False)
        code = main(
            ["--task", "t", "--config", str(_config_file(tmp_path))],
            session_builder=_builder(session),
        )
        assert code == 1

    def test_sandbox_without_docker_returns_two(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "docker_available", lambda _bin: False)
        code = main(
            ["--task", "t", "--sandbox", "--config", str(_config_file(tmp_path))],
            session_builder=_builder(FakeSession()),
        )
        assert code == 2


class TestModelCaveatWarnings:
    def test_fable_models_warn_once_on_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg = _config_file(
            tmp_path,
            "planner:\n  model: claude-fable-5\n"
            "agents:\n  - type: anthropic_api\n    model: claude-fable-5\n",
        )
        main(
            ["--task", "t", "--config", str(cfg)],
            session_builder=_builder(FakeSession()),
        )
        err = capsys.readouterr().err
        # One deduplicated warning even though planner AND agent are Fable.
        assert err.count("claude-fable-5") == 1
        assert "30-day data retention" in err

    def test_ordinary_models_do_not_warn(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(
            ["--task", "t", "--config", str(_config_file(tmp_path))],
            session_builder=_builder(FakeSession()),
        )
        assert "mak: warning:" not in capsys.readouterr().err


_LOCAL_AGENTS = (
    "agents:\n"
    "  - type: ollama_api\n"
    "    model: qwen2.5-coder:14b\n"
)


class TestLocalPlannerMismatchWarning:
    """Wave 15.11: `--models ollama:...` must not look local and quietly not be."""

    def test_an_all_local_roster_with_a_hosted_planner_warns(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg = _config_file(
            tmp_path, f"planner:\n  model: claude-opus-5\n{_LOCAL_AGENTS}"
        )
        main(
            ["--task", "t", "--config", str(cfg)],
            session_builder=_builder(FakeSession()),
        )
        err = capsys.readouterr().err
        assert "every agent is local" in err
        assert "planner.backend" in err

    def test_a_fully_local_run_stays_silent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg = _config_file(
            tmp_path,
            "planner:\n"
            "  model: qwen2.5-coder:14b\n"
            "  backend: ollama\n"
            "  base_url: http://localhost:11434\n"
            f"{_LOCAL_AGENTS}",
        )
        main(
            ["--task", "t", "--config", str(cfg)],
            session_builder=_builder(FakeSession()),
        )
        assert "every agent is local" not in capsys.readouterr().err

    def test_a_cloud_roster_stays_silent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(
            ["--task", "t", "--config", str(_config_file(tmp_path))],
            session_builder=_builder(FakeSession()),
        )
        assert "every agent is local" not in capsys.readouterr().err

    def test_a_hybrid_roster_stays_silent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A cloud planner beside *some* local agents is an ordinary mixed
        # roster, not the silent-surprise case the warning exists for.
        cfg = _config_file(
            tmp_path,
            "planner:\n  model: claude-opus-5\n"
            "agents:\n"
            "  - type: anthropic_api\n"
            "  - type: ollama_api\n    model: qwen2.5-coder:14b\n",
        )
        main(
            ["--task", "t", "--config", str(cfg)],
            session_builder=_builder(FakeSession()),
        )
        assert "every agent is local" not in capsys.readouterr().err


class TestPlannerApiKeyResolution:
    def test_an_explicit_api_key_env_wins_over_inference(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-inferred")
        monkeypatch.setenv("VLLM_TOKEN", "sk-explicit")
        cfg = _config_file(
            tmp_path,
            "planner:\n  model: claude-opus-5\n  api_key_env: VLLM_TOKEN\n"
            "agents:\n  - type: anthropic_api\n",
        )
        from mak.config import load_config

        assert cli._planner_api_key(load_config(cfg)) == "sk-explicit"

    def test_a_local_planner_resolves_to_no_key(self, tmp_path: Path) -> None:
        # None is what lets the adapter apply its placeholder rule rather than
        # forwarding a real cloud key to a local host.
        cfg = _config_file(
            tmp_path,
            "planner:\n  model: qwen2.5-coder:14b\n  backend: ollama\n"
            f"{_LOCAL_AGENTS}",
        )
        from mak.config import load_config

        assert cli._planner_api_key(load_config(cfg)) is None
