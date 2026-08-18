"""Per-request Modbus diagnostic capture (v1.3.0, Phase 0).

WHY THIS EXISTS
---------------
Three days of field data established a strong correlation — both inverters'
failure rates track the MASTER's workload (r = +0.94) while the slave's own
traffic is flat — but could not identify the *mechanism*. Two candidates remain,
and they call for opposite fixes:

  (a) Requests queue behind one another on our shared lock.
      => a bus scheduler fixes it.
  (b) The master's own CPU is saturated (it relays the slave's frames on top of
      its own battery/meter/PV workload).
      => only reducing demand helps; scheduling alone will not.

These are indistinguishable in every sensor available today, because nothing
separates **time spent waiting for admission** from **time spent talking to the
device**. That single split settles it, and it is what this module records.

DESIGN CONSTRAINTS
------------------
* **Default off.** Capture is opt-in via a switch and is deliberately NOT
  persisted across restarts, so it can never be silently left running.
* **Never blocks the event loop.** Records go into a bounded in-memory ring
  buffer; writes happen in an executor thread. Doing disk I/O inline would
  inflate the very service times being measured — the instrument would distort
  the experiment.
* **Bounded on disk.** Hard byte cap with rotation. A diagnostics file that
  fills the disk is a worse failure than the one being diagnosed.
* **No identifying data.** Serial numbers and endpoints are replaced by a
  stable salted pseudonym, so a capture can be shared without exposing the
  installation.
* **Never breaks Modbus I/O.** Every entry point is exception-guarded; a
  diagnostics fault costs diagnostics, nothing else.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections import deque
from typing import Any

_LOGGER = logging.getLogger(__name__)

#: Ring buffer size. At ~1 request/s across all coordinators this is roughly
#: 8 minutes of history held in memory — enough to survive a flush stall
#: without unbounded growth.
MAX_BUFFERED_RECORDS = 500

#: Flush whenever this many records are pending.
FLUSH_THRESHOLD = 100

#: Minimum seconds between flushes, so a burst cannot cause continuous I/O.
MIN_FLUSH_INTERVAL_S = 30.0

#: Hard cap per file before rotation, and how many rotations to keep.
# v2.0.9 (user request, this release): bumped from 5MB to 100MB -- with
# KEEP_ROTATIONS=2, worst case is (current + 2 rotated) x 100MB = 300MB
# for this mechanism alone, 600MB combined with telemetry_capture.py's
# own identical bump below -- flagged explicitly since "100MB each"
# multiplies by the rotation count, not a single flat total.
MAX_FILE_BYTES = 100 * 1024 * 1024
KEEP_ROTATIONS = 2

# v2.0.3 (ICS-08, external ICS audit -- confirmed): the exact same defect
# TEL-001/002/007 already closed in telemetry_capture.py -- this module
# never got the same fix. Same values as that module's own
# MAX_RETRY_ATTEMPTS/DISABLE_FLUSH_TIMEOUT_S, for the same reason
# MAX_FILE_BYTES/KEEP_ROTATIONS above are already shared: no reasoned
# basis yet for these needing to differ between the two capture modules,
# and keeping them identical means one mental model covers both.
MAX_RETRY_ATTEMPTS = 3
DISABLE_FLUSH_TIMEOUT_S = 10.0

_SUBDIR = "huawei_solar_diagnostics"


def pseudonym(value: str) -> str:
    """Stable, non-reversible short identifier for a serial or endpoint."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


#: A token is treated as a serial if it contains a run of >=6 digits.
#: Deliberately NOT anchored with \b: coordinator names are underscore-joined
#: ("<serial>_battery_data_update_coordinator") and "_" is a word
#: character, so \b never fires at the digit/underscore boundary — the first
#: attempt at this regex silently matched nothing.
_DIGIT_RUN_RE = re.compile(r"\d{6,}")


def sanitise_label(label: str) -> str:
    """Replace serial-like tokens in a coordinator name with a pseudonym.

    DEFECT (v1.3.0, found in the first field capture): coordinator names are
    built as ``f"{device.serial_number}_..._update_coordinator"``, so passing
    ``coordinator.name`` straight through wrote real serial numbers into every
    record — despite this module pseudonymising the endpoint and the audit
    claiming no serials were present. The test only asserted the *endpoint*
    was absent, so the leak went unnoticed.

    Sanitising here rather than at the call site means every future caller is
    covered by default, including ones that do not know to be careful.
    """
    parts = label.split("_")
    out = [
        f"dev{pseudonym(part)[:4]}" if _DIGIT_RUN_RE.search(part) else part
        for part in parts
    ]
    return "_".join(out)


