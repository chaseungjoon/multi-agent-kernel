"""Tests for the CLI subprocess adapters (claude_code, codex, copilot)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from mak.agent_runner.adapters import cli_adapter as cli_mod
from mak.agent_runner.adapters.claude_code_adapter import ClaudeCodeAdapter
from mak.agent_runner.adapters.codex_adapter import CodexAdapter
from mak.agent_runner.adapters.copilot_adapter import CopilotAdapter
from mak.agent_runner.sandbox import SandboxConfig
from mak.core.types import NodeId, TaskBundle, TaskResult


class TestCommandConstruction:
    def test_default_commands(self) -> None:
        assert ClaudeCodeAdapter().command == ["claude"]
        assert CodexAdapter().command == ["codex"]
        assert CopilotAdapter().command == ["gh", "copilot"]

    def test_agent_types(self) -> None:
        assert ClaudeCodeAdapter().agent_type == "claude_code"
        assert CodexAdapter().agent_type == "codex"
        assert CopilotAdapter().agent_type == "copilot"

    def test_cmd_override_replaces_binary_only(self) -> None:
        # Single-element command: cmd replaces it.
        assert ClaudeCodeAdapter(cmd="/opt/claude").command == ["/opt/claude"]
        # Multi-element command: cmd replaces only the first element.
        assert CopilotAdapter(cmd="/usr/bin/gh").command == ["/usr/bin/gh", "copilot"]


class TestFormatAndParse:
    def test_round_trip(self) -> None:
        adapter = CodexAdapter()
        bundle = TaskBundle(task_id="t1", description="do it")
        line = adapter.format_task(bundle)
        result = adapter.parse_result(
            '{"protocol_version": "1.0", "task_id": "t1", "success": true,'
            ' "modified_nodes": ["m.py::function::f"]}'
        )
        assert '"task_id": "t1"' in line
        assert result.task_id == "t1"
        assert result.success is True
        assert result.modified_nodes == [NodeId("m.py::function::f")]


class TestSpawn:
    def test_spawn_uses_command_in_working_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        captured: dict[str, Any] = {}

        def fake_popen(argv: list[str], **kwargs: Any) -> object:
            captured["argv"] = argv
            captured["cwd"] = kwargs.get("cwd")
            return object()

        monkeypatch.setattr(cli_mod.subprocess, "Popen", fake_popen)
        ClaudeCodeAdapter(cmd="claude").spawn(str(tmp_path))
        assert captured["argv"] == ["claude"]
        assert captured["cwd"] == str(tmp_path)

    def test_spawn_wraps_in_sandbox(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        captured: dict[str, Any] = {}

        def fake_popen(argv: list[str], **kwargs: Any) -> object:
            captured["argv"] = argv
            return object()

        monkeypatch.setattr(cli_mod.subprocess, "Popen", fake_popen)
        adapter = ClaudeCodeAdapter(cmd="claude", sandbox=SandboxConfig())
        adapter.spawn(str(tmp_path))
        assert captured["argv"][0] == "docker"
        assert captured["argv"][-1] == "claude"


class TestHealthCheck:
    def test_true_when_version_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(args=args, returncode=0)

        monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
        assert CodexAdapter().health_check() is True

    def test_false_when_binary_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            raise FileNotFoundError("codex")

        monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
        assert CodexAdapter().health_check() is False


class TestRunnerIntegration:
    """The CLI adapters must drive cleanly through the real AgentRunner pool."""

    def test_echo_agent_round_trips_through_runner(self, tmp_path: Path) -> None:
        import sys

        from mak.agent_runner.runner import AgentRunner

        # A tiny stub CLI that speaks the MAK line protocol.
        script = tmp_path / "agent.py"
        script.write_text(
            "import sys, json\n"
            "for line in sys.stdin:\n"
            "    d = json.loads(line)\n"
            "    print(json.dumps({'protocol_version': '1.0',"
            " 'task_id': d['task_id'], 'success': True, 'modified_nodes': []}))\n"
            "    sys.stdout.flush()\n"
        )
        adapter = CodexAdapter(command=[sys.executable, str(script)])
        runner = AgentRunner()
        try:
            result = runner.assign(adapter, TaskBundle(task_id="t9", description="x"))
            assert isinstance(result, TaskResult)
            assert result.success is True
            assert result.task_id == "t9"
        finally:
            runner.shutdown()
