"""Specialised DataUpdateCoordinators for Huawei Solar entities.

Optimisation history
--------------------
v2.10b  Exponential back-off, tiered logging, retry_after hints.
v2.11.0 ModbusGuard (serialise + 150 ms gap), RegisterCache (static TTL),
        ModbusTelemetry (10 diagnostic sensors), cache invalidation on write.
v2.12.0 Adaptive TTL (RegisterTier — STATIC/SLOW/NORMAL/FAST), night-mode
        detection, stale-cache fallback, poll-interval dynamic adjustment.
v1.0.2  ModbusGuard load shedding, lru_cache on _classify(), batched
        telemetry cache-hit recording, invalidate_all() skips STATIC tier.
v1.0.3  Energy-counter stale-cache exclusion, coordinator start-time jitter,
        contiguous register sorting, set_telemetry() on RegisterCache.
v1.0.4  Full circadian adaptive learning: AdaptiveModbusController drives
        poll_interval, request_gap, request_timeout and max_queue_depth from
        15-minute time-slot statistics that persist across HA restarts.
        RTT is measured per batch_update() call and fed back to the controller.
        State transitions (day↔night, battery reversal) trigger a transition
        window of maximum-tolerance parameters regardless of learned history.
        10 new diagnostic sensor entities expose the controller's state.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from itertools import chain
import logging
import math
import random
import time
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

from .adaptive_modbus import AdaptiveModbusController
from .const import (
    ADAPTIVE_POLL_MAX,
    ADAPTIVE_POLL_MIN,
    MAX_CONSECUTIVE_TIMEOUTS,
    MODBUS_RETRY_BASE_WAIT,
    MODBUS_RETRY_MAX_WAIT,
    NIGHT_POLL_INTERVAL,
    OPTIMIZER_UPDATE_TIMEOUT,
    UPDATE_TIMEOUT,
)
from .modbus_guard import ModbusGuard
from .modbus_telemetry import ModbusTelemetry
from .night_mode import InverterMode, NightModeDetector
from .register_cache import RegisterCache, is_energy_counter

_LOGGER = logging.getLogger(__name__)


# ── back-off helper ────────────────────────────────────────────────────────────

def _backoff_seconds(consecutive: int) -> float:
    """Exponential back-off with ±10 % jitter, capped at MODBUS_RETRY_MAX_WAIT."""
    base = MODBUS_RETRY_BASE_WAIT.total_seconds()
    cap = MODBUS_RETRY_MAX_WAIT.total_seconds()
    delay = min(base * math.pow(2, consecutive - 1), cap)
    jitter = delay * 0.10 * (2 * random.random() - 1)
    return max(0.0, delay + jitter)


# ── contiguous register sort ───────────────────────────────────────────────────

def _sort_by_modbus_address(names: list[RegisterName]) -> list[RegisterName]:
    """Sort register names by Modbus address for contiguous PDU reads."""
    def _addr(name: RegisterName) -> int:
        for attr_path in (
            "register_definition.register",
            "register_definition.address",
            "address",
            "value",
        ):
            try:
                obj: Any = name
                for part in attr_path.split("."):
                    obj = getattr(obj, part)
                if isinstance(obj, int):
                    return obj
            except AttributeError:
                continue
        return 0

    try:
        return sorted(names, key=_addr)
    except Exception:  # noqa: BLE001
        return names


# ──────────────────────────────────────────────────────────────────────────────
# Main coordinator
# ──────────────────────────────────────────────────────────────────────────────

class HuaweiSolarUpdateCoordinator(
    DataUpdateCoordinator[dict[RegisterName, Result[Any]]]
):
    """Optimised DataUpdateCoordinator for Huawei Solar entities.

    Key behaviours
    --------------
    Adaptive learning (v1.0.4)
      AdaptiveModbusController maintains 96 circadian time slots (15 min each).
      At the start of every poll cycle, get_params() returns recommended values
      for poll_interval, request_gap, request_timeout and max_queue_depth based
      on the slot's historical failure rate, P95 RTT, and whether a state
      transition is active.  RTT is measured around each batch_update() and fed
      back via record_request().  State changes (night↔day, battery reversal)
      trigger notify_transition() which forces maximum-tolerance parameters for
      ADAPTIVE_TRANSITION_DURATION_MINUTES regardless of learned history.

    ModbusGuard serialisation
      One request in-flight at a time; adaptive gap and queue_depth.

    RegisterCache
      Tier-aware TTL; adaptive TTL doubles on stable values; STATIC never re-read.
      Energy counter registers excluded from stale-cache fallback.

    Coordinator jitter
      start_delay offsets the first poll per coordinator so four sharing the
      same guard never fire simultaneously.

    Contiguous register sorting
      stale_names sorted by Modbus address for fewer TCP round-trips per poll.
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
        start_delay: timedelta = timedelta(0),
    ) -> None:
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
        self._day_interval = update_interval if update_interval is not None else timedelta(0)
        self._start_delay = start_delay
        self._first_poll_done: bool = False

        self.guard = ModbusGuard.get_or_create(device.serial_number)
        self.cache = RegisterCache()
        self.telemetry: ModbusTelemetry | None = None
        self._adaptive: AdaptiveModbusController | None = None

        self._night_detector = NightModeDetector(
            on_mode_change=self._on_mode_change,
            poll_interval_day=self._day_interval,
            poll_interval_night=NIGHT_POLL_INTERVAL,
        )

        self._consecutive_timeouts: int = 0
        self._consecutive_failures: int = 0

    # ── wiring ─────────────────────────────────────────────────────────────────

    def attach_telemetry(self, telemetry: ModbusTelemetry) -> None:
        self.telemetry = telemetry
        self.cache.set_telemetry(telemetry)

    def attach_adaptive(self, controller: AdaptiveModbusController) -> None:
        """Attach the circadian adaptive learning controller."""
        self._adaptive = controller

    def invalidate_cache(self, name: RegisterName) -> None:
        self.cache.invalidate(name)

    # ── night-mode / transition callback ───────────────────────────────────────

    def _on_mode_change(self, new_mode: InverterMode) -> None:
        is_night = new_mode == InverterMode.NIGHT
        self.cache.set_night_mode(is_night)
        if self.telemetry:
            self.telemetry.record_night_mode(is_night)
        # A day↔night transition is the highest-impact inverter state change.
        # Notify the adaptive controller immediately so it raises parameters.
        if self._adaptive:
            self._adaptive.notify_transition(
                "night→day" if not is_night else "day→night"
            )
        new_interval = NIGHT_POLL_INTERVAL if is_night else self._adaptive_poll_interval()
        self.update_interval = new_interval
        _LOGGER.info(
            "%s: switching to %s mode — poll interval → %s",
            self.name, new_mode.name, new_interval,
        )

    def _adaptive_poll_interval(self) -> timedelta:
        """Return the current adaptive poll interval, or the configured day interval."""
        if self._adaptive:
            return self._adaptive.get_params().poll_interval
        return self._day_interval

    # ── poll logic ─────────────────────────────────────────────────────────────

    async def _async_update_data(self) -> dict[RegisterName, Result[Any]]:
        """Execute one optimised poll cycle.

        Steps
        -----
        0.  First-poll start_delay (stagger).
        1.  Fetch adaptive params; apply gap + queue_depth to guard.
        2.  Collect register names from active HA entities.
        3.  Cache filter — skip fresh registers.
        4.  If nothing stale, return fully cached result.
        5.  Apply back-off sleep if consecutive timeouts are high.
        6.  Sort stale_names by Modbus address.
        7.  Acquire guard + measure RTT around batch_update().
        8.  On timeout: record failure; stale-cache fallback excluding energy counters.
        9.  On success: record RTT; update cache; dynamically adjust poll interval.
        """
        # ── 0. First-poll stagger ─────────────────────────────────────────────
        if not self._first_poll_done:
            self._first_poll_done = True
            if self._start_delay.total_seconds() > 0:
                _LOGGER.debug(
                    "%s: first-poll start_delay %.1f s",
                    self.name, self._start_delay.total_seconds(),
                )
                await asyncio.sleep(self._start_delay.total_seconds())

        # ── 1. Adaptive params ────────────────────────────────────────────────
        if self._adaptive:
            params = self._adaptive.get_params()
            self.guard.update_gap(params.request_gap.total_seconds())
            self.guard.update_max_queue_depth(params.max_queue_depth)
            effective_timeout = params.request_timeout
            # Dynamically adjust the coordinator's poll interval (outside night mode)
            if not self.cache.night_mode:
                self.update_interval = params.poll_interval
        else:
            effective_timeout = self.update_timeout

        # ── 2. Collect register names ─────────────────────────────────────────
        all_names: list[RegisterName] = list(
            set(chain.from_iterable(
                ctx["register_names"] for ctx in self.async_contexts()
            ))
        )
        if not all_names:
            return {}

        # ── 3. Cache filter ───────────────────────────────────────────────────
        stale_names = self.cache.filter_stale(all_names, self._day_interval)

        # ── 4. Fully cached ───────────────────────────────────────────────────
        if not stale_names:
            _LOGGER.debug(
                "%s: %d register(s) all cached — skipping Modbus [night=%s]",
                self.name, len(all_names), self.cache.night_mode,
            )
            if self.telemetry:
                self.telemetry.record_skipped_poll()
            return {n: v for n in all_names if (v := self.cache.get(n)) is not None}

        # ── 5. Back-off ───────────────────────────────────────────────────────
        if self._consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
            wait = _backoff_seconds(
                self._consecutive_timeouts - MAX_CONSECUTIVE_TIMEOUTS + 1
            )
            _LOGGER.debug(
                "%s: back-off %.1f s after %d consecutive timeout(s)",
                self.name, wait, self._consecutive_timeouts,
            )
            await asyncio.sleep(wait)

        # ── 6. Sort by address ────────────────────────────────────────────────
        stale_names = _sort_by_modbus_address(stale_names)

        # ── 7. Acquire guard + batch_update with RTT measurement ──────────────
        rtt_ms: float = 0.0
        try:
            async with self.guard.request():
                t0 = time.monotonic()
                async with asyncio.timeout(effective_timeout.total_seconds()):
                    if self.telemetry:
                        self.telemetry.record_request(len(stale_names))
                    fresh = await self.device.batch_update(stale_names)
                rtt_ms = (time.monotonic() - t0) * 1000

        except TimeoutError as err:
            self._consecutive_timeouts += 1
            self._consecutive_failures += 1
            if self.telemetry:
                self.telemetry.record_timeout()
            # Feed failure to adaptive controller
            if self._adaptive:
                self._adaptive.record_request(0.0, success=False, timeout=True)

            if self._consecutive_timeouts == 1:
                _LOGGER.warning(
                    "%s: Modbus timeout (no response in %.0f s). "
                    "Back-off after %d consecutive timeouts.",
                    self.name,
                    effective_timeout.total_seconds(),
                    MAX_CONSECUTIVE_TIMEOUTS,
                )
            else:
                _LOGGER.debug(
                    "%s: Modbus timeout #%d (back-off: %s)",
                    self.name,
                    self._consecutive_timeouts,
                    self._consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS,
                )

            # ── 8. Stale-cache fallback — energy counters excluded ────────────
            cached_fallback = {
                n: v
                for n in all_names
                if not is_energy_counter(n)
                and (v := self.cache.get(n)) is not None
            }
            energy_withheld = sum(1 for n in all_names if is_energy_counter(n))
            if cached_fallback or energy_withheld:
                _LOGGER.debug(
                    "%s: stale-cache fallback — %d served, %d energy counter(s) withheld",
                    self.name, len(cached_fallback), energy_withheld,
                )
                if cached_fallback:
                    return cached_fallback

            retry_after = int(_backoff_seconds(max(1, self._consecutive_timeouts)))
            raise UpdateFailed(
                f"Timeout communicating with {self.device.serial_number}: "
                f"no response in {effective_timeout.total_seconds():.0f} s "
                f"(consecutive: {self._consecutive_timeouts})",
                retry_after=retry_after,
            ) from err

        except ReadException as err:
            self._consecutive_failures += 1
            if self.telemetry:
                self.telemetry.record_failure()
            if self._adaptive:
                self._adaptive.record_request(0.0, success=False, timeout=False)
            if err.modbus_exception_code == 0x02:
                _LOGGER.error(
                    "%s: ILLEGAL_DATA_ADDRESS — disable sensors one-by-one "
                    "(wait 30 s each) to find the culprit register.",
                    self.device.serial_number,
                )
            raise UpdateFailed(
                f"Could not update {self.device.serial_number}: {err}"
            ) from err

        except ConnectionInterruptedException as err:
            self._consecutive_failures += 1
            if self.telemetry:
                self.telemetry.record_failure()
            if self._adaptive:
                self._adaptive.record_request(0.0, success=False, timeout=False)
            _LOGGER.warning(
                "%s: connection interrupted — another Modbus client may have connected.",
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
            if self._adaptive:
                self._adaptive.record_request(0.0, success=False, timeout=False)
            raise UpdateFailed(
                f"Could not update {self.device.serial_number}: {err}"
            ) from err

        # ── 9. Success path ───────────────────────────────────────────────────
        if self._adaptive:
            self._adaptive.record_request(rtt_ms, success=True, timeout=False)

        if self._consecutive_timeouts > 0 or self._consecutive_failures > 0:
            _LOGGER.info(
                "%s: communication restored (after %d timeout(s) / %d failure(s))",
                self.name,
                self._consecutive_timeouts,
                self._consecutive_failures,
            )
            self.cache.invalidate_all()

        self._consecutive_timeouts = 0
        self._consecutive_failures = 0

        self.cache.update(fresh)
        merged_result = self.cache.merge(fresh, all_names)
        self._night_detector.evaluate(merged_result)

        return merged_result


# ──────────────────────────────────────────────────────────────────────────────
# Optimizer coordinator
# ──────────────────────────────────────────────────────────────────────────────

class HuaweiSolarOptimizerUpdateCoordinator(
    DataUpdateCoordinator[dict[int, OptimizerRealTimeData]]
):
    """DataUpdateCoordinator for Huawei Solar optimizers."""

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
        self._adaptive: AdaptiveModbusController | None = None
        self._consecutive_timeouts: int = 0
        self._consecutive_failures: int = 0

    def attach_telemetry(self, telemetry: ModbusTelemetry) -> None:
        self.telemetry = telemetry

    def attach_adaptive(self, controller: AdaptiveModbusController) -> None:
        self._adaptive = controller

    async def _async_update_data(self) -> dict[int, OptimizerRealTimeData]:
        if self._consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
            wait = _backoff_seconds(
                self._consecutive_timeouts - MAX_CONSECUTIVE_TIMEOUTS + 1
            )
            await asyncio.sleep(wait)

        if self._adaptive:
            params = self._adaptive.get_params()
            self.guard.update_gap(params.request_gap.total_seconds())
            self.guard.update_max_queue_depth(params.max_queue_depth)
            effective_timeout = params.request_timeout
        else:
            effective_timeout = OPTIMIZER_UPDATE_TIMEOUT

        try:
            async with self.guard.request():
                t0 = time.monotonic()
                async with asyncio.timeout(effective_timeout.total_seconds()):
                    if self.telemetry:
                        self.telemetry.record_request(1)
                    result = await self.device.get_latest_optimizer_history_data()
                rtt_ms = (time.monotonic() - t0) * 1000

        except TimeoutError as err:
            self._consecutive_timeouts += 1
            self._consecutive_failures += 1
            if self.telemetry:
                self.telemetry.record_timeout()
            if self._adaptive:
                self._adaptive.record_request(0.0, success=False, timeout=True)
            if self._consecutive_timeouts == 1:
                _LOGGER.warning(
                    "Optimizer %s: Modbus timeout (attempt %d).",
                    self.device.serial_number, self._consecutive_timeouts,
                )
            retry_after = int(_backoff_seconds(max(1, self._consecutive_timeouts)))
            raise UpdateFailed(
                f"Timeout from {self.device.serial_number} optimizers "
                f"(consecutive: {self._consecutive_timeouts})",
                retry_after=retry_after,
            ) from err

        except ConnectionInterruptedException as err:
            self._consecutive_failures += 1
            if self.telemetry:
                self.telemetry.record_failure()
            if self._adaptive:
                self._adaptive.record_request(0.0, success=False, timeout=False)
            _LOGGER.warning("Optimizer %s: connection interrupted.", self.device.serial_number)
            raise UpdateFailed(
                f"Connection to {self.device.serial_number} interrupted.",
                retry_after=int(MODBUS_RETRY_BASE_WAIT.total_seconds()),
            ) from err

        except DecodeError as err:
            self._consecutive_failures += 1
            if self.telemetry:
                self.telemetry.record_failure()
            if self._adaptive:
                self._adaptive.record_request(0.0, success=False, timeout=False)
            raise UpdateFailed(
                f"Could not decode optimizer data from {self.device.serial_number}: {err}.",
                retry_after=15 * 60,
            ) from err

        except HuaweiSolarException as err:
            self._consecutive_failures += 1
            if self.telemetry:
                self.telemetry.record_failure()
            if self._adaptive:
                self._adaptive.record_request(0.0, success=False, timeout=False)
            raise UpdateFailed(
                f"Could not update {self.device.serial_number} optimizers: {err}"
            ) from err

        if self._adaptive:
            self._adaptive.record_request(rtt_ms, success=True, timeout=False)

        if self._consecutive_timeouts > 0 or self._consecutive_failures > 0:
            _LOGGER.info(
                "Optimizer %s: communication restored after %d timeout(s) / %d failure(s).",
                self.device.serial_number,
                self._consecutive_timeouts,
                self._consecutive_failures,
            )
        self._consecutive_timeouts = 0
        self._consecutive_failures = 0
        return result


# ── Factory helper ─────────────────────────────────────────────────────────────

async def create_optimizer_update_coordinator(
    hass: HomeAssistant,
    device: SUN2000Device,
    optimizer_device_infos: dict[int, DeviceInfo],
    update_interval: timedelta | None,
) -> HuaweiSolarOptimizerUpdateCoordinator:
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
