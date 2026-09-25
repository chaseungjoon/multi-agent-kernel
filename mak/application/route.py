"""The planner's route as one value: where its requests go and how they are keyed.

The interactive app used to hold the route as four independent fields — model,
backend, base URL, endpoint id — that had to be cleared by hand in every setter
so that exactly one of them described the route. The next setter to forget one
field misrouted the planner, which is how the 0.9.2b bug happened. A
``PlannerRoute`` is complete by construction: every setter builds a new one, and
there is nothing to clear.

It is also the one parser of the ``provider:model[@url]`` planner grammar
(``--planner`` and ``/planner`` both come here) and the one place that turns a
route into ``PlannerConfig`` fields.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Literal

from mak.bootstrap import (
    _BASE_URL_PROVIDERS,
    _PROVIDER_TO_API,
    _PROVIDER_TO_LOCAL,
    _PROVIDER_TO_PLANNER_BACKEND,
    SUPPORTED_PROVIDERS,
    _local_agent,
    _split_spec,
    configured_endpoint_ids,
)
from mak.config import PlannerConfig, normalize_base_url
from mak.core.exceptions import ConfigError

RouteKind = Literal["hosted", "endpoint", "local"]

# Hosted providers a planner can name directly, and the local backends.
HOSTED_PROVIDERS: tuple[str, ...] = ("anthropic", "openai", "gemini")
LOCAL_BACKENDS: tuple[str, ...] = ("ollama", "openai")


@dataclass(frozen=True, slots=True)
class PlannerRoute:
    """Where the planner's requests go. Exactly one kind, always complete.

    - ``hosted``: a built-in provider (``provider``); an ``openai`` route may
      name a gateway ``base_url``, whose credential is then not guessed.
    - ``endpoint``: a configured endpoint (``endpoint_id``), which owns its
      address, transport and credential variable.
    - ``local``: a local runtime (``backend`` ``ollama`` or ``openai`` for any
      OpenAI-compatible server) at ``base_url``; no key.
    """

    kind: RouteKind
    model: str
    provider: str = ""
    endpoint_id: str = ""
    backend: str = ""
    base_url: str = ""

    def __post_init__(self) -> None:
        """Refuse a route that names fields of another kind, or misses its own."""
        if not self.model:
            raise ConfigError("a planner route needs a model")
        if self.kind == "hosted":
            self._check_hosted()
        elif self.kind == "endpoint":
            if not self.endpoint_id or self.provider or self.backend or self.base_url:
                raise ConfigError(
                    "an endpoint planner route names only its endpoint id"
                )
        elif self.kind == "local":
            if self.backend not in LOCAL_BACKENDS or not self.base_url:
                raise ConfigError(
                    "a local planner route needs a backend "
                    f"({' or '.join(LOCAL_BACKENDS)}) and a base URL"
                )
            if self.provider or self.endpoint_id:
                raise ConfigError("a local planner route names no provider")
        else:
            raise ConfigError(f"unknown planner route kind {self.kind!r}")

    def _check_hosted(self) -> None:
        if self.provider not in HOSTED_PROVIDERS:
            raise ConfigError(
                f"unknown hosted planner provider {self.provider!r}; "
                f"expected one of {', '.join(HOSTED_PROVIDERS)}"
            )
        if self.endpoint_id or self.backend:
            raise ConfigError("a hosted planner route names only its provider")
        if self.base_url and self.provider not in _BASE_URL_PROVIDERS:
            raise ConfigError(
                f"provider {self.provider!r} does not take an '@<base_url>'"
            )

    # ── Constructors ────────────────────────────────────────────────────────

    @classmethod
    def hosted(cls, provider: str, model: str, base_url: str = "") -> PlannerRoute:
        """Return a route to a built-in hosted provider (``google`` = gemini)."""
        name = _PROVIDER_TO_PLANNER_BACKEND.get(provider.lower(), provider.lower())
        return cls(kind="hosted", model=model, provider=name, base_url=base_url)

    @classmethod
    def endpoint(cls, endpoint_id: str, model: str) -> PlannerRoute:
        """Return a route through a configured endpoint."""
        return cls(kind="endpoint", model=model, endpoint_id=endpoint_id)

    @classmethod
    def local(cls, backend: str, model: str, base_url: str) -> PlannerRoute:
        """Return a route to a local runtime at ``base_url``."""
        return cls(kind="local", model=model, backend=backend, base_url=base_url)

    @classmethod
    def from_spec(
        cls,
        spec: str,
        *,
        endpoint_ids: Iterable[str] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> PlannerRoute:
        """Parse ``provider:model[@base_url]``, the grammar ``--models`` uses.

        A configured endpoint id is tried first, then the built-in hosted
        providers, then the local ones. The model is required — a planner has
        no per-provider default, and the provider is named precisely so that
        one model offered by two services cannot be routed to the wrong one.

        ``endpoint_ids`` defaults to every configured endpoint; ``env`` (for a
        local spec without a URL) to the process environment.
        """
        provider, model, url = _split_spec(spec)
        if not model:
            raise ConfigError(
                f"--planner {spec!r} names no model; write provider:model — "
                f"e.g. anthropic:claude-opus-5"
            )
        endpoints = (
            frozenset(endpoint_ids)
            if endpoint_ids is not None
            else configured_endpoint_ids()
        )
        if provider in endpoints:
            if url:
                raise ConfigError(
                    f"--planner {spec!r} names endpoint {provider!r} and also an "
                    "'@<base_url>'; the endpoint already has an address"
                )
            return cls.endpoint(provider, model)
        if provider in _PROVIDER_TO_LOCAL:
            local = _local_agent(provider, model, url, spec, env)
            backend = "ollama" if provider == "ollama" else "openai"
            return cls.local(backend, model, local.base_url or "")
        if provider in _PROVIDER_TO_API:
            if url and provider not in _BASE_URL_PROVIDERS:
                raise ConfigError(
                    f"provider {provider!r} does not take an '@<base_url>'; only "
                    f"{', '.join(sorted(_BASE_URL_PROVIDERS))} do"
                )
            base_url = (
                normalize_base_url(url, where=f"--planner {spec!r}") if url else ""
            )
            return cls.hosted(provider, model, base_url)
        known = ", ".join(sorted({*SUPPORTED_PROVIDERS, *endpoints}))
        raise ConfigError(
            f"--planner: unknown endpoint or provider {provider!r}; MAK knows "
            f"{known} — write provider:model[@base_url]"
        )

    # ── Rendering and application ───────────────────────────────────────────

    def prefix(self) -> str:
        """Return the spec's provider position: provider, endpoint, or runtime."""
        if self.kind == "endpoint":
            return self.endpoint_id
        if self.kind == "local":
            return "ollama" if self.backend == "ollama" else "local"
        return self.provider

    def spec(self) -> str:
        """Render ``provider:model[@url]`` — what :meth:`from_spec` parses back."""
        suffix = f"@{self.base_url}" if self.base_url else ""
        return f"{self.prefix()}:{self.model}{suffix}"

    def apply(self, planner: PlannerConfig) -> PlannerConfig:
        """Return ``planner`` routed here, with every route field rewritten.

        Rewritten, not merged: a stale ``base_url`` or ``endpoint`` beside the
        new route would give the planner two answers to "where does this go".
        A hosted provider reached directly names its conventional key variable;
        a gateway names none, so the real provider key is never forwarded to it.
        """
        cleared = replace(
            planner,
            model=self.model,
            endpoint=None,
            backend=None,
            base_url=None,
            api_key_env=None,
        )
        if self.kind == "endpoint":
            return replace(cleared, endpoint=self.endpoint_id)
        if self.kind == "local":
            return replace(cleared, backend=self.backend, base_url=self.base_url)
        if self.base_url:
            return replace(cleared, backend=self.provider, base_url=self.base_url)
        return replace(
            cleared,
            backend=self.provider,
            api_key_env=_PROVIDER_TO_API[self.provider][1],
        )
