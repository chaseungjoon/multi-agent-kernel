"""Turn a :class:`RunRequest` into the validated ``MakConfig`` a run uses."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace

from mak.application.request import RunRequest
from mak.application.route import PlannerRoute
from mak.bootstrap import agents_from_specs, configured_endpoint_ids, validate_config
from mak.config import (
    MakConfig,
    anchor_mak_dir,
    discover_config_path,
    load_config,
)
from mak.core.exceptions import ConfigError


def build_config(
    request: RunRequest,
    *,
    env: Mapping[str, str] | None = None,
    anchor: bool = True,
) -> MakConfig:
    """Load, override, validate and anchor the config ``request`` describes.

    Pure apart from reading the config file and the endpoint store: the same
    request and ``env`` always produce the same config, whichever front end
    built the request. The config file is ``request.config_path``, or the one
    :func:`~mak.config.discover_config_path` finds from ``request.work_dir``.

    ``anchor=False`` returns the config before ``session.mak_dir`` is anchored
    in the work dir, for a caller that must first look for a store an older MAK
    left beside the shell (:func:`~mak.config.stale_mak_dir`); it then anchors
    with :func:`~mak.config.anchor_mak_dir` itself.
    """
    source = os.environ if env is None else env
    path = request.config_path or discover_config_path(request.work_dir)
    config = load_config(path)
    if request.work_dir is not None:
        config = replace(
            config, session=replace(config.session, work_dir=request.work_dir)
        )
    endpoint_ids = configured_endpoint_ids(config)
    if request.model_specs:
        config = replace(
            config,
            agents=agents_from_specs(
                list(request.model_specs), env=source, endpoint_ids=endpoint_ids
            ),
        )
    if request.planner is not None:
        route = (
            request.planner
            if isinstance(request.planner, PlannerRoute)
            else PlannerRoute.from_spec(
                request.planner, endpoint_ids=endpoint_ids, env=source
            )
        )
        config = replace(config, planner=route.apply(config.planner))
    if request.max_agents is not None:
        if request.max_agents < 1:
            raise ConfigError("--max-agents must be at least 1")
        config = replace(
            config,
            session=replace(
                config.session, max_concurrent_agents=request.max_agents
            ),
        )
    validate_config(config)
    return anchor_mak_dir(config) if anchor else config
