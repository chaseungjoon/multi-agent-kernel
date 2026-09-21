"""Endpoint domain types: the four identities the old schema conflated.

Before Wave 22, ``AgentConfig.type`` meant three things at once — which adapter
class to construct, which wire protocol to speak, and which key to route work
under. That is why two OpenAI-compatible endpoints could not coexist: they
shared a type, so the second silently overwrote the first in the registry.

This module separates them:

1. **transport** — the wire protocol (:class:`Transport`);
2. **provider profile** — friendly defaults for a known service
   (``mak.endpoints.profiles``);
3. **endpoint** — one configured URL + credential *reference* + capabilities
   (:class:`EndpointConfig`);
4. **agent id** — one schedulable model/endpoint pairing, which is what the
   registry, scheduler, planner and logs key on.

This is a **leaf module**: it imports nothing from ``mak.config``,
``mak.bootstrap`` or any adapter, so the config parser, the CLI and the model
catalog can all depend on it without an import cycle. It performs no I/O.

**Capability fields are tri-state.** ``None`` means *unset — let the layer below
decide*; an explicit ``none`` means *this service does not do that, do not try*.
Collapsing the two would make a profile default impossible to override
downward, so every resolution path preserves the distinction.

**Secrets are never values here.** ``api_key_env`` and
``EndpointHeaderConfig.value_env`` hold variable *names*. A raw key never
enters an endpoint object, which is what lets endpoint metadata be written to
disk, printed by ``/endpoint show`` and pasted into a shared ``mak.yaml``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from mak.core.exceptions import ConfigError


class Transport(StrEnum):
    """The wire protocol an endpoint speaks.

    "Universal" in Wave 22 means universal *inside the OpenAI Chat Completions
    family*. Anthropic Messages, Gemini ``generateContent`` and Ollama's native
    context controls keep dedicated transports rather than being forced through
    the OpenAI SDK, because each carries behaviour the compatible layer drops.
    """

    OPENAI_CHAT = "openai_chat"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    OLLAMA_NATIVE = "ollama_native"
    CLI = "cli"


class Location(StrEnum):
    """Where an endpoint's traffic actually goes.

    This drives every privacy and cost statement MAK makes. It is stored
    explicitly rather than inferred, because the presence of a ``base_url`` has
    never meant "local" — NVIDIA, OpenRouter, DeepSeek and Z.ai all have one and
    are hosted services billing a real account.
    """

    HOSTED = "hosted"
    LOCAL = "local"
    PRIVATE = "private"


class ModelDiscovery(StrEnum):
    """How the model list for an endpoint is obtained.

    ``MANUAL`` never makes a network call — it is the path for a service with no
    ``/models`` route, where the user types an exact model id.
    """

    AUTO = "auto"
    MODELS = "models"
    MANUAL = "manual"


class HealthPolicy(StrEnum):
    """What ``/endpoint test`` and the startup preflight are allowed to do.

    ``CHAT`` costs money, so it runs only after the user has explicitly accepted
    that. ``NONE`` reports "not probed" — never "healthy", which would be a
    claim MAK has not earned.
    """

    MODELS = "models"
    CHAT = "chat"
    NONE = "none"


class StructuredOutput(StrEnum):
    """How the reply's shape is constrained.

    ``AUTO`` starts at the endpoint's preferred rung and descends
    ``json_schema → json_object → none`` on a verified format rejection. The
    named modes are explicit statements and do not roam.
    """

    AUTO = "auto"
    JSON_SCHEMA = "json_schema"
    JSON_OBJECT = "json_object"
    NONE = "none"


class ProviderRouting(StrEnum):
    """Whether this endpoint understands a provider-routing request extension.

    OpenRouter is a *router*: one model id can be served by a dozen upstream
    providers whose capabilities differ, and it documents
    ``provider.require_parameters`` so a client can say "only route to a
    provider that honors the parameters I sent". No other compatible service
    has that concept, and sending the ``provider`` object to one is at best
    ignored and at worst a 400.

    This is stated by a **profile**, never inferred from a hostname. A user can
    proxy OpenRouter, rename the endpoint, or point a custom endpoint at the
    same domain, and in every one of those cases a URL check gives the wrong
    answer about what the request body may contain.

    ``NONE`` is the default for every endpoint, so a new or custom endpoint
    receives only standard OpenAI Chat Completions fields.
    """

    NONE = "none"
    OPENROUTER = "openrouter"


class TokenParameter(StrEnum):
    """Which output-cap field name this endpoint accepts.

    Cloud OpenAI took ``max_tokens`` away in favour of
    ``max_completion_tokens``; most compatible layers implement only the older
    name, where the newer one either 400s or is silently ignored — leaving the
    cap not existing at all. ``AUTO`` resolves per transport and profile;
    ``NONE`` sends no cap and inherits the model's own maximum.
    """

    AUTO = "auto"
    MAX_TOKENS = "max_tokens"
    MAX_COMPLETION_TOKENS = "max_completion_tokens"
    NONE = "none"


# Endpoint and agent ids are slugs, not free text: they appear in CLI specs
# (``nvidia:meta/llama-3.3-70b-instruct``), in YAML, in log lines and in git
# commit trailers. ``:`` and ``@`` are excluded because the spec grammar splits
# on them, and uppercase is excluded so two ids cannot differ only by case.
ID_PATTERN = r"[a-z][a-z0-9_-]{0,63}"
ID_RE = re.compile(rf"^{ID_PATTERN}$")

# POSIX environment-variable names. Validated before anything is written to a
# ``.env`` file, because an arbitrary string there produces a file that the
# shell cannot source and MAK cannot read back.
ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

# Headers MAK sets itself. Letting a config override one is not a customization
# but a credential-routing bug: ``Authorization`` would send a different key
# than the endpoint claims, and ``Host`` would send it to a different server.
FORBIDDEN_HEADERS: frozenset[str] = frozenset(
    {"authorization", "content-type", "host", "user-agent"}
)


def _validate_slug(value: str, *, kind: str) -> str:
    """Return ``value`` normalized to a lowercase slug, or raise ``ConfigError``."""
    text = value.strip().lower()
    if not ID_RE.match(text):
        raise ConfigError(
            f"{kind} {value!r} is not a valid id; use lowercase letters, digits, "
            "'-' and '_', starting with a letter (max 64 characters)"
        )
    return text


def validate_endpoint_id(value: str) -> str:
    """Return a normalized endpoint id, raising ``ConfigError`` on a bad slug."""
    return _validate_slug(value, kind="endpoint id")


def validate_agent_id(value: str) -> str:
    """Return a normalized agent id, raising ``ConfigError`` on a bad slug."""
    return _validate_slug(value, kind="agent id")


def validate_env_name(value: str, *, where: str) -> str:
    """Return a validated environment-variable name for ``where``.

    Checked at config load and before any ``.env`` write, so a typo surfaces
    where it can be fixed rather than as a missing key at dispatch.
    """
    text = value.strip()
    if not ENV_NAME_RE.match(text):
        raise ConfigError(
            f"{where} must be an environment variable NAME like 'MY_SERVICE_API_KEY' "
            f"(A-Z, digits and '_'), got {value!r}. MAK never stores the key "
            "itself — only the name of the variable holding it."
        )
    return text


@dataclass(frozen=True, slots=True)
class EndpointHeaderConfig:
    """One extra request header, carrying either a literal or an env-var name.

    Exactly one value source. A literal is for public metadata — OpenRouter's
    ``HTTP-Referer`` and ``X-Title`` attribution headers are the motivating
    case. Anything secret must use ``value_env``, so the secret stays in the
    environment and the endpoint metadata stays safe to write to disk and paste
    into a shared config. Status output and logs show header *names* only.
    """

    name: str
    value: str | None = None
    value_env: str | None = None

    def __post_init__(self) -> None:
        """Validate the header name and enforce exactly one value source."""
        name = self.name.strip()
        if not name:
            raise ConfigError("a header entry must have a 'name'")
        if name.lower() in FORBIDDEN_HEADERS:
            raise ConfigError(
                f"header {name!r} is set by MAK and cannot be overridden; "
                f"MAK owns {', '.join(sorted(FORBIDDEN_HEADERS))}"
            )
        if (self.value is None) == (self.value_env is None):
            raise ConfigError(
                f"header {name!r} needs exactly one of 'value' (a public literal) "
                "or 'value_env' (the name of the variable holding a secret)"
            )
        if self.value_env is not None:
            validate_env_name(self.value_env, where=f"header {name!r} 'value_env'")
        object.__setattr__(self, "name", name)

    @property
    def is_secret(self) -> bool:
        """Whether this header's value comes from the environment."""
        return self.value_env is not None


