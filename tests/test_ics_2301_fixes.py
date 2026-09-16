"""Tests for v2.3.0.1.

HS-2301-001  async_setup_entry rolls back when Home Assistant CANCELS setup
             (CancelledError is a BaseException, so the existing
             `except Exception` rollback never ran).
HS-2301-002  async_unload_services only removes services that are
             actually registered (15 "Unable to remove unknown service"
             warnings per reload in the field log).
HS-2301-003  The runtime write-permission probe (which writes the
             time-zone register back) is an option, OFF by default.

Approach: the real functions are extracted from the real source with
`ast` and executed (behavioural), plus structural checks on the real
async_setup_entry, whose full import graph needs a live Home Assistant.

Run standalone:  cd tests && python3 -m pytest test_ics_2301_fixes.py
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import inspect
import json
import logging
import pathlib
import types
import unittest
from collections.abc import Callable
from typing import Any

_ROOT = pathlib.Path(__file__).parent.parent
_INIT_SRC = (_ROOT / "__init__.py").read_text()
_SERVICES_SRC = (_ROOT / "services.py").read_text()
_SENSOR_SRC = (_ROOT / "sensor.py").read_text()
_FLOW_SRC = (_ROOT / "config_flow.py").read_text()

_spec = importlib.util.spec_from_file_location("hs2301_const", _ROOT / "const.py")
CONST = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(CONST)


def _extract(source: str, names: list[str], env: dict) -> types.SimpleNamespace:
    """Compile only the named top-level definitions from `source`."""
    tree = ast.parse(source)
    wanted = []
    for node in tree.body:
        name = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(
            node.targets[0], ast.Name
        ):
            name = node.targets[0].id
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
        if name in names:
            wanted.append(node)
    found = {
        getattr(n, "name", None) or getattr(getattr(n, "target", None), "id", None)
        or n.targets[0].id
        for n in wanted
    }
    missing = set(names) - found
    assert not missing, f"not found in source: {missing}"
    ns = dict(env)
    exec(compile(ast.Module(body=wanted, type_ignores=[]), "<extracted>", "exec"), ns)  # noqa: S102
    return types.SimpleNamespace(**{n: ns[n] for n in names}), ns


def _calls_to(source: str, attr: str) -> int:
    """Count real call expressions `<anything>.attr(...)` (comments ignored)."""
    return sum(
        1 for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == attr
    )


def _func(source: str, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _is_cancelled_handler(h: ast.ExceptHandler) -> bool:
    return h.type is not None and ast.unparse(h.type) in (
        "asyncio.CancelledError", "CancelledError",
    )


_LOG = logging.getLogger("hs2301")
ROLLBACK, _ROLLBACK_NS = _extract(
    _INIT_SRC,
    ["_ROLLBACK_TASKS", "_run_cleanup_callbacks", "_await_rollback_shielded"],
    {"asyncio": asyncio, "inspect": inspect, "_LOGGER": _LOG,
     "Callable": Callable, "Any": Any},
)


# ═══════════════════════════════════════════════════════════════════════
# HS-2301-001 — behaviour of the shielded rollback helper
# ═══════════════════════════════════════════════════════════════════════


class TestRollbackHelper(unittest.TestCase):
    def test_async_rollback_runs_to_completion(self):
        done = []

        async def rollback():
            await asyncio.sleep(0.01)
            done.append(True)

        asyncio.run(ROLLBACK._await_rollback_shielded(rollback, "t"))
        self.assertEqual(done, [True])

    def test_sync_rollback_supported(self):
        done = []
        asyncio.run(ROLLBACK._await_rollback_shielded(lambda: done.append(1), "t"))
        self.assertEqual(done, [1])

    def test_rollback_exception_is_logged_not_raised(self):
        async def rollback():
            raise RuntimeError("boom")

        with self.assertLogs("hs2301", level="ERROR") as logs:
            asyncio.run(ROLLBACK._await_rollback_shielded(rollback, "t"))
        self.assertIn("rolling back after t", logs.output[0])

    def test_handler_pattern_reraises_original_cancellation(self):
        """Mirror of the production handler: rollback, then bare raise."""
        order = []

        async def setup():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                await ROLLBACK._await_rollback_shielded(
                    lambda: order.append("rollback"), "t"
                )
                order.append("reraise")
                raise

        async def main():
            task = asyncio.ensure_future(setup())
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(task.cancelled())

        asyncio.run(main())
        self.assertEqual(order, ["rollback", "reraise"])

    def test_second_cancellation_does_not_abandon_rollback(self):
        """A second cancel while rolling back: caller stops waiting, the
        rollback still finishes in the background."""
        progress = []

        async def slow_rollback():
            progress.append("start")
            await asyncio.sleep(0.1)
            progress.append("finished")

        async def setup():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                await ROLLBACK._await_rollback_shielded(slow_rollback, "t")
                raise

        async def main():
            task = asyncio.ensure_future(setup())
            await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.sleep(0.02)       # rollback now running
            self.assertEqual(progress, ["start"])
            self.assertEqual(len(ROLLBACK._ROLLBACK_TASKS), 1)
            task.cancel()                   # cancelled again
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(progress, ["start"])  # still running
            await asyncio.sleep(0.2)
            self.assertEqual(progress, ["start", "finished"])
            self.assertEqual(len(ROLLBACK._ROLLBACK_TASKS), 0)  # reference released

        with self.assertLogs("hs2301", level="WARNING") as logs:
            asyncio.run(main())
        self.assertIn("continues in the background", logs.output[0])

    def test_bounded_steps_still_time_out_inside_rollback(self):
        """The rollback runs in its own task, so a wait_for bound inside
        it still fires normally even though the caller was cancelled."""
        outcome = []

        async def rollback():
            try:
                await asyncio.wait_for(asyncio.sleep(10), timeout=0.02)
            except TimeoutError:
                outcome.append("bounded")

        async def setup():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                await ROLLBACK._await_rollback_shielded(rollback, "t")
                raise

        async def main():
            task = asyncio.ensure_future(setup())
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)

        asyncio.run(main())
        self.assertEqual(outcome, ["bounded"])

    def test_run_cleanup_callbacks_order_and_isolation_unchanged(self):
        seen = []

        def bad():
            raise RuntimeError("x")

        async def later():
            seen.append("async")

        cbs = [lambda: seen.append("first-registered"), bad, later]
        with self.assertLogs("hs2301", level="ERROR"):
            asyncio.run(ROLLBACK._run_cleanup_callbacks(cbs))
        self.assertEqual(seen, ["async", "first-registered"])


# ═══════════════════════════════════════════════════════════════════════
# HS-2301-001 — structure of the real async_setup_entry
# ═══════════════════════════════════════════════════════════════════════


class TestSetupEntryCancellationStructure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.func = _func(_INIT_SRC, "async_setup_entry")
        cls.outer = next(n for n in cls.func.body if isinstance(n, ast.Try))

    def _cancel_handler(self, try_node: ast.Try) -> ast.ExceptHandler:
        handlers = [h for h in try_node.handlers if _is_cancelled_handler(h)]
        self.assertEqual(len(handlers), 1)
        return handlers[0]

    def test_outer_try_has_cancellation_rollback(self):
        h = self._cancel_handler(self.outer)
        src = ast.unparse(h)
        self.assertIn("_run_cleanup_callbacks(cleanup_callbacks)", src)
        self.assertIn("_bounded_device_stop(", src)
        self.assertIn("_await_rollback_shielded(", src)
        self.assertIsInstance(h.body[-1], ast.Raise)
        self.assertIsNone(h.body[-1].exc, "must re-raise the original cancellation")

    def test_every_cleanup_try_in_setup_handles_cancellation(self):
        """Any try in async_setup_entry whose `except Exception` performs a
        rollback (cleanup callbacks or raw-client disconnect) must also
        handle CancelledError, and re-raise it."""
        checked = 0
        for node in ast.walk(self.func):
            if not isinstance(node, ast.Try):
                continue
            generic = [h for h in node.handlers
                       if h.type is not None and ast.unparse(h.type) == "Exception"]
            if not generic:
                continue
            gsrc = ast.unparse(generic[0])
            if "_run_cleanup_callbacks" not in gsrc and "_bounded_client_disconnect" not in gsrc:
                continue
            checked += 1
            h = self._cancel_handler(node)
            self.assertIsInstance(h.body[-1], ast.Raise)
            self.assertIsNone(h.body[-1].exc)
            hsrc = ast.unparse(h)
            if "_bounded_client_disconnect" in gsrc:
                self.assertIn("_bounded_client_disconnect(client)", hsrc)
            if "_run_cleanup_callbacks" in gsrc:
                self.assertIn("_run_cleanup_callbacks(cleanup_callbacks)", hsrc)
        self.assertEqual(checked, 2, "expected the identification try and the outer try")

    def test_no_handler_swallows_cancellation(self):
        for node in ast.walk(self.func):
            if isinstance(node, ast.ExceptHandler):
                if node.type is None:
                    self.fail("bare `except:` would swallow cancellation")
                if ast.unparse(node.type) in ("BaseException",):
                    self.fail("BaseException handler found")

    def test_rollback_performs_no_modbus_io(self):
        h = ast.unparse(self._cancel_handler(self.outer))
        for forbidden in (".get(", ".set(", "batch_update", "async_refresh", "request_refresh"):
            self.assertNotIn(forbidden, h)

    def test_rollback_tasks_are_strongly_referenced(self):
        body = ast.unparse(_func(_INIT_SRC, "_await_rollback_shielded"))
        self.assertIn("_ROLLBACK_TASKS.add(task)", body)
        self.assertIn("add_done_callback(_ROLLBACK_TASKS.discard)", body)
        self.assertIn("asyncio.shield(task)", body)


# ═══════════════════════════════════════════════════════════════════════
# HS-2301-002 — service removal
# ═══════════════════════════════════════════════════════════════════════


class FakeServices:
    def __init__(self, registered):
        self.registered = set(registered)
        self.remove_calls = []

    def has_service(self, domain, name):
        return domain == "huawei_solar" and name in self.registered

    def async_remove(self, domain, name):
        self.remove_calls.append(name)
        if name not in self.registered:
            raise AssertionError(f"would log 'Unable to remove unknown service' for {name}")
        self.registered.discard(name)


class TestServiceRemoval(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = {n: getattr(CONST, n) for n in dir(CONST) if not n.startswith("_")}
        env.update({"HomeAssistant": object, "HuaweiSolarConfigEntry": object})
        cls.ns, cls.env = _extract(
            _SERVICES_SRC,
            ["_entries_with_services", "_CAPABILITY_SERVICES", "_capability_entries",
             "_ALL_SERVICE_NAMES", "async_unload_services",
             "_remove_service_if_registered"],
            env,
        )

    def setUp(self):
        self.env["_entries_with_services"].clear()
        for s in self.env["_capability_entries"].values():
            s.clear()

    def _register(self, entry_id, caps):
        self.env["_entries_with_services"].add(entry_id)
        registered = set()
        for cap in caps:
            self.env["_capability_entries"][cap].add(entry_id)
            registered.update(self.env["_CAPABILITY_SERVICES"][cap])
        return registered

    def test_field_scenario_no_unknown_removals(self):
        """User's setup: battery, no EMMA, no LG, no capacity control."""
        registered = self._register(
            "e1", ["always", "not_has_emma", "has_battery", "has_battery_not_emma"]
        )
        hass = types.SimpleNamespace(services=FakeServices(registered))
        entry = types.SimpleNamespace(entry_id="e1")
        asyncio.run(self.ns.async_unload_services(hass, entry))
        self.assertEqual(hass.services.registered, set())          # all removed
        self.assertEqual(sorted(hass.services.remove_calls), sorted(registered))  # each once

    def test_other_entry_keeps_its_services(self):
        reg1 = self._register("e1", ["always", "has_lg_battery"])
        self._register("e2", ["always"])
        hass = types.SimpleNamespace(services=FakeServices(reg1))
        asyncio.run(self.ns.async_unload_services(hass, types.SimpleNamespace(entry_id="e1")))
        # LG-only service removed (its last provider left); shared ones kept
        self.assertEqual(hass.services.remove_calls, ["set_fixed_charge_periods"])
        self.assertTrue(set(self.env["_CAPABILITY_SERVICES"]["always"]) <= hass.services.registered)

    def test_unknown_entry_unload_is_harmless(self):
        hass = types.SimpleNamespace(services=FakeServices(set()))
        asyncio.run(self.ns.async_unload_services(hass, types.SimpleNamespace(entry_id="x")))
        self.assertEqual(hass.services.remove_calls, [])

    def test_only_one_direct_async_remove_call_site(self):
        self.assertEqual(_calls_to(_SERVICES_SRC, "async_remove"), 1)
        helper = ast.unparse(_func(_SERVICES_SRC, "_remove_service_if_registered"))
        self.assertIn("if hass.services.has_service(DOMAIN, service_name)", helper)


