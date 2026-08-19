"""HA sensor entities for the Battery Health Index subsystem.

Follows the ModbusTelemetry entity pattern: push-based entities subscribed to
a per-serial manager singleton, no coordinator subclassing required.  Wired
into the sensor platform via ``create_battery_health_entities`` in
``sensor.async_setup_entry``.
"""

from __future__ import annotations

import logging
import re
from typing import Any, TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTime, EntityCategory
from homeassistant.core import callback

from .battery_health import HealthReport
from .battery_health_manager import BatteryHealthManager

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceInfo

_LOGGER = logging.getLogger(__name__)


#: Attribute keys whose native_value is a STRING, not a number.
#: Home Assistant's SensorEntity.state property raises ValueError if a sensor
#: carries any numeric-implying hint (unit, state_class, device_class, or a
#: suggested_display_precision) while returning a non-numeric value.  Keys
#: listed here therefore must never receive a precision hint, and are declared
#: with device_class ENUM + an explicit options list instead.
_STRING_VALUED_KEYS: frozenset[str] = frozenset({"confidence"})

#: Valid states of the confidence sensor (must match BatteryHealthEngine).
_CONFIDENCE_OPTIONS: list[str] = ["low", "normal", "stale"]


def _round1(v: float | None) -> float | None:
    return None if v is None else round(v, 1)


# (attr_key, name, unit, icon, extra_attrs, value_fn)
_BATTERY_HEALTH_SENSORS: list[tuple[str, str, str | None, str, dict[str, Any]]] = [
    (
        "bhi",
        "Battery health index",
        PERCENTAGE,
        "mdi:battery-heart-variant",
        {"state_class": SensorStateClass.MEASUREMENT},
    ),
    (
        "confidence",
        "Battery health confidence",
        None,
        "mdi:check-decagram-outline",
        {
            # ENUM is HA's idiomatic declaration for a string-valued sensor.
            # Without it (v1.1.5/v1.1.6) the class-level precision hint made HA
            # treat "low"/"normal"/"stale" as an invalid numeric state, so the
            # entity failed to be added and every later update raised.
            "device_class": SensorDeviceClass.ENUM,
            "options": _CONFIDENCE_OPTIONS,
            "entity_category": EntityCategory.DIAGNOSTIC,
        },
    ),
    (
        "soh_capacity",
        "Battery SOH capacity",
        PERCENTAGE,
        "mdi:battery-charging-100",
        {
            "state_class": SensorStateClass.MEASUREMENT,
            "entity_category": EntityCategory.DIAGNOSTIC,
        },
    ),
    (
        "soh_efficiency",
        "Battery SOH efficiency",
        PERCENTAGE,
        "mdi:swap-vertical-circle-outline",
        {
            "state_class": SensorStateClass.MEASUREMENT,
            "entity_category": EntityCategory.DIAGNOSTIC,
        },
    ),
    (
        "soh_balance",
        "Battery SOH balance",
        PERCENTAGE,
        "mdi:scale-balance",
        {
            "state_class": SensorStateClass.MEASUREMENT,
            "entity_category": EntityCategory.DIAGNOSTIC,
        },
    ),
    (
        "stress_index",
        "Battery stress index",
        PERCENTAGE,
        "mdi:thermometer-alert",
        {
            "state_class": SensorStateClass.MEASUREMENT,
            "entity_category": EntityCategory.DIAGNOSTIC,
            "entity_registry_enabled_default": False,
        },
    ),
    (
        "predicted_soh",
        "Battery predicted SOH",
        PERCENTAGE,
        "mdi:chart-line",
        {
            "state_class": SensorStateClass.MEASUREMENT,
            "entity_category": EntityCategory.DIAGNOSTIC,
            "entity_registry_enabled_default": False,
        },
    ),
    (
        "health_divergence",
        "Battery health divergence",
        None,
        "mdi:call-split",
        {
            "state_class": SensorStateClass.MEASUREMENT,
            "entity_category": EntityCategory.DIAGNOSTIC,
        },
    ),
    (
        "efc",
        "Battery equivalent full cycles",
        None,
        "mdi:battery-sync",
        {"state_class": SensorStateClass.MEASUREMENT},
    ),
    (
        "warranty_consumed_pct",
        "Battery warranty throughput consumed",
        PERCENTAGE,
        "mdi:certificate-outline",
        {
            "state_class": SensorStateClass.MEASUREMENT,
            "entity_category": EntityCategory.DIAGNOSTIC,
        },
    ),
]


