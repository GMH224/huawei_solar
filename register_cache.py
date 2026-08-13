"""Register value cache for Huawei Solar.

The SUN2000 inverter reacts badly to excessive Modbus traffic.  Many registers
never change (rated power, serial number, battery capacity) and others change
slowly.  This module provides a time-aware, adaptive cache that:

  • Assigns TTLs per-register based on observed volatility (adaptive TTL).
  • Groups registers into STATIC / SLOW / NORMAL / FAST tiers.
  • Doubles a register's effective TTL every time its value is unchanged
    (up to a per-tier cap), and resets the TTL as soon as the value changes.
  • Tracks dirty flags so writes immediately invalidate the cache.
  • Reports hit/miss statistics to ModbusTelemetry via a single batched call.

Volatility tiers
----------------
STATIC   – Hardware constants that never change in normal operation:
           serial numbers, firmware versions, model names, rated power,
           battery pack capacities, manufacturer strings.
           Base TTL: 60 min.  Cap: never re-read after first successful read
           (effectively ∞ during a session; invalidated on reconnect).

SLOW     – Values that change at most once per day or once per event:
           daily/total energy counters, working mode, alarm status,
           temperature (changes slowly), SOH calibration status.
           Base TTL: 5 min.  Adaptive cap: 30 min.

NORMAL   – Typical sensor values: SOC, power, voltage, current.
           Base TTL: 30 s (== poll interval).  Adaptive cap: 5 min.

FAST     – High-priority real-time values: grid import/export power,
           battery charge/discharge power, PV input power.
           Base TTL: 0 (always read).  No adaptive stretching.

Adaptive TTL algorithm
----------------------
After each successful poll, for every register in SLOW or NORMAL tier:
  - If value UNCHANGED → new_ttl = min(current_ttl * ADAPTIVE_FACTOR, tier_cap)
  - If value CHANGED   → new_ttl = tier_base_ttl   (reset to minimum)

This means a stable reading (e.g. battery idle at 80 % SOC at night) will
organically slow its own polling from 30 s → 60 s → 120 s → … → 300 s,
while a changing reading stays at 30 s.

Night-mode interaction
----------------------
When the coordinator sets ``night_mode=True`` on the cache, the effective TTL
for NORMAL registers is stretched by NIGHT_TTL_MULTIPLIER (default 10×),
turning a 30 s poll into 300 s.  FAST registers are also stretched to 60 s
so the inverter is not completely silent.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from enum import IntEnum, auto
from functools import lru_cache
from typing import Any, TYPE_CHECKING

from huawei_solar import RegisterName, Result

if TYPE_CHECKING:
    from .modbus_telemetry import ModbusTelemetry

_LOGGER = logging.getLogger(__name__)


# ── Tier definitions ──────────────────────────────────────────────────────────

class RegisterTier(IntEnum):
    FAST   = auto()   # near-real-time power/grid values
    NORMAL = auto()   # standard 30 s poll
    SLOW   = auto()   # 5 min base, adaptive up to 30 min
    STATIC = auto()   # read once per session


# ── Quality model (v2.0.0) ─────────────────────────────────────────────────────
#
# See V2_ARCHITECTURE_DESIGN.md for the full design record; this is the
# implementation of that design, not a new decision.
#
# v1.3.21 and earlier conflated transport/link health with sensor/payload
# health via a single `dirty` boolean, used for two genuinely different
# situations: "we wrote to this, we KNOW it's now wrong" and "the link
# dropped, we don't know if this changed." `merge()` treated both
# identically, silently dropping a register from `coordinator.data`
# entirely whenever either was true and a refresh hadn't yet succeeded —
# the root cause of registers going `Unknown` after routine bus
# contention, independent of and in addition to genuine data loss.
#
# Vocabulary deliberately borrows from OPC UA's Good/Uncertain/Bad
# StatusCode severity model (verified against the real OPC UA spec, not
# assumed) but scoped down to exactly this integration's real failure
# modes -- not the full StatusCode taxonomy, and deliberately NOT OPC UA's
# dual SourceTimestamp/ServerTimestamp model either (this integration's
# polling architecture cannot honestly populate a distinct source
# timestamp; inventing one would be false precision).
class Quality(IntEnum):
    GOOD = auto()        # read succeeded within its tier's current cadence
    UNCERTAIN = auto()   # a real value exists; cannot currently verify it
    BAD = auto()         # no usable value -- never read, known-wrong, or expired


class Reason(IntEnum):
    """Only meaningful when quality != GOOD. Each value exists because it
    tells a consumer something genuinely different from the others -- see
    V2_ARCHITECTURE_DESIGN.md §5 for the full reasoning behind each one.
    """
    # UNCERTAIN
    SHED = auto()               # our own guard declined to admit the request
    # v2.0.0b (MOD-09, external ICS audit -- confirmed): distinct from
    # SHED (declined immediately, queue was full) and from TIMEOUT
    # (request sent, device never answered) -- this is "waited for the
    # bus, but the wait itself exceeded QUEUE_WAIT_TIMEOUT; the device
    # was never contacted". Conflating this with TIMEOUT (the original
    # bug this Reason exists to fix) taught the adaptive learner that
    # internal bus contention was device misbehaviour, exactly the
    # mistake SHED already exists to avoid for the sibling congestion
    # case -- see modbus_guard.py's ModbusAdmissionTimeout for the full
    # reasoning.
    ADMISSION_TIMEOUT = auto()
    BACKOFF_DEFERRED = auto()   # tier-based deferral during back-off, by design
    TIMEOUT = auto()            # request sent, no response within budget
    LINK_DOWN = auto()          # keep-alive-detected connection loss
    DEVICE_BUSY = auto()        # device explicitly responded busy (Modbus 0x06)
    # BAD
    NEVER_READ = auto()         # no cache entry exists at all
    WRITE_PENDING = auto()      # invalidated by our own write, not yet reread
    EXPIRED = auto()            # was UNCERTAIN, aged past the starvation ceiling


# Base TTLs (seconds)
#
# v1.3.3 — SLOW raised 300 s -> 900 s on field evidence.
#
# Reading a SLOW/STATIC register is not marginally more expensive than a
# FAST one; it is CATEGORICALLY more expensive. Measured over 3,400 requests:
#
#   chunk of FAST/NORMAL only  : ~6 ms regardless of size (18 registers: 6.2 ms)
#   chunk containing SLOW/STATIC: ~2,900 ms + 377 ms/register
#
# 99% of all Modbus service time on this installation was spent in the 20.7%
# of requests that touched SLOW-tier content; `data_update_coordinator` alone
# accounted for 52% of it. Tier separation (see _split_by_cost) stops those
# exchanges DELAYING time-critical reads, but only reducing their FREQUENCY
# reduces the total cost — and these are, by their own classification,
# registers that change slowly: temperatures, alarms, device status, daily and
# lifetime counters.
#
# 900 s is a deliberately moderate first step (~3x fewer expensive exchanges)
# rather than the 1800 s cap, so the effect can be measured before going
# further. Tunable via the options flow.
#
# v1.3.21 (Defect Y): FAST changed from 0.0 -> 3.0. A 0.0 TTL means "always
# due whenever the coordinator happens to wake up" -- in ordinary operation
# this is already bounded by the coordinator's own ~30s update_interval, so
# 0.0 mostly mattered on the EDGE cases: back-off's own accelerated retry
# cycling, or multiple overlapping refresh triggers, could re-request the
# exact same FAST-tier register only seconds apart with nothing gained.
# 3.0s is small enough that no dashboard consumer of "instantaneous" power
# could perceive the added staleness, while eliminating genuinely wasteful
# back-to-back re-reads -- deliberately modest, since this exists specifically
# to fund Defect Y's starvation ceiling below without increasing net bus
# demand, not to meaningfully throttle responsiveness on its own.
_TIER_BASE_TTL: dict[RegisterTier, float] = {
    RegisterTier.FAST:   3.0,
    RegisterTier.NORMAL: 30.0,
    RegisterTier.SLOW:   900.0,
    RegisterTier.STATIC: 3600.0,
}


#: Master switch for SLOW-tier coalescing (v1.3.4). Exposed so the behaviour
#: can be disabled in the field without a code change if it ever misbehaves.
#: v1.3.4's SLOW-tier coalescing and night-deferral were REMOVED in v1.3.5.
#: Both were built on the belief that SLOW/STATIC tier predicted per-request
#: Modbus cost. A 29,000-request field capture showed that belief was a
#: confound: the real driver is whether a register set crosses the vendor
#: library's own internal batching boundary (address gap >= 16 or span > 64),
#: independent of tier. Coalescing actively made this WORSE — deliberately
#: gathering a coordinator's whole SLOW/STATIC cohort maximises address
#: scatter, which is exactly what forces the most internal sub-reads. It
#: caused a real outage (every battery entity unavailable) within hours of
#: being enabled in the field. See update_coordinator._address_group() for
#: the replacement mechanism and AUDIT_1.3.5.md for the full incident record.


def set_slow_tier_ttl(seconds: float) -> None:
    """Override the SLOW-tier base TTL (options flow).

    Applies to entries created after the call; existing entries pick it up on
    their next tier reset. Clamped to a sane band so a mistyped option cannot
    either hammer the bus or effectively disable slow-changing data.
    """
    clamped = max(300.0, min(3600.0, float(seconds)))
    _TIER_BASE_TTL[RegisterTier.SLOW] = clamped
    _LOGGER.info(
        "register_cache: SLOW-tier base TTL set to %.0f s "
        "(expensive registers refresh this often)", clamped,
    )

# Adaptive cap TTLs (seconds) — TTL will not grow beyond this
_TIER_CAP_TTL: dict[RegisterTier, float] = {
    RegisterTier.FAST:   60.0,    # even FAST stretches to 60 s in night mode
    RegisterTier.NORMAL: 300.0,   # 5 min cap
    RegisterTier.SLOW:   1800.0,  # 30 min cap
    RegisterTier.STATIC: 86400.0, # effectively "read once"
}

# Multiplier applied to TTL each poll cycle the value is unchanged
ADAPTIVE_FACTOR: float = 2.0

# Multiplier applied to all non-FAST TTLs during inverter night/sleep mode
NIGHT_TTL_MULTIPLIER: float = 10.0


# ── Register classification ───────────────────────────────────────────────────
#
# Rules applied in order; first match wins.
# Patterns are tested against the lowercase string form of the RegisterName.

_STATIC_SUBSTRINGS: tuple[str, ...] = (
    "serial_number",
    "firmware_version",
    "software_version",
    "model_name",
    "model_id",
    "rated_power",
    "rated_capacity",
    "p_max",
    "manufacturer",
    "inverter_rated_power",
    "storage_rated_capacity",
    "storage_maximum_charge_power",    # hardware-rated max (not the soft limit)
    "storage_maximum_discharge_power", # hardware-rated max
    "storage_maximum_power_of_charge_from_grid",
    "charger_rated_power",
)

_SLOW_SUBSTRINGS: tuple[str, ...] = (
    "daily_",
    "current_day_",
    "total_",
    "accumulated_",
    "yearly_",
    "total_charge",
    "total_discharge",
    "total_energy",
    "total_active",
    "total_negative",
    "total_positive",
    "total_feed_in",
    "total_supply",
    "total_pv_energy",
    "grid_accumulated",
    "device_status",
    "running_status",
    "working_mode",
    "alarm",
    "temperature",          # changes slowly
    "soh_calibration",
    "remaining_charge_dis",
    "storage_unit_1_working_mode",
    "storage_unit_2_working_mode",
    "phase_a_active_power_built_in",
    "phase_b_active_power_built_in",
    "phase_c_active_power_built_in",
    "phase_a_active_power_external",
    "phase_b_active_power_external",
    "phase_c_active_power_external",
    "active_power_built_in",
    "active_power_external",
)

_FAST_SUBSTRINGS: tuple[str, ...] = (
    "power_meter_active_power",
    "power_meter_reactive_power",
    "storage_charge_discharge_power",   # battery charge/discharge — real-time
    "storage_unit_1_charge_discharge",
    "storage_unit_2_charge_discharge",
    "battery_pack_1_charge_discharge",
    "battery_pack_2_charge_discharge",
    "battery_pack_3_charge_discharge",
    "input_power",                      # PV input
    "active_power",                     # AC output
    "reactive_power",
    "sdongle_total_active",
    "sdongle_total_input",
    "sdongle_total_battery",
    "smartlogger_active_power",
    "smartlogger_input_power",
    "smartlogger_external_meter_active",
    "smartlogger_external_meter_reactive",
    "inverter_active_power",
)


# ── SLOW-priority overrides (must be checked BEFORE _FAST_SUBSTRINGS) ───────
#
# BUG-3 FIX: Some register names contain substrings present in _FAST_SUBSTRINGS
# but semantically belong to the SLOW tier.  For example:
#   "phase_a_active_power_built_in" contains "active_power" (FAST)
#   "active_power_external"         contains "active_power" (FAST)
# Without this pre-check, they were wrongly classified as FAST and read on
# every poll instead of every 5 minutes.
#
# Rule: list here any SLOW pattern that is a superset of a FAST pattern.
_SLOW_PRIORITY_SUBSTRINGS: tuple[str, ...] = (
    "total_dc_input_power",   # kWh energy accumulator, NOT instantaneous power
    "phase_a_active_power",
    "phase_b_active_power",
    "phase_c_active_power",
    "active_power_built_in",
    "active_power_external",
    "reactive_power_built_in",
    "reactive_power_external",
)


# ── Exact-name tier overrides (checked FIRST — v1.1.6) ───────────────────────
#
# The battery-health engine (battery_health.py) computes segment energy and
# round-trip efficiency from deltas of the lifetime counters, and watches the
# reported rated capacity for BMS recalibration steps.  The generic substring
# tiers are wrong for exactly these three names:
#
#   storage_total_charge / storage_total_discharge
#       "total_" → SLOW (5 min TTL): counter endpoints read up to 5 min stale
#       introduce up to ~0.2 kWh error per segment endpoint (±20% on a
#       minimum-size 2 kWh segment).  NORMAL (30 s base) matches the storage
#       coordinator cadence; the adaptive TTL still stretches to 5 min while
#       the battery idles.  Bus cost ≈ 0: addresses 37780–37783 are contiguous
#       with the SOC/power registers already read every poll (same PDU chunk).
#
#   storage_rated_capacity
#       "rated_capacity" → STATIC (never re-read in-session, skipped by
#       invalidate_all): the BMS-recalibration watch would be blind until an
#       HA restart.  SLOW re-reads it every 5–30 min; address 37758 is
#       likewise PDU-adjacent to always-read registers.
#
# Exact-name matching only — no substring side effects on other registers.
_TIER_OVERRIDES: dict[str, "RegisterTier"] = {
    "storage_total_charge": RegisterTier.NORMAL,
    "storage_total_discharge": RegisterTier.NORMAL,
    "storage_rated_capacity": RegisterTier.SLOW,
    # v2.0.6 (Tier 3, battery health architecture review): per-pack
    # counterparts of the unit-level overrides directly above -- same
    # reasoning exactly. PackCapacityTracker (battery_health.py) runs the
    # same segment-detection approach per pack that the unit-level
    # SegmentTracker already uses, and needs fresh counter readings for
    # the same reason: a stale counter introduces segment-energy error at
    # each endpoint. Without this override these would default to SLOW
    # (matching the generic "total_" substring pattern), which is fine
    # for a value merely displayed, but not for one feeding active
    # segment-boundary arithmetic.
    "storage_unit_1_battery_pack_1_total_charge": RegisterTier.NORMAL,
    "storage_unit_1_battery_pack_1_total_discharge": RegisterTier.NORMAL,
    "storage_unit_1_battery_pack_2_total_charge": RegisterTier.NORMAL,
    "storage_unit_1_battery_pack_2_total_discharge": RegisterTier.NORMAL,
    "storage_unit_1_battery_pack_3_total_charge": RegisterTier.NORMAL,
    "storage_unit_1_battery_pack_3_total_discharge": RegisterTier.NORMAL,
}


@lru_cache(maxsize=256)
def classify_register(name: "RegisterName") -> RegisterTier:
    """Public wrapper around the cached tier classifier.

    Exposed so the coordinator can record which tier a request belongs to in
    the diagnostic capture, without reaching into a private helper.
    """
    return _classify(name)


def _classify(name: RegisterName) -> RegisterTier:
    """Return the volatility tier for a register name.

    Results are memoised with lru_cache: the set of unique RegisterNames seen
    in a session is bounded (≤ ~200), so this eliminates repeated O(N_strings)
    substring scans after the first lookup for each name.

    Classification order (first match wins):
      1. STATIC priority substrings
      2. SLOW priority substrings (BUG-3 FIX: before FAST to prevent misclassification)
      3. FAST substrings
      4. SLOW substrings
      5. NORMAL (default)
    """
    s = str(name).lower()
    override = _TIER_OVERRIDES.get(s)
    if override is not None:
        return override
    for sub in _STATIC_SUBSTRINGS:
        if sub in s:
            return RegisterTier.STATIC
    # BUG-3 FIX: check SLOW-priority patterns before FAST
    for sub in _SLOW_PRIORITY_SUBSTRINGS:
        if sub in s:
            return RegisterTier.SLOW
    for sub in _FAST_SUBSTRINGS:
        if sub in s:
            return RegisterTier.FAST
    for sub in _SLOW_SUBSTRINGS:
        if sub in s:
            return RegisterTier.SLOW
    return RegisterTier.NORMAL


# ── Cache entry ───────────────────────────────────────────────────────────────

class _CacheEntry:
    __slots__ = ("value", "raw", "ts", "quality", "reason", "tier", "effective_ttl")

    def __init__(self, value: Any, raw: Any, ts: float, tier: RegisterTier) -> None:
        self.value = value                          # full Result object
        self.raw = raw                              # comparable raw value for change detection
        self.ts = ts                                # single timestamp -- see Quality docstring above
        # v2.0.0: `dirty` (bool) replaced by `quality`/`reason`. A fresh
        # successful read is always GOOD with no reason; degradation
        # (UNCERTAIN/BAD, with a specific reason) is applied in place by
        # record_attempt() without touching value/ts, so the entry always
        # retains its last known-good reading even while its quality says
        # that reading can no longer be fully trusted.
        self.quality: "Quality" = Quality.GOOD
        self.reason: "Reason | None" = None
        self.tier = tier
        self.effective_ttl: float = _TIER_BASE_TTL[tier]


def _raw(result: "Result[Any]") -> Any:
    """Extract a comparable value from a Result for change detection."""
    try:
        return result.value
    except Exception:
        return result


# ── Main cache class ──────────────────────────────────────────────────────────

class RegisterCache:
    """Adaptive, tier-aware register value cache.

    Parameters
    ----------
    telemetry:
        Optional ModbusTelemetry instance.  When provided, cache hits are
        reported so they appear in the Modbus diagnostic sensors.
    night_mode:
        When True, non-FAST TTLs are multiplied by NIGHT_TTL_MULTIPLIER.
        Set via set_night_mode(); controlled by the coordinator.
    starvation_ceiling_s:
        v2.0.0. How many seconds past an UNCERTAIN entry's due-time before
        it lazily becomes BAD/EXPIRED (see _live_quality). Deliberately a
        constructor parameter, not a module-level import of
        const.REGISTER_STARVATION_CEILING_S -- this file is kept
        dependency-light by design (unlike the "heavy" coordinator/setup
        files, it's imported and executed directly, not just AST-checked,
        by its own test suite), so the real constant is injected by
        whoever constructs the cache rather than imported here. The
        default below matches const.REGISTER_STARVATION_CEILING_S
        (Defect Y, v1.3.21) exactly; construction sites should still pass
        it explicitly so the two never silently drift apart.
    energy_availability_ceiling_s:
        v2.0.0 (V2_ARCHITECTURE_DESIGN.md §8.1). Energy counters get a
        LONGER availability window than starvation_ceiling_s, not a
        shorter one -- a delayed energy reading is less harmful than a
        gap in it (breaks the Energy Dashboard's hourly rollup outright),
        per the operator's own hard hardware/UX constraint, and HA's
        statistics sum delta is value-to-value, not time-weighted, so a
        late-but-genuine reading still lands on the correct total. Must
        stay larger than starvation_ceiling_s -- see _live_quality(), which
        would otherwise never reach this longer ceiling at all, since the
        generic EXPIRED transition would already have fired first. Same
        injection reasoning as starvation_ceiling_s above.
    """

    def __init__(
        self,
        telemetry: "ModbusTelemetry | None" = None,
        starvation_ceiling_s: float = 300.0,
        energy_availability_ceiling_s: float = 600.0,
    ) -> None:
        self._store: dict[RegisterName, _CacheEntry] = {}
        self._telemetry = telemetry
        self._night_mode: bool = False
        self._starvation_ceiling_s = starvation_ceiling_s
        self._energy_availability_ceiling_s = energy_availability_ceiling_s

    # ── night-mode control ────────────────────────────────────────────────────

    def set_telemetry(self, telemetry: "ModbusTelemetry") -> None:
        """Swap the telemetry reference without discarding cached values.

        Preferred over replacing the whole RegisterCache instance (which would
        discard all cached values and adaptive TTL state) when the telemetry
        singleton becomes available after construction.
        """
        self._telemetry = telemetry

    def set_night_mode(self, active: bool) -> None:
        """Enable or disable night-mode TTL stretching."""
        if active != self._night_mode:
            _LOGGER.debug("Register cache: night mode %s", "ON" if active else "OFF")
            self._night_mode = active
            # On wakeup, reset all NORMAL/FAST adaptive TTLs so we get fresh
            # data immediately on the first post-wakeup poll.
            if not active:
                for entry in self._store.values():
                    if entry.tier in (RegisterTier.NORMAL, RegisterTier.FAST):
                        entry.effective_ttl = _TIER_BASE_TTL[entry.tier]

    @property
    def night_mode(self) -> bool:
        return self._night_mode

    # ── effective TTL helper ──────────────────────────────────────────────────

    def _effective_ttl(self, entry: _CacheEntry) -> float:
        """Return the actual TTL to use for a cache entry, respecting night mode."""
        ttl = entry.effective_ttl
        if self._night_mode and entry.tier != RegisterTier.STATIC:
            ttl = min(ttl * NIGHT_TTL_MULTIPLIER, _TIER_CAP_TTL[entry.tier])
        return ttl

    # ── public API ────────────────────────────────────────────────────────────

    def _live_quality(
        self, name: RegisterName, entry: "_CacheEntry", now: float
    ) -> tuple["Quality", "Reason | None"]:
        """Quality/reason as of `now`, applying the lazy EXPIRED transition.

        EXPIRED is a pure function of elapsed time (an UNCERTAIN entry aged
        past its applicable ceiling becomes BAD) -- computed here, on read,
        rather than by any writer, so no background sweep task is needed;
        it reuses the same constant already shipped and field-validated
        for Defect Y (v1.3.21).

        STATIC tier is exempt (V2_ARCHITECTURE_DESIGN.md §10.3): its whole
        purpose is registers that are genuinely immutable within a session
        (serial number, model name). Treating "hasn't needed re-reading"
        as "no longer trustworthy" is semantically wrong for data that, by
        definition, is not expected to change at all -- unlike FAST/NORMAL/
        SLOW tiers, where genuine change over time is real and staleness
        represents real risk.

        Energy counters get a LONGER ceiling, not an exemption
        (V2_ARCHITECTURE_DESIGN.md §8.1): a delayed reading is less harmful
        than a gap for the Energy Dashboard's hourly rollup, and HA's
        statistics sum delta is value-to-value, not time-weighted, so a
        late-but-genuine reading still lands on the correct total. Checked
        by name (is_energy_counter), not tier -- energy counters span more
        than one tier.
        """
        if entry.quality != Quality.UNCERTAIN or entry.tier == RegisterTier.STATIC:
            return entry.quality, entry.reason
        ceiling = (
            self._energy_availability_ceiling_s
            if is_energy_counter(name)
            else self._starvation_ceiling_s
        )
        if now - entry.ts > ceiling:
            return Quality.BAD, Reason.EXPIRED
        return entry.quality, entry.reason

    def filter_stale(
        self,
        names: list[RegisterName],
        default_ttl: timedelta,
    ) -> list[RegisterName]:
        """Return only those register names that need a fresh read.

        Parameters
        ----------
        names:
            All register names requested by active HA entities.
        default_ttl:
            Fallback TTL for NORMAL-tier registers (should equal the
            coordinator's poll interval).  Ignored for other tiers.
        """
        now = time.monotonic()
        stale: list[RegisterName] = []
        cache_hits = 0
        default_ttl_s = default_ttl.total_seconds()

        for name in names:
            entry = self._store.get(name)
            # v2.0.0: not GOOD (rather than the old `dirty`) -- a register
            # currently UNCERTAIN or BAD needs a fresh attempt just as much
            # as one that was never cached at all.
            if entry is None or self._live_quality(name, entry, now)[0] != Quality.GOOD:
                stale.append(name)
                continue

            ttl = self._effective_ttl(entry)

            # For NORMAL tier, never use a TTL shorter than default_ttl so that
            # the coordinator's own interval is always respected as a minimum.
            if entry.tier == RegisterTier.NORMAL:
                ttl = max(ttl, default_ttl_s)

            age = now - entry.ts
            if age >= ttl:
                stale.append(name)
            else:
                cache_hits += 1

        # Report all hits in a single batched call — one time.monotonic() and
        # one deque.extend() instead of N individual calls.
        if cache_hits:
            if self._telemetry:
                self._telemetry.record_cache_hits(cache_hits)
            _LOGGER.debug(
                "Register cache: %d hit(s) / %d miss(es) / %d total  [night=%s]",
                cache_hits, len(stale), len(names), self._night_mode,
            )
        return stale

    def update(self, results: dict[RegisterName, "Result[Any]"]) -> None:
        """Store fresh, successfully-read results. Always ends GOOD.

        v2.0.0: this is exclusively the SUCCESS path now -- see
        record_attempt() for recording a failed/deferred outcome. Adaptive
        TTL stretching compares against the entry's previous raw value,
        UNLESS that previous value was WRITE_PENDING (known-wrong, per our
        own write, not merely unverified) -- comparing a fresh read against
        a value we already knew was wrong would risk concluding "unchanged"
        when the write may well have changed it. Any other prior reason
        (LINK_DOWN, TIMEOUT, SHED, BACKOFF_DEFERRED, EXPIRED) still has a
        meaningful old raw value to compare against.
        """
        now = time.monotonic()
        for name, result in results.items():
            raw_new = _raw(result)
            existing = self._store.get(name)
            tier = existing.tier if existing else _classify(name)

            if existing is not None and existing.reason != Reason.WRITE_PENDING:
                # Adaptive TTL: stretch if value unchanged, reset if changed
                if raw_new == existing.raw:
                    new_ttl = min(
                        existing.effective_ttl * ADAPTIVE_FACTOR,
                        _TIER_CAP_TTL[tier],
                    )
                    existing.effective_ttl = new_ttl
                    existing.value = result
                    existing.ts = now
                else:
                    existing.effective_ttl = _TIER_BASE_TTL[tier]
                    existing.raw = raw_new
                    existing.value = result
                    existing.ts = now
                existing.quality = Quality.GOOD
                existing.reason = None
            else:
                self._store[name] = _CacheEntry(result, raw_new, now, tier)

    def record_attempt(
        self,
        names: list[RegisterName],
        quality: "Quality",
        reason: "Reason | None",
        now: float | None = None,
    ) -> None:
        """Record the OUTCOME of an attempt (or deliberate non-attempt) to
        refresh one or more registers, for anything other than a fresh
        success (use update() for that).

        v2.0.0. Called by the coordinator once per chunk outcome (a Modbus
        read succeeds or fails atomically for the whole register range
        requested together -- see V2_ARCHITECTURE_DESIGN.md §5.2), and
        explicitly for BACKOFF_DEFERRED even though nothing was sent (§5.1
        -- silently leaving an entry unchanged and explicitly recording
        "deliberately skipped, here's why" look identical in the stored
        VALUE but materially different in what a consumer sees in
        reason/age).

        Degrades an existing entry's quality/reason IN PLACE -- value/ts
        are never touched here, so the entry always retains its last
        known-good reading even while reporting that the reading can no
        longer be fully trusted. A register with no existing entry and a
        non-GOOD outcome has nothing to degrade; get()/quality_of() already
        report NEVER_READ correctly for an absent entry.
        """
        if now is None:
            now = time.monotonic()
        for name in names:
            entry = self._store.get(name)
            if entry is not None:
                entry.quality = quality
                entry.reason = reason

    def quality_of(self, name: RegisterName) -> tuple["Quality", "Reason | None", float | None]:
        """(quality, reason, age_seconds) for a register -- the quality-model
        accessor, deliberately separate from get()/merge() (which stay on
        the unchanged, bare-value interface every existing consumer already
        uses -- see V2_ARCHITECTURE_DESIGN.md §10.4). Called only by
        consumers that specifically need quality: the entity layer's
        data_quality/data_quality_reason/data_age_seconds attributes, and
        battery_health_manager.py (§10.4's deliberate exception -- it builds
        stateful deltas from sequential readings, not display state).
        """
        now = time.monotonic()
        entry = self._store.get(name)
        if entry is None:
            return Quality.BAD, Reason.NEVER_READ, None
        quality, reason = self._live_quality(name, entry, now)
        return quality, reason, now - entry.ts

    def merge(
        self,
        fresh: dict[RegisterName, "Result[Any]"],
        requested: list[RegisterName],
    ) -> dict[RegisterName, "Result[Any]"]:
        """Merge fresh results with cached values to produce a complete response.

        v2.0.0 FIX (the root defect this whole rebuild exists to close):
        a cached value is served whenever its live quality is NOT BAD --
        i.e. GOOD or UNCERTAIN both serve. Previously this checked `not
        dirty`, which conflated write-invalidation (BAD: we KNOW the old
        value is wrong) with reconnect-invalidation (should be UNCERTAIN:
        the value is probably still true, we just can't currently verify
        it) -- a register invalidated by any connection blip and not
        immediately re-read was silently dropped from coordinator.data
        entirely, going `Unknown` in Home Assistant even though a recent,
        probably-still-accurate reading existed. This is the fix.
        """
        now = time.monotonic()
        merged: dict[RegisterName, "Result[Any]"] = {}
        for name in requested:
            if name in fresh:
                merged[name] = fresh[name]
                continue
            entry = self._store.get(name)
            if entry is not None and self._live_quality(name, entry, now)[0] != Quality.BAD:
                merged[name] = entry.value
        return merged

    def invalidate(self, name: RegisterName) -> None:
        """Mark a single register BAD/WRITE_PENDING after a write.

        v2.0.0: this is the "we KNOW the old value is now wrong" case --
        stays BAD, not UNCERTAIN. See invalidate_all() for the genuinely
        different reconnect case.
        """
        if name in self._store:
            self._store[name].quality = Quality.BAD
            self._store[name].reason = Reason.WRITE_PENDING
            _LOGGER.debug("Cache invalidated (write pending): %s", name)

    def invalidate_all(self) -> None:
        """Mark every non-STATIC cached register UNCERTAIN/LINK_DOWN after
        reconnect.

        v2.0.0: UNCERTAIN, not the old dirty=True (which merge() treated as
        unservable). We don't know the value changed during the outage --
        we just can't currently verify it -- so it should still be served,
        with its quality honestly reported as degraded, until either a
        fresh read confirms it or REGISTER_STARVATION_CEILING_S elapses and
        it lazily becomes BAD/EXPIRED (_live_quality). This is the direct
        fix for the root defect this rebuild exists to close.

        STATIC registers (serial numbers, firmware versions, rated power,
        etc.) are hardware constants that cannot change between connection
        attempts. Skipping them saves one batch read of ~10-15 registers on
        every reconnect / outage recovery, reducing the initial post-outage
        burst.
        """
        for entry in self._store.values():
            if entry.tier != RegisterTier.STATIC:
                entry.quality = Quality.UNCERTAIN
                entry.reason = Reason.LINK_DOWN

    def invalidate_all_including_static(self) -> None:
        """Mark every cached register untrustworthy, including STATIC tier.

        Use only when the device itself may have changed (firmware update,
        hardware replacement).  Normal outage recovery should call
        invalidate_all() instead.

        v2.0.0: currently unused anywhere in this codebase (reserved for a
        hypothetical future caller). Marked BAD/WRITE_PENDING -- an
        imperfect semantic fit (nothing was written) reused deliberately
        rather than adding a dedicated Reason value for a zero-caller
        method; revisit if this ever gains a real caller.
        """
        for entry in self._store.values():
            entry.quality = Quality.BAD
            entry.reason = Reason.WRITE_PENDING

    def get(self, name: RegisterName) -> "Result[Any] | None":
        """Return a cached value or None.

        v2.0.0: serves whenever live quality is NOT BAD (GOOD or
        UNCERTAIN) -- see merge()'s docstring for the full reasoning; this
        is the same fix, for the other call pattern that reads single
        registers directly.
        """
        entry = self._store.get(name)
        if entry is not None and self._live_quality(name, entry, time.monotonic())[0] != Quality.BAD:
            return entry.value
        return None

    def tier_of(self, name: RegisterName) -> "RegisterTier | None":
        """Return the tier of a cached register, or None if not cached."""
        entry = self._store.get(name)
        return entry.tier if entry else None

    def effective_ttl_of(self, name: RegisterName) -> float:
        """Return the current effective TTL of a cached register in seconds."""
        entry = self._store.get(name)
        return self._effective_ttl(entry) if entry else 0.0

    def overdue_by(self, name: RegisterName) -> float | None:
        """How many seconds PAST its own due-time this register currently is.

        v1.3.21 (Defect Y -- starvation ceiling, requested directly: "if
        they haven't been updated for 5 minutes, they become more
        important"). Deliberately NOT simply "age since last read" --
        SLOW tier's own base TTL is already 900s, so a SLOW register is
        by definition already >=900s old the moment it first becomes due
        at all; thresholding raw age at a 5-minute ceiling would make
        every SLOW/STATIC register satisfy that ceiling immediately upon
        becoming due, defeating tier-based back-off deferral entirely
        (see Finding on update_coordinator.py's priority filter). This
        instead measures how far PAST its own due-time (age - effective
        TTL) the register is -- zero the instant it becomes due, growing
        only from there. A back-off deferral ceiling checked against THIS
        value preserves the intended "SLOW/STATIC waits a while during
        genuine contention" behaviour, while still guaranteeing an
        absolute limit on how much EXTRA delay on top of that is ever
        tolerated.

        Returns None if the register has never been successfully read at
        all -- treat as maximally overdue (never yet observed is a worse
        state than merely stale), which also directly closes the
        "register silently never gets read at all" failure mode found in
        the field (a battery pack's BMS temperature going unread for an
        entire multi-hour capture).
        """
        entry = self._store.get(name)
        if entry is None:
            return None
        age = time.monotonic() - entry.ts
        return age - self._effective_ttl(entry)

    @property
    def size(self) -> int:
        return len(self._store)

    def clear(self) -> None:
        self._store.clear()


# ── Energy counter register identification ─────────────────────────────────────
#
# These are monotonically-increasing kWh accumulator registers written by the
# inverter's own metering IC.  They must NEVER be served from a stale cache
# fallback after a Modbus timeout.
#
# Why: serving a stale cached value makes HA's statistics recorder see a flat
# line during the outage, then a sudden jump on recovery.  HA assigns that jump
# to the wrong hourly bucket, producing the incorrect consumption bars visible
# in the Energy dashboard.  Returning None/unavailable instead gives HA an
# honest gap, which it handles correctly via interpolation — no wrong totals.

_ENERGY_COUNTER_SUBSTRINGS: tuple[str, ...] = (
    "daily_yield",
    "daily_energy",
    "total_yield",
    "total_energy",
    "accumulated_energy",
    "accumulated_yield",
    "yearly_energy",
    "yearly_yield",
    "total_charged_energy",
    "total_discharged_energy",
    "total_charge_energy",
    "total_discharge_energy",
    "grid_accumulated",
    "total_feed_in",
    "total_supply",
    "total_pv_energy",
    "total_active_energy",
    "total_positive_active",
    "total_negative_active",
    "energy_import",
    "energy_export",
    "current_day_charge",
    "current_day_discharge",
    "current_day_yield",
)

# ── Authoritative energy-counter name set (source of truth) ────────────────────
#
# The substring heuristic above missed ~half of the monotonically-increasing
# kWh/kVarh accumulators that sensor.py declares with
# `state_class=TOTAL_INCREASING` (e.g. storage_total_charge, total_dc_input_power,
# every *_today daily counter, several SmartLogger/external-meter totals).
# Those gaps silently disabled the stale-cache exclusion AND the suspicious-zero
# guard for the affected registers, re-introducing the Energy-dashboard
# negative-bar / wrong-bucket corruption at sunrise/sunset.
#
# This explicit set is the source of truth.  It MUST stay in sync with the
# TOTAL_INCREASING energy sensors in sensor.py.  The regression test
# tests/test_energy_counter_coverage.py re-derives the list from sensor.py and
# fails if the two ever drift, so a newly-added energy sensor cannot silently
# lose protection.
_ENERGY_COUNTER_NAMES: frozenset[str] = frozenset({
    "accumulated_yield_energy",
    "charger_total_energy_charged",
    "consumption_today",
    "daily_yield_energy",
    "energy_charged_today",
    "energy_discharged_today",
    "feed_in_to_grid_today",
    "grid_accumulated_energy",
    "grid_accumulated_reactive_power",
    "grid_exported_energy",
    "hourly_yield_energy",
    "inverter_energy_yield_today",
    "inverter_total_absorbed_energy",
    "inverter_total_energy_yield",
    "monthly_yield_energy",
    "pv_yield_today",
    "smartlogger_energy_charged_today",
    "smartlogger_energy_discharged_today",
    "smartlogger_external_meter_negative_active_electricity",
    "smartlogger_external_meter_positive_active_electricity_total",
    "smartlogger_external_meter_total_active_electricity",
    "smartlogger_external_meter_total_reactive_electricity",
    "smartlogger_power_supply_from_grid_today",
    "smartlogger_total_energy_charged",
    "smartlogger_total_energy_discharge_d",
    "smartlogger_total_energy_yield",
    "smartlogger_total_power_supply_from_grid",
    "smartlogger_yield_today",
    "storage_current_day_charge_capacity",
    "storage_current_day_discharge_capacity",
    "storage_total_charge",
    "storage_total_discharge",
    "supply_from_grid_today",
    "total_active_energy_built_in_energy",
    "total_active_energy_external_energy",
    "total_charged_energy",
    "total_dc_input_power",
    "total_discharged_energy",
    "total_energy_consumption",
    "total_feed_in_to_grid",
    "total_negative_active_energy_built_in_energy",
    "total_negative_active_energy_external_energy",
    "total_positive_active_energy_built_in_energy",
    "total_positive_active_energy_external_energy",
    "total_pv_energy_yield",
    "total_supply_from_grid",
    "yearly_yield_energy",
})


@lru_cache(maxsize=256)
def is_energy_counter(name: "RegisterName") -> bool:
    """Return True if *name* is a monotonically-increasing kWh accumulator.

    Energy counter registers must not be served from a stale cache fallback
    after a Modbus timeout, nor accept a suspicious live zero — see module
    docstring for the full rationale.

    Matching is by exact name (the authoritative _ENERGY_COUNTER_NAMES set)
    OR by substring (a coarse safety net for library register-name variants).

    Results are memoised: the set of unique RegisterNames in a session is
    bounded (≤ ~200), so this is effectively O(1) after the first lookup.
    """
    s = str(name).lower()
    if s in _ENERGY_COUNTER_NAMES:
        return True
    return any(sub in s for sub in _ENERGY_COUNTER_SUBSTRINGS)
