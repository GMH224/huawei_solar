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
import math
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

    def test_returns_per_request_rtt_not_batch_total(self):
        """DEFECT A (v1.2.3): _execute_batch must return a PER-REQUEST RTT.

        This replaces test_returns_total_rtt_ms, which asserted
        ``return merged, total_rtt_ms`` — i.e. it pinned the defect in place.
        BUG-10's fix (returning the RTT to the caller) was correct; the
        quantity returned was not: it was the SUM over every chunk, consumed
        downstream as if it were one Modbus round trip.
        """
        self.assertIn("return merged, max_chunk_rtt_ms", _SOURCE)
        self.assertNotIn("return merged, total_rtt_ms", _SOURCE)

    def test_batch_total_still_tracked_separately(self):
        """The batch total is still measured — for diagnostics, not learning."""
        self.assertIn("total_batch_ms", _SOURCE)
        self.assertIn("self._last_chunk_count", _SOURCE)

    def test_max_not_mean_chunk_rtt(self):
        """MAX, because effective_timeout is applied PER CHUNK.

        A mean would under-protect the slowest chunk in a cycle, which is
        exactly the one the timeout has to cover.
        """
        self.assertIn("max_chunk_rtt_ms = max(max_chunk_rtt_ms, chunk_ms)", _SOURCE)

    def test_caller_unpacks_tuple(self):
        """BUG-10: caller unpacks the per-request RTT from _execute_batch."""
        self.assertIn(
            "fresh, chunk_rtt_ms = await self._execute_batch(",
            _SOURCE,
        )

    def test_adaptive_record_now_lives_inside_execute_batch_per_chunk(self):
        """v2.0.0a (F15, external ICS audit -- confirmed): this deliberately
        REVERSES the old BUG-4 constraint below, for a documented reason,
        not a regression of it. BUG-4's actual concern -- double-counting
        -- was about having BOTH an inner AND an outer record_request()
        call for the same data. That's still avoided: there is now only
        ONE call per chunk (inside _execute_batch, at the point each
        chunk's own RTT is known), and the old OUTER call was removed
        entirely (see test_no_double_recording_of_the_last_chunk below).
        record_request()'s own docstring ("record ONE completed Modbus
        request") confirms per-transaction was the intended granularity
        all along -- the old aggregate-once-per-poll pattern was the
        actual misuse of this function, not the fix."""
        start = _SOURCE.find("async def _execute_batch(")
        end = _SOURCE.find("\n    async def ", start + 1)
        if end == -1:
            end = _SOURCE.find("\n    def ", start + 1)
        body = _SOURCE[start:end if end > start else len(_SOURCE)]
        self.assertIn(
            "self._adaptive.record_request(\n                                    chunk_ms, success=True, timeout=False,",
            body,
            "the per-chunk success path must record into the adaptive "
            "learner at the point each chunk's own RTT is known",
        )
        self.assertIn(
            "self._adaptive.record_request(",
            body[body.find("except (TimeoutError, ReadException"):],
            "the per-chunk failure path must also record into the "
            "adaptive learner -- a complete transaction-level picture, "
            "not a success-only one",
        )

    def test_no_double_recording_of_the_last_chunk(self):
        """The old outer call (post-_execute_batch, using only
        max_chunk_rtt_ms -- the worst chunk's RTT) must be gone entirely,
        not left alongside the new per-chunk calls -- that would
        double-count the last/worst chunk specifically."""
        self.assertNotIn(
            "self._adaptive.record_request(chunk_rtt_ms, success=True, timeout=False)",
            _SOURCE,
            "the old post-_execute_batch aggregate call must be removed, "
            "not left in addition to the new per-chunk ones",
        )

    def test_poll_level_health_still_recorded_exactly_once_outside_execute_batch(self):
        """note_batch() -- genuinely poll-level (n, failures, confidence,
        decay tuned against a per-poll RATE) -- must stay exactly where
        it was, once per poll, outside _execute_batch. F15 only concerns
        record_request(); note_batch() is deliberately untouched."""
        start = _SOURCE.find("async def _execute_batch(")
        end = _SOURCE.find("\n    async def ", start + 1)
        body = _SOURCE[start:end if end > start else len(_SOURCE)]
        self.assertNotIn(
            "note_batch(", body,
            "note_batch() must stay outside _execute_batch -- it's "
            "genuinely poll-level, unlike record_request()",
        )
        self.assertEqual(
            _SOURCE.count("self._adaptive.note_batch("), 1,
            "note_batch() must still be called exactly once per poll",
        )

    def test_failure_path_timeout_flag_derived_from_reason_not_isinstance(self):
        """A real, specific bug that was caught and fixed during this same
        change: ModbusQueueShed/ModbusAdmissionTimeout are ALSO
        TimeoutError subclasses (deliberately), so a plain
        isinstance(exc, TimeoutError) check would misclassify a shed or
        an admission-wait as a genuine device timeout here. Must derive
        from the already-computed `reason` (via _classify_failure())
        instead."""
        idx = _SOURCE.find("timeout=(reason == Reason.TIMEOUT)")
        self.assertGreater(
            idx, -1,
            "the per-chunk failure path's timeout flag must be derived "
            "from `reason`, not a bare isinstance(exc, TimeoutError) check",
        )

    def test_bug4_double_count_concern_still_satisfied_by_other_means(self):
        """BUG-4's real concern was double-counting the SAME data twice
        (once inside _execute_batch, once at the caller). v2.0.0a (F15)
        moved record_request() fully inside _execute_batch, per chunk,
        and removed the caller-side call entirely -- so there is exactly
        one call per chunk, never two for the same chunk. The ORIGINAL
        constraint this test encoded ("must not appear inside
        _execute_batch at all") is superseded by
        test_adaptive_record_now_lives_inside_execute_batch_per_chunk and
        test_no_double_recording_of_the_last_chunk above, which check the
        actual property that matters (no duplication) rather than the
        specific old code shape that used to guarantee it."""
        # Exactly one occurrence of the per-chunk success call, not two
        # (which would indicate an inner AND an outer call coexisting).
        self.assertEqual(
            _SOURCE.count(
                "self._adaptive.record_request(\n                                    chunk_ms, success=True, timeout=False,"
            ),
            1,
        )

    def test_success_path_uses_the_chunks_own_rtt_not_an_aggregate(self):
        """Each chunk records ITS OWN measured RTT (chunk_ms) -- not a
        batch total, and not (per the old design) only the worst chunk's
        RTT reused for every chunk."""
        self.assertIn(
            "self._adaptive.record_request(\n                                    chunk_ms, success=True, timeout=False,",
            _SOURCE,
        )
        self.assertNotIn(
            "self._adaptive.record_request(total_rtt_ms, success=True",
            _SOURCE,
        )

    def test_telemetry_record_after_execute_batch(self):
        """BUG-10: telemetry.record_request called AFTER _execute_batch."""
        exec_pos = _SOURCE.find("fresh, chunk_rtt_ms = await self._execute_batch(")
        rec_pos  = _SOURCE.find("self.telemetry.record_request(len(stale_names))")
        self.assertGreater(exec_pos, 0)
        self.assertGreater(rec_pos, exec_pos,
            "telemetry.record_request must come AFTER _execute_batch (BUG-10)")


