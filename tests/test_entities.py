"""Entity-layer test coverage for number / switch / select / button.

Previously the entity modules had no executable tests, even though they contain
the user-facing *write* paths to the inverter (set value, toggle, select mode,
press) plus the read/availability logic. This module covers:

* read path  — `_handle_coordinator_update` populates value + availability and
  correctly goes unavailable when the register is missing from coordinator data;
* write path — `async_set_native_value` / `async_turn_on/off` /
  `async_select_option` call `device.set`, invalidate the cache on success, and
  request a refresh; on a failed write the cache is NOT invalidated;
* number min/max precedence — static vs dynamic vs description vs default;
* switch availability — the custom `check_is_available_func` override.

Self-contained: stubs Home Assistant + the external `huawei_solar` library and
the heavy sibling modules, then loads each entity source file via importlib.
Run directly:  python tests/test_entities.py
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import pathlib
import sys
import types
import unittest
from dataclasses import dataclass
from unittest.mock import MagicMock

_ROOT = pathlib.Path(__file__).parent.parent


def _run(coro):
    return asyncio.run(coro)


# ── Home Assistant stubs ──────────────────────────────────────────────────────
def _install_ha_stubs() -> None:
    def mod(name):
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    mod("homeassistant")
    core = mod("homeassistant.core")
    core.HomeAssistant = type("HomeAssistant", (), {})
    core.callback = lambda f: f

    const = mod("homeassistant.const")
    const.PERCENTAGE = "%"
    const.EntityCategory = type("EntityCategory", (), {"CONFIG": "config", "DIAGNOSTIC": "diagnostic"})

    class _U:
        def __getattr__(self, n):
            return n
    const.UnitOfPower = _U()

    @dataclass(frozen=True)
    class EntityDescription:
        key: str
        name: str | None = None
        icon: str | None = None
        device_class: object | None = None
        entity_category: object | None = None
        entity_registry_enabled_default: bool = True
        translation_key: str | None = None

    he = mod("homeassistant.helpers")
    ent = mod("homeassistant.helpers.entity")
    ent.EntityDescription = EntityDescription

    class Entity:
        _attr_has_entity_name = False

        def async_write_ha_state(self):
            self._ha_state_writes = getattr(self, "_ha_state_writes", 0) + 1

        @property
        def available(self):
            return getattr(self, "_attr_available", True)
    ent.Entity = Entity

    dr = mod("homeassistant.helpers.device_registry")
    dr.DeviceInfo = dict

    ep = mod("homeassistant.helpers.entity_platform")
    ep.AddEntitiesCallback = object

    uc = mod("homeassistant.helpers.update_coordinator")

    class CoordinatorEntity:
        def __class_getitem__(cls, item):
            return cls

        def __init__(self, coordinator, context=None):
            self.coordinator = coordinator

        @property
        def available(self):
            return getattr(self.coordinator, "last_update_success", True)
    uc.CoordinatorEntity = CoordinatorEntity

    st = mod("homeassistant.helpers.storage")

    class Store:
        def __init__(self, hass, version, key):
            self.version = version
            self.key = key
            self.saved = None

        async def async_load(self):
            return None

        async def async_save(self, data):
            self.saved = data

        def async_delay_save(self, data_fn, delay=0):
            self.saved = data_fn()
    st.Store = Store
    uc.DataUpdateCoordinator = type("DataUpdateCoordinator", (), {"__class_getitem__": classmethod(lambda c, i: c)})
    uc.UpdateFailed = type("UpdateFailed", (Exception,), {})

    comps = mod("homeassistant.components")
    # number
    ev = mod("homeassistant.helpers.event")
    ev.async_track_time_interval = lambda *a, **k: (lambda: None)
    ev.async_call_later = lambda *a, **k: (lambda: None)

    sen = mod("homeassistant.components.sensor")

    class SensorEntity:
        _attr_native_value = None

        @property
        def native_value(self):
            return self._attr_native_value

    class SensorStateClass:
        MEASUREMENT = "measurement"
        TOTAL_INCREASING = "total_increasing"

    class SensorDeviceClass:
        ENUM = "enum"
    sen.SensorEntity = SensorEntity
    sen.SensorStateClass = SensorStateClass
    sen.SensorDeviceClass = SensorDeviceClass

    num = mod("homeassistant.components.number")

    @dataclass(frozen=True)
    class NumberEntityDescription(EntityDescription):
        native_unit_of_measurement: str | None = None
        native_min_value: float | None = None
        native_max_value: float | None = None
        native_step: float | None = None
        mode: object | None = None
    num.NumberEntityDescription = NumberEntityDescription
    num.NumberEntity = type("NumberEntity", (Entity,), {})
    num.NumberMode = type("NumberMode", (), {"AUTO": "auto", "BOX": "box", "SLIDER": "slider"})
    numc = mod("homeassistant.components.number.const")
    numc.DEFAULT_MAX_VALUE = 100.0
    numc.DEFAULT_MIN_VALUE = 0.0
    # switch
    sw = mod("homeassistant.components.switch")

    @dataclass(frozen=True)
    class SwitchEntityDescription(EntityDescription):
        pass
    sw.SwitchEntityDescription = SwitchEntityDescription
    sw.SwitchEntity = type("SwitchEntity", (Entity,), {})
    # select
    sel = mod("homeassistant.components.select")

    @dataclass(frozen=True)
    class SelectEntityDescription(EntityDescription):
        options: list | None = None
    sel.SelectEntityDescription = SelectEntityDescription
    sel.SelectEntity = type("SelectEntity", (Entity,), {})
    # button
    btn = mod("homeassistant.components.button")
    btn.ButtonEntity = type("ButtonEntity", (Entity,), {})


# ── External huawei_solar library + heavy sibling stubs ───────────────────────
def _install_lib_stubs() -> None:
    def mod(name):
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    class _RegisterName(str):
        _members: dict = {}

        def __class_getitem__(cls, k):
            return cls._members.setdefault(k, cls(k))

    hs = mod("huawei_solar")
    hs.__path__ = []
    hs.RegisterName = _RegisterName

    class _HsResult:
        def __init__(self, value):
            self.value = value
    hs.Result = _HsResult
    for n in ["HuaweiSolarDevice", "SUN2000Device", "EMMADevice"]:
        setattr(hs, n, type(n, (), {}))

    class _NS:
        def __getattr__(self, n):
            if n == "RegisterName":
                return _RegisterName
            return _RegisterName[n]
    hs.register_names = _NS()

    class _RV:
        def __getattr__(self, n):
            # rv.StorageWorkingModesC.SOMETHING etc.
            return type(n, (), {"__getattr__": lambda s, k: k})()
    hs.register_values = _RV()

    rd = mod("huawei_solar.register_definitions")
    rdn = mod("huawei_solar.register_definitions.number")
    rdn.NumberRegister = type("NumberRegister", (), {})
    regs = mod("huawei_solar.registers")
    regs.REGISTERS = {}
    dev = mod("huawei_solar.device")
    base = mod("huawei_solar.device.base")
    base.HuaweiSolarDevice = hs.HuaweiSolarDevice

    # ── Sibling integration modules (kept light so we don't load the coordinator)
    # Use the REAL const module: adaptive_modbus imports many ADAPTIVE_*
    # constants and a hand-maintained stub list would drift out of sync.
    import importlib.util as _ilu
    _cspec = _ilu.spec_from_file_location(
        "huawei_solar.const", str(_ROOT / "const.py"))
    const = _ilu.module_from_spec(_cspec)
    sys.modules["huawei_solar.const"] = const
    _cspec.loader.exec_module(const)
    const.CONF_ENABLE_PARAMETER_CONFIGURATION = "enable_parameter_configuration"
    const.DATA_DEVICE_DATAS = "device_datas"
    const.CONF_BH_RATED_CAPACITY_KWH = "bh_rated_capacity_kwh"
    const.CONF_BH_WARRANTY_THROUGHPUT_KWH = "bh_warranty_throughput_kwh"
    const.CONF_BH_WEIGHT_CAPACITY = "bh_weight_capacity"
    const.CONF_BH_WEIGHT_EFFICIENCY = "bh_weight_efficiency"
    const.CONF_BH_WEIGHT_BALANCE = "bh_weight_balance"
    const.CONF_BH_WINDOW_DAYS = "bh_window_days"
    const.CONF_BH_MIN_SEGMENT_DELTA_SOC = "bh_min_segment_delta_soc"
    const.CONF_BH_ENABLED = "bh_enabled"
    const.CONF_BH_INSTALL_DATE = "bh_install_date"
    const.CONF_BH_AMBIENT_ENTITY = "bh_ambient_entity"

    # huawei_solar.types — real-shaped HuaweiSolarEntityDescription
    from homeassistant.helpers.entity import EntityDescription

    tps = mod("huawei_solar.types")

    @dataclass(frozen=True)
    class HuaweiSolarEntityDescription(EntityDescription):
        @property
        def register_name(self):
            return self.key
    tps.HuaweiSolarEntityDescription = HuaweiSolarEntityDescription

    def _stub_quality_attrs(self, coordinator, register_key):
        # v2.0.0: minimal stand-in for the real HuaweiSolarEntity._quality_attrs()
        # (types.py) -- these tests exercise value/availability logic, not the
        # quality feature itself, and the fake coordinators built below don't
        # stub a .cache. Returns a fixed, harmless value rather than depending
        # on RegisterCache.quality_of() at all.
        return {"data_quality": "good"}

    # v2.0.0b (MOD-05/MOD-06, external ICS audit): the real _guarded_write()/
    # _guarded_write_sequence() call guard.request()/asyncio.timeout() and
    # device.set() -- reproduced minimally here so entity write tests can
    # exercise the real call sites in number.py/select.py/switch.py without
    # needing the real WRITE_TIMEOUT/WRITE_SEQUENCE_TIMEOUT constants or a
    # real ModbusGuard. Matches MockCoordinator's _FakeGuard (below), whose
    # request() is already a no-op async context manager.
    async def _stub_guarded_write(self, guard, device, name, value, *, label="write"):
        async with guard.request(label=label):
            return await device.set(name, value)

    @contextlib.asynccontextmanager
    async def _stub_guarded_write_sequence(self, guard, *, label="write_sequence"):
        async with guard.request(label=label):
            async def _write(device, name, value):
                return await device.set(name, value)
            yield _write

    tps.HuaweiSolarEntity = type(
        "HuaweiSolarEntity",
        (),
        {
            "_attr_has_entity_name": True,
            "_quality_attrs": _stub_quality_attrs,
            "_guarded_write": _stub_guarded_write,
            "_guarded_write_sequence": _stub_guarded_write_sequence,
        },
    )
    tps.HuaweiSolarEntityContext = dict
    tps.HuaweiSolarDeviceData = type("HuaweiSolarDeviceData", (), {})
    tps.HuaweiSolarInverterData = type("HuaweiSolarInverterData", (), {})
    tps.HuaweiSolarConfigEntry = object

    # v2.0.7 (ICS-12, ICS quality audit -- confirmed): button.py now
    # imports get_device_write_lock from .types -- a minimal but
    # behaviourally faithful stand-in (real per-serial asyncio.Lock
    # registry, same as the real implementation) so tests exercising
    # button.py's actual lock-acquisition call site work against real
    # asyncio.Lock semantics, not a no-op.
    _stub_write_locks: dict[str, asyncio.Lock] = {}

    def _stub_get_device_write_lock(serial_number):
        if serial_number not in _stub_write_locks:
            _stub_write_locks[serial_number] = asyncio.Lock()
        return _stub_write_locks[serial_number]
    tps.get_device_write_lock = _stub_get_device_write_lock

    ucmod = mod("huawei_solar.update_coordinator")
    ucmod.HuaweiSolarUpdateCoordinator = type("HuaweiSolarUpdateCoordinator", (), {})
    ucmod.HuaweiSolarOptimizerUpdateCoordinator = type("HuaweiSolarOptimizerUpdateCoordinator", (), {})


_install_ha_stubs()
_install_lib_stubs()


def _load(modname: str):
    src = _ROOT / f"{modname}.py"
    spec = importlib.util.spec_from_file_location(f"hs_{modname}", str(src))
    m = importlib.util.module_from_spec(spec)
    m.__package__ = "huawei_solar"
    sys.modules[f"hs_{modname}"] = m
    spec.loader.exec_module(m)
    return m


# v1.2.2: switch.py imports the adaptive controller for the shared learning
# switch, so it must resolve as a package submodule too.
ADAPTIVE = _load("adaptive_modbus")
sys.modules["huawei_solar.adaptive_modbus"] = ADAPTIVE
# v1.3.0: switch.py imports the diagnostics capture for the per-bus switch.
BUSDIAG = _load("bus_diagnostics")
sys.modules["huawei_solar.bus_diagnostics"] = BUSDIAG

BATTERY_HEALTH = _load("battery_health")
sys.modules["huawei_solar.battery_health"] = BATTERY_HEALTH
# v2.0.0: battery_health_manager.py now imports Quality from
# .register_cache -- must be loaded and registered as a sibling submodule
# here too, same as adaptive_modbus/bus_diagnostics/battery_health above.
REGISTER_CACHE = _load("register_cache")
sys.modules["huawei_solar.register_cache"] = REGISTER_CACHE
BATTERY_HEALTH_MANAGER = _load("battery_health_manager")
sys.modules["huawei_solar.battery_health_manager"] = BATTERY_HEALTH_MANAGER
# v2.0.0b: switch.py now imports ModbusTelemetry and telemetry_capture for
# the new telemetry-capture switch. telemetry_capture.py itself imports
# from .bus_diagnostics (already loaded/registered above), so it must be
# loaded after that, same reasoning as battery_health_manager needing
# register_cache loaded first.
MODBUS_TELEMETRY = _load("modbus_telemetry")
sys.modules["huawei_solar.modbus_telemetry"] = MODBUS_TELEMETRY
TELEMETRY_CAPTURE = _load("telemetry_capture")
sys.modules["huawei_solar.telemetry_capture"] = TELEMETRY_CAPTURE

NUMBER = _load("number")
SWITCH = _load("switch")
SELECT = _load("select")
BUTTON = _load("button")


# ── Mock coordinator / device ─────────────────────────────────────────────────
class _Result:
    def __init__(self, value):
        self.value = value


class _FakeGuardCtx:
    async def __aenter__(self):
        pass

    async def __aexit__(self, *args):
        pass


class _FakeGuard:
    """v2.0.0a: minimal stand-in for ModbusGuard, needed now that write
    paths route through it (F05, external ICS audit). Matches the same
    pattern already used in test_synchronized_power_coordinator.py."""

    def request(self, *, label: str = "", priority: bool = False):
        return _FakeGuardCtx()


class MockCoordinator:
    def __init__(self, data=None, success=True):
        self.data = data
        self.last_update_success = success
        self.invalidated = []
        self.refresh_calls = 0
        self.guard = _FakeGuard()
        # v2.0.0b (MOD-10): real coordinator writes now reference
        # self.coordinator.name when naming background tasks.
        self.name = "mock_coordinator"

    def invalidate_cache(self, name):
        self.invalidated.append(name)

    async def async_request_refresh(self):
        self.refresh_calls += 1

    async def verify_write(self, name, expected_value):
        # v2.0.0a (F12, external ICS audit): a minimal stand-in -- these
        # tests check the entity's own post-write state (invalidate_cache
        # called, refresh scheduled), not verify_write()'s own retry/log
        # behaviour, which is tested directly against the real
        # implementation in update_coordinator.py's own test file.
        return True

    def create_background_task(self, coro, name):
        # v2.0.0b (MOD-10, external ICS audit): a minimal stand-in for the
        # real entry-scoped create_background_task() -- schedules via a
        # fake hass so the coroutine is at least consumed (avoiding an
        # "was never awaited" warning), without needing a real HA
        # ConfigEntry. MockCoordinator (unlike the entity) has no .hass
        # set by _make(), so this creates one lazily on first use.
        if not hasattr(self, "hass"):
            self.hass = MagicMock()
            self.hass.async_create_task = MagicMock(side_effect=lambda c: c.close())
        self.hass.async_create_task(coro)


class MockDevice:
    def __init__(self, set_result=True, raises=None, serial_number="MOCKSERIAL001"):
        self._set_result = set_result
        self._raises = raises
        self.set_calls = []
        # v2.0.7 (ICS-12, ICS quality audit -- confirmed): button.py's
        # StopForcibleCharge button now looks up a per-serial write lock
        # (_get_device_write_lock(self.device.serial_number)), so this
        # mock needs a real attribute for that lookup to succeed -- a
        # fixed default so existing tests that don't care about the
        # specific value need no changes.
        self.serial_number = serial_number

    async def set(self, name, value):
        self.set_calls.append((name, value))
        if self._raises:
            raise self._raises
        return self._set_result


def _make(entity_cls, description, coordinator, device):
    """Build an entity without running HA's heavy __init__."""
    e = object.__new__(entity_cls)
    e.entity_description = description
    e.coordinator = coordinator
    e.device = device
    e._attr_available = True
    # v2.0.0a (F12, external ICS audit): async_set_native_value()/
    # async_select_option()/async_turn_on()/async_turn_off() now fire
    # verify_write() as a background task via self.hass.async_create_task()
    # -- a real HA entity gets self.hass set during platform setup, which
    # this lightweight construction path (deliberately bypassing HA's
    # heavier __init__) doesn't run. MagicMock's default async_create_task
    # just returns a MagicMock rather than actually scheduling anything,
    # which is fine here -- these tests check the entity's own state after
    # a write, not verify_write()'s own behaviour (that's covered directly
    # in update_coordinator.py's own tests).
    e.hass = MagicMock()
    return e


