"""Token accounting: usage is read off responses, never scraped from the SDK.

The suite this replaces tested three pure helpers (``anthropic_tokens`` and
friends) and that installing the SDK monkeypatches twice was idempotent. Both
held perfectly while the feature was broken: the patch wrapped
``Messages.create``, and every Anthropic call MAK makes — agent adapter and
planner alike — goes through ``messages.stream``. Nothing asserted that the
patched method was the one MAK calls, so a counter that reported a flat zero for
the default provider passed.

These tests assert the property that actually matters instead: tokens a provider
reported on a **streamed** response reach ``Session.total_tokens``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cli.runner import session_tokens

from mak.agent_runner.adapters.anthropic_api_adapter import AnthropicApiAdapter
from mak.core.types import TaskBundle, TaskResult
from mak.node_store.store import NodeStore


class _Usage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _ToolBlock:
    type = "tool_use"
    name = "submit_task_result"

    def __init__(self, payload: dict[str, Any]) -> None:
        self.input = payload


class _Message:
    stop_reason = "tool_use"

    def __init__(self, payload: dict[str, Any], usage: _Usage) -> None:
        self.content = [_ToolBlock(payload)]
        self.usage = usage


class _Stream:
    def __init__(self, message: _Message) -> None:
        self._message = message

    def __enter__(self) -> _Stream:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get_final_message(self) -> _Message:
        return self._message


class _Messages:
    """Records which entry point was used, so the regression stays visible."""

    def __init__(self, message: _Message) -> None:
        self._message = message
        self.stream_calls = 0
        self.create_calls = 0

    def stream(self, **_kwargs: Any) -> _Stream:
        self.stream_calls += 1
        return _Stream(self._message)

    def create(self, **_kwargs: Any) -> _Message:  # pragma: no cover - see below
        self.create_calls += 1
        return self._message


class _Client:
    def __init__(self, message: _Message) -> None:
        self.messages = _Messages(message)


def _adapter(usage: _Usage) -> tuple[AnthropicApiAdapter, _Client]:
    message = _Message({"task_id": "t1", "success": True}, usage)
    client = _Client(message)
    return AnthropicApiAdapter(client=client, model="claude-sonnet-5"), client


class TestUsageSurvivesStreaming:
    def test_streamed_response_reports_its_usage(self) -> None:
        adapter, client = _adapter(_Usage(input_tokens=120, output_tokens=34))
        bundle = TaskBundle(task_id="t1", description="d", target_nodes=[])
        result = adapter.parse_result(adapter.send(adapter.format_task(bundle)))
        # The regression in one line: MAK streams, so a counter hooked onto
        # `create` would have observed nothing at all here.
        assert client.messages.stream_calls == 1
        assert client.messages.create_calls == 0
        assert result.usage == {"input_tokens": 120, "output_tokens": 34}


class _NoUsageResult:
    """A session double exposing only what ``session_tokens`` reads."""

    def __init__(self, total: Any) -> None:
        self.total_tokens = total


class TestSessionTokens:
    def test_reads_the_sessions_own_total(self) -> None:
        assert session_tokens(_NoUsageResult(1234)) == 1234

    def test_missing_or_non_numeric_total_is_zero(self) -> None:
        assert session_tokens(object()) == 0
        assert session_tokens(_NoUsageResult(None)) == 0


class _UsageRunner:
    """An agent that reports provider usage alongside its work."""

    def __init__(self, usage: dict[str, int]) -> None:
        self._usage = usage

    def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
        node = task.target_nodes[0]
        return TaskResult(
            task_id=task.task_id,
            success=True,
            modified_nodes=[node],
            new_sources={node: "def f():\n    return 2\n"},
            usage=dict(self._usage),
        )


class _PlannerWithUsage:
    token_usage = {"input_tokens": 500, "output_tokens": 50}


def _run_one(tmp_path: Path, usage: dict[str, int]) -> Any:
    """Run a one-task session whose agent reports ``usage``; return the session."""
    from tests.test_session import _session, _task

    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    store = NodeStore(tmp_path / ".mak" / "node_store")
    session = _session(
        tmp_path, runner=_UsageRunner(usage), node_store=store
    )
    session.initialize()
    session.install_plan([_task("t1", ["a.py::function::f"])])
    session.run()
    return session


class TestSessionAggregatesUsage:
    def test_agent_and_planner_usage_are_summed(self, tmp_path: Path) -> None:
        session = _run_one(tmp_path, {"input_tokens": 10, "output_tokens": 5})

        assert session.token_usage["input_tokens"] == 10
        assert session.total_tokens == 15

        # A planner's own spend — decomposition, its retries, the critique pass —
        # is part of what a run cost, so it is counted too.
        session._planner = _PlannerWithUsage()  # type: ignore[assignment]
        assert session.total_tokens == 15 + 550

    def test_total_ignores_a_providers_own_total_field(self, tmp_path: Path) -> None:
        # Some SDKs report input, output, *and* a total. Summing every integer
        # would double-count; only the two directional counters are added.
        session = _run_one(
            tmp_path, {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        )
        assert session.total_tokens == 15
