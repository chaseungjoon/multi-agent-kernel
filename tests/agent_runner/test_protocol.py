"""Tests for mak.agent_runner.protocol."""

from __future__ import annotations

import json
import re

import pytest

from mak.agent_runner.protocol import (
    PROTOCOL_VERSION,
    decode_task_bundle,
    decode_task_result,
    encode_task_bundle,
    encode_task_result,
    map_returned_sources,
)
from mak.core.exceptions import AgentProtocolError
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

    def test_decode_carries_the_response_metadata(self) -> None:
        data = {
            "task_id": "t1",
            "success": True,
            "no_changes_required": True,
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 3, "junk": "x"},
            "retryable": False,
        }
        result = decode_task_result(json.dumps(data))
        assert result.no_changes_required is True
        assert result.stop_reason == "tool_use"
        assert result.usage == {"input_tokens": 10, "output_tokens": 3}
        assert result.retryable is False

    def test_metadata_defaults_are_conservative(self) -> None:
        result = decode_task_result(json.dumps({"task_id": "t1", "success": True}))
        assert result.no_changes_required is False
        assert result.stop_reason is None
        assert result.usage == {}
        assert result.retryable is True


class TestMalformedTaskResults:
    """12.4 — every bad shape names the field and the type, and never TypeErrors.

    ``modified_fragments`` as a lone object was the live failure: iterating it
    yields its *keys*, so ``fragment["node_id"]`` raised
    ``TypeError: string indices must be integers`` — which the runner then
    reported as "api call failed", blaming the transport for a decode, and which
    a retry could do nothing with.
    """

    def test_a_lone_fragment_object_is_coerced(self) -> None:
        data = {
            "task_id": "t1",
            "success": True,
            "modified_fragments": {
                "node_id": "m.py::function::a",
                "new_source": "x = 1\n",
            },
        }
        result = decode_task_result(json.dumps(data))
        assert result.new_sources == {NodeId("m.py::function::a"): "x = 1\n"}

    @pytest.mark.parametrize(
        ("fragments", "expected"),
        [
            ("just a string", "must be an array"),
            (["m.py::function::a"], "modified_fragments[0]"),
            ([{"new_source": "x = 1\n"}], "node_id"),
            ([{"node_id": "m.py::function::a", "new_source": 42}], "new_source"),
            ([{"node_id": "", "new_source": "x = 1\n"}], "node_id"),
        ],
    )
    def test_bad_fragment_shapes_are_named(
        self, fragments: object, expected: str
    ) -> None:
        data = {"task_id": "t1", "success": True, "modified_fragments": fragments}
        with pytest.raises(AgentProtocolError, match=re.escape(expected)):
            decode_task_result(json.dumps(data))

    def test_a_missing_required_field_is_named(self) -> None:
        # Exactly what a truncated tool_use produces: the fields that finished.
        with pytest.raises(AgentProtocolError, match="'success'"):
            decode_task_result(json.dumps({"task_id": "t1"}))

    def test_a_non_mapping_new_sources_is_named(self) -> None:
        data = {"task_id": "t1", "success": True, "new_sources": ["x = 1\n"]}
        with pytest.raises(AgentProtocolError, match="'new_sources' must be an object"):
            decode_task_result(json.dumps(data))

    def test_a_non_string_source_is_named(self) -> None:
        data = {"task_id": "t1", "success": True, "new_sources": {"m.py": 7}}
        with pytest.raises(AgentProtocolError, match="must be a string"):
            decode_task_result(json.dumps(data))

    def test_a_non_object_payload_is_named(self) -> None:
        with pytest.raises(AgentProtocolError, match="must be an object"):
            decode_task_result(json.dumps(["t1", True]))

    def test_invalid_json_is_named(self) -> None:
        with pytest.raises(AgentProtocolError, match="not valid JSON"):
            decode_task_result("{not json")

    @pytest.mark.parametrize(
        "payload",
        [
            '{"task_id": "t1", "success": true, "modified_fragments": {"node_id": 1}}',
            '{"task_id": "t1", "success": true, "modified_nodes": "m.py"}',
            '{"task_id": "t1"}',
            "{}",
        ],
    )
    def test_no_shape_escapes_as_a_typeerror_or_keyerror(self, payload: str) -> None:
        with pytest.raises(AgentProtocolError):
            decode_task_result(payload)


class TestMapReturnedSources:
    """Wave 11.3d: the node-granularity contract, shared by every agent path."""

    def test_granted_ids_pass_through(self) -> None:
        grant = [NodeId("m.py::function::a")]
        accepted, dropped = map_returned_sources(
            grant, {NodeId("m.py::function::a"): "def a(): ...\n"}
        )
        assert accepted == {NodeId("m.py::function::a"): "def a(): ...\n"}
        assert dropped == []

    def test_symbol_ids_fold_into_a_whole_file_grant(self) -> None:
        accepted, dropped = map_returned_sources(
            [NodeId("editor/motions.py")],
            {
                NodeId("editor/motions.py::function::word"): "def word():\n    ...\n",
                NodeId("editor/motions.py::function::line"): "def line():\n    ...\n",
            },
        )
        assert dropped == []
        folded = accepted[NodeId("editor/motions.py")]
        assert folded.index("def word") < folded.index("def line")

    def test_explicit_whole_file_source_wins_over_fragments(self) -> None:
        whole = "def only():\n    return 1\n"
        accepted, _dropped = map_returned_sources(
            [NodeId("editor/motions.py")],
            {
                NodeId("editor/motions.py"): whole,
                NodeId("editor/motions.py::function::word"): "def word(): ...\n",
            },
        )
        assert accepted == {NodeId("editor/motions.py"): whole}

    def test_foreign_ids_are_reported_not_silently_dropped(self) -> None:
        accepted, dropped = map_returned_sources(
            [NodeId("m.py::function::a")],
            {NodeId("other.py::function::x"): "def x(): ...\n"},
        )
        assert accepted == {}
        ((node_id, reason),) = dropped
        assert node_id == NodeId("other.py::function::x")
        assert "outside the task" in reason

    def test_a_fragment_grant_does_not_absorb_a_whole_file_rewrite(self) -> None:
        # The reverse mismatch is a real over-reach: other nodes in that file
        # belong to other tasks and are not write-locked here.
        accepted, dropped = map_returned_sources(
            [NodeId("m.py::function::a")], {NodeId("m.py"): "def a(): ...\n"}
        )
        assert accepted == {}
        assert [n for n, _r in dropped] == [NodeId("m.py")]

    def test_a_decode_failure_keeps_the_providers_telemetry(self) -> None:
        # The adapter merges stop_reason/usage into the payload before decoding,
        # so they are in hand even when the body is undecodable. Losing them made
        # a rejected attempt log `usage={}` — hiding how many tokens it burned,
        # which is usually the clue to why the model's schema slipped.
        data = {
            "task_id": "t1",
            "success": True,
            "modified_fragments": "not an array",
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 24000, "output_tokens": 21425},
        }
        with pytest.raises(AgentProtocolError) as excinfo:
            decode_task_result(json.dumps(data))
        assert excinfo.value.stop_reason == "tool_use"
        assert excinfo.value.usage == {
            "input_tokens": 24000,
            "output_tokens": 21425,
        }
