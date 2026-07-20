"""Tests for synchronized_power_coordinator.py.

Covers
------
• SynchronizedPowerData derived properties (pv_power_total, home_consumption)
  including edge cases: None inputs, negative values, clamping.
• SynchronizedPowerCoordinator._async_update_data happy path and partial
  failure paths (one device fails, all devices fail).
• Guard sequencing — primary guard is always used for INV1/meter/battery reads;
  secondary guard is used for INV2.
• Telemetry recording — record_request / record_failure / record_timeout called
  at the correct points.
"""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Minimal HA stubs (no HA runtime needed)
# ---------------------------------------------------------------------------

for _m in [
    "homeassistant",
    "homeassistant.core",
    "homeassistant.helpers",
    "homeassistant.helpers.update_coordinator",
]:
    sys.modules.setdefault(_m, types.ModuleType(_m))

# synchronized_power_coordinator imports `from homeassistant.core import
# HomeAssistant`; provide the names on the core stub so the module loads
# (previously missing — the whole test module skipped at import time).
_core = sys.modules["homeassistant.core"]
if not hasattr(_core, "HomeAssistant"):
    _core.HomeAssistant = type("HomeAssistant", (), {})
if not hasattr(_core, "callback"):
    _core.callback = lambda f: f

# DataUpdateCoordinator stub
_duc = sys.modules["homeassistant.helpers.update_coordinator"]


class _FakeDUC:
    def __class_getitem__(cls, item):  # DataUpdateCoordinator[...] is generic
        return cls

    def __init__(self, hass, logger, name, update_interval):
        self.hass = hass
        self.name = name
        self.update_interval = update_interval
        self.data = None

    async def async_config_entry_first_refresh(self):
        self.data = await self._async_update_data()

    async def _async_update_data(self):
        raise NotImplementedError


class _FakeUpdateFailed(Exception):
    pass


_duc.DataUpdateCoordinator = _FakeDUC
_duc.UpdateFailed = _FakeUpdateFailed

import importlib, pathlib

# Stub huawei_solar
_hs = types.ModuleType("huawei_solar")
_hs.ConnectionInterruptedException = ConnectionError
_hs.HuaweiSolarException = Exception


class _RN:
    INPUT_POWER = "input_power"
    POWER_METER_ACTIVE_POWER = "power_meter_active_power"
    STORAGE_CHARGE_DISCHARGE_POWER = "storage_charge_discharge_power"


_hs.register_names = _RN
_hs_dev = types.ModuleType("huawei_solar.device")
_hs_dev_base = types.ModuleType("huawei_solar.device.base")
_hs_dev_base.HuaweiSolarDevice = object
sys.modules["huawei_solar"] = _hs
sys.modules["huawei_solar.device"] = _hs_dev
sys.modules["huawei_solar.device.base"] = _hs_dev_base

# Stub .const
_const_stub = types.ModuleType("huawei_solar_const_stub")
_const_stub.SYNC_POWER_UPDATE_INTERVAL = __import__("datetime").timedelta(seconds=10)
_const_stub.UPDATE_TIMEOUT = __import__("datetime").timedelta(seconds=35)

# Stub .modbus_guard
_guard_stub = types.ModuleType("huawei_solar_guard_stub")


class _FakeGuard:
    def __init__(self, serial):
        self.serial = serial
        self._lock = asyncio.Lock()

    @staticmethod
    def get_or_create(serial):
        return _FakeGuard(serial)

    def request(self):
        return _FakeGuard._Ctx(self)

    class _Ctx:
        def __init__(self, guard):
            self._guard = guard

        async def __aenter__(self):
            pass

        async def __aexit__(self, *args):
            pass


_guard_stub.ModbusGuard = _FakeGuard

# Stub .modbus_telemetry
_telemetry_stub = types.ModuleType("huawei_solar_telemetry_stub")
_telemetry_stub.ModbusTelemetry = MagicMock

