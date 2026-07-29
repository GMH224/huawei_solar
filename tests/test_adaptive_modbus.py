"""Tests for adaptive_modbus.py — stdlib unittest, no pytest required.

Covers:
  • BUG-5: async_load error path resets _last_decay_date and _first_data_date
  • BUG-6: async_load cancels existing _unsub_push before creating a new one
  • BUG-7: days_of_data clamped to >= 0 on clock skew
  • TimeSlotStats: record, decay, P95 RTT, serialise round-trip
  • _derive_params: bounds, cold-start = 60 s (ADAPTIVE_POLL_COLD_START not POLL_MIN)
  • notify_transition: sets in_transition, forces queue_depth=1
  • Persistence: serialize / deserialize / startup decay
  • All parameter bounds: poll 20→180s, timeout 15→60s, gap ≥150ms
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import time
import types
import unittest
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

# ── HA + huawei_solar stubs ───────────────────────────────────────────────────
for _m in [
    "homeassistant", "homeassistant.components",
    "homeassistant.components.sensor", "homeassistant.const",
    "homeassistant.core", "homeassistant.helpers",
    "homeassistant.helpers.device_registry",
    "homeassistant.helpers.event", "homeassistant.helpers.storage",
    "homeassistant.helpers.entity", "homeassistant.helpers.entity_platform",
]:
    sys.modules.setdefault(_m, types.ModuleType(_m))

_s = sys.modules["homeassistant.components.sensor"]
for _a in ["SensorDeviceClass", "SensorEntity", "SensorStateClass"]:
    setattr(_s, _a, MagicMock())
sys.modules["homeassistant.const"].EntityCategory = MagicMock()
_core = sys.modules["homeassistant.core"]
_core.HomeAssistant = MagicMock; _core.callback = lambda f: f
_ev = sys.modules["homeassistant.helpers.event"]
_ev.async_track_time_interval = MagicMock(return_value=MagicMock())
sys.modules["homeassistant.helpers.storage"].Store = MagicMock
sys.modules["homeassistant.helpers.device_registry"].DeviceInfo = dict

# Load const.py first
_cpath = pathlib.Path(__file__).parent.parent / "const.py"
_cspec = importlib.util.spec_from_file_location("huawei_solar.const", str(_cpath))
_cmod = importlib.util.module_from_spec(_cspec)
_cmod.__package__ = "huawei_solar"
_cspec.loader.exec_module(_cmod)
sys.modules["huawei_solar.const"] = _cmod

# Load adaptive_modbus.py
_SRC = pathlib.Path(__file__).parent.parent / "adaptive_modbus.py"
_SPEC = importlib.util.spec_from_file_location("adaptive_modbus_test", str(_SRC))
_MOD = importlib.util.module_from_spec(_SPEC)
_MOD.__package__ = "huawei_solar"
# Must register BEFORE exec_module so @dataclass can find cls.__module__
sys.modules["adaptive_modbus_test"] = _MOD
_SPEC.loader.exec_module(_MOD)

AdaptiveModbusController = _MOD.AdaptiveModbusController
TimeSlotStats = _MOD.TimeSlotStats

ADAPTIVE_POLL_MIN      = _cmod.ADAPTIVE_POLL_MIN
ADAPTIVE_POLL_MAX      = _cmod.ADAPTIVE_POLL_MAX
ADAPTIVE_POLL_COLD_START = _cmod.ADAPTIVE_POLL_COLD_START
ADAPTIVE_TIMEOUT_MIN   = _cmod.ADAPTIVE_TIMEOUT_MIN
ADAPTIVE_TIMEOUT_MAX   = _cmod.ADAPTIVE_TIMEOUT_MAX
ADAPTIVE_GAP_MIN       = _cmod.ADAPTIVE_GAP_MIN
ADAPTIVE_GAP_MAX       = _cmod.ADAPTIVE_GAP_MAX
ADAPTIVE_DECAY_FACTOR  = _cmod.ADAPTIVE_DECAY_FACTOR
ADAPTIVE_QUEUE_DEPTH_COLD_START = _cmod.ADAPTIVE_QUEUE_DEPTH_COLD_START
ADAPTIVE_SLOT_COUNT    = _cmod.ADAPTIVE_SLOT_COUNT

_LOOP = asyncio.new_event_loop()
def _run(c): return _LOOP.run_until_complete(c)


def _make_ctrl() -> AdaptiveModbusController:
    AdaptiveModbusController.clear_registry()
    hass = MagicMock()
    hass.async_create_task = MagicMock(return_value=MagicMock())
    ctrl = object.__new__(AdaptiveModbusController)
    ctrl.hass = hass
    ctrl.serial_number = "SN-TEST"
    ctrl.device_info = {}
    ctrl._slots = [TimeSlotStats(slot_index=i) for i in range(ADAPTIVE_SLOT_COUNT)]
    ctrl._in_transition = False
    ctrl._transition_expires = 0.0
    ctrl._store = MagicMock()
    ctrl._store.async_load = AsyncMock(return_value=None)
    ctrl._store.async_save = AsyncMock()
    ctrl._last_decay_date = None
    ctrl._first_data_date = None
    ctrl._dirty = False
    ctrl._save_task = None
    ctrl._listeners = []
    ctrl._unsub_push = None
    # v1.2.2 learning gate — open by default so existing tests exercise the
    # learning path unchanged.
    ctrl.learning_enabled = True
    ctrl._suppressed_until = None
    ctrl._suppress_reason = ""
    ctrl.suppressed_observations = 0
    ctrl.settling_events = 0
    # v1.2.3 instrumentation (diagnostics only)
    ctrl.last_batch_ms = 0.0
    ctrl.last_chunk_count = 0
    ctrl.shed_count = 0
    # v1.3.0 Phase 0 bus metrics
    ctrl._bus_occupancy_pct = 0.0
    ctrl._bus_wait_p95 = 0.0
    ctrl._bus_service_p95 = 0.0
    ctrl._bus_requests_waited = 0
    ctrl._bus_total_wait_s = 0.0
    ctrl._coalesce_events = 0
    ctrl._coalesced_registers = 0
    return ctrl


# ── Parameter bounds ──────────────────────────────────────────────────────────

class TestParameterBounds(unittest.TestCase):

    def test_poll_min_20s(self):
        self.assertEqual(ADAPTIVE_POLL_MIN, timedelta(seconds=20))

    def test_poll_max_180s(self):
        self.assertEqual(ADAPTIVE_POLL_MAX, timedelta(seconds=180))

    def test_cold_start_60s(self):
        self.assertEqual(ADAPTIVE_POLL_COLD_START, timedelta(seconds=60))

    def test_cold_start_differs_from_poll_min(self):
        """Cold-start must be independent of ADAPTIVE_POLL_MIN."""
        self.assertNotEqual(ADAPTIVE_POLL_COLD_START, ADAPTIVE_POLL_MIN)

    def test_timeout_min_15s(self):
        self.assertEqual(ADAPTIVE_TIMEOUT_MIN, timedelta(seconds=15))

    def test_timeout_max_60s(self):
        self.assertEqual(ADAPTIVE_TIMEOUT_MAX, timedelta(seconds=60))

    def test_gap_min_150ms(self):
        """150 ms is the hardware FSM floor — must not be reduced."""
        self.assertEqual(ADAPTIVE_GAP_MIN, timedelta(milliseconds=150))

    def test_gap_max_500ms(self):
        self.assertEqual(ADAPTIVE_GAP_MAX, timedelta(milliseconds=500))


# ── TimeSlotStats ─────────────────────────────────────────────────────────────

class TestTimeSlotStats(unittest.TestCase):

    def test_initial_state(self):
        s = TimeSlotStats()
        self.assertEqual(s.n, 0.0)
        self.assertEqual(s.failure_rate, 0.0)
        self.assertEqual(s.confidence, 0.0)

    def test_record_success(self):
        s = TimeSlotStats()
        s.record(100.0, success=True, timeout=False, max_samples=50)
        self.assertEqual(s.n, 1.0)
        self.assertEqual(s.failures, 0.0)
        self.assertGreater(s.rtt_p95_ms, 0)

    def test_record_failure(self):
        s = TimeSlotStats()
        s.record(0.0, success=False, timeout=False, max_samples=50)
        self.assertEqual(s.failures, 1.0)
        self.assertEqual(s.timeouts, 0.0)

    def test_record_timeout(self):
        s = TimeSlotStats()
        s.record(0.0, success=False, timeout=True, max_samples=50)
        self.assertEqual(s.failures, 1.0)
        self.assertEqual(s.timeouts, 1.0)

    def test_rtt_not_stored_for_failures(self):
        s = TimeSlotStats()
        s.record(500.0, success=False, timeout=False, max_samples=50)
        self.assertEqual(s.rtt_samples, [])

    def test_rtt_bounded_to_max_samples(self):
        s = TimeSlotStats()
        for i in range(60):
            s.record(float(i + 1), success=True, timeout=False, max_samples=50)
        self.assertLessEqual(len(s.rtt_samples), 50)

    def test_failure_rate(self):
        s = TimeSlotStats()
        for _ in range(8):
            s.record(100.0, success=True, timeout=False, max_samples=50)
        for _ in range(2):
            s.record(0.0, success=False, timeout=False, max_samples=50)
        self.assertAlmostEqual(s.failure_rate, 0.2, places=5)

    def test_apply_decay(self):
        s = TimeSlotStats(); s.n = 100.0; s.failures = 10.0; s.timeouts = 5.0
        s.apply_decay(0.85)
        self.assertAlmostEqual(s.n, 85.0, places=4)
        self.assertAlmostEqual(s.failures, 8.5, places=4)

    def test_rtt_p95_not_decayed(self):
        s = TimeSlotStats(); s.rtt_p95_ms = 200.0
        s.apply_decay(0.5)
        self.assertEqual(s.rtt_p95_ms, 200.0)

    def test_serialise_round_trip(self):
        s = TimeSlotStats()
        s.record(150.0, success=True, timeout=False, max_samples=50)
        s.record(0.0, success=False, timeout=True, max_samples=50)
        s2 = TimeSlotStats.from_dict(s.to_dict())
        self.assertAlmostEqual(s2.n, s.n, places=3)
        self.assertAlmostEqual(s2.failures, s.failures, places=3)
        self.assertAlmostEqual(s2.rtt_p95_ms, s.rtt_p95_ms, places=1)


# ── _derive_params ────────────────────────────────────────────────────────────

class TestDeriveParams(unittest.TestCase):

    def _make_slot(self, n=300.0, fr=0.0, rtt_p95=200.0):
        s = TimeSlotStats(); s.n = n; s.failures = fr * n; s.rtt_p95_ms = rtt_p95
        return s

    def test_cold_start_uses_60s_baseline(self):
        """BUG fix: cold-start (n=0) must use ADAPTIVE_POLL_COLD_START=60s, not POLL_MIN=20s."""
        ctrl = _make_ctrl()
        p = ctrl._derive_params(self._make_slot(n=0.0, fr=0.0), 0, False)
        self.assertAlmostEqual(p.poll_interval.total_seconds(), 60.0, delta=2.0,
            msg=f"Cold-start poll={p.poll_interval.total_seconds():.1f}s; "
                "expected ~60s from ADAPTIVE_POLL_COLD_START, not 20s from ADAPTIVE_POLL_MIN")

    def test_poll_bounded_by_min_max(self):
        ctrl = _make_ctrl()
        for n, fr in [(0, 0.0), (150, 0.0), (300, 0.0), (300, 0.20)]:
            p = ctrl._derive_params(self._make_slot(n=float(n), fr=fr), 0, False)
            self.assertGreaterEqual(p.poll_interval.total_seconds(),
                ADAPTIVE_POLL_MIN.total_seconds())
            self.assertLessEqual(p.poll_interval.total_seconds(),
                ADAPTIVE_POLL_MAX.total_seconds())

    def test_high_failure_increases_poll(self):
        ctrl = _make_ctrl()
        p0 = ctrl._derive_params(self._make_slot(n=300, fr=0.0), 0, False)
        p1 = ctrl._derive_params(self._make_slot(n=300, fr=0.20), 0, False)
        self.assertGreaterEqual(p1.poll_interval, p0.poll_interval)

    def test_timeout_bounded_by_min_max(self):
        ctrl = _make_ctrl()
        for rtt in [0, 50, 200, 500, 5000]:
            p = ctrl._derive_params(self._make_slot(n=300, rtt_p95=float(rtt)), 0, False)
            self.assertGreaterEqual(p.request_timeout.total_seconds(),
                ADAPTIVE_TIMEOUT_MIN.total_seconds())
            self.assertLessEqual(p.request_timeout.total_seconds(),
                ADAPTIVE_TIMEOUT_MAX.total_seconds())

    def test_gap_never_below_150ms(self):
        """Hardware floor must never be crossed."""
        ctrl = _make_ctrl()
        for n in [0, 300]:
            p = ctrl._derive_params(self._make_slot(n=float(n), rtt_p95=10.0), 0, False)
            self.assertGreaterEqual(p.request_gap.total_seconds(),
                ADAPTIVE_GAP_MIN.total_seconds(),
                f"Gap {p.request_gap.total_seconds()*1000:.0f}ms below 150ms hardware floor")

    def test_transition_forces_queue_depth_1(self):
        ctrl = _make_ctrl()
        p = ctrl._derive_params(self._make_slot(n=300, fr=0.0), 0, in_transition=True)
        self.assertEqual(p.max_queue_depth, 1)
        self.assertTrue(p.in_transition)

    def test_queue_depth_decreases_at_high_failure(self):
        ctrl = _make_ctrl()
        p_low  = ctrl._derive_params(self._make_slot(n=300, fr=0.00), 0, False)
        p_high = ctrl._derive_params(self._make_slot(n=300, fr=0.20), 0, False)
        self.assertLessEqual(p_high.max_queue_depth, p_low.max_queue_depth)


# ── days_of_data — BUG-7 ─────────────────────────────────────────────────────

class TestDaysOfData(unittest.TestCase):

    def test_none_returns_zero(self):
        ctrl = _make_ctrl()
        self.assertEqual(ctrl.days_of_data, 0)

    def test_same_day_returns_one(self):
        ctrl = _make_ctrl(); ctrl._first_data_date = date.today()
        self.assertEqual(ctrl.days_of_data, 1)

    def test_never_negative_on_clock_skew(self):
        """BUG-7 FIX: future first_data_date must not return negative."""
        ctrl = _make_ctrl()
        ctrl._first_data_date = date.today() + timedelta(days=5)
        self.assertGreaterEqual(ctrl.days_of_data, 0,
            "BUG-7 regression: days_of_data returned negative on clock skew")

    def test_multi_day(self):
        ctrl = _make_ctrl()
        ctrl._first_data_date = date.today() - timedelta(days=6)
        self.assertEqual(ctrl.days_of_data, 7)


# ── async_load error recovery — BUG-5 ────────────────────────────────────────

class TestAsyncLoadErrorRecovery(unittest.TestCase):

    def test_corrupt_data_resets_all_state(self):
        """BUG-5 FIX: error path resets date fields so decay starts fresh.

        After async_load() with corrupt data:
          - _reset_slots() zeros all slots
          - _last_decay_date is reset to None, then _apply_startup_decay()
            immediately sets it to today() (correct fresh-start behaviour)
          - _first_data_date remains None (no valid data was recorded)

        The test verifies that a stale date (e.g. yesterday, 10 days ago) is
        NOT carried over from a partial deserialize into the fresh slot set,
        which would cause incorrect decay on the next startup.
        """
        ctrl = _make_ctrl()
        # Simulate stale dates from a previous session with lots of history
        stale_date = date.today() - __import__("datetime").timedelta(days=10)
        ctrl._last_decay_date = stale_date
        ctrl._first_data_date = stale_date
        ctrl._store.async_load = AsyncMock(return_value={"slots": "CORRUPT"})
        _run(ctrl.async_load())
        # _last_decay_date must NOT be the stale date; it should be today
        # (set by _apply_startup_decay after the reset)
        self.assertNotEqual(ctrl._last_decay_date, stale_date,
            "BUG-5: stale _last_decay_date must be cleared on load error; "
            "carrying it over would cause wrong decay on next startup")
        # _first_data_date must be None (no successful data was loaded)
        self.assertIsNone(ctrl._first_data_date,
            "BUG-5: _first_data_date must be None when load failed")
        self.assertTrue(all(s.n == 0.0 for s in ctrl._slots),
            "All slots must be zeroed after corrupt load")

    def test_none_data_is_fine(self):
        ctrl = _make_ctrl()
        ctrl._store.async_load = AsyncMock(return_value=None)
        _run(ctrl.async_load())  # must not raise


# ── async_load double-call — BUG-6 ───────────────────────────────────────────

class TestAsyncLoadDoubleSub(unittest.TestCase):

    def test_second_call_cancels_first_subscription(self):
        """BUG-6 FIX: calling async_load() twice must cancel the first subscription.

        adaptive_modbus.py imports async_track_time_interval directly so we
        must patch it on _MOD (the loaded module object), not on the HA event
        module, otherwise the already-bound name is unaffected.
        """
        ctrl = _make_ctrl()
        ctrl._store.async_load = AsyncMock(return_value=None)
        unsub1 = MagicMock()
        unsub2 = MagicMock()
        calls = [0]

        def mock_track(*a, **kw):
            calls[0] += 1
            return unsub1 if calls[0] == 1 else unsub2

        # Patch on _MOD so the already-imported name is intercepted
        with patch.object(_MOD, "async_track_time_interval", side_effect=mock_track):
            _run(ctrl.async_load())
            _run(ctrl.async_load())  # second call must cancel unsub1

        self.assertTrue(unsub1.called,
            "BUG-6: first subscription (unsub1) must be called to cancel it "
            "when async_load() is invoked a second time")


# ── notify_transition ─────────────────────────────────────────────────────────

class TestNotifyTransition(unittest.TestCase):

    def test_sets_in_transition(self):
        ctrl = _make_ctrl()
        ctrl.notify_transition("test")
        self.assertTrue(ctrl._in_transition)
        self.assertGreater(ctrl._transition_expires, time.monotonic())

    def test_expired_transition_cleared_by_get_params(self):
        ctrl = _make_ctrl()
        ctrl._in_transition = True
        ctrl._transition_expires = time.monotonic() - 1  # already expired
        p = ctrl.get_params()
        self.assertFalse(p.in_transition)
        self.assertFalse(ctrl._in_transition)


# ── Persistence ───────────────────────────────────────────────────────────────

class TestPersistence(unittest.TestCase):

    def test_only_non_empty_slots_serialized(self):
        ctrl = _make_ctrl(); ctrl._slots[5].n = 10.0
        data = ctrl._serialize()
        self.assertIn("5", data["slots"])
        for k, v in data["slots"].items():
            self.assertGreater(float(v.get("n", 0)), 0.0,
                f"Slot {k} is empty but was serialized")

    def test_deserialize_restores_slots(self):
        ctrl = _make_ctrl()
        ctrl._slots[10].n = 25.0; ctrl._slots[10].failures = 5.0
        ctrl._first_data_date = date.today()
        ctrl._last_decay_date = date.today()
        serialized = ctrl._serialize()
        ctrl2 = _make_ctrl(); ctrl2._deserialize(serialized)
        self.assertAlmostEqual(ctrl2._slots[10].n, 25.0, places=3)

    def test_invalid_indices_ignored(self):
        ctrl = _make_ctrl()
        ctrl._deserialize({"slots": {"999": {"n": 1}, "-1": {"n": 1}, "abc": {"n": 1}}})
        # must not raise

    def test_startup_decay_applied(self):
        ctrl = _make_ctrl()
        ctrl._last_decay_date = date.today() - timedelta(days=1)
        ctrl._slots[0].n = 100.0
        ctrl._apply_startup_decay()
        self.assertAlmostEqual(ctrl._slots[0].n, 100.0 * ADAPTIVE_DECAY_FACTOR, places=4)

    def test_no_decay_same_day(self):
        ctrl = _make_ctrl()
        ctrl._last_decay_date = date.today()
        ctrl._slots[0].n = 100.0
        ctrl._apply_startup_decay()
        self.assertAlmostEqual(ctrl._slots[0].n, 100.0, places=4)


# ═══════════════════════════════════════════════════════════════════════════════
# v1.1.1 regression tests — BUG-003, 004, 005, 009, 010, 011
# ═══════════════════════════════════════════════════════════════════════════════

# ── BUG-003: listener iteration mutation ──────────────────────────────────────

class TestListenerIterationMutation(unittest.TestCase):
    """BUG-003: removing a listener during dispatch must not skip later ones."""

    def _make_ctrl_with_listeners(self):
        ctrl = _make_ctrl()
        results = []

        def cb_a(snap):
            results.append("a")

        def cb_b(snap):
            # self-removing callback — previously caused b→c skip
            ctrl.remove_listener(cb_b)
            results.append("b")

        def cb_c(snap):
            results.append("c")

        ctrl.add_listener(cb_a)
        ctrl.add_listener(cb_b)
        ctrl.add_listener(cb_c)
        return ctrl, results

    def test_all_listeners_called_when_one_self_removes(self):
        ctrl, results = self._make_ctrl_with_listeners()
        ctrl._push_to_listeners(None)
        self.assertEqual(results, ["a", "b", "c"],
            "BUG-003: listener 'c' was skipped because 'b' removed itself during iteration")

    def test_listener_list_shrinks_after_self_remove(self):
        ctrl, _ = self._make_ctrl_with_listeners()
        ctrl._push_to_listeners(None)
        self.assertEqual(len(ctrl._listeners), 2,
            "After self-removal during dispatch, only 2 listeners should remain")

    def test_empty_listeners_ok(self):
        ctrl = _make_ctrl()
        ctrl._push_to_listeners(None)  # must not raise


# ── BUG-011: listener callback exception isolation ────────────────────────────

class TestListenerCallbackIsolation(unittest.TestCase):
    """BUG-011: a failing callback must not prevent subsequent listeners from receiving their update."""

    def test_exception_in_one_callback_does_not_abort_others(self):
        ctrl = _make_ctrl()
        results = []

        def bad_cb(snap):
            raise RuntimeError("simulated listener crash")

        def good_cb(snap):
            results.append("delivered")

        ctrl.add_listener(bad_cb)
        ctrl.add_listener(good_cb)
        # Must not raise, and good_cb must still be called
        ctrl._push_to_listeners(None)
        self.assertEqual(results, ["delivered"],
            "BUG-011: good_cb should still be called even though bad_cb raised")

    def test_multiple_bad_callbacks_all_attempted(self):
        ctrl = _make_ctrl()
        call_log = []

        for i in range(3):
            def make_bad(idx):
                def bad(snap):
                    call_log.append(f"bad_{idx}")
                    raise ValueError(f"error from cb {idx}")
                return bad
            ctrl.add_listener(make_bad(i))

        ctrl._push_to_listeners(None)
        self.assertEqual(len(call_log), 3,
            "All 3 bad callbacks should be attempted even though they all raise")


# ── BUG-004: flush pending save on stop() ────────────────────────────────────

class TestFlushOnStop(unittest.TestCase):
    """BUG-004: stop() must persist pending data instead of silently cancelling it."""

    def test_dirty_flag_triggers_save_on_stop(self):
        ctrl = _make_ctrl()
        ctrl._dirty = True
        ctrl.stop()
        # async_create_task must have been called to schedule the save
        ctrl.hass.async_create_task.assert_called_once()

    def test_clean_flag_no_save_on_stop(self):
        ctrl = _make_ctrl()
        ctrl._dirty = False
        ctrl.stop()
        ctrl.hass.async_create_task.assert_not_called()

    def test_unsub_push_cancelled_on_stop(self):
        ctrl = _make_ctrl()
        unsub = MagicMock()
        ctrl._unsub_push = unsub
        ctrl.stop()
        unsub.assert_called_once()
        self.assertIsNone(ctrl._unsub_push)

    def test_pending_save_task_cancelled_on_stop(self):
        ctrl = _make_ctrl()
        task = MagicMock()
        task.done.return_value = False
        ctrl._save_task = task
        ctrl._dirty = False
        ctrl.stop()
        task.cancel.assert_called_once()


# ── BUG-005: TimeSlotStats.label correctness ─────────────────────────────────

class TestTimeSlotStatsLabel(unittest.TestCase):
    """BUG-005: label must return HH:MM–HH:MM, not empty string."""

    def test_slot_0_label(self):
        s = TimeSlotStats(slot_index=0)
        self.assertEqual(s.label, "00:00\u201300:15",
            "BUG-005: slot 0 label should be '00:00–00:15'")

    def test_slot_47_label(self):
        # slot 47 = 47 * 15 = 705 min = 11:45–12:00
        s = TimeSlotStats(slot_index=47)
        self.assertEqual(s.label, "11:45\u201312:00")

    def test_slot_95_label(self):
        # slot 95 = 95 * 15 = 1425 min = 23:45–24:00
        s = TimeSlotStats(slot_index=95)
        self.assertEqual(s.label, "23:45\u201324:00")

    def test_label_never_empty(self):
        for i in range(96):
            s = TimeSlotStats(slot_index=i)
            self.assertTrue(len(s.label) > 0,
                f"BUG-005: slot {i} label is empty string")

    def test_from_dict_preserves_label(self):
        """round-tripping through from_dict must keep correct label."""
        s = TimeSlotStats(slot_index=32)
        original_label = s.label
        s2 = TimeSlotStats.from_dict(s.to_dict(), slot_index=32)
        self.assertEqual(s2.label, original_label)

    def test_reset_slots_preserves_indices(self):
        ctrl = _make_ctrl()
        for i, slot in enumerate(ctrl._slots):
            self.assertEqual(slot.slot_index, i,
                f"Slot {i} has wrong slot_index={slot.slot_index} after _reset_slots()")

    def test_default_label_no_longer_empty(self):
        """Guard against regression to the broken empty-string default."""
        s = TimeSlotStats(slot_index=10)
        self.assertNotEqual(s.label, "",
            "BUG-005 regression: label must not be empty string")


# ── BUG-009: CancelledError propagation in _deferred_save ────────────────────

class TestDeferredSaveCancelledError(unittest.TestCase):
    """BUG-009: cancellation must propagate, not be swallowed."""

    def test_cancelled_error_propagates(self):
        ctrl = _make_ctrl()

        async def _run_and_cancel():
            task = asyncio.get_event_loop().create_task(ctrl._deferred_save())
            await asyncio.sleep(0)  # let task start
            task.cancel()
            try:
                await task
                return False  # CancelledError was swallowed — bug still present
            except asyncio.CancelledError:
                return True   # correct behaviour

        result = _LOOP.run_until_complete(_run_and_cancel())
        self.assertTrue(result,
            "BUG-009: CancelledError must not be swallowed inside _deferred_save")

    def test_save_not_called_on_cancel_before_sleep(self):
        ctrl = _make_ctrl()
        ctrl._dirty = True

        async def _run_and_cancel():
            task = asyncio.get_event_loop().create_task(ctrl._deferred_save())
            await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        _LOOP.run_until_complete(_run_and_cancel())
        ctrl._store.async_save.assert_not_called()


# ── BUG-010: debounce dirty-flag not dropped ──────────────────────────────────

class TestDebounceDirtyFlag(unittest.TestCase):
    """BUG-010: calling _schedule_save while a task is in-flight must keep _dirty=True."""

    def test_dirty_set_unconditionally(self):
        ctrl = _make_ctrl()
        # Simulate an in-flight task
        mock_task = MagicMock()
        mock_task.done.return_value = False
        ctrl._save_task = mock_task
        ctrl._dirty = False

        ctrl._schedule_save()

        self.assertTrue(ctrl._dirty,
            "BUG-010: _dirty must be True even when an in-flight task exists; "
            "otherwise data recorded during the debounce window is silently lost")

    def test_no_new_task_when_in_flight(self):
        ctrl = _make_ctrl()
        mock_task = MagicMock()
        mock_task.done.return_value = False
        ctrl._save_task = mock_task

        ctrl._schedule_save()

        ctrl.hass.async_create_task.assert_not_called()

    def test_new_task_created_when_idle(self):
        ctrl = _make_ctrl()
        ctrl._save_task = None
        ctrl._schedule_save()
        ctrl.hass.async_create_task.assert_called_once()

    def test_deferred_save_persists_if_dirty_on_wake(self):
        ctrl = _make_ctrl()
        ctrl._first_data_date = date.today()
        ctrl._dirty = True

        async def short_save():
            # Shorten the sleep so the test doesn't block for 60 s
            import unittest.mock
            with unittest.mock.patch("asyncio.sleep", new=AsyncMock()):
                await ctrl._deferred_save()

        _LOOP.run_until_complete(short_save())
        ctrl._store.async_save.assert_called_once()


if __name__ == "__main__":
    unittest.main()


# ══════════════════════════════════════════════════════════════════════════════
# v1.2.2 — learning gate
# ══════════════════════════════════════════════════════════════════════════════
class TestLearningGate(unittest.TestCase):
    """record_request() cannot tell WHY a request was slow or failed.

    HA event-loop congestion and an unreachable inverter are recorded
    identically, so the controller must not learn while either is suspected.
    """

    def test_observations_discarded_while_disabled(self):
        ctrl = _make_ctrl()
        ctrl.set_learning_enabled(False)
        for _ in range(20):
            ctrl.record_request(100.0, success=False, timeout=True)
        slot = ctrl._slots[ctrl._current_slot_index()]
        self.assertEqual(slot.n, 0.0)
        self.assertEqual(ctrl.suppressed_observations, 20)

    def test_observations_recorded_when_enabled(self):
        ctrl = _make_ctrl()
        for _ in range(10):
            ctrl.record_request(100.0, success=True, timeout=False)
        slot = ctrl._slots[ctrl._current_slot_index()]
        self.assertEqual(slot.n, 10.0)
        self.assertEqual(ctrl.suppressed_observations, 0)

    def test_settling_blocks_then_expires(self):
        ctrl = _make_ctrl()
        ctrl.mark_recovery("test")
        self.assertFalse(ctrl.learning_active())
        ctrl.record_request(100.0, success=False, timeout=True)
        self.assertEqual(ctrl._slots[ctrl._current_slot_index()].n, 0.0)
        # Simulate the settling window elapsing.
        ctrl._suppressed_until = time.time() - 1
        self.assertTrue(ctrl.learning_active())
        ctrl.record_request(100.0, success=True, timeout=False)
        self.assertEqual(ctrl._slots[ctrl._current_slot_index()].n, 1.0)

    def test_indefinite_suppression_never_expires(self):
        ctrl = _make_ctrl()
        ctrl.suppress_indefinitely("home assistant stopping")
        self.assertFalse(ctrl.learning_active())
        ctrl._suppressed_until = ctrl._suppressed_until  # unchanged by time
        self.assertFalse(ctrl.learning_active())

    def test_reenable_triggers_settling(self):
        ctrl = _make_ctrl()
        ctrl.set_learning_enabled(False)
        ctrl.set_learning_enabled(True)
        self.assertTrue(ctrl.learning_enabled)
        self.assertFalse(ctrl.learning_active())

    def test_firmware_update_scenario_leaves_slot_untouched(self):
        """The exposure that motivated the gate.

        A ~1 h Huawei firmware update is ~120 consecutive failed requests
        across four 15-minute slots. On a mature slot that lifts the failure
        rate from ~3% to ~12%, mapping to a poll interval near 137 s rather
        than 20-30 s. Daily decay does NOT undo it: decay scales failures and
        sample count equally, so the RATIO survives - only new successful
        observations dilute it, and those accrue far more slowly because
        polling has slowed.
        """
        ctrl = _make_ctrl()
        idx = ctrl._current_slot_index()
        # Mature, healthy slot: 300 observations at a ~3% failure rate.
        for i in range(300):
            ctrl.record_request(200.0, success=(i % 33 != 0), timeout=(i % 33 == 0))
        healthy_rate = ctrl._slots[idx].failure_rate
        self.assertLess(healthy_rate, 0.05)

        # Operator disables learning before the firmware update.
        ctrl.set_learning_enabled(False)
        for _ in range(120):
            ctrl.record_request(60000.0, success=False, timeout=True)

        self.assertAlmostEqual(ctrl._slots[idx].failure_rate, healthy_rate,
                               places=6)
        self.assertEqual(ctrl.suppressed_observations, 120)

    def test_unguarded_firmware_update_would_have_poisoned_the_slot(self):
        """Control case: proves the guard is load-bearing, not decorative."""
        ctrl = _make_ctrl()
        idx = ctrl._current_slot_index()
        for i in range(300):
            ctrl.record_request(200.0, success=(i % 33 != 0), timeout=(i % 33 == 0))
        healthy_rate = ctrl._slots[idx].failure_rate
        for _ in range(120):          # learning left ENABLED
            ctrl.record_request(60000.0, success=False, timeout=True)
        poisoned = ctrl._slots[idx].failure_rate
        self.assertGreater(poisoned, healthy_rate * 3)
        self.assertGreater(poisoned, 0.10)

    def test_decay_does_not_repair_a_poisoned_failure_rate(self):
        """Decay lowers confidence, not the failure RATE - the key asymmetry."""
        ctrl = _make_ctrl()
        idx = ctrl._current_slot_index()
        for i in range(300):
            ctrl.record_request(200.0, success=(i % 33 != 0), timeout=(i % 33 == 0))
        for _ in range(120):
            ctrl.record_request(60000.0, success=False, timeout=True)
        before = ctrl._slots[idx].failure_rate
        for _ in range(14):           # a fortnight of decay
            ctrl._slots[idx].apply_decay(ADAPTIVE_DECAY_FACTOR)
        self.assertAlmostEqual(ctrl._slots[idx].failure_rate, before, places=6)

    def test_gate_state_persisted(self):
        ctrl = _make_ctrl()
        ctrl.set_learning_enabled(False)
        ctrl.suppressed_observations = 42
        raw = ctrl._serialize()
        self.assertFalse(raw["learning_enabled"])
        self.assertEqual(raw["suppressed_observations"], 42)


# ══════════════════════════════════════════════════════════════════════════════
# v1.2.3 — Defect A (RTT scale), Defect B (queue-depth blending)
# ══════════════════════════════════════════════════════════════════════════════
class TestDefectAScale(unittest.TestCase):
    """rtt_p95_ms must be a PER-REQUEST figure, not a batch total.

    Field evidence (2 months): the gap sat at its 500 ms ceiling 84% of the
    time (time-weighted) and the timeout at its 60 s ceiling 42%. The gap
    saturates at rtt >= 1250 ms and the timeout at rtt >= 12000 ms, so the
    stored value exceeded twelve SECONDS for nearly half the window — not a
    physically possible single Modbus round trip.
    """

    def _feed(self, ctrl, rtt_ms, n=60):
        for _ in range(n):
            ctrl.record_request(rtt_ms, success=True, timeout=False)

    def test_realistic_per_request_rtt_does_not_saturate_gap(self):
        """The regression the report asked for: healthy RTTs -> no ceiling."""
        ctrl = _make_ctrl()
        self._feed(ctrl, 350.0, n=200)   # a plausible single-chunk RTT
        p = ctrl.get_params()
        self.assertLess(p.request_gap.total_seconds() * 1000, 490,
                        "healthy per-request RTT must not pin the gap")
        self.assertLess(p.request_timeout.total_seconds(), 59)

    def test_batch_scale_rtt_would_saturate_both(self):
        """Control case: proves the assertion above is meaningful.

        Feeding a batch-summed value (the pre-v1.2.3 behaviour) must pin both
        parameters — otherwise the test above would pass for the wrong reason.
        """
        ctrl = _make_ctrl()
        # 200 samples so confidence reaches 1.0 and the derived value is used
        # unblended — otherwise the cold-start blend masks the saturation.
        self._feed(ctrl, 9000.0, n=200)  # ~26 chunks x 350 ms, i.e. a batch total
        p = ctrl.get_params()
        # Gap saturates at rtt >= 1250 ms, so a batch total pins it hard.
        self.assertGreaterEqual(p.request_gap.total_seconds() * 1000, 495)
        # Timeout saturates only at rtt >= 12000 ms; at 9 s it lands at ~45 s,
        # already three times the 15 s floor. The field data showed FULL
        # timeout saturation 42% of the time, i.e. an implied rtt above 12 s.
        self.assertGreaterEqual(p.request_timeout.total_seconds(), 40)

    def test_field_scale_rtt_saturates_the_timeout_ceiling_too(self):
        """Reproduces the observed 42% timeout-ceiling condition."""
        ctrl = _make_ctrl()
        self._feed(ctrl, 12500.0, n=200)
        p = ctrl.get_params()
        self.assertGreaterEqual(p.request_timeout.total_seconds(), 59)

    def test_gap_only_reaches_ceiling_when_failure_rate_is_elevated(self):
        ctrl = _make_ctrl()
        for i in range(200):             # ~20% failures, genuinely unhealthy
            ctrl.record_request(350.0, success=(i % 5 != 0), timeout=(i % 5 == 0))
        p = ctrl.get_params()
        self.assertGreater(p.request_gap.total_seconds() * 1000, 250)

    def test_note_batch_is_diagnostic_only(self):
        """Batch totals must never enter the learning model."""
        ctrl = _make_ctrl()
        self._feed(ctrl, 350.0, n=200)
        before = ctrl._slots[ctrl._current_slot_index()].rtt_p95_ms
        ctrl.note_batch(9000.0, 26)
        after = ctrl._slots[ctrl._current_slot_index()].rtt_p95_ms
        self.assertEqual(before, after)
        self.assertEqual(ctrl.last_chunk_count, 26)


class TestDefectBQueueDepthBlending(unittest.TestCase):
    """Queue depth was the only output with no cold-start blending."""

    def test_unseen_slot_no_longer_gets_most_permissive_depth(self):
        ctrl = _make_ctrl()
        p = ctrl.get_params()
        self.assertEqual(p.confidence, 0.0)
        self.assertLessEqual(p.max_queue_depth, ADAPTIVE_QUEUE_DEPTH_COLD_START)
        self.assertLess(p.max_queue_depth, 3)

    def test_mature_healthy_slot_still_reaches_full_depth(self):
        ctrl = _make_ctrl()
        for _ in range(400):
            ctrl.record_request(300.0, success=True, timeout=False)
        p = ctrl.get_params()
        self.assertEqual(p.confidence, 1.0)
        self.assertEqual(p.max_queue_depth, 3)

    def test_mature_unhealthy_slot_clamps_to_one(self):
        ctrl = _make_ctrl()
        for i in range(400):
            ctrl.record_request(300.0, success=(i % 4 != 0), timeout=(i % 4 == 0))
        self.assertEqual(ctrl.get_params().max_queue_depth, 1)

    def test_depth_never_below_one(self):
        ctrl = _make_ctrl()
        for _ in range(5):
            ctrl.record_request(300.0, success=False, timeout=True)
        self.assertGreaterEqual(ctrl.get_params().max_queue_depth, 1)

    def test_cold_start_baseline_is_two_not_one(self):
        """Depth 1 would shed aggressively on the very slots being protected.

        Queue depth does not create concurrency (the guard holds a single
        lock); it only bounds how many callers may wait. With up to five
        sub-coordinators per inverter on a shared bus, 1 is a shedding
        machine, so the cautious baseline is 2.
        """
        self.assertEqual(ADAPTIVE_QUEUE_DEPTH_COLD_START, 2)


class TestShedNotLearned(unittest.TestCase):
    """Defect D: shed requests must never enter the circadian model."""

    def test_note_shed_does_not_touch_slot_statistics(self):
        ctrl = _make_ctrl()
        for _ in range(50):
            ctrl.record_request(300.0, success=True, timeout=False)
        slot = ctrl._slots[ctrl._current_slot_index()]
        n_before, fr_before = slot.n, slot.failure_rate
        for _ in range(30):
            ctrl.note_shed()
        self.assertEqual(slot.n, n_before)
        self.assertEqual(slot.failure_rate, fr_before)
        self.assertEqual(ctrl.shed_count, 30)

    def test_feedback_loop_is_broken(self):
        """The loop that made Defect B dangerous without Defect D.

        shed -> recorded as failure -> failure rate up -> queue depth down ->
        more shedding. With sheds excluded from learning, a burst of shedding
        must leave the derived parameters untouched.
        """
        ctrl = _make_ctrl()
        for _ in range(400):
            ctrl.record_request(300.0, success=True, timeout=False)
        before = ctrl.get_params()
        for _ in range(200):
            ctrl.note_shed()
        after = ctrl.get_params()
        self.assertEqual(before.max_queue_depth, after.max_queue_depth)
        self.assertEqual(before.poll_interval, after.poll_interval)
        self.assertEqual(before.request_gap, after.request_gap)


class TestStorageMigrationV1toV2(unittest.TestCase):
    """v1 RTT samples are on a different scale and must be discarded.

    Without this, the Defect A fix would appear not to work: rtt_samples is
    FIFO-trimmed rather than time-windowed, so historical batch-summed values
    would dominate the P95 for a long time after the upgrade.
    """

    def test_rtt_state_cleared(self):
        ctrl = _make_ctrl()
        for slot in ctrl._slots[:5]:
            slot.rtt_samples = [9000.0, 9500.0, 11000.0]
            slot.rtt_p95_ms = 11000.0
        ctrl._migrate_v1_rtt_scale()
        for slot in ctrl._slots[:5]:
            self.assertEqual(slot.rtt_samples, [])
            self.assertEqual(slot.rtt_p95_ms, 0.0)

    def test_failure_history_is_preserved(self):
        """Only the RTT scale changed — months of failure learning must stay."""
        ctrl = _make_ctrl()
        slot = ctrl._slots[0]
        slot.n, slot.failures, slot.timeouts = 300.0, 21.0, 9.0
        slot.rtt_samples, slot.rtt_p95_ms = [9000.0], 9000.0
        fr_before = slot.failure_rate
        ctrl._migrate_v1_rtt_scale()
        self.assertEqual(slot.n, 300.0)
        self.assertEqual(slot.failures, 21.0)
        self.assertEqual(slot.failure_rate, fr_before)
        self.assertEqual(slot.rtt_p95_ms, 0.0)

    def test_ha_store_version_must_stay_1(self):
        """REGRESSION (v1.2.3 -> v1.2.4): the outage this caused.

        v1.2.3 bumped the Home Assistant Store version to 2 to trigger the RTT
        rescale. HA's Store calls ``_async_migrate_func`` whenever the stored
        version is older than the requested one, and the default raises
        NotImplementedError. That exception propagated out of async_load(),
        out of _setup_inverter_device_data(), and aborted async_setup_entry —
        taking down EVERY entity in the integration, not just adaptive tuning.

        The Store version may only be bumped together with a migration
        callable. Payload-level migrations use _DATA_SCHEMA_VERSION instead.
        """
        self.assertEqual(
            _MOD._STORAGE_VERSION, 1,
            "bumping the HA Store version without a migration callable aborts "
            "config-entry setup — use _DATA_SCHEMA_VERSION for payload changes",
        )

    def test_payload_schema_version_drives_migration(self):
        self.assertEqual(_MOD._DATA_SCHEMA_VERSION, 2)
        ctrl = _make_ctrl()
        self.assertEqual(ctrl._serialize()["data_schema"], 2)

    def test_pre_1_2_3_payload_has_no_marker_and_migrates(self):
        """Absent marker means pre-v1.2.3 data, recorded on the old RTT scale."""
        legacy = {"version": 1}          # no data_schema key
        self.assertLess(int(legacy.get("data_schema", 1)), _MOD._DATA_SCHEMA_VERSION)

    def test_migration_marks_state_dirty(self):
        ctrl = _make_ctrl()
        ctrl._dirty = False
        ctrl._migrate_v1_rtt_scale()
        self.assertTrue(ctrl._dirty)


class TestStoreLoadFaultIsolation(unittest.TestCase):
    """Adaptive storage must never be able to abort config-entry setup.

    Adaptive learning is an OPTIONAL optimisation. Losing it costs tuned poll
    parameters; an exception escaping async_load() costs the user every entity
    in the integration — which is exactly what happened in v1.2.3.
    """

    def test_store_load_is_guarded(self):
        src = (pathlib.Path(__file__).parent.parent / "adaptive_modbus.py").read_text()
        idx = src.find("await self._store.async_load()")
        self.assertGreater(idx, -1)
        window = src[max(0, idx - 400):idx]
        self.assertIn("try:", window,
                      "the Store load must be inside a try/except so a corrupt "
                      "or version-incompatible store cannot abort entry setup")

    def test_load_failure_leaves_controller_usable(self):
        """A failed load must yield working defaults, not a broken object."""
        ctrl = _make_ctrl()
        params = ctrl.get_params()        # no stored data at all
        self.assertIsNotNone(params.poll_interval)
        self.assertGreaterEqual(params.max_queue_depth, 1)
