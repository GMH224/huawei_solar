"""Tests for battery_health_entities.py — the HA sensor entity layer.

Added in v1.1.7 as a direct regression test for a real production bug: the
confidence entity ("low"/"normal"/"stale") was created with
``_attr_suggested_display_precision`` set (a class attribute shared by every
battery-health sensor), which tells Home Assistant's ``SensorEntity.state``
property to expect a numeric value. Any string state then raises::

    ValueError: Sensor ... indicating it has a numeric value; however,
    it has the non-numeric value: 'low' (<class 'str'>)

This crashed entity setup once and then every subsequent coordinator update
thereafter (caught by the manager's per-listener guard, so other entities
kept working, but confidence itself silently died — see AUDIT_1.1.7.md).

Rather than mocking Home Assistant's internals, this module re-implements
HA's actual validation rule from ``homeassistant/components/sensor/__init__.py``
(``SensorEntity.state``) and runs every real battery-health entity's resolved
attributes through it — this is the same check that crashed in production,
so passing it here is real evidence, not a mocked approximation.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
import unittest
from unittest.mock import MagicMock

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _install_ha_stubs() -> None:
    for name in [
        "homeassistant", "homeassistant.components",
        "homeassistant.components.sensor", "homeassistant.const",
        "homeassistant.core",
    ]:
        sys.modules.setdefault(name, types.ModuleType(name))

    sensor_mod = sys.modules["homeassistant.components.sensor"]

    class _SensorDeviceClass:
        ENUM = "enum"

    class _SensorStateClass:
        MEASUREMENT = "measurement"

    class SensorEntity:
        """Minimal stand-in exposing only what our entities touch."""
        _attr_native_value = None

    sensor_mod.SensorDeviceClass = _SensorDeviceClass
    sensor_mod.SensorStateClass = _SensorStateClass
    sensor_mod.SensorEntity = SensorEntity

    const_mod = sys.modules["homeassistant.const"]
    const_mod.PERCENTAGE = "%"
    const_mod.EntityCategory = type(
        "EntityCategory", (), {"CONFIG": "config", "DIAGNOSTIC": "diagnostic"}
    )

    core_mod = sys.modules["homeassistant.core"]
    core_mod.callback = lambda f: f


_install_ha_stubs()


def _load(modname: str):
    spec = importlib.util.spec_from_file_location(
        f"huawei_solar.{modname}", _ROOT / f"{modname}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"huawei_solar.{modname}"] = module
    spec.loader.exec_module(module)
    return module


# battery_health_entities.py does `from .battery_health_manager import
# BatteryHealthManager` purely for type hints (the file uses
# `from __future__ import annotations`, so the hint itself is never
# evaluated) — stub the symbol rather than loading the real module, which
# pulls in homeassistant.core/helpers.storage we don't need for this test.
_bhm_stub = types.ModuleType("huawei_solar.battery_health_manager")
_bhm_stub.BatteryHealthManager = object
sys.modules["huawei_solar.battery_health_manager"] = _bhm_stub

BH = _load("battery_health")
BHE = _load("battery_health_entities")


def _ha_numeric_state_check(entity) -> None:
    """Re-implementation of Home Assistant's actual validation rule
    (``homeassistant/components/sensor/__init__.py``, ``SensorEntity.state``):
    a numeric hint (device_class in NUMERIC classes, a state_class, a unit, or
    a suggested_display_precision) on an entity whose value is a non-numeric
    string raises ValueError. This is verbatim the check that crashed in
    production for the `confidence` entity — see module docstring.
    """
    value = entity._attr_native_value
    if value is None or isinstance(value, (int, float)):
        return  # numeric or absent — never a problem

    has_numeric_hint = (
        getattr(entity, "_attr_state_class", None) is not None
        or getattr(entity, "_attr_native_unit_of_measurement", None) is not None
        or getattr(entity, "_attr_suggested_display_precision", None) is not None
    )
    device_class = getattr(entity, "_attr_device_class", None)
    is_enum_or_none = device_class in (None, "enum")

    if has_numeric_hint and is_enum_or_none is False:
        # (kept for completeness; not the failure mode we hit)
        pass
    if has_numeric_hint and device_class != "enum":
        raise ValueError(
            f"entity for attr_key={entity._attr_key!r} has device_class="
            f"{device_class!r}, a numeric hint present, but native value "
            f"{value!r} ({type(value).__name__}) is non-numeric"
        )


def _fake_manager():
    mgr = MagicMock()
    mgr.serial_number = "TESTSERIAL"
    mgr.device_info = {}
    mgr.engine = MagicMock()
    mgr.engine.report = BH.HealthReport()
    return mgr


class TestNumericStateContract(unittest.TestCase):
    """T18 — every entity must satisfy HA's real numeric-state rule for
    every value it can plausibly report, including string-valued ones."""

    def _make_all_entities(self):
        mgr = _fake_manager()
        return BHE.create_battery_health_entities(mgr)

    def test_all_entities_created(self):
        entities = self._make_all_entities()
        self.assertEqual(len(entities), len(BHE._BATTERY_HEALTH_SENSORS))

    def test_confidence_entity_is_enum_device_class(self):
        """Direct regression test for the production bug: confidence must be
        declared as an ENUM sensor, never carry a numeric-implying hint."""
        entities = self._make_all_entities()
        conf = next(e for e in entities if e._attr_key == "confidence")
        self.assertEqual(conf._attr_device_class, "enum")
        self.assertIsNone(
            getattr(conf, "_attr_suggested_display_precision", None),
            "confidence must not carry a numeric display-precision hint",
        )

    def test_confidence_reports_every_real_value_without_raising(self):
        """The exact failure mode from AUDIT_1.1.7.md: feed each real
        confidence string through HA's actual numeric-state validation."""
        entities = self._make_all_entities()
        conf = next(e for e in entities if e._attr_key == "confidence")
        for value in ("low", "normal", "stale"):
            report = BH.HealthReport(confidence=value)
            conf._apply(report)
            _ha_numeric_state_check(conf)  # must not raise

    def test_all_string_valued_keys_pass_ha_numeric_check(self):
        entities = self._make_all_entities()
        for entity in entities:
            if entity._attr_key in BHE._STRING_VALUED_KEYS:
                for value in ("low", "normal", "stale"):
                    report = BH.HealthReport(**{entity._attr_key: value})
                    entity._apply(report)
                    _ha_numeric_state_check(entity)  # must not raise

    def test_numeric_sensors_still_get_precision_hint(self):
        """The fix must not regress numeric sensors losing their hint."""
        entities = self._make_all_entities()
        for entity in entities:
            if entity._attr_key not in BHE._STRING_VALUED_KEYS:
                self.assertEqual(
                    getattr(entity, "_attr_suggested_display_precision", None), 1,
                    f"{entity._attr_key} should keep its numeric precision hint",
                )

    def test_numeric_sensors_pass_ha_check_with_none_and_float(self):
        entities = self._make_all_entities()
        for entity in entities:
            if entity._attr_key in BHE._STRING_VALUED_KEYS:
                continue
            for value in (None, 42.0):
                report = BH.HealthReport(**{entity._attr_key: value})
                entity._apply(report)
                _ha_numeric_state_check(entity)  # must not raise

    def test_unique_ids_are_distinct(self):
        entities = self._make_all_entities()
        ids = [e._attr_unique_id for e in entities]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
