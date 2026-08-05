"""Regression tests for three defects found in a full, fresh-eyes ICS sweep
of the codebase (deliberately conducted without reference to any existing
test or audit file), all confirmed against source before fixing
(AUDIT_1.3.15.md):

  P (the sweep's #1, matching "Defect C" from the original 2026-08-04
     handoff): ModbusGuard.update_gap()/update_max_queue_depth() were plain
     overwrites -- on a bus shared by multiple devices, whichever device's
     coordinator polled most recently silently clobbered every other
     device's learned parameters. Fixed by tracking each device's
     contribution separately and deriving the aggregate as the safest
     option (max gap, min depth) across all current contributors.

  Q (the sweep's #2): several write paths (select.py's
     StorageModeSelectEntity, button.py's stop-forcible-charge sequence,
     and all ~15 of services.py's write functions) called
     async_refresh()/async_request_refresh() after a write without first
     invalidating the specific register(s) written -- so a subsequent poll
     could still serve the pre-write cached value if its TTL had not yet
     naturally expired, making a successful write look like it silently
     failed.

  R (the sweep's #3): services.py had no locking between concurrent
     service calls to the same device, unlike switch.py's per-entity
     _change_lock -- two overlapping multi-step write sequences to the
     same device could genuinely interleave.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import unittest

_GUARD_SRC = pathlib.Path(__file__).parent.parent / "modbus_guard.py"
_COORD_SRC = pathlib.Path(__file__).parent.parent / "update_coordinator.py"
_SELECT_SRC = pathlib.Path(__file__).parent.parent / "select.py"
_BUTTON_SRC = pathlib.Path(__file__).parent.parent / "button.py"
_SERVICES_SRC = pathlib.Path(__file__).parent.parent / "services.py"


# ═══════════════════════════════════════════════════════════════════════
# Defect P — ModbusGuard multi-device aggregation
# ═══════════════════════════════════════════════════════════════════════

MIN_GAP = 0.150
MAX_DEPTH = 3


class _FixedGuard:
    """Reproduces the v1.3.15 fixed aggregation logic in isolation."""

    def __init__(self):
        self._gap_contributions: dict[str, float] = {}
        self._depth_contributions: dict[str, int] = {}
        self._effective_gap = MIN_GAP
        self._max_queue_depth = MAX_DEPTH

    def update_gap(self, source: str, gap_seconds: float) -> None:
        clamped = max(MIN_GAP, min(gap_seconds, 0.500))
        self._gap_contributions[source] = clamped
        self._effective_gap = max(self._gap_contributions.values())

    def update_max_queue_depth(self, source: str, depth: int) -> None:
        clamped = max(1, min(depth, MAX_DEPTH))
        self._depth_contributions[source] = clamped
        self._max_queue_depth = min(self._depth_contributions.values())

    def remove_source(self, source: str) -> None:
        self._gap_contributions.pop(source, None)
        self._depth_contributions.pop(source, None)
        self._effective_gap = (
            max(self._gap_contributions.values()) if self._gap_contributions else MIN_GAP
        )
        self._max_queue_depth = (
            min(self._depth_contributions.values()) if self._depth_contributions else MAX_DEPTH
        )


class _OldGuard:
    """Reproduces the pre-fix (plain overwrite) pattern, for the
    adversarial comparison."""

    def __init__(self):
        self._effective_gap = MIN_GAP
        self._max_queue_depth = MAX_DEPTH

    def update_gap(self, gap_seconds: float) -> None:
        self._effective_gap = max(MIN_GAP, min(gap_seconds, 0.500))

    def update_max_queue_depth(self, depth: int) -> None:
        self._max_queue_depth = max(1, min(depth, MAX_DEPTH))


class TestGuardMultiDeviceAggregation(unittest.TestCase):
    def test_old_pattern_is_last_writer_wins(self):
        """Adversarial: proves the OLD pattern really does let one device
        clobber another's more conservative setting."""
        g = _OldGuard()
        g.update_gap(0.150)   # device A: tight gap, everything's fine
        g.update_gap(0.500)   # device B: needs a wide gap, having a rough time
        g.update_gap(0.150)   # device A polls again -- silently undoes B's setting
        self.assertEqual(g._effective_gap, 0.150, "device B's caution was clobbered")

    def test_widest_gap_wins_regardless_of_report_order(self):
        g = _FixedGuard()
        g.update_gap("device-A", 0.150)
        g.update_gap("device-B", 0.500)
        self.assertEqual(g._effective_gap, 0.500)
        g.update_gap("device-A", 0.150)  # device A reports again -- must not win
        self.assertEqual(g._effective_gap, 0.500, "device B's wider gap must still apply")

    def test_shallowest_depth_wins_regardless_of_report_order(self):
        g = _FixedGuard()
        g.update_max_queue_depth("device-A", 3)
        g.update_max_queue_depth("device-B", 1)
        self.assertEqual(g._max_queue_depth, 1)
        g.update_max_queue_depth("device-A", 3)  # must not win
        self.assertEqual(g._max_queue_depth, 1, "device B's shallower depth must still apply")

    def test_remove_source_recomputes_aggregate(self):
        g = _FixedGuard()
        g.update_gap("device-A", 0.150)
        g.update_gap("device-B", 0.500)
        self.assertEqual(g._effective_gap, 0.500)
        g.remove_source("device-B")
        self.assertEqual(
            g._effective_gap, 0.150,
            "removing the wide-gap device must let the aggregate relax",
        )

    def test_remove_source_reverts_to_defaults_when_last_contributor_gone(self):
        g = _FixedGuard()
        g.update_gap("only-device", 0.300)
        g.remove_source("only-device")
        self.assertEqual(g._effective_gap, MIN_GAP)
        self.assertEqual(g._max_queue_depth, MAX_DEPTH)

    def test_remove_source_is_a_safe_noop_for_unknown_source(self):
        g = _FixedGuard()
        g.update_gap("device-A", 0.300)
        g.remove_source("never-contributed")  # must not raise
        self.assertEqual(g._effective_gap, 0.300)


