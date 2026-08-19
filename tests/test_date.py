"""Tests for date.py -- the new per-pack install-date entity platform.

v2.0.12 (Battery Phase 5B UI restructuring, this release). Uses the same
minimal-stub-environment approach as test_battery_health_entities.py
(rather than real imports) for the same reason that file does: this
project's own custom_components package is itself named "huawei_solar",
which collides with the separately-installed "huawei_solar" PyPI library
(the underlying Modbus driver) that battery_health_manager.py also needs
to import from -- confirmed directly by attempting a real import first,
which failed with exactly this collision, before switching to this
approach.
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types
import unittest
from datetime import date

_ROOT = pathlib.Path(__file__).parent.parent


def _run(coro):
    return asyncio.run(coro)


def _install_stubs() -> None:
    def mod(name):
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    if "homeassistant" not in sys.modules:
        mod("homeassistant")
    if "homeassistant.core" not in sys.modules:
        core = mod("homeassistant.core")
        core.HomeAssistant = type("HomeAssistant", (), {})
        core.callback = lambda f: f
    else:
        core = sys.modules["homeassistant.core"]
        if not hasattr(core, "callback"):
            core.callback = lambda f: f

    if "homeassistant.const" not in sys.modules:
        const = mod("homeassistant.const")
    else:
        const = sys.modules["homeassistant.const"]

    class EntityCategory:
        DIAGNOSTIC = "diagnostic"
        CONFIG = "config"
    const.EntityCategory = EntityCategory

    if "homeassistant.components" not in sys.modules:
        mod("homeassistant.components")

    # homeassistant.components.date -- the one genuinely NEW stub this
    # file needs beyond what battery_health_entities.py's own tests
    # already establish.
    date_mod = mod("homeassistant.components.date")

    class DateEntity:
        """Stub mirroring only what production date.py actually uses:
        native_value resolves from _attr_native_value the same way real
        HA's cached_property system does for every other entity base
        class in this project's own test stubs."""

        _attr_native_value = None
        _attr_available = True

        @property
        def native_value(self):
            return self._attr_native_value

    date_mod.DateEntity = DateEntity

    if "homeassistant.helpers" not in sys.modules:
        mod("homeassistant.helpers")
    if "homeassistant.helpers.entity_platform" not in sys.modules:
        ep = mod("homeassistant.helpers.entity_platform")
        ep.AddEntitiesCallback = object
    if "homeassistant.helpers.device_registry" not in sys.modules:
        dr = mod("homeassistant.helpers.device_registry")
        dr.DeviceInfo = dict

    # date.py's own async_setup_entry references DATA_DEVICE_DATAS; type-
    # annotation-only symbols for HuaweiSolarConfigEntry/HuaweiSolarDeviceData
    # are stubbed below in .types (harmless under `from __future__ import
    # annotations`). .const itself is loaded for REAL just before battery_
    # health_manager needs it (see below) -- it has zero HA dependencies of
    # its own, so hand-stubbing its many CONF_BH_* constants individually
    # would be fragile busywork a real load avoids entirely.
    types_stub = types.ModuleType("huawei_solar.types")
    types_stub.HuaweiSolarConfigEntry = object
    types_stub.HuaweiSolarDeviceData = object
    sys.modules["huawei_solar.types"] = types_stub


_install_stubs()


def _load(modname: str):
    src = _ROOT / f"{modname}.py"
    spec = importlib.util.spec_from_file_location(f"d_{modname}", str(src))
    m = importlib.util.module_from_spec(spec)
    m.__package__ = "huawei_solar"
    sys.modules[f"d_{modname}"] = m
    sys.modules[f"huawei_solar.{modname}"] = m
    spec.loader.exec_module(m)
    return m


BH = _load("battery_health")
CONST = _load("const")
REGISTER_CACHE = _load("register_cache")
# battery_health_manager imports `from huawei_solar import register_values`
# (the real PyPI library, not this integration) purely for typing/runtime
# use this test never exercises -- stub it minimally rather than pull in
# the real dependency.
_rv_stub = types.ModuleType("huawei_solar")
_rv_stub.register_values = types.ModuleType("huawei_solar.register_values")
sys.modules["huawei_solar_lib_stub"] = _rv_stub
# Only stub the top-level `huawei_solar` name if the real library isn't
# already importable in this environment -- prefer the real one when
# available, matching this project's own general preference for testing
# against real behaviour wherever practical.
try:
    import huawei_solar as _real_lib  # noqa: F401
except ImportError:
    sys.modules["huawei_solar"] = _rv_stub

# Load order matters: battery_health_entities.py imports
# `.battery_health_manager`, so BHM must already be registered in
# sys.modules under "huawei_solar.battery_health_manager" before BHE
# is loaded, or its own relative import fails to resolve.
BHM = _load("battery_health_manager")
BHE = _load("battery_health_entities")
DATE = _load("date")


