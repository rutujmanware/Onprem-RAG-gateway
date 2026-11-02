"""Small Prometheus-compatible, zero-dependency request telemetry registry."""

from __future__ import annotations

from collections import Counter
from threading import Lock
from typing import Optional


class GatewayMetrics:
    def __init__(self) -> None:
        self._lock = Lock()
        self.requests: Counter = Counter()
        self.cache_hits: Counter = Counter()
        self.routes: Counter = Counter()
        self.score_count = 0
        self.score_total = 0.0

    def record(self, cache_status: str, route: Optional[str] = None, score: Optional[float] = None) -> None:
        with self._lock:
            self.requests[cache_status] += 1
            if cache_status.startswith("HIT"):
                self.cache_hits[cache_status] += 1
            if route:
                self.routes[route] += 1
            if score is not None:
                self.score_count += 1
                self.score_total += float(score)

    def render(self) -> str:
        with self._lock:
            lines = [
                "# HELP gateway_requests_total Requests by cache outcome",
                "# TYPE gateway_requests_total counter",
            ]
            lines += [f'gateway_requests_total{{cache="{key}"}} {value}' for key, value in sorted(self.requests.items())]
            lines += ["# HELP gateway_routes_total Cache-miss routes", "# TYPE gateway_routes_total counter"]
            lines += [f'gateway_routes_total{{tier="{key}"}} {value}' for key, value in sorted(self.routes.items())]
            lines += ["# HELP gateway_complexity_score_mean Mean cache-miss complexity score", "# TYPE gateway_complexity_score_mean gauge"]
            mean = self.score_total / self.score_count if self.score_count else 0.0
            lines.append(f"gateway_complexity_score_mean {mean}")
            return "\n".join(lines) + "\n"


metrics = GatewayMetrics()
