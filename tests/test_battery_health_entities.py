"""Entity-contract tests for the battery-health sensors (T18).

WHY THIS FILE EXISTS
--------------------
v1.1.5/v1.1.6 shipped a defect that no existing test could catch: the entity
class declared ``_attr_suggested_display_precision = 1`` at *class* level, so
every battery-health sensor inherited it — including ``confidence``, whose
native value is the string "low"/"normal"/"stale".

Home Assistant's ``SensorEntity.state`` property treats ANY numeric-implying
hint (unit, state_class, device_class, or a display-precision hint) as a
promise that the value is numeric, and raises ``ValueError`` otherwise.  In
production this meant:

  * ``Error adding entity sensor...battery_health_confidence`` at setup, and
  * the same ValueError on *every* subsequent coordinator tick.

The engine test-suite (T1-T17) never caught it because it exercised the pure
computation core and never instantiated a real ``SensorEntity`` subclass
against HA's own state-validation rule.

WHAT THIS FILE DOES
-------------------
Re-implements HA's actual validation rule (not a mock of our own code) and
runs *every* real battery-health entity, with its real resolved attributes,
through that rule for *every* value it can plausibly report.  This is a
value-domain test rather than a code-path test — the bug class it targets only
manifests for certain values, not certain branches.

``TestRegressionGuard`` additionally proves the harness is load-bearing by
reconstructing the v1.1.6 defect and asserting the validator rejects it.
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types
import unittest

_ROOT = pathlib.Path(__file__).parent.parent


def _run(coro):
    return asyncio.run(coro)


# ── Minimal HA stubs (only what the entities module imports) ─────────────────
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

    # homeassistant.const
    if "homeassistant.const" not in sys.modules:
        const = mod("homeassistant.const")
    else:
        const = sys.modules["homeassistant.const"]
    const.PERCENTAGE = "%"

    class EntityCategory:
        DIAGNOSTIC = "diagnostic"
        CONFIG = "config"
    const.EntityCategory = EntityCategory

    # homeassistant.components.sensor
    if "homeassistant.components" not in sys.modules:
        mod("homeassistant.components")
    sensor = mod("homeassistant.components.sensor")

    class SensorDeviceClass:
        ENUM = "enum"
        BATTERY = "battery"

    class SensorStateClass:
        MEASUREMENT = "measurement"
        TOTAL_INCREASING = "total_increasing"

    class SensorEntity:
        """Stub carrying only the attribute-resolution behaviour we test.

        Real HA resolves ``self.x`` from ``self._attr_x``; we mirror that so the
        production entity class is exercised unmodified.
        """

        _attr_native_value = None
        _attr_native_unit_of_measurement = None
        _attr_device_class = None
        _attr_state_class = None
        _attr_suggested_display_precision = None
        _attr_options = None
        _attr_available = True
        _attr_extra_state_attributes: dict | None = None

        @property
        def native_value(self):
            return self._attr_native_value

        @property
        def native_unit_of_measurement(self):
            return self._attr_native_unit_of_measurement

        @property
        def device_class(self):
            return getattr(self, "_attr_device_class", None)

        @property
        def state_class(self):
            return getattr(self, "_attr_state_class", None)

        @property
        def suggested_display_precision(self):
            return getattr(self, "_attr_suggested_display_precision", None)

        @property
        def options(self):
            return getattr(self, "_attr_options", None)

        def async_write_ha_state(self):
            # Real HA computes state here, which is exactly where the
            # production crash occurred. Mirror that so guarded callbacks are
            # tested against a realistic failure surface.
            ha_sensor_state(self)

    sensor.SensorEntity = SensorEntity
    sensor.SensorDeviceClass = SensorDeviceClass
    sensor.SensorStateClass = SensorStateClass
    return sensor


# ── Faithful re-implementation of HA's SensorEntity.state validation ─────────
def ha_sensor_state(entity):
    """Mirror of homeassistant/components/sensor/__init__.py::state.

    Kept deliberately close to the upstream logic (and its error text) so this
    test fails for the same reason production would, not for a reason of our
    own invention.
    """
    value = entity.native_value
    device_class = entity.device_class
    state_class = entity.state_class
    unit = entity.native_unit_of_measurement
    precision = entity.suggested_display_precision

    # ENUM sensors are the sanctioned way to report a string state.
    if device_class == "enum":
        options = entity.options
        if not options:
            raise ValueError(
                f"Sensor {getattr(entity, '_attr_unique_id', '?')} has device "
                "class 'enum' but does not declare options"
            )
        if state_class is not None or unit is not None or precision is not None:
            raise ValueError(
                f"Sensor {getattr(entity, '_attr_unique_id', '?')} has device "
                "class 'enum' and must not declare state_class, unit, or "
                "display precision"
            )
        if value is not None and value not in options:
            raise ValueError(
                f"Sensor {getattr(entity, '_attr_unique_id', '?')} provides "
                f"state value '{value}' which is not in the options list"
            )
        return value

    # Any numeric-implying hint makes a non-numeric value an error.
    if value is not None and (
        state_class is not None
        or unit is not None
        or precision is not None
        or device_class is not None
    ):
        try:
            int(value)
        except (TypeError, ValueError):
            try:
                float(value)
            except (TypeError, ValueError) as err:
                raise ValueError(
                    f"Sensor {getattr(entity, '_attr_unique_id', '?')} has "
                    f"device class '{device_class}', state class "
                    f"'{state_class}' unit '{unit}' and suggested precision "
                    f"'{precision}' thus indicating it has a numeric value; "
                    f"however, it has the non-numeric value: {value!r} "
                    f"({type(value)})"
                ) from err
    return value


_SENSOR_MOD = _install_stubs()


def _load(modname: str):
    src = _ROOT / f"{modname}.py"
    spec = importlib.util.spec_from_file_location(f"bhe_{modname}", str(src))
    m = importlib.util.module_from_spec(spec)
    m.__package__ = "huawei_solar"
    sys.modules[f"bhe_{modname}"] = m
    spec.loader.exec_module(m)
    return m


BH = _load("battery_health")
sys.modules["huawei_solar.battery_health"] = BH

# battery_health_entities imports BatteryHealthManager for typing only; stub it
_bhm = types.ModuleType("huawei_solar.battery_health_manager")


class _StubManager:
    def __init__(self, serial="TESTSERIAL", report=None, raise_on_add=False):
        self.serial_number = serial
        self.device_info = {"identifiers": {("huawei_solar", serial)}}
        self.engine = types.SimpleNamespace(report=report or BH.HealthReport())
        self.listeners = []
        self._raise_on_add = raise_on_add

    def add_listener(self, cb):
        if self._raise_on_add:
            raise RuntimeError("simulated manager failure")
        self.listeners.append(cb)

    def remove_listener(self, cb):
        if cb in self.listeners:
            self.listeners.remove(cb)


_bhm.BatteryHealthManager = _StubManager
sys.modules["huawei_solar.battery_health_manager"] = _bhm
sys.modules["bhe_battery_health_manager"] = _bhm

_ENT = _load("battery_health_entities")


def _entities(report=None, **kw):
    return _ENT.create_battery_health_entities(_StubManager(report=report, **kw))


#: Every value each sensor can plausibly report, including boundaries.
_PLAUSIBLE_VALUES: dict[str, list] = {
    "confidence": ["low", "normal", "stale", None],
    "bhi": [None, 0.0, 50.0, 87.5, 100.0],
    "soh_capacity": [None, 0.0, 98.6, 100.0],
    "soh_efficiency": [None, 0.0, 84.0, 100.0],
    "soh_balance": [None, 0.0, 87.9, 100.0],
    "stress_index": [None, 0.0, 95.2, 100.0],
    "predicted_soh": [None, 0.0, 97.5, 100.0],
    "health_divergence": [None, -12.5, 0.0, 3.1],
    "efc": [None, 0.0, 150.0, 4000.0],
    "warranty_consumed_pct": [None, 0.0, 5.8, 100.0],
}


class TestEntityStateContract(unittest.TestCase):  # T18
    """Every entity × every value it can report must satisfy HA's rule."""

    def test_all_entities_all_values_are_valid_states(self):
        for ent in _entities():
            key = ent._attr_key
            self.assertIn(key, _PLAUSIBLE_VALUES, f"untested sensor key: {key}")
            for value in _PLAUSIBLE_VALUES[key]:
                with self.subTest(sensor=key, value=value):
                    ent._attr_native_value = value
                    # Must not raise — this is the production crash surface.
                    self.assertEqual(ha_sensor_state(ent), value)

    def test_every_declared_sensor_is_covered_by_the_value_matrix(self):
        keys = {e._attr_key for e in _entities()}
        self.assertEqual(
            keys, set(_PLAUSIBLE_VALUES),
            "sensor list and test value matrix have drifted apart",
        )


