"""Text entities for Huawei Solar.

v2.3.0.0 (new platform). One editable "TOU period N" entity per
time-of-use slot (N = 1..14) on a directly-connected Huawei LUNA2000
battery, in that battery device's Configuration section.

Found and confirmed with the user: the storage working mode could be
switched to "Time of use" from the UI, but the periods that mode acts
on were only visible as a read-only sensor (count + attributes) and
only writable through the ``set_tou_periods`` service. These entities
close that gap. They are deliberately NOT gated on the current working
mode: a schedule has to exist before switching to TOU mode is useful,
so it must be editable while the battery is still in Maximum Self
Consumption (the inverter keeps the periods; they only take effect in
TOU mode).

See tou_periods.py for the per-period text format and for why this is
fourteen short entities rather than one long one.

Write path (ICS/OT discipline, same primitives as every other write in
this integration):

1. Validate the input text locally. No lock, no bus traffic. Invalid
   input is rejected here with a specific, translated reason.
2. Take the shared per-device logical write lock (types.get_device_
   write_lock) -- the SAME lock services.py uses, so this entity and a
   concurrent ``set_tou_periods`` service call can never interleave.
3. Inside ONE ModbusGuard hold bounded by ONE WRITE_SEQUENCE_TIMEOUT:
   a. read the TOU register fresh from the device (the configuration
      coordinator polls it only every few minutes, so its copy may be
      stale -- editing that copy could silently undo a change made in
      the FusionSolar app since the last poll: a lost update),
   b. apply the single-slot edit to that fresh schedule,
   c. skip the write entirely if nothing would change,
   d. validate the complete resulting schedule (count, ordering,
      per-day overlap) -- rejected schedules are never written,
   e. write the whole register once. The library's own encode()
      re-validates independently before any bytes reach the bus.
   Holding the guard across a-e means no other bus traffic, and in
   particular no other writer, can land between the read and the write.
4. Invalidate the cached register, schedule the standard coalesced
   read-back verification, and request a coordinator refresh so every
   slot entity (slots shift when one is cleared) shows the new schedule.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from huawei_solar import (
    EMMADevice,
    SUN2000Device,
    register_names as rn,
    register_values as rv,
)
from huawei_solar.exceptions import HuaweiSolarException

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_ENABLE_PARAMETER_CONFIGURATION,
    DATA_DEVICE_DATAS,
    DOMAIN,
    WRITE_SEQUENCE_TIMEOUT,
)
from .tou_periods import (
    DEFAULT_VISIBLE_TOU_SLOTS,
    MAX_TOU_PERIODS,
    PERIOD_TEXT_MAX_LENGTH,
    TouPeriodError,
    apply_slot_edit,
    format_period,
    parse_period_text,
    validate_periods,
)
from .types import (
    HuaweiSolarConfigEntry,
    HuaweiSolarDeviceData,
    HuaweiSolarEntity,
    HuaweiSolarInverterData,
    get_device_write_lock,
)
from .update_coordinator import HuaweiSolarUpdateCoordinator

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceInfo

_LOGGER = logging.getLogger(__name__)

TOU_REGISTER = rn.STORAGE_HUAWEI_LUNA2000_TIME_OF_USE_CHARGING_AND_DISCHARGING_PERIODS


def tou_slot_entities_eligible(
    device_datas: list[HuaweiSolarDeviceData],
    ucs: HuaweiSolarDeviceData,
) -> bool:
    """Whether ``ucs`` should get TOU slot entities.

    Conservative on purpose -- every condition must hold:

    * the entry has no EMMA. When an EMMA is present it is the battery
      manager; direct inverter battery writes would conflict with it.
      Same rule services.py already applies to forcible charge.
    * the device is a SUN2000 inverter with its own configuration
      coordinator (i.e. parameter configuration is enabled for it),
    * it has a connected battery, and that battery is a Huawei LUNA2000.
      LG RESU uses a different register and format (price periods) and
      is intentionally out of scope; its service remains available.
    """
    if any(isinstance(dd.device, EMMADevice) for dd in device_datas):
        return False
    if not isinstance(ucs, HuaweiSolarInverterData):
        return False
    if not isinstance(ucs.device, SUN2000Device):
        return False
    if ucs.configuration_update_coordinator is None:
        return False
    if ucs.connected_energy_storage is None:
        return False
    return ucs.device.battery_type == rv.StorageProductModel.HUAWEI_LUNA2000


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HuaweiSolarConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Huawei Solar Text entities setup."""
    if not entry.data.get(CONF_ENABLE_PARAMETER_CONFIGURATION, False):
        _LOGGER.info("Skipping text setup, as parameter configuration is not enabled")
        return

    device_datas: list[HuaweiSolarDeviceData] = entry.runtime_data[DATA_DEVICE_DATAS]

    entities: list[HuaweiSolarTOUPeriodTextEntity] = []
    for ucs in device_datas:
        if not tou_slot_entities_eligible(device_datas, ucs):
            continue
        assert isinstance(ucs, HuaweiSolarInverterData)
        assert ucs.configuration_update_coordinator is not None
        assert ucs.connected_energy_storage is not None
        _LOGGER.debug(
            "Adding %d TOU period slot entities for %s",
            MAX_TOU_PERIODS, ucs.device.serial_number,
        )
        entities.extend(
            HuaweiSolarTOUPeriodTextEntity(
                ucs.configuration_update_coordinator,
                ucs.device,
                ucs.connected_energy_storage,
                slot,
            )
            for slot in range(1, MAX_TOU_PERIODS + 1)
        )

    if entities:
        async_add_entities(entities)


