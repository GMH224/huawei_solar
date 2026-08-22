"""Tests for the v2.0.15 excitation service handlers (enable_excitation,
disable_excitation, resume_excitation_after_halt) in services.py.

Real execution against the actual services.py and adaptive_modbus.py --
both load cleanly in this environment (HA is fully installed), so this
follows the same real-execution convention as test_excitation_controller.py
rather than the AST-only fallback test_services.py itself uses for cases
where the full HA environment can't be assembled. get_inverter_data() is
patched directly (matching test_services.py's own established "patch the
device-resolution function, not the full HA device registry" pattern) so
these tests exercise the actual service-handler bodies, controller state
transitions, and error paths end to end.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.exceptions import ServiceValidationError

from .. import services
from ..adaptive_modbus import AdaptiveModbusController


def _make_call(device_id: str = "device-abc") -> MagicMock:
    call = MagicMock()
    call.data = {"device_id": device_id}
    return call


def _make_dd(serial: str) -> MagicMock:
    dd = MagicMock()
    dd.device = MagicMock()
    dd.device.serial_number = serial
    return dd


class TestEnableDisableExcitation:

    def setup_method(self):
        AdaptiveModbusController.clear_registry()

    def teardown_method(self):
        AdaptiveModbusController.clear_registry()

    def test_enable_excitation_enables_and_disables_learning(self):
        dd = _make_dd("SN-ENABLE-1")
        ctrl = AdaptiveModbusController.get_or_create(MagicMock(), "SN-ENABLE-1", {})
        assert not ctrl.excitation_is_enabled()
        assert ctrl.learning_enabled

        with patch.object(services, "get_inverter_data", return_value=dd):
            asyncio.run(services.enable_excitation(_make_call()))

        assert ctrl.excitation_is_enabled()
        assert not ctrl.learning_enabled

    def test_enable_excitation_is_idempotent(self):
        """Calling enable_excitation twice must not replace an in-progress
        schedule with a fresh one -- see AdaptiveModbusController.
        enable_excitation()'s own docstring / is-None guard."""
        dd = _make_dd("SN-ENABLE-2")
        ctrl = AdaptiveModbusController.get_or_create(MagicMock(), "SN-ENABLE-2", {})

        with patch.object(services, "get_inverter_data", return_value=dd):
            asyncio.run(services.enable_excitation(_make_call()))
            first_instance = ctrl._excitation
            asyncio.run(services.enable_excitation(_make_call()))
            assert ctrl._excitation is first_instance

    def test_disable_excitation_re_enables_learning(self):
        dd = _make_dd("SN-DISABLE-1")
        ctrl = AdaptiveModbusController.get_or_create(MagicMock(), "SN-DISABLE-1", {})

        with patch.object(services, "get_inverter_data", return_value=dd):
            asyncio.run(services.enable_excitation(_make_call()))
            assert not ctrl.learning_enabled
            asyncio.run(services.disable_excitation(_make_call()))

        assert not ctrl.excitation_is_enabled()
        assert ctrl.learning_enabled

    def test_disable_excitation_is_a_noop_when_never_enabled(self):
        dd = _make_dd("SN-DISABLE-2")
        ctrl = AdaptiveModbusController.get_or_create(MagicMock(), "SN-DISABLE-2", {})

        with patch.object(services, "get_inverter_data", return_value=dd):
            asyncio.run(services.disable_excitation(_make_call()))  # must not raise

        assert not ctrl.excitation_is_enabled()

    def test_enable_excitation_raises_when_no_controller_registered(self):
        """Adversarial: a device_id resolving to a serial number with no
        AdaptiveModbusController instance at all -- must raise a clear,
        translated error, not an AttributeError from calling a method on
        None."""
        dd = _make_dd("SN-NEVER-CREATED")

        with patch.object(services, "get_inverter_data", return_value=dd):
            with pytest.raises(ServiceValidationError) as exc_info:
                asyncio.run(services.enable_excitation(_make_call()))
        assert exc_info.value.translation_key == "adaptive_controller_not_found"


class TestResumeExcitationAfterHalt:

    def setup_method(self):
        AdaptiveModbusController.clear_registry()

    def teardown_method(self):
        AdaptiveModbusController.clear_registry()

    def test_raises_when_excitation_never_enabled(self):
        dd = _make_dd("SN-RESUME-1")
        AdaptiveModbusController.get_or_create(MagicMock(), "SN-RESUME-1", {})

        with patch.object(services, "get_inverter_data", return_value=dd):
            with pytest.raises(ServiceValidationError) as exc_info:
                asyncio.run(services.resume_excitation_after_halt(_make_call()))
        assert exc_info.value.translation_key == "excitation_not_enabled"

    def test_is_a_noop_when_enabled_but_not_halted(self):
        dd = _make_dd("SN-RESUME-2")
        ctrl = AdaptiveModbusController.get_or_create(MagicMock(), "SN-RESUME-2", {})

        with patch.object(services, "get_inverter_data", return_value=dd):
            asyncio.run(services.enable_excitation(_make_call()))
            asyncio.run(services.resume_excitation_after_halt(_make_call()))  # must not raise

        assert ctrl.excitation_is_enabled()
        assert not ctrl.excitation_is_halted()

    def test_resumes_a_genuinely_halted_schedule(self):
        dd = _make_dd("SN-RESUME-3")
        ctrl = AdaptiveModbusController.get_or_create(MagicMock(), "SN-RESUME-3", {})

        with patch.object(services, "get_inverter_data", return_value=dd):
            asyncio.run(services.enable_excitation(_make_call()))
            # Force a halt directly, mirroring what the go/no-go monitor
            # would do on a real breach -- avoids needing hundreds of real
            # record_request() calls just to exercise this service path.
            ctrl._excitation._state = ctrl._excitation._state.__class__.HALTED
            ctrl._excitation._halt_reason = "test-induced halt"
            assert ctrl.excitation_is_halted()

            asyncio.run(services.resume_excitation_after_halt(_make_call()))

        assert not ctrl.excitation_is_halted()
        assert ctrl.excitation_is_enabled()  # resumed, not disabled

    def test_raises_when_no_controller_registered(self):
        dd = _make_dd("SN-RESUME-NEVER-CREATED")
        with patch.object(services, "get_inverter_data", return_value=dd):
            with pytest.raises(ServiceValidationError) as exc_info:
                asyncio.run(services.resume_excitation_after_halt(_make_call()))
        assert exc_info.value.translation_key == "adaptive_controller_not_found"


class TestServiceTargetsCorrectDevice:
    """Confirms _get_adaptive_controller_for_call() resolves the
    controller for the SPECIFIC device_id in the call, not just any
    registered controller -- adversarial against a hypothetical future
    bug where two devices' own excitation state could be cross-wired."""

    def setup_method(self):
        AdaptiveModbusController.clear_registry()

    def teardown_method(self):
        AdaptiveModbusController.clear_registry()

    def test_enabling_one_device_does_not_affect_another(self):
        dd_a = _make_dd("SN-DEVICE-A")
        dd_b = _make_dd("SN-DEVICE-B")
        ctrl_a = AdaptiveModbusController.get_or_create(MagicMock(), "SN-DEVICE-A", {})
        ctrl_b = AdaptiveModbusController.get_or_create(MagicMock(), "SN-DEVICE-B", {})

        with patch.object(services, "get_inverter_data", return_value=dd_a):
            asyncio.run(services.enable_excitation(_make_call("device-a")))

        assert ctrl_a.excitation_is_enabled()
        assert not ctrl_b.excitation_is_enabled()
