"""Tests for v2.0.15.3's own real fixes, all traced to a genuine 3-day
field-run failure:

  1. ExcitationController is now shared per bus_endpoint, not per
     device -- the original per-device design meant one device's own
     halt (never auto-resumed, since that didn't exist yet either)
     silently vetoed a sibling device's own, genuinely correct GAP
     excitation via ModbusGuard's own max()-across-devices combining.
     Confirmed directly against a real capture: the physical bus gap
     was 500ms for 100% of a 44-hour run despite one device correctly
     requesting 150/325/500ms throughout.
  2. Auto-resume after a cooldown, with a bounded per-mode retry limit,
     closes the specific gap that let the halt above go unnoticed and
     unresolved for the remaining ~43 hours of that same run.
  3. Excitation state (mode, halt reason, time halted, auto-resume
     count) is now visible in snapshot()/telemetry -- the original
     ExcitationController.telemetry_snapshot() existed since 2.0.15 but
     was never wired into anything at all.

Real execution throughout -- ExcitationController and AdaptiveModbus
Controller both load cleanly in this environment (matching the
established convention for test_excitation_controller.py and test_
elevated_permissions_helper.py), so this is not source-level inference.
"""
from __future__ import annotations

from datetime import timedelta
import time
import unittest
from unittest.mock import MagicMock

from ..adaptive_modbus import AdaptiveModbusController
from .. import excitation_controller as ec
from ..excitation_controller import (
    ExcitationController,
    ExcitationLevel,
    ExcitationMode,
    ExcitationScheduleEntry,
)


def _tiny_schedule() -> tuple[ExcitationScheduleEntry, ...]:
    return (
        ExcitationScheduleEntry(ExcitationMode.EXCITE_GAP, (
            ExcitationLevel(150.0, "GAP_LOW"),
        )),
    )


class _FastTestCase(unittest.TestCase):
    """Monkeypatches dwell/count/window/cooldown to small, fast values
    for the duration of each test -- matching the established pattern
    in test_excitation_controller.py's own _FastDwellTestCase."""

    def setUp(self):
        self._orig_dwell = ec._MIN_LEVEL_DWELL
        self._orig_count = ec._MIN_LEVEL_TRANSACTIONS
        self._orig_window = ec._GONOGO_WINDOW_TRANSACTIONS
        ec._MIN_LEVEL_DWELL = timedelta(seconds=0)
        ec._MIN_LEVEL_TRANSACTIONS = 5
        ec._GONOGO_WINDOW_TRANSACTIONS = 20
        AdaptiveModbusController.clear_registry()
        ExcitationController.clear_registry()

    def tearDown(self):
        ec._MIN_LEVEL_DWELL = self._orig_dwell
        ec._MIN_LEVEL_TRANSACTIONS = self._orig_count
        ec._GONOGO_WINDOW_TRANSACTIONS = self._orig_window
        AdaptiveModbusController.clear_registry()
        ExcitationController.clear_registry()


class TestSharedRegistry(_FastTestCase):

    def test_same_endpoint_returns_same_instance(self):
        c1 = ExcitationController.get_or_create("192.168.1.1:502", _tiny_schedule())
        c2 = ExcitationController.get_or_create("192.168.1.1:502", _tiny_schedule())
        self.assertIs(c1, c2)

    def test_different_endpoints_get_different_instances(self):
        c1 = ExcitationController.get_or_create("192.168.1.1:502", _tiny_schedule())
        c2 = ExcitationController.get_or_create("192.168.1.2:502", _tiny_schedule())
        self.assertIsNot(c1, c2)

    def test_two_devices_on_same_bus_request_the_same_level(self):
        """The core defect this release fixes: two independent per-
        device schedules could request different GAP levels, and
        ModbusGuard's own max()-combining would silently discard
        whichever was lower. A shared controller makes this
        structurally impossible, not just less likely."""
        hass = MagicMock()
        endpoint = "192.168.7.22:502"
        ctrl_a = AdaptiveModbusController.get_or_create(hass, "SN-A", {}, bus_endpoint=endpoint)
        ctrl_b = AdaptiveModbusController.get_or_create(hass, "SN-B", {}, bus_endpoint=endpoint)
        ctrl_a.enable_excitation()
        ctrl_b.enable_excitation()
        self.assertIs(ctrl_a._excitation, ctrl_b._excitation)
        self.assertEqual(
            ctrl_a.snapshot()["gap_requested_ms"],
            ctrl_b.snapshot()["gap_requested_ms"],
        )

    def test_disable_on_one_device_does_not_affect_the_other(self):
        hass = MagicMock()
        endpoint = "192.168.7.22:502"
        ctrl_a = AdaptiveModbusController.get_or_create(hass, "SN-A", {}, bus_endpoint=endpoint)
        ctrl_b = AdaptiveModbusController.get_or_create(hass, "SN-B", {}, bus_endpoint=endpoint)
        ctrl_a.enable_excitation()
        ctrl_b.enable_excitation()
        shared = ctrl_b._excitation

        ctrl_a.disable_excitation()

        self.assertIsNone(ctrl_a._excitation)
        self.assertIs(ctrl_b._excitation, shared)
        self.assertIs(ExcitationController.get_or_create(endpoint), shared)

    def test_gonogo_monitor_aggregates_outcomes_from_both_devices(self):
        hass = MagicMock()
        endpoint = "192.168.7.22:502"
        ctrl_a = AdaptiveModbusController.get_or_create(hass, "SN-A", {}, bus_endpoint=endpoint)
        ctrl_b = AdaptiveModbusController.get_or_create(hass, "SN-B", {}, bus_endpoint=endpoint)
        ctrl_a.enable_excitation()
        ctrl_b.enable_excitation()

        for _ in range(10):
            ctrl_a.record_request(rtt_ms=10, success=True, timeout=False)
        for _ in range(10):
            ctrl_b.record_request(rtt_ms=10, success=True, timeout=False)

        self.assertEqual(len(ctrl_a._excitation._gonogo.outcomes), 20)

    def test_enable_excitation_falls_back_to_private_instance_without_endpoint(self):
        """A device with no known bus_endpoint must not crash or
        silently no-op -- it gets a private, non-shared schedule
        exactly as this release's own predecessor always did."""
        hass = MagicMock()
        ctrl = AdaptiveModbusController.get_or_create(hass, "SN-NO-ENDPOINT", {})
        ctrl.enable_excitation()
        self.assertIsNotNone(ctrl._excitation)
        self.assertEqual(len(ExcitationController._registry), 0)