# ═══════════════════════════════════════════════════════════════════════
# HS-2301-003 — write-permission probe option
# ═══════════════════════════════════════════════════════════════════════


class _Emma: ...
class _Logger: ...


class TestWritePermissionProbeOption(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        func = _func(_SENSOR_SRC, "create_sun2000_entities")
        cond = next(
            n.test for n in ast.walk(func)
            if isinstance(n, ast.If) and isinstance(n.test, ast.BoolOp)
            and any(isinstance(x, ast.Await) for x in ast.walk(n.test))
        )
        # Wrap the REAL eligibility expression in a callable.
        src = (
            "async def _cond(ucs, probe_write_permission, _has_write_permission_bounded,"
            " EMMADevice, SmartLoggerDevice):\n"
            f"    return bool({ast.unparse(cond)})\n"
        )
        ns: dict = {}
        exec(compile(src, "<cond>", "exec"), ns)  # noqa: S102
        cls.cond = staticmethod(ns["_cond"])

    def _ucs(self, *, coordinator=True, primary=None):
        dev = types.SimpleNamespace(serial_number="INV1")
        dev.primary_device = primary if primary is not None else object()
        coord = types.SimpleNamespace(guard="G") if coordinator else None
        return types.SimpleNamespace(device=dev, configuration_update_coordinator=coord)

    def _run(self, ucs, probe, result=True):
        calls = []

        async def fake_probe(device, serial, guard=None):
            calls.append((serial, guard))
            return result

        value = asyncio.run(self.cond(ucs, probe, fake_probe, _Emma, _Logger))
        return value, calls

    def test_default_is_off(self):
        self.assertIs(CONST.DEFAULT_WRITE_PERMISSION_PROBE, False)
        self.assertEqual(CONST.CONF_WRITE_PERMISSION_PROBE, "write_permission_probe")

    def test_probe_off_creates_entity_without_any_write(self):
        value, calls = self._run(self._ucs(), probe=False)
        self.assertTrue(value)
        self.assertEqual(calls, [])

    def test_probe_on_uses_probe_result(self):
        self.assertEqual(self._run(self._ucs(), probe=True, result=True), (True, [("INV1", "G")]))
        self.assertEqual(self._run(self._ucs(), probe=True, result=False)[0], False)

    def test_ineligible_devices_never_probe(self):
        for ucs in (self._ucs(coordinator=False), self._ucs(primary=_Emma()),
                    self._ucs(primary=_Logger())):
            for probe in (True, False):
                value, calls = self._run(ucs, probe)
                self.assertFalse(value)
                self.assertEqual(calls, [])

    def test_setup_passes_the_entry_option_with_default(self):
        body = ast.unparse(_func(_SENSOR_SRC, "async_setup_entry"))
        self.assertIn(
            "probe_write_permission=entry.options.get(CONF_WRITE_PERMISSION_PROBE, "
            "DEFAULT_WRITE_PERMISSION_PROBE)",
            body,
        )

    def test_function_default_is_the_safe_default(self):
        f = _func(_SENSOR_SRC, "create_sun2000_entities")
        self.assertEqual(ast.unparse(f.args.kw_defaults[0]), "DEFAULT_WRITE_PERMISSION_PROBE")

    def test_options_flow_offers_the_toggle(self):
        body = ast.unparse(_func(_FLOW_SRC, "async_step_init"))
        self.assertIn(
            "vol.Optional(CONF_WRITE_PERMISSION_PROBE, default=options.get("
            "CONF_WRITE_PERMISSION_PROBE, DEFAULT_WRITE_PERMISSION_PROBE)): bool",
            body,
        )

    def test_labels_present(self):
        for name in ("strings.json", "translations/en.json"):
            data = json.loads((_ROOT / name).read_text(encoding="utf-8"))
            self.assertIn("write_permission_probe", data["options"]["step"]["init"]["data"])

    def test_runtime_probe_call_sites(self):
        """has_write_permission() runs at runtime only via the gated sensor
        path; config_flow's uses are user-initiated (setup/reconfigure)."""
        for path in _ROOT.glob("*.py"):
            if _calls_to(path.read_text(), "has_write_permission"):
                self.assertIn(path.name, ("sensor.py", "config_flow.py"), path.name)
        sensor_calls = ast.unparse(_func(_SENSOR_SRC, "create_sun2000_entities"))
        self.assertIn("not probe_write_permission or await _has_write_permission_bounded", sensor_calls)


class TestVersion(unittest.TestCase):
    def test_manifest_version(self):
        manifest = json.loads((_ROOT / "manifest.json").read_text())
        self.assertEqual(manifest["version"], "2.3.0.1")


if __name__ == "__main__":
    unittest.main()
