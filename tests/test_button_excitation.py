"""Tests for ResumeExcitationAfterHaltButtonEntity (button.py).

v2.0.15b FIX (external ICS review, this release): replaces the
resume_excitation_after_halt SERVICE (removed entirely, along with
enable_excitation/disable_excitation) with this standalone entity --
no Developer Tools action required for any part of excitation control
anymore.

button.py has no prior test coverage in this project at all (confirmed
directly -- no existing test_button.py, and no other test file imports
from it). This file does not attempt to retroactively cover the
pre-existing buttons; it covers only what this release adds, matching
this fix's own scope.

Real execution against the actual button.py and adaptive_modbus.py --
both load cleanly in this environment (HA is fully installed), matching
the same convention already used for test_excitation_controller.py and
test_elevated_permissions_helper.py.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import unittest

from ..adaptive_modbus import AdaptiveModbusController
from ..button import ResumeExcitationAfterHaltButtonEntity


def _make_device(serial: str = "SN-BUTTON-TEST") -> MagicMock:
    device = MagicMock()
    device.serial_number = serial
    return device


class TestResumeExcitationAfterHaltButton(unittest.TestCase):

    def setUp(self):
        AdaptiveModbusController.clear_registry()

    def tearDown(self):
        AdaptiveModbusController.clear_registry()

    def test_unique_id_is_serial_scoped(self):
        device = _make_device("SN-UNIQUE-1")
        btn = ResumeExcitationAfterHaltButtonEntity(device, {})
        self.assertEqual(btn._attr_unique_id, "SN-UNIQUE-1_resume_excitation_after_halt")

    def test_two_devices_get_distinct_unique_ids(self):
        """Adversarial: confirms the unique_id is genuinely per-device,
        not accidentally shared or hardcoded."""
        btn_a = ResumeExcitationAfterHaltButtonEntity(_make_device("SN-A"), {})
        btn_b = ResumeExcitationAfterHaltButtonEntity(_make_device("SN-B"), {})
        self.assertNotEqual(btn_a._attr_unique_id, btn_b._attr_unique_id)

    def test_press_with_no_controller_registered_does_not_raise(self):
        """The device has no AdaptiveModbusController at all (e.g.
        excitation was never enabled for it) -- must be a safe no-op,
        not an AttributeError from calling a method on None."""
        device = _make_device("SN-NEVER-REGISTERED")
        btn = ResumeExcitationAfterHaltButtonEntity(device, {})
        try:
            asyncio.run(btn.async_press())
        except Exception as exc:  # noqa: BLE001
            self.fail(f"async_press() raised with no controller registered: {exc!r}")

    def test_press_when_excitation_never_enabled_does_not_raise(self):
        """A controller exists (e.g. from normal adaptive learning) but
        excitation was never enabled on it -- resume_excitation_after_
        halt() must handle this as a safe no-op too."""
        device = _make_device("SN-NOT-EXCITED")
        AdaptiveModbusController.get_or_create(MagicMock(), device.serial_number, {})
        btn = ResumeExcitationAfterHaltButtonEntity(device, {})
        try:
            asyncio.run(btn.async_press())
        except Exception as exc:  # noqa: BLE001
            self.fail(f"async_press() raised when excitation was never enabled: {exc!r}")

    def test_press_when_enabled_but_not_halted_is_a_noop(self):
        device = _make_device("SN-ENABLED-NOT-HALTED")
        ctrl = AdaptiveModbusController.get_or_create(MagicMock(), device.serial_number, {})
        ctrl.enable_excitation()
        btn = ResumeExcitationAfterHaltButtonEntity(device, {})
        asyncio.run(btn.async_press())
        self.assertTrue(ctrl.excitation_is_enabled())
        self.assertFalse(ctrl.excitation_is_halted())

    def test_press_resumes_a_genuinely_halted_schedule(self):
        """The real, positive case: a halted schedule genuinely resumes
        when this button is pressed."""
        device = _make_device("SN-HALTED")
        ctrl = AdaptiveModbusController.get_or_create(MagicMock(), device.serial_number, {})
        ctrl.enable_excitation()
        # Force a halt directly, mirroring what the go/no-go monitor
        # would do on a real breach.
        ctrl._excitation._state = ctrl._excitation._state.__class__.HALTED
        ctrl._excitation._halt_reason = "test-induced halt"
        self.assertTrue(ctrl.excitation_is_halted())

        btn = ResumeExcitationAfterHaltButtonEntity(device, {})
        asyncio.run(btn.async_press())

        self.assertFalse(ctrl.excitation_is_halted())
        self.assertTrue(ctrl.excitation_is_enabled())  # resumed, not disabled

    def test_press_targets_only_its_own_device(self):
        """Adversarial: pressing one device's own button must never
        affect a different device's own excitation state, even if both
        are registered and halted at the same time."""
        device_a = _make_device("SN-TARGET-A")
        device_b = _make_device("SN-TARGET-B")
        ctrl_a = AdaptiveModbusController.get_or_create(MagicMock(), "SN-TARGET-A", {})
        ctrl_b = AdaptiveModbusController.get_or_create(MagicMock(), "SN-TARGET-B", {})
        for ctrl in (ctrl_a, ctrl_b):
            ctrl.enable_excitation()
            ctrl._excitation._state = ctrl._excitation._state.__class__.HALTED
            ctrl._excitation._halt_reason = "test-induced halt"

        btn_a = ResumeExcitationAfterHaltButtonEntity(device_a, {})
        asyncio.run(btn_a.async_press())

        self.assertFalse(ctrl_a.excitation_is_halted())
        self.assertTrue(ctrl_b.excitation_is_halted())  # untouched


if __name__ == "__main__":
    unittest.main()
