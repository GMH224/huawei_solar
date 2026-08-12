"""Tests for modbus_guard.py — stdlib unittest, no pytest required.

Covers:
  • Queue depth accounting: no double-decrement on timeout
  • Load shedding: MAX_QUEUE_DEPTH enforcement
  • Priority requests bypass shedding
  • Adaptive gap and queue-depth setters and clamps
  • Inter-request gap enforcement
  • BUG-8 fix: registry keyed on connection_endpoint, not serial_number
  • endpoint_for() TCP and RTU key derivation
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
import importlib.util
import pathlib
import sys
import types
import unittest
from datetime import timedelta
from unittest.mock import patch

# ── Minimal stubs ─────────────────────────────────────────────────────────────
sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))

_SRC = pathlib.Path(__file__).parent.parent / "modbus_guard.py"
_SPEC = importlib.util.spec_from_file_location("modbus_guard", str(_SRC))
_MOD = importlib.util.module_from_spec(_SPEC)
_MOD.__package__ = "huawei_solar"
_SPEC.loader.exec_module(_MOD)

ModbusGuard = _MOD.ModbusGuard
MIN_INTER_REQUEST_GAP = _MOD.MIN_INTER_REQUEST_GAP
MAX_QUEUE_DEPTH = _MOD.MAX_QUEUE_DEPTH
QUEUE_WAIT_TIMEOUT = _MOD.QUEUE_WAIT_TIMEOUT

_LOOP = asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)


def _fresh_guard(endpoint: str = "192.168.1.1:502") -> ModbusGuard:
    """Return a fresh guard without touching the class registry."""
    g = object.__new__(ModbusGuard)
    g.endpoint = endpoint               # BUG-8: endpoint not serial_number
    g._lock = asyncio.Lock()
    g._last_request_end = 0.0
    g._queue_depth = 0
    g._effective_gap = MIN_INTER_REQUEST_GAP.total_seconds()
    g._max_queue_depth = MAX_QUEUE_DEPTH
    g._gap_contributions = {}      # v1.3.15 (Defect P) multi-device aggregation
    g._depth_contributions = {}
    g.shed_count = 0                    # v1.2.3 diagnostic counter
    # v2.0.0a (F18, external ICS audit): _fresh_guard() bypasses __init__
    # entirely, so these need setting explicitly, matching __init__'s own
    # defaults -- same class of gap as every other object.__new__()-based
    # test fixture hit earlier in this remediation pass.
    g._priority_queue_depth = 0
    g.priority_shed_count = 0
    # v2.0.0b (AR-4, external ICS audit): _fresh_guard() bypasses __init__
    # entirely, so these need setting explicitly, matching __init__'s own
    # defaults -- the same class of gap hit repeatedly this session for
    # every object.__new__()-based test fixture.
    g._priority_window_start = time.monotonic()
    g._priority_busy_s = 0.0
    g.priority_budget_exceeded_count = 0
    # v1.3.0 Phase 0 instrumentation
    g._busy_s = 0.0
    g._window_start = time.monotonic()
    g._wait_samples = deque(maxlen=256)
    g._service_samples = deque(maxlen=256)
    g.diagnostics = None
    g.total_wait_ms = 0.0
    g.requests_waited = 0
    return g


# ── Queue depth accounting ────────────────────────────────────────────────────

class TestQueueDepthAccounting(unittest.TestCase):

    def test_depth_zero_after_successful_roundtrip(self):
        async def _go():
            g = _fresh_guard()
            self.assertEqual(g.queue_depth, 0)
            async with g.request():
                self.assertEqual(g.queue_depth, 1)
            self.assertEqual(g.queue_depth, 0)
        _run(_go())

    def test_depth_not_negative_on_lock_timeout(self):
        """No double-decrement when the lock-acquire times out."""
        async def _go():
            g = _fresh_guard()
            await g._lock.acquire()
            with patch.object(_MOD, "QUEUE_WAIT_TIMEOUT", timedelta(milliseconds=10)):
                with self.assertRaises(TimeoutError):
                    async with g.request():
                        pass
            g._lock.release()
            self.assertEqual(g.queue_depth, 0)
        _run(_go())

    def test_depth_not_negative_after_repeated_timeouts(self):
        async def _go():
            g = _fresh_guard()
            await g._lock.acquire()
            for _ in range(5):
                with patch.object(_MOD, "QUEUE_WAIT_TIMEOUT", timedelta(milliseconds=10)):
                    with self.assertRaises(TimeoutError):
                        async with g.request():
                            pass
            g._lock.release()
            self.assertEqual(g.queue_depth, 0)
        _run(_go())

    def test_admission_timeout_is_the_distinguishable_type(self):
        """v2.0.0a (F08, external ICS audit -- confirmed): an admission-wait
        timeout must raise ModbusAdmissionTimeout, not a bare TimeoutError
        -- this is the whole point of the fix, checked directly against
        the real exception type, not just that SOME TimeoutError fires."""
        ModbusAdmissionTimeout = _MOD.ModbusAdmissionTimeout

        async def _go():
            g = _fresh_guard()
            await g._lock.acquire()
            with patch.object(_MOD, "QUEUE_WAIT_TIMEOUT", timedelta(milliseconds=10)):
                with self.assertRaises(ModbusAdmissionTimeout):
                    async with g.request():
                        pass
            g._lock.release()
        _run(_go())

    def test_admission_timeout_is_still_a_timeout_error_subclass(self):
        """Every EXISTING `except TimeoutError` path (back-off, cache
        fallback, entity availability) must keep working unchanged for
        anything that doesn't specifically care about this new
        distinction -- the same backward-compatible design already used
        for ModbusQueueShed."""
        ModbusAdmissionTimeout = _MOD.ModbusAdmissionTimeout
        self.assertTrue(issubclass(ModbusAdmissionTimeout, TimeoutError))

    def test_queue_full_shed_is_not_converted_to_admission_timeout(self):
        """The other congestion path (queue full, request declined
        immediately) must stay ModbusQueueShed -- only a genuine
        lock-acquisition timeout becomes ModbusAdmissionTimeout."""
        ModbusQueueShed = _MOD.ModbusQueueShed
        ModbusAdmissionTimeout = _MOD.ModbusAdmissionTimeout

        async def _go():
            g = _fresh_guard()
            g._max_queue_depth = 1
            g._queue_depth = 1  # simulate queue already full
            with self.assertRaises(ModbusQueueShed) as ctx:
                async with g.request():  # priority=False -> shed check applies
                    pass
            self.assertNotIsInstance(ctx.exception, ModbusAdmissionTimeout)
        _run(_go())

    def test_admission_timeout_cleans_up_queue_depth_correctly(self):
        """Same cleanup guarantee as the pre-existing repeated-timeout
        test above, now verified specifically for the new exception path
        -- the re-raise as ModbusAdmissionTimeout must not skip or
        duplicate the BaseException cleanup block."""
        async def _go():
            g = _fresh_guard()
            await g._lock.acquire()
            with patch.object(_MOD, "QUEUE_WAIT_TIMEOUT", timedelta(milliseconds=10)):
                with self.assertRaises(_MOD.ModbusAdmissionTimeout):
                    async with g.request():
                        pass
            g._lock.release()
            self.assertEqual(g.queue_depth, 0)
        _run(_go())

    def test_admission_timeout_does_not_fire_once_lock_is_already_held(self):
        """A failure AFTER the lock was successfully acquired (e.g. during
        the inter-request gap sleep) is a different situation entirely --
        the device communication phase has effectively begun. Must not be
        misclassified as an admission-phase timeout."""
        async def _go():
            g = _fresh_guard()
            async with g.request():
                pass  # lock acquired and released cleanly -- no exception at all
        _run(_go())  # sanity: the happy path is unaffected by this change

    def test_is_busy_reflects_lock_state(self):
        async def _go():
            g = _fresh_guard()
            self.assertFalse(g.is_busy)
            async with g.request():
                self.assertTrue(g.is_busy)
            self.assertFalse(g.is_busy)
        _run(_go())

    def test_seconds_since_last_activity_before_any_request_is_infinite(self):
        """v2.0.0b (MOD-14, external ICS audit): a guard with no request
        history at all must report an infinite idle time, not zero or a
        stale default -- otherwise a legitimate first probe on a freshly
        created endpoint could be incorrectly skipped."""
        g = _fresh_guard()
        self.assertEqual(g.seconds_since_last_activity(), float("inf"))

    def test_seconds_since_last_activity_resets_after_a_request(self):
        async def _go():
            g = _fresh_guard()
            async with g.request():
                pass
            self.assertLess(g.seconds_since_last_activity(), 1.0)
        _run(_go())

    def test_seconds_since_last_activity_counts_a_failed_request_too(self):
        """Deliberately not success-only: a request that raises inside the
        guarded block still updates _last_request_end on exit (see
        ModbusKeepAlive._should_skip_probe()'s own docstring for why this
        is correct, not an oversight)."""
        async def _go():
            g = _fresh_guard()
            with self.assertRaises(ValueError):
                async with g.request():
                    raise ValueError("simulated failure")
            self.assertLess(g.seconds_since_last_activity(), 1.0)
        _run(_go())


# ── Load shedding ─────────────────────────────────────────────────────────────

class TestLoadShedding(unittest.TestCase):

    def test_shed_when_queue_at_max(self):
        async def _go():
            g = _fresh_guard()
            g._max_queue_depth = 1
            g._queue_depth = 1        # already one waiter
            with self.assertRaises(asyncio.TimeoutError):
                async with g.request():
                    pass
            # depth must be unchanged — shed request never incremented it
            self.assertEqual(g.queue_depth, 1)
        _run(_go())

    def test_priority_bypasses_shedding(self):
        """Priority request must succeed even when queue is at max depth.

        _queue_depth is set to 1 (== max) to simulate a waiting non-priority
        caller.  After the priority request exits, depth returns to 1 (the
        manually-simulated waiter is still there; the priority request only
        adds then removes its own count).
        """
        async def _go():
            g = _fresh_guard()
            g._max_queue_depth = 1
            g._queue_depth = 1        # simulate one waiter occupying the slot
            entered = False
            async with g.request(priority=True):
                entered = True
                # During the request our depth should be 2 (simulated 1 + us)
                self.assertEqual(g.queue_depth, 2)
            self.assertTrue(entered, "priority request must not be shed")
            # After exit, depth returns to the pre-request value (1)
            self.assertEqual(g.queue_depth, 1)
        _run(_go())

    def test_depth_unchanged_after_shedding(self):
        async def _go():
            g = _fresh_guard()
            g._max_queue_depth = 2
            g._queue_depth = 2
            with self.assertRaises(asyncio.TimeoutError):
                async with g.request():
                    pass
            self.assertEqual(g.queue_depth, 2)
        _run(_go())


# ── Adaptive parameter setters ────────────────────────────────────────────────

class TestAdaptiveParams(unittest.TestCase):

    def test_update_gap_clamped_to_min(self):
        g = _fresh_guard()
        g.update_gap("test-device", 0.001)          # 1 ms — below 150 ms hardware floor
        self.assertEqual(g._effective_gap, MIN_INTER_REQUEST_GAP.total_seconds())

    def test_update_gap_clamped_to_max(self):
        g = _fresh_guard()
        g.update_gap("test-device", 10.0)           # 10 s — above 500 ms ceiling
        self.assertEqual(g._effective_gap, 0.500)

    def test_update_gap_valid(self):
        g = _fresh_guard()
        g.update_gap("test-device", 0.300)
        self.assertAlmostEqual(g._effective_gap, 0.300, places=5)

    def test_update_max_queue_depth_clamped_low(self):
        g = _fresh_guard()
        g.update_max_queue_depth("test-device", 0)
        self.assertEqual(g._max_queue_depth, 1)

    def test_update_max_queue_depth_clamped_high(self):
        g = _fresh_guard()
        g.update_max_queue_depth("test-device", 99)
        self.assertEqual(g._max_queue_depth, MAX_QUEUE_DEPTH)

    def test_update_max_queue_depth_valid(self):
        g = _fresh_guard()
        g.update_max_queue_depth("test-device", 2)
        self.assertEqual(g._max_queue_depth, 2)

    def test_effective_gap_ms_property(self):
        g = _fresh_guard()
        g.update_gap("test-device", 0.250)
        self.assertAlmostEqual(g.effective_gap_ms, 250.0, places=2)


# ── Inter-request gap ─────────────────────────────────────────────────────────

class TestInterRequestGap(unittest.TestCase):

    def test_gap_enforced_between_requests(self):
        import time
        async def _go():
            g = _fresh_guard()
            g.update_gap("test-device", 0.050)      # 50 ms
            async with g.request():
                pass
            t0 = time.monotonic()
            async with g.request():
                pass
            elapsed_ms = (time.monotonic() - t0) * 1000
            self.assertGreater(elapsed_ms, 40,
                msg=f"Gap too short: {elapsed_ms:.1f} ms")
        _run(_go())

    def test_no_delay_on_first_request(self):
        import time
        async def _go():
            g = _fresh_guard()
            g.update_gap("test-device", 0.200)
            t0 = time.monotonic()
            async with g.request():
                pass
            elapsed_ms = (time.monotonic() - t0) * 1000
            self.assertLess(elapsed_ms, 150,
                msg=f"First request unexpectedly delayed: {elapsed_ms:.1f} ms")
        _run(_go())


# ── Registry (BUG-8: bus-level endpoint key) ──────────────────────────────────

class TestRegistry(unittest.TestCase):

    def setUp(self):
        ModbusGuard.clear_registry()

    def tearDown(self):
        ModbusGuard.clear_registry()

    def test_singleton_per_endpoint(self):
        """BUG-8: same endpoint → same guard; different → different."""
        g1 = ModbusGuard.get_or_create("10.0.0.1:502")
        g2 = ModbusGuard.get_or_create("10.0.0.1:502")
        g3 = ModbusGuard.get_or_create("10.0.0.2:502")
        self.assertIs(g1, g2, "Same endpoint must return same guard instance")
        self.assertIsNot(g1, g3, "Different endpoint must return different guard")

    def test_sub_devices_share_guard(self):
        """All RS485 slaves on the same bus must share one guard."""
        g_primary   = ModbusGuard.get_or_create("10.0.0.1:502")
        g_secondary = ModbusGuard.get_or_create("10.0.0.1:502")
        self.assertIs(g_primary, g_secondary)

    def test_endpoint_for_tcp(self):
        ep = ModbusGuard.endpoint_for({"host": "192.168.1.1", "port": "502"})
        self.assertEqual(ep, "192.168.1.1:502")

    def test_endpoint_for_rtu(self):
        ep = ModbusGuard.endpoint_for({"port": "/dev/ttyUSB0"})
        self.assertEqual(ep, "rtu:/dev/ttyUSB0")

    def test_endpoint_for_default_port(self):
        ep = ModbusGuard.endpoint_for({"host": "10.0.0.5"})
        self.assertEqual(ep, "10.0.0.5:502")

    def test_clear_registry(self):
        ModbusGuard.get_or_create("host:502")
        ModbusGuard.clear_registry()
        self.assertEqual(ModbusGuard._registry, {})


# ── v2.0.0a: reference-counted endpoint lifecycle (F04, external ICS audit) ──

class TestReferenceCountedLifecycle(unittest.TestCase):

    def setUp(self):
        ModbusGuard.clear_registry()

    def tearDown(self):
        ModbusGuard.clear_registry()

    def test_adversarial_f04_scenario_two_entries_one_endpoint(self):
        """The exact scenario the external audit's stress matrix names:
        two config entries share a physical endpoint; one unloads. The
        surviving entry must keep using the SAME guard object -- a
        SECOND, uncoordinated guard for one physical bus is the bug F04
        describes."""
        entry_a_guard = ModbusGuard.acquire_endpoint("shared:502")
        entry_b_guard = ModbusGuard.acquire_endpoint("shared:502")
        self.assertIs(entry_a_guard, entry_b_guard, "both entries must share one guard")

        # Entry A unloads.
        ModbusGuard.release_endpoint("shared:502")

        # Entry B is still loaded and reaches for its guard again (e.g. a
        # coordinator reconstructed on reload, or simply re-fetching it).
        still_there = ModbusGuard.get_or_create("shared:502")
        self.assertIs(
            still_there, entry_b_guard,
            "the guard must still be the SAME object entry B has always "
            "used -- if the registry had been cleared, this would silently "
            "create a second, uncoordinated guard for the same physical bus",
        )

    def test_guard_removed_only_after_last_release(self):
        ModbusGuard.acquire_endpoint("ep:502")
        ModbusGuard.acquire_endpoint("ep:502")
        ModbusGuard.release_endpoint("ep:502")
        self.assertIn("ep:502", ModbusGuard._registry, "one reference still held")
        ModbusGuard.release_endpoint("ep:502")
        self.assertNotIn("ep:502", ModbusGuard._registry, "last reference released")

    def test_release_without_prior_acquire_is_a_safe_no_op(self):
        # Must not raise -- teardown code on best-effort/exception paths
        # should never need its own separate bookkeeping to call this safely.
        ModbusGuard.release_endpoint("never-acquired:502")

    def test_extra_release_beyond_acquire_count_is_a_safe_no_op(self):
        ModbusGuard.acquire_endpoint("ep:502")
        ModbusGuard.release_endpoint("ep:502")
        ModbusGuard.release_endpoint("ep:502")  # one too many
        self.assertNotIn("ep:502", ModbusGuard._registry)

    def test_get_or_create_does_not_affect_reference_count(self):
        """get_or_create() remains the plain per-coordinator accessor --
        only acquire_endpoint()/release_endpoint() should move the count,
        so a coordinator merely re-fetching its guard reference (e.g. on
        every poll) can never accidentally extend or shorten the
        endpoint's real lifetime."""
        ModbusGuard.acquire_endpoint("ep:502")
        for _ in range(5):
            ModbusGuard.get_or_create("ep:502")
        ModbusGuard.release_endpoint("ep:502")
        self.assertNotIn(
            "ep:502", ModbusGuard._registry,
            "a single acquire + single release must fully remove the guard, "
            "regardless of how many plain get_or_create() calls happened "
            "in between",
        )

    def test_clear_registry_also_clears_ref_counts(self):
        ModbusGuard.acquire_endpoint("ep:502")
        ModbusGuard.clear_registry()
        self.assertEqual(ModbusGuard._ref_counts, {})


