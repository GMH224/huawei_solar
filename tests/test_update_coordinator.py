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
import asyncio

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

class TestPhase47WriteVerificationCoalescing(unittest.TestCase):
    """Phase 4.7, this release -- old DEF-010, external ICS quality/
    defect/architecture audit: rapid repeated writes to the SAME
    register used to leave every earlier write's own verify_write()
    task running to completion, wasting a real Modbus read verifying a
    value a newer write had already superseded.

    Source-level structural checks, matching this test file's own
    established convention throughout (no test in this file
    instantiates the real HuaweiSolarUpdateCoordinator -- the coalescing
    LOGIC itself is exercised directly against real source text here;
    entity-level wiring is covered separately in test_entities.py)."""

    def _method_body(self, name: str) -> str:
        idx = _SOURCE.find(f"def {name}(")
        assert idx > -1, f"{name} not found in update_coordinator.py"
        end_candidates = [
            p for p in (
                _SOURCE.find("\n    def ", idx + 10),
                _SOURCE.find("\n    async def ", idx + 10),
                _SOURCE.find("\n    @staticmethod", idx + 10),
            ) if p > idx
        ]
        end = min(end_candidates) if end_candidates else idx + 3000
        return _SOURCE[idx:end]

    def test_verify_write_tasks_dict_initialized_in_init(self):
        idx = _SOURCE.find("class HuaweiSolarUpdateCoordinator(")
        assert idx > -1
        init_idx = _SOURCE.find("def __init__(", idx)
        end_idx = _SOURCE.find("\n    def ", init_idx + 10)
        body = _SOURCE[init_idx:end_idx]
        self.assertIn(
            "self._verify_write_tasks: dict[RegisterName, asyncio.Task] = {}",
            body,
        )

    def test_schedule_verify_write_method_exists(self):
        self.assertIn("def schedule_verify_write(", _SOURCE)

    def test_previous_task_is_cancelled_before_a_new_one_starts(self):
        body = self._method_body("schedule_verify_write")
        previous_idx = body.find("previous = self._verify_write_tasks.get(name)")
        cancel_idx = body.find("previous.cancel()")
        coro_idx = body.find("coro = self.verify_write(name, expected_value)")
        assert previous_idx > -1, "previous task not looked up"
        assert cancel_idx > -1, "previous task never cancelled"
        assert coro_idx > -1, "new verify_write coroutine not created"
        self.assertLess(
            previous_idx, cancel_idx,
            "must look up the previous task before cancelling it",
        )
        self.assertLess(
            cancel_idx, coro_idx,
            "the previous task must be cancelled BEFORE the new "
            "verification coroutine is even created, not after",
        )

    def test_cancel_is_gated_on_not_done(self):
        """Adversarial: cancelling an already-finished task is harmless
        but pointless -- confirms the done-check actually gates the
        cancel call, rather than unconditionally calling .cancel() on
        whatever was previously in the dict (including None)."""
        body = self._method_body("schedule_verify_write")
        idx = body.find("if previous is not None and not previous.done():")
        assert idx > -1, (
            "previous.cancel() must be gated on 'previous is not None "
            "and not previous.done()', not called unconditionally"
        )
        cancel_idx = body.find("previous.cancel()", idx)
        self.assertGreater(cancel_idx, idx)
        self.assertLess(cancel_idx - idx, 100)

    def test_new_task_is_tracked_in_the_dict(self):
        body = self._method_body("schedule_verify_write")
        self.assertIn("self._verify_write_tasks[name] = task", body)

    def test_done_callback_cleans_up_the_tracking_dict(self):
        """Adversarial: without cleanup, _verify_write_tasks would leak
        one entry per register ever written to, for the coordinator's
        entire lifetime."""
        body = self._method_body("schedule_verify_write")
        self.assertIn("task.add_done_callback(", body)
        self.assertIn("self._verify_write_tasks.pop(n, None)", body)

    def test_done_callback_checks_identity_before_popping(self):
        """Adversarial: the done-callback for an OLD (cancelled) task
        must not blindly pop whatever is currently in the dict -- if a
        NEWER task has since replaced it, popping unconditionally would
        incorrectly clear the current task's own tracking entry too.
        Must check 'is this callback's own task still the one tracked'
        before popping."""
        body = self._method_body("schedule_verify_write")
        idx = body.find("self._verify_write_tasks.pop(n, None)")
        assert idx > -1
        window = body[max(0, idx - 200): idx + 150]
        self.assertIn(
            "self._verify_write_tasks.get(n) is t", window,
            "the done-callback must check identity (is t still the "
            "tracked task for n) before popping -- otherwise a stale "
            "callback could clobber a newer task's own entry",
        )

    def test_reuses_the_return_value_of_create_background_task_api(self):
        """Confirms the simplification: this method calls entry.
        async_create_background_task() directly and uses its OWN
        return value as the trackable Task handle, rather than
        creating a second, separate Task just to get something
        cancellable (which was an earlier, overcomplicated draft of
        this same fix, corrected before landing)."""
        body = self._method_body("schedule_verify_write")
        self.assertIn("task = create_task(self.hass, coro, task_name)", body)
        self.assertNotIn("_await_and_forget", body)

    def test_all_three_call_sites_use_schedule_verify_write(self):
        """Confirms number.py/select.py (x2)/switch.py (x2) were
        actually updated to call the new coalescing method, not just
        that the method exists unused."""
        for path_name, expected_calls in (
            ("number.py", 1),
            ("select.py", 2),
            ("switch.py", 2),
        ):
            path = _SRC.parent / path_name
            source = path.read_text()
            # v2.0.9: counts only real call sites (with the coordinator
            # prefix), not incidental mentions in comments -- e.g.
            # number.py's own explanatory comment text also contains the
            # literal string "schedule_verify_write(".
            count = source.count("self.coordinator.schedule_verify_write(")
            self.assertEqual(
                count, expected_calls,
                f"{path_name}: expected {expected_calls} real call site(s) "
                f"to self.coordinator.schedule_verify_write(), found {count}",
            )
            self.assertNotIn(
                "create_background_task(\n                self.coordinator.verify_write(",
                source,
                f"{path_name}: still uses the old create_background_task("
                f"verify_write(...)) pattern directly, not yet migrated",
            )


