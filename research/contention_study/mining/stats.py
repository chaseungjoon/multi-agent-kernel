"""Small distribution summaries shared by the export and analysis stages.

Deliberately dependency-free: the pipeline's only third-party requirement is the
plotting stack, and nothing here needs it.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class Summary:
    """Location and spread of one integer distribution."""

    count: int
    mean: float
    p50: float
    p90: float
    p99: float
    maximum: int

    def as_dict(self) -> dict[str, float | int]:
        """Plain mapping for JSON export."""
        return asdict(self)


def summarise(values: list[int]) -> Summary:
    """Summarise a list of counts; an empty list yields an all-zero summary."""
    if not values:
        return Summary(count=0, mean=0.0, p50=0.0, p90=0.0, p99=0.0, maximum=0)
    ordered = sorted(values)
    return Summary(
        count=len(ordered),
        mean=sum(ordered) / len(ordered),
        p50=percentile(ordered, 0.50),
        p90=percentile(ordered, 0.90),
        p99=percentile(ordered, 0.99),
        maximum=ordered[-1],
    )


def percentile(ordered: list[int], fraction: float) -> float:
    """Linear-interpolated percentile of an already-sorted list."""
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return float(ordered[0])
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def wilson_interval(
    successes: int, trials: int, z: float = 1.96
) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Used wherever a cell of the 2x2 comes from a sample rather than the whole
    population, so the write-up can quote an interval instead of a bare rate.
    """
    if trials == 0:
        return (0.0, 0.0)
    proportion = successes / trials
    denominator = 1 + z * z / trials
    centre = proportion + z * z / (2 * trials)
    spread = z * math.sqrt(
        proportion * (1 - proportion) / trials + z * z / (4 * trials * trials)
    )
    return ((centre - spread) / denominator, (centre + spread) / denominator)


def histogram(values: list[int], limit: int = 64) -> list[tuple[int, int]]:
    """``(value, frequency)`` pairs, truncated to the ``limit`` smallest values."""
    counts: dict[int, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return sorted(counts.items())[:limit]
