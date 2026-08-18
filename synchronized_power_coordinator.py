"""SynchronizedPowerCoordinator — contiguous multi-inverter power snapshot.

Problem solved
--------------
With two inverters on the same Modbus bus the standard per-device coordinators
fire at different times.  ModbusGuard serialises them correctly but the resulting
wall-clock spread between the first and last reading can reach 3–4 seconds.  When
HA renders the Energy dashboard power-flow card it samples all entity states at a
single moment, so values measured 3 s apart can produce wildly wrong arithmetic —
especially during ramp events (cloud passing, EV charger switching on).

Solution
--------
This coordinator reads exactly the four registers needed for power-flow in one
*contiguous* block, serialised behind the primary inverter's ModbusGuard so no
other coordinator can interleave.  All four sensor entities update from the same
HA coordinator tick — their ``last_updated`` timestamps are identical.

Reads performed per poll (in order)
-------------------------------------
1. INV1  — ``INPUT_POWER``             (PV string DC power)
2. Meter — ``POWER_METER_ACTIVE_POWER``  (grid import/export; signed W)
3. Battery — ``STORAGE_CHARGE_DISCHARGE_POWER`` (signed W; + = charging)
4. INV2  — ``INPUT_POWER``             (PV string DC power, standalone inverter)

Derived values exposed as HA sensor entities
---------------------------------------------
• ``pv_power_total``     = INV1_pv + INV2_pv                 [W]
• ``grid_power``         = raw meter reading (signed)         [W]  + = import
• ``battery_power``      = raw battery reading (signed)       [W]  + = charging
• ``home_consumption``   = pv_total + grid_power − battery_power [W]

Sign convention for home_consumption
--------------------------------------
Energy conservation: PV + grid_import = home + grid_export + battery_charge
Rearranging:         home = PV + grid_power − battery_power
  • grid_power   > 0 → importing → adds to home              ✓
  • grid_power   < 0 → exporting → reduces home              ✓
  • battery_power> 0 → charging  → battery consumes, reduces home ✓
  • battery_power< 0 → discharging → battery feeds home      ✓
  • Result is clamped to ≥ 0 W; small negative values indicate
    transient measurement noise.

Architecture
------------
The coordinator acquires the primary inverter guard for the entire poll
sequence.  Because both inverters share the same physical SmartLogger / SDongle
connection, holding the primary guard for sequential reads prevents any other
coordinator from interleaving on the physical bus.  The secondary inverter's
guard is acquired *after* the primary guard releases, making the total spread
the sum of four back-to-back Modbus reads plus three inter-request gaps
(≈ 4 × 300 ms + 3 × 150 ms ≈ 1.7 s worst case, vs 3–4 s with independent
coordinators).

Error handling
--------------
If any individual read fails the coordinator logs a warning at DEBUG level and
continues with ``None`` for that slot.  Derived values that depend on a missing
reading are marked as unavailable (``None``) so HA shows "unavailable" rather
than a silently wrong number.  This is preferable to raising ``UpdateFailed``
for a partial outage.

If ALL reads fail the coordinator raises ``UpdateFailed`` so HA marks all four
entities unavailable and the normal back-off logic takes over.
"""

from __future__ import annotations

import asyncio
import time
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from huawei_solar import register_names as rn
from huawei_solar.device.base import HuaweiSolarDevice

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    SYNC_POWER_UPDATE_INTERVAL,
    SYNC_POWER_CACHE_ALIGNMENT_TOLERANCE_S,
    SYNC_POWER_POLL_DEADLINE,
    UPDATE_TIMEOUT,
)
from .modbus_guard import ModbusAdmissionTimeout, ModbusGuard, ModbusQueueShed
from .modbus_telemetry import ModbusTelemetry
from .register_cache import Quality

if TYPE_CHECKING:
    from .register_cache import RegisterCache

_LOGGER = logging.getLogger(__name__)


# ── Result dataclass ───────────────────────────────────────────────────────────

