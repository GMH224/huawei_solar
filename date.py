"""Date entities for Huawei Solar.

v2.0.12 (Battery Phase 5B UI restructuring, this release): a single new
entity type -- one writable install-date entity per battery pack, attached
to that pack's own physical storage-unit device ("Battery 1"/"Battery 2"),
matching the same per-pack device-attachment convention
battery_health_entities.py's own per-pack sensors use (see that module's
own "Per-pack entities" section comment for the full reasoning).

Found and confirmed directly with the user: setting a pack's own install
date was only reachable via the set_pack_install_date service (Developer
Tools -> Actions, or an automation) -- there was no entity a user could
simply click and set, unlike this integration's own established pattern
for other writable settings (see number.py's own HuaweiSolarNumberEntity).
This platform closes that gap using HA's native DateEntity, the idiomatic
choice for a date-picker-style setting -- this integration did not
previously have a date.py platform at all.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

from homeassistant.components.date import DateEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .battery_health import HealthReport
from .battery_health_entities import SLOT_LABEL_RE
from .battery_health_manager import BatteryHealthManager
from .const import DATA_DEVICE_DATAS
from .types import HuaweiSolarConfigEntry, HuaweiSolarDeviceData

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceInfo

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HuaweiSolarConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Huawei Solar Date entities setup."""
    device_datas: list[HuaweiSolarDeviceData] = entry.runtime_data[DATA_DEVICE_DATAS]

    # Fault isolation (matching sensor.py's own established pattern for
    # the battery-health entity clusters): additive entities must never
    # abort the whole platform.
    pack_date_entities: list[HuaweiSolarPackInstallDateEntity] = []
    try:
        for ucs in device_datas:
            bh_manager = BatteryHealthManager.get(ucs.device.serial_number)
            if bh_manager is None:
                continue
            pack_date_entities.extend(
                create_battery_health_pack_date_entities(
                    bh_manager, ucs.battery_1, ucs.battery_2,
                )
            )
    except Exception:  # noqa: BLE001
        _LOGGER.exception(
            "Failed to build battery health pack install-date entities; "
            "continuing without them. All other entities are unaffected"
        )
        pack_date_entities = []

    if pack_date_entities:
        async_add_entities(pack_date_entities)


def create_battery_health_pack_date_entities(
    manager: BatteryHealthManager,
    battery_1_device_info: "DeviceInfo | None",
    battery_2_device_info: "DeviceInfo | None",
) -> list["HuaweiSolarPackInstallDateEntity"]:
    """One writable install-date entity per pack, attached to that
    pack's own physical storage-unit device -- mirrors battery_health_
    entities.create_battery_health_pack_entities()'s own slot-label
    parsing and device-mapping exactly, deliberately kept in sync with
    it rather than duplicated with different logic.
    """
    slot_labels = manager.engine.pack_capacity.slot_labels
    entities: list[HuaweiSolarPackInstallDateEntity] = []
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
        entities.append(
            HuaweiSolarPackInstallDateEntity(
                manager, device_info, i, slot_label, pack_number,
            )
        )
    return entities


class HuaweiSolarPackInstallDateEntity(DateEntity):
    """Writable install-date entity for one specific battery pack.

    Shows whichever date is CURRENTLY in effect for this pack --
    including a fallback-derived date (the unit's own install date, or
    the automatic first-detected timestamp), not just an explicitly-set
    one -- see effective_pack_install_ts()'s own docstring (battery_
    health.py) for the full three-tier fallback. This is deliberate:
    showing the fallback value lets a user SEE what's currently being
    used and correct it if it's wrong (most commonly, right after
    replacing a pack), rather than showing unknown until they've
    already set something.

    Writes go through BatteryHealthManager.set_pack_install_date() --
    the same shared method the set_pack_install_date service uses, so
    the entity and the service can never write through two different,
    potentially-diverging code paths.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        manager: BatteryHealthManager,
        device_info: "DeviceInfo",
        pack_index: int,
        slot_label: str,
        pack_number: str,
    ) -> None:
        self._manager = manager
        self._pack_index = pack_index
        self._slot_label = slot_label
        self._attr_name = f"Pack {pack_number} install date"
        self._attr_icon = "mdi:calendar-import"
        self._attr_device_info = device_info
        self._attr_unique_id = (
            f"{manager.serial_number}_battery_health_pack_{slot_label}_install_date"
        )
        self._attr_native_value: date | None = None
        self._attr_available = True
        self._cb = self._on_health_update

    async def async_added_to_hass(self) -> None:
        """Register with the manager and populate from the last report.

        Fault isolation, matching every other battery-health entity
        class's own established pattern.
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

        Guarded, matching every other battery-health entity class's own
        established pattern: a failure here must never leave the
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
        ages_days = report.attributes.get("pack_age_days")
        if ages_days is None or self._pack_index >= len(ages_days):
            self._attr_native_value = None
            return
        age_days = ages_days[self._pack_index]
        if age_days is None:
            self._attr_native_value = None
            return
        # v2.0.12: age_days is derived FROM the effective install
        # timestamp (now - install_ts), so reconstructing the date from
        # "now minus age" reproduces the same date effective_pack_
        # install_ts() itself resolved -- avoids a second call into
        # PackCapacityTracker internals from this entity, reusing the
        # value the report already computed and rounded once.
        install_dt = datetime.now(timezone.utc).date()
        self._attr_native_value = install_dt - timedelta(days=age_days)

    async def async_set_value(self, value: date) -> None:
        """Set this pack's own explicit install date.

        Resolves the pack's own CURRENT serial number at call time
        (not a stored one) -- if the pack has since been replaced, the
        date should apply to whichever serial is actually installed
        now, matching set_pack_install_date's own service semantics.
        """
        pack_capacity = self._manager.engine.pack_capacity
        if self._pack_index >= len(pack_capacity._last_serial):
            _LOGGER.warning(
                "battery_health: cannot set install date for %s -- pack "
                "index no longer valid (topology may have changed)",
                self._attr_unique_id,
            )
            return
        serial = pack_capacity._last_serial[self._pack_index]
        if serial is None:
            _LOGGER.warning(
                "battery_health: cannot set install date for %s -- no "
                "serial number observed yet for this pack",
                self._attr_unique_id,
            )
            return
        install_ts = datetime(
            value.year, value.month, value.day, tzinfo=timezone.utc
        ).timestamp()
        self._manager.set_pack_install_date(serial, install_ts)