class BusDiagnostics:
    """Bounded per-request capture for one Modbus endpoint.

    Attached to a ModbusGuard as ``guard.diagnostics``. When disabled,
    ``record()`` costs one boolean check and does nothing else.
    """

    _registry: dict[str, "BusDiagnostics"] = {}
    # v2.0.9 (Phase 4.8, this release -- old DEF-011, external ICS
    # quality/defect/architecture audit -- confirmed): mirrors ModbusGuard's
    # own _ref_counts exactly (modbus_guard.py) -- see acquire_endpoint()/
    # release_endpoint() below for the full reasoning. remove() alone had
    # no concept of "how many entries still need this instance", so one
    # entry unloading could remove a still-in-use instance out from under
    # another entry sharing the same physical endpoint.
    _ref_counts: dict[str, int] = {}

    def __init__(self, hass: Any, endpoint: str) -> None:
        self.hass = hass
        self.endpoint = endpoint
        self.tag = pseudonym(endpoint)
        self.enabled: bool = False
        # v2.0.3 (ICS-08/ICS-03, external ICS audit -- confirmed): each
        # record now carries its own stable sequence number, and removal
        # (in _flush_batch(), below) matches by that number specifically,
        # not by position -- see telemetry_capture.py's own
        # TelemetryCapture._flush_batch() docstring for the full
        # reasoning (the identical defect, already closed there).
        self._buffer: deque[tuple[int, dict[str, Any]]] = deque(
            maxlen=MAX_BUFFERED_RECORDS
        )
        self._next_seq: int = 0
        #: None = never flushed. Initialising to 0.0 was a latent bug: the
        #: rate-limit check reads it as "flushed at monotonic time 0", so on a
        #: host where time.monotonic() is still below MIN_FLUSH_INTERVAL_S
        #: (freshly booted machine or container) the FIRST flush was suppressed
        #: and records sat in the buffer until 30 s of uptime had passed. It
        #: surfaced as an intermittent test failure rather than a wrong value,
        #: which is exactly how it would have behaved in the field.
        self._last_flush: float | None = None
        self.records_captured: int = 0
        self.records_dropped: int = 0
        self.write_errors: int = 0
        # v2.0.3 (ICS-08): the whole flush/persistence lifecycle rebuilt
        # to match telemetry_capture.py's own TelemetryCapture -- see
        # that class's own __init__ comment for the full reasoning
        # behind each of these fields.
        self._pending_write: Any = None
        self._retry_batch: list[tuple[int, dict[str, Any]]] | None = None
        self._retry_attempts: int = 0
        self.records_lost_write_failure: int = 0
        self.last_write_at: float | None = None

    # ── registry ────────────────────────────────────────────────────────────
    @classmethod
    def get_or_create(cls, hass: Any, endpoint: str) -> "BusDiagnostics":
        """Return the instance for *endpoint*, creating it if absent.

        Does NOT itself affect the reference count -- matches ModbusGuard's
        own get_or_create() contract exactly (modbus_guard.py): callers
        that own the endpoint's lifecycle must bracket their own usage
        with acquire_endpoint()/release_endpoint() instead.
        """
        inst = cls._registry.get(endpoint)
        if inst is None:
            inst = cls(hass, endpoint)
            cls._registry[endpoint] = inst
        return inst

    @classmethod
    def get(cls, endpoint: str) -> "BusDiagnostics | None":
        return cls._registry.get(endpoint)

    @classmethod
    def acquire_endpoint(cls, hass: Any, endpoint: str) -> "BusDiagnostics":
        """v2.0.9 (Phase 4.8, this release -- old DEF-011, external ICS
        quality/defect/architecture audit -- confirmed): entry-level
        acquire, mirroring ModbusGuard.acquire_endpoint() exactly --
        creates the instance if needed and increments its reference
        count by one. Must be paired with exactly one later
        release_endpoint() call for the same endpoint.
        """
        inst = cls.get_or_create(hass, endpoint)
        cls._ref_counts[endpoint] = cls._ref_counts.get(endpoint, 0) + 1
        return inst

    @classmethod
    def release_endpoint(cls, endpoint: str) -> None:
        """v2.0.9 (Phase 4.8, this release -- old DEF-011, external ICS
        quality/defect/architecture audit -- confirmed): entry-level
        release, mirroring ModbusGuard.release_endpoint() exactly --
        decrements the endpoint's reference count, removing the
        instance from the registry only once the count reaches zero
        (every entry that acquired it has released it). A release with
        no matching prior acquire is a safe no-op, not an error --
        matches ModbusGuard's own reasoning for the same design choice.
        """
        if endpoint not in cls._ref_counts:
            return
        cls._ref_counts[endpoint] -= 1
        if cls._ref_counts[endpoint] <= 0:
            cls._ref_counts.pop(endpoint, None)
            cls._registry.pop(endpoint, None)

    @classmethod
    def remove(cls, endpoint: str) -> None:
        """DEPRECATED (v2.0.9, Phase 4.8 -- old DEF-011): unconditional
        removal, ignoring reference count -- mirrors ModbusGuard.remove()'s
        own deprecation exactly (modbus_guard.py), which documents this
        as the exact class of bug DEF-011 describes. Retained only so
        any external/legacy caller does not hard-fail; production code
        must use release_endpoint() instead. Calling this directly will
        remove the instance even if another entry sharing the same
        physical endpoint still holds a reference to it.
        """
        cls._registry.pop(endpoint, None)
        cls._ref_counts.pop(endpoint, None)

    @classmethod
    def clear_registry(cls) -> None:
        cls._registry.clear()
        cls._ref_counts.clear()

    # ── control ─────────────────────────────────────────────────────────────
    def set_enabled(self, enabled: bool) -> None:
        """Enable capture. For disabling, use async_disable() instead --
        see TelemetryCapture.async_disable()'s own docstring for why
        disable specifically needs to be awaitable (ICS-08, the same
        defect as TEL-002).
        """
        if enabled == self.enabled:
            return
        self.enabled = enabled
        if enabled:
            _LOGGER.warning(
                "Modbus diagnostics ENABLED for bus %s. Per-request records "
                "will be written to config/%s/. Disable when finished.",
                self.tag, _SUBDIR,
            )
        else:
            # v2.0.3 (ICS-08): kept ONLY as a defensive fallback for a
            # caller that (incorrectly) still calls set_enabled(False)
            # directly instead of async_disable() -- see
            # TelemetryCapture.set_enabled()'s own comment on this same
            # pattern. Every production caller uses async_disable() now.
            _LOGGER.info("Modbus diagnostics disabled for bus %s", self.tag)
            self._schedule_flush(force=True)

    async def async_disable(self) -> None:
        """Disable capture and deterministically persist whatever was
        pending before returning -- the ICS-08 fix, identical in shape
        to TelemetryCapture.async_disable() (TEL-002). See that method's
        own docstring for the full reasoning.
        """
        if not self.enabled:
            return
        self.enabled = False
        _LOGGER.info("Modbus diagnostics disabled for bus %s", self.tag)
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
                    "Modbus diagnostics: final flush for bus %s did not "
                    "complete within %.0fs; proceeding with teardown "
                    "regardless -- some pending records may not have "
                    "been written",
                    self.tag, DISABLE_FLUSH_TIMEOUT_S,
                )
            except Exception:  # noqa: BLE001 — teardown must never raise
                _LOGGER.exception(
                    "Modbus diagnostics: final flush for bus %s failed",
                    self.tag,
                )

    # ── capture ─────────────────────────────────────────────────────────────
    def record(
        self,
        *,
        endpoint: str,
        label: str,
        wait_ms: float,
        service_ms: float,
        queue_depth: int,
        outcome: str,
        registers: int | None = None,
        priority: str | None = None,
        # v2.0.9 (Phase 2.1/2.4, this release -- ICS-16, Architecture R3/
        # R4, both external ICS audits -- confirmed): the exact fields
        # both audits' own "Priority 0" recommendation asked for, added
        # to this ALREADY-EXISTING capture mechanism rather than a new
        # parallel system -- see this module's own docstring; the ring
        # buffer, pseudonymisation, bounded-file, never-blocks-the-loop
        # properties are all unchanged, just three more optional fields
        # per record. All default to None so any caller not yet passing
        # them (there should be none left after this release, but kept
        # defensive) degrades cleanly.
        chunk_index: int | None = None,
        chunk_count: int | None = None,
        retry_count: int | None = None,
        logical_request_id: int | None = None,
        transition_reason: str | None = None,
    ) -> None:
        """Record one completed request. Cheap no-op when disabled."""
        if not self.enabled:
            return
        if len(self._buffer) == self._buffer.maxlen:
            # deque discards silently; count it so the gap is visible in the file.
            self.records_dropped += 1
        record = {
            "t": round(time.time(), 3),
            "bus": self.tag,
            "src": sanitise_label(label),
            "wait_ms": round(wait_ms, 1),
            "service_ms": round(service_ms, 1),
            "qd": queue_depth,
            "out": outcome,
            "regs": registers,
            "prio": priority,
            # v2.0.9 (Phase 2.1/2.4, this release): short keys, matching
            # this record's own existing convention (qd/out/regs/prio),
            # not the longer names used in the Python-level API above --
            # this dict is what actually gets serialised to disk, and
            # every existing key here is already abbreviated for the
            # same file-size reason MAX_FILE_BYTES/rotation exist at all.
            "chunk_idx": chunk_index,
            "chunk_n": chunk_count,
            "retries": retry_count,
            "req_id": logical_request_id,
            "transition": transition_reason,
        }
        self._buffer.append((self._next_seq, record))
        self._next_seq += 1
        self.records_captured += 1
        if len(self._buffer) >= FLUSH_THRESHOLD:
            self._schedule_flush()

    # ── flushing ────────────────────────────────────────────────────────────
    def _schedule_flush(self, force: bool = False) -> Any:
        """Schedule a flush if one isn't already in flight and there's
        something to write. Returns the tracked task if one was (newly
        or already) scheduled, else None. See TelemetryCapture.
        _schedule_flush()'s own docstring for the full reasoning --
        identical in shape here.
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
        """Write one batch, with bounded retry on failure. See
        TelemetryCapture._flush_batch()'s own docstring for the full
        ICS-03/TEL-001/007/010 reasoning -- this is the identical defect
        (ICS-08), closed the identical way.
        """
        try:
            await self.hass.async_add_executor_job(self._write, batch)
            self.last_write_at = time.time()
            self._remove_from_buffer_by_seq(batch)
            self._retry_batch = None
            self._retry_attempts = 0
        except Exception:  # noqa: BLE001 — diagnostics must never break anything
            self.write_errors += 1
            self._retry_attempts += 1
            if self._retry_attempts >= MAX_RETRY_ATTEMPTS:
                self.records_lost_write_failure += len(batch)
                self._remove_from_buffer_by_seq(batch)
                self._retry_batch = None
                self._retry_attempts = 0
                _LOGGER.error(
                    "Modbus diagnostics: write failed %d times for bus "
                    "%s; giving up on this batch of %d record(s) -- they "
                    "are permanently lost",
                    MAX_RETRY_ATTEMPTS, self.tag, len(batch),
                )
            else:
                self._retry_batch = batch
                _LOGGER.warning(
                    "Modbus diagnostics: write failed for bus %s "
                    "(attempt %d/%d); will retry",
                    self.tag, self._retry_attempts, MAX_RETRY_ATTEMPTS,
                )
        finally:
            self._pending_write = None
            if self._retry_batch is not None:
                self._schedule_flush(force=True)
            elif len(self._buffer) >= FLUSH_THRESHOLD:
                self._schedule_flush()

    def _remove_from_buffer_by_seq(self, batch: list[tuple[int, dict[str, Any]]]) -> None:
        """Remove exactly the records in `batch`, matched by their own
        stable sequence number -- not by position."""
        written_seqs = {seq for seq, _ in batch}
        self._buffer = deque(
            (item for item in self._buffer if item[0] not in written_seqs),
            maxlen=MAX_BUFFERED_RECORDS,
        )

    def _write(self, batch: list[tuple[int, dict[str, Any]]]) -> None:
        """Append records as JSON lines. Runs in an executor thread.

        v2.0.3 (ICS-08): now RAISES on failure instead of catching
        internally -- _flush_batch() (the caller, now async) owns the
        retry/give-up decision.
        """
        directory = self.hass.config.path(_SUBDIR)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"bus_{self.tag}.jsonl")

        if os.path.exists(path) and os.path.getsize(path) >= MAX_FILE_BYTES:
            self._rotate(path)

        with open(path, "a", encoding="utf-8") as handle:
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
            "records_captured": self.records_captured,
            "records_dropped": self.records_dropped,
            "write_errors": self.write_errors,
            "buffered": len(self._buffer),
            "last_write_at": self.last_write_at,
            "records_lost_write_failure": self.records_lost_write_failure,
            "pending_retry": self._retry_batch is not None,
        }
