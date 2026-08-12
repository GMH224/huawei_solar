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
# v2.0.0 (V2_ARCHITECTURE_DESIGN.md §8.2)
_const_stub.SYNC_POWER_CACHE_ALIGNMENT_TOLERANCE_S = 3.0
# v2.0.0b (MOD-02/MOD-04, external ICS audit)
_const_stub.SYNC_POWER_POLL_DEADLINE = __import__("datetime").timedelta(seconds=18)

# v2.0.0: stub .register_cache -- synchronized_power_coordinator.py now
# imports Quality from it for the cache-shortcut check
# (_try_cache_shortcut). A minimal, dependency-free stand-in, matching how
# .modbus_guard/.modbus_telemetry are already faked below rather than
# loading the real modules for this isolated test.
_register_cache_stub = types.ModuleType("huawei_solar_register_cache_stub")


class _FakeQuality:
    GOOD = "good"
    UNCERTAIN = "uncertain"
    BAD = "bad"


_register_cache_stub.Quality = _FakeQuality

# Stub .modbus_guard
_guard_stub = types.ModuleType("huawei_solar_guard_stub")


class _FakeGuard:
    def __init__(self, serial):
        self.serial = serial
        self._lock = asyncio.Lock()

    @staticmethod
    def get_or_create(serial):
        return _FakeGuard(serial)

    def request(self, *, label: str = "", priority: bool = False):
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
            "huawei_solar.register_cache": _register_cache_stub,
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
    # Dedicated SyncPower-specific counters -- _make_coordinator() bypasses
    # __init__ entirely, so these need setting explicitly, matching
    # __init__'s own defaults -- the same class of gap hit repeatedly this
    # session for every object.__new__()-based test fixture.
    coord.shortcut_hits = 0
    coord.shortcut_misses = 0
    coord.fallback_cache_hits = 0
    coord.fallback_physical_reads = 0
    coord._primary_guard = _FakeGuard.get_or_create("SN-INV1")
    coord._secondary_guard = (
        _FakeGuard.get_or_create(inv2.serial_number) if inv2 else None
    )
    coord._consecutive_failures = 0
    # v2.0.0 (V2_ARCHITECTURE_DESIGN.md §8.2): __new__ bypasses __init__
    # entirely, so these need setting explicitly, matching the real
    # constructor's own defaults. None for all four means
    # _try_cache_shortcut() always returns None (no cache references to
    # check), correctly falling through to the dedicated read every
    # existing test here already exercises -- these tests predate §8.2 and
    # aren't testing the shortcut itself; see TestCacheShortcut below for
    # that.
    coord._inv1_cache = None
    coord._inv2_cache = None
    coord._meter_cache = None
    coord._battery_cache = None
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

    @pytest.mark.asyncio
    async def test_temporally_uncertain_flag_defaults_false_when_well_aligned(self):
        """v2.0.0a (F09/F20, external ICS audit -- confirmed): a healthy,
        fast dedicated read must not be flagged uncertain."""
        coord = _make_coordinator()
        result = await coord._async_update_data()
        assert result.is_temporally_uncertain is False

    @pytest.mark.asyncio
    async def test_temporally_uncertain_flag_set_when_span_exceeds_tolerance(self):
        """The actual fix: a dedicated read whose four sub-reads end up
        spread further apart than the alignment tolerance (e.g. under
        heavy bus contention, interleaving badly with other coordinators)
        must be explicitly flagged, not silently returned as if it were a
        clean, well-aligned sample."""
        inv1 = _make_device("SN1", pv_power=4000)
        coord = _make_coordinator(inv1=inv1, has_meter=True, has_battery=True)

        # Shrink the tolerance for a fast test rather than sleeping for
        # the real multi-second default.
        with patch.object(sync_mod, "SYNC_POWER_CACHE_ALIGNMENT_TOLERANCE_S", 0.01):
            original_batch_update = inv1.batch_update

            call_count = 0

            async def _delayed_batch_update(registers):
                nonlocal call_count
                call_count += 1
                if call_count > 1:
                    # Force a real gap after the first read, comfortably
                    # past the shrunk 10ms tolerance.
                    await asyncio.sleep(0.05)
                return await original_batch_update(registers)

            inv1.batch_update = _delayed_batch_update
            result = await coord._async_update_data()

        assert result.is_temporally_uncertain is True
        assert result.sample_span_ms is not None
        assert result.sample_span_ms > 10.0


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


