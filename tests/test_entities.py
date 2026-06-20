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
import importlib.util
import pathlib
import sys
import types
import unittest
from dataclasses import dataclass

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
    uc.DataUpdateCoordinator = type("DataUpdateCoordinator", (), {"__class_getitem__": classmethod(lambda c, i: c)})
    uc.UpdateFailed = type("UpdateFailed", (Exception,), {})

    comps = mod("homeassistant.components")
    # number
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
    const = mod("huawei_solar.const")
    const.CONF_ENABLE_PARAMETER_CONFIGURATION = "enable_parameter_configuration"
    const.DATA_DEVICE_DATAS = "device_datas"

    # huawei_solar.types — real-shaped HuaweiSolarEntityDescription
    from homeassistant.helpers.entity import EntityDescription

    tps = mod("huawei_solar.types")

    @dataclass(frozen=True)
    class HuaweiSolarEntityDescription(EntityDescription):
        @property
        def register_name(self):
            return self.key
    tps.HuaweiSolarEntityDescription = HuaweiSolarEntityDescription
    tps.HuaweiSolarEntity = type("HuaweiSolarEntity", (), {"_attr_has_entity_name": True})
    tps.HuaweiSolarEntityContext = dict
    tps.HuaweiSolarDeviceData = type("HuaweiSolarDeviceData", (), {})
    tps.HuaweiSolarInverterData = type("HuaweiSolarInverterData", (), {})
    tps.HuaweiSolarConfigEntry = object

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


NUMBER = _load("number")
SWITCH = _load("switch")
SELECT = _load("select")
BUTTON = _load("button")


# ── Mock coordinator / device ─────────────────────────────────────────────────
class _Result:
    def __init__(self, value):
        self.value = value


class MockCoordinator:
    def __init__(self, data=None, success=True):
        self.data = data
        self.last_update_success = success
        self.invalidated = []
        self.refresh_calls = 0

    def invalidate_cache(self, name):
        self.invalidated.append(name)

    async def async_request_refresh(self):
        self.refresh_calls += 1


class MockDevice:
    def __init__(self, set_result=True, raises=None):
        self._set_result = set_result
        self._raises = raises
        self.set_calls = []

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
