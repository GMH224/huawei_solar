"""Switch entities for Huawei Solar."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import logging
import time
from typing import Any, TypeVar

from huawei_solar import HuaweiSolarDevice, register_names as rn, register_values as rv

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .adaptive_modbus import AdaptiveModbusController
from .battery_health_manager import BatteryHealthManager
from .bus_diagnostics import BusDiagnostics
from .const import CONF_ENABLE_PARAMETER_CONFIGURATION, DATA_DEVICE_DATAS
from .types import (
    HuaweiSolarConfigEntry,
    HuaweiSolarDeviceData,
    HuaweiSolarEntity,
    HuaweiSolarEntityContext,
    HuaweiSolarEntityDescription,
    HuaweiSolarInverterData,
)
from .update_coordinator import HuaweiSolarUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


T = TypeVar("T")


@dataclass(frozen=True)
class HuaweiSolarSwitchEntityDescription[T](
    HuaweiSolarEntityDescription, SwitchEntityDescription
):
    """Huawei Solar Switch Entity Description."""

    is_available_key: rn.RegisterName | None = None
    check_is_available_func: Callable[[Any], bool] | None = None

    def __post_init__(self) -> None:
        """Defaults the translation_key to the switch key."""

        # We use this special setter to be able to set/update the translation_key
        # in this frozen dataclass.
        # cfr. https://docs.python.org/3/library/dataclasses.html#frozen-instances
        object.__setattr__(
            self,
            "translation_key",
            self.translation_key or self.key.replace("#", "_").lower(),
        )

    @property
    def context(self) -> HuaweiSolarEntityContext:
        """Context used by DataUpdateCoordinator."""
        registers = [self.register_name]
        if self.is_available_key:
            registers.append(self.is_available_key)

        return {"register_names": registers}


ENERGY_STORAGE_WITH_CAPACITY_CONTROL_SWITCH_DESCRIPTIONS: tuple[
    HuaweiSolarSwitchEntityDescription, ...
] = (
    HuaweiSolarSwitchEntityDescription(
        key=rn.STORAGE_CHARGE_FROM_GRID_FUNCTION,
        icon="mdi:battery-charging-50",
        entity_category=EntityCategory.CONFIG,
        is_available_key=rn.STORAGE_CAPACITY_CONTROL_MODE,
        check_is_available_func=(
            lambda ccm: ccm != rv.StorageCapacityControlMode.ACTIVE_CAPACITY_CONTROL
        ),
    ),
)

ENERGY_STORAGE_WITHOUT_CAPACITY_CONTROL_SWITCH_DESCRIPTIONS: tuple[
    HuaweiSolarSwitchEntityDescription, ...
] = (
    HuaweiSolarSwitchEntityDescription(
        key=rn.STORAGE_CHARGE_FROM_GRID_FUNCTION,
        icon="mdi:battery-charging-50",
        entity_category=EntityCategory.CONFIG,
    ),
)


INVERTER_SWITCH_DESCRIPTIONS: tuple[HuaweiSolarSwitchEntityDescription, ...] = (
    HuaweiSolarSwitchEntityDescription(
        key=rn.MPPT_MULTIMODAL_SCANNING,
        icon="mdi:magnify-scan",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HuaweiSolarConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Huawei Solar Switch Entities Setup."""
    device_data: list[HuaweiSolarDeviceData] = entry.runtime_data[DATA_DEVICE_DATAS]

    # The battery-health learning switch writes NO inverter registers, so it
    # is registered regardless of the parameter-configuration setting (that
    # gate exists to guard register-writing entities).  Fault-isolated: a
    # failure here must never abort the switch platform (v1.1.7 contract).
    try:
        learning_switches: list[SwitchEntity] = []
        seen_buses: set[str] = set()
        for ucs in device_data:
            bh_manager = BatteryHealthManager.get(ucs.device.serial_number)
            if bh_manager:
                learning_switches.append(
                    AdaptiveLearningSwitchEntity(bh_manager)
                )
            # One diagnostics switch per BUS, not per inverter: the capture is
            # a property of the shared physical connection.
            coordinator = getattr(ucs, "update_coordinator", None)
            guard = getattr(coordinator, "guard", None)
            if guard is not None and guard.endpoint not in seen_buses:
                seen_buses.add(guard.endpoint)
                diagnostics = BusDiagnostics.get_or_create(hass, guard.endpoint)
                guard.diagnostics = diagnostics
                learning_switches.append(
                    ModbusDiagnosticsSwitchEntity(diagnostics, ucs)
                )
        if learning_switches:
            async_add_entities(learning_switches)
    except Exception:  # noqa: BLE001
        _LOGGER.exception(
            "Failed to build adaptive learning switch; continuing "
            "without it. All other switches are unaffected"
        )

    if not entry.data.get(CONF_ENABLE_PARAMETER_CONFIGURATION, False):
        _LOGGER.info("Skipping switch setup, as parameter configuration is not enabled")
        return

    entities_to_add: list[SwitchEntity] = []
    for ucs in device_data:
        if not ucs.configuration_update_coordinator:
            continue

        slave_entities: list[
            HuaweiSolarSwitchEntity | HuaweiSolarOnOffSwitchEntity
        ] = []

        if isinstance(ucs, HuaweiSolarInverterData):
            # This entity dependens on DEVICE_STATUS which is already read by the inverter_update_coordinator
            slave_entities.append(
                HuaweiSolarOnOffSwitchEntity(
                    ucs.update_coordinator, ucs.device, ucs.device_info
                )
            )

            slave_entities.extend(
                [
                    HuaweiSolarSwitchEntity(
                        ucs.update_coordinator,
                        ucs.device,
                        entity_description,
                        ucs.device_info,
                    )
                    for entity_description in INVERTER_SWITCH_DESCRIPTIONS
                ]
            )

            if ucs.connected_energy_storage:
                if ucs.device.supports_capacity_control:
                    slave_entities.extend(
                        HuaweiSolarSwitchEntity(
                            ucs.configuration_update_coordinator,
                            ucs.device,
                            entity_description,
                            ucs.connected_energy_storage,
                        )
                        for entity_description in ENERGY_STORAGE_WITH_CAPACITY_CONTROL_SWITCH_DESCRIPTIONS
                    )
                else:
                    slave_entities.extend(
                        HuaweiSolarSwitchEntity(
                            ucs.configuration_update_coordinator,
                            ucs.device,
                            entity_description,
                            ucs.connected_energy_storage,
                        )
                        for entity_description in ENERGY_STORAGE_WITHOUT_CAPACITY_CONTROL_SWITCH_DESCRIPTIONS
                    )

        entities_to_add.extend(slave_entities)

    async_add_entities(entities_to_add)