# ── Number entity ─────────────────────────────────────────────────────────────
class TestNumberEntity(unittest.TestCase):
    def _desc(self, **kw):
        base = dict(key="storage_maximum_charging_power")
        base.update(kw)
        return NUMBER.HuaweiSolarNumberEntityDescription(**base)

    def test_read_populates_value(self):
        d = self._desc()
        coord = MockCoordinator(data={d.register_name: _Result(2500.0)})
        e = _make(NUMBER.HuaweiSolarNumberEntity, d, coord, MockDevice())
        e._dynamic_min_value = None
        e._dynamic_max_value = None
        e._handle_coordinator_update()
        self.assertEqual(e._attr_native_value, 2500.0)

    def test_unavailable_when_register_absent(self):
        d = self._desc()
        e = _make(NUMBER.HuaweiSolarNumberEntity, d, MockCoordinator(data={}), MockDevice())
        e._handle_coordinator_update()
        self.assertFalse(e._attr_available)
        self.assertIsNone(e._attr_native_value)

    def test_write_success_invalidates_and_refreshes(self):
        d = self._desc()
        coord = MockCoordinator(data={})
        dev = MockDevice(set_result=True)
        e = _make(NUMBER.HuaweiSolarNumberEntity, d, coord, dev)
        _run(e.async_set_native_value(1500.0))
        self.assertEqual(dev.set_calls, [(d.register_name, 1500.0)])
        self.assertEqual(coord.invalidated, [d.register_name])
        self.assertEqual(coord.refresh_calls, 1)
        self.assertEqual(e._attr_native_value, 1500.0)

    def test_write_failure_does_not_invalidate(self):
        d = self._desc()
        coord = MockCoordinator(data={})
        dev = MockDevice(set_result=False)
        e = _make(NUMBER.HuaweiSolarNumberEntity, d, coord, dev)
        _run(e.async_set_native_value(1500.0))
        self.assertEqual(coord.invalidated, [])      # write failed → no cache invalidation
        self.assertEqual(coord.refresh_calls, 1)     # refresh still requested

    def test_max_value_precedence(self):
        d = self._desc(native_max_value=5000.0)
        e = _make(NUMBER.HuaweiSolarNumberEntity, d, MockCoordinator(), MockDevice())
        e._static_max_value = None
        e._dynamic_max_value = None
        self.assertEqual(e.native_max_value, 5000.0)          # description value
        e._dynamic_max_value = 3000.0
        self.assertEqual(e.native_max_value, 3000.0)          # dynamic caps lower
        e._dynamic_max_value = 9000.0
        self.assertEqual(e.native_max_value, 5000.0)          # min(dynamic, static)

    def test_min_value_default(self):
        d = self._desc()
        e = _make(NUMBER.HuaweiSolarNumberEntity, d, MockCoordinator(), MockDevice())
        e._static_min_value = None
        e._dynamic_min_value = None
        self.assertEqual(e.native_min_value, 0.0)             # DEFAULT_MIN_VALUE


