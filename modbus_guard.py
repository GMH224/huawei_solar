"""Modbus traffic guard for Huawei Solar — v2.13.0.

Implements Modbus performance ideas 1, 2, 4, 5, 7:

Idea 1  Coordinator merge   — callers with the same merge_key piggyback on an
                              in-flight result via asyncio.Event fan-out.
Idea 2  Dynamic guard gap   — inter-request pause adapts to measured RTT and
                              rolling failure rate: 80 ms (healthy) → 500 ms (recovery).
Idea 4  TCP keepalive       — 1-register ping every 4 min prevents silent
                              server-side TCP close.
Idea 5  Write coalescing    — rapid set() calls for the same register are
                              debounced (300 ms); only the final value is sent.
                              All concurrent callers share one value_holder list
                              so every waiter reads the most-recent value.
Idea 7  Priority queue      — urgent writes/FAST reads jump ahead of normal polls.
                              Implementation uses asyncio.Condition for the
                              priority wait (no busy-wait) and a separate
                              _request_lock for the gap + Modbus request duration,
                              so the two concerns are cleanly separated.

Design — two-primitive lock strategy
--------------------------------------
asyncio.Condition  — used only to park waiters until they are elected "next".
                     The Condition's underlying lock is held briefly during the
                     predicate check then released.  NOT held during the request.

_request_lock      — a plain asyncio.Lock that is acquired AFTER the Condition
                     signals "you are next".  Held for the full duration of
                     [gap sleep + Modbus request].  Released in __aexit__, after
                     which Condition.notify_all() wakes the next waiter.

This separation means:
  - The Condition lock is never held during I/O (no deadlock risk).
  - The request lock is never acquired without holding the Condition first
    (no priority inversion).
  - __aexit__ does not need to re-acquire the Condition to release the request
    lock, avoiding the "double-lock" pitfall.
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
GAP_MIN_MS: float = 80.0          # fastest gap at 0 % failure rate
GAP_DEFAULT_MS: float = 150.0     # initial / moderate load
GAP_STRESSED_MS: float = 300.0    # failure_rate ≥ STRESSED_THRESHOLD
GAP_RECOVERY_MS: float = 500.0    # one cycle after any timeout

STRESSED_THRESHOLD: float = 0.02  # 2 %
HEALTHY_THRESHOLD: float = 0.005  # 0.5 %
ROLLING_WINDOW: int = 30

# ── Keepalive ─────────────────────────────────────────────────────────────────
KEEPALIVE_INTERVAL = timedelta(minutes=4)

# ── Write coalescing ──────────────────────────────────────────────────────────
WRITE_COALESCE_MS: float = 300.0

# ── Priority queue ────────────────────────────────────────────────────────────
MAX_NORMAL_WAIT: float = 5.0  # seconds before NORMAL promoted to URGENT

# ── Queue wait limit ──────────────────────────────────────────────────────────
QUEUE_WAIT_TIMEOUT = timedelta(seconds=12)


class _Waiter:
    """A slot in the priority queue.  Identity-compared via ``is``."""
    __slots__ = ("urgent", "enqueued_at")

    def __init__(self, urgent: bool) -> None:
        self.urgent = urgent
        self.enqueued_at: float = time.monotonic()


class ModbusGuard:
    """Per-inverter Modbus traffic serialiser.

    All asyncio primitives are created lazily inside coroutines so they are
    always bound to the running event loop.  This avoids DeprecationWarning
    from ``asyncio.get_event_loop()`` (deprecated since Python 3.10, errors in
    3.12 / HA 2024.x) and the "Future attached to a different loop" errors
    that occur on HA config-entry reloads.
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

        # Lazy-initialised asyncio primitives (see _ensure_primitives)
        self._request_lock: asyncio.Lock | None = None
        self._condition: asyncio.Condition | None = None

        self._last_request_end: float = 0.0
        self._last_rtt_s: float = 0.015  # seed with 15 ms estimate

        # Priority queue (idea 7)
        self._waiters: list[_Waiter] = []

        # Rolling failure stats (idea 2)
        self._recent_results: list[bool] = []
        self._in_recovery: bool = False

        # Idea 1: merge tracking
        self._merge_key: str | None = None
        self._merge_event: asyncio.Event | None = None
        self._merge_error: BaseException | None = None

        # Idea 5: write coalescing
        # {register_name: (TimerHandle, shared_value_holder, asyncio.Event)}
        self._pending_writes: dict[str, tuple[Any, list[Any], asyncio.Event]] = {}

        # Idea 4: keepalive
        self._keepalive_task: asyncio.Task | None = None
        self._keepalive_fn: Callable[[], Coroutine[Any, Any, None]] | None = None

    # ── lazy primitive creation ────────────────────────────────────────────────

    def _ensure_primitives(self) -> tuple[asyncio.Lock, asyncio.Condition]:
        """Return (request_lock, condition), creating them if needed.

        Must be called from within a running event loop.
        The Condition uses its OWN internal lock (not _request_lock) so the
        two locks are completely independent.
        """
        if self._request_lock is None:
            self._request_lock = asyncio.Lock()
        if self._condition is None:
            self._condition = asyncio.Condition()  # own internal lock
        return self._request_lock, self._condition

    # ── dynamic gap (idea 2) ──────────────────────────────────────────────────

    def record_result(self, *, success: bool, rtt_s: float | None = None) -> None:
        """Record the outcome of a completed Modbus request."""
        self._recent_results.append(success)
        if len(self._recent_results) > ROLLING_WINDOW:
            self._recent_results.pop(0)
        if rtt_s is not None:
            # Exponential moving average
            self._last_rtt_s = 0.8 * self._last_rtt_s + 0.2 * rtt_s
        if not success:
            self._in_recovery = True

    def _compute_gap_s(self, *, consume_recovery: bool) -> float:
        """Return the inter-request gap in seconds.

        Parameters
        ----------
        consume_recovery:
            Pass ``True`` only immediately before making a real request.
            Pass ``False`` for read-only telemetry inspection so the
            recovery state is not consumed prematurely.
        """
        if self._in_recovery:
            if consume_recovery:
                self._in_recovery = False
            return GAP_RECOVERY_MS / 1000.0

        if not self._recent_results:
            return GAP_DEFAULT_MS / 1000.0

        failure_rate = 1.0 - sum(self._recent_results) / len(self._recent_results)

        if failure_rate >= STRESSED_THRESHOLD:
            gap = GAP_STRESSED_MS / 1000.0
        elif failure_rate <= HEALTHY_THRESHOLD:
            gap = GAP_MIN_MS / 1000.0
        else:
            t = (failure_rate - HEALTHY_THRESHOLD) / (STRESSED_THRESHOLD - HEALTHY_THRESHOLD)
            gap = (GAP_MIN_MS + t * (GAP_STRESSED_MS - GAP_MIN_MS)) / 1000.0

        _LOGGER.debug(
            "ModbusGuard[%s]: gap=%.0f ms  failure_rate=%.1f%%  rtt=%.0f ms",
            self.serial_number, gap * 1000, failure_rate * 100, self._last_rtt_s * 1000,
        )
        return gap

    # ── priority queue helpers (idea 7) ───────────────────────────────────────

    def _elect_next(self) -> _Waiter | None:
        """Return the highest-priority waiter (without removing it).

        Promotes stale NORMAL waiters to URGENT first (anti-starvation, capped
        at MAX_NORMAL_WAIT seconds), then returns the first URGENT entry.
        Falls back to the oldest entry when no URGENT waiter exists.
        """
        if not self._waiters:
            return None
        now = time.monotonic()
        for w in self._waiters:
            if not w.urgent and (now - w.enqueued_at) >= MAX_NORMAL_WAIT:
                w.urgent = True
        for w in self._waiters:
            if w.urgent:
                return w
        return self._waiters[0]

    # ── request context manager ───────────────────────────────────────────────

    class _RequestContext:
        """Async context manager that serialises Modbus access.

        Lifecycle
        ---------
        __aenter__:
          1. Check for coordinator-merge opportunity (idea 1).
          2. Add self to the priority queue and wait on the Condition until
             elected (idea 7).  Condition lock released immediately after.
          3. Acquire _request_lock (ensures only one request in-flight).
          4. Sleep the inter-request gap (idea 2).
          5. Set merge_key / merge_event for fan-out.

        __aexit__:
          6. Record RTT + success/failure.
          7. Signal any merge-waiters.
          8. Release _request_lock.
          9. Notify all Condition waiters so the next in queue can be elected.
        """

        def __init__(
            self,
            guard: "ModbusGuard",
            urgent: bool,
            merge_key: str | None,
        ) -> None:
            self._guard = guard
            self._urgent = urgent
            self._merge_key = merge_key
            self._waiter: _Waiter | None = None
            self._merged = False
            self._t_start: float = 0.0

        async def __aenter__(self) -> "ModbusGuard._RequestContext":
            guard = self._guard
            request_lock, cond = guard._ensure_primitives()

            # ── 1. Coordinator merge (idea 1) ─────────────────────────────────
            if (
                self._merge_key
                and request_lock.locked()
                and guard._merge_key == self._merge_key
                and guard._merge_event is not None
            ):
                merge_event = guard._merge_event
                try:
                    async with asyncio.timeout(QUEUE_WAIT_TIMEOUT.total_seconds()):
                        await merge_event.wait()
                    if guard._merge_error is not None:
                        raise guard._merge_error  # type: ignore[misc]
                    self._merged = True
                    return self
                except asyncio.TimeoutError:
                    _LOGGER.debug(
                        "ModbusGuard[%s]: merge wait timed out, joining queue",
                        guard.serial_number,
                    )
                    # Fall through to normal queue path

            # ── 2. Priority queue wait (idea 7) ───────────────────────────────
            waiter = _Waiter(urgent=self._urgent)
            self._waiter = waiter

            try:
                async with asyncio.timeout(QUEUE_WAIT_TIMEOUT.total_seconds()):
                    async with cond:
                        guard._waiters.append(waiter)
                        try:
                            # Park until we are elected AND _request_lock is free
                            await cond.wait_for(
                                lambda: (
                                    guard._elect_next() is waiter
                                    and not request_lock.locked()
                                )
                            )
                            guard._waiters.remove(waiter)
                        except (asyncio.TimeoutError, asyncio.CancelledError):
                            try:
                                guard._waiters.remove(waiter)
                            except ValueError:
                                pass
                            raise
                    # Condition.__aexit__ released the Condition's lock.
                    # _request_lock is guaranteed free (predicate checked it).

            except asyncio.TimeoutError:
                raise

            # ── 3. Acquire request lock ───────────────────────────────────────
            # Non-blocking because the predicate confirmed it is free.
            # If another coroutine sneaks in between wait_for and here (impossible
            # in asyncio's single-threaded model but guarded for safety):
            await request_lock.acquire()

            # ── 4. Inter-request gap (idea 2) ─────────────────────────────────
            gap = guard._compute_gap_s(consume_recovery=True)
            elapsed = time.monotonic() - guard._last_request_end
            if elapsed < gap:
                await asyncio.sleep(gap - elapsed)

            # ── 5. Set merge context for fan-out ──────────────────────────────
            guard._merge_key = self._merge_key
            guard._merge_event = asyncio.Event()
            guard._merge_error = None
            self._t_start = time.monotonic()
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            guard = self._guard
            if self._merged:
                return

            # ── 6. Record RTT + result ────────────────────────────────────────
            rtt = time.monotonic() - self._t_start
            guard._last_request_end = time.monotonic()
            guard.record_result(success=(exc_type is None), rtt_s=rtt)

            # ── 7. Signal merge-waiters ───────────────────────────────────────
            if guard._merge_event is not None:
                if exc_type is not None and exc is not None:
                    guard._merge_error = exc
                guard._merge_event.set()
            guard._merge_key = None

            # ── 8. Release request lock ───────────────────────────────────────
            _, cond = guard._ensure_primitives()
            guard._request_lock.release()  # type: ignore[union-attr]

            # ── 9. Wake next waiter ───────────────────────────────────────────
            async with cond:
                cond.notify_all()

        @property
        def was_merged(self) -> bool:
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
            If True, placed at front of priority queue (user writes, FAST reads).
        merge_key:
            If set and a request with the same key is in-flight, this caller
            waits for its result instead of issuing a new request.
        """
        return self._RequestContext(self, urgent=urgent, merge_key=merge_key)

    # ── write coalescing (idea 5) ─────────────────────────────────────────────

    def coalesced_write(
        self,
        register_name: str,
        value: Any,
    ) -> tuple[asyncio.Event, list[Any]]:
        """Buffer a write; return (event, shared_value_holder).

        All concurrent callers for the *same* register receive a reference to
        the *same* ``value_holder`` list.  Each subsequent write updates
        ``value_holder[0]`` in-place, so every waiter automatically reads the
        most-recent value when the event fires — no stale intermediate values.

        Pattern::

            event, holder = guard.coalesced_write(str(reg), my_value)
            await event.wait()
            final = holder[0]   # always the most-recently submitted value
        """
        loop = asyncio.get_running_loop()

        if register_name in self._pending_writes:
            old_handle, value_holder, event = self._pending_writes[register_name]
            old_handle.cancel()
            value_holder[0] = value   # update shared holder in-place
        else:
            value_holder = [value]
            event = asyncio.Event()

        def _fire() -> None:
            event.set()
            self._pending_writes.pop(register_name, None)

        handle = loop.call_later(WRITE_COALESCE_MS / 1000.0, _fire)
        self._pending_writes[register_name] = (handle, value_holder, event)
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
        """Register a coroutine factory to ping the inverter when idle."""
        self._keepalive_fn = ping_fn
        if self._keepalive_task is None or self._keepalive_task.done():
            loop = asyncio.get_running_loop()
            self._keepalive_task = loop.create_task(
                self._keepalive_loop(),
                name=f"huawei_solar_keepalive_{self.serial_number}",
            )

    async def _keepalive_loop(self) -> None:
        interval = KEEPALIVE_INTERVAL.total_seconds()
        while True:
            await asyncio.sleep(interval)
            idle = time.monotonic() - self._last_request_end
            if idle >= interval and self._keepalive_fn is not None:
                _LOGGER.debug(
                    "ModbusGuard[%s]: keepalive ping (idle %.0f s)",
                    self.serial_number, idle,
                )
                try:
                    await self._keepalive_fn()
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug(
                        "ModbusGuard[%s]: keepalive failed: %s",
                        self.serial_number, exc,
                    )

    def _stop(self) -> None:
        """Cancel background tasks and flush pending writes."""
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        for handle, _, event in list(self._pending_writes.values()):
            handle.cancel()
            event.set()
        self._pending_writes.clear()

    # ── properties ────────────────────────────────────────────────────────────

    @property
    def queue_depth(self) -> int:
        return len(self._waiters)

    @property
    def is_busy(self) -> bool:
        lock = self._request_lock
        return lock is not None and lock.locked()

    @property
    def current_gap_ms(self) -> float:
        """Current inter-request gap in ms (read-only; does not consume recovery state)."""
        return self._compute_gap_s(consume_recovery=False) * 1000.0

    @property
    def failure_rate(self) -> float:
        if not self._recent_results:
            return 0.0
        return 1.0 - sum(self._recent_results) / len(self._recent_results)
