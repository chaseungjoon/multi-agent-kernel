"""What a front end asks for: one run's settings, however they were entered."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from mak.application.route import PlannerRoute


@dataclass(frozen=True, slots=True)
class RunRequest:
    """The settings one run was asked for, before any config is loaded.

    ``mak run`` fills it from argv and the interactive app from ``CliState``;
    :func:`mak.application.build_config` is the only thing that reads it. Every
    field defaults to "use what the config file says".

    ``planner`` is a :class:`PlannerRoute`, or a ``provider:model[@url]`` spec
    that is parsed against the endpoints the *loaded* config declares — a spec
    cannot be parsed before the file that may define its endpoint is read.
    ``api_keys`` is a session-only overlay on the environment, never exported.
    """

    config_path: Path | None = None
    work_dir: str | None = None
    model_specs: tuple[str, ...] = ()
    planner: PlannerRoute | str | None = None
    max_agents: int | None = None
    default_agent: str | None = None
    sandbox: bool = False
    verbose: int = 0
    api_keys: Mapping[str, str] = field(default_factory=dict)
    no_review: bool = False