class TestGuardSourceStaticChecks(unittest.TestCase):
    def _method(self, tree, name):
        return next(
            (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name),
            None,
        )

    def test_update_gap_takes_a_source_parameter(self):
        tree = ast.parse(_GUARD_SRC.read_text())
        func = self._method(tree, "update_gap")
        assert func is not None
        arg_names = [a.arg for a in func.args.args]
        assert "source" in arg_names, (
            "update_gap() no longer takes a 'source' parameter -- this "
            "reintroduces Defect P's plain-overwrite pattern."
        )

    def test_update_max_queue_depth_takes_a_source_parameter(self):
        tree = ast.parse(_GUARD_SRC.read_text())
        func = self._method(tree, "update_max_queue_depth")
        assert func is not None
        arg_names = [a.arg for a in func.args.args]
        assert "source" in arg_names

    def test_remove_source_exists(self):
        tree = ast.parse(_GUARD_SRC.read_text())
        assert self._method(tree, "remove_source") is not None, (
            "remove_source() is missing -- a torn-down device's "
            "contribution would linger in the aggregate forever."
        )

    def test_coordinator_call_sites_pass_serial_number(self):
        source = _COORD_SRC.read_text()
        # Both call sites (main coordinator, optimizer coordinator) must
        # pass self.device.serial_number as the source.
        count = source.count(
            "self.guard.update_gap(self.device.serial_number,"
        )
        assert count == 2, (
            f"expected 2 call sites passing self.device.serial_number to "
            f"update_gap(), found {count} -- this reintroduces Defect P at "
            "the caller."
        )


# ═══════════════════════════════════════════════════════════════════════
# Defect Q — cache invalidation gaps
# ═══════════════════════════════════════════════════════════════════════

class _FakeCoordinator:
    def __init__(self):
        self.invalidated: list[str] = []
        self.refreshed = False

    def invalidate_cache(self, name: str) -> None:
        self.invalidated.append(name)

    async def async_refresh(self) -> None:
        self.refreshed = True

    async def async_request_refresh(self) -> None:
        self.refreshed = True


class _FakeDevice:
    async def set(self, name, value):
        return True


class _FakeDeviceData:
    def __init__(self, coordinator):
        self.device = _FakeDevice()
        self.configuration_update_coordinator = coordinator


async def _set_and_invalidate(dd, name, value):
    """Mirror of services.py's real helper, reproduced for isolated
    testing per this project's established trade-off for heavy-import
    files."""
    result = await dd.device.set(name, value)
    if dd.configuration_update_coordinator is not None:
        dd.configuration_update_coordinator.invalidate_cache(name)
    return result