class _StubManager:
    """Minimal BatteryHealthManager stand-in -- mirrors the same shape
    test_battery_health_entities.py's own _StubManager provides, extended
    with a pack_capacity namespace date.py's own entities actually read."""

    def __init__(self, report=None, slot_labels=None, last_serial=None, serial="TESTSERIAL"):
        self.serial_number = serial
        report = report if report is not None else BH.HealthReport()
        slot_labels = slot_labels if slot_labels is not None else ["u1p1", "u1p2", "u1p3"]
        last_serial = last_serial if last_serial is not None else [None] * len(slot_labels)
        pack_capacity = types.SimpleNamespace(
            slot_labels=slot_labels, _last_serial=last_serial,
        )
        self.engine = types.SimpleNamespace(report=report, pack_capacity=pack_capacity)
        self.listeners = []
        self.set_pack_install_date_calls: list[tuple[str, float]] = []

    def add_listener(self, cb):
        self.listeners.append(cb)

    def remove_listener(self, cb):
        if cb in self.listeners:
            self.listeners.remove(cb)

    def set_pack_install_date(self, serial: str, install_ts: float) -> None:
        self.set_pack_install_date_calls.append((serial, install_ts))


def _battery_device_info(unit: int):
    return {"identifiers": {("huawei_solar", f"TESTSERIAL/battery_{unit}")}}


class TestCreatePackDateEntities(unittest.TestCase):
    """create_battery_health_pack_date_entities() -- mirrors battery_
    health_entities.create_battery_health_pack_entities()'s own slot-
    label parsing exactly, so tested the same way."""

    def test_one_entity_per_pack(self):
        manager = _StubManager()
        entities = DATE.create_battery_health_pack_date_entities(
            manager, _battery_device_info(1), _battery_device_info(2),
        )
        self.assertEqual(len(entities), 3)

    def test_unit_1_and_2_packs_attach_to_the_correct_device(self):
        manager = _StubManager(slot_labels=["u1p1", "u2p1"])
        entities = DATE.create_battery_health_pack_date_entities(
            manager, _battery_device_info(1), _battery_device_info(2),
        )
        by_index = {e._pack_index: e for e in entities}
        self.assertEqual(by_index[0]._attr_device_info, _battery_device_info(1))
        self.assertEqual(by_index[1]._attr_device_info, _battery_device_info(2))

    def test_missing_device_info_skips_that_units_packs(self):
        manager = _StubManager(slot_labels=["u1p1", "u2p1"])
        entities = DATE.create_battery_health_pack_date_entities(
            manager, _battery_device_info(1), None,
        )
        self.assertEqual(len(entities), 1)

    def test_unparseable_slot_label_is_skipped(self):
        manager = _StubManager(slot_labels=["u1p1", "garbage"])
        entities = DATE.create_battery_health_pack_date_entities(
            manager, _battery_device_info(1), _battery_device_info(2),
        )
        self.assertEqual(len(entities), 1)

    def test_entity_name_includes_pack_number(self):
        manager = _StubManager(slot_labels=["u1p1", "u1p2"])
        entities = DATE.create_battery_health_pack_date_entities(
            manager, _battery_device_info(1), _battery_device_info(2),
        )
        names = sorted(e._attr_name for e in entities)
        self.assertEqual(names, ["Pack 1 install date", "Pack 2 install date"])

    def test_unique_ids_are_distinct(self):
        manager = _StubManager()
        entities = DATE.create_battery_health_pack_date_entities(
            manager, _battery_device_info(1), _battery_device_info(2),
        )
        ids = [e._attr_unique_id for e in entities]
        self.assertEqual(len(ids), len(set(ids)))

    def test_uses_the_shared_slot_label_regex_not_a_duplicate(self):
        """Confirms date.py imports BHE's own SLOT_LABEL_RE rather than
        defining a second, potentially-diverging copy."""
        self.assertIs(DATE.SLOT_LABEL_RE, BHE.SLOT_LABEL_RE)


class TestPackInstallDateEntityReading(unittest.TestCase):
    """_apply() -- reconstructing a date from pack_age_days."""

    def _entity(self, manager=None):
        manager = manager or _StubManager()
        return DATE.HuaweiSolarPackInstallDateEntity(
            manager, _battery_device_info(1), 0, "u1p1", "1",
        )

    def test_none_when_no_age_data_at_all(self):
        report = BH.HealthReport()
        ent = self._entity()
        ent._apply(report)
        self.assertIsNone(ent._attr_native_value)

    def test_none_when_this_packs_own_age_is_none(self):
        report = BH.HealthReport()
        report.attributes["pack_age_days"] = [None, 30.0, 30.0]
        ent = self._entity()
        ent._apply(report)
        self.assertIsNone(ent._attr_native_value)

    def test_reconstructs_a_real_date_from_age_days(self):
        report = BH.HealthReport()
        report.attributes["pack_age_days"] = [10.0]
        ent = self._entity()
        ent._apply(report)
        self.assertIsInstance(ent._attr_native_value, date)
        from datetime import datetime, timezone
        expected = (datetime.now(timezone.utc).date())
        from datetime import timedelta
        self.assertEqual(ent._attr_native_value, expected - timedelta(days=10))

    def test_index_beyond_list_length_is_none_not_an_exception(self):
        report = BH.HealthReport()
        report.attributes["pack_age_days"] = [10.0]  # only 1 value
        manager = _StubManager()
        ent = DATE.HuaweiSolarPackInstallDateEntity(
            manager, _battery_device_info(1), 2, "u1p3", "3",  # index 2
        )
        ent._apply(report)  # must not raise
        self.assertIsNone(ent._attr_native_value)


