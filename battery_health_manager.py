"""Battery Health manager — Home Assistant glue for battery_health.py.

Responsibilities
----------------
1. Subscribe to the per-inverter energy-storage DataUpdateCoordinator with a
   ``{"register_names": [...]}`` context so the required registers are polled
   (same mechanism entities use — see update_coordinator.py step 2).
2. Convert each coordinator tick into a validated ``HealthSample`` and feed it
   to the pure ``BatteryHealthEngine``.
3. Persist engine state via ``homeassistant.helpers.storage.Store`` with a
   versioned schema and debounced writes (spec §8: never rely on recorder
   retention for 90-day windows; don't write on every poll tick).
4. Notify sensor entities via a lightweight listener list (same pattern as
   ModbusTelemetry).

Registry pattern mirrors ModbusTelemetry / AdaptiveModbusController:
per-serial singletons created in ``__init__.async_setup_entry`` and removed in
``async_unload_entry``.

Safety: this module performs NO Modbus writes. It only observes registers the
energy-storage coordinator reads.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from huawei_solar import register_values as rv

from .battery_health import (
    BatteryHealthConfig,
    BatteryHealthEngine,
    HealthReport,
    HealthSample,
    PackSample,
)
from .const import (
    CONF_BH_AMBIENT_ENTITY,
    CONF_BH_INSTALL_DATE,
    CONF_BH_MIN_SEGMENT_DELTA_SOC,
    CONF_BH_RATED_CAPACITY_KWH,
    CONF_BH_WARRANTY_THROUGHPUT_KWH,
    CONF_BH_WEIGHT_BALANCE,
    CONF_BH_WEIGHT_CAPACITY,
    CONF_BH_WEIGHT_EFFICIENCY,
    CONF_BH_WINDOW_DAYS,
    STORAGE_LOAD_TIMEOUT,
)

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceInfo

    from .register_cache import RegisterCache
    from .update_coordinator import HuaweiSolarUpdateCoordinator

from .register_cache import Quality

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY_PREFIX = "huawei_solar_battery_health"
MIN_SAVE_INTERVAL_S = 300.0          # debounce: at most one write per 5 min
PACK_COUNT = 3

# v2.0.8 FIX (Store-version conflation, found in production log during the
# 2.0.7 telemetry run -- not in either audit, discovered independently):
# HA's own Store class has ITS OWN version-mismatch/migration protocol,
# entirely separate from this module's own internal `schema_version`
# dict key (battery_health.py's SCHEMA_VERSION + _SCHEMA_MIGRATIONS,
# built for BH-09). Passing SCHEMA_VERSION directly as Store's own
# `version` constructor argument conflated the two: bumping our internal
# schema (2 -> 3, for TOPO-01) ALSO changed what HA's Store itself
# expected on disk, and Store's base `_async_migrate_func()` unconditionally
# raises NotImplementedError unless overridden -- confirmed directly
# against the installed homeassistant.helpers.storage source. The
# resulting NotImplementedError propagated out of self._store.async_load()
# itself, was caught by async_initialize()'s own OUTER try/except (the
# one guarding the load call, not the one guarding restore()), and `data`
# became None BEFORE engine.restore() -- and therefore BEFORE BH-09's own
# schema-mismatch handling -- ever ran. BH-09's whole point (recording
# last_schema_reset_ts/from_version, visible on the entity) silently
# never fired for the exact scenario it was built for.
#
# _HA_STORE_FORMAT_VERSION is deliberately a SEPARATE, FROZEN constant,
# not derived from SCHEMA_VERSION and never intended to change again --
# BatteryHealthStore's own _async_migrate_func() override below makes
# HA's version-mismatch handling a permanent no-op regardless of what
# number this is, so all future schema evolution routes exclusively
# through SCHEMA_VERSION/_SCHEMA_MIGRATIONS, where BH-09's own machinery
# can actually see and record it.
_HA_STORE_FORMAT_VERSION = 3


class BatteryHealthStore(Store):
    """Store subclass that hands HA's own version-mismatch handling off
    entirely to this module's own internal schema_version logic -- see
    _HA_STORE_FORMAT_VERSION's own module-level comment for the full
    reasoning. old_data is returned completely unchanged: whatever this
    module last wrote via engine.to_dict() is exactly what
    BatteryHealthEngine.restore() already knows how to interpret via its
    own schema_version key, regardless of what HA's own outer
    major/minor version happened to be on disk.
    """

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: Any,
    ) -> Any:
        return old_data

PACK_WORKING_STATUS_RUNNING = 2      # rv: 0=offline,1=standby,2=running,3=fault,4=sleep

# Register names (strings from huawei-solar register_names.py) required per
# storage unit 1. All are READ-ONLY telemetry registers.
_RN_SOC = "storage_state_of_capacity"                       # 37760, /10 %
_RN_POWER = "storage_charge_discharge_power"                # 37765, W (+chg/−dis)
_RN_TEMP = "storage_unit_1_battery_temperature"             # 37022, /10 °C
_RN_TOTAL_CHARGE = "storage_total_charge"                   # 37780, /100 kWh
_RN_TOTAL_DISCHARGE = "storage_total_discharge"             # 37782, /100 kWh
_RN_RATED_CAPACITY = "storage_rated_capacity"               # 37758, Wh (logged)
_RN_UNIT_CALIBRATION = "storage_unit_soh_calibration_status"  # 37926
#: v1.2.0 - "full" is defined relative to the CONFIGURED end-of-charge SOC,
#: not an absolute 100%. A user running a 93% summer cap still reaches their
#: ceiling daily; an absolute gate produced no anchors for 122 days in the
#: field. Read-only here; the integration exposes a separate writable entity.
_RN_END_OF_CHARGE_SOC = "storage_charging_cutoff_capacity"    # 47081, %

# v2.0.7 (TOPO-01 done properly, this release): per-pack register-name
# fields are now built dynamically, per (unit, pack) slot, instead of a
# fixed set of module-level lists hardcoded to storage_unit_1 only.
# Confirmed against the real register map before this change: storage
# unit 2's own block is a genuine, separately-addressed register range
# (offset +126 registers from unit 1's own, same per-pack layout),
# present in the underlying huawei-solar library today but never read by
# this integration at all until now. See _active_storage_units() below
# for how unit 2 is (or isn't) included, and why reading it must stay a
# hard, conditional gate.
_PACK_FIELD_SUFFIXES = (
    "voltage", "maximum_temperature", "minimum_temperature",
    "working_status", "soh_calibration_status", "state_of_capacity",
    "charge_discharge_power", "total_charge", "total_discharge",
    # v2.0.7 (Section F, this release): raw current and serial number --
    # see PackSample's own current_a/serial_number field comments for
    # the current, narrower scope (raw data only, not yet consumed by
    # any capacity/SOH computation -- that's Architecture Phases 2/3).
    "current", "serial_number",
)


def _pack_register_name(unit: int, pack: int, suffix: str) -> str:
    return f"storage_unit_{unit}_battery_pack_{pack}_{suffix}"


def pack_slots_for_units(units: list[int]) -> list[tuple[int, int]]:
    """Every (unit, pack) slot to poll and track, in a stable order --
    unit-major, then pack, e.g. [(1,1),(1,2),(1,3)] for a single unit, or
    [(1,1),(1,2),(1,3),(2,1),(2,2),(2,3)] with a second unit present.

    All PACK_COUNT slots are always included for every active unit, even
    though a given installation may have fewer PHYSICAL packs than that
    in a unit -- an absent pack's own working_status register simply
    never reads as "running", which BH-02's online-gating (battery_
    health.py) already correctly treats as "nothing to learn from this
    tick", the same tolerance already proven safe for a temporarily
    offline pack. This is deliberately NOT a live discovery/probe: see
    _active_storage_units()'s own docstring for why probing an absent
    UNIT (not an absent pack slot within a present unit) is a real risk
    this project avoids, and why the same reasoning does not apply to
    individual pack slots within a unit that is already known to exist.
    """
    return [(unit, pack) for unit in units for pack in range(1, PACK_COUNT + 1)]


def required_register_names(units: list[int]) -> list[str]:
    """Every register this subsystem needs, for the given set of active
    storage units. See pack_slots_for_units()/_active_storage_units()
    for the two different reasons a slot or a whole unit may or may not
    be included.
    """
    names = [
        _RN_SOC, _RN_POWER, _RN_TEMP, _RN_TOTAL_CHARGE, _RN_TOTAL_DISCHARGE,
        _RN_RATED_CAPACITY, _RN_UNIT_CALIBRATION, _RN_END_OF_CHARGE_SOC,
    ]
    for unit, pack in pack_slots_for_units(units):
        for suffix in _PACK_FIELD_SUFFIXES:
            names.append(_pack_register_name(unit, pack, suffix))
    return names


#: Backward-compatible default: unit 1 only, matching every currently-
#: confirmed real installation (including the one this project's own
#: hardware evidence comes from) and every existing test written against
#: that assumption. BatteryHealthManager itself computes its OWN, real
#: per-instance register list from actual discovered topology -- see
#: __init__'s own self._register_names -- this module-level constant is
#: no longer what actually gets polled, only a documented, tested
#: reference default for the single-unit case.
REQUIRED_REGISTER_NAMES: list[str] = required_register_names([1])


def _active_storage_units(device: Any) -> list[int]:
    """Which storage units (1 and/or 2) actually exist on this inverter.

    v2.0.7 (TOPO-01 done properly, this release): reuses the SAME proven
    capability flags (device.battery_1_type/battery_2_type, compared
    against StorageProductModel.NONE) this integration's own __init__.py
    already uses to decide whether to create a battery_2 HA device at
    all -- not a new or separately-reasoned mechanism.

    Unit 1's presence is NOT re-checked here: BatteryHealthManager is
    only ever constructed when the caller (__init__.py's async_setup_
    battery_health) has already confirmed connected_energy_storage is
    not None for this device, which is a stronger precondition than
    battery_1_type alone.

    Deliberately a hard, conditional gate, not a live probe: RegisterClient.
    get_multiple() (the core huawei-solar library) only succeeds when every
    register in one batch is "consecutively available" -- reading a
    genuinely absent second unit's registers would very likely fail the
    WHOLE containing Modbus chunk, not just silently return empty for
    that one register. This must never be attempted speculatively.
    """
    units = [1]
    battery_2_type = getattr(device, "battery_2_type", None)
    none_value = getattr(rv.StorageProductModel, "NONE", None)
    if battery_2_type is not None and battery_2_type != none_value:
        units.append(2)
    return units


def config_from_options(options: dict[str, Any] | None) -> BatteryHealthConfig:
    """Build a BatteryHealthConfig from config-entry options (spec §10)."""
    cfg = BatteryHealthConfig()
    if not options:
        return cfg
    cfg.rated_capacity_kwh = float(
        options.get(CONF_BH_RATED_CAPACITY_KWH, cfg.rated_capacity_kwh)
    )
    cfg.warranty_throughput_kwh = float(
        options.get(CONF_BH_WARRANTY_THROUGHPUT_KWH, cfg.warranty_throughput_kwh)
    )
    cfg.weight_capacity = float(options.get(CONF_BH_WEIGHT_CAPACITY, cfg.weight_capacity))
    cfg.weight_efficiency = float(
        options.get(CONF_BH_WEIGHT_EFFICIENCY, cfg.weight_efficiency)
    )
    cfg.weight_balance = float(options.get(CONF_BH_WEIGHT_BALANCE, cfg.weight_balance))
    cfg.capacity_window_days = float(options.get(CONF_BH_WINDOW_DAYS, cfg.capacity_window_days))
    cfg.min_segment_delta_soc = float(
        options.get(CONF_BH_MIN_SEGMENT_DELTA_SOC, cfg.min_segment_delta_soc)
    )
    # Finding D: the calendar-aging forecast needs the true install date;
    # otherwise an already-aged battery is modelled as new from the moment
    # this integration first ran.
    install = options.get(CONF_BH_INSTALL_DATE)
    if install:
        try:
            cfg.battery_install_ts = datetime.fromisoformat(
                str(install)
            ).replace(tzinfo=timezone.utc).timestamp()
        except (TypeError, ValueError):
            _LOGGER.warning(
                "battery_health: could not parse battery install date %r; "
                "falling back to first-observed date", install)
    return cfg


def _value(cache: "RegisterCache", data: dict[str, Any], name: str) -> Any:
    """Extract a Result.value from coordinator data -- but ONLY if its
    quality is GOOD, not merely present.

    v2.0.0 (V2_ARCHITECTURE_DESIGN.md §10.4's deliberate exception, and the
    specific vulnerability that motivated this entire rebuild -- see §1
    there and PHASE1_BATTERY_HEALTH_DESIGN.md's opening paragraph). Unlike
    a display entity, this consumer builds STATEFUL DELTAS from sequential
    readings (SOC drop across a segment, energy accumulated between
    anchors). RegisterCache.merge() now correctly serves a register whose
    quality is UNCERTAIN (link down, shed, back-off-deferred, ...) rather
    than dropping it -- the right behaviour for a display entity showing a
    probably-still-accurate value, but silently treating that same
    stale-served value as fresh here could corrupt the segment tracker's
    internal state in a way that persists and compounds, not just look
    briefly wrong for one tick.

    A quality-degraded register is therefore treated exactly like a
    genuinely missing one here (returns None) -- the engine's existing
    tolerance for None, already built and adversarially tested against
    real Modbus-saturation conditions this session, handles this
    correctly without any change to battery_health.py's engine internals.
    """
    result = data.get(name)
    if result is None:
        return None
    quality, _reason, _age = cache.quality_of(name)
    if quality != Quality.GOOD:
        return None
    return getattr(result, "value", result)


class BatteryHealthManager:
    """Per-serial singleton bridging the storage coordinator and the engine."""

    _registry: dict[str, "BatteryHealthManager"] = {}

    def __init__(
        self,
        hass: HomeAssistant,
        serial_number: str,
        coordinator: "HuaweiSolarUpdateCoordinator",
        device_info: "DeviceInfo",
        options: dict[str, Any] | None = None,
    ) -> None:
        self.hass = hass
        self.serial_number = serial_number
        self.coordinator = coordinator
        self.device_info = device_info
        # v2.0.7 (TOPO-01 done properly, this release): topology is
        # resolved ONCE at construction time, from the same coordinator.
        # device object __init__.py's own battery_1_device_info/
        # battery_2_device_info already use for exactly this decision --
        # not re-derived per tick. See _active_storage_units()/
        # pack_slots_for_units()'s own docstrings for the full reasoning
        # (why unit presence is a hard gate, never a live probe; why
        # every pack slot within a present unit is always read).
        self._active_units: list[int] = _active_storage_units(
            getattr(coordinator, "device", None)
        )
        self._pack_slots: list[tuple[int, int]] = pack_slots_for_units(
            self._active_units
        )
        self._register_names: list[str] = required_register_names(
            self._active_units
        )
        self.engine = BatteryHealthEngine(
            config_from_options(options),
            pack_count=len(self._pack_slots),
            pack_slot_labels=[f"u{u}p{p}" for u, p in self._pack_slots],
        )
        self._store: Store = BatteryHealthStore(
            hass, _HA_STORE_FORMAT_VERSION, f"{STORAGE_KEY_PREFIX}_{serial_number}"
        )
        self._listeners: list[Callable[[HealthReport], None]] = []
        self._unsub: Callable[[], None] | None = None
        self._last_save = 0.0
        self._last_update_success = True
        self.last_rated_capacity_wh: float | None = None
        # v1.1.6: entity-notification change detection — see _notify().
        self._last_signature: tuple | None = None
        #: Optional entity_id of an ambient temperature sensor (Finding: pack
        #: rise above ambient tracks heat generation). Read defensively every
        #: tick so replacing the sensor only needs an options change.
        self._ambient_entity: str | None = (options or {}).get(
            CONF_BH_AMBIENT_ENTITY) or None
        self._ambient_warned = False

    # ── registry (ModbusTelemetry pattern) ──────────────────────────────────
    @classmethod
    def create(
        cls,
        hass: HomeAssistant,
        serial_number: str,
        coordinator: "HuaweiSolarUpdateCoordinator",
        device_info: "DeviceInfo",
        options: dict[str, Any] | None = None,
    ) -> "BatteryHealthManager":
        mgr = cls(hass, serial_number, coordinator, device_info, options)
        cls._registry[serial_number] = mgr
        return mgr

    @classmethod
    def get(cls, serial_number: str) -> "BatteryHealthManager | None":
        return cls._registry.get(serial_number)

    @classmethod
    def remove(cls, serial_number: str) -> None:
        cls._registry.pop(serial_number, None)

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def async_initialize(self) -> None:
        """Load persisted state BEFORE first coordinator update (spec §8),
        then subscribe with the register-name context."""
        try:
            # v2.0.9 FIX (Phase 4.9, this release -- old DEF-012, external
            # ICS quality/defect/architecture audit -- confirmed): this
            # load had no timeout of its own -- a genuinely stalled Store
            # read (disk contention, a wedged filesystem) could block THIS
            # await, and therefore config-entry setup itself (this is
            # called directly from __init__.py's own async_setup_entry),
            # indefinitely. See STORAGE_LOAD_TIMEOUT's own comment in
            # const.py for the full reasoning, and adaptive_modbus.py's
            # own async_load() for the identical fix applied there --
            # both call self._store.async_load() on the same setup
            # critical path with the same gap. A TimeoutError here is
            # already correctly handled by the existing `except
            # Exception` below -- no new exception handling needed, only
            # the bound itself.
            data = await asyncio.wait_for(
                self._store.async_load(), timeout=STORAGE_LOAD_TIMEOUT.total_seconds()
            )
        except Exception:  # noqa: BLE001 — corrupt store must not block setup
            _LOGGER.exception(
                "battery_health[%s]: failed to load persisted state — starting fresh",
                self.serial_number,
            )
            data = None
        # v2.0.3 FIX (ICS-06, external ICS audit -- confirmed): restore()
        # used to sit OUTSIDE the try/except above -- a store that loaded
        # successfully (syntactically valid) but was structurally corrupt
        # (invalid numeric conversions, malformed segment/nested records,
        # unexpected container types anywhere in the tree restore() walks)
        # could make it raise, bypassing the "corrupt store must not
        # block setup" guarantee the load-failure branch above already
        # provides, and aborting initialization entirely -- silently
        # disabling battery-health tracking for this device (no listener
        # ever gets subscribed) rather than gracefully starting fresh.
        #
        # A bare try/except around the existing self.engine.restore(data)
        # call would not be enough on its own: restore() mutates several
        # of self.engine's own fields directly, in sequence (see
        # BatteryHealthEngine.restore()'s own body) -- if it raises
        # partway through, some fields would already reflect the corrupt
        # data while others do not, leaving self.engine in a genuinely
        # inconsistent mix, neither fully restored nor fully fresh.
        # Discarding that engine entirely and constructing a brand new
        # one (reusing its own already-resolved .cfg, not requiring the
        # original `options` this manager was constructed with) is the
        # only way to guarantee a REALLY clean, fully-fresh state after
        # a partial-restore failure, not just a partially-recovered one.
        try:
            self.engine.restore(data)
        except Exception:  # noqa: BLE001 — corrupt store must not block setup
            _LOGGER.exception(
                "battery_health[%s]: persisted state loaded but was "
                "structurally corrupt (restore() failed) — starting fresh",
                self.serial_number,
            )
            # v2.0.8 FIX (DEF-013, external ICS quality/defect/architecture
            # audit -- confirmed): this used to reconstruct with ONLY
            # self.engine.cfg, silently dropping the pack_count/
            # pack_slot_labels the real __init__ construction (above)
            # already resolved from actual discovered topology --
            # defaulting to pack_count=3 regardless of how many storage
            # units/packs this installation genuinely has. On a two-unit
            # system, a corrupt-state recovery would rebuild an engine
            # with 3 trackers instead of 6, mismatched against
            # self._pack_slots for the rest of this manager's own
            # lifetime. Passing the same topology __init__ already
            # resolved keeps a fallback rebuild consistent with it,
            # exactly as a normal (non-corrupt) construction already is.
            self.engine = BatteryHealthEngine(
                self.engine.cfg,
                pack_count=len(self._pack_slots),
                pack_slot_labels=[f"u{u}p{p}" for u, p in self._pack_slots],
            )
        # v1.2.1: after any (re)start, registers may briefly report stale or
        # default values.  Measurement resumes immediately; irreversible
        # learning waits out the settling period.
        self.engine.mark_recovery("integration start")
        self._unsub = self.coordinator.async_add_listener(
            self._handle_coordinator_update,
            context={"register_names": list(self._register_names)},
        )
        _LOGGER.info(
            "battery_health[%s]: initialized (%d segments restored, baseline %s)",
            self.serial_number,
            len(self.engine.segments.segments),
            "set" if self.engine.efficiency.baseline is not None else "pending",
        )

    async def async_unload(self) -> None:
        """Unsubscribe and flush state to disk."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        # v2.0.9 FIX (Phase 4.11, this release -- found during a log
        # review, not either external audit): this save previously had
        # neither a timeout NOR any exception handling at all -- a bare
        # await. A genuinely stalled write (disk contention, a wedged
        # filesystem) could block entry unload indefinitely; any failure
        # (not just a hang) would propagate uncaught, potentially
        # breaking the caller's own unload sequence entirely rather than
        # simply losing this one, best-effort final flush. Reuses
        # STORAGE_LOAD_TIMEOUT (const.py) -- same underlying concern
        # (bounded local HA Store I/O) as the load-side fix Phase 4.9
        # already applied to this exact class's own async_initialize(),
        # not a separately-tuned constant for what's genuinely the same
        # kind of operation.
        try:
            await asyncio.wait_for(
                self._store.async_save(self.engine.to_dict()),
                timeout=STORAGE_LOAD_TIMEOUT.total_seconds(),
            )
        except Exception:  # noqa: BLE001 — a failed final flush must not block unload
            _LOGGER.exception(
                "battery_health[%s]: failed to flush state to disk during "
                "unload; the most recent learning data since the last "
                "periodic save may be lost. Unload continues regardless",
                self.serial_number,
            )

    def snapshot(self) -> dict[str, Any]:
        """Point-in-time diagnostic snapshot for telemetry_capture.py --
        same public role AdaptiveModbusController.snapshot()/
        ModbusTelemetry.snapshot() already play for their own subsystems.

        v2.0.7 (Section E, this release): the whole reason this method
        exists -- every open Architecture Phase 2/3 question (condition
        coverage, unit-vs-pack residual, confidence-state distribution,
        segment cadence, normalization-floor frequency, per-pack current
        share, topology) needs a real time series to decide from, not
        just the live entity attributes, which telemetry_capture.py's
        periodic capture already provides for the Modbus side. Reuses
        self.engine.report.attributes wholesale -- that dict already
        carries every one of those fields (see battery_health.py's own
        _evaluate(), condition_coverage/combined_norm_floor_hits/
        pack_slot_labels/pack_replaced_count/
        pack_current_share_deviation_pct comments) -- rather than
        duplicating field-by-field extraction here, so a future addition
        to the entity's own attributes is automatically captured too.
        """
        report = self.engine.report
        return {
            "bhi": report.bhi,
            "confidence": report.confidence,
            "soh_capacity": report.soh_capacity,
            # v2.0.7 (TOPO-01 done properly, this release): topology
            # self-description -- without this, a capture from a
            # multi-unit installation would be uninterpretable without
            # cross-referencing entity attributes separately.
            "active_units": list(self._active_units),
            "pack_slots": [f"u{u}p{p}" for u, p in self._pack_slots],
            **report.attributes,
        }

    def stop(self) -> None:
        """Synchronous teardown of the listener (unload path helper)."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    # ── entity listener plumbing (ModbusTelemetry pattern) ──────────────────
    def add_listener(self, cb: Callable[[HealthReport], None]) -> None:
        self._listeners.append(cb)

    def remove_listener(self, cb: Callable[[HealthReport], None]) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    # ── actions ─────────────────────────────────────────────────────────────
    async def async_reset_efficiency_baseline(self) -> None:
        """Manual efficiency-baseline re-capture (button)."""
        self.engine.reset_efficiency_baseline()
        await self._flush_and_notify()

    async def async_reset_balance_baseline(self) -> None:
        """Re-anchor pack-balance scoring. Raw dV/dT are NOT affected."""
        self.engine.reset_balance_baseline()
        await self._flush_and_notify()

    async def async_reanchor_capacity_reference(self) -> bool:
        """Re-anchor SOH capacity to the current measured estimate."""
        applied = self.engine.reanchor_capacity_reference()
        if applied:
            await self._flush_and_notify()
        return applied

    async def async_set_learning_enabled(self, enabled: bool) -> None:
        """Maintenance inhibit for the learning phase (v1.2.1)."""
        self.engine.set_learning_enabled(enabled)
        await self._flush_and_notify()

    async def _flush_and_notify(self) -> None:
        await self._store.async_save(self.engine.to_dict())
        self._last_signature = None     # force the next tick to notify
        self._notify(self.engine.report)

    # ── coordinator callback ────────────────────────────────────────────────
    @callback
    def _handle_coordinator_update(self) -> None:
        coordinator = self.coordinator
        if not coordinator.last_update_success:
            # Read failure: skip the tick entirely; never treat the gap as
            # zero-duration/zero-value (spec §9).
            if self._last_update_success:
                self.engine.mark_gap()
            self._last_update_success = False
            return
        if not self._last_update_success:
            # Coordinator just came back: settle before trusting the data for
            # anything irreversible (v1.2.1).
            self.engine.mark_recovery("coordinator recovered")
        self._last_update_success = True

        data = coordinator.data or {}
        sample = self._build_sample(data)
        # v1.3.20 FIX (Defect X1, independent ICS audit): this is the one
        # call in this entire subsystem that was NOT fault-isolated --
        # every other callback here (listener dispatch, the entity-level
        # _on_health_update, async_added_to_hass) already follows this
        # project's v1.1.7 fault-isolation convention explicitly. If
        # engine.update() raised (the reachable weights-all-zero case is
        # now fixed at its root in battery_health.py, but this is a second,
        # independent line of defence against any other unforeseen input),
        # this callback would raise every single coordinator tick from then
        # on, and the engine would never advance again -- a silent,
        # permanent stall with nothing but a repeating log entry as a
        # symptom. This method already skips a tick cleanly for a read
        # failure (above); the same "skip this tick, try again next time"
        # response applies here.
        try:
            report = self.engine.update(sample)
        except Exception:  # noqa: BLE001 — one bad tick must not stall the engine forever
            _LOGGER.exception(
                "battery_health[%s]: engine.update() failed; skipping this "
                "tick, will retry on the next coordinator update",
                self.serial_number,
            )
            return

        # Log-and-watch: does 37758 step after a Huawei SOH calibration?
        # v2.0.0: quality-gated for the same reason as _build_sample's
        # fields -- a stale-served value could otherwise trigger a
        # misleading "possible SOH calibration step" log line when nothing
        # actually changed.
        rated = _value(coordinator.cache, data, _RN_RATED_CAPACITY)
        if rated is not None:
            try:
                rated_f = float(rated)
            except (TypeError, ValueError):
                rated_f = None
            if rated_f is not None:
                if (
                    self.last_rated_capacity_wh is not None
                    and abs(rated_f - self.last_rated_capacity_wh) >= 1.0
                ):
                    _LOGGER.warning(
                        "battery_health[%s]: storage_rated_capacity changed "
                        "%.0f → %.0f Wh — possible BMS SOH recalibration",
                        self.serial_number, self.last_rated_capacity_wh, rated_f,
                    )
                self.last_rated_capacity_wh = rated_f
                report.attributes["reported_rated_capacity_wh"] = rated_f

        # v1.1.6: notify entities only when a sensor-facing value actually
        # changed.  The engine runs every coordinator tick (30 s), but BHI
        # values move on the scale of days — pushing ten identical states per
        # tick only bloats the HA recorder.  The signature includes the
        # watched rated capacity so a BMS recalibration step still propagates.
        signature = (report.signature(), self.last_rated_capacity_wh)
        if signature != self._last_signature:
            self._last_signature = signature
            self._notify(report)
        self._maybe_save()

    def _build_sample(self, data: dict[str, Any]) -> HealthSample:
        # v2.0.0: single reference for every _value() call below to check
        # quality against -- see _value()'s own docstring for why this
        # consumer specifically needs quality-gating, unlike a display
        # entity.
        cache = self.coordinator.cache
        packs: list[PackSample] = []
        # v2.0.7 (TOPO-01 done properly, this release): iterates
        # self._pack_slots (every (unit, pack) slot actually part of
        # this installation's discovered topology, computed once at
        # construction time -- see __init__'s own comment) instead of a
        # fixed range(PACK_COUNT) over unit-1-only register names. Order
        # matches self.engine.pack_capacity.trackers/slot_labels exactly
        # -- both are built from the same self._pack_slots list, so
        # index i here always corresponds to the same physical slot as
        # tracker i there.
        for unit, pack in self._pack_slots:
            status = _value(
                cache, data, _pack_register_name(unit, pack, "working_status")
            )
            try:
                # int() handles both plain ints and IntEnum register values.
                online = int(status) == PACK_WORKING_STATUS_RUNNING
            except (TypeError, ValueError):
                online = False
            packs.append(
                PackSample(
                    voltage=_value(
                        cache, data, _pack_register_name(unit, pack, "voltage")
                    ),
                    temp_max=_value(
                        cache, data,
                        _pack_register_name(unit, pack, "maximum_temperature"),
                    ),
                    temp_min=_value(
                        cache, data,
                        _pack_register_name(unit, pack, "minimum_temperature"),
                    ),
                    online=online,
                    # v2.0.6 (Tier 3): feeds PackCapacityTracker -- see
                    # this module's own _PACK_FIELD_SUFFIXES comment for
                    # the full reasoning. Quality-gated via _value()
                    # exactly the same as every other field here -- no
                    # separate treatment needed.
                    soc=_value(
                        cache, data,
                        _pack_register_name(unit, pack, "state_of_capacity"),
                    ),
                    power_w=_value(
                        cache, data,
                        _pack_register_name(unit, pack, "charge_discharge_power"),
                    ),
                    lifetime_charge_kwh=_value(
                        cache, data, _pack_register_name(unit, pack, "total_charge")
                    ),
                    lifetime_discharge_kwh=_value(
                        cache, data,
                        _pack_register_name(unit, pack, "total_discharge"),
                    ),
                    # v2.0.7 (Section F, this release): raw current/
                    # serial number, quality-gated via _value() exactly
                    # the same as every other field here. Not yet
                    # consumed by any computation -- see PackSample's
                    # own field comments.
                    current_a=_value(
                        cache, data, _pack_register_name(unit, pack, "current")
                    ),
                    serial_number=_value(
                        cache, data,
                        _pack_register_name(unit, pack, "serial_number"),
                    ),
                )
            )

        calib_values = [_value(cache, data, _RN_UNIT_CALIBRATION)] + [
            _value(cache, data, _pack_register_name(unit, pack, "soh_calibration_status"))
            for unit, pack in self._pack_slots
        ]
        calibration_active = False
        for cv in calib_values:
            raw = cv
            try:
                # Huawei: 0 = not started/idle. Any non-zero = check in
                # progress or just completed on this unit/pack.
                if raw is not None and int(raw) != 0:
                    calibration_active = True
                    break
            except (TypeError, ValueError):
                continue

        return HealthSample(
            timestamp=time.time(),
            soc=_value(cache, data, _RN_SOC),
            power_w=_value(cache, data, _RN_POWER),
            battery_temp_c=_value(cache, data, _RN_TEMP),
            lifetime_charge_kwh=_value(cache, data, _RN_TOTAL_CHARGE),
            lifetime_discharge_kwh=_value(cache, data, _RN_TOTAL_DISCHARGE),
            packs=packs,
            soh_calibration_active=calibration_active,
            charge_ceiling_soc=_value(cache, data, _RN_END_OF_CHARGE_SOC),
            ambient_temp_c=self._read_ambient(),
        )

    def _read_ambient(self) -> float | None:
        """Read the optional ambient temperature sensor, if configured.

        Never raises and never blocks: a missing, renamed, or unavailable
        entity simply disables the thermal-rise attributes.
        """
        if not self._ambient_entity:
            return None
        state = self.hass.states.get(self._ambient_entity)
        if state is None or state.state in ("unknown", "unavailable"):
            if not self._ambient_warned:
                self._ambient_warned = True
                _LOGGER.warning(
                    "battery_health[%s]: ambient temperature entity %s is "
                    "unavailable; thermal-rise diagnostics are disabled until "
                    "it returns (change it under the integration options)",
                    self.serial_number, self._ambient_entity)
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        self._ambient_warned = False
        return value

    def _notify(self, report: HealthReport) -> None:
        for cb in list(self._listeners):
            try:
                cb(report)
            except Exception:  # noqa: BLE001 — one bad entity must not break the rest
                _LOGGER.exception("battery_health[%s]: listener failed", self.serial_number)

    def set_pack_install_date(self, serial: str, install_ts: float) -> None:
        """v2.0.12 (Battery Phase 5B UI restructuring, this release):
        shared write path for a pack's own explicit install date --
        used by BOTH the set_pack_install_date service (services.py)
        and the new per-pack date entity (date.py), so the two can
        never drift out of sync by duplicating this logic separately.
        An explicit, deliberate, infrequent user action -- persisted
        promptly (see _maybe_save()'s own docstring for why this
        doesn't wait on the normal engine-tick debounce).
        """
        self.engine.pack_capacity.pack_install_dates[serial] = install_ts
        self.engine.dirty = True
        self._maybe_save()

    def _maybe_save(self) -> None:
        """Debounced persistence: on engine 'dirty' events (segment closed,
        baseline captured, counter reset) but at most every 5 minutes."""
        if not self.engine.dirty:
            return
        now = time.monotonic()
        if now - self._last_save < MIN_SAVE_INTERVAL_S:
            return
        self._last_save = now
        self.engine.dirty = False
        self._store.async_delay_save(self.engine.to_dict, 10.0)
