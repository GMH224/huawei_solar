"""Regression tests for four defects found and fixed together
(AUDIT_1.3.14.md), all independently reported by the operator (L, M, N) or
found as a bonus while reviewing switch.py for the same session (O):

  L: the deferred first-poll background task (Defect K, v1.3.13) had no
     stored handle and no tie to the config entry's lifecycle, so it could
     outlive a reload/unload and fire stray Modbus traffic against a stale
     coordinator.

  M: create_device_instance() -- the very first await in
     async_setup_entry -- had no bound of its own, so a slow/still-
     reconnecting device could be cancelled externally by Home Assistant's
     own setup timeout (asyncio.CancelledError, uncaught by any existing
     handler since CancelledError is a BaseException, not an Exception)
     instead of failing cleanly with ConfigEntryNotReady.

  N: the one-time optimizer discovery scan
     (device.get_optimizer_system_information_data()) had no bound either,
     for the same reason as M, on the same setup critical path.

  O: MAX_STATUS_CHANGE_TIME_SECONDS in switch.py was 3000 (50 minutes)
     while its own comment said "5 minutes" (300s) -- a 10x mismatch found
     while reviewing this file for the operator's separate guard-bypass
     finding.

Following this project's established trade-off for files too heavy to
import directly (see test_learning_gate_unsub.py's HISTORY docstring):
behavioural sections reproduce the exact fixed (and, for L, the exact old)
logic in isolation; static sections check the real source directly.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import unittest

_INIT_SRC = pathlib.Path(__file__).parent.parent / "__init__.py"
_COORD_SRC = pathlib.Path(__file__).parent.parent / "update_coordinator.py"
_SWITCH_SRC = pathlib.Path(__file__).parent.parent / "switch.py"


# ═══════════════════════════════════════════════════════════════════════
# Defect L — deferred first-poll task lifecycle
# ═══════════════════════════════════════════════════════════════════════

class _OldDeferredPoll:
    """Reproduces the pre-fix pattern: bare task, no entry tie, no
    shutdown guard."""

    def __init__(self, start_delay: float):
        self._start_delay = start_delay
        self.refresh_calls = 0

    def schedule(self):
        async def _deferred():
            await asyncio.sleep(self._start_delay)
            self.refresh_calls += 1  # stand-in for async_request_refresh()

        return asyncio.ensure_future(_deferred())


class _NewDeferredPoll:
    """Reproduces the v1.3.14 fixed pattern: shutdown flag checked before
    the (stand-in) refresh call. The real fix also ties the task to
    entry.async_create_background_task for automatic cancellation; that
    part is exercised via the static AST checks below, since it requires a
    real ConfigEntry/hass pair this fast suite doesn't construct."""

    def __init__(self, start_delay: float):
        self._start_delay = start_delay
        self._shutdown = False
        self.refresh_calls = 0

    def mark_shutdown(self):
        self._shutdown = True

    def schedule(self):
        async def _deferred():
            await asyncio.sleep(self._start_delay)
            if self._shutdown:
                return
            self.refresh_calls += 1  # stand-in for async_request_refresh()

        return asyncio.ensure_future(_deferred())


class TestDeferredPollLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_old_pattern_fires_even_after_shutdown(self):
        """Adversarial: proves the OLD pattern really does fire stray
        traffic after 'unload', so the fix below is meaningful."""
        old = _OldDeferredPoll(start_delay=0.05)
        task = old.schedule()
        # Simulate unload happening immediately (nothing to mark -- the
        # old pattern has no shutdown concept at all).
        await asyncio.sleep(0.2)
        self.assertEqual(old.refresh_calls, 1, "old pattern still refreshes after 'unload'")
        await task

    async def test_new_pattern_skips_refresh_after_shutdown(self):
        new = _NewDeferredPoll(start_delay=0.05)
        task = new.schedule()
        new.mark_shutdown()  # simulate entry unload happening before the delay elapses
        await asyncio.sleep(0.2)
        self.assertEqual(new.refresh_calls, 0, "fixed pattern must not refresh a stale coordinator")
        await task

    async def test_new_pattern_still_refreshes_normally_without_shutdown(self):
        """No regression: if the entry never unloads, the deferred poll
        still happens exactly as before."""
        new = _NewDeferredPoll(start_delay=0.05)
        task = new.schedule()
        await asyncio.sleep(0.2)
        self.assertEqual(new.refresh_calls, 1)
        await task


class TestDeferredPollSourceUsesLifecycleGuards(unittest.TestCase):
    def test_schedule_uses_entry_background_task(self):
        source = _COORD_SRC.read_text()
        tree = ast.parse(source)
        func = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_schedule_deferred_first_poll"
            ),
            None,
        )
        assert func is not None, "_schedule_deferred_first_poll not found"
        uses_background_task = any(
            (
                isinstance(node, ast.Attribute) and node.attr == "async_create_background_task"
            )
            or (
                isinstance(node, ast.Constant)
                and node.value == "async_create_background_task"
            )
            for node in ast.walk(func)
        )
        assert uses_background_task, (
            "_schedule_deferred_first_poll no longer uses "
            "entry.async_create_background_task() -- this reintroduces "
            "Defect L (task not tied to the entry's lifecycle)."
        )

    def test_deferred_coroutine_checks_shutdown_flag(self):
        source = _COORD_SRC.read_text()
        tree = ast.parse(source)
        func = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_schedule_deferred_first_poll"
            ),
            None,
        )
        assert func is not None
        checks_shutdown = any(
            isinstance(node, ast.Attribute) and node.attr == "_shutdown"
            for node in ast.walk(func)
        )
        assert checks_shutdown, (
            "_schedule_deferred_first_poll's deferred coroutine no longer "
            "checks self._shutdown -- this reintroduces Defect L's second "
            "line of defence."
        )

    def test_init_registers_shutdown_callback(self):
        source = _COORD_SRC.read_text()
        tree = ast.parse(source)
        init = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "__init__"
                and any(a.arg == "device" for a in n.args.args)
            ),
            None,
        )
        assert init is not None, "HuaweiSolarUpdateCoordinator.__init__ not found"
        registers_unload = any(
            isinstance(node, ast.Attribute) and node.attr == "async_on_unload"
            for node in ast.walk(init)
        )
        assert registers_unload, (
            "HuaweiSolarUpdateCoordinator.__init__ no longer registers an "
            "async_on_unload callback -- _shutdown would never be set."
        )


