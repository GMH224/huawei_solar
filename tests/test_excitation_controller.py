"""Tests for excitation_controller.py (v2.0.15 experimental identification
release).

Uses real execution against the actual module -- not source-level pattern
matching -- since ExcitationController and its helpers are genuinely
self-contained (stdlib + AdaptiveParams only, no HA-heavy dependency
chain), matching the same real-execution testing convention already used
for physical_group_for()/_split_by_physical_group() in
test_update_coordinator.py.

Test schedules use monkeypatched, near-zero dwell times (real dwell is
hours) so the full lifecycle can be exercised in milliseconds -- this is
about the state machine's own logic, not about waiting out real clocks.
"""
from __future__ import annotations

from datetime import timedelta
import unittest

from ..adaptive_modbus import AdaptiveParams
from .. import excitation_controller as ec
from ..excitation_controller import (
    ExcitationController,
    ExcitationLevel,
    ExcitationMode,
    ExcitationScheduleEntry,
    _DEFAULT_SCHEDULE,
    EXCITATION_TIMEOUT_FLOOR_S,
)


def _base_params(**overrides) -> AdaptiveParams:
    defaults = dict(
        poll_interval=timedelta(seconds=60),
        request_gap=timedelta(milliseconds=500),
        request_timeout=timedelta(seconds=20),
        max_queue_depth=3,
        confidence=1.0,
        in_transition=False,
        slot_index=0,
        slot_failure_rate=0.0,
    )
    defaults.update(overrides)
    return AdaptiveParams(**defaults)


def _tiny_schedule() -> tuple[ExcitationScheduleEntry, ...]:
    """A 2-mode, 2-level-each schedule for fast, deterministic testing."""
    return (
        ExcitationScheduleEntry(ExcitationMode.EXCITE_GAP, (
            ExcitationLevel(150.0, "GAP_LOW"),
            ExcitationLevel(500.0, "GAP_HIGH"),
        )),
        ExcitationScheduleEntry(ExcitationMode.EXCITE_POLL, (
            ExcitationLevel(30.0, "POLL_LOW"),
            ExcitationLevel(90.0, "POLL_HIGH"),
        )),
    )


class _FastDwellTestCase(unittest.TestCase):
    """Monkeypatches dwell/transaction-count thresholds to small, fast
    values for the duration of each test, restoring the real values
    afterward -- so this file never accidentally leaves the module in a
    modified state for anything imported after it."""

    def setUp(self):
        self._orig_dwell = ec._MIN_LEVEL_DWELL
        self._orig_count = ec._MIN_LEVEL_TRANSACTIONS
        self._orig_window = ec._GONOGO_WINDOW_TRANSACTIONS
        ec._MIN_LEVEL_DWELL = timedelta(seconds=0)
        ec._MIN_LEVEL_TRANSACTIONS = 5
        ec._GONOGO_WINDOW_TRANSACTIONS = 20

    def tearDown(self):
        ec._MIN_LEVEL_DWELL = self._orig_dwell
        ec._MIN_LEVEL_TRANSACTIONS = self._orig_count
        ec._GONOGO_WINDOW_TRANSACTIONS = self._orig_window


class TestDefaultScheduleSafety(unittest.TestCase):
    """The ACTUAL production schedule this release ships, not a test
    fixture -- these checks exist specifically to catch a future edit to
    _build_default_schedule() that reintroduces an already-fixed danger,
    without needing a human to notice on review."""

    def test_no_gap_level_outside_the_existing_validated_envelope(self):
        from ..const import ADAPTIVE_GAP_MIN, ADAPTIVE_GAP_MAX
        gap_entry = next(e for e in _DEFAULT_SCHEDULE if e.mode == ExcitationMode.EXCITE_GAP)
        for level in gap_entry.levels:
            self.assertGreaterEqual(level.value, ADAPTIVE_GAP_MIN.total_seconds() * 1000)
            self.assertLessEqual(level.value, ADAPTIVE_GAP_MAX.total_seconds() * 1000)

    def test_no_poll_level_outside_the_agreed_safe_subset(self):
        """30-120s, per IMM_V2_Controller_V1_spec.md's own hard bounds --
        deliberately narrower than the full ADAPTIVE_POLL_MIN..MAX (20-180s)
        envelope, per _build_default_schedule()'s own docstring."""
        poll_entry = next(e for e in _DEFAULT_SCHEDULE if e.mode == ExcitationMode.EXCITE_POLL)
        for level in poll_entry.levels:
            self.assertGreaterEqual(level.value, 30.0)
            self.assertLessEqual(level.value, 120.0)

    def test_no_schedule_entry_for_timeout(self):
        """TIMEOUT is deliberately not excited this release (module
        docstring) -- this test exists so that decision requires an
        explicit, reviewed change to this test, not a silent schedule
        edit."""
        modes = {e.mode for e in _DEFAULT_SCHEDULE}
        self.assertNotIn("EXCITE_TIMEOUT", [m.value for m in modes])

    def test_gap_excitation_sequenced_before_poll(self):
        """Single-input-first discipline: GAP must complete before POLL
        begins, matching Adaptive_Modbus_Controller_Experimental_
        Identification_Release.md Section 9's own reasoning against
        simultaneous multi-input changes."""
        self.assertEqual(_DEFAULT_SCHEDULE[0].mode, ExcitationMode.EXCITE_GAP)
        self.assertEqual(_DEFAULT_SCHEDULE[1].mode, ExcitationMode.EXCITE_POLL)

    def test_timeout_floor_constant_is_at_least_the_agreed_30s(self):
        self.assertGreaterEqual(EXCITATION_TIMEOUT_FLOOR_S, 30.0)


