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
from functools import lru_cache
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
from .modbus_guard import ModbusGuard, ModbusQueueShed
from .modbus_telemetry import ModbusTelemetry
from .night_mode import InverterMode, NightModeDetector
from .register_cache import (
    RegisterCache,
    RegisterTier,
    classify_register,
    is_energy_counter,
)

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


@lru_cache(maxsize=512)
def _modbus_span(name: RegisterName) -> tuple[int, int]:
    """Resolve a register's (start, end) Modbus address span.

    v1.3.5 REPLACEMENT of a reflection-based best-effort lookup that only
    guessed at a start address via several fallback attribute paths and never
    resolved LENGTH at all. That was good enough for sorting, but address-aware
    grouping (see _address_group()) needs the real span of every register to
    reproduce the vendor library's own batching rule exactly — so this now
    reads directly from the same ``REGISTERS`` table
    ``huawei_solar.device.base.batch_update()`` uses internally. Memoised: the
    set of unique RegisterNames in a session is bounded (~200).

    Falls back to a synthetic 1-wide span when a name is not found (e.g. a
    future library version drops or renames a register) rather than raising —
    grouping degrades to "treat as unknown-width", never to a crash.
    """
    try:
        from huawei_solar.registers import REGISTERS
        reg = REGISTERS[name]
        return reg.register, reg.register + reg.length - 1
    except Exception:  # noqa: BLE001 — never let a lookup failure break a poll
        return 0, 0


def _modbus_address(name: RegisterName) -> int:
    """Resolve a register's START Modbus address. Kept for callers that only
    need a sort key (address-aware grouping needs the full span; see above)."""
    return _modbus_span(name)[0]


def _sort_by_modbus_address(names: list[RegisterName]) -> list[RegisterName]:
    """Sort register names by Modbus address for contiguous PDU reads."""
    try:
        return sorted(names, key=_modbus_address)
    except Exception:  # noqa: BLE001
        return names


def _chunk(names: list[RegisterName], size: int) -> list[list[RegisterName]]:
    """Split a list into consecutive chunks of at most *size* items."""
    return [names[i : i + size] for i in range(0, len(names), size)]


# ──────────────────────────────────────────────────────────────────────────────
# Main coordinator
# ──────────────────────────────────────────────────────────────────────────────

def _chunk_tier(chunk: list[RegisterName]) -> str:
    """Label a chunk by the SLOWEST tier it contains, plus its composition.

    v1.3.3 FIX: this previously returned ``min(tiers)`` — the *fastest* tier
    present — so a chunk holding 1 FAST and 26 SLOW registers was labelled
    "FAST". In the field capture **all 3,400 records were labelled FAST**,
    including every 19+ register chunk and a 51.5 s outlier, which made the
    field useless for the exact correlation it was added for.

    ``max`` is the right choice because cost follows the slowest content: a
    single SLOW register drags the whole exchange onto the inverter's slow
    internal path. Example output: ``"SLOW:F1/N2/S24"``.
    """
    try:
        tiers = [classify_register(name) for name in chunk]
        if not tiers:
            return "empty"
        counts = {t: 0 for t in RegisterTier}
        for t in tiers:
            counts[t] += 1
        composition = "/".join(
            f"{t.name[0]}{counts[t]}" for t in RegisterTier if counts[t]
        )
        return f"{max(tiers).name}:{composition}"
    except Exception:  # noqa: BLE001 — instrumentation must never break a poll
        return "unknown"


#: Mirrors huawei_solar.device.base.MAX_BATCHED_REGISTERS_GAP /
#: _COUNT — the vendor library's OWN rule for how many named registers it
#: will fold into one physical Modbus exchange inside a single
#: ``batch_update()`` call. Two independent-address-space-per-request
#: constants, duplicated here (not imported) because we must be able to group
#: registers BEFORE calling the library, in order to issue each group as our
#: OWN separately-paced request (see _address_group / Defect E below) — by the
#: time the library does its internal splitting, it is too late for our
#: adaptive gap to apply between the pieces.
_ADDRESS_GROUP_MAX_GAP = 16
_ADDRESS_GROUP_MAX_SPAN = 64


