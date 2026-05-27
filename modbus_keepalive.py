"""Modbus connection keep-alive and health probe for Huawei Solar.

The SUN2000 inverter silently drops idle TCP connections after approximately
60 seconds of inactivity.  During night mode (5-minute poll interval) this
means every poll cycle begins with a dead socket, causing:

  1. The first batch_update() to time out after the full effective_timeout
     (35–90 s), burning the entire timeout budget on a dead-connection
     discovery rather than a live inverter response.
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
      c. Waits RECONNECT_PAUSE seconds, then reads again as a reconnect check.
      d. Calls ``on_connection_restored()`` when a read succeeds post-failure.

The keep-alive task participates in the shared bus guard with ``priority=True``
so it is never blocked by load-shedding — a priority request bypasses the
queue-depth check but still waits for the lock and respects the inter-request
gap, ensuring it never collides with an in-flight coordinator request.

Architecture
------------
ModbusKeepAlive  — one instance per inverter device.
                   Created in __init__.py alongside the adaptive controller.
                   ``await keepalive.start()``  — begins the background task.
                   ``keepalive.stop()``         — cancels it on unload.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Callable

from huawei_solar import HuaweiSolarException, RegisterName
from huawei_solar.device.base import HuaweiSolarDevice

from .const import KEEPALIVE_INTERVAL, KEEPALIVE_REGISTER
from .modbus_guard import ModbusGuard

if TYPE_CHECKING:
    pass

_LOGGER = logging.getLogger(__name__)

# After a health-probe failure, pause this long before the reconnect check
_RECONNECT_PAUSE = timedelta(seconds=10)

# Timeout for a single keep-alive read (must be shorter than KEEPALIVE_INTERVAL)
_KEEPALIVE_TIMEOUT = timedelta(seconds=20)


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
        self._task = asyncio.ensure_future(self._run())
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
        """Read a single static register via the priority queue slot."""
        try:
            async with self._guard.request(priority=True):
                async with asyncio.timeout(_KEEPALIVE_TIMEOUT.total_seconds()):
                    # batch_update with a single STATIC register costs ~one
                    # Modbus PDU and is effectively free from the inverter's
                    # CPU perspective.
                    await self._device.batch_update(
                        [RegisterName[KEEPALIVE_REGISTER]]
                    )

            # ── Success ───────────────────────────────────────────────────────
            self._last_ok = time.monotonic()
            if not self._healthy:
                _LOGGER.info(
                    "ModbusKeepAlive[%s]: connection restored (was down for %.0f s, "
                    "%d failure(s))",
                    self.serial_number,
                    time.monotonic() - self._last_ok,
                    self._failure_count,
                )
                self._healthy = True
                self._failure_count = 0
                self._on_connection_restored()
            else:
                _LOGGER.debug(
                    "ModbusKeepAlive[%s]: connection healthy (%.0f ms)",
                    self.serial_number,
                    (time.monotonic() - self._last_ok) * 1000,
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
                    "ModbusKeepAlive[%s]: still unreachable (failure #%d)",
                    self.serial_number, self._failure_count,
                )

    # ── properties ────────────────────────────────────────────────────────────

    @property
    def is_healthy(self) -> bool:
        return self._healthy

    @property
    def seconds_since_last_ok(self) -> float:
        return time.monotonic() - self._last_ok
