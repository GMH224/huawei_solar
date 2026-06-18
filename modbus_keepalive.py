"""Modbus connection keep-alive and health probe for Huawei Solar.

The SUN2000 inverter silently drops idle TCP connections after approximately
60 seconds of inactivity.  During night mode (5-minute poll interval) this
means every poll cycle begins with a dead socket, causing:

  1. The first batch_update() to time out after the full effective_timeout
     (15–60 s), burning the entire timeout budget on dead-connection discovery
     rather than a live inverter response.
  2. The resulting timeout being counted as a Modbus failure, incorrectly
     training the adaptive controller's night-mode slots.
  3. Post-reconnect re-authentication overhead (if parameter configuration
     is enabled).

ModbusKeepAlive solves both problems with a single background task:

  Keep-alive
    Reads a single 1-register static value (model_id by default) every 45 s
    regardless of whether a coordinator poll is due.  This keeps the TCP
    session alive through night-mode intervals and periods of low activity.

  Health probe
    If the keep-alive read fails (connection dropped, inverter rebooted,
    network blip), the task immediately:
      a. Logs a warning.
      b. Calls ``on_connection_lost()`` so coordinators can mark their
         caches dirty and prepare for a fresh read.
      c. On the next probe cycle, reads again as a reconnect check.
      d. Calls ``on_connection_restored()`` when a read succeeds post-failure.

The keep-alive task participates in the shared bus guard with ``priority=True``
so it is never blocked by load-shedding — a priority request bypasses the
queue-depth check but still waits for the lock and respects the inter-request
gap, ensuring it never collides with an in-flight coordinator request.

Bugs fixed in v1.1.0
---------------------
BUG-1  Timing: ``self._last_ok`` was updated BEFORE computing
       ``time.monotonic() - self._last_ok`` in the success branch, so the
       "was down for" log always showed 0 s and "connection healthy" always
       showed 0 ms.  Fixed by capturing ``down_for`` / ``rtt_ms`` before
       updating ``_last_ok``.
BUG-2  ``asyncio.ensure_future()`` is deprecated since Python 3.10.  Fixed
       by using ``asyncio.get_event_loop().create_task()`` with a fallback to
       ``asyncio.ensure_future()`` for older runtimes.
BUG-9  ``RegisterName[KEEPALIVE_REGISTER]`` raised ``KeyError`` if the enum
       member name did not exist (e.g., library version mismatch).  Fixed with
       a try/except that falls back to skipping the probe rather than crashing
       the loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Callable

from huawei_solar import HuaweiSolarException, RegisterName
from huawei_solar.device.base import HuaweiSolarDevice

from .const import KEEPALIVE_INTERVAL, KEEPALIVE_REGISTER
from .modbus_guard import ModbusGuard

_LOGGER = logging.getLogger(__name__)

# Timeout for a single keep-alive read (must be shorter than KEEPALIVE_INTERVAL)
_KEEPALIVE_TIMEOUT = timedelta(seconds=20)


def _create_task(coro: object) -> asyncio.Task:
    """Create an asyncio task, compatible with Python 3.10+."""
    try:
        # Preferred API since Python 3.7; works on the running loop
        return asyncio.get_event_loop().create_task(coro)  # type: ignore[arg-type]
    except RuntimeError:
        # No running loop — fall back (tests, edge cases)
        return asyncio.ensure_future(coro)  # type: ignore[arg-type]


# Cache the keepalive RegisterName to avoid repeated enum lookups and to give
# a clear error at import time if the name is invalid.
_KEEPALIVE_REGISTER_NAME: RegisterName | None = None


def _get_keepalive_register() -> RegisterName | None:
    """Return the RegisterName for the keep-alive probe, or None on failure."""
    global _KEEPALIVE_REGISTER_NAME
    if _KEEPALIVE_REGISTER_NAME is not None:
        return _KEEPALIVE_REGISTER_NAME
    try:
        _KEEPALIVE_REGISTER_NAME = RegisterName[KEEPALIVE_REGISTER]
        return _KEEPALIVE_REGISTER_NAME
    except KeyError:
        _LOGGER.warning(
            "ModbusKeepAlive: KEEPALIVE_REGISTER '%s' is not a valid RegisterName "
            "member — keep-alive probes will be skipped.  "
            "Check the KEEPALIVE_REGISTER constant in const.py.",
            KEEPALIVE_REGISTER,
        )
        return None


class ModbusKeepAlive:
    """Per-inverter background keep-alive and connection health monitor."""

    _registry: dict[str, "ModbusKeepAlive"] = {}

    # ── class helpers ─────────────────────────────────────────────────────────

    @classmethod
    def get_or_create(
        cls,
        serial_number: str,
        device: HuaweiSolarDevice,
        guard: ModbusGuard,
        on_connection_lost: Callable[[], None],
        on_connection_restored: Callable[[], None],
    ) -> "ModbusKeepAlive":
        if serial_number not in cls._registry:
            cls._registry[serial_number] = cls(
                serial_number, device, guard,
                on_connection_lost, on_connection_restored,
            )
        return cls._registry[serial_number]

    @classmethod
    def get(cls, serial_number: str) -> "ModbusKeepAlive | None":
        return cls._registry.get(serial_number)

    @classmethod
    def remove(cls, serial_number: str) -> None:
        """Remove a single entry from the registry (per-entry unload).

        Unlike clear_registry(), this leaves singletons belonging to other
        still-loaded config entries intact.
        """
        cls._registry.pop(serial_number, None)

    @classmethod
    def clear_registry(cls) -> None:
        cls._registry.clear()

    # ── instance ──────────────────────────────────────────────────────────────

    def __init__(
        self,
        serial_number: str,
        device: HuaweiSolarDevice,
        guard: ModbusGuard,
        on_connection_lost: Callable[[], None],
        on_connection_restored: Callable[[], None],
    ) -> None:
        self.serial_number = serial_number
        self._device = device
        self._guard = guard
        self._on_connection_lost = on_connection_lost
        self._on_connection_restored = on_connection_restored

        self._task: asyncio.Task | None = None
        self._healthy: bool = True
        self._last_ok: float = time.monotonic()
        self._failure_count: int = 0

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background keep-alive task."""
        if self._task and not self._task.done():
            return
        self._task = _create_task(self._run())
        _LOGGER.debug(
            "ModbusKeepAlive[%s]: started (interval=%ds)",
            self.serial_number,
            KEEPALIVE_INTERVAL.total_seconds(),
        )

    def stop(self) -> None:
        """Cancel the background task (called on integration unload)."""
        if self._task and not self._task.done():
            self._task.cancel()
            self._task = None

    # ── background task ───────────────────────────────────────────────────────

    async def _run(self) -> None:
        """Main loop: sleep → probe → sleep → probe …"""
        while True:
            try:
                await asyncio.sleep(KEEPALIVE_INTERVAL.total_seconds())
                await self._probe()
            except asyncio.CancelledError:
                _LOGGER.debug("ModbusKeepAlive[%s]: stopped", self.serial_number)
                return
            except Exception as exc:  # noqa: BLE001
                # Never let an unhandled exception kill the loop
                _LOGGER.debug(
                    "ModbusKeepAlive[%s]: unexpected error in run loop: %s",
                    self.serial_number, exc,
                )

    async def _probe(self) -> None:
        """Read a single static register via the priority queue slot.

        BUG-1 FIX: ``down_for`` and ``rtt_ms`` are captured BEFORE
        ``self._last_ok`` is updated, so log messages show accurate durations.
        """
        register = _get_keepalive_register()
        if register is None:
            return  # BUG-9 FIX: skip probe if register name is invalid

        try:
            probe_start = time.monotonic()
            async with self._guard.request(priority=True):
                async with asyncio.timeout(_KEEPALIVE_TIMEOUT.total_seconds()):
                    await self._device.batch_update([register])
            rtt_ms = (time.monotonic() - probe_start) * 1000  # BUG-1 FIX: before _last_ok update

            # ── Success ───────────────────────────────────────────────────────
            if not self._healthy:
                # BUG-1 FIX: compute down_for BEFORE updating _last_ok
                down_for = time.monotonic() - self._last_ok
                self._last_ok = time.monotonic()
                _LOGGER.info(
                    "ModbusKeepAlive[%s]: connection restored "
                    "(was down for %.0f s, %d probe failure(s))",
                    self.serial_number, down_for, self._failure_count,
                )
                self._healthy = True
                self._failure_count = 0
                self._on_connection_restored()
            else:
                self._last_ok = time.monotonic()
                _LOGGER.debug(
                    "ModbusKeepAlive[%s]: connection healthy (RTT %.0f ms)",
                    self.serial_number, rtt_ms,  # BUG-1 FIX: use measured rtt_ms
                )

        except (TimeoutError, HuaweiSolarException, OSError) as exc:
            self._failure_count += 1
            if self._healthy:
                _LOGGER.warning(
                    "ModbusKeepAlive[%s]: connection appears dead (%s) — "
                    "coordinators will reconnect on next poll",
                    self.serial_number, exc,
                )
                self._healthy = False
                self._on_connection_lost()
            else:
                _LOGGER.debug(
                    "ModbusKeepAlive[%s]: still unreachable (probe failure #%d)",
                    self.serial_number, self._failure_count,
                )

    # ── properties ────────────────────────────────────────────────────────────

    @property
    def is_healthy(self) -> bool:
        return self._healthy

    @property
    def seconds_since_last_ok(self) -> float:
        return time.monotonic() - self._last_ok
