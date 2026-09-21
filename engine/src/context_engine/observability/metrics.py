"""Small process-local metrics registry for M1 instrumentation."""

from __future__ import annotations

from collections import Counter
from threading import Lock


class MetricsRegistry:
    """Store thread-safe process counters with engine-prefixed names."""

    def __init__(self) -> None:
        self._values: Counter[str] = Counter()
        self._lock = Lock()

    def increment(self, name: str, value: int = 1) -> None:
        """Increase an engine metric by the supplied value."""

        if not name.startswith("context_engine_"):
            raise ValueError("metric names must use the context_engine_ prefix")
        with self._lock:
            self._values[name] += value

    def snapshot(self) -> dict[str, int]:
        """Return an immutable-by-convention copy of current counters."""

        with self._lock:
            return dict(sorted(self._values.items()))

    def render_prometheus(self) -> str:
        """Render counters in Prometheus text exposition format."""

        return "".join(f"{name} {value}\n" for name, value in self.snapshot().items())
