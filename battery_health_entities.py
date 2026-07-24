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