def _validation_error(err: TouPeriodError) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=err.translation_key,
        translation_placeholders=err.placeholders,
    )


class HuaweiSolarTOUPeriodTextEntity(
    CoordinatorEntity[HuaweiSolarUpdateCoordinator], HuaweiSolarEntity, TextEntity
):
    """One editable LUNA2000 time-of-use period slot.

    State is the period text (``00:00-06:00/1234567/+``), or an empty
    string when the slot is unused.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:battery-clock"
    _attr_mode = TextMode.TEXT
    _attr_native_min = 0
    _attr_native_max = PERIOD_TEXT_MAX_LENGTH
    _attr_translation_key = "tou_period"
    # No HA `pattern`: HA validates the *displayed* state against it too,
    # and a device-held value this integration would refuse as input
    # (e.g. 24:00 set from the FusionSolar app) must still be shown
    # rather than turn into a state-write error. Input is validated
    # strictly in async_set_value() instead.

    def __init__(
        self,
        coordinator: HuaweiSolarUpdateCoordinator,
        device: SUN2000Device,
        device_info: DeviceInfo,
        slot: int,
    ) -> None:
        """Create the entity for 1-based ``slot``."""
        if not 1 <= slot <= MAX_TOU_PERIODS:
            raise ValueError(f"TOU slot {slot} out of range 1..{MAX_TOU_PERIODS}")
        super().__init__(coordinator, {"register_names": [TOU_REGISTER]})
        self.coordinator = coordinator
        self.device = device
        self._slot = slot
        self._attr_device_info = device_info
        self._attr_unique_id = f"{device.serial_number}_{TOU_REGISTER}_slot_{slot}"
        self._attr_translation_placeholders = {"slot": str(slot)}
        self._attr_entity_registry_visible_default = slot <= DEFAULT_VISIBLE_TOU_SLOTS
        self._attr_native_value: str | None = None
        self._attr_available = False

    @property
    def slot(self) -> int:
        """1-based slot number."""
        return self._slot

    @property
    def available(self) -> bool:
        """Coordinator healthy AND the TOU register is actually present.

        HA's CoordinatorEntity.available only reflects the coordinator's
        last_update_success and ignores _attr_available, so it is
        combined explicitly here (same approach as sensor.py's optimizer
        entities). Otherwise a missing/BAD register would still show an
        editable, stale-looking slot.
        """
        return super().available and self._attr_available

    def _slot_text(self, periods: list[Any]) -> str | None:
        """Text for this slot from a decoded schedule, or None if unshowable."""
        if self._slot > len(periods):
            return ""
        try:
            text = format_period(periods[self._slot - 1])
        except (AttributeError, TypeError, ValueError):
            _LOGGER.warning(
                "%s: could not render TOU period in slot %d", self.entity_id, self._slot
            )
            return None
        if len(text) > PERIOD_TEXT_MAX_LENGTH:
            # Only reachable with nonsensical device values (hours > 99);
            # HA would refuse a state longer than native_max.
            _LOGGER.warning(
                "%s: TOU period in slot %d is not displayable (%r)",
                self.entity_id, self._slot, text,
            )
            return None
        return text

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        data = self.coordinator.data
        if data and TOU_REGISTER in data and isinstance(data[TOU_REGISTER].value, list):
            periods = data[TOU_REGISTER].value
            self._attr_available = True
            self._attr_native_value = self._slot_text(periods)
            self._attr_extra_state_attributes = {
                "slot": self._slot,
                "configured_periods": len(periods),
            } | self._quality_attrs(self.coordinator, TOU_REGISTER)
        else:
            self._attr_available = False
            self._attr_native_value = None
        self.async_write_ha_state()

    async def async_set_value(self, value: str) -> None:
        """Set (or, with an empty string, clear) this slot's period."""
        # 1. Local validation -- before any lock or bus traffic.
        try:
            new_period = parse_period_text(value)
        except TouPeriodError as err:
            raise _validation_error(err) from err

        serial = self.device.serial_number
        written: list[Any] | None = None

        # 2. Logical per-device write lock shared with services.py.
        async with get_device_write_lock(serial):
            try:
                # 3. One guard hold, one whole-sequence deadline, covering
                #    the fresh read AND the write.
                async with self._guarded_write_sequence(
                    self.coordinator.guard, label="tou_period_write"
                ) as write:
                    result = await self.device.get(TOU_REGISTER)
                    current = getattr(result, "value", None)
                    if not isinstance(current, list):
                        raise HomeAssistantError(
                            translation_domain=DOMAIN,
                            translation_key="tou_period_read_failed",
                            translation_placeholders={"device": str(serial)},
                        )

                    updated = apply_slot_edit(current, self._slot, new_period)
                    if updated == current:
                        _LOGGER.debug(
                            "%s: TOU slot %d unchanged; no write issued",
                            serial, self._slot,
                        )
                    else:
                        validate_periods(updated)
                        _LOGGER.info(
                            "%s: writing TOU schedule (%d -> %d periods) via slot %d",
                            serial, len(current), len(updated), self._slot,
                        )
                        if not await write(self.device, TOU_REGISTER, updated):
                            raise HomeAssistantError(
                                translation_domain=DOMAIN,
                                translation_key="tou_period_write_rejected",
                                translation_placeholders={"device": str(serial)},
                            )
                        written = updated
            except TouPeriodError as err:
                raise _validation_error(err) from err
            except TimeoutError as err:
                # Covers the sequence deadline and ModbusGuard admission
                # timeouts / queue shedding (both TimeoutError subclasses).
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="tou_period_timeout",
                    translation_placeholders={
                        "device": str(serial),
                        "seconds": f"{WRITE_SEQUENCE_TIMEOUT.total_seconds():.0f}",
                    },
                ) from err
            except HuaweiSolarException as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="tou_period_device_error",
                    translation_placeholders={
                        "device": str(serial),
                        "error": type(err).__name__,
                    },
                ) from err

            if written is not None:
                # 4. Same post-write bookkeeping as number/select/switch.
                self.coordinator.invalidate_cache(TOU_REGISTER)
                self.coordinator.schedule_verify_write(TOU_REGISTER, written)
                self._attr_native_value = self._slot_text(written)
                self.async_write_ha_state()

        if written is not None:
            await self.coordinator.async_request_refresh()