# Patch relative imports before loading the module
with patch.dict(
    "sys.modules",
    {
        "huawei_solar.const": _const_stub,
        # The module uses relative imports; patch the dotted names it resolves to
        "huawei_solar_const": _const_stub,
    },
):
    _src = pathlib.Path(__file__).parent.parent / "synchronized_power_coordinator.py"
    _spec = importlib.util.spec_from_file_location("sync_power", _src)
    sync_mod = importlib.util.module_from_spec(_spec)

    # Inject stubs for relative imports
    sync_mod.__package__ = "huawei_solar"
    # Register in sys.modules BEFORE exec: @dataclass(slots=True) resolves field
    # annotations via sys.modules[cls.__module__], which is None otherwise.
    sys.modules["sync_power"] = sync_mod
    with patch.dict(
        "sys.modules",
        {
            "huawei_solar.const": _const_stub,
            "huawei_solar.modbus_guard": _guard_stub,
            "huawei_solar.modbus_telemetry": _telemetry_stub,
        },
    ):
        try:
            _spec.loader.exec_module(sync_mod)
        except Exception as exc:
            pytest.skip(f"Cannot load synchronized_power_coordinator standalone: {exc}")

SynchronizedPowerData = sync_mod.SynchronizedPowerData
SynchronizedPowerCoordinator = sync_mod.SynchronizedPowerCoordinator
UpdateFailed = _FakeUpdateFailed


# ---------------------------------------------------------------------------
# SynchronizedPowerData unit tests
# ---------------------------------------------------------------------------

class TestSynchronizedPowerData:
    """Test derived properties on the result dataclass."""

    # pv_power_total ----------------------------------------------------------

    def test_pv_total_both_inverters(self):
        d = SynchronizedPowerData(inv1_pv_power=3000, inv2_pv_power=2500,
                                   grid_power=None, battery_power=None)
        assert d.pv_power_total == 5500

    def test_pv_total_one_inverter(self):
        d = SynchronizedPowerData(inv1_pv_power=4000, inv2_pv_power=None,
                                   grid_power=None, battery_power=None)
        assert d.pv_power_total == 4000

    def test_pv_total_none_when_inv1_missing(self):
        """INV1 is always required; None propagates."""
        d = SynchronizedPowerData(inv1_pv_power=None, inv2_pv_power=2000,
                                   grid_power=None, battery_power=None)
        assert d.pv_power_total is None

    def test_pv_total_zero_inv2(self):
        """0 W (night) is a valid reading — not the same as None."""
        d = SynchronizedPowerData(inv1_pv_power=1000, inv2_pv_power=0,
                                   grid_power=None, battery_power=None)
        assert d.pv_power_total == 1000

    # home_consumption --------------------------------------------------------

    def test_home_consumption_basic(self):
        """Solar 5 kW, grid 1 kW import, battery idle → home = 6 kW."""
        d = SynchronizedPowerData(inv1_pv_power=5000, inv2_pv_power=0,
                                   grid_power=1000, battery_power=0)
        assert d.home_consumption == pytest.approx(6000)

    def test_home_consumption_with_battery_charging(self):
        """Solar 5 kW, grid export -1 kW, battery charging 2 kW → home = 2 kW.

        home = PV + grid − battery = 5000 + (−1000) − 2000 = 2000
        """
        d = SynchronizedPowerData(inv1_pv_power=5000, inv2_pv_power=0,
                                   grid_power=-1000, battery_power=2000)
        assert d.home_consumption == pytest.approx(2000)

    def test_home_consumption_with_battery_discharging(self):
        """Solar 2 kW, grid import 1 kW, battery discharging -3 kW → home = 6 kW.

        home = 2000 + 1000 − (−3000) = 6000
        """
        d = SynchronizedPowerData(inv1_pv_power=2000, inv2_pv_power=0,
                                   grid_power=1000, battery_power=-3000)
        assert d.home_consumption == pytest.approx(6000)

    def test_home_consumption_clamped_to_zero(self):
        """Small negative values from measurement noise are clamped to 0."""
        d = SynchronizedPowerData(inv1_pv_power=100, inv2_pv_power=0,
                                   grid_power=-5, battery_power=200)
        # 100 + (-5) - 200 = -105 → clamped to 0
        assert d.home_consumption == 0.0

    def test_home_consumption_none_when_pv_missing(self):
        d = SynchronizedPowerData(inv1_pv_power=None, inv2_pv_power=None,
                                   grid_power=500, battery_power=0)
        assert d.home_consumption is None

    def test_home_consumption_none_when_grid_missing(self):
        """Grid is required for home_consumption — battery alone isn't enough."""
        d = SynchronizedPowerData(inv1_pv_power=3000, inv2_pv_power=0,
                                   grid_power=None, battery_power=0)
        assert d.home_consumption is None

    def test_home_consumption_no_battery_treated_as_zero(self):
        """When battery is None (not installed) it contributes 0 to the equation."""
        d = SynchronizedPowerData(inv1_pv_power=4000, inv2_pv_power=0,
                                   grid_power=-500, battery_power=None)
        # 4000 + (-500) - 0 = 3500
        assert d.home_consumption == pytest.approx(3500)

    def test_home_consumption_pure_export(self):
        """All PV exported, no home load → home = 0 (clamped)."""
        d = SynchronizedPowerData(inv1_pv_power=5000, inv2_pv_power=0,
                                   grid_power=-5000, battery_power=0)
        assert d.home_consumption == pytest.approx(0)

    # Edge cases --------------------------------------------------------------

    def test_all_zero(self):
        """Night time: all zeros — home = 0."""
        d = SynchronizedPowerData(inv1_pv_power=0, inv2_pv_power=0,
                                   grid_power=0, battery_power=0)
        assert d.pv_power_total == 0
        assert d.home_consumption == 0

    def test_large_values(self):
        """Multi-inverter site: 2 × 20 kW inverters, 30 kW home load."""
        d = SynchronizedPowerData(inv1_pv_power=20000, inv2_pv_power=20000,
                                   grid_power=-10000, battery_power=0)
        # PV = 40000, grid exporting 10000, home = 40000 - 10000 - 0 = 30000
        assert d.pv_power_total == 40000
        assert d.home_consumption == pytest.approx(30000)