# ── Switch entity ─────────────────────────────────────────────────────────────
class TestSwitchEntity(unittest.TestCase):
    def _desc(self, **kw):
        base = dict(key="storage_charge_from_grid_function")
        base.update(kw)
        return SWITCH.HuaweiSolarSwitchEntityDescription(**base)

    def test_read_is_on(self):
        d = self._desc()
        coord = MockCoordinator(data={d.register_name: _Result(True)})
        e = _make(SWITCH.HuaweiSolarSwitchEntity, d, coord, MockDevice())
        e._handle_coordinator_update()
        self.assertTrue(e._attr_is_on)
        self.assertTrue(e._attr_available)

    def test_unavailable_when_absent(self):
        d = self._desc()
        e = _make(SWITCH.HuaweiSolarSwitchEntity, d, MockCoordinator(data={}), MockDevice())
        e._handle_coordinator_update()
        self.assertIsNone(e._attr_is_on)
        self.assertFalse(e._attr_available)

    def test_turn_on_writes_true_and_invalidates(self):
        d = self._desc()
        coord = MockCoordinator(data={})
        dev = MockDevice(set_result=True)
        e = _make(SWITCH.HuaweiSolarSwitchEntity, d, coord, dev)
        _run(e.async_turn_on())
        self.assertEqual(dev.set_calls, [(d.register_name, True)])
        self.assertTrue(e._attr_is_on)
        self.assertEqual(coord.invalidated, [d.register_name])
        self.assertEqual(coord.refresh_calls, 1)

    def test_turn_off_failure_keeps_state(self):
        d = self._desc()
        coord = MockCoordinator(data={})
        dev = MockDevice(set_result=False)
        e = _make(SWITCH.HuaweiSolarSwitchEntity, d, coord, dev)
        e._attr_is_on = True
        _run(e.async_turn_off())
        self.assertEqual(dev.set_calls, [(d.register_name, False)])
        self.assertEqual(coord.invalidated, [])     # failed write → no invalidation

    def test_check_is_available_func(self):
        d = self._desc(
            is_available_key=SWITCH.rn.STORAGE_WORKING_MODE_SETTINGS,
            check_is_available_func=lambda v: v == 2,
        )
        coord = MockCoordinator(data={
            d.register_name: _Result(True),
            d.is_available_key: _Result(2),
        })
        e = _make(SWITCH.HuaweiSolarSwitchEntity, d, coord, MockDevice())
        e._handle_coordinator_update()
        self.assertTrue(e._attr_available)
        # now make the availability register report a non-matching value
        coord.data[d.is_available_key] = _Result(0)
        e._handle_coordinator_update()
        self.assertFalse(e._attr_available)