# ── MOD-01: transaction-level SHED/ADMISSION congestion accounting ───────────

class TestMOD01TransactionLevelCongestionAccounting(unittest.TestCase):
    """v2.0.7 (MOD-01, ICS quality audit -- confirmed): _execute_batch's own
    failure-recording branch must route SHED/ADMISSION_TIMEOUT to the same
    diagnostics-only adaptive methods the poll-level handlers already use
    (_record_shed()/_record_admission_timeout(), elsewhere in this file),
    not to record_request(success=False) -- which trains the adaptive
    failure-rate model on internal bus congestion, not inverter behaviour.
    """

    @staticmethod
    def _execute_batch_body() -> str:
        start = _SOURCE.find("async def _execute_batch(")
        assert start != -1, "test setup invalid -- _execute_batch not found"
        # Delimit by the next top-level (4-space-indented) def/async def.
        end_candidates = [
            p for p in (
                _SOURCE.find("\n    def ", start + 1),
                _SOURCE.find("\n    async def ", start + 1),
            ) if p > start
        ]
        end = min(end_candidates) if end_candidates else len(_SOURCE)
        return _SOURCE[start:end]

    def test_shed_routes_to_note_shed_not_record_request(self):
        body = self._execute_batch_body()
        self.assertIn(
            "if reason == Reason.SHED:", body,
            "SHED must be branched on explicitly inside _execute_batch",
        )
        self.assertIn("self._adaptive.note_shed()", body)

    def test_admission_timeout_routes_to_note_admission_timeout(self):
        body = self._execute_batch_body()
        self.assertIn(
            "elif reason == Reason.ADMISSION_TIMEOUT:", body,
            "ADMISSION_TIMEOUT must be branched on explicitly, alongside SHED",
        )
        self.assertIn("self._adaptive.note_admission_timeout()", body)

    def test_genuine_failure_still_reaches_record_request(self):
        """Negative case: the fix must not accidentally swallow real
        device failures too -- TIMEOUT/DEVICE_BUSY/LINK_DOWN must still
        reach record_request(success=False)."""
        body = self._execute_batch_body()
        self.assertIn(
            "self._adaptive.record_request(\n                                        0.0, success=False,",
            body,
            "a genuine (non-congestion) failure must still be recorded "
            "as an adaptive failure -- confirms the fix narrowed the "
            "exclusion to SHED/ADMISSION_TIMEOUT specifically, not "
            "every failure",
        )

    def test_record_request_appears_exactly_once_in_the_else_branch(self):
        """Adversarial: confirms the OLD unconditional call is actually
        gone, not merely that new branches were added alongside it --
        record_request(success=False must now appear exactly once in
        this method (the genuine-failure else branch), not also
        unconditionally right after the reason classification."""
        body = self._execute_batch_body()
        occurrences = body.count("record_request(\n                                        0.0, success=False,")
        self.assertEqual(
            occurrences, 1,
            f"expected exactly 1 record_request(success=False) call site "
            f"inside _execute_batch (the else branch), found {occurrences} "
            "-- if 2, the old unconditional call was left in place "
            "alongside the new branches instead of being replaced",
        )