class TestGetOrRestore(_FastTestCase):
    """The registry-aware restoration path -- without this, the second
    of two devices restoring independently from their own persisted
    data would silently create a second, disconnected controller for
    the same bus_endpoint, defeating the sharing fix at exactly the
    moment (a restart) it exists to protect."""

    def test_first_restoration_creates_and_registers(self):
        endpoint = "192.168.1.1:502"
        persisted = {"entry_idx": 0, "level_idx": 0, "state": "EXCITE_GAP", "halt_reason": None}
        restored = ExcitationController.get_or_restore(endpoint, persisted, _tiny_schedule())
        self.assertIs(ExcitationController.get_or_create(endpoint, _tiny_schedule()), restored)

    def test_second_independent_restoration_reuses_the_first(self):
        endpoint = "192.168.1.1:502"
        persisted = {"entry_idx": 0, "level_idx": 0, "state": "EXCITE_GAP", "halt_reason": None}
        restored1 = ExcitationController.get_or_restore(endpoint, persisted, _tiny_schedule())
        # A second, independent call -- as would happen from a second
        # device's own _deserialize(), with its own separately-stored
        # (here, identical) persisted data.
        restored2 = ExcitationController.get_or_restore(endpoint, persisted, _tiny_schedule())
        self.assertIs(restored1, restored2)

    def test_restored_halted_state_gets_a_fresh_halt_mono(self):
        """Adversarial: a restored HALTED state must not leave
        _halt_mono as None, or the very next maybe_advance() call
        would fail _maybe_auto_resume()'s own assertion."""
        endpoint = "192.168.1.1:502"
        persisted = {"entry_idx": 0, "level_idx": 0, "state": "HALTED", "halt_reason": "prior breach"}
        restored = ExcitationController.get_or_restore(endpoint, persisted, _tiny_schedule())
        self.assertIsNotNone(restored._halt_mono)
        try:
            restored.maybe_advance()
        except Exception as exc:  # noqa: BLE001
            self.fail(f"maybe_advance() raised on a restored halted state: {exc!r}")


