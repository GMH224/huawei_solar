"""Button entities for Huawei Solar."""

import logging

from huawei_solar import SUN2000Device, register_names as rn, register_values as rv

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .battery_health_manager import BatteryHealthManager
from .const import DATA_DEVICE_DATAS, elevated_permissions_enabled
# v2.0.7 FIX (ICS-12, ICS quality audit -- confirmed): this button's
# stop-forcible-charge press and services.py's own stop_forcible_charge()
# service are two independent entry points for the same physical
# command. Both already held ModbusGuard continuously for their own
# whole write sequence (preventing literal mid-sequence interleaving
# between them), but had no shared LOGICAL lock -- two complete write
# sequences, one from each path, could still race back-to-back in
# unpredictable order if triggered concurrently (e.g. a user presses
# this button at the same moment an automation calls the service).
# get_device_write_lock() is the SAME per-serial asyncio.Lock the
# service path uses, imported from .types (not .services) specifically
# so this entity-platform module doesn't pick up services.py's own
# heavier dependency chain (voluptuous schemas, ServiceCall handling)
# for something this narrow -- see .types' own get_device_write_lock()
# docstring for the full reasoning on why the shared registry lives
# there.
from .types import (
    HuaweiSolarConfigEntry,
    HuaweiSolarDeviceData,
    HuaweiSolarEntity,
    HuaweiSolarInverterData,
    get_device_write_lock as _get_device_write_lock,
)
from .update_coordinator import HuaweiSolarUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


class _NullContext:
    """v2.0.0a (F05): no-op async context manager -- the defensive fallback
    when a guard reference genuinely isn't available (see async_press()'s
    own comment). Reproduces today's existing unguarded behaviour exactly,
    rather than crashing on a None coordinator reference."""

    async def __aenter__(self):
        return None

    async def __aexit__(self, *args):
        return False


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HuaweiSolarConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Huawei Solar Button entities Setup."""
    device_datas: list[HuaweiSolarDeviceData] = entry.runtime_data[DATA_DEVICE_DATAS]

    # Battery health baseline-reset buttons write NO inverter registers, so
    # they are registered regardless of the parameter-configuration setting.
    # Fault isolation (v1.1.7): see sensor.py — additive entities must never
    # abort the platform.
    health_buttons: list[ButtonEntity] = []
    try:
        for ucs in device_datas:
            bh_manager = BatteryHealthManager.get(ucs.device.serial_number)
            if bh_manager:
                health_buttons.append(
                    ResetEfficiencyBaselineButtonEntity(bh_manager)
                )
                health_buttons.append(
                    RecalibrateBalanceBaselineButtonEntity(bh_manager)
                )
                health_buttons.append(
                    ReanchorCapacityReferenceButtonEntity(bh_manager)
                )
    except Exception:  # noqa: BLE001
        _LOGGER.exception(
            "Failed to build battery health buttons; continuing without them"
        )
        health_buttons = []
    if health_buttons:
        async_add_entities(health_buttons)

    if not elevated_permissions_enabled(entry):
        return

    entities_to_add: list[ButtonEntity] = []
    for ucs in device_datas:
        if not isinstance(ucs, HuaweiSolarInverterData):
            continue
        if not ucs.connected_energy_storage:
            continue

        entities_to_add.append(
            StopForcibleChargeButtonEntity(
                ucs.device,
                ucs.connected_energy_storage,
                ucs.configuration_update_coordinator,
            )
        )

    async_add_entities(entities_to_add)


class StopForcibleChargeButtonEntity(HuaweiSolarEntity, ButtonEntity):
    """Button to stop a running forcible charge or discharge."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "stop_forcible_charge"
    _attr_icon = "mdi:battery-off"

    def __init__(
        self,
        device: SUN2000Device,
        device_info: DeviceInfo,
        configuration_update_coordinator: HuaweiSolarUpdateCoordinator | None,
    ) -> None:
        """Initialize the button entity."""
        self.device = device
        self._attr_device_info = device_info
        self._attr_unique_id = f"{device.serial_number}_stop_forcible_charge"
        self._configuration_update_coordinator = configuration_update_coordinator

    async def async_press(self) -> None:
        """Stop the forcible charge or discharge."""
        # v2.0.7 FIX (ICS-12, ICS quality audit -- confirmed): the whole
        # method body -- writes AND the cache-invalidation/refresh below
        # -- is now serialised against services.py's own
        # stop_forcible_charge() service via the SAME per-serial lock,
        # matching that service's own lock scope exactly (see its own
        # `async with _get_device_write_lock(...)` wrapping both the
        # write sequence and its coordinator refresh). Without this, two
        # complete "stop forcible charge" sequences -- one from each
        # entry point -- could race back-to-back with no shared
        # ordering, and whichever finished last would simply overwrite
        # the other's fully-applied result.
        async with _get_device_write_lock(self.device.serial_number):
            # v2.0.0b (MOD-06, external ICS audit -- confirmed): these four
            # writes already held ONE continuous guard acquisition (a single
            # logical "stop forcible charge" operation, avoiding another
            # coordinator's poll interleaving partway through the sequence --
            # v2.0.0a/F05's own fix), but placed no bound on how long that
            # exclusive hold could last. Now uses _guarded_write_sequence()
            # (types.py), which adds WRITE_SEQUENCE_TIMEOUT as a single
            # whole-sequence deadline on top of the same guard-holding
            # guarantee, rather than four independent unbounded writes.
            # Falls back to unguarded, untimed (today's existing defensive
            # behaviour, not a new risk) only when this entity has no
            # coordinator reference at all -- this entity is not a
            # CoordinatorEntity; it holds an explicitly Optional coordinator
            # reference, unlike switch/select/number.
            guard = (
                self._configuration_update_coordinator.guard
                if self._configuration_update_coordinator is not None
                else None
            )
            if guard is not None:
                async with self._guarded_write_sequence(guard, label="button_write") as write:
                    await write(
                        self.device,
                        rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_WRITE,
                        rv.StorageForcibleChargeDischarge.STOP,
                    )
                    await write(self.device, rn.STORAGE_FORCIBLE_DISCHARGE_POWER, 0)
                    await write(
                        self.device,
                        rn.STORAGE_FORCED_CHARGING_AND_DISCHARGING_PERIOD,
                        0,
                    )
                    await write(
                        self.device,
                        rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_SETTING_MODE,
                        rv.StorageForcibleChargeDischargeTargetMode.TIME,
                    )
            else:
                async with _NullContext():
                    await self.device.set(
                        rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_WRITE,
                        rv.StorageForcibleChargeDischarge.STOP,
                    )
                    await self.device.set(rn.STORAGE_FORCIBLE_DISCHARGE_POWER, 0)
                    await self.device.set(
                        rn.STORAGE_FORCED_CHARGING_AND_DISCHARGING_PERIOD,
                        0,
                    )
                    await self.device.set(
                        rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_SETTING_MODE,
                        rv.StorageForcibleChargeDischargeTargetMode.TIME,
                    )

            if self._configuration_update_coordinator:
                # v1.3.15 FIX (Defect Q, part 2): none of the four writes above
                # were invalidating their cached registers, so the
                # async_request_refresh() below could be served pre-write
                # cached values for any register whose TTL hadn't naturally
                # expired yet -- sensors reflecting this state could continue
                # showing the OLD (still-forcibly-charging/discharging) values
                # for as long as that register's cache TTL lasted, looking
                # exactly like the stop command silently failed.
                for name in (
                    rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_WRITE,
                    rn.STORAGE_FORCIBLE_DISCHARGE_POWER,
                    rn.STORAGE_FORCED_CHARGING_AND_DISCHARGING_PERIOD,
                    rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_SETTING_MODE,
                ):
                    self._configuration_update_coordinator.invalidate_cache(name)
                await self._configuration_update_coordinator.async_request_refresh()


