"""Tests for modbus_guard.py — covers the double-decrement fix (Bug 1).

Bug fixed
---------
When acquiring the internal asyncio.Lock timed out, _queue_depth was decremented
twice: once in a now-removed inner `except TimeoutError` block, and again by the
outer `except Exception` handler.  The fix removes the inner decrement so the
outer handler is the sole cleanup path.

Test strategy
-------------
All tests run inside a real asyncio event loop (pytest-asyncio).  We
deliberately avoid mocking asyncio primitives so that the concurrency behaviour
is exercised as it would be in production.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Inline stub for modbus_guard so the test file is self-contained
# (no Home Assistant environment needed).
# ---------------------------------------------------------------------------

import sys
import types

# Build a minimal stub module so we can import modbus_guard without HA deps.
_stub = types.ModuleType("homeassistant")
sys.modules.setdefault("homeassistant", _stub)

import importlib, pathlib

_src = pathlib.Path(__file__).parent.parent / "modbus_guard.py"
_spec = importlib.util.spec_from_file_location("modbus_guard", _src)
modbus_guard = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(modbus_guard)  # type: ignore[union-attr]

ModbusGuard = modbus_guard.ModbusGuard
QUEUE_WAIT_TIMEOUT = modbus_guard.QUEUE_WAIT_TIMEOUT
MIN_INTER_REQUEST_GAP = modbus_guard.MIN_INTER_REQUEST_GAP


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_guard(serial: str = "SN_TEST") -> ModbusGuard:
    """Return a brand-new ModbusGuard, bypassing the class-level registry."""
    guard = ModbusGuard.__new__(ModbusGuard)
    guard.serial_number = serial
    guard._lock = asyncio.Lock()
    guard._last_request_end = 0.0
    guard._queue_depth = 0
    return guard


# ---------------------------------------------------------------------------
# Bug 1 — double-decrement on lock-acquire timeout
# ---------------------------------------------------------------------------

class TestQueueDepthAccounting:
    """_queue_depth must never go negative regardless of the failure mode."""

    @pytest.mark.asyncio
    async def test_depth_zero_after_successful_round_trip(self):
        """Normal acquire → release leaves depth at 0."""
        guard = _fresh_guard()
        assert guard.queue_depth == 0
        async with guard.request():
            assert guard.queue_depth == 1
        assert guard.queue_depth == 0

    @pytest.mark.asyncio
    async def test_depth_not_negative_on_lock_timeout(self):
        """Lock-acquire timeout must decrement exactly once, not twice.

        Pre-fix: inner `except TimeoutError` decremented, then the outer
        `except Exception` fired again → depth went to -1.
        """
        guard = _fresh_guard()

        # Hold the lock so the second acquire will time out.
        await guard._lock.acquire()

        with patch.object(
            modbus_guard,
            "QUEUE_WAIT_TIMEOUT",
            timedelta(milliseconds=10),
        ):
            with pytest.raises(TimeoutError):
                async with guard.request():
                    pass  # should not reach here

        # The lock is still held by us; release it for cleanup.
        guard._lock.release()

        assert guard.queue_depth == 0, (
            f"queue_depth should be 0 after timeout, got {guard.queue_depth}. "
            "This indicates the double-decrement bug is present."
        )

    @pytest.mark.asyncio
    async def test_depth_not_negative_on_repeated_timeouts(self):
        """Multiple consecutive timeouts must not drive depth below zero."""
        guard = _fresh_guard()
        await guard._lock.acquire()

        for _ in range(5):
            with patch.object(
                modbus_guard,
                "QUEUE_WAIT_TIMEOUT",
                timedelta(milliseconds=10),
            ):
                with pytest.raises(TimeoutError):
                    async with guard.request():
                        pass

        guard._lock.release()
        assert guard.queue_depth == 0

    @pytest.mark.asyncio
    async def test_depth_tracks_concurrent_waiters(self):
        """With two concurrent waiters, depth rises to 2 before falling back."""
        guard = _fresh_guard()
        await guard._lock.acquire()  # block the lock

        results: list[int] = []

        async def waiter():
            with patch.object(
                modbus_guard,
                "QUEUE_WAIT_TIMEOUT",
                timedelta(seconds=1),
            ):
                with pytest.raises(TimeoutError):
                    async with guard.request():
                        pass

        task1 = asyncio.create_task(waiter())
        task2 = asyncio.create_task(waiter())

        # Give both tasks time to increment _queue_depth before timing out.
        await asyncio.sleep(0.02)
        results.append(guard.queue_depth)  # should be 2

        # Let them time out (QUEUE_WAIT_TIMEOUT = 10 ms patch above already
        # means they've timed out, but we need to await).
        await asyncio.gather(task1, task2, return_exceptions=True)

        guard._lock.release()
        assert results[0] == 2, f"Expected 2 concurrent waiters, got {results[0]}"
        assert guard.queue_depth == 0

    @pytest.mark.asyncio
    async def test_is_busy_reflects_lock_state(self):
        guard = _fresh_guard()
        assert not guard.is_busy

        async with guard.request():
            assert guard.is_busy

        assert not guard.is_busy

    @pytest.mark.asyncio
    async def test_inter_request_gap_enforced(self):
        """MIN_INTER_REQUEST_GAP must be respected between consecutive requests."""
        import time

        guard = _fresh_guard()

        with patch.object(
            modbus_guard,
            "MIN_INTER_REQUEST_GAP",
            timedelta(milliseconds=50),
        ):
            async with guard.request():
                pass
            t0 = time.monotonic()
            async with guard.request():
                pass
            elapsed_ms = (time.monotonic() - t0) * 1000

        # Allow a generous margin: at least 40 ms (some CI runners are slow).
        assert elapsed_ms >= 40, (
            f"Inter-request gap too short: {elapsed_ms:.1f} ms (expected ≥ 40 ms)"
        )

    @pytest.mark.asyncio
    async def test_registry_singleton_per_serial(self):
        """get_or_create returns the same instance for the same serial number."""
        ModbusGuard.clear_registry()
        g1 = ModbusGuard.get_or_create("SN-AAA")
        g2 = ModbusGuard.get_or_create("SN-AAA")
        g3 = ModbusGuard.get_or_create("SN-BBB")
        assert g1 is g2
        assert g1 is not g3
        ModbusGuard.clear_registry()
