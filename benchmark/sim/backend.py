"""Deterministic simulated backend implementing the benchmark Backend protocol."""

from __future__ import annotations

import hashlib
import math
import random
import re
import threading
import time
from dataclasses import dataclass

from harness.agents import Usage, _union_registry
from harness.workload import Operation

from sim.profile import CallProfile, SimulationProfile

_REGISTER_LINE = re.compile(r"^\s*register\(.*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class SimulatedCall:
    """One sampled model call, before wall-time scaling."""

    kind: str
    key: str
    attempt: int
    latency_seconds: float
    prompt_bytes: int
    usage: Usage
    failed: bool = False


class SimBackend:
    """Return references while modeling latency, tokens, failures, and line drops."""

    def __init__(
        self,
        name: str,
        profile: SimulationProfile,
        *,
        seed: int,
        time_scale: float = 0.02,
        failure_rate: float | None = None,
        kernel_only: bool = False,
    ) -> None:
        if time_scale < 0:
            raise ValueError("time_scale must be nonnegative")
        self.name = name
        self.profile = profile
        self.seed = seed
        self.time_scale = 0.0 if kernel_only else time_scale
        self.failure_rate = (
            profile.default_failure_rate if failure_rate is None else failure_rate
        )
        self._attempts: dict[tuple[str, str], int] = {}
        self._calls: list[SimulatedCall] = []
        self._windows: list[tuple[float, float]] = []
        self._guard = threading.Lock()

    @property
    def calls(self) -> tuple[SimulatedCall, ...]:
        """Return a stable snapshot of sampled calls."""
        with self._guard:
            return tuple(self._calls)

    @property
    def modeled_seconds(self) -> float:
        """Return unscaled modeled latency across this agent's calls."""
        return sum(call.latency_seconds for call in self.calls)

    @property
    def wall_windows(self) -> tuple[tuple[float, float], ...]:
        """Return actual start/end times used to recover the modeled critical path."""
        with self._guard:
            return tuple(self._windows)

    def implement(self, op: Operation, stub_source: str) -> tuple[str, Usage]:
        """Sleep scaled latency and return the reference or a parseable failure."""
        prompt_bytes = len(stub_source.encode()) + len(op.context.encode())
        sample, rng = self._sample("implement", op.name, prompt_bytes)
        failed = rng.random() < self.failure_rate
        self._finish(sample, failed=failed)
        if failed:
            return self._wrong_but_parseable(op.reference), sample.usage
        return op.reference, sample.usage

    def resolve(self, versions: list[str]) -> tuple[str, Usage]:
        """Return a union merge with independent modeled drops per register line."""
        joined = "\n".join(versions)
        key = hashlib.sha256(joined.encode()).hexdigest()[:16]
        sample, rng = self._sample("resolve", key, len(joined.encode()))
        merged = _union_registry(versions)
        drop_probability = self.profile.resolver_drop_probability
        kept: list[str] = []
        for line in merged.splitlines(keepends=True):
            if _REGISTER_LINE.match(line) and rng.random() < drop_probability:
                continue
            kept.append(line)
        self._finish(sample)
        return "".join(kept), sample.usage

    def _sample(
        self, kind: str, key: str, prompt_bytes: int
    ) -> tuple[SimulatedCall, random.Random]:
        profile = self.profile.calls[kind]
        attempt = self._next_attempt(kind, key)
        rng = random.Random(self._derived_seed(kind, key, attempt))
        latency = self._sample_latency(profile, rng)
        usage = Usage(
            tokens_in=profile.tokens_in.predict(prompt_bytes),
            tokens_out=profile.tokens_out.predict(prompt_bytes),
            calls=1,
        )
        return (
            SimulatedCall(kind, key, attempt, latency, prompt_bytes, usage),
            rng,
        )

    def _finish(self, sample: SimulatedCall, *, failed: bool = False) -> None:
        started = time.perf_counter()
        if self.time_scale:
            time.sleep(sample.latency_seconds * self.time_scale)
        finished = time.perf_counter()
        recorded = SimulatedCall(
            kind=sample.kind,
            key=sample.key,
            attempt=sample.attempt,
            latency_seconds=sample.latency_seconds,
            prompt_bytes=sample.prompt_bytes,
            usage=sample.usage,
            failed=failed,
        )
        with self._guard:
            self._calls.append(recorded)
            self._windows.append((started, finished))

    def _next_attempt(self, kind: str, key: str) -> int:
        with self._guard:
            attempt = self._attempts.get((kind, key), 0)
            self._attempts[(kind, key)] = attempt + 1
        return attempt

    def _derived_seed(self, kind: str, key: str, attempt: int) -> int:
        material = f"{self.seed}\0{kind}\0{key}\0{attempt}".encode()
        return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")

    @staticmethod
    def _sample_latency(profile: CallProfile, rng: random.Random) -> float:
        if profile.fit_r2 < 0.5 and profile.empirical_seconds:
            return profile.empirical_seconds[
                rng.randrange(len(profile.empirical_seconds))
            ]
        return math.exp(rng.gauss(profile.log_mean, profile.log_sigma))

    @staticmethod
    def _wrong_but_parseable(reference: str) -> str:
        lines = reference.splitlines()
        if not lines:
            return reference
        return lines[0] + "\n    raise RuntimeError('simulated agent failure')\n"
