"""The Huawei Solar integration."""

import asyncio
from collections.abc import Callable
import inspect
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
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo

from .const import (
    CONF_BH_ENABLED,
    CONF_SLOW_TIER_TTL_S,
    DEFAULT_SLOW_TIER_TTL_S,
    CONF_ENABLE_PARAMETER_CONFIGURATION,
    CONF_SLAVE_IDS,
    CONFIGURATION_UPDATE_INTERVAL,
    DATA_DEVICE_DATAS,
    DATA_SYNC_POWER_COORDINATOR,
    DEVICE_CONNECT_TIMEOUT,
    DISCONNECT_TIMEOUT,
    DOMAIN,
    ENERGY_STORAGE_UPDATE_INTERVAL,
    INVERTER_UPDATE_INTERVAL,
    OPTIMIZER_DISCOVERY_TIMEOUT,
    OPTIMIZER_UPDATE_INTERVAL,
    POWER_METER_UPDATE_INTERVAL,
    SYNC_POWER_UPDATE_INTERVAL,
)
from .adaptive_modbus import AdaptiveModbusController
from .battery_health_manager import BatteryHealthManager
from .modbus_guard import ModbusGuard
from .register_cache import set_slow_tier_ttl
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

# v1.3.10 FIX (Defect I): the offsets above stagger the FOUR COORDINATOR
# TYPES within one device's first poll -- they say nothing about a SECOND
# device sharing the same physical bus. Daisy-chained inverters (and any
# other multi-device installation on one ModbusGuard endpoint) reuse these
# exact same per-type offsets for every device, so e.g. device 0's and
# device 1's "configuration" coordinators both wake for their first poll at
# +10s, both "energy_storage" coordinators both at +14s, and so on --
# guaranteeing a same-type collision on every reload/restart, once per
# coordinator type, regardless of how many devices share the bus.
#
# Confirmed directly from a debug capture of a real two-inverter reload:
# ModbusGuard's *adaptive, learned* queue depth (from 71 days of real
# history) was 1 at the time -- correctly so for this bus's steady-state
# traffic -- and the resulting shed+10s-retry cycles on the colliding
# config/battery coordinators cost ~20s each, accounting for the bulk of
# the multi-minute startup window (see AUDIT_1.3.10.md). Deliberately NOT
# fixed by widening the queue depth: that value is adaptively learned from
# real steady-state conditions and overriding it globally would change
# behaviour for the other 99.9% of the time to paper over a ~20s startup-
# only collision -- the same class of mistake this project's adaptive
# layer exists to avoid (see the queue-depth learning in adaptive_modbus.py
# and MAX_QUEUE_DEPTH's docstring in modbus_guard.py).
#
# Fixed instead by adding a per-device offset on top of the per-type one,
# so each additional device sharing a bus gets its own, non-overlapping
# stagger window. 5s chosen to comfortably clear a single coordinator's
# first-poll exchange (observed well under 1s when healthy) while keeping
# the total spread modest even with several daisy-chained devices.
_MULTI_DEVICE_STAGGER_STRIDE = timedelta(seconds=5)


def _staggered_start_delay(kind: str, device_index: int) -> timedelta:
    """First-poll stagger for coordinator *kind* on the *device_index*-th
    device sharing this entry's bus (0 = primary device)."""
    return _COORDINATOR_START_DELAYS[kind] + device_index * _MULTI_DEVICE_STAGGER_STRIDE

_LOGGER = logging.getLogger(__name__)


