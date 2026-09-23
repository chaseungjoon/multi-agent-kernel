"""One command that runs the whole study end-to-end.

    ./run.sh mining.run_study                 # the whole corpus
    ./run.sh mining.run_study django/django   # one repository
    ./run.sh mining.run_study --from pairs    # resume at a stage

Every stage is resumable and skips work already in the cache, so a re-run over
cached data completes in well under a minute and reproduces the same figures.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

from mining import (
    analysis,
    audit,
    fetch_prs,
    fetch_refs,
    filters,
    hunks_to_nodes,
    pair_analysis,
    plots,
    profile_export,
    release,
    semantic_probe,
    survivorship,
    window_analysis,
)
from mining.cli import chosen, repo_parser
from mining.config import RepoSpec, StudyConfig, load_config
from mining.github_api import GitHubClient


@dataclass(frozen=True, slots=True)
class Stage:
    """One named, per-repository step of the pipeline.

    ``run`` takes a ``force`` flag: stages whose output is already cached skip
    themselves unless it is set, which is what makes a re-run over cached data
    complete in seconds.
    """

    name: str
    run: Callable[[StudyConfig, RepoSpec, bool], object]


def _fetch_prs(config: StudyConfig, spec: RepoSpec, force: bool) -> None:
    """Fetch PR metadata; the fetcher is resumable on its own."""
    fetch_prs.fetch_repo(config, spec, GitHubClient(), force=force)


def _survivorship(config: StudyConfig, spec: RepoSpec, force: bool) -> None:
    """Compare merged against abandoned changes, unless already recorded."""
    if survivorship.is_cached(config, spec) and not force:
        print("  cached, skipping", flush=True)
        return
    survivorship.analyze(config, spec)


def _audit(config: StudyConfig, spec: RepoSpec, force: bool) -> None:
    """Run every validation and write the manual-audit sample."""
    if audit.already_audited(config, spec) and not force:
        print("  cached, skipping", flush=True)
        return
    audit.audit_mapper(config, spec)
    audit.audit_merge_verdicts(config, spec)
    audit.audit_naive_bias(config, spec)
    audit.write_manual_sample(config, spec)


STAGES: tuple[Stage, ...] = (
    Stage("prs", _fetch_prs),
    Stage("refs", lambda config, spec, _force: fetch_refs.fetch_heads(config, spec)),
    Stage(
        "map", lambda config, spec, _force: hunks_to_nodes.build_changes(config, spec)
    ),
    Stage("filter", lambda config, spec, _force: filters.classify_repo(config, spec)),
    Stage(
        "pairs",
        lambda config, spec, force: pair_analysis.analyze(config, spec, force=force),
    ),
    Stage(
        "windows",
        lambda config, spec, force: window_analysis.analyze(config, spec, force=force),
    ),
    Stage(
        "semantic",
        lambda config, spec, force: semantic_probe.analyze(config, spec, force=force),
    ),
    Stage("survivorship", _survivorship),
    Stage("audit", _audit),
    Stage(
        "profile", lambda config, spec, _force: profile_export.export(config, spec)
    ),
    Stage("release", lambda config, spec, _force: release.export(config, spec)),
)

STAGE_NAMES = tuple(stage.name for stage in STAGES)


def run(
    config: StudyConfig,
    specs: list[RepoSpec],
    stages: tuple[Stage, ...],
    *,
    force: bool = False,
) -> None:
    """Run the selected stages for every repository, then the global reports."""
    for spec in specs:
        print(f"\n=== {spec.slug} ===", flush=True)
        for stage in stages:
            started = time.monotonic()
            print(f"[{stage.name}]", flush=True)
            stage.run(config, spec, force)
            print(f"  done in {time.monotonic() - started:.1f}s", flush=True)

    print("\n=== reports ===", flush=True)
    # Global artifacts must remain global even when only one repository's
    # stages were selected. ``analysis.collect`` already skips corpus members
    # that do not have a profile yet.
    path = analysis.run(config, list(config.repos))
    print(f"  wrote {path}", flush=True)
    for figure in plots.render(config):
        print(f"  wrote {figure}", flush=True)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m mining.run_study [owner/repo ...]``."""
    parser = repo_parser(__doc__)
    parser.add_argument(
        "--from", dest="start", choices=STAGE_NAMES, default=STAGE_NAMES[0],
        help="first stage to run",
    )
    parser.add_argument(
        "--to", dest="end", choices=STAGE_NAMES, default=STAGE_NAMES[-1],
        help="last stage to run",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="recompute stages whose rows are already cached",
    )
    args = parser.parse_args(argv)

    config = load_config()
    specs = chosen(config, args.repos)
    first, last = STAGE_NAMES.index(args.start), STAGE_NAMES.index(args.end)
    if first > last:
        parser.error(f"--from {args.start} comes after --to {args.end}")
    run(config, specs, STAGES[first : last + 1], force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