# ── Select entity ─────────────────────────────────────────────────────────────
class TestSelectEntity(unittest.TestCase):
    def test_select_option_writes_and_refreshes(self):
        d = SELECT.HuaweiSolarSelectEntityDescription(
            key="storage_excess_pv_energy_use_in_tou",
            options=["a", "b"],
        )
        coord = MockCoordinator(data={})
        dev = MockDevice(set_result=True)
        e = _make(SELECT.HuaweiSolarSelectEntity, d, coord, dev)
        # _to_enum maps option text → register enum; stub it to identity
        e._to_enum = lambda opt: opt
        _run(e.async_select_option("b"))
        self.assertEqual(len(dev.set_calls), 1)
        self.assertEqual(e._attr_current_option, "b")
        self.assertEqual(coord.invalidated, [d.register_name])
        self.assertEqual(coord.refresh_calls, 1)

    def test_check_is_available_func(self):
        """Mirrors TestSwitchEntity's own test_check_is_available_func --
        same pattern, confirming select.py's own availability handling
        works correctly when the register IS present."""
        d = SELECT.HuaweiSolarSelectEntityDescription(
            key="storage_capacity_control_mode",
            options=["a", "b"],
            is_available_key=SELECT.rn.STORAGE_CHARGE_FROM_GRID_FUNCTION,
            check_is_available_func=lambda charge_from_grid: charge_from_grid,
        )
        # _friendly_format() calls .name.lower() on the main register's
        # own value (an IntEnum in real use) -- a plain string stands in
        # fine everywhere else in this test file, but not here.
        main_value = types.SimpleNamespace(name="A")
        coord = MockCoordinator(data={
            d.register_name: _Result(main_value),
            d.is_available_key: _Result(True),
        })
        e = _make(SELECT.HuaweiSolarSelectEntity, d, coord, MockDevice())
        e._handle_coordinator_update()
        self.assertTrue(e._attr_available)
        coord.data[d.is_available_key] = _Result(False)
        e._handle_coordinator_update()
        self.assertFalse(e._attr_available)

    def test_missing_availability_register_does_not_raise(self):
        """v2.0.3 FIX (ICS-17, external ICS audit -- confirmed via a real
        production traceback): the main register present but the
        SEPARATE availability register missing from coordinator.data
        (a legitimate, expected state for a partial/degraded coordinator
        payload) used to raise an uncaught KeyError here, crashing the
        whole coordinator-update listener callback. Must now complete
        without raising, with availability falling back to whatever the
        entity description's own check_is_available_func does with None.
        """
        d = SELECT.HuaweiSolarSelectEntityDescription(
            key="storage_capacity_control_mode",
            options=["a", "b"],
            is_available_key=SELECT.rn.STORAGE_CHARGE_FROM_GRID_FUNCTION,
            check_is_available_func=lambda charge_from_grid: charge_from_grid,
        )
        main_value = types.SimpleNamespace(name="A")
        coord = MockCoordinator(data={
            d.register_name: _Result(main_value),
            # d.is_available_key deliberately absent -- the exact
            # partial-payload scenario the real traceback showed.
        })
        e = _make(SELECT.HuaweiSolarSelectEntity, d, coord, MockDevice())
        e._handle_coordinator_update()  # must not raise
        self.assertFalse(
            e._attr_available,
            "a missing availability register should not be treated as "
            "available -- None passed to check_is_available_func here "
            "correctly yields a falsy result",
        )


