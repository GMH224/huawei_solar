"""Tests for telemetry_capture.py.

Covers `TelemetryCapture` (the periodic aggregate-snapshot capture
switch's back end, mirroring bus_diagnostics.py's own established,
field-tested per-request capture) and `check_register_overlap()` (the
one-time structural check for the Physical Demand Planner's
cross-coordinator-merging justification).
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

_ROOT = pathlib.Path(__file__).parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"telcap_{name}", str(_ROOT / f"{name}.py")
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "telcap"
    sys.modules[f"telcap_{name}"] = module
    spec.loader.exec_module(module)
    return module


if "telcap" not in sys.modules:
    pkg = types.ModuleType("telcap")
    pkg.__path__ = []
    sys.modules["telcap"] = pkg

# telemetry_capture.py does `from .bus_diagnostics import pseudonym` --
# load bus_diagnostics.py first, under the same "telcap" package
# namespace, so that relative import resolves.
_BD_FOR_TC = _load("bus_diagnostics")
sys.modules["telcap.bus_diagnostics"] = _BD_FOR_TC

TC = _load("telemetry_capture")


class _FakeHass:
    """Hass fake with genuine async semantics.

    v2.0.2 (TEL-005, external ICS/IQS audit -- confirmed): the original
    version of this fake ran executor jobs INLINE, synchronously --
    meaning a write appeared complete before _schedule_flush() even
    returned, which made it structurally impossible for any test using
    it to exercise a pending executor job, a late failure, or a
    teardown-during-write race. async_add_executor_job is now genuinely
    awaitable (and independently controllable per test via
    executor_delay/executor_error), and async_create_task genuinely
    schedules a real asyncio.Task rather than running the coroutine to
    completion immediately.
    """

    def __init__(
        self, base: str, *,
        executor_delay: float = 0.0,
        executor_error: Exception | None = None,
        fail_times: int = 0,
    ):
        self.config = types.SimpleNamespace(path=lambda *p: os.path.join(base, *p))
        self.jobs = 0
        self.executor_delay = executor_delay
        self.executor_error = executor_error
        # v2.0.2 (TEL-007): fail_times lets a test control EXACTLY how
        # many consecutive attempts fail before succeeding -- 1 to prove
        # a transient failure is survived, >= MAX_RETRY_ATTEMPTS to prove
        # a permanent one is eventually, explicitly given up on.
        # executor_error alone (unconditional) cannot express "fails
        # twice then recovers".
        self.fail_times = fail_times
        self._attempts = 0

    async def async_add_executor_job(self, fn, *args):
        self.jobs += 1
        if self.executor_delay:
            await asyncio.sleep(self.executor_delay)
        if self.executor_error is not None:
            raise self.executor_error
        self._attempts += 1
        if self._attempts <= self.fail_times:
            raise OSError(f"simulated transient failure (attempt {self._attempts})")
        return fn(*args)

    def async_create_task(self, coro):
        return asyncio.ensure_future(coro)


class TestCaptureDisabledByDefault(unittest.TestCase):
    def setUp(self):
        TC.TelemetryCapture.clear_registry()
        self.dir = tempfile.mkdtemp()

    def test_starts_disabled(self):
        d = TC.TelemetryCapture(_FakeHass(self.dir), "192.0.2.1:502")
        self.assertFalse(d.enabled)

    def test_record_snapshot_is_a_no_op_when_disabled(self):
        d = TC.TelemetryCapture(_FakeHass(self.dir), "192.0.2.1:502")
        d.record_snapshot({"x": 1})
        self.assertEqual(d.snapshots_captured, 0)

    def test_get_or_create_returns_the_same_instance_per_endpoint(self):
        hass = _FakeHass(self.dir)
        d = TC.TelemetryCapture.get_or_create(hass, "192.0.2.1:502")
        fresh = TC.TelemetryCapture.get_or_create(hass, "192.0.2.1:502")
        self.assertIs(d, fresh)


class TestSnapshotCaptureAndFlush(unittest.IsolatedAsyncioTestCase):
    """v2.0.2: converted to IsolatedAsyncioTestCase -- _schedule_flush()
    now genuinely schedules an asyncio.Task (via _FakeHass.
    async_create_task(), TEL-005's own fix), not something that runs to
    completion inline, so tests that need a flush to have actually
    happened must let the event loop run it."""

    def setUp(self):
        TC.TelemetryCapture.clear_registry()
        self.dir = tempfile.mkdtemp()

    def test_buffer_is_bounded_and_drops_are_counted(self):
        hass = _FakeHass(self.dir)
        d = TC.TelemetryCapture(hass, "e")
        d.enabled = True
        d._last_flush = time.monotonic() + 1e6  # suppress flushing
        for i in range(TC.MAX_BUFFERED_SNAPSHOTS + 20):
            d.record_snapshot({"i": i})
        self.assertLessEqual(len(d._buffer), TC.MAX_BUFFERED_SNAPSHOTS)
        self.assertGreater(d.snapshots_dropped, 0)

    async def test_flush_writes_jsonl_to_the_correct_distinct_filename(self):
        """Same directory, different filename from bus_diagnostics.py's
        own bus_<tag>.jsonl -- checked directly, not assumed.

        v2.0.2 (TEL-006): the very first snapshot now forces its own
        immediate flush (writing just that one record) rather than
        waiting for FLUSH_THRESHOLD -- checked explicitly here as its
        own step, not just assumed alongside the rest of this test.
        """
        hass = _FakeHass(self.dir)
        d = TC.TelemetryCapture(hass, "e")
        d.set_enabled(True)
        d.record_snapshot({"bus_occupancy_pct": 12.3, "total_cache_hits": 40})
        self.assertIsNotNone(d._pending_write)
        await d._pending_write

        path = os.path.join(self.dir, TC._SUBDIR, f"telemetry_{d.tag}.jsonl")
        self.assertTrue(os.path.exists(path))
        lines = [json.loads(x) for x in open(path).read().splitlines()]
        self.assertEqual(
            len(lines), 1,
            "the first snapshot's own forced flush must write exactly "
            "that one record immediately",
        )
        self.assertEqual(lines[0]["bus_occupancy_pct"], 12.3)
        self.assertEqual(lines[0]["total_cache_hits"], 40)
        self.assertIn("t", lines[0])
        self.assertIn("bus", lines[0])

        # The remaining snapshots up to FLUSH_THRESHOLD accumulate
        # normally (no LONGER force-flushed one at a time) and are
        # written on the next genuine threshold/forced flush.
        for _ in range(TC.FLUSH_THRESHOLD - 1):
            d.record_snapshot({"bus_occupancy_pct": 12.3, "total_cache_hits": 40})
        await d.async_disable()
        lines = [json.loads(x) for x in open(path).read().splitlines()]
        self.assertEqual(len(lines), TC.FLUSH_THRESHOLD)

    def test_telemetry_and_diagnostics_share_the_same_tag_for_one_endpoint(self):
        """The two files for one physical bus must be identifiable as
        belonging together -- same salted pseudonym, not two independent
        ones."""
        endpoint = "192.0.2.1:502"
        telemetry = TC.TelemetryCapture(_FakeHass(self.dir), endpoint)
        diagnostics = _BD_FOR_TC.BusDiagnostics(_FakeHass(self.dir), endpoint)
        self.assertEqual(telemetry.tag, diagnostics.tag)

    async def test_write_failure_does_not_raise(self):
        hass = _FakeHass(self.dir, executor_error=OSError("disk gone"))
        d = TC.TelemetryCapture(hass, "e")
        d.set_enabled(True)
        for _ in range(TC.FLUSH_THRESHOLD):
            d.record_snapshot({"x": 1})  # must not raise
        await d._pending_write  # the failure is caught inside _flush_batch
        self.assertGreater(d.write_errors, 0)

    async def test_disable_flushes_pending_snapshots(self):
        hass = _FakeHass(self.dir)
        d = TC.TelemetryCapture(hass, "e")
        d.set_enabled(True)
        d.record_snapshot({"x": 1})  # below FLUSH_THRESHOLD, stays buffered
        self.assertEqual(hass.jobs, 0)
        await d.async_disable()
        self.assertEqual(hass.jobs, 1, "disabling must flush whatever is pending")

    async def test_disable_cancels_the_periodic_timer_if_one_is_set(self):
        """The periodic timer itself is owned by the caller (switch.py),
        not this module -- but disabling capture must still cancel it via
        the stored callback, so a disabled switch genuinely stops
        producing snapshots, not just stops writing already-produced
        ones."""
        d = TC.TelemetryCapture(_FakeHass(self.dir), "e")
        cancelled = []
        d.set_enabled(True)
        d.cancel_periodic = lambda: cancelled.append(True)
        await d.async_disable()
        self.assertEqual(cancelled, [True])
        self.assertIsNone(d.cancel_periodic)


class TestRegisterOverlapCheck(unittest.TestCase):
    """check_register_overlap() -- the structural check for the Physical
    Demand Planner's cross-coordinator-merging justification."""

    def _coord(self, data):
        return types.SimpleNamespace(data=data)

    def test_no_overlap_when_register_sets_are_disjoint(self):
        result = TC.check_register_overlap({
            "main": self._coord({"input_power": 1, "device_status": 2}),
            "power_meter": self._coord({"power_meter_active_power": 3}),
            "energy_storage": self._coord({"storage_state_of_capacity": 4}),
        })
        self.assertFalse(result["any_overlap_found"])
        self.assertEqual(result["overlaps"], {})
        self.assertEqual(sorted(result["checked"]), ["energy_storage", "main", "power_meter"])

    def test_detects_a_genuine_overlap_when_present(self):
        result = TC.check_register_overlap({
            "main": self._coord({"input_power": 1, "shared_register": 2}),
            "power_meter": self._coord({"shared_register": 2}),
        })
        self.assertTrue(result["any_overlap_found"])
        self.assertIn("main_vs_power_meter", result["overlaps"])
        self.assertEqual(result["overlaps"]["main_vs_power_meter"], ["shared_register"])

    def test_coordinator_with_no_data_yet_is_skipped_not_falsely_clean(self):
        """A coordinator that hasn't completed its first poll yet must be
        excluded from the comparison, not silently treated as
        'confirmed disjoint' -- that would be a false negative, not
        evidence."""
        result = TC.check_register_overlap({
            "main": self._coord({"input_power": 1}),
            "power_meter": self._coord({}),  # never polled yet
            "energy_storage": self._coord(None),  # data attribute is None
        })
        self.assertIn("power_meter", result["skipped_not_yet_polled"])
        self.assertIn("energy_storage", result["skipped_not_yet_polled"])
        self.assertEqual(result["checked"], ["main"])
        self.assertEqual(result["overlaps"], {})

    def test_register_counts_are_reported_per_coordinator(self):
        result = TC.check_register_overlap({
            "main": self._coord({"a": 1, "b": 2, "c": 3}),
        })
        self.assertEqual(result["register_counts"]["main"], 3)

    def test_empty_input_does_not_raise(self):
        result = TC.check_register_overlap({})
        self.assertEqual(result["checked"], [])
        self.assertFalse(result["any_overlap_found"])


class TestTEL001DataNotLostOnFailure(unittest.IsolatedAsyncioTestCase):
    """TEL-001, external ICS/IQS audit -- confirmed: a batch used to be
    removed from the buffer before persistence success was known -- a
    scheduling or write failure silently converted captured evidence
    into an unreported data gap."""

    def setUp(self):
        TC.TelemetryCapture.clear_registry()
        self.dir = tempfile.mkdtemp()

    async def test_batch_survives_a_single_write_failure(self):
        """The core claim: a failed write must not discard the batch --
        it must still be retrievable (and eventually written) once the
        underlying fault clears.

        Note: TEL-010's own fix means a completed flush automatically
        re-schedules itself if a retry is pending -- so the retry here
        happens automatically, within the same await, not via a second
        manual call. The test waits for that automatic chain to settle
        (self._pending_write becoming None) rather than asserting on an
        intermediate state that TEL-010 itself is specifically designed
        to move past quickly.
        """
        hass = _FakeHass(self.dir, fail_times=1)
        d = TC.TelemetryCapture(hass, "e")
        d.set_enabled(True)
        d.record_snapshot({"x": 1})  # forces an immediate flush (TEL-006)
        while d._pending_write is not None:
            await d._pending_write
        self.assertIsNone(d._retry_batch, "a successful retry must clear the retained batch")
        self.assertEqual(d.snapshots_lost_write_failure, 0)
        path = os.path.join(self.dir, TC._SUBDIR, f"telemetry_{d.tag}.jsonl")
        lines = [json.loads(x) for x in open(path).read().splitlines()]
        self.assertEqual(len(lines), 1, "the originally-failed snapshot must still be written")


class TestTEL007BoundedRetry(unittest.IsolatedAsyncioTestCase):
    """TEL-007, external ICS/IQS audit -- confirmed: no retry/recovery
    existed after a write failure -- combined with TEL-001, this meant a
    single transient storage error caused permanent, silent data loss.
    Fixed with BOUNDED retry -- also confirmed here: a permanently
    broken write path must eventually, explicitly give up, not retain an
    ever-growing unwritable batch forever."""

    def setUp(self):
        TC.TelemetryCapture.clear_registry()
        self.dir = tempfile.mkdtemp()

    async def test_permanently_failing_write_is_eventually_given_up_on(self):
        hass = _FakeHass(self.dir, fail_times=999)  # never succeeds
        d = TC.TelemetryCapture(hass, "e")
        d.set_enabled(True)
        d.record_snapshot({"x": 1})
        # The automatic re-flush-on-completion chain (TEL-010) drives its
        # own retries -- just wait for it to settle (give up), not drive
        # it manually.
        while d._pending_write is not None:
            await d._pending_write
        self.assertIsNone(
            d._retry_batch,
            "after MAX_RETRY_ATTEMPTS failures, the batch must no longer "
            "be retained -- retrying forever is its own resource leak",
        )
        self.assertEqual(d.snapshots_lost_write_failure, 1)

    async def test_write_errors_counts_every_attempt_not_just_final_giveup(self):
        hass = _FakeHass(self.dir, fail_times=999)
        d = TC.TelemetryCapture(hass, "e")
        d.set_enabled(True)
        d.record_snapshot({"x": 1})
        while d._pending_write is not None:
            await d._pending_write
        self.assertEqual(
            d.write_errors, TC.MAX_RETRY_ATTEMPTS,
            "write_errors must count every individual failed attempt, "
            "distinct from snapshots_lost_write_failure (only "
            "incremented once, on final give-up)",
        )


class TestICS03SequenceBasedRemoval(unittest.IsolatedAsyncioTestCase):
    """ICS-03, external ICS audit -- confirmed genuine defects in this
    session's own v2.0.2 TEL-001/007/010 fix. Two distinct bugs, both
    rooted in removing buffered records the wrong way:

    (1) A successful RETRY did not remove its own records from
        self._buffer (the old `came_from_buffer` check skipped removal
        specifically for retries), so a retry that eventually succeeded
        left its records sitting there to be written again later --
        genuine duplicate writes.
    (2) Removal was position-based (pop N from the front), which is
        wrong if the deque's own maxlen eviction has already dropped
        some of a pending batch's records to make room for newly
        appended ones -- the position-based pop would then remove the
        wrong (newer) records instead.

    Both closed by giving every record a stable sequence number and
    removing by matching that number specifically.
    """

    def setUp(self):
        TC.TelemetryCapture.clear_registry()
        self.dir = tempfile.mkdtemp()

    async def test_successful_retry_does_not_duplicate_the_batch(self):
        """The core ICS-03 claim: a batch that fails once, then
        succeeds on retry, must be written exactly once -- not left in
        self._buffer to be written again by a later flush."""
        hass = _FakeHass(self.dir, fail_times=1)
        d = TC.TelemetryCapture(hass, "e")
        d.set_enabled(True)
        d.record_snapshot({"x": 1})  # forces an immediate flush (TEL-006)
        while d._pending_write is not None:
            await d._pending_write
        # The retry has now succeeded. Force every subsequent tick's
        # worth of snapshots through to confirm nothing duplicates.
        for i in range(2, TC.FLUSH_THRESHOLD + 2):
            d.record_snapshot({"x": i})
        await d.async_disable()

        path = os.path.join(self.dir, TC._SUBDIR, f"telemetry_{d.tag}.jsonl")
        lines = [json.loads(x) for x in open(path).read().splitlines()]
        x_values = [rec["x"] for rec in lines]
        self.assertEqual(
            x_values, sorted(x_values),
            "records must appear in order with no duplicates",
        )
        self.assertEqual(
            len(x_values), len(set(x_values)),
            f"a value appears more than once -- the originally-retried "
            f"batch was written twice. Full sequence: {x_values}",
        )
        self.assertEqual(x_values[0], 1, "the originally-failed record must still be present exactly once")

    async def test_give_up_removes_the_batch_from_buffer_not_just_retry_batch(self):
        """A batch declared permanently lost must not still be sitting
        in self._buffer to be written by a later, unrelated flush --
        contradicting its own loss counter."""
        hass = _FakeHass(self.dir, fail_times=999)  # never succeeds
        d = TC.TelemetryCapture(hass, "e")
        d.set_enabled(True)
        d.record_snapshot({"x": "should_be_lost"})
        while d._pending_write is not None:
            await d._pending_write
        self.assertEqual(d.snapshots_lost_write_failure, 1)

        # Confirm directly: no record with this content is still
        # sitting in the buffer.
        remaining = [record for _seq, record in d._buffer]
        self.assertFalse(
            any(r.get("x") == "should_be_lost" for r in remaining),
            "a record counted as permanently lost must not still be "
            "sitting in the buffer -- it would be written by some "
            "later, unrelated flush, contradicting its own loss counter",
        )

    async def test_removal_matches_by_sequence_surviving_maxlen_eviction(self):
        """The second, subtler ICS-03 race: if the buffer fills to
        MAX_BUFFERED_SNAPSHOTS while a write is genuinely in flight, the
        deque's own eviction can drop some of the IN-FLIGHT batch's
        records to make room for newly appended ones. Removal-by-
        sequence must still remove exactly the written records (if
        still present) and never remove a newer record that merely
        happens to occupy the front of the buffer now.
        """
        hass = _FakeHass(self.dir, executor_delay=0.05)
        d = TC.TelemetryCapture(hass, "e")
        d.set_enabled(True)
        d.record_snapshot({"x": 0})  # forces an immediate flush (TEL-006)
        self.assertIsNotNone(d._pending_write)
        # While that write is still in flight (delayed by executor_delay),
        # flood the buffer past MAX_BUFFERED_SNAPSHOTS so the deque's own
        # eviction drops the in-flight record (seq 0) from the front.
        for i in range(1, TC.MAX_BUFFERED_SNAPSHOTS + 10):
            d.record_snapshot({"x": i})
        remaining_seqs_before = {seq for seq, _ in d._buffer}
        self.assertNotIn(
            0, remaining_seqs_before,
            "test setup check: seq 0 should already be evicted by maxlen "
            "before the in-flight write even completes",
        )
        newest_seq_before = max(remaining_seqs_before)

        await d._pending_write  # the original (now-evicted) write completes

        remaining_seqs_after = {seq for seq, _ in d._buffer}
        self.assertIn(
            newest_seq_before, remaining_seqs_after,
            "a newer record must survive the completed write's own "
            "removal step -- sequence-based removal must not remove "
            "records it never actually wrote, even if they now occupy "
            "the front of the buffer",
        )


class TestTEL003TimerIdempotency(unittest.TestCase):
    """TEL-003, external ICS/IQS audit -- confirmed: async_turn_on() used
    to install a new timer unconditionally, even if one was already
    running -- two consecutive turn-on calls silently orphaned the
    first timer's own cancel handle."""

    def test_source_checks_for_an_existing_timer_before_installing_a_new_one(self):
        source = pathlib.Path(__file__).parent.parent.joinpath("switch.py").read_text()
        idx = source.find("async def async_turn_on(self, **kwargs: Any) -> None:")
        # There are multiple async_turn_on methods in switch.py; find the
        # one for ModbusTelemetryCaptureSwitchEntity specifically, by
        # searching from where that class starts.
        class_idx = source.find("class ModbusTelemetryCaptureSwitchEntity")
        idx = source.find("async def async_turn_on(", class_idx)
        end = source.find("\n    async def ", idx + 10)
        body = source[idx: end if end > -1 else idx + 1500]
        self.assertIn("if self._capture.cancel_periodic is not None:", body)
        self.assertIn("return", body)


class TestTEL004RegistryReleaseOnUnload(unittest.TestCase):
    """TEL-004, external ICS/IQS audit -- confirmed: TelemetryCapture.
    remove() existed but was never called in production -- every
    endpoint ever captured stayed referenced forever. BusDiagnostics had
    the identical gap, found while checking whether this was unique to
    the new feature; it was not."""

    def _init_source(self) -> str:
        return pathlib.Path(__file__).parent.parent.joinpath("__init__.py").read_text()

    def test_telemetry_capture_remove_is_called_during_unload(self):
        source = self._init_source()
        self.assertIn("TelemetryCapture.remove(endpoint)", source)

    def test_bus_diagnostics_remove_is_also_called_during_unload(self):
        """The same gap, found and fixed for the sibling registry too --
        not just the one the audit happened to name."""
        source = self._init_source()
        self.assertIn("BusDiagnostics.remove(endpoint)", source)

    def test_removal_is_guarded_against_calling_it_more_than_once_per_endpoint(self):
        source = self._init_source()
        idx = source.find("TelemetryCapture.remove(endpoint)")
        window = source[max(0, idx - 400): idx]
        self.assertIn("seen_endpoints", window)


class TestTEL009SnapshotTimingWording(unittest.TestCase):
    """TEL-009, external ICS/IQS audit -- confirmed: documentation
    claimed one snapshot "always reflects the same moment" for every
    coordinator -- the actual reads happen sequentially, not atomically."""

    def test_overstated_atomicity_claim_is_gone(self):
        source = pathlib.Path(__file__).parent.parent.joinpath("switch.py").read_text()
        self.assertNotIn("always reflects the same moment", source)

    def test_wording_now_describes_same_tick_not_same_instant(self):
        source = pathlib.Path(__file__).parent.parent.joinpath("switch.py").read_text()
        # NOT a plain assertIn("same capture tick", ...) -- the docstring
        # wraps across a line break between "same" and "capture tick",
        # so check for the less fragile "capture tick" alone.
        self.assertIn("capture tick", source)


class TestSectionEBatteryHealthTelemetryWiring(unittest.TestCase):  # v2.0.7
    """Section E, this release: build_telemetry_snapshot()'s new
    battery_health_manager_cls parameter."""

    class _FakeDevice:
        def __init__(self, serial):
            self.serial_number = serial

    class _FakeDD:
        def __init__(self, serial):
            self.device = TestSectionEBatteryHealthTelemetryWiring._FakeDevice(serial)

    class _FakeBHManager:
        def __init__(self, snap):
            self._snap = snap

        def snapshot(self):
            return self._snap

    class _FakeBHManagerRegistry:
        _registry: dict = {}

        @classmethod
        def get(cls, serial):
            return cls._registry.get(serial)

    def test_omitted_parameter_does_not_add_a_battery_health_section(self):
        """Backward compatibility: every existing caller that doesn't
        pass battery_health_manager_cls at all must see identical
        output to before this change."""
        dd = self._FakeDD("SN1")
        snap = TC.build_telemetry_snapshot(
            [dd], None, include_register_overlap=False,
            adaptive_controller_cls=self._FakeBHManagerRegistry,
            modbus_telemetry_cls=self._FakeBHManagerRegistry,
        )
        # No adaptive/telemetry registered either -> no device section at all.
        self.assertEqual(snap["devices"], {})

    def test_battery_health_section_present_when_manager_registered(self):
        registry = type("Registry", (), {"_data": {"SN1": self._FakeBHManager(
            {"bhi": 92.5, "confidence": "normal"})}})
        registry.get = classmethod(lambda cls, serial: cls._data.get(serial))
        dd = self._FakeDD("SN1")
        snap = TC.build_telemetry_snapshot(
            [dd], None, include_register_overlap=False,
            adaptive_controller_cls=self._FakeBHManagerRegistry,
            modbus_telemetry_cls=self._FakeBHManagerRegistry,
            battery_health_manager_cls=registry,
        )
        tag = next(iter(snap["devices"]))
        self.assertIn("battery_health", snap["devices"][tag])
        self.assertEqual(snap["devices"][tag]["battery_health"]["bhi"], 92.5)

    def test_devices_without_battery_health_are_skipped_cleanly(self):
        """A device with no registered BatteryHealthManager (e.g. this
        release's CONF_BH_ENABLED disabled, or no battery at all) must
        not raise or add an empty battery_health section -- same "not
        yet known" skip already established for adaptive/telemetry."""
        empty_registry = type("EmptyRegistry", (), {"get": classmethod(lambda cls, s: None)})
        dd = self._FakeDD("SN-NO-BATTERY")
        snap = TC.build_telemetry_snapshot(
            [dd], None, include_register_overlap=False,
            adaptive_controller_cls=self._FakeBHManagerRegistry,
            modbus_telemetry_cls=self._FakeBHManagerRegistry,
            battery_health_manager_cls=empty_registry,
        )
        self.assertEqual(snap["devices"], {})

    def test_device_serial_is_pseudonymised_same_as_adaptive_telemetry(self):
        """battery_health must not leak a real serial number into the
        capture file -- same pseudonym scheme every other section uses."""
        adaptive_registry = type("AdaptiveRegistry", (), {
            "_data": {"SN-REAL-SERIAL-123": object()},
            "get": classmethod(lambda cls, s: None),  # not registered here
        })
        bh_registry = type("BHRegistry", (), {
            "_data": {"SN-REAL-SERIAL-123": self._FakeBHManager({"bhi": 88.0})},
        })
        bh_registry.get = classmethod(lambda cls, s: cls._data.get(s))
        dd = self._FakeDD("SN-REAL-SERIAL-123")
        snap = TC.build_telemetry_snapshot(
            [dd], None, include_register_overlap=False,
            adaptive_controller_cls=adaptive_registry,
            modbus_telemetry_cls=adaptive_registry,
            battery_health_manager_cls=bh_registry,
        )
        tag = next(iter(snap["devices"]))
        self.assertNotIn("SN-REAL-SERIAL-123", tag)
        self.assertTrue(tag.startswith("dev"))


if __name__ == "__main__":
    unittest.main()
