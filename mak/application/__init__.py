"""The application API: the one way either front end turns settings into a session.

``mak run`` and the interactive app used to each resolve planner routes, planner
keys and config overrides for themselves, so the same settings could behave
differently depending on which one launched the run. Both now describe what they
want as a :class:`RunRequest` and go through the same four steps::

    config = build_config(request, env=env)
    key = resolve_planner_key(config, env)          # inside build_session
    session = build_session(config, env=env, ...)

``env`` is always an explicit mapping. Nothing here writes ``os.environ``.
"""

from __future__ import annotations

from mak.application.config import build_config
from mak.application.env import load_env_file, read_env_files
from mak.application.keys import planner_endpoint, resolve_planner_key
from mak.application.request import RunRequest
from mak.application.route import PlannerRoute
from mak.application.session import build_session

__all__ = [
    "PlannerRoute",
    "RunRequest",
    "build_config",
    "build_session",
    "load_env_file",
    "planner_endpoint",
    "read_env_files",
    "resolve_planner_key",
]