async def _run_cleanup_callbacks(callbacks: list[Callable[[], object]]) -> None:
    """Run every accumulated cleanup callback, isolating failures so one
    callback raising never prevents the rest from running.

    v1.3.18 FIX (Defect U, from an independent ICS audit of v1.3.17): a
    partially-completed setup attempt that had already started long-lived
    resources for one or more devices (currently: the keep-alive background
    task started in `_setup_inverter_device_data`) had no way to roll those
    resources back if a LATER step in the SAME setup attempt then failed --
    e.g. a second daisy-chained device timing out during discovery, after
    the first device's keep-alive task was already running. Every existing
    exception handler in `async_setup_entry` only ever called
    `primary_device.stop()`; nothing tore down keep-alive tasks already
    started for devices that came before the failure.
    
    This matters because Home Assistant does not guarantee
    `async_unload_entry()` runs after a failed `async_setup_entry()` -- so
    an orphaned background task could survive to interfere with the next
    setup attempt for the same device (a duplicate keep-alive loop running
    against a device object the next attempt no longer has a reference to).
    
    Runs callbacks in reverse registration order (last-created, first torn
    down -- the usual teardown convention) and supports both sync and
    async callables, though the one callback registered today
    (`ModbusKeepAlive.stop`) is synchronous.
    """
    for cb in reversed(callbacks):
        try:
            result = cb()
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 — one failing cleanup must not block the rest
            _LOGGER.exception(
                "Error while cleaning up a resource after a failed setup attempt"
            )

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
    # v1.3.18 FIX (Defect U): accumulates cleanup callbacks for long-lived
    # resources (currently: keep-alive tasks) started for devices already
    # set up successfully in THIS attempt, so they can be torn down if a
    # LATER device or step in the same attempt fails -- see
    # _run_cleanup_callbacks for the full reasoning.
    cleanup_callbacks: list[Callable[[], object]] = []
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

        # v1.3.14 FIX (Defect M): bound this call ourselves so we give up,
        # cleanly, before Home Assistant's own external setup timeout can
        # cancel us mid-connection (see const.DEVICE_CONNECT_TIMEOUT for
        # the full reasoning and the field evidence behind this bound).
        try:
            primary_device = await asyncio.wait_for(
                create_device_instance(client),
                timeout=DEVICE_CONNECT_TIMEOUT.total_seconds(),
            )
        except TimeoutError as err:
            host = entry.data.get(CONF_HOST) or entry.data.get(CONF_PORT)
            _LOGGER.warning(
                "Connecting to and identifying the inverter at %s took "
                "longer than %.0fs. The device may still be completing "
                "its own reconnect after a previous session ended; this "
                "will be retried automatically",
                host, DEVICE_CONNECT_TIMEOUT.total_seconds(),
            )
            raise ConfigEntryNotReady(
                f"Timed out connecting to and identifying the inverter at "
                f"{host} after {DEVICE_CONNECT_TIMEOUT.total_seconds():.0f}s. "
                "It may still be finishing its own reconnect; this will be "
                "retried automatically."
            ) from err

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
                # v1.3.18 FIX (Defect U/Finding 1, independent ICS audit of
                # v1.3.17): this login call had no bound of its own. On a
                # slow or still-reconnecting device, exactly the same
                # setup-timeout risk as Defect M (v1.3.14) applied here too
                # -- just for the login handshake instead of the initial
                # connection. Bounded with the same DEVICE_CONNECT_TIMEOUT
                # used for that call, converting a timeout into a clean
                # ConfigEntryNotReady rather than leaving it exposed to
                # an external cancellation.
                try:
                    await asyncio.wait_for(
                        primary_device.login(
                            entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD]
                        ),
                        timeout=DEVICE_CONNECT_TIMEOUT.total_seconds(),
                    )
                except InvalidCredentials as err:
                    raise ConfigEntryAuthFailed from err
                except TimeoutError as err:
                    _LOGGER.warning(
                        "Logging in to the inverter at %s took longer than "
                        "%.0fs. The device may still be completing its own "
                        "reconnect; this will be retried automatically",
                        entry.data.get(CONF_HOST) or entry.data.get(CONF_PORT),
                        DEVICE_CONNECT_TIMEOUT.total_seconds(),
                    )
                    raise ConfigEntryNotReady(
                        f"Timed out logging in to the inverter after "
                        f"{DEVICE_CONNECT_TIMEOUT.total_seconds():.0f}s. "
                        "It may still be finishing its own reconnect; this "
                        "will be retried automatically."
                    ) from err

        primary_device_data = await _setup_device_data(
            hass,
            entry,
            primary_device,
            bus_endpoint=bus_endpoint,
            device_index=0,
            register_cleanup=cleanup_callbacks.append,
        )

        device_datas: list[HuaweiSolarDeviceData] = [primary_device_data]

        for device_index, extra_unit_id in enumerate(entry.data[CONF_SLAVE_IDS][1:], start=1):
            # v1.3.18 FIX (Defect U/Finding 1): sub-device discovery had no
            # bound either, and the loop is sequential -- one slow slave
            # could stall discovery of every later one indefinitely. Same
            # DEVICE_CONNECT_TIMEOUT bound and ConfigEntryNotReady
            # conversion as the primary device's own connection (Defect M).
            try:
                sub_device = await asyncio.wait_for(
                    create_sub_device_instance(primary_device, extra_unit_id),
                    timeout=DEVICE_CONNECT_TIMEOUT.total_seconds(),
                )
            except TimeoutError as err:
                _LOGGER.warning(
                    "Discovering slave device %s took longer than %.0fs. "
                    "The bus may still be settling after a reconnect; this "
                    "will be retried automatically",
                    extra_unit_id, DEVICE_CONNECT_TIMEOUT.total_seconds(),
                )
                raise ConfigEntryNotReady(
                    f"Timed out discovering slave device {extra_unit_id} "
                    f"after {DEVICE_CONNECT_TIMEOUT.total_seconds():.0f}s. "
                    "This will be retried automatically."
                ) from err
            # sub_device shares the same physical RS485 bus as primary_device
            # — passing bus_endpoint gives it the same ModbusGuard instance.
            # device_index (v1.3.10) staggers this device's first-poll timing
            # away from the primary device's, since they share one guard —
            # see _staggered_start_delay / Defect I.
            sub_device_data = await _setup_device_data(
                hass, entry, sub_device, bus_endpoint=bus_endpoint,
                device_index=device_index, register_cleanup=cleanup_callbacks.append,
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
                    bus_endpoint=bus_endpoint,
                )
                # Attach INV1's telemetry so sync-coordinator reads are counted
                # in the same Modbus traffic dashboard as the other coordinators.
                telemetry = ModbusTelemetry.get(inv1_data.device.serial_number)
                if telemetry:
                    sync_coordinator.attach_telemetry(telemetry)

                # v1.3.8 FIX (Defect G): this used to be `await
                # sync_coordinator.async_config_entry_first_refresh()` -- a
                # full, blocking read of every instantaneous power register
                # across up to two inverters plus meter/battery, awaited
                # directly on the entry setup critical path. This is the
                # single largest identified contributor to the 2-3 minute
                # "waiting for Huawei Solar to start up" window Home
                # Assistant shows on every boot AND every reload of this
                # entry (see AUDIT_1.3.8.md) -- and, per the same audit, a
                # plausible reason a slow/cancelled setup could leave a
                # later-created coordinator (configuration_update_coordinator,
                # created after this point in the per-device setup sequence)
                # never constructed at all.
                #
                # Fixed the same way as the optimizer coordinator just above
                # and battery-health init (_async_setup_battery_health):
                # schedule the real first refresh as a background task
                # instead of blocking setup on it. Entities fed by this
                # coordinator show unavailable until it completes, exactly
                # like every other coordinator in this integration already
                # behaves without complaint.
                async def _sync_first_refresh(
                    coord: SynchronizedPowerCoordinator = sync_coordinator,
                ) -> None:
                    try:
                        await coord.async_config_entry_first_refresh()
                    except Exception:  # noqa: BLE001 — background task must not raise
                        _LOGGER.exception(
                            "SynchronizedPowerCoordinator: first refresh failed; "
                            "synchronised power-flow sensors will report unknown "
                            "until the next scheduled poll. All other entities "
                            "are unaffected"
                        )

                try:
                    create_task = getattr(entry, "async_create_background_task", None)
                    if create_task is not None:
                        create_task(
                            hass, _sync_first_refresh(),
                            "sync_power_coordinator_first_refresh",
                        )
                    else:  # pragma: no cover — older HA cores
                        hass.async_create_task(_sync_first_refresh())
                except Exception:  # noqa: BLE001 — never break entry setup
                    _LOGGER.exception(
                        "SynchronizedPowerCoordinator: could not schedule "
                        "first refresh"
                    )

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

        # ── Battery Health managers (v1.1.5; fault-isolated in v1.1.7) ──────
        # One read-only health engine per inverter with a connected battery.
        #
        # ISOLATION CONTRACT (v1.1.7): this subsystem is strictly additive and
        # must never be able to delay, cancel, or fail config-entry setup.
        # Nothing here is awaited on the setup critical path and every failure
        # mode is swallowed and logged.  See _async_setup_battery_health().
        # v1.3.3: apply the configured SLOW-tier refresh interval before any
        # polling starts, so the first cycle already uses it.
        try:
            set_slow_tier_ttl(
                entry.options.get(CONF_SLOW_TIER_TTL_S, DEFAULT_SLOW_TIER_TTL_S)
            )
        except Exception:  # noqa: BLE001 — never break setup over a tunable
            _LOGGER.exception("Could not apply SLOW-tier TTL option")

        # v1.3.4's coalesce/night-defer options were REMOVED in v1.3.5
        # (see const.py) after causing a production outage. Nothing to apply
        # here any more.

        _async_setup_battery_health(hass, entry, device_datas)
        # v1.2.2: gate BOTH learners across HA start-up and shutdown.
        try:
            _async_register_learning_gates(
                hass, entry, [d.device.serial_number for d in device_datas]
            )
        except Exception:  # noqa: BLE001 — must never break entry setup
            _LOGGER.exception(
                "Failed to register learning gates; learning will proceed "
                "without start-up suppression"
            )
    except ConnectionInterruptedException as err:
        await _run_cleanup_callbacks(cleanup_callbacks)
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
        await _run_cleanup_callbacks(cleanup_callbacks)
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
        await _run_cleanup_callbacks(cleanup_callbacks)
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
        await _run_cleanup_callbacks(cleanup_callbacks)
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
        # v1.3.18 FIX (Defect U): also tear down any long-lived resources
        # (keep-alive tasks) already started for devices that succeeded
        # earlier in this same, now-failed, setup attempt.
        await _run_cleanup_callbacks(cleanup_callbacks)
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