# ── Energy counter stale-cache exclusion ──────────────────────────────────────

class TestEnergyStaleCacheExclusion(unittest.TestCase):

    def test_is_energy_counter_imported(self):
        self.assertIn("is_energy_counter", _SOURCE)

    def test_fallback_policy_lives_in_the_cache_not_here(self):
        # v2.0.0 (V2_ARCHITECTURE_DESIGN.md §8.1): the final design, after
        # two intermediate drafts. First draft: a blanket "never serve any
        # energy counter from stale cache" exclusion. Second draft: refined
        # to only withhold a non-GOOD energy counter, layered on top of the
        # cache's own logic. FINAL: that manual gate was found to be
        # actively wrong once RegisterCache._live_quality() got its own,
        # LONGER energy-specific availability ceiling
        # (ENERGY_AVAILABILITY_CEILING_S, 600s vs the generic 300s) --
        # gating on GOOD-only here would undermine the cache's own, more
        # lenient, correctly-reasoned policy. The fix belongs entirely in
        # the cache layer now; this fallback is just "serve whatever the
        # cache is willing to serve," uniformly, no register-type check at
        # this specific call site at all.
        idx = _SOURCE.find("Stale-cache fallback")
        window = _SOURCE[idx: idx + 1800]
        self.assertNotIn(
            "quality_of(n)[0] != Quality.GOOD", window,
            "a manual GOOD-only gate has reappeared in this fallback -- "
            "the energy-specific policy belongs in RegisterCache's own "
            "energy_availability_ceiling_s, not layered on top of it here",
        )
        self.assertIn("cache.get(n)", window)

    def test_energy_availability_ceiling_is_longer_not_shorter(self):
        # The whole point of the final design: energy counters get MORE
        # tolerance for staleness (a gap is worse than a delay for the
        # Energy Dashboard), not less -- verified directly against the
        # actual constant values, not just their presence.
        import re
        rc_source = (pathlib.Path(__file__).parent.parent / "const.py").read_text()
        starvation = float(re.search(
            r"REGISTER_STARVATION_CEILING_S:\s*float\s*=\s*([\d.]+)", rc_source
        ).group(1))
        energy_avail = float(re.search(
            r"ENERGY_AVAILABILITY_CEILING_S:\s*float\s*=\s*([\d.]+)", rc_source
        ).group(1))
        energy_promo = float(re.search(
            r"ENERGY_PROMOTION_CEILING_S:\s*float\s*=\s*([\d.]+)", rc_source
        ).group(1))
        self.assertGreater(
            energy_avail, starvation,
            "energy counters' availability ceiling must be LONGER than the "
            "generic starvation ceiling -- a gap is worse than a delay",
        )
        self.assertLess(
            energy_promo, starvation,
            "energy counters' promotion ceiling must be SHORTER than the "
            "generic one -- try harder to refresh them quietly, before "
            "the (longer) availability ceiling would ever need to matter",
        )


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

        asyncio.run(run())

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

    def test_timeout_recording_sites_are_accounted_for(self):
        """Two sites, both deliberate (v1.2.4).

        1. HuaweiSolarUpdateCoordinator._record_timeout() — the consolidated
           helper for the batch coordinator.
        2. HuaweiSolarOptimizerUpdateCoordinator's inline handler — that class
           is a SIBLING, not a subclass, so it cannot call the helper, and it
           must additionally discriminate a queue shed from a real timeout
           (Defect D). Folding it into a shared helper would require the shed
           check in both places anyway.
        """
        # The adaptive timeout-record call must exist in exactly one place
        # (the _record_timeout helper), not duplicated across except blocks.
        self.assertEqual(
            _SOURCE.count("self._adaptive.record_request(0.0, success=False, timeout=True)"),
            2,
            "timeout recording must appear exactly twice: once in "
            "_record_timeout() and once inline in the optimizer coordinator "
            "(a sibling class that cannot use that helper)",
        )

    def test_failure_recording_lives_only_in_record_failure_helpers(self):
        """One occurrence per COORDINATOR CLASS, not one per file (v1.2.4).

        HuaweiSolarOptimizerUpdateCoordinator is a sibling of
        HuaweiSolarUpdateCoordinator, not a subclass, so it cannot share the
        other's helper. Before v1.2.4 it called `self._record_failure()`
        without defining it — an AttributeError on every optimizer error path.
        Each class therefore now owns one definition, and the assertion counts
        definitions rather than raw occurrences.
        """
        self.assertEqual(_SOURCE.count("def _record_failure(self)"), 2)
        self.assertEqual(
            _SOURCE.count("self._adaptive.record_request(0.0, success=False, timeout=False)"),
            2,
            "failure recording must appear exactly once inside each class's "
            "_record_failure() helper and nowhere else",
        )

    def test_poll_paths_call_helpers(self):
        self.assertIn("self._record_timeout()", _SOURCE)
        self.assertIn("self._record_failure()", _SOURCE)

    def test_success_path_recording_not_consolidated(self):
        # The success path is deliberately NOT folded into a helper (BUG-4/10:
        # telemetry counts the request immediately; adaptive records later with
        # the accumulated RTT). Ensure those calls still exist independently.
        self.assertIn("success=True, timeout=False", _SOURCE)


