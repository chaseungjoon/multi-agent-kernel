"""Tests for mak.agent_runner.runner.AgentRunner (API + subprocess paths)."""

from __future__ import annotations

import subprocess
import sys

import pytest

from mak.agent_runner.adapters.base_adapter import (
    AgentAdapter,
    SubprocessAgentAdapter,
)
from mak.agent_runner.protocol import (
    decode_task_result,
    encode_task_bundle,
    encode_task_result,
)
from mak.agent_runner.runner import AgentRunner
from mak.core.exceptions import AgentError
from mak.core.types import NodeId, TaskBundle, TaskResult

# --- API-style stub adapters -------------------------------------------------


class StubApiAdapter(AgentAdapter):
    """An API adapter whose ``send`` echoes a canned result."""

    agent_id = "stub-0"
    agent_type = "stub_api"

    def __init__(self, result: TaskResult) -> None:
        self._result = result
        self.sent: list[str] = []

    def format_task(self, task_bundle: TaskBundle) -> str:
        return encode_task_bundle(task_bundle)

    def send(self, prompt: str) -> str:
        self.sent.append(prompt)
        return encode_task_result(self._result)

    def parse_result(self, raw_output: str) -> TaskResult:
        return decode_task_result(raw_output)

    def health_check(self) -> bool:
        return True


class ExplodingApiAdapter(StubApiAdapter):
    """An API adapter whose backend call raises."""

    def send(self, prompt: str) -> str:
        raise RuntimeError("backend exploded")


class NotAnAdapter(AgentAdapter):
    """An adapter that is neither subprocess- nor API-shaped (no ``send``)."""

    agent_id = "bad-0"
    agent_type = "bad"

    def format_task(self, task_bundle: TaskBundle) -> str:
        return "{}"

    def parse_result(self, raw_output: str) -> TaskResult:
        return TaskResult(task_id="t", success=True)

    def health_check(self) -> bool:
        return True


# --- real subprocess adapters ------------------------------------------------

# Reads one JSON task line from stdin and replies with a success TaskResult line.
_ECHO_CODE = (
    "import sys, json\n"
    "for line in sys.stdin:\n"
    "    d = json.loads(line)\n"
    "    out = {'protocol_version': '1.0', 'task_id': d['task_id'],"
    " 'success': True, 'modified_nodes': []}\n"
    "    sys.stdout.write(json.dumps(out) + '\\n')\n"
    "    sys.stdout.flush()\n"
)

# Reads a line then reports failure (success=False).
_FAIL_CODE = (
    "import sys, json\n"
    "for line in sys.stdin:\n"
    "    d = json.loads(line)\n"
    "    out = {'protocol_version': '1.0', 'task_id': d['task_id'],"
    " 'success': False, 'error': 'agent failed'}\n"
    "    sys.stdout.write(json.dumps(out) + '\\n')\n"
    "    sys.stdout.flush()\n"
)

# Never reads or replies — used to trigger the runner's read timeout.
_HANG_CODE = "import time\ntime.sleep(30)\n"

# Emits non-JSON debug/progress preamble before the result line.
_NOISY_CODE = (
    "import sys, json\n"
    "for line in sys.stdin:\n"
    "    d = json.loads(line)\n"
    "    sys.stdout.write('INFO: starting work\\n')\n"
    "    sys.stdout.write('progress: 50%\\n')\n"
    "    out = {'protocol_version': '1.0', 'task_id': d['task_id'],"
    " 'success': True, 'modified_nodes': []}\n"
    "    sys.stdout.write(json.dumps(out) + '\\n')\n"
    "    sys.stdout.flush()\n"
)

# Pretty-prints the result JSON across multiple lines.
_MULTILINE_CODE = (
    "import sys, json\n"
    "for line in sys.stdin:\n"
    "    d = json.loads(line)\n"
    "    out = {'protocol_version': '1.0', 'task_id': d['task_id'],"
    " 'success': True, 'modified_nodes': []}\n"
    "    sys.stdout.write(json.dumps(out, indent=2) + '\\n')\n"
    "    sys.stdout.flush()\n"
)


