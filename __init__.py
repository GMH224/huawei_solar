"""The Huawei Solar integration."""

import logging
from datetime import timedelta

from huawei_solar import (
    ConnectionException,
    ConnectionInterruptedException,
    EMMADevice,
    HuaweiSolarException,
    InvalidCredentials,
    MeterDevice,
    SChargerDevice,
    SDongleDevice,
    SmartLoggerDevice,
    SUN2000Device,
    create_device_instance,
    create_rtu_client,
    create_sub_device_instance,
    create_tcp_client,
    register_values as rv,
)
from huawei_solar.device.base import HuaweiSolarDevice, HuaweiSolarDeviceWithLogin
from huawei_solar.modbus_pdu import PermissionDeniedError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo

from .const import (
    CONF_ENABLE_PARAMETER_CONFIGURATION,
    CONF_SLAVE_IDS,
    CONFIGURATION_UPDATE_INTERVAL,
    DATA_DEVICE_DATAS,
    DATA_SYNC_POWER_COORDINATOR,
    DOMAIN,
    ENERGY_STORAGE_UPDATE_INTERVAL,
    INVERTER_UPDATE_INTERVAL,
    OPTIMIZER_UPDATE_INTERVAL,
    POWER_METER_UPDATE_INTERVAL,
    SYNC_POWER_UPDATE_INTERVAL,
)
from .adaptive_modbus import AdaptiveModbusController
from .battery_health_manager import BatteryHealthManager
from .modbus_guard import ModbusGuard
from .modbus_keepalive import ModbusKeepAlive
from .modbus_telemetry import ModbusTelemetry
from .services import async_setup_services
from .synchronized_power_coordinator import SynchronizedPowerCoordinator
from .types import (
    HuaweiSolarConfigEntry,
    HuaweiSolarDeviceData,
    HuaweiSolarInverterData,
)
from .update_coordinator import (
    HuaweiSolarUpdateCoordinator,
    create_optimizer_update_coordinator,
)