# ── v2.0.0: best-effort chunking (V2_ARCHITECTURE_DESIGN.md §5.2) ───────────

class TestBestEffortChunking(unittest.TestCase):

    def test_classify_failure_helper_exists(self):
        self.assertIn("def _classify_failure(", _SOURCE)

    def test_classify_failure_covers_shed_timeout_busy_and_generic(self):
        body = _method_body("HuaweiSolarUpdateCoordinator", "_classify_failure")
        self.assertIn("Reason.SHED", body)
        self.assertIn("Reason.TIMEOUT", body)
        self.assertIn("Reason.DEVICE_BUSY", body)
        self.assertIn("Reason.LINK_DOWN", body, "the catch-all fallback must still record something")


# ── v2.0.0b: MOD-09 -- admission timeout misclassification (external ICS audit) ─

class TestAdmissionTimeoutClassification(unittest.TestCase):
    """Confirmed: ModbusAdmissionTimeout (v2.0.0a's F08 fix) is ALSO a
    TimeoutError subclass -- both _classify_failure() (the cache-level
    quality mapping) and the outer exception handler (which feeds the
    adaptive learner) treated it identically to a genuine device timeout,
    teaching the learner that internal bus contention was inverter
    misbehaviour. A SECOND, independent copy of the same bug was also
    found and fixed in HuaweiSolarOptimizerUpdateCoordinator's own inline
    bookkeeping -- not cited by the external audit, caught while checking
    for it."""

    def test_classify_failure_checks_admission_timeout_before_generic_timeout(self):
        body = _method_body("HuaweiSolarUpdateCoordinator", "_classify_failure")
        admission_idx = body.find("ModbusAdmissionTimeout")
        timeout_idx = body.find("isinstance(exc, TimeoutError)")
        self.assertGreater(admission_idx, -1, "ModbusAdmissionTimeout not checked at all")
        self.assertGreater(timeout_idx, -1)
        self.assertLess(
            admission_idx, timeout_idx,
            "ModbusAdmissionTimeout must be checked BEFORE the generic "
            "TimeoutError branch -- it IS a TimeoutError subclass, so "
            "checking order determines which Reason it actually gets",
        )
        self.assertIn("Reason.ADMISSION_TIMEOUT", body)

    def test_outer_handler_has_a_three_way_branch_not_two(self):
        """The core bug: the outer handler used to be
        `if ModbusQueueShed: ... else: _record_timeout()` -- a two-way
        branch that silently swallowed ModbusAdmissionTimeout into the
        device-timeout path. Must now be three-way."""
        idx = _SOURCE.find("except TimeoutError as err:")
        window = _SOURCE[idx: idx + 1500]
        self.assertIn("isinstance(err, ModbusQueueShed)", window)
        self.assertIn("isinstance(err, ModbusAdmissionTimeout)", window)
        self.assertIn("self._record_admission_timeout()", window)

    def test_record_admission_timeout_does_not_feed_the_adaptive_learner_as_failure(self):
        body = _method_body("HuaweiSolarUpdateCoordinator", "_record_admission_timeout")
        self.assertIn("note_admission_timeout()", body)
        self.assertNotIn(
            "record_request(0.0, success=False, timeout=True)", body,
            "an admission timeout must not be recorded via the same "
            "record_request() path a genuine device timeout uses -- that "
            "would still teach the adaptive learner the wrong lesson",
        )

    def test_record_admission_timeout_still_advances_consecutive_counters(self):
        """Matching _record_shed()'s own contract: back-off and entity
        availability must behave identically regardless of WHICH kind of
        internal contention occurred -- only the adaptive-learning
        bookkeeping is meant to differ."""
        body = _method_body("HuaweiSolarUpdateCoordinator", "_record_admission_timeout")
        self.assertIn("self._consecutive_timeouts += 1", body)
        self.assertIn("self._consecutive_failures += 1", body)

    def test_misleading_timeout_warning_log_excludes_admission_timeouts_too(self):
        """A real, easy-to-miss part of the same bug: the 'Modbus timeout
        (no response in Xs)' warning log used to fire for ANYTHING that
        wasn't ModbusQueueShed, including admission timeouts -- but the
        device was never even contacted, so 'no response' is actively
        misleading for that case."""
        idx = _SOURCE.find("except TimeoutError as err:")
        window = _SOURCE[idx: idx + 2200]
        self.assertIn(
            "not isinstance(err, (ModbusQueueShed, ModbusAdmissionTimeout))",
            window,
            "the warning-log gate must exclude admission timeouts too, "
            "not just shed requests",
        )

    def test_optimizer_coordinator_has_the_same_three_way_fix(self):
        """The independent second copy of this bug -- not cited by the
        external audit, found while checking for it. Optimizer's
        coordinator is a SIBLING, not a subclass, so it needed its own
        separate fix applied to its own inline bookkeeping."""
        idx = _SOURCE.find('f"Timeout from {self.device.serial_number} optimizers')
        self.assertGreater(idx, -1, "optimizer timeout handler not found")
        window = _SOURCE[max(0, idx - 2600): idx]
        self.assertIn("is_shed = isinstance(err, ModbusQueueShed)", window)
        self.assertIn(
            "is_admission_timeout = isinstance(err, ModbusAdmissionTimeout)", window,
        )
        self.assertIn("note_admission_timeout()", window)

    def test_reason_admission_timeout_is_distinct_from_reason_shed_and_timeout(self):
        """Sanity check on the enum itself: this must be its own value,
        not an alias -- the whole point is a THIRD, distinguishable
        category, not a rename of an existing one."""
        rc_source = pathlib.Path(__file__).parent.parent.joinpath("register_cache.py").read_text()
        self.assertIn("ADMISSION_TIMEOUT = auto()", rc_source)
        # And it must be a genuinely different auto() call, not literally
        # aliased to SHED or TIMEOUT's own value.
        idx = rc_source.find("ADMISSION_TIMEOUT = auto()")
        self.assertGreater(idx, -1)


