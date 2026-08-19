"""Tests for Phase 0 instrumentation (v1.3.0).

Covers `bus_diagnostics.BusDiagnostics` and the ModbusGuard wait/service split.

The point of this instrumentation is one measurement: **wait time vs service
time**. Three days of field data showed both inverters' failure rates tracking
the master's workload (r = +0.94) but could not say why, because nothing
separated "queued behind another request" from "the device was slow". Those two
call for opposite fixes, so the split has to be trustworthy — hence the tests
below assert it is accounted correctly even on error paths.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import time
import types
import unittest
from collections import deque

_ROOT = pathlib.Path(__file__).parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"phase0_{name}", str(_ROOT / f"{name}.py")
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "phase0"
    sys.modules[f"phase0_{name}"] = module
    spec.loader.exec_module(module)
    return module


if "phase0" not in sys.modules:
    pkg = types.ModuleType("phase0")
    pkg.__path__ = []
    sys.modules["phase0"] = pkg

BD = _load("bus_diagnostics")


class _FakeHass:
    """Hass fake with genuine async semantics.

    v2.0.3 (ICS-08, external ICS audit -- confirmed, the same defect as
    TEL-005 already closed in test_telemetry_capture.py): the original
    version of this fake ran executor jobs INLINE, synchronously --
    structurally unable to exercise a pending executor job, a late
    failure, or a teardown-during-write race. async_add_executor_job is
    now genuinely awaitable (and independently controllable per test via
    executor_delay/executor_error), and async_create_task genuinely
    schedules a real asyncio.Task rather than running the coroutine to
    completion immediately.
    """

    def __init__(
        self, base: str, *,
        executor_delay: float = 0.0,
        executor_error: Exception | None = None,
    ):
        self.config = types.SimpleNamespace(path=lambda *p: os.path.join(base, *p))
        self.jobs = 0
        self.executor_delay = executor_delay
        self.executor_error = executor_error

    async def async_add_executor_job(self, fn, *args):
        self.jobs += 1
        if self.executor_delay:
            await asyncio.sleep(self.executor_delay)
        if self.executor_error is not None:
            raise self.executor_error
        return fn(*args)

    def async_create_task(self, coro):
        return asyncio.ensure_future(coro)


class TestCaptureDisabledByDefault(unittest.TestCase):
    def setUp(self):
        BD.BusDiagnostics.clear_registry()
        self.dir = tempfile.mkdtemp()

    def test_disabled_by_default(self):
        d = BD.BusDiagnostics(_FakeHass(self.dir), "192.0.2.1:502")
        self.assertFalse(d.enabled)

    def test_record_is_noop_when_disabled(self):
        hass = _FakeHass(self.dir)
        d = BD.BusDiagnostics(hass, "192.0.2.1:502")
        for _ in range(50):
            d.record(endpoint="e", label="c", wait_ms=1, service_ms=2,
                     queue_depth=0, outcome="ok")
        self.assertEqual(d.records_captured, 0)
        self.assertEqual(hass.jobs, 0)
        self.assertEqual(len(os.listdir(self.dir)), 0)

    def test_enabling_is_explicit_and_not_restored(self):
        """A capture must never be silently left running across a restart."""
        d = BD.BusDiagnostics(_FakeHass(self.dir), "192.0.2.1:502")
        d.set_enabled(True)
        self.assertTrue(d.enabled)
        fresh = BD.BusDiagnostics(_FakeHass(self.dir), "192.0.2.1:502")
        self.assertFalse(fresh.enabled)


class TestFlushRateLimit(unittest.IsolatedAsyncioTestCase):
    """The first flush must never be rate-limited.

    REGRESSION: `_last_flush` was initialised to 0.0, which the rate-limit
    check read as "flushed at monotonic time 0". On a host whose
    time.monotonic() was still below MIN_FLUSH_INTERVAL_S — a freshly booted
    machine or container — the FIRST flush was suppressed and records sat in
    the buffer. It showed up as an intermittent test failure, which is exactly
    how it would have behaved in the field: silently, and only sometimes.
    """

    def setUp(self):
        BD.BusDiagnostics.clear_registry()
        self.dir = tempfile.mkdtemp()

    def test_last_flush_starts_as_none(self):
        d = BD.BusDiagnostics(_FakeHass(self.dir), "e")
        self.assertIsNone(d._last_flush)

    async def test_first_flush_happens_even_at_low_monotonic(self):
        hass = _FakeHass(self.dir)
        d = BD.BusDiagnostics(hass, "e")
        d.set_enabled(True)
        for _ in range(BD.FLUSH_THRESHOLD):
            d.record(endpoint="e", label="c", wait_ms=1, service_ms=2,
                     queue_depth=0, outcome="ok")
        self.assertIsNotNone(d._pending_write)
        await d._pending_write
        self.assertGreaterEqual(hass.jobs, 1, "first flush must not be suppressed")

    async def test_second_flush_is_rate_limited(self):
        hass = _FakeHass(self.dir)
        d = BD.BusDiagnostics(hass, "e")
        d.set_enabled(True)
        for _ in range(BD.FLUSH_THRESHOLD):
            d.record(endpoint="e", label="c", wait_ms=1, service_ms=2,
                     queue_depth=0, outcome="ok")
        await d._pending_write
        for _ in range(BD.FLUSH_THRESHOLD):
            d.record(endpoint="e", label="c", wait_ms=1, service_ms=2,
                     queue_depth=0, outcome="ok")
        self.assertEqual(hass.jobs, 1, "a burst must not cause continuous I/O")


class TestCaptureBounded(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        BD.BusDiagnostics.clear_registry()
        self.dir = tempfile.mkdtemp()

    def test_buffer_is_bounded_and_drops_are_counted(self):
        """Memory must be bounded; silent loss would be worse than a gap."""
        hass = _FakeHass(self.dir)
        d = BD.BusDiagnostics(hass, "e")
        d.enabled = True
        d._last_flush = time.monotonic() + 1e6      # suppress flushing
        for _ in range(BD.MAX_BUFFERED_RECORDS + 120):
            d.record(endpoint="e", label="c", wait_ms=1, service_ms=2,
                     queue_depth=0, outcome="ok")
        self.assertLessEqual(len(d._buffer), BD.MAX_BUFFERED_RECORDS)
        self.assertGreater(d.records_dropped, 0)

    async def test_flush_writes_jsonl(self):
        hass = _FakeHass(self.dir)
        d = BD.BusDiagnostics(hass, "e")
        d.set_enabled(True)
        for _ in range(BD.FLUSH_THRESHOLD):
            d.record(endpoint="e", label="battery_coord", wait_ms=12.3,
                     service_ms=45.6, queue_depth=2, outcome="ok", registers=5)
        await d._pending_write
        path = os.path.join(self.dir, BD._SUBDIR, f"bus_{d.tag}.jsonl")
        self.assertTrue(os.path.exists(path))
        lines = [json.loads(x) for x in open(path).read().splitlines()]
        self.assertEqual(len(lines), BD.FLUSH_THRESHOLD)
        self.assertEqual(lines[0]["wait_ms"], 12.3)
        self.assertEqual(lines[0]["service_ms"], 45.6)
        self.assertEqual(lines[0]["regs"], 5)

    async def test_write_failure_does_not_raise(self):
        """Diagnostics faults must cost diagnostics and nothing else."""
        hass = _FakeHass(self.dir, executor_error=OSError("disk gone"))
        d = BD.BusDiagnostics(hass, "e")
        d.set_enabled(True)
        for _ in range(BD.FLUSH_THRESHOLD):
            d.record(endpoint="e", label="c", wait_ms=1, service_ms=2,
                     queue_depth=0, outcome="ok")
        await d._pending_write  # must not raise
        self.assertGreater(d.write_errors, 0)


class TestConfidentiality(unittest.IsolatedAsyncioTestCase):
    def test_endpoint_is_pseudonymised(self):
        """A capture must be shareable without exposing the installation."""
        endpoint = "192.168.1.55:502"
        tag = BD.pseudonym(endpoint)
        self.assertNotIn("192", tag)
        self.assertNotIn("502", tag)
        self.assertEqual(len(tag), 8)
        self.assertEqual(tag, BD.pseudonym(endpoint))          # stable
        self.assertNotEqual(tag, BD.pseudonym("192.168.1.56:502"))

    def test_serial_in_coordinator_name_is_stripped(self):
        """REGRESSION — the leak found in the first real field capture.

        Coordinator names are built as
        f"{device.serial_number}_..._update_coordinator", so passing
        coordinator.name straight through wrote real serials into every
        record. The original test only checked the ENDPOINT, so the leak
        shipped despite the audit claiming no serials were present.
        """
        leaked = "HV9990001111_battery_data_update_coordinator"
        clean = BD.sanitise_label(leaked)
        self.assertNotIn("HV9990001111", clean)
        self.assertIn("battery_data_update_coordinator", clean,
                      "the useful part of the label must survive")

    def test_sanitised_labels_are_stable_and_distinguishing(self):
        """Two inverters must stay distinguishable after pseudonymisation."""
        a = BD.sanitise_label("HV9990001111_data_update_coordinator")
        b = BD.sanitise_label("HV9990002222_data_update_coordinator")
        self.assertNotEqual(a, b)
        self.assertEqual(a, BD.sanitise_label("HV9990001111_data_update_coordinator"))

    def test_labels_without_serials_are_untouched(self):
        for label in ("power_meter_data_update_coordinator",
                      "config_data_update_coordinator"):
            self.assertEqual(BD.sanitise_label(label), label)

    def test_serial_survives_no_word_boundary(self):
        """The first sanitiser used \\b and silently matched nothing.

        "_" is a word character, so \\b never fires between the digits and the
        underscore. Pinned so the anchoring cannot regress.
        """
        self.assertNotIn("HV9990001111", BD.sanitise_label("HV9990001111_x"))

    async def test_written_records_contain_no_serial(self):
        dirpath = tempfile.mkdtemp()
        hass = _FakeHass(dirpath)
        d = BD.BusDiagnostics(hass, "192.168.1.55:502")
        d.set_enabled(True)
        for _ in range(BD.FLUSH_THRESHOLD):
            d.record(endpoint="192.168.1.55:502",
                     label="HV9990001111_battery_data_update_coordinator",
                     wait_ms=1, service_ms=2, queue_depth=0, outcome="ok")
        await d._pending_write
        blob = open(os.path.join(dirpath, BD._SUBDIR, f"bus_{d.tag}.jsonl")).read()
        self.assertNotIn("HV9990001111", blob)

    async def test_records_contain_no_endpoint_or_serial(self):
        dirpath = tempfile.mkdtemp()
        hass = _FakeHass(dirpath)
        d = BD.BusDiagnostics(hass, "192.168.1.55:502")
        d.set_enabled(True)
        for _ in range(BD.FLUSH_THRESHOLD):
            d.record(endpoint="192.168.1.55:502", label="inverter_data",
                     wait_ms=1, service_ms=2, queue_depth=0, outcome="ok")
        await d._pending_write
        blob = open(os.path.join(dirpath, BD._SUBDIR, f"bus_{d.tag}.jsonl")).read()
        self.assertNotIn("192.168.1.55", blob)
        self.assertNotIn("502", blob.replace('"service_ms":2', ''))


# ── Guard wait/service split ─────────────────────────────────────────────────
MG = _load("modbus_guard")


def _fresh_guard(endpoint="e"):
    g = object.__new__(MG.ModbusGuard)
    g.endpoint = endpoint
    g._lock = asyncio.Lock()
    g._last_request_end = 0.0
    g._queue_depth = 0
    g._effective_gap = 0.0
    g._max_queue_depth = 3
    g.shed_count = 0
    g._busy_s = 0.0
    g._window_start = time.monotonic()
    g._wait_samples = deque(maxlen=256)
    g._service_samples = deque(maxlen=256)
    g.diagnostics = None
    g.total_wait_ms = 0.0
    g.requests_waited = 0
    # v2.0.0a (F18) / v2.0.0b (AR-4, external ICS audit): object.__new__()
    # bypasses __init__ entirely, so these need setting explicitly,
    # matching __init__'s own defaults -- the same class of gap hit
    # repeatedly this session for every object.__new__()-based test
    # fixture, including this file's own separate copy of _fresh_guard().
    g._priority_queue_depth = 0
    g.priority_shed_count = 0
    g._priority_window_start = time.monotonic()
    g._priority_busy_s = 0.0
    g.priority_budget_exceeded_count = 0
    # v2.0.11 (Phase 5.2, this release): same class of gap, this file's
    # own separate copy.
    g._bus_admission_ewma_n = 0.0
    g._bus_admission_ewma_failures = 0.0
    g.admission_timeout_count = 0
    return g


class TestWaitServiceSplit(unittest.TestCase):
    """THE Phase 0 measurement — must attribute time to the right bucket."""

    def test_service_time_measured_not_wait(self):
        async def run():
            g = _fresh_guard()
            async with g.request(label="x"):
                await asyncio.sleep(0.05)          # device is slow
            return g.wait_service_split()
        wait, service = asyncio.run(run())
        self.assertGreater(service, 40)
        self.assertLess(wait, 20, "an uncontended request must not show wait time")

    def test_wait_time_measured_under_contention(self):
        async def run():
            g = _fresh_guard()
            async def holder():
                async with g.request(label="holder"):
                    await asyncio.sleep(0.08)
            async def waiter():
                await asyncio.sleep(0.01)
                async with g.request(label="waiter"):
                    pass
            await asyncio.gather(holder(), waiter())
            return g.wait_service_split()
        wait, service = asyncio.run(run())
        self.assertGreater(wait, 40, "a queued request must record wait time")

    def test_occupancy_counts_only_held_time(self):
        async def run():
            g = _fresh_guard()
            async with g.request():
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.05)              # idle: must not count
            return g.occupancy()
        occ = asyncio.run(run())
        self.assertGreater(occ, 0.2)
        self.assertLess(occ, 0.8)

    def test_accounting_survives_an_exception_inside_the_context(self):
        async def run():
            g = _fresh_guard()
            try:
                async with g.request():
                    await asyncio.sleep(0.02)
                    raise RuntimeError("device blew up")
            except RuntimeError:
                pass
            return g, g.wait_service_split()
        g, (wait, service) = asyncio.run(run())
        self.assertGreater(service, 10, "service time must still be attributed")
        self.assertEqual(g.queue_depth, 0)
        self.assertFalse(g._lock.locked())

    def test_register_count_and_tier_are_recorded(self):
        """The second Phase 0 defect: these fields existed but were never set.

        Without them a stall cannot be correlated with what was being read,
        which is exactly the next question after the wait/service split.
        """
        captured = []

        class Sink:
            enabled = True
            def record(self, **kw):
                captured.append(kw)

        async def run():
            g = _fresh_guard()
            g.diagnostics = Sink()
            async with g.request(label="c") as req:
                req.registers = 7
                req.priority_tier = "FAST"
        asyncio.run(run())
        self.assertEqual(captured[0]["registers"], 7)
        self.assertEqual(captured[0]["priority"], "FAST")

    def test_request_context_exposes_detail_fields(self):
        async def run():
            g = _fresh_guard()
            async with g.request(label="c") as req:
                return req.registers, req.priority_tier
        self.assertEqual(asyncio.run(run()), (None, None))

    def test_diagnostics_sink_receives_records(self):
        captured = []

        class Sink:
            enabled = True
            def record(self, **kw):
                captured.append(kw)

        async def run():
            g = _fresh_guard()
            g.diagnostics = Sink()
            async with g.request(label="battery_coord"):
                await asyncio.sleep(0.01)
        asyncio.run(run())
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["label"], "battery_coord")
        self.assertEqual(captured[0]["outcome"], "ok")
        self.assertGreater(captured[0]["service_ms"], 5)

    def test_failing_sink_never_breaks_modbus_io(self):
        class BadSink:
            enabled = True
            def record(self, **kw):
                raise RuntimeError("sink exploded")

        async def run():
            g = _fresh_guard()
            g.diagnostics = BadSink()
            async with g.request():      # must not raise
                pass
            return g.queue_depth, g._lock.locked()
        depth, locked = asyncio.run(run())
        self.assertEqual(depth, 0)
        self.assertFalse(locked)


class TestPhase21AttributionFields(unittest.IsolatedAsyncioTestCase):
    """v2.0.9 (Phase 2.1/2.4, this release -- ICS-16, both external ICS
    audits): chunk_index/chunk_count/retry_count/logical_request_id/
    transition_reason -- the fields needed to attribute an extreme event
    (e.g. a 102s/8-chunk batch) to a specific poll, chunk, and retry
    sequence, previously entirely absent from every captured record."""

    def setUp(self):
        BD.BusDiagnostics.clear_registry()
        self.dir = tempfile.mkdtemp()

    async def test_new_fields_are_captured_and_serialised(self):
        hass = _FakeHass(self.dir)
        d = BD.BusDiagnostics(hass, "192.0.2.1:502")
        d.enabled = True
        d.record(
            endpoint="e", label="main_coord", wait_ms=1.0, service_ms=2.0,
            queue_depth=1, outcome="ok", registers=40, priority="normal",
            chunk_index=3, chunk_count=8, retry_count=1,
            logical_request_id=42, transition_reason="night→day",
        )
        seq, record = d._buffer[-1]
        self.assertEqual(record["chunk_idx"], 3)
        self.assertEqual(record["chunk_n"], 8)
        self.assertEqual(record["retries"], 1)
        self.assertEqual(record["req_id"], 42)
        self.assertEqual(record["transition"], "night→day")

    async def test_new_fields_default_to_none_for_callers_not_passing_them(self):
        """Negative case: any caller not yet updated (there should be
        none after this release, but this confirms the extension is
        additive, not breaking) must not raise and must record None."""
        hass = _FakeHass(self.dir)
        d = BD.BusDiagnostics(hass, "192.0.2.1:502")
        d.enabled = True
        d.record(endpoint="e", label="c", wait_ms=1, service_ms=2,
                 queue_depth=0, outcome="ok")  # old-style call, no new kwargs
        seq, record = d._buffer[-1]
        self.assertIsNone(record["chunk_idx"])
        self.assertIsNone(record["req_id"])
        self.assertIsNone(record["transition"])

    async def test_adversarial_retries_of_same_chunk_share_one_logical_id(self):
        """The core scenario this closes: multiple physical attempts
        (BUSY retries) of the SAME chunk must share one logical_request_
        id, distinguishing them from a different chunk's own attempts --
        this is what lets a captured file group '3 rows, all belonging
        to chunk 4 of poll 42' rather than 3 anonymous data points."""
        hass = _FakeHass(self.dir)
        d = BD.BusDiagnostics(hass, "192.0.2.1:502")
        d.enabled = True
        # Two retries then a success, all logically the same chunk attempt.
        for retry in (0, 1, 2):
            d.record(
                endpoint="e", label="main_coord", wait_ms=1.0, service_ms=2.0,
                queue_depth=1, outcome=("ok" if retry == 2 else "error"),
                chunk_index=4, chunk_count=8, retry_count=retry,
                logical_request_id=42,
            )
        ids = [rec["req_id"] for _, rec in list(d._buffer)[-3:]]
        self.assertEqual(ids, [42, 42, 42])
        retries = [rec["retries"] for _, rec in list(d._buffer)[-3:]]
        self.assertEqual(retries, [0, 1, 2])