class TestPackInstallDateEntityWriting(unittest.TestCase):
    """async_set_value() -- the write path, and that it goes through the
    shared BatteryHealthManager.set_pack_install_date() method."""

    def test_writes_via_the_shared_manager_method(self):
        manager = _StubManager(last_serial=["SN-PACK-1", None, None])
        ent = DATE.HuaweiSolarPackInstallDateEntity(
            manager, _battery_device_info(1), 0, "u1p1", "1",
        )
        _run(ent.async_set_value(date(2026, 1, 15)))
        self.assertEqual(len(manager.set_pack_install_date_calls), 1)
        serial, ts = manager.set_pack_install_date_calls[0]
        self.assertEqual(serial, "SN-PACK-1")
        from datetime import datetime, timezone
        expected_ts = datetime(2026, 1, 15, tzinfo=timezone.utc).timestamp()
        self.assertEqual(ts, expected_ts)

    def test_resolves_the_packs_current_serial_at_call_time(self):
        """Adversarial: confirms the entity looks up the serial fresh on
        each call, not a serial captured once at construction time --
        important if the pack has been replaced since the entity was
        created."""
        manager = _StubManager(last_serial=["SN-ORIGINAL", None, None])
        ent = DATE.HuaweiSolarPackInstallDateEntity(
            manager, _battery_device_info(1), 0, "u1p1", "1",
        )
        manager.engine.pack_capacity._last_serial[0] = "SN-REPLACEMENT"
        _run(ent.async_set_value(date(2026, 1, 15)))
        serial, _ts = manager.set_pack_install_date_calls[0]
        self.assertEqual(serial, "SN-REPLACEMENT")

    def test_no_serial_yet_does_not_write_or_raise(self):
        manager = _StubManager(last_serial=[None, None, None])
        ent = DATE.HuaweiSolarPackInstallDateEntity(
            manager, _battery_device_info(1), 0, "u1p1", "1",
        )
        _run(ent.async_set_value(date(2026, 1, 15)))  # must not raise
        self.assertEqual(manager.set_pack_install_date_calls, [])

    def test_index_beyond_current_topology_does_not_write_or_raise(self):
        manager = _StubManager(last_serial=["SN-1"])  # only 1 slot now
        ent = DATE.HuaweiSolarPackInstallDateEntity(
            manager, _battery_device_info(1), 2, "u1p3", "3",  # index 2
        )
        _run(ent.async_set_value(date(2026, 1, 15)))  # must not raise
        self.assertEqual(manager.set_pack_install_date_calls, [])


class TestPushUpdateLifecycle(unittest.TestCase):
    """async_added_to_hass / async_will_remove_from_hass / the update
    callback -- matching the same push-entity contract every other
    battery-health entity class in this integration already follows."""

    def test_added_to_hass_registers_listener_and_applies_current_report(self):
        report = BH.HealthReport()
        report.attributes["pack_age_days"] = [5.0]
        manager = _StubManager(report=report)
        ent = DATE.HuaweiSolarPackInstallDateEntity(
            manager, _battery_device_info(1), 0, "u1p1", "1",
        )
        _run(ent.async_added_to_hass())
        self.assertIn(ent._cb, manager.listeners)
        self.assertIsNotNone(ent._attr_native_value)

    def test_will_remove_deregisters_listener(self):
        manager = _StubManager()
        ent = DATE.HuaweiSolarPackInstallDateEntity(
            manager, _battery_device_info(1), 0, "u1p1", "1",
        )
        _run(ent.async_added_to_hass())
        _run(ent.async_will_remove_from_hass())
        self.assertNotIn(ent._cb, manager.listeners)

    def test_update_callback_is_guarded_against_a_bad_report(self):
        """Fault isolation, matching every other battery-health entity
        class: a failure applying one report must not propagate and
        take down the rest of the update batch."""
        manager = _StubManager()
        ent = DATE.HuaweiSolarPackInstallDateEntity(
            manager, _battery_device_info(1), 0, "u1p1", "1",
        )
        bad_report = object()  # has no .attributes at all
        try:
            ent._on_health_update(bad_report)
        except Exception:  # noqa: BLE001
            self.fail("_on_health_update must not raise even for a malformed report")


if __name__ == "__main__":
    unittest.main()