class TestAutoResume(_FastTestCase):
    """The specific gap that let a single early halt silently waste an
    entire unattended 44-hour run."""

    def _halted_ctrl(self) -> ExcitationController:
        ctrl = ExcitationController(schedule=_tiny_schedule())
        ctrl._HALT_COOLDOWN = timedelta(seconds=0.15)
        ctrl._MAX_AUTO_RESUME_ATTEMPTS_PER_MODE = 2
        for _ in range(19):
            ctrl.record_outcome(success=True, was_timeout=False)
        for _ in range(2):
            ctrl.record_outcome(success=False, was_timeout=False)
        self.assertEqual(ctrl._state, ExcitationMode.HALTED)
        return ctrl

    def test_does_not_auto_resume_before_cooldown_elapses(self):
        ctrl = self._halted_ctrl()
        ctrl.maybe_advance()
        self.assertEqual(ctrl._state, ExcitationMode.HALTED)

    def test_auto_resumes_after_cooldown(self):
        ctrl = self._halted_ctrl()
        time.sleep(0.2)
        ctrl.maybe_advance()
        self.assertEqual(ctrl._state, ExcitationMode.EXCITE_GAP)
        self.assertEqual(ctrl._auto_resume_count_this_mode, 1)

    def test_auto_resume_restarts_at_level_zero_same_as_manual(self):
        ctrl = self._halted_ctrl()
        time.sleep(0.2)
        ctrl.maybe_advance()
        self.assertEqual(ctrl._level_idx, 0)

    def test_stops_auto_resuming_after_budget_exhausted(self):
        ctrl = self._halted_ctrl()
        for _ in range(2):  # matches _MAX_AUTO_RESUME_ATTEMPTS_PER_MODE
            time.sleep(0.2)
            ctrl.maybe_advance()
            for _ in range(19):
                ctrl.record_outcome(success=True, was_timeout=False)
            for _ in range(2):
                ctrl.record_outcome(success=False, was_timeout=False)
        self.assertEqual(ctrl._state, ExcitationMode.HALTED)
        time.sleep(0.2)
        ctrl.maybe_advance()  # 3rd attempt -- budget exhausted, must NOT resume
        self.assertEqual(ctrl._state, ExcitationMode.HALTED)

    def test_manual_resume_still_works_after_budget_exhausted(self):
        ctrl = self._halted_ctrl()
        ctrl._auto_resume_count_this_mode = ctrl._MAX_AUTO_RESUME_ATTEMPTS_PER_MODE
        ctrl.resume_after_halt()
        self.assertEqual(ctrl._state, ExcitationMode.EXCITE_GAP)

    def test_attempt_counter_resets_on_genuine_mode_advance(self):
        """A mode that never halted before must get its own full
        attempt budget, not one inherited from an earlier mode that
        struggled."""
        schedule = (
            ExcitationScheduleEntry(ExcitationMode.EXCITE_GAP, (
                ExcitationLevel(150.0, "GAP_LOW"),
            )),
            ExcitationScheduleEntry(ExcitationMode.EXCITE_POLL, (
                ExcitationLevel(30.0, "POLL_LOW"),
            )),
        )
        ctrl = ExcitationController(schedule=schedule)
        ctrl._auto_resume_count_this_mode = 3  # simulate exhausted budget in GAP mode
        for _ in range(5):
            ctrl.record_outcome(success=True, was_timeout=False)
        ctrl.maybe_advance()  # advances GAP_LOW -> EXCITE_POLL (only 1 level in GAP)
        self.assertEqual(ctrl._state, ExcitationMode.EXCITE_POLL)
        self.assertEqual(ctrl._auto_resume_count_this_mode, 0)


class TestSnapshotVisibility(_FastTestCase):
    """ExcitationController.telemetry_snapshot() has existed since the
    original 2.0.15 release but was never called from anywhere at all
    -- confirmed directly in this release's own audit. Closed here."""

    def test_excitation_fields_present_when_enabled(self):
        hass = MagicMock()
        ctrl = AdaptiveModbusController.get_or_create(hass, "SN-VIS", {}, bus_endpoint="1.2.3.4:502")
        ctrl.enable_excitation()
        snap = ctrl.snapshot()
        self.assertEqual(snap["excitation_mode"], "EXCITE_GAP")
        self.assertIsNone(snap["excitation_halted_for_s"])
        self.assertEqual(snap["excitation_auto_resume_count_this_mode"], 0)

    def test_excitation_fields_absent_when_never_enabled(self):
        """Adversarial: no excitation_* keys should appear at all for a
        device that never opted in -- not present-but-None, genuinely
        absent, so a capture-format check can distinguish "never
        enabled" from "enabled but not currently excited"."""
        hass = MagicMock()
        ctrl = AdaptiveModbusController.get_or_create(hass, "SN-NEVER", {})
        snap = ctrl.snapshot()
        self.assertNotIn("excitation_mode", snap)

    def test_halted_state_visible_with_reason_and_duration(self):
        hass = MagicMock()
        ctrl = AdaptiveModbusController.get_or_create(hass, "SN-HALT-VIS", {}, bus_endpoint="1.2.3.4:502")
        ctrl.enable_excitation()
        ctrl._excitation._state = ExcitationMode.HALTED
        ctrl._excitation._halt_mono = time.monotonic()
        ctrl._excitation._halt_reason = "test breach"
        snap = ctrl.snapshot()
        self.assertEqual(snap["excitation_mode"], "HALTED")
        self.assertEqual(snap["excitation_halt_reason"], "test breach")
        self.assertIsInstance(snap["excitation_halted_for_s"], float)


if __name__ == "__main__":
    unittest.main()
