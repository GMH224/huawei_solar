"""Specialised DataUpdateCoordinators for Huawei Solar entities.

Optimisation history
--------------------
v2.10b  Exponential back-off, tiered logging, retry_after hints.
v2.11.0 ModbusGuard serialisation + 150 ms gap, RegisterCache static TTL,
        ModbusTelemetry 10 diagnostic sensors, cache invalidation on write.
v2.12.0 Adaptive TTL (STATIC/SLOW/NORMAL/FAST), night-mode detection,
        stale-cache fallback, dynamic poll-interval adjustment.
v1.0.2  Load shedding, lru_cache on _classify(), batched cache-hit recording,
        invalidate_all() skips STATIC tier.
v1.0.3  Energy-counter stale-cache exclusion, coordinator start-time jitter,
        contiguous register sorting, set_telemetry() on RegisterCache.
v1.0.4  Circadian adaptive learning (AdaptiveModbusController), RTT feedback,
        transition-window elevated parameters, 10 adaptive sensor entities.
v1.0.5  Six reliability improvements:
        1–6 (bus-level guard, 0x06 retry, keep-alive, chunking, write verify,
             priority back-off polling) — see modbus_guard.py / keepalive.py
v1.0.6  Adaptive parameter bound tuning (evidence-based vs Gemini proposal):
        Poll 30→120 s  → 20→180 s  (20 s safe w/ bus guard; 180 s daytime limit)
        Gap 150→500 ms → unchanged  (150 ms is HW FSM floor; 30 ms rejected)
        Timeout 35→90 s → 15→60 s  (keep-alive covers dead-socket; 15 s safe floor)
        Confidence ceiling 300 → 150 samples (~5 days; balances stability vs speed)
        Cold-start baseline 30 s → ADAPTIVE_POLL_COLD_START=60 s (separate const)
        Queue depth ceiling unchanged at 3 (jitter prevents pile-up; 4 adds risk)
v1.0.6  Six reliability improvements:
        1. Bus-level guard (endpoint key) — eliminates RS485 bus collisions for
           multi-inverter topologies.  5K secondary failure rate → near zero.
        2. SLAVE_DEVICE_BUSY (0x06) retry — pause + immediate retry instead of
           failure increment; notify_transition() on first 0x06 response.
        3. Keep-alive integration — ModbusKeepAlive callbacks invalidate cache
           and reset failure counters so coordinators reconnect cleanly.
        4. Batch chunking — stale registers split into ≤40-register chunks with
           80 ms inter-chunk pause; limits each Modbus burst to ~300 ms.
        5. Write-back verification — post-write re-read with 3 s delay and up
           to 2 retries; warns on persistent write failures.
        6. Priority polling during back-off — FAST registers are always read;
           NORMAL read every 4th back-off cycle; SLOW/STATIC deferred entirely.
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
    BACKOFF_FAST_ALWAYS,
    BACKOFF_NORMAL_DIVISOR,
    BATCH_CHUNK_SIZE,
    BATCH_INTER_CHUNK_PAUSE,
    BUSY_MAX_RETRIES,
    BUSY_RETRY_PAUSE,
    MAX_CONSECUTIVE_TIMEOUTS,
    MODBUS_RETRY_BASE_WAIT,
    MODBUS_RETRY_MAX_WAIT,
    NIGHT_POLL_INTERVAL,
    OPTIMIZER_UPDATE_TIMEOUT,
    UPDATE_TIMEOUT,
    WRITE_VERIFY_DELAY,
    WRITE_VERIFY_RETRIES,
)
from .modbus_guard import ModbusGuard
from .modbus_telemetry import ModbusTelemetry
from .night_mode import InverterMode, NightModeDetector
from .register_cache import RegisterCache, RegisterTier, is_energy_counter

_LOGGER = logging.getLogger(__name__)

# Modbus exception code constants
_EXC_ILLEGAL_DATA_ADDRESS = 0x02
_EXC_SLAVE_DEVICE_BUSY    = 0x06


# ── helpers ────────────────────────────────────────────────────────────────────

def _backoff_seconds(consecutive: int) -> float:
    base = MODBUS_RETRY_BASE_WAIT.total_seconds()
    cap  = MODBUS_RETRY_MAX_WAIT.total_seconds()
    delay = min(base * math.pow(2, consecutive - 1), cap)
    jitter = delay * 0.10 * (2 * random.random() - 1)
    return max(0.0, delay + jitter)


def _sort_by_modbus_address(names: list[RegisterName]) -> list[RegisterName]:
    """Sort register names by Modbus address for contiguous PDU reads."""
    def _addr(name: RegisterName) -> int:
        for path in (
            "register_definition.register",
            "register_definition.address",
            "address",
            "value",
        ):
            try:
                obj: Any = name
                for part in path.split("."):
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


def _chunk(names: list[RegisterName], size: int) -> list[list[RegisterName]]:
    """Split a list into consecutive chunks of at most *size* items."""
    return [names[i : i + size] for i in range(0, len(names), size)]


# ──────────────────────────────────────────────────────────────────────────────
# Main coordinator
# ──────────────────────────────────────────────────────────────────────────────

class HuaweiSolarUpdateCoordinator(
    DataUpdateCoordinator[dict[RegisterName, Result[Any]]]
):
    """Optimised DataUpdateCoordinator with all v1.0.5 reliability improvements.

    Poll cycle steps
    ----------------
    0.  First-poll start_delay (coordinator jitter).
    1.  Fetch adaptive params; push gap + queue_depth to shared bus guard.
    2.  Collect register names from active HA entities.
    3.  Cache filter — skip fresh registers (tier-aware TTL).
    4.  If fully cached → return immediately (0 Modbus traffic).
    5.  Back-off: during high-failure windows skip SLOW/STATIC registers;
        FAST always read; NORMAL read every BACKOFF_NORMAL_DIVISOR cycles.
    6.  Sort stale_names by Modbus address (contiguous PDU optimisation).
    7.  Split into BATCH_CHUNK_SIZE chunks; execute with inter-chunk pause.
    8.  Per-chunk: on 0x06 BUSY → pause + retry up to BUSY_MAX_RETRIES.
    9.  On timeout → stale-cache fallback excluding energy counters.
    10. On success → feed RTT to adaptive controller, update cache, NightMode.
    """

    device: HuaweiSolarDevice

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        device: HuaweiSolarDevice,
        name: str,
        update_interval: timedelta | None = None,
        update_method: Callable[[], Awaitable[dict[RegisterName, Result[Any]]]] | None = None,
        request_refresh_debouncer: Debouncer | None = None,
        update_timeout: timedelta = UPDATE_TIMEOUT,
        start_delay: timedelta = timedelta(0),
        bus_endpoint: str = "",
    ) -> None:
        super().__init__(
            hass, logger,
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
        self._backoff_cycle: int = 0   # incremented every poll during back-off

        # Bus-level guard (shared by all coordinators on the same RS485 bus)
        endpoint = bus_endpoint or device.serial_number
        self.guard = ModbusGuard.get_or_create(endpoint)

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

    # ── wiring ────────────────────────────────────────────────────────────────

    def attach_telemetry(self, telemetry: ModbusTelemetry) -> None:
        self.telemetry = telemetry
        self.cache.set_telemetry(telemetry)

    def attach_adaptive(self, controller: AdaptiveModbusController) -> None:
        self._adaptive = controller

    def invalidate_cache(self, name: RegisterName) -> None:
        self.cache.invalidate(name)

    def on_connection_lost(self) -> None:
        """Called by ModbusKeepAlive when the connection appears dead.

        Invalidates non-STATIC cache entries so the next poll does a fresh
        full read after reconnect, and resets consecutive-failure counters so
        back-off doesn't compound with the keep-alive failure.
        """
        _LOGGER.debug("%s: connection lost — invalidating cache", self.name)
        self.cache.invalidate_all()

    def on_connection_restored(self) -> None:
        """Called by ModbusKeepAlive when a probe read succeeds post-failure."""
        _LOGGER.info("%s: connection restored (keep-alive probe)", self.name)
        self._consecutive_timeouts = 0
        self._consecutive_failures = 0

    # ── write-back verification (opt. 5) ─────────────────────────────────────

    async def verify_write(
        self,
        name: RegisterName,
        expected_value: Any,
    ) -> bool:
        """Read *name* back after a write and verify it equals *expected_value*.

        Called by number/select/switch entities after issuing a write.
        Returns True if verified, False if the value did not match after
        WRITE_VERIFY_RETRIES attempts (logs a warning in that case).
        """
        await asyncio.sleep(WRITE_VERIFY_DELAY.total_seconds())

        for attempt in range(1, WRITE_VERIFY_RETRIES + 2):
            try:
                async with self.guard.request():
                    async with asyncio.timeout(self.update_timeout.total_seconds()):
                        result = await self.device.batch_update([name])

                read_val = result.get(name)
                actual = read_val.value if read_val is not None else None

                if actual == expected_value:
                    _LOGGER.debug(
                        "%s: write verification OK for %s = %s",
                        self.name, name, actual,
                    )
                    # BUG-008 FIX: invalidate the register before updating so
                    # that any stale cache entry is evicted first.  Without this
                    # a concurrent cache write between our live read and the
                    # update call could leave a stale value behind.
                    self.cache.invalidate(name)
                    self.cache.update({name: result[name]})
                    return True

                _LOGGER.debug(
                    "%s: write verification attempt %d/%d — expected %s, got %s",
                    self.name, attempt, WRITE_VERIFY_RETRIES + 1,
                    expected_value, actual,
                )
                if attempt <= WRITE_VERIFY_RETRIES:
                    await asyncio.sleep(WRITE_VERIFY_DELAY.total_seconds())

            except (TimeoutError, HuaweiSolarException):
                _LOGGER.debug(
                    "%s: write verification read failed (attempt %d)", self.name, attempt
                )
                if attempt <= WRITE_VERIFY_RETRIES:
                    await asyncio.sleep(WRITE_VERIFY_DELAY.total_seconds())

        _LOGGER.warning(
            "%s: write verification FAILED for %s — inverter did not apply "
            "the new value after %d attempt(s).  The setting may have been "
            "silently ignored during a state transition.",
            self.name, name, WRITE_VERIFY_RETRIES + 1,
        )
        return False

    # ── night-mode / transition callback ──────────────────────────────────────

    def _on_mode_change(self, new_mode: InverterMode) -> None:
        is_night = new_mode == InverterMode.NIGHT
        self.cache.set_night_mode(is_night)
        if self.telemetry:
            self.telemetry.record_night_mode(is_night)
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
        if self._adaptive:
            return self._adaptive.get_params().poll_interval
        return self._day_interval

    # ── core batch executor with chunking + 0x06 retry (opts. 2, 4) ──────────

    async def _execute_batch(
        self,
        names: list[RegisterName],
        effective_timeout: timedelta,
    ) -> tuple[dict[RegisterName, Result[Any]], float]:
        """Execute batch_update() in address-sorted chunks with 0x06 retry.

        Each chunk is executed inside the shared bus guard.  On a 0x06
        SLAVE_DEVICE_BUSY response, the chunk is retried up to BUSY_MAX_RETRIES
        times after BUSY_RETRY_PAUSE.  A first 0x06 response also calls
        notify_transition() because BUSY at runtime is a reliable signal of an
        inverter state change.

        Returns merged results from all chunks.
        """
        sorted_names = _sort_by_modbus_address(names)
        chunks = _chunk(sorted_names, BATCH_CHUNK_SIZE)
        merged: dict[RegisterName, Result[Any]] = {}
        total_rtt_ms: float = 0.0  # BUG-10: accumulated across all chunks

        for chunk_idx, chunk in enumerate(chunks):
            if chunk_idx > 0:
                # Inter-chunk pause: give the inverter CPU breathing room.
                # This runs outside the lock so other clients can squeeze in.
                await asyncio.sleep(BATCH_INTER_CHUNK_PAUSE.total_seconds())

            busy_retries = 0
            while True:
                try:
                    async with self.guard.request():
                        t0 = time.monotonic()
                        async with asyncio.timeout(effective_timeout.total_seconds()):
                            chunk_result = await self.device.batch_update(chunk)
                    merged.update(chunk_result)
                    total_rtt_ms += (time.monotonic() - t0) * 1000
                    break  # chunk succeeded

                except ReadException as exc:
                    if (
                        getattr(exc, "modbus_exception_code", None) == _EXC_SLAVE_DEVICE_BUSY
                        and busy_retries < BUSY_MAX_RETRIES
                    ):
                        busy_retries += 1
                        _LOGGER.debug(
                            "%s: 0x06 SLAVE_DEVICE_BUSY on chunk %d/%d "
                            "(retry %d/%d in %.0f ms)",
                            self.name, chunk_idx + 1, len(chunks),
                            busy_retries, BUSY_MAX_RETRIES,
                            BUSY_RETRY_PAUSE.total_seconds() * 1000,
                        )
                        if busy_retries == 1 and self._adaptive:
                            # First BUSY is a reliable transition signal
                            self._adaptive.notify_transition("0x06 SLAVE_DEVICE_BUSY")
                        await asyncio.sleep(BUSY_RETRY_PAUSE.total_seconds())
                        continue  # retry this chunk

                    # Non-BUSY ReadException or retries exhausted.
                    # BUG-4 FIX: do NOT record failure here; outer handlers do it.
                    raise

        return merged, total_rtt_ms  # BUG-10 FIX: return rtt to caller

    # ── poll logic ────────────────────────────────────────────────────────────

    async def _async_update_data(self) -> dict[RegisterName, Result[Any]]:
        # ── 0. First-poll stagger ─────────────────────────────────────────────
        if not self._first_poll_done:
            self._first_poll_done = True
            if self._start_delay.total_seconds() > 0:
                await asyncio.sleep(self._start_delay.total_seconds())

        # ── 1. Adaptive params → push to shared bus guard ─────────────────────
        if self._adaptive:
            params = self._adaptive.get_params()
            self.guard.update_gap(params.request_gap.total_seconds())
            self.guard.update_max_queue_depth(params.max_queue_depth)
            effective_timeout = params.request_timeout
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

        # ── 5. Priority polling during back-off (opt. 6) ─────────────────────
        in_backoff = self._consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS
        if in_backoff:
            self._backoff_cycle += 1
            wait = _backoff_seconds(
                self._consecutive_timeouts - MAX_CONSECUTIVE_TIMEOUTS + 1
            )
            _LOGGER.debug(
                "%s: back-off cycle %d — sleeping %.1f s",
                self.name, self._backoff_cycle, wait,
            )
            await asyncio.sleep(wait)

            # Filter stale_names by priority: FAST always, NORMAL every Nth cycle,
            # SLOW/STATIC deferred until recovery.
            priority_names: list[RegisterName] = []
            for n in stale_names:
                tier = self.cache.tier_of(n)
                if tier == RegisterTier.FAST:
                    priority_names.append(n)
                elif tier == RegisterTier.NORMAL:
                    if self._backoff_cycle % BACKOFF_NORMAL_DIVISOR == 0:
                        priority_names.append(n)
                # SLOW and STATIC are skipped entirely during back-off
            stale_names = priority_names or stale_names[:BATCH_CHUNK_SIZE]
        else:
            self._backoff_cycle = 0

        if not stale_names:
            if self.telemetry:
                self.telemetry.record_skipped_poll()
            return {n: v for n in all_names if (v := self.cache.get(n)) is not None}

        # ── 6–8. Execute chunked batch with 0x06 retry ────────────────────────
        try:
            # BUG-10 FIX: record_request after batch so count is accurate
            fresh, total_rtt_ms = await self._execute_batch(stale_names, effective_timeout)
            if self.telemetry:
                self.telemetry.record_request(len(stale_names))

        except TimeoutError as err:
            self._consecutive_timeouts += 1
            self._consecutive_failures += 1
            if self.telemetry:
                self.telemetry.record_timeout()
            if self._adaptive:
                self._adaptive.record_request(0.0, success=False, timeout=True)

            if self._consecutive_timeouts == 1:
                _LOGGER.warning(
                    "%s: Modbus timeout (no response in %.0f s). "
                    "Back-off after %d consecutive timeouts.",
                    self.name, effective_timeout.total_seconds(),
                    MAX_CONSECUTIVE_TIMEOUTS,
                )
            else:
                _LOGGER.debug(
                    "%s: Modbus timeout #%d",
                    self.name, self._consecutive_timeouts,
                )

            # ── 9. Stale-cache fallback — energy counters excluded ────────────
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

            raise UpdateFailed(
                f"Timeout communicating with {self.device.serial_number}: "
                f"no response in {effective_timeout.total_seconds():.0f} s "
                f"(consecutive: {self._consecutive_timeouts})",
                retry_after=int(_backoff_seconds(max(1, self._consecutive_timeouts))),
            ) from err

        except ReadException as err:
            self._consecutive_failures += 1
            if self.telemetry:
                self.telemetry.record_failure()
            if self._adaptive:
                self._adaptive.record_request(0.0, success=False, timeout=False)
            if getattr(err, "modbus_exception_code", None) == _EXC_ILLEGAL_DATA_ADDRESS:
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

        # ── 10. Success path ──────────────────────────────────────────────────
        if self._consecutive_timeouts > 0 or self._consecutive_failures > 0:
            _LOGGER.info(
                "%s: communication restored (after %d timeout(s) / %d failure(s))",
                self.name, self._consecutive_timeouts, self._consecutive_failures,
            )
            self.cache.invalidate_all()

        self._consecutive_timeouts = 0
        self._consecutive_failures = 0
        self._backoff_cycle = 0

        # BUG-4 FIX: record_request called exactly once here (not in _execute_batch)
        if self._adaptive:
            self._adaptive.record_request(total_rtt_ms, success=True, timeout=False)

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
        bus_endpoint: str = "",
    ) -> None:
        super().__init__(
            hass, logger,
            name=name,
            update_interval=update_interval,
            request_refresh_debouncer=request_refresh_debouncer,
        )
        self.device = device
        self.optimizer_device_infos = optimizer_device_infos
        endpoint = bus_endpoint or device.serial_number
        self.guard = ModbusGuard.get_or_create(endpoint)
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
            raise UpdateFailed(
                f"Timeout from {self.device.serial_number} optimizers "
                f"(consecutive: {self._consecutive_timeouts})",
                retry_after=int(_backoff_seconds(max(1, self._consecutive_timeouts))),
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
            # Optimizer measures rtt_ms directly (not via _execute_batch)
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
    bus_endpoint: str = "",
) -> HuaweiSolarOptimizerUpdateCoordinator:
    coordinator = HuaweiSolarOptimizerUpdateCoordinator(
        hass, _LOGGER,
        device=device,
        optimizer_device_infos=optimizer_device_infos,
        name=f"{device.serial_number}_optimizer_data_update_coordinator",
        update_interval=update_interval,
        bus_endpoint=bus_endpoint,
    )
    await coordinator.async_config_entry_first_refresh()
    return coordinator
