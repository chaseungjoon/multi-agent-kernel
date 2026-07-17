"""Shared CLI session state."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CliState:
    api_keys: dict[str, str] = field(default_factory=dict)
    selected_models: list[str] = field(default_factory=list)
    max_agents: int = 3
    work_dir: str = "."
    planner_model: str = "claude-sonnet-5"
    # Empty string = auto-discover (./mak.yaml → ~/.config/mak/config.yaml →
    # the packaged default); a non-empty value is an explicit file from /config.
    config_path: str = ""
    no_review: bool = False

    def config_display(self) -> str:
        """Return the config path for status displays ('auto' = discovery)."""
        return self.config_path or "auto"

    def models_display(self) -> str:
        return "  ".join(self.selected_models) if self.selected_models else "none"

    def work_dir_display(self) -> str:
        p = Path(self.work_dir).resolve()
        try:
            return "~/" + str(p.relative_to(Path.home()))
        except ValueError:
            return str(p)
