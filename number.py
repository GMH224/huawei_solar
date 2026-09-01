"""Number entities for Huawei Solar."""

import asyncio
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from huawei_solar import (
    EMMADevice,
    HuaweiSolarDevice,
    RegisterName,
    register_names as rn,
)

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.components.number.const import DEFAULT_MAX_VALUE, DEFAULT_MIN_VALUE
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ENABLE_PARAMETER_CONFIGURATION, DATA_DEVICE_DATAS, STATIC_BOUND_READ_TIMEOUT
from .types import (
    HuaweiSolarConfigEntry,
    HuaweiSolarDeviceData,
    HuaweiSolarEntity,
    HuaweiSolarEntityContext,
    HuaweiSolarEntityDescription,
    HuaweiSolarInverterData,
)
from .update_coordinator import HuaweiSolarUpdateCoordinator

if TYPE_CHECKING:
    from .modbus_guard import ModbusGuard

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class HuaweiSolarNumberEntityDescription(
    HuaweiSolarEntityDescription, NumberEntityDescription
):
    """Huawei Solar Number Entity Description."""

    # Used when the min/max cannot dynamically change
    static_minimum_key: RegisterName | None = None
    static_maximum_key: RegisterName | None = None

    # Used when the min/max is influenced by other parameters
    dynamic_minimum_key: RegisterName | None = None
    dynamic_maximum_key: RegisterName | None = None

    def __post_init__(self) -> None:
        """Defaults the translation_key to the number key."""
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
        if self.dynamic_minimum_key:
            registers.append(self.dynamic_minimum_key)
        if self.dynamic_maximum_key:
            registers.append(self.dynamic_maximum_key)
        return HuaweiSolarEntityContext(register_names=registers)