class TestLifecycle(_FastDwellTestCase):

    def test_starts_at_first_schedule_entrys_own_mode(self):
        ctrl = ExcitationController(schedule=_tiny_schedule())
        self.assertEqual(ctrl._state, ExcitationMode.EXCITE_GAP)

    def test_empty_schedule_starts_complete(self):
        ctrl = ExcitationController(schedule=())
        self.assertEqual(ctrl._state, ExcitationMode.COMPLETE)

    def test_apply_overrides_only_the_excited_field(self):
        ctrl = ExcitationController(schedule=_tiny_schedule())
        base = _base_params()
        applied = ctrl.apply(base, in_transition=False)
        self.assertEqual(applied.request_gap, timedelta(milliseconds=150))
        self.assertEqual(applied.poll_interval, base.poll_interval)  # untouched
        self.assertEqual(applied.request_timeout, base.request_timeout)  # untouched
        self.assertEqual(applied.max_queue_depth, base.max_queue_depth)  # untouched

    def test_in_transition_always_wins_no_override(self):
        ctrl = ExcitationController(schedule=_tiny_schedule())
        base = _base_params()
        applied = ctrl.apply(base, in_transition=True)
        self.assertEqual(applied, base)

    def test_advances_within_a_mode_after_dwell_and_count_met(self):
        ctrl = ExcitationController(schedule=_tiny_schedule())
        for _ in range(5):
            ctrl.record_outcome(success=True, was_timeout=False)
        ctrl.maybe_advance()
        self.assertEqual(ctrl._current_level().label, "GAP_HIGH")

    def test_does_not_advance_before_transaction_count_met(self):
        ctrl = ExcitationController(schedule=_tiny_schedule())
        for _ in range(4):  # one short of the monkeypatched threshold (5)
            ctrl.record_outcome(success=True, was_timeout=False)
        ctrl.maybe_advance()
        self.assertEqual(ctrl._current_level().label, "GAP_LOW")

    def test_advances_across_modes_not_just_within_one(self):
        ctrl = ExcitationController(schedule=_tiny_schedule())
        for _ in range(2):
            for _ in range(5):
                ctrl.record_outcome(success=True, was_timeout=False)
            ctrl.maybe_advance()
        self.assertEqual(ctrl._state, ExcitationMode.EXCITE_POLL)
        self.assertEqual(ctrl._current_level().label, "POLL_LOW")

    def test_reaches_complete_after_every_level_of_every_mode(self):
        ctrl = ExcitationController(schedule=_tiny_schedule())
        for _ in range(4):  # 2 modes x 2 levels each = 4 advances to COMPLETE
            for _ in range(5):
                ctrl.record_outcome(success=True, was_timeout=False)
            ctrl.maybe_advance()
        self.assertEqual(ctrl._state, ExcitationMode.COMPLETE)

    def test_complete_behaves_identically_to_normal_no_override(self):
        ctrl = ExcitationController(schedule=())  # empty -> immediately COMPLETE
        base = _base_params()
        applied = ctrl.apply(base, in_transition=False)
        self.assertEqual(applied, base)

    def test_level_counters_reset_on_advance(self):
        ctrl = ExcitationController(schedule=_tiny_schedule())
        for _ in range(5):
            ctrl.record_outcome(success=True, was_timeout=False)
        ctrl.maybe_advance()
        self.assertEqual(ctrl._level_transaction_count, 0)


