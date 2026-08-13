"""Fault-isolation contract tests for the battery-health subsystem (T19).

WHY THIS FILE EXISTS
--------------------
The battery-health subsystem is *additive*: it must never be able to degrade
the integration that existed before it.  A user running v1.1.6 hit a
whole-config-entry setup cancellation while the Modbus link was struggling:

    Setup of config entry '<inverter>' ... cancelled
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
from unittest.mock import AsyncMock, MagicMock

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


# v2.0.0: battery_health_manager.py now imports Quality from
# .register_cache, which does `from huawei_solar import RegisterName,
# Result`. Neither battery_health.py nor battery_health_manager.py import
# anything from huawei_solar directly (confirmed) -- register_cache.py is
# the only reason this test needs huawei_solar to exist at all, and it
# only needs these two names, not the real package's full device/
# modbus_client/register_client chain (which needs the real vendor
# huawei_solar.const, not this project's own const.py this file
# substitutes below for its own relative-import trick -- pulling in that
# real chain here caused exactly that collision, and is unnecessary).
# setdefault, matching register_cache.py's own test file's established
# pattern: only installs the stub if nothing (real or another test's
# stub) is already there, never clobbering a working sys.modules entry.
if "huawei_solar" not in sys.modules:
    _hs = types.ModuleType("huawei_solar")
    _hs.RegisterName = str  # type: ignore[attr-defined]

    class _Result:
        def __init__(self, v):
            self.value = v

    _hs.Result = _Result  # type: ignore[attr-defined]
    sys.modules["huawei_solar"] = _hs

BH = _load("battery_health")
sys.modules["huawei_solar.battery_health"] = BH
CONST = _load("const")
sys.modules["huawei_solar.const"] = CONST
RC = _load("register_cache")
sys.modules["huawei_solar.register_cache"] = RC
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
class TestTOPO01UnitDiscovery(unittest.TestCase):  # v2.0.7 (TOPO-01 done properly)
    """_active_storage_units() must reuse the SAME proven capability
    flags __init__.py's own battery_1_device_info/battery_2_device_info
    already use -- never a live probe of a possibly-absent unit (see
    that function's own docstring for why: RegisterClient.get_multiple()
    fails the WHOLE batch, not just one register, when any register in
    it doesn't exist)."""

    class _FakeStorageProductModel:
        NONE = 0
        HUAWEI_LUNA2000 = 1

    def _device(self, battery_2_type):
        d = type("FakeDevice", (), {})()
        d.battery_2_type = battery_2_type
        return d

    def test_single_unit_by_default(self):
        d = self._device(self._FakeStorageProductModel.NONE)
        self.assertEqual(MGR._active_storage_units(d), [1])

    def test_second_unit_included_when_present(self):
        d = self._device(self._FakeStorageProductModel.HUAWEI_LUNA2000)
        self.assertEqual(MGR._active_storage_units(d), [1, 2])

    def test_missing_device_or_attribute_defaults_to_single_unit_not_a_crash(self):
        """Adversarial: a coordinator whose .device is None, or a device
        object with no battery_2_type at all, must default to the safe,
        already-proven single-unit case -- not raise, and NEVER
        speculatively include unit 2 when its presence can't actually be
        confirmed."""
        self.assertEqual(MGR._active_storage_units(None), [1])
        bare_device = type("BareDevice", (), {})()  # no battery_2_type attr
        self.assertEqual(MGR._active_storage_units(bare_device), [1])

    def test_pack_slots_cover_all_three_packs_per_active_unit(self):
        self.assertEqual(
            MGR.pack_slots_for_units([1]),
            [(1, 1), (1, 2), (1, 3)],
        )
        self.assertEqual(
            MGR.pack_slots_for_units([1, 2]),
            [(1, 1), (1, 2), (1, 3), (2, 1), (2, 2), (2, 3)],
        )

    def test_required_register_names_scales_with_unit_count(self):
        one_unit = MGR.required_register_names([1])
        two_units = MGR.required_register_names([1, 2])
        self.assertEqual(len(two_units) - len(one_unit), 3 * len(MGR._PACK_FIELD_SUFFIXES))
        # Every unit-2 pack register must genuinely be unit-2-addressed,
        # not accidentally duplicated unit-1 names.
        for pack in range(1, MGR.PACK_COUNT + 1):
            self.assertIn(
                MGR._pack_register_name(2, pack, "voltage"), two_units,
            )
            self.assertNotIn(
                MGR._pack_register_name(2, pack, "voltage"), one_unit,
            )

    def test_unit_2_never_read_when_absent(self):
        """The core safety guarantee: with only unit 1 active, ZERO
        unit-2 registers appear in the required list at all -- confirms
        this is a hard gate, not merely a preference."""
        names = MGR.required_register_names([1])
        for name in names:
            self.assertNotIn("storage_unit_2", name)


class TestGoldenRegisterSet(unittest.TestCase):  # T19.1
    """The Modbus footprint is pinned; growth must be a deliberate change.

    This list is byte-identical to the v1.1.6 set, which the user confirmed
    operates correctly on real hardware. Any addition here increases load on
    a shared RS485 bus and must be justified and re-validated, not slipped in.
    """

    #: v1.2.0 CHANGE (deliberate, +1 register): storage_charging_cutoff_capacity
    #: (47081) was added because "full" must be defined relative to the
    #: CONFIGURED end-of-charge SOC, not an absolute 100%.  Field evidence: a
    #: 93%/95% configured cap meant an absolute gate produced ZERO efficiency
    #: anchors for 122 consecutive days and zero balance samples for 78.
    #: Cost: one U16 register, adjacent to the storage control block already
    #: polled by the integration's own end-of-charge SOC entity.
    #: v2.0.6 CHANGE (deliberate, +12 registers): Tier 3 of the battery
    #: health architecture review added PackCapacityTracker -- a direct,
    #: measured per-pack capacity estimate (the same segment-detection
    #: approach the unit-level tracker already uses), chosen over a
    #: simpler dV/dT-only balance proxy once per-pack SOC/power/lifetime
    #: charge/discharge counters were confirmed to exist with the same
    #: units/gain as their unit-level equivalents. Cost: 12 registers (3
    #: packs x 4 fields), but all four sit in a tight, PDU-adjacent block
    #: (38229-38240) alongside storage_unit_1_battery_pack_N_voltage,
    #: already read every poll -- confirmed against the real register map
    #: before adding these, not assumed. The two counter fields per pack
    #: (total_charge/total_discharge) specifically need NORMAL tier, not
    #: the SLOW tier their name would otherwise default to -- see
    #: register_cache.py's own _TIER_OVERRIDES for the same reasoning
    #: already established for the unit-level counters.
    #: v2.0.7 CHANGE (deliberate, +6 registers): Section F of this
    #: release wires up per-pack current and serial number -- confirmed
    #: present in the underlying huawei-solar register map, PDU-adjacent
    #: to the other per-pack fields already polled every tick, so the
    #: marginal bus-traffic cost is expected to be low, same reasoning as
    #: v2.0.6's own addition above. Raw data only this release -- neither
    #: field is yet consumed by any capacity/SOH computation (that's
    #: Architecture Phases 2/3, deliberately deferred); this addition
    #: exists so no further register-map change is needed once that work
    #: happens.
    GOLDEN = sorted([
        "storage_charging_cutoff_capacity",
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
        "storage_unit_1_battery_pack_1_state_of_capacity",
        "storage_unit_1_battery_pack_2_state_of_capacity",
        "storage_unit_1_battery_pack_3_state_of_capacity",
        "storage_unit_1_battery_pack_1_charge_discharge_power",
        "storage_unit_1_battery_pack_2_charge_discharge_power",
        "storage_unit_1_battery_pack_3_charge_discharge_power",
        "storage_unit_1_battery_pack_1_total_charge",
        "storage_unit_1_battery_pack_2_total_charge",
        "storage_unit_1_battery_pack_3_total_charge",
        "storage_unit_1_battery_pack_1_total_discharge",
        "storage_unit_1_battery_pack_2_total_discharge",
        "storage_unit_1_battery_pack_3_total_discharge",
        "storage_unit_1_battery_pack_1_current",
        "storage_unit_1_battery_pack_2_current",
        "storage_unit_1_battery_pack_3_current",
        "storage_unit_1_battery_pack_1_serial_number",
        "storage_unit_1_battery_pack_2_serial_number",
        "storage_unit_1_battery_pack_3_serial_number",
    ])

    def test_register_set_matches_golden_list(self):
        self.assertEqual(sorted(MGR.REQUIRED_REGISTER_NAMES), self.GOLDEN)

    def test_register_set_has_no_duplicates(self):
        names = MGR.REQUIRED_REGISTER_NAMES
        self.assertEqual(len(names), len(set(names)))

    def test_register_count_is_bounded(self):
        # Guard rail: a batched poll of this size is known-good on the
        # reporter's hardware. Meaningful growth needs re-validation.
        # v2.0.6: raised from 25 to 40 for Tier 3's own deliberate +12
        # (see GOLDEN's own comment above) -- still a guard rail against
        # further, unreviewed growth, not a removal of one.
        # v2.0.7: raised from 40 to 46 for Section F's own deliberate +6
        # (see GOLDEN's own comment above) -- same reasoning.
        self.assertLessEqual(len(MGR.REQUIRED_REGISTER_NAMES), 46)


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


class TestQualityGatedValueExtraction(unittest.TestCase):  # v2.0.0
    """MGR._value()'s quality-gating -- the specific vulnerability that
    motivated this entire rebuild (V2_ARCHITECTURE_DESIGN.md §1's opening
    paragraph, §10.4's deliberate exception). Uses the REAL RegisterCache
    (RC, already loaded above), not a fake -- this is exactly the
    integration point where a fake could hide a real mismatch.
    """

    def _cache_with(self, name, value, quality):
        cache = RC.RegisterCache()
        cache.update({name: RC.Result(value)})
        if quality != RC.Quality.GOOD:
            cache.record_attempt([name], quality, RC.Reason.LINK_DOWN)
        return cache

    def test_good_quality_value_is_returned(self):
        cache = self._cache_with("soc", 80, RC.Quality.GOOD)
        data = {"soc": cache.get("soc")}
        self.assertEqual(MGR._value(cache, data, "soc"), 80)

    def test_uncertain_quality_value_is_treated_as_none(self):
        """The core fix: a stale-served UNCERTAIN value must NOT be
        silently trusted as current by this consumer -- unlike a display
        entity, where serving UNCERTAIN is correct and intended."""
        cache = self._cache_with("soc", 80, RC.Quality.UNCERTAIN)
        data = {"soc": cache.get("soc")}
        self.assertIsNotNone(
            data["soc"], "sanity: merge()/get() must still serve UNCERTAIN "
            "to a normal consumer -- this is the v2.0.0 fix working as intended"
        )
        self.assertIsNone(
            MGR._value(cache, data, "soc"),
            "but THIS consumer must treat a stale-served UNCERTAIN reading "
            "as None, not silently trust it as current -- it builds deltas "
            "from sequential readings, unlike a display entity",
        )

    def test_bad_quality_value_is_treated_as_none(self):
        cache = RC.RegisterCache()  # never read at all -- NEVER_READ, BAD
        self.assertIsNone(MGR._value(cache, {}, "soc"))

    def test_missing_from_data_is_none_regardless_of_cache_quality(self):
        # Existing behaviour, unaffected by the quality gate: if the
        # register simply isn't in `data` this cycle, None regardless of
        # what quality_of() would separately say.
        cache = self._cache_with("soc", 80, RC.Quality.GOOD)
        self.assertIsNone(MGR._value(cache, {}, "soc"))

    def test_adversarial_old_unguarded_extraction_would_have_trusted_the_stale_value(self):
        """Proves the vulnerability was real, not hypothetical: reproduces
        the OLD (pre-v2.0.0) _value(data, name) signature -- no quality
        check at all -- and shows it WOULD have returned the stale value
        as if it were current, unlike the fixed version."""
        def _old_value(data, name):
            result = data.get(name)
            if result is None:
                return None
            return getattr(result, "value", result)

        cache = self._cache_with("soc", 80, RC.Quality.UNCERTAIN)
        data = {"soc": cache.get("soc")}
        old_result = _old_value(data, "soc")
        new_result = MGR._value(cache, data, "soc")
        self.assertEqual(old_result, 80, "sanity: the old, unguarded extraction trusted it")
        self.assertIsNone(new_result, "the new, quality-gated extraction correctly does not")

    def test_rated_capacity_diagnostic_check_is_also_quality_gated(self):
        """The second call site fixed alongside _build_sample()'s 14 --
        the log-and-watch check for a Huawei SOH calibration step. Checked
        at the source level: confirms it was updated to pass the cache
        through, not just the more heavily-tested segment-building path."""
        src = _source("battery_health_manager.py")
        idx = src.find("Log-and-watch")
        window = src[idx: idx + 500]
        self.assertIn(
            "_value(coordinator.cache", window,
            "the rated-capacity diagnostic read must also be quality-gated, "
            "not just the segment-building fields",
        )


class TestSectionEManagerSnapshot(unittest.TestCase):  # v2.0.7
    """Section E, this release: BatteryHealthManager.snapshot() is the
    integration point telemetry_capture.py consumes, same public role
    AdaptiveModbusController.snapshot()/ModbusTelemetry.snapshot() play
    for their own subsystems."""

    def test_snapshot_includes_topology_and_report_attributes(self):
        mgr = object.__new__(MGR.BatteryHealthManager)
        mgr._active_units = [1]
        mgr._pack_slots = MGR.pack_slots_for_units([1])
        mgr.engine = BH.BatteryHealthEngine(
            pack_count=len(mgr._pack_slots),
            pack_slot_labels=[f"u{u}p{p}" for u, p in mgr._pack_slots],
        )
        # Force a report to exist -- update() populates self._last_report.
        mgr.engine.update(BH.HealthSample(
            timestamp=0.0, soc=90.0, power_w=-2500.0,
            lifetime_discharge_kwh=0.0, charge_ceiling_soc=100.0,
        ))
        snap = mgr.snapshot()
        self.assertEqual(snap["active_units"], [1])
        self.assertEqual(snap["pack_slots"], ["u1p1", "u1p2", "u1p3"])
        self.assertIn("bhi", snap)
        self.assertIn("confidence", snap)
        # Report attributes (Section E's own condition_coverage etc.)
        # must be present, confirming the wholesale merge actually works.
        self.assertIn("condition_coverage", snap)
        self.assertIn("pack_current_share_deviation_pct", snap)

    def test_snapshot_reflects_multi_unit_topology(self):
        mgr = object.__new__(MGR.BatteryHealthManager)
        mgr._active_units = [1, 2]
        mgr._pack_slots = MGR.pack_slots_for_units([1, 2])
        mgr.engine = BH.BatteryHealthEngine(
            pack_count=len(mgr._pack_slots),
            pack_slot_labels=[f"u{u}p{p}" for u, p in mgr._pack_slots],
        )
        mgr.engine.update(BH.HealthSample(
            timestamp=0.0, soc=90.0, power_w=-2500.0,
            lifetime_discharge_kwh=0.0, charge_ceiling_soc=100.0,
        ))
        snap = mgr.snapshot()
        self.assertEqual(snap["active_units"], [1, 2])
        self.assertEqual(len(snap["pack_slots"]), 6)
        self.assertIn("u2p1", snap["pack_slots"])


class TestSectionFCurrentAndSerialWiring(unittest.TestCase):  # v2.0.7
    """Section F, this release: per-pack current and serial number,
    confirmed present in the underlying register map but not previously
    read at all. Verifies the actual wiring end to end -- register names
    declared, included in required_register_names(), and populated onto
    PackSample by _build_sample() -- not just that the fields exist on
    the dataclass."""

    @staticmethod
    def _cache_with_all(values):
        cache = RC.RegisterCache()
        for name, value in values.items():
            cache.update({name: RC.Result(value, None)})
        return cache

    def test_pack_current_registers_are_in_required_register_names(self):
        names = MGR.required_register_names([1])
        for pack in range(1, MGR.PACK_COUNT + 1):
            self.assertIn(MGR._pack_register_name(1, pack, "current"), names)

    def test_pack_serial_registers_are_in_required_register_names(self):
        names = MGR.required_register_names([1])
        for pack in range(1, MGR.PACK_COUNT + 1):
            self.assertIn(MGR._pack_register_name(1, pack, "serial_number"), names)

    def test_build_sample_populates_current_and_serial_per_pack(self):
        mgr = object.__new__(MGR.BatteryHealthManager)
        mgr.coordinator = type("FakeCoord", (), {})()
        mgr._pack_slots = MGR.pack_slots_for_units([1])
        mgr._ambient_entity = None
        mgr._ambient_warned = False

        values = {}
        for pack in range(1, MGR.PACK_COUNT + 1):
            values[MGR._pack_register_name(1, pack, "working_status")] = (
                MGR.PACK_WORKING_STATUS_RUNNING
            )
            values[MGR._pack_register_name(1, pack, "voltage")] = 53.0
            values[MGR._pack_register_name(1, pack, "maximum_temperature")] = 25.0
            values[MGR._pack_register_name(1, pack, "minimum_temperature")] = 24.0
            values[MGR._pack_register_name(1, pack, "state_of_capacity")] = 80.0
            values[MGR._pack_register_name(1, pack, "charge_discharge_power")] = -1200.0
            values[MGR._pack_register_name(1, pack, "total_charge")] = 100.0
            values[MGR._pack_register_name(1, pack, "total_discharge")] = 95.0
            values[MGR._pack_register_name(1, pack, "soh_calibration_status")] = 0
            values[MGR._pack_register_name(1, pack, "current")] = -22.5 - pack
            values[MGR._pack_register_name(1, pack, "serial_number")] = f"SN-PACK-{pack}"
        values[MGR._RN_UNIT_CALIBRATION] = 0

        cache = self._cache_with_all(values)
        data = {name: cache.get(name) for name in values}

        mgr.coordinator.cache = cache
        sample = mgr._build_sample(data)

        self.assertEqual(len(sample.packs), MGR.PACK_COUNT)
        for idx, pack_sample in enumerate(sample.packs):
            pack = idx + 1
            self.assertAlmostEqual(pack_sample.current_a, -22.5 - pack, places=3)
            self.assertEqual(pack_sample.serial_number, f"SN-PACK-{pack}")

    def test_missing_current_and_serial_degrade_to_none_not_a_crash(self):
        """Negative case: an offline/unreadable pack (no current/serial
        registers present this tick) must not raise -- same tolerance
        every other per-pack field already has."""
        mgr = object.__new__(MGR.BatteryHealthManager)
        mgr.coordinator = type("FakeCoord", (), {})()
        mgr._pack_slots = MGR.pack_slots_for_units([1])
        mgr._ambient_entity = None
        mgr._ambient_warned = False

        values = {}
        for pack in range(1, MGR.PACK_COUNT + 1):
            values[MGR._pack_register_name(1, pack, "working_status")] = 0  # offline
        cache = self._cache_with_all(values)
        data = {name: cache.get(name) for name in values}
        mgr.coordinator.cache = cache

        sample = mgr._build_sample(data)  # must not raise
        for pack_sample in sample.packs:
            self.assertIsNone(pack_sample.current_a)
            self.assertIsNone(pack_sample.serial_number)


class TestICS06RestoreErrorBoundary(unittest.IsolatedAsyncioTestCase):
    """ICS-06, external ICS audit -- confirmed: restore() sat OUTSIDE
    the load-failure try/except -- a store that loaded successfully
    (syntactically valid) but was structurally corrupt could make
    restore() raise, bypassing the "corrupt store must not block
    setup" guarantee the load-failure branch already provides, and
    aborting initialization entirely (no listener ever gets
    subscribed -- battery-health tracking silently stops working for
    that device) rather than gracefully starting fresh."""

    def _make_manager(self, load_return):
        mgr = object.__new__(MGR.BatteryHealthManager)
        mgr.serial_number = "SNTEST"
        mgr.hass = MagicMock()
        mgr.coordinator = MagicMock()
        mgr.coordinator.async_add_listener = MagicMock(return_value=lambda: None)
        mgr.engine = BH.BatteryHealthEngine()
        mgr._store = MagicMock()
        mgr._store.async_load = AsyncMock(return_value=load_return)
        # v2.0.7 (TOPO-01 done properly, this release): async_initialize()
        # now reads self._register_names (computed once at real __init__
        # time from discovered topology) -- this bypasses __init__ via
        # object.__new__, so it must be set explicitly here, same as
        # every other attribute this helper already fakes.
        mgr._register_names = MGR.required_register_names([1])
        return mgr

    async def test_structurally_corrupt_but_loadable_store_does_not_abort_init(self):
        """The core ICS-06 scenario: async_load() succeeds (no
        exception), but the loaded data is structurally wrong in a way
        that makes restore() itself raise -- the "segments" field is a
        string instead of the dict restore() expects. Must not abort
        initialization -- the listener must still get subscribed,
        matching the same "corrupt store must not block setup"
        guarantee the load-failure branch already provides."""
        corrupt_data = {
            "schema_version": BH.SCHEMA_VERSION,  # must match, or restore()
                                                    # early-returns before
                                                    # ever reaching the
                                                    # vulnerable code path
            "segments": "not_a_dict_at_all",
        }
        mgr = self._make_manager(corrupt_data)
        await mgr.async_initialize()  # must not raise
        mgr.coordinator.async_add_listener.assert_called_once()
        self.assertIsNotNone(
            mgr._unsub,
            "the coordinator listener must still be subscribed after a "
            "restore() failure -- otherwise battery-health tracking is "
            "silently disabled for this device with no listener ever set",
        )

    async def test_engine_is_replaced_not_partially_mutated(self):
        """A partial restore (some fields successfully overwritten
        before the exception, others not reached) must not leave the
        engine in an inconsistent mix -- the whole engine is discarded
        and replaced with a genuinely fresh one, reusing its own
        already-resolved .cfg."""
        corrupt_data = {
            "schema_version": BH.SCHEMA_VERSION,
            "first_seen_ts": 123456.0,  # a field restore() sets successfully...
            "segments": "not_a_dict_at_all",  # ...before this one raises
        }
        mgr = self._make_manager(corrupt_data)
        original_engine = mgr.engine
        await mgr.async_initialize()
        self.assertIsNot(
            mgr.engine, original_engine,
            "the engine must be replaced entirely after a restore() "
            "failure, not reused with only some of its fields "
            "successfully overwritten by the corrupt data",
        )
        self.assertIsNone(
            mgr.engine.first_seen_ts,
            "the replacement engine must be genuinely fresh -- not "
            "carrying over the one field the corrupt data DID manage "
            "to set before restore() raised on a later one",
        )

    async def test_normal_valid_restore_still_works(self):
        """Negative case: a genuinely valid, loadable store must still
        restore correctly -- confirms the fix didn't make every restore
        path start fresh regardless of validity."""
        valid_data = {
            "schema_version": BH.SCHEMA_VERSION,
            "first_seen_ts": 999.0,
            "held_subscores": {},
            "learning_enabled": True,
            "settling_events": 0,
            "ceiling": {},
            "segments": {},
            "efficiency": {},
            "balance": {},
            "stress": {},
            "charge_counter": {},
            "discharge_counter": {},
        }
        mgr = self._make_manager(valid_data)
        await mgr.async_initialize()
        self.assertEqual(mgr.engine.first_seen_ts, 999.0)


if __name__ == "__main__":
    unittest.main()
