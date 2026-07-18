"""Tests for the CLI protocol-bridge wrappers (bridge + per-CLI specs)."""

from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Callable
from typing import Any

import pytest

from mak.agent_runner.wrappers import bridge
from mak.agent_runner.wrappers.bridge import (
    CliSpec,
    build_prompt,
    extract_json_object,
    run_task,
)
from mak.core.types import NodeId, TaskBundle

_SPEC = CliSpec(
    agent_type="claude_code",
    cli_name="claude",
    base_argv=("claude", "-p"),
    prompt_via="arg",
)


class TestExtractJsonObject:
    def test_plain_object(self) -> None:
        assert extract_json_object('{"a": 1}') == {"a": 1}

    def test_object_in_prose_and_fences(self) -> None:
        text = (
            'Here you go:\n```json\n'
            '{"m.py::function::f": "def f():\\n    return 1\\n"}\n```\ndone'
        )
        assert extract_json_object(text) == {
            "m.py::function::f": "def f():\n    return 1\n"
        }

    def test_braces_inside_strings_are_ignored(self) -> None:
        assert extract_json_object('{"k": "a { nested } brace"}') == {
            "k": "a { nested } brace"
        }

    def test_no_object_returns_none(self) -> None:
        assert extract_json_object("no json here") is None

    def test_array_is_not_an_object(self) -> None:
        assert extract_json_object("[1, 2, 3]") is None


class TestBuildPrompt:
    def test_includes_targets_ids_and_context(self) -> None:
        bundle = TaskBundle(
            task_id="t",
            description="make foo return 42",
            target_nodes=[NodeId("m.py::function::foo")],
            context={
                "write_source:m.py::function::foo": "def foo():\n    return 1\n",
                "read_source:m.py::function::bar": "def bar():\n    return 2\n",
            },
        )
        prompt = build_prompt(bundle)
        assert "make foo return 42" in prompt
        assert "m.py::function::foo" in prompt
        assert "def foo():" in prompt
        assert "read-only" in prompt.lower()
        assert "def bar():" in prompt


def _fake_run(
    stdout: str = "", returncode: int = 0
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

    return run


class TestRunTask:
    def _bundle(self) -> TaskBundle:
        return TaskBundle(
            task_id="t1",
            description="rewrite foo",
            target_nodes=[NodeId("m.py::function::foo")],
            context={"write_source:m.py::function::foo": "def foo():\n    return 1\n"},
        )

    def test_success_maps_authorized_sources(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bridge.shutil, "which", lambda _b: "/bin/claude")
        out = json.dumps({"m.py::function::foo": "def foo():\n    return 42\n"})
        monkeypatch.setattr(bridge.subprocess, "run", _fake_run(stdout=out))
        result = run_task(_SPEC, self._bundle(), None)
        assert result.success is True
        assert result.new_sources[NodeId("m.py::function::foo")] == (
            "def foo():\n    return 42\n"
        )

    def test_out_of_scope_edits_are_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bridge.shutil, "which", lambda _b: "/bin/claude")
        # CLI returns a node the task was not authorized to touch → ignored.
        out = json.dumps({"other.py::function::x": "def x(): ..."})
        monkeypatch.setattr(bridge.subprocess, "run", _fake_run(stdout=out))
        result = run_task(_SPEC, self._bundle(), None)
        assert result.success is False
        assert result.new_sources == {}

    def test_missing_cli_fails_cleanly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bridge.shutil, "which", lambda _b: None)
        result = run_task(_SPEC, self._bundle(), None)
        assert result.success is False
        assert "not found" in (result.error or "")

    def test_explicit_error_object(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bridge.shutil, "which", lambda _b: "/bin/claude")
        out = json.dumps({"error": "could not do it"})
        monkeypatch.setattr(bridge.subprocess, "run", _fake_run(stdout=out))
        result = run_task(_SPEC, self._bundle(), None)
        assert result.success is False
        assert result.error == "could not do it"

    def test_nonzero_exit_fails_cleanly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bridge.shutil, "which", lambda _b: "/bin/claude")
        monkeypatch.setattr(
            bridge.subprocess, "run", _fake_run(stdout="boom", returncode=1)
        )
        result = run_task(_SPEC, self._bundle(), None)
        assert result.success is False


class TestMain:
    def test_health_check_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bridge.shutil, "which", lambda _b: "/bin/claude")
        assert bridge.main(_SPEC, ["--health-check"]) == 0

    def test_health_check_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bridge.shutil, "which", lambda _b: None)
        assert bridge.main(_SPEC, ["--health-check"]) == 1

    def test_bridge_loop_reads_tasks_writes_results(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(bridge.shutil, "which", lambda _b: "/bin/claude")
        out = json.dumps({"m.py::function::foo": "def foo():\n    return 42\n"})
        monkeypatch.setattr(bridge.subprocess, "run", _fake_run(stdout=out))
        bundle = TaskBundle(
            task_id="t1",
            description="rewrite foo",
            target_nodes=[NodeId("m.py::function::foo")],
            context={"write_source:m.py::function::foo": "def foo():\n    return 1\n"},
        )
        from mak.agent_runner.protocol import encode_task_bundle

        monkeypatch.setattr(
            bridge.sys, "stdin", io.StringIO(encode_task_bundle(bundle))
        )
        assert bridge.main(_SPEC, []) == 0
        emitted = json.loads(capsys.readouterr().out)
        assert emitted["task_id"] == "t1"
        assert emitted["success"] is True
