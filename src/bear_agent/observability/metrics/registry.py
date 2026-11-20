"""In-process metric primitives.

The durable metrics report is computed from authoritative tables and the event
log (see ``query.py``), so the harness does not write metric samples on the hot
path. This registry is used for live diagnostics, benchmark statistics and
focused performance measurement. It implements the three metric types that are
actually useful here: counters, gauges and histograms.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from bear_agent.observability.metrics.stats import percentile_from_histogram

MetricKind = Literal["counter", "gauge", "histogram"]
LabelValue = str | int | float | bool
Labels = Mapping[str, LabelValue]

DEFAULT_LATENCY_BUCKETS: tuple[float, ...] = (
    1.0,
    2.0,
    5.0,
    10.0,
    20.0,
    50.0,
    100.0,
    200.0,
    500.0,
    1_000.0,
    2_000.0,
    5_000.0,
    10_000.0,
    30_000.0,
    60_000.0,
    120_000.0,
    300_000.0,
)

MAX_LABEL_KEYS = 12
MAX_LABEL_VALUE = 128
MAX_SERIES_PER_METRIC = 200


def normalize_labels(labels: Labels | None) -> tuple[tuple[str, str], ...]:
    if not labels:
        return ()
    if len(labels) > MAX_LABEL_KEYS:
        raise ValueError(f"too many metric labels (max {MAX_LABEL_KEYS})")
    normalized: list[tuple[str, str]] = []
    for key, value in sorted(labels.items()):
        if not key or not isinstance(key, str):
            raise ValueError("metric label keys must be non-empty strings")
        text = str(value)
        if len(text) > MAX_LABEL_VALUE:
            text = text[:MAX_LABEL_VALUE] + "..."
        normalized.append((key, text))
    return tuple(normalized)


@dataclass
class Counter:
    value: float = 0.0

    def increment(self, amount: float = 1.0) -> None:
        if amount < 0:
            raise ValueError("counters can only increase")
        self.value += amount


@dataclass
class Gauge:
    value: float = 0.0

    def set(self, value: float) -> None:
        if not math.isfinite(value):
            raise ValueError("gauge value must be finite")
        self.value = value


@dataclass
class Histogram:
    buckets: tuple[float, ...] = DEFAULT_LATENCY_BUCKETS
    counts: list[int] = field(init=False)
    count: int = 0
    total: float = 0.0
    min: float = 0.0
    max: float = 0.0

    def __post_init__(self) -> None:
        self.counts = [0] * len(self.buckets)

    def observe(self, value: float) -> None:
        if value < 0 or not math.isfinite(value):
            raise ValueError("histogram observations must be finite and non-negative")
        self.count += 1
        self.total += value
        self.min = value if self.count == 1 else min(self.min, value)
        self.max = max(self.max, value)
        for index, upper in enumerate(self.buckets):
            if value <= upper:
                self.counts[index] += 1
                return
        # Values beyond the largest bucket are still counted in count/total/max.

    def snapshot(self) -> dict[str, Any]:
        raw = list(zip(self.buckets, self.counts, strict=True))
        cumulative: list[tuple[float, int]] = []
        running = 0
        for upper, bucket_count in raw:
            running += bucket_count
            cumulative.append((upper, running))
        return {
            "count": self.count,
            "sum": round(self.total, 3),
            "mean": round(self.total / self.count, 3) if self.count else 0.0,
            "min": round(self.min, 3),
            "max": round(self.max, 3),
            "p50": round(percentile_from_histogram(raw, self.count, 0.5), 3),
            "p95": round(percentile_from_histogram(raw, self.count, 0.95), 3),
            "p99": round(percentile_from_histogram(raw, self.count, 0.99), 3),
            "buckets": [{"le": upper, "count": count} for upper, count in cumulative],
        }


@dataclass
class _Series:
    kind: MetricKind
    labels: tuple[tuple[str, str], ...]
    counter: Counter | None = None
    gauge: Gauge | None = None
    histogram: Histogram | None = None


class MetricsRegistry:
    """Thread-safe, cardinality-bounded metric registry."""

    def __init__(
        self,
        *,
        max_series_per_metric: int = MAX_SERIES_PER_METRIC,
        histogram_buckets: tuple[float, ...] = DEFAULT_LATENCY_BUCKETS,
    ) -> None:
        self.max_series_per_metric = max_series_per_metric
        self.histogram_buckets = histogram_buckets
        self._lock = threading.Lock()
        self._series: dict[tuple[str, MetricKind], dict[tuple[tuple[str, str], ...], _Series]] = {}

    def counter(self, name: str, labels: Labels | None = None, amount: float = 1.0) -> None:
        series = self._get(name, "counter", labels)
        assert series.counter is not None
        series.counter.increment(amount)

    def gauge(self, name: str, value: float, labels: Labels | None = None) -> None:
        series = self._get(name, "gauge", labels)
        assert series.gauge is not None
        series.gauge.set(value)

    def observe(self, name: str, value: float, labels: Labels | None = None) -> None:
        series = self._get(name, "histogram", labels)
        assert series.histogram is not None
        series.histogram.observe(value)

    def value(self, name: str, labels: Labels | None = None) -> float:
        key = normalize_labels(labels)
        with self._lock:
            for kind in ("counter", "gauge", "histogram"):
                series = self._series.get((name, kind), {}).get(key)
                if series is None:
                    continue
                if series.counter is not None:
                    return series.counter.value
                if series.gauge is not None:
                    return series.gauge.value
                if series.histogram is not None:
                    return float(series.histogram.count)
        return 0.0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            result: dict[str, Any] = {}
            for (name, kind), family in sorted(self._series.items()):
                entry = result.setdefault(name, {"kind": kind, "series": []})
                for labels, series in family.items():
                    item: dict[str, Any] = {"labels": dict(labels)}
                    if series.counter is not None:
                        item["value"] = series.counter.value
                    elif series.gauge is not None:
                        item["value"] = series.gauge.value
                    elif series.histogram is not None:
                        item["histogram"] = series.histogram.snapshot()
                    entry["series"].append(item)
            return result

    def reset(self) -> None:
        with self._lock:
            self._series.clear()

    def _get(self, name: str, kind: MetricKind, labels: Labels | None) -> _Series:
        if not name or len(name) > 200:
            raise ValueError("metric name must be 1..200 characters")
        key = normalize_labels(labels)
        family_key = (name, kind)
        with self._lock:
            family = self._series.setdefault(family_key, {})
            if key not in family and len(family) >= self.max_series_per_metric:
                # High-cardinality input (an id, a path, user text) must not
                # create unbounded series. Fold the overflow into one bucket
                # and keep the metric usable.
                key = (("_overflow", "true"),)
            series = family.get(key)
            if series is None:
                series = _Series(
                    kind=kind,
                    labels=key,
                    counter=Counter() if kind == "counter" else None,
                    gauge=Gauge() if kind == "gauge" else None,
                    histogram=(
                        Histogram(buckets=self.histogram_buckets) if kind == "histogram" else None
                    ),
                )
                family[key] = series
            return series
