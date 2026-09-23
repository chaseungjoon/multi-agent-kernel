"""Static configuration for the contention study: repositories, window, limits.

Every downstream module takes a :class:`StudyConfig` (or a :class:`RepoSpec`)
rather than loose strings, so the study corpus and its cost caps are declared in
exactly one place and are recorded verbatim in the run manifest.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from mining.exceptions import ContentionStudyError

_DEFAULT_CACHE = Path.home() / ".cache" / "mak-contention-study"


@dataclass(frozen=True, slots=True)
class RepoSpec:
    """One repository in the study corpus.

    ``main_branch`` is the integration branch that PRs merge into; it is the
    reference every PR's fork point is computed against.
    """

    owner: str
    name: str
    main_branch: str
    note: str = ""

    @property
    def slug(self) -> str:
        """``owner/name``, the form the GitHub REST API expects."""
        return f"{self.owner}/{self.name}"

    @property
    def key(self) -> str:
        """Filesystem-safe identifier used for cache and data directories."""
        return f"{self.owner}__{self.name}"


@dataclass(frozen=True, slots=True)
class StudyConfig:
    """The whole study's parameters: corpus, window, sampling caps and paths."""

    repos: tuple[RepoSpec, ...]
    since: str
    until: str
    max_prs_per_repo: int = 2000
    max_pairs_per_repo: int = 25000
    max_windows_per_k: int = 4000
    window_sizes: tuple[int, ...] = (2, 4, 8, 16, 32, 64)
    semantic_probe_sample: int = 400
    audit_sample_per_cell: int = 50
    random_seed: int = 20260922
    workers: int = 8
    cache_root: Path = field(default=_DEFAULT_CACHE)
    data_root: Path = field(default=Path(__file__).resolve().parent.parent / "data")
    plots_root: Path = field(
        default=Path(__file__).resolve().parent.parent / "plots"
    )

    def repo(self, slug: str) -> RepoSpec:
        """Look a repository up by ``owner/name``.

        Raises :class:`ContentionStudyError` when the slug is not in the corpus,
        rather than silently returning a default.
        """
        for spec in self.repos:
            if spec.slug == slug:
                return spec
        known = ", ".join(s.slug for s in self.repos)
        raise ContentionStudyError(f"unknown repo {slug!r}; corpus is: {known}")

    def clone_dir(self, spec: RepoSpec) -> Path:
        """Path of the bare clone for ``spec`` (outside the project tree)."""
        return self.cache_root / "repos" / f"{spec.key}.git"

    def repo_data_dir(self, spec: RepoSpec) -> Path:
        """Directory holding the derived dataset for ``spec``."""
        return self.data_root / spec.key


# The study corpus. Chosen from a throughput/Python-share reconnaissance run on
# 2026-09-22 (counts recorded in CUR_WAVE.md), covering four contention regimes:
# registry-heavy plugin monorepos, a model-zoo monorepo, numeric libraries, and a
# mature framework with a slow, heavily reviewed merge cadence.
CORPUS: tuple[RepoSpec, ...] = (
    RepoSpec("home-assistant", "core", "dev", "registry-heavy integration monorepo"),
    RepoSpec("apache", "airflow", "main", "provider-package monorepo"),
    RepoSpec("huggingface", "transformers", "main", "model-zoo monorepo"),
    RepoSpec("pandas-dev", "pandas", "main", "numeric library, mixed C/Python"),
    RepoSpec("scikit-learn", "scikit-learn", "main", "numeric library, estimator API"),
    RepoSpec("django", "django", "main", "mature framework, low PR cadence"),
)

DEFAULT_CONFIG = StudyConfig(
    repos=CORPUS,
    since="2025-01-01",
    until="2026-09-01",
)


def load_config() -> StudyConfig:
    """Return the default config with environment overrides applied.

    ``MAK_STUDY_CACHE`` relocates the bare clones; ``MAK_STUDY_WORKERS`` caps the
    process pool. Both exist so the pipeline can be re-run on a machine with a
    different disk layout without editing source.
    """
    config = DEFAULT_CONFIG
    cache_override = os.environ.get("MAK_STUDY_CACHE")
    if cache_override:
        config = replace(config, cache_root=Path(cache_override).expanduser())
    worker_override = os.environ.get("MAK_STUDY_WORKERS")
    if worker_override:
        try:
            config = replace(config, workers=max(1, int(worker_override)))
        except ValueError as exc:
            raise ContentionStudyError(
                f"MAK_STUDY_WORKERS must be an integer, got {worker_override!r}"
            ) from exc
    return config
