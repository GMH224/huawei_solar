"""Module-import and class-contract tests (v1.2.4).

WHY THIS FILE EXISTS
--------------------
v1.2.3 shipped with 412 green tests and still failed to load at all:

    File "adaptive_modbus.py", line 367, in async_load
        raw = await self._store.async_load()
    File "homeassistant/helpers/storage.py", line 622, in _async_migrate_func
        raise NotImplementedError

Two independent gaps let that through:

1. ``test_update_coordinator.py`` validates that module by **string-matching
   its source text**. It never imports it, never constructs a coordinator and
   never executes a code path, so any import-time or attribute-level defect
   passes untouched.

2. Nothing checked that a class actually *owns* the methods it calls. The same
   release added ``self._record_shed()`` to
   ``HuaweiSolarOptimizerUpdateCoordinator``, which is a SIBLING of
   ``HuaweiSolarUpdateCoordinator``, not a subclass — and the pre-existing
   ``self._record_failure()`` calls there had the same latent fault.

This file closes both. It imports every module in the integration against a
realistic Home Assistant stub, then asserts that each coordinator class
resolves the helpers it invokes.
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys
import types
import unittest

_ROOT = pathlib.Path(__file__).parent.parent

#: Modules importable with stubs alone. Platform modules (sensor, switch, ...)
#: pull in large parts of HA's entity machinery and are covered by
#: test_entities.py instead.
_MODULES = [
    "const",
    "night_mode",
    "modbus_guard",
    "register_cache",
    "modbus_telemetry",
    "adaptive_modbus",
    "battery_health",
    "bus_diagnostics",
]


def _stub(name: str, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _install_ha_stubs() -> None:
    for name in (
        "homeassistant",
        "homeassistant.core",
        "homeassistant.const",
        "homeassistant.helpers",
        "homeassistant.helpers.event",
        "homeassistant.helpers.storage",
        "homeassistant.helpers.device_registry",
        "homeassistant.helpers.update_coordinator",
        "homeassistant.components",
        "homeassistant.components.sensor",
    ):
        if name not in sys.modules:
            _stub(name)

    core = sys.modules["homeassistant.core"]
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))
    if not hasattr(core, "callback"):
        core.callback = lambda f: f
    if not hasattr(core, "CoreState"):
        core.CoreState = type("CoreState", (), {"running": "running"})

    ev = sys.modules["homeassistant.helpers.event"]
    if not hasattr(ev, "async_track_time_interval"):
        ev.async_track_time_interval = lambda *a, **k: (lambda: None)
    if not hasattr(ev, "async_call_later"):
        ev.async_call_later = lambda *a, **k: (lambda: None)

    st = sys.modules["homeassistant.helpers.storage"]
    if not hasattr(st, "Store"):
        class Store:  # minimal stand-in
            def __init__(self, hass, version, key):
                self.version, self.key = version, key

            async def async_load(self):
                return None

            async def async_save(self, data):
                return None

            def async_delay_save(self, fn, delay=0):
                return None

        st.Store = Store

    dr = sys.modules["homeassistant.helpers.device_registry"]
    if not hasattr(dr, "DeviceInfo"):
        dr.DeviceInfo = dict

    sensor = sys.modules["homeassistant.components.sensor"]
    if not hasattr(sensor, "SensorEntity"):
        sensor.SensorEntity = type("SensorEntity", (), {})
    if not hasattr(sensor, "SensorStateClass"):
        sensor.SensorStateClass = type(
            "SensorStateClass", (),
            {"MEASUREMENT": "measurement", "TOTAL_INCREASING": "total_increasing",
             "TOTAL": "total"},
        )
    if not hasattr(sensor, "SensorDeviceClass"):
        sensor.SensorDeviceClass = type("SensorDeviceClass", (), {"ENUM": "enum"})

    const = sys.modules["homeassistant.const"]
    for attr, value in (("PERCENTAGE", "%"), ("EVENT_HOMEASSISTANT_STARTED", "started"),
                        ("EVENT_HOMEASSISTANT_STOP", "stop")):
        if not hasattr(const, attr):
            setattr(const, attr, value)
    if not hasattr(const, "EntityCategory"):
        const.EntityCategory = type(
            "EntityCategory", (), {"DIAGNOSTIC": "diagnostic", "CONFIG": "config"}
        )


def _ensure_huawei_solar_stub() -> None:
    """Augment (never replace) the shared ``huawei_solar`` library stub.

    Other test modules install their own stub of the vendor library, and test
    execution order is not fixed. Replacing it would break them; assuming it is
    complete breaks this file. So we add only what is missing.
    """
    hs = sys.modules.get("huawei_solar")
    if hs is None:
        hs = _stub("huawei_solar")
        hs.__path__ = []

    if not hasattr(hs, "RegisterName"):
        class RegisterName(str):
            _members: dict = {}

            def __class_getitem__(cls, key):
                return cls._members.setdefault(key, cls(key))

        hs.RegisterName = RegisterName

    if not hasattr(hs, "Result"):
        class Result:
            def __init__(self, value=None):
                self.value = value

        hs.Result = Result

    for name in ("HuaweiSolarDevice", "SUN2000Device", "EMMADevice"):
        if not hasattr(hs, name):
            setattr(hs, name, type(name, (), {}))

    for name in (
        "HuaweiSolarException", "ConnectionException", "ReadException",
        "WriteException", "PermissionDenied", "DecodeError",
        "InvalidCredentials", "ConnectionInterruptedException",
    ):
        if not hasattr(hs, name):
            setattr(hs, name, type(name, (Exception,), {}))

    class _Namespace:
        def __getattr__(self, item):
            return hs.RegisterName[item.lower()]

    for name in ("register_names", "register_values"):
        if not hasattr(hs, name):
            setattr(hs, name, _Namespace())


_install_ha_stubs()
_ensure_huawei_solar_stub()

_PKG = "hs_import_check"
if _PKG not in sys.modules:
    pkg = _stub(_PKG)
    pkg.__path__ = []


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{_PKG}.{name}", str(_ROOT / f"{name}.py")
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = _PKG
    sys.modules[f"{_PKG}.{name}"] = module
    spec.loader.exec_module(module)
    return module


class TestModulesImport(unittest.TestCase):
    """Every module must actually import — not merely parse."""

    def setUp(self):
        # Re-assert the stubs at TEST time, not import time: other test modules
        # install their own `huawei_solar` stub while pytest is collecting, and
        # collection order is not fixed, so whatever ran last would otherwise
        # decide whether this file works.
        _install_ha_stubs()
        _ensure_huawei_solar_stub()

    def test_all_modules_import(self):
        for name in _MODULES:
            with self.subTest(module=name):
                try:
                    _load(name)
                except Exception as err:  # noqa: BLE001
                    self.fail(f"{name}.py failed to import: {type(err).__name__}: {err}")


class TestCoordinatorMethodOwnership(unittest.TestCase):
    """A class must own (or inherit) every ``self._x()`` helper it calls.

    ``HuaweiSolarOptimizerUpdateCoordinator`` is a sibling of
    ``HuaweiSolarUpdateCoordinator``, so helpers defined on one are NOT
    available on the other. Both the v1.2.3 ``_record_shed`` regression and a
    pre-existing ``_record_failure`` fault came from assuming otherwise, and
    neither surfaces until an error path runs on real hardware.

    Checked by AST so it needs no HA runtime and cannot rot.
    """

    #: Helpers whose ownership matters — they run only on error paths, so a
    #: missing definition stays invisible until something is already wrong.
    _TRACKED = ("_record_timeout", "_record_failure", "_record_shed")

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse((_ROOT / "update_coordinator.py").read_text())
        cls.classes = [
            n for n in cls.tree.body if isinstance(n, ast.ClassDef)
        ]

    def _defined(self, cls_node) -> set[str]:
        return {
            n.name for n in cls_node.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    def _called(self, cls_node) -> set[str]:
        out = set()
        for node in ast.walk(cls_node):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
            ):
                out.add(node.func.attr)
        return out

    def test_tracked_helpers_are_owned_by_their_callers(self):
        for cls_node in self.classes:
            defined = self._defined(cls_node)
            called = self._called(cls_node)
            for helper in self._TRACKED:
                if helper in called:
                    with self.subTest(cls=cls_node.name, helper=helper):
                        self.assertIn(
                            helper, defined,
                            f"{cls_node.name} calls self.{helper}() but does not "
                            f"define it, and does not inherit it (sibling classes). "
                            f"This raises AttributeError on an error path.",
                        )

    def test_optimizer_and_batch_coordinators_are_siblings(self):
        """Pins the assumption the ownership test depends on."""
        bases = {
            c.name: [b.value.id if isinstance(b, ast.Subscript) and isinstance(b.value, ast.Name)
                     else getattr(b, "id", None)
                     for b in c.bases]
            for c in self.classes
        }
        self.assertIn("HuaweiSolarOptimizerUpdateCoordinator", bases)
        self.assertNotIn(
            "HuaweiSolarUpdateCoordinator",
            bases["HuaweiSolarOptimizerUpdateCoordinator"],
            "if this ever becomes a subclass, the ownership test can be relaxed",
        )


class TestCoordinatorClassIntegrity(unittest.TestCase):
    """Each coordinator class must actually CONTAIN its required methods.

    REGRESSION (v1.3.1): a module-level ``def`` (column 0) was inserted inside
    a class body. In Python that TERMINATES the class — every method defined
    after it, including ``_async_update_data``, silently became a module-level
    function. Home Assistant then hit its base-class stub:

        NotImplementedError: Update method not implemented

    ...and every sensor on the integration failed to update.

    ``ast.parse`` accepted the file. All 440 tests passed. Nothing noticed,
    because no test instantiates a coordinator — a gap AUDIT_1.2.4 §6.1
    recorded as open, and which then caused the very next outage.

    This check is deliberately structural (AST): it needs no HA runtime, and
    it fails on exactly the shape of mistake that caused the incident.
    """

    #: Methods HA requires, or that the integration's own error paths call.
    REQUIRED = {
        "HuaweiSolarUpdateCoordinator": {
            "_async_update_data", "_execute_batch", "_record_timeout",
            "_record_failure", "_record_shed",
        },
        "HuaweiSolarOptimizerUpdateCoordinator": {
            "_async_update_data", "_record_failure",
        },
    }

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse((_ROOT / "update_coordinator.py").read_text())
        cls.classes = {
            n.name: n for n in cls.tree.body if isinstance(n, ast.ClassDef)
        }

    def test_required_methods_are_inside_their_class(self):
        for cls_name, required in self.REQUIRED.items():
            with self.subTest(cls=cls_name):
                self.assertIn(cls_name, self.classes)
                defined = {
                    m.name for m in self.classes[cls_name].body
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                missing = required - defined
                self.assertFalse(
                    missing,
                    f"{cls_name} is missing {sorted(missing)} — most likely a "
                    f"module-level def was inserted into the class body, which "
                    f"silently ends the class",
                )

    def test_classes_are_not_suspiciously_small(self):
        """A truncated class body is the symptom; catch it directly."""
        for cls_name, minimum in (("HuaweiSolarUpdateCoordinator", 8),
                                  ("HuaweiSolarOptimizerUpdateCoordinator", 3)):
            with self.subTest(cls=cls_name):
                count = sum(
                    1 for m in self.classes[cls_name].body
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                )
                self.assertGreaterEqual(count, minimum)

    def test_no_module_level_def_between_class_methods(self):
        """Pin the failure mode itself, across every module in the package."""
        for path in sorted(_ROOT.glob("*.py")):
            tree = ast.parse(path.read_text())
            for node in tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        # A method's col_offset must be indented relative to
                        # its class; column 0 means the class body ended.
                        with self.subTest(file=path.name, cls=node.name,
                                          fn=item.name):
                            self.assertGreater(item.col_offset, node.col_offset)


if __name__ == "__main__":
    unittest.main()


class TestConstImportsAreDefined(unittest.TestCase):
    """Every name imported from ``const`` must actually be defined there.

    REGRESSION — the exact failure that broke v1.3.5's deployed package:

        ImportError: cannot import name 'CONF_COALESCE_SLOW_TIER' from
        'custom_components.huawei_solar.const'

    v1.3.5 removed CONF_COALESCE_SLOW_TIER and CONF_PREFER_NIGHT_FOR_SLOW
    (and their DEFAULT_ counterparts) from const.py after retiring the
    coalescing/night-deferral mechanism they configured — but a working copy
    of the deployed package retained a stale ``__init__.py`` still importing
    them, which fails at import time before any test in this suite (or the
    module-import checks above) ever gets a chance to run, because
    ``__init__.py`` itself was never in the covered module list.

    This check is deliberately NOT a runtime import of ``__init__.py``:
    that module transitively pulls in Home Assistant's config-entry,
    service-registration and entity-platform machinery plus the full
    huawei_solar device hierarchy, which would require stubbing a large and
    constantly-shifting surface unrelated to the actual defect class. A pure
    AST cross-check of "every name imported from const.py is defined in
    const.py" catches this exact bug — and any future instance of the same
    class, in ANY file, not just __init__.py — in milliseconds and with zero
    runtime dependencies.
    """

    @staticmethod
    def _names_imported_from(path: pathlib.Path, module: str) -> set[str]:
        tree = ast.parse(path.read_text())
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == module:
                names.update(alias.name for alias in node.names)
        return names

    @staticmethod
    def _names_defined_in(path: pathlib.Path) -> set[str]:
        tree = ast.parse(path.read_text())
        names: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                names.update(
                    t.id for t in node.targets if isinstance(t, ast.Name)
                )
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            # v2.0.15 FIX (external ICS review, this release): const.py's
            # own first module-level FUNCTION (elevated_permissions_
            # enabled) exposed a real gap here -- this helper only ever
            # recognized ast.Assign/ast.AnnAssign, since every prior name
            # in const.py was a constant. A function defined at module
            # level is exactly as valid and importable a name as a
            # constant is; not recognizing it here would make this test
            # fail against genuinely correct code, for a name that
            # demonstrably imports and runs cleanly (see this file's own
            # TestModulesImport, and test_const_services.py, which both
            # already exercise const.py directly). Root-caused and fixed
            # here rather than worked around by avoiding a function
            # definition in const.py, which would have been treating this
            # test's own coverage gap as a constraint on the code instead.
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
        return names

    def test_every_const_import_resolves(self):
        const_names = self._names_defined_in(_ROOT / "const.py")
        for pyfile in sorted(_ROOT.glob("*.py")):
            if pyfile.name == "const.py":
                continue
            imported = self._names_imported_from(pyfile, "const")
            missing = imported - const_names
            with self.subTest(file=pyfile.name):
                self.assertFalse(
                    missing,
                    f"{pyfile.name} imports {sorted(missing)} from const, "
                    f"but const.py does not define them",
                )

    def test_the_specific_v1_3_5_incident_names_stay_gone(self):
        """Pins the exact regression, not just the general class."""
        const_src = (_ROOT / "const.py").read_text()
        for name in ("CONF_COALESCE_SLOW_TIER", "CONF_PREFER_NIGHT_FOR_SLOW",
                     "DEFAULT_COALESCE_SLOW_TIER", "DEFAULT_PREFER_NIGHT_FOR_SLOW"):
            self.assertNotIn(name, const_src)
            for pyfile in sorted(_ROOT.glob("*.py")):
                with self.subTest(file=pyfile.name, name=name):
                    self.assertNotIn(name, pyfile.read_text())
