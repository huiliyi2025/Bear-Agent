"""Small statistics helpers shared by metrics and benchmarks.

Percentiles use linear interpolation over the observed samples. They describe
exactly the rows that were queried; when a query is truncated the result says
so instead of silently pretending the sample is complete.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    if not 0.0 <= q <= 1.0:
        raise ValueError("q must be in [0, 1]")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower] * (1 - weight) + ordered[upper] * weight)


def stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance)


@dataclass(frozen=True)
class Distribution:
    count: int = 0
    total: float = 0.0
    mean: float = 0.0
    median: float = 0.0
    p50: float = 0.0
    p95: float = 0.0
    p99: float = 0.0
    min: float = 0.0
    max: float = 0.0
    stdev: float = 0.0

    @classmethod
    def of(cls, values: list[float]) -> Distribution:
        cleaned = [float(value) for value in values if math.isfinite(float(value))]
        if not cleaned:
            return cls()
        total = sum(cleaned)
        return cls(
            count=len(cleaned),
            total=total,
            mean=total / len(cleaned),
            median=percentile(cleaned, 0.5),
            p50=percentile(cleaned, 0.5),
            p95=percentile(cleaned, 0.95),
            p99=percentile(cleaned, 0.99),
            min=min(cleaned),
            max=max(cleaned),
            stdev=stdev(cleaned),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "total": round(self.total, 3),
            "mean": round(self.mean, 3),
            "median": round(self.median, 3),
            "p50": round(self.p50, 3),
            "p95": round(self.p95, 3),
            "p99": round(self.p99, 3),
            "min": round(self.min, 3),
            "max": round(self.max, 3),
            "stdev": round(self.stdev, 3),
        }


def percentile_from_histogram(buckets: list[tuple[float, int]], count: int, q: float) -> float:
    """Approximate a percentile from cumulative histogram buckets.

    Used only by the live registry, where samples are not retained. Metrics
    reports computed from SQL use exact samples.
    """
    if count <= 0 or not buckets:
        return 0.0
    target = max(1, math.ceil(count * q))
    seen = 0
    previous = 0.0
    for upper, bucket_count in buckets:
        seen += bucket_count
        if seen >= target:
            return float(upper)
        previous = float(upper)
    return previous
