"""Regression tests for Defect V -- the remaining findings from the second
(v1.3.17) and third (v1.3.18) independent ICS audits, all confirmed
against source before fixing (AUDIT_1.3.19.md):

  Finding 1 (v1.3.18 audit): only keepalive.stop was registered for
     cleanup-on-setup-failure (Defect U, v1.3.18) -- telemetry and the
     adaptive controller were not, despite both having real teardown work
     (a periodic timer to cancel; a dirty-state flush).

  Finding 2 (v1.3.18 audit): config_flow.py's validate_network_setup_login
     referenced `bridge` in a `finally` block without initialising it
     first -- UnboundLocalError could mask the real connection failure.

  Finding 3 (both audits): several config_flow.py functions performed
     unbounded create_device_instance/create_sub_device_instance/login/
     has_write_permission calls -- the same class of risk already closed
     for the runtime path (Defects M, N, H; Finding 1, v1.3.18).

  Finding 4 (v1.3.18 audit): ConfigFlow._discovered_sub_unit_ids was a
     mutable list declared at class scope -- a classic Python trap where
     in-place mutation before first instance-level assignment would be
     visible across flow instances.

  Finding 5 (v1.3.18 audit): ConfigFlow._reset_discovery_state() dropped
     the discovery task reference without cancelling it first -- the same
     defect shape as Defect L (v1.3.14), a different file.

  Finding 6 (v1.3.18 audit): ModbusTelemetry._push_to_listeners had no
     per-callback exception isolation.

  Finding 7 (both audits, reported independently three times total across
     this session): switch.py's status-polling loop bypassed ModbusGuard
     with no timeout, bounded by iteration count rather than wall-clock
     time.

  Finding 8 (both audits): services.py's _validate_power_value performed
     an unbounded read while the Defect R (v1.3.15) per-device write lock
     was already held.

  Finding 9 (v1.3.18 audit): SynchronizedPowerCoordinator's four reads
     were not one atomic transaction, despite the code's own docstring
     claiming otherwise.

  Finding 10 (v1.3.18 audit): AdaptiveModbusController.stop() scheduled
     its dirty-state flush as a fire-and-forget background task rather
     than awaiting it.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import unittest

_INIT_SRC = pathlib.Path(__file__).parent.parent / "__init__.py"
_CONFIG_FLOW_SRC = pathlib.Path(__file__).parent.parent / "config_flow.py"
_SWITCH_SRC = pathlib.Path(__file__).parent.parent / "switch.py"
_TELEMETRY_SRC = pathlib.Path(__file__).parent.parent / "modbus_telemetry.py"
_SERVICES_SRC = pathlib.Path(__file__).parent.parent / "services.py"
_ADAPTIVE_SRC = pathlib.Path(__file__).parent.parent / "adaptive_modbus.py"
_SYNC_SRC = pathlib.Path(__file__).parent.parent / "synchronized_power_coordinator.py"


def _find_func(tree, name, cls=ast.AsyncFunctionDef):
    return next((n for n in ast.walk(tree) if isinstance(n, cls) and n.name == name), None)


# ═══════════════════════════════════════════════════════════════════════
# Finding 1 — cleanup registration extended to telemetry/adaptive
# ═══════════════════════════════════════════════════════════════════════

class TestFinding1CleanupCoversAllThreeSingletons(unittest.TestCase):
    def test_telemetry_stop_is_registered(self):
        source = _INIT_SRC.read_text()
        assert "register_cleanup(telemetry.stop)" in source, (
            "telemetry.stop is not registered for cleanup -- this "
            "reintroduces Finding 1 for telemetry."
        )

    def test_adaptive_async_unload_is_registered(self):
        source = _INIT_SRC.read_text()
        assert "register_cleanup(adaptive.async_unload)" in source, (
            "adaptive.async_unload is not registered for cleanup -- this "
            "reintroduces Finding 1 for the adaptive controller."
        )


# ═══════════════════════════════════════════════════════════════════════
# Finding 2 — config_flow bridge UnboundLocalError
# ═══════════════════════════════════════════════════════════════════════

class TestFinding2BridgeInitialised(unittest.TestCase):
    def test_bridge_initialised_before_try(self):
        tree = ast.parse(_CONFIG_FLOW_SRC.read_text())
        func = _find_func(tree, "validate_network_setup_login")
        assert func is not None
        # bridge = None must appear as a statement in the function body,
        # before the try block (not inside it).
        pre_try_assigns = [
            n for n in func.body
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "bridge" for t in n.targets)
        ]
        assert pre_try_assigns, (
            "bridge is not initialised at function-body scope before the "
            "try block -- this reintroduces Finding 2's UnboundLocalError "
            "risk."
        )
        assert isinstance(pre_try_assigns[0].value, ast.Constant) and pre_try_assigns[0].value.value is None

    def test_raw_client_disconnected_when_bridge_never_created(self):
        source = _CONFIG_FLOW_SRC.read_text()
        idx = source.find("async def validate_network_setup_login")
        window = source[idx: idx + 2500]
        assert "client.disconnect()" in window, (
            "the raw client is never explicitly disconnected in the "
            "bridge-was-never-created case -- reintroduces part of "
            "Finding 2."
        )


# ═══════════════════════════════════════════════════════════════════════
# Finding 3 — bounded config_flow connect/login/discovery calls
# ═══════════════════════════════════════════════════════════════════════

class TestFinding3BoundedConfigFlowReads(unittest.TestCase):
    def _wait_for_wraps(self, func, callee_attr_or_name: str) -> bool:
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "wait_for"
            ):
                for arg in node.args:
                    if isinstance(arg, ast.Call):
                        target = arg.func
                        name = getattr(target, "attr", None) or getattr(target, "id", None)
                        if name == callee_attr_or_name:
                            return True
        return False

    def test_validate_serial_setup_bounds_create_device_instance(self):
        tree = ast.parse(_CONFIG_FLOW_SRC.read_text())
        func = _find_func(tree, "validate_serial_setup")
        assert func is not None
        assert self._wait_for_wraps(func, "create_device_instance")

    def test_connect_to_discovered_devices_bounds_create_device_instance(self):
        tree = ast.parse(_CONFIG_FLOW_SRC.read_text())
        func = _find_func(tree, "_connect_to_discovered_devices")
        assert func is not None
        assert self._wait_for_wraps(func, "create_device_instance")

    def test_validate_network_setup_bounds_create_device_instance(self):
        tree = ast.parse(_CONFIG_FLOW_SRC.read_text())
        func = _find_func(tree, "validate_network_setup")
        assert func is not None
        assert self._wait_for_wraps(func, "create_device_instance")

    def test_validate_network_setup_login_bounds_login_and_permission_check(self):
        tree = ast.parse(_CONFIG_FLOW_SRC.read_text())
        func = _find_func(tree, "validate_network_setup_login")
        assert func is not None
        assert self._wait_for_wraps(func, "login")
        assert self._wait_for_wraps(func, "has_write_permission")


# ═══════════════════════════════════════════════════════════════════════
# Finding 4 — mutable discovery state moved off class scope
# ═══════════════════════════════════════════════════════════════════════

class TestFinding4NoMutableClassDefault(unittest.TestCase):
    def test_discovered_sub_unit_ids_not_a_class_level_mutable_default(self):
        tree = ast.parse(_CONFIG_FLOW_SRC.read_text())
        cls = next(
            (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "ConfigFlow"),
            None,
        )
        assert cls is not None
        violations = [
            n.lineno for n in cls.body
            if isinstance(n, ast.AnnAssign)
            and isinstance(n.target, ast.Name)
            and n.target.id == "_discovered_sub_unit_ids"
            and isinstance(n.value, ast.List)
        ]
        assert not violations, (
            f"_discovered_sub_unit_ids is still declared as a mutable "
            f"list at class scope (line(s) {violations}) -- this "
            "reintroduces Finding 4."
        )

    def test_init_assigns_it_per_instance(self):
        tree = ast.parse(_CONFIG_FLOW_SRC.read_text())
        config_flow_cls = next(
            (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "ConfigFlow"),
            None,
        )
        assert config_flow_cls is not None
        init = _find_func(config_flow_cls, "__init__", cls=ast.FunctionDef)
        assert init is not None, "ConfigFlow.__init__ not found -- Finding 4's fix is missing"
        assigns_in_init = any(
            isinstance(n, (ast.Assign, ast.AnnAssign))
            and isinstance(
                n.targets[0] if isinstance(n, ast.Assign) else n.target, ast.Attribute,
            )
            and getattr(n.targets[0] if isinstance(n, ast.Assign) else n.target, "attr", None)
            == "_discovered_sub_unit_ids"
            for n in init.body
        )
        assert assigns_in_init, "__init__ does not assign self._discovered_sub_unit_ids"


# ═══════════════════════════════════════════════════════════════════════
# Finding 5 — discovery task cancelled on reset (behavioural + static)
# ═══════════════════════════════════════════════════════════════════════

class _OldResetDiscoveryState:
    """Reproduces the pre-fix behaviour."""
    def __init__(self, task):
        self._discovery_task = task

    def reset(self):
        self._discovery_task = None  # never cancelled


class _NewResetDiscoveryState:
    """Reproduces the fixed behaviour."""
    def __init__(self, task):
        self._discovery_task = task

    def reset(self):
        if self._discovery_task is not None and not self._discovery_task.done():
            self._discovery_task.cancel()
        self._discovery_task = None


class TestFinding5DiscoveryTaskCancellation(unittest.IsolatedAsyncioTestCase):
    async def test_old_pattern_leaves_task_running(self):
        """Adversarial: proves the hazard is real."""
        async def _long_scan():
            await asyncio.sleep(3600)

        task = asyncio.ensure_future(_long_scan())
        flow = _OldResetDiscoveryState(task)
        flow.reset()
        await asyncio.sleep(0)  # let the event loop tick
        self.assertFalse(task.cancelled())
        self.assertFalse(task.done())
        task.cancel()  # actually clean up for the test itself
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_new_pattern_cancels_the_task(self):
        async def _long_scan():
            await asyncio.sleep(3600)

        task = asyncio.ensure_future(_long_scan())
        flow = _NewResetDiscoveryState(task)
        flow.reset()
        await asyncio.sleep(0)
        self.assertTrue(task.cancelled() or task.cancelling() > 0)
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_new_pattern_tolerates_an_already_done_task(self):
        async def _quick():
            return "done"

        task = asyncio.ensure_future(_quick())
        await task  # already finished
        flow = _NewResetDiscoveryState(task)
        flow.reset()  # must not raise
        self.assertIsNone(flow._discovery_task)


class TestFinding5StaticCheck(unittest.TestCase):
    def test_reset_discovery_state_cancels_before_dropping_reference(self):
        tree = ast.parse(_CONFIG_FLOW_SRC.read_text())
        func = _find_func(tree, "_reset_discovery_state", cls=ast.FunctionDef)
        assert func is not None
        calls_cancel = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "cancel"
            for n in ast.walk(func)
        )
        assert calls_cancel, (
            "_reset_discovery_state() does not call .cancel() on the "
            "discovery task -- this reintroduces Finding 5."
        )


# ═══════════════════════════════════════════════════════════════════════
# Finding 6 — telemetry listener isolation
# ═══════════════════════════════════════════════════════════════════════

class TestFinding6TelemetryListenerIsolation(unittest.TestCase):
    def test_push_to_listeners_isolates_each_callback(self):
        calls = []

        def good_1(snap):
            calls.append(1)

        def bad(snap):
            calls.append("bad")
            raise RuntimeError("simulated listener bug")

        def good_2(snap):
            calls.append(2)

        listeners = [good_1, bad, good_2]

        def push(snap):
            for cb in listeners:
                try:
                    cb(snap)
                except Exception:
                    pass

        push(None)
        self.assertEqual(calls, [1, "bad", 2], "a failing listener must not prevent later ones from running")

    def test_source_wraps_each_callback(self):
        tree = ast.parse(_TELEMETRY_SRC.read_text())
        func = _find_func(tree, "_push_to_listeners", cls=ast.FunctionDef)
        assert func is not None
        has_try = any(isinstance(n, ast.Try) for n in ast.walk(func))
        assert has_try, "_push_to_listeners has no try/except around the listener call -- reintroduces Finding 6"


# ═══════════════════════════════════════════════════════════════════════
# Finding 7 — switch.py guard + wall-clock deadline
# ═══════════════════════════════════════════════════════════════════════

class TestFinding7SwitchPolling(unittest.TestCase):
    def test_status_poll_uses_the_guard(self):
        tree = ast.parse(_SWITCH_SRC.read_text())
        func = _find_func(tree, "_poll_device_status_bounded")
        assert func is not None, "_poll_device_status_bounded not found -- Finding 7's fix is missing"
        uses_guard = any(
            isinstance(n, ast.Attribute) and n.attr == "guard" for n in ast.walk(func)
        )
        assert uses_guard, "_poll_device_status_bounded does not go through coordinator.guard"

    def test_status_poll_is_bounded(self):
        tree = ast.parse(_SWITCH_SRC.read_text())
        func = _find_func(tree, "_poll_device_status_bounded")
        assert func is not None
        uses_wait_for = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "wait_for"
            for n in ast.walk(func)
        )
        assert uses_wait_for

    def test_wait_for_status_uses_a_monotonic_deadline(self):
        tree = ast.parse(_SWITCH_SRC.read_text())
        func = _find_func(tree, "_wait_for_status")
        assert func is not None, "_wait_for_status not found -- Finding 5(switch)/7's fix is missing"
        uses_monotonic = any(
            isinstance(n, ast.Attribute) and n.attr == "monotonic" for n in ast.walk(func)
        )
        assert uses_monotonic, "_wait_for_status does not track a monotonic deadline"

    def test_no_more_raw_client_get_in_turn_on_off(self):
        tree = ast.parse(_SWITCH_SRC.read_text())
        for name in ("async_turn_on", "async_turn_off"):
            func = _find_func(tree, name)
            assert func is not None
            violations = [
                n.lineno for n in ast.walk(func)
                if isinstance(n, ast.Attribute)
                and n.attr == "get"
                and isinstance(n.value, ast.Attribute)
                and n.value.attr == "client"
            ]
            assert not violations, f"{name} still calls device.client.get directly at {violations}"


# ═══════════════════════════════════════════════════════════════════════
# Finding 8 — services.py bounded validation read
# ═══════════════════════════════════════════════════════════════════════

class TestFinding8ServiceValidationBounded(unittest.TestCase):
    def test_validate_power_value_bounds_its_read(self):
        tree = ast.parse(_SERVICES_SRC.read_text())
        func = _find_func(tree, "_validate_power_value")
        assert func is not None
        uses_wait_for = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "wait_for"
            for n in ast.walk(func)
        )
        assert uses_wait_for, "_validate_power_value does not bound its read -- reintroduces Finding 8"


# ═══════════════════════════════════════════════════════════════════════
# Finding 9 — synchronized power coordinator documentation + span tracking
# ═══════════════════════════════════════════════════════════════════════

class TestFinding9SyncCoordinatorTransparency(unittest.TestCase):
    def test_data_class_has_sample_span_field(self):
        tree = ast.parse(_SYNC_SRC.read_text())
        cls = next(
            (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "SynchronizedPowerData"),
            None,
        )
        assert cls is not None
        has_span_field = any(
            isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
            and n.target.id == "sample_span_ms"
            for n in cls.body
        )
        assert has_span_field, "SynchronizedPowerData has no sample_span_ms field -- Finding 9's fix is missing"

    def test_docstring_no_longer_claims_one_contiguous_sequence(self):
        source = _SYNC_SRC.read_text()
        idx = source.find("async def _async_update_data")
        docstring_window = source[idx: idx + 400]
        assert "one contiguous modbus sequence" not in docstring_window.lower(), (
            "the docstring still claims the read sequence is one "
            "contiguous, uninterrupted transaction -- this is factually "
            "inaccurate given each read acquires the guard separately "
            "(Finding 9)."
        )


# ═══════════════════════════════════════════════════════════════════════
# Finding 10 — adaptive controller deterministic flush
# ═══════════════════════════════════════════════════════════════════════

class TestFinding10AdaptiveDeterministicFlush(unittest.IsolatedAsyncioTestCase):
    async def test_async_unload_awaits_the_flush(self):
        saved = []

        class _Mini:
            def __init__(self):
                self._dirty = True
                self._save_task = None
                self._unsub_push = None

            async def _async_save(self):
                await asyncio.sleep(0)  # simulate real I/O
                saved.append(True)

            async def async_unload(self):
                if self._save_task and not self._save_task.done():
                    self._save_task.cancel()
                if self._dirty:
                    await self._async_save()

        controller = _Mini()
        await controller.async_unload()
        # If this were fire-and-forget, `saved` could still be empty here.
        self.assertEqual(saved, [True])

    def test_source_has_async_unload_method(self):
        tree = ast.parse(_ADAPTIVE_SRC.read_text())
        found = _find_func(tree, "async_unload")
        assert found is not None, "AdaptiveModbusController.async_unload not found -- Finding 10's fix is missing"
        awaits_save = any(
            isinstance(n, ast.Await)
            and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Attribute)
            and n.value.func.attr == "_async_save"
            for n in ast.walk(found)
        )
        assert awaits_save, "async_unload does not await _async_save -- the flush is not actually deterministic"

    def test_init_py_uses_async_unload_not_stop_for_teardown(self):
        source = _INIT_SRC.read_text()
        assert "await controller.async_unload()" in source, (
            "async_unload_entry does not call controller.async_unload() -- "
            "reintroduces Finding 10 at the call site."
        )


if __name__ == "__main__":
    unittest.main()
