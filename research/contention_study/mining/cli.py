"""Shared command-line scaffolding for the per-repository stages.

Every stage takes the same argument — zero or more ``owner/repo`` slugs,
defaulting to the whole corpus — so it is defined once here rather than
repeated in each module's ``main``.
"""

from __future__ import annotations

import argparse

from mining.config import RepoSpec, StudyConfig

_REPOS_HELP = "owner/repo slugs; default is the whole corpus"


def repo_parser(description: str | None) -> argparse.ArgumentParser:
    """Build the standard parser: an optional list of repository slugs."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("repos", nargs="*", help=_REPOS_HELP)
    return parser


def chosen(config: StudyConfig, slugs: list[str]) -> list[RepoSpec]:
    """Resolve slugs to specs, falling back to the whole corpus when empty."""
    if not slugs:
        return list(config.repos)
    return [config.repo(slug) for slug in slugs]
