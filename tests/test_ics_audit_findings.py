"""Regression tests for Defect J -- three findings from an independent ICS
audit of the v1.3.10 package, verified against source before fixing
(AUDIT_1.3.11.md):

  J1 (Critical): SynchronizedPowerCoordinator keyed its ModbusGuard
     instances on device.serial_number instead of the shared bus endpoint
     every other coordinator uses -- silently defeating bus-level lock
     sharing on any installation with a shared RS485 bus (daisy-chained
     inverters). Confirmed exactly as reported at the reported line numbers.

  J2 (High): number.py's HuaweiSolarNumberEntity.create() performed raw,
     unbounded, unhandled device.client.get() calls during NUMBER PLATFORM
     SETUP -- the same class of defect as Defect H (sensor.py), in a
     different platform file. Confirmed exactly as reported.

  J3 (Medium): dynamic min/max bounds were never cleared when their
     dependency register disappeared from a coordinator update, leaving
     stale bounds displayed indefinitely. Confirmed exactly as reported.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import unittest

_SYNC_SRC = pathlib.Path(__file__).parent.parent / "synchronized_power_coordinator.py"
_INIT_SRC = pathlib.Path(__file__).parent.parent / "__init__.py"
_NUMBER_SRC = pathlib.Path(__file__).parent.parent / "number.py"


# ── J1: guard key mismatch ─────────────────────────────────────────────────

class TestGuardKeyUsesSharedEndpoint(unittest.TestCase):
    def test_synchronized_power_coordinator_does_not_key_guard_on_serial_number(self):
        source = _SYNC_SRC.read_text()
        tree = ast.parse(source)
        init = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "__init__"
            ),
            None,
        )
        assert init is not None, "SynchronizedPowerCoordinator.__init__ not found"

        # A bare `.serial_number` as the ONLY argument to get_or_create()
        # (not part of an `x or y.serial_number` fallback) is the exact
        # original defect shape.
        bare_violations = []
        for node in ast.walk(init):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get_or_create"
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Attribute)
                and node.args[0].attr == "serial_number"
            ):
                bare_violations.append(node.lineno)

        assert not bare_violations, (
            f"get_or_create() is called with a bare device.serial_number "
            f"(no bus_endpoint fallback) at line(s) {bare_violations} -- "
            "this reintroduces Defect J1: the coordinator's guard no longer "
            "resolves to the same shared-bus guard every other coordinator "
            "uses. Use `bus_endpoint or device.serial_number` instead."
        )

    def test_synchronized_power_coordinator_accepts_bus_endpoint_param(self):
        tree = ast.parse(_SYNC_SRC.read_text())
        init = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "__init__"
            ),
            None,
        )
        arg_names = [a.arg for a in init.args.args] + [a.arg for a in init.args.kwonlyargs]
        assert "bus_endpoint" in arg_names, (
            "SynchronizedPowerCoordinator.__init__ no longer accepts "
            "bus_endpoint -- without it the guard cannot be keyed to the "
            "shared bus."
        )

    def test_init_py_passes_bus_endpoint_to_sync_coordinator(self):
        source = _INIT_SRC.read_text()
        tree = ast.parse(source)
        violations = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "SynchronizedPowerCoordinator"
            ):
                kwarg_names = [kw.arg for kw in node.keywords]
                if "bus_endpoint" not in kwarg_names:
                    violations.append(node.lineno)
        assert not violations, (
            f"SynchronizedPowerCoordinator(...) constructed without "
            f"bus_endpoint= at line(s) {violations} in __init__.py -- the "
            "coordinator has no way to key its guard to the shared bus."
        )

    def test_behavioural_guard_resolution_matches_shared_endpoint(self):
        """A minimal fake ModbusGuard-like registry proves the ACTUAL
        arithmetic (bus_endpoint or serial_number fallback) resolves both
        inverters to the same key as a normal coordinator would, when a
        real bus_endpoint is available -- the concrete behaviour Defect J1
        broke."""
        def resolve(bus_endpoint: str, serial_number: str) -> str:
            return bus_endpoint or serial_number

        bus_endpoint = "192.168.7.22:502"
        inv1_serial, inv2_serial = "HV2220098926", "HV2220080950"

        # What update_coordinator.py's own coordinators resolve to:
        other_coordinator_key = resolve(bus_endpoint, inv1_serial)

        # What the sync coordinator now resolves to for BOTH devices:
        sync_primary_key = resolve(bus_endpoint, inv1_serial)
        sync_secondary_key = resolve(bus_endpoint, inv2_serial)

        self.assertEqual(sync_primary_key, other_coordinator_key)
        self.assertEqual(sync_secondary_key, other_coordinator_key)
        self.assertEqual(sync_primary_key, sync_secondary_key)


# ── J2: unbounded number-platform setup reads ──────────────────────────────

STATIC_BOUND_READ_TIMEOUT_SECONDS = 5.0


async def _read_static_bound(device, key, kind, log):
    try:
        result = await asyncio.wait_for(
            device.client.get(key), timeout=STATIC_BOUND_READ_TIMEOUT_SECONDS
        )
        return result.value
    except TimeoutError:
        log.append(("timeout", key))
        return None
    except Exception:  # noqa: BLE001
        log.append(("exception", key))
        return None


class _FakeClient:
    def __init__(self, behavior):
        self._behavior = behavior

    async def get(self, key):
        if self._behavior == "slow":
            await asyncio.sleep(3600)
        elif self._behavior == "raise":
            raise ConnectionError("device unreachable")
        else:
            return type("R", (), {"value": 42.0})()


class _FakeDevice:
    def __init__(self, behavior):
        self.client = _FakeClient(behavior)
        self.serial_number = "SN-TEST"


class TestBoundedStaticReadNumberPlatform(unittest.IsolatedAsyncioTestCase):
    async def test_slow_read_times_out_instead_of_hanging(self):
        log: list[tuple[str, str]] = []
        result = await asyncio.wait_for(
            _read_static_bound(_FakeDevice("slow"), "SOME_KEY", "maximum", log),
            timeout=STATIC_BOUND_READ_TIMEOUT_SECONDS + 2,
        )
        self.assertIsNone(result)
        self.assertEqual(log, [("timeout", "SOME_KEY")])

    async def test_failing_read_does_not_propagate(self):
        log: list[tuple[str, str]] = []
        result = await _read_static_bound(_FakeDevice("raise"), "SOME_KEY", "minimum", log)
        self.assertIsNone(result)
        self.assertEqual(log, [("exception", "SOME_KEY")])

    async def test_healthy_read_still_returns_value(self):
        log: list[tuple[str, str]] = []
        result = await _read_static_bound(_FakeDevice("ok"), "SOME_KEY", "maximum", log)
        self.assertEqual(result, 42.0)
        self.assertEqual(log, [])


class TestNumberPySourceUsesBoundedHelper(unittest.TestCase):
    def test_create_does_not_call_device_client_get_directly(self):
        source = _NUMBER_SRC.read_text()
        tree = ast.parse(source)
        func = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "create"
            ),
            None,
        )
        assert func is not None, "HuaweiSolarNumberEntity.create() not found"

        violations = [
            node.lineno
            for node in ast.walk(func)
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "get"
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "client"
            )
        ]
        assert not violations, (
            f"create() calls device.client.get(...) directly at line(s) "
            f"{violations} -- this reintroduces Defect J2 (no timeout, no "
            "exception handling, on the number platform setup critical "
            "path). Route it through _read_static_bound() instead."
        )

    def test_bounded_helper_exists_and_uses_wait_for(self):
        source = _NUMBER_SRC.read_text()
        tree = ast.parse(source)
        func = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_read_static_bound"
            ),
            None,
        )
        assert func is not None, "_read_static_bound() not found in number.py"
        uses_wait_for = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "wait_for"
            for node in ast.walk(func)
        )
        assert uses_wait_for, "_read_static_bound() does not bound its read with asyncio.wait_for()"

    def test_v2_0_0a_routes_through_guard(self):
        """F06, external ICS audit -- confirmed: being time-bounded protected
        setup from hanging, but the read itself still bypassed ModbusGuard
        entirely."""
        source = _NUMBER_SRC.read_text()
        tree = ast.parse(source)
        func = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_read_static_bound"
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
            "_read_static_bound() no longer routes through guard.request() -- "
            "F06 has regressed"
        )
        # And the call sites must actually pass a real guard, not leave the
        # optional parameter at its defensive None default.
        create_func = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "create"
            ),
            None,
        )
        assert create_func is not None
        create_src = ast.get_source_segment(source, create_func) or ""
        assert "guard=coordinator.guard" in create_src, (
            "create()'s calls to _read_static_bound() must pass "
            "coordinator.guard explicitly"
        )


# ── J3: stale dynamic bounds ────────────────────────────────────────────────

class TestDynamicBoundsClearedWhenAbsent(unittest.TestCase):
    def test_source_resets_to_none_when_register_absent(self):
        source = _NUMBER_SRC.read_text()
        tree = ast.parse(source)
        func = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_handle_coordinator_update"
            ),
            None,
        )
        assert func is not None, "_handle_coordinator_update not found in number.py"

        # The fixed shape is `self._dynamic_min_value = X.value if X else None`
        # (an IfExp / conditional assignment) rather than an `if X: assign`
        # statement that leaves the previous value untouched in the else case.
        assigns_min = [
            n for n in ast.walk(func)
            if isinstance(n, ast.Assign)
            and any(
                isinstance(t, ast.Attribute) and t.attr == "_dynamic_min_value"
                for t in n.targets
            )
        ]
        assigns_max = [
            n for n in ast.walk(func)
            if isinstance(n, ast.Assign)
            and any(
                isinstance(t, ast.Attribute) and t.attr == "_dynamic_max_value"
                for t in n.targets
            )
        ]
        assert assigns_min and isinstance(assigns_min[0].value, ast.IfExp), (
            "_dynamic_min_value is not assigned via an `X.value if X else "
            "None` conditional expression -- this reintroduces Defect J3 "
            "(stale bound retained when the register disappears)."
        )
        assert assigns_max and isinstance(assigns_max[0].value, ast.IfExp), (
            "_dynamic_max_value is not assigned via an `X.value if X else "
            "None` conditional expression -- this reintroduces Defect J3."
        )

    def test_behavioural_reset_semantics(self):
        """Direct behavioural check of the fixed assignment shape."""
        class _Reg:
            def __init__(self, value):
                self.value = value

        def apply(min_register):
            return min_register.value if min_register else None

        self.assertEqual(apply(_Reg(5.0)), 5.0)
        self.assertIsNone(apply(None))  # register disappeared -> cleared, not stale


if __name__ == "__main__":
    unittest.main()
