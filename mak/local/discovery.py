"""Ask the machine what local model servers are actually running.

MAK never starts a runtime and never installs one (see the wave's non-goals): a
code editor has no business launching a background daemon on someone's machine.
What it can do is *look*, so the first-run setup and the ``/local`` wizard can be
honest about what is there instead of asking the user to know.

The well-known endpoints are probed **concurrently**, each on its own
short-lived thread under a sub-second timeout, because the common case is that
most of them are not listening and a serial scan would spend the whole timeout on
each one in turn. :func:`discover` never raises: it runs on a UI path and during
startup, where "nothing answered" is an ordinary result and an exception would be
a startup MAK failed for the sake of a convenience.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor

from mak.local.ollama_client import DEFAULT_BASE_URL
from mak.local.runtime import LocalRuntime, probe_ollama, probe_openai_compatible

# The environment variable a user points at a server MAK does not know about.
# Read by discovery, by ``--models local:…``/``ollama:…``, and by the wizard, so
# one export configures all three.
LOCAL_BASE_URL_ENV = "MAK_LOCAL_BASE_URL"

_DEFAULT_TIMEOUT_S = 0.4

# ``(name, base_url)`` for each runtime's conventional listen address. Ollama is
# probed separately (it is the one with a native API); these are the ones MAK
# reaches through the OpenAI-compatible transport.
_WELL_KNOWN_OPENAI: tuple[tuple[str, str], ...] = (
    ("LM Studio", "http://localhost:1234/v1"),
    ("vLLM", "http://localhost:8000/v1"),
    ("llama.cpp", "http://localhost:8080/v1"),
)

# A probe: given a URL and a timeout, return the runtime there or None. Injected
# in tests so discovery is exercised without a socket.
Prober = Callable[[str, float], "LocalRuntime | None"]


def _probe_extra(base_url: str, timeout: float) -> LocalRuntime | None:
    """Probe a user-supplied endpoint, trying both protocols.

    OpenAI-compatible first, because that is what a ``$MAK_LOCAL_BASE_URL``
    normally points at (the native Ollama port is already scanned); Ollama's
    ``/api/version`` second, so pointing the variable at an Ollama instance on a
    non-standard port still reports the native runtime and gets the native
    adapter.
    """
    found = probe_openai_compatible(base_url, timeout=timeout, name="custom")
    if found is not None:
        return found
    return probe_ollama(base_url, timeout=timeout)


def _default_prober(base_url: str, timeout: float) -> LocalRuntime | None:
    """Probe one of the addresses :func:`discover` scans."""
    if base_url == DEFAULT_BASE_URL:
        return probe_ollama(base_url, timeout=timeout)
    for name, known_url in _WELL_KNOWN_OPENAI:
        if base_url == known_url:
            return probe_openai_compatible(base_url, timeout=timeout, name=name)
    return _probe_extra(base_url, timeout)


def scan_targets(extra_urls: Sequence[str] = ()) -> list[str]:
    """Return the endpoints :func:`discover` probes, in report order.

    Ollama leads because it is the runtime MAK supports natively; the rest
    follow by port. ``$MAK_LOCAL_BASE_URL`` and any ``extra_urls`` come last and
    are deduplicated against the well-known ports, so a user who exports the
    Ollama default does not see it twice.
    """
    targets = [DEFAULT_BASE_URL, *(url for _name, url in _WELL_KNOWN_OPENAI)]
    from_env = os.environ.get(LOCAL_BASE_URL_ENV, "").strip()
    for candidate in (from_env, *extra_urls):
        url = candidate.strip().rstrip("/")
        if url and url not in targets:
            targets.append(url)
    return targets


def discover(
    *,
    extra_urls: Sequence[str] = (),
    timeout: float = _DEFAULT_TIMEOUT_S,
    prober: Prober | None = None,
) -> list[LocalRuntime]:
    """Return every local runtime that answered, Ollama first.

    Deduplicated by ``base_url``. Never raises — a probe that fails is simply a
    runtime that is not there.
    """
    probe = prober or _default_prober
    targets = scan_targets(extra_urls)
    if not targets:
        return []

    def run(url: str) -> LocalRuntime | None:
        try:
            return probe(url, timeout)
        except Exception:
            # A prober is documented not to raise, but discovery is on a UI
            # path: a third-party or test double that breaks that contract must
            # not be able to take the scan (or the startup) down with it.
            return None

    # One thread per target, all launched together: most of them will not be
    # listening, and probing serially would pay the timeout once per address.
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        results = list(pool.map(run, targets))

    found: list[LocalRuntime] = []
    seen: set[str] = set()
    for runtime in results:
        if runtime is None or runtime.base_url in seen:
            continue
        seen.add(runtime.base_url)
        found.append(runtime)
    return found
