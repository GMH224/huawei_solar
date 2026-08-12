"""Tests for modbus_telemetry.py — stdlib unittest, no pytest required.

Covers:
  • record_failure/record_timeout call _evict (deques stay bounded)
  • Lifetime totals never decrease
  • snapshot() returns accurate windowed counts
  • record_cache_hits(N) batch vs singular
  • record_skipped_poll
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import time
import types
import unittest
from unittest.mock import MagicMock, patch

# ── HA stubs ──────────────────────────────────────────────────────────────────
for _m in [
    "homeassistant", "homeassistant.components",
    "homeassistant.components.sensor", "homeassistant.const",
    "homeassistant.core", "homeassistant.helpers",
    "homeassistant.helpers.device_registry",
    "homeassistant.helpers.event",
    "homeassistant.helpers.entity",
    "homeassistant.helpers.entity_platform",
]:
    sys.modules.setdefault(_m, types.ModuleType(_m))

_s = sys.modules["homeassistant.components.sensor"]
for _a in ["SensorDeviceClass", "SensorEntity", "SensorStateClass"]:
    setattr(_s, _a, MagicMock())
_c = sys.modules["homeassistant.const"]
_c.EntityCategory = MagicMock()
_c.UnitOfTime = MagicMock()
_core = sys.modules["homeassistant.core"]
_core.HomeAssistant = MagicMock
_core.callback = lambda f: f
_ev = sys.modules["homeassistant.helpers.event"]
_ev.async_track_time_interval = MagicMock(return_value=MagicMock())
sys.modules["homeassistant.helpers.device_registry"].DeviceInfo = dict

# const stub
_cstub = types.ModuleType("huawei_solar.const")
_cstub.DOMAIN = "huawei_solar"  # type: ignore[attr-defined]
sys.modules["huawei_solar.const"] = _cstub

_SRC = pathlib.Path(__file__).parent.parent / "modbus_telemetry.py"
_SPEC = importlib.util.spec_from_file_location("modbus_telemetry_test", str(_SRC))
_MOD = importlib.util.module_from_spec(_SPEC)
_MOD.__package__ = "huawei_solar"
_SPEC.loader.exec_module(_MOD)

ModbusTelemetry = _MOD.ModbusTelemetry
_WINDOW_SEC = _MOD._WINDOW_SEC


def _make() -> ModbusTelemetry:
    ModbusTelemetry._registry.clear()
    return ModbusTelemetry(MagicMock(), "SN-TEST", MagicMock())


# ── Deque eviction ────────────────────────────────────────────────────────────

class TestEviction(unittest.TestCase):

    def test_failures_evicted_on_record_failure(self):
        t = _make()
        t._failures.append(time.monotonic() - _WINDOW_SEC - 10)
        t.record_failure()
        self.assertEqual(len(t._failures), 1,
            "record_failure() must call _evict(); old entry not removed")

    def test_timeouts_evicted_on_record_timeout(self):
        t = _make()
        t._timeouts.append(time.monotonic() - _WINDOW_SEC - 10)
        t.record_timeout()
        self.assertEqual(len(t._timeouts), 1,
            "record_timeout() must call _evict(); old entry not removed")

    def test_deques_bounded_under_continuous_failures(self):
        t = _make()
        base = time.monotonic() - 3 * _WINDOW_SEC
        for i in range(200):
            t._failures.append(base + i)
            t._timeouts.append(base + i)
        t.record_failure()
        t.record_timeout()
        now = time.monotonic()
        cutoff = now - _WINDOW_SEC
        self.assertFalse([ts for ts in t._failures if ts < cutoff],
            "Stale failure entries remain after eviction")
        self.assertFalse([ts for ts in t._timeouts if ts < cutoff],
            "Stale timeout entries remain after eviction")

    def test_failure_rate_accurate_after_eviction(self):
        t = _make()
        old = time.monotonic() - _WINDOW_SEC - 10
        for _ in range(50):
            t._failures.append(old)
            t._requests.append(old)
        t.record_timeout()
        snap = t.snapshot()
        self.assertEqual(snap["requests_per_hour"], 0)
        self.assertEqual(snap["timeouts_per_hour"], 1)


# ── Lifetime totals ───────────────────────────────────────────────────────────

class TestLifetimeTotals(unittest.TestCase):

    def test_totals_monotonically_increasing(self):
        t = _make()
        t.record_request(batch_size=5)
        t.record_failure()
        t.record_timeout()
        self.assertEqual(t.total_requests, 1)
        self.assertEqual(t.total_timeouts, 1)
        self.assertGreaterEqual(t.total_failures, 2)

    def test_cache_hits_accumulated(self):
        t = _make()
        t.record_cache_hit()
        t.record_cache_hit()
        self.assertEqual(t.total_cache_hits, 2)

    def test_batch_cache_hits(self):
        t = _make()
        t.record_cache_hits(10)
        self.assertEqual(t.total_cache_hits, 10)
        snap = t.snapshot()
        self.assertEqual(snap["cache_hits_per_hour"], 10)

    def test_skipped_polls_tracked(self):
        t = _make()
        t.record_skipped_poll()
        t.record_skipped_poll()
        self.assertEqual(t.total_skipped_polls, 2)


# ── v2.0.3: F-03 -- failure-rate metric blind to timeout-only failures ──────

class TestF03TimeoutInclusiveRates(unittest.TestCase):
    """F-03, external ICS audit -- confirmed with exact numbers matched
    against a real telemetry capture: a device with 24 timeouts and zero
    non-timeout failures reported failure_rate_percent: 0.0, completely
    masking a real, ongoing timeout problem. record_timeout() always
    increments total_failures (the lifetime counter) but only ever
    appends to self._timeouts, never self._failures -- the rolling,
    windowed failure_rate_percent (computed from self._failures alone)
    was blind to any failure pattern that happened to be all timeouts."""

    def test_timeout_only_failures_reproduce_the_real_capture_scenario(self):
        """Reproduces the exact real-world numbers from the telemetry
        capture that surfaced this finding: 24 requests, 24 timeouts,
        zero non-timeout failures."""
        t = _make()
        for _ in range(24):
            t.record_request()
            t.record_timeout()
        snap = t.snapshot()
        self.assertEqual(snap["total_failures"], 24)
        self.assertEqual(snap["total_timeouts"], 24)
        self.assertEqual(
            snap["failure_rate_percent"], 0.0,
            "failure_rate_percent's EXISTING meaning (non-timeout "
            "failures only) is deliberately preserved, not silently "
            "redefined -- see this fix's own reasoning for why",
        )
        self.assertEqual(
            snap["timeout_rate_percent"], 100.0,
            "the new field must show the timeout problem the old, "
            "sole metric completely masked",
        )
        self.assertEqual(snap["overall_failed_attempt_rate_percent"], 100.0)

    def test_failure_rate_percent_keeps_its_existing_meaning(self):
        """Negative case, protecting backward compatibility: a device
        with ONLY non-timeout failures (no timeouts at all) must show
        the identical failure_rate_percent behaviour as before this fix
        -- confirms the existing field's semantics were not changed."""
        t = _make()
        for _ in range(10):
            t.record_request()
        for _ in range(3):
            t.record_failure()
        snap = t.snapshot()
        self.assertEqual(snap["failure_rate_percent"], 30.0)
        self.assertEqual(snap["timeout_rate_percent"], 0.0)
        self.assertEqual(snap["overall_failed_attempt_rate_percent"], 30.0)

    def test_mixed_timeout_and_non_timeout_failures(self):
        t = _make()
        for _ in range(20):
            t.record_request()
        for _ in range(2):
            t.record_failure()
        for _ in range(3):
            t.record_timeout()
        snap = t.snapshot()
        self.assertEqual(snap["failure_rate_percent"], 10.0)   # 2/20
        self.assertEqual(snap["timeout_rate_percent"], 15.0)   # 3/20
        self.assertEqual(
            snap["overall_failed_attempt_rate_percent"], 25.0,  # (2+3)/20
            "the combined rate must reflect BOTH failure types together "
            "-- this is the number that would have caught the masking "
            "the original single metric could not",
        )

    def test_zero_requests_does_not_divide_by_zero(self):
        t = _make()
        snap = t.snapshot()
        self.assertEqual(snap["failure_rate_percent"], 0.0)
        self.assertEqual(snap["timeout_rate_percent"], 0.0)
        self.assertEqual(snap["overall_failed_attempt_rate_percent"], 0.0)

    def test_total_failures_lifetime_counter_is_unaffected(self):
        """This fix only changes the windowed-rate calculations -- the
        lifetime total_failures counter (which already correctly
        included timeouts) must be completely unchanged."""
        t = _make()
        t.record_timeout()
        t.record_failure()
        snap = t.snapshot()
        self.assertEqual(snap["total_failures"], 2)
