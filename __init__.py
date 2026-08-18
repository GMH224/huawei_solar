"""The Huawei Solar integration."""

import asyncio
from collections.abc import Callable
import inspect
import logging
from datetime import timedelta
from typing import Any

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
    CONF_SYNC_POWER_DEDICATED_READS,
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
from .bus_diagnostics import BusDiagnostics
from .number import clear_static_bound_cache
from .telemetry_capture import TelemetryCapture
from .services import async_setup_services, async_unload_services
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
    # v2.0.0b (MOD-03, external ICS audit -- confirmed): SyncPower's first
    # refresh used to fire immediately, with no stagger slot of its own --
    # exactly when the regular per-device caches are coldest, guaranteeing
    # its cache-shortcut (§8.2) misses and triggering the dedicated-read
    # fallback at the worst possible moment, adding uncoordinated traffic
    # on top of an already carefully sequenced startup. Positioned after
    # energy_storage's 14s slot (the latest of the other four) rather than
    # picking an earlier one: by 16s, every regular coordinator has had a
    # real chance to complete its own first poll (typically well under 1s
    # per-exchange when healthy, per this dict's own established
    # reasoning), so SyncPower's now-cache-first reads (MOD-01) are far
    # more likely to find the regular caches already populated -- turning
    # even its "fallback" path into mostly cache hits, not fresh physical
    # reads, rather than merely delaying the same worst-case traffic.
    "sync_power":    timedelta(seconds=16),
    # v2.0.7 FIX (START-01, ICS quality audit -- confirmed): the optimizer
    # coordinator's first refresh previously had no stagger slot of its
    # own at all -- it's a SIBLING class of HuaweiSolarUpdateCoordinator
    # (see update_coordinator.py's own HuaweiSolarOptimizerUpdateCoordinator
    # docstring), not a subclass, so it never inherited _start_delay or
    # the first-poll stagger mechanism the other four coordinator types
    # get automatically. Its background first-refresh task could
    # therefore fire at t=0, alongside the main inverter's own first
    # poll, the exact startup contention this whole stagger scheme exists
    # to avoid. Positioned after sync_power's own 16s slot -- optimizer
    # traffic is a distinct register domain from the other five (not
    # cache-shortcut dependent the way sync_power is), so there's no
    # sync_power-style reason to place it earlier; simply keeping it
    # after everything else already scheduled is enough to avoid
    # colliding with the rest of this device's own startup sequence.
    "optimizer":     timedelta(seconds=18),
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
# stagger window.
#
# v2.0.3 FIX (ICS-04, external ICS audit -- confirmed): this stride was
# 5s -- comfortably clearing a SINGLE coordinator's own first-poll
# exchange, but smaller than the 16s SPAN the five coordinator-type
# offsets above already occupy (0s to 16s). That meant a per-device
# offset of N*5s could land inside an EARLIER device's own 0-16s window
# rather than strictly after it -- e.g. device 2's "main" (0+2*5=10s)
# landing exactly on device 0's own "configuration" (10s). The audit's
# own recommendation was a full bus-wide scheduler; a much smaller
# change closes the same defect just as completely here: this stride
# only needs to be strictly greater than the maximum offset in
# _COORDINATOR_START_DELAYS (16s) for every device's entire 5-slot
# window to be non-overlapping with every other device's, for ANY
# number of devices -- not just "not too small for one coordinator",
# but "not too small for one device's WHOLE set of coordinators". 20s
# was chosen as a round number with a few seconds of margin above that
# 16s minimum, not tuned to any specific number of devices -- the
# guarantee holds regardless. This invariant (stride > max offset) is
# checked directly in tests/test_multi_device_stagger.py, not just
# assumed here, so a future change to either value that reopens this
# same class of collision is caught rather than silently reintroduced.
_MULTI_DEVICE_STAGGER_STRIDE = timedelta(seconds=20)


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

        # v2.0.9 FIX (DEF-001, external ICS quality/defect/architecture
        # audit -- confirmed Critical): bus_endpoint only ever needed
        # entry.data (confirmed by reading endpoint_for()'s own
        # signature) -- it never needed the client or device object at
        # all, so there was no real reason for this to run AFTER the
        # client/device were created rather than before. Moved to the
        # very top of connection setup, before any Modbus I/O whatsoever
        # (including the identification read below), so the guard is
        # acquired -- and the identification read itself is genuinely
        # serialized against any OTHER entry already using this same
        # physical endpoint -- before this entry ever touches the bus.
        # Previously, a second entry starting on the same endpoint while
        # an existing entry was polling could produce genuinely unguarded
        # concurrent Modbus traffic during exactly this identification
        # window.
        bus_endpoint = ModbusGuard.endpoint_for(dict(entry.data))
        _LOGGER.debug("Bus endpoint: %s", bus_endpoint)

        # v2.0.0a (F04, external ICS audit): acquire this entry's reference
        # to the endpoint's guard HERE, once, before any coordinator is
        # constructed -- not implicitly via each coordinator's own
        # get_or_create() call, which does not affect the reference count.
        # Every coordinator below shares this SAME acquired guard for the
        # duration of this entry's lifetime; the matching release_endpoint()
        # call happens exactly once, in async_unload_entry, regardless of
        # how many coordinators this entry created on this endpoint.
        guard = ModbusGuard.acquire_endpoint(bus_endpoint)
        # Registered via the SAME cleanup mechanism Defect U already built
        # for exactly this situation: HA does not guarantee
        # async_unload_entry() runs after a failed async_setup_entry(), so
        # a setup attempt that acquires the endpoint and then fails partway
        # through (e.g. a second daisy-chained device timing out) must still
        # release its reference, or the count leaks permanently and the
        # guard for this endpoint can never be cleaned up even after every
        # real entry using it is gone. Registered FIRST, immediately after
        # the acquire -- _run_cleanup_callbacks tears down in REVERSE
        # registration order, so this correctly runs LAST, after every
        # other cleanup (e.g. stopping keep-alive tasks) that might still
        # need the guard to exist while it does its own teardown.
        cleanup_callbacks.append(lambda: ModbusGuard.release_endpoint(bus_endpoint))

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
        #
        # v2.0.9 FIX (DEF-001, same audit -- confirmed): the actual
        # identification I/O now happens inside `async with guard.
        # request(...)` -- the fix described above; this is the point
        # where it actually takes effect. A one-time setup cost (holding
        # the guard for this whole call, rather than per-request pacing)
        # is appropriate here since nothing else on THIS entry could
        # possibly be contending for the bus yet -- no coordinator exists
        # until after this succeeds.
        try:
            async with guard.request(label="setup_identify"):
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
            # v2.0.9 FIX (DEF-002, same audit -- confirmed): the raw
            # client -- which may already hold a genuinely open TCP/
            # serial connection at this point -- was previously never
            # explicitly disconnected on this failure path, left to
            # eventual garbage collection instead of a deterministic
            # close. For an RTU/serial port specifically, an unreleased
            # handle can block every subsequent connection attempt to
            # that same port, not just this one's own retry.
            await _bounded_client_disconnect(client)
            raise ConfigEntryNotReady(
                f"Timed out connecting to and identifying the inverter at "
                f"{host} after {DEVICE_CONNECT_TIMEOUT.total_seconds():.0f}s. "
                "It may still be finishing its own reconnect; this will be "
                "retried automatically."
            ) from err
        except Exception:
            # v2.0.9 FIX (DEF-002, same audit -- confirmed): any OTHER
            # failure from the identification read (a connection error,
            # not specifically a timeout) previously propagated with the
            # same unreleased-raw-client gap as the TimeoutError case
            # above -- this method had no catch-all at all before this
            # fix, so a non-timeout failure skipped client cleanup
            # entirely rather than merely handling it differently.
            await _bounded_client_disconnect(client)
            raise

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
                    # v2.0.9 (Phase 3.1, this release): see const.py's
                    # own CONF_SYNC_POWER_DEDICATED_READS comment.
                    dedicated_reads_enabled=entry.options.get(
                        CONF_SYNC_POWER_DEDICATED_READS, True
                    ),
                    # v2.0.0 (V2_ARCHITECTURE_DESIGN.md §8.2): the regular
                    # per-device coordinators' own caches, checked for a
                    # cheap shortcut before this coordinator's dedicated
                    # read. Each is None when not applicable (e.g. no
                    # second inverter, no meter, no battery) -- handled the
                    # same as "not aligned, do the dedicated read".
                    inv1_cache=inv1_data.update_coordinator.cache,
                    inv2_cache=(
                        inv2_data.update_coordinator.cache
                        if inv2_data is not None else None
                    ),
                    meter_cache=(
                        inv1_data.power_meter_update_coordinator.cache
                        if inv1_data.power_meter_update_coordinator is not None
                        else None
                    ),
                    battery_cache=(
                        inv1_data.energy_storage_update_coordinator.cache
                        if inv1_data.energy_storage_update_coordinator is not None
                        else None
                    ),
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
                    # v2.0.0b (MOD-03, external ICS audit -- confirmed):
                    # stagger this the same way every other coordinator's
                    # first poll already is -- see
                    # _COORDINATOR_START_DELAYS["sync_power"]'s own comment
                    # for why 16s specifically.
                    await asyncio.sleep(
                        _staggered_start_delay("sync_power", 0).total_seconds()
                    )
                    # v2.0.3 FIX (F-02, external ICS audit -- confirmed via a
                    # genuine production traceback): this used to call
                    # coord.async_config_entry_first_refresh() -- but that
                    # API's own contract requires the config entry to still
                    # be in ConfigEntryState.SETUP_IN_PROGRESS, and this
                    # coroutine, by design (see the comment above this
                    # function), only ever runs AFTER async_setup_entry()
                    # has already returned and the entry has transitioned to
                    # LOADED. The call was therefore destined to always
                    # raise ConfigEntryError from the moment the v2.0.0b
                    # deferral was introduced -- confirmed directly from a
                    # real HA log's own traceback, not just inferred from
                    # source. async_request_refresh() is the correct,
                    # ordinary post-setup refresh mechanism every other
                    # coordinator in this integration already uses for its
                    # own non-first polls; unlike
                    # async_config_entry_first_refresh(), it does not raise
                    # on failure at all (it records the failure on the
                    # coordinator itself and notifies listeners) -- the
                    # try/except below is kept regardless, as a defensive
                    # measure consistent with this background task's own
                    # "must never raise" contract, not because this call is
                    # expected to need it.
                    try:
                        await coord.async_request_refresh()
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
            # v2.0.0b (MOD-16, external ICS audit -- confirmed): bounded,
            # not a bare await primary_device.stop() -- see
            # _bounded_device_stop's own docstring.
            await _bounded_device_stop(primary_device)
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
            # v2.0.0b (MOD-16): see the ConnectionInterruptedException
            # handler's own note above on this same fix.
            await _bounded_device_stop(primary_device)
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
            # v2.0.0b (MOD-16): see the ConnectionInterruptedException
            # handler's own note above on this same fix.
            await _bounded_device_stop(primary_device)
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
            # v2.0.0b (MOD-16): see the ConnectionInterruptedException
            # handler's own note above on this same fix.
            await _bounded_device_stop(primary_device)
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
            # v2.0.0b (MOD-16): see the ConnectionInterruptedException
            # handler's own note above on this same fix.
            await _bounded_device_stop(primary_device)
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
                # v2.0.9 FIX (Phase 4.10, this release -- found during a
                # log review, not either external audit): this fallback
                # previously had no tie to the entry's own lifecycle at
                # all -- unlike the async_create_background_task() branch
                # above, a task started here could survive an entry
                # unload/reload and attempt a delayed Modbus read against
                # already-torn-down state (the transport disconnected,
                # the guard already released) -- exactly the class of
                # problem async_create_background_task() itself exists to
                # prevent. entry.async_on_unload() is a much older, more
                # universally-available HA API than async_create_
                # background_task() (already relied on elsewhere in this
                # same function -- see the update-listener registration
                # above), so it can provide the same entry-lifecycle-tied
                # cancellation even on HA cores old enough to lack the
                # newer API this fallback exists for.
                task = hass.async_create_task(_initialize())
                entry.async_on_unload(task.cancel)
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "battery_health[%s]: could not schedule initialisation", serial
            )