class TestPhase48RefCountedEndpointLifecycle(unittest.TestCase):
    """Phase 4.8, this release -- old DEF-011, external ICS quality/
    defect/architecture audit: BusDiagnostics/TelemetryCapture's own
    remove() was unconditional, ignoring reference count -- unlike
    ModbusGuard's own established acquire_endpoint()/release_endpoint()
    pair. One entry unloading could remove a still-in-use instance out
    from under another entry sharing the same physical endpoint."""

    def setUp(self):
        BD.BusDiagnostics.clear_registry()
        self.dir = tempfile.mkdtemp()

    def test_acquire_creates_and_increments(self):
        hass = _FakeHass(self.dir)
        inst = BD.BusDiagnostics.acquire_endpoint(hass, "192.0.2.1:502")
        self.assertIsNotNone(inst)
        self.assertEqual(BD.BusDiagnostics._ref_counts["192.0.2.1:502"], 1)

    def test_second_acquire_by_a_different_entry_increments_again(self):
        hass = _FakeHass(self.dir)
        first = BD.BusDiagnostics.acquire_endpoint(hass, "192.0.2.1:502")
        second = BD.BusDiagnostics.acquire_endpoint(hass, "192.0.2.1:502")
        self.assertIs(
            first, second,
            "the SAME instance must be returned -- one instance per "
            "endpoint, shared across every entry using it",
        )
        self.assertEqual(BD.BusDiagnostics._ref_counts["192.0.2.1:502"], 2)

    def test_adversarial_release_by_one_entry_does_not_remove_a_still_used_instance(self):
        """The core scenario this closes: two entries share one
        physical endpoint. Entry A unloads (releases); Entry B is still
        loaded and must keep working -- its own BusDiagnostics instance
        must NOT have been removed out from under it."""
        hass = _FakeHass(self.dir)
        inst = BD.BusDiagnostics.acquire_endpoint(hass, "192.0.2.1:502")
        BD.BusDiagnostics.acquire_endpoint(hass, "192.0.2.1:502")  # Entry B also acquires

        BD.BusDiagnostics.release_endpoint("192.0.2.1:502")  # Entry A unloads

        self.assertIs(
            BD.BusDiagnostics.get("192.0.2.1:502"), inst,
            "the instance must still be registered -- Entry B's own "
            "reference is still outstanding",
        )
        self.assertEqual(BD.BusDiagnostics._ref_counts["192.0.2.1:502"], 1)

    def test_instance_removed_only_once_every_entry_has_released(self):
        hass = _FakeHass(self.dir)
        BD.BusDiagnostics.acquire_endpoint(hass, "192.0.2.1:502")
        BD.BusDiagnostics.acquire_endpoint(hass, "192.0.2.1:502")

        BD.BusDiagnostics.release_endpoint("192.0.2.1:502")
        self.assertIsNotNone(BD.BusDiagnostics.get("192.0.2.1:502"))

        BD.BusDiagnostics.release_endpoint("192.0.2.1:502")
        self.assertIsNone(BD.BusDiagnostics.get("192.0.2.1:502"))
        self.assertNotIn("192.0.2.1:502", BD.BusDiagnostics._ref_counts)

    def test_release_with_no_matching_acquire_is_a_safe_noop(self):
        """Matches ModbusGuard's own established reasoning: teardown
        code that runs on best-effort/exception paths shouldn't need
        its own separate bookkeeping to avoid an error here."""
        BD.BusDiagnostics.release_endpoint("192.0.2.1:502")  # never acquired
        self.assertIsNone(BD.BusDiagnostics.get("192.0.2.1:502"))

    def test_deprecated_remove_still_works_but_is_unconditional(self):
        """Negative case: remove() is kept for backward compatibility,
        but confirmed to still have the OLD, unconditional behaviour --
        it's documented as deprecated, not silently made safe."""
        hass = _FakeHass(self.dir)
        BD.BusDiagnostics.acquire_endpoint(hass, "192.0.2.1:502")
        BD.BusDiagnostics.acquire_endpoint(hass, "192.0.2.1:502")
        BD.BusDiagnostics.remove("192.0.2.1:502")
        self.assertIsNone(
            BD.BusDiagnostics.get("192.0.2.1:502"),
            "remove() must still remove unconditionally, regardless of "
            "ref count -- that's exactly why it's deprecated in favour "
            "of release_endpoint()",
        )

    def test_get_or_create_does_not_affect_ref_count(self):
        """Negative case: plain accessor usage (by coordinators, not
        entry-level setup) must not itself bump the reference count --
        only acquire_endpoint() does."""
        hass = _FakeHass(self.dir)
        BD.BusDiagnostics.get_or_create(hass, "192.0.2.1:502")
        BD.BusDiagnostics.get_or_create(hass, "192.0.2.1:502")
        self.assertNotIn("192.0.2.1:502", BD.BusDiagnostics._ref_counts)



    """Confirms _RequestContext itself (modbus_guard.py) carries the new
    fields through to BusDiagnostics.record(), not just that record()
    accepts them in isolation."""

    def test_request_context_new_fields_default_to_none(self):
        async def _go():
            g = MG.ModbusGuard("e")
            async with g.request(label="test") as req:
                self.assertIsNone(req.chunk_index)
                self.assertIsNone(req.chunk_count)
                self.assertIsNone(req.retry_count)
                self.assertIsNone(req.logical_request_id)
                self.assertIsNone(req.transition_reason)
        asyncio.run(_go())

    def test_request_context_fields_reach_the_diagnostics_sink(self):
        async def _go():
            g = MG.ModbusGuard("e")
            sink = BD.BusDiagnostics(_FakeHass(tempfile.mkdtemp()), "e")
            sink.enabled = True
            g.diagnostics = sink
            async with g.request(label="test") as req:
                req.chunk_index = 2
                req.chunk_count = 5
                req.retry_count = 1
                req.logical_request_id = 99
                req.transition_reason = "0x06 SLAVE_DEVICE_BUSY"
            seq, record = sink._buffer[-1]
            self.assertEqual(record["chunk_idx"], 2)
            self.assertEqual(record["chunk_n"], 5)
            self.assertEqual(record["retries"], 1)
            self.assertEqual(record["req_id"], 99)
            self.assertEqual(record["transition"], "0x06 SLAVE_DEVICE_BUSY")
        asyncio.run(_go())


if __name__ == "__main__":
    unittest.main()

