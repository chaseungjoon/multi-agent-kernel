"""Endpoint subsystem: what a model is reached *through*.

``mak.models`` answers "which models exist". This package answers "over which
URL, with which credential, speaking which protocol, with which capabilities" —
and keeps that separate from "which adapter class to construct" and "which id
work is routed under", three questions ``AgentConfig.type`` used to answer at
once.

Layout:

* ``types`` — the leaf dataclasses and enums (no I/O, no config import);
* ``profiles`` — the built-in provider table and the reserved-id rule;
* ``resolution`` — profile + endpoint + agent precedence, producing immutable
  resolved records;
* ``store`` — the per-user endpoint metadata file (never credential values).
"""

from mak.endpoints.profiles import (
    BUILTIN_PROFILES,
    RESERVED_ENDPOINT_IDS,
    EndpointProfile,
    adapter_type_for,
    is_reserved,
    profile_for,
    profile_ids,
)
from mak.endpoints.types import (
    ENV_NAME_RE,
    FORBIDDEN_HEADERS,
    ID_RE,
    EndpointConfig,
    EndpointHeaderConfig,
    HealthPolicy,
    Location,
    ModelDiscovery,
    StructuredOutput,
    TokenParameter,
    Transport,
    validate_agent_id,
    validate_endpoint_id,
    validate_env_name,
)

__all__ = [
    "BUILTIN_PROFILES",
    "ENV_NAME_RE",
    "FORBIDDEN_HEADERS",
    "ID_RE",
    "RESERVED_ENDPOINT_IDS",
    "EndpointConfig",
    "EndpointHeaderConfig",
    "EndpointProfile",
    "HealthPolicy",
    "Location",
    "ModelDiscovery",
    "StructuredOutput",
    "TokenParameter",
    "Transport",
    "adapter_type_for",
    "is_reserved",
    "profile_for",
    "profile_ids",
    "validate_agent_id",
    "validate_endpoint_id",
    "validate_env_name",
]
