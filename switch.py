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
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .adaptive_modbus import AdaptiveModbusController
from .battery_health_manager import BatteryHealthManager
from .bus_diagnostics import BusDiagnostics
from .const import (
    CONF_ENABLE_PARAMETER_CONFIGURATION,
    DATA_DEVICE_DATAS,
    DATA_SYNC_POWER_COORDINATOR,
    TELEMETRY_CAPTURE_INTERVAL,
)
from .modbus_telemetry import ModbusTelemetry
from .telemetry_capture import TelemetryCapture, build_telemetry_snapshot
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
                # v2.0.0b: same "one per bus" scoping as the diagnostics
                # switch immediately above -- periodic aggregate capture
                # is also a property of the shared physical connection,
                # not any one inverter. device_data (the full list, not
                # just `ucs`) and the entry's SyncPower coordinator (if
                # this entry has one) are threaded through so one snapshot
                # tick can gather every coordinator on the entry, not just
                # this one device's own.
                telemetry_capture = TelemetryCapture.get_or_create(hass, guard.endpoint)
                learning_switches.append(
                    ModbusTelemetryCaptureSwitchEntity(
                        telemetry_capture, ucs, device_data,
                        entry.runtime_data.get(DATA_SYNC_POWER_COORDINATOR),
                    )
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

            # v2.0.0: quality/reason/age attributes -- see _quality_attrs()'s
            # own docstring. Custom availability logic above is untouched.
            self._attr_extra_state_attributes = self._quality_attrs(
                self.coordinator, self.entity_description.register_name
            )
        else:
            self._attr_is_on = None
            self._attr_available = False

        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the setting on."""
        # v2.0.0b (MOD-05, external ICS audit -- confirmed): now uses the
        # shared _guarded_write() helper (types.py), pairing the guard
        # with WRITE_TIMEOUT -- see number.py's own note on this pattern.
        wrote = await self._guarded_write(
            self.coordinator.guard, self.device,
            self.entity_description.register_name, True,
            label="switch_write",
        )
        if wrote:
            self._attr_is_on = True
            self.coordinator.invalidate_cache(self.entity_description.register_name)
            # v2.0.0a (F12, external ICS audit -- confirmed): verify_write()
            # existed, fully built and already guarded, but had zero
            # production callers. Fired as a background task, not awaited --
            # see number.py's own note on this same pattern for the full
            # reasoning (~3-9s to confirm, whole value is a warning log on
            # silent failure, not worth blocking the toggle on).
            # v2.0.0b (MOD-10): entry-scoped via the coordinator now, not a
            # bare self.hass.async_create_task().
            self.coordinator.create_background_task(
                self.coordinator.verify_write(self.entity_description.register_name, True),
                f"{self.coordinator.name}_verify_write_{self.entity_description.register_name}",
            )

        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the setting off."""
        wrote = await self._guarded_write(
            self.coordinator.guard, self.device,
            self.entity_description.register_name, False,
            label="switch_write",
        )
        if wrote:
            self._attr_is_on = False
            self.coordinator.invalidate_cache(self.entity_description.register_name)
            self.coordinator.create_background_task(
                self.coordinator.verify_write(self.entity_description.register_name, False),
                f"{self.coordinator.name}_verify_write_{self.entity_description.register_name}",
            )

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
    # v1.3.14 FIX (Defect O): this constant was 3000 (50 minutes) while the
    # comment beside it, and the one in async_turn_on/async_turn_off below,
    # both stated "5 minutes" (300s) -- a 10x mismatch between the code and
    # its own documented intent, found while reviewing this file for a
    # separate issue. 300s matches the stated intent and is also the
    # physically reasonable figure for a SUN2000's actual startup/shutdown
    # sequence; corrected rather than the comments, since a 50-minute
    # worst-case poll loop was almost certainly never the intent.
    MAX_STATUS_CHANGE_TIME_SECONDS = 300  # Maximum status change time is 5 minutes

    # v1.3.19 (Defect V/Finding 7, reported independently by two separate
    # ICS audits): bound for each individual status read below. A healthy
    # device answers in well under a second; this is generous headroom
    # without letting one slow read consume an outsized share of the
    # overall MAX_STATUS_CHANGE_TIME_SECONDS budget.
    STATUS_POLL_READ_TIMEOUT_SECONDS = 15

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
            self._attr_extra_state_attributes = self._quality_attrs(
                self.coordinator, rn.DEVICE_STATUS
            )
        else:
            self._attr_available = False

        self.async_write_ha_state()

    async def _poll_device_status_bounded(self) -> str | None:
        """Read DEVICE_STATUS through the shared bus guard, bounded by a
        per-read timeout, instead of a raw, unguarded client call.

        v1.3.19 FIX (Defect V/Finding 7): this used to be
        `await self.device.client.get(rn.DEVICE_STATUS)` directly --
        outside ModbusGuard entirely, unpaced, and with no timeout of its
        own. Under contention, this could inject extra raw reads while the
        main coordinators were already polling, and a single slow read
        could block the whole status-change operation -- and, since it
        runs while self._change_lock is held, every OTHER action on this
        entity too -- for an unbounded amount of time. Returns None on any
        failure (timeout, shed, or otherwise) rather than raising, so the
        caller can simply treat it as "still don't know, try again next
        cycle" exactly like a normal coordinator poll would.
        """
        try:
            async with self.coordinator.guard.request(
                label=f"{self.device.serial_number}_switch_status"
            ):
                result = await asyncio.wait_for(
                    self.device.client.get(rn.DEVICE_STATUS),
                    timeout=self.STATUS_POLL_READ_TIMEOUT_SECONDS,
                )
            return result.value
        except Exception:  # noqa: BLE001 — a failed poll just means "try again next cycle"
            _LOGGER.debug(
                "%s: status poll failed; will retry on the next cycle",
                self.device.serial_number,
            )
            return None

    async def _wait_for_status(self, is_target_status) -> bool:
        """Poll DEVICE_STATUS until `is_target_status(status)` is True or
        the overall deadline elapses, whichever comes first. Returns
        whether the target status was reached.

        v1.3.19 FIX (Defect V/Finding 5, reported independently by two
        separate ICS audits): this loop used to be bounded by an iteration
        count (`MAX_STATUS_CHANGE_TIME_SECONDS // POLL_FREQUENCY_SECONDS`),
        with each iteration doing an unbounded sleep-then-read. In the best
        case (every read instant) that adds up to the stated 5-minute
        limit -- but since each read had no bound of its own, the REAL
        wall-clock duration could exceed that limit by an arbitrary amount
        if any single read blocked. Now tracks an explicit monotonic
        deadline, enforced around both the sleep and the read, so the
        total operation genuinely cannot exceed
        MAX_STATUS_CHANGE_TIME_SECONDS regardless of how long any
        individual read takes.
        """
        deadline = time.monotonic() + self.MAX_STATUS_CHANGE_TIME_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(self.POLL_FREQUENCY_SECONDS, remaining))
            if time.monotonic() >= deadline:
                return False
            status = await self._poll_device_status_bounded()
            if status is not None and is_target_status(status):
                return True

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the setting on."""
        # v2.0.0b (MOD-05, external ICS audit -- confirmed): now uses the
        # shared _guarded_write() helper (types.py), pairing the guard
        # with WRITE_TIMEOUT -- see number.py's own note on this pattern.
        # Unrelated to and unaffected by the SEPARATE, already-bounded
        # _wait_for_status() polling loop below (which has its own real
        # monotonic deadline, MAX_STATUS_CHANGE_TIME_SECONDS) -- this
        # timeout covers only the write itself.
        async with self._change_lock:
            await self._guarded_write(
                self.coordinator.guard, self.device, rn.STARTUP, 0,
                label="switch_write",
            )

            # Turning on can take up to 5 minutes... We'll poll every 15 seconds
            if await self._wait_for_status(lambda status: not self._is_off(status)):
                self._attr_is_on = True

        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the setting off."""
        async with self._change_lock:
            await self._guarded_write(
                self.coordinator.guard, self.device, rn.SHUTDOWN, 0,
                label="switch_write",
            )

            # Turning off can take up to 5 minutes... We'll poll every 15 seconds
            if await self._wait_for_status(self._is_off):
                self._attr_is_on = False

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
        """Stop capturing and deterministically flush anything pending
        before returning.

        v2.0.3 FIX (ICS-08, external ICS audit -- confirmed, the same
        defect as TEL-002): now awaits async_disable()
        (bus_diagnostics.py) instead of calling the synchronous
        set_enabled(False) -- turning this switch off now genuinely
        guarantees the final batch was persisted (or explicitly
        failed/timed out, logged) before this method returns.
        """
        await self._diagnostics.async_disable()
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Deterministically flush any pending capture if this entity is
        torn down (unload/reload) while capture is still running.

        v2.0.3 (ICS-08): BusDiagnostics owns no periodic timer (unlike
        ModbusTelemetryCaptureSwitchEntity's own equivalent method, which
        also has a timer to cancel) -- but it does now own a genuine
        async flush that must be awaited, not left as a fire-and-forget
        best effort, for the same reason turning the switch off does.
        """
        if self._diagnostics.enabled:
            await self._diagnostics.async_disable()


class ModbusTelemetryCaptureSwitchEntity(SwitchEntity):
    """Periodic aggregate Modbus telemetry snapshot capture for one
    physical bus.

    Complements ModbusDiagnosticsSwitchEntity above: where that switch
    records EVERY individual request's wait/service split, this one
    periodically snapshots the AGGREGATE metrics already computed
    elsewhere (per-device adaptive/telemetry counters, and
    SynchronizedPowerCoordinator's own dedicated cache-hit/physical-read
    counters) into a real time series -- the data needed to directly
    assess the external ICS audit's Physical Demand Planner
    recommendation without a second deployment purely to add more
    telemetry.

    Default OFF, and deliberately NOT restored across restarts -- same
    reasoning as the diagnostics switch: a capture must never be silently
    left running forever.

    Records go to ``config/huawei_solar_diagnostics/telemetry_<tag>.jsonl``
    -- same directory and same salted-hash tag as the per-request capture
    above (so the two files for one physical bus are easy to find
    together), different filename. Runs on its own periodic timer
    (TELEMETRY_CAPTURE_INTERVAL), independent of any single coordinator's
    own poll cadence, so one snapshot's contents all come from the same
    capture tick, not staggered across separate ticks each coordinator
    triggers on its own.

    v2.0.2 FIX (TEL-009, external ICS/IQS audit -- confirmed): this
    docstring previously claimed one snapshot "always reflects the same
    moment" for every coordinator -- an overstatement of what actually
    happens. build_telemetry_snapshot() (telemetry_capture.py) reads
    each coordinator's own already-computed, in-memory snapshot() dict
    in sequence, not concurrently or atomically -- there is no shared
    barrier or lock making every read land at one identical instant. In
    practice this is a same-tick, near-coincident sample (the reads
    themselves are cheap in-memory dict lookups, not physical I/O, so the
    actual spread between the first and last read within one tick is
    small), not a literal single instant across every coordinator. The
    outer record's own "t" timestamp (telemetry_capture.py's
    record_snapshot()) is assigned once, after all of this tick's reads
    complete -- it marks when the snapshot was recorded, not exactly when
    each individual value inside it was itself sampled.

    Writes no inverter registers, and never blocks the event loop.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False
    _attr_name = "Modbus telemetry capture"
    _attr_icon = "mdi:chart-line"

    def __init__(
        self,
        capture: TelemetryCapture,
        device_data: Any,
        device_datas: list[Any],
        sync_coordinator: Any | None,
    ) -> None:
        """Initialize the telemetry capture switch."""
        self._capture = capture
        self._device_datas = device_datas
        self._sync_coordinator = sync_coordinator
        # The register-overlap structural check is genuinely one-time
        # (see check_register_overlap()'s own docstring) -- tracked here
        # so it is attempted on the first tick after being enabled and
        # then left out of every subsequent one, not recomputed forever.
        # Reset on every re-enable: a fresh capture session should get
        # its own fresh attempt, in case coordinators were not yet polled
        # the first time this ran.
        self._register_overlap_captured = False
        self._attr_device_info = device_data.device_info
        self._attr_unique_id = (
            f"{device_data.device.serial_number}_modbus_telemetry_capture"
        )

    @property
    def is_on(self) -> bool:
        """Return True while capture is running."""
        return self._capture.enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose capture health, including dropped snapshots."""
        return self._capture.stats()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start periodic aggregate snapshot capture.

        v2.0.2 FIX (TEL-003, external ICS/IQS audit -- confirmed): this
        used to install a new periodic timer unconditionally, even if
        one was already running -- TelemetryCapture.set_enabled(True)
        itself correctly no-ops if already enabled, but this method
        never checked that before installing a second timer anyway. Two
        consecutive turn-on calls (a double UI click, or a service call
        racing a UI action) would silently orphan the first timer's own
        cancel handle -- both would keep firing independently, doubling
        the write volume with no way to cancel the orphaned one. Made
        idempotent: a second turn-on while a timer is already registered
        is now a no-op for the timer specifically, matching
        TelemetryCapture's own already-idempotent enabled-state handling.
        """
        self._capture.set_enabled(True)
        self._register_overlap_captured = False
        if self._capture.cancel_periodic is not None:
            return
        # Stored on the capture object itself (not just a local variable)
        # so TelemetryCapture.async_disable() -- called both from
        # async_turn_off() below AND anywhere else this capture might be
        # disabled -- can always cancel it, not only this specific code
        # path.
        self._capture.cancel_periodic = async_track_time_interval(
            self.hass, self._async_snapshot_tick, TELEMETRY_CAPTURE_INTERVAL,
        )
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop capturing and deterministically flush anything pending
        before returning.

        v2.0.2 (TEL-002, external ICS/IQS audit -- confirmed): now
        awaits async_disable() (telemetry_capture.py) instead of calling
        the synchronous set_enabled(False) -- the actual fix. Turning
        this switch off now genuinely guarantees the final batch was
        persisted (or explicitly failed/timed out, logged) before this
        method returns, not a fire-and-forget best effort.
        """
        await self._capture.async_disable()  # also cancels the periodic timer
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Ensure the periodic timer is cancelled -- and any pending
        capture deterministically flushed -- if this entity is torn
        down (unload/reload) while capture is still running.

        BusDiagnostics has no equivalent concern -- it has no timer of
        its own, only a buffer. This switch owns a genuinely new kind of
        resource (a recurring callback holding references to this
        entry's coordinators), and turning the switch off is not the
        only way this entity's lifecycle can end -- an unload/reload
        while capture happens to be on must not leave that timer firing
        against coordinators that no longer exist, and (v2.0.2, TEL-002)
        must not silently drop whatever was still pending at that moment
        either.
        """
        if self._capture.enabled:
            await self._capture.async_disable()

    async def _async_snapshot_tick(self, now: Any) -> None:
        """Gather and record one telemetry snapshot. Runs on the timer.

        Exception-guarded end to end: a telemetry fault must cost
        telemetry, never the coordinators' own polling -- the same
        discipline bus_diagnostics.py's own module docstring states for
        its per-request capture.
        """
        try:
            snapshot = build_telemetry_snapshot(
                self._device_datas,
                self._sync_coordinator,
                include_register_overlap=not self._register_overlap_captured,
                adaptive_controller_cls=AdaptiveModbusController,
                modbus_telemetry_cls=ModbusTelemetry,
            )
            if "register_overlap" in snapshot:
                self._register_overlap_captured = True
            self._capture.record_snapshot(snapshot)
        except Exception:  # noqa: BLE001 — telemetry must never break polling
            _LOGGER.exception("Modbus telemetry capture: snapshot tick failed")