def _async_register_learning_gates(
    hass: HomeAssistant,
    entry: HuaweiSolarConfigEntry,
    serials: list[str],
) -> None:
    """Suspend both learners across Home Assistant start-up and shutdown.

    Keyed on Home Assistant's OWN lifecycle events rather than on integration
    setup time.  Integration setup routinely completes while HA is still
    grinding through recorder migration and other integrations, so a window
    measured from setup could expire before the congestion does - defeating
    the purpose.

    During those windows Modbus round-trip times and timeouts reflect Home
    Assistant, not the inverter.  The adaptive controller cannot distinguish
    the two, and restarts are not uniformly distributed across the day
    (scheduled updates, evening maintenance), so the same circadian slots
    would be poisoned repeatedly.

    Nothing stops POLLING here - only learning from what is observed.
    """

    def _settle(reason: str) -> None:
        for serial in serials:
            controller = AdaptiveModbusController.get(serial)
            if controller is not None:
                controller.mark_recovery(reason)
            manager = BatteryHealthManager.get(serial)
            if manager is not None:
                manager.engine.mark_recovery(reason)

    def _suppress(reason: str) -> None:
        for serial in serials:
            controller = AdaptiveModbusController.get(serial)
            if controller is not None:
                controller.suppress_indefinitely(reason)

    # v1.3.7 FIX (Defect F / §2.3): hass.bus.async_listen_once() already
    # self-unsubscribes the INSTANT its event fires -- that is the whole
    # point of "once". Handing its unsub callable straight to
    # entry.async_on_unload() means that callable gets invoked a SECOND time
    # whenever the entry unloads/reloads AFTER the event already fired (the
    # common case: HA finishes starting long before most reloads, and the
    # STOP listener below is re-armed fresh on every setup). The second
    # removal hits an already-empty listener slot and HA logs "Unable to
    # remove unknown job listener" -- and because this runs inside the
    # entry's unload sequence, an unhandled exception here can abort
    # whatever unload/setup work for this entry hadn't completed yet, which
    # is a plausible mechanism for a partial, one-coordinator-only failure
    # to restart on reload (see the still-open §2.2 outage investigation).
    #
    # Fix: track whether the event already fired and skip the redundant
    # removal in that case. `entry.async_on_unload` still gets a callable
    # (so unload during the OTHER case -- entry unloaded before the event
    # ever fired -- still cleanly cancels the pending listener).
    def _guarded_once(event_type: str, on_fire) -> None:
        fired = False

        @callback
        def _wrapped(event) -> None:
            nonlocal fired
            fired = True
            on_fire(event)

        unsub = hass.bus.async_listen_once(event_type, _wrapped)

        def _remove() -> None:
            if not fired:
                unsub()

        entry.async_on_unload(_remove)

    if hass.state is not CoreState.running:
        # Set up during HA start-up: hold learning until HA reports ready,
        # then settle from THAT point.
        _suppress("home assistant still starting")
        _guarded_once(
            EVENT_HOMEASSISTANT_STARTED, lambda _event: _settle("home assistant started")
        )
    else:
        _settle("integration (re)loaded")

    _guarded_once(
        EVENT_HOMEASSISTANT_STOP,
        # Components unload in order and Modbus can fail while the loop
        # winds down; those failures are artefacts, not inverter behaviour.
        lambda _event: _suppress("home assistant stopping"),
    )


