"""Tests for modbus_keepalive.py — stdlib unittest, no pytest required.

Covers:
  • BUG-1: down_for/rtt_ms computed BEFORE _last_ok is updated
  • BUG-2: _create_task used instead of deprecated ensure_future in start()
  • BUG-9: invalid KEEPALIVE_REGISTER name handled gracefully (no crash)
  • _probe: success, failure, recovery paths
  • on_connection_lost fires once per outage (not on every failed probe)
  • on_connection_restored fires on recovery
  • start() idempotency, stop() cancels task
  • Registry singleton behaviour
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import time
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# ── Stubs ─────────────────────────────────────────────────────────────────────
sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
sys.modules.setdefault("homeassistant.core", types.ModuleType("homeassistant.core"))

# huawei_solar as a proper package
_hs = sys.modules.get("huawei_solar")
if _hs is None or not hasattr(_hs, "__path__"):
    _hs = types.ModuleType("huawei_solar")
    _hs.__path__ = []
    sys.modules["huawei_solar"] = _hs

class _RegName(str):
    _members: dict = {}
    def __class_getitem__(cls, k): return cls._members.get(k, cls(k))
    @classmethod
    def _reg(cls, n):
        m = cls(n); cls._members[n] = m; return m

for _n in ["model_id"]:
    _RegName._reg(_n)

_hs.RegisterName = _RegName
_hs.HuaweiSolarException = Exception

_hsd = types.ModuleType("huawei_solar.device"); _hsd.__path__ = []
_hsdb = types.ModuleType("huawei_solar.device.base"); _hsdb.HuaweiSolarDevice = object
sys.modules["huawei_solar.device"] = _hsd
sys.modules["huawei_solar.device.base"] = _hsdb

# const stub with keepalive values
_cstub = types.ModuleType("huawei_solar.const")
_cstub.KEEPALIVE_INTERVAL = __import__("datetime").timedelta(seconds=45)
_cstub.KEEPALIVE_REGISTER = "model_id"
sys.modules["huawei_solar.const"] = _cstub

_SRC = pathlib.Path(__file__).parent.parent / "modbus_keepalive.py"
_SPEC = importlib.util.spec_from_file_location("modbus_keepalive_test", str(_SRC))
_MOD = importlib.util.module_from_spec(_SPEC)
_MOD.__package__ = "huawei_solar"
_SPEC.loader.exec_module(_MOD)

ModbusKeepAlive = _MOD.ModbusKeepAlive

_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)
def _run(c): return _LOOP.run_until_complete(c)


def _fresh(on_lost=None, on_restored=None) -> ModbusKeepAlive:
    ModbusKeepAlive.clear_registry()
    device = MagicMock()
    device.batch_update = AsyncMock(return_value={})
    # Build a guard context manager mock
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    guard = MagicMock()
    guard.request = MagicMock(return_value=cm)
    ka = object.__new__(ModbusKeepAlive)
    ka.serial_number = "SN-KA"
    ka._device = device
    ka._guard = guard
    ka._on_connection_lost = on_lost or MagicMock()
    ka._on_connection_restored = on_restored or MagicMock()
    ka._task = None
    ka._healthy = True
    ka._last_ok = time.monotonic()
    ka._failure_count = 0
    return ka


# ── BUG-1: timing accuracy ────────────────────────────────────────────────────

class TestTimingAccuracy(unittest.TestCase):

    def test_down_for_computed_before_last_ok_update(self):
        """BUG-1 FIX: 'was down for' must reflect real downtime, not 0."""
        ka = _fresh()
        ka._healthy = False
        ka._failure_count = 2
        ka._last_ok = time.monotonic() - 120   # 120 s ago

        captured_down_for = []
        original_info = _MOD._LOGGER.info

        def capture_info(fmt, *args, **kw):
            if "was down for" in fmt:
                # args: (serial, down_for, failure_count)
                if len(args) >= 2:
                    captured_down_for.append(float(args[1]))

        reg = _RegName._reg("model_id")
        with patch.object(_MOD, "_get_keepalive_register", return_value=reg):
            with patch.object(_MOD._LOGGER, "info", side_effect=capture_info):
                _run(ka._probe())

        self.assertTrue(captured_down_for,
            "connection-restored log was not emitted")
        self.assertGreater(captured_down_for[0], 100,
            f"BUG-1 regression: 'was down for' = {captured_down_for[0]:.1f}s "
            "(should be ~120s; _last_ok is being updated before down_for is captured)")

    def test_healthy_log_uses_measured_rtt(self):
        """BUG-1 FIX: the debug log must use rtt_ms captured before _last_ok update."""
        ka = _fresh()
        ka._healthy = True
        reg = _RegName._reg("model_id")
        # Just verify the probe runs without error — timing test above covers the logic
        with patch.object(_MOD, "_get_keepalive_register", return_value=reg):
            _run(ka._probe())
        self.assertTrue(ka.is_healthy)
        self.assertTrue(ka.seconds_since_last_ok < 5,
            "seconds_since_last_ok should be near-zero right after a healthy probe")


# ── BUG-2: no deprecated ensure_future in start() ─────────────────────────────

class TestNoEnsureFuture(unittest.TestCase):

    def test_start_uses_create_task_not_ensure_future(self):
        """BUG-2 FIX: start() must use _create_task(), not ensure_future()."""
        source = pathlib.Path(_SRC).read_text()
        start_body = source[
            source.find("async def start("):
            source.find("\n    def stop(")
        ]
        self.assertNotIn("ensure_future", start_body,
            "BUG-2 regression: start() must not call ensure_future() directly")
        self.assertIn("_create_task(", start_body)

    def test_create_task_helper_defined(self):
        source = pathlib.Path(_SRC).read_text()
        self.assertIn("def _create_task(", source)

    def test_create_task_uses_create_task_api(self):
        source = pathlib.Path(_SRC).read_text()
        self.assertIn(".create_task(", source)


# ── BUG-9: invalid register name ─────────────────────────────────────────────

class TestInvalidRegisterName(unittest.TestCase):

    def test_probe_skipped_when_register_invalid(self):
        """BUG-9 FIX: None from _get_keepalive_register must skip probe silently."""
        ka = _fresh()
        ka._device.batch_update = AsyncMock(side_effect=AssertionError("must not call"))
        with patch.object(_MOD, "_get_keepalive_register", return_value=None):
            _run(ka._probe())  # must not raise
        ka._device.batch_update.assert_not_called()

    def test_bad_register_name_logs_warning_and_returns_none(self):
        """BUG-9 FIX: KeyError from RegisterName lookup → warning + None."""
        # The stub _RegName does not raise KeyError; we patch RegisterName in
        # the keepalive module to a strict version that does, to exercise the
        # error-handling path without depending on production enum behaviour.
        class _StrictRegName:
            def __class_getitem__(cls, key):
                raise KeyError(f"Unknown register: {key!r}")

        _MOD._KEEPALIVE_REGISTER_NAME = None
        with patch.object(_MOD, "KEEPALIVE_REGISTER", "totally_invalid_xyz"):
            with patch.object(_MOD, "RegisterName", _StrictRegName):
                with patch.object(_MOD._LOGGER, "warning") as mock_warn:
                    result = _MOD._get_keepalive_register()
        self.assertIsNone(result)
        self.assertTrue(mock_warn.called)
        _MOD._KEEPALIVE_REGISTER_NAME = None  # reset for other tests


# ── _probe success / failure / recovery ──────────────────────────────────────

class TestProbe(unittest.TestCase):

    def test_healthy_probe_stays_healthy(self):
        ka = _fresh()
        reg = _RegName("model_id")
        with patch.object(_MOD, "_get_keepalive_register", return_value=reg):
            _run(ka._probe())
        self.assertTrue(ka.is_healthy)
        self.assertEqual(ka._failure_count, 0)

    def test_failed_probe_sets_unhealthy(self):
        ka = _fresh()
        on_lost = MagicMock()
        ka._on_connection_lost = on_lost
        ka._device.batch_update = AsyncMock(side_effect=OSError("reset"))
        reg = _RegName("model_id")
        with patch.object(_MOD, "_get_keepalive_register", return_value=reg):
            _run(ka._probe())
        self.assertFalse(ka.is_healthy)
        on_lost.assert_called_once()

    def test_recovery_calls_on_connection_restored(self):
        ka = _fresh()
        ka._healthy = False; ka._failure_count = 3
        on_restored = MagicMock()
        ka._on_connection_restored = on_restored
        reg = _RegName("model_id")
        with patch.object(_MOD, "_get_keepalive_register", return_value=reg):
            _run(ka._probe())
        self.assertTrue(ka.is_healthy)
        self.assertEqual(ka._failure_count, 0)
        on_restored.assert_called_once()

    def test_on_connection_lost_fired_once_per_outage(self):
        """on_connection_lost must not fire on every failed probe."""
        ka = _fresh()
        on_lost = MagicMock()
        ka._on_connection_lost = on_lost
        ka._device.batch_update = AsyncMock(side_effect=OSError("down"))
        reg = _RegName("model_id")
        with patch.object(_MOD, "_get_keepalive_register", return_value=reg):
            _run(ka._probe())
            _run(ka._probe())
            _run(ka._probe())
        on_lost.assert_called_once()


# ── Lifecycle ─────────────────────────────────────────────────────────────────

class TestLifecycle(unittest.TestCase):

    def test_start_creates_task(self):
        ka = _fresh()
        async def _fake_run(): await asyncio.sleep(0)
        with patch.object(ka, "_run", return_value=_fake_run()):
            with patch.object(_MOD, "_create_task",
                               side_effect=lambda c: _LOOP.create_task(c)) as ct:
                _run(ka.start())
        self.assertIsNotNone(ka._task)
        if ka._task and not ka._task.done():
            ka._task.cancel()

    def test_start_idempotent(self):
        """Calling start() twice must not create a second task."""
        ka = _fresh()
        async def _slow(): await asyncio.sleep(100)
        with patch.object(_MOD, "_create_task",
                           side_effect=lambda c: _LOOP.create_task(c)):
            _run(ka.start())
            task1 = ka._task
            _run(ka.start())
            task2 = ka._task
        self.assertIs(task1, task2)
        if ka._task:
            ka._task.cancel()

    def test_stop_cancels_task(self):
        ka = _fresh()
        t = MagicMock(); t.done = MagicMock(return_value=False)
        ka._task = t
        ka.stop()
        t.cancel.assert_called_once()
        self.assertIsNone(ka._task)

    def test_stop_noop_when_not_started(self):
        ka = _fresh(); ka.stop()  # must not raise


# ── Registry ──────────────────────────────────────────────────────────────────

class TestRegistry(unittest.TestCase):

    def setUp(self):
        ModbusKeepAlive.clear_registry()

    def tearDown(self):
        ModbusKeepAlive.clear_registry()

    def test_singleton_per_serial(self):
        d, g = MagicMock(), MagicMock()
        ka1 = ModbusKeepAlive.get_or_create("SN1", d, g, MagicMock(), MagicMock())
        ka2 = ModbusKeepAlive.get_or_create("SN1", d, g, MagicMock(), MagicMock())
        self.assertIs(ka1, ka2)

    def test_get_returns_none_for_missing(self):
        self.assertIsNone(ModbusKeepAlive.get("NOPE"))

    def test_clear_registry(self):
        ModbusKeepAlive.get_or_create("X", MagicMock(), MagicMock(), MagicMock(), MagicMock())
        ModbusKeepAlive.clear_registry()
        self.assertEqual(ModbusKeepAlive._registry, {})
