"""Modbus traffic telemetry for Huawei Solar.

Tracks per-device Modbus statistics (requests, failures, timeouts, cache hits)
over a rolling 1-hour window and exposes them as HA sensor entities.

Architecture
------------
ModbusTelemetry  – singleton-per-serial-number, thread-safe via asyncio.
                   Stored in hass.data[DOMAIN]["telemetry"][serial_number].

HuaweiSolarModbusTelemetrySensorEntity
                 – standard HA SensorEntity that pulls values from the
                   singleton on each HA poll (no coordinator needed).

Integration wiring
------------------
1. ModbusTelemetry.get_or_create(hass, serial_number) in __init__.py after
   each coordinator is built — returns the singleton.
2. Coordinators call record_request() / record_failure() / record_timeout() /
   record_cache_hits() on the singleton.
3. sensor.py calls create_telemetry_entities() to register the HA sensors.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
import logging
import time
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval


_LOGGER = logging.getLogger(__name__)

# Rolling window for rate calculations
_WINDOW = timedelta(hours=1)
_WINDOW_SEC = _WINDOW.total_seconds()

# How often the HA sensors are pushed an update (independent of poll cycle)
_TELEMETRY_PUSH_INTERVAL = timedelta(seconds=60)


# ──────────────────────────────────────────────────────────────────────────────
# Core telemetry object
# ──────────────────────────────────────────────────────────────────────────────

class ModbusTelemetry:
    """Per-device rolling Modbus traffic statistics.

    Uses deques of timestamps for O(1) append and O(k) windowed-count, where k
    is typically very small (≤ 120 events/hour at 30 s poll intervals).
    """

    _registry: dict[str, "ModbusTelemetry"] = {}

    # ── class-level helpers ───────────────────────────────────────────────────

    @classmethod
    def get_or_create(
        cls, hass: HomeAssistant, serial_number: str, device_info: DeviceInfo
    ) -> "ModbusTelemetry":
        """Return existing singleton or create a new one."""
        if serial_number not in cls._registry:
            cls._registry[serial_number] = cls(hass, serial_number, device_info)
        return cls._registry[serial_number]

    @classmethod
    def get(cls, serial_number: str) -> "ModbusTelemetry | None":
        """Return existing singleton or None."""
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
        """Remove all singletons (called on integration unload)."""
        cls._registry.clear()

    # ── instance ──────────────────────────────────────────────────────────────

    def __init__(
        self, hass: HomeAssistant, serial_number: str, device_info: DeviceInfo
    ) -> None:
        self.hass = hass
        self.serial_number = serial_number
        self.device_info = device_info

        # Rolling timestamp deques — one entry per event
        # v2.0.5 FIX (F-04, external ICS audit -- confirmed with exact
        # numbers matched against the real telemetry capture: timeout_
        # rate_percent readings up to 400%, e.g. 1 request / 3 timeouts):
        # every rate this class computes used to divide by req_ph (len(
        # self._requests)) alone. record_request() is only ever called
        # AFTER a batch succeeds (update_coordinator.py's own comment:
        # "record_request after batch so count is accurate") -- so
        # req_ph counts successful batches, not attempts. Any window
        # with more failures than successes produced a rate exceeding
        # 100%, which is not a meaningful percentage.
        #
        # self._attempts (below) is the fix: bumped by every recording
        # method below, success or failure alike, so it always equals
        # the true population every rate's numerator is drawn from --
        # every rate this class computes is now mathematically bounded
        # to [0, 100]% by construction, not just usually.
        self._attempts: deque[float] = deque()
        self._requests: deque[float] = deque()
        self._failures: deque[float] = deque()
        # v2.0.5 (F-04): split from one shared "timeout" bucket into the
        # three genuinely distinct outcomes it always actually was.
        # self._timeouts is KEPT, unchanged in meaning (all three kinds
        # combined) -- pre-existing, established field, not touched --
        # but the new, specific deques let timeout_rate_percent (below)
        # mean what its own name says: a genuine DEVICE timeout rate,
        # not device timeouts, internal bus contention, and admission
        # queueing delay all conflated into one number, which is the
        # separate conflation concern this same audit raised in its own
        # section 11 -- not just the denominator bug, a second, related
        # semantic problem closed by the same redesign.
        self._timeouts: deque[float] = deque()
        self._device_timeouts: deque[float] = deque()
        self._queue_sheds: deque[float] = deque()
        self._admission_timeouts: deque[float] = deque()
        self._cache_hits: deque[float] = deque()
        self._batch_sizes: deque[int] = deque()

        # Lifetime totals (never reset)
        self.total_attempts: int = 0
        self.total_requests: int = 0
        self.total_failures: int = 0
        self.total_timeouts: int = 0
        self.total_device_timeouts: int = 0
        self.total_queue_sheds: int = 0
        self.total_admission_timeouts: int = 0
        self.total_cache_hits: int = 0
        self.total_skipped_polls: int = 0
        # v2.0.9 (Phase 1.2, this release -- ICS-15, both external ICS
        # audits): 0x06 SLAVE_DEVICE_BUSY retry logic has existed in
        # update_coordinator.py since v1.0.6, but had no dedicated
        # counter anywhere -- both audits independently flagged retry
        # amplification as "a credible but unquantified risk". total_
        # physical_attempts counts every actual wire transaction
        # attempted, including BUSY sub-retries of the same chunk;
        # total_attempts above (renamed nowhere, kept as-is for
        # backward compatibility) counts one per chunk's FINAL outcome
        # only -- the same distinction the newer audit's own report
        # draws as "logical" vs "physical" attempts. retry_amplification
        # (physical/logical, computed in snapshot() below) is the
        # concrete number both audits asked for.
        self.total_busy_events: int = 0
        self.total_physical_attempts: int = 0
        self._night_mode: bool = False

        # Derived metrics updated on each call to snapshot()
        self._last_snapshot: dict[str, Any] = {}

        # HA entity update callbacks registered via add_listener()
        self._listeners: list[callback] = []

        # Schedule periodic push to HA entities
        self._unsub = async_track_time_interval(
            hass, self._push_to_listeners, _TELEMETRY_PUSH_INTERVAL
        )

    # ── event recording (called from coordinators) ────────────────────────────

    def record_request(self, batch_size: int = 1) -> None:
        """Record a successful Modbus request.

        v2.0.13 FIX (MOD-021, external ICS quality/defect/architecture
        audit -- confirmed): this used to also increment total_
        physical_attempts here -- correct for a genuinely single-
        transaction caller (e.g. the optimizer coordinator's own
        record_request(1)), but the main coordinator's own
        _execute_batch() can split ONE logical poll into several
        physical batch_update() calls, and this method is only called
        ONCE per poll regardless of chunk count -- undercounting real
        wire transactions by up to (chunk_count - 1) for every
        multi-chunk poll. Physical-attempt counting is now explicit
        (record_physical_attempt(), called once per chunk from
        _execute_batch() itself) rather than implicitly bundled into
        this logical-poll-level method. Callers that make exactly one
        physical attempt per logical request (the optimizer
        coordinator) now call record_physical_attempt() explicitly
        alongside this method, preserving their own already-correct
        1:1 behaviour without this method silently doing it for them.
        """
        now = time.monotonic()
        self._requests.append(now)
        self._attempts.append(now)
        self._batch_sizes.append(batch_size)
        self.total_requests += 1
        self.total_attempts += 1
        self._evict(now)

    def record_physical_attempt(self) -> None:
        """v2.0.13 (MOD-021, external ICS quality/defect/architecture
        audit -- confirmed): record ONE genuine physical wire
        transaction -- a real batch_update(chunk) invocation that was
        actually admitted and sent, regardless of its own outcome.

        Deliberately a separate, explicit method rather than bundled
        into record_request()/record_failure()/record_timeout() (see
        each of their own docstrings) -- those are logical-poll-level
        outcomes (once per poll, regardless of how many chunks that
        poll needed), while THIS is the physical-transaction-level
        counter the audit's own recommended model calls for:
        `logical poll -> N chunks -> N physical attempts (+ each
        BUSY retry)`. Called once per CHUNK from _execute_batch()'s
        own per-chunk loop -- not once per retry-loop iteration within
        a chunk, since record_busy_retry() already correctly counts
        each retry as its own additional physical attempt separately
        (confirmed directly against that method's own, pre-existing
        implementation before this fix was designed, specifically to
        avoid double-counting a retried chunk).
        """
        self.total_physical_attempts += 1

    def record_failure(self) -> None:
        """Record a non-timeout failure.

        v2.0.13 FIX (MOD-021, this release): see record_request()'s
        own docstring for the full reasoning -- physical-attempt
        counting moved to the explicit record_physical_attempt(),
        called once per chunk by callers that chunk (_execute_batch())
        and explicitly alongside this method by callers that don't
        (the optimizer coordinator's own single-transaction path).
        """
        now = time.monotonic()
        self._failures.append(now)
        self._attempts.append(now)
        self.total_failures += 1
        self.total_attempts += 1
        self._evict(now)

    def record_busy_retry(self) -> None:
        """Record one 0x06 SLAVE_DEVICE_BUSY response that triggered a
        retry of the same chunk.

        v2.0.9 (Phase 1.2, this release -- ICS-15, both external ICS
        audits -- confirmed): this is a genuine additional physical wire
        transaction (the BUSY response itself was a real exchange, and
        the retry that follows is another one) that was previously
        completely invisible to telemetry -- neither total_attempts nor
        any other existing counter saw it, since only the chunk's own
        FINAL outcome (via record_request/record_failure/record_timeout)
        was ever recorded. Deliberately does NOT touch total_attempts
        (the logical/chunk-outcome counter) -- a chunk that BUSY-retried
        twice then succeeded is still exactly one logical attempt, with
        three physical ones.
        """
        self.total_busy_events += 1
        self.total_physical_attempts += 1

    def record_timeout(self, kind: str = "device") -> None:
        """Record a timeout.

        v2.0.5 FIX (F-04, external ICS audit -- confirmed): `kind`
        distinguishes a genuine device/transport timeout from internal
        bus contention (a queue shed or an admission timeout) -- both of
        which this project's own established discipline elsewhere
        (MOD-09 and others) already treats as "not the inverter's fault"
        for the adaptive-learning model, but which this telemetry class
        previously folded into one undifferentiated timeout bucket
        regardless. Every caller of this method (update_coordinator.py's
        own three _record_*() methods, plus the optimizer coordinator's
        own inline copy) already independently classifies which of the
        three actually happened -- this just makes that existing
        classification visible in telemetry too, not a new judgement
        call invented here.

        self._timeouts (the pre-existing, established deque/counter) is
        deliberately still updated on every call here, regardless of
        kind, keeping its own existing meaning (all three kinds
        combined) completely unchanged -- only the NEW, kind-specific
        counters and self._attempts are new behaviour.
        """
        if kind not in ("device", "queue_shed", "admission"):
            raise ValueError(f"record_timeout: unknown kind {kind!r}")
        now = time.monotonic()
        self._timeouts.append(now)
        self._attempts.append(now)
        self.total_timeouts += 1
        self.total_failures += 1
        self.total_attempts += 1
        if kind == "device":
            self._device_timeouts.append(now)
            self.total_device_timeouts += 1
            # v2.0.13 FIX (MOD-021, this release): total_physical_
            # attempts no longer incremented here -- see record_
            # request()'s own docstring for the full reasoning. A
            # device timeout on the main coordinator's own chunked path
            # is still one real physical attempt (the chunk WAS sent,
            # it just never got a response in time) -- _execute_batch()
            # already records that via record_physical_attempt() at the
            # top of its own per-chunk loop, before the outcome is even
            # known, so nothing is lost by removing it here. Callers
            # with no chunking of their own (the optimizer coordinator)
            # call record_physical_attempt() explicitly alongside this
            # method instead.
        elif kind == "queue_shed":
            self._queue_sheds.append(now)
            self.total_queue_sheds += 1
        else:  # "admission"
            self._admission_timeouts.append(now)
            self.total_admission_timeouts += 1
        self._evict(now)

    def record_cache_hits(self, count: int) -> None:
        """Record *count* register cache hits in a single operation.

        Replaces the old per-register record_cache_hit() with one
        time.monotonic() call and a single deque.extend(), which is
        meaningfully cheaper when count is large (30-80 registers/poll).
        """
        if count <= 0:
            return
        now = time.monotonic()
        self._cache_hits.extend([now] * count)
        self.total_cache_hits += count

    # Thin compatibility shim — callers outside register_cache may still use
    # the singular form; route them through the batched version.
    def record_cache_hit(self) -> None:
        """Record a single register cache hit."""
        self.record_cache_hits(1)

    def record_skipped_poll(self) -> None:
        """Record a poll that was entirely skipped (back-off / dedup)."""
        self.total_skipped_polls += 1

    def record_night_mode(self, active: bool) -> None:
        """Record the current night-mode state (for snapshot reporting)."""
        self._night_mode = active

    # ── derived metric helpers ────────────────────────────────────────────────

    def _evict(self, now: float) -> None:
        """Remove entries older than the rolling window from all deques."""
        cutoff = now - _WINDOW_SEC
        for dq in (
            self._attempts,
            self._requests,
            self._failures,
            self._timeouts,
            self._device_timeouts,
            self._queue_sheds,
            self._admission_timeouts,
            self._cache_hits,
        ):
            while dq and dq[0] < cutoff:
                dq.popleft()
        # batch_sizes can grow independently — trim to same length as requests
        while len(self._batch_sizes) > len(self._requests):
            self._batch_sizes.popleft()

    def snapshot(self) -> dict[str, Any]:
        """Return a point-in-time snapshot of all metrics."""
        now = time.monotonic()
        self._evict(now)

        attempts_ph = len(self._attempts)
        req_ph = len(self._requests)
        fail_ph = len(self._failures)
        to_ph = len(self._timeouts)
        device_to_ph = len(self._device_timeouts)
        shed_ph = len(self._queue_sheds)
        admission_to_ph = len(self._admission_timeouts)
        cache_ph = len(self._cache_hits)

        avg_batch = (
            round(sum(self._batch_sizes) / len(self._batch_sizes), 1)
            if self._batch_sizes
            else 0.0
        )
        # v2.0.3 FIX (F-03, external ICS audit -- confirmed, with exact
        # numbers matched against a real telemetry capture: 24 timeouts,
        # 24 total_failures, yet failure_rate_percent: 0.0 for one
        # device; 7/7/0.0 for another): record_timeout() (above) always
        # increments total_failures (the lifetime counter) but only ever
        # appends to self._timeouts, never self._failures -- so this
        # rolling, windowed rate was silently blind to any failure
        # pattern that happened to be all timeouts, exactly the case
        # both devices in that real capture were in.
        #
        # v2.0.5 FIX (F-04, external ICS audit -- confirmed, with exact
        # numbers matched against the real telemetry capture: readings
        # up to timeout_rate_percent: 400.0): ALL rates below are now
        # computed over attempts_ph (self._attempts, bumped by every
        # recording method above -- success or any failure kind alike),
        # not req_ph (successful batches only, since record_request()
        # only ever fires after a batch succeeds). Every rate's
        # numerator is a strict subset of the same attempts_ph
        # population by construction, so every rate below is now
        # mathematically bounded to [0, 100]% -- not just usually, as a
        # side effect of typically-low failure counts, but always,
        # structurally.
        #
        # timeout_rate_percent's own MEANING also changes here, not just
        # its denominator: it now reflects device_to_ph (genuine device/
        # transport timeouts) specifically, not to_ph (which still
        # includes queue sheds and admission timeouts -- internal bus
        # contention this project's own established discipline
        # elsewhere, MOD-09 and others, already treats as "not the
        # inverter's fault" for adaptive learning, but which this metric
        # previously conflated with genuine device timeouts regardless).
        # failure_rate_percent's own existing meaning (non-timeout
        # failures only) is unchanged -- only its denominator is fixed.
        failure_rate = (
            round(fail_ph / attempts_ph * 100, 1) if attempts_ph else 0.0
        )
        timeout_rate = (
            round(device_to_ph / attempts_ph * 100, 1) if attempts_ph else 0.0
        )
        overall_failed_attempt_rate = (
            round((fail_ph + device_to_ph) / attempts_ph * 100, 1)
            if attempts_ph else 0.0
        )
        # New fields: internal bus contention, now visible in its own
        # right rather than hidden inside a device-timeout-named metric.
        queue_shed_rate = (
            round(shed_ph / attempts_ph * 100, 1) if attempts_ph else 0.0
        )
        admission_timeout_rate = (
            round(admission_to_ph / attempts_ph * 100, 1) if attempts_ph else 0.0
        )

        snap = {
            "attempts_per_hour": attempts_ph,
            "requests_per_hour": req_ph,
            "failures_per_hour": fail_ph,
            "timeouts_per_hour": to_ph,
            "device_timeouts_per_hour": device_to_ph,
            "queue_sheds_per_hour": shed_ph,
            "admission_timeouts_per_hour": admission_to_ph,
            "cache_hits_per_hour": cache_ph,
            "failure_rate_percent": failure_rate,
            "timeout_rate_percent": timeout_rate,
            "overall_failed_attempt_rate_percent": overall_failed_attempt_rate,
            "queue_shed_rate_percent": queue_shed_rate,
            "admission_timeout_rate_percent": admission_timeout_rate,
            "avg_batch_size": avg_batch,
            "total_attempts": self.total_attempts,
            "total_requests": self.total_requests,
            "total_failures": self.total_failures,
            "total_timeouts": self.total_timeouts,
            "total_device_timeouts": self.total_device_timeouts,
            "total_queue_sheds": self.total_queue_sheds,
            "total_admission_timeouts": self.total_admission_timeouts,
            "total_cache_hits": self.total_cache_hits,
            # v2.0.9 (Phase 1.2, this release -- ICS-15, both external
            # ICS audits): "logical_attempts" is an explicit alias for
            # total_attempts, matching the exact terminology both audits
            # used, so the field is self-explanatory in an exported
            # capture without needing to cross-reference this class's
            # own naming history. retry_amplification is exactly the
            # ratio both audits asked for (physical/logical); None when
            # there have been no logical attempts yet, not a divide by
            # zero.
            "logical_attempts": self.total_attempts,
            "total_physical_attempts": self.total_physical_attempts,
            "total_busy_events": self.total_busy_events,
            "retry_amplification": (
                round(self.total_physical_attempts / self.total_attempts, 3)
                if self.total_attempts else None
            ),
            "total_skipped_polls": self.total_skipped_polls,
            "night_mode_active": self._night_mode,
        }
        self._last_snapshot = snap
        return snap

    # ── HA listener plumbing ──────────────────────────────────────────────────

    def add_listener(self, cb: Any) -> None:
        """Register a callback that is called when telemetry is pushed."""
        self._listeners.append(cb)

    def remove_listener(self, cb: Any) -> None:
        """Unregister a previously registered callback."""
        try:
            self._listeners.remove(cb)
        except ValueError:
            pass

    @callback
    def _push_to_listeners(self, _now: datetime) -> None:
        """Push updated telemetry to all registered HA entities.

        v1.3.19 FIX (Defect V, independent ICS audit): this loop used to
        call each listener directly with no exception isolation -- one
        misbehaving callback raising would stop iteration entirely, so
        every listener registered AFTER the failing one silently stopped
        receiving updates. Each callback is now isolated so one bad
        consumer can never suppress the rest.

        v1.3.20 FOLLOW-UP (Defect X2, independent ICS audit): this still
        iterated the LIVE self._listeners list, not a snapshot -- the same
        defect class adaptive_modbus.py's sibling implementation already
        guards against explicitly (its own BUG-003 fix, from an earlier
        session, snapshots via list(self._listeners) with a comment
        explaining exactly why). If any listener removed itself, or another
        listener, during its own callback, Python's list-mutation-during-
        iteration semantics could skip whichever listener was next in
        line. No currently-registered listener does this, so it wasn't
        actively misbehaving, but it's the same lesson this codebase had
        already learned once, in the sibling file, not carried over here.
        """
        snap = self.snapshot()
        for cb_fn in list(self._listeners):
            try:
                cb_fn(snap)
            except Exception:  # noqa: BLE001 — one bad listener must not break the others
                _LOGGER.exception(
                    "%s: telemetry listener callback raised; skipping it "
                    "for this update, other listeners are unaffected",
                    self.serial_number,
                )

    def stop(self) -> None:
        """Cancel the periodic push timer."""
        if self._unsub:
            self._unsub()
            self._unsub = None


# ──────────────────────────────────────────────────────────────────────────────
# HA Sensor entities
# ──────────────────────────────────────────────────────────────────────────────

# Sensor definitions: (attr_key, name, unit, icon, extra_kwargs)
_TELEMETRY_SENSORS: list[tuple[str, str, str | None, str, dict]] = [
    (
        "requests_per_hour",
        "Modbus requests / hour",
        None,
        "mdi:counter",
        {"state_class": SensorStateClass.MEASUREMENT},
    ),
    (
        "failures_per_hour",
        "Modbus failures / hour",
        None,
        "mdi:alert-circle-outline",
        {"state_class": SensorStateClass.MEASUREMENT},
    ),
    (
        "timeouts_per_hour",
        "Modbus timeouts / hour",
        None,
        "mdi:timer-off-outline",
        {"state_class": SensorStateClass.MEASUREMENT},
    ),
    (
        "cache_hits_per_hour",
        "Modbus cache hits / hour",
        None,
        "mdi:database-check-outline",
        {"state_class": SensorStateClass.MEASUREMENT},
    ),
    (
        "failure_rate_percent",
        "Modbus failure rate",
        "%",
        "mdi:percent",
        {"state_class": SensorStateClass.MEASUREMENT},
    ),
    # v2.0.5 (F-04, external ICS audit): these two fields were added to
    # the snapshot dict by v2.0.4's own F-03 fix, but a real gap from
    # that same fix -- never noticed until this later pass -- is that
    # they were never wired up as actual HA sensor entities, only ever
    # reachable via the raw telemetry JSONL capture, not visible in the
    # UI at all. Added here alongside the new v2.0.5 fields, since
    # they're the same kind of oversight this pass is already fixing.
    (
        "timeout_rate_percent",
        "Modbus device timeout rate",
        "%",
        "mdi:timer-alert-outline",
        {"state_class": SensorStateClass.MEASUREMENT},
    ),
    (
        "overall_failed_attempt_rate_percent",
        "Modbus overall failed attempt rate",
        "%",
        "mdi:percent-outline",
        {"state_class": SensorStateClass.MEASUREMENT},
    ),
    # v2.0.5 (F-04): internal bus contention, now visible in its own
    # right instead of hidden inside timeout_rate_percent (which now
    # means genuine device timeouts specifically -- see modbus_telemetry
    # .py's own snapshot() docstring for the full reasoning).
    (
        "queue_shed_rate_percent",
        "Modbus queue shed rate",
        "%",
        "mdi:filter-remove-outline",
        {"state_class": SensorStateClass.MEASUREMENT},
    ),
    (
        "admission_timeout_rate_percent",
        "Modbus admission timeout rate",
        "%",
        "mdi:timer-sand",
        {"state_class": SensorStateClass.MEASUREMENT},
    ),
    (
        "attempts_per_hour",
        "Modbus attempts / hour",
        None,
        "mdi:counter",
        {
            "state_class": SensorStateClass.MEASUREMENT,
            "entity_registry_enabled_default": False,
        },
    ),
    (
        "device_timeouts_per_hour",
        "Modbus device timeouts / hour",
        None,
        "mdi:timer-off-outline",
        {
            "state_class": SensorStateClass.MEASUREMENT,
            "entity_registry_enabled_default": False,
        },
    ),
    (
        "queue_sheds_per_hour",
        "Modbus queue sheds / hour",
        None,
        "mdi:filter-remove-outline",
        {
            "state_class": SensorStateClass.MEASUREMENT,
            "entity_registry_enabled_default": False,
        },
    ),
    (
        "admission_timeouts_per_hour",
        "Modbus admission timeouts / hour",
        None,
        "mdi:timer-sand",
        {
            "state_class": SensorStateClass.MEASUREMENT,
            "entity_registry_enabled_default": False,
        },
    ),
    (
        "avg_batch_size",
        "Avg Modbus batch size",
        None,
        "mdi:package-variant",
        {"state_class": SensorStateClass.MEASUREMENT},
    ),
    (
        "total_requests",
        "Modbus total requests",
        None,
        "mdi:counter",
        {
            "state_class": SensorStateClass.TOTAL_INCREASING,
            "entity_registry_enabled_default": False,
        },
    ),
    (
        "total_failures",
        "Modbus total failures",
        None,
        "mdi:alert-circle",
        {
            "state_class": SensorStateClass.TOTAL_INCREASING,
            "entity_registry_enabled_default": False,
        },
    ),
    (
        "total_attempts",
        "Modbus total attempts",
        None,
        "mdi:counter",
        {
            "state_class": SensorStateClass.TOTAL_INCREASING,
            "entity_registry_enabled_default": False,
        },
    ),
    (
        "total_device_timeouts",
        "Modbus total device timeouts",
        None,
        "mdi:timer-off-outline",
        {
            "state_class": SensorStateClass.TOTAL_INCREASING,
            "entity_registry_enabled_default": False,
        },
    ),
    (
        "total_queue_sheds",
        "Modbus total queue sheds",
        None,
        "mdi:filter-remove-outline",
        {
            "state_class": SensorStateClass.TOTAL_INCREASING,
            "entity_registry_enabled_default": False,
        },
    ),
    (
        "total_admission_timeouts",
        "Modbus total admission timeouts",
        None,
        "mdi:timer-sand",
        {
            "state_class": SensorStateClass.TOTAL_INCREASING,
            "entity_registry_enabled_default": False,
        },
    ),
    (
        "total_cache_hits",
        "Modbus total cache hits",
        None,
        "mdi:database-check",
        {
            "state_class": SensorStateClass.TOTAL_INCREASING,
            "entity_registry_enabled_default": False,
        },
    ),
    (
        "total_skipped_polls",
        "Modbus skipped polls",
        None,
        "mdi:skip-next-circle-outline",
        {
            "state_class": SensorStateClass.TOTAL_INCREASING,
            "entity_registry_enabled_default": False,
        },
    ),
    (
        "night_mode_active",
        "Inverter night mode",
        None,
        "mdi:weather-night",
        {},   # no state_class — this is a boolean-ish string sensor
    ),
]


def create_telemetry_entities(
    telemetry: ModbusTelemetry,
) -> list["HuaweiSolarModbusTelemetrySensorEntity"]:
    """Create all HA sensor entities for a ModbusTelemetry instance."""
    return [
        HuaweiSolarModbusTelemetrySensorEntity(telemetry, attr_key, name, unit, icon, extra)
        for attr_key, name, unit, icon, extra in _TELEMETRY_SENSORS
    ]


class HuaweiSolarModbusTelemetrySensorEntity(SensorEntity):
    """HA Sensor backed by ModbusTelemetry — no coordinator needed."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(
        self,
        telemetry: ModbusTelemetry,
        attr_key: str,
        name: str,
        unit: str | None,
        icon: str,
        extra: dict,
    ) -> None:
        self._telemetry = telemetry
        self._attr_key = attr_key
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_device_info = telemetry.device_info
        self._attr_unique_id = (
            f"{telemetry.serial_number}_modbus_telemetry_{attr_key}"
        )
        self._attr_native_value: Any = 0

        for k, v in extra.items():
            setattr(self, f"_attr_{k}", v)

        self._cb = self._on_telemetry_update

    async def async_added_to_hass(self) -> None:
        """Register callback with the telemetry singleton."""
        self._telemetry.add_listener(self._cb)
        # Populate immediately
        snap = self._telemetry.snapshot()
        self._attr_native_value = snap.get(self._attr_key, 0)

    async def async_will_remove_from_hass(self) -> None:
        """Deregister callback."""
        self._telemetry.remove_listener(self._cb)

    @callback
    def _on_telemetry_update(self, snap: dict[str, Any]) -> None:
        """Receive a fresh snapshot and push to HA."""
        self._attr_native_value = snap.get(self._attr_key, 0)
        self.async_write_ha_state()
