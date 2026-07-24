"""HA sensor entities for the Battery Health Index subsystem.

Follows the ModbusTelemetry entity pattern: push-based entities subscribed to
a per-serial manager singleton, no coordinator subclassing required.  Wired
into the sensor platform via ``create_battery_health_entities`` in
``sensor.async_setup_entry``.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import callback

from .battery_health import HealthReport
from .battery_health_manager import BatteryHealthManager

_LOGGER = logging.getLogger(__name__)


def _round1(v: float | None) -> float | None:
    return None if v is None else round(v, 1)


# Attribute keys whose native value is a string, not a number (v1.1.6 bug fix:
# these must NEVER receive a numeric hint like suggested_display_precision —
# HA's sensor base class treats that hint as a promise of numeric state and
# raises ValueError on any non-numeric value, which silently killed entity
# setup and every subsequent update. See CLAUDE.md changelog v1.1.7.)
_STRING_VALUED_KEYS: frozenset[str] = frozenset({"confidence"})

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
            "entity_category": EntityCategory.DIAGNOSTIC,
            "device_class": SensorDeviceClass.ENUM,
            "options": ["low", "normal", "stale"],
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
    # NOTE (v1.1.7 bug fix): suggested_display_precision must NOT be a class
    # attribute here. HA's sensor base class treats its mere presence as a
    # promise that native_value is numeric and raises ValueError on any
    # string state (e.g. "low"/"normal"/"stale" for `confidence`) — this
    # silently prevented the confidence entity from ever being added and
    # crashed every subsequent update. See CLAUDE.md changelog v1.1.7.
    # It is set per-instance in __init__, only for numeric-valued sensors.

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
        """Register with the manager and populate from the last report."""
        self._manager.add_listener(self._cb)
        self._apply(self._manager.engine.report)

    async def async_will_remove_from_hass(self) -> None:
        """Deregister callback."""
        self._manager.remove_listener(self._cb)

    @callback
    def _on_health_update(self, report: HealthReport) -> None:
        self._apply(report)
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
                "segment_count": report.attributes.get("segment_count"),
                "golden_segment_count": report.attributes.get("golden_segment_count"),
            }
