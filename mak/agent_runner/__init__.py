"""Agent runner subsystem: protocol, adapters, and registry."""

from mak.agent_runner.adapters.base_adapter import (
    AgentAdapter,
    SubprocessAgentAdapter,
)
from mak.agent_runner.protocol import (
    PROTOCOL_VERSION,
    decode_task_bundle,
    decode_task_result,
    encode_task_bundle,
    encode_task_result,
)
from mak.agent_runner.registry import AdapterRegistry

__all__ = [
    "PROTOCOL_VERSION",
    "AdapterRegistry",
    "AgentAdapter",
    "SubprocessAgentAdapter",
    "decode_task_bundle",
    "decode_task_result",
    "encode_task_bundle",
    "encode_task_result",
]