async def _bounded_device_stop(device: Any) -> None:
    """Stop *device* with the same bounded, fault-isolated pattern
    async_unload_entry's own normal disconnect already uses (see its own
    comment on that call, just below) -- extracted here rather than
    repeated at every call site, so the bound only has to be gotten right
    once.

    v2.0.0b FIX (MOD-16, external ICS audit -- confirmed): five setup-
    failure exception handlers each called `await primary_device.stop()`
    directly, with no timeout of its own -- unlike the NORMAL unload path
    (async_unload_entry, below), which was already correctly bounded
    (Defect U/Finding 3). A wedged or half-dead transport during a
    FAILED setup could therefore hang cleanup indefinitely, delaying
    Home Assistant's own retry of the failed entry -- turning a setup-
    time transport problem into a stuck retry loop, exactly the failure
    mode Defect U's own fix already prevents for the unload path.
    """
    try:
        await asyncio.wait_for(device.stop(), timeout=DISCONNECT_TIMEOUT.total_seconds())
    except Exception:  # noqa: BLE001 — never let a stuck stop() block setup-failure cleanup
        _LOGGER.exception(
            "Error stopping the inverter device during setup-failure "
            "cleanup; continuing regardless"
        )


async def _bounded_client_disconnect(client: Any) -> None:
    """Disconnect a raw, not-yet-wrapped Modbus client with the same
    bounded, fault-isolated pattern _bounded_device_stop() already uses
    for the (later-stage) device object.

    v2.0.9 FIX (DEF-002, external ICS quality/defect/architecture audit
    -- confirmed): before this fix, a raw client that had already opened
    a genuine transport connection, but then failed during
    create_device_instance() (device identification), was never
    explicitly disconnected -- left entirely to eventual garbage
    collection instead of a deterministic close. For an RTU/serial
    endpoint specifically, an unreleased OS-level handle can block every
    subsequent connection attempt to that same port, not just this
    entry's own retry -- a materially worse failure mode than the
    already-fixed device-level case _bounded_device_stop() covers.
    """
    disconnect = getattr(client, "disconnect", None)
    if disconnect is None:
        return
    try:
        await asyncio.wait_for(disconnect(), timeout=DISCONNECT_TIMEOUT.total_seconds())
    except Exception:  # noqa: BLE001 — never let a stuck disconnect mask the real error
        _LOGGER.exception(
            "Error disconnecting the raw Modbus client during setup-failure "
            "cleanup; continuing regardless"
        )


