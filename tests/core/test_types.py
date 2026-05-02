"""Smoke tests for shared core value objects."""

import dataclasses

from mak.core import (
    LockEntry,
    LockMode,
    NodeFragment,
    NodeId,
    ResourceKind,
    ResourceRef,
    SubTask,
    TaskBundle,
    TaskResult,
)


def test_resource_ref_defaults_to_file_without_symbol() -> None:
    """Resource references can represent file-level locks."""
    resource = ResourceRef(kind=ResourceKind.FILE, path="mak/core/types.py")

    assert resource.kind is ResourceKind.FILE
    assert resource.path == "mak/core/types.py"
    assert resource.symbol is None


def test_lock_mode_values_match_wire_contract() -> None:
    """Lock mode enum values stay stable for serialized contracts."""
    assert LockMode.READ.value == "read"
    assert LockMode.WRITE.value == "write"


def test_lock_mode_intent_write() -> None:
    """Intent-write mode is available."""
    assert LockMode.INTENT_WRITE.value == "intent_write"


def test_node_id_is_a_string() -> None:
    """NodeId wraps a plain string."""
    nid = NodeId("mak/core/types.py::ResourceRef")
    assert isinstance(nid, str)
    assert nid == "mak/core/types.py::ResourceRef"


def test_node_fragment_creation() -> None:
    """NodeFragment stores AST fragment metadata."""
    nid = NodeId("mak/core/types.py::ResourceRef")
    frag = NodeFragment(node_id=nid, kind="class", source="class ResourceRef: ...", version=1)

    assert frag.node_id == nid
    assert frag.kind == "class"
    assert frag.source == "class ResourceRef: ..."
    assert frag.version == 1


def test_lock_entry_creation() -> None:
    """LockEntry captures all lock metadata."""
    ref = ResourceRef(kind=ResourceKind.FILE, path="foo.py")
    entry = LockEntry(resource=ref, mode=LockMode.WRITE, holder="agent-1", acquired_at=1000.0)

    assert entry.resource is ref
    assert entry.mode is LockMode.WRITE
    assert entry.holder == "agent-1"
    assert entry.acquired_at == 1000.0


def test_task_bundle_defaults() -> None:
    """TaskBundle fields default to empty collections."""
    bundle = TaskBundle(task_id="t1", description="do stuff")

    assert bundle.task_id == "t1"
    assert bundle.description == "do stuff"
    assert bundle.target_nodes == []
    assert bundle.locks == []
    assert bundle.context == {}


def test_task_bundle_with_values() -> None:
    """TaskBundle accepts explicit values for all fields."""
    nid = NodeId("a.py::foo")
    ref = ResourceRef(kind=ResourceKind.SYMBOL, path="a.py", symbol="foo")
    lock = LockEntry(resource=ref, mode=LockMode.READ, holder="agent-2", acquired_at=2000.0)
    bundle = TaskBundle(
        task_id="t2",
        description="refactor foo",
        target_nodes=[nid],
        locks=[lock],
        context={"reason": "cleanup"},
    )

    assert bundle.target_nodes == [nid]
    assert bundle.locks == [lock]
    assert bundle.context == {"reason": "cleanup"}


def test_task_result_success() -> None:
    """TaskResult represents a successful execution."""
    nid = NodeId("a.py::bar")
    result = TaskResult(task_id="t1", success=True, modified_nodes=[nid])

    assert result.success is True
    assert result.modified_nodes == [nid]
    assert result.error is None


def test_task_result_failure() -> None:
    """TaskResult captures error on failure."""
    result = TaskResult(task_id="t1", success=False, error="something broke")

    assert result.success is False
    assert result.modified_nodes == []
    assert result.error == "something broke"


def test_sub_task_creation() -> None:
    """SubTask tracks dependencies and agent type."""
    nid = NodeId("b.py::baz")
    sub = SubTask(
        task_id="s1",
        description="implement baz",
        target_nodes=[nid],
        depends_on=["t0"],
        agent_type="coder",
    )

    assert sub.task_id == "s1"
    assert sub.target_nodes == [nid]
    assert sub.depends_on == ["t0"]
    assert sub.agent_type == "coder"


def test_sub_task_defaults() -> None:
    """SubTask fields default to empty collections."""
    sub = SubTask(task_id="s2", description="review")

    assert sub.target_nodes == []
    assert sub.depends_on == []
    assert sub.agent_type == ""


def test_node_fragment_round_trip() -> None:
    """NodeFragment survives dataclasses.asdict() round-trip."""
    nid = NodeId("x.py::Cls")
    frag = NodeFragment(node_id=nid, kind="class", source="class Cls: pass", version=3)
    d = dataclasses.asdict(frag)

    assert d == {"node_id": "x.py::Cls", "kind": "class", "source": "class Cls: pass", "version": 3}

    restored = NodeFragment(**d)
    assert restored == frag


def test_lock_entry_round_trip() -> None:
    """LockEntry survives dataclasses.asdict() round-trip."""
    ref = ResourceRef(kind=ResourceKind.FILE, path="f.py")
    entry = LockEntry(resource=ref, mode=LockMode.READ, holder="a1", acquired_at=99.5)
    d = dataclasses.asdict(entry)

    restored = LockEntry(
        resource=ResourceRef(**d["resource"]),
        mode=LockMode(d["mode"]),
        holder=d["holder"],
        acquired_at=d["acquired_at"],
    )
    assert restored == entry


def test_task_bundle_round_trip() -> None:
    """TaskBundle survives dataclasses.asdict() round-trip."""
    bundle = TaskBundle(task_id="t1", description="test", context={"k": "v"})
    d = dataclasses.asdict(bundle)

    assert d == {
        "task_id": "t1",
        "description": "test",
        "target_nodes": [],
        "locks": [],
        "context": {"k": "v"},
    }

    restored = TaskBundle(**d)
    assert restored == bundle


def test_task_result_round_trip() -> None:
    """TaskResult survives dataclasses.asdict() round-trip."""
    result = TaskResult(task_id="t1", success=True, error=None)
    d = dataclasses.asdict(result)

    restored = TaskResult(**d)
    assert restored == result
