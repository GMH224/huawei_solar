"""Regression test for the energy-counter coverage gap (v1.1.3).

Bug: roughly half of the sensors declared with state_class=TOTAL_INCREASING in
sensor.py were not recognised by register_cache.is_energy_counter(), silently
disabling the stale-cache exclusion and the suspicious-zero guard for them.

This test re-derives the authoritative set from sensor.py via AST and asserts
that is_energy_counter() recognises every one — so the two can never drift
apart again without a test failure.

Standalone: stubs the external huawei_solar library and HA, loads register_cache
via importlib, parses sensor.py as text (no import needed).  Run directly:
    python tests/test_energy_counter_coverage.py
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys
import types
import unittest

_ROOT = pathlib.Path(__file__).parent.parent

# ── Minimal stubs so register_cache can be imported standalone ────────────────
sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))


class _RegisterName(str):
    """Stringifies to its own value; is_energy_counter does str(name).lower()."""


_hs = sys.modules.get("huawei_solar")
if _hs is None or not hasattr(_hs, "__path__"):
    _hs = types.ModuleType("huawei_solar")
    _hs.__path__ = []
    sys.modules["huawei_solar"] = _hs
_hs.RegisterName = _RegisterName


class _Result:
    def __class_getitem__(cls, item):
        return cls


_hs.Result = _Result

_SRC = _ROOT / "register_cache.py"
_SPEC = importlib.util.spec_from_file_location("register_cache_ec", str(_SRC))
_MOD = importlib.util.module_from_spec(_SPEC)
_MOD.__package__ = "huawei_solar"
_SPEC.loader.exec_module(_MOD)

is_energy_counter = _MOD.is_energy_counter


def _total_increasing_keys_from_sensor_py() -> set[str]:
    """Every `key=rn.X` where state_class is TOTAL_INCREASING, lowercased."""
    tree = ast.parse((_ROOT / "sensor.py").read_text())
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        sc = kw.get("state_class")
        key = kw.get("key")
        if (
            isinstance(sc, ast.Attribute)
            and sc.attr == "TOTAL_INCREASING"
            and isinstance(key, ast.Attribute)
        ):
            keys.add(key.attr.lower())
    return keys


class TestEnergyCounterCoverage(unittest.TestCase):
    def test_every_total_increasing_sensor_is_recognised(self):
        keys = _total_increasing_keys_from_sensor_py()
        self.assertGreater(len(keys), 0, "no TOTAL_INCREASING sensors found")
        missing = sorted(k for k in keys if not is_energy_counter(k))
        self.assertEqual(
            missing,
            [],
            "These TOTAL_INCREASING energy sensors are NOT recognised by "
            "is_energy_counter(), so the stale-cache exclusion and "
            "suspicious-zero guard do not protect them:\n  "
            + "\n  ".join(missing),
        )

    def test_known_previously_missing_names(self):
        # Spot-check the registers that were unprotected before v1.1.3.
        for name in [
            "storage_total_charge",
            "storage_total_discharge",
            "total_dc_input_power",
            "consumption_today",
            "pv_yield_today",
            "grid_exported_energy",
            "inverter_total_absorbed_energy",
            "smartlogger_total_power_supply_from_grid",
        ]:
            self.assertTrue(
                is_energy_counter(name), f"{name} should be an energy counter"
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