# ── Button entity ─────────────────────────────────────────────────────────────
class TestButtonEntity(unittest.TestCase):
    def test_stop_forcible_charge_press_sequence(self):
        e = object.__new__(BUTTON.StopForcibleChargeButtonEntity)
        dev = MockDevice(set_result=True)
        coord = MockCoordinator()
        e.device = dev
        e._configuration_update_coordinator = coord
        _run(e.async_press())
        # Four register writes: stop trigger, discharge power 0, period 0, mode TIME
        self.assertEqual(len(dev.set_calls), 4)
        written = [name for name, _ in dev.set_calls]
        self.assertIn(BUTTON.rn.STORAGE_FORCIBLE_DISCHARGE_POWER, written)
        self.assertIn(BUTTON.rn.STORAGE_FORCED_CHARGING_AND_DISCHARGING_PERIOD, written)
        # Config coordinator refreshed afterwards
        self.assertEqual(coord.refresh_calls, 1)

    def test_press_without_config_coordinator_is_safe(self):
        e = object.__new__(BUTTON.StopForcibleChargeButtonEntity)
        e.device = MockDevice(set_result=True)
        e._configuration_update_coordinator = None
        _run(e.async_press())  # must not raise when no config coordinator present

    def test_adversarial_press_serialises_against_the_shared_write_lock(self):
        """ICS-12 (ICS quality audit -- confirmed): async_press() must
        actually wait on the SAME per-serial lock services.py's own
        stop_forcible_charge() uses -- not merely hold ModbusGuard, which
        only prevents mid-sequence interleaving, not two COMPLETE
        sequences racing back-to-back. Proven directly: hold the shared
        lock for this serial manually (simulating a concurrent service
        call already in flight), start a press, confirm it makes ZERO
        progress while the lock is held, then release and confirm it
        proceeds -- not just that both eventually succeed independently."""
        async def _go():
            e = object.__new__(BUTTON.StopForcibleChargeButtonEntity)
            dev = MockDevice(set_result=True, serial_number="SHARED-LOCK-TEST")
            coord = MockCoordinator()
            e.device = dev
            e._configuration_update_coordinator = coord

            lock = BUTTON._get_device_write_lock("SHARED-LOCK-TEST")
            await lock.acquire()
            try:
                press_task = asyncio.ensure_future(e.async_press())
                # Give the event loop a real chance to run the task up to
                # its own lock-acquire point.
                await asyncio.sleep(0.05)
                self.assertEqual(
                    len(dev.set_calls), 0,
                    "async_press() must not have written anything yet -- "
                    "it should be blocked waiting on the lock this test "
                    "already holds for the SAME serial number",
                )
            finally:
                lock.release()
            await press_task
            self.assertEqual(
                len(dev.set_calls), 4,
                "once the lock is released, the press must complete "
                "normally, proving this isn't simply deadlocked or "
                "silently skipped",
            )
        _run(_go())

    def test_different_serials_do_not_serialise_against_each_other(self):
        """Negative case: the lock is per-SERIAL, not global -- a press
        for one device must not wait on another device's lock."""
        async def _go():
            e = object.__new__(BUTTON.StopForcibleChargeButtonEntity)
            dev = MockDevice(set_result=True, serial_number="DEVICE-B")
            e.device = dev
            e._configuration_update_coordinator = None

            other_lock = BUTTON._get_device_write_lock("DEVICE-A")
            await other_lock.acquire()
            try:
                # Must complete promptly -- not blocked by DEVICE-A's lock.
                await asyncio.wait_for(e.async_press(), timeout=1.0)
            finally:
                other_lock.release()
            self.assertEqual(len(dev.set_calls), 4)
        _run(_go())


