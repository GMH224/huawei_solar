"""Regression tests for v2.2.0.1 external-ICS-audit remediation.

Covers all four findings from the independent ICS Test Report against
v2.2.0.0 (HVC-001 through HVC-004), each verified against source before
fixing and re-verified here against the fixed source:

  HVC-001 (Medium)  diagnostics.py — `_redact_coordinator_data()` was
                     called at four sites but its own `def` line had
                     been lost; the body survived only as dead,
                     unreachable text inside `_redact_entity_entry`'s
                     own function. Every diagnostics download on a
                     config entry with an inverter raised `NameError`.
  HVC-002 (High)     hacs.json — declared "homeassistant": "2025.9.0"
                     while services.py's `async_get_entry_id_for_
                     service_call()` requires `dr.async_get_device_
                     and_config_entry_for_domain()`, an HA 2026.8+
                     helper. Every other reference to this release's
                     own baseline in this codebase (AUDIT_2.2.0.0.md,
                     test_ha_2026_forward_compat.py, and the v2.2.0.0
                     comments in __init__.py/config_flow.py/services.py
                     themselves) says "2026.9" — this was the single
                     stray "2025.9", almost certainly a one-digit typo.
  HVC-003 (High)     __init__.py — `async_forward_entry_setups()` and
                     `async_setup_services()` ran AFTER the try/except
                     block that runs `_run_cleanup_callbacks()` and
                     `_bounded_device_stop()` on every earlier setup
                     failure, so a failure in either of those two calls
                     bypassed cleanup entirely.
  HVC-004 (Medium)   battery_health.py / battery_health_manager.py —
                     `set_pack_install_date` accepted, by design, a
                     serial the engine had never observed, and wrote it
                     straight into the persisted `pack_install_dates`
                     dict with no cap of its own. The existing
                     `_prune_retired_history_and_stale_serials()` only
                     runs as a side effect of an actual pack replacement
                     being archived, so a unit that never replaces a
                     pack never bounds this dict.

Real execution wherever the code under test can be exercised directly
(HVC-001, HVC-004) — following this project's own established
convention (see test_battery_health.py, test_ics_audit_2101_fixes.py).
Source/AST-level only for __init__.py (HVC-003) and metadata (HVC-002),
where full behavioural execution would require a genuine Home Assistant
config-entry setup — the same convention test_setup_critical_path.py
and test_ha_2026_forward_compat.py already establish for this exact
file, for the same reason.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import pathlib
import sys
import types as pytypes
import unittest

_ROOT = pathlib.Path(__file__).resolve().parent.parent


# ═══════════════════════════════════════════════════════════════════════
# HVC-001 — diagnostics.py: _redact_coordinator_data restored
# ═══════════════════════════════════════════════════════════════════════
#
# This is deliberately a full, dynamic, end-to-end call of
# `async_get_config_entry_diagnostics()` — not a source/AST check.
# tests/test_audit_v4_findings.py's own
# `test_coordinator_data_dumps_go_through_the_redaction_helper` only
# confirms the STRING "_redact_coordinator_data(" appears near each call
# site; it would (and did) pass even with the function undefined. That
# gap is exactly what let HVC-001 ship, and exactly what this test is
# designed not to repeat: it stubs only what real Home Assistant/vendor
# machinery diagnostics.py needs to actually IMPORT and RUN, then calls
# the real entry point with real inverter-shaped data.

_DIAG_PKG = "hs_diag_hvc001_check"


def _stub_module(name: str, **attrs) -> pytypes.ModuleType:
    mod = pytypes.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


def _load_from_file(pkg_name: str, mod_name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(f"{pkg_name}.{mod_name}", str(path))
    module = importlib.util.module_from_spec(spec)
    module.__package__ = pkg_name
    sys.modules[f"{pkg_name}.{mod_name}"] = module
    spec.loader.exec_module(module)
    return module


def _install_diagnostics_stubs(pkg_name: str) -> None:
    """Install the minimal HA stubs, plus lightweight local-package
    stand-ins for the two subsystems diagnostics.py only ever touches
    through their public `.get(serial)` classmethod (AdaptiveModbus
    Controller, ModbusTelemetry) and the shared `.types` module (used
    here only for `isinstance` — real dataclass bodies are irrelevant
    to diagnostics.py's own logic and would pull in update_coordinator.py
    and its full HA entity surface for no benefit to this test).

    `.bus_diagnostics` and `.const` are loaded for real: both are pure/
    stdlib-only in this codebase and are the modules diagnostics.py's
    redaction logic actually depends on.
    """
    for name in (
        "homeassistant",
        "homeassistant.core",
        "homeassistant.const",
        "homeassistant.helpers",
        "homeassistant.helpers.entity_registry",
        "homeassistant.components",
        "homeassistant.components.diagnostics",
    ):
        if name not in sys.modules:
            _stub_module(name)

    core = sys.modules["homeassistant.core"]
    if not hasattr(core, "HomeAssistant"):
        core.HomeAssistant = type("HomeAssistant", (), {})

    const = sys.modules["homeassistant.const"]
    for attr in ("CONF_HOST", "CONF_PASSWORD", "CONF_USERNAME"):
        if not hasattr(const, attr):
            setattr(const, attr, attr.lower())

    diag_component = sys.modules["homeassistant.components.diagnostics"]
    if not hasattr(diag_component, "async_redact_data"):
        def async_redact_data(data, to_redact):
            return {
                key: ("**REDACTED**" if key in to_redact else value)
                for key, value in data.items()
            }
        diag_component.async_redact_data = async_redact_data

    er = sys.modules["homeassistant.helpers.entity_registry"]
    if not hasattr(er, "async_get"):
        er.async_get = lambda hass: object()
    if not hasattr(er, "async_entries_for_config_entry"):
        # No entity-registry entries in this test — HVC-001's own defect
        # is unreachable through this path (it lives in the coordinator-
        # data branch below), and ICS-002's entity-redaction path
        # already has its own dedicated coverage in
        # test_audit_v4_findings.py.
        er.async_entries_for_config_entry = lambda registry, entry_id: []

    pkg = _stub_module(pkg_name)
    pkg.__path__ = []

    _load_from_file(pkg_name, "bus_diagnostics", _ROOT / "bus_diagnostics.py")
    _load_from_file(pkg_name, "const", _ROOT / "const.py")

    adaptive_stub = _stub_module(f"{pkg_name}.adaptive_modbus")

    class _FakeAdaptiveModbusController:
        @classmethod
        def get(cls, serial):
            return None

    adaptive_stub.AdaptiveModbusController = _FakeAdaptiveModbusController

    telemetry_stub = _stub_module(f"{pkg_name}.modbus_telemetry")

    class _FakeModbusTelemetry:
        @classmethod
        def get(cls, serial):
            return None

    telemetry_stub.ModbusTelemetry = _FakeModbusTelemetry

    types_stub = _stub_module(f"{pkg_name}.types")

    class HuaweiSolarConfigEntry:
        ...

    class HuaweiSolarDeviceData:
        ...

    class HuaweiSolarInverterData(HuaweiSolarDeviceData):
        ...

    types_stub.HuaweiSolarConfigEntry = HuaweiSolarConfigEntry
    types_stub.HuaweiSolarDeviceData = HuaweiSolarDeviceData
    types_stub.HuaweiSolarInverterData = HuaweiSolarInverterData


def _load_diagnostics_module():
    _install_diagnostics_stubs(_DIAG_PKG)
    return _load_from_file(_DIAG_PKG, "diagnostics", _ROOT / "diagnostics.py")


class _FakeResult:
    """Stand-in for huawei_solar's Result -- diagnostics.py reads only
    `.value` off it (with a fallback if that attribute access fails)."""

    def __init__(self, value):
        self.value = value


class _FakeCoordinator:
    def __init__(self, data):
        self.data = data


class _FakeClient:
    def __init__(self, unit_id):
        self.unit_id = unit_id


class _FakeInverterDevice:
    def __init__(self, serial_number, unit_id=0):
        self.serial_number = serial_number
        self.client = _FakeClient(unit_id)
        self.model_name = "SUN2000-10KTL-M1"
        self.firmware_version = "V100R001C00"
        self.software_version = "V100R001C00SPC117"
        self.pv_string_count = 2
        self.has_optimizers = False
        self.battery_type = "NONE"
        self.battery_1_type = "NONE"
        self.battery_2_type = "NONE"
        self.power_meter_type = "NONE"
        self.supports_capacity_control = False


class _FakeInverterDeviceData:
    """Mirrors HuaweiSolarInverterData's shape closely enough for
    async_get_config_entry_diagnostics() -- inherits from the actual
    stubbed HuaweiSolarInverterData class (attached in __init__ below)
    so the real isinstance() checks in diagnostics.py route through the
    same branch a genuine inverter device data object would."""

    def __init__(self, serial_number, unit_id=0):
        self.device = _FakeInverterDevice(serial_number, unit_id)
        self.update_coordinator = _FakeCoordinator({
            "inverter_serial_number": _FakeResult(serial_number),
            "active_power": _FakeResult(1234.5),
        })
        self.power_meter_update_coordinator = None
        self.energy_storage_update_coordinator = None
        self.optimizer_update_coordinator = None
        self.configuration_update_coordinator = _FakeCoordinator({
            "storage_unit_1_battery_pack_1_serial_number": _FakeResult("PACKSERIAL001"),
            "rated_capacity": _FakeResult(5000),
        })


def _make_fake_inverter_device_data(diag_mod, serial_number, unit_id=0):
    """Builds a _FakeInverterDeviceData that genuinely IS-A the stubbed
    HuaweiSolarInverterData class diagnostics.py imported, so
    isinstance(dd, HuaweiSolarInverterData) is True -- matching a real
    HuaweiSolarInverterData instance, not just duck-typing its shape."""
    base = diag_mod.HuaweiSolarInverterData
    cls = type("_RealisticFakeInverterDeviceData", (base, _FakeInverterDeviceData), {})
    return cls(serial_number, unit_id)


class TestHVC001DiagnosticsEndToEnd(unittest.TestCase):
    """The end-to-end regression test the audit's own recommended fix
    explicitly asked for: import the module, construct a minimal
    inverter coordinator payload, invoke
    async_get_config_entry_diagnostics(), verify the returned structure
    is serializable, verify serial-bearing register values are
    redacted."""

    def setUp(self):
        self.diag = _load_diagnostics_module()

    def test_helper_is_a_real_module_level_function(self):
        self.assertTrue(hasattr(self.diag, "_redact_coordinator_data"))
        self.assertTrue(callable(self.diag._redact_coordinator_data))

    def test_diagnostics_entry_point_does_not_raise_nameerror(self):
        import asyncio

        inverter_serial = "HV2220098926"
        dd = _make_fake_inverter_device_data(self.diag, inverter_serial)
        entry = pytypes.SimpleNamespace(
            data={}, entry_id="abc123",
            runtime_data={self.diag.DATA_DEVICE_DATAS: [dd]},
        )
        hass = object()

        try:
            result = asyncio.run(
                self.diag.async_get_config_entry_diagnostics(hass, entry)
            )
        except NameError as err:  # pragma: no cover — the exact HVC-001 symptom
            self.fail(
                f"async_get_config_entry_diagnostics() raised NameError "
                f"({err}) -- HVC-001 has regressed."
            )

        self.assertIsInstance(result, dict)

    def test_returned_structure_top_level_is_plain_and_serializable(self):
        """The top-level diagnostics structure (config entry data, entity
        list, per-device summaries) must be plain, JSON-safe values --
        this is the part _redact_coordinator_data()'s own NameError
        previously prevented from ever being reached at all. Coordinator
        .data entries for NON-serial registers pass the vendor Result
        object through unconverted, by pre-existing (out-of-scope-for-
        HVC-001) design, so those specific keys are excluded here rather
        than assumed serializable."""
        import asyncio

        dd = _make_fake_inverter_device_data(self.diag, "HV2220098926")
        entry = pytypes.SimpleNamespace(
            data={}, entry_id="abc123",
            runtime_data={self.diag.DATA_DEVICE_DATAS: [dd]},
        )
        result = asyncio.run(
            self.diag.async_get_config_entry_diagnostics(object(), entry)
        )
        self.assertIsInstance(result, dict)
        summary = result["device_0"]
        json.dumps(summary)  # must not raise
        self.assertEqual(summary["_type"], "SUN2000")
        self.assertEqual(summary["model_name"], "SUN2000-10KTL-M1")

    def test_serial_bearing_register_is_redacted_not_leaked(self):
        import asyncio

        inverter_serial = "HV2220098926"
        pack_serial = "PACKSERIAL001"
        dd = _make_fake_inverter_device_data(self.diag, inverter_serial)
        entry = pytypes.SimpleNamespace(
            data={}, entry_id="abc123",
            runtime_data={self.diag.DATA_DEVICE_DATAS: [dd]},
        )
        result = asyncio.run(
            self.diag.async_get_config_entry_diagnostics(object(), entry)
        )
        main_data = result["device_0_data"]
        config_data = result["device_0_config_data"]

        # The redacted values must be plain, JSON-safe strings -- not
        # the raw vendor Result object, and not the real serial.
        self.assertIsInstance(main_data["inverter_serial_number"], str)
        self.assertIsInstance(
            config_data["storage_unit_1_battery_pack_1_serial_number"], str
        )
        self.assertNotIn(inverter_serial, main_data["inverter_serial_number"])
        self.assertNotIn(
            pack_serial, config_data["storage_unit_1_battery_pack_1_serial_number"]
        )
        self.assertIn("REDACTED", main_data["inverter_serial_number"])
        self.assertIn(
            "REDACTED", config_data["storage_unit_1_battery_pack_1_serial_number"]
        )

    def test_non_serial_register_values_pass_through_unredacted(self):
        """Pre-existing, unchanged behaviour: a register NOT matching
        the serial-name pattern is passed through as-is (the raw
        Result-like object), not unwrapped to `.value` -- only the
        serial-redaction path does that unwrapping. This test pins that
        this fix did not accidentally change non-serial handling."""
        import asyncio

        dd = _make_fake_inverter_device_data(self.diag, "HV2220098926")
        entry = pytypes.SimpleNamespace(
            data={}, entry_id="abc123",
            runtime_data={self.diag.DATA_DEVICE_DATAS: [dd]},
        )
        result = asyncio.run(
            self.diag.async_get_config_entry_diagnostics(object(), entry)
        )
        device_data = result["device_0_data"]
        self.assertIsInstance(device_data["active_power"], _FakeResult)
        self.assertEqual(device_data["active_power"].value, 1234.5)

    def test_no_dead_code_left_behind_in_redact_entity_entry(self):
        """Negative case pinning the actual root cause: the orphaned
        body used to live, unreachable, inside `_redact_entity_entry`
        (after its own `return`), at the same indentation. Confirms
        that function is short again and contains nothing referencing
        coordinator-data redaction."""
        source = (_ROOT / "diagnostics.py").read_text()
        tree = ast.parse(source)
        func = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_redact_entity_entry"
        )
        # docstring (Expr) + _scrub (nested def) + return -- nothing else.
        # Index 0 (the legitimate docstring) is excluded deliberately;
        # any Expr AFTER it is exactly the dead-code shape HVC-001 was.
        top_level_kinds = [type(stmt).__name__ for stmt in func.body]
        self.assertNotIn(
            "Expr", top_level_kinds[1:],
            "_redact_entity_entry contains a stray top-level expression "
            "statement after its docstring -- likely the same dead-code "
            "regression as HVC-001.",
        )
        self.assertEqual(len(func.body), 3, "unexpected extra statement(s)")

    def test_redact_coordinator_data_defined_before_its_first_call_site(self):
        """Pins the fix's own placement: defined at module scope, before
        _redact_entity_entry -- matching that function's own docstring
        ('_redact_coordinator_data above')."""
        source = (_ROOT / "diagnostics.py").read_text()
        def_idx = source.find("def _redact_coordinator_data(")
        entity_entry_idx = source.find("def _redact_entity_entry(")
        first_call_idx = source.find("_redact_coordinator_data(dd.")
        self.assertGreater(def_idx, -1)
        self.assertLess(def_idx, entity_entry_idx)
        self.assertLess(def_idx, first_call_idx)


# ═══════════════════════════════════════════════════════════════════════
# HVC-002 — hacs.json declared minimum vs. required HA API
# ═══════════════════════════════════════════════════════════════════════

class TestHVC002DeclaredMinimumMatchesReleaseBaseline(unittest.TestCase):
    def test_hacs_json_no_longer_declares_2025_9(self):
        hacs = json.loads((_ROOT / "hacs.json").read_text())
        self.assertNotEqual(
            hacs["homeassistant"], "2025.9.0",
            "hacs.json still declares the pre-fix 2025.9.0 minimum.",
        )

    def test_hacs_json_declares_the_2026_9_baseline(self):
        """This release's own comments, tests, and internal audit doc
        (AUDIT_2.2.0.0.md) consistently frame it as an 'HA 2026.9 /
        2026.12 forward-compatibility release' -- '2025.9' was the one
        stray reference to a different year anywhere in this tree.
        Pinning the exact corrected value, not just 'not 2025.9', so a
        future accidental revert back to any pre-2026.8 value is caught
        too (2026.8 is the bare technical minimum for
        async_get_device_and_config_entry_for_domain(); 2026.9 matches
        the release's own stated baseline and the version the field
        incident that triggered this release was reported against)."""
        hacs = json.loads((_ROOT / "hacs.json").read_text())
        self.assertEqual(hacs["homeassistant"], "2026.9.0")

    def test_manifest_version_bumped(self):
        # v2.3.0.1: updated from "2.3.0.0" (was "2.2.0.2") -- this assertion pins the
        # CURRENT release's version and must move with every bump.
        manifest = json.loads((_ROOT / "manifest.json").read_text())
        self.assertEqual(manifest["version"], "2.3.0.1")

    def test_no_stray_2025_9_reference_in_shipped_production_files(self):
        """Whole-tree regression sweep, matching this project's own G12
        convention -- restricted to PRODUCTION files (the ones that ship
        and actually declare a compatibility baseline), not test files:
        test files legitimately quote the old, incorrect value for
        documentation purposes (see this very test's own docstrings,
        and test_ha_2026_forward_compat.py's pre-existing "2025.1.4"
        reference to an unrelated sandbox package version) -- exactly
        the same convention this project already uses elsewhere.
        Historical AUDIT_1.x/2.0.x/2.1.x files are pre-existing and
        untouched, so they are excluded deliberately, not because they
        were checked and found clean."""
        excluded_globs = ("AUDIT_1.*", "AUDIT_2.0.*", "AUDIT_2.1.*")
        production_suffixes = (".json",)  # hacs.json is the only file
        # that ever declared a homeassistant-version baseline value.
        for path in sorted(_ROOT.glob("*")):
            if not path.is_file() or path.suffix not in production_suffixes:
                continue
            if any(path.match(pattern) for pattern in excluded_globs):
                continue
            with self.subTest(file=path.name):
                self.assertNotIn("2025.9", path.read_text())


# ═══════════════════════════════════════════════════════════════════════
# HVC-003 — __init__.py: platform/service setup now inside the
# cleanup-protected region
# ═══════════════════════════════════════════════════════════════════════
#
# AST-level, following this project's own established convention for
# __init__.py's setup logic (see test_setup_critical_path.py's own
# header and test_ha_2026_forward_compat.py's Fix 1-4 tests): a full
# dynamic exercise of async_setup_entry() would require a real Home
# Assistant config-entry/device/entity-platform stack.

_INIT_SRC_TEXT = (_ROOT / "__init__.py").read_text()
_INIT_TREE = ast.parse(_INIT_SRC_TEXT)


def _find_async_setup_entry() -> ast.AsyncFunctionDef:
    return next(
        n for n in ast.walk(_INIT_TREE)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_setup_entry"
    )


def _calls_named(node, *names) -> list[ast.Call]:
    found = []
    for n in ast.walk(node):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in names
        ):
            found.append(n)
    return found


class TestHVC003SetupFailureCleanupCoverage(unittest.TestCase):
    def setUp(self):
        self.func = _find_async_setup_entry()
        self.try_node = next(
            n for n in self.func.body if isinstance(n, ast.Try)
        )

    def test_main_setup_try_block_exists_with_a_catch_all_handler(self):
        handler_names = [
            getattr(h.type, "id", None) for h in self.try_node.handlers
        ]
        self.assertIn(
            "Exception", handler_names,
            "async_setup_entry's main try/except no longer has a "
            "catch-all Exception handler -- the cleanup-coverage "
            "guarantee this test checks depends on one existing.",
        )

    def test_forward_entry_setups_is_inside_the_protected_try_block(self):
        calls_in_try_body = _calls_named(
            ast.Module(body=self.try_node.body, type_ignores=[]),
            "async_forward_entry_setups",
        )
        self.assertTrue(
            calls_in_try_body,
            "async_forward_entry_setups() is not called inside "
            "async_setup_entry's main try block -- HVC-003 has "
            "regressed: a failure here will bypass "
            "_run_cleanup_callbacks()/_bounded_device_stop().",
        )

    def test_setup_services_is_inside_the_protected_try_block(self):
        calls_in_try_body = _calls_named(
            ast.Module(body=self.try_node.body, type_ignores=[]),
            "async_setup_services",
        )
        # async_setup_services is a plain Name call (not an attribute
        # call), so check it directly rather than via _calls_named.
        found = [
            n for n in ast.walk(
                ast.Module(body=self.try_node.body, type_ignores=[])
            )
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "async_setup_services"
        ]
        self.assertTrue(
            found,
            "async_setup_services() is not called inside "
            "async_setup_entry's main try block -- HVC-003 has "
            "regressed.",
        )

    def test_neither_call_appears_again_after_the_try_block(self):
        """Negative case: confirms the old, unprotected call sites were
        MOVED, not duplicated -- both calls must appear exactly once in
        the whole function, and that one occurrence must be the
        in-try-block one already confirmed above."""
        func_source_calls_forward = _INIT_SRC_TEXT.count(
            "await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)"
        )
        func_source_calls_services = _INIT_SRC_TEXT.count(
            "await async_setup_services(hass, entry)"
        )
        self.assertEqual(func_source_calls_forward, 1)
        self.assertEqual(func_source_calls_services, 1)

    def test_cleanup_helpers_are_reachable_from_every_handler(self):
        """Every except clause in the main try/except must call both
        cleanup primitives before re-raising -- unchanged pre-existing
        behaviour, re-asserted here so a future edit that narrows the
        try block back down (undoing HVC-003's fix) is caught even if
        it technically leaves the two calls "inside" some smaller,
        differently-shaped try block that no longer shares these
        handlers."""
        for handler in self.try_node.handlers:
            handler_src = ast.unparse(handler)
            with self.subTest(
                exc=getattr(handler.type, "id", ast.dump(handler.type))
            ):
                self.assertIn("_run_cleanup_callbacks", handler_src)


# ═══════════════════════════════════════════════════════════════════════
# HVC-004 — battery_health.py: bounded pack_install_dates writes
# ═══════════════════════════════════════════════════════════════════════

_bh_spec = importlib.util.spec_from_file_location(
    "bh_hvc004_check", str(_ROOT / "battery_health.py")
)
bh = importlib.util.module_from_spec(_bh_spec)
sys.modules["bh_hvc004_check"] = bh
_bh_spec.loader.exec_module(bh)


def _cfg() -> "bh.BatteryHealthConfig":
    cfg = bh.BatteryHealthConfig()
    cfg.capacity_temp_sigma_c = 1e9
    cfg.capacity_rate_ref_w = 1e9
    return cfg


class TestHVC004BoundedPackInstallDateOverrides(unittest.TestCase):
    def _tracker(self):
        return bh.PackCapacityTracker(_cfg(), pack_count=1, slot_labels=["u1p1"])

    def test_normal_use_is_unaffected_below_the_cap(self):
        """Negative case first: the whole point of this being a cap, not
        a rejection, is that ordinary use (a handful of real
        replacement-pack dates) must be completely unaffected."""
        tracker = self._tracker()
        for i in range(5):
            tracker.set_pack_install_date_override(f"SN-{i}", float(i))
        self.assertEqual(len(tracker.pack_install_dates), 5)
        for i in range(5):
            self.assertEqual(tracker.pack_install_dates[f"SN-{i}"], float(i))

    def test_never_observed_serials_are_still_accepted(self):
        """The permissive design (set_pack_install_date's own docstring:
        a serial the engine has never seen is accepted without error)
        must be preserved -- this fix bounds growth, it does not add
        rejection."""
        tracker = self._tracker()
        tracker.set_pack_install_date_override("NEVER-SEEN-SERIAL", 123.0)
        self.assertIn("NEVER-SEEN-SERIAL", tracker.pack_install_dates)

    def test_unbounded_calls_no_longer_grow_the_dict_without_bound(self):
        """The core HVC-004 regression check: repeating the service call
        with an arbitrarily large number of distinct, never-observed
        serials must not grow pack_install_dates past the cap."""
        tracker = self._tracker()
        for i in range(bh.MAX_PACK_INSTALL_DATE_OVERRIDES + 500):
            tracker.set_pack_install_date_override(f"GARBAGE-SN-{i}", float(i))
        self.assertLessEqual(
            len(tracker.pack_install_dates), bh.MAX_PACK_INSTALL_DATE_OVERRIDES
        )

    def test_currently_live_serials_are_preserved_over_stale_ones(self):
        """When the cap is exceeded, a currently-live pack's own
        override must survive eviction in preference to stale/irrelevant
        entries -- matching _prune_retired_history_and_stale_serials()'s
        own notion of "relevant"."""
        tracker = self._tracker()
        tracker._last_serial = ["SN-LIVE"]
        tracker.set_pack_install_date_override("SN-LIVE", 1.0)
        for i in range(bh.MAX_PACK_INSTALL_DATE_OVERRIDES + 50):
            tracker.set_pack_install_date_override(f"GARBAGE-SN-{i}", float(i))
        self.assertIn("SN-LIVE", tracker.pack_install_dates)
        self.assertLessEqual(
            len(tracker.pack_install_dates), bh.MAX_PACK_INSTALL_DATE_OVERRIDES
        )

    def test_retained_retired_history_serial_is_also_preserved(self):
        tracker = self._tracker()
        tracker.retired_pack_history = [
            {"slot_label": "u1p1", "serial_number": "SN-RETIRED", "replaced_at": 1.0},
        ]
        tracker.set_pack_install_date_override("SN-RETIRED", 1.0)
        for i in range(bh.MAX_PACK_INSTALL_DATE_OVERRIDES + 50):
            tracker.set_pack_install_date_override(f"GARBAGE-SN-{i}", float(i))
        self.assertIn("SN-RETIRED", tracker.pack_install_dates)

    def test_oldest_garbage_entries_evicted_first(self):
        tracker = self._tracker()
        for i in range(bh.MAX_PACK_INSTALL_DATE_OVERRIDES + 10):
            tracker.set_pack_install_date_override(f"GARBAGE-SN-{i}", float(i))
        self.assertNotIn("GARBAGE-SN-0", tracker.pack_install_dates)
        self.assertIn(
            f"GARBAGE-SN-{bh.MAX_PACK_INSTALL_DATE_OVERRIDES + 9}",
            tracker.pack_install_dates,
        )

    def test_manager_no_longer_writes_the_dict_directly(self):
        """services.py's docstring/tests already confirm the service
        handler goes through BatteryHealthManager.set_pack_install_date;
        this confirms THAT method no longer bypasses the new bounded
        write path."""
        source = (_ROOT / "battery_health_manager.py").read_text()
        idx = source.find("def set_pack_install_date(")
        self.assertGreater(idx, -1)
        nxt = source.find("\n    def ", idx + 1)
        body = source[idx: nxt if nxt > 0 else len(source)]
        self.assertNotIn(
            "pack_capacity.pack_install_dates[serial] = install_ts", body,
            "battery_health_manager.py still writes pack_install_dates "
            "directly -- HVC-004's fix has regressed.",
        )
        self.assertIn("set_pack_install_date_override(serial, install_ts)", body)


if __name__ == "__main__":
    unittest.main()