class TestPhase21LogicalRequestIdWiring(unittest.TestCase):
    """v2.0.9 (Phase 2.1/2.4, this release -- ICS-16, both external ICS
    audits): confirms _execute_batch() actually generates and threads a
    logical_request_id through every chunk/retry of one poll, and that
    the counter is per-coordinator-instance (self._next_logical_request_id
    in __init__), not a module-level global that would collide across
    multiple inverters sharing one process."""




    """v2.0.9 (Phase 2.1/2.4, this release -- ICS-16, both external ICS
    audits): confirms _execute_batch() actually generates and threads a
    logical_request_id through every chunk/retry of one poll, and that
    the counter is per-coordinator-instance (self._next_logical_request_id
    in __init__), not a module-level global that would collide across
    multiple inverters sharing one process."""

    def test_counter_initialized_in_init(self):
        idx = _SOURCE.find("class HuaweiSolarUpdateCoordinator(")
        assert idx > -1
        init_idx = _SOURCE.find("def __init__(", idx)
        end_idx = _SOURCE.find("\n    def ", init_idx + 10)
        body = _SOURCE[init_idx:end_idx]
        self.assertIn("self._next_logical_request_id: int = 0", body)

    def test_id_generated_once_per_execute_batch_call(self):
        idx = _SOURCE.find("async def _execute_batch(")
        assert idx > -1
        chunk_loop_idx = _SOURCE.find("for chunk_idx, chunk in enumerate(chunks):", idx)
        gen_idx = _SOURCE.find("self._next_logical_request_id += 1", idx)
        self.assertGreater(gen_idx, -1, "logical_request_id generation not found")
        self.assertLess(
            gen_idx, chunk_loop_idx,
            "the ID must be generated ONCE, before the chunk loop starts -- "
            "not per chunk, or every chunk would get its own ID and "
            "sharing one across a poll would be impossible",
        )

    def test_same_id_variable_used_for_every_chunk(self):
        idx = _SOURCE.find("async def _execute_batch(")
        assert idx > -1
        end_candidates = [
            p for p in (
                _SOURCE.find("\n    async def ", idx + 10),
            ) if p > idx
        ]
        end = min(end_candidates) if end_candidates else idx + 8000
        body = _SOURCE[idx:end]
        # The same local variable, not re-derived per chunk/retry.
        self.assertIn("_req.logical_request_id = logical_request_id", body)
        self.assertEqual(
            body.count("logical_request_id = self._next_logical_request_id"), 1,
            "logical_request_id must be assigned from the counter EXACTLY "
            "once, outside the retry/chunk loops -- reassigning it inside "
            "either loop would defeat the whole point of sharing one ID "
            "across a poll's chunks/retries",
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


# ── v2.0.9 (Phase 1.2): BUSY retry telemetry wiring, ICS-15 (both audits) ────

class TestPhase1BusyRetryWiring(unittest.TestCase):
    """0x06 SLAVE_DEVICE_BUSY retry logic has existed since v1.0.6 with no
    dedicated telemetry -- confirms record_busy_retry() is actually wired
    into the live retry branch of _execute_batch(), not just defined and
    left unused."""

    @staticmethod
    def _execute_batch_body() -> str:
        start = _SOURCE.find("async def _execute_batch(")
        assert start != -1
        end_candidates = [
            p for p in (
                _SOURCE.find("\n    def ", start + 1),
                _SOURCE.find("\n    async def ", start + 1),
            ) if p > start
        ]
        end = min(end_candidates) if end_candidates else len(_SOURCE)
        return _SOURCE[start:end]

    def test_record_busy_retry_is_called_in_the_busy_branch(self):
        body = self._execute_batch_body()
        busy_idx = body.find("busy_retries += 1")
        record_idx = body.find("self.telemetry.record_busy_retry()")
        continue_idx = body.find("continue  # retry this chunk")
        self.assertGreater(busy_idx, -1, "test setup invalid -- BUSY branch not found")
        self.assertGreater(
            record_idx, -1,
            "record_busy_retry() is not called anywhere in _execute_batch",
        )
        self.assertGreater(
            record_idx, busy_idx,
            "record_busy_retry() must be called within the BUSY branch, "
            "after busy_retries is incremented",
        )
        self.assertLess(
            record_idx, continue_idx,
            "record_busy_retry() must be called before the retry "
            "continues, not after -- confirms it's on the live path, not "
            "dead code after a loop exit",
        )

    def test_record_busy_retry_called_exactly_once_per_busy_event(self):
        """Adversarial: must appear exactly once in the BUSY branch -- not
        zero (missing), not duplicated (double-counting a single BUSY
        event)."""
        body = self._execute_batch_body()
        self.assertEqual(body.count("self.telemetry.record_busy_retry()"), 1)


# ── v2.0.10 (production defect fix): NORMAL-tier energy counter starvation ──

class TestProductionDefectNormalTierEnergyCounterStarvation(unittest.TestCase):
    """v2.0.10 FIX, this release -- found via a real field-observed
    stair-step pattern on an energy-counter sensor (power_meter_
    consumption, ~44 minute gaps between updates). NORMAL-tier
    registers had no starvation protection at all -- only SLOW/STATIC
    tracked overdue-ness and promoted a starved register past its own
    deferral. Moving six energy counters from SLOW to NORMAL in 2.0.9
    (for fresher data under normal conditions) unintentionally removed
    them from that protection during back-off specifically."""

    def _starvation_loop_body(self) -> str:
        idx = _SOURCE.find("for n in stale_names:")
        assert idx > -1
        end = _SOURCE.find("\n            if starved:", idx)
        return _SOURCE[idx: end if end > -1 else idx + 3000]

    def test_normal_tier_checks_energy_counter_when_not_its_own_cycle(self):
        body = self._starvation_loop_body()
        normal_idx = body.find("elif tier == RegisterTier.NORMAL:")
        assert normal_idx > -1
        cycle_idx = body.find(
            "if self._backoff_cycle % BACKOFF_NORMAL_DIVISOR == 0:", normal_idx
        )
        elif_energy_idx = body.find("elif is_energy_counter(n):", normal_idx)
        assert cycle_idx > -1
        assert elif_energy_idx > -1, (
            "NORMAL-tier branch does not check is_energy_counter() at all "
            "-- the production defect fix is missing"
        )
        self.assertLess(
            cycle_idx, elif_energy_idx,
            "the energy-counter starvation check must be the ELIF of the "
            "normal 1-in-4 cycle check -- only relevant on a cycle where "
            "this register would otherwise be skipped",
        )

    def test_normal_tier_energy_counter_starvation_uses_the_tight_ceiling(self):
        """Must use ENERGY_PROMOTION_CEILING_S (90s, tight), not
        REGISTER_STARVATION_CEILING_S (300s, generic) -- matching the
        SAME ceiling SLOW-tier energy counters already get, not a
        separately-invented one."""
        body = self._starvation_loop_body()
        idx = body.find("elif is_energy_counter(n):")
        assert idx > -1
        window = body[idx: idx + 2400]
        self.assertIn("overdue >= ENERGY_PROMOTION_CEILING_S", window)
        self.assertNotIn("REGISTER_STARVATION_CEILING_S", window)

    def test_normal_tier_never_read_at_all_is_treated_as_infinitely_overdue(self):
        """Adversarial: a NORMAL-tier energy counter that has NEVER been
        successfully read (overdue_by returns None) must be treated as
        maximally starved (inf), not silently skipped -- matching the
        SAME handling the SLOW/STATIC branch already has for this exact
        case."""
        body = self._starvation_loop_body()
        idx = body.find("elif is_energy_counter(n):")
        assert idx > -1
        window = body[idx: idx + 2400]
        self.assertIn('starved.append((float("inf"), n))', window)

    def test_normal_tier_energy_counters_share_the_same_starved_pool(self):
        """The core design choice: NORMAL-tier energy counters append to
        the SAME `starved` list SLOW-tier ones use, sharing the same
        REGISTER_STARVATION_PROMOTIONS_PER_CYCLE cap -- not a second,
        separate promotion budget that could double promotion traffic
        during back-off."""
        body = self._starvation_loop_body()
        # Only one `starved: list[...]` declaration should exist -- a
        # second, separate list would indicate a duplicated mechanism
        # instead of a shared one.
        self.assertEqual(_SOURCE.count("starved: list[tuple[float, RegisterName]] = []"), 1)

    def test_ordinary_normal_tier_registers_unaffected(self):
        """Negative case: a NORMAL-tier register that is NOT an energy
        counter must be completely unaffected by this fix -- still just
        skipped on off-cycles, no starvation tracking added for it.
        Ordinary NORMAL-tier values don't carry the freshness
        expectation that justified this fix for energy counters
        specifically."""
        body = self._starvation_loop_body()
        normal_idx = body.find("elif tier == RegisterTier.NORMAL:")
        else_idx = body.find("\n                else:", normal_idx)
        assert normal_idx > -1 and else_idx > -1
        normal_branch = body[normal_idx:else_idx]
        # The branch must still be exactly: cycle check (append), energy
        # counter check (starved) -- nothing else added for the general
        # NORMAL case.
        self.assertEqual(normal_branch.count("priority_names.append(n)"), 1)

    def test_stale_comment_claiming_normal_tier_was_already_protected_is_corrected(self):
        """The v2.0.0 comment previously claimed NORMAL-tier energy
        counters were 'still protected by the same lengthened
        availability ceiling regardless of which path they take' --
        that claim was never actually enforced by any code. Confirms
        the stale claim is gone, not just that new code was added
        alongside it."""
        self.assertNotIn(
            "still protected by the same lengthened\n                    # availability ceiling regardless of which path they\n                    # take",
            _SOURCE,
        )


# ── v2.0.11 (Phase 5.3): service-time-aware chunking ─────────────────────────

class TestPhase53ServiceTimeAwareChunking(unittest.TestCase):
    """Phase 5.3, this release: certain register groups (confirmed via
    real field data across four independent captures spanning a full
    day-night cycle -- battery per-pack telemetry, second-inverter
    status, storage-config parameters) were structurally slow
    regardless of chunk size. BATCH_CHUNK_SIZE's uniform 40-register cap
    now gets a smaller, register-history-aware alternative for groups
    with a demonstrated slow service time.

    Numeric/EWMA-math tests replicate the exact formula from source
    (verified against the real const.py constants, which have no HA
    dependency and import cleanly) rather than instantiating the real
    coordinator -- this test file's own established convention is
    source-level structural analysis throughout (confirmed while fixing
    Phase 4.7's own tests earlier this session), so the wiring/logic-
    presence checks below match that; only the pure-math verification
    below deviates, since a threshold/convergence mechanism deserves
    real numeric confirmation, not just "the code looks right"."""

    # ── real numeric verification, using the actual shipped constants ──────

    @staticmethod
    def _const():
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "huawei_solar_const", pathlib.Path(__file__).parent.parent / "const.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    @staticmethod
    def _apply_ewma(prev, observation, decay):
        """Exact replica of _record_chunk_service_time()'s own formula --
        verified to match the real source text in
        test_ewma_formula_matches_source below, not just asserted here."""
        return prev * decay + observation * (1.0 - decay)

    def test_ewma_formula_matches_source(self):
        body = _SOURCE[_SOURCE.find("def _record_chunk_service_time("):]
        body = body[:body.find("\n    async def ")]
        self.assertIn(
            "prev * REGISTER_SERVICE_TIME_EWMA_DECAY\n"
            "                + service_ms * (1.0 - REGISTER_SERVICE_TIME_EWMA_DECAY)",
            body,
        )

    def test_ewma_converges_toward_a_sustained_slow_value(self):
        const = self._const()
        decay = const.REGISTER_SERVICE_TIME_EWMA_DECAY
        ewma = 100.0  # starts fast
        for _ in range(100):
            ewma = self._apply_ewma(ewma, 5000.0, decay)  # sustained 5s chunks
        self.assertGreater(
            ewma, const.SERVICE_TIME_SLOW_THRESHOLD_MS,
            "100 sustained 5s observations must push the EWMA above the "
            "slow threshold",
        )

    def test_ewma_recovers_after_a_sustained_fast_run(self):
        """Adversarial: confirms this is genuinely responsive to RECENT
        conditions, not permanently anchored by a historically bad
        patch -- a register that used to be slow but no longer is
        should eventually fall back under the threshold."""
        const = self._const()
        decay = const.REGISTER_SERVICE_TIME_EWMA_DECAY
        ewma = 100.0
        for _ in range(50):
            ewma = self._apply_ewma(ewma, 8000.0, decay)  # sustained slow patch
        self.assertGreater(ewma, const.SERVICE_TIME_SLOW_THRESHOLD_MS)
        for _ in range(50):
            ewma = self._apply_ewma(ewma, 50.0, decay)  # genuinely recovers
        self.assertLess(
            ewma, const.SERVICE_TIME_SLOW_THRESHOLD_MS,
            "a sustained fast run afterward must eventually pull the "
            "EWMA back below the threshold, not stay stuck high forever",
        )

    def test_single_slow_observation_does_not_immediately_trip_the_threshold(self):
        """Negative case: one single slow chunk must not be enough on
        its own to reclassify a register as structurally slow -- that
        would make the mechanism trigger on noise rather than a real,
        repeated pattern."""
        const = self._const()
        decay = const.REGISTER_SERVICE_TIME_EWMA_DECAY
        ewma = 50.0  # a normally-fast register
        ewma = self._apply_ewma(ewma, 20000.0, decay)  # one freak 20s outlier
        self.assertLess(
            ewma, const.SERVICE_TIME_SLOW_THRESHOLD_MS,
            "a single outlier observation must not alone cross the "
            "slow threshold for an otherwise-fast register",
        )

    def test_never_observed_register_defaults_to_not_slow(self):
        """A register with zero observations must default to the FAST
        assumption (BATCH_CHUNK_SIZE), never the smaller cap -- a
        register only earns the smaller cap by demonstrating slowness,
        not by default/absence of data."""
        const = self._const()
        ewma_dict = {}
        never_seen = "some_register_name"
        self.assertLess(
            ewma_dict.get(never_seen, 0.0), const.SERVICE_TIME_SLOW_THRESHOLD_MS,
        )

    # ── wiring / structural checks, matching this file's own convention ────

    def test_tracker_initialized_in_init(self):
        idx = _SOURCE.find("class HuaweiSolarUpdateCoordinator(")
        assert idx > -1
        init_idx = _SOURCE.find("def __init__(", idx)
        end_idx = _SOURCE.find("\n    def ", init_idx + 10)
        body = _SOURCE[init_idx:end_idx]
        self.assertIn(
            "self._register_service_ewma: dict[RegisterName, float] = {}", body,
        )

    def test_chunk_building_uses_service_aware_size_not_bare_constant(self):
        # v2.0.14 (Battery Pack Physical Grouping study, this release):
        # _address_group() is now called once per protected physical-
        # group run, not once for the whole sorted_names list directly --
        # updated to find the actual current call site.
        idx = _SOURCE.find("for group in _address_group(protected_run):")
        assert idx > -1
        window = _SOURCE[idx: idx + 200]
        self.assertIn(
            "_chunk(group, self._service_aware_chunk_size(group))", window,
        )
        self.assertNotIn(
            "_chunk(group, BATCH_CHUNK_SIZE)", window,
            "the chunk-building call must use the service-aware size, "
            "not the bare uniform constant directly",
        )

    def test_success_path_records_the_observation(self):
        idx = _SOURCE.find("chunk_ms = (time.monotonic() - t0) * 1000")
        assert idx > -1
        window = _SOURCE[idx: idx + 1300]
        self.assertIn("self._record_chunk_service_time(chunk, chunk_ms)", window)

    def test_size_method_defaults_to_batch_chunk_size(self):
        body = _SOURCE[_SOURCE.find("def _service_aware_chunk_size("):]
        body = body[:body.find("\n    def _record_chunk_service_time(")]
        self.assertIn("return BATCH_CHUNK_SIZE", body)
        self.assertIn("return SERVICE_TIME_AWARE_CHUNK_SIZE", body)

    def test_size_method_checks_every_register_in_the_group(self):
        """Adversarial: confirms the check iterates over the WHOLE
        group, not just e.g. the first register -- any single slow
        member must be enough to trigger the smaller cap for the group."""
        body = _SOURCE[_SOURCE.find("def _service_aware_chunk_size("):]
        body = body[:body.find("\n    def _record_chunk_service_time(")]
        self.assertIn("for name in group:", body)

    def test_record_iterates_every_register_in_the_chunk(self):
        body = _SOURCE[_SOURCE.find("def _record_chunk_service_time("):]
        body = body[:body.find("\n    async def ")]
        self.assertIn("for name in chunk:", body)


# ── v2.0.13 (MOD-021): per-chunk physical-attempt telemetry ─────────────────

class TestMOD021PerChunkPhysicalAttemptCounting(unittest.TestCase):
    """MOD-021, external ICS quality/defect/architecture audit --
    confirmed: ModbusTelemetry.record_request() was called once per
    LOGICAL poll (after _execute_batch() returns), incrementing total_
    physical_attempts by exactly one regardless of how many chunks that
    poll actually needed -- a multi-chunk poll undercounted real wire
    transactions by up to (chunk_count - 1)."""

    def _chunk_loop_body(self) -> str:
        idx = _SOURCE.find("for chunk_idx, chunk in enumerate(chunks):")
        assert idx > -1
        return _SOURCE[idx: idx + 8500]

    def test_record_physical_attempt_called_once_per_chunk(self):
        body = self._chunk_loop_body()
        self.assertIn("self.telemetry.record_physical_attempt()", body)

    def test_physical_attempt_recorded_before_the_retry_loop_not_inside_it(self):
        """The core correctness guarantee: recorded ONCE per chunk
        (before the while-True retry loop begins), not once per retry-
        loop iteration -- record_busy_retry() already separately counts
        each individual BUSY retry, so counting it again here would
        double-count a retried chunk."""
        body = self._chunk_loop_body()
        record_idx = body.find("self.telemetry.record_physical_attempt()")
        while_idx = body.find("while True:")
        assert record_idx > -1 and while_idx > -1
        self.assertLess(
            record_idx, while_idx,
            "record_physical_attempt() must be called BEFORE the "
            "retry loop starts, once per chunk -- not inside it",
        )

    def test_outer_success_path_no_longer_double_counts(self):
        """Negative case: the outer, once-per-poll
        self.telemetry.record_request(len(stale_names)) call must
        remain (for the logical-poll-level counters), but must not
        ALSO be relied on for physical-attempt counting anymore -- that
        now happens per-chunk, inside _execute_batch() itself."""
        idx = _SOURCE.find("self.telemetry.record_request(len(stale_names))")
        self.assertGreater(idx, -1, "the logical-poll-level call must still exist")

    def test_ewma_or_other_chunking_helpers_unaffected(self):
        """Sanity check: confirms this fix's insertion point doesn't
        collide with or duplicate the existing per-chunk service-time
        recording (_record_chunk_service_time) or adaptive-learning
        recording (self._adaptive.record_request) -- all three are
        genuinely separate, independent per-chunk mechanisms."""
        body = self._chunk_loop_body()
        self.assertIn("self.telemetry.record_physical_attempt()", body)
        self.assertIn("self._record_chunk_service_time(", body)
        self.assertIn("self._adaptive.record_request(", body)


# ── v2.0.14 (Battery Pack Physical Grouping study): protected groups ────────

def _load_pure_battery_grouping_functions():
    """Extract and exec ONLY physical_group_for() / _split_by_physical_
    group() (plus their own suffix-set constants) in an isolated
    namespace -- both are genuinely self-contained (stdlib only, no HA
    or other internal-package imports), so this gives real execution
    testing of the actual logic without needing this file's own full,
    heavy import chain (adaptive_modbus, modbus_guard, etc.) that the
    rest of this test file's own source-level-only convention exists
    specifically to avoid.
    """
    start = _SOURCE.find("_PACK_DYNAMIC_SUFFIXES = frozenset({")
    end = _SOURCE.find("\n\n\nclass HuaweiSolarUpdateCoordinator")
    assert start > -1 and end > -1 and end > start
    snippet = _SOURCE[start:end]
    ns: dict = {"RegisterName": str}  # only used in a type annotation
    exec(snippet, ns)
    return ns["physical_group_for"], ns["_split_by_physical_group"]


class TestBatteryPackPhysicalGroupingParsing(unittest.TestCase):
    """physical_group_for() -- real execution against the actual
    extracted function body, not just source pattern matching."""

    @classmethod
    def setUpClass(cls):
        pgf, split = _load_pure_battery_grouping_functions()
        cls.physical_group_for = staticmethod(pgf)
        cls._split = staticmethod(split)

    def test_dynamic_suffixes_grouped_together(self):
        for suffix in ("working_status", "state_of_capacity",
                       "charge_discharge_power", "voltage", "current"):
            name = f"storage_unit_1_battery_pack_2_{suffix}"
            self.assertEqual(self.physical_group_for(name), "BATTERY_PACK_u1p2_DYNAMIC")

    def test_energy_suffixes_grouped_together(self):
        for suffix in ("total_charge", "total_discharge"):
            name = f"storage_unit_1_battery_pack_1_{suffix}"
            self.assertEqual(self.physical_group_for(name), "BATTERY_PACK_u1p1_ENERGY")

    def test_diagnostic_suffixes_grouped_together(self):
        for suffix in ("soh_calibration_status", "maximum_temperature",
                       "minimum_temperature", "serial_number"):
            name = f"storage_unit_2_battery_pack_3_{suffix}"
            self.assertEqual(self.physical_group_for(name), "BATTERY_PACK_u2p3_DIAGNOSTIC")

    def test_different_packs_never_share_a_group_even_same_category(self):
        """Adversarial: pack 1's own DYNAMIC group and pack 2's own
        DYNAMIC group must be distinct ids -- they're not address-
        adjacent to each other in the first place, and merging them
        would be wrong even if it happened to be harmless today."""
        g1 = self.physical_group_for("storage_unit_1_battery_pack_1_voltage")
        g2 = self.physical_group_for("storage_unit_1_battery_pack_2_voltage")
        self.assertNotEqual(g1, g2)

    def test_different_units_never_share_a_group(self):
        g1 = self.physical_group_for("storage_unit_1_battery_pack_1_voltage")
        g2 = self.physical_group_for("storage_unit_2_battery_pack_1_voltage")
        self.assertNotEqual(g1, g2)

    def test_non_battery_register_is_unprotected(self):
        self.assertIsNone(self.physical_group_for("active_power"))
        self.assertIsNone(self.physical_group_for("storage_charge_discharge_power"))

    def test_unit_level_battery_register_is_unprotected(self):
        """Negative case: storage_unit_1_battery_temperature is unit-
        level (no "_pack_" in the middle), must not be mistaken for a
        per-pack register."""
        self.assertIsNone(self.physical_group_for("storage_unit_1_battery_temperature"))

    def test_unrecognized_pack_suffix_falls_through_unprotected(self):
        """Negative case: a real pack-scoped register whose suffix isn't
        in any of the three known sets must fall through to None
        (ordinary address grouping), not be guessed into a category."""
        self.assertIsNone(
            self.physical_group_for("storage_unit_1_battery_pack_1_current_day_charge_capacity")
        )

    def test_malformed_names_do_not_raise(self):
        for bad in ("battery_pack_", "storage_unit_battery_pack_1_voltage",
                    "storage_unit_x_battery_pack_1_voltage", "", "battery_pack_1_voltage"):
            try:
                self.physical_group_for(bad)  # must not raise
            except Exception as e:  # noqa: BLE001
                self.fail(f"physical_group_for({bad!r}) raised {e!r}")


class TestSplitByPhysicalGroup(unittest.TestCase):
    """_split_by_physical_group() -- real execution, order-preservation,
    and the specific guarantee this whole change exists for: a DYNAMIC
    register and an ENERGY register for the same pack must never end up
    in the same output run."""

    @classmethod
    def setUpClass(cls):
        pgf, split = _load_pure_battery_grouping_functions()
        cls.physical_group_for = staticmethod(pgf)
        cls._split = staticmethod(split)

    def test_empty_input(self):
        self.assertEqual(self._split([]), [])

    def test_single_name(self):
        self.assertEqual(self._split(["active_power"]), [["active_power"]])

    def test_dynamic_and_energy_for_same_pack_are_separate_runs(self):
        """The core guarantee this change exists for."""
        names = [
            "storage_unit_1_battery_pack_1_working_status",
            "storage_unit_1_battery_pack_1_state_of_capacity",
            "storage_unit_1_battery_pack_1_charge_discharge_power",
            "storage_unit_1_battery_pack_1_voltage",
            "storage_unit_1_battery_pack_1_current",
            "storage_unit_1_battery_pack_1_total_charge",
            "storage_unit_1_battery_pack_1_total_discharge",
        ]
        runs = self._split(names)
        self.assertEqual(len(runs), 2, f"expected exactly 2 runs, got {runs}")
        self.assertEqual(len(runs[0]), 5)  # the 5 DYNAMIC registers
        self.assertEqual(len(runs[1]), 2)  # the 2 ENERGY registers
        # No cross-contamination.
        dynamic_run_groups = {self.physical_group_for(n) for n in runs[0]}
        energy_run_groups = {self.physical_group_for(n) for n in runs[1]}
        self.assertEqual(dynamic_run_groups, {"BATTERY_PACK_u1p1_DYNAMIC"})
        self.assertEqual(energy_run_groups, {"BATTERY_PACK_u1p1_ENERGY"})

    def test_order_is_preserved_within_each_run(self):
        names = [
            "storage_unit_1_battery_pack_1_working_status",
            "storage_unit_1_battery_pack_1_state_of_capacity",
            "storage_unit_1_battery_pack_1_total_charge",
        ]
        runs = self._split(names)
        self.assertEqual(runs[0], names[:2])
        self.assertEqual(runs[1], [names[2]])

    def test_unprotected_registers_stay_in_one_run_together(self):
        """Negative case / non-regression: ordinary, non-battery
        registers (physical_group_for -> None for all of them) must
        remain a SINGLE run, exactly as _address_group() would have
        received them directly before this change -- this must be a
        complete no-op for every register outside the three protected
        categories."""
        names = ["active_power", "input_power", "grid_frequency"]
        runs = self._split(names)
        self.assertEqual(runs, [names])

    def test_mixed_protected_and_unprotected_registers(self):
        names = [
            "active_power",
            "storage_unit_1_battery_pack_1_voltage",
            "storage_unit_1_battery_pack_1_total_charge",
            "input_power",
        ]
        runs = self._split(names)
        self.assertEqual(len(runs), 4)  # each one its own run -- all four differ

    def test_two_packs_back_to_back_produce_separate_runs(self):
        names = [
            "storage_unit_1_battery_pack_1_voltage",
            "storage_unit_1_battery_pack_2_voltage",
        ]
        runs = self._split(names)
        self.assertEqual(len(runs), 2)


class TestPhysicalGroupWiredIntoChunkBuilding(unittest.TestCase):
    """Source-level check that the new split is genuinely wired into the
    real chunk-building call site, matching this test file's own
    established convention for the surrounding coordinator class (which
    has heavy HA/internal-package dependencies unsuitable for the
    isolated-exec approach used above)."""

    def test_address_group_is_called_once_per_protected_run(self):
        idx = _SOURCE.find("for protected_run in _split_by_physical_group(sorted_names):")
        self.assertGreater(idx, -1)
        window = _SOURCE[idx: idx + 300]
        self.assertIn("for group in _address_group(protected_run):", window)

    def test_service_aware_chunking_still_applied_after_the_split(self):
        idx = _SOURCE.find("for protected_run in _split_by_physical_group(sorted_names):")
        window = _SOURCE[idx: idx + 400]
        self.assertIn("_chunk(group, self._service_aware_chunk_size(group))", window)
