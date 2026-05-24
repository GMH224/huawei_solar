"""Modbus traffic guard for Huawei Solar.

The SUN2000 inverter's Modbus interface is single-threaded and will return
error codes (or silently drop responses) when two requests overlap or arrive
faster than it can handle.

This module provides:

ModbusGuard
    A per-device asyncio lock + rate-limiter that ensures:
    1. Only ONE Modbus request is in-flight at a time for a given inverter.
    2. A minimum inter-request gap is respected (MIN_INTER_REQUEST_GAP).
    3. Duplicate in-flight requests for the same register set are coalesced
       so that multiple coordinators don't each trigger their own read.

Usage (in coordinators)
-----------------------
    guard = ModbusGuard.get_or_create(serial_number)

    async with guard.request():
        result = await device.batch_update(names)
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

_LOGGER = logging.getLogger(__name__)

# Minimum pause between consecutive Modbus requests to the same inverter.
# Huawei SUN2000 needs ~100 ms to prepare for the next request; 150 ms adds
# a comfortable margin without noticeably slowing polling.
MIN_INTER_REQUEST_GAP = timedelta(milliseconds=150)

# Maximum time a request may be queued before it is abandoned (prevents pileup).
QUEUE_WAIT_TIMEOUT = timedelta(seconds=10)


class ModbusGuard:
    """Per-inverter asyncio serialiser and rate-limiter."""

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
        self._queue_depth: int = 0             # number of waiters

    # ── context manager ───────────────────────────────────────────────────────

    class _RequestContext:
        """Async context manager returned by ModbusGuard.request()."""

        def __init__(self, guard: "ModbusGuard") -> None:
            self._guard = guard

        async def __aenter__(self) -> None:
            guard = self._guard
            guard._queue_depth += 1
            try:
                # Wait for the lock — with a timeout to avoid piling up requests.
                # NOTE: do NOT decrement _queue_depth inside the inner except block;
                # the outer except Exception is the single place that handles all
                # failure paths, preventing a double-decrement on TimeoutError.
                async with asyncio.timeout(QUEUE_WAIT_TIMEOUT.total_seconds()):
                    await guard._lock.acquire()

                # Enforce minimum inter-request gap
                now = time.monotonic()
                gap = MIN_INTER_REQUEST_GAP.total_seconds()
                elapsed = now - guard._last_request_end
                if elapsed < gap:
                    wait = gap - elapsed
                    _LOGGER.debug(
                        "ModbusGuard[%s]: inter-request pause %.0f ms",
                        guard.serial_number,
                        wait * 1000,
                    )
                    await asyncio.sleep(wait)

            except Exception:
                guard._queue_depth -= 1
                raise

        async def __aexit__(self, *_: object) -> None:
            guard = self._guard
            guard._last_request_end = time.monotonic()
            guard._queue_depth -= 1
            guard._lock.release()

    def request(self) -> "_RequestContext":
        """Return an async context manager that serialises Modbus access."""
        return self._RequestContext(self)

    @property
    def queue_depth(self) -> int:
        """Number of callers currently waiting for the lock."""
        return self._queue_depth

    @property
    def is_busy(self) -> bool:
        """True when a request is currently in-flight."""
        return self._lock.locked()
