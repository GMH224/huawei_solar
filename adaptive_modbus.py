"""Adaptive Modbus parameter learning for Huawei Solar.

Learns optimal Modbus parameters (poll interval, inter-request gap, request
timeout, queue depth) from historical success/failure/RTT data, organised into
15-minute circadian time slots across the 24-hour day cycle.

Why circadian slots?
--------------------
Inverter failure rates are NOT random.  They cluster at specific times of day
that correspond to inverter state transitions:

  • ~Sunrise  — MPPT ramp-up, maximum CPU load for power tracking
  • ~Midday   — MPPT saturation, inverter switches between power curves
  • ~Sunset   — Grid ↔ battery handover, working-mode negotiation
  • ~03:00    — Pre-dawn battery maintenance charge, BMS wake-up

By associating failures with the time-of-day slot in which they occurred, the
controller can pre-emptively raise parameters (wider gap, longer timeout, slower
polling) before known-bad windows, rather than only reacting after the fact.

How many days until full learning?
-----------------------------------
Each 15-minute slot needs:
  • ≥ 50 weighted requests → "learning" confidence (useful predictions)
  • ≥ 150 requests         → "confident" (good predictions)
  • ≥ 300 requests         → "full" confidence (stable)

At a 30 s poll interval: 30 requests/slot/day.
  • Useful from:  day 2
  • Good from:    day 5
  • Full by:      day 10

Plan for **7 days** of normal operation as the minimum for stable circadian
patterns.  The controller is beneficial from day 1 (it still reacts to observed
failures within each poll cycle) but its time-of-day predictions improve
continuously over the first two weeks.

Persistence
-----------
All slot statistics are written to HA's .storage/ directory via
``homeassistant.helpers.storage.Store`` so they survive HA restarts and updates.
Daily decay (factor 0.85/day) is applied on load, giving observations from 14
days ago only ~10 % the weight of today's observations.

Architecture
------------
AdaptiveModbusController  — singleton per inverter serial number.
                             Owns the slot statistics, persistence, transition
                             detection, and parameter derivation.
AdaptiveParams             — immutable snapshot of recommended parameters for
                             the current moment.
create_adaptive_entities() — factory producing 9 HA diagnostic sensor entities
                             that expose the controller's internal state.

Integration wiring
------------------
1. __init__.py creates the controller and attaches it to each coordinator via
   ``coordinator.attach_adaptive(controller)``.
2. update_coordinator.py:
   a. Calls ``controller.get_params()`` at the start of each poll cycle.
   b. Applies params to the guard (gap, queue_depth) and self (timeout).
   c. Measures RTT around ``batch_update()``.
   d. Calls ``controller.record_request(rtt_ms, success, timeout)``.
   e. Calls ``controller.notify_transition()`` when a state change is detected.
3. sensor.py (via __init__.py) calls ``create_adaptive_entities()`` to register
   the 9 diagnostic sensors.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import logging
import math
import time
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import (
    ADAPTIVE_DECAY_FACTOR,
    ADAPTIVE_FAILURE_RATE_HIGH,
    ADAPTIVE_FAILURE_RATE_LOW,
    ADAPTIVE_FULL_CONFIDENCE_N,
    ADAPTIVE_GAP_MAX,
    ADAPTIVE_GAP_MIN,
    ADAPTIVE_POLL_COLD_START,
    ADAPTIVE_POLL_MAX,
    ADAPTIVE_POLL_MIN,
    ADAPTIVE_RTT_SAMPLE_SIZE,
    ADAPTIVE_SLOT_COUNT,
    ADAPTIVE_SLOT_MINUTES,
    ADAPTIVE_TIMEOUT_MAX,
    ADAPTIVE_TIMEOUT_MIN,
    ADAPTIVE_TRANSITION_DURATION_MINUTES,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# HA storage version — increment when the on-disk schema changes
_STORAGE_VERSION = 1
# How often the controller pushes updates to its HA sensor entities
_SENSOR_PUSH_INTERVAL = timedelta(seconds=60)
# Minimum interval between storage writes (debounce)
_SAVE_DEBOUNCE_SECONDS = 60.0


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class TimeSlotStats:
    """Weighted statistics for one 15-minute window of the 24-hour day.

    All counts are floating-point to support the daily decay multiplication
    without losing fractional information.  They represent *weighted* event
    counts, not raw integers.
    """
    slot_index: int = 0      # BUG-005 FIX: store index so label property works
    n: float = 0.0           # weighted request count
    failures: float = 0.0    # weighted failure count (includes timeouts)
    timeouts: float = 0.0    # weighted timeout count
    rtt_p95_ms: float = 0.0  # running P95 RTT estimate (ms)
    # Recent raw RTT samples, bounded to ADAPTIVE_RTT_SAMPLE_SIZE
    rtt_samples: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": round(self.n, 4),
            "f": round(self.failures, 4),
            "t": round(self.timeouts, 4),
            "rtt_p95": round(self.rtt_p95_ms, 1),
            "rtt_s": [round(x, 1) for x in self.rtt_samples],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any], slot_index: int = 0) -> "TimeSlotStats":
        return cls(
            slot_index=slot_index,
            n=float(d.get("n", 0)),
            failures=float(d.get("f", 0)),
            timeouts=float(d.get("t", 0)),
            rtt_p95_ms=float(d.get("rtt_p95", 0)),
            rtt_samples=[float(x) for x in d.get("rtt_s", [])],
        )

    def apply_decay(self, factor: float) -> None:
        """Multiply weighted counts by *factor* (daily decay)."""
        self.n *= factor
        self.failures *= factor
        self.timeouts *= factor
        # RTT P95 is not decayed — it represents the shape of RTT distribution,
        # not a count, and should remain representative even as counts decay.

    def record(
        self,
        rtt_ms: float,
        success: bool,
        timeout: bool,
        max_samples: int,
    ) -> None:
        """Add one observation to this slot."""
        self.n += 1.0
        if not success:
            self.failures += 1.0
        if timeout:
            self.timeouts += 1.0

        # Maintain bounded RTT sample list for P95 estimation.
        # Only record RTT for successful (non-timeout) requests — a timeout
        # RTT would be the full timeout duration, not the inverter's response time.
        if success and rtt_ms > 0:
            self.rtt_samples.append(rtt_ms)
            if len(self.rtt_samples) > max_samples:
                self.rtt_samples.pop(0)   # FIFO trim (list is small; pop(0) fine)
            # Recompute P95 from sorted samples
            sorted_s = sorted(self.rtt_samples)
            idx = max(0, int(math.ceil(0.95 * len(sorted_s))) - 1)
            self.rtt_p95_ms = sorted_s[idx]

    @property
    def failure_rate(self) -> float:
        return self.failures / self.n if self.n >= 1.0 else 0.0

    @property
    def confidence(self) -> float:
        """0.0 → 1.0 based on how many weighted requests this slot has seen."""
        return min(self.n / ADAPTIVE_FULL_CONFIDENCE_N, 1.0)

    @property
    def label(self) -> str:
        """Slot descriptor e.g. '11:30–11:45'.

        BUG-005 FIX: previously always returned '' because the slot index was
        not stored on the dataclass.  slot_index is now set at construction time
        so the label can be derived without calling back into the controller.
        """
        start_min = self.slot_index * ADAPTIVE_SLOT_MINUTES
        end_min = start_min + ADAPTIVE_SLOT_MINUTES
        return (
            f"{start_min // 60:02d}:{start_min % 60:02d}"
            f"\u2013{end_min // 60:02d}:{end_min % 60:02d}"
        )


@dataclass(frozen=True)
class AdaptiveParams:
    """Recommended Modbus parameters for the current moment.

    Derived from the current time-slot's historical statistics, blended with
    the overall fleet baseline when confidence is low.
    """
    poll_interval: timedelta
    request_gap: timedelta
    request_timeout: timedelta
    max_queue_depth: int
    confidence: float          # 0.0–1.0
    in_transition: bool
    slot_index: int
    slot_failure_rate: float   # raw failure rate for the current slot


# ── Controller ─────────────────────────────────────────────────────────────────

class AdaptiveModbusController:
    """Per-inverter circadian Modbus learning controller.

    Lifecycle
    ---------
    1. ``AdaptiveModbusController(hass, serial_number, device_info)``
    2. ``await controller.async_load()``  — must be called before first poll
    3. Coordinator calls ``get_params()`` at the start of each poll cycle.
    4. Coordinator calls ``record_request(rtt_ms, success, timeout)`` after each poll.
    5. Coordinator calls ``notify_transition()`` on detected state changes.
    6. ``controller.stop()`` on integration unload.
    """

    _registry: dict[str, "AdaptiveModbusController"] = {}

    # ── class helpers ─────────────────────────────────────────────────────────

    @classmethod
    def get_or_create(
        cls,
        hass: HomeAssistant,
        serial_number: str,
        device_info: DeviceInfo,
    ) -> "AdaptiveModbusController":
        if serial_number not in cls._registry:
            cls._registry[serial_number] = cls(hass, serial_number, device_info)
        return cls._registry[serial_number]

    @classmethod
    def get(cls, serial_number: str) -> "AdaptiveModbusController | None":
        return cls._registry.get(serial_number)

    @classmethod
    def clear_registry(cls) -> None:
        cls._registry.clear()

    # ── instance ──────────────────────────────────────────────────────────────

    def __init__(
        self,
        hass: HomeAssistant,
        serial_number: str,
        device_info: DeviceInfo,
    ) -> None:
        self.hass = hass
        self.serial_number = serial_number
        self.device_info = device_info

        # 96 time slots covering the 24-hour day
        self._slots: list[TimeSlotStats] = [
            TimeSlotStats(slot_index=i) for i in range(ADAPTIVE_SLOT_COUNT)
        ]

        # Transition state (elevated params for ADAPTIVE_TRANSITION_DURATION_MINUTES)
        self._in_transition: bool = False
        self._transition_expires: float = 0.0   # monotonic time

        # Persistence
        self._store: Store = Store(
            hass,
            _STORAGE_VERSION,
            f"{DOMAIN}.adaptive.{serial_number}",
        )
        self._last_decay_date: date | None = None
        self._first_data_date: date | None = None
        self._dirty: bool = False
        self._save_task: asyncio.Task | None = None

        # HA sensor push
        self._listeners: list[Any] = []
        self._unsub_push: Any = None

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def async_load(self) -> None:
        """Load persisted statistics from HA storage and apply daily decay."""
        raw = await self._store.async_load()
        if raw:
            try:
                self._deserialize(raw)
                _LOGGER.info(
                    "AdaptiveModbus[%s]: loaded %d days of learning data",
                    self.serial_number,
                    self.days_of_data,
                )
            except Exception as exc:
                _LOGGER.warning(
                    "AdaptiveModbus[%s]: could not load stored data (%s) — starting fresh",
                    self.serial_number,
                    exc,
                )
                # BUG-5 FIX: reset ALL state, not just slots.  Without this,
                # a partial _deserialize() could leave stale date fields that
                # would cause incorrect decay calculations on the fresh empty slots.
                self._reset_slots()
                self._last_decay_date = None
                self._first_data_date = None

        self._apply_startup_decay()

        # BUG-6 FIX: cancel any existing push subscription before creating a new
        # one.  If async_load() is called twice (e.g., config-entry reload) the
        # previous subscription must be cleaned up to prevent memory leaks.
        if self._unsub_push:
            self._unsub_push()
        self._unsub_push = async_track_time_interval(
            self.hass, self._push_to_listeners, _SENSOR_PUSH_INTERVAL
        )

    def stop(self) -> None:
        """Cancel background tasks (called on integration unload).

        BUG-004 FIX: if a save is pending (dirty flag set, task in-flight),
        persist synchronously before cancelling so no learning data is lost on
        reload or shutdown.  ``async_create_task`` is used so the save runs on
        the HA event loop without requiring ``stop()`` itself to be a coroutine.
        """
        if self._unsub_push:
            self._unsub_push()
            self._unsub_push = None
        if self._save_task and not self._save_task.done():
            self._save_task.cancel()
            self._save_task = None
        # Flush any unsaved data synchronously before the integration tears down.
        if self._dirty:
            self.hass.async_create_task(self._async_save())

    # ── public API ────────────────────────────────────────────────────────────

    def get_params(self) -> AdaptiveParams:
        """Return recommended Modbus parameters for the current moment.

        Call at the start of each poll cycle.  The result reflects:
        • The current 15-minute time slot's historical failure rate and RTT.
        • Whether a state transition is currently active.
        • Blending with a conservative baseline when slot confidence is low.
        """
        now_mono = time.monotonic()
        if self._in_transition and now_mono > self._transition_expires:
            self._in_transition = False

        slot_idx = self._current_slot_index()
        slot = self._slots[slot_idx]
        return self._derive_params(slot, slot_idx, self._in_transition)

    def record_request(
        self, rtt_ms: float, success: bool, timeout: bool
    ) -> None:
        """Record one completed Modbus request into the current time slot."""
        slot_idx = self._current_slot_index()
        self._slots[slot_idx].record(
            rtt_ms, success, timeout, ADAPTIVE_RTT_SAMPLE_SIZE
        )
        self._schedule_save()

    def notify_transition(self, reason: str = "") -> None:
        """Signal that the inverter has changed operating state.

        Called by the coordinator when any of the following is detected:
        • Day ↔ Night mode switch (NightModeDetector callback)
        • Battery charge/discharge direction reversal
        • Working mode change

        Elevates Modbus parameters to their maximum tolerances for
        ADAPTIVE_TRANSITION_DURATION_MINUTES regardless of the slot's learned
        history, because the CPU load spike during a state change is immediate
        and can't be predicted from historical averages alone.
        """
        duration = timedelta(minutes=ADAPTIVE_TRANSITION_DURATION_MINUTES)
        self._in_transition = True
        self._transition_expires = time.monotonic() + duration.total_seconds()
        _LOGGER.debug(
            "AdaptiveModbus[%s]: transition detected (%s) — elevated params for %d min",
            self.serial_number,
            reason or "unknown",
            ADAPTIVE_TRANSITION_DURATION_MINUTES,
        )
        self._push_to_listeners(None)

    # ── HA sensor listener plumbing ───────────────────────────────────────────

    def add_listener(self, cb: Any) -> None:
        self._listeners.append(cb)

    def remove_listener(self, cb: Any) -> None:
        try:
            self._listeners.remove(cb)
        except ValueError:
            pass

    @callback
    def _push_to_listeners(self, _now: Any) -> None:
        snap = self._snapshot()
        # BUG-003 FIX: snapshot the list before iteration so that a listener
        # calling remove_listener() during its own callback does not cause
        # subsequent listeners to be skipped (Python list mutation semantics).
        for cb_fn in list(self._listeners):
            try:
                cb_fn(snap)
            except Exception:  # noqa: BLE001
                # BUG-011 FIX: isolate each callback so one failing listener
                # cannot abort delivery to all subsequent listeners.
                _LOGGER.exception(
                    "AdaptiveModbus[%s]: listener callback %r raised an exception",
                    self.serial_number,
                    cb_fn,
                )

    def _snapshot(self) -> dict[str, Any]:
        """Produce a point-in-time diagnostic snapshot for sensor entities."""
        params = self.get_params()
        slot = self._slots[params.slot_index]
        return {
            "poll_interval_s": params.poll_interval.total_seconds(),
            "gap_ms": params.request_gap.total_seconds() * 1000,
            "timeout_s": params.request_timeout.total_seconds(),
            "max_queue_depth": params.max_queue_depth,
            "confidence_pct": round(params.confidence * 100, 1),
            "slot_failure_rate_pct": round(params.slot_failure_rate * 100, 2),
            "in_transition": "ON" if params.in_transition else "OFF",
            "days_of_data": self.days_of_data,
            "current_slot": self._slot_label(params.slot_index),
            "slot_requests": round(slot.n, 1),
        }

    # ── properties ────────────────────────────────────────────────────────────

    @property
    def days_of_data(self) -> int:
        if self._first_data_date is None:
            return 0
        # BUG-7 FIX: clamp to 0 so clock-skew/drift never returns a negative value
        return max(0, (date.today() - self._first_data_date).days + 1)

    # ── internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _current_slot_index() -> int:
        """Map the current wall-clock time to a slot index (0–95)."""
        now = datetime.now()
        return (now.hour * 60 + now.minute) // ADAPTIVE_SLOT_MINUTES

    @staticmethod
    def _slot_label(idx: int) -> str:
        start_min = idx * ADAPTIVE_SLOT_MINUTES
        end_min = start_min + ADAPTIVE_SLOT_MINUTES
        return (
            f"{start_min // 60:02d}:{start_min % 60:02d}"
            f"–{end_min // 60:02d}:{end_min % 60:02d}"
        )

    def _derive_params(
        self,
        slot: TimeSlotStats,
        slot_idx: int,
        in_transition: bool,
    ) -> AdaptiveParams:
        """Map slot statistics to concrete Modbus parameters.

        Blending strategy
        -----------------
        When confidence is low (< 0.3), blend 70 % conservative baseline
        with 30 % slot-derived params.  At full confidence, use slot params
        entirely.  This prevents a single early failure from over-tuning a slot
        before enough data is available.

        Transition override
        -------------------
        If ``in_transition`` is True, the failure_rate used for derivation is
        floored at ADAPTIVE_FAILURE_RATE_HIGH so that maximum tolerances are
        applied unconditionally for the transition window.
        """
        confidence = slot.confidence
        raw_fr = slot.failure_rate
        fr = max(raw_fr, ADAPTIVE_FAILURE_RATE_HIGH) if in_transition else raw_fr

        # Normalise failure rate to [0, 1] across the LOW→HIGH range
        t = min(
            max((fr - ADAPTIVE_FAILURE_RATE_LOW) / (ADAPTIVE_FAILURE_RATE_HIGH - ADAPTIVE_FAILURE_RATE_LOW), 0.0),
            1.0,
        )

        # ── Poll interval: 20 s → 180 s ───────────────────────────────────────
        # At full confidence: use slot-derived value (failure-rate interpolation).
        # At zero confidence: use ADAPTIVE_POLL_COLD_START (60 s) — deliberately
        # independent of ADAPTIVE_POLL_MIN so that lowering the minimum (20 s)
        # never makes unknown slots poll at their most aggressive rate.
        poll_s_derived = (
            ADAPTIVE_POLL_MIN.total_seconds()
            + t * (ADAPTIVE_POLL_MAX.total_seconds() - ADAPTIVE_POLL_MIN.total_seconds())
        )
        poll_s_baseline = ADAPTIVE_POLL_COLD_START.total_seconds()  # 60 s cold start
        poll_s = confidence * poll_s_derived + (1 - confidence) * poll_s_baseline
        poll_interval = timedelta(seconds=round(poll_s))

        # ── Gap: 150 ms → 500 ms (floor is a hardware constraint, not configurable) ──
        # Base on P95 RTT: gap should be at least RTT_P95 × 0.4 so the inverter
        # FSM has recovered before the next request arrives.
        rtt_based_gap_ms = slot.rtt_p95_ms * 0.4
        gap_ms_derived = max(
            ADAPTIVE_GAP_MIN.total_seconds() * 1000,
            min(rtt_based_gap_ms + t * 150, ADAPTIVE_GAP_MAX.total_seconds() * 1000),
        )
        gap_ms_baseline = ADAPTIVE_GAP_MIN.total_seconds() * 1000
        gap_ms = confidence * gap_ms_derived + (1 - confidence) * gap_ms_baseline
        request_gap = timedelta(milliseconds=gap_ms)

        # ── Timeout: 15 s → 60 s ──────────────────────────────────────────────
        # Set timeout to at least 5× the P95 RTT so that a slow-but-responding
        # inverter is not cut off prematurely.  The keep-alive probe (opt. 3)
        # handles dead-connection detection independently, so this timeout is
        # purely a 'live-but-slow' guard — 60 s max is sufficient.
        rtt_based_timeout_s = (slot.rtt_p95_ms / 1000) * 5
        timeout_s_derived = max(
            ADAPTIVE_TIMEOUT_MIN.total_seconds(),
            min(rtt_based_timeout_s + t * 20, ADAPTIVE_TIMEOUT_MAX.total_seconds()),
        )
        # Cold-start timeout baseline: use ADAPTIVE_TIMEOUT_MIN (15 s) — already
        # conservative enough; no separate cold-start constant needed here because
        # a 15 s timeout during a low-confidence poll is correctly cautious.
        timeout_s_baseline = ADAPTIVE_TIMEOUT_MIN.total_seconds()
        timeout_s = confidence * timeout_s_derived + (1 - confidence) * timeout_s_baseline
        request_timeout = timedelta(seconds=round(timeout_s))

        # ── Queue depth: 3 → 1 ────────────────────────────────────────────────
        if fr >= ADAPTIVE_FAILURE_RATE_HIGH or in_transition:
            max_queue_depth = 1
        elif fr >= ADAPTIVE_FAILURE_RATE_LOW:
            max_queue_depth = 2
        else:
            max_queue_depth = 3

        return AdaptiveParams(
            poll_interval=poll_interval,
            request_gap=request_gap,
            request_timeout=request_timeout,
            max_queue_depth=max_queue_depth,
            confidence=confidence,
            in_transition=in_transition,
            slot_index=slot_idx,
            slot_failure_rate=raw_fr,
        )

    # ── persistence ───────────────────────────────────────────────────────────

    def _schedule_save(self) -> None:
        """Debounced save: write at most once per _SAVE_DEBOUNCE_SECONDS.

        BUG-010 FIX: the previous implementation silently discarded calls that
        arrived while a debounce sleep was in-flight — any data recorded after
        the task was created but before its 60 s sleep expired would never be
        persisted unless another _schedule_save() fired after the task finished.
        The fix always sets _dirty=True so that when the in-flight task wakes up
        it will still see the flag and persist the latest state.  If no task is
        running, a new one is created as before.
        """
        self._dirty = True          # mark dirty unconditionally
        if self._save_task and not self._save_task.done():
            # A debounce task is already sleeping; it will see _dirty=True when
            # it wakes and will persist the latest state.  No new task needed.
            return
        self._save_task = self.hass.async_create_task(self._deferred_save())

    async def _deferred_save(self) -> None:
        try:
            await asyncio.sleep(_SAVE_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            # BUG-009 FIX: if the task is cancelled (e.g. during stop()),
            # honour the cancellation cleanly without suppressing it.  The
            # dirty-flag flush in stop() takes responsibility for persistence.
            raise
        if self._dirty:
            await self._async_save()

    async def _async_save(self) -> None:
        if self._first_data_date is None:
            self._first_data_date = date.today()
        self._dirty = False
        await self._store.async_save(self._serialize())
        _LOGGER.debug(
            "AdaptiveModbus[%s]: statistics persisted (%d days of data)",
            self.serial_number,
            self.days_of_data,
        )

    def _serialize(self) -> dict[str, Any]:
        return {
            "version": _STORAGE_VERSION,
            "serial": self.serial_number,
            "last_decay_date": (self._last_decay_date or date.today()).isoformat(),
            "first_data_date": (self._first_data_date or date.today()).isoformat(),
            "slots": {
                str(i): s.to_dict()
                for i, s in enumerate(self._slots)
                if s.n > 0.001   # skip empty slots to keep storage compact
            },
        }

    def _deserialize(self, raw: dict[str, Any]) -> None:
        self._reset_slots()
        slots_raw = raw.get("slots", {})
        for idx_str, slot_dict in slots_raw.items():
            try:
                idx = int(idx_str)
                if 0 <= idx < ADAPTIVE_SLOT_COUNT:
                    self._slots[idx] = TimeSlotStats.from_dict(slot_dict, slot_index=idx)
            except (ValueError, KeyError):
                pass
        last_str = raw.get("last_decay_date")
        self._last_decay_date = date.fromisoformat(last_str) if last_str else None
        first_str = raw.get("first_data_date")
        self._first_data_date = date.fromisoformat(first_str) if first_str else None

    def _reset_slots(self) -> None:
        self._slots = [TimeSlotStats(slot_index=i) for i in range(ADAPTIVE_SLOT_COUNT)]

    def _apply_startup_decay(self) -> None:
        """Apply accumulated daily decay since the last save."""
        today = date.today()
        if self._last_decay_date is None:
            self._last_decay_date = today
            return

        days_elapsed = (today - self._last_decay_date).days
        if days_elapsed <= 0:
            return

        factor = ADAPTIVE_DECAY_FACTOR ** days_elapsed
        for slot in self._slots:
            slot.apply_decay(factor)

        _LOGGER.debug(
            "AdaptiveModbus[%s]: applied %.3f decay (%d day(s)) to all slots",
            self.serial_number,
            factor,
            days_elapsed,
        )
        self._last_decay_date = today
        self._dirty = True


# ── HA Sensor entities ─────────────────────────────────────────────────────────

_ADAPTIVE_SENSORS: list[tuple[str, str, str | None, str]] = [
    ("poll_interval_s",        "Adaptive poll interval",       "s",   "mdi:timer-sync-outline"),
    ("gap_ms",                 "Adaptive Modbus gap",          "ms",  "mdi:timer-pause-outline"),
    ("timeout_s",              "Adaptive Modbus timeout",      "s",   "mdi:timer-alert-outline"),
    ("max_queue_depth",        "Adaptive queue depth",         None,  "mdi:layers-triple-outline"),
    ("confidence_pct",         "Adaptive learning confidence", "%",   "mdi:school-outline"),
    ("slot_failure_rate_pct",  "Adaptive slot failure rate",   "%",   "mdi:percent"),
    ("in_transition",          "Inverter state transition",    None,  "mdi:swap-horizontal-bold"),
    ("days_of_data",           "Adaptive days of data",        "d",   "mdi:calendar-range"),
    ("current_slot",           "Adaptive time slot",           None,  "mdi:clock-time-four-outline"),
    ("slot_requests",          "Adaptive slot requests",       None,  "mdi:counter"),
]


def create_adaptive_entities(
    controller: AdaptiveModbusController,
) -> list["HuaweiSolarAdaptiveSensorEntity"]:
    """Create all HA sensor entities for an AdaptiveModbusController."""
    return [
        HuaweiSolarAdaptiveSensorEntity(controller, attr_key, name, unit, icon)
        for attr_key, name, unit, icon in _ADAPTIVE_SENSORS
    ]


class HuaweiSolarAdaptiveSensorEntity(SensorEntity):
    """HA diagnostic Sensor backed by AdaptiveModbusController."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(
        self,
        controller: AdaptiveModbusController,
        attr_key: str,
        name: str,
        unit: str | None,
        icon: str,
    ) -> None:
        self._controller = controller
        self._attr_key = attr_key
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_device_info = controller.device_info
        self._attr_unique_id = (
            f"{controller.serial_number}_adaptive_{attr_key}"
        )
        self._attr_native_value: Any = None
        # Numeric sensors get MEASUREMENT state class; non-numeric get none
        if unit is not None:
            self._attr_state_class = SensorStateClass.MEASUREMENT

    async def async_added_to_hass(self) -> None:
        self._controller.add_listener(self._on_update)
        # Populate immediately
        snap = self._controller._snapshot()
        self._attr_native_value = snap.get(self._attr_key)

    async def async_will_remove_from_hass(self) -> None:
        self._controller.remove_listener(self._on_update)

    @callback
    def _on_update(self, snap: dict[str, Any]) -> None:
        self._attr_native_value = snap.get(self._attr_key)
        self.async_write_ha_state()