def _address_group(names: list[RegisterName]) -> list[list[RegisterName]]:
    """Partition ADDRESS-SORTED registers into contiguous groups.

    DEFECT E (v1.3.5) — retires the tier-based cost model entirely.

    Field measurement first suggested cost tracked register TIER (SLOW/STATIC
    content ~2,900 ms + 377 ms/register vs ~6 ms for FAST/NORMAL). That
    correlation was real but NOT causal: it was confounded by SLOW-tier
    register sets in this integration happening to be large and scattered
    across the address map. A much larger capture (29,000 requests) found the
    TRUE variable: `huawei_solar.device.base.batch_update()` groups the
    registers it is given into physical Modbus exchanges using EXACTLY the
    rule reproduced below (gap < 16, span <= 64) — and a chunk that this rule
    forces into 2+ physical exchanges costs roughly one MORE ~2.9-3.0 s fixed
    entry toll per exchange, REGARDLESS of tier:

        regs=7  (fits in 1 exchange): ~7-60 ms,   independent of tier
        regs=8  (forced into 2)     : ~2,800-4,600 ms, independent of tier

    Corroborated against the REAL register address map (huawei_solar 3.0.5):
    a representative main-inverter register set (input_power ..
    internal_temperature, addresses 32064-32087) forms a single CONTIGUOUS
    9-register block, followed by a register 18 addresses further on
    (accumulated_yield_energy, gap 18 > 16, forced into a new group). The
    field's own directly-measured threshold sits at regs=8 (7 fast, 8+
    mostly slow); this representative set is one register larger because it
    is an approximation of the real coordinator's exact entity list, which
    is not statically enumerable (see _collect_register_names) — the two
    numbers are consistent, not required to match exactly.

    THE FIX: group registers into physical-exchange-sized batches OURSELVES,
    using the library's own rule, BEFORE calling batch_update() — so each
    group becomes a SEPARATE guard.request(), individually paced by the
    adaptive gap. Previously the library did this splitting internally,
    invisible to us and with NO pacing between the pieces — plausibly the
    cause of the "unexpected response, discarding bytes" transaction-ID
    desync seen in the field log immediately preceding a suspected freeze.

    Names must already be address-sorted (see _sort_by_modbus_address).
    """
    if not names:
        return []
    groups: list[list[RegisterName]] = []
    current: list[RegisterName] = [names[0]]
    _, current_end = _modbus_span(names[0])
    group_start, _ = _modbus_span(names[0])

    for name in names[1:]:
        start, end = _modbus_span(name)
        gap = start - current_end - 1
        span = end - group_start
        if gap < _ADDRESS_GROUP_MAX_GAP and span <= _ADDRESS_GROUP_MAX_SPAN:
            current.append(name)
            current_end = max(current_end, end)
        else:
            groups.append(current)
            current = [name]
            group_start, current_end = start, end
    groups.append(current)
    return groups


