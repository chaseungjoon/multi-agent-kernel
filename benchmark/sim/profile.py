"""Typed simulation profile schema and deterministic profile loading."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class TokenModel:
    """Linear token estimate as a function of prompt bytes."""

    intercept: float
    per_prompt_byte: float
    fit_r2: float

    def predict(self, prompt_bytes: int) -> int:
        """Return a nonnegative rounded token prediction."""
        return max(0, round(self.intercept + self.per_prompt_byte * prompt_bytes))


@dataclass(frozen=True, slots=True)
class CallProfile:
    """Latency and token model for one call kind."""

    log_mean: float
    log_sigma: float
    fit_r2: float
    empirical_seconds: tuple[float, ...]
    tokens_in: TokenModel
    tokens_out: TokenModel


@dataclass(frozen=True, slots=True)
class SimulationProfile:
    """All modeled behavior for one provider/model pair."""

    provider: str
    model: str
    calls: dict[str, CallProfile]
    resolver_drop_alpha: float
    resolver_drop_beta: float
    default_failure_rate: float
    source: str

    @property
    def resolver_drop_probability(self) -> float:
        """Return the posterior mean probability of losing a contended line."""
        total = self.resolver_drop_alpha + self.resolver_drop_beta
        return self.resolver_drop_alpha / total if total else 0.0


def _number(value: object, label: str) -> float:
    if isinstance(value, int | float):
        return float(value)
    raise ValueError(f"{label} must be numeric, got {value!r}")


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return {str(key): item for key, item in value.items()}


def _numbers(value: object, label: str) -> tuple[float, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return tuple(_number(item, label) for item in value)


def _token_model(data: dict[str, object]) -> TokenModel:
    return TokenModel(
        intercept=_number(data["intercept"], "intercept"),
        per_prompt_byte=_number(data["per_prompt_byte"], "per_prompt_byte"),
        fit_r2=_number(data.get("fit_r2", 0.0), "fit_r2"),
    )


def _call_profile(data: dict[str, object]) -> CallProfile:
    return CallProfile(
        log_mean=_number(data["log_mean"], "log_mean"),
        log_sigma=_number(data["log_sigma"], "log_sigma"),
        fit_r2=_number(data.get("fit_r2", 0.0), "fit_r2"),
        empirical_seconds=_numbers(
            data.get("empirical_seconds", []), "empirical_seconds"
        ),
        tokens_in=_token_model(_mapping(data["tokens_in"], "tokens_in")),
        tokens_out=_token_model(_mapping(data["tokens_out"], "tokens_out")),
    )


def load_profile(path: Path) -> SimulationProfile:
    """Load and validate a simulator profile from JSON."""
    data = _mapping(json.loads(path.read_text()), "profile")
    raw_calls = _mapping(data["calls"], "calls")
    calls = {
        name: _call_profile(_mapping(value, f"calls.{name}"))
        for name, value in raw_calls.items()
    }
    missing = {"implement", "resolve"} - set(calls)
    if missing:
        raise ValueError(f"profile is missing call kinds: {sorted(missing)}")
    posterior = _mapping(
        data.get("resolver_drop_posterior", {}), "resolver_drop_posterior"
    )
    return SimulationProfile(
        provider=str(data["provider"]),
        model=str(data["model"]),
        calls=calls,
        resolver_drop_alpha=_number(posterior.get("alpha", 1.0), "alpha"),
        resolver_drop_beta=_number(posterior.get("beta", 99.0), "beta"),
        default_failure_rate=_number(
            data.get("default_failure_rate", 0.0), "default_failure_rate"
        ),
        source=str(data.get("source", path.name)),
    )


def profile_hash(path: Path) -> str:
    """Return a stable short content hash used in every sweep record."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