# ── v2.0.0 (V2_ARCHITECTURE_DESIGN.md §8.2): cache-shortcut behaviour ────────

rn = sync_mod.rn  # not directly imported at test-module level; sync_mod has it
_Q = sync_mod.Quality  # the stubbed Quality (GOOD/UNCERTAIN/BAD as strings)


class _Res:
    def __init__(self, v):
        self.value = v


class _FakeCache:
    """Minimal stand-in for RegisterCache.quality_of()/get(), keyed by
    register name -> (quality, reason, age, value)."""

    def __init__(self):
        self._entries = {}

    def set(self, name, *, quality, age, value, reason=None):
        self._entries[name] = (quality, reason, age, value)

    def quality_of(self, name):
        if name not in self._entries:
            return "bad", "never_read", None
        quality, reason, age, _value = self._entries[name]
        return quality, reason, age

    def get(self, name):
        if name not in self._entries:
            return None
        _q, _r, _a, value = self._entries[name]
        if value is None:
            return None
        return _Res(value)


def _make_coordinator_with_caches(
    inv1_cache=None, inv2_cache=None, meter_cache=None, battery_cache=None,
    inv2=None, has_meter=True, has_battery=True,
):
    coord = _make_coordinator(inv2=inv2, has_meter=has_meter, has_battery=has_battery)
    coord._inv1_cache = inv1_cache
    coord._inv2_cache = inv2_cache
    coord._meter_cache = meter_cache
    coord._battery_cache = battery_cache
    return coord


