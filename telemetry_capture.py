"""Periodic Modbus telemetry snapshot capture.

WHY THIS EXISTS
---------------
Complements bus_diagnostics.py's per-request capture: where that module
records EVERY individual request's wait/service split, this module
periodically snapshots the AGGREGATE metrics already computed elsewhere
(AdaptiveModbusController, ModbusTelemetry, SynchronizedPowerCoordinator's
own dedicated counters) into a time series on disk.

This exists specifically to answer the question raised assessing the
external ICS audit's Part 2 (architectural recommendations): whether the
Physical Demand Planner is worth building. AR-9's diagnostics.py surfacing
gives a single point-in-time snapshot; this gives a real time series,
without needing a second deployment purely to add more telemetry.

A real, confirmed gap motivated this rather than reusing bus_diagnostics.py
as-is: SynchronizedPowerCoordinator shares its ModbusTelemetry object with
the main/meter/battery/config coordinators on the same device (all five
call attach_telemetry() with the same instance), so aggregate request
counts in that shared object cannot be cleanly attributed to SyncPower's
own fallback specifically. SyncPower now keeps its own dedicated counters
(see synchronized_power_coordinator.py's own shortcut_hits/shortcut_misses/
fallback_cache_hits/fallback_physical_reads), which THIS module snapshots
directly by name, giving a clean, self-contained hit rate with no
cross-referencing required.

DESIGN CONSTRAINTS
-------------------
Deliberately the same discipline as bus_diagnostics.py, not a different
one invented for this module:
* **Default off.** Opt-in via a switch, NOT persisted across restarts, so
  it can never be silently left running.
* **Never blocks the event loop.** Writes happen in an executor thread.
* **Bounded on disk.** Hard byte cap with rotation -- a telemetry file
  that fills the disk is a worse failure than the one being diagnosed.
* **No identifying data.** The same salted pseudonym scheme as
  bus_diagnostics.py, not a separate one -- one tag identifies one
  physical bus consistently across both files.
* **Never breaks Modbus I/O or polling.** Every entry point is
  exception-guarded; a telemetry fault costs telemetry, nothing else.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from typing import Any

from .bus_diagnostics import pseudonym

_LOGGER = logging.getLogger(__name__)

#: Snapshots are small (a handful of numbers per device); a much larger
#: buffer than bus_diagnostics.py's per-request one is still cheap. At a
#: 30s cadence this is roughly 4 hours of history held in memory before a
#: flush stall would start dropping records.
MAX_BUFFERED_SNAPSHOTS = 500

#: Flush whenever this many snapshots are pending.
FLUSH_THRESHOLD = 20

#: Minimum seconds between flushes, so a burst cannot cause continuous I/O.
MIN_FLUSH_INTERVAL_S = 30.0

#: Hard cap per file before rotation, and how many rotations to keep.
#: Same values as bus_diagnostics.py -- no reasoned basis yet for these
#: needing to differ, and keeping them identical means one mental model
#: covers both files' disk behaviour.
MAX_FILE_BYTES = 5 * 1024 * 1024
KEEP_ROTATIONS = 2

# v2.0.2 (TEL-001/TEL-007, external ICS/IQS audit -- confirmed): a batch
# that fails to write is retried, not silently discarded -- but bounded,
# not indefinitely: a permanently broken write path (e.g. the config
# directory becoming unwritable) must eventually give up and report the
# loss explicitly, rather than accumulating an ever-growing unwritable
# batch forever. 3 attempts, not tuned against any specific failure mode
# -- chosen as a small, defensible number that tolerates a single
# transient failure (the common case: a momentary disk/FS hiccup)
# without treating every failure as instantly permanent.
MAX_RETRY_ATTEMPTS = 3

# v2.0.2 (TEL-002, external ICS/IQS audit -- confirmed): the bounded wait
# for a pending flush to complete during disable/unload -- the same
# "never block indefinitely" discipline already established project-wide
# (DISCONNECT_TIMEOUT and its own siblings, const.py) applied here for
# the first time to this module. A hung executor job must not hang HA's
# own unload/shutdown sequence; if the timeout is hit, the attempt is
# logged and teardown proceeds regardless -- the same fault-isolation
# contract every other bounded-cleanup site in this project already
# follows.
DISABLE_FLUSH_TIMEOUT_S = 10.0

_SUBDIR = "huawei_solar_diagnostics"


class TelemetryCapture:
    """Bounded periodic aggregate-snapshot capture for one Modbus endpoint.

    One instance per physical bus (matching BusDiagnostics' own "per bus,
    not per inverter" scoping) -- multiple devices sharing one endpoint
    each contribute their own named section within a single snapshot
    record, not separate files.
    """

    _registry: dict[str, "TelemetryCapture"] = {}

    def __init__(self, hass: Any, endpoint: str) -> None:
        self.hass = hass
        self.endpoint = endpoint
        self.tag = pseudonym(endpoint)
        self.enabled: bool = False
        # v2.0.3 FIX (ICS-03, external ICS audit -- confirmed): the
        # buffer used to hold bare record dicts, and records were
        # removed from it BY POSITION (popleft() N times) rather than by
        # identity. That was fragile in a specific, real way: if the
        # buffer filled to MAX_BUFFERED_SNAPSHOTS while a write was
        # pending, the deque's own maxlen eviction could ALREADY have
        # dropped some of the pending batch's records from the front to
        # make room for newly-appended ones -- meaning the position-based
        # popleft() loop, unaware of that, would end up popping newer
        # records instead of the ones it actually meant to remove. Each
        # record now carries its own stable, monotonically increasing
        # sequence number, and removal (_flush_batch(), below) matches
        # by that number specifically, not by position -- correct
        # regardless of what maxlen eviction may have already done to
        # the buffer in the meantime.
        self._buffer: deque[tuple[int, dict[str, Any]]] = deque(
            maxlen=MAX_BUFFERED_SNAPSHOTS
        )
        self._next_seq: int = 0
        self._last_flush: float | None = None
        self.snapshots_captured: int = 0
        self.snapshots_dropped: int = 0
        self.write_errors: int = 0
        #: Set by the caller that starts the periodic timer (switch.py) so
        #: it can be cancelled cleanly on disable/unload -- this module
        #: does not own the timer itself, only the capture/write logic,
        #: since the timer needs access to hass.helpers.event and the
        #: full set of coordinators to snapshot, neither of which belongs
        #: here.
        self.cancel_periodic: Any = None
        # v2.0.2 (TEL-001/TEL-002/TEL-007/TEL-010, external ICS/IQS audit
        # -- confirmed): the whole flush/persistence lifecycle redesigned
        # together, since these four findings were all really the same
        # underlying gap (no tracked, awaitable, retryable flush
        # operation) surfacing in four different ways.
        #
        # _pending_write: the currently in-flight flush, if any -- a
        # tracked asyncio.Task, not a fire-and-forget
        # hass.async_add_executor_job() call with its return value
        # discarded (the original TEL-001/TEL-002 defect). Lets a caller
        # (async_disable(), below) actually await completion, and lets
        # _schedule_flush() itself detect an already-in-flight write
        # rather than risk two overlapping ones.
        self._pending_write: Any = None
        # _retry_batch/_retry_attempts: a batch that failed to write is
        # retained here for a bounded number of retries (TEL-007), NOT
        # merged back into _buffer -- keeping it separate preserves
        # in-order delivery (this batch must be retried and written
        # before anything newer) without needing to reason about
        # deque.maxlen eviction interacting with a partially-failed batch.
        self._retry_batch: list[dict[str, Any]] | None = None
        self._retry_attempts: int = 0
        #: Diagnostic counter: snapshots permanently discarded after
        #: MAX_RETRY_ATTEMPTS failures -- distinct from write_errors
        #: (every individual failed attempt) and snapshots_dropped (the
        #: in-memory buffer overflowing) -- three different failure
        #: modes worth being able to tell apart.
        self.snapshots_lost_write_failure: int = 0
        # v2.0.2 (TEL-006, external ICS/IQS audit -- confirmed): forces
        # an immediate flush for the very first snapshot after enabling,
        # bypassing FLUSH_THRESHOLD -- without this, the normal 20-
        # snapshot/30s-cadence batching meant no file could appear for
        # roughly 10 minutes after enabling, which is difficult to
        # distinguish from the feature being broken (the operator's own
        # experience deploying this switch, independent of the actual
        # crash bug found separately). Only the first snapshot forces an
        # early write; every flush after that still respects the normal
        # threshold/interval, so this does not change steady-state
        # batching behaviour or write frequency.
        self._first_snapshot_pending: bool = True
        #: TEL-006's other half: last_snapshot_at/last_write_at, surfaced
        #: via stats() (switch.py's own extra_state_attributes), so
        #: "is this actually working" is answerable directly from the
        #: entity's own attributes without needing to inspect the
        #: filesystem or logs at all.
        self.last_snapshot_at: float | None = None
        self.last_write_at: float | None = None

    # ── registry ────────────────────────────────────────────────────────────
    @classmethod
    def get_or_create(cls, hass: Any, endpoint: str) -> "TelemetryCapture":
        inst = cls._registry.get(endpoint)
        if inst is None:
            inst = cls(hass, endpoint)
            cls._registry[endpoint] = inst
        return inst

    @classmethod
    def get(cls, endpoint: str) -> "TelemetryCapture | None":
        return cls._registry.get(endpoint)

    @classmethod
    def remove(cls, endpoint: str) -> None:
        cls._registry.pop(endpoint, None)

    @classmethod
    def clear_registry(cls) -> None:
        cls._registry.clear()

    # ── control ─────────────────────────────────────────────────────────────
    def set_enabled(self, enabled: bool) -> None:
        """Enable capture. For disabling, use async_disable() instead --
        see its own docstring for why disable specifically needs to be
        awaitable (TEL-002).
        """
        if enabled == self.enabled:
            return
        self.enabled = enabled
        if enabled:
            self._first_snapshot_pending = True
            _LOGGER.warning(
                "Modbus telemetry capture ENABLED for bus %s. Periodic "
                "aggregate snapshots will be written to config/%s/. "
                "Disable when finished.",
                self.tag, _SUBDIR,
            )
        else:
            # v2.0.2 (TEL-002): the synchronous path is kept ONLY as a
            # defensive fallback for a caller that (incorrectly) still
            # calls set_enabled(False) directly instead of
            # async_disable() -- it still cancels the timer and attempts
            # a fire-and-forget flush, but cannot await it. Every
            # production caller in this codebase uses async_disable()
            # now; nothing should reach this branch in practice.
            _LOGGER.info("Modbus telemetry capture disabled for bus %s", self.tag)
            self._schedule_flush(force=True)
            if self.cancel_periodic is not None:
                self.cancel_periodic()
                self.cancel_periodic = None

    async def async_disable(self) -> None:
        """Disable capture and deterministically persist whatever was
        pending before returning -- the actual TEL-002 fix.

        Cancels the periodic timer FIRST (so nothing new can be added to
        the buffer while this waits), then awaits the final flush,
        bounded by DISABLE_FLUSH_TIMEOUT_S so a hung executor job cannot
        hang HA's own unload/shutdown sequence -- the same "never block
        indefinitely" contract every other bounded-cleanup site in this
        project already follows. A timeout here is logged and swallowed;
        teardown must proceed regardless of whether the final write
        actually completed in time.
        """
        if not self.enabled:
            return
        self.enabled = False
        _LOGGER.info("Modbus telemetry capture disabled for bus %s", self.tag)
        if self.cancel_periodic is not None:
            self.cancel_periodic()
            self.cancel_periodic = None
        task = self._schedule_flush(force=True)
        if task is None:
            task = self._pending_write
        if task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(task), timeout=DISABLE_FLUSH_TIMEOUT_S,
                )
            except TimeoutError:
                _LOGGER.warning(
                    "Modbus telemetry capture: final flush for bus %s did "
                    "not complete within %.0fs; proceeding with teardown "
                    "regardless -- some pending telemetry may not have "
                    "been written",
                    self.tag, DISABLE_FLUSH_TIMEOUT_S,
                )
            except Exception:  # noqa: BLE001 — teardown must never raise
                _LOGGER.exception(
                    "Modbus telemetry capture: final flush for bus %s failed",
                    self.tag,
                )

    # ── capture ─────────────────────────────────────────────────────────────
    def record_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Record one periodic aggregate snapshot. Cheap no-op when disabled."""
        if not self.enabled:
            return
        if len(self._buffer) == self._buffer.maxlen:
            self.snapshots_dropped += 1
        record = {"t": round(time.time(), 3), "bus": self.tag, **snapshot}
        # v2.0.3 (ICS-03): tagged with its own stable sequence number --
        # see self._buffer's own comment in __init__ for why.
        self._buffer.append((self._next_seq, record))
        self._next_seq += 1
        self.snapshots_captured += 1
        self.last_snapshot_at = time.time()
        if self._first_snapshot_pending:
            # v2.0.2 (TEL-006): force an early flush for the very first
            # snapshot only -- see this flag's own comment in __init__
            # for the full reasoning.
            self._first_snapshot_pending = False
            self._schedule_flush(force=True)
        elif len(self._buffer) >= FLUSH_THRESHOLD:
            self._schedule_flush()

    # ── flushing ────────────────────────────────────────────────────────────
    def _schedule_flush(self, force: bool = False) -> Any:
        """Schedule a flush if one isn't already in flight and there's
        something to write. Returns the tracked task if one was
        (newly or already) scheduled, else None.

        v2.0.2 (TEL-001/TEL-002/TEL-010, external ICS/IQS audit --
        confirmed): the batch is no longer cleared from self._buffer
        until the write has genuinely succeeded (see _flush_batch()
        below) -- this method's own job is now just deciding WHAT to
        send for writing and tracking the resulting task, not owning
        the buffer's own lifecycle directly.
        """
        now = time.monotonic()
        if self._pending_write is not None and not self._pending_write.done():
            return self._pending_write
        if (
            not force
            and self._last_flush is not None
            and (now - self._last_flush) < MIN_FLUSH_INTERVAL_S
        ):
            return None
        # A failed batch awaiting retry takes priority over newly buffered
        # snapshots -- preserves in-order delivery rather than writing
        # newer data ahead of older data that hasn't been persisted yet.
        if self._retry_batch is not None:
            batch = self._retry_batch
        elif self._buffer:
            batch = list(self._buffer)
        else:
            return None
        self._last_flush = now
        self._pending_write = self.hass.async_create_task(
            self._flush_batch(batch)
        )
        return self._pending_write

    async def _flush_batch(self, batch: list[tuple[int, dict[str, Any]]]) -> None:
        """Write one batch, with bounded retry on failure.

        v2.0.2 (TEL-001/TEL-007/TEL-010, external ICS/IQS audit --
        confirmed): a batch is only removed from self._buffer once this
        coroutine confirms the write genuinely succeeded, not before
        scheduling it (the original bug). On failure, the batch is
        retained (self._retry_batch) for up to MAX_RETRY_ATTEMPTS, not
        silently discarded; only after exhausting those does it count as
        permanently lost, explicitly (snapshots_lost_write_failure), not
        just another write_errors increment indistinguishable from a
        transient, later-recovered failure.

        v2.0.3 FIX (ICS-03, external ICS audit -- confirmed): two
        distinct bugs closed together, since they were both really the
        same root cause -- removal keyed on the wrong thing.

        (1) This method used to gate buffer removal on `batch is not
        self._retry_batch` ("came_from_buffer"), reasoning that a retry
        batch had "already" been removed once. That reasoning was wrong:
        removal only ever happened on SUCCESS, so a batch that failed
        its first attempt was NEVER removed from self._buffer -- it was
        only copied into self._retry_batch for safekeeping. A successful
        retry therefore still needed to remove it, and the old code
        skipped exactly that step, leaving the original records sitting
        in self._buffer to be written again on the next flush -- a
        genuine duplicate-write bug, confirmed, not theoretical.

        (2) Separately, removal used to pop `len(batch)` items by
        POSITION from the front of self._buffer, assuming the batch's
        own records were still exactly the oldest ones there. If
        self._buffer filled to MAX_BUFFERED_SNAPSHOTS while this write
        was in flight, the deque's own maxlen eviction could already
        have dropped some of THIS batch's records to make room for newly
        appended ones -- meaning the position-based pop would end up
        removing newer records instead of the ones actually written.

        Both are fixed by giving every record a stable sequence number
        (record_snapshot(), self._next_seq) and removing by matching
        that number specifically -- correct regardless of retry history
        or what maxlen eviction may have already done to the buffer.
        """
        try:
            await self.hass.async_add_executor_job(self._write, batch)
            self.last_write_at = time.time()
            self._remove_from_buffer_by_seq(batch)
            self._retry_batch = None
            self._retry_attempts = 0
        except Exception:  # noqa: BLE001 — telemetry must never break anything
            self.write_errors += 1
            self._retry_attempts += 1
            if self._retry_attempts >= MAX_RETRY_ATTEMPTS:
                # v2.0.3 (ICS-03): the give-up path used to leave these
                # records sitting in self._buffer despite counting them
                # as permanently lost -- a later flush could then write
                # a batch the code had already told the operator was
                # gone. Removed here too, for the same reason removal
                # happens on success: once a batch is declared lost, it
                # must not still be sitting there to be written anyway.
                self.snapshots_lost_write_failure += len(batch)
                self._remove_from_buffer_by_seq(batch)
                self._retry_batch = None
                self._retry_attempts = 0
                _LOGGER.error(
                    "Modbus telemetry capture: write failed %d times for "
                    "bus %s; giving up on this batch of %d snapshot(s) -- "
                    "they are permanently lost",
                    MAX_RETRY_ATTEMPTS, self.tag, len(batch),
                )
            else:
                self._retry_batch = batch
                _LOGGER.warning(
                    "Modbus telemetry capture: write failed for bus %s "
                    "(attempt %d/%d); will retry",
                    self.tag, self._retry_attempts, MAX_RETRY_ATTEMPTS,
                )
        finally:
            self._pending_write = None
            # v2.0.2 (TEL-010): if a retry is pending, or enough new data
            # has accumulated while this write was in flight, schedule
            # the next flush immediately rather than waiting for a later
            # snapshot tick or a forced disable to notice.
            if self._retry_batch is not None:
                self._schedule_flush(force=True)
            elif len(self._buffer) >= FLUSH_THRESHOLD:
                self._schedule_flush()

    def _remove_from_buffer_by_seq(self, batch: list[tuple[int, dict[str, Any]]]) -> None:
        """Remove exactly the records in `batch`, matched by their own
        stable sequence number -- not by position. See _flush_batch()'s
        own docstring (ICS-03) for why position-based removal was wrong.
        """
        written_seqs = {seq for seq, _ in batch}
        self._buffer = deque(
            (item for item in self._buffer if item[0] not in written_seqs),
            maxlen=MAX_BUFFERED_SNAPSHOTS,
        )

    def _write(self, batch: list[tuple[int, dict[str, Any]]]) -> None:
        """Append snapshots as JSON lines. Runs in an executor thread.

        v2.0.2 (TEL-001/TEL-007): now RAISES on failure instead of
        catching internally -- _flush_batch() (the caller, now async)
        owns the retry/give-up decision, which needs to see the actual
        exception, not just a side-effect counter increment.
        """
        directory = self.hass.config.path(_SUBDIR)
        os.makedirs(directory, exist_ok=True)
        # Different filename from bus_diagnostics.py's bus_<tag>.jsonl,
        # same directory, same tag -- deliberately, so the two files
        # for one physical bus are easy to find together.
        path = os.path.join(directory, f"telemetry_{self.tag}.jsonl")

        # v2.0.2 (TEL-008, external ICS/IQS audit -- confirmed): this
        # check alone is a PRE-write rotation threshold, not a hard
        # post-write cap -- a large batch can still push the active file
        # above MAX_FILE_BYTES after this check passes. Documented
        # explicitly here (matching the audit's own suggested remediation
        # of clarifying rather than changing the behaviour) rather than
        # computing exact encoded batch size up front: this capture's
        # batches are small and bounded (at most MAX_BUFFERED_SNAPSHOTS
        # records), so the actual overshoot in practice is a few KB past
        # the nominal cap at most, not a meaningful risk on its own.
        if os.path.exists(path) and os.path.getsize(path) >= MAX_FILE_BYTES:
            self._rotate(path)

        with open(path, "a", encoding="utf-8") as handle:
            # v2.0.3 (ICS-03): batch entries are (seq, record) pairs now
            # -- only the record itself is written; the sequence number
            # is purely an internal buffer-bookkeeping detail, not part
            # of the on-disk format.
            for _seq, record in batch:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")

    def _rotate(self, path: str) -> None:
        oldest = f"{path}.{KEEP_ROTATIONS}"
        if os.path.exists(oldest):
            os.remove(oldest)
        for index in range(KEEP_ROTATIONS - 1, 0, -1):
            src, dst = f"{path}.{index}", f"{path}.{index + 1}"
            if os.path.exists(src):
                os.replace(src, dst)
        os.replace(path, f"{path}.1")

    # ── diagnostics about the diagnostics ───────────────────────────────────
    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "bus": self.tag,
            "snapshots_captured": self.snapshots_captured,
            "snapshots_dropped": self.snapshots_dropped,
            "write_errors": self.write_errors,
            "buffered": len(self._buffer),
            # v2.0.2 (TEL-004/TEL-006/TEL-007, external ICS/IQS audit):
            # last_snapshot_at/last_write_at directly answer "is this
            # working" from the entity's own attributes, without needing
            # to inspect the filesystem or logs. snapshots_lost_write_
            # failure and pending_retry surface the new bounded-retry
            # semantics (TEL-007) -- distinct from write_errors, which
            # counts every individual failed attempt, including ones
            # later recovered by a successful retry.
            "last_snapshot_at": self.last_snapshot_at,
            "last_write_at": self.last_write_at,
            "snapshots_lost_write_failure": self.snapshots_lost_write_failure,
            "pending_retry": self._retry_batch is not None,
        }


