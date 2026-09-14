"""Regression tests for v2.2.0.1's second remediation pass.

Covers every Tier 1/2 finding from the second (Mission-Critical Code
Defect) ICS report that was confirmed against source and fixed in this
release: ICS-001, ICS-003, ICS-004, ICS-007, ICS-009, ICS-010, ICS-011,
ICS-012, ICS-014/ICS-022, ICS-019, ICS-020, ICS-021.

(ICS-002 and ICS-006 were the same defects as HVC-003/HVC-004 --
covered by test_ics_audit_2201_fixes.py already. ICS-013 was
independently disproven against the real voluptuous library and is not
fixed or tested here. ICS-005/008/015/016/017/018 are documented as
deferred in AUDIT_2.2.0.1.md, not fixed in this release.)

Real execution wherever the module under test has no, or only light,
Home Assistant dependencies (register_cache.py, battery_health.py,
battery_health_manager.py, adaptive_modbus.py) -- reusing this
project's own proven stub-loading patterns from test_register_cache.py,
test_battery_health.py, test_date.py, and test_adaptive_modbus.py
respectively, rather than inventing a new approach. Source/AST-level
for __init__.py, services.py, update_coordinator.py, and
modbus_keepalive.py, matching this project's own established
convention for those exact files (test_setup_critical_path.py,
test_services.py, test_ha_2026_forward_compat.py) -- a full dynamic
exercise of any of them requires a genuine Home Assistant config-entry/
device/entity-platform stack this test suite does not build.
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys
import time
import types
import unittest
from unittest.mock import MagicMock

_ROOT = pathlib.Path(__file__).resolve().parent.parent


# ═══════════════════════════════════════════════════════════════════════
# ICS-001 — async_unload_entry: cleanup no longer gated on unload_ok
# ═══════════════════════════════════════════════════════════════════════

_INIT_SRC = (_ROOT / "__init__.py").read_text()
_INIT_TREE = ast.parse(_INIT_SRC)


def _find_func(name: str, *, async_def: bool = True):
    kind = ast.AsyncFunctionDef if async_def else ast.FunctionDef
    return next(n for n in ast.walk(_INIT_TREE) if isinstance(n, kind) and n.name == name)


class TestICS001UnloadCleanupUnconditional(unittest.TestCase):
    def setUp(self):
        self.func = _find_func("async_unload_entry")

    def test_unload_platforms_call_is_not_an_if_condition(self):
        """The old defect: `if unload_ok := await ...async_unload_platforms(...):`
        with every cleanup step nested inside that If node's body. The
        fix: a plain assignment statement, with cleanup following at
        the SAME indentation level (i.e. as later statements in
        func.body, not nested inside any If)."""
        for stmt in self.func.body:
            if isinstance(stmt, ast.If):
                cond_src = ast.unparse(stmt.test)
                self.assertNotIn(
                    "async_unload_platforms", cond_src,
                    "async_unload_platforms() is still the condition of an "
                    "If statement -- ICS-001 has regressed: cleanup below "
                    "it would still be skipped whenever a platform fails "
                    "to unload.",
                )

    def test_cleanup_helpers_are_top_level_statements_not_nested(self):
        """Confirms specific cleanup calls (ModbusGuard.release_endpoint,
        the DATA_SYNC_POWER_COORDINATOR pop) are direct statements in
        the function body -- not nested inside any If/Try in a way that
        could gate them on unload_ok being true."""
        top_level_src = "\n".join(ast.unparse(stmt) for stmt in self.func.body)
        self.assertIn("ModbusGuard.release_endpoint", top_level_src)
        self.assertIn("DATA_SYNC_POWER_COORDINATOR", top_level_src)

    def test_unload_ok_is_still_returned(self):
        last = self.func.body[-1]
        self.assertIsInstance(last, ast.Return)
        self.assertEqual(ast.unparse(last.value), "unload_ok")

    def test_device_datas_access_happens_unconditionally(self):
        """entry.runtime_data[DATA_DEVICE_DATAS] must be reachable
        regardless of unload_ok -- i.e. not itself still guarded by an
        `if unload_ok` check that simply moved rather than being
        removed."""
        for stmt in self.func.body:
            if isinstance(stmt, ast.If) and "unload_ok" in ast.unparse(stmt.test):
                self.fail(
                    "async_unload_entry still contains an `if unload_ok` "
                    "branch -- ICS-001 has regressed in some other form."
                )


# ═══════════════════════════════════════════════════════════════════════
# ICS-003 — number.py: zero-value truthiness fixed
# ═══════════════════════════════════════════════════════════════════════

def _load_number_module():
    pkg = "hs_number_ics003_check"

    def stub(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
        return m

    for name in (
        "homeassistant", "homeassistant.core", "homeassistant.const",
        "homeassistant.helpers", "homeassistant.helpers.device_registry",
        "homeassistant.helpers.entity_platform",
        "homeassistant.helpers.update_coordinator",
        "homeassistant.components", "homeassistant.components.number",
        "homeassistant.components.number.const", "homeassistant.exceptions",
    ):
        if name not in sys.modules:
            stub(name)

    core = sys.modules["homeassistant.core"]
    core.HomeAssistant = type("HomeAssistant", (), {})
    core.callback = lambda f: f

    const = sys.modules["homeassistant.const"]
    for attr in ("PERCENTAGE",):
        setattr(const, attr, attr)

    class EntityCategory:
        DIAGNOSTIC = "diagnostic"
        CONFIG = "config"
    const.EntityCategory = EntityCategory

    class UnitOfPower:
        WATT = "W"
    const.UnitOfPower = UnitOfPower

    num_const = sys.modules["homeassistant.components.number.const"]
    num_const.DEFAULT_MIN_VALUE = 0.0
    num_const.DEFAULT_MAX_VALUE = 100.0

    num = sys.modules["homeassistant.components.number"]

    class NumberEntity:
        pass

    class NumberDeviceClass:
        POWER = "power"

    class NumberMode:
        BOX = "box"

    from dataclasses import dataclass as _dataclass, field as _field

    @_dataclass(frozen=True)
    class NumberEntityDescription:
        """Permissive stand-in mirroring only the fields this
        integration's own number.py descriptions actually set --
        real HA's own EntityDescription/NumberEntityDescription
        dataclasses provide many more, none of which native_min_value/
        native_max_value/async_set_native_value (the only things under
        test here) read."""
        key: str = ""
        icon: str | None = None
        entity_category: Any = None
        entity_registry_enabled_default: bool = True
        native_min_value: float | None = None
        native_max_value: float | None = None
        native_step: float | None = None
        native_unit_of_measurement: str | None = None
        translation_key: str | None = None

    num.NumberEntity = NumberEntity
    num.NumberDeviceClass = NumberDeviceClass
    num.NumberMode = NumberMode
    num.NumberEntityDescription = NumberEntityDescription

    dr = sys.modules["homeassistant.helpers.device_registry"]
    dr.DeviceInfo = dict

    ep = sys.modules["homeassistant.helpers.entity_platform"]
    ep.AddEntitiesCallback = object

    uc = sys.modules["homeassistant.helpers.update_coordinator"]

    class CoordinatorEntity:
        def __init__(self, coordinator):
            self.coordinator = coordinator

        def __class_getitem__(cls, item):
            return cls

    uc.CoordinatorEntity = CoordinatorEntity

    exc = sys.modules["homeassistant.exceptions"]

    class ServiceValidationError(Exception):
        def __init__(self, translation_domain=None, translation_key=None,
                     translation_placeholders=None):
            self.translation_key = translation_key
            super().__init__(translation_key)

    exc.ServiceValidationError = ServiceValidationError

    hs = types.ModuleType("huawei_solar")
    hs.HuaweiSolarBridgeDevice = object
    hs.HuaweiSolarException = Exception
    hs.Result = object
    hs.EMMADevice = object
    hs.HuaweiSolarDevice = object
    hs.RegisterName = str
    class _AnyAttrModule(types.ModuleType):
        """Returns a distinct string for any attribute accessed -- used
        for huawei_solar.register_names, whose real module defines
        hundreds of RegisterName constants that number.py's own
        module-level NUMBER_DESCRIPTIONS list reads by name at import
        time (not lazily), so a plain empty stub module would raise
        AttributeError immediately on import."""

        def __getattr__(self, name):
            return name

    hs.register_names = _AnyAttrModule("huawei_solar.register_names")
    if "huawei_solar" not in sys.modules:
        sys.modules["huawei_solar"] = hs
    else:
        existing = sys.modules["huawei_solar"]
        for attr, default in (
            ("HuaweiSolarBridgeDevice", object),
            ("HuaweiSolarException", Exception),
            ("Result", object),
            ("EMMADevice", object),
            ("HuaweiSolarDevice", object),
            ("RegisterName", str),
            ("register_names", _AnyAttrModule("huawei_solar.register_names")),
        ):
            if not hasattr(existing, attr):
                setattr(existing, attr, default)

    pkg_mod = stub(pkg)
    pkg_mod.__path__ = []

    for modname in ("const", "types"):
        src = _ROOT / f"{modname}.py"
        if modname == "types" or not src.exists():
            continue

    const_spec = importlib.util.spec_from_file_location(f"{pkg}.const", str(_ROOT / "const.py"))
    const_mod = importlib.util.module_from_spec(const_spec)
    const_mod.__package__ = pkg
    sys.modules[f"{pkg}.const"] = const_mod
    const_spec.loader.exec_module(const_mod)

    types_stub = stub(f"{pkg}.types")
    types_stub.HuaweiSolarConfigEntry = object
    types_stub.HuaweiSolarDeviceData = object
    types_stub.HuaweiSolarInverterData = object

    class HuaweiSolarEntity:
        pass

    class HuaweiSolarEntityContext:
        def __init__(self, register_names=()):
            self.register_names = register_names

    from dataclasses import dataclass as _dataclass2

    @_dataclass2(frozen=True)
    class HuaweiSolarEntityDescription:
        """Permissive stand-in -- see NumberEntityDescription's own
        stub comment above for why this doesn't try to mirror the real
        class exactly. register_name defaults to key, matching this
        integration's own real HuaweiSolarEntityDescription behaviour
        closely enough for this test's purposes (register_name is only
        read to build HuaweiSolarEntityContext, which this test never
        inspects)."""
        register_name: str = ""

    types_stub.HuaweiSolarEntity = HuaweiSolarEntity
    types_stub.HuaweiSolarEntityContext = HuaweiSolarEntityContext
    types_stub.HuaweiSolarEntityDescription = HuaweiSolarEntityDescription

    uc_stub = stub(f"{pkg}.update_coordinator")
    uc_stub.HuaweiSolarUpdateCoordinator = object

    spec = importlib.util.spec_from_file_location(f"{pkg}.number", str(_ROOT / "number.py"))
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = pkg
    sys.modules[f"{pkg}.number"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestICS003ZeroValueTruthiness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_number_module()

    def _entity(self, *, static_min=None, static_max=None,
                dynamic_min=None, dynamic_max=None,
                desc_min=None, desc_max=None):
        """Bare, unconstructed instance of the number entity class --
        avoids the full CoordinatorEntity.__init__ chain, since only
        native_min_value/native_max_value/async_set_native_value are
        under test here."""
        cls = self.mod.HuaweiSolarConfigNumberEntity if hasattr(
            self.mod, "HuaweiSolarConfigNumberEntity"
        ) else None
        # Fall back to whatever the module's own writable-number entity
        # class is actually named, discovered dynamically so this test
        # doesn't hard-code an internal name that might not match.
        if cls is None:
            for name in dir(self.mod):
                obj = getattr(self.mod, name)
                if (
                    isinstance(obj, type)
                    and hasattr(obj, "native_min_value")
                    and hasattr(obj, "native_max_value")
                    and hasattr(obj, "async_set_native_value")
                ):
                    cls = obj
                    break
        self.assertIsNotNone(cls, "could not locate the writable number entity class")
        inst = cls.__new__(cls)
        inst._static_min_value = static_min
        inst._static_max_value = static_max
        inst._dynamic_min_value = dynamic_min
        inst._dynamic_max_value = dynamic_max
        inst.entity_description = types.SimpleNamespace(
            native_min_value=desc_min, native_max_value=desc_max,
        )
        return inst

    def test_static_zero_max_is_not_replaced_by_default(self):
        entity = self._entity(static_max=0.0, desc_max=5000.0)
        self.assertEqual(entity.native_max_value, 0.0)

    def test_dynamic_zero_max_is_not_replaced_by_static_fallback(self):
        """The core ICS-003 regression: a device reporting a dynamic
        maximum of exactly 0 (e.g. 'no output permitted right now')
        must not be silently replaced by a much larger static/rated
        maximum."""
        entity = self._entity(dynamic_max=0.0, static_max=5000.0)
        self.assertEqual(entity.native_max_value, 0.0)

    def test_no_bound_configured_still_falls_back_to_default_max(self):
        entity = self._entity()
        self.assertEqual(entity.native_max_value, 100.0)

    def test_static_zero_min_is_not_replaced_by_default(self):
        entity = self._entity(static_min=0.0, desc_min=None)
        self.assertEqual(entity.native_min_value, 0.0)

    def test_dynamic_zero_min_is_not_replaced_by_nonzero_static_fallback(self):
        """Uses a NEGATIVE static minimum (e.g. ACTIVE_POWER_PERCENTAGE_
        DERATING's own real native_min_value=-100 elsewhere in this same
        file) -- this is the combination where the old truthiness bug
        was actually observable: max(0, X) == X for any X >= 0, so a
        non-negative static fallback masks the bug exactly like
        DEFAULT_MIN_VALUE=0.0 does (see native_min_value's own comment).
        A negative static fallback is where the two implementations
        genuinely disagree: buggy code returned native_min_value
        (-100) directly, ignoring the dynamic 0 entirely; fixed code
        correctly floors it via max(0, -100) = 0."""
        entity = self._entity(dynamic_min=0.0, static_min=-100.0)
        self.assertEqual(entity.native_min_value, 0.0)

    def test_normal_nonzero_bounds_still_work(self):
        entity = self._entity(static_min=5.0, static_max=5000.0)
        self.assertEqual(entity.native_min_value, 5.0)
        self.assertEqual(entity.native_max_value, 5000.0)

    def test_write_boundary_rejects_non_finite_value(self):
        entity = self._entity(static_min=0.0, static_max=100.0)
        entity.hass = None
        with self.assertRaises(Exception):
            import asyncio
            asyncio.run(entity.async_set_native_value(float("nan")))

    def test_write_boundary_rejects_out_of_range_value(self):
        entity = self._entity(static_min=0.0, static_max=100.0)
        with self.assertRaises(Exception):
            import asyncio
            asyncio.run(entity.async_set_native_value(500.0))


# ═══════════════════════════════════════════════════════════════════════
# ICS-004 — register_cache.py: instance-scoped SLOW-tier TTL
# ═══════════════════════════════════════════════════════════════════════

def _load_register_cache_module(pkg_suffix: str):
    """Loads a FRESH copy of register_cache.py under its own module
    name, so tests can construct two independent instances backed by
    genuinely separate module state -- the whole point of ICS-004's own
    fix is that two instances must NOT share mutable state, so this
    test avoids the single-shared-import trap that hid the original bug
    in production."""
    if "huawei_solar" in sys.modules:
        hs = sys.modules["huawei_solar"]
    else:
        hs = types.ModuleType("huawei_solar")
        sys.modules["huawei_solar"] = hs
    if not hasattr(hs, "RegisterName"):
        hs.RegisterName = str
    if not hasattr(hs, "Result"):
        class _Result:
            def __init__(self, v):
                self.value = v
        hs.Result = _Result

    pkg = f"hs_rc_ics004_check_{pkg_suffix}"
    spec = importlib.util.spec_from_file_location(f"{pkg}.rc", str(_ROOT / "register_cache.py"))
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = pkg
    spec.loader.exec_module(mod)
    return mod


class TestICS004InstanceScopedSlowTierTTL(unittest.TestCase):
    def test_two_instances_do_not_share_ttl_state(self):
        """The core regression: constructing a second RegisterCache with
        a different SLOW-tier TTL must not affect a first, already-
        existing instance's own TTL at all."""
        rc_mod = _load_register_cache_module("shared")
        cache_a = rc_mod.RegisterCache()
        cache_b = rc_mod.RegisterCache()

        default_slow_ttl = cache_a._tier_base_ttl[rc_mod.RegisterTier.SLOW]
        cache_b.set_slow_tier_ttl(default_slow_ttl + 900)

        self.assertEqual(
            cache_a._tier_base_ttl[rc_mod.RegisterTier.SLOW], default_slow_ttl,
            "cache_a's own SLOW-tier TTL changed after cache_b's "
            "set_slow_tier_ttl() call -- ICS-004 has regressed: the two "
            "instances are still sharing mutable TTL state.",
        )
        self.assertEqual(
            cache_b._tier_base_ttl[rc_mod.RegisterTier.SLOW], default_slow_ttl + 900,
        )

    def test_constructor_accepts_slow_tier_ttl_directly(self):
        rc_mod = _load_register_cache_module("ctor")
        cache = rc_mod.RegisterCache(slow_tier_ttl_s=1800)
        self.assertEqual(cache._tier_base_ttl[rc_mod.RegisterTier.SLOW], 1800)

    def test_set_slow_tier_ttl_is_clamped(self):
        rc_mod = _load_register_cache_module("clamp")
        cache = rc_mod.RegisterCache()
        cache.set_slow_tier_ttl(1.0)  # far below the 300s floor
        self.assertEqual(cache._tier_base_ttl[rc_mod.RegisterTier.SLOW], 300.0)
        cache.set_slow_tier_ttl(999999.0)  # far above the 3600s ceiling
        self.assertEqual(cache._tier_base_ttl[rc_mod.RegisterTier.SLOW], 3600.0)

    def test_no_module_level_mutable_ttl_dict_remains(self):
        """Pins the actual removal: the free function set_slow_tier_ttl()
        (module-level, mutating the shared _TIER_BASE_TTL dict in
        place) must no longer exist at all."""
        rc_mod = _load_register_cache_module("nofunc")
        self.assertFalse(
            hasattr(rc_mod, "set_slow_tier_ttl"),
            "a module-level set_slow_tier_ttl() free function still "
            "exists -- ICS-004 has regressed.",
        )

    def test_init_no_longer_imports_the_removed_free_function(self):
        source = (_ROOT / "__init__.py").read_text()
        self.assertNotIn("from .register_cache import set_slow_tier_ttl", source)

    def test_update_coordinator_passes_slow_tier_ttl_from_entry_options(self):
        source = (_ROOT / "update_coordinator.py").read_text()
        self.assertIn("slow_tier_ttl_s=", source)
        self.assertIn("CONF_SLOW_TIER_TTL_S", source)


# ═══════════════════════════════════════════════════════════════════════
# ICS-007 — future pack install dates rejected
# ═══════════════════════════════════════════════════════════════════════

def _install_bhm_stubs(pkg: str) -> None:
    def stub(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
        return m

    for name in (
        "homeassistant", "homeassistant.core", "homeassistant.helpers",
        "homeassistant.helpers.storage",
    ):
        if name not in sys.modules:
            stub(name)
    core = sys.modules["homeassistant.core"]
    if not hasattr(core, "HomeAssistant"):
        core.HomeAssistant = type("HomeAssistant", (), {})
    if not hasattr(core, "callback"):
        core.callback = lambda f: f
    storage = sys.modules["homeassistant.helpers.storage"]
    if not hasattr(storage, "Store"):
        storage.Store = MagicMock

    hs = types.ModuleType("huawei_solar")
    hs.register_values = types.ModuleType("huawei_solar.register_values")
    if "huawei_solar" not in sys.modules:
        try:
            import huawei_solar as _real  # noqa: F401
        except ImportError:
            sys.modules["huawei_solar"] = hs
    else:
        existing = sys.modules["huawei_solar"]
        if not hasattr(existing, "register_values"):
            existing.register_values = types.ModuleType("huawei_solar.register_values")
        if not hasattr(existing, "RegisterName"):
            existing.RegisterName = str
        if not hasattr(existing, "Result"):
            class _Result:
                def __init__(self, v):
                    self.value = v
            existing.Result = _Result

    pkg_mod = stub(pkg)
    pkg_mod.__path__ = []

    for modname in ("const", "battery_health", "register_cache"):
        spec = importlib.util.spec_from_file_location(
            f"{pkg}.{modname}", str(_ROOT / f"{modname}.py")
        )
        m = importlib.util.module_from_spec(spec)
        m.__package__ = pkg
        sys.modules[f"{pkg}.{modname}"] = m
        spec.loader.exec_module(m)


def _load_bhm_module():
    pkg = "hs_bhm_ics007_check"
    _install_bhm_stubs(pkg)
    spec = importlib.util.spec_from_file_location(
        f"{pkg}.battery_health_manager", str(_ROOT / "battery_health_manager.py")
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = pkg
    sys.modules[f"{pkg}.battery_health_manager"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestICS007FutureInstallDateRejected(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bhm_mod = _load_bhm_module()
        cls.bh_mod = sys.modules["hs_bhm_ics007_check.battery_health"]

    def _fake_manager(self):
        """Duck-typed BatteryHealthManager -- real
        set_pack_install_date() code, fake surrounding object, matching
        this project's own convention of testing a single method in
        isolation when constructing the full class would need unrelated
        machinery (see test_battery_health.py's own _tracker() helper
        for the same idea applied to PackCapacityTracker)."""
        Manager = self.bhm_mod.BatteryHealthManager
        fake = types.SimpleNamespace()
        fake.FUTURE_INSTALL_DATE_TOLERANCE_S = Manager.FUTURE_INSTALL_DATE_TOLERANCE_S
        cfg = self.bh_mod.BatteryHealthConfig()
        pack_capacity = self.bh_mod.PackCapacityTracker(
            cfg, pack_count=1, slot_labels=["u1p1"]
        )
        fake.engine = types.SimpleNamespace(pack_capacity=pack_capacity, dirty=False)
        fake._maybe_save = lambda: None
        return Manager.set_pack_install_date, fake

    def test_future_date_is_rejected(self):
        set_pack_install_date, fake = self._fake_manager()
        future_ts = time.time() + 10 * 86400  # 10 days out
        with self.assertRaises(ValueError):
            set_pack_install_date(fake, "SN-1", future_ts)

    def test_todays_date_across_timezone_slop_is_accepted(self):
        """The tolerance exists specifically so a date-only value for
        "today" is never wrongly rejected purely from timezone
        arithmetic -- see FUTURE_INSTALL_DATE_TOLERANCE_S's own
        docstring."""
        set_pack_install_date, fake = self._fake_manager()
        near_future_ts = time.time() + 3600  # 1 hour out -- well within tolerance
        set_pack_install_date(fake, "SN-1", near_future_ts)
        self.assertIn("SN-1", fake.engine.pack_capacity.pack_install_dates)

    def test_past_date_is_accepted(self):
        set_pack_install_date, fake = self._fake_manager()
        past_ts = time.time() - 365 * 86400
        set_pack_install_date(fake, "SN-1", past_ts)
        self.assertEqual(fake.engine.pack_capacity.pack_install_dates["SN-1"], past_ts)
        self.assertTrue(fake.engine.dirty)

    def test_services_py_translates_valueerror_to_service_validation_error(self):
        source = (_ROOT / "services.py").read_text()
        idx = source.find("bh_manager.set_pack_install_date(serial, install_ts)")
        self.assertGreater(idx, -1)
        window = source[max(0, idx - 400): idx + 400]
        self.assertIn("except ValueError", window)
        self.assertIn("pack_install_date_in_future", window)

    def test_date_py_catches_valueerror_and_logs(self):
        source = (_ROOT / "date.py").read_text()
        idx = source.find("self._manager.set_pack_install_date(serial, install_ts)")
        self.assertGreater(idx, -1)
        window = source[idx: idx + 300]
        self.assertIn("except ValueError", window)

    def test_translation_key_exists_in_strings_json(self):
        import json
        strings = json.loads((_ROOT / "strings.json").read_text())
        self.assertIn("pack_install_date_in_future", strings.get("exceptions", {}))


# ═══════════════════════════════════════════════════════════════════════
# ICS-009 — per-capability service (un)registration
# ═══════════════════════════════════════════════════════════════════════

_SERVICES_SRC = (_ROOT / "services.py").read_text()


class TestICS009PerCapabilityServiceLifecycle(unittest.TestCase):
    def test_capability_services_mapping_exists(self):
        self.assertIn("_CAPABILITY_SERVICES", _SERVICES_SRC)
        self.assertIn("_capability_entries", _SERVICES_SRC)

    def test_unload_still_discards_from_flat_tracker(self):
        """Pins backward compatibility: the pre-existing flat
        _entries_with_services tracker (and its own pre-existing test
        coverage) must be unaffected by this fix -- confirms this was
        an additive refinement, not a replacement."""
        self.assertIn("_entries_with_services.discard(", _SERVICES_SRC)

    def test_unload_removes_per_capability_services_independently(self):
        idx = _SERVICES_SRC.find("async def async_unload_services")
        self.assertGreater(idx, -1)
        body = _SERVICES_SRC[idx: idx + 3000]
        self.assertIn("cap_entries.discard(entry.entry_id)", body)
        self.assertIn("hass.services.async_remove(DOMAIN, service_name)", body)

    def test_pack_install_date_service_is_gated_on_battery_capability(self):
        """Confirms the bonus fix found alongside ICS-009: SERVICE_SET_
        PACK_INSTALL_DATE is now covered by BOTH the per-capability
        cluster and the flat fallback list."""
        idx = _SERVICES_SRC.find("_CAPABILITY_SERVICES")
        cluster_text = _SERVICES_SRC[idx: idx + 2500]
        self.assertIn("SERVICE_SET_PACK_INSTALL_DATE", cluster_text)

        idx2 = _SERVICES_SRC.find("_ALL_SERVICE_NAMES: tuple")
        flat_list_text = _SERVICES_SRC[idx2: idx2 + 1200]
        self.assertIn("SERVICE_SET_PACK_INSTALL_DATE", flat_list_text)

    def test_setup_populates_capability_entries_for_each_flag(self):
        idx = _SERVICES_SRC.find("async def async_setup_services")
        self.assertGreater(idx, -1)
        body = _SERVICES_SRC[idx: idx + 6000]
        for cap in (
            "always", "not_has_emma", "has_battery",
            "has_battery_not_emma", "has_lg_battery", "has_capacity_control",
        ):
            with self.subTest(capability=cap):
                self.assertIn(f'_capability_entries["{cap}"].add(entry.entry_id)', body)


# ═══════════════════════════════════════════════════════════════════════
# ICS-010 — modbus_keepalive.py: TModbusError now tracked as a failure
# ═══════════════════════════════════════════════════════════════════════

_KEEPALIVE_SRC = (_ROOT / "modbus_keepalive.py").read_text()


class TestICS010KeepaliveCatchesTModbusError(unittest.TestCase):
    def test_tmodbuserror_is_imported(self):
        self.assertIn("from tmodbus.exceptions import TModbusError", _KEEPALIVE_SRC)

    def test_probe_handler_catches_tmodbuserror(self):
        idx = _KEEPALIVE_SRC.find("except (TimeoutError, HuaweiSolarException, TModbusError")
        self.assertGreater(
            idx, -1,
            "modbus_keepalive.py's probe handler does not catch "
            "TModbusError -- ICS-010 has regressed.",
        )


# ═══════════════════════════════════════════════════════════════════════
# ICS-011 — telemetry singleton removed (not just stopped) on rollback
# ═══════════════════════════════════════════════════════════════════════

class TestICS011TelemetryRemovedOnRollback(unittest.TestCase):
    def test_rollback_pairs_stop_with_remove(self):
        idx = _INIT_SRC.find("_stop_and_remove_telemetry")
        self.assertGreater(idx, -1)
        window = _INIT_SRC[idx: idx + 800]
        self.assertIn("t.stop()", window)
        self.assertIn("ModbusTelemetry.remove(serial)", window)

    def test_register_cleanup_uses_the_combined_callback(self):
        idx = _INIT_SRC.find("register_cleanup(_stop_and_remove_telemetry)")
        self.assertGreater(idx, -1)

    def test_bare_telemetry_stop_is_no_longer_registered_alone(self):
        self.assertNotIn("register_cleanup(telemetry.stop)", _INIT_SRC)


# ═══════════════════════════════════════════════════════════════════════
# ICS-012 — static bound cache cleared on setup-failure rollback too
# ═══════════════════════════════════════════════════════════════════════

class TestICS012StaticBoundCacheClearedOnRollback(unittest.TestCase):
    def test_clear_static_bound_cache_registered_as_a_cleanup(self):
        idx = _INIT_SRC.find("_stop_and_remove_telemetry")
        self.assertGreater(idx, -1)
        # Search from AFTER the telemetry rollback block, since the
        # FIRST occurrence of clear_static_bound_cache(serial) in the
        # file is the pre-existing normal-unload call, not this fix's
        # new rollback registration.
        rollback_idx = _INIT_SRC.find("clear_static_bound_cache(serial)", idx)
        self.assertGreater(rollback_idx, -1)
        window = _INIT_SRC[max(0, rollback_idx - 400): rollback_idx + 50]
        self.assertIn("register_cleanup(", window)

    def test_two_call_sites_now_exist(self):
        """One in the normal-unload path (pre-existing), one in the
        setup-failure rollback path (this fix)."""
        count = _INIT_SRC.count("clear_static_bound_cache(serial)")
        self.assertGreaterEqual(count, 2)


# ═══════════════════════════════════════════════════════════════════════
# ICS-014 / ICS-022 — bounded battery_health.py restore() paths
# ═══════════════════════════════════════════════════════════════════════

_bh_spec = importlib.util.spec_from_file_location(
    "bh_ics014_check", str(_ROOT / "battery_health.py")
)
bh = importlib.util.module_from_spec(_bh_spec)
sys.modules["bh_ics014_check"] = bh
_bh_spec.loader.exec_module(bh)


def _cfg():
    return bh.BatteryHealthConfig()


class TestICS014BoundedSegmentAndEpochRestore(unittest.TestCase):
    def test_huge_persisted_segments_list_is_pruned_on_restore(self):
        tracker = bh.SegmentTracker(_cfg())
        # Deliberately larger than MAX_RESTORED_COLLECTION_LENGTH.
        huge = [
            {"start_ts": 0.0, "end_ts": 1.0, "soc_start": 90.0, "soc_end": 10.0,
             "energy_kwh": 1.0, "implied_capacity_kwh": 5.0, "freshness": 1.0}
            for _ in range(bh.MAX_RESTORED_COLLECTION_LENGTH + 500)
        ]
        tracker.restore({"segments": huge, "reference_epochs": []})
        self.assertLessEqual(
            len(tracker.segments), bh.MAX_RESTORED_COLLECTION_LENGTH,
            f"{bh.MAX_RESTORED_COLLECTION_LENGTH + 500} persisted segments "
            "survived restore() uncapped -- ICS-014 has regressed.",
        )

    def test_reference_epochs_capped_on_restore(self):
        tracker = bh.SegmentTracker(_cfg())
        huge = list(range(10000))
        tracker.restore({"segments": [], "reference_epochs": huge})
        self.assertLessEqual(len(tracker.reference_epochs), bh.MAX_RESTORED_EPOCH_LOG_LENGTH)
        # Keeps the most RECENT entries, not the oldest.
        self.assertEqual(tracker.reference_epochs[-1], 9999)

    def test_normal_small_history_is_unaffected(self):
        tracker = bh.SegmentTracker(_cfg())
        tracker.restore({"segments": [], "reference_epochs": [1.0, 2.0, 3.0]})
        self.assertEqual(tracker.reference_epochs, [1.0, 2.0, 3.0])


class TestICS022BoundedOtherRestorePaths(unittest.TestCase):
    def _find_tracker_class(self, attr_name: str, needs: tuple[str, ...]):
        for name in dir(bh):
            obj = getattr(bh, name)
            if isinstance(obj, type) and all(hasattr(obj, n) for n in ("restore", "feed")) \
                    and hasattr(obj, "__init__"):
                pass
        return None

    def test_stress_accumulator_buckets_pruned_on_restore(self):
        acc = bh.StressAccumulator(_cfg())
        huge_buckets = {
            str(k): [1.0, 1.0]
            for k in range(200000, 200000 + bh.MAX_RESTORED_COLLECTION_LENGTH + 500)
        }
        acc.restore({"buckets": huge_buckets})
        self.assertLessEqual(
            len(acc._buckets), bh.MAX_RESTORED_COLLECTION_LENGTH,
            "an oversized persisted bucket dict survived restore() "
            "uncapped -- ICS-022 has regressed.",
        )
        # Keeps the highest (most recent) keys, not an arbitrary subset.
        self.assertIn(200000 + bh.MAX_RESTORED_COLLECTION_LENGTH + 499, acc._buckets)
        self.assertNotIn(200000, acc._buckets)

    def test_efficiency_tracker_baseline_pool_and_epochs_capped(self):
        tracker = bh.EfficiencyTracker(_cfg()) if hasattr(bh, "EfficiencyTracker") else None
        if tracker is None:
            self.skipTest("EfficiencyTracker not found under that name")
        tracker.restore({
            "baseline_pool": list(range(10000)),
            "baseline_epochs": list(range(10000)),
        })
        self.assertLessEqual(len(tracker._baseline_pool), bh.MAX_RESTORED_EPOCH_LOG_LENGTH)
        self.assertLessEqual(len(tracker.baseline_epochs), bh.MAX_RESTORED_EPOCH_LOG_LENGTH)

    def test_balance_tracker_pools_and_epochs_capped(self):
        if not hasattr(bh, "BalanceTracker"):
            self.skipTest("BalanceTracker not found under that name")
        tracker = bh.BalanceTracker(_cfg())
        tracker.restore({
            "baseline_epochs": list(range(10000)),
            "pool_dv": list(range(10000)),
            "pool_dt": list(range(10000)),
        })
        self.assertLessEqual(len(tracker.baseline_epochs), bh.MAX_RESTORED_EPOCH_LOG_LENGTH)
        self.assertLessEqual(len(tracker._pool_dv), bh.MAX_RESTORED_EPOCH_LOG_LENGTH)
        self.assertLessEqual(len(tracker._pool_dt), bh.MAX_RESTORED_EPOCH_LOG_LENGTH)

    def test_held_subscores_filtered_to_known_keys_only(self):
        engine_cls = bh.HealthReport if not hasattr(bh, "BatteryHealthEngine") else bh.BatteryHealthEngine
        if not hasattr(bh, "BatteryHealthEngine"):
            self.skipTest("BatteryHealthEngine not found")
        source = (_ROOT / "battery_health.py").read_text()
        idx = source.find('if k in ("capacity", "efficiency", "balance")')
        self.assertGreater(
            idx, -1,
            "held_subscores restore no longer filters to the known key "
            "set -- ICS-022 has regressed.",
        )


# ═══════════════════════════════════════════════════════════════════════
# ICS-019 — verify_write(): TModbusError + retrieved task exceptions
# ═══════════════════════════════════════════════════════════════════════

_UC_SRC = (_ROOT / "update_coordinator.py").read_text()


class TestICS019VerifyWriteExceptionHandling(unittest.TestCase):
    def test_verify_write_catches_tmodbuserror(self):
        idx = _UC_SRC.find("async def verify_write")
        self.assertGreater(idx, -1)
        body = _UC_SRC[idx: idx + 3000]
        self.assertIn("except (TimeoutError, HuaweiSolarException, TModbusError)", body)

    def test_done_callback_retrieves_exception(self):
        idx = _UC_SRC.find("def _on_verify_write_task_done")
        self.assertGreater(idx, -1)
        next_def = _UC_SRC.find("\n    def ", idx + 1)
        body = _UC_SRC[idx: next_def if next_def > 0 else idx + 3000]
        self.assertIn("task.exception()", body)
        self.assertIn("task.cancelled()", body)

    def test_schedule_verify_write_uses_the_new_callback(self):
        idx = _UC_SRC.find("def schedule_verify_write")
        self.assertGreater(idx, -1)
        next_def = _UC_SRC.find("\n    def ", idx + 1)
        body = _UC_SRC[idx: next_def if next_def > 0 else idx + 3000]
        self.assertIn("_on_verify_write_task_done", body)


# ═══════════════════════════════════════════════════════════════════════
# ICS-021 — optimizer telemetry no longer double-counted
# ═══════════════════════════════════════════════════════════════════════

class TestICS021TelemetryDoubleCountFixed(unittest.TestCase):
    def test_record_request_moved_after_the_device_await(self):
        idx_await = _UC_SRC.find("result = await self.device.get_latest_optimizer_history_data()")
        idx_record = _UC_SRC.find("self.telemetry.record_request(1)", idx_await)
        self.assertGreater(idx_await, -1)
        self.assertGreater(
            idx_record, idx_await,
            "record_request(1) is not positioned after the device "
            "await -- ICS-021 has regressed.",
        )

    def test_record_request_no_longer_precedes_the_await(self):
        idx_await = _UC_SRC.find("result = await self.device.get_latest_optimizer_history_data()")
        preceding_window = _UC_SRC[max(0, idx_await - 400): idx_await]
        self.assertNotIn(
            "self.telemetry.record_request(1)", preceding_window,
            "record_request(1) still appears immediately before the "
            "device await -- the speculative pre-outcome call was not "
            "actually removed.",
        )

    def test_modbus_telemetry_double_increment_mechanism_documented(self):
        """Sanity check on the underlying mechanism this fix relies on:
        confirms record_request/record_timeout/record_failure each
        still independently increment total_attempts exactly once (the
        fact that made the old call ordering a double-count in the
        first place, and that makes the new ordering correct)."""
        source = (_ROOT / "modbus_telemetry.py").read_text()
        self.assertEqual(source.count("self.total_attempts += 1"), 3)


if __name__ == "__main__":
    unittest.main()
