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
    planner_backend: str = ""     # "" = infer from the model id
    planner_base_url: str = ""

    def uses_local_agents(self) -> bool:
        """Whether this session's agents run on a local runtime."""
        return self.mode in (MODE_LOCAL, MODE_HYBRID)

    def has_local_runtime(self) -> bool:
        """Whether a local runtime has actually been configured (not just named)."""
        return bool(self.local_base_url)

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
