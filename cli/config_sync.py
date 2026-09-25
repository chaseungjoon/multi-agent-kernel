"""Show, and run with, what the session's config file says until the user chooses.

The app's planner and roster used to start from the catalog's recommendation
for the first provider with a key, and every run applied them over the config
file — so a project's ``planner:`` and ``agents:`` were never used by the app,
only by ``mak run``. Now the state mirrors the config: its planner route is read
from the ``planner:`` section (and passed to a run only once the user pins one),
and an empty roster selection means the config's ``agents:`` verbatim, with
every per-agent setting intact.

Called at startup and whenever the config in effect can change: ``/work-dir``,
``/config``, and creating a project config.
"""
from __future__ import annotations

from cli.core.models import default_planner_route
from cli.core.state import CliState
from mak.application.route import PlannerRoute
from mak.config import AgentConfig, load_config
from mak.core.exceptions import ConfigError

# Adapter type -> the provider position of a ``--models`` spec.
_TYPE_TO_PROVIDER: dict[str, str] = {
    "anthropic_api": "anthropic",
    "openai_api": "openai",
    "gemini_api": "gemini",
    "ollama_api": "ollama",
    "local_api": "local",
}


def agent_label(agent: AgentConfig) -> str:
    """Render a configured agent the way ``/models`` writes one.

    An agent with no model shows its provider alone (the adapter's default); a
    CLI agent shows its type.
    """
    prefix = agent.endpoint or _TYPE_TO_PROVIDER.get(agent.type, agent.type)
    label = f"{prefix}:{agent.model}" if agent.model else prefix
    if agent.base_url and not agent.endpoint:
        label += f"@{agent.base_url}"
    return label


def sync_with_config(state: CliState) -> None:
    """Refresh ``state``'s view of its config file's planner and roster.

    A planner the user pinned this session is kept. Otherwise the planner is
    the config's; if the config names none this value can hold, the hosted
    default for the session's keys is pinned, so a run always has a planner.
    An unreadable config leaves the state as it is — the run reports it.
    """
    try:
        config = load_config(state.config_file())
    except ConfigError:
        return
    state.config_roster = [agent_label(agent) for agent in config.agents]
    if state.planner_pinned:
        return
    route = PlannerRoute.from_planner_config(config.planner)
    if route is None:
        state.pin_planner(default_planner_route(state.models(), state.api_keys))
        return
    state.planner = route
