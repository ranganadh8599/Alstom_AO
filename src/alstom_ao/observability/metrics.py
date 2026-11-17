"""Lightweight in-process metrics, exposed in Prometheus text format.

Deliberately dependency-free: the platform should not require a metrics SDK to
answer "how many runs failed today".
"""

from __future__ import annotations

import threading
from collections import Counter, defaultdict


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counters: Counter[str] = Counter()
        self.latency_totals: defaultdict[str, float] = defaultdict(float)
        self.latency_counts: defaultdict[str, int] = defaultdict(int)

    def incr(self, name: str, value: int = 1) -> None:
        with self._lock:
            self.counters[name] += value

    def observe(self, name: str, seconds: float) -> None:
        with self._lock:
            self.latency_totals[name] += seconds
            self.latency_counts[name] += 1

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            for name, value in sorted(self.counters.items()):
                metric = name.replace(".", "_").replace("-", "_")
                lines.append(f"# TYPE {metric} counter")
                lines.append(f"{metric} {value}")
            for name, total in sorted(self.latency_totals.items()):
                metric = name.replace(".", "_").replace("-", "_")
                count = max(self.latency_counts[name], 1)
                lines.append(f"# TYPE {metric}_seconds summary")
                lines.append(f"{metric}_seconds_sum {total:.6f}")
                lines.append(f"{metric}_seconds_count {self.latency_counts[name]}")
                lines.append(f"{metric}_seconds_avg {total / count:.6f}")
        return "\n".join(lines) + "\n"


metrics = Metrics()
