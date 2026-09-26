"""Shared CLI session state."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from cli.core.local_seams import LocalSeams
from mak.application.route import PlannerRoute
from mak.config import discover_config_path
from mak.models import ModelRegistry

# How a session gets its models. A first-class field rather than a label,
# because it decides which surfaces validate against API keys and which against
# a local runtime — and because ``hybrid`` (cloud planner + local agents) is a
# real configuration, not a halfway house: it is the cost/privacy sweet spot for
# anyone whose local model plans worse than it edits.
MODE_CLOUD = "cloud"
MODE_LOCAL = "local"
MODE_HYBRID = "hybrid"
MODES: tuple[str, ...] = (MODE_CLOUD, MODE_LOCAL, MODE_HYBRID)

_MODE_SUMMARY: dict[str, str] = {
    MODE_CLOUD: "hosted APIs (Anthropic, OpenAI, Google)",
    MODE_LOCAL: "on this machine, private and offline",
    MODE_HYBRID: "cloud planner + local agents",
}


def _default_planner() -> PlannerRoute:
    """Return the planner a new session starts with (see ``cli.core.models``)."""
    return PlannerRoute.hosted("anthropic", "claude-opus-5")


@dataclass
class LocalHost:
    """A model server MAK has connected to, with the models it last reported."""

    url: str
    kind: str = "ollama"          # "ollama" | "openai_compatible"
    models: list[str] = field(default_factory=list)

    def provider(self) -> str:
        """Return the spec prefix for this host's models (``ollama`` / ``local``)."""
        return "ollama" if self.kind == "ollama" else "local"

    def host_display(self) -> str:
        """Return the endpoint without its scheme, for compact menus."""
        return self.url.split("://", 1)[-1]

    def is_this_machine(self) -> bool:
        """Return whether the endpoint names the loopback interface."""
        from urllib.parse import urlparse

        hostname = urlparse(self.url).hostname or ""
        return hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def mode_summary(mode: str) -> str:
    """Return the one-line description of a mode, for menus and /mode."""
    return _MODE_SUMMARY.get(mode, "")


