"""Tests for RandomExcitationController (2.0.15.4 broadband random
excitation release).

See random_excitation_controller.py's own module docstring for the full
design and reasoning: a deliberately separate, much simpler mechanism
from ExcitationController (2.0.15/2.0.15b/2.0.15.3), answering a
different question -- edge-transition and joint-lever behavior the
sequential, long-dwell schedule structurally cannot observe.

Real execution throughout -- both this module and adaptive_modbus.py
load cleanly in this environment, matching the established convention
for every prior excitation-related test file this project has written.
"""
from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock

from .. import random_excitation_controller as rec
from ..random_excitation_controller import (
    GAP_MAX_MS,
    GAP_MIN_MS,
    POLL_MAX_S,
    POLL_MIN_S,
    RandomExcitationController,
    REDRAW_INTERVAL,
    TIMEOUT_MAX_S,
    TIMEOUT_MIN_S,
)
from ..adaptive_modbus import AdaptiveModbusController, AdaptiveParams
from datetime import timedelta


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


class TestRandomDrawBounds(unittest.TestCase):
    """The bounds themselves are the real safety property here -- no
    go/no-go monitor exists to catch an out-of-range value at runtime
    (see module docstring for why that omission is deliberate), so the
    draw function itself staying within bounds matters more than it
    would for ExcitationController, which has a second layer of
    protection this class does not."""

    def test_many_draws_all_stay_within_bounds(self):
        """Statistical, not just single-sample: draws 500 times and
        checks every one, rather than trusting one lucky (or unlucky)
        sample to represent random.uniform()'s own real behavior."""
        for _ in range(500):
            d = rec._new_draw()
            self.assertGreaterEqual(d.gap_ms, GAP_MIN_MS)
            self.assertLessEqual(d.gap_ms, GAP_MAX_MS)
            self.assertGreaterEqual(d.timeout_s, TIMEOUT_MIN_S)
            self.assertLessEqual(d.timeout_s, TIMEOUT_MAX_S)
            self.assertGreaterEqual(d.poll_s, POLL_MIN_S)
            self.assertLessEqual(d.poll_s, POLL_MAX_S)

    def test_bounds_are_within_this_projects_own_already_validated_envelope(self):
        """Pins the actual numbers against const.py's own hard bounds,
        confirmed directly before this module was written -- not
        re-derived here, just pinned so a future edit to either file
        can't silently drift the two apart."""
        self.assertEqual(GAP_MIN_MS, 150.0)
        self.assertEqual(GAP_MAX_MS, 500.0)
        self.assertEqual(TIMEOUT_MIN_S, 20.0)  # the deliberate edge-condition floor, not the earlier 30s used for the sequential releases -- see the module's own docstring for why this release's own purpose changes the trade-off
        self.assertEqual(TIMEOUT_MAX_S, 60.0)
        self.assertEqual(POLL_MIN_S, 30.0)
        self.assertEqual(POLL_MAX_S, 90.0)

    def test_draws_are_not_coupled_to_each_other(self):
        """Adversarial: confirms gap/timeout/poll are drawn
        independently, not from some shared random state that would
        make them move together -- checked directly with random.seed()
        pinned to a known value at a low level would be fragile against
        the CPython PRNG's own implementation details, so this checks
        the more robust property instead: across many draws, the three
        series are not perfectly correlated (a real, if weak,
        statistical check that they are not secretly the same draw
        rescaled three ways)."""
        draws = [rec._new_draw() for _ in range(200)]
        gaps = [d.gap_ms for d in draws]
        timeouts = [d.timeout_s for d in draws]
        # normalize each series to [0,1] and confirm they don't move in lockstep
        def norm(xs, lo, hi):
            return [(x - lo) / (hi - lo) for x in xs]
        ng = norm(gaps, GAP_MIN_MS, GAP_MAX_MS)
        nt = norm(timeouts, TIMEOUT_MIN_S, TIMEOUT_MAX_S)
        # if coupled, differences would be near-zero for every pair; if
        # independent, they should vary substantially across the sample
        diffs = [abs(a - b) for a, b in zip(ng, nt)]
        self.assertGreater(max(diffs), 0.3, "gap and timeout draws look suspiciously coupled")