class ResetEfficiencyBaselineButtonEntity(HuaweiSolarEntity, ButtonEntity):
    """Button to re-capture the battery health efficiency baseline.

    Local-only action: clears the stored round-trip-efficiency baseline so it
    is re-captured from the next qualifying full-charge windows.  Writes no
    inverter/BMS registers.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "Reset battery health efficiency baseline"
    _attr_icon = "mdi:backup-restore"

    def __init__(self, manager: BatteryHealthManager) -> None:
        """Initialize the button entity."""
        self._manager = manager
        self._attr_device_info = manager.device_info
        self._attr_unique_id = (
            f"{manager.serial_number}_battery_health_reset_efficiency_baseline"
        )

    async def async_press(self) -> None:
        """Reset the efficiency baseline."""
        await self._manager.async_reset_efficiency_baseline()


class RecalibrateBalanceBaselineButtonEntity(HuaweiSolarEntity, ButtonEntity):
    """Re-anchor pack-balance scoring to the current resting spread.

    Balance is scored as deviation from a learned baseline, so a fixed sensor
    or rack-position offset cancels out.  Press this after hardware changes
    (pack replacement, sensor swap, firmware update that shifts calibration).

    This appends a new baseline *epoch*; it does not overwrite history, and
    the raw dV/dT attributes are never re-zeroed — so the long-term record
    stays reconstructible.  Note that re-anchoring AFTER real degradation has
    occurred will hide that degradation in the score; the raw series and the
    epoch list are the audit trail.  Writes no inverter registers.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "Recalibrate battery pack balance baseline"
    _attr_icon = "mdi:scale-balance"

    def __init__(self, manager: BatteryHealthManager) -> None:
        """Initialize the button entity."""
        self._manager = manager
        self._attr_device_info = manager.device_info
        self._attr_unique_id = (
            f"{manager.serial_number}_battery_health_recalibrate_balance_baseline"
        )

    async def async_press(self) -> None:
        """Start a new pack-balance baseline epoch."""
        await self._manager.async_reset_balance_baseline()


class ReanchorCapacityReferenceButtonEntity(HuaweiSolarEntity, ButtonEntity):
    """Re-anchor SOH capacity to the currently measured capacity.

    Disabled by default: this redefines what 100% health means.  It is the
    right action after a battery or module replacement, and wrong as routine
    maintenance — pressing it after genuine fade would silently reset the
    baseline to the degraded value.  Refuses when too few segments exist to
    anchor on.  Writes no inverter registers.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False
    _attr_name = "Re-anchor battery capacity reference"
    _attr_icon = "mdi:target-variant"

    def __init__(self, manager: BatteryHealthManager) -> None:
        """Initialize the button entity."""
        self._manager = manager
        self._attr_device_info = manager.device_info
        self._attr_unique_id = (
            f"{manager.serial_number}_battery_health_reanchor_capacity_reference"
        )

    async def async_press(self) -> None:
        """Re-anchor the capacity reference to the measured value."""
        await self._manager.async_reanchor_capacity_reference()