# Stagger offsets applied to the first poll of each coordinator sharing the
# same ModbusGuard.  Without jitter all four coordinators (main, power_meter,
# energy_storage, configuration) fire simultaneously at t=0 and again at every
# shared interval boundary, pushing the guard queue depth to 4 and triggering
# load-shedding under normal conditions.  These fixed offsets spread them
# evenly across the 30 s poll window so the guard never sees more than one
# in-flight request at a time during steady-state operation.
#
# The configuration coordinator uses a 15-minute interval and is staggered by
# 10 s — small relative to its own cadence, large enough to clear the guard.
_COORDINATOR_START_DELAYS = {
    "main":          timedelta(seconds=0),
    "power_meter":   timedelta(seconds=7),
    "energy_storage": timedelta(seconds=14),
    "configuration": timedelta(seconds=10),
}

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: HuaweiSolarConfigEntry) -> bool:
    """Set up Huawei Solar from a config entry."""

    primary_device = None
    try:
        # Multiple inverters can be connected to each other via a daisy chain,
        # via an internal modbus-network (ie. not the same modbus network that we are
        # using to talk to the inverter).
        #
        # Each inverter receives it's own 'slave id' in that case.
        # The inverter that we use as 'gateway' will then forward the request to
        # the proper inverter.

        #               ┌─────────────┐
        #               │  EXTERNAL   │
        #               │ APPLICATION │
        #               └──────┬──────┘
        #                      │
        #                 ┌────┴────┐
        #                 │PRIMARY  │
        #                 │INVERTER │
        #                 └────┬────┘
        #       ┌──────────────┼───────────────┐
        #       │              │               │
        #  ┌────┴────┐     ┌───┴─────┐    ┌────┴────┐
        #  │ SLAVE X │     │ SLAVE Y │    │SLAVE ...│
        #  └─────────┘     └─────────┘    └─────────┘

        if entry.data[CONF_HOST] is None:
            client = create_rtu_client(
                port=entry.data[CONF_PORT], unit_id=entry.data[CONF_SLAVE_IDS][0]
            )
        else:
            client = create_tcp_client(
                host=entry.data[CONF_HOST],
                port=entry.data[CONF_PORT],
                unit_id=entry.data[CONF_SLAVE_IDS][0],
            )

        primary_device = await create_device_instance(client)

        # Derive the bus endpoint once from the config entry.
        # All inverters on the same physical RS485 bus share this endpoint
        # and will therefore share one ModbusGuard (bus-level serialisation).
        bus_endpoint = ModbusGuard.endpoint_for(dict(entry.data))
        _LOGGER.debug("Bus endpoint: %s", bus_endpoint)

        if entry.data.get(CONF_ENABLE_PARAMETER_CONFIGURATION):
            if (
                isinstance(primary_device, HuaweiSolarDeviceWithLogin)
                and entry.data.get(CONF_USERNAME)
                and entry.data.get(CONF_PASSWORD)
            ):
                try:
                    await primary_device.login(
                        entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD]
                    )
                except InvalidCredentials as err:
                    raise ConfigEntryAuthFailed from err

        primary_device_data = await _setup_device_data(
            hass,
            entry,
            primary_device,
            bus_endpoint=bus_endpoint,
        )

        device_datas: list[HuaweiSolarDeviceData] = [primary_device_data]

        for extra_unit_id in entry.data[CONF_SLAVE_IDS][1:]:
            sub_device = await create_sub_device_instance(primary_device, extra_unit_id)
            # sub_device shares the same physical RS485 bus as primary_device
            # — passing bus_endpoint gives it the same ModbusGuard instance.
            sub_device_data = await _setup_device_data(
                hass, entry, sub_device, bus_endpoint=bus_endpoint
            )

            device_datas.append(sub_device_data)

        # ── SynchronizedPowerCoordinator ──────────────────────────────────────
        # Build a coordinator that reads all instantaneous power registers in one
        # contiguous Modbus block, eliminating the timing spread that causes
        # power-flow card arithmetic errors when multiple coordinators fire at
        # different moments.
        #
        # Conditions for enabling:
        #  • At least one SUN2000 inverter is present (always true at this point)
        #  • Primary inverter has a meter OR battery — otherwise there is nothing
        #    interesting to synchronise beyond INV1's own PV reading, which the
        #    existing update_coordinator already handles.

        inverter_datas = [d for d in device_datas if isinstance(d, HuaweiSolarInverterData)]
        sync_coordinator: SynchronizedPowerCoordinator | None = None

        if inverter_datas:
            inv1_data = inverter_datas[0]
            inv2_data = inverter_datas[1] if len(inverter_datas) > 1 else None
            has_meter = inv1_data.power_meter is not None
            has_battery = inv1_data.connected_energy_storage is not None

            if has_meter or has_battery or inv2_data is not None:
                sync_coordinator = SynchronizedPowerCoordinator(
                    hass,
                    inv1_device=inv1_data.device,
                    inv2_device=inv2_data.device if inv2_data is not None else None,
                    has_meter=has_meter,
                    has_battery=has_battery,
                    update_interval=SYNC_POWER_UPDATE_INTERVAL,
                )
                # Attach INV1's telemetry so sync-coordinator reads are counted
                # in the same Modbus traffic dashboard as the other coordinators.
                telemetry = ModbusTelemetry.get(inv1_data.device.serial_number)
                if telemetry:
                    sync_coordinator.attach_telemetry(telemetry)

                await sync_coordinator.async_config_entry_first_refresh()
                _LOGGER.info(
                    "SynchronizedPowerCoordinator enabled: INV1=%s, INV2=%s, "
                    "meter=%s, battery=%s, interval=%ss",
                    inv1_data.device.serial_number,
                    inv2_data.device.serial_number if inv2_data else "none",
                    has_meter,
                    has_battery,
                    SYNC_POWER_UPDATE_INTERVAL.total_seconds(),
                )

        entry.runtime_data = {
            DATA_DEVICE_DATAS: device_datas,
            DATA_SYNC_POWER_COORDINATOR: sync_coordinator,
        }

        # ── Battery Health managers (v1.1.5) ─────────────────────────────────
        # One read-only health engine per inverter with a connected battery.
        # Persisted state is loaded inside async_initialize() BEFORE the
        # listener attaches, so segment detection never restarts from a false
        # "empty" state after a reboot (spec §8).
        for device_data in device_datas:
            if (
                isinstance(device_data, HuaweiSolarInverterData)
                and device_data.energy_storage_update_coordinator is not None
                and device_data.connected_energy_storage is not None
            ):
                bh_manager = BatteryHealthManager.create(
                    hass,
                    device_data.device.serial_number,
                    device_data.energy_storage_update_coordinator,
                    device_data.connected_energy_storage,
                    dict(entry.options),
                )
                await bh_manager.async_initialize()
    except ConnectionInterruptedException as err:
        if primary_device is not None:
            await primary_device.stop()
        host = entry.data.get(CONF_HOST) or entry.data.get(CONF_PORT)
        _LOGGER.warning(
            "Connection to the inverter at %s was interrupted during setup. "
            "The inverter only supports one Modbus connection at a time. "
            "Check whether another device is currently connected to the inverter",
            host,
        )
        raise ConfigEntryNotReady(
            f"Connection to the inverter at {host} was interrupted, probably by another device. "
            "The inverter only supports one Modbus connection at a time."
        ) from err
    except ConnectionException as err:
        if primary_device is not None:
            await primary_device.stop()
        host = entry.data.get(CONF_HOST) or entry.data.get(CONF_PORT)
        _LOGGER.warning(
            "Cannot connect to the inverter at %s. "
            "Verify the address and that the device is reachable on the network. "
            "If the inverter's IP address has changed, reconfigure the integration",
            host,
        )
        raise ConfigEntryNotReady(
            f"Cannot connect to the inverter at {host}. "
            "Verify the address and that the device is reachable. "
            "If the IP address has changed, reconfigure the integration."
        ) from err

    except TimeoutError as err:
        if primary_device is not None:
            await primary_device.stop()
        _LOGGER.warning(
            "The inverter is not responding to requests. "
            "The connection was established but no data was received. "
            "The device may be starting up, overloaded, or blocking Modbus requests"
        )
        raise ConfigEntryNotReady(
            "The inverter is not responding to requests. "
            "It may be starting up or temporarily busy."
        ) from err

    except HuaweiSolarException as err:
        if primary_device is not None:
            await primary_device.stop()
        _LOGGER.warning(
            "Failed to communicate with the inverter during setup: %s. ",
            err,
            exc_info=err,
        )
        raise ConfigEntryNotReady(
            f"Failed to communicate with the inverter: {err}"
        ) from err

    except Exception:
        # always try to stop the bridge, as it will keep retrying
        # in the background otherwise!
        if primary_device is not None:
            await primary_device.stop()
        raise

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await async_setup_services(hass, entry)

    # Reload on options change (battery health tunables — spec §10). Raw
    # persisted segment/sample logs stay valid; only aggregation changes.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: HuaweiSolarConfigEntry
) -> None:
    """Handle an options update by reloading the config entry."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: HuaweiSolarConfigEntry
) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        device_datas: list[HuaweiSolarDeviceData] = entry.runtime_data[DATA_DEVICE_DATAS]
        primary_device = device_datas[0].device
        await primary_device.client.disconnect()

        # Tear down ONLY this entry's singletons.  These registries are
        # process-global and may hold instances belonging to other config
        # entries that are still loaded (e.g. a second inverter added as a
        # separate entry).  clear_registry() would wipe those too — breaking
        # bus serialisation for the surviving entry and orphaning its
        # keep-alive tasks.  Remove per-serial / per-endpoint instead.
        for device_data in device_datas:
            serial = device_data.device.serial_number

            telemetry = ModbusTelemetry.get(serial)
            if telemetry:
                telemetry.stop()
            ModbusTelemetry.remove(serial)

            controller = AdaptiveModbusController.get(serial)
            if controller:
                controller.stop()
            AdaptiveModbusController.remove(serial)

            keepalive = ModbusKeepAlive.get(serial)
            if keepalive:
                keepalive.stop()
            ModbusKeepAlive.remove(serial)

            bh_manager = BatteryHealthManager.get(serial)
            if bh_manager:
                await bh_manager.async_unload()
            BatteryHealthManager.remove(serial)

        # The ModbusGuard is keyed on the connection endpoint shared by all
        # sub-devices of this entry; remove just that endpoint's guard.
        ModbusGuard.remove(ModbusGuard.endpoint_for(entry.data))

        # The SynchronizedPowerCoordinator has no background tasks of its own —
        # HA cancels its scheduled refresh when the config entry is unloaded.
        # We only need to drop the reference so it can be garbage-collected.
        entry.runtime_data.pop(DATA_SYNC_POWER_COORDINATOR, None)

    return unload_ok


def _battery_product_model_to_manufacturer(spm: rv.StorageProductModel) -> str | None:
    if spm == rv.StorageProductModel.HUAWEI_LUNA2000:
        return "Huawei"
    if spm == rv.StorageProductModel.LG_RESU:
        return "LG Chem"
    return None


def _battery_product_model_to_model(spm: rv.StorageProductModel) -> str | None:
    if spm == rv.StorageProductModel.HUAWEI_LUNA2000:
        return "LUNA 2000"
    if spm == rv.StorageProductModel.LG_RESU:
        return "RESU"
    return None


async def _setup_inverter_device_data(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device: SUN2000Device,
    connecting_inverter_device_id: tuple[str, str] | None,
    bus_endpoint: str = "",
) -> HuaweiSolarInverterData:
    device_registry = dr.async_get(hass)

    inverter_device_info = DeviceInfo(
        identifiers={(DOMAIN, device.serial_number)},
        translation_key="inverter",
        manufacturer="Huawei",
        model=device.model_name,
        serial_number=device.serial_number,
        sw_version=device.software_version,
        via_device=connecting_inverter_device_id,  # type: ignore[typeddict-item]
    )

    # Add inverter device to device registery
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, device.serial_number)},
        manufacturer="Huawei",
        name=device.model_name,
        model=device.model_name,
        sw_version=device.software_version,
    )

    update_coordinator = HuaweiSolarUpdateCoordinator(
        hass,
        _LOGGER,
        device=device,
        name=f"{device.serial_number}_data_update_coordinator",
        update_interval=INVERTER_UPDATE_INTERVAL,
        start_delay=_COORDINATOR_START_DELAYS["main"],
        bus_endpoint=bus_endpoint,
    )

    # Create telemetry singleton and attach to the main coordinator.
    # All sub-coordinators (power meter, battery, config) for this inverter
    # share the same singleton so all Modbus traffic is aggregated.
    telemetry = ModbusTelemetry.get_or_create(
        hass, device.serial_number, inverter_device_info
    )
    update_coordinator.attach_telemetry(telemetry)

    # Create the circadian adaptive learning controller and load persisted
    # statistics from HA storage.  All coordinators for this inverter share
    # one controller so every Modbus request contributes to the same model.
    adaptive = AdaptiveModbusController.get_or_create(
        hass, device.serial_number, inverter_device_info
    )
    await adaptive.async_load()
    update_coordinator.attach_adaptive(adaptive)

    # Create the keep-alive / connection health probe.
    # The callbacks wire directly into the main coordinator so that a
    # dead-connection detection immediately invalidates the cache and
    # resets failure counters on all coordinators for this inverter.
    keepalive = ModbusKeepAlive.get_or_create(
        serial_number=device.serial_number,
        device=device,
        guard=update_coordinator.guard,
        on_connection_lost=update_coordinator.on_connection_lost,
        on_connection_restored=update_coordinator.on_connection_restored,
    )
    await keepalive.start()

    # Add power meter device if a power meter is detected
    if device.power_meter_type is not None:
        power_meter_device_info = DeviceInfo(
            identifiers={
                (DOMAIN, f"{device.serial_number}/power_meter"),
            },
            translation_key="power_meter",
            via_device=(DOMAIN, device.serial_number),
        )
        power_meter_update_coordinator = HuaweiSolarUpdateCoordinator(
            hass,
            _LOGGER,
            device=device,
            name=f"{device.serial_number}_power_meter_data_update_coordinator",
            update_interval=POWER_METER_UPDATE_INTERVAL,
            start_delay=_COORDINATOR_START_DELAYS["power_meter"],
            bus_endpoint=bus_endpoint,
        )
        power_meter_update_coordinator.attach_telemetry(telemetry)
        power_meter_update_coordinator.attach_adaptive(adaptive)
    else:
        power_meter_device_info = None
        power_meter_update_coordinator = None

    # Add battery device if a battery is detected
    if device.battery_type != rv.StorageProductModel.NONE:
        battery_device_info = DeviceInfo(
            identifiers={
                (DOMAIN, f"{device.serial_number}/connected_energy_storage"),
            },
            translation_key="connected_energy_storage",
            model="Batteries",
            manufacturer=inverter_device_info.get("manufacturer"),
            via_device=(DOMAIN, device.serial_number),
        )

        energy_storage_update_coordinator = HuaweiSolarUpdateCoordinator(
            hass,
            _LOGGER,
            device=device,
            name=f"{device.serial_number}_battery_data_update_coordinator",
            update_interval=ENERGY_STORAGE_UPDATE_INTERVAL,
            start_delay=_COORDINATOR_START_DELAYS["energy_storage"],
            bus_endpoint=bus_endpoint,
        )
        energy_storage_update_coordinator.attach_telemetry(telemetry)
        energy_storage_update_coordinator.attach_adaptive(adaptive)
    else:
        battery_device_info = None
        energy_storage_update_coordinator = None

    if device.battery_1_type != rv.StorageProductModel.NONE:
        battery_1_device_info = DeviceInfo(
            identifiers={
                (DOMAIN, f"{device.serial_number}/battery_1"),
            },
            translation_key="battery_1",
            manufacturer=_battery_product_model_to_manufacturer(device.battery_1_type),
            model=_battery_product_model_to_model(device.battery_1_type),
            via_device=(DOMAIN, device.serial_number),
        )
    else:
        battery_1_device_info = None

    if device.battery_2_type != rv.StorageProductModel.NONE:
        battery_2_device_info = DeviceInfo(
            identifiers={
                (DOMAIN, f"{device.serial_number}/battery_2"),
            },
            translation_key="battery_2",
            manufacturer=_battery_product_model_to_manufacturer(device.battery_2_type),
            model=_battery_product_model_to_model(device.battery_2_type),
            via_device=(DOMAIN, device.serial_number),
        )
    else:
        battery_2_device_info = None

    optimizers_device_infos = {}
    optimizer_update_coordinator = None

    # Add optimizer devices if optimizers are detected
    if device.has_optimizers and (
        # Optimizers are not accessible when connected through a SmartLogger
        not isinstance(device.primary_device, SmartLoggerDevice)
    ):
        try:
            optimizer_system_infos = (
                await device.get_optimizer_system_information_data()
            )

            optimizers_device_infos = {
                optimizer_id: DeviceInfo(
                    identifiers={(DOMAIN, optimizer.sn)},
                    name=optimizer.sn,
                    manufacturer="Huawei",
                    model=optimizer.model,
                    sw_version=optimizer.software_version,
                    via_device=(DOMAIN, device.serial_number),
                )
                for optimizer_id, optimizer in optimizer_system_infos.items()
            }

            optimizer_update_coordinator = await create_optimizer_update_coordinator(
                hass,
                device,
                optimizers_device_infos,
                OPTIMIZER_UPDATE_INTERVAL,
                bus_endpoint=bus_endpoint,
            )
            optimizer_update_coordinator.attach_telemetry(telemetry)
            optimizer_update_coordinator.attach_adaptive(adaptive)
        except PermissionDeniedError as exception:
            _LOGGER.info(
                "Cannot create optimizer sensor entities as the integration has insufficient permissions. "
                "Consider enabling elevated permissions to get more optimizer data",
                exc_info=exception,
            )
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.exception(
                "Cannot create optimizer sensor entities due to an unexpected error",
                exc_info=exc,
            )

    if entry.data.get(CONF_ENABLE_PARAMETER_CONFIGURATION, False):
        configuration_update_coordinator = HuaweiSolarUpdateCoordinator(
            hass,
            _LOGGER,
            device=device,
            name=f"{device.serial_number}_config_data_update_coordinator",
            update_interval=CONFIGURATION_UPDATE_INTERVAL,
            start_delay=_COORDINATOR_START_DELAYS["configuration"],
            bus_endpoint=bus_endpoint,
        )
        configuration_update_coordinator.attach_telemetry(telemetry)
        configuration_update_coordinator.attach_adaptive(adaptive)
    else:
        configuration_update_coordinator = None

    return HuaweiSolarInverterData(
        device=device,
        device_info=inverter_device_info,
        update_coordinator=update_coordinator,
        power_meter=power_meter_device_info,
        power_meter_update_coordinator=power_meter_update_coordinator,
        connected_energy_storage=battery_device_info,
        energy_storage_update_coordinator=energy_storage_update_coordinator,
        optimizer_device_infos=optimizers_device_infos,
        optimizer_update_coordinator=optimizer_update_coordinator,
        battery_1=battery_1_device_info,
        battery_2=battery_2_device_info,
        configuration_update_coordinator=configuration_update_coordinator,
    )


DEVICE_CLASS_TO_TRANSLATION_KEY: dict[type[HuaweiSolarDevice], str] = {
    EMMADevice: "emma",
    MeterDevice: "power_meter",
    SChargerDevice: "charger",
    SDongleDevice: "sdongle",
    SmartLoggerDevice: "smartlogger",
}


async def _setup_device_data(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device: HuaweiSolarDevice,
    bus_endpoint: str = "",
) -> HuaweiSolarDeviceData:
    """Create the correct DeviceInfo-objects, which can be used to correctly assign to entities in this integration."""
    if isinstance(device, SUN2000Device):
        return await _setup_inverter_device_data(
            hass, entry, device, None, bus_endpoint=bus_endpoint
        )

    device_registry = dr.async_get(hass)

    sw_version = getattr(device, "software_version", None)

    device_info = DeviceInfo(
        identifiers={(DOMAIN, device.serial_number)},
        translation_key=DEVICE_CLASS_TO_TRANSLATION_KEY[type(device)],
        manufacturer="Huawei",
        model=device.model_name,
        serial_number=device.serial_number,
        sw_version=sw_version,
    )

    # Add device to device registery
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, device.serial_number)},
        manufacturer="Huawei",
        name=device.model_name,
        model=device.model_name,
        sw_version=sw_version,
    )

    update_coordinator = HuaweiSolarUpdateCoordinator(
        hass,
        _LOGGER,
        device=device,
        name=f"{device.serial_number}_data_update_coordinator",
        update_interval=INVERTER_UPDATE_INTERVAL,
    )

    if entry.data.get(CONF_ENABLE_PARAMETER_CONFIGURATION, False):
        configuration_update_coordinator = HuaweiSolarUpdateCoordinator(
            hass,
            _LOGGER,
            device=device,
            name=f"{device.serial_number}_config_data_update_coordinator",
            update_interval=CONFIGURATION_UPDATE_INTERVAL,
        )
    else:
        configuration_update_coordinator = None

    return HuaweiSolarDeviceData(
        device=device,
        device_info=device_info,
        update_coordinator=update_coordinator,
        configuration_update_coordinator=configuration_update_coordinator,
    )
