"""Regression test for a real bug caught against a live capture, not by
any prior test: AdaptiveModbusController.snapshot()'s own max_queue_
depth_effective field read ModbusGuard.queue_depth (live, current queue
OCCUPANCY -- fluctuates with real traffic) instead of the actual
min()-combined CEILING that field's own name promises. Confirmed
directly: a real capture showed max_queue_depth_effective fluctuating
0-1 against a requested ceiling of 3 -- two genuinely unrelated,
differently-scaled quantities.

Unlike gap_effective_ms (a genuine max()-combine of the SAME quantity
as gap_requested_ms), queue_depth and max_queue_depth were always two
separate attributes on ModbusGuard with two separate meanings; the bug
was picking the wrong one of the two, not a semantic ambiguity in the
guard itself. Fixed by adding ModbusGuard.effective_max_queue_depth
(exposing the real ceiling) and switching snapshot() to use it.

Real execution -- both files load cleanly in this environment.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from ..adaptive_modbus import AdaptiveModbusController
from ..modbus_guard import ModbusGuard


class TestQueueDepthEffectiveIsTheRealCeiling(unittest.TestCase):

    def setUp(self):
        AdaptiveModbusController.clear_registry()
        ModbusGuard.clear_registry()
        self.hass = MagicMock()
        self.endpoint = "1.2.3.4:502"

    def tearDown(self):
        AdaptiveModbusController.clear_registry()
        ModbusGuard.clear_registry()

    def test_effective_reports_the_min_combined_ceiling_not_live_occupancy(self):
        """The exact regression: with live occupancy and the combined
        ceiling deliberately set to DIFFERENT values, snapshot() must
        report the ceiling, not occupancy."""
        ctrl = AdaptiveModbusController.get_or_create(
            self.hass, "SN-QD", {}, bus_endpoint=self.endpoint
        )
        guard = ModbusGuard.get_or_create(self.endpoint)
        guard.update_max_queue_depth("SN-QD", 3)
        guard._queue_depth = 1  # live occupancy, deliberately different from the ceiling

        snap = ctrl.snapshot()
        self.assertEqual(snap["max_queue_depth_effective"], 3)
        self.assertNotEqual(snap["max_queue_depth_effective"], guard.queue_depth)

    def test_min_combining_across_two_devices_reflected_correctly(self):
        """Confirms the ceiling reported is genuinely the min()-combined
        value across devices sharing the bus, matching how gap's own
        max()-combining is already tested."""
        ctrl_a = AdaptiveModbusController.get_or_create(
            self.hass, "SN-A", {}, bus_endpoint=self.endpoint
        )
        guard = ModbusGuard.get_or_create(self.endpoint)
        guard.update_max_queue_depth("SN-A", 3)
        guard.update_max_queue_depth("SN-B", 2)

        snap = ctrl_a.snapshot()
        self.assertEqual(snap["max_queue_depth_effective"], 2)

    def test_effective_max_queue_depth_property_exists_and_is_distinct(self):
        """Adversarial: pins that ModbusGuard now exposes the ceiling
        under its OWN, correctly-named property, separate from the
        pre-existing queue_depth (occupancy) property -- not that one
        property's own meaning was silently redefined. Uses 3 (the
        actual MAX_QUEUE_DEPTH constant), not an out-of-range value --
        update_max_queue_depth() correctly clamps to [1, MAX_QUEUE_
        DEPTH], and asserting against an already-clamped value would
        test the clamp, not this property."""
        guard = ModbusGuard.get_or_create(self.endpoint)
        guard.update_max_queue_depth("SN-X", 3)
        guard._queue_depth = 2
        self.assertEqual(guard.effective_max_queue_depth, 3)
        self.assertEqual(guard.queue_depth, 2)
        self.assertNotEqual(guard.effective_max_queue_depth, guard.queue_depth)


if __name__ == "__main__":
    unittest.main()