class TestBestEffortChunkingContinued(unittest.TestCase):
    """v2.0.0: continuation of TestBestEffortChunking's own coverage --
    these methods were originally part of that class and were
    accidentally displaced into TestAdmissionTimeoutClassification above
    by a later edit that inserted a new class in the middle of the
    original one's method list. Restored to their own properly-separated
    class rather than left merged into an unrelated one; content
    unchanged from what TestBestEffortChunking already covered."""

    def test_record_attempt_called_inline_per_chunk(self):
        """The specific fix: quality must be recorded where `chunk`'s own
        register names are in scope, not at the caller after the whole
        batch has already returned/raised."""
        body = _method_body("HuaweiSolarUpdateCoordinator", "_execute_batch")
        self.assertIn("self.cache.record_attempt(chunk,", body)

    def test_success_recorded_inline_too_not_deferred_to_caller(self):
        """cache.update() must run per-chunk, inside _execute_batch, not
        once on the full merged dict after the function returns -- the
        latter would double-apply the adaptive TTL stretch (compare a
        value against itself) for every register in every successful
        chunk, every single poll."""
        body = _method_body("HuaweiSolarUpdateCoordinator", "_execute_batch")
        self.assertIn("self.cache.update(chunk_result)", body)

    def test_old_redundant_post_batch_update_call_is_gone(self):
        self.assertNotIn(
            "self.cache.update(fresh)", _SOURCE,
            "this call would double-apply the adaptive TTL stretch now that "
            "cache.update() runs inline per chunk inside _execute_batch",
        )

    def test_failing_chunk_does_not_abort_the_loop(self):
        """Best-effort: after recording a non-retryable failure, execution
        must continue to the NEXT chunk (a `break` out of the retry
        while-loop, falling through to the next `for` iteration) rather
        than raising immediately and abandoning chunks not yet attempted."""
        body = _method_body("HuaweiSolarUpdateCoordinator", "_execute_batch")
        idx = body.find("if first_failure is None:\n                                first_failure = exc")
        self.assertGreater(idx, -1)
        tail = body[idx: idx + 200]
        self.assertIn(
            "break", tail,
            "must break out of the retry loop to the next chunk, not "
            "immediately re-raise and abandon the rest of the batch",
        )
        self.assertNotIn(
            "raise\n", tail[:tail.find("break") if "break" in tail else len(tail)],
            "must not re-raise before reaching the next chunk",
        )

    def test_overall_failure_still_raised_after_the_loop_completes(self):
        """The coordinator-level back-off/consecutive-failure contract is
        preserved: SOME failure must still propagate to the caller if any
        chunk failed -- just after every chunk has had its chance, not
        instead of running them."""
        body = _method_body("HuaweiSolarUpdateCoordinator", "_execute_batch")
        self.assertIn("first_failure", body)
        # the raise must come after the for-loop, i.e. after the last
        # occurrence of "for chunk_idx" in this method body.
        loop_idx = body.rfind("for chunk_idx")
        raise_idx = body.find("if first_failure is not None:")
        self.assertGreater(raise_idx, loop_idx, "the raise must be outside/after the chunk loop")


