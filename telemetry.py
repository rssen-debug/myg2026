"""Bounded, measured latency samples. Unknown pipeline stages stay unknown."""
from collections import deque
import math
import statistics
import threading
import time


class Telemetry:
    def __init__(self, capacity=1000):
        self.capacity = capacity
        self._lock = threading.Lock()
        self._series = {}

    def record(self, name, duration_ms, success=True, error=None):
        value = float(duration_ms)
        if not math.isfinite(value) or value < 0:
            raise ValueError("Duration must be finite and nonnegative")
        with self._lock:
            item = self._series.setdefault(name, {"samples": deque(maxlen=self.capacity),
                "requests": 0, "failures": 0, "last_success_at": None, "last_error": None})
            item["samples"].append(value)
            item["requests"] += 1
            item["failures"] += not success
            item["last_attempt_at"] = time.time()
            item["last_ok"] = bool(success)
            if success:
                item["last_success_at"] = item["last_attempt_at"]
                item["last_error"] = None
            else:
                item["last_error"] = "".join(c for c in str(error or "request failed") if c.isprintable())[:160]

    def snapshot(self):
        with self._lock:
            result = {}
            for name, item in self._series.items():
                values = sorted(item["samples"])
                result[name] = {k: v for k, v in item.items() if k != "samples"}
                result[name].update(sample_count=len(values), p50_ms=round(statistics.median(values), 2),
                    p95_ms=round(values[max(0, math.ceil(len(values)*.95)-1)], 2),
                    p99_ms=round(values[max(0, math.ceil(len(values)*.99)-1)], 2))
            return result


NETWORK_METRICS = Telemetry()
