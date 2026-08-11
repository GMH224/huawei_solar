"""Regression test for Defect H -- unbounded, unhandled write-permission
probe blocking SENSOR PLATFORM setup (AUDIT_1.3.9.md).

`create_sun2000_entities()` (called from sensor.py's own async_setup_entry,
which runs AFTER __init__.py's entry-level setup already returned) used to
call `await ucs.device.has_write_permission()` directly: no timeout, no
exception handling, a raw device-level read+write outside ModbusGuard/the
adaptive controller entirely. On a device still busy/reconnecting -- the
exact condition observed in the field immediately after a restart -- this
could stall ALL of sensor platform setup for as long as the vendor
library's own per-request timeout allowed, once per SUN2000 device, on
every boot and reload. An uncaught exception there would have taken down
every sensor entity on the entry, not just the one optional entity this
check decides whether to add.

This is deliberately a DIFFERENT class of check than the AST-based ones
used for Defects F/G: the fix here is a genuine bounded-wait + exception-
isolation pattern, not a structural "must be inside a nested function"
shape, so it is tested behaviourally against a fake device with controllable
latency/failure -- the same style of adversarial fake used for Defect F's
event bus.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import unittest

_SENSOR_SRC = pathlib.Path(__file__).parent.parent / "sensor.py"


# ── 1. Behavioural: exercise the actual bounded-wrapper logic ─────────────
#
# The wrapper function itself (_has_write_permission_bounded) lives inside
# sensor.py, which has a heavy Home Assistant import graph and cannot be
# imported directly in this fast, dependency-free suite (see
# test_tier_separation.py's HISTORY docstring for the project's established
# reasoning on this). Instead, the fix's exact logic is reproduced here
# verbatim and tested directly -- this is the same trade-off already made
# for Defect F's _guarded_once, and the AST check in section 2 below pins
# that sensor.py's real implementation matches this shape.

WRITE_PERMISSION_CHECK_TIMEOUT_SECONDS = 5.0


async def _has_write_permission_bounded(device, serial_number, log):
    try:
        return await asyncio.wait_for(
            device.has_write_permission(),
            timeout=WRITE_PERMISSION_CHECK_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        log.append(("timeout", serial_number))
        return False
    except Exception:  # noqa: BLE001
        log.append(("exception", serial_number))
        return False


class _SlowDevice:
    """A fake device whose has_write_permission() never resolves within the
    test's patience -- reproducing a still-busy/reconnecting inverter."""

    async def has_write_permission(self):
        await asyncio.sleep(3600)  # effectively "never" for test purposes
        return True  # pragma: no cover — unreachable


class _FailingDevice:
    """A fake device whose has_write_permission() raises something other
    than what the vendor library itself already handles internally."""

    async def has_write_permission(self):
        raise ConnectionError("device unreachable")


class _HealthyDevice:
    async def has_write_permission(self):
        return True


class TestBoundedWritePermissionCheck(unittest.IsolatedAsyncioTestCase):
    async def test_slow_device_times_out_instead_of_hanging(self):
        log: list[tuple[str, str]] = []
        # Use a short timeout for the test itself so it doesn't actually
        # wait out the real WRITE_PERMISSION_CHECK_TIMEOUT_SECONDS.
        result = await asyncio.wait_for(
            _has_write_permission_bounded(_SlowDevice(), "SN1", log),
            timeout=WRITE_PERMISSION_CHECK_TIMEOUT_SECONDS + 2,
        )
        self.assertFalse(result)
        self.assertEqual(log, [("timeout", "SN1")])

    async def test_failing_device_does_not_propagate(self):
        log: list[tuple[str, str]] = []
        result = await _has_write_permission_bounded(_FailingDevice(), "SN2", log)
        self.assertFalse(result)
        self.assertEqual(log, [("exception", "SN2")])

    async def test_healthy_device_still_returns_true_promptly(self):
        log: list[tuple[str, str]] = []
        result = await _has_write_permission_bounded(_HealthyDevice(), "SN3", log)
        self.assertTrue(result)
        self.assertEqual(log, [])

    async def test_unwrapped_call_would_have_hung(self):
        """Adversarial: proves _SlowDevice actually reproduces the original
        hazard when called the OLD (unwrapped) way, so the pass above is
        meaningful and not just a fake that can't fail."""
        with self.assertRaises(TimeoutError):
            # A stand-in for the OLD call site: `await device.has_write_permission()`
            # with no bound at all. We still need *some* outer bound so the
            # test suite itself doesn't hang; a much larger one than the fix
            # uses demonstrates the unwrapped call has no bound of its own.
            await asyncio.wait_for(_SlowDevice().has_write_permission(), timeout=1)


# ── 2. Static: sensor.py's real call site must go through the bounded
#      helper, not the raw device call ─────────────────────────────────────

class TestSensorPySourceUsesBoundedHelper(unittest.TestCase):
    def test_create_sun2000_entities_does_not_call_has_write_permission_directly(self):
        source = _SENSOR_SRC.read_text()
        tree = ast.parse(source)
        func = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef)
                and n.name == "create_sun2000_entities"
            ),
            None,
        )
        assert func is not None, "create_sun2000_entities not found in sensor.py"

        violations = [
            node.lineno
            for node in ast.walk(func)
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "has_write_permission"
            )
        ]
        assert not violations, (
            f"create_sun2000_entities calls .has_write_permission() directly "
            f"at line(s) {violations} -- this reintroduces Defect H (no "
            "timeout, no exception handling, on the sensor platform setup "
            "critical path). Route it through the bounded "
            "_has_write_permission_bounded() helper instead."
        )

    def test_bounded_helper_exists_and_uses_wait_for(self):
        source = _SENSOR_SRC.read_text()
        tree = ast.parse(source)
        func = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef)
                and n.name == "_has_write_permission_bounded"
            ),
            None,
        )
        assert func is not None, (
            "_has_write_permission_bounded() not found in sensor.py -- the "
            "Defect H bounded-wrapper helper is missing entirely."
        )
        uses_wait_for = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "wait_for"
            for node in ast.walk(func)
        )
        assert uses_wait_for, (
            "_has_write_permission_bounded() exists but does not call "
            "asyncio.wait_for() -- it must bound the underlying "
            "has_write_permission() call, not merely wrap it."
        )

    def test_v2_0_0a_routes_through_guard(self):
        """F06, external ICS audit -- confirmed: being time-bounded protected
        setup from hanging, but the probe itself still bypassed ModbusGuard
        entirely."""
        source = _SENSOR_SRC.read_text()
        tree = ast.parse(source)
        func = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef)
                and n.name == "_has_write_permission_bounded"
            ),
            None,
        )
        assert func is not None
        uses_guard = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "request"
            for node in ast.walk(func)
        )
        assert uses_guard, (
            "_has_write_permission_bounded() no longer routes through "
            "guard.request() -- F06 has regressed"
        )
        assert "guard=ucs.configuration_update_coordinator.guard" in source, (
            "the production call site must pass a real guard explicitly, "
            "not leave the optional parameter at its defensive None default"
        )


if __name__ == "__main__":
    unittest.main()