class TestSharedRegistryAndApply(unittest.TestCase):

    def setUp(self):
        RandomExcitationController.clear_registry()

    def tearDown(self):
        RandomExcitationController.clear_registry()

    def test_same_endpoint_returns_same_instance(self):
        c1 = RandomExcitationController.get_or_create("1.2.3.4:502")
        c2 = RandomExcitationController.get_or_create("1.2.3.4:502")
        self.assertIs(c1, c2)

    def test_different_endpoints_get_different_instances(self):
        c1 = RandomExcitationController.get_or_create("1.2.3.4:502")
        c2 = RandomExcitationController.get_or_create("5.6.7.8:502")
        self.assertIsNot(c1, c2)

    def test_two_devices_sharing_a_bus_apply_identical_values(self):
        """The core property this class exists to guarantee (mirroring
        ExcitationController's own, for the same underlying reason --
        ModbusGuard's own max()-combining of GAP across every device on
        a bus): two devices must draw and apply the SAME values at the
        SAME moment, not independently."""
        ctrl = RandomExcitationController.get_or_create("shared:502")
        base = _base_params()
        applied_1 = ctrl.apply(base, in_transition=False)
        applied_2 = ctrl.apply(base, in_transition=False)
        self.assertEqual(applied_1.request_gap, applied_2.request_gap)
        self.assertEqual(applied_1.request_timeout, applied_2.request_timeout)
        self.assertEqual(applied_1.poll_interval, applied_2.poll_interval)

    def test_apply_overrides_gap_timeout_and_poll_only(self):
        """Adversarial: max_queue_depth and confidence must be left
        completely untouched -- this release's own randomization scope
        is deliberately GAP/TIMEOUT/POLL only, confirmed directly here
        rather than assumed from the module docstring alone."""
        ctrl = RandomExcitationController.get_or_create("1.2.3.4:502")
        base = _base_params(max_queue_depth=3, confidence=0.42)
        applied = ctrl.apply(base, in_transition=False)
        self.assertEqual(applied.max_queue_depth, 3)
        self.assertEqual(applied.confidence, 0.42)
        self.assertEqual(applied.slot_index, base.slot_index)

    def test_in_transition_always_returns_base_unchanged(self):
        """The one safety net this release does NOT touch or weaken --
        confirmed directly, not just documented."""
        ctrl = RandomExcitationController.get_or_create("1.2.3.4:502")
        base = _base_params()
        applied = ctrl.apply(base, in_transition=True)
        self.assertEqual(applied, base)

    def test_applied_values_within_declared_bounds(self):
        ctrl = RandomExcitationController.get_or_create("1.2.3.4:502")
        applied = ctrl.apply(_base_params(), in_transition=False)
        gap_ms = applied.request_gap.total_seconds() * 1000
        timeout_s = applied.request_timeout.total_seconds()
        poll_s = applied.poll_interval.total_seconds()
        self.assertGreaterEqual(gap_ms, GAP_MIN_MS)
        self.assertLessEqual(gap_ms, GAP_MAX_MS)
        self.assertGreaterEqual(timeout_s, TIMEOUT_MIN_S)
        self.assertLessEqual(timeout_s, TIMEOUT_MAX_S)
        self.assertGreaterEqual(poll_s, POLL_MIN_S)
        self.assertLessEqual(poll_s, POLL_MAX_S)


class TestRedrawTiming(unittest.TestCase):

    def setUp(self):
        RandomExcitationController.clear_registry()

    def tearDown(self):
        RandomExcitationController.clear_registry()

    def test_no_redraw_before_interval_elapses(self):
        ctrl = RandomExcitationController.get_or_create("1.2.3.4:502")
        first = ctrl._draw
        ctrl.maybe_advance()
        self.assertIs(ctrl._draw, first)

    def test_redraw_happens_once_interval_has_elapsed(self):
        ctrl = RandomExcitationController.get_or_create("1.2.3.4:502")
        first = ctrl._draw
        ctrl._draw_start_mono = time.monotonic() - REDRAW_INTERVAL.total_seconds() - 1
        ctrl.maybe_advance()
        self.assertIsNot(ctrl._draw, first)

    def test_redraw_resets_the_clock(self):
        ctrl = RandomExcitationController.get_or_create("1.2.3.4:502")
        ctrl._draw_start_mono = time.monotonic() - REDRAW_INTERVAL.total_seconds() - 1
        ctrl.maybe_advance()
        elapsed = time.monotonic() - ctrl._draw_start_mono
        self.assertLess(elapsed, 1.0)

    def test_calling_maybe_advance_repeatedly_before_interval_is_harmless(self):
        """Matches ExcitationController's own established convention:
        get_params() may call this once per real device poll, but real
        devices sharing a bus can trigger multiple get_params() calls
        close together -- repeated calls before the interval elapses
        must never cause more than one redraw."""
        ctrl = RandomExcitationController.get_or_create("1.2.3.4:502")
        first = ctrl._draw
        for _ in range(20):
            ctrl.maybe_advance()
        self.assertIs(ctrl._draw, first)


