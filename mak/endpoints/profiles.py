"""Built-in provider profiles: friendly defaults for known compatible services.

A profile is **data, not a subclass**. NVIDIA, OpenRouter, DeepSeek and Z.ai all
speak OpenAI Chat Completions, so they share one transport and differ only in a
URL, a credential-variable convention, and which capability rungs they support.
Giving each one an adapter class would be four copies of the same file kept in
sync by hand; giving each one a row in this table is the whole difference.

**These rows are external facts.** Base URLs, credential conventions and
capability support are the providers'  to change, not MAK's. They were checked
against official documentation on 2026-09-20 (``docs_url`` on each row is the
page checked). Treat a change here like a code change: re-read the vendor page,
source it, test it, and note it in the changelog. Never download an executable
provider definition, and never accept a remote profile — a profile can inject
headers and choose a URL, which is enough to redirect a credential.

**Model ids are deliberately absent.** A model catalog is live state belonging
to the endpoint's ``/models`` response or to explicit user input. Freezing model
ids into a profile ships facts that rot between releases.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mak.endpoints.types import (
    EndpointConfig,
    EndpointHeaderConfig,
    HealthPolicy,
    Location,
    ModelDiscovery,
    StructuredOutput,
    TokenParameter,
    Transport,
)

# The endpoint ids MAK reserves for itself. These are exactly the provider
# prefixes ``--models`` has always accepted, so keeping them in the *same*
# namespace as user endpoints is what lets one rule resolve
# ``--models openai:gpt-5.6-sol`` and ``--models nvidia:meta/llama-3.3-70b``.
# A user endpoint may not take one: two meanings for one prefix is how a spec
# silently reaches a different host than the user intended.
RESERVED_ENDPOINT_IDS: frozenset[str] = frozenset(
    {"anthropic", "openai", "gemini", "google", "local", "ollama"}
)

# Transport -> the adapter type ``mak.bootstrap`` constructs for it. ``type``
# survives Wave 22 purely as this constructor selector; the *routing* key is the
# agent id.
TRANSPORT_ADAPTER_TYPE: dict[Transport, str] = {
    Transport.OPENAI_CHAT: "openai_api",
    Transport.ANTHROPIC: "anthropic_api",
    Transport.GEMINI: "gemini_api",
    Transport.OLLAMA_NATIVE: "ollama_api",
}


@dataclass(frozen=True, slots=True)
class EndpointProfile:
    """Defaults for one known service, and the doc page they were read from.

    A profile pre-fills the wizard and supplies the middle tier of resolution
    (explicit endpoint field > profile default > transport default). It never
    overrides something the user stated.
    """

    id: str
    display_name: str
    transport: Transport
    base_url: str | None
    api_key_env: str | None
    location: Location
    docs_url: str
    model_discovery: ModelDiscovery | None = None
    health_check: HealthPolicy | None = None
    structured_output: StructuredOutput | None = None
    token_parameter: TokenParameter | None = None
    headers: tuple[EndpointHeaderConfig, ...] = field(default_factory=tuple)
    # Shown in the wizard before the URL is chosen. Z.ai's two plans bill
    # different balances, so picking the wrong one is a money question, not a
    # preference.
    note: str = ""

    def to_endpoint(self, endpoint_id: str = "") -> EndpointConfig:
        """Return a concrete endpoint pre-filled from this profile.

        The wizard calls this and then applies the user's answers, so a preset
        *pre-fills* rather than hides its values — the user sees the URL and the
        credential variable they are agreeing to.
        """
        return EndpointConfig(
            id=endpoint_id or self.id,
            transport=self.transport,
            base_url=self.base_url,
            api_key_env=self.api_key_env,
            location=self.location,
            profile=self.id,
            display_name=self.display_name,
            model_discovery=self.model_discovery,
            health_check=self.health_check,
            structured_output=self.structured_output,
            token_parameter=self.token_parameter,
            headers=self.headers,
        )


BUILTIN_PROFILES: tuple[EndpointProfile, ...] = (
    EndpointProfile(
        id="nvidia",
        display_name="NVIDIA Build / hosted NIM",
        transport=Transport.OPENAI_CHAT,
        base_url="https://integrate.api.nvidia.com/v1",
        api_key_env="NVIDIA_API_KEY",
        location=Location.HOSTED,
        docs_url="https://docs.api.nvidia.com/nim/re/reference/llm-apis",
        model_discovery=ModelDiscovery.MODELS,
        health_check=HealthPolicy.MODELS,
        structured_output=StructuredOutput.AUTO,
        token_parameter=TokenParameter.MAX_TOKENS,
    ),
    EndpointProfile(
        id="openrouter",
        display_name="OpenRouter",
        transport=Transport.OPENAI_CHAT,
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        location=Location.HOSTED,
        docs_url="https://openrouter.ai/docs/quickstart",
        model_discovery=ModelDiscovery.MODELS,
        health_check=HealthPolicy.MODELS,
        # Structured-output support is per *model* on OpenRouter, not per
        # account, so the ladder has to discover it rather than assume it.
        structured_output=StructuredOutput.AUTO,
        token_parameter=TokenParameter.MAX_TOKENS,
        note=(
            "OpenRouter routes to many upstream models; structured-output and "
            "usage accuracy vary by model. Attribution headers are optional."
        ),
    ),
    EndpointProfile(
        id="deepseek",
        display_name="DeepSeek",
        transport=Transport.OPENAI_CHAT,
        # Deliberately not '/v1': DeepSeek documents the bare host as the SDK
        # base. MAK never adds or strips a '/v1' segment for exactly this kind
        # of provider.
        base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
        location=Location.HOSTED,
        docs_url="https://api-docs.deepseek.com/api/create-chat-completion/",
        model_discovery=ModelDiscovery.AUTO,
        health_check=HealthPolicy.MODELS,
        structured_output=StructuredOutput.JSON_OBJECT,
        token_parameter=TokenParameter.MAX_TOKENS,
    ),
    EndpointProfile(
        id="zai-general",
        display_name="Z.ai (general / prepaid balance)",
        transport=Transport.OPENAI_CHAT,
        base_url="https://api.z.ai/api/paas/v4",
        api_key_env="ZAI_API_KEY",
        location=Location.HOSTED,
        docs_url="https://zcode.z.ai/en/docs/configuration",
        model_discovery=ModelDiscovery.AUTO,
        health_check=HealthPolicy.MODELS,
        structured_output=StructuredOutput.AUTO,
        token_parameter=TokenParameter.MAX_TOKENS,
        note=(
            "Bills the general/prepaid balance. If you hold a Coding Plan "
            "subscription, this URL bypasses its quota — pick 'zai-coding'."
        ),
    ),
    EndpointProfile(
        id="zai-coding",
        display_name="Z.ai Coding Plan",
        transport=Transport.OPENAI_CHAT,
        base_url="https://api.z.ai/api/coding/paas/v4",
        api_key_env="ZAI_API_KEY",
        location=Location.HOSTED,
        docs_url="https://zcode.z.ai/en/docs/configuration",
        model_discovery=ModelDiscovery.AUTO,
        health_check=HealthPolicy.MODELS,
        structured_output=StructuredOutput.AUTO,
        token_parameter=TokenParameter.MAX_TOKENS,
        note=(
            "Draws on the Coding Plan subscription quota. Using the general "
            "URL instead charges your prepaid balance for the same request."
        ),
    ),
    EndpointProfile(
        id="custom",
        display_name="Custom OpenAI-compatible endpoint",
        transport=Transport.OPENAI_CHAT,
        base_url=None,
        api_key_env=None,
        location=Location.LOCAL,
        docs_url="",
        # Nothing is assumed about an unknown server: discovery, health and
        # response format all negotiate rather than declare.
        model_discovery=ModelDiscovery.AUTO,
        health_check=HealthPolicy.MODELS,
        structured_output=StructuredOutput.AUTO,
        token_parameter=TokenParameter.AUTO,
        note=(
            "vLLM, llama.cpp, LM Studio, LocalAI, or any other service speaking "
            "OpenAI Chat Completions."
        ),
    ),
)

_BY_ID: dict[str, EndpointProfile] = {p.id: p for p in BUILTIN_PROFILES}


def profile_for(profile_id: str) -> EndpointProfile | None:
    """Return the built-in profile with this id, or None if there is none."""
    return _BY_ID.get(profile_id.strip().lower())


def profile_ids() -> tuple[str, ...]:
    """Return every built-in profile id, in presentation order."""
    return tuple(p.id for p in BUILTIN_PROFILES)


def adapter_type_for(transport: Transport) -> str:
    """Return the adapter type that implements ``transport``.

    Raises ``KeyError`` for ``Transport.CLI``, which has no single adapter — the
    CLI wrappers are selected by their own agent type and never by an endpoint.
    """
    return TRANSPORT_ADAPTER_TYPE[transport]


def is_reserved(endpoint_id: str) -> bool:
    """Return whether ``endpoint_id`` is one MAK reserves for legacy providers."""
    return endpoint_id.strip().lower() in RESERVED_ENDPOINT_IDS