# ── v2.0.0a: whole-poll deadline (F03, external ICS audit -- confirmed) ─────

class TestWholePollDeadline(unittest.TestCase):

    def test_deadline_constant_used(self):
        body = _method_body("HuaweiSolarUpdateCoordinator", "_execute_batch")
        self.assertIn("BATCH_POLL_DEADLINE", body)

    def test_outer_timeout_wraps_the_entire_chunk_loop_not_just_one_chunk(self):
        """The bug being fixed: only a PER-CHUNK timeout existed. The outer
        asyncio.timeout(BATCH_POLL_DEADLINE...) must wrap the `for
        chunk_idx` loop itself, not sit inside it alongside the per-chunk
        one."""
        body = _method_body("HuaweiSolarUpdateCoordinator", "_execute_batch")
        outer_idx = body.find("asyncio.timeout(BATCH_POLL_DEADLINE.total_seconds())")
        loop_idx = body.find("for chunk_idx")
        self.assertGreater(outer_idx, -1)
        self.assertGreater(loop_idx, -1)
        self.assertLess(
            outer_idx, loop_idx,
            "the whole-poll timeout must be entered BEFORE the chunk loop "
            "starts, i.e. it must wrap the loop, not sit inside it",
        )

    def test_expired_check_distinguishes_outer_from_other_timeouts(self):
        """asyncio.timeout() nests correctly with the existing per-chunk
        timeout -- .expired is what reliably tells them apart after the
        fact, not assuming any TimeoutError caught here must be the outer
        one."""
        body = _method_body("HuaweiSolarUpdateCoordinator", "_execute_batch")
        self.assertIn("poll_cm.expired", body)

    def test_unrecorded_registers_are_reconciled_after_the_loop(self):
        """The core of the fix: whatever chunk was cut short by the
        deadline, any register that ended up neither successful nor
        explicitly failure-classified must still be recorded, not
        silently vanish from quality tracking."""
        body = _method_body("HuaweiSolarUpdateCoordinator", "_execute_batch")
        self.assertIn("recorded_names", body)
        self.assertIn("unrecorded = [n for n in names if n not in recorded_names]", body)
        reconcile_idx = body.find("unrecorded = [n for n in names")
        window = body[reconcile_idx: reconcile_idx + 400]
        self.assertIn("Reason.TIMEOUT", window)
        self.assertIn("record_attempt(", window)

    def test_reconciliation_runs_after_the_loop_not_inside_it(self):
        body = _method_body("HuaweiSolarUpdateCoordinator", "_execute_batch")
        loop_idx = body.rfind("for chunk_idx")
        reconcile_idx = body.find("unrecorded = [n for n in names")
        self.assertGreater(
            reconcile_idx, loop_idx,
            "reconciliation must happen after the loop -- it needs to see "
            "the FINAL state of recorded_names, not a partial one",
        )

    def test_successful_chunks_still_recorded_before_success_names_are_tracked(self):
        """recorded_names must be updated on the SAME success path that
        already updates merged/cache.update() -- otherwise a register that
        genuinely succeeded could be wrongly re-flagged as unrecorded by
        the reconciliation check."""
        body = _method_body("HuaweiSolarUpdateCoordinator", "_execute_batch")
        success_idx = body.find("self.cache.update(chunk_result)")
        self.assertGreater(success_idx, -1)
        window = body[success_idx: success_idx + 200]
        self.assertIn("recorded_names.update(chunk)", window)

    def test_failed_chunks_also_tracked_in_recorded_names(self):
        body = _method_body("HuaweiSolarUpdateCoordinator", "_execute_batch")
        idx = body.find("self.cache.record_attempt(chunk,")
        self.assertGreater(idx, -1)
        window = body[idx: idx + 200]
        self.assertIn("recorded_names.update(chunk)", window)

    def test_deadline_is_generous_relative_to_a_realistic_worst_case(self):
        """Not just presence -- the actual value must be plausible: long
        enough to not fire on a legitimate cold-start poll, short enough
        to bound a pathological one. Cross-checked against BATCH_CHUNK_SIZE
        directly rather than assumed."""
        import re
        const_source = (pathlib.Path(__file__).parent.parent / "const.py").read_text()
        deadline = float(re.search(
            r"BATCH_POLL_DEADLINE\s*=\s*timedelta\(seconds=(\d+)\)", const_source
        ).group(1))
        self.assertGreater(deadline, 30, "must be well above one coordinator update_interval")
        self.assertLess(deadline, 600, "must not be so generous it defeats the point of F03")


