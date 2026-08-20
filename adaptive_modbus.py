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
from datetime import date, timedelta
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
from homeassistant.util import dt as dt_util

from .const import (
    ADAPTIVE_DECAY_FACTOR,
    ADAPTIVE_QUEUE_DEPTH_COLD_START,
    LEARNING_SETTLING_PERIOD_S,
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
    STORAGE_LOAD_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)

# HA storage version — increment when the on-disk schema changes
#: Home Assistant Store schema version.
#:
#: DO NOT BUMP without also supplying a migration callable to Store(). HA's
#: Store calls ``_async_migrate_func`` whenever the persisted version is lower
#: than the requested one, and the default implementation raises
#: NotImplementedError — which propagates out of ``async_load()`` and aborts
#: config-entry setup for the entire integration. v1.2.3 bumped this to 2 and
#: did exactly that; v1.2.4 reverts it. See _DATA_SCHEMA_VERSION below for how
#: internal payload migrations are handled instead.
_STORAGE_VERSION = 1

#: Our OWN payload schema version, stored inside the data dict and therefore
#: entirely under our control — changing it can never trip HA's migration
#: machinery.
#:
#: v2 (v1.2.3): the RTT scale changed. Before the Defect A fix, rtt_samples
#: held BATCH-summed values (the sum of every chunk in a poll); afterwards they
#: hold a single chunk's round trip. Mixing the two in one P95 list keeps the
#: old inflated values dominant for weeks and makes the fix look ineffective,
#: so v1 RTT data is discarded on upgrade. Failure/timeout counts, slot
#: identities and decay dates are scale-independent and are KEPT.
_DATA_SCHEMA_VERSION = 2
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
    # v2.0.9 FIX (Phase 3.2, this release -- ICS-08/MOD-02, both external
    # ICS audits -- confirmed): n/failures above are fed by EVERY
    # record_request() call regardless of granularity, including the
    # per-CHUNK calls from _execute_batch() (since v2.0.0a/F15) -- which
    # is architecturally correct for THEIR purpose (RTT/pacing genuinely
    # needs per-transaction granularity, confirmed by tracing the actual
    # gap-derivation formula below, which is fundamentally about spacing
    # between physical wire transactions). What was never separated is
    # CONFIDENCE, which conceptually means "how well do I understand
    # this time-slot's overall behaviour" -- a poll-level question, not
    # a transaction-level one. Field data confirms this mattered in
    # practice: n growing ~262-329x faster than genuine poll count meant
    # confidence saturated to 100% within hours, not the days of
    # observation the original design assumed. poll_n/poll_failures are
    # fed ONLY by genuine poll-level record_request() calls (the
    # default, unchunked call sites) -- see record_poll() below and
    # AdaptiveModbusController.record_request()'s own updated docstring.
    poll_n: float = 0.0
    poll_failures: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": round(self.n, 4),
            "f": round(self.failures, 4),
            "t": round(self.timeouts, 4),
            "rtt_p95": round(self.rtt_p95_ms, 1),
            "rtt_s": [round(x, 1) for x in self.rtt_samples],
            # v2.0.9 (Phase 3.2, this release): new keys, additive to the
            # existing persisted format -- old persisted data simply
            # won't have these, and from_dict() below defaults them to
            # 0.0, which is the honest, correct starting point (this
            # data genuinely didn't exist to migrate from).
            "poll_n": round(self.poll_n, 4),
            "poll_f": round(self.poll_failures, 4),
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
            poll_n=float(d.get("poll_n", 0)),
            poll_failures=float(d.get("poll_f", 0)),
        )

    def apply_decay(self, factor: float) -> None:
        """Multiply weighted counts by *factor* (daily decay)."""
        self.n *= factor
        self.failures *= factor
        self.timeouts *= factor
        # v2.0.9 (Phase 3.2, this release): decayed identically to n/
        # failures above -- same reasoning (old observations should
        # matter less over time), just for the separate poll-level
        # population.
        self.poll_n *= factor
        self.poll_failures *= factor
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

    def record_poll(self, success: bool) -> None:
        """v2.0.9 (Phase 3.2, this release): the genuine poll-level
        counterpart to record() above -- deliberately simpler (no RTT/
        timeout tracking; the transaction-level record() already owns
        that, and duplicating it here would just be the same MOD-02
        conflation in a new place). Only n/failures exist at this
        granularity, since "did the whole poll succeed" is the entire
        poll-level question confidence is meant to answer.
        """
        self.poll_n += 1.0
        if not success:
            self.poll_failures += 1.0

    @property
    def failure_rate(self) -> float:
        return self.failures / self.n if self.n >= 1.0 else 0.0

    @property
    def confidence(self) -> float:
        """0.0 → 1.0 based on how many weighted TRANSACTION-level (not
        poll-level) requests this slot has seen.

        v2.0.9 (Phase 3.2, this release): kept unchanged in meaning and
        computation -- still transaction-based, still exactly what it
        always was -- because the RTT-pacing formulas that read this
        historically (see AdaptiveModbusController's own gap/timeout
        derivation) are genuinely transaction-level concepts and
        continuing to blend against transaction-level confidence there
        is correct, not a bug. poll_confidence below is the NEW,
        separate signal for genuinely poll-level maturity -- see its own
        docstring for where that's actually used instead.
        """
        return min(self.n / ADAPTIVE_FULL_CONFIDENCE_N, 1.0)

    @property
    def poll_confidence(self) -> float:
        """v2.0.9 (Phase 3.2, this release -- ICS-08/MOD-02, both
        external ICS audits -- confirmed): the genuinely poll-level
        confidence signal MOD-02 asked for -- 0.0 -> 1.0 based on how
        many weighted POLLS (not transactions/chunks) this slot has
        seen. Uses the SAME ADAPTIVE_FULL_CONFIDENCE_N threshold as
        confidence above -- no separately-tuned constant invented here,
        since there's no field evidence yet for what a poll-specific
        threshold should be; reusing the existing, already-reasoned
        value is more honest than guessing a new one.
        """
        return min(self.poll_n / ADAPTIVE_FULL_CONFIDENCE_N, 1.0)

    @property
    def poll_failure_rate(self) -> float:
        """v2.0.9 (Phase 3.2, this release): poll-level counterpart to
        failure_rate above -- "what fraction of whole POLLS failed",
        distinct from failure_rate's "what fraction of individual
        transactions/chunks failed". These can differ substantially: a
        poll with 8 chunks where 1 chunk failed is one poll-level
        failure but only a 12.5% transaction-level failure rate for
        that poll.
        """
        return self.poll_failures / self.poll_n if self.poll_n >= 1.0 else 0.0

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
    # v2.0.9 (Phase 3.2, this release -- ICS-08/MOD-02, both external ICS
    # audits -- confirmed): the genuinely poll-level confidence signal,
    # separate from confidence above (transaction-level) -- see
    # TimeSlotStats.poll_confidence's own docstring for the full
    # reasoning, and _derive_params()'s own comment for exactly which
    # blend this drives (poll_interval only, not gap/timeout/queue_depth).
    poll_confidence: float = 0.0