class TestCacheShortcut:
    """_try_cache_shortcut() -- the actual v2.0.0 addition. Every OTHER test
    in this file already confirms the shortcut correctly does NOT engage
    (all caches default to None); these specifically exercise it engaging,
    and the boundary conditions that must prevent it from engaging."""

    def test_engages_when_all_good_and_aligned(self):
        cache = _FakeCache()
        cache.set(rn.INPUT_POWER, quality=_Q.GOOD, age=1.0, value=3000)
        coord = _make_coordinator_with_caches(
            inv1_cache=cache, has_meter=False, has_battery=False,
        )
        result = coord._try_cache_shortcut()
        assert result is not None, "shortcut must engage: single GOOD, aligned register"
        assert result.inv1_pv_power == 3000
        assert result.sample_span_ms == 0.0

    def test_does_not_engage_when_a_cache_reference_is_missing(self):
        coord = _make_coordinator_with_caches(
            inv1_cache=None, has_meter=False, has_battery=False,
        )
        assert coord._try_cache_shortcut() is None

    def test_does_not_engage_when_quality_is_uncertain(self):
        cache = _FakeCache()
        cache.set(rn.INPUT_POWER, quality=_Q.UNCERTAIN, age=1.0, value=3000)
        coord = _make_coordinator_with_caches(
            inv1_cache=cache, has_meter=False, has_battery=False,
        )
        assert coord._try_cache_shortcut() is None, (
            "UNCERTAIN is servable to a normal entity, but not trustworthy "
            "enough for the synchronized-read shortcut to substitute for a "
            "real read"
        )

    def test_does_not_engage_when_age_spread_exceeds_tolerance(self):
        meter_cache = _FakeCache()
        meter_cache.set(rn.POWER_METER_ACTIVE_POWER, quality=_Q.GOOD, age=0.5, value=500)
        inv1_cache = _FakeCache()
        inv1_cache.set(rn.INPUT_POWER, quality=_Q.GOOD, age=10.0, value=3000)  # >3s apart
        coord = _make_coordinator_with_caches(
            inv1_cache=inv1_cache, meter_cache=meter_cache, has_battery=False,
        )
        assert coord._try_cache_shortcut() is None, (
            "a 9.5s age-spread must exceed the 3s tolerance and refuse the shortcut"
        )

    def test_engages_at_exactly_the_boundary_of_tolerance(self):
        meter_cache = _FakeCache()
        meter_cache.set(rn.POWER_METER_ACTIVE_POWER, quality=_Q.GOOD, age=1.0, value=500)
        inv1_cache = _FakeCache()
        inv1_cache.set(rn.INPUT_POWER, quality=_Q.GOOD, age=4.0, value=3000)  # exactly 3s
        coord = _make_coordinator_with_caches(
            inv1_cache=inv1_cache, meter_cache=meter_cache, has_battery=False,
        )
        result = coord._try_cache_shortcut()
        assert result is not None, "exactly at the tolerance boundary must still engage"

    def test_reports_honest_measured_span_not_zero(self):
        meter_cache = _FakeCache()
        meter_cache.set(rn.POWER_METER_ACTIVE_POWER, quality=_Q.GOOD, age=0.5, value=500)
        inv1_cache = _FakeCache()
        inv1_cache.set(rn.INPUT_POWER, quality=_Q.GOOD, age=2.0, value=3000)
        coord = _make_coordinator_with_caches(
            inv1_cache=inv1_cache, meter_cache=meter_cache, has_battery=False,
        )
        result = coord._try_cache_shortcut()
        assert result is not None
        assert round(result.sample_span_ms - 1500.0, 1) == 0

    def test_all_four_registers_checked_when_all_applicable(self):
        inv1_cache = _FakeCache()
        inv1_cache.set(rn.INPUT_POWER, quality=_Q.GOOD, age=1.0, value=3000)
        inv2_cache = _FakeCache()
        inv2_cache.set(rn.INPUT_POWER, quality=_Q.GOOD, age=1.2, value=1500)
        meter_cache = _FakeCache()
        meter_cache.set(rn.POWER_METER_ACTIVE_POWER, quality=_Q.GOOD, age=0.8, value=200)
        battery_cache = _FakeCache()
        battery_cache.set(rn.STORAGE_CHARGE_DISCHARGE_POWER, quality=_Q.GOOD, age=1.5, value=-400)

        inv2 = MagicMock()
        inv2.serial_number = "SN2"
        coord = _make_coordinator_with_caches(
            inv1_cache=inv1_cache, inv2_cache=inv2_cache,
            meter_cache=meter_cache, battery_cache=battery_cache,
            inv2=inv2, has_meter=True, has_battery=True,
        )
        result = coord._try_cache_shortcut()
        assert result is not None
        assert result.inv1_pv_power == 3000
        assert result.inv2_pv_power == 1500
        assert result.grid_power == 200
        assert result.battery_power == -400

    def test_one_bad_register_among_several_prevents_the_shortcut(self):
        inv1_cache = _FakeCache()
        inv1_cache.set(rn.INPUT_POWER, quality=_Q.GOOD, age=1.0, value=3000)
        meter_cache = _FakeCache()
        meter_cache.set(rn.POWER_METER_ACTIVE_POWER, quality=_Q.BAD, age=None, value=None)
        coord = _make_coordinator_with_caches(
            inv1_cache=inv1_cache, meter_cache=meter_cache, has_battery=False,
        )
        assert coord._try_cache_shortcut() is None

    @pytest.mark.asyncio
    async def test_async_update_data_uses_the_shortcut_and_skips_the_dedicated_read(self):
        """End-to-end: when the shortcut is available, _async_update_data()
        must never touch the device/guard at all."""
        inv1_cache = _FakeCache()
        inv1_cache.set(rn.INPUT_POWER, quality=_Q.GOOD, age=1.0, value=3000)
        inv1 = _make_device("SN1", pv_power=99999)  # would be WRONG if the dedicated read ran
        inv1.batch_update = AsyncMock(side_effect=AssertionError(
            "dedicated read must not run when the cache shortcut is available"
        ))
        coord = _make_coordinator_with_caches(
            inv1_cache=inv1_cache, has_meter=False, has_battery=False,
        )
        coord._inv1 = inv1
        result = await coord._async_update_data()
        assert result.inv1_pv_power == 3000

    @pytest.mark.asyncio
    async def test_shortcut_does_not_touch_consecutive_failures(self):
        """F13, external ICS audit -- confirmed: resetting
        _consecutive_failures and logging 'communication restored' on a
        shortcut hit was wrong -- no I/O occurred, so nothing was actually
        verified. The counter must be left exactly as it was."""
        inv1_cache = _FakeCache()
        inv1_cache.set(rn.INPUT_POWER, quality=_Q.GOOD, age=1.0, value=3000)
        coord = _make_coordinator_with_caches(
            inv1_cache=inv1_cache, has_meter=False, has_battery=False,
        )
        coord._consecutive_failures = 3  # simulate a prior run of genuine failures
        result = await coord._async_update_data()
        assert result.inv1_pv_power == 3000
        assert coord._consecutive_failures == 3, (
            "a cache-shortcut hit must not silently reset or otherwise "
            "touch the failure counter -- it performed no I/O and learned "
            "nothing about whether communication is actually healthy"
        )


