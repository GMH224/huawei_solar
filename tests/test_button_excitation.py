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
from ..excitation_controller import ExcitationController


def _make_device(serial: str = "SN-BUTTON-TEST") -> MagicMock:
    device = MagicMock()
    device.serial_number = serial
    return device


class TestResumeExcitationAfterHaltButton(unittest.TestCase):

    def setUp(self):
        AdaptiveModbusController.clear_registry()
        ExcitationController.clear_registry()

    def tearDown(self):
        AdaptiveModbusController.clear_registry()
        ExcitationController.clear_registry()

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

    def test_press_is_always_a_safe_noop_even_with_a_halted_looking_controller(self):
        """v2.0.15.4: resume_excitation_after_halt() is now an
        UNCONDITIONAL no-op (see that method's own docstring in
        adaptive_modbus.py) -- it does not call anything on self.
        _excitation at all, regardless of what class is assigned or
        what state it claims to be in. This test replaces the prior
        release's own "press genuinely resumes a halt" test, which is
        no longer true for this release by design: RandomExcitationController
        has no halt concept, and even a manually-assigned, halted-
        looking ExcitationController is left completely untouched by a
        press in this release. Confirms that explicitly, rather than
        silently losing coverage of this method's own new behavior.
        """
        device = _make_device("SN-HALTED")
        ctrl = AdaptiveModbusController.get_or_create(MagicMock(), device.serial_number, {})
        ctrl._excitation = ExcitationController.get_or_create(device.serial_number)
        ctrl._excitation._state = ctrl._excitation._state.__class__.HALTED
        ctrl._excitation._halt_reason = "test-induced halt"
        self.assertTrue(ctrl._excitation._state.value == "HALTED")

        btn = ResumeExcitationAfterHaltButtonEntity(device, {})
        asyncio.run(btn.async_press())

        # Still halted -- the press did nothing to it, by this
        # release's own design, not because the resume logic failed.
        self.assertTrue(ctrl._excitation._state.value == "HALTED")
        self.assertTrue(ctrl.excitation_is_enabled())  # still enabled, untouched either way

    def test_press_never_affects_a_different_devices_own_excitation(self):
        """Adversarial: pressing one device's own button must never
        affect a different device's own excitation state -- true for
        this release too, even though the mechanism (an unconditional
        no-op) is different from why it was true before (a real,
        per-device-shared resume that only ever targeted its own bus
        endpoint).
        """
        device_a = _make_device("SN-TARGET-A")
        device_b = _make_device("SN-TARGET-B")
        ctrl_a = AdaptiveModbusController.get_or_create(MagicMock(), "SN-TARGET-A", {})
        ctrl_b = AdaptiveModbusController.get_or_create(MagicMock(), "SN-TARGET-B", {})
        ctrl_a._excitation = ExcitationController.get_or_create("SN-TARGET-A")
        ctrl_b._excitation = ExcitationController.get_or_create("SN-TARGET-B")
        for ctrl in (ctrl_a, ctrl_b):
            ctrl._excitation._state = ctrl._excitation._state.__class__.HALTED
            ctrl._excitation._halt_reason = "test-induced halt"

        btn_a = ResumeExcitationAfterHaltButtonEntity(device_a, {})
        asyncio.run(btn_a.async_press())

        # Both remain halted -- device A's own press affects neither,
        # by this release's own design.
        self.assertTrue(ctrl_a._excitation._state.value == "HALTED")
        self.assertTrue(ctrl_b._excitation._state.value == "HALTED")


if __name__ == "__main__":
    unittest.main()
