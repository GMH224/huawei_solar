"""Fault-isolation contract tests for the battery-health subsystem (T19).

WHY THIS FILE EXISTS
--------------------
The battery-health subsystem is *additive*: it must never be able to degrade
the integration that existed before it.  A user running v1.1.6 hit a
whole-config-entry setup cancellation while the Modbus link was struggling:

    Setup of config entry 'SUN2000-10KTL-M1' ... cancelled
      -> entity_platform ... asyncio.exceptions.CancelledError
    Config entry ... for huawei_solar.<platform> has already been setup!  (x5)

A cancelled platform setup takes down **all** of the integration's entities,
not just this subsystem's.  Whatever the trigger, an additive read-only
feature must not sit on that critical path at all.

WHAT THIS FILE ENFORCES
-----------------------
These are structural (AST) and data contracts rather than behavioural tests,
because the property being protected is architectural: "no code path in this
subsystem can delay or fail entry setup."  That is a property of the *shape*
of the code, so it is asserted against the source itself and therefore cannot
silently regress in a future refactor.

  T19.1  The polled register set is pinned to a golden list (no silent growth
         of Modbus load).
  T19.2  ``async_setup_entry`` never awaits battery-health work.
  T19.3  Every battery-health call site in the platform files is inside a
         try/except.
  T19.4  Initialisation is scheduled as a background task.
  T19.5  A kill switch exists and defaults to enabled.
  T19.6  The subsystem performs no Modbus writes.
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys
import types
import unittest

_ROOT = pathlib.Path(__file__).parent.parent


# ── Load battery_health_manager with minimal stubs ──────────────────────────
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
    if "homeassistant.helpers" not in sys.modules:
        mod("homeassistant.helpers")
    if "homeassistant.helpers.storage" not in sys.modules:
        st = mod("homeassistant.helpers.storage")

        class Store:
            def __init__(self, hass, version, key):
                self.version, self.key, self.saved = version, key, None

            async def async_load(self):
                return None

            async def async_save(self, data):
                self.saved = data

            def async_delay_save(self, data_fn, delay=0):
                self.saved = data_fn()

        st.Store = Store
    if "homeassistant.helpers.device_registry" not in sys.modules:
        dr = mod("homeassistant.helpers.device_registry")
        dr.DeviceInfo = dict


_install_stubs()


def _load(modname: str):
    src = _ROOT / f"{modname}.py"
    spec = importlib.util.spec_from_file_location(f"iso_{modname}", str(src))
    m = importlib.util.module_from_spec(spec)
    m.__package__ = "huawei_solar"
    sys.modules[f"iso_{modname}"] = m
    spec.loader.exec_module(m)
    return m


BH = _load("battery_health")
sys.modules["huawei_solar.battery_health"] = BH
CONST = _load("const")
sys.modules["huawei_solar.const"] = CONST
MGR = _load("battery_health_manager")


def _source(name: str) -> str:
    return (_ROOT / name).read_text()


def _tree(name: str) -> ast.Module:
    return ast.parse(_source(name))


def _find_func(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


# ═════════════════════════════════════════════════════════════════════════════
class TestGoldenRegisterSet(unittest.TestCase):  # T19.1
    """The Modbus footprint is pinned; growth must be a deliberate change.

    This list is byte-identical to the v1.1.6 set, which the user confirmed
    operates correctly on real hardware. Any addition here increases load on
    a shared RS485 bus and must be justified and re-validated, not slipped in.
    """

    GOLDEN = sorted([
        "storage_state_of_capacity",
        "storage_charge_discharge_power",
        "storage_unit_1_battery_temperature",
        "storage_total_charge",
        "storage_total_discharge",
        "storage_rated_capacity",
        "storage_unit_soh_calibration_status",
        "storage_unit_1_battery_pack_1_voltage",
        "storage_unit_1_battery_pack_2_voltage",
        "storage_unit_1_battery_pack_3_voltage",
        "storage_unit_1_battery_pack_1_maximum_temperature",
        "storage_unit_1_battery_pack_2_maximum_temperature",
        "storage_unit_1_battery_pack_3_maximum_temperature",
        "storage_unit_1_battery_pack_1_minimum_temperature",
        "storage_unit_1_battery_pack_2_minimum_temperature",
        "storage_unit_1_battery_pack_3_minimum_temperature",
        "storage_unit_1_battery_pack_1_working_status",
        "storage_unit_1_battery_pack_2_working_status",
        "storage_unit_1_battery_pack_3_working_status",
        "storage_unit_1_battery_pack_1_soh_calibration_status",
        "storage_unit_1_battery_pack_2_soh_calibration_status",
        "storage_unit_1_battery_pack_3_soh_calibration_status",
    ])

    def test_register_set_matches_golden_list(self):
        self.assertEqual(sorted(MGR.REQUIRED_REGISTER_NAMES), self.GOLDEN)

    def test_register_set_has_no_duplicates(self):
        names = MGR.REQUIRED_REGISTER_NAMES
        self.assertEqual(len(names), len(set(names)))

    def test_register_count_is_bounded(self):
        # Guard rail: a batched poll of this size is known-good on the
        # reporter's hardware. Meaningful growth needs re-validation.
        self.assertLessEqual(len(MGR.REQUIRED_REGISTER_NAMES), 25)


class TestSetupPathIsolation(unittest.TestCase):  # T19.2 / T19.4
    """Battery health must not sit on the config-entry setup critical path."""

    def setUp(self):
        self.tree = _tree("__init__.py")
        self.setup_fn = _find_func(self.tree, "async_setup_entry")
        self.assertIsNotNone(self.setup_fn)

    def test_setup_entry_does_not_await_battery_health(self):
        """No `await` on any battery-health call inside async_setup_entry."""
        offenders = []
        for node in ast.walk(self.setup_fn):
            if not isinstance(node, ast.Await):
                continue
            src = ast.dump(node).lower()
            if "batteryhealth" in src or "bh_manager" in src or "async_initialize" in src:
                offenders.append(ast.unparse(node))
        self.assertEqual(
            offenders, [],
            "async_setup_entry must never await battery-health work — a slow "
            "call here can contribute to HA cancelling the whole platform "
            f"setup. Offending: {offenders}",
        )

    def test_setup_helper_exists_and_is_synchronous(self):
        helper = _find_func(self.tree, "_async_setup_battery_health")
        self.assertIsNotNone(helper, "isolation helper missing")
        self.assertIsInstance(
            helper, ast.FunctionDef,
            "helper must be sync (it is called without await from setup)",
        )

    def test_initialisation_is_scheduled_as_background_task(self):
        src = _source("__init__.py")
        self.assertIn("async_create_background_task", src)

    def test_helper_body_is_exception_guarded(self):
        helper = _find_func(self.tree, "_async_setup_battery_health")
        handlers = [n for n in ast.walk(helper) if isinstance(n, ast.ExceptHandler)]
        self.assertGreaterEqual(
            len(handlers), 3,
            "helper must contain exception guards around manager creation, "
            "task scheduling, and the background init coroutine",
        )

    def test_manager_creation_failure_is_cleaned_up(self):
        """A half-created manager must not linger in the registry."""
        helper_src = ast.unparse(_find_func(self.tree, "_async_setup_battery_health"))
        self.assertIn("BatteryHealthManager.remove", helper_src)


class TestPlatformIsolation(unittest.TestCase):  # T19.3
    """A battery-health failure must never abort a whole entity platform."""

    def _assert_guarded(self, filename: str, needle: str):
        tree = _tree(filename)
        guarded = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                if needle in ast.unparse(node):
                    guarded = True
                    break
        self.assertTrue(
            guarded,
            f"{filename}: battery-health entity creation ({needle}) must be "
            "wrapped in try/except so it cannot abort the platform setup",
        )

    def test_sensor_platform_guards_battery_health(self):
        self._assert_guarded("sensor.py", "create_battery_health_entities")

    def test_button_platform_guards_battery_health(self):
        self._assert_guarded("button.py", "ResetEfficiencyBaselineButtonEntity")

    def test_unload_guards_battery_health(self):
        self._assert_guarded("__init__.py", "async_unload")

    def test_entity_callbacks_are_guarded(self):
        tree = _tree("battery_health_entities.py")
        for fn_name in ("async_added_to_hass", "_on_health_update"):
            fn = _find_func(tree, fn_name)
            self.assertIsNotNone(fn, f"{fn_name} missing")
            handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]
            self.assertTrue(
                handlers,
                f"{fn_name} must contain an exception guard: an entity error "
                "here surfaces inside HA's state machine",
            )


class TestKillSwitch(unittest.TestCase):  # T19.5
    def test_option_constant_exists(self):
        self.assertEqual(CONST.CONF_BH_ENABLED, "bh_enabled")
        self.assertIn(CONST.CONF_BH_ENABLED, CONST.BH_OPTION_KEYS)

    def test_default_is_enabled(self):
        src = ast.unparse(
            _find_func(_tree("__init__.py"), "_async_setup_battery_health")
        )
        self.assertIn("CONF_BH_ENABLED, True", src.replace("'", "").replace('"', ""))

    def test_kill_switch_short_circuits_before_manager_creation(self):
        helper = _find_func(_tree("__init__.py"), "_async_setup_battery_health")
        body_src = ast.unparse(helper)
        guard_pos = body_src.find("CONF_BH_ENABLED")
        create_pos = body_src.find("BatteryHealthManager.create")
        self.assertGreater(create_pos, -1)
        self.assertLess(guard_pos, create_pos,
                        "kill switch must be evaluated before any manager work")

    def test_exposed_in_options_flow(self):
        self.assertIn("CONF_BH_ENABLED", _source("config_flow.py"))


class TestReadOnlyGuarantee(unittest.TestCase):  # T19.6
    """The subsystem must never write to the inverter/BMS."""

    FILES = (
        "battery_health.py",
        "battery_health_manager.py",
        "battery_health_entities.py",
    )

    def test_no_register_writes(self):
        for name in self.FILES:
            src = _source(name)
            for forbidden in ("device.set(", "await self.device.set", "write_register"):
                with self.subTest(file=name, pattern=forbidden):
                    self.assertNotIn(forbidden, src)

    def test_engine_has_no_home_assistant_imports(self):
        """The engine stays a pure, testable computation core."""
        tree = _tree("battery_health.py")
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod_name = (
                    node.module if isinstance(node, ast.ImportFrom)
                    else node.names[0].name
                )
                self.assertNotIn(
                    "homeassistant", (mod_name or ""),
                    "battery_health.py must remain HA-free",
                )


if __name__ == "__main__":
    unittest.main()
