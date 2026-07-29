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
from collections import deque
from datetime import timedelta
from typing import Any

_LOGGER = logging.getLogger(__name__)

MIN_INTER_REQUEST_GAP = timedelta(milliseconds=150)
QUEUE_WAIT_TIMEOUT = timedelta(seconds=10)


class ModbusQueueShed(asyncio.TimeoutError):
    """Raised when a request is shed because the guard's queue is full.

    DEFECT D (v1.2.3) — this used to be a bare ``asyncio.TimeoutError``, which
    the coordinator's error handling could not distinguish from an inverter
    that failed to answer.  A shed request therefore reached the adaptive
    learner as ``record_request(success=False, timeout=True)`` — i.e. purely
    internal contention between our own sub-coordinators was recorded as
    *inverter* misbehaviour, in the same circadian slot model that drives poll
    interval, gap and timeout.

    Why that mattered more than it looks: shedding gets *more* likely as
    ``max_queue_depth`` drops, and ``max_queue_depth`` drops as the failure
    rate rises.  Recording sheds as failures therefore closed a positive
    feedback loop — shed → recorded failure → higher failure rate → lower
    queue depth → more shedding — which would have been triggered by the very
    cold-start blending introduced to make unproven slots *safer*.

    Subclassing ``asyncio.TimeoutError`` preserves every existing
    ``except asyncio.TimeoutError`` path (back-off, cache fallback, entity
    availability); only the adaptive-learning bookkeeping treats it specially.
    """
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

    @classmethod
    def remove(cls, endpoint: str) -> None:
        """Remove a single guard from the registry (per-entry unload).

        Unlike clear_registry(), this does not disturb guards belonging to
        other still-loaded config entries that share the registry.
        """
        cls._registry.pop(endpoint, None)

    # ── instance ──────────────────────────────────────────────────────────────

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self._lock = asyncio.Lock()
        self._last_request_end: float = 0.0
        self._queue_depth: int = 0
        self._effective_gap: float = MIN_INTER_REQUEST_GAP.total_seconds()
        self._max_queue_depth: int = MAX_QUEUE_DEPTH
        #: Diagnostic counter (v1.2.3): how many requests this guard has shed.
        #: Exposed so internal contention is observable rather than silently
        #: mixed into the inverter's failure statistics.
        self.shed_count: int = 0
        # ── v1.3.0 Phase 0 instrumentation ───────────────────────────────────
        #: Wall-clock time this guard has spent holding the line, and the
        #: window it was measured over. Occupancy = busy / elapsed is the
        #: FEEDFORWARD signal the scheduler will eventually pace from: unlike
        #: failure rate it leads the problem instead of lagging it, and unlike
        #: request count it reflects how long the line is actually tied up.
        self._busy_s: float = 0.0
        self._window_start: float = time.monotonic()
        #: Rolling separation of the two costs. This distinction is the whole
        #: point of Phase 0: long WAIT means requests queue behind each other
        #: (our scheduling); long SERVICE means the device itself is slow
        #: (the master's CPU). The field data cannot tell these apart.
        self._wait_samples: deque[float] = deque(maxlen=256)
        self._service_samples: deque[float] = deque(maxlen=256)
        #: Optional sink for per-request records (bus_diagnostics.BusDiagnostics).
        self.diagnostics: Any | None = None

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
        def __init__(
            self,
            guard: "ModbusGuard",
            priority: bool = False,
            label: str = "",
        ) -> None:
            self._guard = guard
            self._priority = priority  # keep-alive uses priority=True, bypasses shedding
            self._label = label        # who asked, for diagnostics attribution
            #: Per-request detail filled in by the CALLER after admission
            #: (v1.3.0 fix). The first field capture wrote these as null
            #: because the fields existed but nothing populated them — so the
            #: capture could not correlate stall duration with what was
            #: actually being read, which is the whole next question.
            self.registers: int | None = None
            self.priority_tier: str | None = None
            self._t_submit: float = 0.0
            self._t_admitted: float = 0.0
            #: Time spent waiting for admission (lock + inter-request gap).
            self.wait_ms: float = 0.0

        async def __aenter__(self) -> "ModbusGuard._RequestContext":
            guard = self._guard
            self._t_submit = time.monotonic()

            if not self._priority and guard._queue_depth >= guard._max_queue_depth:
                _LOGGER.debug(
                    "ModbusGuard[%s]: queue full (%d/%d) — shedding request",
                    guard.endpoint, guard._queue_depth, guard._max_queue_depth,
                )
                guard.shed_count += 1
                raise ModbusQueueShed(
                    f"ModbusGuard[{guard.endpoint}] queue full "
                    f"({guard._queue_depth}/{guard._max_queue_depth})"
                )

            guard._queue_depth += 1
            lock_acquired = False
            try:
                async with asyncio.timeout(QUEUE_WAIT_TIMEOUT.total_seconds()):
                    await guard._lock.acquire()
                lock_acquired = True

                now = time.monotonic()
                elapsed = now - guard._last_request_end
                if elapsed < guard._effective_gap:
                    wait = guard._effective_gap - elapsed
                    _LOGGER.debug(
                        "ModbusGuard[%s]: inter-request pause %.0f ms",
                        guard.endpoint, wait * 1000,
                    )
                    await asyncio.sleep(wait)

                self._t_admitted = time.monotonic()
                self.wait_ms = (self._t_admitted - self._t_submit) * 1000
                guard._wait_samples.append(self.wait_ms)

            except BaseException:
                # MUST be BaseException, not Exception: asyncio.CancelledError is a
                # BaseException.  A cancellation during lock.acquire() or during the
                # inter-request gap sleep (after the lock was granted) would
                # otherwise leak both the queue counter and — fatally — the lock,
                # deadlocking the entire Modbus bus until HA is restarted.
                guard._queue_depth -= 1
                if lock_acquired:
                    guard._lock.release()
                raise
            return self

        async def __aexit__(self, exc_type, *_: object) -> None:
            guard = self._guard
            now = time.monotonic()
            service_ms = (now - self._t_admitted) * 1000 if self._t_admitted else 0.0
            guard._last_request_end = now
            guard._queue_depth -= 1
            guard._lock.release()

            # Occupancy accounting: only time actually holding the line counts.
            if self._t_admitted:
                guard._busy_s += (now - self._t_admitted)
                guard._service_samples.append(service_ms)

            sink = guard.diagnostics
            if sink is not None:
                try:
                    sink.record(
                        endpoint=guard.endpoint,
                        label=self._label,
                        wait_ms=self.wait_ms,
                        service_ms=service_ms,
                        queue_depth=guard._queue_depth,
                        outcome="error" if exc_type else "ok",
                        registers=self.registers,
                        priority=self.priority_tier,
                    )
                except Exception:  # noqa: BLE001 — diagnostics must never break I/O
                    _LOGGER.exception("ModbusGuard[%s]: diagnostics sink failed",
                                      guard.endpoint)

    def request(
        self, priority: bool = False, label: str = ""
    ) -> "_RequestContext":
        """Return an async context manager that serialises Modbus access.

        priority=True is used by the keep-alive task to bypass queue-depth
        shedding — the keep-alive probe must always be able to run regardless
        of how many coordinators are waiting.
        """
        return self._RequestContext(self, priority=priority, label=label)

    def occupancy(self, reset: bool = False) -> float:
        """Fraction of wall-clock time this guard has held the line (0.0-1.0).

        The feedforward signal for the future scheduler. Unlike failure rate it
        LEADS the problem: occupancy rises before queues build, whereas a
        failure has already cost a timeout by the time it is recorded.
        """
        now = time.monotonic()
        elapsed = now - self._window_start
        if elapsed <= 0:
            return 0.0
        value = min(1.0, self._busy_s / elapsed)
        if reset:
            self._busy_s = 0.0
            self._window_start = now
        return value

    def wait_service_split(self) -> tuple[float, float]:
        """Return (p95 wait ms, p95 service ms) over the rolling window.

        THE Phase 0 measurement. Long wait => requests queue behind one another
        and a scheduler is the fix. Long service => the device itself is slow
        and only demand reduction helps. Three days of field data could not
        distinguish these, because nothing measured them separately.
        """
        def p95(samples: deque[float]) -> float:
            if not samples:
                return 0.0
            ordered = sorted(samples)
            return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        return p95(self._wait_samples), p95(self._service_samples)

    @property
    def queue_depth(self) -> int:
        return self._queue_depth

    @property
    def is_busy(self) -> bool:
        return self._lock.locked()

    @property
    def effective_gap_ms(self) -> float:
        return self._effective_gap * 1000