class TestGoNoGoSafety(_FastDwellTestCase):
    """The safety-critical path -- exercised most thoroughly of anything
    in this file, matching how central it is to the "no rollercoaster
    deployment" requirement this release was built under."""

    def test_error_rate_breach_halts(self):
        ctrl = ExcitationController(schedule=_tiny_schedule())
        for _ in range(19):
            ctrl.record_outcome(success=True, was_timeout=False)
        for _ in range(2):  # pushes error rate over 5% in the 20-window
            ctrl.record_outcome(success=False, was_timeout=False)
        self.assertEqual(ctrl._state, ExcitationMode.HALTED)
        self.assertIsNotNone(ctrl._halt_reason)

    def test_timeout_rate_breach_halts(self):
        ctrl = ExcitationController(schedule=_tiny_schedule())
        for _ in range(19):
            ctrl.record_outcome(success=True, was_timeout=False)
        ctrl.record_outcome(success=False, was_timeout=True)  # 5% timeout rate, over 3% threshold
        self.assertEqual(ctrl._state, ExcitationMode.HALTED)

    def test_below_threshold_does_not_halt(self):
        ctrl = ExcitationController(schedule=_tiny_schedule())
        for _ in range(20):
            ctrl.record_outcome(success=True, was_timeout=False)
        self.assertNotEqual(ctrl._state, ExcitationMode.HALTED)

    def test_incomplete_window_never_judged(self):
        """Fewer transactions than the window size must never trigger a
        halt, even at 100% failure -- there isn't enough data yet to
        judge, and judging early would be a false-positive risk, not a
        safety benefit."""
        ctrl = ExcitationController(schedule=_tiny_schedule())
        for _ in range(19):  # one short of the window
            ctrl.record_outcome(success=False, was_timeout=False)
        self.assertNotEqual(ctrl._state, ExcitationMode.HALTED)

    def test_halted_applies_no_override(self):
        ctrl = ExcitationController(schedule=_tiny_schedule())
        for _ in range(19):
            ctrl.record_outcome(success=True, was_timeout=False)
        for _ in range(2):
            ctrl.record_outcome(success=False, was_timeout=False)
        base = _base_params()
        applied = ctrl.apply(base, in_transition=False)
        self.assertEqual(applied, base)

    def test_halted_does_not_auto_advance(self):
        ctrl = ExcitationController(schedule=_tiny_schedule())
        for _ in range(19):
            ctrl.record_outcome(success=True, was_timeout=False)
        for _ in range(2):
            ctrl.record_outcome(success=False, was_timeout=False)
        ctrl.maybe_advance()
        self.assertEqual(ctrl._state, ExcitationMode.HALTED)

    def test_halted_does_not_auto_resume_via_record_outcome_either(self):
        """Adversarial: feeding a long run of clean outcomes AFTER a halt
        must not silently un-halt the controller -- only an explicit
        resume_after_halt() call may do that."""
        ctrl = ExcitationController(schedule=_tiny_schedule())
        for _ in range(19):
            ctrl.record_outcome(success=True, was_timeout=False)
        for _ in range(2):
            ctrl.record_outcome(success=False, was_timeout=False)
        self.assertEqual(ctrl._state, ExcitationMode.HALTED)
        for _ in range(100):
            ctrl.record_outcome(success=True, was_timeout=False)
        self.assertEqual(ctrl._state, ExcitationMode.HALTED)

    def test_resume_after_halt_restarts_current_mode_at_level_zero(self):
        ctrl = ExcitationController(schedule=_tiny_schedule())
        for _ in range(5):
            ctrl.record_outcome(success=True, was_timeout=False)
        ctrl.maybe_advance()  # now at GAP_HIGH
        for _ in range(19):
            ctrl.record_outcome(success=True, was_timeout=False)
        for _ in range(2):
            ctrl.record_outcome(success=False, was_timeout=False)
        self.assertEqual(ctrl._state, ExcitationMode.HALTED)
        ctrl.resume_after_halt()
        self.assertEqual(ctrl._state, ExcitationMode.EXCITE_GAP)
        self.assertEqual(ctrl._level_idx, 0)
        self.assertIsNone(ctrl._halt_reason)

    def test_resume_after_halt_is_a_noop_when_not_halted(self):
        ctrl = ExcitationController(schedule=_tiny_schedule())
        ctrl.resume_after_halt()  # must not raise or change state
        self.assertEqual(ctrl._state, ExcitationMode.EXCITE_GAP)

    def test_window_resets_on_level_advance_not_carried_over(self):
        """A level that was safe must not inherit a near-breach window
        from the level before it -- and a fresh level starting with a
        few failures should not immediately halt using stale data from
        the PREVIOUS level."""
        ctrl = ExcitationController(schedule=_tiny_schedule())
        for _ in range(5):
            ctrl.record_outcome(success=True, was_timeout=False)
        ctrl.maybe_advance()  # advance clears the window (asserted below)
        self.assertEqual(len(ctrl._gonogo.outcomes), 0)


