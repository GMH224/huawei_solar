"""Tests for telemetry_capture.py.

Covers `TelemetryCapture` (the periodic aggregate-snapshot capture
switch's back end, mirroring bus_diagnostics.py's own established,
field-tested per-request capture) and `check_register_overlap()` (the
one-time structural check for the Physical Demand Planner's
cross-coordinator-merging justification).
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import time
import types
import unittest

_ROOT = pathlib.Path(__file__).parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"telcap_{name}", str(_ROOT / f"{name}.py")
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "telcap"
    sys.modules[f"telcap_{name}"] = module
    spec.loader.exec_module(module)
    return module


if "telcap" not in sys.modules:
    pkg = types.ModuleType("telcap")
    pkg.__path__ = []
    sys.modules["telcap"] = pkg

# telemetry_capture.py does `from .bus_diagnostics import pseudonym` --
# load bus_diagnostics.py first, under the same "telcap" package
# namespace, so that relative import resolves.
_BD_FOR_TC = _load("bus_diagnostics")
sys.modules["telcap.bus_diagnostics"] = _BD_FOR_TC

TC = _load("telemetry_capture")


class _FakeHass:
    """Minimal hass: runs executor jobs inline so writes are observable."""

    def __init__(self, base: str):
        self.config = types.SimpleNamespace(path=lambda *p: os.path.join(base, *p))
        self.jobs = 0

    def async_add_executor_job(self, fn, *args):
        self.jobs += 1
        fn(*args)


class TestCaptureDisabledByDefault(unittest.TestCase):
    def setUp(self):
        TC.TelemetryCapture.clear_registry()
        self.dir = tempfile.mkdtemp()

    def test_starts_disabled(self):
        d = TC.TelemetryCapture(_FakeHass(self.dir), "192.0.2.1:502")
        self.assertFalse(d.enabled)

    def test_record_snapshot_is_a_no_op_when_disabled(self):
        d = TC.TelemetryCapture(_FakeHass(self.dir), "192.0.2.1:502")
        d.record_snapshot({"x": 1})
        self.assertEqual(d.snapshots_captured, 0)

    def test_get_or_create_returns_the_same_instance_per_endpoint(self):
        hass = _FakeHass(self.dir)
        d = TC.TelemetryCapture.get_or_create(hass, "192.0.2.1:502")
        fresh = TC.TelemetryCapture.get_or_create(hass, "192.0.2.1:502")
        self.assertIs(d, fresh)


class TestSnapshotCaptureAndFlush(unittest.TestCase):
    def setUp(self):
        TC.TelemetryCapture.clear_registry()
        self.dir = tempfile.mkdtemp()

    def test_buffer_is_bounded_and_drops_are_counted(self):
        hass = _FakeHass(self.dir)
        d = TC.TelemetryCapture(hass, "e")
        d.enabled = True
        d._last_flush = time.monotonic() + 1e6  # suppress flushing
        for i in range(TC.MAX_BUFFERED_SNAPSHOTS + 20):
            d.record_snapshot({"i": i})
        self.assertLessEqual(len(d._buffer), TC.MAX_BUFFERED_SNAPSHOTS)
        self.assertGreater(d.snapshots_dropped, 0)

    def test_flush_writes_jsonl_to_the_correct_distinct_filename(self):
        """Same directory, different filename from bus_diagnostics.py's
        own bus_<tag>.jsonl -- checked directly, not assumed."""
        hass = _FakeHass(self.dir)
        d = TC.TelemetryCapture(hass, "e")
        d.set_enabled(True)
        for _ in range(TC.FLUSH_THRESHOLD):
            d.record_snapshot({"bus_occupancy_pct": 12.3, "total_cache_hits": 40})
        path = os.path.join(self.dir, TC._SUBDIR, f"telemetry_{d.tag}.jsonl")
        self.assertTrue(os.path.exists(path))
        lines = [json.loads(x) for x in open(path).read().splitlines()]
        self.assertEqual(len(lines), TC.FLUSH_THRESHOLD)
        self.assertEqual(lines[0]["bus_occupancy_pct"], 12.3)
        self.assertEqual(lines[0]["total_cache_hits"], 40)
        self.assertIn("t", lines[0])
        self.assertIn("bus", lines[0])

    def test_telemetry_and_diagnostics_share_the_same_tag_for_one_endpoint(self):
        """The two files for one physical bus must be identifiable as
        belonging together -- same salted pseudonym, not two independent
        ones."""
        endpoint = "192.0.2.1:502"
        telemetry = TC.TelemetryCapture(_FakeHass(self.dir), endpoint)
        diagnostics = _BD_FOR_TC.BusDiagnostics(_FakeHass(self.dir), endpoint)
        self.assertEqual(telemetry.tag, diagnostics.tag)

    def test_write_failure_does_not_raise(self):
        class _Boom(_FakeHass):
            def async_add_executor_job(self, fn, *args):
                raise OSError("disk gone")
        d = TC.TelemetryCapture(_Boom(self.dir), "e")
        d.set_enabled(True)
        for _ in range(TC.FLUSH_THRESHOLD):
            d.record_snapshot({"x": 1})  # must not raise
        self.assertGreater(d.write_errors, 0)

    def test_disable_flushes_pending_snapshots(self):
        hass = _FakeHass(self.dir)
        d = TC.TelemetryCapture(hass, "e")
        d.set_enabled(True)
        d.record_snapshot({"x": 1})  # below FLUSH_THRESHOLD, stays buffered
        self.assertEqual(hass.jobs, 0)
        d.set_enabled(False)
        self.assertEqual(hass.jobs, 1, "disabling must flush whatever is pending")

    def test_disable_cancels_the_periodic_timer_if_one_is_set(self):
        """The periodic timer itself is owned by the caller (switch.py),
        not this module -- but disabling capture must still cancel it via
        the stored callback, so a disabled switch genuinely stops
        producing snapshots, not just stops writing already-produced
        ones."""
        d = TC.TelemetryCapture(_FakeHass(self.dir), "e")
        cancelled = []
        d.set_enabled(True)
        d.cancel_periodic = lambda: cancelled.append(True)
        d.set_enabled(False)
        self.assertEqual(cancelled, [True])
        self.assertIsNone(d.cancel_periodic)


class TestRegisterOverlapCheck(unittest.TestCase):
    """check_register_overlap() -- the structural check for the Physical
    Demand Planner's cross-coordinator-merging justification."""

    def _coord(self, data):
        return types.SimpleNamespace(data=data)

    def test_no_overlap_when_register_sets_are_disjoint(self):
        result = TC.check_register_overlap({
            "main": self._coord({"input_power": 1, "device_status": 2}),
            "power_meter": self._coord({"power_meter_active_power": 3}),
            "energy_storage": self._coord({"storage_state_of_capacity": 4}),
        })
        self.assertFalse(result["any_overlap_found"])
        self.assertEqual(result["overlaps"], {})
        self.assertEqual(sorted(result["checked"]), ["energy_storage", "main", "power_meter"])

    def test_detects_a_genuine_overlap_when_present(self):
        result = TC.check_register_overlap({
            "main": self._coord({"input_power": 1, "shared_register": 2}),
            "power_meter": self._coord({"shared_register": 2}),
        })
        self.assertTrue(result["any_overlap_found"])
        self.assertIn("main_vs_power_meter", result["overlaps"])
        self.assertEqual(result["overlaps"]["main_vs_power_meter"], ["shared_register"])

    def test_coordinator_with_no_data_yet_is_skipped_not_falsely_clean(self):
        """A coordinator that hasn't completed its first poll yet must be
        excluded from the comparison, not silently treated as
        'confirmed disjoint' -- that would be a false negative, not
        evidence."""
        result = TC.check_register_overlap({
            "main": self._coord({"input_power": 1}),
            "power_meter": self._coord({}),  # never polled yet
            "energy_storage": self._coord(None),  # data attribute is None
        })
        self.assertIn("power_meter", result["skipped_not_yet_polled"])
        self.assertIn("energy_storage", result["skipped_not_yet_polled"])
        self.assertEqual(result["checked"], ["main"])
        self.assertEqual(result["overlaps"], {})

    def test_register_counts_are_reported_per_coordinator(self):
        result = TC.check_register_overlap({
            "main": self._coord({"a": 1, "b": 2, "c": 3}),
        })
        self.assertEqual(result["register_counts"]["main"], 3)

    def test_empty_input_does_not_raise(self):
        result = TC.check_register_overlap({})
        self.assertEqual(result["checked"], [])
        self.assertFalse(result["any_overlap_found"])


if __name__ == "__main__":
    unittest.main()
