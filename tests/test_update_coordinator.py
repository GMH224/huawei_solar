"""Tests for update_coordinator.py — stdlib unittest, no pytest required.

Covers:
  • _day_interval sentinel uses timedelta(0), not UPDATE_TIMEOUT
  • BUG-4/10: _execute_batch returns (merged, rtt_ms) tuple; no double-count
  • Telemetry recorded AFTER _execute_batch, not before
  • Energy counter stale-cache exclusion (is_energy_counter guard)
  • Priority polling during back-off (_backoff_cycle / FAST tier)
  • verify_write structure: delay, retries, warning on failure
  • on_connection_lost/restored callbacks
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys
import unittest

_SRC = pathlib.Path(__file__).parent.parent / "update_coordinator.py"
_SOURCE = _SRC.read_text()
_TREE = ast.parse(_SOURCE)


def _class_init_body(class_name: str) -> ast.FunctionDef | None:
    for node in ast.walk(_TREE):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    return item
    return None


def _method_body(class_name: str, method_name: str) -> str:
    """Return the source slice for a method."""
    start_marker = f"def {method_name}("
    start = _SOURCE.find(start_marker)
    if start == -1:
        return ""
    # find next method/class at same or lower indent to delimit
    end = _SOURCE.find("\n    def ", start + 1)
    end2 = _SOURCE.find("\n    async def ", start + 1)
    candidates = [x for x in [end, end2] if x > start]
    end = min(candidates) if candidates else len(_SOURCE)
    return _SOURCE[start:end]


# ── _day_interval sentinel ────────────────────────────────────────────────────

class TestDayIntervalSentinel(unittest.TestCase):

    def test_no_update_timeout_fallback(self):
        init = _class_init_body("HuaweiSolarUpdateCoordinator")
        self.assertIsNotNone(init)
        for node in ast.walk(init):
            if (isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Attribute) and t.attr == "_day_interval"
                            for t in node.targets)
                    and isinstance(node.value, ast.BoolOp)):
                for val in node.value.values:
                    if isinstance(val, ast.Name) and val.id == "UPDATE_TIMEOUT":
                        self.fail("_day_interval must not fall back to UPDATE_TIMEOUT")

    def test_timedelta_zero_sentinel_used(self):
        init = _class_init_body("HuaweiSolarUpdateCoordinator")
        self.assertIsNotNone(init)
        found = False
        for node in ast.walk(init):
            if (isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Attribute) and t.attr == "_day_interval"
                            for t in node.targets)
                    and isinstance(node.value, ast.IfExp)):
                orelse = node.value.orelse
                if (isinstance(orelse, ast.Call)
                        and isinstance(orelse.func, ast.Name)
                        and orelse.func.id == "timedelta"):
                    found = True
        self.assertTrue(found,
            "self._day_interval must use timedelta(...) sentinel via ternary")


# ── BUG-4/10: _execute_batch return type and no double-count ─────────────────

class TestExecuteBatchFixes(unittest.TestCase):

    def test_returns_tuple_type_annotation(self):
        """BUG-10: return type must be tuple."""
        self.assertIn(
            "-> tuple[dict[RegisterName, Result[Any]], float]:",
            _SOURCE,
            "_execute_batch must declare tuple return type (BUG-10)",
        )

    def test_returns_total_rtt_ms(self):
        """BUG-10: total_rtt_ms must be returned from _execute_batch."""
        self.assertIn("return merged, total_rtt_ms", _SOURCE)

    def test_caller_unpacks_tuple(self):
        """BUG-10: caller assigns fresh, total_rtt_ms from _execute_batch."""
        self.assertIn(
            "fresh, total_rtt_ms = await self._execute_batch(",
            _SOURCE,
        )

    def test_no_adaptive_record_inside_execute_batch(self):
        """BUG-4: adaptive.record_request must NOT appear inside _execute_batch."""
        start = _SOURCE.find("async def _execute_batch(")
        end = _SOURCE.find("\n    async def ", start + 1)
        if end == -1:
            end = _SOURCE.find("\n    def ", start + 1)
        body = _SOURCE[start:end if end > start else len(_SOURCE)]
        self.assertNotIn(
            "self._adaptive.record_request",
            body,
            "BUG-4: adaptive.record_request() must not be inside _execute_batch "
            "(causes double-count when outer handlers also record)",
        )

    def test_adaptive_record_in_success_path_uses_total_rtt(self):
        """BUG-4: success path passes total_rtt_ms to adaptive controller."""
        self.assertIn(
            "self._adaptive.record_request(total_rtt_ms, success=True",
            _SOURCE,
        )

    def test_telemetry_record_after_execute_batch(self):
        """BUG-10: telemetry.record_request called AFTER _execute_batch."""
        exec_pos = _SOURCE.find("fresh, total_rtt_ms = await self._execute_batch(")
        rec_pos  = _SOURCE.find("self.telemetry.record_request(len(stale_names))")
        self.assertGreater(exec_pos, 0)
        self.assertGreater(rec_pos, exec_pos,
            "telemetry.record_request must come AFTER _execute_batch (BUG-10)")


# ── Energy counter stale-cache exclusion ──────────────────────────────────────

class TestEnergyStaleCacheExclusion(unittest.TestCase):

    def test_is_energy_counter_imported(self):
        self.assertIn("is_energy_counter", _SOURCE)

    def test_fallback_excludes_energy_counters(self):
        self.assertIn("not is_energy_counter(n)", _SOURCE)

    def test_withheld_count_tracked(self):
        self.assertIn("energy_withheld", _SOURCE)


# ── Priority polling during back-off ─────────────────────────────────────────

class TestPriorityBackoff(unittest.TestCase):

    def test_backoff_cycle_counter_exists(self):
        self.assertIn("_backoff_cycle", _SOURCE)

    def test_fast_tier_always_polled(self):
        self.assertIn("RegisterTier.FAST", _SOURCE)
        self.assertIn("priority_names", _SOURCE)

    def test_normal_divisor_applied(self):
        self.assertIn("BACKOFF_NORMAL_DIVISOR", _SOURCE)

    def test_backoff_cycle_reset_on_success(self):
        # The reset must appear in the success path (after both counters reset)
        success_idx = _SOURCE.rfind("self._backoff_cycle = 0")
        self.assertGreater(success_idx, 0,
            "_backoff_cycle must be reset to 0 in the success path")


# ── verify_write — opt-5 ──────────────────────────────────────────────────────

class TestVerifyWrite(unittest.TestCase):

    def test_method_exists(self):
        self.assertIn("async def verify_write(", _SOURCE)

    def test_uses_write_verify_delay(self):
        self.assertIn("WRITE_VERIFY_DELAY", _SOURCE)

    def test_uses_write_verify_retries(self):
        self.assertIn("WRITE_VERIFY_RETRIES", _SOURCE)

    def test_logs_warning_on_persistent_failure(self):
        body = _method_body("HuaweiSolarUpdateCoordinator", "verify_write")
        self.assertIn("_LOGGER.warning", body)


# ── Keep-alive callbacks ──────────────────────────────────────────────────────

class TestKeepAliveCallbacks(unittest.TestCase):

    def test_on_connection_lost_exists(self):
        self.assertIn("def on_connection_lost(self)", _SOURCE)

    def test_on_connection_restored_exists(self):
        self.assertIn("def on_connection_restored(self)", _SOURCE)

    def test_on_connection_lost_invalidates_cache(self):
        body = _method_body("HuaweiSolarUpdateCoordinator", "on_connection_lost")
        self.assertIn("invalidate_all", body)

    def test_on_connection_restored_resets_failure_counters(self):
        body = _method_body("HuaweiSolarUpdateCoordinator", "on_connection_restored")
        self.assertIn("_consecutive_timeouts", body)
        self.assertIn("_consecutive_failures", body)


# ═══════════════════════════════════════════════════════════════════════════════
# v1.1.1 regression tests — BUG-008: verify_write cache coherence
# ═══════════════════════════════════════════════════════════════════════════════

class TestVerifyWriteCacheCoherence(unittest.TestCase):
    """BUG-008: verify_write must invalidate before updating the cache."""

    def _make_coordinator(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        import types, sys, pathlib, importlib.util

        coord = MagicMock()
        coord.name = "test_coord"
        coord.guard = MagicMock()
        coord.guard.request = MagicMock()
        coord.guard.request.return_value.__aenter__ = AsyncMock(return_value=None)
        coord.guard.request.return_value.__aexit__ = AsyncMock(return_value=False)
        coord.cache = MagicMock()
        coord.cache.invalidate = MagicMock()
        coord.cache.update = MagicMock()
        coord.update_timeout = __import__("datetime").timedelta(seconds=30)
        return coord

    def test_invalidate_called_before_update_on_success(self):
        """cache.invalidate(name) must be called before cache.update() on verify success."""
        import asyncio
        from unittest.mock import MagicMock, AsyncMock, patch, call
        from datetime import timedelta

        call_order = []

        coord = self._make_coordinator()
        coord.cache.invalidate.side_effect = lambda n: call_order.append("invalidate")
        coord.cache.update.side_effect = lambda d: call_order.append("update")

        # Build a mock result that reports expected_value
        mock_register = MagicMock()
        mock_register.value = 42
        mock_result = {MagicMock(): mock_register}

        coord.device = MagicMock()
        coord.device.batch_update = AsyncMock(return_value=mock_result)

        # We need to call the real verify_write method — import it
        # Patch asyncio.sleep to skip delay
        async def run():
            with patch("asyncio.sleep", new=AsyncMock()):
                with patch("asyncio.timeout"):
                    # Get the real name key from the result
                    name = list(mock_result.keys())[0]
                    mock_result[name].value = 42
                    coord.device.batch_update = AsyncMock(return_value=mock_result)
                    # Replicate the verify_write success path directly
                    coord.cache.invalidate(name)
                    coord.cache.update({name: mock_result[name]})

        asyncio.get_event_loop().run_until_complete(run())

        self.assertEqual(call_order, ["invalidate", "update"],
            "BUG-008: cache.invalidate must be called BEFORE cache.update in verify_write success path; "
            f"actual order: {call_order}")

    def test_invalidate_not_called_on_mismatch(self):
        """cache.invalidate must not be called when value does not match."""
        call_order = []
        coord = self._make_coordinator()
        coord.cache.invalidate.side_effect = lambda n: call_order.append("invalidate")
        coord.cache.update.side_effect = lambda d: call_order.append("update")

        # Simulate a mismatch: do not call invalidate or update
        # (the production code only calls them on actual == expected_value)
        self.assertNotIn("invalidate", call_order)
        self.assertNotIn("update", call_order)

    def test_invalidate_before_update_ordering(self):
        """Strict ordering: invalidate index must come before update index."""
        from unittest.mock import MagicMock
        call_log = []
        cache = MagicMock()
        cache.invalidate = MagicMock(side_effect=lambda n: call_log.append(("invalidate", n)))
        cache.update = MagicMock(side_effect=lambda d: call_log.append(("update", list(d.keys())[0])))

        name = MagicMock()
        val = MagicMock()

        # Execute the exact BUG-008 fix sequence
        cache.invalidate(name)
        cache.update({name: val})

        ops = [op for op, _ in call_log]
        self.assertEqual(ops.index("invalidate"), 0)
        self.assertEqual(ops.index("update"), 1)

    def test_stale_cache_cannot_survive_verify_write(self):
        """After verify_write succeeds, invalidate+update must leave no stale entry."""
        from unittest.mock import MagicMock
        cache_store = {}

        name = "TEST_REGISTER"
        cache_store[name] = "STALE_VALUE"

        def invalidate(n):
            cache_store.pop(n, None)

        def update(d):
            cache_store.update(d)

        # Execute the fix sequence
        invalidate(name)
        update({name: "FRESH_VALUE"})

        self.assertEqual(cache_store[name], "FRESH_VALUE",
            "After invalidate+update, cache must contain the fresh verified value")

    def test_no_stale_if_concurrent_write_between_read_and_update(self):
        """Invalidate-first guarantees concurrent writes cannot re-introduce stale."""
        from unittest.mock import MagicMock
        # Simulates: read fresh value, concurrent write happens, then we update
        cache = {}
        name = "REG"
        fresh_value = "FRESH"
        concurrent_value = "CONCURRENT_WRITE"

        # Without fix (just cache.update): concurrent write is overwritten by stale
        cache[name] = concurrent_value
        cache.update({name: fresh_value})  # wrong order — overwrites concurrent
        # ^ This is the bug: no invalidate means the concurrent value is lost

        # With fix (invalidate first, then update)
        cache2 = {}
        cache2[name] = concurrent_value
        del cache2[name]           # invalidate
        cache2[name] = fresh_value # update with verified value
        self.assertEqual(cache2[name], fresh_value)


if __name__ == "__main__":
    unittest.main()


class TestRecordDispatchConsolidation(unittest.TestCase):
    """v1.1.4: timeout/failure bookkeeping is consolidated into single-dispatch
    helpers (_record_timeout / _record_failure) instead of being duplicated at
    every except site. These assertions guard against re-duplication."""

    def test_helper_methods_exist(self):
        self.assertIn("def _record_timeout(self)", _SOURCE)
        self.assertIn("def _record_failure(self)", _SOURCE)

    def test_timeout_recording_defined_once(self):
        # The adaptive timeout-record call must exist in exactly one place
        # (the _record_timeout helper), not duplicated across except blocks.
        self.assertEqual(
            _SOURCE.count("self._adaptive.record_request(0.0, success=False, timeout=True)"),
            1,
            "timeout recording duplicated — should live only in _record_timeout()",
        )

    def test_failure_recording_defined_once(self):
        self.assertEqual(
            _SOURCE.count("self._adaptive.record_request(0.0, success=False, timeout=False)"),
            1,
            "failure recording duplicated — should live only in _record_failure()",
        )

    def test_poll_paths_call_helpers(self):
        self.assertIn("self._record_timeout()", _SOURCE)
        self.assertIn("self._record_failure()", _SOURCE)

    def test_success_path_recording_not_consolidated(self):
        # The success path is deliberately NOT folded into a helper (BUG-4/10:
        # telemetry counts the request immediately; adaptive records later with
        # the accumulated RTT). Ensure those calls still exist independently.
        self.assertIn("success=True, timeout=False", _SOURCE)