def _async_setup_battery_health(
    hass: HomeAssistant,
    entry: HuaweiSolarConfigEntry,
    device_datas: list[HuaweiSolarDeviceData],
) -> None:
    """Create battery-health managers without touching the setup critical path.

    Fault-isolation rules (v1.1.7), in order of importance:

    1. **Never awaited during setup.**  ``async_initialize()`` performs disk
       I/O (Store load) and attaches a coordinator listener.  In v1.1.5/v1.1.6
       it was awaited inline in ``async_setup_entry``; if it were slow while
       the Modbus link was already struggling, it added time to a path Home
       Assistant itself will cancel on timeout — and a cancelled platform
       setup takes down *all* of the integration's entities, not just this
       subsystem's.  It now runs as a background task.
    2. **Every exception is contained.**  A failure creating or initialising a
       manager leaves that inverter simply without health sensors; the rest of
       the integration is unaffected.
    3. **User-visible kill switch.**  Setting the ``bh_enabled`` option to
       False skips the subsystem entirely.

    Manager construction itself is pure object creation (no I/O), so it stays
    synchronous — the sensor/button platforms need ``BatteryHealthManager.get``
    to resolve while they set up.
    """
    if not entry.options.get(CONF_BH_ENABLED, True):
        _LOGGER.info(
            "Battery health subsystem disabled by configuration option; "
            "skipping setup"
        )
        return

    for device_data in device_datas:
        if not (
            isinstance(device_data, HuaweiSolarInverterData)
            and device_data.energy_storage_update_coordinator is not None
            and device_data.connected_energy_storage is not None
        ):
            continue

        serial = device_data.device.serial_number
        try:
            bh_manager = BatteryHealthManager.create(
                hass,
                serial,
                device_data.energy_storage_update_coordinator,
                device_data.connected_energy_storage,
                dict(entry.options),
            )
        except Exception:  # noqa: BLE001 — never break entry setup
            _LOGGER.exception(
                "battery_health[%s]: manager creation failed; battery health "
                "sensors will be unavailable for this inverter. All other "
                "entities are unaffected",
                serial,
            )
            BatteryHealthManager.remove(serial)
            continue

        async def _initialize(manager: BatteryHealthManager = bh_manager) -> None:
            try:
                await manager.async_initialize()
            except Exception:  # noqa: BLE001 — background task must not raise
                _LOGGER.exception(
                    "battery_health[%s]: initialisation failed; health sensors "
                    "will report unknown. All other entities are unaffected",
                    manager.serial_number,
                )

        try:
            create_task = getattr(entry, "async_create_background_task", None)
            if create_task is not None:
                create_task(hass, _initialize(), f"battery_health_init_{serial}")
            else:  # pragma: no cover — older HA cores
                hass.async_create_task(_initialize())
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "battery_health[%s]: could not schedule initialisation", serial
            )


