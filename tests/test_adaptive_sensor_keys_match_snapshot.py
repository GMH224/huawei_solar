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
        includes every key relevant to whichever excitation class this
        release's own enable_excitation() actually instantiates.

        v2.0.15.4 NOTE: which class that is is now a per-release
        decision (this release uses RandomExcitationController, not
        ExcitationController -- see random_excitation_controller.py's
        own module docstring). The two classes' own telemetry_
        snapshot() keys are deliberately disjoint (excitation_* vs
        random_excitation_*, confirmed directly when the second set was
        added, specifically to avoid reproducing this exact class of
        bug in a new shape) and BOTH sets coexist permanently in
        _ADAPTIVE_SENSORS -- so only the keys relevant to whichever
        class is actually active are required to be present here; the
        other release's own keys are correctly, accurately absent, not
        a gap this test should flag.
        """
        ctrl = AdaptiveModbusController.get_or_create(
            self.hass, "SN-SENSOR-CHECK", {}, bus_endpoint="1.2.3.4:502"
        )
        ctrl.enable_excitation()
        snap = ctrl.snapshot()

        relevant = [key for key, *_ in _ADAPTIVE_SENSORS if not key.startswith("excitation_")]
        missing = [key for key in relevant if key not in snap]
        self.assertEqual(
            missing, [],
            f"_ADAPTIVE_SENSORS references key(s) not present in snapshot(): "
            f"{missing} -- these will show 'Unknown' in the live HA UI, "
            f"exactly the bug this test exists to catch.",
        )
        # And the legacy ExcitationController-specific keys must be
        # genuinely, correctly absent -- not present-but-None.
        legacy_keys = [key for key, *_ in _ADAPTIVE_SENSORS if key.startswith("excitation_")]
        present_legacy = [key for key in legacy_keys if key in snap]
        self.assertEqual(
            present_legacy, [],
            "legacy ExcitationController-specific keys unexpectedly present "
            "while RandomExcitationController is active",
        )

    def test_every_non_excitation_sensor_key_resolves_without_excitation(self):
        """The base-case check: a device that never enables excitation
        must still see every one of its OWN (non-excitation-specific)
        sensors populated correctly -- confirms the fix didn't only
        work in the excitation-enabled case.

        v2.0.15.4 NOTE: excludes BOTH excitation_* (ExcitationController)
        and random_excitation_* (this release's own RandomExcitationController)
        prefixes -- both are excitation-specific and correctly absent
        for a device that never enabled excitation at all, not "base"
        fields this test should require.
        """
        ctrl = AdaptiveModbusController.get_or_create(
            self.hass, "SN-SENSOR-CHECK-2", {}, bus_endpoint="1.2.3.5:502"
        )
        snap = ctrl.snapshot()

        base_keys = [
            key for key, *_ in _ADAPTIVE_SENSORS
            if not (key.startswith("excitation_") or key.startswith("random_excitation_"))
        ]
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