@dataclass(frozen=True, slots=True)
class EndpointConfig:
    """One configured endpoint: a URL, a credential reference, and capabilities.

    Every capability field is tri-state (see the module docstring): ``None``
    means unset and defers to the profile, then to the transport.

    ``display_name`` is what the user sees in menus, ``/status`` and the planner
    prompt. It never contains the URL, so a screenshot or a shared log cannot
    leak an internal hostname.
    """

    id: str
    transport: Transport
    base_url: str | None = None
    api_key_env: str | None = None
    location: Location = Location.HOSTED
    profile: str | None = None
    display_name: str = ""
    model_discovery: ModelDiscovery | None = None
    health_check: HealthPolicy | None = None
    structured_output: StructuredOutput | None = None
    token_parameter: TokenParameter | None = None
    provider_routing: ProviderRouting | None = None
    headers: tuple[EndpointHeaderConfig, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """Normalize the id and validate the credential reference."""
        object.__setattr__(self, "id", validate_endpoint_id(self.id))
        if self.api_key_env is not None:
            object.__setattr__(
                self,
                "api_key_env",
                validate_env_name(
                    self.api_key_env, where=f"endpoint '{self.id}' 'api_key_env'"
                ),
            )
        seen: set[str] = set()
        for header in self.headers:
            lowered = header.name.lower()
            if lowered in seen:
                raise ConfigError(
                    f"endpoint '{self.id}' sets header {header.name!r} twice"
                )
            seen.add(lowered)
        if not self.display_name:
            object.__setattr__(self, "display_name", self.id)

    @property
    def is_hosted(self) -> bool:
        """Whether traffic to this endpoint leaves the user's own network."""
        return self.location is Location.HOSTED

    def secret_env_names(self) -> tuple[str, ...]:
        """Return every environment variable this endpoint needs, in order.

        Consumed by ``/apikey`` and by the key store, which must know about
        per-endpoint credentials without hard-coding a provider list.
        """
        names: list[str] = []
        if self.api_key_env:
            names.append(self.api_key_env)
        names.extend(h.value_env for h in self.headers if h.value_env)
        return tuple(names)

    def sanitized_base_url(self) -> str:
        """Return the base URL with any query string removed, for display.

        Some gateways carry a token in the query string. Status lines, logs and
        error messages go through here so that token is never rendered.
        """
        if not self.base_url:
            return ""
        return self.base_url.split("?", 1)[0]
