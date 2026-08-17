"""Observability: process-local counters/gauges for /metrics and logs."""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Iterable


class Metrics:
    """Thread-safe in-process metrics. Prometheus-style text export."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict = defaultdict(int)
        self._gauges: dict = defaultdict(float)
        self._histograms: dict = defaultdict(list)

    def inc(self, name: str, value: int = 1, labels: tuple = ()) -> None:
        with self._lock:
            self._counters[(name, labels)] += value

    def set_gauge(self, name: str, value: float, labels: tuple = ()) -> None:
        with self._lock:
            self._gauges[(name, labels)] = value

    def observe(self, name: str, value: float, labels: tuple = ()) -> None:
        with self._lock:
            arr = self._histograms[(name, labels)]
            arr.append(value)
            if len(arr) > 1000:
                self._histograms[(name, labels)] = arr[-1000:]

    def render(self) -> str:
        lines: list = []
        with self._lock:
            for (name, labels), v in self._counters.items():
                lines.append(f"{name}{self._fmt_labels(labels)} {v}")
            for (name, labels), v in self._gauges.items():
                lines.append(f"{name}{self._fmt_labels(labels)} {v}")
            for (name, labels), values in self._histograms.items():
                if not values:
                    continue
                lines.append(f"{name}_count{self._fmt_labels(labels)} {len(values)}")
                lines.append(f"{name}_sum{self._fmt_labels(labels)} {sum(values):.4f}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _fmt_labels(labels: Iterable[tuple]) -> str:
        labels = tuple(labels)
        if not labels:
            return ""
        items = ",".join(f'{k}="{v}"' for k, v in labels)
        return "{" + items + "}"


metrics = Metrics()
