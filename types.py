"""Typing for the Huawei Solar integration."""

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypedDict, cast

from huawei_solar import HuaweiSolarDevice, RegisterName, SUN2000Device

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity, EntityDescription

from .const import WRITE_SEQUENCE_TIMEOUT, WRITE_TIMEOUT
from .update_coordinator import (
    HuaweiSolarOptimizerUpdateCoordinator,
    HuaweiSolarUpdateCoordinator,
)

if TYPE_CHECKING:
    from .modbus_guard import ModbusGuard


@dataclass
class HuaweiSolarDeviceData:
    """Runtime data for the Huawei Solar integration."""

    device: HuaweiSolarDevice
    device_info: DeviceInfo
    update_coordinator: HuaweiSolarUpdateCoordinator
    configuration_update_coordinator: HuaweiSolarUpdateCoordinator | None


@dataclass
class HuaweiSolarInverterData(HuaweiSolarDeviceData):
    """Runtime data for the Huawei Solar integration for SUN2000 inverter devices."""

    device: SUN2000Device

    power_meter: DeviceInfo | None
    connected_energy_storage: DeviceInfo | None
    battery_1: DeviceInfo | None
    battery_2: DeviceInfo | None
    optimizer_device_infos: dict[int, DeviceInfo] | None

    power_meter_update_coordinator: HuaweiSolarUpdateCoordinator | None
    energy_storage_update_coordinator: HuaweiSolarUpdateCoordinator | None
    optimizer_update_coordinator: HuaweiSolarOptimizerUpdateCoordinator | None


type HuaweiSolarConfigEntry = ConfigEntry[HuaweiSolarData]


class HuaweiSolarData(TypedDict):
    """Data for each Huawei Solar config entry."""

    device_datas: list[HuaweiSolarDeviceData]


class HuaweiSolarEntity(Entity):
    """Huawei Solar Entity."""

    _attr_has_entity_name = True

    def _quality_attrs(
        self,
        coordinator: "HuaweiSolarUpdateCoordinator",
        register_key: RegisterName,
    ) -> dict[str, str | float]:
        """v2.0.0: build the data_quality/data_quality_reason/data_age_seconds
        extra_state_attributes for one register.

        Shared, exactly once, across every entity platform (sensor, number,
        select, switch) via this common mixin — see
        V2_ARCHITECTURE_DESIGN.md §8 (the attribute shape) and §10.4 (why
        this is a separate, additive accessor rather than a change to
        coordinator.data's own shape — every platform's existing
        `self._register_key in self.coordinator.data` availability check
        is untouched by this addition; RegisterCache.merge() already
        implements the correct GOOD-or-UNCERTAIN-serves,
        only-BAD-is-omitted rule that check depends on).
        """
        quality, reason, age = coordinator.cache.quality_of(register_key)
        attrs: dict[str, str | float] = {"data_quality": quality.name.lower()}
        if reason is not None:
            attrs["data_quality_reason"] = reason.name.lower()
        if age is not None:
            attrs["data_age_seconds"] = round(age, 1)
        return attrs

    async def _guarded_write(
        self,
        guard: "ModbusGuard",
        device: HuaweiSolarDevice,
        name: RegisterName,
        value: Any,
        *,
        label: str = "entity_write",
    ) -> bool:
        """Perform one guarded, time-bounded device.set() call.

        v2.0.0b (MOD-05, external ICS audit -- confirmed): v2.0.0a's F05
        fix routed every entity write through ModbusGuard, but never
        bounded the underlying device.set() call itself -- the guard
        provides serialisation, not a deadline. A stalled write held the
        guard indefinitely, starving every other coordinator/consumer on
        the endpoint. This is the single shared primitive number.py,
        select.py, and switch.py's simple (single-register) writes now
        all call, so the guard+timeout pairing only has to be gotten
        right in one place, not re-derived at every call site.

        For a MULTI-register logical write sequence that must hold the
        guard continuously across several writes, use
        _guarded_write_sequence() below instead -- it applies one
        whole-sequence deadline rather than bounding each write
        individually, which would let the sequence's total duration grow
        unboundedly with its own length.
        """
        async with guard.request(label=label):
            async with asyncio.timeout(WRITE_TIMEOUT.total_seconds()):
                return await device.set(name, value)

    @contextlib.asynccontextmanager
    async def _guarded_write_sequence(
        self,
        guard: "ModbusGuard",
        *,
        label: str = "entity_write_sequence",
    ):
        """Hold the guard continuously across a multi-register logical
        write command, bounded by ONE whole-sequence deadline.

        v2.0.0b (MOD-06, external ICS audit -- confirmed): a multi-write
        command (e.g. "stop forcible charge": four sequential writes that
        must apply together) already held the guard continuously here --
        correctly guaranteeing atomicity against other bus traffic -- but
        placed no bound on how long that exclusive hold could last. Use:

            async with self._guarded_write_sequence(guard) as write:
                await write(device, rn.SOME_REGISTER, value)
                await write(device, rn.OTHER_REGISTER, value)

        The yielded `write` callable issues the raw device.set() call
        without its own guard/timeout (the surrounding `async with`
        already provides both, once, for the whole sequence) -- do not
        call device.set() directly inside this block, or the sequence
        loses its bound.
        """
        async with guard.request(label=label):
            async with asyncio.timeout(WRITE_SEQUENCE_TIMEOUT.total_seconds()):
                async def _write(device: HuaweiSolarDevice, name: RegisterName, value: Any) -> bool:
                    return await device.set(name, value)
                yield _write


class HuaweiSolarEntityDescription(EntityDescription):
    """Huawei Solar Entity Description."""

    @property
    def register_name(self) -> RegisterName:
        """Return the register name."""
        return cast("RegisterName", self.key)


class HuaweiSolarEntityContext(TypedDict):
    """Context for Huawei Solar Entities."""

    register_names: list[RegisterName]


# v2.0.7 FIX (ICS-12, ICS quality audit -- confirmed): the per-serial
# "one logical command at a time" write lock, moved here from services.py
# so BOTH button.py and services.py can share the SAME registry without
# button.py needing to import services.py's own heavy dependency chain
# (voluptuous schemas, ServiceCall handling, config_validation, etc. --
# none of which an entity-platform module has any other reason to need).
# types.py is already a dependency of both call sites (button.py already
# imports from here for _guarded_write_sequence; services.py already
# imports WRITE_TIMEOUT-adjacent constants), so this adds no new edge.
#
# Previously, button.py's StopForcibleCharge press and services.py's own
# stop_forcible_charge() service held ONLY ModbusGuard (preventing literal
# mid-sequence interleaving between them, since both already hold the
# guard for their whole write sequence) but had no shared LOGICAL lock --
# two COMPLETE write sequences, one from each entry point, could still
# race back-to-back in unpredictable order if triggered concurrently
# (e.g. a user presses the button at the same moment an automation calls
# the service), with whichever finished last simply overwriting the
# other's fully-applied result.
_device_write_locks: dict[str, asyncio.Lock] = {}


def get_device_write_lock(serial_number: str) -> asyncio.Lock:
    """Return the shared per-serial write lock, creating it on first use.

    Locks are deliberately never removed from this registry (matching the
    reasoning already established for the equivalent ModbusGuard/
    AdaptiveModbusController per-device registries elsewhere in this
    project): an idle asyncio.Lock is negligible memory, and removing one
    while a call might still be queued on it would be far more dangerous
    than never removing it at all.
    """
    if serial_number not in _device_write_locks:
        _device_write_locks[serial_number] = asyncio.Lock()
    return _device_write_locks[serial_number]
