"""Modbus traffic guard for Huawei Solar.

The SUN2000 inverter's Modbus interface is single-threaded and will return
error codes (or silently drop responses) when two requests overlap or arrive
faster than it can handle.

ModbusGuard provides per-bus serialisation and rate-limiting:
1. Only ONE Modbus request is in-flight at a time per physical RS485 bus.
2. A minimum inter-request gap is respected.  Adaptive via update_gap().
3. Requests that arrive when _max_queue_depth callers are already waiting
   are shed immediately (fail-fast).  Adaptive via update_max_queue_depth().

Bus-level keying (v1.0.5)
--------------------------
Guards are keyed on ``connection_endpoint`` (host:port string, or the serial
port path for RTU) rather than ``serial_number``.  All inverters sharing the
same physical RS485 wire (i.e., sub-devices created via
``create_sub_device_instance``) therefore share one guard, preventing concurrent
requests that would cause RS485 bus collisions and are the primary cause of the
secondary inverter's elevated failure rate.

Usage (in coordinators)
-----------------------
    endpoint = ModbusGuard.endpoint_for(entry.data)
    guard = ModbusGuard.get_or_create(endpoint)
    guard.update_gap(device.serial_number, params.request_gap.total_seconds())
    guard.update_max_queue_depth(device.serial_number, params.max_queue_depth)

    async with guard.request():
        result = await device.batch_update(names)

Multi-device aggregation (v1.3.15)
-----------------------------------
update_gap()/update_max_queue_depth() take the reporting device's serial
number as *source* and track every current contributor's clamped value
separately. The guard's effective gap/depth is the SAFEST option across
all contributors (widest gap, shallowest queue depth) rather than whichever
device happened to report last -- see Defect P in AUDIT_1.3.15.md for the
full history. Call remove_source(serial_number) when a device's coordinator
unloads so it stops influencing the aggregate.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import timedelta
from typing import Any

_LOGGER = logging.getLogger(__name__)

MIN_INTER_REQUEST_GAP = timedelta(milliseconds=150)
QUEUE_WAIT_TIMEOUT = timedelta(seconds=10)


class ModbusQueueShed(asyncio.TimeoutError):
    """Raised when a request is shed because the guard's queue is full.

    DEFECT D (v1.2.3) — this used to be a bare ``asyncio.TimeoutError``, which
    the coordinator's error handling could not distinguish from an inverter
    that failed to answer.  A shed request therefore reached the adaptive
    learner as ``record_request(success=False, timeout=True)`` — i.e. purely
    internal contention between our own sub-coordinators was recorded as
    *inverter* misbehaviour, in the same circadian slot model that drives poll
    interval, gap and timeout.

    Why that mattered more than it looks: shedding gets *more* likely as
    ``max_queue_depth`` drops, and ``max_queue_depth`` drops as the failure
    rate rises.  Recording sheds as failures therefore closed a positive
    feedback loop — shed → recorded failure → higher failure rate → lower
    queue depth → more shedding — which would have been triggered by the very
    cold-start blending introduced to make unproven slots *safer*.

    Subclassing ``asyncio.TimeoutError`` preserves every existing
    ``except asyncio.TimeoutError`` path (back-off, cache fallback, entity
    availability); only the adaptive-learning bookkeeping treats it specially.
    """


class ModbusAdmissionTimeout(asyncio.TimeoutError):
    """Raised when a request waited for the bus (past QUEUE_WAIT_TIMEOUT)
    but never got the chance to actually talk to the device.

    v2.0.0a (F08, external ICS audit -- confirmed). Same reasoning as
    ModbusQueueShed above, for the OTHER congestion outcome: a request that
    doesn't get shed immediately (queue wasn't full) can still wait long
    enough for the lock/inter-request-gap acquisition itself to time out --
    this used to raise a bare ``asyncio.TimeoutError``, indistinguishable
    by type from the device itself failing to respond. The distinction
    matters most for ModbusKeepAlive: a single ``except TimeoutError``
    there previously treated "the bus was busy for 10+ seconds" and "the
    device didn't answer within its own 20s budget" identically, both
    triggering on_connection_lost() -- meaning ordinary bus congestion
    (exactly the condition the rest of this integration's back-off/guard
    machinery is built to handle gracefully) could manufacture a false
    "connection lost" event and an unnecessary full cache invalidation.

    Subclassing ``asyncio.TimeoutError`` for the same reason as
    ModbusQueueShed: every existing ``except TimeoutError`` path keeps
    working exactly as before for anything that doesn't specifically care
    about this distinction; only ModbusKeepAlive's own handling needs to
    (and now does) treat it specially.
    """
MAX_QUEUE_DEPTH = 3

# v2.0.11 (Phase 5.2, this release): per-event EWMA decay factor for the
# bus-health admission signal (ModbusGuard._bus_admission_ewma_n/
# _bus_admission_ewma_failures). Chosen so the signal's effective memory
# spans roughly the last 30-70 admission attempts -- a few minutes to
# ~10 minutes of recent history at this system's own typical admission
# rate (confirmed against real field telemetry: ~11 requests/minute
# across both devices combined) -- responsive to genuinely current bus
# conditions without being jumpy on any single event. A judgment call,
# not a derived constant; flag if a different responsiveness is wanted
# once this has been observed running for a while.
BUS_HEALTH_EWMA_DECAY = 0.98

# v2.0.0a (F18, external ICS audit -- confirmed): the priority lane's own
# bound, independent of MAX_QUEUE_DEPTH above. Deliberately small --
# realistically at most a handful of independent keep-alive tasks could
# ever share one physical endpoint (one per device on that bus, and most
# installations have one or two devices total), so this is not meant to
# accommodate genuine concurrent load the way the normal queue depth is;
# it exists purely so priority admission has SOME ceiling rather than
# none, catching a genuinely pathological pile-up (e.g. a bug elsewhere
# repeatedly spawning priority requests) rather than tuning for expected
# normal operation.
MAX_PRIORITY_QUEUE_DEPTH = 2

# v2.0.0b (AR-4, external ICS audit): the priority lane's own airtime
# budget -- see _priority_window_start/_priority_busy_s's own comment in
# __init__ for why this is a distinct mechanism from MAX_PRIORITY_QUEUE_
# DEPTH above. Both values are the report's own explicitly-stated
# starting point ("P = 20%, T = 10 seconds, both field-tunable"), not
# independently re-derived here -- only one priority producer (keep-alive)
# exists in this codebase today, so there is no field data yet showing
# these specific numbers need to be different; they exist as a genuine,
# working ceiling from day one rather than an unbounded right, with room
# to be tuned later against real multi-producer field data.
PRIORITY_AIRTIME_BUDGET_FRACTION: float = 0.20
PRIORITY_AIRTIME_WINDOW_S: float = 10.0


class ModbusGuard:
    """Per-bus asyncio serialiser and rate-limiter with adaptive parameters."""

    # Key: connection_endpoint string → ModbusGuard instance
    _registry: dict[str, "ModbusGuard"] = {}

    # v2.0.0a (F04, external ICS audit -- confirmed): the registry used to
    # be removed unconditionally on a single config entry's unload, with no
    # awareness of whether other entries (or, from this release, config-flow
    # sessions) still shared the same physical endpoint. A surviving entry
    # keeps working fine immediately after (it already holds its own
    # reference to the old guard object) -- the real failure surfaces
    # LATER, the next time anything on that endpoint calls acquire_endpoint()
    # again (e.g. a third entry loading, or a reload): finding nothing in
    # the registry, it creates a SECOND, independent ModbusGuard for one
    # physical bus, silently breaking the serialisation guarantee between
    # whichever coordinators ended up on each of the two objects.
    #
    # Fixed with entry-level (not coordinator-level) reference counting.
    # The unit is deliberately the ENTRY (or config-flow session), not each
    # individual coordinator -- multiple coordinators within one entry
    # (main, power_meter, energy_storage, configuration, sync-power) all
    # share one endpoint and are constructed/torn down together as a unit,
    # so counting at that finer grain would be needless bookkeeping for no
    # extra correctness. acquire_endpoint()/release_endpoint() are the
    # entry/flow-level lifecycle calls; get_or_create() remains available
    # as the plain "get me the object" accessor coordinators already use --
    # by the time any coordinator calls it, its entry has already called
    # acquire_endpoint() first, so the object is guaranteed to exist.
    _ref_counts: dict[str, int] = {}

    # ── class helpers ─────────────────────────────────────────────────────────

    @classmethod
    def endpoint_for(cls, entry_data: dict) -> str:
        """Derive the connection-endpoint key from a config-entry data dict.

        For TCP connections: ``"host:port"``
        For RTU connections: ``"rtu:<port>"``

        This key is the same for all slave IDs on the same physical bus, so all
        sub-device coordinators get the same guard instance.
        """
        host = entry_data.get("host")
        port = entry_data.get("port", "502")
        if host is None:
            return f"rtu:{port}"
        return f"{host}:{port}"

    @classmethod
    def get_or_create(cls, endpoint: str) -> "ModbusGuard":
        """Return the guard object for *endpoint*, creating it if absent.

        Does NOT itself affect the reference count -- callers that own the
        endpoint's lifecycle (a config entry's setup, or a config-flow
        session) must bracket their own usage with
        acquire_endpoint()/release_endpoint() instead. Individual
        coordinators within an already-acquired entry can keep calling this
        as a plain accessor, exactly as before.
        """
        if endpoint not in cls._registry:
            cls._registry[endpoint] = cls(endpoint)
        return cls._registry[endpoint]

    @classmethod
    def acquire_endpoint(cls, endpoint: str) -> "ModbusGuard":
        """Entry/flow-level acquire: creates the guard if needed and
        increments its reference count by one. Must be paired with exactly
        one later acquire_endpoint()-matching release_endpoint() call for
        the same endpoint.
        """
        guard = cls.get_or_create(endpoint)
        cls._ref_counts[endpoint] = cls._ref_counts.get(endpoint, 0) + 1
        return guard

    @classmethod
    def release_endpoint(cls, endpoint: str) -> None:
        """Entry/flow-level release: decrements the endpoint's reference
        count, removing the guard from the registry only once the count
        reaches zero -- i.e. once every entry/flow that acquired it has
        released it. A release with no matching prior acquire (count
        already absent or at zero) is a safe no-op, not an error -- this
        keeps teardown code that runs on best-effort/exception paths from
        needing its own separate bookkeeping.
        """
        if endpoint not in cls._ref_counts:
            return
        cls._ref_counts[endpoint] -= 1
        if cls._ref_counts[endpoint] <= 0:
            cls._ref_counts.pop(endpoint, None)
            cls._registry.pop(endpoint, None)

    @classmethod
    def clear_registry(cls) -> None:
        cls._registry.clear()
        cls._ref_counts.clear()

    @classmethod
    def remove(cls, endpoint: str) -> None:
        """DEPRECATED (v2.0.0a, F04): unconditional removal, ignoring
        reference count. Retained only so any external/legacy caller does
        not hard-fail; production code should use release_endpoint()
        instead, which is reference-count-aware. Calling this directly
        will remove the guard even if other entries/flows still hold a
        reference to it -- exactly the bug F04 describes.
        """
        cls._registry.pop(endpoint, None)
        cls._ref_counts.pop(endpoint, None)

    # ── instance ──────────────────────────────────────────────────────────────

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self._lock = asyncio.Lock()
        self._last_request_end: float = 0.0
        self._queue_depth: int = 0
        self._effective_gap: float = MIN_INTER_REQUEST_GAP.total_seconds()
        self._max_queue_depth: int = MAX_QUEUE_DEPTH
        # v1.3.15 FIX (Defect P / "Defect C" from the original 2026-08-04
        # handoff): update_gap()/update_max_queue_depth() used to be plain
        # setters -- self._effective_gap = clamp(gap_seconds), overwriting
        # whatever the previous caller had set. On a bus shared by more than
        # one device (daisy-chained inverters), every device's coordinator
        # calls these every poll with ITS OWN adaptive controller's learned
        # params -- so the bus's actual operating parameters at any instant
        # were simply whichever device happened to poll most recently, not
        # a reconciled view of every device sharing the bus. A device having
        # a rough patch could have its own (correctly conservative) gap
        # silently overwritten moments later by another device's more
        # aggressive setting, and vice versa.
        #
        # Fixed by tracking each contributing source's clamped value
        # separately and deriving the aggregate as the SAFEST option across
        # all current contributors: the widest gap (max) and the shallowest
        # queue depth (min). A device that needs more caution now makes the
        # whole shared bus more cautious, rather than being overridden by a
        # sibling device's more optimistic view. Sources are removed via
        # remove_source() when their coordinator's entry unloads, so a
        # torn-down device's learned parameters cannot permanently pin the
        # aggregate after it's gone.
        self._gap_contributions: dict[str, float] = {}
        self._depth_contributions: dict[str, int] = {}
        #: Diagnostic counter (v1.2.3): how many requests this guard has shed.
        #: Exposed so internal contention is observable rather than silently
        #: mixed into the inverter's failure statistics.
        self.shed_count: int = 0
        # v2.0.11 (Phase 5.2, this release -- separate device-health from
        # bus-health learning models): a genuine, decaying BUS-level
        # health signal, deliberately kept observational only -- it does
        # NOT alter admission/scheduling behaviour (that's Phase 5.1's
        # own, deliberately deferred scope). Confirmed before building
        # this that the device-health signal (AdaptiveModbusController's
        # own TimeSlotStats.failure_rate/confidence) was ALREADY cleanly
        # isolated from bus congestion -- record_request() is only ever
        # called for requests that were genuinely admitted and got a
        # real device-level outcome; shed/admission-timeout events route
        # to note_shed()/note_admission_timeout() instead, explicitly
        # marked "diagnostics only". What was actually missing was the
        # OTHER half: a bus-health signal with the SAME rigor (a real,
        # recency-weighted rate, not just a lifetime counter that
        # becomes less sensitive to current conditions the longer the
        # bus has been running) -- shed_count/priority_shed_count above
        # are lifetime totals, not a rate, and were never fed into
        # anything. EWMA-decayed, matching this project's own
        # established pattern (ADAPTIVE_DECAY_FACTOR) rather than a
        # fixed-window rolling average, which would need its own
        # separate history buffer.
        self._bus_admission_ewma_n: float = 0.0
        self._bus_admission_ewma_failures: float = 0.0
        self.admission_timeout_count: int = 0
        # v2.0.0a (F18, external ICS audit -- confirmed): priority=True
        # requests (keep-alive) bypass the normal queue-depth shedding
        # check entirely -- correct, that's the whole point of priority --
        # but that ALSO meant they had NO upper bound of their own. The
        # normal queue-depth limit is not a true hard ceiling once
        # priority producers exist: multiple priority requests could pile
        # up waiting, unbounded, even while the normal queue is already at
        # its own limit. Only one priority producer (keep-alive) exists in
        # this codebase today, but a bus shared by multiple devices means
        # multiple independent keep-alive tasks CAN legitimately overlap
        # on one endpoint -- this is a real, reachable case, not a
        # hypothetical one. Tracked as a SEPARATE counter from
        # _queue_depth (which priority requests still increment/decrement,
        # for occupancy/diagnostics purposes) -- this one exists purely to
        # give priority admission its own, independent, bounded lane.
        self._priority_queue_depth: int = 0
        #: Diagnostic counter, mirroring shed_count but for the priority
        #: lane specifically -- a priority shed is a materially different
        #: signal (keep-alive itself is being starved) from a normal one.
        self.priority_shed_count: int = 0
        # v2.0.0b (AR-4, external ICS audit): the priority lane's own
        # AIRTIME budget, distinct from MOD-18's queue-DEPTH bound above.
        # Depth bounds how many priority requests can be simultaneously
        # admitted/waiting; this bounds what FRACTION of the bus's own
        # occupied time, within a rolling window, priority traffic may
        # consume -- closing the report's own framing: "priority should
        # mean gets admitted ahead of ordinary work, not can consume
        # arbitrary bus capacity whenever it wants." A self-contained
        # rolling window, deliberately not sharing occupancy()'s own
        # _busy_s/_window_start pair above -- that window's reset cadence
        # is controlled by whatever external caller passes reset=True to
        # occupancy(), which may not roll forward on any particular
        # schedule; this budget needs a predictable, self-managed window
        # to mean anything as a genuine per-window cap.
        self._priority_window_start: float = time.monotonic()
        self._priority_busy_s: float = 0.0
        #: Diagnostic counter: how many priority requests were admitted
        #: under NORMAL-lane fairness rules (i.e. subject to the ordinary
        #: queue-depth shed check) because the priority airtime budget for
        #: the current window was already exhausted.
        self.priority_budget_exceeded_count: int = 0
        # ── v1.3.0 Phase 0 instrumentation ───────────────────────────────────
        #: Wall-clock time this guard has spent holding the line, and the
        #: window it was measured over. Occupancy = busy / elapsed is the
        #: FEEDFORWARD signal the scheduler will eventually pace from: unlike
        #: failure rate it leads the problem instead of lagging it, and unlike
        #: request count it reflects how long the line is actually tied up.
        self._busy_s: float = 0.0
        self._window_start: float = time.monotonic()
        #: Rolling separation of the two costs. This distinction is the whole
        #: point of Phase 0: long WAIT means requests queue behind each other
        #: (our scheduling); long SERVICE means the device itself is slow
        #: (the master's CPU). The field data cannot tell these apart.
        self._wait_samples: deque[float] = deque(maxlen=256)
        self._service_samples: deque[float] = deque(maxlen=256)
        #: (item 2, v1.3.4) Cumulative queueing cost. The Phase 0 capture showed
        #: 291 requests waiting >1 s for a total of 1,362 s — the main
        #: justification for tier separation. Tracking it makes the effect
        #: measurable instead of assumed.
        self.total_wait_ms: float = 0.0
        self.requests_waited: int = 0
        #: Optional sink for per-request records (bus_diagnostics.BusDiagnostics).
        self.diagnostics: Any | None = None

    # ── adaptive parameter setters ────────────────────────────────────────────

    def update_gap(self, source: str, gap_seconds: float) -> None:
        """Report *source*'s (a device serial number) learned inter-request
        gap. Clamped to [150 ms, 500 ms] as before, but no longer a plain
        overwrite: the guard's effective gap is the MAXIMUM (safest, widest)
        across every device currently sharing this bus, so one device's
        conservative learning cannot be silently undone by a sibling
        device's more optimistic one (Defect P).

        The 150 ms floor is a hardware constraint (SUN2000 Modbus FSM reset
        time ≈ 100 ms) and is never reduced regardless of network health.
        Gemini recommended 30 ms; this was rejected — it causes pervasive
        0x06 SLAVE_DEVICE_BUSY responses on all SUN2000 hardware.
        """
        clamped = max(MIN_INTER_REQUEST_GAP.total_seconds(), min(gap_seconds, 0.500))
        self._gap_contributions[source] = clamped
        self._effective_gap = max(self._gap_contributions.values())

    def update_max_queue_depth(self, source: str, depth: int) -> None:
        """Report *source*'s (a device serial number) learned max queue
        depth. Clamped to [1, MAX_QUEUE_DEPTH] as before; the guard's
        effective depth is the MINIMUM (safest, shallowest) across every
        device currently sharing this bus (Defect P) -- see update_gap()
        for the full reasoning, which applies symmetrically here.
        """
        clamped = max(1, min(depth, MAX_QUEUE_DEPTH))
        self._depth_contributions[source] = clamped
        self._max_queue_depth = min(self._depth_contributions.values())

    def remove_source(self, source: str) -> None:
        """Drop *source*'s contribution to the shared aggregate (its
        coordinator's config entry unloaded or reloaded). Without this, a
        torn-down device's last-reported gap/depth would keep influencing
        the bus's effective parameters forever, even for a device that no
        longer exists. Safe to call for a source that never contributed
        (e.g. a coordinator that unloads before its first poll) or to call
        more than once (multiple coordinators for the same device all
        unload together and each may call this with the same source) --
        both are no-ops beyond the first successful removal.

        Reverts to the pre-contribution defaults (the tightest gap, the
        deepest queue) once the last contributor is removed, matching this
        guard's original single-device starting state.
        """
        self._gap_contributions.pop(source, None)
        self._depth_contributions.pop(source, None)
        self._effective_gap = (
            max(self._gap_contributions.values())
            if self._gap_contributions
            else MIN_INTER_REQUEST_GAP.total_seconds()
        )
        self._max_queue_depth = (
            min(self._depth_contributions.values())
            if self._depth_contributions
            else MAX_QUEUE_DEPTH
        )

    # ── context manager ───────────────────────────────────────────────────────

    class _RequestContext:
        def __init__(
            self,
            guard: "ModbusGuard",
            priority: bool = False,
            label: str = "",
        ) -> None:
            self._guard = guard
            self._priority = priority
            # v2.0.3 (ICS-09): admission exemption, not lock-acquisition
            # priority -- see ModbusGuard.request()'s own docstring for
            # the full distinction. Bypasses shedding at admission time;
            # does not change this request's position once it reaches
            # guard._lock, a plain FIFO asyncio.Lock every request
            # (priority or not) waits on identically.
            self._label = label        # who asked, for diagnostics attribution
            #: Per-request detail filled in by the CALLER after admission
            #: (v1.3.0 fix). The first field capture wrote these as null
            #: because the fields existed but nothing populated them — so the
            #: capture could not correlate stall duration with what was
            #: actually being read, which is the whole next question.
            self.registers: int | None = None
            self.priority_tier: str | None = None
            # v2.0.9 (Phase 2.1/2.4, this release -- ICS-16, both external
            # ICS audits -- confirmed): same "caller fills in after
            # admission" pattern as registers/priority_tier above, not a
            # new mechanism. chunk_index/chunk_count/retry_count/
            # logical_request_id/transition_reason are all optional
            # (None when the caller doesn't set them, e.g. the keep-alive
            # probe or write-path callers that don't operate in terms of
            # chunks at all) so this stays a strict superset of the
            # existing fields, not a breaking change to anything already
            # using this context.
            self.chunk_index: int | None = None
            self.chunk_count: int | None = None
            self.retry_count: int | None = None
            self.logical_request_id: int | None = None
            self.transition_reason: str | None = None
            # v2.0.10 (finer-grained instrumentation, this release --
            # added while investigating a real production defect): the
            # fields above attribute a stall to a specific chunk/poll,
            # but not to WHAT was actually being read. A full list, not
            # just registers' own count above -- see the real call
            # site's own comment (update_coordinator.py) for the full
            # reasoning on why a count alone proved insufficient in
            # practice.
            self.register_names: list[str] | None = None
            self._t_submit: float = 0.0
            self._t_admitted: float = 0.0
            #: Time spent waiting for admission (lock + inter-request gap).
            self.wait_ms: float = 0.0

        async def __aenter__(self) -> "ModbusGuard._RequestContext":
            guard = self._guard
            self._t_submit = time.monotonic()

            # v2.0.0b (AR-4, external ICS audit): the priority lane's own
            # airtime budget. Rolls the self-contained rolling window
            # forward if it has expired, then checks whether THIS
            # request, if it claims priority, still has budget within the
            # current window. If not, it is demoted to normal-lane
            # fairness for the queue-depth check immediately below --
            # deliberately ONLY for that one check, not for anything else
            # (the priority-lane depth check further below, and the
            # priority-queue-depth bookkeeping, still treat this as the
            # priority request it actually is; only its ability to
            # unconditionally bypass ordinary shedding is what the
            # exhausted budget takes away for this one admission).
            if self._t_submit - guard._priority_window_start > PRIORITY_AIRTIME_WINDOW_S:
                guard._priority_window_start = self._t_submit
                guard._priority_busy_s = 0.0
            window_elapsed = self._t_submit - guard._priority_window_start
            priority_fraction = (
                guard._priority_busy_s / window_elapsed if window_elapsed > 0 else 0.0
            )
            self._demoted_for_admission = False
            effective_priority = self._priority
            if self._priority and priority_fraction >= PRIORITY_AIRTIME_BUDGET_FRACTION:
                guard.priority_budget_exceeded_count += 1
                effective_priority = False
                self._demoted_for_admission = True
                _LOGGER.debug(
                    "ModbusGuard[%s]: priority airtime budget exhausted "
                    "(%.0f%% of last %.0fs) -- this priority request is "
                    "subject to normal-lane admission for this cycle",
                    guard.endpoint, priority_fraction * 100, PRIORITY_AIRTIME_WINDOW_S,
                )

            if not effective_priority and guard._queue_depth >= guard._max_queue_depth:
                _LOGGER.debug(
                    "ModbusGuard[%s]: queue full (%d/%d) — shedding request",
                    guard.endpoint, guard._queue_depth, guard._max_queue_depth,
                )
                guard.shed_count += 1
                # v2.0.11 (Phase 5.2, this release): see guard's own
                # _record_bus_admission_outcome() docstring.
                guard._record_bus_admission_outcome(failed=True)
                raise ModbusQueueShed(
                    f"ModbusGuard[{guard.endpoint}] queue full "
                    f"({guard._queue_depth}/{guard._max_queue_depth})"
                )

            # v2.0.0a (F18, external ICS audit -- confirmed): priority
            # requests bypass the check above by design, but that used to
            # mean they had NO upper bound of their own -- the normal
            # queue-depth limit was not a true hard ceiling once a priority
            # producer existed. This is the priority lane's OWN, separate,
            # deliberately small bound (MAX_PRIORITY_QUEUE_DEPTH) -- it
            # does not interact with or reduce the normal queue-depth
            # check above at all, it only catches a genuinely pathological
            # pile-up of priority requests specifically.
            if self._priority and guard._priority_queue_depth >= MAX_PRIORITY_QUEUE_DEPTH:
                _LOGGER.debug(
                    "ModbusGuard[%s]: priority lane full (%d/%d) — "
                    "shedding priority request",
                    guard.endpoint, guard._priority_queue_depth, MAX_PRIORITY_QUEUE_DEPTH,
                )
                guard.priority_shed_count += 1
                guard._record_bus_admission_outcome(failed=True)
                raise ModbusQueueShed(
                    f"ModbusGuard[{guard.endpoint}] priority lane full "
                    f"({guard._priority_queue_depth}/{MAX_PRIORITY_QUEUE_DEPTH})"
                )

            guard._queue_depth += 1
            if self._priority:
                guard._priority_queue_depth += 1
            lock_acquired = False
            try:
                async with asyncio.timeout(QUEUE_WAIT_TIMEOUT.total_seconds()):
                    await guard._lock.acquire()
                lock_acquired = True

                now = time.monotonic()
                elapsed = now - guard._last_request_end
                if elapsed < guard._effective_gap:
                    wait = guard._effective_gap - elapsed
                    _LOGGER.debug(
                        "ModbusGuard[%s]: inter-request pause %.0f ms",
                        guard.endpoint, wait * 1000,
                    )
                    await asyncio.sleep(wait)

                self._t_admitted = time.monotonic()
                self.wait_ms = (self._t_admitted - self._t_submit) * 1000
                guard._wait_samples.append(self.wait_ms)
                guard.total_wait_ms += self.wait_ms
                if self.wait_ms > 1000.0:
                    guard.requests_waited += 1
                # v2.0.11 (Phase 5.2, this release): genuine admission
                # success -- past both the shed check and the admission-
                # wait timeout. See guard's own
                # _record_bus_admission_outcome() docstring.
                guard._record_bus_admission_outcome(failed=False)

            except BaseException as exc:
                # MUST be BaseException, not Exception: asyncio.CancelledError is a
                # BaseException.  A cancellation during lock.acquire() or during the
                # inter-request gap sleep (after the lock was granted) would
                # otherwise leak both the queue counter and — fatally — the lock,
                # deadlocking the entire Modbus bus until HA is restarted.
                guard._queue_depth -= 1
                if self._priority:
                    # v2.0.0a (F18): mirrors _queue_depth's own cleanup here --
                    # the priority lane counter must never leak on this path
                    # either, or it would eventually shed every future
                    # priority request permanently.
                    guard._priority_queue_depth -= 1
                if lock_acquired:
                    guard._lock.release()
                # v2.0.0a (F08, external ICS audit -- confirmed): if we're
                # still waiting for the lock (lock_acquired is still False)
                # and this is specifically a plain TimeoutError, it can only
                # have come from the admission-wait asyncio.timeout() above
                # -- the device was never contacted at all. Re-raised as a
                # distinguishable type so ModbusKeepAlive (and anything else
                # that cares) can tell "bus was busy" apart from "device
                # didn't answer", instead of both surfacing as the same
                # bare TimeoutError. Anything else (a genuine external
                # CancelledError, or a failure once the lock WAS already
                # held) passes through unchanged.
                #
                # v2.0.7 FIX (MOD-03, ICS quality audit -- confirmed): this
                # used to check `not lock_acquired` -- but lock_acquired
                # becomes True immediately after guard._lock.acquire()
                # succeeds, BEFORE the inter-request gap sleep just above
                # runs. A TimeoutError arriving during that sleep (e.g. an
                # OUTER caller-side deadline, such as the whole-poll
                # deadline, firing while still waiting out the gap) had
                # lock_acquired == True by then, so it fell through this
                # check entirely and escaped as a bare TimeoutError --
                # indistinguishable from a genuine device timeout, even
                # though the device still hadn't been contacted.
                # self._t_admitted (0.0 until admission genuinely
                # completes, just above) is the correct boundary: admission
                # is not complete until the whole sequence -- lock AND gap
                # -- has finished, not merely once the lock is held.
                if (
                    not self._t_admitted
                    and isinstance(exc, TimeoutError)
                    and not isinstance(exc, (ModbusQueueShed, ModbusAdmissionTimeout))
                ):
                    # v2.0.11 (Phase 5.2, this release): see guard's own
                    # _record_bus_admission_outcome() docstring. Lifetime
                    # counter mirrors shed_count's own role above --
                    # never previously tracked at all on ModbusGuard
                    # itself (only as a per-device ModbusTelemetry
                    # counter, one level up).
                    guard.admission_timeout_count += 1
                    guard._record_bus_admission_outcome(failed=True)
                    raise ModbusAdmissionTimeout(
                        f"ModbusGuard[{guard.endpoint}] admission wait exceeded "
                        f"{QUEUE_WAIT_TIMEOUT.total_seconds():.0f}s"
                    ) from exc
                raise
            return self

        async def __aexit__(self, exc_type, *_: object) -> None:
            guard = self._guard
            now = time.monotonic()
            service_ms = (now - self._t_admitted) * 1000 if self._t_admitted else 0.0
            guard._last_request_end = now
            guard._queue_depth -= 1
            if self._priority:
                # v2.0.0a (F18): mirrors _queue_depth's own decrement here --
                # the normal, successful-completion exit path.
                guard._priority_queue_depth -= 1
            guard._lock.release()

            # Occupancy accounting: only time actually holding the line counts.
            if self._t_admitted:
                guard._busy_s += (now - self._t_admitted)
                guard._service_samples.append(service_ms)
                # v2.0.0b (AR-4): priority traffic's own airtime, tracked
                # separately from -- and regardless of whether this
                # specific request was demoted to normal-lane admission
                # above -- it is still functionally priority traffic that
                # consumed bus time, and the budget needs to see that
                # accurately to mean anything.
                if self._priority:
                    guard._priority_busy_s += (now - self._t_admitted)

            sink = guard.diagnostics
            if sink is not None:
                try:
                    sink.record(
                        endpoint=guard.endpoint,
                        label=self._label,
                        wait_ms=self.wait_ms,
                        service_ms=service_ms,
                        queue_depth=guard._queue_depth,
                        outcome="error" if exc_type else "ok",
                        registers=self.registers,
                        priority=self.priority_tier,
                        # v2.0.9 (Phase 2.1/2.4, this release): see
                        # _RequestContext's own field comments above.
                        chunk_index=self.chunk_index,
                        chunk_count=self.chunk_count,
                        retry_count=self.retry_count,
                        logical_request_id=self.logical_request_id,
                        transition_reason=self.transition_reason,
                        # v2.0.10 (finer-grained instrumentation, this
                        # release): see _RequestContext's own
                        # register_names field comment.
                        register_names=self.register_names,
                    )
                except Exception:  # noqa: BLE001 — diagnostics must never break I/O
                    _LOGGER.exception("ModbusGuard[%s]: diagnostics sink failed",
                                      guard.endpoint)

    def request(
        self, priority: bool = False, label: str = ""
    ) -> "_RequestContext":
        """Return an async context manager that serialises Modbus access.

        v2.0.3 (ICS-09, external ICS audit -- confirmed): this docstring
        used to say only what priority=True DOES ("bypass queue-depth
        shedding"); it did not say what it does NOT do, which is exactly
        the gap the audit's own confidence in this area came from. Made
        explicit here rather than left implicit: priority=True is
        admission exemption -- it changes whether a request gets shed at
        the queue-depth check (this file's own MAX_QUEUE_DEPTH/
        MAX_PRIORITY_QUEUE_DEPTH/AR-4's own airtime-budget checks), and
        which of two separate queue-depth budgets it's weighed against.
        It is NOT lock-acquisition priority: every admitted request,
        priority or not, ultimately waits on the same guard._lock, which
        is a plain asyncio.Lock -- Python's own FIFO-fair primitive, with
        no priority-ordering concept of its own. A priority request that
        arrives after several normal requests are already queued on that
        lock waits its turn behind them exactly like a normal request
        would; admission exemption only ever helps BEFORE that point,
        deciding whether the request is allowed to queue at all rather
        than where in the queue it lands. If true priority ordering
        (jumping ahead of already-queued normal requests, not just
        skipping the shed decision) is ever genuinely needed, it would
        require a different primitive entirely -- a real priority queue
        or condition-variable-based scheduler, not a parameter on top of
        a single shared Lock -- deliberately not built speculatively
        here without field evidence keep-alive is actually being starved
        in a way admission exemption alone doesn't already prevent.

        priority=True is used by the keep-alive task to bypass queue-depth
        shedding — the keep-alive probe must always be able to run regardless
        of how many coordinators are waiting.
        """
        return self._RequestContext(self, priority=priority, label=label)

    def occupancy(self, reset: bool = False) -> float:
        """Fraction of wall-clock time this guard has held the line (0.0-1.0).

        The feedforward signal for the future scheduler. Unlike failure rate it
        LEADS the problem: occupancy rises before queues build, whereas a
        failure has already cost a timeout by the time it is recorded.
        """
        now = time.monotonic()
        elapsed = now - self._window_start
        if elapsed <= 0:
            return 0.0
        value = min(1.0, self._busy_s / elapsed)
        if reset:
            self._busy_s = 0.0
            self._window_start = now
        return value

    def seconds_since_last_activity(self) -> float:
        """Wall-clock seconds since ANY request on this endpoint last
        completed -- from any source (any coordinator, a write, another
        entry's config-flow discovery, or keep-alive's own prior probe).

        v2.0.0b (MOD-14, external ICS audit -- confirmed): distinct from
        ModbusKeepAlive.seconds_since_last_ok(), which only tracks
        keep-alive's OWN probe history and is therefore always
        approximately KEEPALIVE_INTERVAL (it resets on every keep-alive
        success, regardless of what else has happened on the bus) --
        not a useful signal for "has the endpoint genuinely been idle".
        This tracks the SAME `_last_request_end` timestamp every guarded
        request already updates on exit (see the request context
        manager's own __aexit__), giving a true whole-endpoint activity
        signal keep-alive can gate its own probing on.
        """
        if self._last_request_end <= 0:
            # No request has ever completed on this endpoint -- treat as
            # "idle for a very long time" so a legitimate first probe is
            # never suppressed by this check.
            return float("inf")
        return time.monotonic() - self._last_request_end

    def _record_bus_admission_outcome(self, *, failed: bool) -> None:
        """v2.0.11 (Phase 5.2, this release): update the bus-health EWMA
        with one admission outcome -- called from every one of the four
        places admission is actually decided (normal shed, priority-lane
        shed, admission timeout, and successful admission), so the
        signal reflects the SAME event population those counters do,
        just as a recency-weighted rate instead of a lifetime count.

        Deliberately observational only -- see this class's own
        _bus_admission_ewma_n field comment for why this doesn't (yet)
        feed back into admission control itself.
        """
        self._bus_admission_ewma_n = (
            self._bus_admission_ewma_n * BUS_HEALTH_EWMA_DECAY + 1.0
        )
        self._bus_admission_ewma_failures = (
            self._bus_admission_ewma_failures * BUS_HEALTH_EWMA_DECAY
            + (1.0 if failed else 0.0)
        )

    def bus_health_pct(self) -> float | None:
        """v2.0.11 (Phase 5.2, this release): 0-100, the EWMA-weighted
        percentage of RECENT admission attempts on this endpoint that
        succeeded (were neither shed nor timed out waiting for
        admission) -- the bus-level counterpart to AdaptiveModbusController's
        own device-level confidence, deliberately computed from a
        completely separate signal (admission outcomes on the shared
        guard, not any one device's own RTT/failure history) so the two
        never get conflated. None until at least one admission attempt
        has been observed -- a fresh/idle endpoint has no bus-health
        opinion yet, not a fake 100%.
        """
        if self._bus_admission_ewma_n < 1e-9:
            return None
        rate_failed = self._bus_admission_ewma_failures / self._bus_admission_ewma_n
        return round(max(0.0, min(1.0, 1.0 - rate_failed)) * 100.0, 1)

    def wait_service_split(self) -> tuple[float, float]:
        """Return (p95 wait ms, p95 service ms) over the rolling window.

        THE Phase 0 measurement. Long wait => requests queue behind one another
        and a scheduler is the fix. Long service => the device itself is slow
        and only demand reduction helps. Three days of field data could not
        distinguish these, because nothing measured them separately.
        """
        def p95(samples: deque[float]) -> float:
            if not samples:
                return 0.0
            ordered = sorted(samples)
            return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        return p95(self._wait_samples), p95(self._service_samples)

    @property
    def queue_depth(self) -> int:
        return self._queue_depth

    @property
    def is_busy(self) -> bool:
        return self._lock.locked()

    @property
    def effective_gap_ms(self) -> float:
        return self._effective_gap * 1000
