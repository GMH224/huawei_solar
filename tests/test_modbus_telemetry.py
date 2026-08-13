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
    was blind to any failure pattern that happened to be all timeouts.

    v2.0.5 (F-04): expected values below updated to reflect the new,
    attempts_ph-based denominator (see that fix's own docstring in
    modbus_telemetry.py) -- every rate in this class's own numbers
    changed as a direct, mechanical consequence, not because F-03's own
    original fix was wrong about WHICH failures should be counted.
    """

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
            snap["timeout_rate_percent"], 50.0,  # 24 timeouts / 48 attempts
            "the new field must show the timeout problem the old, "
            "sole metric completely masked -- 50%, not 100%, since the "
            "denominator (v2.0.5, F-04) is now attempts (24 successes + "
            "24 timeouts = 48), not successes alone",
        )
        self.assertEqual(snap["overall_failed_attempt_rate_percent"], 50.0)

    def test_failure_rate_percent_denominator_is_fixed_but_numerator_meaning_is_not(self):
        """Negative case, protecting backward compatibility of MEANING
        (not the exact number, which v2.0.5/F-04 deliberately changed):
        a device with ONLY non-timeout failures must still show
        failure_rate_percent counting non-timeout failures alone, not
        timeouts -- the numerator's own semantics are what F-03
        established and what must still hold, independent of F-04's
        own denominator fix."""
        t = _make()
        for _ in range(10):
            t.record_request()
        for _ in range(3):
            t.record_failure()
        snap = t.snapshot()
        # attempts_ph = 10 successes + 3 non-timeout failures = 13
        self.assertEqual(snap["failure_rate_percent"], round(3 / 13 * 100, 1))
        self.assertEqual(snap["timeout_rate_percent"], 0.0)
        self.assertEqual(
            snap["overall_failed_attempt_rate_percent"],
            snap["failure_rate_percent"],
            "with zero timeouts, the overall rate must equal the "
            "failure rate exactly",
        )

    def test_mixed_timeout_and_non_timeout_failures(self):
        t = _make()
        for _ in range(20):
            t.record_request()
        for _ in range(2):
            t.record_failure()
        for _ in range(3):
            t.record_timeout()
        snap = t.snapshot()
        # attempts_ph = 20 + 2 + 3 = 25
        self.assertEqual(snap["failure_rate_percent"], 8.0)   # 2/25
        self.assertEqual(snap["timeout_rate_percent"], 12.0)  # 3/25
        self.assertEqual(
            snap["overall_failed_attempt_rate_percent"], 20.0,  # (2+3)/25
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


# ── v2.0.5: F-04 -- telemetry rate denominator mismatch (external ICS audit) ─

class TestF04AttemptsBasedDenominator(unittest.TestCase):
    """F-04, external ICS audit -- confirmed with exact numbers matched
    against the real post-v2.0.4 telemetry capture: readings up to
    timeout_rate_percent: 400.0% (e.g. 1 request / 3 timeouts in the
    same rolling window). record_request() only fires after a batch
    SUCCEEDS -- every rate in v2.0.4 divided by that success-only count,
    not a true attempt count, so any window with more failures than
    successes produced a rate exceeding 100%, which is not a meaningful
    percentage for a health metric."""

    def test_more_timeouts_than_successes_no_longer_exceeds_100_percent(self):
        """Reproduces the exact real-capture scenario: 1 successful
        request, 3 device timeouts in the same window. v2.0.4 computed
        3/1 = 300%; v2.0.5 must compute 3/4 = 75%."""
        t = _make()
        t.record_request()
        for _ in range(3):
            t.record_timeout()
        snap = t.snapshot()
        self.assertEqual(snap["attempts_per_hour"], 4)
        self.assertEqual(snap["timeout_rate_percent"], 75.0)
        self.assertLessEqual(
            snap["timeout_rate_percent"], 100.0,
            "no rate this class computes may ever exceed 100% -- that "
            "was the whole point of this fix",
        )

    def test_every_rate_is_bounded_to_100_percent_under_adversarial_load(self):
        """Property-style check: however lopsided the mix of outcomes,
        every rate must stay within [0, 100] -- not just in the one
        specific scenario reproduced above."""
        t = _make()
        for _ in range(1):
            t.record_request()
        for _ in range(50):
            t.record_timeout()
        for _ in range(30):
            t.record_failure()
        snap = t.snapshot()
        for field in (
            "failure_rate_percent", "timeout_rate_percent",
            "overall_failed_attempt_rate_percent",
            "queue_shed_rate_percent", "admission_timeout_rate_percent",
        ):
            self.assertGreaterEqual(snap[field], 0.0, field)
            self.assertLessEqual(snap[field], 100.0, field)

    def test_queue_shed_and_admission_timeout_are_separated_from_device_timeout(self):
        """The second half of F-04's own concern (this project's audit
        section 11): queue sheds and admission timeouts are internal bus
        contention, not device misbehaviour, and must not inflate
        timeout_rate_percent -- a metric specifically about the device."""
        t = _make()
        for _ in range(10):
            t.record_request()
        t.record_timeout(kind="device")
        t.record_timeout(kind="queue_shed")
        t.record_timeout(kind="queue_shed")
        t.record_timeout(kind="admission")
        snap = t.snapshot()
        # attempts_ph = 10 + 1 + 2 + 1 = 14
        self.assertEqual(snap["device_timeouts_per_hour"], 1)
        self.assertEqual(snap["queue_sheds_per_hour"], 2)
        self.assertEqual(snap["admission_timeouts_per_hour"], 1)
        self.assertEqual(
            snap["timeout_rate_percent"], round(1 / 14 * 100, 1),
            "timeout_rate_percent must reflect ONLY the genuine device "
            "timeout, not the queue sheds or admission timeouts mixed in",
        )
        self.assertEqual(snap["queue_shed_rate_percent"], round(2 / 14 * 100, 1))
        self.assertEqual(snap["admission_timeout_rate_percent"], round(1 / 14 * 100, 1))
        # timeouts_per_hour (the pre-existing, established field) keeps
        # its own existing meaning: ALL THREE kinds combined, unchanged.
        self.assertEqual(snap["timeouts_per_hour"], 4)

    def test_invalid_kind_is_rejected(self):
        t = _make()
        with self.assertRaises(ValueError):
            t.record_timeout(kind="not_a_real_kind")

    def test_default_kind_is_device(self):
        """Backward compatibility: existing callers using the bare
        record_timeout() with no argument must still behave exactly as
        a genuine device timeout, matching pre-v2.0.5 behaviour."""
        t = _make()
        t.record_timeout()
        snap = t.snapshot()
        self.assertEqual(snap["device_timeouts_per_hour"], 1)
        self.assertEqual(snap["queue_sheds_per_hour"], 0)
        self.assertEqual(snap["admission_timeouts_per_hour"], 0)

    def test_total_attempts_lifetime_counter(self):
        t = _make()
        t.record_request()
        t.record_failure()
        t.record_timeout(kind="device")
        t.record_timeout(kind="queue_shed")
        snap = t.snapshot()
        self.assertEqual(snap["total_attempts"], 4)
