"""The Huawei Solar services."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Literal, TypedDict, TypeVar

from huawei_solar import (
    EMMADevice,
    HuaweiSolarDevice,
    RegisterName,
    SUN2000Device,
    register_names as rn,
    register_values as rv,
)
from huawei_solar.register_definitions.periods import (
    ChargeDischargePeriod,
    ChargeFlag,
    HUAWEI_LUNA2000_TimeOfUsePeriod,
    LG_RESU_TimeOfUsePeriod,
    PeakSettingPeriod,
)
import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
import homeassistant.helpers.config_validation as cv

from .battery_health_manager import BatteryHealthManager

from .const import (
    CONF_ENABLE_PARAMETER_CONFIGURATION,
    DATA_DEVICE_DATAS,
    DOMAIN,
    SERVICE_FORCIBLE_CHARGE,
    SERVICE_FORCIBLE_CHARGE_SOC,
    SERVICE_FORCIBLE_DISCHARGE,
    SERVICE_FORCIBLE_DISCHARGE_SOC,
    SERVICE_RESET_MAXIMUM_FEED_GRID_POWER,
    SERVICE_SET_CAPACITY_CONTROL_PERIODS,
    SERVICE_SET_DI_ACTIVE_POWER_SCHEDULING,
    SERVICE_SET_FIXED_CHARGE_PERIODS,
    SERVICE_SET_MAXIMUM_FEED_GRID_POWER,
    SERVICE_SET_MAXIMUM_FEED_GRID_POWER_PERCENT,
    SERVICE_SET_PACK_INSTALL_DATE,
    SERVICE_SET_TOU_PERIODS,
    SERVICE_SET_ZERO_POWER_GRID_CONNECTION,
    SERVICE_STOP_FORCIBLE_CHARGE,
    SERVICE_VALIDATION_READ_TIMEOUT,
    WRITE_SEQUENCE_TIMEOUT,
    WRITE_TIMEOUT,
)
from .types import (
    HuaweiSolarConfigEntry,
    HuaweiSolarDeviceData,
    HuaweiSolarInverterData,
    get_device_write_lock,
)

ALL_SERVICES = [
    SERVICE_FORCIBLE_CHARGE,
    SERVICE_FORCIBLE_CHARGE_SOC,
    SERVICE_FORCIBLE_DISCHARGE,
    SERVICE_FORCIBLE_DISCHARGE_SOC,
    SERVICE_RESET_MAXIMUM_FEED_GRID_POWER,
    SERVICE_SET_CAPACITY_CONTROL_PERIODS,
    SERVICE_SET_DI_ACTIVE_POWER_SCHEDULING,
    SERVICE_SET_FIXED_CHARGE_PERIODS,
    SERVICE_SET_MAXIMUM_FEED_GRID_POWER,
    SERVICE_SET_MAXIMUM_FEED_GRID_POWER_PERCENT,
    SERVICE_SET_PACK_INSTALL_DATE,
    SERVICE_SET_TOU_PERIODS,
    SERVICE_SET_ZERO_POWER_GRID_CONNECTION,
    SERVICE_STOP_FORCIBLE_CHARGE,
]

DATA_DEVICE_ID = "device_id"
DATA_POWER = "power"
DATA_POWER_PERCENTAGE = "power_percentage"
DATA_DURATION = "duration"
DATA_TARGET_SOC = "target_soc"
DATA_PERIODS = "periods"


_LOGGER = logging.getLogger(__name__)


class HuaweiSolarServiceException(Exception):
    """Exception while executing Huawei Solar Service Call."""


#############################################
# Device validation and retrieval functions #
#############################################

T = TypeVar("T", bound=HuaweiSolarDevice)


@callback
def async_get_entry_id_for_service_call(
    call: ServiceCall,
) -> tuple[dr.DeviceEntry, HuaweiSolarConfigEntry]:
    """Get the entry ID related to a service call (by device ID)."""
    device_registry = dr.async_get(call.hass)
    device_id = call.data[ATTR_DEVICE_ID]
    if (device_entry := device_registry.async_get(device_id)) is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_device_id",
            translation_placeholders={"device_id": device_id},
        )

    for entry_id in device_entry.config_entries:
        if (entry := call.hass.config_entries.async_get_entry(entry_id)) is None:
            continue
        if entry.domain == DOMAIN:
            if entry.state is not ConfigEntryState.LOADED:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="entry_not_loaded",
                    translation_placeholders={"entry": entry.title},
                )
            return (device_entry, entry)

    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="config_entry_not_found",
        translation_placeholders={"device_id": device_id},
    )


@callback
def _get_device_data(
    call: ServiceCall,
) -> HuaweiSolarDeviceData:
    """Return the HuaweiSolarDeviceData associated with the device_id in the service call."""
    device_entry, entry = async_get_entry_id_for_service_call(call)

    device_datas: list[HuaweiSolarDeviceData] = entry.runtime_data[DATA_DEVICE_DATAS]
    for dd in device_datas:
        assert "identifiers" in dd.device_info
        for identifier in dd.device_info["identifiers"]:
            for device_identifier in device_entry.identifiers:
                if identifier == device_identifier:
                    return dd

    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="ha_device_not_found",
        translation_placeholders={"device_id": device_entry.id},
    )


@callback
def _get_device_of_type_data[T](
    call: ServiceCall, device_type: type[T]
) -> HuaweiSolarDeviceData:
    dd = _get_device_data(call)
    if not isinstance(dd.device, device_type):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="wrong_device_type",
            translation_placeholders={
                "device_id": call.data[ATTR_DEVICE_ID],
                "expected_type": device_type.__name__,
                "actual_type": type(dd.device).__name__,
            },
        )
    return dd


@callback
def get_emma_device(call: ServiceCall) -> HuaweiSolarDeviceData:
    """Return the HuaweiEMMABridge associated with the emma device_id in the service call."""
    return _get_device_of_type_data(call, EMMADevice)


EMMA_DEVICE_SCHEMA = vol.Schema({DATA_DEVICE_ID: vol.All(cv.string, str)})


@callback
def _get_battery_device_data(call: ServiceCall) -> HuaweiSolarInverterData:
    """Return the HuaweiSolarDeviceData associated with the device_id in the service call."""
    device_entry, entry = async_get_entry_id_for_service_call(call)

    device_datas: list[HuaweiSolarDeviceData] = entry.runtime_data[DATA_DEVICE_DATAS]
    for dd in device_datas:
        if not isinstance(dd, HuaweiSolarInverterData):
            continue
        if not dd.connected_energy_storage:
            continue
        assert "identifiers" in dd.connected_energy_storage
        for identifier in dd.connected_energy_storage["identifiers"]:
            for device_identifier in device_entry.identifiers:
                if identifier == device_identifier:
                    return dd

    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="ha_device_not_found",
        translation_placeholders={"device_id": device_entry.id},
    )


@callback
def get_battery_device_data(call: ServiceCall) -> HuaweiSolarInverterData:
    """Return the HuaweiSolarInverterData associated with the battery device_id in the service call."""
    return _get_battery_device_data(call)


BATTERY_DEVICE_SCHEMA = vol.Schema({DATA_DEVICE_ID: vol.All(cv.string, str)})

# v2.0.12 (Battery Phase 5B, this release -- per-pack install dates):
# a genuinely separate identifier from DATA_DEVICE_ID above -- the
# battery DEVICE's own device_id resolves WHICH physical unit/inverter
# to look up (matches every other battery service's own schema), while
# this identifies WHICH of its (up to 3, or 6 across two units) packs
# the date applies to, by that pack's own serial number -- not a slot
# label, matching effective_pack_install_ts()'s own reasoning
# (battery_health.py) for why this is serial-keyed throughout.
DATA_PACK_SERIAL_NUMBER = "pack_serial_number"
DATA_INSTALL_DATE = "install_date"

SET_PACK_INSTALL_DATE_SCHEMA = BATTERY_DEVICE_SCHEMA.extend({
    vol.Required(DATA_PACK_SERIAL_NUMBER): cv.string,
    vol.Required(DATA_INSTALL_DATE): cv.string,
})


async def set_pack_install_date(service_call: ServiceCall) -> None:
    """Set the install date for one specific battery pack, identified by
    its own serial number (not a slot label -- a pack's own age is a
    property of the physical pack, not whichever wiring slot it
    currently occupies).

    v2.0.12 (Battery Phase 5B, this release): the primary intended use
    is recording a REPLACEMENT pack's own real install date, since the
    unit-level CONF_BH_INSTALL_DATE option is actively wrong for a pack
    installed later than the unit itself was -- see effective_pack_
    install_ts()'s own docstring (battery_health.py) for the full
    three-tier fallback this feeds into. Not restricted to only
    currently-installed packs' own serials -- setting a date for a
    serial this engine has never seen is accepted without error (it
    simply has no effect until/unless that serial is later observed),
    matching this integration's general preference for permissive,
    forward-compatible service inputs over rejecting a call that isn't
    actually harmful.
    """
    dd = get_battery_device_data(service_call)
    serial = service_call.data[DATA_PACK_SERIAL_NUMBER]
    install_date_str = service_call.data[DATA_INSTALL_DATE]

    try:
        install_ts = (
            datetime.fromisoformat(install_date_str)
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except (TypeError, ValueError) as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_pack_install_date",
            translation_placeholders={"install_date": install_date_str},
        ) from err

    bh_manager = BatteryHealthManager.get(dd.device.serial_number)
    if bh_manager is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="battery_health_not_enabled",
            translation_placeholders={"device_id": service_call.data[DATA_DEVICE_ID]},
        )

    # v2.0.12 (Battery Phase 5B UI restructuring, this release): now
    # goes through the shared BatteryHealthManager.set_pack_install_date()
    # write path -- see that method's own docstring. Keeps this service
    # and the new per-pack date entity (date.py) from being able to
    # drift out of sync by each reimplementing the same three steps
    # separately.
    bh_manager.set_pack_install_date(serial, install_ts)


@callback
def get_inverter_data(call: ServiceCall) -> HuaweiSolarInverterData:
    """Return the HuaweiSolarBridge associated with the inverter device_id in the service call."""
    dd = _get_device_of_type_data(call, SUN2000Device)
    assert isinstance(dd, HuaweiSolarInverterData)
    return dd


###################################################
# Service schemas and schema validation functions #
###################################################

INVERTER_DEVICE_SCHEMA = vol.Schema({DATA_DEVICE_ID: vol.All(cv.string, str)})


FORCIBLE_CHARGE_BASE_SCHEMA = BATTERY_DEVICE_SCHEMA.extend(
    {
        vol.Required(DATA_POWER): cv.positive_int,
    }
)

DURATION_SCHEMA = FORCIBLE_CHARGE_BASE_SCHEMA.extend(
    {vol.Required(DATA_DURATION): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440))}
)

SOC_SCHEMA = FORCIBLE_CHARGE_BASE_SCHEMA.extend(
    {
        vol.Required(DATA_TARGET_SOC): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=100)
        )
    }
)

MAXIMUM_FEED_GRID_POWER_SCHEMA = {
    vol.Required(DATA_POWER): vol.All(vol.Coerce(int), vol.Range(min=-1000)),
}


MAXIMUM_FEED_GRID_POWER_PERCENTAGE_SCHEMA = {
    vol.Required(DATA_POWER_PERCENTAGE): vol.All(
        vol.Coerce(int), vol.Range(min=0, max=100)
    ),
}


# Strict HH:MM sub-pattern: hours 00-23, minutes 00-59.  Rejects 24:00 and
# malformed values like 29:99 that the old "[0-2]\d:\d\d" allowed.
_TIME = r"(?:[01]\d|2[0-3]):[0-5]\d"

# Period quantifier stays {0,N}: an empty string is a valid "clear all periods"
# request.  The parsers below skip blank lines so empty input clears safely
# instead of raising.  The day field requires at least one day ([1-7]{1,7}).
HUAWEI_LUNA2000_TOU_PATTERN = rf"({_TIME}-{_TIME}/[1-7]{{1,7}}/[+-]\n?){{0,14}}"
LG_RESU_TOU_PATTERN = rf"({_TIME}-{_TIME}/\d+\.?\d*\n?){{0,14}}"

BATTERY_TOU_PERIODS_SCHEMA = BATTERY_DEVICE_SCHEMA.extend(
    {
        vol.Required(DATA_PERIODS): vol.All(
            cv.string,
            vol.Match(HUAWEI_LUNA2000_TOU_PATTERN + r"|" + LG_RESU_TOU_PATTERN),
        )
    }
)

EMMA_TOU_PERIODS_SCHEMA = EMMA_DEVICE_SCHEMA.extend(
    {
        vol.Required(DATA_PERIODS): vol.All(
            cv.string,
            vol.Match(HUAWEI_LUNA2000_TOU_PATTERN),
        )
    }
)

CAPACITY_CONTROL_PERIODS_PATTERN = (
    rf"({_TIME}-{_TIME}/[1-7]{{1,7}}/\d+W\n?){{0,14}}"
)

CAPACITY_CONTROL_PERIODS_SCHEMA = BATTERY_DEVICE_SCHEMA.extend(
    {
        vol.Required(DATA_PERIODS): vol.All(
            cv.string,
            vol.Match(CAPACITY_CONTROL_PERIODS_PATTERN),
        )
    }
)

FIXED_CHARGE_PERIODS_PATTERN = rf"({_TIME}-{_TIME}/\d+W\n?){{0,10}}"

FIXED_CHARGE_PERIODS_SCHEMA = BATTERY_DEVICE_SCHEMA.extend(
    {
        vol.Required(DATA_PERIODS): vol.All(
            cv.string,
            vol.Match(FIXED_CHARGE_PERIODS_PATTERN),
        )
    }
)


def _parse_days_effective(
    days_text: str,
) -> tuple[bool, bool, bool, bool, bool, bool, bool]:
    days = [False, False, False, False, False, False, False]
    for day in days_text:
        days[int(day) % 7] = True

    return tuple(days)  # type: ignore[return-value]


def _parse_time(value: str) -> int:
    hours, minutes = value.split(":")

    hours_i, minutes_i = int(hours), int(minutes)
    if not (0 <= hours_i <= 23 and 0 <= minutes_i <= 59):
        raise ValueError(f"Invalid time '{value}': must be between 00:00 and 23:59")
    return hours_i * 60 + minutes_i


async def _validate_power_value(
    power: Any, dd: HuaweiSolarDeviceData, max_value_key: rn.RegisterName
) -> int:
    # this already checked by voluptuous:
    assert isinstance(power, int)

    # v1.3.19 FIX (Defect V/Finding 8, independent ICS audit): this read
    # had no bound of its own, and runs BEFORE any write -- while the
    # per-device write lock (Defect R, v1.3.15) is already held for the
    # whole service call. An unbounded read here doesn't just risk hanging
    # this one call, it blocks every OTHER write action for the same
    # device too, for as long as it takes.
    #
    # v2.0.0a FIX (F07, external ICS audit -- confirmed): the bounded read
    # still bypassed ModbusGuard entirely, meaning it could physically
    # collide with a coordinator's own guarded polling exchange on the
    # same bus even though it was correctly time-bounded. Routed through
    # the same guard every write in this module now uses (see
    # _set_and_invalidate's own v2.0.0a note) -- the logical per-device
    # write lock above is unrelated and unaffected; this only adds
    # physical bus serialisation underneath it.
    try:
        async with dd.update_coordinator.guard.request(label="service_validation_read"):
            maximum_active_power = (
                await asyncio.wait_for(
                    dd.device.get(max_value_key),
                    timeout=SERVICE_VALIDATION_READ_TIMEOUT.total_seconds(),
                )
            ).value
    except TimeoutError as err:
        raise ValueError(
            f"Timed out reading the maximum allowed power ({max_value_key}) "
            f"after {SERVICE_VALIDATION_READ_TIMEOUT.total_seconds():.0f}s; "
            "the inverter may be busy. Try again shortly."
        ) from err

    if maximum_active_power is None:
        raise ValueError(
            f"Could not read the maximum allowed power ({max_value_key}); "
            "the inverter did not return a value. Try again shortly."
        )

    if not power <= maximum_active_power:
        raise ValueError(f"Power cannot be more than {maximum_active_power}W")

    return power


async def _set_and_invalidate(
    dd: HuaweiSolarDeviceData, name: rn.RegisterName, value: Any
) -> bool:
    """Write a register and immediately invalidate its cached entry.

    v1.3.15 FIX (Defect Q, part 3). Every write in this module used to call
    `dd.device.set(...)` directly and rely solely on the
    `dd.configuration_update_coordinator.async_refresh()` call at the end
    of each service function to pick up the new value. That refresh
    triggers a normal poll, which still consults the register cache's own
    staleness filter -- a register whose TTL had not yet naturally expired
    would be served its pre-write cached value, not the fresh one, despite
    the write having succeeded. This meant every one of this module's ~15
    service functions could leave a sensor showing stale (pre-write) data
    for as long as that register's cache TTL lasted, looking exactly like
    the service call silently failed.

    Centralised here, rather than fixed by adding an `invalidate_cache`
    call after every one of the ~39 `dd.device.set(...)` call sites in this
    file individually, both because it is less error-prone (nothing to
    remember at each new call site) and because every write in this module
    is read back through the same `configuration_update_coordinator`.

    v2.0.0a FIX (F05, external ICS audit -- confirmed): the write itself
    used to call `dd.device.set(...)` directly, bypassing ModbusGuard
    entirely -- meaning a service-triggered write could physically overlap
    a coordinator's own guarded polling exchange on the same bus, or land
    between two halves of an in-progress guarded read. Centralising this
    write path here (the same reasoning as the invalidation fix above)
    means routing it through the guard fixes all ~39 call sites in this
    file at once. `dd.update_coordinator` is always present (non-optional
    on HuaweiSolarDeviceData) and shares the same ModbusGuard instance as
    every other coordinator on this device's endpoint.

    v2.0.0b FIX (MOD-05, external ICS audit -- confirmed): being routed
    through the guard provided serialisation, not a deadline -- the
    underlying device.set() call still had no timeout of its own, so a
    stalled write held the guard indefinitely, starving every other
    coordinator on the endpoint. WRITE_TIMEOUT (const.py) added here,
    fixing the same gap at all ~39 call sites at once, the same way the
    guard-routing fix above did.
    """
    async with dd.update_coordinator.guard.request(label="service_write"):
        async with asyncio.timeout(WRITE_TIMEOUT.total_seconds()):
            result = await dd.device.set(name, value)
    if dd.configuration_update_coordinator is not None:
        dd.configuration_update_coordinator.invalidate_cache(name)
    return result


@contextlib.asynccontextmanager
async def _set_and_invalidate_sequence(dd: HuaweiSolarDeviceData):
    """Hold the guard continuously across a multi-register logical write
    command, bounded by ONE whole-sequence deadline.

    v2.0.0b FIX (MOD-19/MOD-20, external ICS audit -- confirmed): eight
    service functions in this module each perform several sequential
    writes via `_set_and_invalidate()`, which acquires and releases the
    guard PER CALL -- meaning another coordinator's poll could be
    admitted to the bus between any two steps of what is logically one
    atomic command (e.g. "start forcible charge": mode, power, and
    duration registers that only make sense applied together). The
    per-device `_get_device_write_lock()` (below) does not close this
    gap: it is a logical asyncio.Lock keyed by serial number, preventing
    two service calls from interleaving with EACH OTHER, but it never
    touches ModbusGuard, so it does nothing to stop a coordinator's poll
    from interleaving with a service write sequence.

    This is services.py's equivalent of types.py's
    `HuaweiSolarEntity._guarded_write_sequence()` -- module-level rather
    than a mixin method, since these are service-call functions, not
    entities. MOD-20 (two independent write paths for "stop forcible
    charge" -- this module's service and button.py's button -- using
    mutually unaware locks) is closed as a direct consequence: both now
    hold the SAME kind of bound, guard-serialised sequence, even though
    they remain two separate call sites (unifying them into one shared
    code path is a further, separable simplification, not required for
    either to be individually correct).

    Usage:
        async with _set_and_invalidate_sequence(dd) as write:
            await write(rn.SOME_REGISTER, value)
            await write(rn.OTHER_REGISTER, value)

    The yielded `write` callable performs the same guard-free
    device.set() + invalidate_cache() pairing `_set_and_invalidate()`
    does per-call -- do not call `_set_and_invalidate()` or
    `dd.device.set()` directly inside this block, or the sequence loses
    its single guard hold and bound.
    """
    async with dd.update_coordinator.guard.request(label="service_write_sequence"):
        async with asyncio.timeout(WRITE_SEQUENCE_TIMEOUT.total_seconds()):
            async def _write(name: rn.RegisterName, value: Any) -> bool:
                result = await dd.device.set(name, value)
                if dd.configuration_update_coordinator is not None:
                    dd.configuration_update_coordinator.invalidate_cache(name)
                return result
            yield _write


# v1.3.15 FIX (Defect R): per-device lock preventing two concurrent service
# calls from interleaving their multi-step write sequences.
#
# Every function below performs several sequential `dd.device.set(...)`
# calls representing ONE logical command (e.g. "start a forcible charge" is
# four separate register writes that only mean what they're supposed to
# mean together). Nothing previously serialised these sequences against
# EACH OTHER -- ModbusGuard serialises individual requests at the wire
# level, but two automations (or a user action racing an automation)
# calling, say, forcible_charge and stop_forcible_charge on the same device
# at nearly the same time could have their four-step sequences genuinely
# interleave. Both calls could complete "successfully" individually while
# leaving the inverter in whatever contradictory state the interleaved
# writes happened to produce -- a different and, in a sense, worse failure
# mode than a single sequence merely failing partway (which at least fails
# visibly).
#
# switch.py's HuaweiSolarOnOffSwitchEntity already has exactly this
# protection via its own per-entity self._change_lock; these are
# module-level functions with no natural "self", so a small registry keyed
# by device serial number (the same pattern already used throughout this
# codebase for ModbusGuard, AdaptiveModbusController, etc.) provides the
# same guarantee: any two service-level operations on the same device are
# always serialised, never interleaved.
#
# v2.0.7 FIX (ICS-12, ICS quality audit -- confirmed): the actual
# registry now lives in .types (get_device_write_lock()), shared with
# button.py's own StopForcibleCharge press -- see that function's own
# docstring for the full reasoning. This wrapper is kept, under the same
# name and with the same call signature, purely so every existing call
# site below (and this project's own static tests asserting this helper
# exists and is used) needs no changes at all.
def _get_device_write_lock(serial_number: str) -> asyncio.Lock:
    return get_device_write_lock(serial_number)


# v2.0.3 (ICS-10, external ICS audit -- confirmed): tracks which config
# entries currently have parameter-configuration services registered, so
# async_unload_services() (below) can tell whether the entry being
# unloaded was the LAST one needing them, and only then actually
# unregister from Home Assistant -- see async_setup_services()'s own
# comment on this same fix for why this is reference-counted removal,
# not also a guard against re-registration.
_entries_with_services: set[str] = set()

# Every service name this integration can register -- used by
# async_unload_services() to unregister all of them once the last
# relevant entry unloads. hass.services.async_remove() is safe to call
# for a service that was never registered by this process (a no-op, not
# an exception), so this list is intentionally unconditional rather than
# tracking exactly which subset a given setup actually registered.
_ALL_SERVICE_NAMES: tuple[str, ...] = (
    SERVICE_FORCIBLE_CHARGE,
    SERVICE_FORCIBLE_DISCHARGE,
    SERVICE_FORCIBLE_CHARGE_SOC,
    SERVICE_FORCIBLE_DISCHARGE_SOC,
    SERVICE_STOP_FORCIBLE_CHARGE,
    SERVICE_RESET_MAXIMUM_FEED_GRID_POWER,
    SERVICE_SET_DI_ACTIVE_POWER_SCHEDULING,
    SERVICE_SET_ZERO_POWER_GRID_CONNECTION,
    SERVICE_SET_MAXIMUM_FEED_GRID_POWER,
    SERVICE_SET_MAXIMUM_FEED_GRID_POWER_PERCENT,
    SERVICE_SET_TOU_PERIODS,
    SERVICE_SET_CAPACITY_CONTROL_PERIODS,
    SERVICE_SET_FIXED_CHARGE_PERIODS,
)


async def async_unload_services(
    hass: HomeAssistant,
    entry: HuaweiSolarConfigEntry,
) -> None:
    """Unregister this integration's services once the LAST config entry
    that needed them has unloaded.

    v2.0.3 FIX (ICS-10, external ICS audit -- confirmed): services used
    to be registered on every async_setup_entry() call and never
    unregistered anywhere -- not on entry unload, not when the last
    huawei_solar entry was removed entirely. They stayed registered in
    Home Assistant's own service registry indefinitely, callable (and
    appearing in the UI/service-call autocomplete) even with zero
    huawei_solar entries left loaded, and even after the integration
    itself was uninstalled from a running instance.
    """
    _entries_with_services.discard(entry.entry_id)
    if _entries_with_services:
        return  # other entries still need these services registered
    for service_name in _ALL_SERVICE_NAMES:
        hass.services.async_remove(DOMAIN, service_name)


def _parse_huawei_luna2000_periods(text: str) -> list[HUAWEI_LUNA2000_TimeOfUsePeriod]:
    result = []
    for line in text.split("\n"):
        if not line.strip():
            continue  # tolerate blank lines / empty input (clears all periods)
        start_end_time_str, days_effective_str, charge_flag_str = line.split("/")
        start_time_str, end_time_str = start_end_time_str.split("-")

        result.append(
            HUAWEI_LUNA2000_TimeOfUsePeriod(
                _parse_time(start_time_str),
                _parse_time(end_time_str),
                ChargeFlag.CHARGE if charge_flag_str == "+" else ChargeFlag.DISCHARGE,
                _parse_days_effective(days_effective_str),
            )
        )

    return result


def _parse_lg_resu_periods(text: str) -> list[LG_RESU_TimeOfUsePeriod]:
    result = []
    for line in text.split("\n"):
        if not line.strip():
            continue  # tolerate blank lines / empty input (clears all periods)
        start_end_time_str, energy_price = line.split("/")
        start_time_str, end_time_str = start_end_time_str.split("-")

        result.append(
            LG_RESU_TimeOfUsePeriod(
                _parse_time(start_time_str),
                _parse_time(end_time_str),
                float(energy_price),
            )
        )

    return result


###################################
# Service handler implementations #
###################################


async def forcible_charge(service_call: ServiceCall) -> None:
    """Start a forcible charge on the battery."""
    dd = get_battery_device_data(service_call)
    async with _get_device_write_lock(dd.device.serial_number):
        power = await _validate_power_value(
            service_call.data[DATA_POWER], dd, rn.STORAGE_MAXIMUM_CHARGE_POWER
        )

        duration = service_call.data[DATA_DURATION]
        if duration > 1440:
            raise ValueError("Maximum duration is 1440 minutes")

        # v2.0.0b (MOD-19, external ICS audit -- confirmed): these four
        # writes are one logical "start forcible charge" command -- held
        # under one continuous guard acquisition + one whole-sequence
        # deadline now, not four separately-guarded, unbounded writes
        # another coordinator's poll could interleave between.
        async with _set_and_invalidate_sequence(dd) as write:
            await write(rn.STORAGE_FORCIBLE_CHARGE_POWER, power)
            await write(rn.STORAGE_FORCED_CHARGING_AND_DISCHARGING_PERIOD, duration)
            await write(
                rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_SETTING_MODE,
                rv.StorageForcibleChargeDischargeTargetMode.TIME,
            )
            await write(
                rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_WRITE,
                rv.StorageForcibleChargeDischarge.CHARGE,
            )

        assert dd.configuration_update_coordinator
        await dd.configuration_update_coordinator.async_refresh()


async def forcible_discharge(service_call: ServiceCall) -> None:
    """Start a forcible charge on the battery."""
    dd = get_battery_device_data(service_call)
    async with _get_device_write_lock(dd.device.serial_number):
        power = await _validate_power_value(
            service_call.data[DATA_POWER], dd, rn.STORAGE_MAXIMUM_DISCHARGE_POWER
        )

        duration = service_call.data[DATA_DURATION]
        if duration > 1440:
            raise ValueError("Maximum duration is 1440 minutes")

        # v2.0.0b (MOD-19, external ICS audit -- confirmed): see
        # forcible_charge()'s own note on this same pattern above.
        async with _set_and_invalidate_sequence(dd) as write:
            await write(rn.STORAGE_FORCIBLE_DISCHARGE_POWER, power)
            await write(rn.STORAGE_FORCED_CHARGING_AND_DISCHARGING_PERIOD, duration)
            await write(
                rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_SETTING_MODE,
                rv.StorageForcibleChargeDischargeTargetMode.TIME,
            )
            await write(
                rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_WRITE,
                rv.StorageForcibleChargeDischarge.DISCHARGE,
            )
        assert dd.configuration_update_coordinator
        await dd.configuration_update_coordinator.async_refresh()


async def forcible_charge_soc(service_call: ServiceCall) -> None:
    """Start a forcible charge on the battery until the target SOC is hit."""
    dd = get_battery_device_data(service_call)
    async with _get_device_write_lock(dd.device.serial_number):
        target_soc = service_call.data[DATA_TARGET_SOC]
        power = await _validate_power_value(
            service_call.data[DATA_POWER], dd, rn.STORAGE_MAXIMUM_CHARGE_POWER
        )

        # v2.0.3 FIX (ICS-11, external ICS audit -- confirmed): this
        # SOC-targeted variant used to perform four separately-guarded
        # _set_and_invalidate() calls -- exactly the MOD-19 pattern
        # already closed for the time-based forcible_charge()/
        # forcible_discharge() below, but missed for these two SOC
        # variants when that fix was made. Now uses the same atomic
        # sequence helper: one continuous guard hold across all four
        # writes, bounded by one whole-sequence deadline, so another
        # coordinator's poll cannot be admitted between any two of them.
        async with _set_and_invalidate_sequence(dd) as write:
            await write(rn.STORAGE_FORCIBLE_CHARGE_POWER, power)
            await write(rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_SOC, target_soc)
            await write(
                rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_SETTING_MODE,
                rv.StorageForcibleChargeDischargeTargetMode.SOC,
            )
            await write(
                rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_WRITE,
                rv.StorageForcibleChargeDischarge.CHARGE,
            )
        assert dd.configuration_update_coordinator
        await dd.configuration_update_coordinator.async_refresh()


async def forcible_discharge_soc(service_call: ServiceCall) -> None:
    """Start a forcible discharge on the battery until the target SOC is hit."""
    dd = get_battery_device_data(service_call)
    async with _get_device_write_lock(dd.device.serial_number):
        target_soc = service_call.data[DATA_TARGET_SOC]
        power = await _validate_power_value(
            service_call.data[DATA_POWER], dd, rn.STORAGE_MAXIMUM_DISCHARGE_POWER
        )

        # v2.0.3 FIX (ICS-11): see forcible_charge_soc()'s own note on
        # this same fix, immediately above.
        async with _set_and_invalidate_sequence(dd) as write:
            await write(rn.STORAGE_FORCIBLE_DISCHARGE_POWER, power)
            await write(rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_SOC, target_soc)
            await write(
                rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_SETTING_MODE,
                rv.StorageForcibleChargeDischargeTargetMode.SOC,
            )
            await write(
                rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_WRITE,
                rv.StorageForcibleChargeDischarge.DISCHARGE,
            )
        assert dd.configuration_update_coordinator
        await dd.configuration_update_coordinator.async_refresh()


async def stop_forcible_charge(service_call: ServiceCall) -> None:
    """Stop a forcible charge or discharge."""
    dd = get_battery_device_data(service_call)
    async with _get_device_write_lock(dd.device.serial_number):
        # v2.0.0b (MOD-19/MOD-20, external ICS audit -- confirmed): five
        # writes, one logical "stop forcible charge" command -- see
        # forcible_charge()'s own note above. MOD-20 specifically: this is
        # the service-layer equivalent of button.py's StopForcibleCharge
        # button, which already used one guard hold (v2.0.0a) and now
        # also has a whole-sequence deadline (v2.0.0b) -- both entry
        # points for the same physical command now carry the same bound,
        # guard-serialised guarantee.
        async with _set_and_invalidate_sequence(dd) as write:
            await write(
                rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_WRITE,
                rv.StorageForcibleChargeDischarge.STOP,
            )
            # Reset both charge and discharge power registers so no stale power value
            # remains on the inverter after the operation is cancelled.
            await write(rn.STORAGE_FORCIBLE_CHARGE_POWER, 0)
            await write(rn.STORAGE_FORCIBLE_DISCHARGE_POWER, 0)
            await write(rn.STORAGE_FORCED_CHARGING_AND_DISCHARGING_PERIOD, 0)
            await write(
                rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_SETTING_MODE,
                rv.StorageForcibleChargeDischargeTargetMode.TIME,
            )

        assert dd.configuration_update_coordinator
        await dd.configuration_update_coordinator.async_refresh()


class _PowerControlRegisters(TypedDict):
    MODE_REGISTER: RegisterName
    POWER_WATT_REGISTER: RegisterName
    POWER_PERCENT_REGISTER: RegisterName


PowerControlManagerType = Literal["inverter", "emma"]

POWER_CONTROL_REGISTERS: dict[PowerControlManagerType, _PowerControlRegisters] = {
    "inverter": {
        "MODE_REGISTER": rn.ACTIVE_POWER_CONTROL_MODE,
        "POWER_WATT_REGISTER": rn.MAXIMUM_FEED_GRID_POWER_WATT,
        "POWER_PERCENT_REGISTER": rn.MAXIMUM_FEED_GRID_POWER_PERCENT,
    },
    "emma": {
        "MODE_REGISTER": rn.EMMA_POWER_CONTROL_MODE_AT_GRID_CONNECTION_POINT,
        "POWER_WATT_REGISTER": rn.EMMA_MAXIMUM_FEED_GRID_POWER_WATT,
        "POWER_PERCENT_REGISTER": rn.EMMA_MAXIMUM_FEED_GRID_POWER_PERCENT,
    },
}


def _get_power_control_device_data(
    manager_type: PowerControlManagerType,
    service_call: ServiceCall,
) -> HuaweiSolarDeviceData:
    if manager_type == "emma":
        return get_emma_device(service_call)
    return get_inverter_data(service_call)


@callback
def _resolve_power_control_device(
    service_call: ServiceCall,
) -> tuple[HuaweiSolarDeviceData, PowerControlManagerType]:
    """v2.0.9 FIX (DEF-004, external ICS quality/defect/architecture audit
    -- confirmed): resolves the target device ONCE, generically, then
    determines its real manager_type from the device ITSELF -- rather
    than the caller needing to already know (and pre-supply) which kind
    of device it's dealing with, which is what functools.partial()
    baked in at REGISTRATION time (see async_setup_services()'s own
    updated comment for the full history of why that was wrong).

    With this, `reset_maximum_feed_grid_power`/`set_zero_power_grid_
    connection`/`set_maximum_feed_grid_power`/`set_maximum_feed_grid_
    power_percentage` below no longer need a manager_type parameter
    supplied by their caller at all -- they resolve it themselves, from
    the actual target device named in the service call, every time.
    This is what makes registering each service exactly once (not
    twice, not order-dependently per entry) both possible and correct:
    dispatch now depends on the SERVICE CALL'S OWN target, never on
    which entry happened to register last.
    """
    dd = _get_device_data(service_call)
    if isinstance(dd.device, EMMADevice):
        return dd, "emma"
    if isinstance(dd.device, SUN2000Device):
        return dd, "inverter"
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="wrong_device_type",
        translation_placeholders={
            "device_id": service_call.data[ATTR_DEVICE_ID],
            "expected_type": f"{EMMADevice.__name__} or {SUN2000Device.__name__}",
            "actual_type": type(dd.device).__name__,
        },
    )


async def reset_maximum_feed_grid_power(
    service_call: ServiceCall,
) -> None:
    """Set Active Power Control to 'Unlimited'."""
    dd, manager_type = _resolve_power_control_device(service_call)

    async with _get_device_write_lock(dd.device.serial_number):
        # v2.0.0b (MOD-19, external ICS audit -- confirmed): see
        # forcible_charge()'s own note on this pattern.
        async with _set_and_invalidate_sequence(dd) as write:
            await write(
                POWER_CONTROL_REGISTERS[manager_type]["MODE_REGISTER"],
                rv.ActivePowerControlMode.UNLIMITED,
            )
            await write(POWER_CONTROL_REGISTERS[manager_type]["POWER_WATT_REGISTER"], 0)
            await write(POWER_CONTROL_REGISTERS[manager_type]["POWER_PERCENT_REGISTER"], 0)

        assert dd.configuration_update_coordinator
        await dd.configuration_update_coordinator.async_refresh()


# only available for inverters
async def set_di_active_power_scheduling(service_call: ServiceCall) -> None:
    """Set Active Power Control to 'DI active scheduling'."""
    dd = get_inverter_data(service_call)
    async with _get_device_write_lock(dd.device.serial_number):
        async with _set_and_invalidate_sequence(dd) as write:
            await write(
                rn.ACTIVE_POWER_CONTROL_MODE,
                rv.ActivePowerControlMode.DI_ACTIVE_SCHEDULING,
            )
            await write(rn.MAXIMUM_FEED_GRID_POWER_WATT, 0)
            await write(rn.MAXIMUM_FEED_GRID_POWER_PERCENT, 0)

        assert dd.configuration_update_coordinator
        await dd.configuration_update_coordinator.async_refresh()


async def set_zero_power_grid_connection(
    service_call: ServiceCall,
) -> None:
    """Set Active Power Control to 'Zero-Power Grid Connection'."""
    dd, manager_type = _resolve_power_control_device(service_call)
    async with _get_device_write_lock(dd.device.serial_number):
        async with _set_and_invalidate_sequence(dd) as write:
            await write(
                POWER_CONTROL_REGISTERS[manager_type]["MODE_REGISTER"],
                rv.ActivePowerControlMode.ZERO_POWER_GRID_CONNECTION,
            )
            await write(POWER_CONTROL_REGISTERS[manager_type]["POWER_WATT_REGISTER"], 0)
            await write(POWER_CONTROL_REGISTERS[manager_type]["POWER_PERCENT_REGISTER"], 0)

        assert dd.configuration_update_coordinator
        await dd.configuration_update_coordinator.async_refresh()


async def set_maximum_feed_grid_power(
    service_call: ServiceCall,
) -> None:
    """Set Active Power Control to 'Power-limited grid connection' with the given wattage."""
    dd, manager_type = _resolve_power_control_device(service_call)
    async with _get_device_write_lock(dd.device.serial_number):
        power = await _validate_power_value(service_call.data[DATA_POWER], dd, rn.P_MAX)

        async with _set_and_invalidate_sequence(dd) as write:
            await write(POWER_CONTROL_REGISTERS[manager_type]["POWER_WATT_REGISTER"], power)
            await write(
                POWER_CONTROL_REGISTERS[manager_type]["MODE_REGISTER"],
                rv.ActivePowerControlMode.POWER_LIMITED_GRID_CONNECTION_WATT,
            )

        assert dd.configuration_update_coordinator
        await dd.configuration_update_coordinator.async_refresh()


async def set_maximum_feed_grid_power_percentage(
    service_call: ServiceCall,
) -> None:
    """Set Active Power Control to 'Power-limited grid connection' with the given percentage."""
    dd, manager_type = _resolve_power_control_device(service_call)
    async with _get_device_write_lock(dd.device.serial_number):
        power_percentage = service_call.data[DATA_POWER_PERCENTAGE]

        async with _set_and_invalidate_sequence(dd) as write:
            await write(
                POWER_CONTROL_REGISTERS[manager_type]["POWER_PERCENT_REGISTER"],
                power_percentage,
            )
            await write(
                POWER_CONTROL_REGISTERS[manager_type]["MODE_REGISTER"],
                rv.ActivePowerControlMode.POWER_LIMITED_GRID_CONNECTION_PERCENT,
            )

        assert dd.configuration_update_coordinator
        await dd.configuration_update_coordinator.async_refresh()


async def set_battery_tou_periods(
    service_call: ServiceCall,
) -> None:
    """Set the TOU periods of the battery."""

    dd = get_battery_device_data(service_call)

    async with _get_device_write_lock(dd.device.serial_number):
        if dd.device.battery_type == rv.StorageProductModel.HUAWEI_LUNA2000:
            if not re.fullmatch(
                HUAWEI_LUNA2000_TOU_PATTERN, service_call.data[DATA_PERIODS]
            ):
                raise ValueError(
                    f"Invalid periods: validation failed for '{service_call.data[DATA_PERIODS]}' as LUNA2000 TOU periods"
                )
            await _set_and_invalidate(
                dd,
                rn.STORAGE_HUAWEI_LUNA2000_TIME_OF_USE_CHARGING_AND_DISCHARGING_PERIODS,
                _parse_huawei_luna2000_periods(service_call.data[DATA_PERIODS]),
            )
        elif dd.device.battery_type == rv.StorageProductModel.LG_RESU:
            if not re.fullmatch(LG_RESU_TOU_PATTERN, service_call.data[DATA_PERIODS]):
                raise ValueError(
                    f"Invalid periods: validation failed for '{service_call.data[DATA_PERIODS]}' as LG RESU TOU periods"
                )
            await _set_and_invalidate(
                dd,
                rn.STORAGE_LG_RESU_TIME_OF_USE_PRICE_PERIODS,
                _parse_lg_resu_periods(service_call.data[DATA_PERIODS]),
            )

        assert dd.configuration_update_coordinator
        await dd.configuration_update_coordinator.async_refresh()


async def set_emma_tou_periods(
    service_call: ServiceCall,
) -> None:
    """Set the TOU periods of a battery controlled by an EMMA."""

    dd = get_emma_device(service_call)

    async with _get_device_write_lock(dd.device.serial_number):
        if not re.fullmatch(HUAWEI_LUNA2000_TOU_PATTERN, service_call.data[DATA_PERIODS]):
            raise ValueError(
                f"Invalid periods: validation failed for '{service_call.data[DATA_PERIODS]}' as TOU periods"
            )
        await _set_and_invalidate(
            dd,
            rn.EMMA_TOU_PERIODS,
            _parse_huawei_luna2000_periods(service_call.data[DATA_PERIODS]),
        )

        assert dd.configuration_update_coordinator
        await dd.configuration_update_coordinator.async_refresh()


def _parse_capacity_control_periods(text: str) -> list[PeakSettingPeriod]:
    result = []
    for line in text.split("\n"):
        if not line.strip():
            continue  # tolerate blank lines / empty input (clears all periods)
        start_end_time_str, days_str, wattage_str = line.split("/")
        start_time_str, end_time_str = start_end_time_str.split("-")

        result.append(
            PeakSettingPeriod(
                _parse_time(start_time_str),
                _parse_time(end_time_str),
                int(wattage_str[:-1]),
                _parse_days_effective(days_str),
            )
        )
    return result


def _validate_capacity_control_periods(periods: list[PeakSettingPeriod]) -> None:
    """Reject syntactically-valid but semantically-invalid capacity
    control period sets before they ever reach the write path.

    v2.0.7 FIX (CP-02, ICS quality audit -- confirmed): the regex this
    function's own caller applies validates only character-level syntax
    (HH:MM format, days mask, integer wattage) -- it says nothing about
    whether the resulting periods actually cover every day completely.
    PeakSettingPeriodRegisters (the vendor huawei_solar package) DOES
    already implement the correct check -- a `_validate()` method that
    requires full 00:00-23:59 coverage per weekday with no gaps -- but
    its own `encode()` never calls it (a genuine vendor defect,
    confirmed directly against that source), so nothing upstream of the
    device would actually catch an incomplete or gapped schedule.

    Deliberately mirrors the vendor's own `_validate()` algorithm
    exactly, not a reinvented one -- same per-weekday "must start at
    00:00, each period's start must equal or be one minute past the
    previous period's end, must reach 23:59/24:00" rule, same
    sort-by-start-time-then-check-adjacent-pairs approach. Reproduced
    here (in this repo's own code, not by patching the vendor package --
    see this project's own settled position on where fixed code for
    genuinely external dependencies belongs) rather than invented from
    scratch, for the same reason _validate_fixed_charge_periods() above
    mirrors ITS sibling vendor validator instead of guessing a new rule.
    """
    for day_idx in range(7):
        active_periods = [p for p in periods if p.days_effective[day_idx]]
        if not active_periods:
            raise ValueError(
                f"Invalid capacity control periods: day index {day_idx} "
                f"(0=Sunday) has no period covering it at all -- every "
                f"day of the week must be covered"
            )
        active_periods = sorted(active_periods, key=lambda p: p.start_time)
        if active_periods[0].start_time != 0:
            raise ValueError(
                f"Invalid capacity control periods: day index {day_idx} "
                f"(0=Sunday) is not covered from 00:00 -- its earliest "
                f"period starts at {active_periods[0].start_time // 60:02d}:"
                f"{active_periods[0].start_time % 60:02d}"
            )
        for prev, current in zip(active_periods, active_periods[1:]):
            if current.start_time not in (prev.end_time, prev.end_time + 1):
                raise ValueError(
                    f"Invalid capacity control periods: day index {day_idx} "
                    f"(0=Sunday) has a gap or overlap between "
                    f"{prev.start_time // 60:02d}:{prev.start_time % 60:02d}-"
                    f"{prev.end_time // 60:02d}:{prev.end_time % 60:02d} and "
                    f"{current.start_time // 60:02d}:{current.start_time % 60:02d}-"
                    f"{current.end_time // 60:02d}:{current.end_time % 60:02d} "
                    f"-- every moment of each day must be covered exactly once"
                )
        last = active_periods[-1]
        if last.end_time not in (24 * 60 - 1, 24 * 60):
            raise ValueError(
                f"Invalid capacity control periods: day index {day_idx} "
                f"(0=Sunday) is not covered until 23:59 -- its latest "
                f"period ends at {last.end_time // 60:02d}:{last.end_time % 60:02d}"
            )


async def set_capacity_control_periods(service_call: ServiceCall) -> None:
    """Set the Capacity Control Periods of the battery."""

    dd = get_battery_device_data(service_call)

    async with _get_device_write_lock(dd.device.serial_number):
        if not re.fullmatch(
            CAPACITY_CONTROL_PERIODS_PATTERN, service_call.data[DATA_PERIODS]
        ):
            raise ValueError(
                f"Invalid periods: could not validate '{service_call.data[DATA_PERIODS]}' as capacity control periods"
            )

        periods = _parse_capacity_control_periods(service_call.data[DATA_PERIODS])
        # v2.0.7 (CP-02): semantic validation, on top of the syntax
        # check above -- before this device is ever touched, matching
        # the same established pattern as set_fixed_charge_periods()'s
        # own ICS-07 fix.
        _validate_capacity_control_periods(periods)

        await _set_and_invalidate(
            dd,
            rn.STORAGE_CAPACITY_CONTROL_PERIODS,
            periods,
        )

        assert dd.configuration_update_coordinator
        await dd.configuration_update_coordinator.async_refresh()


def _parse_fixed_charge_periods(text: str) -> list[ChargeDischargePeriod]:
    result = []
    for line in text.split("\n"):
        if not line.strip():
            continue  # tolerate blank lines / empty input (clears all periods)
        start_end_time_str, wattage_str = line.split("/")
        start_time_str, end_time_str = start_end_time_str.split("-")

        result.append(
            ChargeDischargePeriod(
                _parse_time(start_time_str),
                _parse_time(end_time_str),
                int(wattage_str[:-1]),
            )
        )
    return result


def _validate_fixed_charge_periods(periods: list[ChargeDischargePeriod]) -> None:
    """Reject syntactically-valid but semantically-invalid period sets
    before they ever reach the write path.

    v2.0.3 FIX (ICS-07, external ICS audit -- confirmed): the regex this
    function's own caller applies validates only character-level syntax
    (HH:MM format, integer wattage, line count) -- it says nothing about
    whether the resulting periods actually make sense together.
    ChargeDischargePeriodRegisters.encode() (the vendor huawei_solar
    package) does not validate this either -- checked directly against
    that source before writing this fix, not assumed -- it only checks
    the maximum period COUNT, nothing about ordering or overlap. Nothing
    upstream of the device itself would have caught a schedule like
    "09:00-08:00" (end before start) or two periods that overlap.

    Deliberately mirrors the exact algorithm the SAME vendor package
    already uses for the sibling TOU-period register types (register_
    definitions/periods.py's own encoder-side validation) rather than
    inventing a new one -- same "start must be strictly before end, no
    wraparound past midnight" rule, same sort-then-check-adjacent-pairs
    overlap test. Consistent with how this vendor's own devices are
    already known to validate this exact family of period types
    elsewhere, not a judgment call made from scratch here.
    """
    for period in periods:
        if period.start_time >= period.end_time:
            raise ValueError(
                f"Invalid period {period.start_time // 60:02d}:"
                f"{period.start_time % 60:02d}-{period.end_time // 60:02d}:"
                f"{period.end_time % 60:02d}: start must be strictly "
                f"before end (periods wrapping past midnight are not "
                f"supported)"
            )
    sorted_periods = sorted(periods, key=lambda p: p.start_time)
    for prev, current in zip(sorted_periods, sorted_periods[1:]):
        if (
            prev.start_time <= current.start_time < prev.end_time
            or prev.start_time < current.end_time <= prev.end_time
        ):
            raise ValueError(
                f"Overlapping periods: "
                f"{prev.start_time // 60:02d}:{prev.start_time % 60:02d}-"
                f"{prev.end_time // 60:02d}:{prev.end_time % 60:02d} and "
                f"{current.start_time // 60:02d}:{current.start_time % 60:02d}-"
                f"{current.end_time // 60:02d}:{current.end_time % 60:02d}"
            )


async def set_fixed_charge_periods(service_call: ServiceCall) -> None:
    """Set the fixed charging periods of the battery."""
    dd = get_battery_device_data(service_call)

    async with _get_device_write_lock(dd.device.serial_number):
        if not re.fullmatch(FIXED_CHARGE_PERIODS_PATTERN, service_call.data[DATA_PERIODS]):
            raise ValueError(
                f"Invalid periods: could not validate '{service_call.data[DATA_PERIODS]}' as fixed charging periods"
            )

        periods = _parse_fixed_charge_periods(service_call.data[DATA_PERIODS])
        # v2.0.3 (ICS-07): semantic validation, on top of the syntax
        # check above -- before this device is ever touched.
        _validate_fixed_charge_periods(periods)

        await _set_and_invalidate(
            dd,
            rn.STORAGE_FIXED_CHARGING_AND_DISCHARGING_PERIODS,
            periods,
        )

        assert dd.configuration_update_coordinator
        await dd.configuration_update_coordinator.async_refresh()


async def async_setup_services(
    hass: HomeAssistant,
    entry: HuaweiSolarConfigEntry,
) -> None:
    """Huawei Solar Services Setup."""
    if not entry.data.get(CONF_ENABLE_PARAMETER_CONFIGURATION, False):
        return

    # Deliberately NOT also guarded against re-registration on this same
    # entry_id already being present -- see async_unload_services() for
    # the reference-counted unregistration this pairs with.
    #
    # v2.0.9 FIX (DEF-004, external ICS quality/defect/architecture audit
    # -- confirmed): this comment used to explain why re-registration
    # ("last setup wins") was accepted as correct behaviour for the four
    # power-control services below, because their handlers were bound
    # via functools.partial() to a FIXED device-kind ("emma" vs
    # "inverter") resolved from whichever entry happened to register
    # last -- multiple entries with different device kinds coexisting
    # meant dispatch depended on registration ORDER, not on the actual
    # target device named in each individual service call. That
    # dependency is now eliminated at its root: _resolve_power_control_
    # device() (see its own docstring) resolves the real device kind
    # from the SERVICE CALL's own target, every time, so these four
    # services are now registered exactly once each, unconditionally,
    # regardless of which entry sets up first or what device kinds any
    # given entry happens to have.
    _entries_with_services.add(entry.entry_id)

    hsucs: list[HuaweiSolarDeviceData] = entry.runtime_data[DATA_DEVICE_DATAS]

    has_battery = any(
        isinstance(uc.device, SUN2000Device)
        and uc.device.battery_type != rv.StorageProductModel.NONE
        for uc in hsucs
    )

    has_lg_battery = any(
        isinstance(uc.device, SUN2000Device)
        and uc.device.battery_type == rv.StorageProductModel.LG_RESU
        for uc in hsucs
    )

    has_capacity_control = any(
        isinstance(uc.device, SUN2000Device) and uc.device.supports_capacity_control
        for uc in hsucs
    )
    has_emma = any(isinstance(uc.device, EMMADevice) for uc in hsucs)

    # v2.0.9 FIX (DEF-004, same audit -- confirmed): registered exactly
    # once each, unconditionally -- not gated on has_emma, and not
    # duplicated with two different functools.partial() bindings. Both
    # EMMA_DEVICE_SCHEMA and INVERTER_DEVICE_SCHEMA are identical
    # (DATA_DEVICE_ID only); INVERTER_DEVICE_SCHEMA is used here simply
    # as the one consistent choice, not because it privileges either
    # device kind -- device-kind resolution now happens entirely inside
    # the handler, from the service call's own target.
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_MAXIMUM_FEED_GRID_POWER,
        reset_maximum_feed_grid_power,
        schema=INVERTER_DEVICE_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_ZERO_POWER_GRID_CONNECTION,
        set_zero_power_grid_connection,
        schema=INVERTER_DEVICE_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_MAXIMUM_FEED_GRID_POWER,
        set_maximum_feed_grid_power,
        schema=INVERTER_DEVICE_SCHEMA.extend(MAXIMUM_FEED_GRID_POWER_SCHEMA),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_MAXIMUM_FEED_GRID_POWER_PERCENT,
        set_maximum_feed_grid_power_percentage,
        schema=INVERTER_DEVICE_SCHEMA.extend(
            MAXIMUM_FEED_GRID_POWER_PERCENTAGE_SCHEMA
        ),
    )

    if not has_emma:
        # this service is only available on inverters, not on EMMA
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_DI_ACTIVE_POWER_SCHEDULING,
            set_di_active_power_scheduling,
            schema=INVERTER_DEVICE_SCHEMA,
        )

    if has_battery:
        # When an EMMA is present, it is responsible for managing the battery.
        # No direct control of the battery is possible.
        if has_emma:
            hass.services.async_register(
                DOMAIN,
                SERVICE_SET_TOU_PERIODS,
                set_emma_tou_periods,
                schema=EMMA_TOU_PERIODS_SCHEMA,
            )
        else:
            hass.services.async_register(
                DOMAIN,
                SERVICE_SET_TOU_PERIODS,
                set_battery_tou_periods,
                schema=BATTERY_TOU_PERIODS_SCHEMA,
            )

        # Direct forcible charge/discharge control writes STORAGE_FORCIBLE_*
        # registers straight to the inverter.  When an EMMA is present it is the
        # sole battery manager, so exposing these would let a user issue a write
        # that conflicts with EMMA's control.  Register them for non-EMMA setups
        # only — consistent with the EMMA/else split for SET_TOU_PERIODS above.
        if not has_emma:
            hass.services.async_register(
                DOMAIN,
                SERVICE_FORCIBLE_CHARGE,
                forcible_charge,
                schema=DURATION_SCHEMA,
            )
            hass.services.async_register(
                DOMAIN,
                SERVICE_FORCIBLE_DISCHARGE,
                forcible_discharge,
                schema=DURATION_SCHEMA,
            )
            hass.services.async_register(
                DOMAIN,
                SERVICE_FORCIBLE_CHARGE_SOC,
                forcible_charge_soc,
                schema=SOC_SCHEMA,
            )
            hass.services.async_register(
                DOMAIN,
                SERVICE_FORCIBLE_DISCHARGE_SOC,
                forcible_discharge_soc,
                schema=SOC_SCHEMA,
            )
            hass.services.async_register(
                DOMAIN,
                SERVICE_STOP_FORCIBLE_CHARGE,
                stop_forcible_charge,
                schema=BATTERY_DEVICE_SCHEMA,
            )
            # v2.0.12 (Battery Phase 5B, this release): registered
            # inside the SAME has_battery gate as the rest of this
            # cluster -- a genuinely battery-specific service, not
            # meaningful for a device with no battery at all.
            hass.services.async_register(
                DOMAIN,
                SERVICE_SET_PACK_INSTALL_DATE,
                set_pack_install_date,
                schema=SET_PACK_INSTALL_DATE_SCHEMA,
            )

    if has_lg_battery:
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_FIXED_CHARGE_PERIODS,
            set_fixed_charge_periods,
            schema=FIXED_CHARGE_PERIODS_SCHEMA,
        )

    if has_capacity_control:
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_CAPACITY_CONTROL_PERIODS,
            set_capacity_control_periods,
            schema=CAPACITY_CONTROL_PERIODS_SCHEMA,
        )
