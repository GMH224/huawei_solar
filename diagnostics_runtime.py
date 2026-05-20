
"""
Huawei Solar 2.10.10 Modbus diagnostics runtime.
"""

from collections import deque
from dataclasses import dataclass, field
from time import time


@dataclass
class ModbusEvent:
    ts: float
    success: bool
    latency: float
    timeout: bool = False
    retry: bool = False
    busy: bool = False


@dataclass
class ModbusStatistics:
    events: deque = field(default_factory=lambda: deque(maxlen=5000))

    def record(self, success, latency, timeout=False, retry=False, busy=False):
        self.events.append(
            ModbusEvent(
                ts=time(),
                success=success,
                latency=latency,
                timeout=timeout,
                retry=retry,
                busy=busy,
            )
        )

    def _last_hour(self):
        cutoff = time() - 3600
        return [e for e in self.events if e.ts >= cutoff]

    @property
    def calls_per_hour(self):
        return len(self._last_hour())

    @property
    def failures_per_hour(self):
        return len([e for e in self._last_hour() if not e.success])

    @property
    def timeouts_per_hour(self):
        return len([e for e in self._last_hour() if e.timeout])

    @property
    def retries_per_hour(self):
        return len([e for e in self._last_hour() if e.retry])

    @property
    def busy_errors_per_hour(self):
        return len([e for e in self._last_hour() if e.busy])

    @property
    def average_latency(self):
        vals = [e.latency for e in self._last_hour()]
        return round(sum(vals) / len(vals), 3) if vals else 0

    @property
    def max_latency(self):
        vals = [e.latency for e in self._last_hour()]
        return round(max(vals), 3) if vals else 0

    @property
    def availability_percent(self):
        events = self._last_hour()
        if not events:
            return 100
        ok = len([e for e in events if e.success])
        return round((ok / len(events)) * 100, 2)


MODBUS_STATS = ModbusStatistics()


# Huawei Solar 2.10.11 runtime diagnostics enhancements

def record_success(latency):
    MODBUS_STATS.record(
        success=True,
        latency=latency,
    )


def record_failure(latency=0, timeout=False, retry=False, busy=False):
    MODBUS_STATS.record(
        success=False,
        latency=latency,
        timeout=timeout,
        retry=retry,
        busy=busy,
    )
