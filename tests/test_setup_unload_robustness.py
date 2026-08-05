"""Regression tests for Defect U -- three findings (1, 2, 3) from an
independent ICS audit of the v1.3.17 package, all confirmed against source
before fixing (AUDIT_1.3.18.md):

  Finding 1 (High): primary_device.login(...) and
     create_sub_device_instance(...) during setup had no bound of their
     own -- the same class of risk as Defect M (v1.3.14), just for two
     call sites that fix hadn't covered. A slow slave device could stall
     discovery of every later one indefinitely, since the loop is
     sequential.

  Finding 2 (High): a setup attempt that failed AFTER already starting a
     device's keep-alive background task had no way to roll that task
     back -- every exception handler in async_setup_entry only ever
     called primary_device.stop(). Since Home Assistant does not
     guarantee async_unload_entry() runs after a failed
     async_setup_entry(), an orphaned keep-alive task could survive to
     interfere with the next setup attempt for the same device.

  Finding 3 (High): async_unload_entry() awaited
     primary_device.client.disconnect() with no timeout, sitting BEFORE
     every teardown loop that follows it (telemetry, the adaptive
     controller, keep-alive, battery health, the shared guard). A wedged
     transport blocking there would prevent ALL of that cleanup from ever
     running.

Following this project's established trade-off for __init__.py (too
heavy to import directly -- see test_learning_gate_unsub.py's precedent):
the cleanup-callback runner's logic is reproduced in isolation for
behavioural testing (it has no Home Assistant or device-layer
dependencies of its own); everything else is verified statically (AST)
against the real source.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import unittest

_INIT_SRC = pathlib.Path(__file__).parent.parent / "__init__.py"


# ═══════════════════════════════════════════════════════════════════════
# Finding 2 (part 1) — the cleanup-callback runner itself
# ═══════════════════════════════════════════════════════════════════════

async def _run_cleanup_callbacks(callbacks, log):
    """Exact mirror of the real helper added for this fix."""
    import inspect
    for cb in reversed(callbacks):
        try:
            result = cb()
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001
            log.append("error")


async def _run_cleanup_callbacks_unguarded(callbacks):
    """The OLD (hypothetical, never-existed) shape: no try/except at all --
    reproduced only to prove, adversarially, that ONE failing callback
    would otherwise prevent the rest from running."""
    for cb in reversed(callbacks):
        result = cb()
        if hasattr(result, "__await__"):
            await result


class TestCleanupCallbackRunner(unittest.IsolatedAsyncioTestCase):
    async def test_runs_in_reverse_order(self):
        order = []
        callbacks = [lambda: order.append(1), lambda: order.append(2), lambda: order.append(3)]
        await _run_cleanup_callbacks(callbacks, [])
        self.assertEqual(order, [3, 2, 1])

    async def test_one_failing_callback_does_not_block_the_rest(self):
        order = []

        def _boom():
            raise RuntimeError("simulated cleanup failure")

        callbacks = [lambda: order.append("first"), _boom, lambda: order.append("last")]
        log: list[str] = []
        await _run_cleanup_callbacks(callbacks, log)

        self.assertEqual(order, ["last", "first"], "both good callbacks must still run")
        self.assertEqual(log, ["error"], "the failure must be recorded, not silently lost")

    async def test_adversarial_unguarded_runner_really_does_stop_early(self):
        """Proves the hazard the try/except protects against is real: an
        unguarded runner lets one exception prevent everything registered
        before it (in reverse order) from running."""
        order = []

        def _boom():
            raise RuntimeError("simulated cleanup failure")

        callbacks = [lambda: order.append("first"), _boom, lambda: order.append("last")]
        with self.assertRaises(RuntimeError):
            await _run_cleanup_callbacks_unguarded(callbacks)
        self.assertEqual(order, ["last"], "unguarded: everything before the failure never ran")

    async def test_supports_async_callables(self):
        order = []

        async def _async_cleanup():
            order.append("async")

        await _run_cleanup_callbacks([_async_cleanup, lambda: order.append("sync")], [])
        self.assertEqual(order, ["sync", "async"])


# ═══════════════════════════════════════════════════════════════════════
# Static (AST) checks against the real source
# ═══════════════════════════════════════════════════════════════════════

class TestFinding1BoundedLoginAndSlaveDiscovery(unittest.TestCase):
    def _async_setup_entry(self, tree):
        func = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_setup_entry"
            ),
            None,
        )
        assert func is not None, "async_setup_entry not found"
        return func

    def test_login_is_wrapped_in_wait_for(self):
        tree = ast.parse(_INIT_SRC.read_text())
        func = self._async_setup_entry(tree)
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
                    and arg.func.attr == "login"
                    for arg in node.args
                ):
                    found = True
        assert found, (
            "primary_device.login(...) is not wrapped in asyncio.wait_for(...) "
            "-- this reintroduces Finding 1 for the login call."
        )

    def test_slave_discovery_is_wrapped_in_wait_for(self):
        tree = ast.parse(_INIT_SRC.read_text())
        func = self._async_setup_entry(tree)
        found = False
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "wait_for"
            ):
                if any(
                    isinstance(arg, ast.Call)
                    and isinstance(arg.func, ast.Name)
                    and arg.func.id == "create_sub_device_instance"
                    for arg in node.args
                ):
                    found = True
        assert found, (
            "create_sub_device_instance(...) is not wrapped in "
            "asyncio.wait_for(...) -- this reintroduces Finding 1 for slave "
            "discovery."
        )


class TestFinding2CleanupOnPartialSetupFailure(unittest.TestCase):
    def test_run_cleanup_callbacks_helper_exists(self):
        tree = ast.parse(_INIT_SRC.read_text())
        found = any(
            isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_cleanup_callbacks"
            for n in ast.walk(tree)
        )
        assert found, "_run_cleanup_callbacks() not found in __init__.py"

    def test_keepalive_stop_is_registered_after_start(self):
        tree = ast.parse(_INIT_SRC.read_text())
        func = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_setup_inverter_device_data"
            ),
            None,
        )
        assert func is not None
        source_segment = ast.get_source_segment(_INIT_SRC.read_text(), func) or ""
        assert "register_cleanup(keepalive.stop)" in source_segment, (
            "_setup_inverter_device_data no longer registers keepalive.stop "
            "for cleanup -- this reintroduces Finding 2's specific hazard "
            "(an orphaned keep-alive task surviving a later setup failure)."
        )

    def test_async_setup_entry_calls_cleanup_runner_at_least_five_times(self):
        """One call per exception handler (ConnectionInterruptedException,
        ConnectionException, TimeoutError, HuaweiSolarException, and the
        generic Exception catch-all)."""
        tree = ast.parse(_INIT_SRC.read_text())
        func = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_setup_entry"
            ),
            None,
        )
        assert func is not None
        call_count = sum(
            1 for n in ast.walk(func)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_run_cleanup_callbacks"
        )
        assert call_count >= 5, (
            f"_run_cleanup_callbacks is called {call_count} time(s) in "
            "async_setup_entry, expected at least 5 (one per exception "
            "handler) -- this reintroduces part of Finding 2."
        )


class TestFinding3BoundedDisconnect(unittest.TestCase):
    def test_disconnect_is_wrapped_in_wait_for(self):
        tree = ast.parse(_INIT_SRC.read_text())
        func = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_unload_entry"
            ),
            None,
        )
        assert func is not None, "async_unload_entry not found"
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
                    and arg.func.attr == "disconnect"
                    for arg in node.args
                ):
                    found = True
        assert found, (
            "primary_device.client.disconnect() is not wrapped in "
            "asyncio.wait_for(...) -- this reintroduces Finding 3."
        )

    def test_disconnect_failure_is_caught_not_propagated(self):
        source = _INIT_SRC.read_text()
        # Use rfind: the module's own explanatory comment mentions
        # "primary_device.client.disconnect()" (describing the OLD,
        # pre-fix call) before the real code does, so find() would match
        # the comment instead of the actual call site.
        idx = source.rfind("primary_device.client.disconnect()")
        assert idx != -1
        window = source[idx: idx + 400]
        assert "except Exception" in window, (
            "No except Exception handler found near the bounded disconnect "
            "call -- a disconnect failure could still propagate and skip "
            "the teardown loops that follow, reintroducing Finding 3."
        )


if __name__ == "__main__":
    unittest.main()
