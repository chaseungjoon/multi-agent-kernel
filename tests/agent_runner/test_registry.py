"""Tests for mak.agent_runner.registry."""

from __future__ import annotations

import pytest

from mak.agent_runner.adapters.base_adapter import AgentAdapter
from mak.agent_runner.registry import AdapterRegistry
from mak.core.exceptions import ConfigError, UnknownAgentTypeError
from mak.core.types import TaskBundle, TaskResult


class StubAdapter(AgentAdapter):
    """Minimal API-style adapter (no subprocess) for registry tests."""

    agent_id = "stub-0"
    agent_type = "stub"

    def format_task(self, task_bundle: TaskBundle) -> str:
        return "{}"

    def parse_result(self, raw_output: str) -> TaskResult:
        return TaskResult(task_id="t", success=True)

    def health_check(self) -> bool:
        return True


class TestAdapterRegistry:
    def test_register_and_get(self) -> None:
        registry = AdapterRegistry()
        registry.register("stub", StubAdapter)
        adapter = registry.get("stub")
        assert isinstance(adapter, StubAdapter)
        assert adapter.agent_type == "stub"

    def test_get_unknown_raises(self) -> None:
        registry = AdapterRegistry()
        with pytest.raises(UnknownAgentTypeError, match="no adapter"):
            registry.get("nonexistent")

    def test_list_types(self) -> None:
        registry = AdapterRegistry()
        registry.register("stub", StubAdapter)
        assert "stub" in registry.list_types()

    def test_list_empty(self) -> None:
        assert AdapterRegistry().list_types() == []

    def test_clear(self) -> None:
        registry = AdapterRegistry()
        registry.register("stub", StubAdapter)
        registry.clear()
        assert registry.list_types() == []

    def test_a_second_registration_of_one_id_is_refused(self) -> None:
        """Wave 22: the overwrite that silently dropped a configured agent."""
        registry = AdapterRegistry()
        registry.register("stub", StubAdapter)
        with pytest.raises(ConfigError, match="two agents are configured"):
            registry.register("stub", StubAdapter)

    def test_replace_factory_is_the_deliberate_door(self) -> None:
        registry = AdapterRegistry()
        registry.register("stub", StubAdapter)
        double = StubAdapter()
        registry.replace_factory("stub", lambda: double)
        assert registry.get("stub") is double
        assert registry.list_ids() == ["stub"]

    def test_replacing_an_unregistered_id_is_refused(self) -> None:
        """A typo here would create an agent nothing ever dispatches to."""
        registry = AdapterRegistry()
        with pytest.raises(UnknownAgentTypeError, match="cannot replace"):
            registry.replace_factory("absent", StubAdapter)

    def test_list_ids_is_the_current_name(self) -> None:
        registry = AdapterRegistry()
        registry.register("stub", StubAdapter)
        assert registry.list_ids() == registry.list_types() == ["stub"]

    def test_registries_are_isolated(self) -> None:
        # Regression for the old module-global registry: instances must not share state.
        a = AdapterRegistry()
        b = AdapterRegistry()
        a.register("stub", StubAdapter)
        assert b.list_types() == []

    def test_multiple_registrations(self) -> None:
        registry = AdapterRegistry()
        registry.register("stub_a", StubAdapter)
        registry.register("stub_b", StubAdapter)
        names = registry.list_types()
        assert "stub_a" in names
        assert "stub_b" in names

    def test_adapter_health_check(self) -> None:
        registry = AdapterRegistry()
        registry.register("stub", StubAdapter)
        assert registry.get("stub").health_check()

    def test_adapter_format_and_parse(self) -> None:
        registry = AdapterRegistry()
        registry.register("stub", StubAdapter)
        adapter = registry.get("stub")
        bundle = TaskBundle(task_id="t1", description="test")
        formatted = adapter.format_task(bundle)
        assert isinstance(formatted, str)
        assert adapter.parse_result(formatted).success