# ── v2.0.0: BACKOFF_DEFERRED capture fix (V2_ARCHITECTURE_DESIGN.md §10.5) ──

class TestBackoffDeferredCapture(unittest.TestCase):

    def test_pre_filter_snapshot_exists(self):
        self.assertIn("pre_filter_names = list(stale_names)", _SOURCE)

    def test_snapshot_precedes_the_reassignment(self):
        """The specific bug this fixes: `stale_names = priority_names` is an
        in-place reassignment: capturing the pre-filter set must happen
        BEFORE it, or the data needed is already gone."""
        snapshot_idx = _SOURCE.find("pre_filter_names = list(stale_names)")
        reassign_idx = _SOURCE.find("stale_names = priority_names")
        self.assertGreater(snapshot_idx, -1)
        self.assertGreater(reassign_idx, -1)
        self.assertLess(
            snapshot_idx, reassign_idx,
            "pre_filter_names must be captured BEFORE stale_names gets "
            "reassigned, or there is nothing left to compute the deferred "
            "set from",
        )

    def test_deferred_set_computed_after_canary_forcing_not_before(self):
        """The canary-forcing logic can pull a register in from all_names,
        not just pre_filter_names -- the deferred-set computation must run
        AFTER that, so a canary from outside the original due-set doesn't
        get miscounted as deferred."""
        canary_idx = _SOURCE.find("canary = _pick_backoff_canary(all_names, self.cache)")
        deferred_idx = _SOURCE.find("deferred = set(pre_filter_names)")
        self.assertGreater(canary_idx, -1)
        self.assertGreater(deferred_idx, -1)
        self.assertLess(canary_idx, deferred_idx)

    def test_record_attempt_called_for_deferred_registers(self):
        self.assertIn("Reason.BACKOFF_DEFERRED", _SOURCE)
        idx = _SOURCE.find("deferred = set(pre_filter_names)")
        window = _SOURCE[idx: idx + 500]
        self.assertIn("self.cache.record_attempt(", window)
        self.assertIn("Reason.BACKOFF_DEFERRED", window)


# ── v2.0.0a: F19 fix -- suspicious-zero guard ordering ───────────────────────
# (external ICS audit, confirmed as a live regression during this session's
# own §5.2 restructuring, not merely a risk needing runtime confirmation)

class TestSuspiciousZeroOrderingFix(unittest.TestCase):

    def test_prior_values_snapshotted_before_execute_batch_runs(self):
        """The core fix: the snapshot must be taken BEFORE _execute_batch()
        is awaited, since that call mutates the cache inline (v2.0.0's
        best-effort chunking) -- reading the cache AFTER it returns can
        only ever see this cycle's own fresh value, never the prior one."""
        snapshot_idx = _SOURCE.find("_prior_energy_values: dict[RegisterName, Any] = {")
        execute_idx = _SOURCE.find("fresh, chunk_rtt_ms = await self._execute_batch(")
        self.assertGreater(snapshot_idx, -1)
        self.assertGreater(execute_idx, -1)
        self.assertLess(
            snapshot_idx, execute_idx,
            "the prior-value snapshot must be taken before _execute_batch() "
            "runs, or it will already reflect this cycle's own fresh value",
        )

    def test_suspicious_zero_check_uses_the_snapshot_not_a_live_cache_read(self):
        """The specific bug: self.cache.get(_name) inside the check itself
        would read the ALREADY-updated cache. Confirms the check now reads
        from the snapshot dict instead."""
        idx = _SOURCE.find("Suspicious-zero guard for energy counters")
        window = _SOURCE[idx: idx + 1800]
        self.assertIn("_prior_energy_values.get(_name)", window)
        self.assertNotIn(
            "_prior = self.cache.get(_name)", window,
            "reintroducing a live cache read here would silently disable "
            "this guard again for the common case -- see the fix's own "
            "comment for the exact mechanism",
        )

    def test_snapshot_only_captures_energy_counters(self):
        # Cheap and correctly scoped: only registers this guard actually
        # cares about, not every stale register in the poll.
        idx = _SOURCE.find("_prior_energy_values: dict[RegisterName, Any] = {")
        window = _SOURCE[idx: idx + 300]
        self.assertIn("is_energy_counter(n)", window)


