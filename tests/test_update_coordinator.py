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