# ═══════════════════════════════════════════════════════════════════════
# Defect M — create_device_instance() setup-timeout bound
# ═══════════════════════════════════════════════════════════════════════

class _SlowConnect:
    async def __call__(self):
        await asyncio.sleep(3600)


class TestDeviceConnectBound(unittest.IsolatedAsyncioTestCase):
    async def test_slow_connect_raises_timeout_not_hangs(self):
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(_SlowConnect()(), timeout=0.1)

    def test_source_wraps_create_device_instance_in_wait_for(self):
        tree = ast.parse(_INIT_SRC.read_text())
        setup_func = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_setup_entry"
            ),
            None,
        )
        assert setup_func is not None
        found = False
        for node in ast.walk(setup_func):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "wait_for"
            ):
                if any(
                    isinstance(arg, ast.Call)
                    and isinstance(arg.func, ast.Name)
                    and arg.func.id == "create_device_instance"
                    for arg in node.args
                ):
                    found = True
        assert found, (
            "create_device_instance(...) is not wrapped in asyncio.wait_for(...) "
            "in async_setup_entry -- this reintroduces Defect M (no bound "
            "against Home Assistant's own external setup-timeout "
            "cancellation)."
        )

    def test_source_converts_timeout_to_config_entry_not_ready(self):
        source = _INIT_SRC.read_text()
        # A simple, targeted textual check: ConfigEntryNotReady must be
        # raised somewhere near a `except TimeoutError` that mentions
        # connecting/identifying the inverter -- confirms the conversion
        # exists, without over-fitting to exact line numbers.
        idx = source.find("primary_device = await asyncio.wait_for")
        assert idx != -1, "the bounded create_device_instance call was not found"
        window = source[idx: idx + 1200]
        assert "except TimeoutError" in window, (
            "No except TimeoutError handler found near the bounded "
            "create_device_instance() call."
        )
        assert "ConfigEntryNotReady" in window, (
            "The TimeoutError handler near create_device_instance() does "
            "not raise ConfigEntryNotReady -- this reintroduces Defect M."
        )


# ═══════════════════════════════════════════════════════════════════════
# Defect N — optimizer discovery setup-timeout bound
# ═══════════════════════════════════════════════════════════════════════

class TestOptimizerDiscoveryBound(unittest.IsolatedAsyncioTestCase):
    async def test_slow_discovery_raises_timeout_not_hangs(self):
        async def _slow_discovery():
            await asyncio.sleep(3600)

        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(_slow_discovery(), timeout=0.1)

    def test_source_wraps_optimizer_discovery_in_wait_for(self):
        tree = ast.parse(_INIT_SRC.read_text())
        func = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef)
                and n.name == "_setup_inverter_device_data"
            ),
            None,
        )
        assert func is not None
        found = False
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "wait_for"
            ):
                if any(
                    isinstance(arg, ast.Call)
                    and isinstance(arg.func, ast.Attribute)
                    and arg.func.attr == "get_optimizer_system_information_data"
                    for arg in node.args
                ):
                    found = True
        assert found, (
            "get_optimizer_system_information_data(...) is not wrapped in "
            "asyncio.wait_for(...) in _setup_inverter_device_data -- this "
            "reintroduces Defect N."
        )

    def test_source_has_dedicated_timeout_handler(self):
        source = _INIT_SRC.read_text()
        idx = source.find("device.get_optimizer_system_information_data()")
        assert idx != -1
        window = source[idx: idx + 1500]
        assert "except TimeoutError" in window, (
            "No dedicated except TimeoutError handler found near the "
            "bounded optimizer discovery call."
        )


# ═══════════════════════════════════════════════════════════════════════
# Defect O — MAX_STATUS_CHANGE_TIME_SECONDS mismatch
# ═══════════════════════════════════════════════════════════════════════

class TestSwitchTimingConstantMatchesItsComment(unittest.TestCase):
    def test_constant_equals_300_seconds(self):
        tree = ast.parse(_SWITCH_SRC.read_text())
        assign = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == "MAX_STATUS_CHANGE_TIME_SECONDS"
                    for t in n.targets
                )
            ),
            None,
        )
        assert assign is not None, "MAX_STATUS_CHANGE_TIME_SECONDS not found in switch.py"
        assert isinstance(assign.value, ast.Constant)
        assert assign.value.value == 300, (
            f"MAX_STATUS_CHANGE_TIME_SECONDS is {assign.value.value}, not 300 "
            "(5 minutes) -- this reintroduces Defect O's mismatch between "
            "the constant and its own documented intent."
        )


if __name__ == "__main__":
    unittest.main()
