"""Tests for domain-specific exceptions."""

import pytest

from mak.core import (
    AgentError,
    ConflictDetectionError,
    GitIntegrationError,
    LockError,
    MakError,
    NodeStoreError,
    PlannerFailedError,
    SchedulingError,
    UnknownAgentTypeError,
)


@pytest.mark.parametrize(
    "exc_class",
    [
        LockError,
        SchedulingError,
        ConflictDetectionError,
        GitIntegrationError,
        NodeStoreError,
        PlannerFailedError,
        AgentError,
        UnknownAgentTypeError,
    ],
)
def test_all_exceptions_are_mak_errors(exc_class: type) -> None:
    """Every domain exception inherits from MakError."""
    assert issubclass(exc_class, MakError)


def test_unknown_agent_type_error_is_agent_error() -> None:
    """UnknownAgentTypeError is a specialization of AgentError."""
    assert issubclass(UnknownAgentTypeError, AgentError)


@pytest.mark.parametrize(
    "exc_class",
    [
        MakError,
        LockError,
        SchedulingError,
        ConflictDetectionError,
        GitIntegrationError,
        NodeStoreError,
        PlannerFailedError,
        AgentError,
        UnknownAgentTypeError,
    ],
)
def test_exceptions_can_be_raised_and_caught(exc_class: type) -> None:
    """Domain exceptions can be raised and caught normally."""
    with pytest.raises(exc_class, match="test message"):
        raise exc_class("test message")


def test_exception_message_is_preserved() -> None:
    """Exception args are accessible after construction."""
    err = NodeStoreError("node not found")
    assert str(err) == "node not found"
    assert err.args == ("node not found",)


def test_unknown_agent_caught_as_agent_error() -> None:
    """UnknownAgentTypeError can be caught via AgentError handler."""
    with pytest.raises(AgentError):
        raise UnknownAgentTypeError("no such agent: planner-v2")


def test_unknown_agent_caught_as_mak_error() -> None:
    """UnknownAgentTypeError can be caught via MakError handler."""
    with pytest.raises(MakError):
        raise UnknownAgentTypeError("no such agent")