def create_battery_health_entities(
    manager: BatteryHealthManager,
) -> list["HuaweiSolarBatteryHealthSensorEntity"]:
    """Create all battery-health sensor entities for one manager."""
    return [
        HuaweiSolarBatteryHealthSensorEntity(manager, attr_key, name, unit, icon, extra)
        for attr_key, name, unit, icon, extra in _BATTERY_HEALTH_SENSORS
    ]


class HuaweiSolarBatteryHealthSensorEntity(SensorEntity):
    """Push-based sensor backed by a BatteryHealthManager."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    # NOTE (v1.1.7): suggested_display_precision must NOT be a class attribute.
    # HA treats its presence as a promise that native_value is numeric and
    # raises ValueError for any string state, which silently killed the
    # `confidence` entity in v1.1.5/v1.1.6.  It is applied per-instance below,
    # only for numeric-valued keys.

    def __init__(
        self,
        manager: BatteryHealthManager,
        attr_key: str,
        name: str,
        unit: str | None,
        icon: str,
        extra: dict[str, Any],
    ) -> None:
        self._manager = manager
        self._attr_key = attr_key
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        if attr_key not in _STRING_VALUED_KEYS:
            self._attr_suggested_display_precision = 1
        self._attr_icon = icon
        self._attr_device_info = manager.device_info
        self._attr_unique_id = (
            f"{manager.serial_number}_battery_health_{attr_key}"
        )
        self._attr_native_value: Any = None
        self._attr_available = True

        for k, v in extra.items():
            setattr(self, f"_attr_{k}", v)

        self._cb = self._on_health_update

    async def async_added_to_hass(self) -> None:
        """Register with the manager and populate from the last report.

        Fault isolation (v1.1.7): registration and the initial value read are
        guarded so a manager in an unexpected state can never prevent this
        entity — or the rest of the sensor platform — from being added.
        """
        try:
            self._manager.add_listener(self._cb)
            self._apply(self._manager.engine.report)
        except Exception:  # noqa: BLE001 — never block platform setup
            _LOGGER.exception(
                "battery_health: failed to initialise entity %s; it will "
                "report unknown until the next successful update",
                self._attr_unique_id,
            )

    async def async_will_remove_from_hass(self) -> None:
        """Deregister callback."""
        self._manager.remove_listener(self._cb)

    @callback
    def _on_health_update(self, report: HealthReport) -> None:
        """Apply a new report and write state.

        Guarded (v1.1.7): the manager already isolates listener exceptions from
        one another, but a failure here must additionally never leave the
        entity holding a value HA cannot serialise.
        """
        try:
            self._apply(report)
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "battery_health: failed to apply report to %s", self._attr_unique_id
            )
            return
        self.async_write_ha_state()

    def _apply(self, report: HealthReport) -> None:
        value = getattr(report, self._attr_key, None)
        # "No sub-scores computable at all" → entity must read unavailable/
        # unknown, never 0 (spec §9). None native_value renders as unknown.
        self._attr_native_value = value

        if self._attr_key == "bhi":
            self._attr_extra_state_attributes = {
                **report.attributes,
                "soh_capacity": _round1(report.soh_capacity),
                "soh_efficiency": _round1(report.soh_efficiency),
                "soh_balance": _round1(report.soh_balance),
                "confidence": report.confidence,
                "note": (
                    "Self-referential trend proxy, not a validated diagnostic. "
                    "Track change over time, not the absolute number. "
                    "Sub-scores are measured against learned per-installation "
                    "baselines; raw values are exposed alongside them. "
                    "See BATTERY_HEALTH.md."
                ),
            }
        elif self._attr_key == "warranty_consumed_pct":
            self._attr_extra_state_attributes = {
                "note": (
                    "Warranty/legal reference (CH/EEA: 28.84 MWh to 60% "
                    "retention), NOT '% of real battery life used'. Real LFP "
                    "cycle life is typically far higher."
                ),
            }
        elif self._attr_key == "predicted_soh":
            self._attr_extra_state_attributes = {
                "note": (
                    "Heuristic √t calendar + throughput cycle model. Used for "
                    "divergence detection against measured SOH, not as a "
                    "lab-grade prediction."
                ),
                "stress_ratio": report.stress_ratio,
            }
        elif self._attr_key == "confidence":
            self._attr_extra_state_attributes = {
                "contributing_terms": report.attributes.get("contributing_terms", []),
                "held_terms": report.attributes.get("held_terms", []),
                "segment_count": report.attributes.get("segment_count"),
                "excluded_calibration_segment_count": report.attributes.get(
                    "excluded_calibration_segment_count"),
                "discarded_segment_count": report.attributes.get(
                    "discarded_segment_count"),
                "gap_bridged_count": report.attributes.get("gap_bridged_count"),
                "stale_endpoint_skips": report.attributes.get("stale_endpoint_skips"),
                "efficiency_window_count": report.attributes.get(
                    "efficiency_window_count"),
                "balance_sample_count": report.attributes.get("balance_sample_count"),
            }
        elif self._attr_key == "soh_capacity":
            self._attr_extra_state_attributes = {
                k: report.attributes.get(k) for k in (
                    "estimated_capacity_kwh", "capacity_spread_kwh",
                    "capacity_reference_kwh", "capacity_reference_is_measured",
                    "capacity_reference_captured", "capacity_reference_epochs",
                    "segment_soc_midpoint_mean", "segment_charge_ceiling_mean",
                    "segment_count",
                    # v2.0.12 FIX (Battery Phase 5B UI restructuring, this
                    # release): the genuinely PER-PACK values that used
                    # to live here (pack_capacity_soh_percent, pack_
                    # capacity_segment_count, pack_replaced_count,
                    # pack_age_days, pack_age_source) have been removed
                    # -- confirmed with the user directly that this
                    # placement didn't match how every OTHER per-pack
                    # value in this integration is exposed (individual
                    # entities under that pack's own "Battery 1"/
                    # "Battery 2" device, per sensor.py's own
                    # BATTERY_TEMPLATE_SENSOR_DESCRIPTIONS convention).
                    # See create_battery_health_pack_entities() below
                    # for where they live now. Only genuinely AGGREGATE
                    # values -- ones with no single pack they belong to
                    # -- remain here.
                    "pack_capacity_spread_pct",
                    "soh_capacity_source", "weakest_pack_slot",
                    "soh_capacity_unit_independent", "capacity_cross_check_diverged",
                    # pack_slot_labels retained here as index-reference
                    # context (which slot is index 0, 1, 2 -- useful
                    # alongside weakest_pack_slot above); retired_pack_
                    # history retained here since a retired pack has no
                    # live slot to attach an individual entity to.
                    "pack_slot_labels", "retired_pack_history",
                )
            }
        elif self._attr_key == "soh_balance":
            # Raw dV/dT are ground truth and are never re-zeroed by any
            # recalibration - the baseline only re-zeroes the derived score.
            self._attr_extra_state_attributes = {
                k: report.attributes.get(k) for k in (
                    "balance_raw_dv", "balance_raw_dt",
                    "balance_baseline_dv", "balance_baseline_dt",
                    "balance_baseline_captured", "balance_baseline_epochs",
                    "balance_sample_soc_mean", "balance_sample_count",
                    "packs_included", "packs_excluded",
                )
            }
        elif self._attr_key == "soh_efficiency":
            self._attr_extra_state_attributes = {
                k: report.attributes.get(k) for k in (
                    "efficiency_baseline", "efficiency_current",
                    "efficiency_baseline_tier", "efficiency_current_tier",
                    "efficiency_window_count", "efficiency_baseline_epochs",
                    "efficiency_charge_ceiling",
                )
            }


# ── Per-pack entities (v2.0.12, Battery Phase 5B UI restructuring) ──────────
#
# The sensors above are all attached to the aggregate "Batteries" device
# (manager.device_info) and read a single scalar off the report -- correct
# for genuinely aggregate/cross-pack values (BHI, confidence, which pack is
# weakest, the spread between packs). But several values that were also
# being exposed that way are genuinely PER-PACK (one value belonging to one
# specific physical pack, not the whole system) -- pack_capacity_soh_percent,
# pack_capacity_segment_count, pack_replaced_count, pack_age_days, and
# pack_age_source were all list-valued attributes bundled onto the
# aggregate soh_capacity sensor, which does not match how every OTHER
# per-pack value in this integration is exposed: sensor.py's own
# BATTERY_TEMPLATE_SENSOR_DESCRIPTIONS creates one individual entity per
# pack (translation keys like "pack_1_state_of_capacity"), attached to
# that pack's own physical storage-unit device ("Battery 1"/"Battery 2"),
# not a separate aggregate device. This section corrects that mismatch,
# found and confirmed directly with the user rather than assumed --
# real per-pack sensors moved here, genuinely aggregate ones (weakest_
# pack_slot, pack_capacity_spread_pct, soh_capacity_source, soh_capacity_
# unit_independent, capacity_cross_check_diverged) deliberately left on
# the existing soh_capacity sensor above, where they still belong.

#: Public (no leading underscore, deliberately): shared across modules --
#: date.py's own create_battery_health_pack_date_entities() imports this
#: directly rather than duplicating it, so the slot-label parsing used
#: to decide which physical device a pack's entities attach to can
#: never drift between the sensor and date platforms.
SLOT_LABEL_RE = re.compile(r"^u(\d+)p(\d+)$")

#: pack_age_source is STRING-valued, like confidence above -- must never
#: receive a numeric precision hint (see _STRING_VALUED_KEYS's own comment
#: for the exact bug class this prevents).
_STRING_VALUED_PACK_KEYS: frozenset[str] = frozenset({"pack_age_source"})

_PACK_AGE_SOURCE_OPTIONS: list[str] = [
    "install_date", "unit_install_date", "first_detected", "unknown",
]

# (report_attribute_key, name_suffix, unit, icon, extra_attrs)
_PACK_HEALTH_SENSORS: list[tuple[str, str, str | None, str, dict[str, Any]]] = [
    (
        "pack_capacity_soh_percent",
        "SOH capacity",
        PERCENTAGE,
        "mdi:battery-charging-100",
        {
            "state_class": SensorStateClass.MEASUREMENT,
            "entity_category": EntityCategory.DIAGNOSTIC,
        },
    ),
    (
        "pack_capacity_segment_count",
        "SOH capacity segment count",
        None,
        "mdi:counter",
        {
            "entity_category": EntityCategory.DIAGNOSTIC,
            # Diagnostic/evidence-quality detail, not a health value
            # itself -- matches this integration's own convention of
            # disabling secondary diagnostic detail by default (see
            # sensor.py's own BATTERY_TEMPLATE_SENSOR_DESCRIPTIONS,
            # entity_registry_enabled_default=False throughout).
            "entity_registry_enabled_default": False,
        },
    ),
    (
        "pack_replaced_count",
        "Replaced count",
        None,
        "mdi:swap-horizontal",
        {"entity_category": EntityCategory.DIAGNOSTIC},
    ),
    (
        "pack_age_days",
        "Age",
        UnitOfTime.DAYS,
        "mdi:calendar-clock",
        {
            "state_class": SensorStateClass.MEASUREMENT,
            "entity_category": EntityCategory.DIAGNOSTIC,
        },
    ),
    (
        "pack_age_source",
        "Age source",
        None,
        "mdi:information-outline",
        {
            "device_class": SensorDeviceClass.ENUM,
            "options": _PACK_AGE_SOURCE_OPTIONS,
            "entity_category": EntityCategory.DIAGNOSTIC,
        },
    ),
]


def create_battery_health_pack_entities(
    manager: BatteryHealthManager,
    battery_1_device_info: "DeviceInfo | None",
    battery_2_device_info: "DeviceInfo | None",
) -> list["HuaweiSolarBatteryHealthPackSensorEntity"]:
    """Create per-pack Battery Health sensor entities, one set per pack,
    each attached to that specific pack's own physical storage-unit
    device -- NOT the aggregate "Batteries" device the rest of this
    module's entities use. See this section's own module-level comment
    for the full reasoning.

    Slot labels are parsed (not string-prefix-matched) to find each
    pack's own unit number and pack number -- robust to any pack count,
    not assuming single-digit unit numbers stay single-digit forever.
    A slot whose corresponding storage-unit device_info is unavailable
    (or a slot_label that doesn't parse, which should not happen in
    practice) is skipped rather than raising -- matches the same fault-
    isolation posture already used for the aggregate entities' own
    setup path in sensor.py.
    """
    slot_labels = manager.engine.pack_capacity.slot_labels
    entities: list[HuaweiSolarBatteryHealthPackSensorEntity] = []
    for i, slot_label in enumerate(slot_labels):
        match = SLOT_LABEL_RE.match(slot_label)
        if match is None:
            continue
        unit_number, pack_number = match.group(1), match.group(2)
        if unit_number == "1":
            device_info = battery_1_device_info
        elif unit_number == "2":
            device_info = battery_2_device_info
        else:
            device_info = None
        if device_info is None:
            continue
        for report_key, name_suffix, unit, icon, extra in _PACK_HEALTH_SENSORS:
            entities.append(
                HuaweiSolarBatteryHealthPackSensorEntity(
                    manager, device_info, i, slot_label, pack_number,
                    report_key, name_suffix, unit, icon, extra,
                )
            )
    return entities


class HuaweiSolarBatteryHealthPackSensorEntity(SensorEntity):
    """Push-based per-pack sensor, backed by the SAME BatteryHealthManager
    as the aggregate sensors above, but reading one specific index out
    of a list-valued report attribute, and attached to that pack's own
    physical storage-unit device instead of the aggregate one.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        manager: BatteryHealthManager,
        device_info: "DeviceInfo",
        pack_index: int,
        slot_label: str,
        pack_number: str,
        report_key: str,
        name_suffix: str,
        unit: str | None,
        icon: str,
        extra: dict[str, Any],
    ) -> None:
        self._manager = manager
        self._pack_index = pack_index
        self._report_key = report_key
        # e.g. "Pack 2 SOH capacity" -- pack number in the name is
        # required, not cosmetic: multiple packs can share the same
        # device (e.g. u1p1 and u1p2 both under "Battery 1"), so
        # without it every pack's own entity would collide on the same
        # display name within that device.
        self._attr_name = f"Pack {pack_number} {name_suffix}"
        self._attr_native_unit_of_measurement = unit
        if report_key not in _STRING_VALUED_PACK_KEYS:
            self._attr_suggested_display_precision = 1
        self._attr_icon = icon
        self._attr_device_info = device_info
        self._attr_unique_id = (
            f"{manager.serial_number}_battery_health_pack_{slot_label}_{report_key}"
        )
        self._attr_native_value: Any = None
        self._attr_available = True

        for k, v in extra.items():
            setattr(self, f"_attr_{k}", v)

        self._cb = self._on_health_update

    async def async_added_to_hass(self) -> None:
        """Register with the manager and populate from the last report.

        Fault isolation, matching the aggregate entity class's own
        established pattern: registration and the initial value read
        are guarded so a manager in an unexpected state can never
        prevent this entity -- or the rest of the sensor platform --
        from being added.
        """
        try:
            self._manager.add_listener(self._cb)
            self._apply(self._manager.engine.report)
        except Exception:  # noqa: BLE001 — never block platform setup
            _LOGGER.exception(
                "battery_health: failed to initialise entity %s; it will "
                "report unknown until the next successful update",
                self._attr_unique_id,
            )

    async def async_will_remove_from_hass(self) -> None:
        """Deregister callback."""
        self._manager.remove_listener(self._cb)

    @callback
    def _on_health_update(self, report: HealthReport) -> None:
        """Apply a new report and write state.

        Guarded, matching the aggregate entity class's own established
        pattern: a failure here must never leave the entity holding a
        value HA cannot serialise.
        """
        try:
            self._apply(report)
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "battery_health: failed to apply report to %s", self._attr_unique_id
            )
            return
        self.async_write_ha_state()

    def _apply(self, report: HealthReport) -> None:
        values = report.attributes.get(self._report_key)
        # Deliberately defensive against a list shorter than expected
        # (e.g. a topology change mid-restart) -- reports unknown for
        # this one pack rather than raising and losing every other
        # entity's own update in the same batch.
        if values is not None and self._pack_index < len(values):
            self._attr_native_value = values[self._pack_index]
        else:
            self._attr_native_value = None