async def async_unload_entry(
    hass: HomeAssistant, entry: HuaweiSolarConfigEntry
) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        device_datas: list[HuaweiSolarDeviceData] = entry.runtime_data[DATA_DEVICE_DATAS]
        primary_device = device_datas[0].device

        # v2.0.3 FIX (ICS-10, external ICS audit -- confirmed): services
        # used to be registered on every setup and never unregistered
        # anywhere -- staying registered in Home Assistant's own service
        # registry indefinitely, even with zero huawei_solar entries
        # left loaded. Unregisters here (once this was confirmed to be
        # the LAST entry still needing them -- see async_unload_
        # services()'s own reference-counting) as the very first step,
        # since it doesn't depend on any of the per-device teardown
        # below and is safe to run regardless of whether the rest of it
        # succeeds.
        await async_unload_services(hass, entry)

        # v2.0.0a FIX (F21, external ICS audit -- confirmed): keepalive is
        # the only ACTIVE TRAFFIC PRODUCER among everything torn down in
        # this function -- telemetry/adaptive/battery-health only persist
        # local state, they don't independently talk to the device. The
        # keep-alive loop is cancellation-aware (not a confirmed deadlock),
        # but there was still a real window where it could be mid-probe --
        # having already acquired the guard, awaiting batch_update() --
        # exactly when the transport got disconnected out from under it.
        # Stopped for EVERY device on this entry FIRST, in its own pass,
        # before the single shared-transport disconnect below (not
        # interleaved with the rest of per-device teardown, which doesn't
        # produce new traffic and is safe to run after).
        for device_data in device_datas:
            keepalive = ModbusKeepAlive.get(device_data.device.serial_number)
            if keepalive:
                keepalive.stop()

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
        seen_endpoints: set[str] = set()
        for device_data in device_datas:
            serial = device_data.device.serial_number

            telemetry = ModbusTelemetry.get(serial)
            if telemetry:
                telemetry.stop()
            ModbusTelemetry.remove(serial)

            # v2.0.2 (TEL-004, external ICS/IQS audit -- confirmed): both
            # BusDiagnostics and TelemetryCapture are per-ENDPOINT
            # registries (not per-serial, like everything else in this
            # loop) -- get_or_create() was the only production call
            # either one ever had; remove() existed on both but was never
            # called anywhere. Every endpoint ever captured stayed
            # referenced forever, together with its hass object, buffers,
            # and counters. Fixed for both together, not just
            # TelemetryCapture specifically -- BusDiagnostics had the
            # identical gap, found while checking whether this was a
            # one-off or a systemic pattern; it was the latter.
            # seen_endpoints avoids a redundant (harmless, but noisy)
            # second remove() call for a second device sharing the same
            # physical bus on this same entry.
            #
            # v2.0.9 FIX (Phase 4.8, this release -- old DEF-011,
            # external ICS quality/defect/architecture audit --
            # confirmed): remove() itself is now the bug -- it was
            # unconditional, ignoring whether another entry sharing this
            # same physical endpoint still holds a reference. Switched
            # to release_endpoint(), the reference-counted pairing for
            # switch.py's own acquire_endpoint() call at setup time (see
            # its own comment) -- mirrors ModbusGuard's own established
            # acquire/release pattern exactly, including the "no
            # matching prior acquire is a safe no-op" behaviour, so this
            # still degrades gracefully if switch.py's own setup ever
            # failed to run for some reason.
            guard = getattr(device_data.update_coordinator, "guard", None)
            endpoint = getattr(guard, "endpoint", None)
            if endpoint is not None and endpoint not in seen_endpoints:
                seen_endpoints.add(endpoint)
                BusDiagnostics.release_endpoint(endpoint)
                TelemetryCapture.release_endpoint(endpoint)

            # v2.0.0b (MOD-13, external ICS audit): clear this device's
            # cached static number-entity bounds -- a reload can follow a
            # firmware update or hardware swap, and a stale cached bound
            # surviving that would be a correctness regression for the
            # sake of an efficiency win that only needs to last one
            # session anyway. See number.py's own _STATIC_BOUND_CACHE
            # comment for the full reasoning.
            clear_static_bound_cache(serial)

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

            # v2.0.0a (F21): keepalive.stop() itself already moved to its
            # own pass above, before the transport disconnect -- only the
            # registry cleanup (removing this entry's reference) remains
            # here, alongside the rest of per-device teardown.
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
        # v2.0.0a (F04, external ICS audit): release_endpoint(), not the old
        # unconditional remove() -- this only actually removes the guard
        # from the registry once every entry/flow that acquired a reference
        # to this endpoint (this one included) has released it. A second
        # entry sharing this same physical endpoint keeps working
        # uninterrupted; the guard is torn down only when the last such
        # user is gone.
        ModbusGuard.release_endpoint(ModbusGuard.endpoint_for(entry.data))

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
    # v2.0.1 (H-02, ICS re-audit): this comment used to claim the
    # callbacks below "reset failure counters on all coordinators for
    # this inverter" -- they did not; at the time this call is made, only
    # update_coordinator (the main coordinator) exists yet, and these
    # callbacks are bound to ITS OWN on_connection_lost/on_connection_
    # restored specifically. The callbacks are re-wired further below,
    # once every coordinator for this device is known, to actually reach
    # all of them -- see that re-wiring's own comment for the full
    # reasoning. The wiring here is deliberately kept as the initial
    # value (not left unset), so the main coordinator is still correctly
    # covered even in the unlikely case the re-wiring below is never
    # reached (e.g. a later step in this same function raising first).
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
            #
            # v2.0.0b FIX (MOD-07, external ICS audit -- confirmed): being
            # time-bounded protected setup from hanging, but this call
            # bypassed ModbusGuard entirely -- it could physically collide
            # with any other in-flight or concurrently issued transaction
            # on the same endpoint (main/meter/battery coordinators, or
            # another entry's config-flow discovery). Routed through the
            # same guard every other transaction now goes through;
            # get_or_create() (not acquire_endpoint()) is correct here --
            # this entry's own reference to the endpoint was already
            # acquired once, earlier in async_setup_entry (F04), so this
            # is just fetching the existing guard object, not creating a
            # new reference to its lifecycle.
            async with ModbusGuard.get_or_create(bus_endpoint).request(
                label="optimizer_discovery"
            ):
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
                start_delay=_staggered_start_delay("optimizer", device_index),
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

    # v2.0.1 FIX (H-02, ICS re-audit -- confirmed): keepalive's
    # on_connection_lost/on_connection_restored were wired only to
    # update_coordinator (the main coordinator) at creation time, above --
    # before power_meter/energy_storage/configuration's own coordinators
    # (each with their own separate RegisterCache) existed yet. The
    # comment at that creation site claimed this "resets failure counters
    # on all coordinators for this inverter" -- it did not; only the main
    # coordinator's own cache/counters were ever touched. A keepalive-
    # detected outage therefore invalidated only the main coordinator's
    # cache; the others kept whatever pre-outage values they had as
    # Quality.GOOD, which MOD-01's SyncPower fallback (this same
    # project's own earlier fix) would then actively serve as if nothing
    # had happened. Re-wired here, once every coordinator for this device
    # is known, rather than at each coordinator's own scattered creation
    # site above -- keepalive is created before most of them exist, and
    # _on_connection_lost/_on_connection_restored are plain, reassignable
    # attributes (not fixed permanently at ModbusKeepAlive construction).
    # The optimizer coordinator is deliberately excluded: it is a
    # separate class (HuaweiSolarOptimizerUpdateCoordinator, a sibling,
    # not a subclass) with its own dict-based data model, not a
    # RegisterCache -- it has no on_connection_lost/on_connection_restored
    # method to call, and H-02's concern (stale RegisterCache values)
    # does not apply to it.
    _coordinators_for_keepalive = [
        c for c in (
            update_coordinator,
            power_meter_update_coordinator,
            energy_storage_update_coordinator,
            configuration_update_coordinator,
        ) if c is not None
    ]

    def _on_connection_lost_all() -> None:
        for c in _coordinators_for_keepalive:
            try:
                c.on_connection_lost()
            except Exception:  # noqa: BLE001 -- one coordinator's failure must not skip the rest
                _LOGGER.exception(
                    "%s: on_connection_lost callback failed", c.name
                )

    def _on_connection_restored_all() -> None:
        for c in _coordinators_for_keepalive:
            try:
                c.on_connection_restored()
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "%s: on_connection_restored callback failed", c.name
                )

    keepalive._on_connection_lost = _on_connection_lost_all
    keepalive._on_connection_restored = _on_connection_restored_all

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