class TestSetAndInvalidateHelper(unittest.IsolatedAsyncioTestCase):
    async def test_write_invalidates_the_specific_register(self):
        coordinator = _FakeCoordinator()
        dd = _FakeDeviceData(coordinator)
        await _set_and_invalidate(dd, "SOME_REGISTER", 42)
        self.assertEqual(coordinator.invalidated, ["SOME_REGISTER"])

    async def test_no_coordinator_does_not_crash(self):
        dd = _FakeDeviceData(None)
        result = await _set_and_invalidate(dd, "SOME_REGISTER", 42)
        self.assertTrue(result)  # must not raise with no coordinator attached


class TestCacheInvalidationStaticChecks(unittest.TestCase):
    def test_select_storage_mode_entity_invalidates_cache(self):
        source = _SELECT_SRC.read_text()
        tree = ast.parse(source)
        cls = next(
            (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "StorageModeSelectEntity"),
            None,
        )
        assert cls is not None, "StorageModeSelectEntity not found in select.py"
        func = next(
            (n for n in ast.walk(cls) if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_select_option"),
            None,
        )
        assert func is not None
        calls_invalidate = any(
            isinstance(n, ast.Attribute) and n.attr == "invalidate_cache"
            for n in ast.walk(func)
        )
        assert calls_invalidate, (
            "StorageModeSelectEntity.async_select_option no longer calls "
            "invalidate_cache() -- this reintroduces Defect Q: a write "
            "here can leave the entity showing stale data."
        )

    def test_button_stop_forcible_charge_invalidates_all_four_registers(self):
        source = _BUTTON_SRC.read_text()
        tree = ast.parse(source)
        func = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_press"
            ),
            None,
        )
        assert func is not None
        # The fix may call invalidate_cache() once per register literally,
        # or once inside a loop over all written registers -- either shape
        # is fine. Check for a call site, and if it's inside a For loop,
        # confirm the loop iterates over at least 4 register names.
        invalidate_calls = [
            n for n in ast.walk(func)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "invalidate_cache"
        ]
        assert invalidate_calls, (
            "async_press has no invalidate_cache() call at all -- this "
            "reintroduces Defect Q."
        )

        for_loops_with_invalidate = [
            n for n in ast.walk(func)
            if isinstance(n, ast.For)
            and any(
                isinstance(c, ast.Call)
                and isinstance(c.func, ast.Attribute)
                and c.func.attr == "invalidate_cache"
                for c in ast.walk(n)
            )
        ]
        if len(invalidate_calls) < 4:
            assert for_loops_with_invalidate, (
                f"only {len(invalidate_calls)} invalidate_cache() call(s) "
                "found, and none are inside a loop -- expected either 4+ "
                "direct calls (one per register written) or a loop "
                "iterating over all of them."
            )
            loop = for_loops_with_invalidate[0]
            # The loop must iterate over a tuple/list of at least 4 items
            # (the four registers written by this button).
            iterated = loop.iter
            if isinstance(iterated, (ast.Tuple, ast.List)):
                assert len(iterated.elts) >= 4, (
                    f"the invalidate_cache loop only iterates over "
                    f"{len(iterated.elts)} register(s), expected at least "
                    "4 -- this reintroduces Defect Q for the missing ones."
                )

    def test_services_has_no_raw_device_set_outside_the_helper(self):
        source = _SERVICES_SRC.read_text()
        tree = ast.parse(source)
        helper = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_set_and_invalidate"
            ),
            None,
        )
        assert helper is not None, "_set_and_invalidate() helper not found in services.py"
        helper_start, helper_end = helper.lineno, (helper.end_lineno or helper.lineno)

        violations = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "set"
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "device"
            ):
                if not (helper_start <= node.lineno <= helper_end):
                    violations.append(node.lineno)
        assert not violations, (
            f"raw dd.device.set(...) called outside _set_and_invalidate() "
            f"at line(s) {violations} -- this reintroduces Defect Q for "
            "that call site."
        )

    def test_services_helper_uses_invalidate_cache(self):
        tree = ast.parse(_SERVICES_SRC.read_text())
        helper = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_set_and_invalidate"
            ),
            None,
        )
        assert helper is not None
        uses_invalidate = any(
            isinstance(n, ast.Attribute) and n.attr == "invalidate_cache"
            for n in ast.walk(helper)
        )
        assert uses_invalidate