DEVICE_STATUS_OFF_RANGE_START = 0x3000
DEVICE_STATUS_OFF_RANGE_END = 0x3FFF


class HuaweiSolarSwitchEntity(
    CoordinatorEntity[HuaweiSolarUpdateCoordinator], HuaweiSolarEntity, SwitchEntity
):
    """Huawei Solar Switch Entity."""

    entity_description: HuaweiSolarSwitchEntityDescription

    def __init__(
        self,
        coordinator: HuaweiSolarUpdateCoordinator,
        device: HuaweiSolarDevice,
        description: HuaweiSolarSwitchEntityDescription,
        device_info: DeviceInfo,
    ) -> None:
        """Huawei Solar Switch Entity constructor.

        Do not use directly. Use `.create` instead!
        """
        super().__init__(coordinator, description.context)
        self.coordinator = coordinator

        self.device = device
        self.entity_description = description

        self._attr_device_info = device_info
        self._attr_unique_id = f"{device.serial_number}_{description.key}"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if (
            self.coordinator.data
            and self.entity_description.key in self.coordinator.data
        ):
            self._attr_is_on = self.coordinator.data[
                self.entity_description.register_name
            ].value

            if self.entity_description.check_is_available_func:
                assert self.entity_description.is_available_key
                is_available_register = self.coordinator.data.get(
                    self.entity_description.is_available_key
                )
                self._attr_available = self.entity_description.check_is_available_func(
                    is_available_register.value if is_available_register else None
                )
            else:
                self._attr_available = True
        else:
            self._attr_is_on = None
            self._attr_available = False

        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the setting on."""
        if await self.device.set(self.entity_description.register_name, True):
            self._attr_is_on = True
            self.coordinator.invalidate_cache(self.entity_description.register_name)

        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the setting off."""
        if await self.device.set(self.entity_description.register_name, False):
            self._attr_is_on = False
            self.coordinator.invalidate_cache(self.entity_description.register_name)

        await self.coordinator.async_request_refresh()

    @property
    def available(self) -> bool:
        """Is the entity available.

        Override available property (from CoordinatorEntity) to
        take into account the custom check_is_available_func result.
        """
        available = super().available

        if self.entity_description.check_is_available_func and available:
            return self._attr_available

        return available


