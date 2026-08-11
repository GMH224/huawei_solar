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
        self._buffer: deque[dict[str, Any]] = deque(maxlen=MAX_BUFFERED_SNAPSHOTS)
        self._last_flush: float | None = None
        self._flush_in_progress: bool = False
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
        """Enable or disable capture. Disabling flushes whatever is pending."""
        if enabled == self.enabled:
            return
        self.enabled = enabled
        if enabled:
            _LOGGER.warning(
                "Modbus telemetry capture ENABLED for bus %s. Periodic "
                "aggregate snapshots will be written to config/%s/. "
                "Disable when finished.",
                self.tag, _SUBDIR,
            )
        else:
            _LOGGER.info("Modbus telemetry capture disabled for bus %s", self.tag)
            self._schedule_flush(force=True)
            if self.cancel_periodic is not None:
                self.cancel_periodic()
                self.cancel_periodic = None

    # ── capture ─────────────────────────────────────────────────────────────
    def record_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Record one periodic aggregate snapshot. Cheap no-op when disabled."""
        if not self.enabled:
            return
        if len(self._buffer) == self._buffer.maxlen:
            self.snapshots_dropped += 1
        record = {"t": round(time.time(), 3), "bus": self.tag, **snapshot}
        self._buffer.append(record)
        self.snapshots_captured += 1
        if len(self._buffer) >= FLUSH_THRESHOLD:
            self._schedule_flush()

    # ── flushing ────────────────────────────────────────────────────────────
    def _schedule_flush(self, force: bool = False) -> None:
        now = time.monotonic()
        if self._flush_in_progress:
            return
        if (
            not force
            and self._last_flush is not None
            and (now - self._last_flush) < MIN_FLUSH_INTERVAL_S
        ):
            return
        if not self._buffer:
            return
        self._last_flush = now
        batch = list(self._buffer)
        self._buffer.clear()
        self._flush_in_progress = True
        try:
            self.hass.async_add_executor_job(self._write, batch)
        except Exception:  # noqa: BLE001
            self._flush_in_progress = False
            self.write_errors += 1
            _LOGGER.exception("Modbus telemetry capture: could not schedule flush")

    def _write(self, batch: list[dict[str, Any]]) -> None:
        """Append snapshots as JSON lines. Runs in an executor thread."""
        try:
            directory = self.hass.config.path(_SUBDIR)
            os.makedirs(directory, exist_ok=True)
            # Different filename from bus_diagnostics.py's bus_<tag>.jsonl,
            # same directory, same tag -- deliberately, so the two files
            # for one physical bus are easy to find together.
            path = os.path.join(directory, f"telemetry_{self.tag}.jsonl")

            if os.path.exists(path) and os.path.getsize(path) >= MAX_FILE_BYTES:
                self._rotate(path)

            with open(path, "a", encoding="utf-8") as handle:
                for record in batch:
                    handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        except Exception:  # noqa: BLE001 — telemetry must never break anything
            self.write_errors += 1
            _LOGGER.exception("Modbus telemetry capture: write failed")
        finally:
            self._flush_in_progress = False

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
