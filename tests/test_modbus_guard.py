"""Tests for modbus_guard.py — stdlib unittest, no pytest required.

Covers:
  • Queue depth accounting: no double-decrement on timeout
  • Load shedding: MAX_QUEUE_DEPTH enforcement
  • Priority requests bypass shedding
  • Adaptive gap and queue-depth setters and clamps
  • Inter-request gap enforcement
  • BUG-8 fix: registry keyed on connection_endpoint, not serial_number
  • endpoint_for() TCP and RTU key derivation
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types
import unittest
from datetime import timedelta
from unittest.mock import patch

# ── Minimal stubs ─────────────────────────────────────────────────────────────
sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))

_SRC = pathlib.Path(__file__).parent.parent / "modbus_guard.py"
_SPEC = importlib.util.spec_from_file_location("modbus_guard", str(_SRC))
_MOD = importlib.util.module_from_spec(_SPEC)
_MOD.__package__ = "huawei_solar"
_SPEC.loader.exec_module(_MOD)

ModbusGuard = _MOD.ModbusGuard
MIN_INTER_REQUEST_GAP = _MOD.MIN_INTER_REQUEST_GAP
MAX_QUEUE_DEPTH = _MOD.MAX_QUEUE_DEPTH
QUEUE_WAIT_TIMEOUT = _MOD.QUEUE_WAIT_TIMEOUT

_LOOP = asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)


def _fresh_guard(endpoint: str = "192.168.1.1:502") -> ModbusGuard:
    """Return a fresh guard without touching the class registry."""
    g = object.__new__(ModbusGuard)
    g.endpoint = endpoint               # BUG-8: endpoint not serial_number
    g._lock = asyncio.Lock()
    g._last_request_end = 0.0
    g._queue_depth = 0
    g._effective_gap = MIN_INTER_REQUEST_GAP.total_seconds()
    g._max_queue_depth = MAX_QUEUE_DEPTH
    return g


# ── Queue depth accounting ────────────────────────────────────────────────────

class TestQueueDepthAccounting(unittest.TestCase):

    def test_depth_zero_after_successful_roundtrip(self):
        async def _go():
            g = _fresh_guard()
            self.assertEqual(g.queue_depth, 0)
            async with g.request():
                self.assertEqual(g.queue_depth, 1)
            self.assertEqual(g.queue_depth, 0)
        _run(_go())

    def test_depth_not_negative_on_lock_timeout(self):
        """No double-decrement when the lock-acquire times out."""
        async def _go():
            g = _fresh_guard()
            await g._lock.acquire()
            with patch.object(_MOD, "QUEUE_WAIT_TIMEOUT", timedelta(milliseconds=10)):
                with self.assertRaises(TimeoutError):
                    async with g.request():
                        pass
            g._lock.release()
            self.assertEqual(g.queue_depth, 0)
        _run(_go())

    def test_depth_not_negative_after_repeated_timeouts(self):
        async def _go():
            g = _fresh_guard()
            await g._lock.acquire()
            for _ in range(5):
                with patch.object(_MOD, "QUEUE_WAIT_TIMEOUT", timedelta(milliseconds=10)):
                    with self.assertRaises(TimeoutError):
                        async with g.request():
                            pass
            g._lock.release()
            self.assertEqual(g.queue_depth, 0)
        _run(_go())

    def test_is_busy_reflects_lock_state(self):
        async def _go():
            g = _fresh_guard()
            self.assertFalse(g.is_busy)
            async with g.request():
                self.assertTrue(g.is_busy)
            self.assertFalse(g.is_busy)
        _run(_go())


# ── Load shedding ─────────────────────────────────────────────────────────────

class TestLoadShedding(unittest.TestCase):

    def test_shed_when_queue_at_max(self):
        async def _go():
            g = _fresh_guard()
            g._max_queue_depth = 1
            g._queue_depth = 1        # already one waiter
            with self.assertRaises(asyncio.TimeoutError):
                async with g.request():
                    pass
            # depth must be unchanged — shed request never incremented it
            self.assertEqual(g.queue_depth, 1)
        _run(_go())

    def test_priority_bypasses_shedding(self):
        """Priority request must succeed even when queue is at max depth.

        _queue_depth is set to 1 (== max) to simulate a waiting non-priority
        caller.  After the priority request exits, depth returns to 1 (the
        manually-simulated waiter is still there; the priority request only
        adds then removes its own count).
        """
        async def _go():
            g = _fresh_guard()
            g._max_queue_depth = 1
            g._queue_depth = 1        # simulate one waiter occupying the slot
            entered = False
            async with g.request(priority=True):
                entered = True
                # During the request our depth should be 2 (simulated 1 + us)
                self.assertEqual(g.queue_depth, 2)
            self.assertTrue(entered, "priority request must not be shed")
            # After exit, depth returns to the pre-request value (1)
            self.assertEqual(g.queue_depth, 1)
        _run(_go())

    def test_depth_unchanged_after_shedding(self):
        async def _go():
            g = _fresh_guard()
            g._max_queue_depth = 2
            g._queue_depth = 2
            with self.assertRaises(asyncio.TimeoutError):
                async with g.request():
                    pass
            self.assertEqual(g.queue_depth, 2)
        _run(_go())


# ── Adaptive parameter setters ────────────────────────────────────────────────

class TestAdaptiveParams(unittest.TestCase):

    def test_update_gap_clamped_to_min(self):
        g = _fresh_guard()
        g.update_gap(0.001)          # 1 ms — below 150 ms hardware floor
        self.assertEqual(g._effective_gap, MIN_INTER_REQUEST_GAP.total_seconds())

    def test_update_gap_clamped_to_max(self):
        g = _fresh_guard()
        g.update_gap(10.0)           # 10 s — above 500 ms ceiling
        self.assertEqual(g._effective_gap, 0.500)

    def test_update_gap_valid(self):
        g = _fresh_guard()
        g.update_gap(0.300)
        self.assertAlmostEqual(g._effective_gap, 0.300, places=5)

    def test_update_max_queue_depth_clamped_low(self):
        g = _fresh_guard()
        g.update_max_queue_depth(0)
        self.assertEqual(g._max_queue_depth, 1)

    def test_update_max_queue_depth_clamped_high(self):
        g = _fresh_guard()
        g.update_max_queue_depth(99)
        self.assertEqual(g._max_queue_depth, MAX_QUEUE_DEPTH)

    def test_update_max_queue_depth_valid(self):
        g = _fresh_guard()
        g.update_max_queue_depth(2)
        self.assertEqual(g._max_queue_depth, 2)

    def test_effective_gap_ms_property(self):
        g = _fresh_guard()
        g.update_gap(0.250)
        self.assertAlmostEqual(g.effective_gap_ms, 250.0, places=2)


# ── Inter-request gap ─────────────────────────────────────────────────────────

class TestInterRequestGap(unittest.TestCase):

    def test_gap_enforced_between_requests(self):
        import time
        async def _go():
            g = _fresh_guard()
            g.update_gap(0.050)      # 50 ms
            async with g.request():
                pass
            t0 = time.monotonic()
            async with g.request():
                pass
            elapsed_ms = (time.monotonic() - t0) * 1000
            self.assertGreater(elapsed_ms, 40,
                msg=f"Gap too short: {elapsed_ms:.1f} ms")
        _run(_go())

    def test_no_delay_on_first_request(self):
        import time
        async def _go():
            g = _fresh_guard()
            g.update_gap(0.200)
            t0 = time.monotonic()
            async with g.request():
                pass
            elapsed_ms = (time.monotonic() - t0) * 1000
            self.assertLess(elapsed_ms, 150,
                msg=f"First request unexpectedly delayed: {elapsed_ms:.1f} ms")
        _run(_go())


# ── Registry (BUG-8: bus-level endpoint key) ──────────────────────────────────

class TestRegistry(unittest.TestCase):

    def setUp(self):
        ModbusGuard.clear_registry()

    def tearDown(self):
        ModbusGuard.clear_registry()

    def test_singleton_per_endpoint(self):
        """BUG-8: same endpoint → same guard; different → different."""
        g1 = ModbusGuard.get_or_create("10.0.0.1:502")
        g2 = ModbusGuard.get_or_create("10.0.0.1:502")
        g3 = ModbusGuard.get_or_create("10.0.0.2:502")
        self.assertIs(g1, g2, "Same endpoint must return same guard instance")
        self.assertIsNot(g1, g3, "Different endpoint must return different guard")

    def test_sub_devices_share_guard(self):
        """All RS485 slaves on the same bus must share one guard."""
        g_primary   = ModbusGuard.get_or_create("10.0.0.1:502")
        g_secondary = ModbusGuard.get_or_create("10.0.0.1:502")
        self.assertIs(g_primary, g_secondary)

    def test_endpoint_for_tcp(self):
        ep = ModbusGuard.endpoint_for({"host": "192.168.1.1", "port": "502"})
        self.assertEqual(ep, "192.168.1.1:502")

    def test_endpoint_for_rtu(self):
        ep = ModbusGuard.endpoint_for({"port": "/dev/ttyUSB0"})
        self.assertEqual(ep, "rtu:/dev/ttyUSB0")

    def test_endpoint_for_default_port(self):
        ep = ModbusGuard.endpoint_for({"host": "10.0.0.5"})
        self.assertEqual(ep, "10.0.0.5:502")

    def test_clear_registry(self):
        ModbusGuard.get_or_create("host:502")
        ModbusGuard.clear_registry()
        self.assertEqual(ModbusGuard._registry, {})