async def async_unload_entry(
    hass: HomeAssistant, entry: HuaweiSolarConfigEntry
) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        device_datas: list[HuaweiSolarDeviceData] = entry.runtime_data[DATA_DEVICE_DATAS]
        primary_device = device_datas[0].device

        # v1.3.18 FIX (Defect U/Finding 3, independent ICS audit of
        # v1.3.17): this used to be a bare `await
        # primary_device.client.disconnect()`, with no timeout, sitting
        # BEFORE every teardown loop below (telemetry, the adaptive
        # controller, keep-alive, battery health, the shared guard). A
        # wedged or half-dead transport could block here indefinitely,
        # preventing ALL of that cleanup from ever running -- turning an
        # unload-time transport problem into a stuck reload/config-change
        # for the entire entry. Bounded with a short timeout, and any
        # failure (timeout or otherwise) is logged and swallowed so
        # teardown below always proceeds regardless of whether disconnect
        # actually succeeded.
        try:
            await asyncio.wait_for(
                primary_device.client.disconnect(),
                timeout=DISCONNECT_TIMEOUT.total_seconds(),
            )
        except Exception:  # noqa: BLE001 — never let a stuck disconnect block teardown
            _LOGGER.exception(
                "Error disconnecting from the inverter during unload; "
                "continuing with entry teardown regardless"
            )

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

            # v1.3.19 FIX (Defect V/Finding 10, independent ICS audit): the
            # old sync stop() only ever scheduled the dirty-state flush as
            # a fire-and-forget background task, which could be cancelled
            # or simply never run before teardown finished. async_unload()
            # awaits the flush deterministically. Same fault-isolation
            # pattern already used for battery_health just below: a failed
            # flush must never prevent the rest of the entry from
            # unloading cleanly.
            controller = AdaptiveModbusController.get(serial)
            if controller:
                try:
                    await controller.async_unload()
                except Exception:  # noqa: BLE001
                    _LOGGER.exception(
                        "adaptive[%s]: unload failed; continuing with "
                        "entry teardown", serial,
                    )
                    controller.stop()
            AdaptiveModbusController.remove(serial)

            keepalive = ModbusKeepAlive.get(serial)
            if keepalive:
                keepalive.stop()
            ModbusKeepAlive.remove(serial)

            # Fault isolation (v1.1.7): a failed state flush must never
            # prevent the rest of the entry from unloading cleanly — a stuck
            # unload blocks reloads and config changes for the whole entry.
            bh_manager = BatteryHealthManager.get(serial)
            if bh_manager:
                try:
                    await bh_manager.async_unload()
                except Exception:  # noqa: BLE001
                    _LOGGER.exception(
                        "battery_health[%s]: unload failed; continuing with "
                        "entry teardown", serial,
                    )
                    bh_manager.stop()
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
    device_index: int = 0,
    register_cleanup: Callable[[Callable[[], object]], None] | None = None,
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
        start_delay=_staggered_start_delay("main", device_index),
        bus_endpoint=bus_endpoint,
            entry=entry,
    )

    # Create telemetry singleton and attach to the main coordinator.
    # All sub-coordinators (power meter, battery, config) for this inverter
    # share the same singleton so all Modbus traffic is aggregated.
    telemetry = ModbusTelemetry.get_or_create(
        hass, device.serial_number, inverter_device_info
    )
    update_coordinator.attach_telemetry(telemetry)
    # v1.3.19 FIX (Defect V/Finding 1, independent ICS audit): v1.3.18's
    # Defect U only registered keepalive.stop for cleanup-on-setup-failure,
    # reasoning that telemetry and the adaptive controller were "idempotent
    # singletons with no independent ongoing work" -- which turned out to
    # be wrong for both: telemetry.stop() cancels a real periodic timer,
    # and (see below) the adaptive controller manages real background save
    # tasks and persisted state. Registered here for symmetry with what
    # async_unload_entry already does for a successful unload.
    if register_cleanup is not None:
        register_cleanup(telemetry.stop)

    # Create the circadian adaptive learning controller and load persisted
    # statistics from HA storage.  All coordinators for this inverter share
    # one controller so every Modbus request contributes to the same model.
    adaptive = AdaptiveModbusController.get_or_create(
        hass, device.serial_number, inverter_device_info
    )
    await adaptive.async_load()
    update_coordinator.attach_adaptive(adaptive)
    # v1.3.19 FIX (Defect V/Finding 1): registered for the same reason as
    # telemetry above. Uses async_unload() (Defect V/Finding 10 -- flushes
    # any dirty learning state deterministically) rather than the plain
    # sync stop(), since _run_cleanup_callbacks already supports awaiting
    # async callables and the more reliable option costs nothing extra here.
    if register_cleanup is not None:
        register_cleanup(adaptive.async_unload)

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
    # v1.3.18 FIX (Defect U): register this task for teardown if a LATER
    # step in the same setup attempt fails -- without this, a second
    # daisy-chained device timing out during discovery would leave this
    # device's keep-alive loop running indefinitely, orphaned, since Home
    # Assistant does not guarantee async_unload_entry() runs after a failed
    # async_setup_entry() (see _run_cleanup_callbacks for the full
    # reasoning).
    if register_cleanup is not None:
        register_cleanup(keepalive.stop)

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
            start_delay=_staggered_start_delay("power_meter", device_index),
            bus_endpoint=bus_endpoint,
            entry=entry,
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
            start_delay=_staggered_start_delay("energy_storage", device_index),
            bus_endpoint=bus_endpoint,
            entry=entry,
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
            # v1.3.14 FIX (Defect N): bounded so a slow/still-reconnecting
            # device can't hold entry setup open indefinitely on this one
            # discovery scan (see const.OPTIMIZER_DISCOVERY_TIMEOUT).
            optimizer_system_infos = await asyncio.wait_for(
                device.get_optimizer_system_information_data(),
                timeout=OPTIMIZER_DISCOVERY_TIMEOUT.total_seconds(),
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
                entry=entry,
            )
            optimizer_update_coordinator.attach_telemetry(telemetry)
            optimizer_update_coordinator.attach_adaptive(adaptive)
        except TimeoutError:
            _LOGGER.warning(
                "%s: optimizer discovery took longer than %.0fs; skipping "
                "optimizer entities for this setup. Will be retried on the "
                "next reload. All other entities are unaffected",
                device.serial_number, OPTIMIZER_DISCOVERY_TIMEOUT.total_seconds(),
            )
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
            start_delay=_staggered_start_delay("configuration", device_index),
            bus_endpoint=bus_endpoint,
            entry=entry,
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
    device_index: int = 0,
    register_cleanup: Callable[[Callable[[], object]], None] | None = None,
) -> HuaweiSolarDeviceData:
    """Create the correct DeviceInfo-objects, which can be used to correctly assign to entities in this integration."""
    if isinstance(device, SUN2000Device):
        return await _setup_inverter_device_data(
            hass, entry, device, None, bus_endpoint=bus_endpoint, device_index=device_index,
            register_cleanup=register_cleanup,
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
        entry=entry,
    )

    if entry.data.get(CONF_ENABLE_PARAMETER_CONFIGURATION, False):
        configuration_update_coordinator = HuaweiSolarUpdateCoordinator(
            hass,
            _LOGGER,
            device=device,
            name=f"{device.serial_number}_config_data_update_coordinator",
            update_interval=CONFIGURATION_UPDATE_INTERVAL,
            entry=entry,
        )
    else:
        configuration_update_coordinator = None

    return HuaweiSolarDeviceData(
        device=device,
        device_info=device_info,
        update_coordinator=update_coordinator,
        configuration_update_coordinator=configuration_update_coordinator,
    )