class _FakeCapture:
    """Minimal stand-in for TelemetryCapture -- these tests exercise the
    entity's own wiring (does it call set_enabled/record_snapshot
    correctly), not TelemetryCapture's own buffering/flush behaviour,
    which is tested directly against the real implementation in
    test_telemetry_capture.py."""

    def __init__(self):
        self.enabled = False
        self.cancel_periodic = None
        self.snapshots: list[dict] = []

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        if not enabled and self.cancel_periodic is not None:
            self.cancel_periodic()
            self.cancel_periodic = None

    async def async_disable(self) -> None:
        # v2.0.2 (TEL-002): the switch entity now calls this, not
        # set_enabled(False) directly -- same effect here (this fake has
        # no real flush to await), but async so the entity's own
        # `await self._capture.async_disable()` call works unchanged.
        self.set_enabled(False)

    def record_snapshot(self, snapshot: dict) -> None:
        self.snapshots.append(snapshot)

    def stats(self) -> dict:
        return {"enabled": self.enabled, "snapshots_captured": len(self.snapshots)}


class TestModbusTelemetryCaptureSwitchEntity(unittest.TestCase):
    """v2.0.0b: the periodic aggregate telemetry-capture switch. Built to
    close a real, confirmed gap -- see this session's own architecture
    reanalysis -- in what telemetry existed to assess the Physical Demand
    Planner question without a second deployment."""

    def _make_switch(self):
        capture = _FakeCapture()
        device_data = types.SimpleNamespace(
            device_info={},
            device=types.SimpleNamespace(serial_number="SN1"),
            update_coordinator=MagicMock(data=None),
            power_meter_update_coordinator=None,
            energy_storage_update_coordinator=None,
            configuration_update_coordinator=None,
        )
        e = object.__new__(SWITCH.ModbusTelemetryCaptureSwitchEntity)
        e._capture = capture
        e._device_datas = [device_data]
        e._sync_coordinator = None
        e._register_overlap_captured = False
        e.hass = MagicMock()
        return e, capture

    def test_is_on_reflects_capture_state(self):
        e, capture = self._make_switch()
        self.assertFalse(e.is_on)
        capture.enabled = True
        self.assertTrue(e.is_on)

    def test_turn_on_enables_capture_and_starts_the_timer(self):
        e, capture = self._make_switch()
        _run(e.async_turn_on())
        self.assertTrue(capture.enabled)
        self.assertIsNotNone(
            capture.cancel_periodic,
            "turning on must store a cancel callback on the capture object, "
            "not just start a timer nobody can stop later",
        )

    def test_turn_on_resets_the_register_overlap_flag(self):
        """A fresh capture session should get its own fresh overlap
        attempt, in case coordinators were not yet polled the first
        time this ran."""
        e, capture = self._make_switch()
        e._register_overlap_captured = True
        _run(e.async_turn_on())
        self.assertFalse(e._register_overlap_captured)

    def test_turn_off_disables_capture(self):
        e, capture = self._make_switch()
        _run(e.async_turn_on())
        _run(e.async_turn_off())
        self.assertFalse(capture.enabled)

    def test_will_remove_from_hass_cancels_a_still_running_capture(self):
        """The core lifecycle fix: an unload/reload while capture happens
        to be on must not leave the timer firing against coordinators
        that no longer exist."""
        e, capture = self._make_switch()
        _run(e.async_turn_on())
        self.assertTrue(capture.enabled)
        _run(e.async_will_remove_from_hass())
        self.assertFalse(capture.enabled)

    def test_will_remove_from_hass_is_a_no_op_when_already_off(self):
        e, capture = self._make_switch()
        _run(e.async_will_remove_from_hass())  # must not raise
        self.assertFalse(capture.enabled)

    def test_snapshot_tick_records_a_snapshot(self):
        e, capture = self._make_switch()
        _run(e._async_snapshot_tick(None))
        self.assertEqual(len(capture.snapshots), 1)

    def test_snapshot_tick_marks_overlap_captured_once_a_coordinator_exists(self):
        """update_coordinator is present (even before its own first poll,
        i.e. .data is still None) -- check_register_overlap() itself
        correctly handles that by skipping that one coordinator WITHIN
        the check (see telemetry_capture.py's own "not yet polled"
        handling), not by omitting the whole check from the snapshot.
        So the flag correctly becomes True after one tick here."""
        e, capture = self._make_switch()
        self.assertFalse(e._register_overlap_captured)
        _run(e._async_snapshot_tick(None))
        self.assertTrue(e._register_overlap_captured)
        self.assertIn("register_overlap", capture.snapshots[0])

    def test_snapshot_tick_overlap_flag_stays_false_with_no_coordinators_at_all(self):
        """The genuine negative case: a device with no coordinator
        references at all (all None) must leave the flag False, since
        there is nothing to check yet."""
        e, capture = self._make_switch()
        e._device_datas[0].update_coordinator = None
        _run(e._async_snapshot_tick(None))
        self.assertFalse(e._register_overlap_captured)
        self.assertNotIn("register_overlap", capture.snapshots[0])

    def test_snapshot_tick_failure_does_not_raise(self):
        """Telemetry must never break polling -- a failure gathering the
        snapshot must be caught and logged, not propagate."""
        e, capture = self._make_switch()
        e._device_datas = None  # will break build_telemetry_snapshot's own iteration
        _run(e._async_snapshot_tick(None))  # must not raise


if __name__ == "__main__":
    unittest.main(verbosity=2)