def check_register_overlap(coordinators: dict[str, Any]) -> dict[str, Any]:
    """One-time structural check: does any pair of these coordinators poll
    the same register?

    Part of assessing the external ICS audit's Physical Demand Planner
    recommendation -- the Planner's second justification (merging demand
    across coordinators) rests on there being real overlap to merge. The
    reanalysis argued from the register maps' own structure that main,
    meter, battery, and configuration coordinators poll almost entirely
    disjoint sets by physical device, so there was never much to merge in
    the first place -- this makes that claim directly checkable against
    what each coordinator has actually been asked to poll, not just
    argued from reading the source.

    Deliberately a one-time structural check, not a per-snapshot repeated
    one: register assignment is fixed at entity-setup time (which
    registers a coordinator polls is derived from which entities are
    currently listening, via async_contexts() -- see
    update_coordinator.py's own "collect register names" step), so unlike
    the traffic counters elsewhere in this module, this fact does not
    change tick to tick once entities are set up. Takes coordinator.data
    (populated after that coordinator's own first successful poll) as the
    source of "what this coordinator actually polls" -- deliberately not
    a static list, since none exists; this is the real, current set.

    `coordinators` maps a short, human-readable kind name ("main",
    "power_meter", "energy_storage", "configuration") to the coordinator
    object itself. Coordinators whose `.data` is empty or not yet
    populated (no successful poll yet) are skipped for that pairing
    rather than reported as a false "no overlap" -- silence there means
    "not yet known", not "confirmed disjoint".
    """
    kinds = list(coordinators.keys())
    register_sets: dict[str, set[Any]] = {}
    skipped: list[str] = []
    for kind in kinds:
        data = getattr(coordinators[kind], "data", None)
        if not data:
            skipped.append(kind)
            continue
        register_sets[kind] = set(data.keys())

    overlaps: dict[str, list[str]] = {}
    checked_kinds = list(register_sets.keys())
    for i, kind_a in enumerate(checked_kinds):
        for kind_b in checked_kinds[i + 1:]:
            shared = register_sets[kind_a] & register_sets[kind_b]
            if shared:
                overlaps[f"{kind_a}_vs_{kind_b}"] = sorted(str(s) for s in shared)

    return {
        "checked": checked_kinds,
        "skipped_not_yet_polled": skipped,
        "register_counts": {k: len(v) for k, v in register_sets.items()},
        "overlaps": overlaps,
        "any_overlap_found": bool(overlaps),
    }