class TestPersistence(unittest.TestCase):

    def test_round_trip_preserves_mode_and_position(self):
        ctrl = ExcitationController(schedule=_tiny_schedule())
        ctrl._entry_idx = 1
        ctrl._level_idx = 1
        ctrl._state = ExcitationMode.EXCITE_POLL
        data = ctrl.to_persisted_dict()
        restored = ExcitationController.from_persisted_dict(data, schedule=_tiny_schedule())
        self.assertEqual(restored._state, ExcitationMode.EXCITE_POLL)
        self.assertEqual(restored._entry_idx, 1)
        self.assertEqual(restored._level_idx, 1)

    def test_transaction_count_and_window_never_trusted_across_restart(self):
        """A restart must not let pre-restart transactions silently
        satisfy this run's own min-transaction-count requirement -- the
        level's own progress counters reset, even though its position in
        the schedule is preserved."""
        ctrl = ExcitationController(schedule=_tiny_schedule())
        data = ctrl.to_persisted_dict()
        restored = ExcitationController.from_persisted_dict(data, schedule=_tiny_schedule())
        self.assertEqual(restored._level_transaction_count, 0)
        self.assertEqual(len(restored._gonogo.outcomes), 0)

    def test_malformed_data_recovers_without_raising(self):
        bad = {"entry_idx": "not_a_number", "level_idx": 99, "state": "GARBAGE"}
        try:
            restored = ExcitationController.from_persisted_dict(bad, schedule=_tiny_schedule())
        except Exception as exc:  # noqa: BLE001
            self.fail(f"from_persisted_dict raised on malformed input: {exc!r}")
        self.assertIn(restored._state, (ExcitationMode.EXCITE_GAP, ExcitationMode.COMPLETE))

    def test_out_of_range_level_idx_is_clamped_not_crashed(self):
        overflow = {"entry_idx": 0, "level_idx": 99, "state": "EXCITE_GAP"}
        restored = ExcitationController.from_persisted_dict(overflow, schedule=_tiny_schedule())
        self.assertLess(restored._level_idx, len(_tiny_schedule()[0].levels))

    def test_halted_state_round_trips_with_its_reason(self):
        ctrl = ExcitationController(schedule=_tiny_schedule())
        ctrl._state = ExcitationMode.HALTED
        ctrl._halt_reason = "test breach reason"
        data = ctrl.to_persisted_dict()
        restored = ExcitationController.from_persisted_dict(data, schedule=_tiny_schedule())
        self.assertEqual(restored._state, ExcitationMode.HALTED)
        self.assertEqual(restored._halt_reason, "test breach reason")


class TestTelemetrySnapshot(_FastDwellTestCase):

    def test_snapshot_reflects_current_level(self):
        ctrl = ExcitationController(schedule=_tiny_schedule())
        snap = ctrl.telemetry_snapshot()
        self.assertEqual(snap["excitation_mode"], "EXCITE_GAP")
        self.assertEqual(snap["excitation_level_label"], "GAP_LOW")
        self.assertEqual(snap["excitation_commanded_value"], 150.0)

    def test_snapshot_applied_value_none_before_any_apply_call(self):
        ctrl = ExcitationController(schedule=_tiny_schedule())
        snap = ctrl.telemetry_snapshot()
        self.assertIsNone(snap["excitation_applied_value"])

    def test_snapshot_applied_value_set_after_apply(self):
        ctrl = ExcitationController(schedule=_tiny_schedule())
        ctrl.apply(_base_params(), in_transition=False)
        snap = ctrl.telemetry_snapshot()
        self.assertEqual(snap["excitation_applied_value"], 150.0)

    def test_snapshot_applied_value_none_during_transition_override(self):
        """Adversarial: apply() being skipped due to in_transition must
        be visible in telemetry as "nothing was actually applied", not
        silently show the last-commanded value as if it took effect."""
        ctrl = ExcitationController(schedule=_tiny_schedule())
        ctrl.apply(_base_params(), in_transition=False)  # sets a value
        ctrl.apply(_base_params(), in_transition=True)   # transition overrides
        snap = ctrl.telemetry_snapshot()
        self.assertIsNone(snap["excitation_applied_value"])

    def test_snapshot_includes_halt_reason_when_halted(self):
        ctrl = ExcitationController(schedule=_tiny_schedule())
        ctrl._state = ExcitationMode.HALTED
        ctrl._halt_reason = "manual test reason"
        snap = ctrl.telemetry_snapshot()
        self.assertEqual(snap["excitation_halt_reason"], "manual test reason")


if __name__ == "__main__":
    unittest.main()
