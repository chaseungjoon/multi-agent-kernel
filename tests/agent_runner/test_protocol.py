"""Tests for mak.agent_runner.protocol."""

from __future__ import annotations

import json

import pytest

from mak.agent_runner.protocol import (
    PROTOCOL_VERSION,
    decode_task_bundle,
    decode_task_result,
    encode_task_bundle,
    encode_task_result,
)
from mak.core.types import (
    LockEntry,
    LockMode,
    NodeId,
    ResourceKind,
    ResourceRef,
    TaskBundle,
    TaskResult,
)


class TestTaskBundleProtocol:
    def test_encode_includes_protocol_version(self) -> None:
        bundle = TaskBundle(task_id="t1", description="do stuff")
        raw = encode_task_bundle(bundle)
        data = json.loads(raw)
        assert data["protocol_version"] == PROTOCOL_VERSION

    def test_encode_ends_with_newline(self) -> None:
        bundle = TaskBundle(task_id="t1", description="do stuff")
        raw = encode_task_bundle(bundle)
        assert raw.endswith("\n")

    def test_round_trip(self) -> None:
        bundle = TaskBundle(
            task_id="t1",
            description="implement foo",
            target_nodes=[NodeId("mod.py::function::foo")],
            context={"style": "snake_case"},
        )
        raw = encode_task_bundle(bundle)
        decoded = decode_task_bundle(raw)
        assert decoded.task_id == bundle.task_id
        assert decoded.description == bundle.description
        assert decoded.target_nodes == bundle.target_nodes
        assert decoded.context == bundle.context

    def test_decode_with_version(self) -> None:
        data = {
            "protocol_version": "1.0",
            "task_id": "t1",
            "description": "test",
        }
        bundle = decode_task_bundle(json.dumps(data))
        assert bundle.task_id == "t1"

    def test_decode_wrong_version_raises(self) -> None:
        data = {
            "protocol_version": "99.0",
            "task_id": "t1",
            "description": "test",
        }
        with pytest.raises(ValueError, match="unsupported protocol version"):
            decode_task_bundle(json.dumps(data))

    def test_decode_no_version_ok(self) -> None:
        data = {"task_id": "t1", "description": "test"}
        bundle = decode_task_bundle(json.dumps(data))
        assert bundle.task_id == "t1"

    def test_decode_defaults(self) -> None:
        data = {
            "protocol_version": "1.0",
            "task_id": "t1",
            "description": "test",
        }
        bundle = decode_task_bundle(json.dumps(data))
        assert bundle.target_nodes == []
        assert bundle.context == {}

    def test_locks_round_trip_as_lock_entries(self) -> None:
        # Risk M2: decoded locks must be LockEntry objects, not raw dicts.
        lock = LockEntry(
            resource=ResourceRef(kind=ResourceKind.SYMBOL, path="a.py", symbol="foo"),
            mode=LockMode.WRITE,
            holder="agent-1",
            acquired_at=1234.5,
        )
        bundle = TaskBundle(task_id="t1", description="d", locks=[lock])
        decoded = decode_task_bundle(encode_task_bundle(bundle))
        assert decoded.locks == [lock]
        assert isinstance(decoded.locks[0], LockEntry)
        assert decoded.locks[0].resource.symbol == "foo"


class TestTaskResultProtocol:
    def test_encode_includes_protocol_version(self) -> None:
        result = TaskResult(task_id="t1", success=True)
        raw = encode_task_result(result)
        data = json.loads(raw)
        assert data["protocol_version"] == PROTOCOL_VERSION

    def test_round_trip(self) -> None:
        result = TaskResult(
            task_id="t1",
            success=True,
            modified_nodes=[NodeId("mod.py::function::foo")],
            error=None,
        )
        raw = encode_task_result(result)
        decoded = decode_task_result(raw)
        assert decoded.task_id == result.task_id
        assert decoded.success == result.success
        assert decoded.modified_nodes == result.modified_nodes
        assert decoded.error is None

    def test_round_trip_failure(self) -> None:
        result = TaskResult(
            task_id="t1",
            success=False,
            error="something broke",
        )
        raw = encode_task_result(result)
        decoded = decode_task_result(raw)
        assert not decoded.success
        assert decoded.error == "something broke"

    def test_decode_wrong_version_raises(self) -> None:
        data = {
            "protocol_version": "2.0",
            "task_id": "t1",
            "success": True,
        }
        with pytest.raises(ValueError, match="unsupported protocol version"):
            decode_task_result(json.dumps(data))

    def test_decode_modified_fragments_into_new_sources(self) -> None:
        # The shape the API adapters elicit: an array of {node_id, new_source}.
        src_a = "def a():\n    return 1\n"
        src_b = "def b():\n    return 2\n"
        data = {
            "task_id": "t1",
            "success": True,
            "modified_fragments": [
                {"node_id": "m.py::function::a", "new_source": src_a},
                {"node_id": "m.py::function::b", "new_source": src_b},
            ],
        }
        result = decode_task_result(json.dumps(data))
        assert result.modified_nodes == [
            NodeId("m.py::function::a"),
            NodeId("m.py::function::b"),
        ]
        assert result.new_sources[NodeId("m.py::function::a")] == src_a
        assert result.new_sources[NodeId("m.py::function::b")] == src_b

    def test_decode_explicit_new_sources_mapping(self) -> None:
        data = {
            "task_id": "t1",
            "success": True,
            "new_sources": {"m.py::function::a": "x = 1\n"},
        }
        result = decode_task_result(json.dumps(data))
        assert result.new_sources == {NodeId("m.py::function::a"): "x = 1\n"}
        assert result.modified_nodes == [NodeId("m.py::function::a")]

    def test_new_sources_round_trip(self) -> None:
        result = TaskResult(
            task_id="t1",
            success=True,
            modified_nodes=[NodeId("m.py::function::a")],
            new_sources={NodeId("m.py::function::a"): "def a():\n    return 9\n"},
        )
        decoded = decode_task_result(encode_task_result(result))
        assert decoded.new_sources == result.new_sources
        assert decoded.modified_nodes == result.modified_nodes

    def test_decode_ids_only_leaves_new_sources_empty(self) -> None:
        # Legacy / ids-only result: no source on the wire, new_sources stays empty.
        data = {
            "task_id": "t1",
            "success": True,
            "modified_nodes": ["m.py::function::a"],
        }
        result = decode_task_result(json.dumps(data))
        assert result.modified_nodes == [NodeId("m.py::function::a")]
        assert result.new_sources == {}
