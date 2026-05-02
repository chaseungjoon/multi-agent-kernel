"""Agent runner subsystem: protocol, adapters, and registry."""

from mak.agent_runner.adapters.base_adapter import AgentAdapter
from mak.agent_runner.protocol import (
    PROTOCOL_VERSION,
    decode_task_bundle,
    decode_task_result,
    encode_task_bundle,
    encode_task_result,
)
from mak.agent_runner.registry import (
    clear_registry,
    get_adapter,
    list_adapters,
    register_adapter,
)

__all__ = [
    "PROTOCOL_VERSION",
    "AgentAdapter",
    "clear_registry",
    "decode_task_bundle",
    "decode_task_result",
    "encode_task_bundle",
    "encode_task_result",
    "get_adapter",
    "list_adapters",
    "register_adapter",
]
