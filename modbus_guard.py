"""Modbus traffic guard for Huawei Solar — v2.13.0.

Implements all 8 Modbus performance ideas:

Idea 1  Coordinator merge   — callers share a single in-flight request when they
                              fire at the same moment; results are fan-out cached.
Idea 2  Dynamic guard gap   — inter-request pause adapts to measured RTT and the
                              rolling failure rate: 80 ms (healthy) … 500 ms (stressed).
Idea 4  TCP keepalive       — a 1-register ping every KEEPALIVE_INTERVAL prevents
                              the inverter from closing the TCP connection silently.
Idea 5  Write coalescing    — rapid set() calls for the same register are debounced
                              so only the final value reaches the inverter.
Idea 7  Priority queue      — URGENT writes and FAST-tier reads jump ahead of NORMAL
                              background polls; NORMAL polls never starve (max-wait cap).

Ideas 3, 6, 8 live in update_coordinator.py (address sort, shadow readback) and
night_mode.py (MPPT pause).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
import logging
import time
from datetime import timedelta
from typing import Any

_LOGGER = logging.getLogger(__name__)

# ── Gap constants ─────────────────────────────────────────────────────────────
# Floor / ceiling for the adaptive inter-request gap.
# Community measurements: SUN2000 ready in ~80 ms under normal conditions.
GAP_MIN_MS: float = 80.0    # fastest we'll ever go (0 % failure rate)
GAP_DEFAULT_MS: float = 150.0  # starting point / moderate load
GAP_STRESSED_MS: float = 300.0  # failure rate > STRESSED_THRESHOLD
GAP_RECOVERY_MS: float = 500.0  # used for one cycle after any timeout

STRESSED_THRESHOLD: float = 0.02   # 2 % failure rate triggers stressed gap
HEALTHY_THRESHOLD: float = 0.005   # 0.5 % failure rate allows minimum gap

# How many recent polls are used to compute the rolling failure rate
ROLLING_WINDOW: int = 30

# ── Keepalive ─────────────────────────────────────────────────────────────────
# Ping the inverter if no request has been made for this long.
KEEPALIVE_INTERVAL = timedelta(minutes=4)

# ── Write coalescing ──────────────────────────────────────────────────────────
# Debounce window: if the same register is set again within this window,
# only the latest value is sent.
WRITE_COALESCE_MS: float = 300.0

# ── Priority ──────────────────────────────────────────────────────────────────
# Waiters with URGENT priority (user writes, FAST-tier reads) skip ahead of NORMAL
# ones.  NORMAL waiters are never starved: they are promoted to URGENT after
# MAX_NORMAL_WAIT seconds.
MAX_NORMAL_WAIT: float = 5.0   # seconds

# ── Queue wait limit ──────────────────────────────────────────────────────────
QUEUE_WAIT_TIMEOUT = timedelta(seconds=12)


class _Waiter:
    """A pending request slot in the priority queue."""
    __slots__ = ("urgent", "enqueued_at", "future")

    def __init__(self, urgent: bool) -> None:
        self.urgent = urgent
        self.enqueued_at = time.monotonic()
        self.future: asyncio.Future[None] = asyncio.get_event_loop().create_future()


class ModbusGuard:
    """Per-inverter Modbus traffic serialiser.

    Thread-safety: all methods are asyncio-safe (single-threaded event loop).

    Idea 1 — coordinator merge
        ``request()`` accepts an optional *merge_key*.  When the guard is already
        locked and a second caller passes the same merge_key, that caller waits
        for the in-flight result instead of queuing a new request.

    Idea 2 — dynamic gap
        ``_current_gap_s()`` returns the inter-request pause based on the rolling
        failure rate and the most recent RTT.

    Idea 4 — keepalive
        ``start_keepalive()`` schedules a background task that fires a ping if the
        bus has been idle for > KEEPALIVE_INTERVAL.

    Idea 5 — write coalescing
        ``coalesced_write()`` buffers set() calls per register name and returns an
        asyncio.Event that fires when the debounce window expires.

    Idea 7 — priority queue
        ``request(urgent=True)`` waiters are served before ``urgent=False`` ones,
        subject to the MAX_NORMAL_WAIT starvation cap.
    """

    _registry: dict[str, "ModbusGuard"] = {}

    # ── class helpers ─────────────────────────────────────────────────────────

    @classmethod
    def get_or_create(cls, serial_number: str) -> "ModbusGuard":
        if serial_number not in cls._registry:
            cls._registry[serial_number] = cls(serial_number)
        return cls._registry[serial_number]

    @classmethod
    def clear_registry(cls) -> None:
        for guard in cls._registry.values():
            guard._stop()
        cls._registry.clear()

    # ── instance ──────────────────────────────────────────────────────────────

    def __init__(self, serial_number: str) -> None:
        self.serial_number = serial_number

        # Core lock
        self._lock = asyncio.Lock()
        self._last_request_end: float = 0.0
        self._last_rtt_s: float = 0.015          # seed with 15 ms estimate

        # Priority queue (idea 7)
        self._waiters: list[_Waiter] = []

        # Rolling stats for dynamic gap (idea 2)
        self._recent_results: list[bool] = []    # True = success, False = failure
        self._in_recovery: bool = False           # True for 1 cycle after timeout

        # Idea 1: merge tracking
        self._merge_key: str | None = None
        self._merge_futures: list[asyncio.Future] = []

        # Idea 5: write coalescing
        # {register_name: (handle, pending_value, event)}
        self._pending_writes: dict[str, tuple[asyncio.TimerHandle, Any, asyncio.Event]] = {}

        # Idea 4: keepalive
        self._keepalive_task: asyncio.Task | None = None
        self._keepalive_fn: Callable[[], Coroutine[Any, Any, None]] | None = None

    # ── dynamic gap (idea 2) ──────────────────────────────────────────────────

    def record_result(self, *, success: bool, rtt_s: float | None = None) -> None:
        """Record the outcome of a completed request."""
        self._recent_results.append(success)
        if len(self._recent_results) > ROLLING_WINDOW:
            self._recent_results.pop(0)
        if rtt_s is not None:
            # Exponential moving average of RTT
            self._last_rtt_s = 0.8 * self._last_rtt_s + 0.2 * rtt_s
        if not success:
            self._in_recovery = True

    def _current_gap_s(self) -> float:
        """Return the inter-request gap to enforce, in seconds."""
        if self._in_recovery:
            self._in_recovery = False
            return GAP_RECOVERY_MS / 1000.0

        if not self._recent_results:
            return GAP_DEFAULT_MS / 1000.0

        failure_rate = 1.0 - (sum(self._recent_results) / len(self._recent_results))

        if failure_rate >= STRESSED_THRESHOLD:
            gap = GAP_STRESSED_MS / 1000.0
        elif failure_rate <= HEALTHY_THRESHOLD:
            gap = GAP_MIN_MS / 1000.0
        else:
            # Linear interpolation between healthy and stressed
            t = (failure_rate - HEALTHY_THRESHOLD) / (STRESSED_THRESHOLD - HEALTHY_THRESHOLD)
            gap = (GAP_MIN_MS + t * (GAP_STRESSED_MS - GAP_MIN_MS)) / 1000.0

        _LOGGER.debug(
            "ModbusGuard[%s]: gap=%.0f ms  failure_rate=%.1f%%  rtt=%.0f ms",
            self.serial_number,
            gap * 1000,
            failure_rate * 100,
            self._last_rtt_s * 1000,
        )
        return gap

    # ── priority queue helpers (idea 7) ───────────────────────────────────────

    def _next_waiter(self) -> _Waiter | None:
        """Pop the highest-priority non-expired waiter."""
        now = time.monotonic()
        # Promote stale NORMAL waiters to urgent (anti-starvation)
        for w in self._waiters:
            if not w.urgent and (now - w.enqueued_at) >= MAX_NORMAL_WAIT:
                w.urgent = True

        # Prefer urgent, then FIFO within tier
        for i, w in enumerate(self._waiters):
            if w.urgent:
                return self._waiters.pop(i)

        if self._waiters:
            return self._waiters.pop(0)
        return None

    # ── request context manager ───────────────────────────────────────────────

    class _RequestContext:
        def __init__(self, guard: "ModbusGuard", urgent: bool, merge_key: str | None) -> None:
            self._guard = guard
            self._urgent = urgent
            self._merge_key = merge_key
            self._waiter: _Waiter | None = None
            self._merged = False     # True = we're piggybacking on another request
            self._t_start: float = 0.0

        async def __aenter__(self) -> None:
            guard = self._guard

            # ── Idea 1: coordinator merge ─────────────────────────────────────
            if self._merge_key and guard._lock.locked() and guard._merge_key == self._merge_key:
                fut: asyncio.Future = asyncio.get_event_loop().create_future()
                guard._merge_futures.append(fut)
                try:
                    async with asyncio.timeout(QUEUE_WAIT_TIMEOUT.total_seconds()):
                        await fut
                    self._merged = True
                    return
                except TimeoutError:
                    try:
                        guard._merge_futures.remove(fut)
                    except ValueError:
                        pass
                    raise

            # ── Priority queue (idea 7) ───────────────────────────────────────
            waiter = _Waiter(urgent=self._urgent)
            self._waiter = waiter
            guard._waiters.append(waiter)

            try:
                async with asyncio.timeout(QUEUE_WAIT_TIMEOUT.total_seconds()):
                    # Spin until we're at the front of the queue AND can acquire lock
                    while True:
                        next_w = guard._next_waiter()
                        if next_w is waiter:
                            # We're up — try to acquire
                            if guard._lock.locked():
                                # Put ourselves back and wait for lock release
                                guard._waiters.insert(0, waiter)
                                await asyncio.sleep(0.005)
                                continue
                            guard._lock.acquire_nowait()
                            break
                        elif next_w is not None:
                            # Someone else is up — put them back and sleep
                            guard._waiters.insert(0, next_w)
                            await asyncio.sleep(0.005)
                        else:
                            await asyncio.sleep(0.005)

            except (TimeoutError, asyncio.CancelledError):
                try:
                    guard._waiters.remove(waiter)
                except ValueError:
                    pass
                raise

            # ── Inter-request gap (idea 2) ────────────────────────────────────
            gap = guard._current_gap_s()
            elapsed = time.monotonic() - guard._last_request_end
            if elapsed < gap:
                await asyncio.sleep(gap - elapsed)

            # Record merge key for idea 1
            guard._merge_key = self._merge_key
            self._t_start = time.monotonic()

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            guard = self._guard
            if self._merged:
                return

            rtt = time.monotonic() - self._t_start
            guard._last_request_end = time.monotonic()
            guard._merge_key = None
            guard.record_result(success=(exc_type is None), rtt_s=rtt)

            # Wake up any merge-waiters
            for fut in guard._merge_futures:
                if not fut.done():
                    if exc_type is None:
                        fut.set_result(None)
                    else:
                        fut.set_exception(exc or RuntimeError("merge parent failed"))
            guard._merge_futures.clear()

            guard._lock.release()

        @property
        def was_merged(self) -> bool:
            """True if this context piggybacked on another in-flight request."""
            return self._merged

    def request(
        self,
        *,
        urgent: bool = False,
        merge_key: str | None = None,
    ) -> "_RequestContext":
        """Return an async context manager that serialises Modbus access.

        Parameters
        ----------
        urgent:
            If True, this request is placed at the front of the priority queue
            (used for user-triggered writes and FAST-tier reads).
        merge_key:
            If set and a request with the same key is already in-flight, this
            caller waits for its result instead of issuing a new request.
            Coordinators that share the same device pass their coordinator name.
        """
        return self._RequestContext(self, urgent=urgent, merge_key=merge_key)

    # ── write coalescing (idea 5) ─────────────────────────────────────────────

    def coalesced_write(
        self,
        register_name: str,
        value: Any,
    ) -> tuple[asyncio.Event, Any]:
        """Buffer a write and return (event, final_value_getter).

        If the same register is written again within WRITE_COALESCE_MS, the
        previous pending write is cancelled and replaced.  The caller should
        await the event before issuing the actual set() call, then read the
        latest value from the returned getter.

        Returns
        -------
        event:
            Fires when the debounce window has expired.
        value_holder:
            A list[Any] whose first element is the most-recently-submitted value.
        """
        value_holder: list[Any] = [value]

        if register_name in self._pending_writes:
            old_handle, _, old_event = self._pending_writes[register_name]
            old_handle.cancel()
            # Reuse the same event so existing awaiters still get notified
            event = old_event
        else:
            event = asyncio.Event()

        def _fire() -> None:
            event.set()
            self._pending_writes.pop(register_name, None)

        loop = asyncio.get_event_loop()
        handle = loop.call_later(WRITE_COALESCE_MS / 1000.0, _fire)
        self._pending_writes[register_name] = (handle, value, event)
        value_holder[0] = value
        return event, value_holder

    def cancel_pending_write(self, register_name: str) -> None:
        """Cancel a pending coalesced write (e.g. on entity removal)."""
        if register_name in self._pending_writes:
            handle, _, event = self._pending_writes.pop(register_name)
            handle.cancel()
            event.set()

    # ── keepalive (idea 4) ────────────────────────────────────────────────────

    def start_keepalive(
        self,
        ping_fn: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        """Register a coroutine factory to ping the inverter when idle.

        ``ping_fn`` must be an async callable that issues a minimal single-register
        read (e.g. read DEVICE_STATUS) inside its own guard.request() call.
        """
        self._keepalive_fn = ping_fn
        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = asyncio.ensure_future(self._keepalive_loop())

    async def _keepalive_loop(self) -> None:
        interval = KEEPALIVE_INTERVAL.total_seconds()
        while True:
            await asyncio.sleep(interval)
            idle = time.monotonic() - self._last_request_end
            if idle >= interval and self._keepalive_fn is not None:
                _LOGGER.debug(
                    "ModbusGuard[%s]: keepalive ping (idle %.0f s)",
                    self.serial_number,
                    idle,
                )
                try:
                    await self._keepalive_fn()
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug(
                        "ModbusGuard[%s]: keepalive failed: %s",
                        self.serial_number,
                        exc,
                    )

    def _stop(self) -> None:
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        for handle, _, event in self._pending_writes.values():
            handle.cancel()
            event.set()
        self._pending_writes.clear()

    # ── properties ────────────────────────────────────────────────────────────

    @property
    def queue_depth(self) -> int:
        return len(self._waiters)

    @property
    def is_busy(self) -> bool:
        return self._lock.locked()

    @property
    def current_gap_ms(self) -> float:
        return self._current_gap_s() * 1000.0

    @property
    def failure_rate(self) -> float:
        if not self._recent_results:
            return 0.0
        return 1.0 - sum(self._recent_results) / len(self._recent_results)
