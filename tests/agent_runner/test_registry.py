"""Tests for mak.agent_runner.registry."""

from __future__ import annotations

import subprocess

import pytest

from mak.core.exceptions import UnknownAgentTypeError
from mak.core.types import TaskBundle, TaskResult
from mak.agent_runner.adapters.base_adapter import AgentAdapter
from mak.agent_runner.registry import (
    ADAPTER_REGISTRY,
    clear_registry,
    get_adapter,
    list_adapters,
    register_adapter,
)


class StubAdapter(AgentAdapter):
    agent_id = "stub-0"
    agent_type = "stub"

    def spawn(self, working_dir: str) -> subprocess.Popen[str]:
        raise NotImplementedError

    def format_task(self, task_bundle: TaskBundle) -> str:
        return "{}"

    def parse_result(self, raw_output: str) -> TaskResult:
        return TaskResult(task_id="t", success=True)

    def health_check(self) -> bool:
        return True


class TestRegistry:
    def setup_method(self) -> None:
        clear_registry()

    def teardown_method(self) -> None:
        clear_registry()

    def test_register_and_get(self) -> None:
        register_adapter("stub", StubAdapter)
        adapter = get_adapter("stub")
        assert isinstance(adapter, StubAdapter)
        assert adapter.agent_type == "stub"

    def test_get_unknown_raises(self) -> None:
        with pytest.raises(UnknownAgentTypeError, match="no adapter"):
            get_adapter("nonexistent")

    def test_list_adapters(self) -> None:
        register_adapter("stub", StubAdapter)
        assert "stub" in list_adapters()

    def test_list_empty(self) -> None:
        assert list_adapters() == []

    def test_clear_registry(self) -> None:
        register_adapter("stub", StubAdapter)
        clear_registry()
        assert list_adapters() == []

    def test_overwrite_registration(self) -> None:
        register_adapter("stub", StubAdapter)
        register_adapter("stub", StubAdapter)
        assert list_adapters() == ["stub"]

    def test_multiple_registrations(self) -> None:
        register_adapter("stub_a", StubAdapter)
        register_adapter("stub_b", StubAdapter)
        names = list_adapters()
        assert "stub_a" in names
        assert "stub_b" in names

    def test_adapter_health_check(self) -> None:
        register_adapter("stub", StubAdapter)
        adapter = get_adapter("stub")
        assert adapter.health_check()

    def test_adapter_format_and_parse(self) -> None:
        register_adapter("stub", StubAdapter)
        adapter = get_adapter("stub")
        bundle = TaskBundle(task_id="t1", description="test")
        formatted = adapter.format_task(bundle)
        assert isinstance(formatted, str)
        result = adapter.parse_result(formatted)
        assert result.success
