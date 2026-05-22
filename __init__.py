"""The Huawei Solar integration — v2.12.2."""

from datetime import timedelta
import logging

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
    COORDINATOR_STAGGER_SECONDS,
    DATA_DEVICE_DATAS,
    DOMAIN,
    ENERGY_STORAGE_UPDATE_INTERVAL,
    INVERTER_UPDATE_INTERVAL,
    NIGHT_POLL_INTERVAL,
    OPTIMIZER_UPDATE_INTERVAL,
    POWER_METER_UPDATE_INTERVAL,
)
from .modbus_guard import ModbusGuard
from .modbus_telemetry import ModbusTelemetry
from .night_mode import NightModeDetector
from .register_cache import LiveRegisterBus, StaticRegisterCache
from .services import async_setup_services
from .types import (
    HuaweiSolarConfigEntry,
    HuaweiSolarDeviceData,
    HuaweiSolarInverterData,
)
from .update_coordinator import (
    HuaweiSolarUpdateCoordinator,
    create_optimizer_update_coordinator,
)

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

        primary_device_data = await _setup_device_data(hass, entry, primary_device)
        device_datas: list[HuaweiSolarDeviceData] = [primary_device_data]

        for extra_unit_id in entry.data[CONF_SLAVE_IDS][1:]:
            sub_device = await create_sub_device_instance(primary_device, extra_unit_id)
            sub_device_data = await _setup_device_data(hass, entry, sub_device)
            device_datas.append(sub_device_data)

        entry.runtime_data = {DATA_DEVICE_DATAS: device_datas}

    except ConnectionInterruptedException as err:
        if primary_device:
            await primary_device.stop()
        host = entry.data.get(CONF_HOST) or entry.data.get(CONF_PORT)
        raise ConfigEntryNotReady(
            f"Connection to {host} was interrupted — another device may be connected. "
            "The inverter only supports one Modbus connection at a time."
        ) from err
    except ConnectionException as err:
        if primary_device:
            await primary_device.stop()
        host = entry.data.get(CONF_HOST) or entry.data.get(CONF_PORT)
        raise ConfigEntryNotReady(
            f"Cannot connect to {host}. Verify the address and network reachability."
        ) from err
    except TimeoutError as err:
        if primary_device:
            await primary_device.stop()
        raise ConfigEntryNotReady(
            "The inverter is not responding. It may be starting up or temporarily busy."
        ) from err
    except HuaweiSolarException as err:
        if primary_device:
            await primary_device.stop()
        raise ConfigEntryNotReady(f"Failed to communicate with inverter: {err}") from err
    except Exception:
        if primary_device:
            await primary_device.stop()
        raise

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await async_setup_services(hass, entry)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HuaweiSolarConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        device_datas: list[HuaweiSolarDeviceData] = entry.runtime_data[DATA_DEVICE_DATAS]

        await device_datas[0].device.client.disconnect()

        for device_data in device_datas:
            serial = device_data.device.serial_number

            # Stop telemetry push timers
            telemetry = ModbusTelemetry.get(serial)
            if telemetry:
                telemetry.stop()

            # Unregister all night-mode callbacks
            detector = NightModeDetector.get_or_create(serial)
            for attr in (
                "update_coordinator",
                "configuration_update_coordinator",
                "power_meter_update_coordinator",
                "energy_storage_update_coordinator",
                "optimizer_update_coordinator",
            ):
                coord = getattr(device_data, attr, None)
                if coord is not None:
                    if hasattr(coord, "_on_mode_change"):
                        detector.unregister_callback(coord._on_mode_change)
                    # B3: stop independent keepalive timer if running
                    if hasattr(coord, "_stop_keepalive_timer"):
                        coord._stop_keepalive_timer()

        # Clear all per-inverter singletons
        ModbusGuard.clear_registry()
        ModbusTelemetry.clear_registry()
        NightModeDetector.clear_registry()
        StaticRegisterCache.clear_registry()
        LiveRegisterBus.clear_registry()

    return unload_ok


# ── Device data factory helpers ───────────────────────────────────────────────

def _battery_manufacturer(spm: rv.StorageProductModel) -> str | None:
    return {
        rv.StorageProductModel.HUAWEI_LUNA2000: "Huawei",
        rv.StorageProductModel.LG_RESU: "LG Chem",
    }.get(spm)


def _battery_model(spm: rv.StorageProductModel) -> str | None:
    return {
        rv.StorageProductModel.HUAWEI_LUNA2000: "LUNA 2000",
        rv.StorageProductModel.LG_RESU: "RESU",
    }.get(spm)


def _make_coordinator(
    hass: HomeAssistant,
    device: HuaweiSolarDevice,
    name_suffix: str,
    update_interval: timedelta,
    telemetry: ModbusTelemetry,
    stagger_index: int,
    is_night: bool,
) -> HuaweiSolarUpdateCoordinator:
    """Create one HuaweiSolarUpdateCoordinator with stagger offset and night mode applied."""
    coord = HuaweiSolarUpdateCoordinator(
        hass,
        _LOGGER,
        device=device,
        name=f"{device.serial_number}_{name_suffix}",
        update_interval=update_interval,
        stagger_offset=timedelta(seconds=COORDINATOR_STAGGER_SECONDS * stagger_index),
    )
    coord.attach_telemetry(telemetry)
    if is_night:
        coord.update_interval = NIGHT_POLL_INTERVAL
        coord.cache.set_night_mode(True)
        coord._is_night = True
    return coord


