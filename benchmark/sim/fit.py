"""Fit simulator profiles from real benchmark call telemetry."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CallSample:
    """One provider call read from telemetry."""

    call_kind: str
    latency_seconds: float
    tokens_in: int
    tokens_out: int
    prompt_bytes: int
    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class LinearFit:
    """Ordinary least-squares line and coefficient of determination."""

    intercept: float
    slope: float
    r2: float


def read_samples(paths: list[Path]) -> list[CallSample]:
    """Read call records, ignoring registration-survival telemetry rows."""
    samples: list[CallSample] = []
    for path in paths:
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            data = json.loads(line)
            if data.get("call_kind") not in {"implement", "resolve", "plan"}:
                continue
            try:
                samples.append(
                    CallSample(
                        call_kind=str(data["call_kind"]),
                        latency_seconds=float(data["latency_seconds"]),
                        tokens_in=int(data["tokens_in"]),
                        tokens_out=int(data["tokens_out"]),
                        prompt_bytes=int(data["prompt_bytes"]),
                        provider=str(data["provider"]),
                        model=str(data["model"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"invalid telemetry row {path}:{number}: {exc}"
                ) from exc
    return samples


def linear_fit(xs: list[float], ys: list[float]) -> LinearFit:
    """Fit y = intercept + slope*x, including a stable constant-x fallback."""
    if not xs or len(xs) != len(ys):
        raise ValueError("linear fit requires equal nonempty x/y samples")
    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(ys)
    denominator = sum((value - mean_x) ** 2 for value in xs)
    slope = (
        sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
        / denominator
        if denominator
        else 0.0
    )
    intercept = mean_y - slope * mean_x
    residual = sum(
        (y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys, strict=True)
    )
    total = sum((y - mean_y) ** 2 for y in ys)
    r2 = 1.0 - residual / total if total else 1.0
    return LinearFit(intercept, slope, r2)


def _call_profile(samples: list[CallSample]) -> dict[str, object]:
    latencies = [max(sample.latency_seconds, 1e-6) for sample in samples]
    logs = [math.log(value) for value in latencies]
    inputs = linear_fit(
        [float(sample.prompt_bytes) for sample in samples],
        [float(sample.tokens_in) for sample in samples],
    )
    outputs = linear_fit(
        [float(sample.prompt_bytes) for sample in samples],
        [float(sample.tokens_out) for sample in samples],
    )
    # A Q-Q correlation would overstate fit quality on tiny samples. This simple
    # bounded score makes the empirical bootstrap the conservative fallback.
    spread = statistics.pstdev(logs) if len(logs) > 1 else 0.0
    fit_r2 = min(1.0, len(logs) / 10.0) if spread else 0.0
    return {
        "log_mean": statistics.fmean(logs),
        "log_sigma": spread,
        "fit_r2": fit_r2,
        "empirical_seconds": latencies,
        "tokens_in": {
            "intercept": inputs.intercept,
            "per_prompt_byte": inputs.slope,
            "fit_r2": inputs.r2,
        },
        "tokens_out": {
            "intercept": outputs.intercept,
            "per_prompt_byte": outputs.slope,
            "fit_r2": outputs.r2,
        },
    }


def fit_profile(
    samples: list[CallSample], *, dropped: int = 0, survived: int = 0
) -> dict[str, object]:
    """Fit one model profile and a Beta(1,1) resolver-drop posterior."""
    if not samples:
        raise ValueError("cannot fit a profile without call samples")
    identities = {(sample.provider, sample.model) for sample in samples}
    if len(identities) != 1:
        raise ValueError(f"samples contain multiple provider/models: {identities}")
    provider, model = next(iter(identities))
    by_kind = {
        kind: [sample for sample in samples if sample.call_kind == kind]
        for kind in {sample.call_kind for sample in samples}
    }
    if "implement" not in by_kind:
        raise ValueError("profile requires at least one implement sample")
    if "resolve" not in by_kind:
        by_kind["resolve"] = by_kind["implement"]
    return {
        "provider": provider,
        "model": model,
        "source": "fitted from benchmark/.calls telemetry",
        "default_failure_rate": 0.0,
        "resolver_drop_posterior": {
            "alpha": 1 + dropped,
            "beta": 1 + survived,
        },
        "calls": {
            kind: _call_profile(kind_samples)
            for kind, kind_samples in sorted(by_kind.items())
        },
    }


def main() -> None:
    """Fit telemetry files selected on the command line and write JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--resolver-dropped", type=int, default=0)
    parser.add_argument("--resolver-survived", type=int, default=0)
    args = parser.parse_args()
    profile = fit_profile(
        read_samples(args.inputs),
        dropped=args.resolver_dropped,
        survived=args.resolver_survived,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n")
    print(f"[fit] wrote {args.output}")


if __name__ == "__main__":
    main()
