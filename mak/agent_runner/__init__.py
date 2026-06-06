"""Agent runner subsystem: protocol, adapters, registry, and runner."""

from mak.agent_runner.adapters.anthropic_api_adapter import AnthropicApiAdapter
from mak.agent_runner.adapters.base_adapter import (
    AgentAdapter,
    SubprocessAgentAdapter,
)
from mak.agent_runner.adapters.openai_api_adapter import OpenAiApiAdapter
from mak.agent_runner.protocol import (
    PROTOCOL_VERSION,
    decode_task_bundle,
    decode_task_result,
    encode_task_bundle,
    encode_task_result,
)
from mak.agent_runner.registry import AdapterRegistry
from mak.agent_runner.runner import AgentRunner, ApiAdapter

__all__ = [
    "PROTOCOL_VERSION",
    "AdapterRegistry",
    "AgentAdapter",
    "AgentRunner",
    "AnthropicApiAdapter",
    "ApiAdapter",
    "OpenAiApiAdapter",
    "SubprocessAgentAdapter",
    "decode_task_bundle",
    "decode_task_result",
    "encode_task_bundle",
    "encode_task_result",
]
