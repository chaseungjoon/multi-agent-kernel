"""TaskBundle and TaskResult JSON serialization for the agent protocol."""

from __future__ import annotations

import json
from dataclasses import asdict

from mak.core.types import NodeId, TaskBundle, TaskResult

PROTOCOL_VERSION = "1.0"


def encode_task_bundle(bundle: TaskBundle) -> str:
    """Serialize a TaskBundle to a newline-delimited JSON string."""
    data = asdict(bundle)
    data["protocol_version"] = PROTOCOL_VERSION
    return json.dumps(data) + "\n"


def decode_task_bundle(raw: str) -> TaskBundle:
    """Deserialize a JSON string into a TaskBundle."""
    data = json.loads(raw.strip())
    version = data.pop("protocol_version", None)
    if version is not None and version != PROTOCOL_VERSION:
        raise ValueError(
            f"unsupported protocol version: {version} (expected {PROTOCOL_VERSION})"
        )
    return TaskBundle(
        task_id=data["task_id"],
        description=data["description"],
        target_nodes=[NodeId(n) for n in data.get("target_nodes", [])],
        locks=data.get("locks", []),
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
    version = data.pop("protocol_version", None)
    if version is not None and version != PROTOCOL_VERSION:
        raise ValueError(
            f"unsupported protocol version: {version} (expected {PROTOCOL_VERSION})"
        )
    return TaskResult(
        task_id=data["task_id"],
        success=data["success"],
        modified_nodes=[NodeId(n) for n in data.get("modified_nodes", [])],
        error=data.get("error"),
    )