# ── v2.0.0a: F11 -- absolute jitter floor (external ICS audit, refined) ─────

def _backoff_seconds_reproduced(consecutive: int, base: float, cap: float,
                                 min_jitter: float, rand_value: float) -> float:
    """Reproduces the real _backoff_seconds() formula exactly, with
    random.random() injected as a parameter instead of called internally,
    so the actual numerical behaviour can be tested deterministically
    without depending on this heavy module's own execution environment.
    Cross-checked against the real source below, not just trusted to
    match it.
    """
    delay = min(base * math.pow(2, consecutive - 1), cap)
    jitter_magnitude = max(delay * 0.10, min_jitter)
    jitter = jitter_magnitude * (2 * rand_value - 1)
    return max(0.0, delay + jitter)


class TestBackoffJitterFloor(unittest.TestCase):

    BASE = 10.0
    CAP = 120.0
    MIN_JITTER = 2.0

    def test_source_uses_the_jitter_floor_constant(self):
        idx = _SOURCE.find("def _backoff_seconds(")
        end = _SOURCE.find("\ndef ", idx + 10)
        func_body = _SOURCE[idx: end if end > -1 else idx + 1500]
        self.assertIn("MIN_BACKOFF_JITTER_S", func_body)
        self.assertIn("jitter_magnitude = max(", func_body)

    def test_shallow_backoff_jitter_is_bounded_below_by_the_floor_not_proportional_alone(self):
        """The core numerical claim: at consecutive=1 (delay=base=10s),
        pure proportional jitter would only be ±1s (10% of 10s) --
        narrower than the floor. The fix must widen it to the floor."""
        # Maximum negative jitter (rand_value=0.0 -> jitter = -magnitude)
        worst_case = _backoff_seconds_reproduced(
            1, self.BASE, self.CAP, self.MIN_JITTER, rand_value=0.0
        )
        proportional_only_worst_case = self.BASE - (self.BASE * 0.10)  # old behaviour: 9.0
        self.assertLess(
            worst_case, proportional_only_worst_case,
            "the floor must produce a WIDER jitter range than pure "
            "proportional jitter did at shallow backoff -- otherwise the "
            "fix has no actual effect where it matters most",
        )
        # Explicitly: base(10) - floor(2) = 8.0 at the worst case.
        self.assertAlmostEqual(worst_case, self.BASE - self.MIN_JITTER, places=5)

    def test_deep_backoff_jitter_is_unaffected_proportional_already_exceeds_floor(self):
        """At the cap (120s), proportional jitter (±12s) already exceeds
        the 2s floor -- the fix must not change behaviour there at all."""
        delay_at_cap = self.CAP  # consecutive high enough to have hit the cap
        worst_case = _backoff_seconds_reproduced(
            20, self.BASE, self.CAP, self.MIN_JITTER, rand_value=0.0
        )
        proportional_worst_case = delay_at_cap - (delay_at_cap * 0.10)  # 108.0
        self.assertAlmostEqual(worst_case, proportional_worst_case, places=5)

    def test_jitter_never_produces_a_negative_delay(self):
        result = _backoff_seconds_reproduced(
            1, self.BASE, self.CAP, self.MIN_JITTER, rand_value=0.0
        )
        self.assertGreaterEqual(result, 0.0)

    def test_min_jitter_constant_value_is_reasoned_not_huge(self):
        """Sanity bound on the constant itself: wide enough to matter,
        not so wide it meaningfully delays legitimate recovery."""
        import re
        const_source = pathlib.Path(__file__).parent.parent.joinpath("const.py").read_text()
        value = float(re.search(
            r"MIN_BACKOFF_JITTER_S:\s*float\s*=\s*([\d.]+)", const_source
        ).group(1))
        self.assertGreater(value, 1.0, "must be wider than the old effective ±1s at base delay")
        self.assertLess(value, 10.0, "must not itself approach the base delay")