class HuaweiSolarOnOffSwitchEntity(
    CoordinatorEntity[HuaweiSolarUpdateCoordinator], HuaweiSolarEntity, SwitchEntity
):
    """Huawei Solar Switch Entity."""

    POLL_FREQUENCY_SECONDS = 15
    MAX_STATUS_CHANGE_TIME_SECONDS = 3000  # Maximum status change time is 5 minutes

    def __init__(
        self,
        # not the HuaweiSolarConfigurationUpdateCoordinator as
        # this entity depends on the 'Device Status' register
        coordinator: HuaweiSolarUpdateCoordinator,
        device: HuaweiSolarDevice,
        device_info: DeviceInfo,
    ) -> None:
        """Huawei Solar Switch Entity constructor.

        Do not use directly. Use `.create` instead!
        """
        super().__init__(coordinator, {"register_names": [rn.DEVICE_STATUS]})
        self.coordinator = coordinator

        self.device = device
        self.entity_description = SwitchEntityDescription(
            key=rn.STARTUP,
            icon="mdi:power-standby",
            entity_category=EntityCategory.CONFIG,
        )

        self._attr_device_info = device_info
        self._attr_unique_id = f"{device.serial_number}_{self.entity_description.key}"

        self._change_lock = asyncio.Lock()

    @staticmethod
    def _is_off(device_status: str) -> bool:
        return device_status.startswith("Shutdown")

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self._change_lock.locked():
            return  # Don't do status updates if async_turn_on or async_turn_off is running

        if self.coordinator.data and rn.DEVICE_STATUS in self.coordinator.data:
            device_status = self.coordinator.data[rn.DEVICE_STATUS].value

            self._attr_is_on = not self._is_off(device_status)
            self._attr_available = True
        else:
            self._attr_available = False

        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the setting on."""
        async with self._change_lock:
            await self.device.set(rn.STARTUP, 0)

            # Turning on can take up to 5 minutes... We'll poll every 15 seconds
            for _ in range(
                self.MAX_STATUS_CHANGE_TIME_SECONDS // self.POLL_FREQUENCY_SECONDS
            ):
                await asyncio.sleep(self.POLL_FREQUENCY_SECONDS)
                device_status = (await self.device.client.get(rn.DEVICE_STATUS)).value
                if not self._is_off(device_status):
                    self._attr_is_on = True
                    break

        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the setting off."""
        async with self._change_lock:
            await self.device.set(rn.SHUTDOWN, 0)

            # Turning on can take up to 5 minutes... We'll poll every 15 seconds
            for _ in range(
                self.MAX_STATUS_CHANGE_TIME_SECONDS // self.POLL_FREQUENCY_SECONDS
            ):
                await asyncio.sleep(self.POLL_FREQUENCY_SECONDS)
                device_status = (await self.device.client.get(rn.DEVICE_STATUS)).value
                if self._is_off(device_status):
                    self._attr_is_on = False
                    break

        await self.coordinator.async_request_refresh()


