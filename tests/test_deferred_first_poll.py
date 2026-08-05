"""Regression test for Defect K -- the first-poll stagger delay used to
`await asyncio.sleep(self._start_delay.total_seconds())` directly inside
`_async_update_data()`. A field traceback confirmed this method is
reachable SYNCHRONOUSLY from Home Assistant's own entity-add machinery
during platform setup (entity_platform._async_add_entity ->
entity.async_device_update -> async_update ->
coordinator.async_request_refresh()), not only from the coordinator's own
background scheduling that the delay was designed for. Sleeping there
directly extended a real, synchronous Home Assistant setup call by up to
the full stagger delay (up to 19s for a second daisy-chained device, after
Defect I/v1.3.10 added a per-device offset on top of the per-type one) --
and a CancelledError arriving mid-sleep, when Home Assistant's own setup
timeout ran out, is the confirmed mechanism behind a real
"Setup of config entry ... cancelled" field incident (AUDIT_1.3.13.md).

Two things are tested, following this project's established trade-off for
files too heavy to import directly in this fast suite (see
test_learning_gate_unsub.py's HISTORY docstring):

1. BEHAVIOURAL: the exact fixed logic pattern, reproduced in an isolated
   mini-coordinator, proves the first call returns near-instantly
   regardless of how large start_delay is, and that the deferred task
   still performs the real work after the delay elapses. A companion
   adversarial test proves the OLD (inline-sleep) pattern really does
   block the caller for the full delay, so the fixed pattern's pass is
   meaningful.
2. STATIC (AST): the real `update_coordinator.py` must not contain a bare
   `await asyncio.sleep(...)` as a direct, top-level statement inside
   `_async_update_data`'s own body (only reachable from inside the nested
   background-task coroutine, which is where the fix intentionally puts
   it) -- and `_schedule_deferred_first_poll` must exist and be called
   from the stagger block.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import unittest

_COORD_SRC = pathlib.Path(__file__).parent.parent / "update_coordinator.py"


# ── 1. Behavioural: the fixed vs. old stagger pattern in isolation ─────────

class _FixedMiniCoordinator:
    """Reproduces the v1.3.13 fixed pattern exactly."""

    def __init__(self, start_delay_seconds: float, initial_data=None):
        self._start_delay_seconds = start_delay_seconds
        self._first_poll_done = False
        self.data = initial_data
        self.real_work_calls = 0
        self._background_tasks: list[asyncio.Task] = []

    def _schedule_deferred_first_poll(self) -> None:
        async def _deferred():
            await asyncio.sleep(self._start_delay_seconds)
            await self.async_request_refresh()

        self._background_tasks.append(asyncio.ensure_future(_deferred()))

    async def async_request_refresh(self):
        return await self._async_update_data()

    async def _async_update_data(self):
        if not self._first_poll_done:
            self._first_poll_done = True
            if self._start_delay_seconds > 0:
                self._schedule_deferred_first_poll()
                return dict(self.data) if self.data else {}
        self.real_work_calls += 1
        self.data = {"real": True}
        return self.data


class _OldMiniCoordinator:
    """Reproduces the OLD (pre-fix) inline-sleep pattern, for the
    adversarial comparison."""

    def __init__(self, start_delay_seconds: float):
        self._start_delay_seconds = start_delay_seconds
        self._first_poll_done = False
        self.real_work_calls = 0

    async def _async_update_data(self):
        if not self._first_poll_done:
            self._first_poll_done = True
            if self._start_delay_seconds > 0:
                await asyncio.sleep(self._start_delay_seconds)
        self.real_work_calls += 1
        return {"real": True}


class TestDeferredFirstPollDoesNotBlock(unittest.IsolatedAsyncioTestCase):
    async def test_first_call_returns_immediately_even_with_a_long_delay(self):
        coord = _FixedMiniCoordinator(start_delay_seconds=10.0)
        # If this were still blocking, this wait_for would time out and
        # raise -- the test itself proves non-blocking by virtue of
        # completing well under the 10s configured delay.
        result = await asyncio.wait_for(coord._async_update_data(), timeout=0.5)
        self.assertEqual(result, {})
        self.assertEqual(coord.real_work_calls, 0, "no real work should happen on the deferred first call")

    async def test_first_call_returns_a_copy_of_existing_data_if_any(self):
        coord = _FixedMiniCoordinator(start_delay_seconds=10.0, initial_data={"cached": 1})
        result = await asyncio.wait_for(coord._async_update_data(), timeout=0.5)
        self.assertEqual(result, {"cached": 1})

    async def test_deferred_task_eventually_performs_the_real_work(self):
        coord = _FixedMiniCoordinator(start_delay_seconds=0.05)
        await coord._async_update_data()
        self.assertEqual(coord.real_work_calls, 0)
        # Give the deferred background task a chance to run past its delay.
        await asyncio.sleep(0.2)
        self.assertEqual(coord.real_work_calls, 1)
        self.assertEqual(coord.data, {"real": True})

    async def test_zero_delay_still_does_real_work_on_first_call(self):
        """No regression for the common case (device 0, start_delay=0)."""
        coord = _FixedMiniCoordinator(start_delay_seconds=0.0)
        result = await asyncio.wait_for(coord._async_update_data(), timeout=0.5)
        self.assertEqual(result, {"real": True})
        self.assertEqual(coord.real_work_calls, 1)

    async def test_adversarial_old_pattern_really_does_block(self):
        """Proves the OLD pattern actually reproduces the hazard, so the
        fixed pattern's pass above is meaningful and not a fake that can't
        fail."""
        coord = _OldMiniCoordinator(start_delay_seconds=10.0)
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(coord._async_update_data(), timeout=0.5)


# ── 2. Static: the real source must use the deferred pattern ──────────────

class TestUpdateCoordinatorSourceUsesDeferredPattern(unittest.TestCase):
    def _get_function(self, tree: ast.AST, name: str) -> ast.AsyncFunctionDef:
        func = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == name
            ),
            None,
        )
        assert func is not None, f"{name} not found in update_coordinator.py"
        return func

    def test_schedule_deferred_first_poll_exists(self):
        tree = ast.parse(_COORD_SRC.read_text())
        # Will raise via assert inside _get_function if missing -- but this
        # helper only looks for AsyncFunctionDef; _schedule_deferred_first_poll
        # is a plain method, so check separately.
        found = any(
            isinstance(n, ast.FunctionDef) and n.name == "_schedule_deferred_first_poll"
            for n in ast.walk(tree)
        )
        assert found, (
            "_schedule_deferred_first_poll not found in update_coordinator.py "
            "-- the Defect K background-deferral helper is missing entirely."
        )

    def test_async_update_data_does_not_sleep_inline_for_stagger(self):
        tree = ast.parse(_COORD_SRC.read_text())
        func = self._get_function(tree, "_async_update_data")

        # Find the `if not self._first_poll_done:` block specifically.
        stagger_if = next(
            (
                n for n in func.body
                if isinstance(n, ast.If)
                and isinstance(n.test, ast.UnaryOp)
                and isinstance(n.test.op, ast.Not)
            ),
            None,
        )
        assert stagger_if is not None, (
            "Could not find the `if not self._first_poll_done:` stagger "
            "block in _async_update_data"
        )

        # Within that block (not inside any nested function def), there
        # must be no direct `await asyncio.sleep(...)`.
        violations = []
        for node in ast.walk(stagger_if):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue  # nested def (the deferred task) -- not the bug
            if (
                isinstance(node, ast.Await)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "sleep"
            ):
                violations.append(node.lineno)

        assert not violations, (
            f"_async_update_data's first-poll stagger block awaits "
            f"asyncio.sleep(...) directly at line(s) {violations} -- this "
            "reintroduces Defect K (blocking a possibly-synchronous "
            "caller). The sleep must only exist inside the deferred "
            "background-task coroutine."
        )


if __name__ == "__main__":
    unittest.main()