async def _setup_inverter_device_data(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device: SUN2000Device,
) -> HuaweiSolarInverterData:
    device_registry = dr.async_get(hass)

    inverter_device_info = DeviceInfo(
        identifiers={(DOMAIN, device.serial_number)},
        translation_key="inverter",
        manufacturer="Huawei",
        model=device.model_name,
        serial_number=device.serial_number,
        sw_version=device.software_version,
    )
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, device.serial_number)},
        manufacturer="Huawei",
        name=device.model_name,
        model=device.model_name,
        sw_version=device.software_version,
    )

    # ── Night mode singleton — restore from storage ───────────────────────────
    night_detector = NightModeDetector.get_or_create(device.serial_number)
    night_detector.attach_hass(hass)
    await night_detector.async_restore()
    is_night = night_detector.is_night

    # ── Shared telemetry singleton ────────────────────────────────────────────
    telemetry = ModbusTelemetry.get_or_create(hass, device.serial_number, inverter_device_info)

    # ── Coordinator 0: main inverter (no stagger — fires immediately) ─────────
    update_coordinator = _make_coordinator(
        hass, device, "data_update_coordinator",
        INVERTER_UPDATE_INTERVAL, telemetry, 0, is_night,
    )

    # ── Coordinator 1: power meter ────────────────────────────────────────────
    power_meter_device_info = None
    power_meter_update_coordinator = None
    if device.power_meter_type is not None:
        power_meter_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{device.serial_number}/power_meter")},
            translation_key="power_meter",
            via_device=(DOMAIN, device.serial_number),
        )
        power_meter_update_coordinator = _make_coordinator(
            hass, device, "power_meter_data_update_coordinator",
            POWER_METER_UPDATE_INTERVAL, telemetry, 1, is_night,
        )

    # ── Coordinator 2: energy storage ─────────────────────────────────────────
    battery_device_info = None
    energy_storage_update_coordinator = None
    battery_1_device_info = None
    battery_2_device_info = None

    if device.battery_type != rv.StorageProductModel.NONE:
        battery_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{device.serial_number}/connected_energy_storage")},
            translation_key="connected_energy_storage",
            model="Batteries",
            manufacturer=inverter_device_info.get("manufacturer"),
            via_device=(DOMAIN, device.serial_number),
        )
        energy_storage_update_coordinator = _make_coordinator(
            hass, device, "battery_data_update_coordinator",
            ENERGY_STORAGE_UPDATE_INTERVAL, telemetry, 2, is_night,
        )

    if device.battery_1_type != rv.StorageProductModel.NONE:
        battery_1_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{device.serial_number}/battery_1")},
            translation_key="battery_1",
            manufacturer=_battery_manufacturer(device.battery_1_type),
            model=_battery_model(device.battery_1_type),
            via_device=(DOMAIN, device.serial_number),
        )
    if device.battery_2_type != rv.StorageProductModel.NONE:
        battery_2_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{device.serial_number}/battery_2")},
            translation_key="battery_2",
            manufacturer=_battery_manufacturer(device.battery_2_type),
            model=_battery_model(device.battery_2_type),
            via_device=(DOMAIN, device.serial_number),
        )

    # ── Optimizer coordinator ─────────────────────────────────────────────────
    optimizers_device_infos: dict = {}
    optimizer_update_coordinator = None

    if device.has_optimizers and not isinstance(device.primary_device, SmartLoggerDevice):
        try:
            optimizer_system_infos = await device.get_optimizer_system_information_data()
            optimizers_device_infos = {
                oid: DeviceInfo(
                    identifiers={(DOMAIN, opt.sn)},
                    name=opt.sn,
                    manufacturer="Huawei",
                    model=opt.model,
                    sw_version=opt.software_version,
                    via_device=(DOMAIN, device.serial_number),
                )
                for oid, opt in optimizer_system_infos.items()
            }
            optimizer_update_coordinator = await create_optimizer_update_coordinator(
                hass, device, optimizers_device_infos, OPTIMIZER_UPDATE_INTERVAL,
            )
            optimizer_update_coordinator.attach_telemetry(telemetry)
        except PermissionDeniedError as exc:
            _LOGGER.info("Optimizer entities skipped — insufficient permissions.", exc_info=exc)
        except Exception as exc:
            _LOGGER.exception("Optimizer setup failed.", exc_info=exc)

    # ── Coordinator 3: configuration ──────────────────────────────────────────
    configuration_update_coordinator = None
    if entry.data.get(CONF_ENABLE_PARAMETER_CONFIGURATION, False):
        configuration_update_coordinator = _make_coordinator(
            hass, device, "config_data_update_coordinator",
            CONFIGURATION_UPDATE_INTERVAL, telemetry, 3, is_night,
        )

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
) -> HuaweiSolarDeviceData:
    if isinstance(device, SUN2000Device):
        return await _setup_inverter_device_data(hass, entry, device)

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
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, device.serial_number)},
        manufacturer="Huawei",
        name=device.model_name,
        model=device.model_name,
        sw_version=sw_version,
    )

    update_coordinator = HuaweiSolarUpdateCoordinator(
        hass, _LOGGER, device=device,
        name=f"{device.serial_number}_data_update_coordinator",
        update_interval=INVERTER_UPDATE_INTERVAL,
    )
    configuration_update_coordinator = None
    if entry.data.get(CONF_ENABLE_PARAMETER_CONFIGURATION, False):
        configuration_update_coordinator = HuaweiSolarUpdateCoordinator(
            hass, _LOGGER, device=device,
            name=f"{device.serial_number}_config_data_update_coordinator",
            update_interval=CONFIGURATION_UPDATE_INTERVAL,
            stagger_offset=timedelta(seconds=COORDINATOR_STAGGER_SECONDS),
        )

    return HuaweiSolarDeviceData(
        device=device,
        device_info=device_info,
        update_coordinator=update_coordinator,
        configuration_update_coordinator=configuration_update_coordinator,
    )