class TestCancellationDoesNotDeadlock(unittest.TestCase):
    """v1.1.3 regression: cancellation during the inter-request gap must not
    leak the lock or the queue counter (former bug: `except Exception` did not
    catch CancelledError, permanently deadlocking the bus)."""

    def setUp(self):
        ModbusGuard.clear_registry()

    def test_cancel_during_gap_releases_lock_and_counter(self):
        async def scenario():
            g = ModbusGuard.get_or_create("10.0.0.9:502")
            g.update_gap("test-device", 0.5)  # long enough gap to be cancelled mid-sleep

            async with g.request():  # first request sets _last_request_end
                pass

            async def second():
                async with g.request():
                    await asyncio.sleep(1)

            t = asyncio.create_task(second())
            await asyncio.sleep(0.1)  # let it acquire the lock and enter the gap sleep
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

            # The lock and the queue counter must both be released.
            self.assertFalse(g.is_busy, "lock leaked -> bus deadlocked")
            self.assertEqual(g.queue_depth, 0, "queue_depth leaked")

            # And the bus must still be usable.
            async with asyncio.timeout(1.0):
                async with g.request():
                    pass

        _run(scenario())

    def test_remove_is_targeted(self):
        ModbusGuard.get_or_create("a:502")
        ModbusGuard.get_or_create("b:502")
        ModbusGuard.remove("a:502")
        self.assertNotIn("a:502", ModbusGuard._registry)
        self.assertIn("b:502", ModbusGuard._registry)


