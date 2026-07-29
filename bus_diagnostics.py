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
MAX_FILE_BYTES = 5 * 1024 * 1024
KEEP_ROTATIONS = 2

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

    def __init__(self, hass: Any, endpoint: str) -> None:
        self.hass = hass
        self.endpoint = endpoint
        self.tag = pseudonym(endpoint)
        self.enabled: bool = False
        self._buffer: deque[dict[str, Any]] = deque(maxlen=MAX_BUFFERED_RECORDS)
        self._last_flush: float = 0.0
        self._flush_in_progress: bool = False
        self.records_captured: int = 0
        self.records_dropped: int = 0
        self.write_errors: int = 0

    # ── registry ────────────────────────────────────────────────────────────
    @classmethod
    def get_or_create(cls, hass: Any, endpoint: str) -> "BusDiagnostics":
        inst = cls._registry.get(endpoint)
        if inst is None:
            inst = cls(hass, endpoint)
            cls._registry[endpoint] = inst
        return inst

    @classmethod
    def get(cls, endpoint: str) -> "BusDiagnostics | None":
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
                "Modbus diagnostics ENABLED for bus %s. Per-request records "
                "will be written to config/%s/. Disable when finished.",
                self.tag, _SUBDIR,
            )
        else:
            _LOGGER.info("Modbus diagnostics disabled for bus %s", self.tag)
            self._schedule_flush(force=True)

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
    ) -> None:
        """Record one completed request. Cheap no-op when disabled."""
        if not self.enabled:
            return
        if len(self._buffer) == self._buffer.maxlen:
            # deque discards silently; count it so the gap is visible in the file.
            self.records_dropped += 1
        self._buffer.append(
            {
                "t": round(time.time(), 3),
                "bus": self.tag,
                        "src": sanitise_label(label),
                "wait_ms": round(wait_ms, 1),
                "service_ms": round(service_ms, 1),
                "qd": queue_depth,
                "out": outcome,
                "regs": registers,
                "prio": priority,
            }
        )
        self.records_captured += 1
        if len(self._buffer) >= FLUSH_THRESHOLD:
            self._schedule_flush()

    # ── flushing ────────────────────────────────────────────────────────────
    def _schedule_flush(self, force: bool = False) -> None:
        now = time.monotonic()
        if self._flush_in_progress:
            return
        if not force and (now - self._last_flush) < MIN_FLUSH_INTERVAL_S:
            return
        if not self._buffer:
            return
        self._last_flush = now
        batch = list(self._buffer)
        self._buffer.clear()
        self._flush_in_progress = True
        try:
            # Executor, never the event loop: inline disk I/O would inflate the
            # service times this module exists to measure.
            self.hass.async_add_executor_job(self._write, batch)
        except Exception:  # noqa: BLE001
            self._flush_in_progress = False
            self.write_errors += 1
            _LOGGER.exception("Modbus diagnostics: could not schedule flush")

    def _write(self, batch: list[dict[str, Any]]) -> None:
        """Append records as JSON lines. Runs in an executor thread."""
        try:
            directory = self.hass.config.path(_SUBDIR)
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, f"bus_{self.tag}.jsonl")

            if os.path.exists(path) and os.path.getsize(path) >= MAX_FILE_BYTES:
                self._rotate(path)

            with open(path, "a", encoding="utf-8") as handle:
                for record in batch:
                    handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        except Exception:  # noqa: BLE001 — diagnostics must never break anything
            self.write_errors += 1
            _LOGGER.exception("Modbus diagnostics: write failed")
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
            "records_captured": self.records_captured,
            "records_dropped": self.records_dropped,
            "write_errors": self.write_errors,
            "buffered": len(self._buffer),
        }
