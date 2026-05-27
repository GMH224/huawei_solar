"""Modbus traffic guard for Huawei Solar.

The SUN2000 inverter's Modbus interface is single-threaded and will return
error codes (or silently drop responses) when two requests overlap or arrive
faster than it can handle.

ModbusGuard provides per-device serialisation and rate-limiting:
1. Only ONE Modbus request is in-flight at a time per inverter.
2. A minimum inter-request gap is respected between consecutive requests.
   The gap is adaptive: the AdaptiveModbusController sets it dynamically via
   ``update_gap()`` based on observed RTT and per-slot failure history.
3. Requests that arrive when ``_max_queue_depth`` callers are already waiting
   are shed immediately (fail-fast) rather than queuing.  The depth limit is
   also adaptive, reduced to 1 during high-failure windows or transitions.

Usage (in coordinators)
-----------------------
    guard = ModbusGuard.get_or_create(serial_number)
    guard.update_gap(params.request_gap.total_seconds())
    guard.update_max_queue_depth(params.max_queue_depth)

    async with guard.request():
        result = await device.batch_update(names)
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

_LOGGER = logging.getLogger(__name__)

# Default (non-adaptive) minimum pause between consecutive Modbus requests.
# The AdaptiveModbusController overrides this per-instance via update_gap().
MIN_INTER_REQUEST_GAP = timedelta(milliseconds=150)

# Maximum time a request may be queued before it is abandoned.
QUEUE_WAIT_TIMEOUT = timedelta(seconds=10)

# Default maximum number of concurrent waiters.
# The AdaptiveModbusController overrides this via update_max_queue_depth().
MAX_QUEUE_DEPTH = 3


class ModbusGuard:
    """Per-inverter asyncio serialiser and rate-limiter with adaptive parameters."""

    _registry: dict[str, "ModbusGuard"] = {}

    # ── class helpers ─────────────────────────────────────────────────────────

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
        self._last_request_end: float = 0.0
        self._queue_depth: int = 0

        # Adaptive parameters — updated by the coordinator each poll cycle
        self._effective_gap: float = MIN_INTER_REQUEST_GAP.total_seconds()
        self._max_queue_depth: int = MAX_QUEUE_DEPTH

    # ── adaptive parameter setters ────────────────────────────────────────────

    def update_gap(self, gap_seconds: float) -> None:
        """Set the inter-request gap from the adaptive controller.

        Clamped to [MIN_INTER_REQUEST_GAP, 500 ms] to prevent runaway values.
        """
        clamped = max(MIN_INTER_REQUEST_GAP.total_seconds(), min(gap_seconds, 0.500))
        self._effective_gap = clamped

    def update_max_queue_depth(self, depth: int) -> None:
        """Set the maximum queue depth from the adaptive controller.

        Clamped to [1, MAX_QUEUE_DEPTH].
        """
        self._max_queue_depth = max(1, min(depth, MAX_QUEUE_DEPTH))

    # ── context manager ───────────────────────────────────────────────────────

    class _RequestContext:
        """Async context manager returned by ModbusGuard.request()."""

        def __init__(self, guard: "ModbusGuard") -> None:
            self._guard = guard

        async def __aenter__(self) -> None:
            guard = self._guard

            # Load shedding: use the current (adaptive) max depth.
            if guard._queue_depth >= guard._max_queue_depth:
                _LOGGER.debug(
                    "ModbusGuard[%s]: queue full (%d/%d) — shedding request",
                    guard.serial_number,
                    guard._queue_depth,
                    guard._max_queue_depth,
                )
                raise asyncio.TimeoutError(
                    f"ModbusGuard[{guard.serial_number}] queue full "
                    f"({guard._queue_depth}/{guard._max_queue_depth})"
                )

            guard._queue_depth += 1
            try:
                async with asyncio.timeout(QUEUE_WAIT_TIMEOUT.total_seconds()):
                    await guard._lock.acquire()

                # Enforce adaptive inter-request gap
                now = time.monotonic()
                elapsed = now - guard._last_request_end
                if elapsed < guard._effective_gap:
                    wait = guard._effective_gap - elapsed
                    _LOGGER.debug(
                        "ModbusGuard[%s]: inter-request pause %.0f ms (adaptive)",
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
        return self._queue_depth

    @property
    def is_busy(self) -> bool:
        return self._lock.locked()

    @property
    def effective_gap_ms(self) -> float:
        """Current inter-request gap in milliseconds (for telemetry)."""
        return self._effective_gap * 1000