class TestNoGoNoGoMonitor(unittest.TestCase):
    """The deliberate omission this release is built on -- see module
    docstring for the full reasoning (read-only telemetry collection;
    halting on exactly the instability this release exists to observe
    would discard the most valuable data it could produce)."""

    def setUp(self):
        RandomExcitationController.clear_registry()

    def tearDown(self):
        RandomExcitationController.clear_registry()

    def test_record_outcome_never_raises_regardless_of_outcome(self):
        ctrl = RandomExcitationController.get_or_create("1.2.3.4:502")
        try:
            for _ in range(500):
                ctrl.record_outcome(success=False, was_timeout=True)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"record_outcome() raised: {exc!r}")

    def test_repeated_failures_never_change_apply_behavior(self):
        """Adversarial: confirms there is genuinely no hidden state
        that record_outcome() could be building toward a halt with --
        apply() must keep applying draws exactly the same way after
        500 recorded failures as before any."""
        ctrl = RandomExcitationController.get_or_create("1.2.3.4:502")
        base = _base_params()
        applied_before = ctrl.apply(base, in_transition=False)
        for _ in range(500):
            ctrl.record_outcome(success=False, was_timeout=True)
        applied_after = ctrl.apply(base, in_transition=False)
        # same draw (no redraw triggered by record_outcome, only by
        # maybe_advance()'s own timer) -> same applied values
        self.assertEqual(applied_before.request_gap, applied_after.request_gap)
        self.assertEqual(applied_before.request_timeout, applied_after.request_timeout)
        self.assertEqual(applied_before.poll_interval, applied_after.poll_interval)

    def test_no_state_attribute_exists_at_all(self):
        """Adversarial: confirms this is not just "always returns
        False" logic hiding an unused _state attribute -- the concept
        genuinely does not exist on this class, unlike
        ExcitationController."""
        ctrl = RandomExcitationController.get_or_create("1.2.3.4:502")
        self.assertFalse(hasattr(ctrl, "_state"))
        self.assertFalse(hasattr(ctrl, "_gonogo"))
        self.assertFalse(hasattr(ctrl, "_halt_reason"))


class TestNoPersistence(unittest.TestCase):
    """The other deliberate omission -- see module docstring: every
    10-minute window is independent, so there is no "progress" a
    restart needs to preserve."""

    def test_no_to_persisted_dict_method_exists(self):
        ctrl = RandomExcitationController()
        self.assertFalse(hasattr(ctrl, "to_persisted_dict"))

    def test_no_from_persisted_dict_classmethod_exists(self):
        self.assertFalse(hasattr(RandomExcitationController, "from_persisted_dict"))
        self.assertFalse(hasattr(RandomExcitationController, "get_or_restore"))


class TestTelemetrySnapshot(unittest.TestCase):

    def setUp(self):
        RandomExcitationController.clear_registry()

    def tearDown(self):
        RandomExcitationController.clear_registry()

    def test_snapshot_reflects_the_current_draw(self):
        ctrl = RandomExcitationController.get_or_create("1.2.3.4:502")
        snap = ctrl.telemetry_snapshot()
        self.assertEqual(snap["random_excitation_draw_gap_ms"], ctrl._draw.gap_ms)
        self.assertEqual(snap["random_excitation_draw_timeout_s"], ctrl._draw.timeout_s)
        self.assertEqual(snap["random_excitation_draw_poll_s"], ctrl._draw.poll_s)

    def test_applied_values_none_before_any_apply_call(self):
        ctrl = RandomExcitationController.get_or_create("1.2.3.4:502")
        snap = ctrl.telemetry_snapshot()
        self.assertIsNone(snap["random_excitation_applied_gap_ms"])

    def test_applied_values_set_after_apply(self):
        ctrl = RandomExcitationController.get_or_create("1.2.3.4:502")
        ctrl.apply(_base_params(), in_transition=False)
        snap = ctrl.telemetry_snapshot()
        self.assertEqual(snap["random_excitation_applied_gap_ms"], ctrl._draw.gap_ms)

    def test_applied_values_none_during_transition_override(self):
        """Adversarial: matches ExcitationController's own established
        pattern -- apply() being skipped due to in_transition must be
        visible in telemetry as "nothing was actually applied", not
        silently show the last-commanded value as if it took effect."""
        ctrl = RandomExcitationController.get_or_create("1.2.3.4:502")
        ctrl.apply(_base_params(), in_transition=False)  # sets applied values
        ctrl.apply(_base_params(), in_transition=True)   # transition overrides
        snap = ctrl.telemetry_snapshot()
        self.assertIsNone(snap["random_excitation_applied_gap_ms"])
        self.assertIsNone(snap["random_excitation_applied_timeout_s"])
        self.assertIsNone(snap["random_excitation_applied_poll_s"])