# ── v2.0.0b: AR-9 -- cache-hit recording (external ICS audit) ───────────────

class TestAR9CacheHitRecording:
    """Confirmed: the shortcut's own hit rate was always the most direct
    evidence of whether the cache-first design actually eliminates
    physical traffic, but nothing recorded when it fired -- for either
    the aligned shortcut (§8.2) or the MOD-01 per-value fallback."""

    def test_full_shortcut_hit_records_one_hit_per_needed_register(self):
        cache = _FakeCache()
        cache.set(rn.INPUT_POWER, quality=_Q.GOOD, age=1.0, value=3000)
        coord = _make_coordinator_with_caches(
            inv1_cache=cache, has_meter=False, has_battery=False,
        )
        coord._telemetry = MagicMock()
        result = coord._try_cache_shortcut()
        assert result is not None
        coord._telemetry.record_cache_hits.assert_called_once_with(1)

    def test_full_shortcut_hit_records_all_four_when_all_present(self):
        inv1_cache = _FakeCache()
        inv1_cache.set(rn.INPUT_POWER, quality=_Q.GOOD, age=1.0, value=3000)
        inv2_cache = _FakeCache()
        inv2_cache.set(rn.INPUT_POWER, quality=_Q.GOOD, age=1.0, value=1500)
        meter_cache = _FakeCache()
        meter_cache.set(rn.POWER_METER_ACTIVE_POWER, quality=_Q.GOOD, age=1.0, value=200)
        battery_cache = _FakeCache()
        battery_cache.set(rn.STORAGE_CHARGE_DISCHARGE_POWER, quality=_Q.GOOD, age=1.0, value=-400)
        inv2 = MagicMock()
        inv2.serial_number = "SN2"
        coord = _make_coordinator_with_caches(
            inv1_cache=inv1_cache, inv2_cache=inv2_cache,
            meter_cache=meter_cache, battery_cache=battery_cache,
            inv2=inv2, has_meter=True, has_battery=True,
        )
        coord._telemetry = MagicMock()
        result = coord._try_cache_shortcut()
        assert result is not None
        coord._telemetry.record_cache_hits.assert_called_once_with(4)

    def test_shortcut_miss_does_not_record_a_hit(self):
        """No cache reference at all -- the shortcut can't engage, so
        nothing should be recorded as a hit."""
        coord = _make_coordinator_with_caches(
            inv1_cache=None, has_meter=False, has_battery=False,
        )
        coord._telemetry = MagicMock()
        result = coord._try_cache_shortcut()
        assert result is None
        coord._telemetry.record_cache_hits.assert_not_called()

    def test_telemetry_none_does_not_raise(self):
        """coord._telemetry defaults to None (no telemetry attached) --
        the recording call must be conditional, not assume a real
        telemetry object always exists."""
        cache = _FakeCache()
        cache.set(rn.INPUT_POWER, quality=_Q.GOOD, age=1.0, value=3000)
        coord = _make_coordinator_with_caches(
            inv1_cache=cache, has_meter=False, has_battery=False,
        )
        assert coord._telemetry is None
        result = coord._try_cache_shortcut()  # must not raise
        assert result is not None

    @pytest.mark.asyncio
    async def test_mod01_fallback_cache_hit_records_one_hit(self):
        """The other half of AR-9: a MOD-01 per-value cache hit inside the
        dedicated-read fallback (when the full aligned shortcut misses,
        but this specific value is still Quality.GOOD) must also be
        recorded."""
        inv1_cache = _FakeCache()
        inv1_cache.set(rn.INPUT_POWER, quality=_Q.GOOD, age=1.0, value=3000)
        coord = _make_coordinator_with_caches(
            inv1_cache=inv1_cache, has_meter=False, has_battery=False,
        )
        coord._telemetry = MagicMock()
        # Force the full shortcut to miss (age exceeds the alignment
        # tolerance) so the dedicated-read fallback's own MOD-01 check
        # is what actually serves this value.
        with patch.object(sync_mod, "SYNC_POWER_CACHE_ALIGNMENT_TOLERANCE_S", -1.0):
            result = await coord._async_update_data()
        assert result.inv1_pv_power == 3000
        coord._telemetry.record_cache_hit.assert_called_once_with()