class HuaweiSolarUpdateCoordinator(
    DataUpdateCoordinator[dict[RegisterName, Result[Any]]]
):
    """Optimised DataUpdateCoordinator with all v1.0.5 reliability improvements.

    Poll cycle steps
    ----------------
    0.  First-poll start_delay (coordinator jitter) — deferred as a
        background task since v1.3.13 so it can never block a caller.
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
        #: Requests shed by the shared bus guard (v1.2.3, Defect D). Counted
        #: separately from inverter failures because they measure OUR
        #: contention, not the inverter's health.
        self._shed_count: int = 0
        #: Diagnostics for the Defect A fix — the batch total and how many
        #: chunks produced it, so the inflation factor is directly observable
        #: instead of having to be back-solved from saturated parameters.
        self._last_batch_ms: float = 0.0
        self._last_chunk_count: int = 0

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

    def _record_timeout(self) -> None:
        """Record a Modbus timeout outcome to all observers (single dispatch).

        Consolidates the previously-duplicated bookkeeping: the consecutive
        counters, the telemetry diagnostic feed, and the adaptive RTT tuner.
        """
        self._consecutive_timeouts += 1
        self._consecutive_failures += 1
        if self.telemetry:
            self.telemetry.record_timeout()
        if self._adaptive:
            self._adaptive.record_request(0.0, success=False, timeout=True)

    def _record_shed(self) -> None:
        """Record a queue-shed request — NOT as inverter misbehaviour.

        DEFECT D (v1.2.3): shedding happens when our own sub-coordinators
        contend for the shared bus lock.  It says nothing about the inverter,
        so it must not enter the adaptive circadian model that drives poll
        interval, gap and timeout.  Telemetry still sees it (a shed request is
        genuinely interesting operationally), and the consecutive-failure
        counters still advance so back-off and entity availability behave
        exactly as before.
        """
        self._consecutive_timeouts += 1
        self._consecutive_failures += 1
        self._shed_count += 1
        if self.telemetry:
            self.telemetry.record_timeout()
        if self._adaptive:
            self._adaptive.note_shed()   # diagnostics only, NOT a failure

    def _record_failure(self) -> None:
        """Record a non-timeout Modbus failure to all observers (single dispatch)."""
        self._consecutive_failures += 1
        if self.telemetry:
            self.telemetry.record_failure()
        if self._adaptive:
            self._adaptive.record_request(0.0, success=False, timeout=False)

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
        # (v1.3.5, Defect E) ADDRESS-AWARE CHUNKING — replaces v1.3.3's tier
        # separation and v1.3.4's coalescing, BOTH of which are retired.
        #
        # Group registers into physical-Modbus-exchange-sized batches using
        # the SAME rule huawei_solar.device.base.batch_update() uses
        # internally (gap<16, span<=64) — see _address_group() for the full
        # rationale and the real-register-map evidence. Each group becomes
        # its OWN guard.request() below, so the adaptive gap is enforced
        # BETWEEN every physical exchange, not just once per poll — closing
        # the pacing gap that plausibly caused the transaction-ID desync
        # ("unexpected response ... discarding bytes") seen in the field log.
        #
        # A group that stays within one physical exchange is cheap regardless
        # of tier (~7-60 ms observed); a group forced across the boundary
        # costs roughly one further ~2.9-3.0 s fixed toll. There is nothing
        # left to gain by further splitting a group that is ALREADY one
        # physical exchange, so BATCH_CHUNK_SIZE still caps how large any
        # single group may grow before we split it ourselves as a safety net
        # (the vendor library caps at 64 addresses of SPAN, not count, so a
        # sparse but very wide group could in principle contain more names
        # than is comfortable to hold in one PDU).
        chunks: list[list[RegisterName]] = []
        for group in _address_group(sorted_names):
            chunks.extend(_chunk(group, BATCH_CHUNK_SIZE))
        merged: dict[RegisterName, Result[Any]] = {}
        # DEFECT A (v1.2.3) — two DIFFERENT quantities, previously conflated.
        #
        # ``total_batch_ms`` is how long the whole poll cycle spent talking to
        # the inverter.  ``max_chunk_rtt_ms`` is how long ONE Modbus exchange
        # took.  Until v1.2.3 the sum was returned and fed to
        # AdaptiveModbusController.record_request() as if it were a single
        # round trip, so rtt_p95_ms was inflated by the chunk count.
        #
        # Field evidence (2 months, one inverter): gap sat at its 500 ms
        # ceiling 84% of the time (time-weighted) and the request timeout at
        # its 60 s ceiling 42%, implying a stored rtt_p95_ms above 12 SECONDS
        # for nearly half the window — not physically possible for one Modbus
        # TCP exchange.
        #
        # MAX, not mean: ``effective_timeout`` is applied per chunk (see the
        # asyncio.timeout below), so the value that drives it must cover the
        # slowest chunk in the cycle, not the average one.
        total_batch_ms: float = 0.0
        max_chunk_rtt_ms: float = 0.0
        chunk_count: int = 0

        for chunk_idx, chunk in enumerate(chunks):
            if chunk_idx > 0:
                # Inter-chunk pause: give the inverter CPU breathing room.
                # This runs outside the lock so other clients can squeeze in.
                await asyncio.sleep(BATCH_INTER_CHUNK_PAUSE.total_seconds())

            busy_retries = 0
            while True:
                try:
                    async with self.guard.request(label=self.name) as _req:
                        # Attribute this exchange to what it actually reads, so
                        # a stall can be correlated with register count/tier.
                        _req.registers = len(chunk)
                        _req.priority_tier = _chunk_tier(chunk)
                        t0 = time.monotonic()
                        async with asyncio.timeout(effective_timeout.total_seconds()):
                            chunk_result = await self.device.batch_update(chunk)
                    merged.update(chunk_result)
                    chunk_ms = (time.monotonic() - t0) * 1000
                    total_batch_ms += chunk_ms
                    max_chunk_rtt_ms = max(max_chunk_rtt_ms, chunk_ms)
                    chunk_count += 1
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

        self._last_batch_ms = total_batch_ms
        self._last_chunk_count = chunk_count
        # Return the PER-REQUEST figure: this is what the adaptive controller
        # consumes. The batch total is retained for diagnostics only.
        return merged, max_chunk_rtt_ms

    # ── poll logic ────────────────────────────────────────────────────────────

    def _schedule_deferred_first_poll(self) -> None:
        """Run the actual first poll, after the stagger delay, as a
        background task instead of sleeping inline (see Defect K below)."""

        async def _deferred() -> None:
            try:
                await asyncio.sleep(self._start_delay.total_seconds())
                await self.async_request_refresh()
            except Exception:  # noqa: BLE001 — background task must not raise
                _LOGGER.exception(
                    "%s: deferred first-poll refresh failed; will retry on "
                    "the coordinator's normal schedule", self.name,
                )

        self.hass.async_create_task(_deferred())

    async def _async_update_data(self) -> dict[RegisterName, Result[Any]]:
        # ── 0. First-poll stagger ─────────────────────────────────────────────
        if not self._first_poll_done:
            self._first_poll_done = True
            if self._start_delay.total_seconds() > 0:
                # v1.3.13 FIX (Defect K): this used to be
                # `await asyncio.sleep(self._start_delay.total_seconds())`
                # directly inline, which blocks WHOEVER calls
                # _async_update_data() for that entire delay. A field
                # traceback confirmed this method is reachable
                # SYNCHRONOUSLY from Home Assistant's own entity-add
                # machinery during platform setup
                # (entity_platform._async_add_entity ->
                # entity.async_device_update -> async_update ->
                # coordinator.async_request_refresh()), not only from the
                # coordinator's own background scheduling that this delay
                # was designed for. Sleeping here therefore directly
                # extended a synchronous Home Assistant setup call by up to
                # the full stagger delay -- worse still for higher device
                # indices since Defect I (v1.3.10) added a per-device
                # offset on top of the per-type one. A CancelledError
                # arriving mid-sleep, when Home Assistant's own setup
                # timeout ran out, is the confirmed mechanism behind a real
                # field incident: "Setup of config entry ... cancelled"
                # (see AUDIT_1.3.13.md) -- the exact error this project has
                # been trying to explain since the very first handoff.
                #
                # Fixed by never blocking here at all. Whatever calls this
                # method gets an immediate answer -- a copy of the last
                # known data if any exists, or an empty dict on a genuine
                # first call (already an established, safe pattern in this
                # exact method -- see the `if not all_names: return {}`
                # case a few lines below, and every entity's existing
                # handling of "no data yet" via `if self.coordinator.data
                # and ...`). The REAL first poll is scheduled as a
                # background task that sleeps for the stagger delay and
                # then calls async_request_refresh() itself -- preserving
                # the exact same effect on bus traffic (nothing real hits
                # the bus before the deadline) without ever occupying a
                # caller's stack for it.
                self._schedule_deferred_first_poll()
                return dict(self.data) if self.data else {}

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
            # If nothing is due this cycle (no FAST stale, not a NORMAL cycle),
            # read nothing and serve the cached snapshot — do NOT fall back to
            # reading SLOW/STATIC, which would defeat the deferral.
            stale_names = priority_names
        else:
            self._backoff_cycle = 0

        if not stale_names:
            if self.telemetry:
                self.telemetry.record_skipped_poll()
            return {n: v for n in all_names if (v := self.cache.get(n)) is not None}

        # ── 6–8. Execute chunked batch with 0x06 retry ────────────────────────
        try:
            # BUG-10 FIX: record_request after batch so count is accurate
            fresh, chunk_rtt_ms = await self._execute_batch(stale_names, effective_timeout)
            if self.telemetry:
                self.telemetry.record_request(len(stale_names))

        except TimeoutError as err:
            # Defect D (v1.2.3): a queue shed is OUR contention, not the
            # inverter's fault. Everything downstream (back-off, stale-cache
            # fallback, entity availability) is deliberately shared with the
            # real-timeout path — only the adaptive-learning bookkeeping
            # differs, so the circadian model is never taught that internal
            # contention is inverter misbehaviour.
            if isinstance(err, ModbusQueueShed):
                self._record_shed()
                _LOGGER.debug(
                    "%s: request shed by bus guard (%s); not recorded as an "
                    "inverter failure", self.name, err,
                )
            else:
                self._record_timeout()

            if not isinstance(err, ModbusQueueShed) and self._consecutive_timeouts == 1:
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
            self._record_failure()
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
            self._record_failure()
            _LOGGER.warning(
                "%s: connection interrupted — another Modbus client may have connected.",
                self.device.serial_number,
            )
            raise UpdateFailed(
                f"Connection to {self.device.serial_number} interrupted.",
                retry_after=int(MODBUS_RETRY_BASE_WAIT.total_seconds()),
            ) from err

        except HuaweiSolarException as err:
            self._record_failure()
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
            # One observation per POLL (unchanged): n, failures, confidence and
            # the daily decay factor are all tuned against a per-poll rate.
            # Only the RTT *value* is corrected to per-request scale.
            self._adaptive.note_batch(self._last_batch_ms, self._last_chunk_count)
            # Bus-level metrics live on the shared guard (per endpoint) but are
            # surfaced through each controller (per serial) for visibility.
            try:
                wait_p95, service_p95 = self.guard.wait_service_split()
                self._adaptive.note_bus_metrics(
                    self.guard.occupancy() * 100.0,
                    wait_p95,
                    service_p95,
                    requests_waited=self.guard.requests_waited,
                    total_wait_s=self.guard.total_wait_ms / 1000.0,
                )
            except Exception:  # noqa: BLE001 — instrumentation is never critical
                _LOGGER.debug("%s: bus metric update failed", self.name, exc_info=True)
            self._adaptive.record_request(chunk_rtt_ms, success=True, timeout=False)

        # ── 11. Suspicious-zero guard for energy counters ─────────────────────
        # A live Modbus read can return 0 for kWh accumulators during inverter
        # sleep entry (~sunset), startup flash, or state-transition races even
        # though no timeout occurred.  The stale-cache exclusion (step 9) only
        # covers the timeout path; this guard covers the success path.
        #
        # Rule: if an energy-counter register comes back as 0 from a live read,
        # AND the cache already holds a non-zero value for that register, drop
        # it from 'fresh'.  The sensor entity will not find it in coordinator.data
        # and will mark itself unavailable — an honest gap that HA interpolates
        # correctly, consistent with the v1.0.3 design philosophy.
        #
        # A genuine midnight reset (daily_yield going 0→0 or decreasing
        # naturally to 0 as production ends) is NOT affected: in that case the
        # cached prior value is already at or near 0 so the guard does not fire.
        for _name in list(fresh):
            if is_energy_counter(_name):
                _result = fresh[_name]
                if _result is not None and _result.value == 0:
                    _prior = self.cache.get(_name)
                    if (
                        _prior is not None
                        and _prior.value is not None
                        and _prior.value > 0
                    ):
                        _LOGGER.debug(
                            "%s: suspicious zero dropped for energy counter '%s' "
                            "(prior cached value: %s kWh) — marking unavailable",
                            self.name, _name, _prior.value,
                        )
                        del fresh[_name]
                        # Invalidate the cached entry as well; otherwise
                        # cache.merge() would re-inject the stale prior value and
                        # the sensor would show a flat value instead of going
                        # unavailable (the honest-gap behaviour we want, matching
                        # the timeout-path exclusion in step 9).
                        self.cache.invalidate(_name)

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

    def _record_failure(self) -> None:
        """Record a non-timeout Modbus failure.

        Defined here because HuaweiSolarOptimizerUpdateCoordinator is a SIBLING
        of HuaweiSolarUpdateCoordinator, not a subclass — it cannot use that
        class's helpers. Before v1.2.4 these call sites raised AttributeError
        instead of recording the failure, masking the real error behind a
        confusing traceback whenever an optimizer read failed.
        """
        self._consecutive_failures += 1
        if self.telemetry:
            self.telemetry.record_failure()
        if self._adaptive:
            self._adaptive.record_request(0.0, success=False, timeout=False)

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
            # Defect D. NOTE: HuaweiSolarOptimizerUpdateCoordinator is a
            # SIBLING of HuaweiSolarUpdateCoordinator, not a subclass, so the
            # _record_* helpers defined there are NOT available here. The
            # bookkeeping is therefore done inline (v1.2.4).
            is_shed = isinstance(err, ModbusQueueShed)
            self._consecutive_timeouts += 1
            self._consecutive_failures += 1
            if self.telemetry:
                self.telemetry.record_timeout()
            if self._adaptive:
                if is_shed:
                    # Internal contention — diagnostics only, never learning.
                    self._adaptive.note_shed()
                else:
                    self._adaptive.record_request(0.0, success=False, timeout=True)
            if not is_shed and self._consecutive_timeouts == 1:
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
            self._record_failure()
            _LOGGER.warning("Optimizer %s: connection interrupted.", self.device.serial_number)
            raise UpdateFailed(
                f"Connection to {self.device.serial_number} interrupted.",
                retry_after=int(MODBUS_RETRY_BASE_WAIT.total_seconds()),
            ) from err

        except DecodeError as err:
            self._record_failure()
            raise UpdateFailed(
                f"Could not decode optimizer data from {self.device.serial_number}: {err}.",
                retry_after=15 * 60,
            ) from err

        except HuaweiSolarException as err:
            self._record_failure()
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
    entry: Any = None,
) -> HuaweiSolarOptimizerUpdateCoordinator:
    coordinator = HuaweiSolarOptimizerUpdateCoordinator(
        hass, _LOGGER,
        device=device,
        optimizer_device_infos=optimizer_device_infos,
        name=f"{device.serial_number}_optimizer_data_update_coordinator",
        update_interval=update_interval,
        bus_endpoint=bus_endpoint,
    )

    # v1.3.8 FIX (Defect G): this used to be `await
    # coordinator.async_config_entry_first_refresh()` -- a full, blocking,
    # real Modbus read of every optimizer's registers, awaited on the entry
    # setup critical path, for EVERY inverter that has optimizers, before
    # `async_setup_entry` could return. On an installation with many
    # optimizers this is a real contributor to multi-minute setup times (see
    # AUDIT_1.3.8.md), and it runs again in full on every reload. No other
    # coordinator in this codebase is awaited like this in setup; entities
    # simply show unavailable until the coordinator's own first scheduled
    # refresh completes, exactly as for the main/battery/power-meter/config
    # coordinators. This brings the optimizer coordinator in line with that
    # existing pattern instead of being the exception, using the same
    # background-task idiom already established for battery-health init
    # (see _async_setup_battery_health).
    #
    # Trade-off, stated plainly: this coordinator no longer raises
    # ConfigEntryNotReady if the optimizer read fails on the very first
    # attempt. Given the primary device connection has already succeeded by
    # the time this factory runs, a failure here is far more likely to be
    # "this specific file read timed out" than "the device is unreachable" --
    # and the coordinator's normal retry/backoff handles that case the same
    # way it already handles any later transient failure.
    async def _first_refresh() -> None:
        try:
            await coordinator.async_config_entry_first_refresh()
        except Exception:  # noqa: BLE001 — background task must not raise
            _LOGGER.exception(
                "optimizer_coordinator[%s]: first refresh failed; optimizer "
                "sensors will report unknown until the next scheduled poll. "
                "All other entities are unaffected",
                device.serial_number,
            )

    try:
        create_task = getattr(entry, "async_create_background_task", None)
        if create_task is not None:
            create_task(
                hass, _first_refresh(),
                f"optimizer_coordinator_first_refresh_{device.serial_number}",
            )
        else:  # pragma: no cover — older HA cores, or no entry passed
            hass.async_create_task(_first_refresh())
    except Exception:  # noqa: BLE001 — never break entry setup over scheduling
        _LOGGER.exception(
            "optimizer_coordinator[%s]: could not schedule first refresh",
            device.serial_number,
        )

    return coordinator
