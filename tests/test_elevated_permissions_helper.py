"""Tests for const.py's elevated_permissions_enabled() helper.

v2.0.15 FIX (external ICS review, this release): CONF_ENABLE_PARAMETER_
CONFIGURATION moved from being read directly off entry.data at 8 separate
call sites, to a single shared helper checking entry.options first (the
new "Configure" screen location, alongside CONF_BH_ENABLED and CONF_SYNC_
POWER_DEDICATED_READS) with a fallback to entry.data (the original
initial-setup location, preserved for every pre-2.0.15 installation).

Real execution against the actual const.py -- it has no Home Assistant or
third-party dependency (see test_const_services.py's own comment making
the same claim, and this file's own confirmation below), so this uses
real execution rather than AST/source-level checks, matching this
project's own preference for real execution wherever the module under
test genuinely permits it.
"""
from __future__ import annotations

import importlib.util
import pathlib
import unittest

_CONST_PATH = pathlib.Path(__file__).parent.parent / "const.py"


def _load_const():
    spec = importlib.util.spec_from_file_location("huawei_solar_const_helper_test", _CONST_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeEntry:
    """Minimal stand-in for a ConfigEntry -- only .data and .options
    matter to the function under test, and using a real ConfigEntry
    would pull in the Home Assistant dependency this helper (and this
    test file) is deliberately free of."""

    def __init__(self, data: dict | None = None, options: dict | None = None):
        self.data = data or {}
        self.options = options or {}


class TestConstHasNoHomeAssistantDependency(unittest.TestCase):
    """Pins the property elevated_permissions_enabled()'s own docstring
    claims and depends on: const.py must remain importable with zero
    Home Assistant or third-party dependency. A future edit that
    accidentally imports something HA-specific into const.py would be
    caught here, not discovered later via some other module's own
    unrelated import failure."""

    def test_const_module_loads_standalone(self):
        try:
            _load_const()
        except ImportError as exc:  # noqa: BLE001
            self.fail(f"const.py could not be imported standalone: {exc!r}")


class TestElevatedPermissionsEnabled(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.const = _load_const()

    def test_neither_location_set_defaults_false(self):
        entry = _FakeEntry()
        self.assertFalse(self.const.elevated_permissions_enabled(entry))

    def test_true_in_data_only_pre_2_0_15_install(self):
        """The exact shape of every installation that existed before
        this release -- the value was written once, during initial
        setup, into entry.data, and this release must not silently
        change behavior for any of them."""
        entry = _FakeEntry(data={"enable_parameter_configuration": True})
        self.assertTrue(self.const.elevated_permissions_enabled(entry))

    def test_false_in_data_only(self):
        entry = _FakeEntry(data={"enable_parameter_configuration": False})
        self.assertFalse(self.const.elevated_permissions_enabled(entry))

    def test_true_in_options_only_new_configure_screen_path(self):
        entry = _FakeEntry(options={"enable_parameter_configuration": True})
        self.assertTrue(self.const.elevated_permissions_enabled(entry))

    def test_false_in_options_only(self):
        entry = _FakeEntry(options={"enable_parameter_configuration": False})
        self.assertFalse(self.const.elevated_permissions_enabled(entry))

    def test_options_true_overrides_data_false(self):
        entry = _FakeEntry(
            data={"enable_parameter_configuration": False},
            options={"enable_parameter_configuration": True},
        )
        self.assertTrue(self.const.elevated_permissions_enabled(entry))

    def test_options_false_overrides_data_true(self):
        """The adversarial case: a user who had this enabled at initial
        setup (entry.data=True) deliberately disables it later via the
        Configure screen (entry.options=False). The explicit, more
        recent choice must win -- a user turning this OFF must actually
        turn it off, not have a stale True from setup silently keep
        write access enabled."""
        entry = _FakeEntry(
            data={"enable_parameter_configuration": True},
            options={"enable_parameter_configuration": False},
        )
        self.assertFalse(self.const.elevated_permissions_enabled(entry))

    def test_missing_data_key_falls_through_to_default_false(self):
        """entry.data present but the key itself absent (not just
        falsy) -- must not raise, must resolve to False."""
        entry = _FakeEntry(data={"some_other_key": "value"})
        self.assertFalse(self.const.elevated_permissions_enabled(entry))

    def test_return_type_is_always_bool(self):
        """Adversarial: a truthy-but-non-bool stored value (e.g. from a
        manually edited storage file) must still coerce to an actual
        bool, not be returned as-is."""
        entry = _FakeEntry(options={"enable_parameter_configuration": 1})
        result = self.const.elevated_permissions_enabled(entry)
        self.assertIs(result, True)
        self.assertIsInstance(result, bool)

    def test_does_not_mutate_the_entry(self):
        """Adversarial: a read helper must not have side effects on the
        object it reads from -- e.g. must not call .setdefault() or
        otherwise write into entry.data/entry.options as a side effect
        of checking them."""
        entry = _FakeEntry(data={"enable_parameter_configuration": True})
        data_before = dict(entry.data)
        options_before = dict(entry.options)
        self.const.elevated_permissions_enabled(entry)
        self.assertEqual(entry.data, data_before)
        self.assertEqual(entry.options, options_before)


if __name__ == "__main__":
    unittest.main()