class TestConfidenceSensorDeclaration(unittest.TestCase):  # T18
    """The specific defect from v1.1.6, pinned."""

    def _confidence(self):
        return next(e for e in _entities() if e._attr_key == "confidence")

    def test_confidence_is_declared_as_enum(self):
        ent = self._confidence()
        self.assertEqual(ent.device_class, "enum")
        self.assertEqual(ent.options, ["low", "normal", "stale"])

    def test_confidence_has_no_numeric_hints(self):
        ent = self._confidence()
        self.assertIsNone(ent.suggested_display_precision)
        self.assertIsNone(ent.state_class)
        self.assertIsNone(ent.native_unit_of_measurement)

    def test_confidence_options_match_engine_outputs(self):
        """Guard against the engine and the entity drifting apart."""
        produced = set()
        cfg = BH.BatteryHealthConfig()
        cfg.freshness_tau_kwh = 1e12
        cfg.confidence_min_segments = 1
        cfg.eff_baseline_windows = 1
        cfg.capacity_window_days = 365.0
        eng = BH.BatteryHealthEngine(cfg)

        def S(ts, **kw):
            return BH.HealthSample(timestamp=ts, **kw)

        produced.add(eng.update(
            S(0.0, soc=50.0, power_w=0.0, battery_temp_c=20.0,
              lifetime_charge_kwh=0.0, lifetime_discharge_kwh=0.0)
        ).confidence)
        t = 0.0
        for i in range(21):
            f = i / 20
            eng.update(S(t + i * 60, soc=95 - 20 * f, power_w=-2500.0,
                         battery_temp_c=20.0, lifetime_charge_kwh=100.0,
                         lifetime_discharge_kwh=100.0 + 4 * f))
        t += 22 * 60
        eng.update(S(t, soc=75.0, power_w=0.0, battery_temp_c=20.0,
                     lifetime_charge_kwh=100.0, lifetime_discharge_kwh=104.0))
        eng.efficiency.feed(S(t + 60, soc=99.0, power_w=0.0,
                              lifetime_charge_kwh=200.0,
                              lifetime_discharge_kwh=150.0))
        eng.efficiency.feed(S(t + 86400, soc=99.0, power_w=0.0,
                              lifetime_charge_kwh=240.0,
                              lifetime_discharge_kwh=188.0))
        produced.add(eng.update(
            S(t + 86400 + 60, soc=50.0, power_w=0.0, battery_temp_c=20.0,
              lifetime_charge_kwh=240.0, lifetime_discharge_kwh=188.0)
        ).confidence)
        produced.add(eng.update(
            S(t + 61 * 86400, soc=50.0, power_w=0.0, battery_temp_c=20.0,
              lifetime_charge_kwh=240.0, lifetime_discharge_kwh=188.0)
        ).confidence)

        self.assertTrue(produced <= set(_ENT._CONFIDENCE_OPTIONS),
                        f"engine produced confidence values outside options: "
                        f"{produced - set(_ENT._CONFIDENCE_OPTIONS)}")