# ═══════════════════════════════════════════════════════════════════════
# Defect R — no locking between concurrent service calls
# ═══════════════════════════════════════════════════════════════════════

class TestServiceCallLocking(unittest.IsolatedAsyncioTestCase):
    async def test_old_pattern_allows_interleaving(self):
        """Adversarial: without a lock, two 'logical operations' on the
        same device really do interleave their steps."""
        order: list[str] = []

        async def op_a():
            order.append("A-step1")
            await asyncio.sleep(0.02)
            order.append("A-step2")

        async def op_b():
            order.append("B-step1")
            await asyncio.sleep(0.01)
            order.append("B-step2")

        await asyncio.gather(op_a(), op_b())
        # With no serialisation, B's steps land in between A's.
        self.assertEqual(order, ["A-step1", "B-step1", "B-step2", "A-step2"])

    async def test_new_pattern_serialises_operations_on_the_same_device(self):
        locks: dict[str, asyncio.Lock] = {}

        def get_lock(serial: str) -> asyncio.Lock:
            if serial not in locks:
                locks[serial] = asyncio.Lock()
            return locks[serial]

        order: list[str] = []

        async def op_a():
            async with get_lock("SAME-DEVICE"):
                order.append("A-step1")
                await asyncio.sleep(0.02)
                order.append("A-step2")

        async def op_b():
            async with get_lock("SAME-DEVICE"):
                order.append("B-step1")
                await asyncio.sleep(0.01)
                order.append("B-step2")

        await asyncio.gather(op_a(), op_b())
        # Whichever operation acquires the lock first must fully complete
        # (both its steps) before the other one starts at all.
        self.assertIn(
            order,
            (["A-step1", "A-step2", "B-step1", "B-step2"],
             ["B-step1", "B-step2", "A-step1", "A-step2"]),
        )

    async def test_different_devices_do_not_block_each_other(self):
        locks: dict[str, asyncio.Lock] = {}

        def get_lock(serial: str) -> asyncio.Lock:
            if serial not in locks:
                locks[serial] = asyncio.Lock()
            return locks[serial]

        order: list[str] = []

        async def op_a():
            async with get_lock("DEVICE-A"):
                order.append("A-step1")
                await asyncio.sleep(0.02)
                order.append("A-step2")

        async def op_b():
            async with get_lock("DEVICE-B"):
                order.append("B-step1")
                await asyncio.sleep(0.01)
                order.append("B-step2")

        await asyncio.gather(op_a(), op_b())
        # Different devices must NOT serialise against each other -- B
        # (shorter sleep) should still be able to interleave with A.
        self.assertEqual(order, ["A-step1", "B-step1", "B-step2", "A-step2"])


class TestServiceLockingStaticChecks(unittest.TestCase):
    def test_lock_registry_helper_exists(self):
        tree = ast.parse(_SERVICES_SRC.read_text())
        found = any(
            isinstance(n, ast.FunctionDef) and n.name == "_get_device_write_lock"
            for n in ast.walk(tree)
        )
        assert found, "_get_device_write_lock() not found in services.py"

    def test_every_write_function_acquires_the_lock(self):
        write_function_names = {
            "forcible_charge", "forcible_discharge", "forcible_charge_soc",
            "forcible_discharge_soc", "stop_forcible_charge",
            "reset_maximum_feed_grid_power", "set_di_active_power_scheduling",
            "set_zero_power_grid_connection", "set_maximum_feed_grid_power",
            "set_maximum_feed_grid_power_percentage", "set_battery_tou_periods",
            "set_emma_tou_periods", "set_capacity_control_periods",
            "set_fixed_charge_periods",
        }
        tree = ast.parse(_SERVICES_SRC.read_text())
        missing = []
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name in write_function_names:
                acquires_lock = any(
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name)
                    and n.func.id == "_get_device_write_lock"
                    for n in ast.walk(node)
                )
                if not acquires_lock:
                    missing.append(node.name)
        assert not missing, (
            f"the following service functions do not acquire the per-"
            f"device write lock: {missing} -- this reintroduces Defect R "
            "for those functions."
        )
        found_names = {
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name in write_function_names
        }
        assert found_names == write_function_names, (
            f"expected to find all {len(write_function_names)} write "
            f"functions, found {len(found_names)}: missing "
            f"{write_function_names - found_names}"
        )


if __name__ == "__main__":
    unittest.main()
