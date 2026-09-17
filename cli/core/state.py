"""Shared CLI session state."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

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
    """

    api_keys: dict[str, str] = field(default_factory=dict)
    selected_models: list[str] = field(default_factory=list)
    max_agents: int = 3
    work_dir: str = "."
    planner_model: str = "claude-opus-5"
    # Empty string = auto-discover (./mak.yaml → ~/.config/mak/config.yaml →
    # the packaged default); a non-empty value is an explicit file from /config.
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
    planner_backend: str = ""     # "" = infer from the model id
    planner_base_url: str = ""

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
        """Return the selected agent models for status displays."""
        return "  ".join(self.selected_models) if self.selected_models else "none"

    def work_dir_display(self) -> str:
        """Return the working directory for status displays, abbreviating $HOME."""
        p = Path(self.work_dir).resolve()
        try:
            return "~/" + str(p.relative_to(Path.home()))
        except ValueError:
            return str(p)