# ── v2.0.3: ICS-01 -- fallback temporal alignment (external ICS audit) ──────

class TestICS01FallbackTemporalAlignment:
    """ICS-01, external ICS audit -- confirmed: the fallback path
    accepted any cache value with Quality.GOOD, without checking its age
    against the other values being combined into the same
    SynchronizedPowerData result. sample_span_ms only measured when
    _read_one() happened to be CALLED for each value, not when each
    value was actually captured -- for a cache hit those are different
    moments, and the gap was invisible to the metric. A composite could
    therefore silently combine a several-seconds-old cached value with a
    just-now physical read and still report a tight sample_span_ms."""

    @pytest.mark.asyncio
    async def test_aged_cache_value_combined_with_fresh_read_is_flagged_uncertain(self):
        """The core ICS-01 scenario: one value served from a cache entry
        old enough to exceed the alignment tolerance, combined with
        another value from a fresh physical read. Must now be flagged
        is_temporally_uncertain=True -- confirmed false (never flagged)
        before this fix, regardless of how old the cache value actually
        was, since age was never consulted at all."""
        inv1_cache = _FakeCache()
        # 5.0s -- clearly past SYNC_POWER_CACHE_ALIGNMENT_TOLERANCE_S (3.0s).
        inv1_cache.set(rn.INPUT_POWER, quality=_Q.GOOD, age=5.0, value=3000)
        coord = _make_coordinator_with_caches(
            inv1_cache=inv1_cache, meter_cache=None,  # meter has no cache -> physical read
            has_meter=True, has_battery=False,
        )
        # Force the full aligned shortcut to miss, so the dedicated-read
        # fallback (MOD-01's per-value check) is what actually serves
        # the cached value.
        with patch.object(sync_mod, "SYNC_POWER_CACHE_ALIGNMENT_TOLERANCE_S", -1.0):
            shortcut_missed = coord._try_cache_shortcut() is None
        assert shortcut_missed, "test setup check: shortcut must miss for this test to be meaningful"

        result = await coord._async_update_data()
        assert result.inv1_pv_power == 3000  # served from the aged cache
        assert result.grid_power == -500.0  # served from a fresh physical read
        assert result.is_temporally_uncertain is True, (
            "a 5.0s-old cached value combined with a fresh physical read "
            "must be flagged uncertain -- this is exactly the composite "
            "ICS-01 describes"
        )
        assert result.sample_span_ms is not None
        assert result.sample_span_ms >= 4500, (
            f"sample_span_ms ({result.sample_span_ms}) must reflect the "
            f"cached value's real ~5s age, not just the few-millisecond "
            f"spread between when _read_one() happened to be called for "
            f"each value (the pre-fix behaviour)"
        )

    @pytest.mark.asyncio
    async def test_two_fresh_physical_reads_are_not_flagged_uncertain(self):
        """Negative case: two values from physical reads completing
        close together in wall-clock time must NOT be flagged uncertain
        -- confirms the fix didn't make the check overly conservative."""
        coord = _make_coordinator_with_caches(
            inv1_cache=None, meter_cache=None, has_meter=True, has_battery=False,
        )
        result = await coord._async_update_data()
        assert result.is_temporally_uncertain is False
        assert result.sample_span_ms is not None
        assert result.sample_span_ms < 1000, "two back-to-back physical reads should be well under 1s apart"

    @pytest.mark.asyncio
    async def test_cache_hit_alone_within_tolerance_is_not_flagged(self):
        """A cache value young enough to be within tolerance, combined
        with a fresh read, must NOT be flagged -- confirms the fix
        checks the actual age against the actual tolerance, not just
        "any cache use at all"."""
        inv1_cache = _FakeCache()
        inv1_cache.set(rn.INPUT_POWER, quality=_Q.GOOD, age=0.5, value=3000)
        coord = _make_coordinator_with_caches(
            inv1_cache=inv1_cache, meter_cache=None, has_meter=True, has_battery=False,
        )
        with patch.object(sync_mod, "SYNC_POWER_CACHE_ALIGNMENT_TOLERANCE_S", -1.0):
            assert coord._try_cache_shortcut() is None
        result = await coord._async_update_data()
        assert result.is_temporally_uncertain is False, (
            "a 0.5s-old cached value combined with a fresh read is well "
            "within the 3.0s tolerance and must not be flagged"
        )
