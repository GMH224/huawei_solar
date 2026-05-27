"""Modbus traffic guard for Huawei Solar.

The SUN2000 inverter's Modbus interface is single-threaded and will return
error codes (or silently drop responses) when two requests overlap or arrive
faster than it can handle.

ModbusGuard provides per-bus serialisation and rate-limiting:
1. Only ONE Modbus request is in-flight at a time per physical RS485 bus.
2. A minimum inter-request gap is respected.  Adaptive via update_gap().
3. Requests that arrive when _max_queue_depth callers are already waiting
   are shed immediately (fail-fast).  Adaptive via update_max_queue_depth().

Bus-level keying (v1.0.5)
--------------------------
Guards are keyed on ``connection_endpoint`` (host:port string, or the serial
port path for RTU) rather than ``serial_number``.  All inverters sharing the
same physical RS485 wire (i.e., sub-devices created via
``create_sub_device_instance``) therefore share one guard, preventing concurrent
requests that would cause RS485 bus collisions and are the primary cause of the
secondary inverter's elevated failure rate.

Usage (in coordinators)
-----------------------
    endpoint = ModbusGuard.endpoint_for(entry.data)
    guard = ModbusGuard.get_or_create(endpoint)
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

MIN_INTER_REQUEST_GAP = timedelta(milliseconds=150)
QUEUE_WAIT_TIMEOUT = timedelta(seconds=10)
MAX_QUEUE_DEPTH = 3


class ModbusGuard:
    """Per-bus asyncio serialiser and rate-limiter with adaptive parameters."""

    # Key: connection_endpoint string → ModbusGuard instance
    _registry: dict[str, "ModbusGuard"] = {}

    # ── class helpers ─────────────────────────────────────────────────────────

    @classmethod
    def endpoint_for(cls, entry_data: dict) -> str:
        """Derive the connection-endpoint key from a config-entry data dict.

        For TCP connections: ``"host:port"``
        For RTU connections: ``"rtu:<port>"``

        This key is the same for all slave IDs on the same physical bus, so all
        sub-device coordinators get the same guard instance.
        """
        host = entry_data.get("host")
        port = entry_data.get("port", "502")
        if host is None:
            return f"rtu:{port}"
        return f"{host}:{port}"

    @classmethod
    def get_or_create(cls, endpoint: str) -> "ModbusGuard":
        if endpoint not in cls._registry:
            cls._registry[endpoint] = cls(endpoint)
        return cls._registry[endpoint]

    @classmethod
    def clear_registry(cls) -> None:
        cls._registry.clear()

    # ── instance ──────────────────────────────────────────────────────────────

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self._lock = asyncio.Lock()
        self._last_request_end: float = 0.0
        self._queue_depth: int = 0
        self._effective_gap: float = MIN_INTER_REQUEST_GAP.total_seconds()
        self._max_queue_depth: int = MAX_QUEUE_DEPTH

    # ── adaptive parameter setters ────────────────────────────────────────────

    def update_gap(self, gap_seconds: float) -> None:
        """Set the inter-request gap. Clamped to [150 ms, 500 ms].

        The 150 ms floor is a hardware constraint (SUN2000 Modbus FSM reset
        time ≈ 100 ms) and is never reduced regardless of network health.
        Gemini recommended 30 ms; this was rejected — it causes pervasive
        0x06 SLAVE_DEVICE_BUSY responses on all SUN2000 hardware.
        """
        self._effective_gap = max(
            MIN_INTER_REQUEST_GAP.total_seconds(), min(gap_seconds, 0.500)
        )

    def update_max_queue_depth(self, depth: int) -> None:
        """Set the maximum queue depth. Clamped to [1, MAX_QUEUE_DEPTH]."""
        self._max_queue_depth = max(1, min(depth, MAX_QUEUE_DEPTH))

    # ── context manager ───────────────────────────────────────────────────────

    class _RequestContext:
        def __init__(self, guard: "ModbusGuard", priority: bool = False) -> None:
            self._guard = guard
            self._priority = priority  # keep-alive uses priority=True, bypasses shedding

        async def __aenter__(self) -> None:
            guard = self._guard

            if not self._priority and guard._queue_depth >= guard._max_queue_depth:
                _LOGGER.debug(
                    "ModbusGuard[%s]: queue full (%d/%d) — shedding request",
                    guard.endpoint, guard._queue_depth, guard._max_queue_depth,
                )
                raise asyncio.TimeoutError(
                    f"ModbusGuard[{guard.endpoint}] queue full "
                    f"({guard._queue_depth}/{guard._max_queue_depth})"
                )

            guard._queue_depth += 1
            try:
                async with asyncio.timeout(QUEUE_WAIT_TIMEOUT.total_seconds()):
                    await guard._lock.acquire()

                now = time.monotonic()
                elapsed = now - guard._last_request_end
                if elapsed < guard._effective_gap:
                    wait = guard._effective_gap - elapsed
                    _LOGGER.debug(
                        "ModbusGuard[%s]: inter-request pause %.0f ms",
                        guard.endpoint, wait * 1000,
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

    def request(self, priority: bool = False) -> "_RequestContext":
        """Return an async context manager that serialises Modbus access.

        priority=True is used by the keep-alive task to bypass queue-depth
        shedding — the keep-alive probe must always be able to run regardless
        of how many coordinators are waiting.
        """
        return self._RequestContext(self, priority=priority)

    @property
    def queue_depth(self) -> int:
        return self._queue_depth

    @property
    def is_busy(self) -> bool:
        return self._lock.locked()

    @property
    def effective_gap_ms(self) -> float:
        return self._effective_gap * 1000