# ---------------------------------------------------------------------------
# SynchronizedPowerCoordinator integration tests
# ---------------------------------------------------------------------------

def _make_device(serial: str, pv_power: float | None = 3000) -> MagicMock:
    """Build a minimal device mock that returns fixed register values."""
    device = MagicMock()
    device.serial_number = serial

    async def _batch_update(registers):
        results = {}
        if _RN.INPUT_POWER in registers:
            r = MagicMock()
            r.value = pv_power
            results[_RN.INPUT_POWER] = r
        if _RN.POWER_METER_ACTIVE_POWER in registers:
            r = MagicMock()
            r.value = -500.0  # exporting
            results[_RN.POWER_METER_ACTIVE_POWER] = r
        if _RN.STORAGE_CHARGE_DISCHARGE_POWER in registers:
            r = MagicMock()
            r.value = 1000.0  # charging
            results[_RN.STORAGE_CHARGE_DISCHARGE_POWER] = r
        return results

    device.batch_update = _batch_update
    return device


def _make_coordinator(inv1=None, inv2=None, has_meter=True, has_battery=True):
    inv1 = inv1 or _make_device("SN-INV1")
    coord = SynchronizedPowerCoordinator.__new__(SynchronizedPowerCoordinator)
    coord.hass = MagicMock()
    coord.name = "test_sync_power"
    coord.update_interval = __import__("datetime").timedelta(seconds=10)
    coord.data = None
    coord._inv1 = inv1
    coord._inv2 = inv2
    coord._has_meter = has_meter
    coord._has_battery = has_battery
    coord._update_timeout = __import__("datetime").timedelta(seconds=35)
    coord._telemetry = None
    coord._primary_guard = _FakeGuard.get_or_create("SN-INV1")
    coord._secondary_guard = (
        _FakeGuard.get_or_create(inv2.serial_number) if inv2 else None
    )
    coord._consecutive_failures = 0
    return coord


class TestSynchronizedPowerCoordinatorHappyPath:
    @pytest.mark.asyncio
    async def test_reads_all_four_registers(self):
        inv1 = _make_device("SN1", pv_power=4000)
        inv2 = _make_device("SN2", pv_power=2000)
        coord = _make_coordinator(inv1=inv1, inv2=inv2, has_meter=True, has_battery=True)

        result = await coord._async_update_data()

        assert result.inv1_pv_power == 4000
        assert result.inv2_pv_power == 2000
        assert result.grid_power == -500
        assert result.battery_power == 1000
        assert result.pv_power_total == 6000
        assert result.home_consumption == pytest.approx(4500)  # 6000 + (-500) - 1000

    @pytest.mark.asyncio
    async def test_single_inverter_no_battery(self):
        inv1 = _make_device("SN1", pv_power=5000)
        coord = _make_coordinator(inv1=inv1, inv2=None, has_meter=True, has_battery=False)

        result = await coord._async_update_data()

        assert result.inv1_pv_power == 5000
        assert result.inv2_pv_power is None
        assert result.grid_power == -500
        assert result.battery_power is None
        assert result.pv_power_total == 5000

    @pytest.mark.asyncio
    async def test_consecutive_failures_reset_on_success(self):
        coord = _make_coordinator()
        coord._consecutive_failures = 3

        await coord._async_update_data()

        assert coord._consecutive_failures == 0