class _ScriptSubprocessAdapter(SubprocessAgentAdapter):
    code = ""
    agent_type = "script"

    def __init__(self) -> None:
        self.agent_id = "script-0"
        self.spawned = 0

    def spawn(self, working_dir: str) -> subprocess.Popen[str]:
        self.spawned += 1
        return subprocess.Popen(
            [sys.executable, "-c", self.code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            cwd=working_dir,
        )

    def format_task(self, task_bundle: TaskBundle) -> str:
        return encode_task_bundle(task_bundle)

    def parse_result(self, raw_output: str) -> TaskResult:
        return decode_task_result(raw_output)

    def health_check(self) -> bool:
        return True


class EchoAdapter(_ScriptSubprocessAdapter):
    code = _ECHO_CODE
    agent_type = "echo"


class FailAdapter(_ScriptSubprocessAdapter):
    code = _FAIL_CODE
    agent_type = "fail"


class HangAdapter(_ScriptSubprocessAdapter):
    code = _HANG_CODE
    agent_type = "hang"


class NoisyAdapter(_ScriptSubprocessAdapter):
    code = _NOISY_CODE
    agent_type = "noisy"


class MultilineAdapter(_ScriptSubprocessAdapter):
    code = _MULTILINE_CODE
    agent_type = "multiline"


def _bundle(task_id: str = "t1") -> TaskBundle:
    return TaskBundle(task_id=task_id, description="do work")


# --- API path tests ----------------------------------------------------------


class TestApiDispatch:
    def test_assign_returns_parsed_result(self) -> None:
        adapter = StubApiAdapter(
            TaskResult(task_id="t1", success=True, modified_nodes=[NodeId("n")])
        )
        runner = AgentRunner()
        result = runner.assign(adapter, _bundle())
        assert result.success is True
        assert result.modified_nodes == [NodeId("n")]

    def test_assign_sends_formatted_bundle(self) -> None:
        adapter = StubApiAdapter(TaskResult(task_id="t1", success=True))
        AgentRunner().assign(adapter, _bundle("t1"))
        assert adapter.sent and '"t1"' in adapter.sent[0]

    def test_backend_exception_becomes_failed_result(self) -> None:
        adapter = ExplodingApiAdapter(TaskResult(task_id="t1", success=True))
        result = AgentRunner().assign(adapter, _bundle("t1"))
        assert result.success is False
        assert result.task_id == "t1"
        assert "backend exploded" in (result.error or "")

    def test_unknown_adapter_shape_raises(self) -> None:
        with pytest.raises(AgentError, match="neither an API adapter"):
            AgentRunner().assign(NotAnAdapter(), _bundle())


# --- subprocess path tests ---------------------------------------------------


class TestSubprocessDispatch:
    def test_echo_subprocess_returns_success(self) -> None:
        runner = AgentRunner()
        adapter = EchoAdapter()
        try:
            result = runner.assign(adapter, _bundle("t1"))
            assert result.success is True
            assert result.task_id == "t1"
        finally:
            runner.shutdown()

    def test_process_reused_from_pool(self) -> None:
        runner = AgentRunner()
        adapter = EchoAdapter()
        try:
            runner.assign(adapter, _bundle("t1"))
            runner.assign(adapter, _bundle("t2"))
            # Second assignment reuses the idle process — only one spawn total.
            assert adapter.spawned == 1
        finally:
            runner.shutdown()

    def test_failed_result_discards_process(self) -> None:
        runner = AgentRunner()
        adapter = FailAdapter()
        try:
            result = runner.assign(adapter, _bundle("t1"))
            assert result.success is False
            # A failed task discards the (possibly broken) process; the next
            # assignment must spawn a fresh one.
            runner.assign(adapter, _bundle("t2"))
            assert adapter.spawned == 2
        finally:
            runner.shutdown()

    def test_timeout_returns_failed_result(self) -> None:
        runner = AgentRunner(timeout_s=0.5)
        adapter = HangAdapter()
        try:
            result = runner.assign(adapter, _bundle("t1"))
            assert result.success is False
            assert "timed out" in (result.error or "")
        finally:
            runner.shutdown()

    def test_shutdown_terminates_pool(self) -> None:
        runner = AgentRunner()
        adapter = EchoAdapter()
        runner.assign(adapter, _bundle("t1"))
        runner.shutdown()
        # Pool drained; a subsequent assignment spawns anew.
        runner.assign(adapter, _bundle("t2"))
        assert adapter.spawned == 2
        runner.shutdown()

    def test_skips_noisy_preamble(self) -> None:
        # RA-6: debug/progress lines before the JSON result must not break parsing.
        runner = AgentRunner()
        adapter = NoisyAdapter()
        try:
            result = runner.assign(adapter, _bundle("t1"))
            assert result.success is True
            assert result.task_id == "t1"
        finally:
            runner.shutdown()

    def test_accepts_multiline_json(self) -> None:
        # RA-6: a result pretty-printed across several lines is accumulated.
        runner = AgentRunner()
        adapter = MultilineAdapter()
        try:
            result = runner.assign(adapter, _bundle("t1"))
            assert result.success is True
            assert result.task_id == "t1"
        finally:
            runner.shutdown()


class TestConfiguration:
    def test_discard_disabled_keeps_process(self) -> None:
        # With discard_on_failure False, a failed task does not terminate the
        # process, so it stays available and is reused on the next assignment.
        runner = AgentRunner(discard_on_failure=False)
        adapter = FailAdapter()
        try:
            runner.assign(adapter, _bundle("t1"))
            runner.assign(adapter, _bundle("t2"))
            assert adapter.spawned == 1
        finally:
            runner.shutdown()