class AdaptiveLearningSwitchEntity(SwitchEntity):
    """Maintenance inhibit for ALL adaptive learning on this inverter.

    Governs two independent learners (v1.2.2 - one control, one concept):

      * the battery-health engine (segments, capacity/balance/efficiency
        baselines, charge-ceiling epochs), and
      * the adaptive Modbus controller (circadian poll interval, gap, timeout).

    Turn OFF before planned work.  A Huawei firmware update takes about an
    hour, and the vendor does not document which registers stay meaningful
    during the cycle.  Turn back ON once the system is stable.

    Why this matters most for the Modbus learner: an hour of unreachable
    inverter is ~120 consecutive failed requests spread over four 15-minute
    circadian slots.  On a mature slot that lifts the failure rate from ~3% to
    ~12%, which maps to a poll interval near 137 s instead of 20-30 s.  Daily
    decay does NOT undo it - decay scales failures and sample count equally, so
    it lowers confidence but leaves the ratio intact.  Only new successful
    observations dilute it, and those accrue 4-5x more slowly precisely because
    polling has slowed.  One maintenance window can therefore cost weeks of
    degraded polling.

    While off, everything keeps RUNNING and every sensor keeps updating - only
    learning is frozen.  A day without learning costs nothing.

    Unplanned disturbances cannot be prepared for, so both subsystems also
    self-suspend for a settling period after Home Assistant starts.  This
    switch is the planned-work counterpart.

    Writes no inverter registers.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "Adaptive learning"
    _attr_icon = "mdi:school-outline"

    def __init__(self, manager: BatteryHealthManager) -> None:
        """Initialize the learning switch."""
        self._manager = manager
        self._attr_device_info = manager.device_info
        # unique_id retained from the battery-health-only switch so existing
        # entity registry entries and any automations survive the rename.
        self._attr_unique_id = f"{manager.serial_number}_battery_health_learning"

    @property
    def is_on(self) -> bool:
        """Return True when learning is enabled."""
        return self._manager.engine.learning_enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the state of both learners."""
        engine = self._manager.engine
        attrs: dict[str, Any] = {
            "battery_health_learning_active": engine.learning_active(time.time()),
            "battery_health_settling_events": engine.settling_events,
            "settling_period_s": engine.cfg.settling_period_s,
        }
        controller = AdaptiveModbusController.get(self._manager.serial_number)
        if controller is not None:
            attrs.update({
                "modbus_learning_active": controller.learning_active(),
                "modbus_suppressed_observations": controller.suppressed_observations,
                "modbus_settling_events": controller.settling_events,
            })
        return attrs

    async def _apply(self, enabled: bool) -> None:
        await self._manager.async_set_learning_enabled(enabled)
        controller = AdaptiveModbusController.get(self._manager.serial_number)
        if controller is not None:
            controller.set_learning_enabled(enabled)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable learning (a settling period still applies)."""
        await self._apply(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Freeze all learning for planned maintenance."""
        await self._apply(False)
        self.async_write_ha_state()


class ModbusDiagnosticsSwitchEntity(SwitchEntity):
    """Per-request Modbus diagnostic capture for one physical bus.

    Default OFF, and deliberately NOT restored across restarts, so a capture
    can never be left silently running. Enable it for a bounded window when
    investigating, then turn it off.

    Records go to ``config/huawei_solar_diagnostics/bus_<tag>.jsonl`` — one JSON
    object per request, with **wait time and service time separated**. That
    split is the point: wait-dominated means requests queue behind one another
    (a scheduler is the fix); service-dominated means the device itself is slow
    (only reducing demand helps). No sensor available today distinguishes them.

    The bus identifier is a salted hash, not the host or serial, so a capture
    can be shared without exposing the installation.

    Writes no inverter registers, and never blocks the event loop.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False
    _attr_name = "Modbus diagnostic capture"
    _attr_icon = "mdi:file-search-outline"

    def __init__(self, diagnostics: BusDiagnostics, device_data: Any) -> None:
        """Initialize the diagnostics switch."""
        self._diagnostics = diagnostics
        self._attr_device_info = device_data.device_info
        self._attr_unique_id = (
            f"{device_data.device.serial_number}_modbus_diagnostic_capture"
        )

    @property
    def is_on(self) -> bool:
        """Return True while capture is running."""
        return self._diagnostics.enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose capture health, including dropped records."""
        return self._diagnostics.stats()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start capturing per-request records."""
        self._diagnostics.set_enabled(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop capturing and flush anything pending."""
        self._diagnostics.set_enabled(False)
        self.async_write_ha_state()