def build_telemetry_snapshot(
    device_datas: list[Any],
    sync_coordinator: Any | None,
    *,
    include_register_overlap: bool,
    adaptive_controller_cls: Any,
    modbus_telemetry_cls: Any,
) -> dict[str, Any]:
    """Gather one combined, JSON-serialisable snapshot across every
    coordinator on this entry, for one periodic capture tick.

    Pure and testable: takes the classes it needs to query as parameters
    (rather than importing AdaptiveModbusController/ModbusTelemetry
    directly) so it can be exercised with fakes in isolation, the same
    reasoning already applied throughout this project's own test suite
    for similar per-device registry lookups.

    Device serials are pseudonymised (same salted scheme as
    bus_diagnostics.py/TelemetryCapture's own tag), matching this
    project's established privacy discipline for anything that could end
    up in a shared capture file.

    include_register_overlap gates the (comparatively expensive, and
    genuinely one-time -- register assignment does not change tick to
    tick once entities are set up) check_register_overlap() call. The
    caller is responsible for calling this only once real data exists
    across the relevant coordinators; passing True before that simply
    means every relevant coordinator is skipped (see check_register_
    overlap()'s own "not yet known, not confirmed disjoint" handling) and
    costs almost nothing to check again on a later tick.
    """
    devices: dict[str, Any] = {}
    for dd in device_datas:
        serial = dd.device.serial_number
        tag = f"dev{pseudonym(serial)[:8]}"
        section: dict[str, Any] = {}

        adaptive = adaptive_controller_cls.get(serial)
        if adaptive is not None:
            section["adaptive"] = adaptive.snapshot()

        telemetry = modbus_telemetry_cls.get(serial)
        if telemetry is not None:
            section["telemetry"] = telemetry.snapshot()

        if section:
            devices[tag] = section

    result: dict[str, Any] = {"devices": devices}

    if sync_coordinator is not None:
        result["sync_power"] = sync_coordinator.snapshot()

    if include_register_overlap:
        coordinators: dict[str, Any] = {}
        for dd in device_datas:
            serial_tag = f"dev{pseudonym(dd.device.serial_number)[:8]}"
            if getattr(dd, "update_coordinator", None) is not None:
                coordinators[f"{serial_tag}_main"] = dd.update_coordinator
            if getattr(dd, "power_meter_update_coordinator", None) is not None:
                coordinators[f"{serial_tag}_power_meter"] = dd.power_meter_update_coordinator
            if getattr(dd, "energy_storage_update_coordinator", None) is not None:
                coordinators[f"{serial_tag}_energy_storage"] = dd.energy_storage_update_coordinator
            if getattr(dd, "configuration_update_coordinator", None) is not None:
                coordinators[f"{serial_tag}_configuration"] = dd.configuration_update_coordinator
        if coordinators:
            result["register_overlap"] = check_register_overlap(coordinators)

    return result