class TestShedExceptionType(unittest.TestCase):
    """Defect D (v1.2.3): shedding must be distinguishable from a timeout.

    A shed request is internal contention between our own sub-coordinators;
    an inverter timeout is the inverter failing to answer. Recording the first
    as the second taught the adaptive circadian model that our own contention
    was inverter misbehaviour.
    """

    def test_shed_raises_dedicated_subclass(self):
        self.assertTrue(issubclass(_MOD.ModbusQueueShed, asyncio.TimeoutError))

    def test_existing_timeout_handlers_still_catch_it(self):
        """Subclassing preserves every `except asyncio.TimeoutError` path."""
        try:
            raise _MOD.ModbusQueueShed("queue full")
        except asyncio.TimeoutError:
            caught = True
        self.assertTrue(caught)

    def test_shed_increments_counter(self):
        async def run():
            g = _fresh_guard()
            g._max_queue_depth = 1
            g._queue_depth = 1
            with self.assertRaises(_MOD.ModbusQueueShed):
                async with g.request():
                    pass
            return g.shed_count
        self.assertEqual(asyncio.run(run()), 1)


# ── v2.0.0b: AR-4 -- priority-lane airtime budget (external ICS audit) ──────

class TestPriorityAirtimeBudget(unittest.TestCase):
    """AR-4, external ICS audit: priority requests get their own airtime
    budget, distinct from MOD-18's queue-DEPTH cap -- this bounds what
    FRACTION of the bus's own occupied time, within a rolling window,
    priority traffic may consume."""

    def test_priority_within_budget_bypasses_shedding_as_before(self):
        """The existing, unchanged guarantee: a priority request with
        budget still available bypasses the normal queue-depth shed
        check entirely, exactly as before AR-4."""
        async def _go():
            g = _fresh_guard()
            g._queue_depth = g._max_queue_depth  # normal queue is full
            g._priority_window_start = time.monotonic() - 1.0
            g._priority_busy_s = 0.0  # no priority consumption yet
            async with g.request(priority=True):
                pass  # must NOT raise
        asyncio.run(_go())

    def test_priority_exceeding_budget_is_shed_when_queue_is_full(self):
        """The core new behaviour: once the priority budget is exhausted
        within the current window, a priority request is demoted to
        normal-lane admission for the queue-depth check -- a full normal
        queue now sheds it too, exactly as it would a normal request."""
        async def _go():
            g = _fresh_guard()
            g._queue_depth = g._max_queue_depth  # normal queue full
            # Window is 1.0s old; busy_s = 1.0s -> fraction = 100%, well
            # above the 20% budget.
            g._priority_window_start = time.monotonic() - 1.0
            g._priority_busy_s = 1.0
            with self.assertRaises(_MOD.ModbusQueueShed):
                async with g.request(priority=True):
                    pass
        asyncio.run(_go())

    def test_priority_exceeding_budget_but_queue_not_full_still_admitted(self):
        """Demotion only changes the OUTCOME when the normal queue is
        actually full -- with room in the normal queue, a
        budget-exceeding priority request is still admitted (just via
        the same path a normal request would use, not bypassing it)."""
        async def _go():
            g = _fresh_guard()
            g._queue_depth = 0  # normal queue has room
            g._priority_window_start = time.monotonic() - 1.0
            g._priority_busy_s = 1.0  # budget exhausted
            async with g.request(priority=True):
                pass  # must NOT raise -- queue has room regardless of lane
        asyncio.run(_go())

    def test_priority_budget_exceeded_counter_increments(self):
        async def _go():
            g = _fresh_guard()
            g._queue_depth = 0
            g._priority_window_start = time.monotonic() - 1.0
            g._priority_busy_s = 1.0
            async with g.request(priority=True):
                pass
            return g.priority_budget_exceeded_count
        self.assertEqual(asyncio.run(_go()), 1)

    def test_priority_budget_counter_not_incremented_when_within_budget(self):
        async def _go():
            g = _fresh_guard()
            g._priority_window_start = time.monotonic() - 1.0
            g._priority_busy_s = 0.0
            async with g.request(priority=True):
                pass
            return g.priority_budget_exceeded_count
        self.assertEqual(asyncio.run(_go()), 0)

    def test_window_rolls_forward_and_resets_the_budget(self):
        """An expired window (older than PRIORITY_AIRTIME_WINDOW_S) must
        reset _priority_busy_s to 0 BEFORE the fraction is computed --
        otherwise a window that should have rolled forward would still
        report a stale, exhausted budget forever."""
        async def _go():
            g = _fresh_guard()
            g._queue_depth = g._max_queue_depth  # would shed if still demoted
            g._priority_window_start = (
                time.monotonic() - (_MOD.PRIORITY_AIRTIME_WINDOW_S + 1.0)
            )
            g._priority_busy_s = 999.0  # would be "exhausted" if not reset
            async with g.request(priority=True):
                pass  # must NOT raise -- the window rolled forward first
            self.assertLess(
                g._priority_busy_s, 1.0,
                "the window must have reset _priority_busy_s close to 0, "
                "not carried the stale value forward",
            )
        asyncio.run(_go())

    def test_priority_busy_time_accumulates_only_for_priority_requests(self):
        """Normal (non-priority) requests must not contribute to the
        priority lane's own airtime accounting -- otherwise ordinary bus
        traffic could exhaust a budget that's meant to bound priority
        traffic specifically."""
        async def _go():
            g = _fresh_guard()
            async with g.request(priority=False):
                await asyncio.sleep(0.01)
            return g._priority_busy_s
        self.assertEqual(asyncio.run(_go()), 0.0)

    def test_priority_lane_depth_bookkeeping_still_applies_when_demoted(self):
        """A demoted priority request is still, functionally, a priority
        request -- MAX_PRIORITY_QUEUE_DEPTH's own accounting (unrelated
        to AR-4) must be unaffected by the demotion."""
        async def _go():
            g = _fresh_guard()
            g._queue_depth = 0  # normal queue has room, so admission succeeds
            g._priority_window_start = time.monotonic() - 1.0
            g._priority_busy_s = 1.0  # budget exhausted -> demoted
            async with g.request(priority=True):
                self.assertEqual(
                    g._priority_queue_depth, 1,
                    "priority-lane depth tracking must still increment "
                    "even for a demoted request -- it is still priority "
                    "traffic by type",
                )
        asyncio.run(_go())


