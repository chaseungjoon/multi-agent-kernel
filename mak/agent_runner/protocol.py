"""TaskBundle and TaskResult JSON serialization for the agent protocol.

The wire schema is exactly the ``TaskBundle`` / ``TaskResult`` dataclasses (the
single canonical schema). ``decode_task_bundle`` rebuilds nested ``LockEntry`` /
``ResourceRef`` objects rather than leaving raw dicts in a field typed
``list[LockEntry]``.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from mak.core.types import (
    LockEntry,
    LockMode,
    NodeId,
    ResourceKind,
    ResourceRef,
    TaskBundle,
    TaskResult,
)

PROTOCOL_VERSION = "1.0"


def _check_version(data: dict[str, Any]) -> None:
    version = data.pop("protocol_version", None)
    if version is not None and version != PROTOCOL_VERSION:
        raise ValueError(
            f"unsupported protocol version: {version} (expected {PROTOCOL_VERSION})"
        )


def _decode_lock_entry(raw: dict[str, Any]) -> LockEntry:
    resource = raw["resource"]
    return LockEntry(
        resource=ResourceRef(
            kind=ResourceKind(resource["kind"]),
            path=resource["path"],
            symbol=resource.get("symbol"),
        ),
        mode=LockMode(raw["mode"]),
        holder=raw["holder"],
        acquired_at=raw["acquired_at"],
    )


def encode_task_bundle(bundle: TaskBundle) -> str:
    """Serialize a TaskBundle to a newline-delimited JSON string."""
    data = asdict(bundle)
    data["protocol_version"] = PROTOCOL_VERSION
    return json.dumps(data) + "\n"


def decode_task_bundle(raw: str) -> TaskBundle:
    """Deserialize a JSON string into a TaskBundle."""
    data = json.loads(raw.strip())
    _check_version(data)
    return TaskBundle(
        task_id=data["task_id"],
        description=data["description"],
        target_nodes=[NodeId(n) for n in data.get("target_nodes", [])],
        locks=[_decode_lock_entry(e) for e in data.get("locks", [])],
        context=data.get("context", {}),
    )


def encode_task_result(result: TaskResult) -> str:
    """Serialize a TaskResult to a newline-delimited JSON string."""
    data = asdict(result)
    data["protocol_version"] = PROTOCOL_VERSION
    return json.dumps(data) + "\n"


def decode_task_result(raw: str) -> TaskResult:
    """Deserialize a JSON string into a TaskResult."""
    data = json.loads(raw.strip())
    _check_version(data)
    return TaskResult(
        task_id=data["task_id"],
        success=data["success"],
        modified_nodes=[NodeId(n) for n in data.get("modified_nodes", [])],
        error=data.get("error"),
    )