class TestAdaptiveModbusControllerIntegration(unittest.TestCase):
    """Real execution through AdaptiveModbusController's own
    enable_excitation()/get_params()/record_request() -- confirms the
    full, real integration works, not just RandomExcitationController
    in isolation."""

    def setUp(self):
        AdaptiveModbusController.clear_registry()
        RandomExcitationController.clear_registry()
        self.hass = MagicMock()

    def tearDown(self):
        AdaptiveModbusController.clear_registry()
        RandomExcitationController.clear_registry()

    def test_enable_excitation_uses_random_excitation_controller(self):
        ctrl = AdaptiveModbusController.get_or_create(
            self.hass, "SN-A", {}, bus_endpoint="1.2.3.4:502"
        )
        ctrl.enable_excitation()
        self.assertIsInstance(ctrl._excitation, RandomExcitationController)

    def test_two_devices_on_the_same_bus_get_identical_params(self):
        endpoint = "shared-bus:502"
        ctrl_a = AdaptiveModbusController.get_or_create(self.hass, "SN-A", {}, bus_endpoint=endpoint)
        ctrl_b = AdaptiveModbusController.get_or_create(self.hass, "SN-B", {}, bus_endpoint=endpoint)
        ctrl_a.enable_excitation()
        ctrl_b.enable_excitation()
        params_a = ctrl_a.get_params()
        params_b = ctrl_b.get_params()
        self.assertEqual(params_a.request_gap, params_b.request_gap)
        self.assertEqual(params_a.request_timeout, params_b.request_timeout)
        self.assertEqual(params_a.poll_interval, params_b.poll_interval)

    def test_learning_disabled_while_enabled(self):
        ctrl = AdaptiveModbusController.get_or_create(
            self.hass, "SN-A", {}, bus_endpoint="1.2.3.4:502"
        )
        assert ctrl.learning_enabled
        ctrl.enable_excitation()
        self.assertFalse(ctrl.learning_enabled)

    def test_disable_excitation_reenables_learning(self):
        ctrl = AdaptiveModbusController.get_or_create(
            self.hass, "SN-A", {}, bus_endpoint="1.2.3.4:502"
        )
        ctrl.enable_excitation()
        ctrl.disable_excitation()
        self.assertTrue(ctrl.learning_enabled)
        self.assertIsNone(ctrl._excitation)

    def test_record_request_never_halts_regardless_of_failures(self):
        """The real, end-to-end version of TestNoGoNoGoMonitor's own
        isolated check -- through the actual record_request() path a
        real coordinator would use."""
        ctrl = AdaptiveModbusController.get_or_create(
            self.hass, "SN-A", {}, bus_endpoint="1.2.3.4:502"
        )
        ctrl.enable_excitation()
        for _ in range(200):
            ctrl.record_request(rtt_ms=9999, success=False, timeout=True)
        self.assertFalse(ctrl.excitation_is_halted())
        self.assertTrue(ctrl.excitation_is_enabled())

    def test_serialize_never_includes_an_excitation_key(self):
        """No persistence, confirmed through the real _serialize() path
        -- not just that the method is absent on the controller class."""
        ctrl = AdaptiveModbusController.get_or_create(
            self.hass, "SN-A", {}, bus_endpoint="1.2.3.4:502"
        )
        ctrl.enable_excitation()
        serialized = ctrl._serialize()
        self.assertNotIn("excitation", serialized)

    def test_legacy_persisted_excitation_data_is_discarded_not_restored(self):
        """The real safety fix: a device upgraded in place from 2.0.15.3
        without clearing storage must not silently get the OLD
        sequential ExcitationController restored -- self._excitation
        must stay None until enable_excitation() is called fresh."""
        ctrl = AdaptiveModbusController.get_or_create(
            self.hass, "SN-A", {}, bus_endpoint="1.2.3.4:502"
        )
        legacy_raw = {
            "version": 1, "data_schema": 1, "serial": "SN-A",
            "last_decay_date": "2026-01-01", "learning_enabled": True,
            "suppressed_observations": 0, "settling_events": 0,
            "first_data_date": "2026-01-01", "slots": {},
            "excitation": {
                "entry_idx": 0, "level_idx": 1,
                "state": "EXCITE_GAP", "halt_reason": None,
            },
        }
        ctrl._deserialize(legacy_raw)
        self.assertIsNone(ctrl._excitation)


if __name__ == "__main__":
    unittest.main()
