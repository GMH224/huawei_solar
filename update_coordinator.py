"""Specialized DataUpdateCoordinators for Huawei Solar entities.

Key improvements over the original (v2.10b → v2.11.0)
------------------------------------------------------
1. **ModbusGuard integration** — all Modbus traffic for a given inverter passes
   through a single asyncio lock with a mandatory inter-request gap (150 ms).
   This prevents the SUN2000 from seeing overlapping requests from multiple
   coordinators running simultaneously.

2. **Register cache** — a TTL-aware ``RegisterCache`` stores the last known
   value for every register.  On each poll cycle only *stale* registers are
   fetched.  Static registers (rated power, battery capacity, …) have a 5-minute
   TTL; regular registers inherit the coordinator's own poll interval.

3. **Exponential back-off with jitter** — after ``MAX_CONSECUTIVE_TIMEOUTS``
   consecutive failures the coordinator sleeps for an exponentially growing
   interval (10 s → 20 s → … → 120 s, ±10 % jitter) before attempting the
   next poll.  This keeps the Modbus bus quiet while the inverter recovers.

4. **Telemetry recording** — every request, failure, timeout, cache-hit, and
   skipped poll is forwarded to the ``ModbusTelemetry`` singleton so that the
   Modbus diagnostic sensors stay up to date.

5. **Cache invalidation on write** — ``invalidate_cache(name)`` lets number /
   select / switch entities invalidate a cached register immediately after a
   successful write, ensuring the next poll fetches the fresh value.

6. **ConnectionInterruptedException back-off** — when another device steals the
   single Modbus slot the coordinator now backs off for ``MODBUS_RETRY_BASE_WAIT``
   seconds instead of retrying immediately.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from itertools import chain
import logging
import math
import random
from typing import Any

from huawei_solar import (
    ConnectionInterruptedException,
    DecodeError,
    HuaweiSolarException,
    ReadException,
    RegisterName,
    Result,
    SUN2000Device,
)
from huawei_solar.device.base import HuaweiSolarDevice
from huawei_solar.files import OptimizerRealTimeData

from homeassistant.core import HomeAssistant
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    MAX_CONSECUTIVE_TIMEOUTS,
    MODBUS_RETRY_BASE_WAIT,
    MODBUS_RETRY_MAX_WAIT,
    OPTIMIZER_UPDATE_TIMEOUT,
    UPDATE_TIMEOUT,
)
from .modbus_guard import ModbusGuard
from .modbus_telemetry import ModbusTelemetry
from .register_cache import RegisterCache

_LOGGER = logging.getLogger(__name__)


# ── back-off helper ────────────────────────────────────────────────────────────

def _backoff_seconds(consecutive: int) -> float:
    """Exponential back-off with ±10 % jitter, capped at MODBUS_RETRY_MAX_WAIT.

    consecutive=1 → ~MODBUS_RETRY_BASE_WAIT (10 s)
    consecutive=2 → ~20 s
    consecutive=3 → ~40 s …  capped at 120 s
    """
    base = MODBUS_RETRY_BASE_WAIT.total_seconds()
    cap = MODBUS_RETRY_MAX_WAIT.total_seconds()
    delay = min(base * math.pow(2, consecutive - 1), cap)
    jitter = delay * 0.10 * (2 * random.random() - 1)   # ±10 %
    return max(0.0, delay + jitter)


# ──────────────────────────────────────────────────────────────────────────────
# Main coordinator
# ──────────────────────────────────────────────────────────────────────────────

class HuaweiSolarUpdateCoordinator(
    DataUpdateCoordinator[dict[RegisterName, Result[Any]]]
):
    """Optimised DataUpdateCoordinator for Huawei Solar entities.

    Attributes
    ----------
    device:
        The underlying ``HuaweiSolarDevice`` (SUN2000, EMMA, meter, …).
    update_timeout:
        Per-request timeout; defaults to ``UPDATE_TIMEOUT`` (35 s).
    cache:
        ``RegisterCache`` instance shared across this coordinator's polls.
    guard:
        ``ModbusGuard`` singleton for this inverter's serial number.
    telemetry:
        Optional ``ModbusTelemetry`` — attached after construction via
        ``attach_telemetry()``.
    """

    device: HuaweiSolarDevice

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        device: HuaweiSolarDevice,
        name: str,
        update_interval: timedelta | None = None,
        update_method: Callable[[], Awaitable[dict[RegisterName, Result[Any]]]]
        | None = None,
        request_refresh_debouncer: Debouncer | None = None,
        update_timeout: timedelta = UPDATE_TIMEOUT,
    ) -> None:
        """Create a HuaweiSolarUpdateCoordinator."""
        super().__init__(
            hass,
            logger,
            name=name,
            update_interval=update_interval,
            update_method=update_method,
            request_refresh_debouncer=request_refresh_debouncer,
        )
        self.device = device
        self.update_timeout = update_timeout
        self._poll_interval = update_interval or UPDATE_TIMEOUT

        # Modbus serialiser shared across all coordinators for this inverter
        self.guard = ModbusGuard.get_or_create(device.serial_number)

        # Register value cache (telemetry attached later via attach_telemetry)
        self.cache = RegisterCache()

        # Optional telemetry (set via attach_telemetry after __init__)
        self.telemetry: ModbusTelemetry | None = None

        # Back-off / failure tracking
        self._consecutive_timeouts: int = 0
        self._consecutive_failures: int = 0

    def attach_telemetry(self, telemetry: ModbusTelemetry) -> None:
        """Connect a ModbusTelemetry instance.  Called from __init__.py."""
        self.telemetry = telemetry
        self.cache = RegisterCache(telemetry)

    def invalidate_cache(self, name: RegisterName) -> None:
        """Invalidate a cached register after a write.  Called from number/select/switch."""
        self.cache.invalidate(name)

    # ── poll logic ─────────────────────────────────────────────────────────────

    async def _async_update_data(self) -> dict[RegisterName, Result[Any]]:
        """Execute one optimised poll cycle.

        Steps
        -----
        1. Collect the set of registers requested by active HA entities.
        2. Ask the cache which registers are stale (need a fresh read).
        3. If nothing is stale, skip the Modbus request entirely.
        4. Apply back-off sleep if many consecutive timeouts have occurred.
        5. Acquire the ModbusGuard lock (serialises against other coordinators).
        6. Fetch only the stale registers in one batch_update() call.
        7. Merge fresh results with cached values.
        8. Update the cache.
        9. Record telemetry.
        """
        # ── 1. Collect requested register names ───────────────────────────────
        all_names: list[RegisterName] = list(
            set(
                chain.from_iterable(
                    ctx["register_names"] for ctx in self.async_contexts()
                )
            )
        )

        if not all_names:
            return {}

        # ── 2. Cache filtering — skip registers with fresh values ─────────────
        stale_names = self.cache.filter_stale(all_names, self._poll_interval)

        # ── 3. Nothing stale → return fully cached response ───────────────────
        if not stale_names:
            _LOGGER.debug(
                "%s: all %d register(s) served from cache — no Modbus request",
                self.name,
                len(all_names),
            )
            if self.telemetry:
                self.telemetry.record_skipped_poll()
            # Rebuild a complete result dict from the cache
            merged: dict[RegisterName, Result[Any]] = {}
            for n in all_names:
                val = self.cache.get(n)
                if val is not None:
                    merged[n] = val
            return merged

        # ── 4. Proactive back-off if many consecutive timeouts ────────────────
        if self._consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
            wait = _backoff_seconds(
                self._consecutive_timeouts - MAX_CONSECUTIVE_TIMEOUTS + 1
            )
            _LOGGER.debug(
                "%s: back-off %.1f s after %d consecutive timeout(s)",
                self.name,
                wait,
                self._consecutive_timeouts,
            )
            await asyncio.sleep(wait)

        # ── 5–6. Acquire guard + execute Modbus request ───────────────────────
        try:
            async with self.guard.request():
                async with asyncio.timeout(self.update_timeout.total_seconds()):
                    if self.telemetry:
                        self.telemetry.record_request(len(stale_names))
                    fresh = await self.device.batch_update(stale_names)

        except TimeoutError as err:
            self._consecutive_timeouts += 1
            self._consecutive_failures += 1
            if self.telemetry:
                self.telemetry.record_timeout()

            if self._consecutive_timeouts == 1:
                _LOGGER.warning(
                    "%s: Modbus timeout (no response in %.0f s). "
                    "Back-off activates after %d consecutive timeouts.",
                    self.name,
                    self.update_timeout.total_seconds(),
                    MAX_CONSECUTIVE_TIMEOUTS,
                )
            else:
                _LOGGER.debug(
                    "%s: Modbus timeout #%d (back-off: %s)",
                    self.name,
                    self._consecutive_timeouts,
                    self._consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS,
                )

            retry_after = int(_backoff_seconds(max(1, self._consecutive_timeouts)))

            # Fall back to fully-cached data so HA entities stay available
            cached_fallback = self.cache.merge({}, all_names)
            if cached_fallback:
                _LOGGER.debug(
                    "%s: serving %d register(s) from stale cache after timeout",
                    self.name,
                    len(cached_fallback),
                )
                return cached_fallback

            raise UpdateFailed(
                f"Timeout communicating with {self.device.serial_number}: "
                f"no response in {self.update_timeout.total_seconds():.0f} s "
                f"(consecutive: {self._consecutive_timeouts})",
                retry_after=retry_after,
            ) from err

        except ReadException as err:
            self._consecutive_failures += 1
            if self.telemetry:
                self.telemetry.record_failure()
            if err.modbus_exception_code == 0x02:  # ILLEGAL_DATA_ADDRESS
                _LOGGER.error(
                    "%s: ILLEGAL_DATA_ADDRESS from inverter during batch update. "
                    "Disable sensors one-by-one (wait 30 s each) to find the culprit "
                    "and report it to the integration maintainers.",
                    self.device.serial_number,
                )
            raise UpdateFailed(
                f"Could not update {self.device.serial_number}: {err}"
            ) from err

        except ConnectionInterruptedException as err:
            self._consecutive_failures += 1
            if self.telemetry:
                self.telemetry.record_failure()
            _LOGGER.warning(
                "%s: connection interrupted — another device may have connected "
                "to the inverter (only one Modbus client supported).",
                self.device.serial_number,
            )
            raise UpdateFailed(
                f"Connection to {self.device.serial_number} interrupted.",
                retry_after=int(MODBUS_RETRY_BASE_WAIT.total_seconds()),
            ) from err

        except HuaweiSolarException as err:
            self._consecutive_failures += 1
            if self.telemetry:
                self.telemetry.record_failure()
            raise UpdateFailed(
                f"Could not update {self.device.serial_number}: {err}"
            ) from err

        # ── 7–9. Success: merge, update cache, reset counters, record telemetry ─
        if self._consecutive_timeouts > 0 or self._consecutive_failures > 0:
            _LOGGER.info(
                "%s: communication restored (was: %d timeout(s) / %d failure(s))",
                self.name,
                self._consecutive_timeouts,
                self._consecutive_failures,
            )
            # Invalidate the entire cache after an outage so next full poll is
            # guaranteed fresh.
            self.cache.invalidate_all()

        self._consecutive_timeouts = 0
        self._consecutive_failures = 0

        # Update cache with fresh results
        self.cache.update(fresh)

        # Build the complete merged result (fresh + still-valid cached)
        return self.cache.merge(fresh, all_names)


# ──────────────────────────────────────────────────────────────────────────────
# Optimizer coordinator
# ──────────────────────────────────────────────────────────────────────────────

class HuaweiSolarOptimizerUpdateCoordinator(
    DataUpdateCoordinator[dict[int, OptimizerRealTimeData]]
):
    """DataUpdateCoordinator for Huawei Solar optimizers.

    Optimizers are polled much less frequently (every 5 minutes) since the
    inverter only refreshes optimizer data every 5 minutes.  The guard is
    still used to serialise with any concurrent inverter coordinator request.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        device: SUN2000Device,
        optimizer_device_infos: dict[int, DeviceInfo],
        name: str,
        update_interval: timedelta | None = None,
        request_refresh_debouncer: Debouncer | None = None,
    ) -> None:
        """Create a HuaweiSolarOptimizerUpdateCoordinator."""
        super().__init__(
            hass,
            logger,
            name=name,
            update_interval=update_interval,
            request_refresh_debouncer=request_refresh_debouncer,
        )
        self.device = device
        self.optimizer_device_infos = optimizer_device_infos
        self.guard = ModbusGuard.get_or_create(device.serial_number)
        self.telemetry: ModbusTelemetry | None = None
        self._consecutive_timeouts: int = 0

    def attach_telemetry(self, telemetry: ModbusTelemetry) -> None:
        """Connect a ModbusTelemetry instance."""
        self.telemetry = telemetry

    async def _async_update_data(self) -> dict[int, OptimizerRealTimeData]:
        """Retrieve the latest optimizer history data."""
        if self._consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
            wait = _backoff_seconds(
                self._consecutive_timeouts - MAX_CONSECUTIVE_TIMEOUTS + 1
            )
            _LOGGER.debug(
                "Optimizer %s: back-off %.1f s after %d consecutive timeout(s)",
                self.device.serial_number,
                wait,
                self._consecutive_timeouts,
            )
            await asyncio.sleep(wait)

        try:
            async with self.guard.request():
                async with asyncio.timeout(OPTIMIZER_UPDATE_TIMEOUT.total_seconds()):
                    if self.telemetry:
                        self.telemetry.record_request(1)
                    result = await self.device.get_latest_optimizer_history_data()

        except TimeoutError as err:
            self._consecutive_timeouts += 1
            if self.telemetry:
                self.telemetry.record_timeout()
            if self._consecutive_timeouts == 1:
                _LOGGER.warning(
                    "Optimizer %s: Modbus timeout (attempt %d).",
                    self.device.serial_number,
                    self._consecutive_timeouts,
                )
            retry_after = int(_backoff_seconds(max(1, self._consecutive_timeouts)))
            raise UpdateFailed(
                f"Timeout from {self.device.serial_number} optimizers "
                f"(consecutive: {self._consecutive_timeouts})",
                retry_after=retry_after,
            ) from err

        except ConnectionInterruptedException as err:
            if self.telemetry:
                self.telemetry.record_failure()
            _LOGGER.warning(
                "Optimizer %s: connection interrupted.",
                self.device.serial_number,
            )
            raise UpdateFailed(
                f"Connection to {self.device.serial_number} interrupted.",
                retry_after=int(MODBUS_RETRY_BASE_WAIT.total_seconds()),
            ) from err

        except DecodeError as err:
            if self.telemetry:
                self.telemetry.record_failure()
            raise UpdateFailed(
                f"Could not decode optimizer data from {self.device.serial_number}: {err}.",
                retry_after=15 * 60,
            ) from err

        except HuaweiSolarException as err:
            if self.telemetry:
                self.telemetry.record_failure()
            raise UpdateFailed(
                f"Could not update {self.device.serial_number} optimizers: {err}"
            ) from err

        if self._consecutive_timeouts > 0:
            _LOGGER.info(
                "Optimizer %s: communication restored after %d timeout(s).",
                self.device.serial_number,
                self._consecutive_timeouts,
            )
        self._consecutive_timeouts = 0
        return result


# ──────────────────────────────────────────────────────────────────────────────
# Factory helper
# ──────────────────────────────────────────────────────────────────────────────

async def create_optimizer_update_coordinator(
    hass: HomeAssistant,
    device: SUN2000Device,
    optimizer_device_infos: dict[int, DeviceInfo],
    update_interval: timedelta | None,
) -> HuaweiSolarOptimizerUpdateCoordinator:
    """Create and perform the first refresh of an optimizer coordinator."""
    coordinator = HuaweiSolarOptimizerUpdateCoordinator(
        hass,
        _LOGGER,
        device=device,
        optimizer_device_infos=optimizer_device_infos,
        name=f"{device.serial_number}_optimizer_data_update_coordinator",
        update_interval=update_interval,
    )
    await coordinator.async_config_entry_first_refresh()
    return coordinator