@dataclass(slots=True)
class SynchronizedPowerData:
    """Best-effort, near-simultaneous snapshot of power-flow values from a
    single coordinator tick -- NOT a true atomic transaction.

    v1.3.19 NOTE (Defect V/Finding 9, independent ICS audit): each field is
    read via its own, separate ModbusGuard acquisition (see
    SynchronizedPowerCoordinator._async_update_data) rather than one
    acquisition held across all four reads. Other coordinators sharing the
    same guard CAN interleave between them. This is a deliberate trade-off,
    not an oversight: holding one guard for the entire read sequence would
    block every other coordinator on the same bus for the whole sequence's
    duration, working directly against the fairness Defect P (v1.3.15) was
    built to guarantee across devices sharing a bus. `sample_span_ms`
    reports how much wall-clock time actually separated the first and last
    successful read in this tick, so a consumer can judge how tightly
    grouped a given sample really was rather than assuming perfect
    simultaneity.

    All fields share the same ``last_updated`` timestamp because they are
    populated from the same ``DataUpdateCoordinator`` cycle -- that
    timestamp marks when the COORDINATOR finished, not that the underlying
    values were captured at the same physical instant.

    ``None`` indicates that the reading failed (device unavailable / not
    installed).  Sensors must treat ``None`` as unavailable.
    """

    #: DC power from inverter 1's PV strings (W).  Always present.
    inv1_pv_power: float | None

    #: DC power from inverter 2's PV strings (W).  ``None`` if not installed.
    inv2_pv_power: float | None

    #: Grid power, signed W.  Positive = importing, negative = exporting.
    #: ``None`` if no meter is connected to INV1.
    grid_power: float | None

    #: Battery charge/discharge power, signed W.
    #: Positive = charging (battery consuming power from system).
    #: Negative = discharging (battery supplying power to system).
    #: ``None`` if no battery is connected.
    battery_power: float | None

    #: Topology flags — distinguish "not installed" (a missing input legitimately
    #: contributes 0) from "installed but this tick's read failed" (a missing
    #: input means the derived value is unknown and must be reported as
    #: unavailable, not silently computed with a wrong term).  Default False so
    #: that a single-inverter / no-battery / no-meter system is unaffected.
    has_inv2: bool = False
    has_meter: bool = False
    has_battery: bool = False

    #: Wall-clock milliseconds between the first and last successful read
    #: in this tick (v1.3.19, Defect V/Finding 9) -- diagnostic only, gives
    #: visibility into how time-skewed a given sample actually was, since
    #: the four reads are not one atomic transaction. ``None`` if fewer
    #: than two reads succeeded this tick (nothing to measure a span over).
    sample_span_ms: float | None = None

    #: v2.0.0a (F09/F20, external ICS audit -- confirmed): sample_span_ms
    #: existed as instrumentation, but nothing gated on it -- a composite
    #: reading with a large time-skew (e.g. under heavy bus contention,
    #: where the four separately-guarded reads interleave badly with other
    #: coordinators) was returned identically to a well-aligned one, with
    #: no signal that the underlying power-flow arithmetic might be
    #: combining readings from materially different moments. True when
    #: sample_span_ms exceeds SYNC_POWER_CACHE_ALIGNMENT_TOLERANCE_S (the
    #: same hardware-derived tolerance §8.2's cache-shortcut already uses
    #: for the identical underlying question -- "are these readings close
    #: enough in time to combine" doesn't have a different answer just
    #: because the data came from a dedicated read instead of the cache).
    #: False (not True) when sample_span_ms is None -- fewer than two
    #: reads succeeded at all is a different, already-covered failure mode
    #: (the individual fields are already None), not a new one this flag
    #: needs to also signal.
    is_temporally_uncertain: bool = False

    # ── Derived properties ────────────────────────────────────────────────────

    @property
    def pv_power_total(self) -> float | None:
        """Sum of all PV string power across both inverters.

        Returns ``None`` if INV1 is unavailable, or if a second inverter is
        installed but its reading failed this tick (otherwise the total would
        silently omit INV2's contribution and report a wrong number).
        """
        if self.inv1_pv_power is None:
            return None
        if self.has_inv2 and self.inv2_pv_power is None:
            return None
        return self.inv1_pv_power + (self.inv2_pv_power or 0)

    @property
    def home_consumption(self) -> float | None:
        """Estimated home consumption derived from the power balance equation.

        Returns ``None`` if any required reading is unavailable — including a
        battery that is installed but failed to read this tick (substituting 0
        would over/under-count home by the actual battery power).
        A small negative result (measurement noise) is clamped to 0.
        """
        pv = self.pv_power_total
        grid = self.grid_power
        batt = self.battery_power
        if pv is None or grid is None:
            return None
        if self.has_battery and batt is None:
            return None
        # Battery contribution: discharging (negative) feeds home so we subtract
        # battery_power (positive = charging reduces available power).
        raw = pv + grid - (batt or 0)
        return max(0.0, raw)


# ── Coordinator ────────────────────────────────────────────────────────────────

