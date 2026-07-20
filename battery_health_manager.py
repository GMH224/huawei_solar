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

import logging
import time
from typing import TYPE_CHECKING, Any, Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .battery_health import (
    BatteryHealthConfig,
    BatteryHealthEngine,
    HealthReport,
    HealthSample,
    PackSample,
    SCHEMA_VERSION,
)
from .const import (
    CONF_BH_MIN_SEGMENT_DELTA_SOC,
    CONF_BH_RATED_CAPACITY_KWH,
    CONF_BH_WARRANTY_THROUGHPUT_KWH,
    CONF_BH_WEIGHT_BALANCE,
    CONF_BH_WEIGHT_CAPACITY,
    CONF_BH_WEIGHT_EFFICIENCY,
    CONF_BH_WINDOW_DAYS,
)

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceInfo

    from .update_coordinator import HuaweiSolarUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY_PREFIX = "huawei_solar_battery_health"
MIN_SAVE_INTERVAL_S = 300.0          # debounce: at most one write per 5 min
PACK_COUNT = 3
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

_RN_PACK_VOLTAGE = [
    f"storage_unit_1_battery_pack_{i}_voltage" for i in range(1, PACK_COUNT + 1)
]
_RN_PACK_TMAX = [
    f"storage_unit_1_battery_pack_{i}_maximum_temperature"
    for i in range(1, PACK_COUNT + 1)
]
_RN_PACK_TMIN = [
    f"storage_unit_1_battery_pack_{i}_minimum_temperature"
    for i in range(1, PACK_COUNT + 1)
]
_RN_PACK_STATUS = [
    f"storage_unit_1_battery_pack_{i}_working_status"
    for i in range(1, PACK_COUNT + 1)
]
_RN_PACK_CALIBRATION = [
    f"storage_unit_1_battery_pack_{i}_soh_calibration_status"
    for i in range(1, PACK_COUNT + 1)
]

REQUIRED_REGISTER_NAMES: list[str] = [
    _RN_SOC,
    _RN_POWER,
    _RN_TEMP,
    _RN_TOTAL_CHARGE,
    _RN_TOTAL_DISCHARGE,
    _RN_RATED_CAPACITY,
    _RN_UNIT_CALIBRATION,
    *_RN_PACK_VOLTAGE,
    *_RN_PACK_TMAX,
    *_RN_PACK_TMIN,
    *_RN_PACK_STATUS,
    *_RN_PACK_CALIBRATION,
]


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
    return cfg


def _value(data: dict[str, Any], name: str) -> Any:
    """Extract a Result.value from coordinator data, tolerating absence."""
    result = data.get(name)
    if result is None:
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
        self.engine = BatteryHealthEngine(config_from_options(options))
        self._store: Store = Store(
            hass, SCHEMA_VERSION, f"{STORAGE_KEY_PREFIX}_{serial_number}"
        )
        self._listeners: list[Callable[[HealthReport], None]] = []
        self._unsub: Callable[[], None] | None = None
        self._last_save = 0.0
        self._last_update_success = True
        self.last_rated_capacity_wh: float | None = None
        # v1.1.6: entity-notification change detection — see _notify().
        self._last_signature: tuple | None = None

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
            data = await self._store.async_load()
        except Exception:  # noqa: BLE001 — corrupt store must not block setup
            _LOGGER.exception(
                "battery_health[%s]: failed to load persisted state — starting fresh",
                self.serial_number,
            )
            data = None
        self.engine.restore(data)
        self._unsub = self.coordinator.async_add_listener(
            self._handle_coordinator_update,
            context={"register_names": list(REQUIRED_REGISTER_NAMES)},
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
        await self._store.async_save(self.engine.to_dict())

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
        await self._store.async_save(self.engine.to_dict())
        self._last_signature = None     # force next tick to notify
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
        self._last_update_success = True

        data = coordinator.data or {}
        sample = self._build_sample(data)
        report = self.engine.update(sample)

        # Log-and-watch: does 37758 step after a Huawei SOH calibration?
        rated = _value(data, _RN_RATED_CAPACITY)
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
        packs: list[PackSample] = []
        for i in range(PACK_COUNT):
            status = _value(data, _RN_PACK_STATUS[i])
            try:
                # int() handles both plain ints and IntEnum register values.
                online = int(status) == PACK_WORKING_STATUS_RUNNING
            except (TypeError, ValueError):
                online = False
            packs.append(
                PackSample(
                    voltage=_value(data, _RN_PACK_VOLTAGE[i]),
                    temp_max=_value(data, _RN_PACK_TMAX[i]),
                    temp_min=_value(data, _RN_PACK_TMIN[i]),
                    online=online,
                )
            )

        calib_values = [_value(data, _RN_UNIT_CALIBRATION)] + [
            _value(data, name) for name in _RN_PACK_CALIBRATION
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
            soc=_value(data, _RN_SOC),
            power_w=_value(data, _RN_POWER),
            battery_temp_c=_value(data, _RN_TEMP),
            lifetime_charge_kwh=_value(data, _RN_TOTAL_CHARGE),
            lifetime_discharge_kwh=_value(data, _RN_TOTAL_DISCHARGE),
            packs=packs,
            soh_calibration_active=calibration_active,
        )

    def _notify(self, report: HealthReport) -> None:
        for cb in list(self._listeners):
            try:
                cb(report)
            except Exception:  # noqa: BLE001 — one bad entity must not break the rest
                _LOGGER.exception("battery_health[%s]: listener failed", self.serial_number)

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