class TestNumericSensorsKeepPrecision(unittest.TestCase):  # T18
    def test_numeric_sensors_have_precision_hint(self):
        for ent in _entities():
            if ent._attr_key in _ENT._STRING_VALUED_KEYS:
                continue
            with self.subTest(sensor=ent._attr_key):
                self.assertEqual(ent.suggested_display_precision, 1)


class TestGuardedCallbacks(unittest.TestCase):  # T18 / fault isolation
    def test_added_to_hass_survives_manager_failure(self):
        """A broken manager must not prevent the entity from being added."""
        ents = _entities(raise_on_add=True)
        for ent in ents:
            with self.subTest(sensor=ent._attr_key):
                _run(ent.async_added_to_hass())   # must not raise

    def test_update_callback_survives_bad_report(self):
        ent = next(e for e in _entities() if e._attr_key == "bhi")
        _run(ent.async_added_to_hass())

        class _Exploding:
            def __getattr__(self, item):
                raise RuntimeError("simulated bad report")

        ent._on_health_update(_Exploding())        # must not raise

    def test_update_callback_writes_valid_state_for_real_report(self):
        report = BH.HealthReport(bhi=93.2, confidence="normal")
        for ent in _entities(report=report):
            with self.subTest(sensor=ent._attr_key):
                ent._on_health_update(report)      # calls ha_sensor_state


class TestRegressionGuard(unittest.TestCase):  # T18
    """Proof the harness would have caught the original production bug."""

    def test_class_level_precision_on_string_sensor_is_rejected(self):
        ent = next(e for e in _entities() if e._attr_key == "confidence")
        # Reconstruct the v1.1.6 defect exactly: numeric hint + string value.
        ent._attr_device_class = None
        ent._attr_options = None
        ent._attr_suggested_display_precision = 1
        ent._attr_native_value = "low"
        with self.assertRaises(ValueError) as ctx:
            ha_sensor_state(ent)
        self.assertIn("non-numeric value", str(ctx.exception))

    def test_enum_without_options_is_rejected(self):
        ent = next(e for e in _entities() if e._attr_key == "confidence")
        ent._attr_options = None
        ent._attr_native_value = "low"
        with self.assertRaises(ValueError):
            ha_sensor_state(ent)

    def test_enum_with_unexpected_state_is_rejected(self):
        ent = next(e for e in _entities() if e._attr_key == "confidence")
        ent._attr_native_value = "excellent"       # not in options
        with self.assertRaises(ValueError):
            ha_sensor_state(ent)


if __name__ == "__main__":
    unittest.main()