@dataclass
class CliState:
    """Mutable per-session settings the TUI edits through its slash commands.

    ``mode`` decides how the app *fills* ``selected_models`` and which
    validations apply — never how the kernel is configured. The roster the
    runner builds is always ``selected_models``; a cloud entry reads
    ``anthropic:claude-sonnet-5`` and a local one
    ``ollama:qwen2.5-coder:14b@http://localhost:11434``, and
    ``agents_from_specs`` parses both by the same rule.

    The planner is **one** :class:`PlannerRoute`. Its four old facets
    (``planner_model``, ``planner_backend``, ``planner_base_url``,
    ``planner_endpoint_id``) remain as read-only properties for display code;
    a setter assigns a whole new route, so no field can be left stale.

    The state also carries the app's services — the ``/local`` seams and the
    lazily built model registry — because it is the one object the app already
    passes to every handler; neither lives at module level.
    """

    api_keys: dict[str, str] = field(default_factory=dict)
    selected_models: list[str] = field(default_factory=list)
    max_agents: int = 3
    work_dir: str = "."
    planner: PlannerRoute = field(default_factory=_default_planner)
    # False while ``planner`` only mirrors the config file's ``planner:``
    # section; a run then uses that section verbatim. True once the user chose
    # a planner this session, which then overrides the config.
    planner_pinned: bool = False
    # The config file's roster, for display while ``selected_models`` is empty
    # (an empty selection means "the config's agents, as configured").
    config_roster: list[str] = field(default_factory=list)
    # Empty string = auto-discover from the work dir (<work dir>/.mak/config.yaml
    # → ~/.config/mak/config.yaml → the packaged default); a
    # non-empty value is an explicit file from /config.
    config_path: str = ""
    no_review: bool = False
    # ── Local runtime (see MODES above) ──────────────────────────────────────
    mode: str = MODE_CLOUD
    local_kind: str = ""          # "ollama" | "openai_compatible"
    local_base_url: str = ""
    local_models: list[str] = field(default_factory=list)
    # Every host connected to before, remembered across sessions. The active
    # one is the ``local_*`` fields above; its entry here may be stale.
    local_hosts: list[LocalHost] = field(default_factory=list)
    # ── Endpoints ────────────────────────────────────────────────────────────
    # Ids of endpoints this session knows about, in configured order. Held so
    # /status, the toolbar and the completer can name them without re-reading
    # the store on every keystroke.
    endpoint_ids: list[str] = field(default_factory=list)
    # ── Services (held here, never module-level) ─────────────────────────────
    local_seams: LocalSeams = field(
        default_factory=LocalSeams.default, repr=False, compare=False
    )
    # Built on first use by ``models()``; a test may inject its own.
    model_registry: ModelRegistry | None = field(
        default=None, repr=False, compare=False
    )

    def models(self) -> ModelRegistry:
        """Return the model registry, reading the catalog on first use."""
        if self.model_registry is None:
            self.model_registry = ModelRegistry()
        return self.model_registry

    def config_file(self) -> Path:
        """Return the config file this session uses: explicit, or discovered."""
        if self.config_path:
            return Path(self.config_path)
        return discover_config_path(self.work_dir)

    # ── Planner route ────────────────────────────────────────────────────────

    def pin_planner(self, route: PlannerRoute) -> None:
        """Make ``route`` the session's chosen planner, overriding the config."""
        self.planner = route
        self.planner_pinned = True

    def set_cloud_planner(self, provider: str, model: str) -> None:
        """Point the planner at a built-in hosted provider's model.

        The provider is part of the route rather than inferred from the model
        id, because one model can be served by more than one provider
        (``anthropic:`` and an ``openrouter:`` endpoint), and the choice the
        user made is the one that must be routed.
        """
        self.pin_planner(PlannerRoute.hosted(provider, model))

    def set_endpoint_planner(self, endpoint_id: str, model: str) -> None:
        """Point the planner at ``model`` on a configured endpoint."""
        self.pin_planner(PlannerRoute.endpoint(endpoint_id, model))

    def set_local_planner(self, backend: str, model: str, base_url: str) -> None:
        """Point the planner at ``model`` on the local runtime at ``base_url``."""
        self.pin_planner(PlannerRoute.local(backend, model, base_url))

    @property
    def planner_model(self) -> str:
        """The planner's model id (read-only; set a route instead)."""
        return self.planner.model

    @property
    def planner_backend(self) -> str:
        """The planner's provider or local backend; '' for an endpoint."""
        if self.planner.kind == "hosted":
            return self.planner.provider
        return self.planner.backend

    @property
    def planner_base_url(self) -> str:
        """The planner's base URL, '' when the route has none of its own."""
        return self.planner.base_url

    @property
    def planner_endpoint_id(self) -> str:
        """The endpoint the planner routes through, '' when it names none."""
        return self.planner.endpoint_id

    def planner_cloud_provider(self) -> str:
        """Return the built-in provider the planner routes to, or '' if none."""
        route = self.planner
        return route.provider if route.kind == "hosted" and not route.base_url else ""

    def planner_spec(self) -> str:
        """Return the planner as ``provider:model`` — the form ``/planner`` takes.

        A planner on a non-active local host carries its ``@url``, exactly as
        ``/models`` lists such a host's models.
        """
        route = self.planner
        if route.kind == "local" and route.base_url == self.local_base_url:
            return f"{route.prefix()}:{route.model}"
        return route.spec()

    def planner_endpoint_display(self) -> str:
        """Return the planner's route for /status."""
        route = self.planner
        if route.kind == "endpoint":
            return route.endpoint_id
        if route.kind == "local":
            return f"local runtime at {route.base_url}"
        return f"built-in {route.provider}"

    def uses_local_agents(self) -> bool:
        """Whether this session's agents run on a local runtime."""
        return self.mode in (MODE_LOCAL, MODE_HYBRID)

    def has_local_runtime(self) -> bool:
        """Whether a local runtime has actually been configured (not just named)."""
        return bool(self.local_base_url)

    def active_local_host(self) -> LocalHost | None:
        """Return the active runtime as a ``LocalHost`` (None when unset)."""
        if not self.local_base_url:
            return None
        return LocalHost(
            url=self.local_base_url,
            kind=self.local_kind or "ollama",
            models=list(self.local_models),
        )

    def all_local_hosts(self) -> list[LocalHost]:
        """Return every known host, the active one first and current."""
        active = self.active_local_host()
        others = [h for h in self.local_hosts if h.url != self.local_base_url]
        return [active, *others] if active else others

    def local_provider(self) -> str:
        """Return the spec prefix for the configured runtime's models.

        ``ollama`` for the native runtime, ``local`` for an OpenAI-compatible
        one — the same names ``/models`` and ``mak run --models`` accept.
        """
        return "ollama" if self.local_kind == "ollama" else "local"

    def local_host_display(self) -> str:
        """Return the runtime endpoint without its scheme, for compact menus."""
        return self.local_base_url.split("://", 1)[-1]

    def mode_display(self) -> str:
        """Return the mode for the toolbar and /status."""
        return self.mode

    def local_display(self) -> str:
        """Return the configured runtime for /status ('none' when unset)."""
        if not self.local_base_url:
            return "none"
        kind = self.local_kind or "runtime"
        count = len(self.local_models)
        plural = "" if count == 1 else "s"
        return f"{kind} · {self.local_base_url} · {count} model{plural}"

    def config_display(self) -> str:
        """Return the config path for status displays ('auto' = discovery)."""
        return self.config_path or "auto"

    def models_display(self) -> str:
        """Return the agent models a run uses: the selection, else the config's."""
        roster = self.selected_models or self.config_roster
        return "  ".join(roster) if roster else "none"

    def work_dir_display(self) -> str:
        """Return the working directory for status displays, abbreviating $HOME."""
        p = Path(self.work_dir).resolve()
        try:
            return "~/" + str(p.relative_to(Path.home()))
        except ValueError:
            return str(p)
