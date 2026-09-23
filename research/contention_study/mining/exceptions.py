"""Domain exceptions for the Wave 23 contention study.

The kernel keeps its own exceptions in ``mak/core/exceptions.py``. This study is
read-only with respect to the kernel, so it defines its own hierarchy here and
never extends the kernel's.
"""

from __future__ import annotations


class ContentionStudyError(Exception):
    """Base class for every error raised by the mining pipeline."""


class GitHubApiError(ContentionStudyError):
    """The GitHub REST API returned a response the fetcher cannot use."""


class RateLimitExhausted(GitHubApiError):
    """The primary or secondary rate limit was hit and waiting is not allowed."""


class GitCommandError(ContentionStudyError):
    """A ``git`` invocation failed in a way the pipeline cannot recover from."""


class RepositoryNotPrepared(ContentionStudyError):
    """A repository's bare clone or pull refs are missing for a requested step."""


class NodeMappingError(ContentionStudyError):
    """A diff hunk could not be mapped onto the kernel's node decomposition."""


class ProfileSchemaError(ContentionStudyError):
    """An exported profile does not satisfy the Wave 21 consumer schema."""