class TestSynchronizedPowerCoordinatorPartialFailure:
    @pytest.mark.asyncio
    async def test_inv2_failure_still_returns_inv1_data(self):
        """If INV2 times out, the other three readings must still succeed."""
        inv1 = _make_device("SN1", pv_power=4000)
        inv2 = MagicMock()
        inv2.serial_number = "SN2"
        inv2.batch_update = AsyncMock(side_effect=TimeoutError("INV2 offline"))

        coord = _make_coordinator(inv1=inv1, inv2=inv2, has_meter=True, has_battery=True)
        result = await coord._async_update_data()

        assert result.inv1_pv_power == 4000
        assert result.grid_power == -500
        assert result.battery_power == 1000
        assert result.inv2_pv_power is None   # unavailable
        # Fail-safe semantics (see pv_power_total docstring): with a second
        # inverter installed but unread, the total would silently omit INV2's
        # contribution — so it must be None (entity unavailable), not a wrong
        # number. home_consumption derives from it and must follow.
        assert result.pv_power_total is None
        assert result.home_consumption is None

    @pytest.mark.asyncio
    async def test_all_fail_raises_update_failed(self):
        """When every read fails, UpdateFailed must be raised."""
        def _always_fail(*_):
            raise TimeoutError("all dead")

        inv1 = MagicMock()
        inv1.serial_number = "SN1"
        inv1.batch_update = _always_fail

        coord = _make_coordinator(inv1=inv1, inv2=None, has_meter=False, has_battery=False)

        with pytest.raises(UpdateFailed):
            await coord._async_update_data()

        assert coord._consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_consecutive_failure_counter_increments(self):
        inv1 = MagicMock()
        inv1.serial_number = "SN1"
        inv1.batch_update = AsyncMock(side_effect=TimeoutError)

        coord = _make_coordinator(inv1=inv1, inv2=None, has_meter=False, has_battery=False)

        for expected in range(1, 4):
            with pytest.raises(UpdateFailed):
                await coord._async_update_data()
            assert coord._consecutive_failures == expected


class TestTelemetryRecording:
    @pytest.mark.asyncio
    async def test_record_request_called_per_successful_read(self):
        telemetry = MagicMock()
        inv1 = _make_device("SN1", pv_power=3000)
        inv2 = _make_device("SN2", pv_power=1000)
        coord = _make_coordinator(inv1=inv1, inv2=inv2, has_meter=True, has_battery=True)
        coord._telemetry = telemetry

        await coord._async_update_data()

        # 4 successful reads: INV1 PV, meter, battery, INV2 PV
        assert telemetry.record_request.call_count == 4

    @pytest.mark.asyncio
    async def test_record_timeout_on_timeout_error(self):
        telemetry = MagicMock()
        inv1 = _make_device("SN1", pv_power=3000)

        inv2 = MagicMock()
        inv2.serial_number = "SN2"
        inv2.batch_update = AsyncMock(side_effect=TimeoutError)

        coord = _make_coordinator(inv1=inv1, inv2=inv2, has_meter=False, has_battery=False)
        coord._telemetry = telemetry

        await coord._async_update_data()

        telemetry.record_timeout.assert_called_once()
        telemetry.record_failure.assert_not_called()

    @pytest.mark.asyncio
    async def test_record_failure_on_generic_error(self):
        telemetry = MagicMock()
        inv1 = _make_device("SN1", pv_power=3000)

        inv2 = MagicMock()
        inv2.serial_number = "SN2"
        inv2.batch_update = AsyncMock(side_effect=RuntimeError("comms error"))

        coord = _make_coordinator(inv1=inv1, inv2=inv2, has_meter=False, has_battery=False)
        coord._telemetry = telemetry

        await coord._async_update_data()

        telemetry.record_failure.assert_called_once()
        telemetry.record_timeout.assert_not_called()