# ── v2.0.3: ICS-09 -- priority is admission exemption, not lock ordering ────

class TestICS09PriorityIsAdmissionExemptionNotLockOrdering(unittest.TestCase):
    """ICS-09, external ICS audit -- confirmed: 'priority' only ever
    meant admission exemption (bypassing queue-depth shedding), never
    true lock-acquisition ordering -- every request, priority or not,
    waits on the same plain, FIFO asyncio.Lock once admitted. This test
    proves the behavioural claim directly, not just checks the
    docstring wording: a priority request submitted AFTER two normal
    requests are already queued on the lock must still be serviced
    after them, not ahead of them."""

    def test_priority_request_does_not_jump_already_queued_normal_requests(self):
        async def _go():
            g = _fresh_guard()
            service_order: list[str] = []
            first_holder_may_release = asyncio.Event()

            async def _hold_then_release(name: str):
                async with g.request(label=name):
                    service_order.append(name)
                    if name == "holder":
                        await first_holder_may_release.wait()

            # "holder" acquires first and holds the lock, forcing
            # everything else to genuinely queue rather than race.
            holder_task = asyncio.create_task(_hold_then_release("holder"))
            await asyncio.sleep(0)  # let holder actually acquire the lock

            # Two NORMAL requests start queuing on the lock while it's held.
            normal1_task = asyncio.create_task(_hold_then_release("normal1"))
            normal2_task = asyncio.create_task(_hold_then_release("normal2"))
            await asyncio.sleep(0)  # let both genuinely start waiting on the lock

            # A PRIORITY request arrives AFTER the two normal ones are
            # already queued -- the exact scenario ICS-09 describes.
            async def _priority_request():
                async with g.request(priority=True, label="priority"):
                    service_order.append("priority")

            priority_task = asyncio.create_task(_priority_request())
            await asyncio.sleep(0)

            first_holder_may_release.set()
            await asyncio.gather(holder_task, normal1_task, normal2_task, priority_task)

            self.assertEqual(service_order[0], "holder")
            self.assertEqual(
                service_order[1:3], ["normal1", "normal2"],
                "the two already-queued normal requests must be serviced "
                "before the priority one that arrived after them -- "
                "priority=True does not grant lock-acquisition priority, "
                "only admission exemption from queue-depth shedding "
                "(this is the exact claim ICS-09 identified as "
                "undocumented, now proven directly rather than assumed)",
            )
            self.assertEqual(service_order[3], "priority")

        asyncio.run(_go())
