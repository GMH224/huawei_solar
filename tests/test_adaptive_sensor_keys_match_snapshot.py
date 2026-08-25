"""Regression test for a real, shipped bug: _ADAPTIVE_SENSORS
(adaptive_modbus.py) drifted from snapshot()'s own actual keys after the
2.0.15b telemetry-correctness fix renamed gap_ms/max_queue_depth to
gap_requested_ms/gap_effective_ms/max_queue_depth_requested/max_queue_
depth_effective -- the rename was applied to snapshot() itself but never
propagated to this separate, hardcoded sensor-definition list.

The result: "Adaptive Modbus gap" and "Adaptive queue depth" showed
"Unknown" in the live Home Assistant UI for every installation running
2.0.15b, silently, for as long as it shipped -- caught only when a user
sent a screenshot of their own diagnostics page, not by any test. No
existing test checked this correspondence at all; this file exists
specifically to close that gap so the same class of bug (a key renamed
in one place, not propagated to this list) cannot silently ship again.

Real execution -- both files load cleanly in this environment.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from ..adaptive_modbus import AdaptiveModbusController, _ADAPTIVE_SENSORS
from ..excitation_controller import ExcitationController


class TestAdaptiveSensorKeysMatchSnapshot(unittest.TestCase):

    def setUp(self):
        AdaptiveModbusController.clear_registry()
        ExcitationController.clear_registry()
        self.hass = MagicMock()

    def tearDown(self):
        AdaptiveModbusController.clear_registry()
        ExcitationController.clear_registry()

    def test_every_sensor_key_resolves_with_excitation_enabled(self):
        """The strongest check: with excitation enabled, snapshot()
        includes every key this project currently knows about (the
        base adaptive fields AND the excitation_* fields) -- every
        single _ADAPTIVE_SENSORS entry must be found in that dict, or
        the corresponding HA sensor entity will show "Unknown" exactly
        as reported."""
        ctrl = AdaptiveModbusController.get_or_create(
            self.hass, "SN-SENSOR-CHECK", {}, bus_endpoint="1.2.3.4:502"
        )
        ctrl.enable_excitation()
        snap = ctrl.snapshot()

        missing = [key for key, name, unit, icon in _ADAPTIVE_SENSORS if key not in snap]
        self.assertEqual(
            missing, [],
            f"_ADAPTIVE_SENSORS references key(s) not present in snapshot(): "
            f"{missing} -- these will show 'Unknown' in the live HA UI, "
            f"exactly the bug this test exists to catch.",
        )

    def test_every_non_excitation_sensor_key_resolves_without_excitation(self):
        """The base-case check: a device that never enables excitation
        must still see every one of its OWN (non-excitation_*) sensors
        populated correctly -- confirms the fix didn't only work in the
        excitation-enabled case."""
        ctrl = AdaptiveModbusController.get_or_create(
            self.hass, "SN-SENSOR-CHECK-2", {}, bus_endpoint="1.2.3.5:502"
        )
        snap = ctrl.snapshot()

        base_keys = [key for key, *_ in _ADAPTIVE_SENSORS if not key.startswith("excitation_")]
        missing = [key for key in base_keys if key not in snap]
        self.assertEqual(
            missing, [],
            f"Non-excitation sensor key(s) missing from snapshot() even "
            f"without excitation enabled: {missing}",
        )

    def test_old_removed_keys_are_genuinely_not_referenced(self):
        """Adversarial, pins the exact regression by name: the two old,
        removed key names must never reappear in this list, even
        alongside their correct replacements."""
        referenced_keys = {key for key, *_ in _ADAPTIVE_SENSORS}
        self.assertNotIn("gap_ms", referenced_keys)
        self.assertNotIn("max_queue_depth", referenced_keys)

    def test_gap_and_queue_depth_have_both_requested_and_effective_sensors(self):
        """Confirms the fix didn't just remove the broken references --
        it replaced each one with BOTH of its genuine replacements, not
        arbitrarily picking only one of the two."""
        referenced_keys = {key for key, *_ in _ADAPTIVE_SENSORS}
        for key in (
            "gap_requested_ms", "gap_effective_ms",
            "max_queue_depth_requested", "max_queue_depth_effective",
        ):
            self.assertIn(key, referenced_keys)

    def test_no_duplicate_sensor_keys(self):
        """Adversarial: a duplicated key would create two HA entities
        with colliding unique_ids (controller.serial_number + "_adaptive_"
        + attr_key) -- catch this at the definition level, not at
        entity-registration time."""
        keys = [key for key, *_ in _ADAPTIVE_SENSORS]
        self.assertEqual(len(keys), len(set(keys)), "duplicate sensor key(s) found")


if __name__ == "__main__":
    unittest.main()