class SynchronizedPowerCoordinator(DataUpdateCoordinator[SynchronizedPowerData]):
    """DataUpdateCoordinator that reads all power-flow registers in one block.

    Parameters
    ----------
    inv1_device:
        The primary SUN2000 inverter device object (has meter + battery).
    inv2_device:
        The secondary SUN2000 inverter device (standalone).
        Pass ``None`` if there is only one inverter.
    has_meter:
        Whether a power meter is connected to INV1.
    has_battery:
        Whether a LUNA2000 battery is connected to INV1.
    update_interval:
        How often to poll.  Default is ``SYNC_POWER_UPDATE_INTERVAL`` (10 s).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        inv1_device: HuaweiSolarDevice,
        inv2_device: HuaweiSolarDevice | None,
        *,
        has_meter: bool,
        has_battery: bool,
        update_interval: timedelta = SYNC_POWER_UPDATE_INTERVAL,
        update_timeout: timedelta = UPDATE_TIMEOUT,
        bus_endpoint: str = "",
        inv1_cache: "RegisterCache | None" = None,
        inv2_cache: "RegisterCache | None" = None,
        meter_cache: "RegisterCache | None" = None,
        battery_cache: "RegisterCache | None" = None,
        # v2.0.9 (Phase 3.1, this release): see CONF_SYNC_POWER_DEDICATED_
        # READS' own comment in const.py for the full reasoning. True
        # preserves this coordinator's original behaviour exactly.
        dedicated_reads_enabled: bool = True,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="huawei_solar_synchronized_power",
            update_interval=update_interval,
        )
        self._inv1 = inv1_device
        self._inv2 = inv2_device
        self._has_meter = has_meter
        self._has_battery = has_battery
        self._update_timeout = update_timeout
        self._telemetry: ModbusTelemetry | None = None
        # Dedicated, SyncPower-specific attempt counters -- deliberately
        # NOT folded into self._telemetry above. That object is shared
        # across every coordinator on this device (all of them call
        # attach_telemetry() with the same instance), so its own
        # aggregate request counts cannot be cleanly attributed to
        # SyncPower's own fallback specifically -- confirmed while
        # assessing whether enough telemetry existed to answer the
        # architecture question without a second deployment. These four
        # counters give a clean, self-contained hit rate readable
        # directly, with no cross-referencing against the other
        # coordinators' own traffic required.
        self.shortcut_hits: int = 0
        self.shortcut_misses: int = 0
        self.fallback_cache_hits: int = 0
        self.fallback_physical_reads: int = 0
        # v2.0.5 (F-05, external ICS audit -- confirmed genuine gap: the
        # ICS-01/ICS-05 fix (v2.0.3) correctly computes is_temporally_
        # uncertain per-result, but nothing tracked how often it actually
        # fires in practice -- the report's own concern about the
        # dedicated-read fallback's per-value guard-release-between-reads
        # risk had no way to be answered from data, only bounded
        # indirectly via fallback_cache_hit_rate above. results_with_
        # span_computed is the denominator (only results where at least
        # two values had a real, distinct capture time -- see sample_
        # span_ms's own None-when-fewer-than-two-values behaviour);
        # temporally_uncertain_count is the numerator.
        self.temporally_uncertain_count: int = 0
        self.results_with_span_computed: int = 0
        # v2.0.0 (V2_ARCHITECTURE_DESIGN.md §8.2): optional references to the
        # regular per-device RegisterCache instances that already hold these
        # same four registers, checked for a cheap shortcut before falling
        # back to this coordinator's own dedicated read -- see
        # _try_cache_shortcut(). None for any cache that isn't relevant
        # (e.g. inv2_cache when there's no second inverter) is expected and
        # handled the same as "not aligned, do the dedicated read".
        self._inv1_cache = inv1_cache
        self._inv2_cache = inv2_cache
        self._meter_cache = meter_cache
        self._battery_cache = battery_cache
        self._dedicated_reads_enabled = dedicated_reads_enabled
        # v2.0.9 (Phase 3.1, this release): distinct from shortcut_hits/
        # misses above -- those specifically track the STRICT, aligned
        # shortcut (_try_cache_shortcut), which cache-only mode never
        # calls at all (it has its own, more lenient path -- see
        # _cache_only_snapshot()). This counts every cache-only-mode
        # evaluation, so the two modes' own activity stays distinguishable
        # in a snapshot rather than one silently masquerading as the other.
        self.cache_only_snapshots: int = 0
        # v2.0.10 (finer-grained instrumentation, this release): one ID
        # shared by all four of THIS coordinator's own dedicated reads
        # within one update cycle -- same "one ID per logical poll"
        # convention the main coordinator's own _next_logical_request_id
        # already established (update_coordinator.py), so a diagnostics
        # capture can group SyncPower's own reads together the same way.
        self._next_logical_request_id: int = 0

        # v1.3.11 FIX (Defect J, reported by an independent ICS audit and
        # confirmed against source): guards were keyed on inv1/inv2's own
        # serial_number, NOT the shared bus endpoint every other coordinator
        # in this codebase uses (see update_coordinator.py:
        # `endpoint = bus_endpoint or device.serial_number`). ModbusGuard's
        # registry is keyed by that string, so this created TWO ENTIRELY
        # SEPARATE guard objects for this coordinator's reads -- distinct
        # from, and with zero awareness of, the ONE shared guard every other
        # coordinator (main/battery/power_meter/config, for both devices) was
        # already using for the same physical bus. This coordinator's reads
        # were therefore never actually serialized against the rest of this
        # bus's traffic at all, on any installation with a shared RS485 bus
        # (daisy-chained inverters) -- exactly the collision risk
        # ModbusGuard exists to prevent, and worth calling out clearly: this
        # is a plausible contributor to the broader multi-coordinator
        # shedding pattern seen in this session's field investigation
        # (AUDIT_1.3.10.md), on top of Defect I.
        #
        # Fixed by using the SAME `bus_endpoint or device.serial_number`
        # fallback convention as update_coordinator.py, and by threading
        # `bus_endpoint` in from __init__.py (which already computes it once
        # per entry). Both inverters on one entry share one physical bus, so
        # they now correctly resolve to the SAME ModbusGuard instance that
        # every other coordinator on this entry already shares -- not just
        # a same-object match between inv1/inv2 as before, but the actual
        # shared-bus guard.
        primary_endpoint = bus_endpoint or inv1_device.serial_number
        # Primary guard: serialises all reads for this coordinator.
        # Because both inverters are on the same SmartLogger/SDongle TCP
        # connection, holding the primary guard prevents interleaving on the
        # shared physical bus.
        self._primary_guard = ModbusGuard.get_or_create(primary_endpoint)

        # Secondary guard: acquired separately after primary releases, for the
        # INV2 read. With the shared bus_endpoint fix above this now resolves
        # to the SAME guard object as _primary_guard whenever inv1 and inv2
        # share one physical bus (the common case) -- correct, and no
        # deadlock risk because we never hold both simultaneously.
        secondary_endpoint = (
            (bus_endpoint or inv2_device.serial_number) if inv2_device is not None else None
        )
        self._secondary_guard: ModbusGuard | None = (
            ModbusGuard.get_or_create(secondary_endpoint)
            if secondary_endpoint is not None
            else None
        )

        self._consecutive_failures = 0

    # ── wiring ─────────────────────────────────────────────────────────────────

    def attach_telemetry(self, telemetry: ModbusTelemetry) -> None:
        """Wire in a ModbusTelemetry instance (called from __init__.py)."""
        self._telemetry = telemetry

    def snapshot(self) -> dict[str, Any]:
        """Point-in-time snapshot of this coordinator's own dedicated
        telemetry -- the data needed to directly answer "what fraction of
        SyncPower's own ticks are served from cache", without cross-
        referencing the shared ModbusTelemetry object other coordinators
        also write into (see shortcut_hits/shortcut_misses/
        fallback_cache_hits/fallback_physical_reads' own comment in
        __init__ for why that cross-referencing would otherwise be
        necessary).

        Deliberately exposes the raw counters plus two separate,
        individually clear rates, rather than one combined "overall hit
        rate" number: a full shortcut hit represents a variable number of
        values served at once (1-4, depending on whether this
        installation has a meter/battery/second inverter configured),
        while a fallback cache hit represents exactly one. Collapsing
        both into a single ratio would need that per-installation
        weighting made explicit anyway, so it is clearer to report them
        separately and let the reader combine them with full knowledge of
        what each one actually means.
        """
        shortcut_attempts = self.shortcut_hits + self.shortcut_misses
        fallback_attempts = self.fallback_cache_hits + self.fallback_physical_reads
        return {
            "shortcut_hits": self.shortcut_hits,
            "shortcut_misses": self.shortcut_misses,
            "shortcut_hit_rate": (
                round(self.shortcut_hits / shortcut_attempts, 3)
                if shortcut_attempts else None
            ),
            "fallback_cache_hits": self.fallback_cache_hits,
            "fallback_physical_reads": self.fallback_physical_reads,
            "fallback_cache_hit_rate": (
                round(self.fallback_cache_hits / fallback_attempts, 3)
                if fallback_attempts else None
            ),
            # The single most direct number for the architecture question:
            # how many individual physical reads did SyncPower actually
            # need, of any kind, in this snapshot period -- regardless of
            # which path (shortcut miss then fallback-physical) produced
            # them. Compared against the known ~360 ticks/hour cadence
            # (SYNC_POWER_UPDATE_INTERVAL), this is directly interpretable
            # without needing either rate above explained first.
            "physical_reads_total": self.fallback_physical_reads,
            # v2.0.5 (F-05, external ICS audit): directly answers how
            # often a result was actually flagged temporally uncertain
            # (ICS-01/ICS-05, v2.0.3) -- previously only inferable
            # indirectly via fallback_cache_hit_rate above, not measured.
            "temporally_uncertain_count": self.temporally_uncertain_count,
            "results_with_span_computed": self.results_with_span_computed,
            "temporally_uncertain_rate": (
                round(self.temporally_uncertain_count / self.results_with_span_computed, 3)
                if self.results_with_span_computed else None
            ),
            # v2.0.9 (Phase 3.1, this release): whether dedicated
            # physical reads are enabled for this installation, and how
            # many cache-only snapshots have been served -- lets a
            # capture directly distinguish an installation running in
            # cache-only mode from one still using dedicated reads,
            # without needing to cross-reference config.
            "dedicated_reads_enabled": self._dedicated_reads_enabled,
            "cache_only_snapshots": self.cache_only_snapshots,
        }

    # ── poll ───────────────────────────────────────────────────────────────────

    def _try_cache_shortcut(self) -> SynchronizedPowerData | None:
        """v2.0.0 (V2_ARCHITECTURE_DESIGN.md §8.2): if the regular per-device
        caches already hold GOOD, well-aligned values for every register
        this coordinator needs, use those directly instead of performing a
        dedicated read -- a genuine Modbus traffic reduction using
        information (per-register quality and age) that did not exist
        before the v2.0.0 rebuild. Purely synchronous: no Modbus traffic,
        just reading what the regular coordinators already have in memory.

        Returns None (meaning: fall back to the dedicated read below) if
        any needed cache reference is missing (e.g. this coordinator was
        constructed without them, or a relevant coordinator hasn't been
        created), any needed register's quality isn't GOOD, or the
        age-spread across whatever IS needed exceeds
        SYNC_POWER_CACHE_ALIGNMENT_TOLERANCE_S. Never raises -- a failure
        to take the shortcut is not a failure of this coordinator, just a
        reason to do the dedicated read as before.

        Wraps _try_cache_shortcut_impl() to track shortcut_hits/
        shortcut_misses at exactly one point (here), rather than at each
        of that method's several return points individually -- lower risk
        of one of them being missed or handled inconsistently as the
        implementation evolves.
        """
        result = self._try_cache_shortcut_impl()
        if result is not None:
            self.shortcut_hits += 1
        else:
            self.shortcut_misses += 1
        return result

    def _try_cache_shortcut_impl(self) -> SynchronizedPowerData | None:
        needed: list[tuple[RegisterCache | None, Any]] = [
            (self._inv1_cache, rn.INPUT_POWER),
        ]
        if self._inv2 is not None:
            needed.append((self._inv2_cache, rn.INPUT_POWER))
        if self._has_meter:
            needed.append((self._meter_cache, rn.POWER_METER_ACTIVE_POWER))
        if self._has_battery:
            needed.append((self._battery_cache, rn.STORAGE_CHARGE_DISCHARGE_POWER))

        ages: list[float] = []
        for cache, name in needed:
            if cache is None:
                return None
            quality, _reason, age = cache.quality_of(name)
            if quality != Quality.GOOD or age is None:
                return None
            ages.append(age)

        if ages and (max(ages) - min(ages)) > SYNC_POWER_CACHE_ALIGNMENT_TOLERANCE_S:
            return None

        # v2.0.0b (AR-9, external ICS audit -- the missing measurement the
        # audit itself flagged): this shortcut existing was always the
        # direct evidence of whether the cache-first design actually
        # eliminates physical traffic, but nothing recorded when it fired.
        # Recorded as `len(needed)` cache hits -- this shortcut entirely
        # replaces what would otherwise have been that many separate
        # physical reads in the dedicated-read fallback below.
        if self._telemetry:
            self._telemetry.record_cache_hits(len(needed))

        return SynchronizedPowerData(
            inv1_pv_power=_cache_value_w(self._inv1_cache, rn.INPUT_POWER),
            inv2_pv_power=(
                _cache_value_w(self._inv2_cache, rn.INPUT_POWER)
                if self._inv2 is not None else None
            ),
            grid_power=(
                _cache_value_w(self._meter_cache, rn.POWER_METER_ACTIVE_POWER)
                if self._has_meter else None
            ),
            battery_power=(
                _cache_value_w(self._battery_cache, rn.STORAGE_CHARGE_DISCHARGE_POWER)
                if self._has_battery else None
            ),
            has_inv2=self._inv2 is not None,
            has_meter=self._has_meter,
            has_battery=self._has_battery,
            # Honest, measured spread -- not a fake 0 -- even for the
            # shortcut path: this is the actual age-spread across the
            # cached readings used, which passed the tolerance check above
            # but is still real information worth reporting, same as the
            # dedicated read already does.
            sample_span_ms=(max(ages) - min(ages)) * 1000.0 if ages else None,
            # v2.0.0a (F09/F20): is_temporally_uncertain correctly defaults
            # to False here, not set explicitly -- the shortcut already
            # refused to engage (returned None above) if the age-spread
            # exceeded SYNC_POWER_CACHE_ALIGNMENT_TOLERANCE_S, so any
            # SynchronizedPowerData actually constructed here is
            # well-aligned by construction, not by omission.
        )

    def _cache_only_snapshot(self) -> SynchronizedPowerData:
        """v2.0.9 (Phase 3.1, this release): the whole point of making
        dedicated reads optional -- when disabled, this coordinator must
        never perform a physical read AND must never claim temporal
        alignment, but the four entities should stay populated rather
        than going unavailable, since the underlying registers are kept
        warm regardless by the regular independent per-device
        coordinators (confirmed directly against source before this was
        built: INPUT_POWER, POWER_METER_ACTIVE_POWER, STORAGE_CHARGE_
        DISCHARGE_POWER are all already read by their own standard
        sensor entities, entirely independent of this coordinator).

        Deliberately more lenient than _try_cache_shortcut_impl() above,
        not just that method with the tolerance check removed: each
        value is taken independently -- one stale/unavailable value does
        not blank out the other three, matching the same per-field
        tolerance every other part of this engine already applies rather
        than an all-or-nothing gate. Quality.BAD is excluded (no usable
        value at all -- never read, known-wrong, or expired); Quality.
        GOOD and UNCERTAIN are both accepted (a real value exists,
        confirmed by register_cache.py's own Quality docstring), since
        cache-only mode's entire premise is best-effort staleness
        tolerance, not strict freshness.

        Always reports is_temporally_uncertain=True -- alignment was
        never checked at all in this mode, so claiming otherwise would
        be the exact "advertise a temporally uncertain composite as
        equivalent to an atomic measurement" problem today's audit
        explicitly warns against (§2.7's own requirement).
        """
        self.cache_only_snapshots += 1
        pairs: list[tuple["RegisterCache | None", Any, bool]] = [
            (self._inv1_cache, rn.INPUT_POWER, True),
            (self._inv2_cache, rn.INPUT_POWER, self._inv2 is not None),
            (self._meter_cache, rn.POWER_METER_ACTIVE_POWER, self._has_meter),
            (self._battery_cache, rn.STORAGE_CHARGE_DISCHARGE_POWER, self._has_battery),
        ]
        values: list[float | None] = []
        ages: list[float] = []
        for cache, name, applicable in pairs:
            if not applicable or cache is None:
                values.append(None)
                continue
            quality, _reason, age = cache.quality_of(name)
            if quality == Quality.BAD:
                values.append(None)
                continue
            values.append(_cache_value_w(cache, name))
            if age is not None:
                ages.append(age)

        return SynchronizedPowerData(
            inv1_pv_power=values[0],
            inv2_pv_power=values[1],
            grid_power=values[2],
            battery_power=values[3],
            has_inv2=self._inv2 is not None,
            has_meter=self._has_meter,
            has_battery=self._has_battery,
            sample_span_ms=(max(ages) - min(ages)) * 1000.0 if len(ages) >= 2 else None,
            is_temporally_uncertain=True,
        )

    async def _async_update_data(self) -> SynchronizedPowerData:
        """Read all power-flow registers as a best-effort, near-simultaneous
        sample -- NOT one atomic transaction.

        v2.0.0 (V2_ARCHITECTURE_DESIGN.md §8.2): first tries
        _try_cache_shortcut() -- if the regular per-device caches already
        hold GOOD, well-aligned values for everything needed, use those
        and skip the dedicated read below entirely. Falls through to the
        dedicated-read fallback whenever the shortcut isn't available.

        v2.0.0b FIX (MOD-01, external ICS audit -- confirmed): the
        fallback used to defeat the cache-first intent above -- ANY
        single miss (e.g. one value's age-spread slightly exceeding
        SYNC_POWER_CACHE_ALIGNMENT_TOLERANCE_S) triggered ALL FOUR
        physical reads, discarding whatever OTHER values were already
        fresh in the regular caches. Each of the four reads below now
        checks its own regular cache first (_read_one()) and only
        performs a physical read for the specific value that's actually
        missing -- reusing the exact per-device cache references
        _try_cache_shortcut() already holds, not a parallel mechanism.

        v2.0.0b FIX (MOD-02/MOD-04, external ICS audit -- confirmed): the
        four reads had no whole-operation deadline -- each individually
        bounded by UPDATE_TIMEOUT (35s), but nothing bounded the SUM
        (worst case 4 x 35s = 140s against a nominal 10s cadence).
        SYNC_POWER_POLL_DEADLINE (18s) is checked explicitly before
        STARTING each read (_deadline_exceeded()), not enforced via a
        single outer asyncio.timeout() wrapping the whole sequence --
        the latter would deliver cancellation through whichever read
        happens to be in flight when the deadline fires, discarding any
        already-collected partial results. Once exceeded, no further
        read is attempted and whatever was already gathered (from cache
        hits or completed reads) is returned as a partial result,
        exactly as it already was for any other partial-failure case.

        v1.3.19 NOTE (Defect V/Finding 9, independent ICS audit): each
        physical read is a minimal single-register ``batch_update`` call,
        but each acquires and releases its guard SEPARATELY -- other
        coordinators sharing the same guard can genuinely interleave
        between these reads. Holding one guard acquisition across the
        whole sequence was considered and rejected: it would block every
        other coordinator on the same bus for the full duration, directly
        undermining the fairness Defect P (v1.3.15) was built to guarantee.
        `sample_span_ms` on the returned data reports how much wall-clock
        time actually separated the first and last successful read, so
        this is measured and visible rather than silently assumed away.
        Partial failures are tolerated and reported as ``None`` in the
        result.
        """
        # v2.0.0a FIX (F13, external ICS audit -- confirmed): resetting
        # _consecutive_failures and logging "communication restored" here
        # used to happen on every cache-shortcut hit, even though NO Modbus
        # I/O occurred this cycle -- the whole point of the shortcut is to
        # skip the dedicated read entirely. That conflated "cache data is
        # available" with "this coordinator performed successful I/O", and
        # could mask a real, ongoing problem specific to this coordinator's
        # own communication path: if its dedicated reads were genuinely
        # failing for some coordinator-specific reason, but the regular
        # caches happened to have good data from OTHER coordinators' own
        # polling, this would incorrectly report "restored" and reset the
        # failure count without ever having verified anything. The counter
        # is simply left untouched on a shortcut hit now -- it reflects
        # only genuine I/O outcomes, from the dedicated-read path below.
        # v2.0.9 (Phase 3.1, this release): cache-only mode short-circuits
        # everything below -- no dedicated read is ever attempted, no
        # alignment tolerance is enforced. See _cache_only_snapshot()'s
        # own docstring for the full reasoning.
        if not self._dedicated_reads_enabled:
            return self._cache_only_snapshot()

        # v2.0.10 (finer-grained instrumentation, this release): one ID
        # for this whole update cycle, shared by every _read_one() call
        # below -- see self._next_logical_request_id's own comment.
        self._next_logical_request_id += 1
        logical_request_id = self._next_logical_request_id

        shortcut = self._try_cache_shortcut()
        if shortcut is not None:
            return shortcut

        any_success = False
        first_capture_at: float | None = None
        last_capture_at: float | None = None

        # v2.0.3 FIX (ICS-01, external ICS audit -- confirmed): renamed
        # from first/last_success_at and changed from "when did
        # _mark_success() get CALLED" to "when was this specific value
        # actually CAPTURED" -- these are not the same thing for a cache
        # hit. The old version timestamped every value (cache hit or
        # physical read alike) at the moment _read_one() happened to run,
        # which for a cache hit is irrelevant -- the value itself may
        # have been captured seconds earlier. sample_span_ms computed
        # from those call-time timestamps could look tight (all four
        # calls executing within milliseconds of each other) while
        # silently combining, say, a physical read from just now with a
        # cache value that is genuinely 2.9 seconds old -- exactly the
        # composite ICS-01 describes, invisible to the old metric.
        # Tracked as min/max across ALL captures, not first-call/last-call
        # order: a cache hit's own effective capture time can be earlier
        # than an earlier physical read's, even though the cache check
        # happens later in this sequence -- call order is not time order
        # once cache ages are involved.
        def _mark_success(capture_time: float) -> None:
            nonlocal any_success, first_capture_at, last_capture_at
            any_success = True
            if first_capture_at is None or capture_time < first_capture_at:
                first_capture_at = capture_time
            if last_capture_at is None or capture_time > last_capture_at:
                last_capture_at = capture_time

        timeout = self._update_timeout.total_seconds()
        deadline = time.monotonic() + SYNC_POWER_POLL_DEADLINE.total_seconds()
        deadline_hit = False

        def _deadline_exceeded() -> bool:
            nonlocal deadline_hit
            if time.monotonic() >= deadline:
                if not deadline_hit:
                    _LOGGER.debug(
                        "SyncPower: whole-operation deadline (%.0fs) exceeded "
                        "-- skipping any remaining reads, returning partial "
                        "results", SYNC_POWER_POLL_DEADLINE.total_seconds(),
                    )
                deadline_hit = True
                return True
            return False

        async def _read_one(
            guard: "ModbusGuard", device: Any, register: Any,
            cache: "RegisterCache | None", label: str,
        ) -> float | None:
            # MOD-01: the regular cache may already have exactly what we
            # need -- checked against Quality.GOOD specifically (not
            # merely "not BAD"), since we're about to substitute this for
            # a physical read and want the same freshness bar the
            # cache-shortcut itself uses.
            if cache is not None:
                quality, _reason, age = cache.quality_of(register)
                if quality == Quality.GOOD:
                    cached_value = _cache_value_w(cache, register)
                    if cached_value is not None:
                        # v2.0.3 (ICS-01): the cached value's own age is
                        # now what determines its effective capture time
                        # -- NOT the moment this check happened to run.
                        # age is None only if quality_of() itself has no
                        # age information (defensive fallback: treat as
                        # captured right now rather than crash or silently
                        # skip the alignment computation for this value).
                        _mark_success(time.monotonic() - (age or 0.0))
                        # v2.0.0b (AR-9, external ICS audit): this is the OTHER
                        # half of the same missing measurement fixed in
                        # _try_cache_shortcut() above -- every register served
                        # from cache here is one fewer physical read the
                        # pre-MOD-01 fallback would unconditionally have done.
                        if self._telemetry:
                            self._telemetry.record_cache_hit()
                        self.fallback_cache_hits += 1
                        return cached_value
            # MOD-02: don't start a new physical read once the
            # whole-operation deadline has passed.
            if _deadline_exceeded():
                return None
            try:
                # Labelled (previously bare guard.request()) so per-request
                # diagnostic capture (bus_diagnostics.py) can distinguish
                # SyncPower's own dedicated reads from every other
                # coordinator's traffic on the same bus -- found to be a
                # real gap while assessing whether enough telemetry
                # existed to answer the architecture question.
                #
                # v2.0.10 (finer-grained instrumentation, this release):
                # `as _req` added so this coordinator's own reads can
                # carry the same logical_request_id/register_names
                # attribution the main coordinator's _execute_batch()
                # already provides -- confirmed as a real, 45%-of-all-
                # traffic gap in a live field capture: every one of
                # SyncPower's own dedicated reads previously showed up
                # in a diagnostics capture with none of this attribution
                # at all, only distinguishable by their own label string.
                async with guard.request(label=label) as _req:
                    _req.logical_request_id = logical_request_id
                    _req.register_names = [str(register)]
                    async with asyncio.timeout(timeout):
                        result = await device.batch_update([register])
                        value = _extract_w(result, register)
                        # A physical read's effective capture time IS
                        # simply when it completed -- no age adjustment
                        # needed, unlike the cache-hit branch above.
                        _mark_success(time.monotonic())
                        if self._telemetry:
                            self._telemetry.record_request(1)
                        self.fallback_physical_reads += 1
                        return value
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("SyncPower: failed to read %s: %s", label, exc)
                if self._telemetry:
                    _record_failure(self._telemetry, exc)
                return None

        # ── read 1: INV1 PV power ───────────────────────────────────────────
        inv1_pv = await _read_one(
            self._primary_guard, self._inv1, rn.INPUT_POWER,
            self._inv1_cache, "INV1 INPUT_POWER",
        )

        # ── read 2: grid meter (only if present) ────────────────────────────
        grid = None
        if self._has_meter:
            grid = await _read_one(
                self._primary_guard, self._inv1, rn.POWER_METER_ACTIVE_POWER,
                self._meter_cache, "POWER_METER_ACTIVE_POWER",
            )

        # ── read 3: battery (only if present) ───────────────────────────────
        battery = None
        if self._has_battery:
            battery = await _read_one(
                self._primary_guard, self._inv1, rn.STORAGE_CHARGE_DISCHARGE_POWER,
                self._battery_cache, "STORAGE_CHARGE_DISCHARGE_POWER",
            )

        # ── read 4: INV2 PV power (secondary guard) ─────────────────────────
        inv2_pv = None
        if self._inv2 is not None and self._secondary_guard is not None:
            inv2_pv = await _read_one(
                self._secondary_guard, self._inv2, rn.INPUT_POWER,
                self._inv2_cache, "INV2 INPUT_POWER",
            )

        # ── failure handling ──────────────────────────────────────────────────
        if not any_success:
            self._consecutive_failures += 1
            raise UpdateFailed(
                f"SynchronizedPowerCoordinator: all reads failed "
                f"(consecutive: {self._consecutive_failures})"
            )

        if self._consecutive_failures > 0:
            _LOGGER.info(
                "SyncPower: communication restored after %d consecutive failure(s)",
                self._consecutive_failures,
            )
        self._consecutive_failures = 0

        sample_span_ms = (
            (last_capture_at - first_capture_at) * 1000.0
            if first_capture_at is not None and last_capture_at is not None
            else None
        )
        # v2.0.0a (F09/F20): explicit quality gate, not just instrumentation.
        # v2.0.3 (ICS-01): now computed from each value's own effective
        # capture time (age-adjusted for cache hits), not from when
        # _read_one() happened to be called for it -- see
        # first_capture_at/last_capture_at's own comment above for the
        # full reasoning on why that distinction matters.
        is_temporally_uncertain = (
            sample_span_ms is not None
            and sample_span_ms > SYNC_POWER_CACHE_ALIGNMENT_TOLERANCE_S * 1000.0
        )
        if is_temporally_uncertain:
            _LOGGER.debug(
                "SyncPower: sample span %.0f ms exceeds the %.0f ms alignment "
                "tolerance -- marking this reading temporally uncertain",
                sample_span_ms, SYNC_POWER_CACHE_ALIGNMENT_TOLERANCE_S * 1000.0,
            )
        # v2.0.5 (F-05): tracked here, the one place is_temporally_
        # uncertain is ever computed with a genuine sample_span_ms (the
        # aligned-shortcut path above always has it False by
        # construction, so is deliberately not double-counted here).
        if sample_span_ms is not None:
            self.results_with_span_computed += 1
            if is_temporally_uncertain:
                self.temporally_uncertain_count += 1

        return SynchronizedPowerData(
            inv1_pv_power=inv1_pv,
            inv2_pv_power=inv2_pv,
            grid_power=grid,
            battery_power=battery,
            has_inv2=self._inv2 is not None,
            has_meter=self._has_meter,
            has_battery=self._has_battery,
            sample_span_ms=sample_span_ms,
            is_temporally_uncertain=is_temporally_uncertain,
        )


# ── helpers ────────────────────────────────────────────────────────────────────

def _extract_w(result: dict[Any, Any], key: Any) -> float | None:
    """Pull the numeric watt value from a batch_update result dict."""
    try:
        val = result[key].value
        return float(val) if val is not None else None
    except (KeyError, AttributeError, TypeError, ValueError):
        return None


def _cache_value_w(cache: "RegisterCache | None", name: Any) -> float | None:
    """v2.0.0 (V2_ARCHITECTURE_DESIGN.md §8.2): the same extraction as
    _extract_w() above, but reading a single register directly from a
    RegisterCache rather than a fresh batch_update() result dict -- used
    by the cache-shortcut path in _try_cache_shortcut().
    """
    if cache is None:
        return None
    result = cache.get(name)
    if result is None:
        return None
    try:
        return float(result.value) if result.value is not None else None
    except (TypeError, ValueError):
        return None


def _record_failure(telemetry: ModbusTelemetry, exc: Exception) -> None:
    """Route the exception to the appropriate telemetry counter.

    v2.0.5 FIX (F-04, external ICS audit -- confirmed): this used to
    treat every TimeoutError as a device timeout unconditionally. But
    the guard.request() this helper's own caller (_read_one(), above)
    is wrapped around can itself raise ModbusQueueShed or
    ModbusAdmissionTimeout -- both TimeoutError subclasses representing
    internal bus contention, not a genuine device timeout -- exactly the
    same three-way distinction update_coordinator.py's own _record_
    timeout()/_record_shed()/_record_admission_timeout() already make.
    This was the one remaining record_timeout() call site in the whole
    project still collapsing all three into "device" by default.
    """
    if isinstance(exc, ModbusQueueShed):
        telemetry.record_timeout(kind="queue_shed")
    elif isinstance(exc, ModbusAdmissionTimeout):
        telemetry.record_timeout(kind="admission")
    elif isinstance(exc, TimeoutError):
        telemetry.record_timeout(kind="device")
    else:
        telemetry.record_failure()
