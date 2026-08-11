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

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .adaptive_modbus import AdaptiveModbusController
from .const import (
    BACKOFF_NORMAL_DIVISOR,
    REGISTER_STARVATION_CEILING_S,
    ENERGY_AVAILABILITY_CEILING_S,
    ENERGY_PROMOTION_CEILING_S,
    REGISTER_STARVATION_PROMOTIONS_PER_CYCLE,
    BATCH_CHUNK_SIZE,
    BATCH_INTER_CHUNK_PAUSE,
    BATCH_POLL_DEADLINE,
    BUSY_MAX_RETRIES,
    BUSY_RETRY_PAUSE,
    MAX_CONSECUTIVE_TIMEOUTS,
    MODBUS_RETRY_BASE_WAIT,
    MODBUS_RETRY_MAX_WAIT,
    MIN_BACKOFF_JITTER_S,
    NIGHT_POLL_INTERVAL,
    OPTIMIZER_UPDATE_TIMEOUT,
    UPDATE_TIMEOUT,
    WRITE_VERIFY_DELAY,
    WRITE_VERIFY_RETRIES,
)
from .modbus_guard import ModbusAdmissionTimeout, ModbusGuard, ModbusQueueShed
from .modbus_telemetry import ModbusTelemetry
from .night_mode import InverterMode, NightModeDetector
from .register_cache import (
    Quality,
    Reason,
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
    """Exponential back-off delay with jitter.

    v2.0.0a FIX (F11, external ICS audit -- confirmed, with a real
    refinement found while checking it): the original concern was framed
    as "independent coordinator backoff can create synchronized wakeups",
    which reads as if no jitter existed at all -- it did (±10%
    proportional), so that framing itself was corrected during
    verification. But checking the ACTUAL numbers this formula produces
    surfaced a genuine, different problem: proportional-only jitter is
    weakest exactly where a common-failure scenario needs it most. At
    consecutive=1 (the FIRST retry after several coordinators experience
    the SAME shared bus/device failure simultaneously), delay=base=10s
    and jitter is only ±1s -- every affected coordinator wakes up within
    a roughly 2-second window of each other, right at the point where
    they're all retrying together for the first time. The jitter only
    widens in absolute terms as backoff deepens (±12s at the 120s cap),
    which is the opposite of where the risk is concentrated.

    Fixed with a minimum ABSOLUTE jitter floor, not just a proportional
    one: MIN_BACKOFF_JITTER_S guarantees a meaningful spread even at the
    shortest delays, while leaving deep backoff's already-adequate
    proportional jitter untouched (it already exceeds the floor there).
    """
    base = MODBUS_RETRY_BASE_WAIT.total_seconds()
    cap  = MODBUS_RETRY_MAX_WAIT.total_seconds()
    delay = min(base * math.pow(2, consecutive - 1), cap)
    jitter_magnitude = max(delay * 0.10, MIN_BACKOFF_JITTER_S)
    jitter = jitter_magnitude * (2 * random.random() - 1)
    return max(0.0, delay + jitter)


def _pick_backoff_canary(
    candidates: list[RegisterName], cache: RegisterCache
) -> RegisterName | None:
    """Pick one register to force a genuine Modbus attempt during back-off,
    when normal filtering would otherwise let a whole poll cycle pass
    without trying anything at all (Defect T, v1.3.17).

    Without this, a coordinator that has entered back-off can become
    permanently wedged there: every early-return path in
    `_async_update_data` ("everything's still within its cache TTL",
    "nothing FAST-tier due this particular cycle") reports success to Home
    Assistant without ever attempting real communication, so the device's
    actual recovery is never observed and the back-off state is never
    reset. Forcing one cheap, real attempt every cycle closes that hole:
    if the device answers, the real success path resets back-off
    correctly; if it doesn't, the existing failure handling applies
    exactly as it already does for a genuine timeout.

    Prefers a FAST-tier register (cheapest, and consistent with back-off's
    existing "only FAST tier" policy for genuinely due registers) so this
    does not undermine the deliberate SLOW/STATIC deferral back-off relies
    on to reduce load. Falls back to any available register rather than
    reading nothing, since testing the connection at all is the point.
    """
    for n in candidates:
        if cache.tier_of(n) == RegisterTier.FAST:
            return n
    return candidates[0] if candidates else None


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
        entry: ConfigEntry | None = None,
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

        # v1.3.14 FIX (Defect L, part 1): the entry is threaded through so
        # the deferred first-poll task (below) can be tied to the entry's
        # own lifecycle rather than outliving it. See
        # _schedule_deferred_first_poll for the full explanation.
        #
        # v1.3.15 (Defect P): the same unload callback also removes this
        # device's contribution from the shared guard's aggregate (see
        # _on_entry_unload), so a torn-down device's learned parameters
        # cannot keep influencing the bus after it's gone.
        self._entry = entry
        self._shutdown = False
        if entry is not None:
            entry.async_on_unload(self._on_entry_unload)

        # Bus-level guard (shared by all coordinators on the same RS485 bus)
        endpoint = bus_endpoint or device.serial_number
        self.guard = ModbusGuard.get_or_create(endpoint)

        self.cache = RegisterCache(
            starvation_ceiling_s=REGISTER_STARVATION_CEILING_S,
            energy_availability_ceiling_s=ENERGY_AVAILABILITY_CEILING_S,
        )
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

    def _record_admission_timeout(self) -> None:
        """Record a bus-admission timeout — NOT as inverter misbehaviour.

        v2.0.0b (MOD-09, external ICS audit -- confirmed). The sibling of
        _record_shed() above, for the other congestion outcome
        (ModbusAdmissionTimeout, v2.0.0a's F08 fix): admitted to wait for
        the bus, but the wait itself exceeded QUEUE_WAIT_TIMEOUT before
        the device was ever contacted. Same reasoning as _record_shed():
        this says nothing about the inverter, so it must not enter the
        adaptive circadian model. Consecutive counters and telemetry
        still advance identically to a genuine timeout, so back-off and
        entity availability behave exactly as before -- only the
        adaptive-learning bookkeeping differs.
        """
        self._consecutive_timeouts += 1
        self._consecutive_failures += 1
        if self.telemetry:
            self.telemetry.record_timeout()
        if self._adaptive:
            self._adaptive.note_admission_timeout()   # diagnostics only, NOT a failure

    def _record_failure(self) -> None:
        """Record a non-timeout Modbus failure to all observers (single dispatch)."""
        self._consecutive_failures += 1
        if self.telemetry:
            self.telemetry.record_failure()
        if self._adaptive:
            self._adaptive.record_request(0.0, success=False, timeout=False)

    def _classify_failure(self, exc: Exception) -> tuple["Quality", "Reason"]:
        """Map a chunk-execution exception to (Quality, Reason) for
        record_attempt(). v2.0.0 -- covers exactly the exception types this
        coordinator's outer handlers already distinguish (TimeoutError incl.
        ModbusQueueShed, ReadException incl. the 0x06 DEVICE_BUSY case,
        ConnectionInterruptedException, and the HuaweiSolarException
        catch-all), in the same most-specific-first order, so the mapping
        stays consistent with the coordinator-level failure handling it
        sits alongside. See V2_ARCHITECTURE_DESIGN.md §5 for the reasoning
        behind each Reason value.

        v2.0.0b FIX (MOD-09, external ICS audit -- confirmed): a bus-
        admission timeout (ModbusAdmissionTimeout, v2.0.0a's F08 fix) is
        ALSO a TimeoutError subclass, exactly like ModbusQueueShed --
        checked here before the generic TimeoutError branch for the same
        reason SHED already is: without this, an admission timeout was
        indistinguishable from a genuine device timeout, teaching every
        downstream consumer of this cache entry's Reason (most
        importantly the adaptive learner, via _record_timeout() in the
        outer handler below) that internal bus contention was the
        inverter misbehaving.
        """
        if isinstance(exc, ModbusQueueShed):
            return Quality.UNCERTAIN, Reason.SHED
        if isinstance(exc, ModbusAdmissionTimeout):
            return Quality.UNCERTAIN, Reason.ADMISSION_TIMEOUT
        if isinstance(exc, TimeoutError):
            return Quality.UNCERTAIN, Reason.TIMEOUT
        if isinstance(exc, ReadException):
            if getattr(exc, "modbus_exception_code", None) == _EXC_SLAVE_DEVICE_BUSY:
                return Quality.UNCERTAIN, Reason.DEVICE_BUSY
            # Any other ReadException (illegal data address, a connection
            # problem surfaced through this type, etc.) -- LINK_DOWN is the
            # most general "something about the transport/device is wrong"
            # bucket, since it isn't specifically a timeout, shed, or busy
            # response.
            return Quality.UNCERTAIN, Reason.LINK_DOWN
        # ConnectionInterruptedException, HuaweiSolarException catch-all,
        # or anything unanticipated -- still record SOMETHING rather than
        # silently leaving the entry's quality stale.
        return Quality.UNCERTAIN, Reason.LINK_DOWN

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

        v2.0.0 (V2_ARCHITECTURE_DESIGN.md §5.2) -- BEST-EFFORT PER CHUNK,
        restructured from the previous all-or-nothing behaviour. Traced
        against the pre-rebuild code before this change was made: the
        specific failing chunk's register names were only ever in scope
        INSIDE this per-chunk loop -- a bare `raise` propagated the
        exception out of this whole function, discarding both (a) which
        specific chunk failed (the caller only ever saw `stale_names`, the
        full requested set across every chunk) and (b) any already-succeeded
        earlier chunks' fresh results in the same batch (a local `merged`
        that never got returned). Both are fixed here: every chunk's
        outcome -- success or failure -- is recorded into the cache
        immediately, per chunk, right where the chunk's own register names
        are genuinely in scope, and a failing chunk no longer aborts chunks
        that haven't been attempted yet.

        The coordinator-level back-off/consecutive-failure state machine
        still needs an overall pass/fail signal for THIS cycle, unchanged
        from before -- so if any chunk failed, the FIRST such failure is
        still raised, but only after every chunk has had a chance to run,
        not instead of running them.

        Returns merged results from all successfully-completed chunks.
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
        # (the underlying batch_update() caps at 64 addresses of SPAN, not
        # count, so a sparse but very wide group could in principle contain
        # more names than is comfortable to hold in one PDU).
        #
        # v2.0.0b (MOD-11, external ICS audit): the audit recommended
        # driving this split directly from the PDU-span constraint instead
        # of a fixed count, reasoning that count-based splitting can
        # fragment a group that would otherwise fit in one physical
        # exchange. Checked against this file's own history before
        # implementing it, and found the recommendation rests on an
        # incomplete premise: BATCH_CHUNK_SIZE's own documented purpose
        # (const.py) is not "does it fit in one PDU" at all -- it exists
        # specifically to keep a single burst under ~300ms of inverter CPU
        # time, a documented trigger for 0x06 BUSY responses, independent
        # of whether the group is span-compliant. A group can satisfy
        # _ADDRESS_GROUP_MAX_SPAN (64) while still containing enough
        # densely-packed registers to exceed that CPU-time budget -- a
        # purely span-driven split would not catch that case, and could
        # reintroduce the exact BUSY-response problem this constant was
        # field-validated to prevent. NOT implemented as recommended for
        # this reason; a genuine dual-constraint split (respecting both
        # span AND a CPU-time-derived count) remains a real, open
        # question, but changing this without the field data
        # BATCH_CHUNK_SIZE's own value of 40 was originally validated
        # against risks trading a confirmed problem for an unconfirmed
        # fix. Deliberately deferred, not silently dropped -- see
        # AUDIT_2.0.0b.md.
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
        first_failure: Exception | None = None
        # v2.0.0a (F03, external ICS audit -- confirmed): tracks which
        # register names have actually been accounted for -- either
        # succeeded (in merged) or had their own failure explicitly
        # classified via record_attempt() below. Anything left over after
        # the loop exits, for ANY reason, gets picked up by the
        # reconciliation check after the loop -- this is deliberately not
        # tied to "which chunk was in flight when the deadline fired",
        # which would need fragile bookkeeping of exactly where inside a
        # chunk's own retry loop the cancellation landed. Tracking what
        # WAS recorded and diffing against what SHOULD have been is robust
        # regardless of exactly where the cut-off happens.
        recorded_names: set[RegisterName] = set()

        try:
            # v2.0.0a (F03): the whole-poll deadline. Each chunk already has
            # its own per-chunk timeout (effective_timeout) and BUSY retries
            # add further delay on top, but nothing previously bounded the
            # SUM across every chunk in one poll -- a poll with many chunks
            # could consume roughly chunks * per-chunk-timeout before ever
            # reporting a failure. This wraps the ENTIRE loop, not each
            # chunk individually (that's what effective_timeout already
            # does); asyncio.timeout() nests correctly with the per-chunk
            # one below -- `poll_cm.expired` after the fact reliably
            # distinguishes "this outer deadline fired" from any other
            # TimeoutError.
            async with asyncio.timeout(BATCH_POLL_DEADLINE.total_seconds()) as poll_cm:
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
                            # v2.0.0: record this chunk's success into the cache
                            # immediately, per chunk -- not deferred to a single
                            # cache.update(fresh) call after the whole batch
                            # completes, which would never run at all for a chunk
                            # that succeeded before a LATER chunk failed under the
                            # old all-or-nothing control flow.
                            self.cache.update(chunk_result)
                            recorded_names.update(chunk)
                            chunk_ms = (time.monotonic() - t0) * 1000
                            total_batch_ms += chunk_ms
                            max_chunk_rtt_ms = max(max_chunk_rtt_ms, chunk_ms)
                            chunk_count += 1
                            # v2.0.0a FIX (F15, external ICS audit -- confirmed):
                            # record_request()'s own docstring says "record ONE
                            # completed Modbus request" and delegates to a
                            # rolling time-slot accumulator with a bounded
                            # sample size -- it was designed to be called per
                            # TRANSACTION, not once per POLL. The old call site
                            # (after this whole method returns, using only
                            # max_chunk_rtt_ms) fed the learner exactly one
                            # observation per poll regardless of chunk count,
                            # using only the WORST chunk's RTT -- a poll with 20
                            # chunks and a poll with 1 chunk looked identical to
                            # the learner despite very different actual bus
                            # cost, and the other 19 chunks' RTTs were simply
                            # discarded. Moved inline, per chunk, mirroring the
                            # exact same pattern already used for
                            # cache.update()/record_attempt() above -- this
                            # does NOT touch the separate poll-level-health call
                            # further below (n, failures, confidence, decay are
                            # correctly tuned against a per-poll RATE and stay
                            # exactly as they were).
                            if self._adaptive:
                                self._adaptive.record_request(chunk_ms, success=True, timeout=False)
                            break  # chunk succeeded

                        except (TimeoutError, ReadException, ConnectionInterruptedException,
                                HuaweiSolarException) as exc:
                            if (
                                isinstance(exc, ReadException)
                                and getattr(exc, "modbus_exception_code", None) == _EXC_SLAVE_DEVICE_BUSY
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

                            # Non-BUSY failure, or retries exhausted for this chunk.
                            # v2.0.0: record it into the cache right here, where
                            # `chunk`'s specific register names are genuinely in
                            # scope, then move on to the NEXT chunk instead of
                            # aborting the rest of the batch (best-effort, per
                            # V2_ARCHITECTURE_DESIGN.md §5.2's explicit decision).
                            # BUG-4's original intent (don't double-record failure
                            # at both this level and the caller's) is preserved:
                            # this only touches the CACHE's per-register quality,
                            # not the coordinator's own _consecutive_* counters --
                            # those still come from the caller re-raising
                            # first_failure below, unchanged from before.
                            quality, reason = self._classify_failure(exc)
                            self.cache.record_attempt(chunk, quality, reason, time.monotonic())
                            recorded_names.update(chunk)
                            # v2.0.0a (F15): the learner should see this chunk's
                            # genuine failure too, not just successes -- a
                            # complete transaction-level picture, not a
                            # success-only one. `timeout` derived from `reason`
                            # (already computed above via _classify_failure()),
                            # not a separate isinstance(exc, TimeoutError) check
                            # -- ModbusQueueShed/ModbusAdmissionTimeout are ALSO
                            # TimeoutError subclasses (deliberately, so existing
                            # generic except-TimeoutError paths keep working),
                            # so that check alone would misclassify a shed or an
                            # admission-wait as a genuine device timeout here.
                            if self._adaptive:
                                self._adaptive.record_request(
                                    0.0, success=False, timeout=(reason == Reason.TIMEOUT),
                                )
                            if first_failure is None:
                                first_failure = exc
                            break  # move on to the next chunk, not the next retry
        except TimeoutError:
            if not poll_cm.expired:
                # Defensive: shouldn't normally happen (the per-chunk timeout
                # is caught inside the loop's own try/except above), but if
                # some other TimeoutError somehow escapes uncaught, don't
                # silently misattribute it to the whole-poll deadline.
                raise
            _LOGGER.warning(
                "%s: whole-poll deadline (%.0fs) exceeded after %d/%d chunks "
                "-- stopping, remaining registers deferred to next cycle",
                self.name, BATCH_POLL_DEADLINE.total_seconds(),
                chunk_count, len(chunks),
            )

        self._last_batch_ms = total_batch_ms
        self._last_chunk_count = chunk_count

        # v2.0.0a (F03): reconciliation -- any register that never got
        # recorded, for any reason (the whole-poll deadline being the only
        # one currently possible, but this is deliberately not coupled to
        # that specific cause), is explicitly marked UNCERTAIN/TIMEOUT here
        # rather than silently vanishing from quality tracking. Preserves
        # successful partial results (already in `merged`/the cache via the
        # per-chunk inline update above) and returns control to the
        # coordinator's own back-off logic via the same first_failure
        # contract as every other failure path in this method.
        unrecorded = [n for n in names if n not in recorded_names]
        if unrecorded:
            self.cache.record_attempt(
                unrecorded, Quality.UNCERTAIN, Reason.TIMEOUT, time.monotonic(),
            )
            if first_failure is None:
                first_failure = TimeoutError(
                    f"{self.name}: whole-poll deadline "
                    f"({BATCH_POLL_DEADLINE.total_seconds():.0f}s) exceeded after "
                    f"{chunk_count}/{len(chunks)} chunks -- "
                    f"{len(unrecorded)} register(s) deferred"
                )

        if first_failure is not None:
            # The coordinator-level back-off/consecutive-failure state
            # machine still needs an overall pass/fail signal for this
            # cycle -- unchanged from the pre-rebuild contract. The
            # difference from before is WHEN this fires: only after every
            # chunk has had its chance to run, not instead of running them.
            raise first_failure
        # Return the PER-REQUEST figure: this is what the adaptive controller
        # consumes. The batch total is retained for diagnostics only.
        return merged, max_chunk_rtt_ms

    # ── poll logic ────────────────────────────────────────────────────────────

    def _on_entry_unload(self) -> None:
        """Called when this coordinator's config entry unloads (Defect L,
        extended for Defect P). Two independent cleanup actions:

        1. Sets self._shutdown, guarding the deferred first-poll task
           against firing on a coordinator whose entry is already gone
           (see _schedule_deferred_first_poll) -- a second, independent
           layer on top of that task's own cancellation.
        2. Removes this device from the shared ModbusGuard's aggregate
           (see ModbusGuard.remove_source) so its last-reported gap/queue-
           depth parameters stop influencing the bus once the device is
           torn down.
        """
        self._shutdown = True
        self.guard.remove_source(self.device.serial_number)

    def _schedule_deferred_first_poll(self) -> None:
        """Run the actual first poll, after the stagger delay, as a
        background task instead of sleeping inline (see Defect K).

        v1.3.14 FIX (Defect L, part 1): this used to call
        `self.hass.async_create_task(_deferred())` with no stored handle
        and no tie to the config entry's lifecycle. If the entry reloaded
        or unloaded before the stagger delay elapsed, the old task kept
        running regardless, and its eventual `async_request_refresh()`
        call would run against a coordinator no longer considered "active"
        by anything -- yet still holding a real, guard-registered
        ModbusGuard reference (the SAME shared guard every other
        coordinator on this bus uses, since Defect J1), meaning a stale
        task could inject uncoordinated Modbus traffic at exactly the
        moment a fresh setup attempt is trying to establish itself.

        Fixed with two independent layers:
        1. `entry.async_create_background_task()` (falling back to
           `hass.async_create_task()` if no entry was provided) instead of
           a bare, untracked task -- Home Assistant cancels
           entry-scoped background tasks automatically on unload/reload,
           which is the primary fix.
        2. An explicit `self._shutdown` check inside the deferred
           coroutine itself, set by `_on_entry_unload()` via
           `entry.async_on_unload()`, as a second, independent guard
           against the narrow race where the sleep completes right as
           unload begins but before task cancellation has propagated.
        """

        async def _deferred() -> None:
            try:
                await asyncio.sleep(self._start_delay.total_seconds())
                if self._shutdown:
                    _LOGGER.debug(
                        "%s: deferred first-poll skipped -- entry unloaded "
                        "before the stagger delay elapsed", self.name,
                    )
                    return
                await self.async_request_refresh()
            except Exception:  # noqa: BLE001 — background task must not raise
                _LOGGER.exception(
                    "%s: deferred first-poll refresh failed; will retry on "
                    "the coordinator's normal schedule", self.name,
                )

        create_task = getattr(self._entry, "async_create_background_task", None)
        if self._entry is not None and create_task is not None:
            create_task(
                self.hass, _deferred(), f"{self.name}_deferred_first_poll"
            )
        else:  # pragma: no cover — no entry provided, or an older HA core
            self.hass.async_create_task(_deferred())

    def create_background_task(self, coro, name: str) -> None:
        """Schedule *coro* as an entry-scoped background task.

        v2.0.0b (MOD-10, external ICS audit -- confirmed): write
        verification (number/select/switch, after F12 wired verify_write()
        in) used a bare `self.hass.async_create_task(...)`, not this same
        entry-scoped pattern the deferred first-poll task above already
        uses. A bare task can survive an entry reload and perform a
        delayed Modbus read against stale lifecycle state (the transport
        already disconnected, the guard already released) -- exactly the
        class of problem `entry.async_create_background_task()` exists to
        prevent, by tying the task's lifetime to the entry's own and
        cancelling it automatically on unload/reload. Extracted here, once,
        so entities calling this don't each need to re-derive the same
        getattr/fallback dance already established above.
        """
        create_task = getattr(self._entry, "async_create_background_task", None)
        if self._entry is not None and create_task is not None:
            create_task(self.hass, coro, name)
        else:  # pragma: no cover — no entry provided, or an older HA core
            self.hass.async_create_task(coro)

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
            self.guard.update_gap(self.device.serial_number, params.request_gap.total_seconds())
            self.guard.update_max_queue_depth(self.device.serial_number, params.max_queue_depth)
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

        # v1.3.17 FIX (Defect T): computed early (before either early-return
        # below) so both can be guarded against the same hazard -- see the
        # canary-forcing logic just below for the full explanation.
        in_backoff = self._consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS

        # ── 4. Fully cached ───────────────────────────────────────────────────
        if not stale_names:
            if in_backoff:
                # v1.3.17 FIX (Defect T): while genuinely in back-off, this
                # coordinator must never go a full cycle without attempting
                # AT LEAST ONE real Modbus exchange -- otherwise it can
                # remain wedged in back-off forever. This exact scenario was
                # confirmed in the field: every register happened to still
                # be within its cache TTL, so this branch returned the
                # cached snapshot immediately, Home Assistant logged
                # "success" (no exception was raised), and
                # _consecutive_timeouts/_backoff_cycle were never reset --
                # because that reset only happens after a REAL attempt
                # completes, further down in this function, which this
                # early return skips entirely. The coordinator therefore
                # had no way to ever discover the device had recovered, and
                # stayed in back-off indefinitely -- observed directly as a
                # back-off cycle counter climbing into double digits with
                # no failure logged in between, while most non-FAST-tier
                # entities remained permanently `unknown` (back-off, by
                # design, defers NORMAL/SLOW/STATIC registers entirely; see
                # step 5 below -- so a coordinator that can never leave
                # back-off can never read them again).
                #
                # Fixed by forcing a single, cheap register through as a
                # genuine test of the connection whenever back-off would
                # otherwise let a full cycle pass without trying anything at
                # all. If the device answers, this reaches the real success
                # path further down and correctly resets back-off state. If
                # it doesn't, the existing failure handling applies exactly
                # as it already does elsewhere in this function -- back-off
                # correctly continues, because the device genuinely hasn't
                # recovered yet.
                canary = _pick_backoff_canary(all_names, self.cache)
                if canary is not None:
                    stale_names = [canary]
            if not stale_names:
                _LOGGER.debug(
                    "%s: %d register(s) all cached — skipping Modbus [night=%s]",
                    self.name, len(all_names), self.cache.night_mode,
                )
                if self.telemetry:
                    self.telemetry.record_skipped_poll()
                return {n: v for n in all_names if (v := self.cache.get(n)) is not None}

        # ── 5. Priority polling during back-off (opt. 6) ─────────────────────
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

            # v2.0.0 (V2_ARCHITECTURE_DESIGN.md §10.5): snapshot the
            # pre-filter set NOW, before it's lost -- `stale_names` gets
            # reassigned to the filtered `priority_names` a few lines below,
            # and by then there is no way to recover "what was due before
            # deferral" to know which specific registers need a
            # BACKOFF_DEFERRED quality recording.
            pre_filter_names = list(stale_names)

            # Filter stale_names by priority: FAST always, NORMAL every Nth cycle,
            # SLOW/STATIC deferred until recovery -- UNLESS a register has gone
            # unread for more than REGISTER_STARVATION_CEILING_S past its own
            # due-time (Defect Y, v1.3.21): back-off's SLOW/STATIC deferral had
            # no ceiling of its own, and field evidence showed a register can go
            # entirely unread for the better part of two hours under sustained
            # contention. Every affected register is read-only telemetry, so an
            # upper bound on staleness is worth more here than strict tier
            # purity during a rough patch. Starved candidates are collected
            # separately and only the single most-overdue one promoted per
            # cycle (REGISTER_STARVATION_PROMOTIONS_PER_CYCLE) -- see const.py
            # for why: several SLOW/STATIC registers read together originally
            # tend to cross the ceiling within moments of each other, and
            # promoting all of them at once would inject a burst of expensive
            # reads into a cycle that is, by definition, already struggling.
            priority_names: list[RegisterName] = []
            starved: list[tuple[float, RegisterName]] = []
            for n in stale_names:
                # v1.3.21 (Defect Y): classify_register(n), not
                # self.cache.tier_of(n) -- tier_of() only knows a tier for a
                # register that's already been cached at least once. A
                # register that has NEVER been successfully read (tier_of
                # returns None) would otherwise fall through to the
                # SLOW/STATIC starvation path below regardless of its real
                # tier, meaning a brand-new FAST-tier register could be
                # capped to one promotion per cycle instead of always being
                # included. classify_register() is a pure, name-based
                # classification (see register_cache.py) that works
                # correctly whether or not the register has ever been cached.
                tier = classify_register(n)
                if tier == RegisterTier.FAST:
                    priority_names.append(n)
                elif tier == RegisterTier.NORMAL:
                    if self._backoff_cycle % BACKOFF_NORMAL_DIVISOR == 0:
                        priority_names.append(n)
                else:
                    # SLOW/STATIC: deferred by default, but track how overdue
                    # each one is so the worst offender(s) can still break
                    # through the deferral below.
                    #
                    # v2.0.0 (V2_ARCHITECTURE_DESIGN.md §8.1): energy counters
                    # get a TIGHTER promotion ceiling than everything else --
                    # try harder to get them fresh, quietly, before the
                    # (also lengthened, see RegisterCache's
                    # energy_availability_ceiling_s) availability ceiling
                    # ever has to matter. Scoped to this SLOW/STATIC path
                    # deliberately: most energy counters (accumulated/daily/
                    # monthly yield, etc.) classify as SLOW tier and are
                    # reachable here. A few (storage_total_charge/discharge)
                    # are NORMAL tier and go through the "every
                    # BACKOFF_NORMAL_DIVISOR-th cycle" branch above instead
                    # -- already meaningfully less deferred than SLOW/STATIC
                    # by design, and still protected by the same lengthened
                    # availability ceiling regardless of which path they
                    # take. Extending BACKOFF_NORMAL_DIVISOR itself to be
                    # register-type-aware would be a different, more
                    # invasive change not covered by this design pass.
                    overdue = self.cache.overdue_by(n)
                    ceiling = (
                        ENERGY_PROMOTION_CEILING_S
                        if is_energy_counter(n)
                        else REGISTER_STARVATION_CEILING_S
                    )
                    if overdue is None:
                        starved.append((float("inf"), n))  # never read at all
                    elif overdue >= ceiling:
                        starved.append((overdue, n))
            if starved:
                starved.sort(key=lambda item: item[0], reverse=True)
                promoted = [n for _, n in starved[:REGISTER_STARVATION_PROMOTIONS_PER_CYCLE]]
                if promoted:
                    # v2.0.0: the ceiling that actually applied varies per
                    # register now (energy counters use the tighter
                    # ENERGY_PROMOTION_CEILING_S) -- log each promoted
                    # register's own applicable ceiling rather than a single
                    # fixed value that would be wrong for some of them.
                    _LOGGER.info(
                        "%s: promoting %d starved SLOW/STATIC register(s) "
                        "past back-off deferral: %s",
                        self.name, len(promoted),
                        ", ".join(
                            f"{n} (overdue >= "
                            f"{ENERGY_PROMOTION_CEILING_S if is_energy_counter(n) else REGISTER_STARVATION_CEILING_S:.0f}s)"
                            for n in promoted
                        ),
                    )
                priority_names.extend(promoted)
            # v1.3.17 FIX (Defect T): the same hazard as step 4 above, one
            # level down -- priority-filtering stale_names down to nothing
            # (no FAST-tier register was stale, and this wasn't an Nth
            # NORMAL cycle) used to mean "read nothing, serve the cached
            # snapshot", silently extending back-off with no test of
            # recovery. Force a canary here too, drawn from the full
            # register set (not just stale_names, which by construction
            # contains no FAST-tier candidate in this branch) so the pick
            # still prefers a cheap FAST-tier register over an arbitrary
            # SLOW/STATIC one.
            if not priority_names:
                canary = _pick_backoff_canary(all_names, self.cache)
                if canary is not None:
                    priority_names = [canary]

            # v2.0.0 (§10.5): everything that was due before filtering, but
            # didn't survive it, was deliberately skipped this cycle by
            # design -- not a failure, but genuinely different from "quality
            # unchanged" too (see record_attempt()'s docstring). Recorded
            # AFTER the canary-forcing logic above (which can pull a
            # register in from all_names, not just pre_filter_names) and
            # BEFORE the reassignment below, using the set difference so a
            # canary drawn from outside pre_filter_names doesn't affect
            # which registers actually count as deferred.
            deferred = set(pre_filter_names) - set(priority_names)
            if deferred:
                self.cache.record_attempt(
                    list(deferred), Quality.UNCERTAIN, Reason.BACKOFF_DEFERRED,
                    time.monotonic(),
                )
            stale_names = priority_names
        else:
            self._backoff_cycle = 0

        if not stale_names:
            if self.telemetry:
                self.telemetry.record_skipped_poll()
            return {n: v for n in all_names if (v := self.cache.get(n)) is not None}

        # ── 6–8. Execute chunked batch with 0x06 retry ────────────────────────
        # v2.0.0a (F19, external ICS audit -- confirmed as a live regression,
        # not merely a risk needing confirmation): §5.2's best-effort chunking
        # made _execute_batch() call cache.update() INLINE, per chunk, before
        # this function ever reaches the suspicious-zero guard below. That
        # guard's whole purpose is to compare a fresh live zero against the
        # value cached BEFORE this poll -- but by the time it used to run,
        # the cache had already been overwritten with this cycle's own fresh
        # (possibly suspicious) zero, so `_prior.value > 0` could never be
        # true for any register actually read this cycle. The guard was
        # silently inert for the common case. Fixed by snapshotting the
        # pre-poll values HERE, before _execute_batch() runs and mutates the
        # cache, rather than reading the cache again afterward.
        _prior_energy_values: dict[RegisterName, Any] = {
            n: v.value
            for n in stale_names
            if is_energy_counter(n) and (v := self.cache.get(n)) is not None
        }

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
            #
            # v2.0.0b FIX (MOD-09, external ICS audit -- confirmed): this
            # used to be a two-way branch (ModbusQueueShed vs. everything
            # else), so ModbusAdmissionTimeout (v2.0.0a's F08 fix -- ALSO a
            # TimeoutError subclass) fell into the "else" branch and was
            # recorded as a genuine device timeout. Now a three-way branch,
            # matching _classify_failure()'s own reasoning above.
            if isinstance(err, ModbusQueueShed):
                self._record_shed()
                _LOGGER.debug(
                    "%s: request shed by bus guard (%s); not recorded as an "
                    "inverter failure", self.name, err,
                )
            elif isinstance(err, ModbusAdmissionTimeout):
                self._record_admission_timeout()
                _LOGGER.debug(
                    "%s: bus admission timed out (%s); device was never "
                    "contacted, not recorded as an inverter failure",
                    self.name, err,
                )
            else:
                self._record_timeout()

            if (
                not isinstance(err, (ModbusQueueShed, ModbusAdmissionTimeout))
                and self._consecutive_timeouts == 1
            ):
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

            # ── 9. Stale-cache fallback ──────────────────────────────────────
            # v2.0.0 (V2_ARCHITECTURE_DESIGN.md §8.1): an earlier draft of
            # this block manually withheld any energy counter whose quality
            # wasn't GOOD, layered on top of the cache's own logic. That's
            # now redundant AND wrong: RegisterCache._live_quality() already
            # applies a LONGER availability ceiling for energy counters
            # specifically (ENERGY_AVAILABILITY_CEILING_S, 600s, vs the
            # generic REGISTER_STARVATION_CEILING_S) before an UNCERTAIN
            # entry lazily becomes BAD/EXPIRED and stops being served by
            # cache.get() at all -- a manual GOOD-only gate here would
            # UNDERMINE that, forcing energy counters back to the stricter
            # policy the cache is specifically designed not to apply to
            # them. The policy now lives entirely in the cache layer, keyed
            # by register type via is_energy_counter(), not at this
            # consumption site -- so this fallback is simply "serve
            # whatever the cache is willing to serve," the same as every
            # other register, uniformly.
            cached_fallback = {
                n: v for n in all_names if (v := self.cache.get(n)) is not None
            }
            if cached_fallback:
                _LOGGER.debug(
                    "%s: stale-cache fallback — %d register(s) served",
                    self.name, len(cached_fallback),
                )
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

        # BUG-4 FIX (v2.0.0a: still exactly once here, but now ONLY for
        # poll-level health -- record_request() itself moved to per-chunk,
        # inline inside _execute_batch(), per F15 above).
        if self._adaptive:
            # One observation per POLL (unchanged): n, failures, confidence and
            # the daily decay factor are all tuned against a per-poll rate.
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
            # v2.0.0a (F15, external ICS audit -- confirmed): the old call
            # here -- record_request(chunk_rtt_ms, success=True,
            # timeout=False) -- fed exactly one observation per poll using
            # only the WORST chunk's RTT, discarding every other chunk's
            # own RTT entirely. Removed: every chunk now records its own
            # outcome inline, inside _execute_batch(), at the exact point
            # its own RTT is known -- keeping this call too would
            # double-count the last chunk's RTT on top of that.

        # ── 11. Suspicious-zero guard for energy counters ─────────────────────
        # A live Modbus read can return 0 for kWh accumulators during inverter
        # sleep entry (~sunset), startup flash, or state-transition races even
        # though no timeout occurred.  The stale-cache exclusion (step 9) only
        # covers the timeout path; this guard covers the success path.
        #
        # Rule: if an energy-counter register comes back as 0 from a live read,
        # AND the PRE-POLL cached value was non-zero (v2.0.0a: from the
        # _prior_energy_values snapshot taken before _execute_batch() ran --
        # see that snapshot's own comment for why the live cache can no
        # longer be read here directly), drop it from 'fresh'.  The sensor
        # entity will not find it in coordinator.data and will mark itself
        # unavailable — an honest gap that HA interpolates correctly,
        # consistent with the v1.0.3 design philosophy.
        #
        # A genuine midnight reset (daily_yield going 0→0 or decreasing
        # naturally to 0 as production ends) is NOT affected: in that case the
        # cached prior value is already at or near 0 so the guard does not fire.
        for _name in list(fresh):
            if is_energy_counter(_name):
                _result = fresh[_name]
                if _result is not None and _result.value == 0:
                    _prior_value = _prior_energy_values.get(_name)
                    if _prior_value is not None and _prior_value > 0:
                        _LOGGER.debug(
                            "%s: suspicious zero dropped for energy counter '%s' "
                            "(prior cached value: %s kWh) — marking unavailable",
                            self.name, _name, _prior_value,
                        )
                        del fresh[_name]
                        # Invalidate the cached entry as well; otherwise
                        # cache.merge() would re-inject the stale prior value and
                        # the sensor would show a flat value instead of going
                        # unavailable (the honest-gap behaviour we want, matching
                        # the timeout-path exclusion in step 9).
                        self.cache.invalidate(_name)

        # v2.0.0: cache.update(fresh) removed here -- every successfully-read
        # register was already recorded (GOOD, with correct adaptive-TTL-
        # stretch applied exactly once) inline, per chunk, inside
        # _execute_batch() itself. Calling update() again on the same data
        # here would compare each value against itself and always conclude
        # "unchanged", silently doubling the TTL-stretch rate for every
        # successful poll cycle. The energy-counter suspicious-zero check
        # just above still correctly overrides any bad entry via
        # cache.invalidate() regardless of this removal -- that call runs
        # synchronously, after _execute_batch() has already returned, with
        # no await in between, so there is no window where another
        # coroutine could observe the brief pre-correction state.
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
            self.guard.update_gap(self.device.serial_number, params.request_gap.total_seconds())
            self.guard.update_max_queue_depth(self.device.serial_number, params.max_queue_depth)
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
            #
            # v2.0.0b FIX (MOD-09, external ICS audit -- confirmed): the
            # same gap as HuaweiSolarUpdateCoordinator's own
            # _classify_failure()/outer-handler fix above -- this
            # coordinator has its own separate inline copy of the same
            # bookkeeping, so it needed the same fix applied separately.
            is_shed = isinstance(err, ModbusQueueShed)
            is_admission_timeout = isinstance(err, ModbusAdmissionTimeout)
            self._consecutive_timeouts += 1
            self._consecutive_failures += 1
            if self.telemetry:
                self.telemetry.record_timeout()
            if self._adaptive:
                if is_shed:
                    # Internal contention — diagnostics only, never learning.
                    self._adaptive.note_shed()
                elif is_admission_timeout:
                    # Also internal contention, not device misbehaviour —
                    # see note_admission_timeout()'s own docstring.
                    self._adaptive.note_admission_timeout()
                else:
                    self._adaptive.record_request(0.0, success=False, timeout=True)
            if not (is_shed or is_admission_timeout) and self._consecutive_timeouts == 1:
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

    # v1.3.15 FIX (Defect P): remove this device's contribution to the
    # shared guard's aggregate when the entry unloads -- see
    # ModbusGuard.remove_source and HuaweiSolarUpdateCoordinator's own
    # _on_entry_unload (this coordinator is a sibling class, not a
    # subclass, so the same cleanup is registered directly here instead).
    if entry is not None:
        entry.async_on_unload(
            lambda: coordinator.guard.remove_source(device.serial_number)
        )

    return coordinator