INVERTER_NUMBER_DESCRIPTIONS: tuple[HuaweiSolarNumberEntityDescription, ...] = (
    HuaweiSolarNumberEntityDescription(
        key=rn.ACTIVE_POWER_PERCENTAGE_DERATING,
        native_max_value=100,
        native_step=0.1,
        native_min_value=-100,
        icon="mdi:transmission-tower-off",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    HuaweiSolarNumberEntityDescription(
        key=rn.ACTIVE_POWER_FIXED_VALUE_DERATING,
        static_maximum_key=rn.P_MAX,
        native_step=1,
        native_min_value=0,
        icon="mdi:transmission-tower-off",
        native_unit_of_measurement=UnitOfPower.WATT,
        entity_category=EntityCategory.CONFIG,
    ),
    HuaweiSolarNumberEntityDescription(
        key=rn.MPPT_SCANNING_INTERVAL,
        native_max_value=30,
        native_step=1,
        native_min_value=5,
        icon="mdi:sun-clock",
        native_unit_of_measurement="minutes",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
)

EMMA_NUMBER_DESCRIPTIONS: tuple[HuaweiSolarNumberEntityDescription, ...] = (
    HuaweiSolarNumberEntityDescription(
        key=rn.EMMA_MAXIMUM_FEED_GRID_POWER_PERCENT,
        native_max_value=100,
        native_step=0.1,
        native_min_value=-100,
        icon="mdi:transmission-tower-off",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    HuaweiSolarNumberEntityDescription(
        key=rn.EMMA_MAXIMUM_FEED_GRID_POWER_WATT,
        static_maximum_key=rn.INVERTER_RATED_POWER,
        native_step=1,
        native_min_value=-1000,
        icon="mdi:transmission-tower-off",
        native_unit_of_measurement=UnitOfPower.WATT,
        entity_category=EntityCategory.CONFIG,
    ),
    HuaweiSolarNumberEntityDescription(
        key=rn.EMMA_TOU_MAXIMUM_POWER_FOR_CHARGING_BATTERIES_FROM_GRID,
        native_min_value=0,
        native_max_value=50000,
        icon="mdi:battery-positive",
        native_unit_of_measurement=UnitOfPower.WATT,
        entity_category=EntityCategory.CONFIG,
    ),
)

ENERGY_STORAGE_NUMBER_DESCRIPTIONS: tuple[HuaweiSolarNumberEntityDescription, ...] = (
    # ── Maximum Charge Power ─────────────────────────────────────────────────
    # Controls the upper bound of battery charging power.  Enabled by default
    # so users can tune this without first having to un-hide the entity.
    HuaweiSolarNumberEntityDescription(
        key=rn.STORAGE_MAXIMUM_CHARGING_POWER,
        native_min_value=0,
        native_step=100,
        static_maximum_key=rn.STORAGE_MAXIMUM_CHARGE_POWER,
        icon="mdi:battery-positive",
        native_unit_of_measurement=UnitOfPower.WATT,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=True,
    ),
    # ── Maximum Discharge Power ──────────────────────────────────────────────
    # Controls the upper bound of battery discharging power.  Enabled by
    # default for the same reason as Maximum Charge Power.
    HuaweiSolarNumberEntityDescription(
        key=rn.STORAGE_MAXIMUM_DISCHARGING_POWER,
        native_min_value=0,
        native_step=100,
        static_maximum_key=rn.STORAGE_MAXIMUM_DISCHARGE_POWER,
        icon="mdi:battery-negative",
        native_unit_of_measurement=UnitOfPower.WATT,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=True,
    ),
    # ── End-of-charge SOC ────────────────────────────────────────────────────
    # The battery will stop charging once it reaches this state-of-charge.
    # Typical range: 90–100 %.  Enabled by default — commonly used in
    # automations to extend battery cycle life.
    HuaweiSolarNumberEntityDescription(
        key=rn.STORAGE_CHARGING_CUTOFF_CAPACITY,
        native_min_value=90,
        native_max_value=100,
        native_step=0.1,
        icon="mdi:battery-charging-high",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=True,
    ),
    HuaweiSolarNumberEntityDescription(
        key=rn.STORAGE_BACKUP_POWER_STATE_OF_CHARGE,
        native_min_value=0,
        native_max_value=100,
        native_step=0.1,
        icon="mdi:battery-negative",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    HuaweiSolarNumberEntityDescription(
        key=rn.STORAGE_GRID_CHARGE_CUTOFF_STATE_OF_CHARGE,
        native_min_value=20,
        native_max_value=100,
        native_step=0.1,
        icon="mdi:battery-charging-50",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.CONFIG,
    ),
    HuaweiSolarNumberEntityDescription(
        key=rn.STORAGE_POWER_OF_CHARGE_FROM_GRID,
        native_min_value=0,
        dynamic_maximum_key=rn.STORAGE_MAXIMUM_POWER_OF_CHARGE_FROM_GRID,
        icon="mdi:battery-negative",
        native_unit_of_measurement=UnitOfPower.WATT,
        entity_category=EntityCategory.CONFIG,
    ),
)

CAPACITY_CONTROL_NUMBER_DESCRIPTIONS: tuple[HuaweiSolarNumberEntityDescription, ...] = (
    HuaweiSolarNumberEntityDescription(
        key=rn.STORAGE_CAPACITY_CONTROL_SOC_PEAK_SHAVING,
        dynamic_minimum_key=rn.STORAGE_DISCHARGING_CUTOFF_CAPACITY,
        native_max_value=100,
        native_step=0.1,
        icon="mdi:battery-arrow-up",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.CONFIG,
    ),
    # this entity has a dynamic maximum value which is only available when capacity control is supported
    HuaweiSolarNumberEntityDescription(
        key=rn.STORAGE_DISCHARGING_CUTOFF_CAPACITY,
        native_min_value=0,
        native_max_value=20,
        dynamic_maximum_key=rn.STORAGE_CAPACITY_CONTROL_SOC_PEAK_SHAVING,
        native_step=0.1,
        icon="mdi:battery-negative",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.CONFIG,
    ),
)


NON_CAPACITY_CONTROL_NUMBER_DESCRIPTIONS: tuple[
    HuaweiSolarNumberEntityDescription, ...
] = (
    # this entity is identical to the one above, but without dynamic maximum.
    HuaweiSolarNumberEntityDescription(
        key=rn.STORAGE_DISCHARGING_CUTOFF_CAPACITY,
        native_min_value=0,
        native_max_value=20,
        native_step=0.1,
        icon="mdi:battery-negative",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HuaweiSolarConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Huawei Solar Number entities Setup."""
    if not entry.data.get(CONF_ENABLE_PARAMETER_CONFIGURATION):
        _LOGGER.info("Skipping number setup, as parameter configuration is not enabled")
        return

    device_datas: list[HuaweiSolarDeviceData] = entry.runtime_data[DATA_DEVICE_DATAS]

    entities_to_add: list[NumberEntity] = []
    for ucs in device_datas:
        if not ucs.configuration_update_coordinator:
            continue
        slave_entities: list[HuaweiSolarNumberEntity] = []
        if isinstance(ucs.device, EMMADevice):
            for entity_description in EMMA_NUMBER_DESCRIPTIONS:
                slave_entities.append(  # noqa: PERF401
                    await HuaweiSolarNumberEntity.create(
                        ucs.configuration_update_coordinator,
                        ucs.device,
                        entity_description,
                        ucs.device_info,
                    )
                )

        if isinstance(ucs, HuaweiSolarInverterData):
            for entity_description in INVERTER_NUMBER_DESCRIPTIONS:
                slave_entities.append(  # noqa: PERF401
                    await HuaweiSolarNumberEntity.create(
                        ucs.configuration_update_coordinator,
                        ucs.device,
                        entity_description,
                        ucs.device_info,
                    )
                )

            if ucs.connected_energy_storage:
                for entity_description in ENERGY_STORAGE_NUMBER_DESCRIPTIONS:
                    slave_entities.append(  # noqa: PERF401
                        await HuaweiSolarNumberEntity.create(
                            ucs.configuration_update_coordinator,
                            ucs.device,
                            entity_description,
                            ucs.device_info,
                        )
                    )

                if ucs.device.supports_capacity_control:
                    _LOGGER.debug(
                        "Adding capacity control number entities on device %s",
                        ucs.device.serial_number,
                    )
                    for entity_description in CAPACITY_CONTROL_NUMBER_DESCRIPTIONS:
                        slave_entities.append(  # noqa: PERF401
                            await HuaweiSolarNumberEntity.create(
                                ucs.configuration_update_coordinator,
                                ucs.device,
                                entity_description,
                                ucs.connected_energy_storage,
                            )
                        )

                else:
                    _LOGGER.debug(
                        "Capacity control not supported on slave %s. Skipping capacity control number entities",
                        ucs.device.serial_number,
                    )
                    for entity_description in NON_CAPACITY_CONTROL_NUMBER_DESCRIPTIONS:
                        slave_entities.append(  # noqa: PERF401
                            await HuaweiSolarNumberEntity.create(
                                ucs.configuration_update_coordinator,
                                ucs.device,
                                entity_description,
                                ucs.connected_energy_storage,
                            )
                        )

        else:
            _LOGGER.debug(
                "No battery detected on slave %s. Skipping energy storage number entities",
                ucs.device.client.unit_id,
            )

        entities_to_add.extend(slave_entities)

    async_add_entities(entities_to_add)


#: v2.0.0b (MOD-13, external ICS audit -- confirmed): each number entity
#: independently read its own static min/max register during platform
#: setup, so startup traffic scaled with entity count even though these
#: are genuinely static device metadata (by definition of the STATIC
#: tier itself, RegisterTier's own model) that cannot change within a
#: session. Keyed by (device serial, register name) -- the same static
#: register could in principle be shared by more than one entity
#: description, and multiple devices on one entry each have their own
#: independent values for the "same-named" register. Deliberately a
#: module-level dict, not per-coordinator: entity platform setup
#: (create(), below) does not have a natural longer-lived object to
#: attach this to, and every real caller already goes through this one
#: module. Cleared on unload (see clear_static_bound_cache()) rather
#: than kept for the life of the Home Assistant process -- a reload can
#: follow a firmware update or hardware swap, and a genuinely stale
#: bound surviving that would be a correctness regression for the sake
#: of an efficiency win that only needs to last one session anyway.
_STATIC_BOUND_CACHE: dict[tuple[str, RegisterName], float | None] = {}


def clear_static_bound_cache(serial_number: str | None = None) -> None:
    """Clear cached static bounds for one device, or all of them.

    v2.0.0b (MOD-13): called from __init__.py's unload path so a reload
    always re-reads static bounds fresh, rather than trusting a value
    that could predate a firmware update or hardware swap.
    """
    if serial_number is None:
        _STATIC_BOUND_CACHE.clear()
        return
    for key in [k for k in _STATIC_BOUND_CACHE if k[0] == serial_number]:
        del _STATIC_BOUND_CACHE[key]


async def _read_static_bound_cached(
    device: HuaweiSolarDevice, key: RegisterName, kind: str,
    guard: "ModbusGuard | None" = None,
) -> float | None:
    """Cached wrapper around _read_static_bound() -- see
    _STATIC_BOUND_CACHE's own module-level comment for the full
    reasoning. A cached ``None`` (a prior read that timed out or failed)
    is deliberately also treated as a valid cache hit, not re-attempted
    per entity -- a device that failed to answer once during setup is
    unlikely to answer a second, third, or fortieth immediately-repeated
    attempt any differently, and retrying it once per entity would
    itself reintroduce the exact traffic-scaling problem this fix exists
    to remove. The next reload gets a fresh attempt regardless (see
    clear_static_bound_cache()).
    """
    cache_key = (device.serial_number, key)
    if cache_key in _STATIC_BOUND_CACHE:
        return _STATIC_BOUND_CACHE[cache_key]
    value = await _read_static_bound(device, key, kind, guard=guard)
    _STATIC_BOUND_CACHE[cache_key] = value
    return value


async def _read_static_bound(
    device: HuaweiSolarDevice, key: RegisterName, kind: str,
    guard: "ModbusGuard | None" = None,
) -> float | None:
    """Bounded, isolated read of a static min/max register.

    v1.3.11 FIX (Defect J). This used to be an inline
    `await device.client.get(key)` with no timeout and no exception
    handling, directly on the NUMBER PLATFORM SETUP critical path
    (HuaweiSolarNumberEntity.create(), called once per number entity before
    async_add_entities() returns). Bounded here to
    STATIC_BOUND_READ_TIMEOUT for the same reason as sensor.py's
    _has_write_permission_bounded (Defect H): a slow or busy device must
    not be able to stall platform setup, and an exception here must never
    propagate -- doing so would take down every number entity on the
    entry, not just this one bound. On timeout or failure the bound is
    simply left unset (identical in effect to the entity's own existing
    "no static key configured" case) rather than blocking or crashing.

    v2.0.0a FIX (F06, external ICS audit -- confirmed): being time-bounded
    protected platform setup from hanging, but the read itself still
    bypassed ModbusGuard entirely -- several number entities each doing
    their own unguarded read during setup could inject frames that collide
    with each other or with a coordinator's own guarded polling. `guard`
    is optional (defaults to None, reproducing the old unguarded read)
    only for callers that genuinely have no coordinator reference yet;
    every production call site in this file has one and passes it.
    """
    try:
        if guard is not None:
            async with guard.request(label="number_static_bound_read"):
                result = await asyncio.wait_for(
                    device.client.get(key), timeout=STATIC_BOUND_READ_TIMEOUT.total_seconds()
                )
        else:
            result = await asyncio.wait_for(
                device.client.get(key), timeout=STATIC_BOUND_READ_TIMEOUT.total_seconds()
            )
        return result.value
    except TimeoutError:
        _LOGGER.warning(
            "static_bound_read[%s/%s]: timed out after %.0fs; entity will "
            "use its default %s bound for this setup. All other entities "
            "are unaffected",
            device.serial_number, key,
            STATIC_BOUND_READ_TIMEOUT.total_seconds(), kind,
        )
        return None
    except Exception:  # noqa: BLE001 — must never break platform setup
        _LOGGER.exception(
            "static_bound_read[%s/%s]: failed; entity will use its default "
            "%s bound for this setup. All other entities are unaffected",
            device.serial_number, key, kind,
        )
        return None


class HuaweiSolarNumberEntity(
    CoordinatorEntity[HuaweiSolarUpdateCoordinator], HuaweiSolarEntity, NumberEntity
):
    """Huawei Solar Number Entity."""

    entity_description: HuaweiSolarNumberEntityDescription
    _attr_mode = NumberMode.BOX  # Always allow a precise number

    _static_min_value: float | None = None
    _static_max_value: float | None = None

    _dynamic_min_value: float | None = None
    _dynamic_max_value: float | None = None

    def __init__(
        self,
        coordinator: HuaweiSolarUpdateCoordinator,
        device: HuaweiSolarDevice,
        description: HuaweiSolarNumberEntityDescription,
        device_info: DeviceInfo,
        static_max_value: float | None = None,
        static_min_value: float | None = None,
    ) -> None:
        """Huawei Solar Number Entity constructor.

        Do not use directly. Use `.create` instead!
        """
        super().__init__(coordinator, description.context)
        self.coordinator = coordinator

        self.device = device
        self.entity_description = description

        self._attr_device_info = device_info
        self._attr_unique_id = f"{device.serial_number}_{description.key}"

        self._static_max_value = static_max_value
        self._static_min_value = static_min_value

    @classmethod
    async def create(
        cls,
        coordinator: HuaweiSolarUpdateCoordinator,
        device: HuaweiSolarDevice,
        description: HuaweiSolarNumberEntityDescription,
        device_info: DeviceInfo,
    ) -> "HuaweiSolarNumberEntity":
        """Huawei Solar Number Entity constructor.

        This async constructor fills in the necessary min/max values
        """

        static_max_value = None
        if description.static_maximum_key:
            # v2.0.0b (MOD-13, external ICS audit -- confirmed): cached,
            # not a fresh read per entity -- see _STATIC_BOUND_CACHE's own
            # module-level comment.
            static_max_value = await _read_static_bound_cached(
                device, description.static_maximum_key, "maximum",
                guard=coordinator.guard,
            )

        static_min_value = None
        if description.static_minimum_key:
            static_min_value = await _read_static_bound_cached(
                device, description.static_minimum_key, "minimum",
                guard=coordinator.guard,
            )

        return cls(
            coordinator,
            device,
            description,
            device_info,
            static_max_value,
            static_min_value,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if (
            self.coordinator.data
            and self.entity_description.key in self.coordinator.data
        ):
            self._attr_native_value = self.coordinator.data[
                self.entity_description.register_name
            ].value

            if self.entity_description.dynamic_minimum_key:
                min_register = self.coordinator.data.get(
                    self.entity_description.dynamic_minimum_key
                )
                # v1.3.11 FIX (Defect J, part 3): previously only assigned
                # when present, so a register that disappears (transient bus
                # issue, or a capability that stops being reported) left the
                # entity advertising a stale bound indefinitely instead of
                # falling back to the entity's static/default bound.
                self._dynamic_min_value = min_register.value if min_register else None

            if self.entity_description.dynamic_maximum_key:
                max_register = self.coordinator.data.get(
                    self.entity_description.dynamic_maximum_key
                )
                self._dynamic_max_value = max_register.value if max_register else None

            # v2.0.0: quality/reason/age attributes -- see _quality_attrs()'s
            # own docstring. Availability (the default CoordinatorEntity
            # behaviour for the success case here, explicitly overridden to
            # False below only when this register is absent) is untouched.
            self._attr_extra_state_attributes = self._quality_attrs(
                self.coordinator, self.entity_description.register_name
            )
        else:
            self._attr_available = False
            self._attr_native_value = None

        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        """Set a new value."""
        # v2.0.0b (MOD-05, external ICS audit -- confirmed): v2.0.0a's F05
        # fix routed this write through ModbusGuard but never bounded the
        # underlying device.set() call itself -- a stalled write held the
        # guard indefinitely. Now uses the shared _guarded_write() helper
        # (types.py), which pairs the guard with WRITE_TIMEOUT.
        wrote = await self._guarded_write(
            self.coordinator.guard, self.device,
            self.entity_description.register_name, float(value),
            label="number_write",
        )
        if wrote:
            self._attr_native_value = float(value)
            # Invalidate the cached register so the next poll fetches a fresh value
            # regardless of what verify_write() below finds -- its own failure
            # path (the write was silently ignored) does not touch the cache at
            # all, so this immediate invalidation is still needed as the
            # baseline guarantee; verify_write() adds an earlier, explicit
            # confirmation/warning on top of it, not a replacement for it.
            self.coordinator.invalidate_cache(self.entity_description.register_name)
            # v2.0.0a (F12, external ICS audit -- confirmed): verify_write()
            # existed, fully built and already guarded, but had zero
            # production callers -- its own docstring says it was designed
            # for exactly this (number/select/switch, after a write), so
            # production semantics never matched what was documented.
            # Fired as a background task, not awaited directly: it takes
            # WRITE_VERIFY_DELAY plus up to WRITE_VERIFY_RETRIES more
            # (~3-9s) to confirm the inverter actually applied the value,
            # and its whole value is a warning log if it silently didn't
            # (an inverter mid state-transition can accept a write and
            # then ignore it) -- not a check worth making the user's
            # slider interaction visibly wait on.
            #
            # v2.0.0b (MOD-10, external ICS audit -- confirmed): this used
            # to be a bare self.hass.async_create_task(...) -- a task that
            # could survive an entry reload and perform a delayed Modbus
            # read against stale lifecycle state. Now entry-scoped via the
            # coordinator's own create_background_task(), the same pattern
            # already used for the deferred first-poll task.
            #
            # v2.0.9 FIX (Phase 4.7, this release -- old DEF-010, external
            # ICS quality/defect/architecture audit -- confirmed): now
            # routed through schedule_verify_write() instead of create_
            # background_task() directly -- a rapid second write to this
            # SAME register (e.g. dragging this slider) cancels the
            # PREVIOUS write's own still-running verification, which
            # would otherwise complete a real Modbus read to verify a
            # value this new write has already superseded. See schedule_
            # verify_write()'s own docstring for the full reasoning.
            self.coordinator.schedule_verify_write(
                self.entity_description.register_name, float(value)
            )

        await self.coordinator.async_request_refresh()

    @property
    def native_max_value(self) -> float:
        """Maximum value, possibly determined dynamically using _dynamic_max_value."""
        native_max_value = (
            self._static_max_value or self.entity_description.native_max_value
        )

        if self._dynamic_max_value:
            if native_max_value:
                return min(self._dynamic_max_value, native_max_value)
            return self._dynamic_max_value

        if native_max_value:
            return native_max_value
        return DEFAULT_MAX_VALUE

    @property
    def native_min_value(self) -> float:
        """Minimum value, possibly determined dynamically using _dynamic_min_value."""
        native_min_value = (
            self._static_min_value or self.entity_description.native_min_value
        )

        if self._dynamic_min_value:
            if native_min_value:
                return max(self._dynamic_min_value, native_min_value)
            return self._dynamic_min_value

        if native_min_value:
            return native_min_value
        return DEFAULT_MIN_VALUE
