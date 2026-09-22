"""A small, rate-limit-aware GitHub REST client built on the standard library.

Only PR *metadata* comes from the API — PR code comes from git itself (see
``fetch_refs.py``), so a single token's 5,000 requests/hour is ample. The client
deliberately avoids third-party HTTP libraries so the study has no runtime
dependency beyond the plotting stack.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from mining.exceptions import GitHubApiError, RateLimitExhausted

_API_ROOT = "https://api.github.com"
_USER_AGENT = "mak-contention-study/1.0 (+https://github.com/chaseungjoon/multi-agent-kernel)"


@dataclass(frozen=True, slots=True)
class RateLimitState:
    """The rate-limit headers returned with the most recent response."""

    remaining: int
    reset_at: float

    @property
    def seconds_until_reset(self) -> float:
        """Seconds to wait before the primary limit refills (never negative)."""
        return max(0.0, self.reset_at - time.time())


class GitHubClient:
    """Authenticated REST client with pagination and rate-limit backoff."""

    def __init__(self, token: str | None = None, max_retries: int = 5) -> None:
        resolved = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not resolved:
            raise GitHubApiError(
                "no GitHub token found; set GITHUB_TOKEN so the fetcher gets the "
                "5,000 requests/hour authenticated limit"
            )
        self._token = resolved
        self._max_retries = max_retries
        self.rate_limit = RateLimitState(remaining=5000, reset_at=time.time())

    def get(self, path: str) -> tuple[Any, dict[str, str]]:
        """GET one API path, returning the decoded body and response headers."""
        url = path if path.startswith("http") else _API_ROOT + path
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": _USER_AGENT,
            },
        )
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    headers = {k.lower(): v for k, v in response.headers.items()}
                    self._record_rate_limit(headers)
                    return json.load(response), headers
            except urllib.error.HTTPError as exc:
                last_error = exc
                headers = {k.lower(): v for k, v in exc.headers.items()}
                self._record_rate_limit(headers)
                if exc.code in (403, 429):
                    self._sleep_for_limit(headers, attempt)
                    continue
                if exc.code >= 500:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                raise GitHubApiError(f"GET {url} -> HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
                time.sleep(2.0 * (attempt + 1))
        raise GitHubApiError(
            f"GET {url} failed after {self._max_retries} attempts: {last_error}"
        )

    def paginate(self, path: str, per_page: int = 100) -> Iterator[list[Any]]:
        """Yield successive pages of a list endpoint, following ``Link: next``."""
        separator = "&" if "?" in path else "?"
        url: str | None = f"{path}{separator}per_page={per_page}"
        while url:
            body, headers = self.get(url)
            if not isinstance(body, list):
                raise GitHubApiError(
                    f"expected a list body from {url}, "
                    f"got {type(body).__name__}"
                )
            yield body
            url = _next_link(headers.get("link", ""))

    def _record_rate_limit(self, headers: dict[str, str]) -> None:
        try:
            self.rate_limit = RateLimitState(
                remaining=int(headers.get("x-ratelimit-remaining", "0")),
                reset_at=float(headers.get("x-ratelimit-reset", time.time())),
            )
        except ValueError:
            # Missing or malformed headers are not fatal; keep the previous state
            # and let the retry path handle an actual 403.
            pass

    def _sleep_for_limit(self, headers: dict[str, str], attempt: int) -> None:
        """Block until the limit refills, or fail loudly if the wait is absurd."""
        retry_after = headers.get("retry-after")
        if retry_after:
            delay = float(retry_after)
        elif self.rate_limit.remaining == 0:
            delay = self.rate_limit.seconds_until_reset + 5.0
        else:
            delay = min(60.0, 5.0 * (attempt + 1))
        if delay > 3700.0:
            raise RateLimitExhausted(
                f"rate limit reset is {delay:.0f}s away, which is longer than "
                f"a full window"
            )
        time.sleep(delay)


def _next_link(link_header: str) -> str | None:
    """Extract the ``rel="next"`` URL from an RFC 5988 ``Link`` header."""
    for part in link_header.split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        if 'rel="next"' in section[1].strip():
            return section[0].strip().strip("<>")
    return None