# ── Controller ─────────────────────────────────────────────────────────────────

class AdaptiveModbusController:
    """Per-inverter circadian Modbus learning controller.

    Lifecycle
    ---------
    1. ``AdaptiveModbusController(hass, serial_number, device_info)``
    2. ``await controller.async_load()``  — must be called before first poll
    3. Coordinator calls ``get_params()`` at the start of each poll cycle.
    4. Coordinator calls ``record_request(rtt_ms, success, timeout)`` per
       CHUNK now (v2.0.0a, F15, external ICS audit -- confirmed;
       record_request()'s own docstring says "one completed Modbus
       request", which is per-transaction, not per-poll -- the old
       call-once-per-poll-with-the-worst-chunk's-RTT usage was the actual
       misuse of this function). ``note_batch()`` stays genuinely
       poll-level (n, failures, confidence, decay tuned against a
       per-poll rate) and is unaffected.
    5. Coordinator calls ``notify_transition()`` on detected state changes.
    6. ``controller.stop()`` on integration unload.

    v2.0.0a (F14, external ICS audit -- DELIBERATELY DEFERRED, not fixed
    in this pass): this learner is fed from the main coordinator's own
    polling only. Writes, config-flow discovery/validation, static
    startup reads, and keep-alive probes all now route through the same
    ModbusGuard (v2.0.0a's F01/F05/F06/F08 fixes) but do NOT feed this
    learner -- the audit's own framing is correct that a shared bus
    policy is therefore tuned from a partial view of the bus, not the
    complete one. Not fixed here because it is a materially different,
    larger scope than everything else in this remediation pass: it would
    mean touching every one of those newly-guarded call sites again to
    also report into this learner, deciding how to weight fundamentally
    different traffic types (a write's RTT characteristics are not
    obviously comparable to a read's), and doing so without destabilising
    a learning model that has been tuned and field-validated against real
    data across many prior sessions. Deserves its own dedicated design
    pass, the same treatment already given to other deliberately deferred
    items this session (battery-health polling cadence, §8.3;
    synchronized-power cadence realignment, F10).
    """

    _registry: dict[str, "AdaptiveModbusController"] = {}

    #: v1.2.2 - LEARNING GATE.
    #:
    #: record_request() cannot tell WHY a request was slow or failed. An RTT
    #: inflated by Home Assistant's own event-loop congestion is recorded
    #: identically to a genuinely slow inverter, which violates this module's
    #: founding premise that failure patterns reflect INVERTER state.
    #:
    #: Two exposures motivated the gate:
    #:
    #: 1. HA startup/shutdown. Congestion produces spurious failures. Restarts
    #:    are not uniformly distributed in time (scheduled updates, evening
    #:    tinkering), so the same circadian slots are poisoned repeatedly.
    #:
    #: 2. Planned inverter maintenance - the larger exposure. A Huawei firmware
    #:    update makes the inverter unreachable for ~1 h: ~120 consecutive
    #:    failures spread across four 15-minute slots. Applied to a mature slot
    #:    that lifts the failure rate from ~3% to ~12%, which maps to a poll
    #:    interval of ~137 s instead of 20-30 s.
    #:
    #: Recovery is slow and asymmetric. Daily decay multiplies n AND failures
    #: by the same factor, so it lowers CONFIDENCE but NOT the failure rate -
    #: only new successful observations dilute that, and those now accrue 4-5x
    #: more slowly because polling just slowed down. A single maintenance
    #: window therefore costs weeks of degraded polling.
    #:
    #: Note the deliberate asymmetry with the battery-health engine: that one
    #: also settles after a COORDINATOR recovery, because stale register values
    #: would corrupt it. This controller does NOT, because Modbus timing is
    #: precisely what it exists to measure - a recovering link is genuine
    #: signal, not noise.

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
    def remove(cls, serial_number: str) -> None:
        """Remove a single entry from the registry (per-entry unload).

        Unlike clear_registry(), this leaves singletons belonging to other
        still-loaded config entries intact.
        """
        cls._registry.pop(serial_number, None)

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
        # v2.0.9 (Phase 2.2, this release): see notify_transition()'s own
        # comment. Deliberately not persisted -- same "most recent live
        # state" reasoning as last_batch_ms/last_chunk_count elsewhere in
        # this class; a transition reason from a prior HA session isn't
        # meaningful after a restart.
        self._last_transition_reason: str = ""
        self._last_transition_ts: float | None = None

        # Persistence
        self._store: Store = Store(
            hass,
            _STORAGE_VERSION,
            f"{DOMAIN}.adaptive.{serial_number}",
        )
        self._last_decay_date: date | None = None
        # Learning gate (v1.2.2)
        self.learning_enabled = True
        self._suppressed_until: float | None = None
        self._suppress_reason: str = ""
        self.suppressed_observations = 0
        self.settling_events = 0
        # v2.0.7 (MOD-02 telemetry, ICS quality audit -- confirmed):
        # observational only -- see record_request()'s own docstring.
        self._granularity_counts: dict[str, int] = {}
        #: Reported by the coordinator each poll (v1.2.3 instrumentation).
        self.last_batch_ms: float = 0.0
        self.last_chunk_count: int = 0
        # v2.0.9 (Phase 2.3, this release): see note_batch()'s own
        # comment. None until the first batch completes.
        self.last_batch_deadline_margin_ms: float | None = None
        self.shed_count: int = 0
        # v2.0.0b (MOD-09, external ICS audit): a separate diagnostic
        # counter from shed_count above -- see note_admission_timeout()'s
        # own docstring for why these are deliberately not conflated.
        self.admission_timeout_count: int = 0
        # Bus-level metrics pushed in by the coordinator (the guard owns them;
        # it is shared per endpoint, while this controller is per serial).
        self._bus_occupancy_pct: float = 0.0
        self._bus_wait_p95: float = 0.0
        self._bus_service_p95: float = 0.0
        self._bus_requests_waited: int = 0
        self._bus_total_wait_s: float = 0.0
        # v2.0.11 (Phase 5.2, this release): see note_bus_metrics()'s
        # own comment. None until the coordinator has pushed at least
        # one real value through -- matches ModbusGuard.bus_health_pct()'s
        # own "None until observed" semantics rather than a fake default.
        self._bus_health_pct: float | None = None
        self._first_data_date: date | None = None
        self._dirty: bool = False
        # v2.0.3 FIX (ICS-02, external ICS audit -- confirmed): a
        # generation counter closing a genuine async lost-update race in
        # _async_save() (below) -- see that method's own docstring for
        # the full mechanism. Incremented at every one of this class's
        # own _dirty=True mutation sites, alongside it -- not derived
        # automatically (e.g. via a property on _dirty itself), since
        # this project's own test fixtures widely bypass __init__ via
        # object.__new__() and then set ._dirty directly; an automatic
        # property would need _generation to already exist before that
        # direct assignment runs, which is not guaranteed for those
        # fixtures. Direct, explicit increments at each real mutation
        # site are the safer choice here.
        self._generation: int = 0
        self._save_task: asyncio.Task | None = None

        # HA sensor push
        self._listeners: list[Any] = []
        self._unsub_push: Any = None

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def async_load(self) -> None:
        """Load persisted statistics from HA storage and apply daily decay."""
        # v1.2.4: the store load itself must never be able to abort
        # config-entry setup. Adaptive learning is an OPTIONAL optimisation;
        # losing it costs tuned poll parameters, whereas an exception here
        # costs the user every entity in the integration. (v1.2.3 shipped a
        # Store version bump with no migration callable, and the resulting
        # NotImplementedError did exactly that.)
        #
        # v2.0.9 FIX (Phase 4.9, this release -- old DEF-012, external ICS
        # quality/defect/architecture audit -- confirmed): the load itself
        # had no timeout bound -- a genuinely stalled Store read (disk
        # contention, a wedged filesystem) could block THIS await, and
        # therefore config-entry setup itself (this is called directly
        # from __init__.py's own async_setup_entry), indefinitely. See
        # STORAGE_LOAD_TIMEOUT's own comment in const.py for the full
        # reasoning. A TimeoutError here is already correctly handled by
        # the existing `except Exception` below -- no new exception
        # handling needed, only the bound itself.
        try:
            raw = await asyncio.wait_for(
                self._store.async_load(), timeout=STORAGE_LOAD_TIMEOUT.total_seconds()
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "AdaptiveModbus[%s]: could not read stored learning data — "
                "continuing with defaults. Poll/gap/timeout will re-learn; no "
                "other functionality is affected.",
                self.serial_number,
            )
            raw = None
        if raw:
            try:
                self._deserialize(raw)
                # Payload-level migration (NOT HA's Store version — see
                # _STORAGE_VERSION). Absent marker == pre-v1.2.3 data.
                if int(raw.get("data_schema", 1)) < _DATA_SCHEMA_VERSION:
                    self._migrate_v1_rtt_scale()
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

        v1.3.19 NOTE (Defect V/Finding 10, independent ICS audit): despite
        this docstring's original claim, scheduling `_async_save()` via
        `async_create_task` is NOT synchronous/deterministic -- the task
        may be cancelled or simply never get a chance to run before
        teardown finishes, exactly when the system is unstable and the
        data matters most. `stop()` is kept as-is (synchronous, best-effort)
        for callers that cannot await -- e.g. the setup-failure rollback
        path (Defect U, v1.3.18), where losing a few minutes of not-yet-
        persisted learning is an acceptable trade-off for keeping cleanup
        itself simple and synchronous. For a REAL unload, where losing
        learning data is not acceptable, use `async_unload()` instead,
        which awaits the flush deterministically -- the same two-method
        split (sync `stop()` + async `async_unload()`) already used by
        `BatteryHealthManager` for exactly this reason.
        """
        if self._unsub_push:
            self._unsub_push()
            self._unsub_push = None
        if self._save_task and not self._save_task.done():
            self._save_task.cancel()
            self._save_task = None
        # Best-effort only — see async_unload() for the deterministic path.
        if self._dirty:
            self.hass.async_create_task(self._async_save())

    async def async_unload(self) -> None:
        """Deterministic teardown: cancel background tasks and, if dirty,
        AWAIT the flush to completion before returning (Defect V, v1.3.19).

        Use this instead of `stop()` wherever the caller can await and
        losing not-yet-persisted learning data is not acceptable -- a real
        entry unload/reload being the primary case. Mirrors
        `BatteryHealthManager.async_unload()`'s existing pattern in this
        codebase for the same reason.
        """
        if self._unsub_push:
            self._unsub_push()
            self._unsub_push = None
        if self._save_task and not self._save_task.done():
            self._save_task.cancel()
            self._save_task = None
        if self._dirty:
            await self._async_save()

    # ── public API ────────────────────────────────────────────────────────────

    def current_transition_reason(self) -> str | None:
        """The reason for the currently-active transition, or None if not
        currently in one.

        v2.0.9 (Phase 2.1/2.4, this release): a clean public accessor for
        BusDiagnostics attribution (see update_coordinator.py's own
        _execute_batch()) -- deliberately not a bare attribute read of
        _last_transition_reason/_in_transition from outside this class,
        matching this codebase's own established convention of exposing
        state through get_params()-style methods, not private fields.
        Distinct from _last_transition_reason itself, which persists the
        MOST RECENT reason even after the transition has expired -- this
        returns None once expired, since attributing a request to a
        transition that's no longer active would be misleading.
        """
        return self._last_transition_reason or None if self._in_transition else None

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

    def learning_active(self) -> bool:
        """True when it is safe to learn from observations."""
        if not self.learning_enabled:
            return False
        if self._suppressed_until is not None:
            if time.time() < self._suppressed_until:
                return False
            self._suppressed_until = None
            self._suppress_reason = ""
        return True

    def set_learning_enabled(self, enabled: bool) -> None:
        """Maintenance inhibit shared with the battery-health engine."""
        if enabled == self.learning_enabled:
            return
        self.learning_enabled = enabled
        self._dirty = True
        self._generation += 1  # v2.0.3 (ICS-02): see this field's own comment in __init__
        if enabled:
            self.mark_recovery("learning re-enabled")
        else:
            _LOGGER.warning(
                "AdaptiveModbus[%s]: learning DISABLED - observations are "
                "discarded and learned parameters frozen. Polling continues "
                "using the current parameters.",
                self.serial_number,
            )

    def mark_recovery(self, reason: str) -> None:
        """Suppress learning for the settling period after a disturbance."""
        self._suppressed_until = time.time() + LEARNING_SETTLING_PERIOD_S
        self._suppress_reason = reason
        self.settling_events += 1
        _LOGGER.info(
            "AdaptiveModbus[%s]: not learning for %.0f s after %s - polling "
            "continues with current parameters",
            self.serial_number, LEARNING_SETTLING_PERIOD_S, reason,
        )

    def suppress_indefinitely(self, reason: str) -> None:
        """Suppress learning until explicitly resumed (e.g. HA shutting down)."""
        self._suppressed_until = float("inf")
        self._suppress_reason = reason
        _LOGGER.debug(
            "AdaptiveModbus[%s]: learning suppressed (%s)",
            self.serial_number, reason,
        )

    def note_batch(
        self, batch_ms: float, chunk_count: int, deadline_margin_ms: float | None = None,
    ) -> None:
        """Diagnostics only — never feeds the circadian model.

        Records how long a whole poll took and how many chunks it needed, so
        the ratio between the batch total and the per-request RTT is visible
        in a sensor export rather than having to be inferred.

        v2.0.9 (Phase 2.3, this release -- today's ICS audit §23,
        Architecture doc "Priority 2 — batch deadline margin"):
        deadline_margin_ms is the remaining headroom
        (BATCH_POLL_DEADLINE - batch_ms) at the moment this batch
        completed -- confirmed against field telemetry that a real
        102.47s batch got uncomfortably close to the 120s outer bound
        (17.5s margin) without that margin ever being directly visible;
        an operator would otherwise have to infer it by subtracting
        last_batch_ms from a constant they'd need to already know.
        Optional and defaults to None so any caller not yet passing it
        (there should be exactly one, in update_coordinator.py) degrades
        cleanly rather than breaking.
        """
        self.last_batch_ms = batch_ms
        self.last_chunk_count = chunk_count
        if deadline_margin_ms is not None:
            self.last_batch_deadline_margin_ms = deadline_margin_ms

    def note_bus_metrics(
        self,
        occupancy_pct: float,
        wait_p95_ms: float,
        service_p95_ms: float,
        requests_waited: int = 0,
        total_wait_s: float = 0.0,
        bus_health_pct: float | None = None,
    ) -> None:
        """Diagnostics only — bus-level counters.

        v1.3.5: dropped coalesce_events/coalesced_registers — that mechanism
        was removed (see const.py). ``last_chunk_count`` (via note_batch) now
        reports something more useful than before: with address-aware
        chunking it is the real count of physical Modbus exchanges a poll
        needed, so a falling value is direct evidence the fix is working.

        v2.0.11 (Phase 5.2, this release): bus_health_pct is
        ModbusGuard.bus_health_pct()'s own EWMA-based admission-success
        rate -- see that method's own docstring for the full reasoning.
        Added here, not as a separate note_ method, because it's the
        same kind of thing everything else in this method already is:
        a bus-level metric pushed through for visibility, storage only,
        never consumed by this class's own parameter derivation.
        Optional/defaults to None so this stays backward compatible
        with any caller not yet passing it.
        """
        self._bus_occupancy_pct = occupancy_pct
        self._bus_wait_p95 = wait_p95_ms
        self._bus_service_p95 = service_p95_ms
        self._bus_requests_waited = requests_waited
        self._bus_total_wait_s = total_wait_s
        if bus_health_pct is not None:
            self._bus_health_pct = bus_health_pct

    def note_shed(self) -> None:
        """Diagnostics only — a request shed by the shared bus guard."""
        self.shed_count += 1

    def note_admission_timeout(self) -> None:
        """Diagnostics only — a request that waited for the bus but the
        wait itself exceeded QUEUE_WAIT_TIMEOUT before the device was
        ever contacted.

        v2.0.0b (MOD-09, external ICS audit -- confirmed). Deliberately a
        separate counter from shed_count above, not a reuse of it: SHED
        (declined immediately, queue was already full) and
        ADMISSION_TIMEOUT (admitted to wait, but the wait itself timed
        out) are genuinely different signals about bus behaviour under
        load, and the audit's own recommendation was to give this its own
        telemetry reason rather than collapse it into an existing one.
        """
        self.admission_timeout_count += 1

    def record_request(
        self, rtt_ms: float, success: bool, timeout: bool,
        granularity: str = "poll",
    ) -> None:
        """Record one completed Modbus request into the current time slot.

        Discarded outright while the learning gate is closed. Suppression is
        preferred over down-weighting: a weight would be another unvalidated
        constant, whereas "recorded or not" is directly verifiable.

        v2.0.7 (MOD-02 telemetry, ICS quality audit -- confirmed): `granularity`
        was originally purely observational -- it did NOT affect record(), the
        confidence threshold, or anything else this method already did.
        MOD-02's actual finding (confidence thresholds designed assuming
        one call per poll, but this method has been called once per
        CHUNK from _execute_batch() since v2.0.0a/F15, without a
        corresponding threshold change) was deliberately NOT decided in
        that release -- seeing the real transaction-vs-poll call-volume
        ratio from field telemetry was a precondition for deciding it
        correctly, not a decision itself.

        v2.0.9 FIX (Phase 3.2, this release -- ICS-08/MOD-02, both
        external ICS audits -- confirmed): now decided, from real field
        data (both this project's own 2.0.7 telemetry and an independent
        23h capture both showed confidence saturating to 100% within
        hours via transaction-level n). `granularity="poll"` now ALSO
        feeds TimeSlotStats.record_poll() -- a genuinely separate,
        poll-level statistic -- alongside the existing record() call,
        which keeps its original transaction-level meaning unchanged
        (see TimeSlotStats.confidence's own docstring for why that's
        correct to leave as-is, not a bug). "poll" is still the default
        so every pre-existing call site (this controller's own
        optimizer-coordinator and non-chunked failure/timeout paths)
        needs no change at all; only _execute_batch()'s own chunk-level
        call sites pass "transaction" explicitly, and those do NOT feed
        poll_n/poll_failures.
        """
        if not self.learning_active():
            self.suppressed_observations += 1
            return
        self._granularity_counts[granularity] = (
            self._granularity_counts.get(granularity, 0) + 1
        )
        slot_idx = self._current_slot_index()
        self._slots[slot_idx].record(
            rtt_ms, success, timeout, ADAPTIVE_RTT_SAMPLE_SIZE
        )
        if granularity == "poll":
            self._slots[slot_idx].record_poll(success)
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
        # v2.0.9 (Phase 2.2, this release -- ICS-13/ICS-14, both external
        # ICS audits): the reason string was previously only ever used in
        # this DEBUG log line -- never retained as state, so telemetry
        # could see THAT a transition was active but never WHY. Field
        # data already shows transition state strongly correlates with
        # degradation (19.1% vs 8.4% failure rate); knowing the reason
        # is the direct next question that correlation raises.
        self._last_transition_reason = reason or "unknown"
        self._last_transition_ts = time.monotonic()
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
        snap = self.snapshot()
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

    def snapshot(self) -> dict[str, Any]:
        """Produce a point-in-time diagnostic snapshot for sensor entities.

        v2.0.1 FIX (confirmed via a genuine production traceback, not a
        static audit finding this time -- the operator's own deployment
        surfaced this directly): this method was defined with a leading
        underscore, but its two INTERNAL callers (_push_to_listeners(),
        this class's own sensor-push mechanism, and the diagnostic
        sensor entity's async_added_to_hass(), both in this same file)
        correctly used that private name, while its EXTERNAL callers
        (diagnostics.py, telemetry_capture.py) called it as `.snapshot()`
        -- the public name every sibling class (ModbusTelemetry,
        SynchronizedPowerCoordinator) already uses for the equivalent
        method. The external callers' assumption was the correct one:
        this method exists specifically to be consumed by other modules,
        the same public role its siblings play. Renamed here to match
        what those external callers already, correctly, expected, with
        both internal call sites updated to the same public name for
        consistency -- not a partial fix that left two different names
        for the same method depending on which side of a module boundary
        called it.

        Impact before this fix: every external caller raised
        AttributeError on every single call -- in production, every ~30
        seconds once the telemetry capture switch was enabled, and on
        every attempt to use Home Assistant's own "Download Diagnostics"
        feature for this integration (diagnostics.py's own call had no
        exception guard around it at all, so this would have crashed
        that entire, unrelated, core HA feature too, not just the
        opt-in telemetry switch).

        Why the existing test suite never caught this: build_telemetry_
        snapshot() looks up the controller via
        `adaptive_controller_cls.get(serial)` (telemetry_capture.py),
        and no test in test_entities.py ever registered a real
        AdaptiveModbusController instance for its test serial via
        get_or_create() -- so that lookup always returned None, and the
        line calling .snapshot() was never reached by any test at all.
        A genuine test-coverage gap, not a mock silently swallowing a
        real failure. Closed by adding a test that actually registers a
        controller and exercises this exact path end to end.
        """
        params = self.get_params()
        slot = self._slots[params.slot_index]
        return {
            "poll_interval_s": params.poll_interval.total_seconds(),
            "gap_ms": params.request_gap.total_seconds() * 1000,
            "timeout_s": params.request_timeout.total_seconds(),
            "max_queue_depth": params.max_queue_depth,
            "confidence_pct": round(params.confidence * 100, 1),
            # v2.0.9 (Phase 3.2, this release -- ICS-08/MOD-02, both
            # external ICS audits -- confirmed): exposed separately and
            # explicitly, not just internally -- a capture must be able
            # to distinguish these two numbers directly, since they can
            # now genuinely diverge (confidence_pct saturating fast from
            # per-chunk observations while poll_confidence_pct climbs
            # much more slowly from genuine poll counts is the expected,
            # correct behaviour after this fix, not a discrepancy to
            # investigate).
            "poll_confidence_pct": round(params.poll_confidence * 100, 1),
            "slot_poll_requests": round(slot.poll_n, 1),
            "slot_failure_rate_pct": round(params.slot_failure_rate * 100, 2),
            "in_transition": "ON" if params.in_transition else "OFF",
            # v2.0.9 (Phase 2.2, this release -- ICS-13/ICS-14, both
            # external ICS audits): see notify_transition()'s own
            # comment for the full reasoning. None/None when no
            # transition has occurred this instance's lifetime.
            "last_transition_reason": self._last_transition_reason or None,
            "last_transition_ts": self._last_transition_ts,
            "days_of_data": self.days_of_data,
            "current_slot": self._slot_label(params.slot_index),
            "slot_requests": round(slot.n, 1),
            # v1.2.3 instrumentation. rtt_p95_ms previously had to be
            # back-solved from saturated gap/timeout values during incident
            # analysis; exposing it (and the chunk count that inflated it)
            # makes the Defect A fix directly verifiable from a sensor export.
            "rtt_p95_ms": round(slot.rtt_p95_ms, 1),
            "last_batch_ms": round(self.last_batch_ms, 1),
            "last_chunk_count": self.last_chunk_count,
            # v2.0.9 (Phase 2.3, this release): see note_batch()'s own
            # comment for the full reasoning.
            "last_batch_deadline_margin_ms": (
                round(self.last_batch_deadline_margin_ms, 1)
                if self.last_batch_deadline_margin_ms is not None else None
            ),
            "shed_count": self.shed_count,
            # v2.0.0b (MOD-09, external ICS audit): exposed alongside
            # shed_count for the same diagnostic-visibility reason.
            "admission_timeout_count": self.admission_timeout_count,
            "bus_occupancy_pct": round(self._bus_occupancy_pct, 1),
            "bus_wait_p95_ms": round(self._bus_wait_p95, 1),
            "bus_service_p95_ms": round(self._bus_service_p95, 1),
            "bus_requests_waited": self._bus_requests_waited,
            "bus_total_wait_s": round(self._bus_total_wait_s, 1),
            # v2.0.11 (Phase 5.2, this release): see note_bus_metrics()'s
            # own comment for the full reasoning -- deliberately
            # separate from confidence_pct/poll_confidence_pct above,
            # which are both device-level signals; this one is purely
            # bus-level.
            "bus_health_pct": self._bus_health_pct,
            # v1.2.2 learning gate visibility
            "learning_enabled": self.learning_enabled,
            "learning_active": self.learning_active(),
            "suppressed_observations": self.suppressed_observations,
            "settling_events": self.settling_events,
            "suppress_reason": self._suppress_reason or None,
            # v2.0.7 (MOD-02 telemetry, ICS quality audit -- confirmed):
            # the real transaction-vs-poll call-volume split, needed to
            # decide MOD-02's deferred confidence-threshold question
            # from field data -- see record_request()'s own docstring.
            "transaction_level_requests": self._granularity_counts.get("transaction", 0),
            "poll_level_requests": self._granularity_counts.get("poll", 0),
        }

    # ── properties ────────────────────────────────────────────────────────────

    @property
    def days_of_data(self) -> int:
        if self._first_data_date is None:
            return 0
        # BUG-7 FIX: clamp to 0 so clock-skew/drift never returns a negative value
        # v2.0.13 FIX (MOD-022, external ICS quality/defect/architecture
        # audit -- confirmed): date.today() used the HOST's own naive
        # local date, not Home Assistant's own configured timezone --
        # see _current_slot_index()'s own comment for the full
        # reasoning, identical here.
        return max(0, (dt_util.now().date() - self._first_data_date).days + 1)

    # ── internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _current_slot_index() -> int:
        """Map the current wall-clock time to a slot index (0–95).

        v2.0.13 FIX (MOD-022, external ICS quality/defect/architecture
        audit -- confirmed): previously used datetime.now() -- naive,
        HOST-local wall-clock time, not Home Assistant's own configured
        timezone. This controller's whole architecture is explicitly
        circadian (learning conditions around sunrise, midday, sunset,
        night transitions); if the host OS timezone doesn't match HA's
        own configured timezone (common in a containerized/cloud
        deployment defaulting to UTC regardless of the user's actual
        location), every learned slot would be shifted by the timezone
        offset, applying parameters learned for one local operating
        period to a different one. dt_util.now() returns HA's own
        configured local time, exactly the "local civil time" this
        model is meant to key off of.
        """
        now = dt_util.now()
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
        # v2.0.9 FIX (Phase 3.2, this release -- ICS-08/MOD-02, both
        # external ICS audits -- confirmed): poll_interval below is the
        # ONE parameter among these four that is genuinely a poll-level
        # decision ("how often should a whole new poll begin"), not a
        # transaction-pacing one -- gap_ms/timeout_s/max_queue_depth
        # further down are all legitimately about individual physical
        # transactions (spacing between them, waiting for one response,
        # how many may be outstanding) and correctly continue blending
        # against the transaction-based `confidence` above unchanged.
        # Confirmed by tracing each formula's own actual meaning, not a
        # blanket replacement -- see TimeSlotStats.poll_confidence's own
        # docstring for the full reasoning.
        poll_confidence = slot.poll_confidence
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
        # v2.0.9 FIX (Phase 3.2, this release): poll_confidence, not
        # confidence -- this blend decides how much to trust THIS
        # SLOT'S OWN LEARNED POLL CADENCE, which should mature based on
        # how many actual polls (not chunks) have been observed. Before
        # this fix, a slot with thousands of chunk-level observations
        # but only a handful of genuine polls reached full confidence
        # here almost immediately, exactly the field-observed symptom
        # both audits independently flagged.
        poll_s = poll_confidence * poll_s_derived + (1 - poll_confidence) * poll_s_baseline
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
        # DEFECT B (v1.2.3): this was the only one of the four outputs with no
        # confidence blending. Because TimeSlotStats.failure_rate returns 0.0
        # for an unseen slot (n < 1), a slot with ZERO observations fell
        # straight through to the MOST permissive value (3) — the exact
        # opposite of the cautious cold-start posture the other three
        # mechanisms adopt, and precisely the case the blending exists for.
        if fr >= ADAPTIVE_FAILURE_RATE_HIGH or in_transition:
            depth_derived = 1
        elif fr >= ADAPTIVE_FAILURE_RATE_LOW:
            depth_derived = 2
        else:
            depth_derived = 3
        depth_blended = (
            confidence * depth_derived
            + (1 - confidence) * ADAPTIVE_QUEUE_DEPTH_COLD_START
        )
        max_queue_depth = max(1, int(round(depth_blended)))

        return AdaptiveParams(
            poll_interval=poll_interval,
            request_gap=request_gap,
            request_timeout=request_timeout,
            max_queue_depth=max_queue_depth,
            confidence=confidence,
            in_transition=in_transition,
            slot_index=slot_idx,
            slot_failure_rate=raw_fr,
            poll_confidence=poll_confidence,
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

        v2.0.3 (ICS-02): this is the primary, most-used entry point most
        mutations actually go through -- also bumps _generation, closing
        a DIFFERENT, later race than BUG-010 above solved: even with
        _dirty correctly seen as True when a save wakes, _async_save()
        itself used to unconditionally clear _dirty back to False once
        its own already-in-flight save completed, silently discarding
        any mutation that arrived specifically DURING that save's own
        await window (see _async_save()'s own docstring for the full
        mechanism this counter closes).
        """
        self._dirty = True          # mark dirty unconditionally
        self._generation += 1
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
        # v2.0.0a (F16, external ICS audit -- confirmed): _dirty used to be
        # cleared BEFORE the actual persistence call, so a failed save
        # (storage backend error, disk full, etc.) would leave _dirty=False
        # even though nothing was actually written -- silently losing the
        # "this still needs saving" indication. Cleared only after
        # async_save() has genuinely succeeded; a raised exception now
        # leaves _dirty=True, exactly as it should, and propagates to the
        # caller (already handled at the async_unload() call site in
        # __init__.py's own teardown sequence).
        #
        # v2.0.3 FIX (ICS-02, external ICS audit -- confirmed): a
        # SEPARATE, later race from F16's above -- this one happens even
        # when the save itself succeeds. self._serialize() snapshots
        # state synchronously, before the `await` below suspends; while
        # suspended, another poll can mutate state and call
        # _schedule_save() (setting _dirty=True again, correctly, per
        # BUG-010). The old code then unconditionally set _dirty=False
        # once ITS OWN already-in-flight save completed -- silently
        # overwriting that fresh True with False, even though the new
        # mutation was never included in the snapshot just written. The
        # new state stayed correct in memory, but was never marked as
        # needing to be saved -- if no FURTHER mutation happened before
        # the next restart, it would simply be lost from disk with no
        # exception, no log line, nothing to indicate anything went
        # wrong.
        #
        # Fixed with a generation counter (self._generation, bumped
        # alongside every self._dirty=True mutation site in this class --
        # see that field's own comment in __init__): captured here
        # before the await, and only treated as clean if the generation
        # is still the same one this specific save's snapshot reflects.
        # If a new mutation raced in during the await, loop and save
        # again immediately -- rather than calling self._schedule_save()
        # (which would silently no-op here: this coroutine IS the
        # in-flight save _schedule_save() checks for, and it is not yet
        # "done" from its own perspective while still running) -- so the
        # newest state is captured without waiting on some unrelated
        # future mutation to eventually trigger a save cycle that
        # includes it.
        if self._first_data_date is None:
            # v2.0.13 FIX (MOD-022, this release): see _current_slot_
            # index()'s own comment for the full reasoning, identical
            # here.
            self._first_data_date = dt_util.now().date()
        while True:
            save_generation = self._generation
            await self._store.async_save(self._serialize())
            if self._generation == save_generation:
                self._dirty = False
                break
            _LOGGER.debug(
                "AdaptiveModbus[%s]: state mutated during save (generation "
                "%d -> %d); saving again immediately to capture it",
                self.serial_number, save_generation, self._generation,
            )
        _LOGGER.debug(
            "AdaptiveModbus[%s]: statistics persisted (%d days of data)",
            self.serial_number,
            self.days_of_data,
        )

    def _serialize(self) -> dict[str, Any]:
        # v2.0.13 FIX (MOD-022, this release): computed once and reused
        # below for both fallbacks -- see _current_slot_index()'s own
        # comment for the full reasoning on why this is dt_util.now().
        # date() rather than date.today().
        _today = dt_util.now().date()
        return {
            "version": _STORAGE_VERSION,
            "data_schema": _DATA_SCHEMA_VERSION,
            "serial": self.serial_number,
            "last_decay_date": (self._last_decay_date or _today).isoformat(),
            "learning_enabled": self.learning_enabled,
            "suppressed_observations": self.suppressed_observations,
            "settling_events": self.settling_events,
            "first_data_date": (self._first_data_date or _today).isoformat(),
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
        self.learning_enabled = bool(raw.get("learning_enabled", True))
        self.suppressed_observations = int(raw.get("suppressed_observations", 0))
        self.settling_events = int(raw.get("settling_events", 0))
        last_str = raw.get("last_decay_date")
        self._last_decay_date = date.fromisoformat(last_str) if last_str else None
        first_str = raw.get("first_data_date")
        self._first_data_date = date.fromisoformat(first_str) if first_str else None

    def _reset_slots(self) -> None:
        self._slots = [TimeSlotStats(slot_index=i) for i in range(ADAPTIVE_SLOT_COUNT)]

    def _migrate_v1_rtt_scale(self) -> None:
        """Discard v1 RTT samples — they are on a different scale (Defect A).

        v1 stored the SUM of every chunk's round trip in a poll. v2 stores one
        chunk's round trip. A P95 computed over a mixture is meaningless, and
        because the list is FIFO-trimmed rather than time-windowed, the old
        inflated values would dominate for a long time.

        Deliberately narrow: only rtt_samples/rtt_p95_ms are cleared. The
        failure and timeout counts, slot occupancy and decay bookkeeping are
        independent of the RTT scale and represent months of learning, so they
        are preserved.
        """
        cleared = 0
        for slot in self._slots:
            if slot.rtt_samples or slot.rtt_p95_ms:
                cleared += 1
            slot.rtt_samples = []
            slot.rtt_p95_ms = 0.0
        self._dirty = True
        self._generation += 1  # v2.0.3 (ICS-02): see this field's own comment in __init__
        _LOGGER.warning(
            "AdaptiveModbus[%s]: storage upgraded v1 -> v2. Cleared RTT "
            "samples from %d time slots because they were recorded on the "
            "pre-v1.2.3 batch-summed scale; failure-rate history is kept. "
            "Gap and timeout will re-learn over the next few days.",
            self.serial_number, cleared,
        )

    def _apply_startup_decay(self) -> None:
        """Apply accumulated daily decay since the last save."""
        # v2.0.13 FIX (MOD-022, this release): see _current_slot_
        # index()'s own comment for the full reasoning.
        today = dt_util.now().date()
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
        self._generation += 1  # v2.0.3 (ICS-02): see this field's own comment in __init__


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
    # ── v1.3.0 Phase 0 ───────────────────────────────────────────────────────
    # v1.2.3 added these keys to _snapshot() but never gave them sensor
    # definitions, and this module has no extra_state_attributes anywhere — so
    # they were unreachable. Diagnosing the gap/timeout ceiling required
    # BACK-SOLVING rtt_p95_ms from saturation thresholds. That should not be
    # necessary twice.
    ("rtt_p95_ms",             "Adaptive RTT p95",             "ms",  "mdi:speedometer"),
    ("last_chunk_count",       "Adaptive last chunk count",    None,  "mdi:format-list-numbered"),
    ("last_batch_ms",          "Adaptive last batch time",     "ms",  "mdi:timer-outline"),
    ("shed_count",             "Adaptive shed requests",       None,  "mdi:call-split"),
    # Bus-level metrics: the same physical line, reported per controller.
    # bus_occupancy_pct is the FEEDFORWARD signal the scheduler will pace from.
    # bus_wait_p95_ms vs bus_service_p95_ms is THE Phase 0 measurement:
    # wait-dominated => queueing (a scheduler helps); service-dominated =>
    # the device itself is slow (only demand reduction helps).
    ("bus_occupancy_pct",      "Bus occupancy",                "%",   "mdi:gauge"),
    ("bus_wait_p95_ms",        "Bus wait p95",                 "ms",  "mdi:timer-sand"),
    ("bus_service_p95_ms",     "Bus service p95",              "ms",  "mdi:transmission-tower"),
    # (item 2) Did tier separation actually reduce queueing? Measured, not assumed.
    ("bus_requests_waited",    "Bus requests delayed",         None,  "mdi:timer-alert-outline"),
    ("bus_total_wait_s",       "Bus total wait",               "s",   "mdi:timer-sand-full"),
    # (item 1) Is coalescing firing, and how much is it pulling forward?
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
        snap = self._controller.snapshot()
        self._attr_native_value = snap.get(self._attr_key)

    async def async_will_remove_from_hass(self) -> None:
        self._controller.remove_listener(self._on_update)

    @callback
    def _on_update(self, snap: dict[str, Any]) -> None:
        self._attr_native_value = snap.get(self._attr_key)
        self.async_write_ha_state()
