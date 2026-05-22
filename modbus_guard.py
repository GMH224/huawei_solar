"""Modbus traffic guard for Huawei Solar.

The SUN2000 inverter's Modbus interface is single-threaded and will return
error codes (or silently drop responses) when two requests overlap or arrive
faster than it can handle.

This module provides:

ModbusGuard
    A per-device asyncio lock + adaptive rate-limiter that ensures:
    1. Only ONE Modbus request is in-flight at a time for a given inverter.
    2. A minimum inter-request gap is respected, derived from observed RTTs.

Dynamic gap (v2.12.2)
---------------------
Rather than a fixed 150 ms gap, the guard maintains a rolling median of the
last N round-trip times and sets the gap to ``max(MIN_GAP, median_rtt * RTT_GAP_FACTOR)``.
On a healthy wired LAN where typical RTT is 40–80 ms this shrinks the gap to
~60–90 ms, cutting the dead-time between the 4 coordinator polls from
4 × 150 ms = 600 ms down to ~250–350 ms.  On a flaky WiFi or RS485 link the
gap self-heals upward as RTTs climb.

The gap is also reported back to ModbusTelemetry so it appears in the
diagnostic sensors.

Usage (in coordinators)
-----------------------
    guard = ModbusGuard.get_or_create(serial_number)

    async with guard.request():
        result = await device.batch_update(names)

    # After the request, record the RTT so the gap adapts:
    guard.record_rtt(elapsed_seconds)
"""

from __future__ import annotations

import asyncio
from collections import deque
import logging
import statistics
import time
from datetime import timedelta

_LOGGER = logging.getLogger(__name__)

# Hard floor — never drop below this regardless of how fast the inverter is.
MIN_INTER_REQUEST_GAP = timedelta(milliseconds=50)

# Hard ceiling — never exceed this (guards against one huge RTT spiking the gap).
MAX_INTER_REQUEST_GAP = timedelta(milliseconds=500)

# Initial / fallback gap used before enough RTT samples have been collected.
DEFAULT_INTER_REQUEST_GAP = timedelta(milliseconds=150)

# gap = clamp(median_rtt * factor, MIN, MAX)
RTT_GAP_FACTOR: float = 1.5

# Number of RTT samples kept for the rolling median.
RTT_WINDOW: int = 10

# Maximum time a request may be queued before it is abandoned (prevents pileup).
QUEUE_WAIT_TIMEOUT = timedelta(seconds=10)


class ModbusGuard:
    """Per-inverter asyncio serialiser and adaptive rate-limiter."""

    _registry: dict[str, "ModbusGuard"] = {}

    # ── class-level helpers ───────────────────────────────────────────────────

    @classmethod
    def get_or_create(cls, serial_number: str) -> "ModbusGuard":
        if serial_number not in cls._registry:
            cls._registry[serial_number] = cls(serial_number)
        return cls._registry[serial_number]

    @classmethod
    def clear_registry(cls) -> None:
        cls._registry.clear()

    # ── instance ──────────────────────────────────────────────────────────────

    def __init__(self, serial_number: str) -> None:
        self.serial_number = serial_number
        self._lock = asyncio.Lock()
        self._last_request_end: float = 0.0   # monotonic time
        self._queue_depth: int = 0
        self._rtt_samples: deque[float] = deque(maxlen=RTT_WINDOW)
        self._current_gap: float = DEFAULT_INTER_REQUEST_GAP.total_seconds()

    # ── RTT recording (called by coordinator after each successful request) ───

    def record_rtt(self, rtt_seconds: float) -> None:
        """Record a round-trip time sample and recompute the dynamic gap."""
        if rtt_seconds <= 0:
            return
        self._rtt_samples.append(rtt_seconds)
        if len(self._rtt_samples) >= 3:
            median_rtt = statistics.median(self._rtt_samples)
            new_gap = median_rtt * RTT_GAP_FACTOR
            lo = MIN_INTER_REQUEST_GAP.total_seconds()
            hi = MAX_INTER_REQUEST_GAP.total_seconds()
            new_gap = max(lo, min(new_gap, hi))
            if abs(new_gap - self._current_gap) > 0.005:  # >5 ms change
                _LOGGER.debug(
                    "ModbusGuard[%s]: gap %.0f ms → %.0f ms  (median RTT %.0f ms, n=%d)",
                    self.serial_number,
                    self._current_gap * 1000,
                    new_gap * 1000,
                    median_rtt * 1000,
                    len(self._rtt_samples),
                )
                self._current_gap = new_gap

    def reset_rtt(self) -> None:
        """Reset RTT samples after a connection failure (conservative restart)."""
        self._rtt_samples.clear()
        self._current_gap = DEFAULT_INTER_REQUEST_GAP.total_seconds()

    @property
    def current_gap_ms(self) -> float:
        """Current dynamic inter-request gap in milliseconds."""
        return self._current_gap * 1000

    @property
    def median_rtt_ms(self) -> float | None:
        """Rolling median RTT in milliseconds, or None if insufficient samples."""
        if len(self._rtt_samples) < 3:
            return None
        return statistics.median(self._rtt_samples) * 1000

    # ── context manager ───────────────────────────────────────────────────────

    class _RequestContext:
        """Async context manager returned by ModbusGuard.request().

        Two non-nested except blocks ensure _queue_depth is decremented
        exactly once regardless of which phase raises (v2.12.1 fix).
        """

        def __init__(self, guard: "ModbusGuard") -> None:
            self._guard = guard
            self._request_start: float = 0.0

        async def __aenter__(self) -> None:
            guard = self._guard
            guard._queue_depth += 1

            # ── Phase 1: Acquire the lock with a timeout ───────────────────
            try:
                async with asyncio.timeout(QUEUE_WAIT_TIMEOUT.total_seconds()):
                    await guard._lock.acquire()
            except TimeoutError:
                guard._queue_depth -= 1
                _LOGGER.warning(
                    "ModbusGuard[%s]: timed out after %.0f s waiting for lock "
                    "(queue depth was %d before this waiter)",
                    guard.serial_number,
                    QUEUE_WAIT_TIMEOUT.total_seconds(),
                    guard._queue_depth + 1,
                )
                raise

            # Lock is now held.
            # ── Phase 2: Enforce dynamic inter-request gap ─────────────────
            try:
                now = time.monotonic()
                gap = guard._current_gap
                elapsed = now - guard._last_request_end
                if elapsed < gap:
                    wait = gap - elapsed
                    _LOGGER.debug(
                        "ModbusGuard[%s]: inter-request pause %.0f ms (gap=%.0f ms)",
                        guard.serial_number,
                        wait * 1000,
                        gap * 1000,
                    )
                    await asyncio.sleep(wait)
                self._request_start = time.monotonic()
            except BaseException:
                guard._queue_depth -= 1
                guard._lock.release()
                raise

        async def __aexit__(self, exc_type: object, *_: object) -> None:
            guard = self._guard
            now = time.monotonic()
            guard._last_request_end = now

            # Record RTT for successful requests only
            if exc_type is None and self._request_start > 0:
                rtt = now - self._request_start
                guard.record_rtt(rtt)

            guard._queue_depth -= 1
            guard._lock.release()

    def request(self) -> "_RequestContext":
        """Return an async context manager that serialises Modbus access."""
        return self._RequestContext(self)

    # ── diagnostics ───────────────────────────────────────────────────────────

    @property
    def queue_depth(self) -> int:
        """Number of callers currently waiting for the lock."""
        return self._queue_depth

    @property
    def is_busy(self) -> bool:
        """True when a request is currently in-flight."""
        return self._lock.locked()
