"""Constants for the Huawei Solar integration."""

from datetime import timedelta
from typing import Any

DOMAIN = "huawei_solar"
DEFAULT_PORT = 502
DEFAULT_USERNAME = "installer"

CONF_SLAVE_IDS = "slave_ids"
CONF_ENABLE_PARAMETER_CONFIGURATION = "enable_parameter_configuration"


def elevated_permissions_enabled(entry: Any) -> bool:
    """Whether write/control access (services, number/select/switch/button
    entities) is enabled for a config entry.

    v2.0.15 FIX (external ICS review, this release): checks entry.options
    FIRST, falling back to entry.data -- NOT entry.data alone, which is
    where every call site checked prior to this release. This exists
    specifically because CONF_ENABLE_PARAMETER_CONFIGURATION now has two
    legitimate storage locations depending on how it was set:

      - entry.data: written once, during initial setup (config_flow.py's
        own async_step_setup_network), where the choice is validated
        against the live device's own actual write-permission. Every
        installation that existed before this release has its value
        here, and nothing about this release changes that.
      - entry.options: written by BatteryHealthOptionsFlowHandler,
        alongside CONF_BH_ENABLED and CONF_SYNC_POWER_DEDICATED_READS --
        the "Configure" screen, changeable at any time without touching
        connection details or re-validating against the device (a
        deliberate trade-off; see that flow's own schema comment).

    A single, shared helper (used by every one of this integration's
    own read sites, rather than each repeating its own two-level .get()
    chain) exists specifically so that trade-off -- and the precedence
    between the two locations -- only has to be gotten right once, not
    independently re-derived at every call site with the attendant risk
    of one of them drifting out of sync with the others.

    `entry` is typed Any, not ConfigEntry, so this module can remain
    free of any Home Assistant import -- see this file's own use by
    test_const_services.py, which imports const.py directly and depends
    on it having no HA or third-party dependency to do so.
    """
    options_value = entry.options.get(CONF_ENABLE_PARAMETER_CONFIGURATION)
    if options_value is not None:
        return bool(options_value)
    return bool(entry.data.get(CONF_ENABLE_PARAMETER_CONFIGURATION, False))

DATA_DEVICE_DATAS = "device_datas"
DATA_SYNC_POWER_COORDINATOR = "sync_power_coordinator"

INVERTER_UPDATE_INTERVAL = timedelta(seconds=30)
POWER_METER_UPDATE_INTERVAL = timedelta(seconds=30)
ENERGY_STORAGE_UPDATE_INTERVAL = timedelta(seconds=30)
SYNC_POWER_UPDATE_INTERVAL = timedelta(seconds=10)

# v2.0.0 (V2_ARCHITECTURE_DESIGN.md §8.2) — how many seconds of age-spread
# across the four power-flow registers in the REGULAR cache is acceptable
# before SynchronizedPowerCoordinator's dedicated read is skipped in favour
# of the cache. Derived directly from the operator's real hardware
# constraints, not picked arbitrarily:
#   - the device's own Modbus register only refreshes at 1 Hz -- readings
#     within about a second aren't merely close, they're the same
#     underlying value, so this tolerance is comfortably above that floor.
#   - the four devices are not phase-locked (no PTP), but each device's own
#     internal sampling loop runs ~40-50 Hz -- the resulting cross-device
#     jitter this could add is on the order of 20-25 ms, negligible here.
#   - sensor accuracy is Class 1.0 (+/-1%) for power on three of the four
#     channels -- a few seconds of age-spread is well inside what the
#     sensors themselves could resolve as materially different moments.
#   - cross-checked against the dedicated read's own measured performance
#     (sample_span_ms, Defect V/Finding 9): a healthy dedicated read
#     typically already achieves sub-second spread, so this tolerance
#     never accepts materially worse alignment than what it replaces.
SYNC_POWER_CACHE_ALIGNMENT_TOLERANCE_S: float = 3.0

# v2.0.0b (MOD-02/MOD-04, external ICS audit -- confirmed): the
# whole-operation deadline for SynchronizedPowerCoordinator's dedicated-
# read fallback (up to 4 sequential reads, each individually bounded by
# UPDATE_TIMEOUT/35s, but with no bound on the SUM). Worst case before
# this fix: 4 x 35s = 140s for one update against a nominal 10s cadence.
# 18s chosen from the report's own recommended 15-20s range: comfortably
# above what 4 healthy sequential reads actually take (each typically
# well under a second per the dedicated read's own measured
# sample_span_ms), while bounding the pathological case to a small
# multiple of the 10s cadence rather than 14x it. Enforced as an explicit
# deadline CHECKED BEFORE STARTING each read, not a single outer
# asyncio.timeout() wrapping the whole sequence -- the latter would
# deliver cancellation through whichever read happens to be in flight
# when the deadline fires, discarding any already-collected partial
# results (an explicit per-read check preserves them, matching the
# report's own "no further read may start" framing).
SYNC_POWER_POLL_DEADLINE = timedelta(seconds=18)

# UPDATE_TIMEOUT is intentionally shorter than the update intervals so that
# a hung request is cancelled before the next poll cycle begins.
# Raised from 29s → 35s to give slow/busy inverters a bit more breathing room
# while still ensuring we don't stack back-to-back requests.
UPDATE_TIMEOUT = timedelta(seconds=35)

# configuration can only change when edited through FusionSolar web or app
CONFIGURATION_UPDATE_INTERVAL = timedelta(minutes=15)

# optimizer data is only refreshed every 5 minutes by the inverter.
OPTIMIZER_UPDATE_INTERVAL = timedelta(minutes=5)
OPTIMIZER_UPDATE_TIMEOUT = timedelta(minutes=2)

# v1.3.14 (Defect N): bound for the ONE-TIME optimizer discovery scan
# (device.get_optimizer_system_information_data(), a vendor-library file
# read) performed once per inverter with optimizers, directly on the entry
# setup critical path in _setup_inverter_device_data -- before the
# optimizer coordinator's own first refresh even exists to be backgrounded
# (that part was already fixed for Defect G). This call is outside
# ModbusGuard, had no explicit timeout of its own, and its own
# `except Exception` guard does not stop it from extending the overall
# setup duration before failing -- contributing to the same setup-timeout
# risk as Defects G, M, and the create_device_instance() latency this
# session found directly. No field measurement exists yet for this
# specific call's typical duration (unlike create_device_instance's ~30-40s
# worst case), so 30s is a reasoned, moderate bound: generous for a
# single-frame file read, but meaningfully shorter than
# OPTIMIZER_UPDATE_TIMEOUT (2 min, which governs the coordinator's regular,
# backgrounded polling, not this one-time setup-path scan). On timeout,
# optimizer entities are simply skipped for this setup pass -- identical
# to the existing `except Exception` fallback already in place, and
# retried automatically on the next reload.
OPTIMIZER_DISCOVERY_TIMEOUT = timedelta(seconds=30)

# v1.3.9 (Defect H): bound for the write-permission probe performed once per
# SUN2000 device during SENSOR PLATFORM SETUP (sensor.py:create_sun2000_entities)
# to decide whether to add one optional entity (Active Power Control Mode).
# This is a raw device-level read+write, outside ModbusGuard/adaptive pacing
# entirely, and was previously awaited with no bound at all -- on a
# slow/still-recovering device it could stall platform setup for as long as
# the vendor library's own per-request timeout allowed (up to ~20-30s across
# the read+write pair), once per inverter, on every single boot and reload.
# A short, dedicated timeout here means a slow device costs at most this
# much extra setup time per device, and simply skips the optional entity for
# this pass (it is re-attempted on the next reload) rather than blocking
# everyone. Deliberately shorter than UPDATE_TIMEOUT: a healthy device
# answers this in well under a second, so there is nothing to gain by
# waiting as long as we would for a real data poll.
WRITE_PERMISSION_CHECK_TIMEOUT = timedelta(seconds=5)

# v1.3.14 (Defect M): bound for create_device_instance() -- the very first
# call in async_setup_entry, establishing the connection and running the
# vendor library's own device-detection sequence (several individual
# register reads, e.g. DAYLIGHT_SAVING_TIME). A field traceback confirmed
# this specific call can be slow enough, right after a reconnect, that Home
# Assistant's OWN external config-entry setup timeout cancels the whole
# setup with an unhandled asyncio.CancelledError -- which is not a
# TimeoutError and is not caught by `except Exception` (CancelledError is a
# BaseException), so it bypassed every existing ConfigEntryNotReady
# handler in async_setup_entry.
#
# Rather than trying to catch and reinterpret an external cancellation
# (fighting normal asyncio cancellation semantics), this bounds the SAME
# call with our own, shorter timeout, so that on a slow/still-reconnecting
# device we are always the one who gives up first, in a controlled way --
# raising our own ConfigEntryNotReady, which Home Assistant already knows
# how to retry cleanly -- rather than being caught by an external
# cancellation with a raw, alarming traceback. 45s is chosen generously
# above the ~30-40s worst case directly observed for this phase in the
# field (multiple individual slow register reads during detection), while
# still meaningfully shorter than the ~50s+ at which the external
# cancellation was observed to fire for the entry's setup as a whole.
DEVICE_CONNECT_TIMEOUT = timedelta(seconds=45)

# v2.0.0b (MOD-08, external ICS audit -- confirmed): eight config-flow
# call sites did `await client.connect()` with no timeout of their own --
# a stalled TCP/serial connection attempt could hang the configuration
# flow indefinitely. Deliberately a SEPARATE, SHORTER constant from
# DEVICE_CONNECT_TIMEOUT above, not a reuse of it: connection
# establishment (opening the socket/port) and device identification
# (the read exchanges DEVICE_CONNECT_TIMEOUT already bounds) are
# different operations with different expected durations -- a healthy
# connect() completes in well under a second; DEVICE_CONNECT_TIMEOUT's
# 45s budget exists for the heavier, multi-exchange identification phase
# that follows it, not the connection itself. 20s chosen generously above
# a healthy connect (which should be near-instant) while still bounding
# the pathological case (an unreachable host/port hanging on TCP's own
# retry behaviour) well short of DEVICE_CONNECT_TIMEOUT's 45s.
MODBUS_CONNECT_TIMEOUT = timedelta(seconds=20)

# v2.0.0a (F02, external ICS audit -- confirmed): slave discovery iterates
# up to 18 unit IDs ([0, 100, 1..16]) with no per-probe bound of its own --
# duration was governed entirely by whatever the underlying vendor
# library's own timeout happened to be, not an integration-owned budget.
# 5s per probe: this is connectivity discovery, not a full data poll -- a
# genuinely present device answers a single get_device_infos/
# detect_device_type call in well under a second on a healthy bus; a
# non-responsive unit ID should not be allowed to hold up the scan
# anywhere near as long as DEVICE_CONNECT_TIMEOUT (which covers a much
# heavier full device-instance construction, not one discovery probe).
DISCOVERY_PROBE_TIMEOUT = timedelta(seconds=5)

# v2.0.0a (F02): the whole-scan deadline. Without this, a silent/
# non-responsive bus could make discovery consume up to
# 18 * DISCOVERY_PROBE_TIMEOUT = 90s even with the per-probe bound above --
# still a very long, uninterruptible stretch of config-flow UI with no
# feedback. Set equal to that worst case rather than shorter: the
# per-probe timeout is already the real defence against any single probe
# misbehaving; this is the backstop against the sum across all of them,
# not a tighter budget layered on top for its own sake.
DISCOVERY_TOTAL_TIMEOUT = timedelta(seconds=90)

# v1.3.18 (Defect U/Finding 3, independent ICS audit of v1.3.17): bound for
# primary_device.client.disconnect() during async_unload_entry. This runs
# BEFORE every teardown loop that follows it (telemetry, the adaptive
# controller, keep-alive, battery health, the shared guard) -- a wedged or
# half-dead transport blocking here would prevent ALL of that cleanup from
# ever running. A clean disconnect should be near-instant; 10s is generous
# headroom without meaningfully delaying unload in the normal case.
DISCONNECT_TIMEOUT = timedelta(seconds=10)

# v2.0.9 (Phase 4.9, this release -- old DEF-012, external ICS quality/
# defect/architecture audit -- confirmed): both AdaptiveModbusController.
# async_load() and BatteryHealthManager.async_initialize() call self.
# _store.async_load() on the config-entry setup critical path (await'ed
# directly from __init__.py's own async_setup_entry) with no timeout of
# their own -- each already has an `except Exception` broad enough to
# treat a load failure as "start fresh" gracefully, but nothing bounded
# how long the awaited load itself could take. A genuinely stalled HA
# Store read (disk contention, a wedged filesystem) would block entry
# setup indefinitely, for what's explicitly optional, best-effort
# persisted state -- exactly the class of problem DISCONNECT_TIMEOUT
# above already exists to prevent for the analogous disconnect case.
# A plain local HA Store read of a small JSON file should be near-
# instant; 10s (matching DISCONNECT_TIMEOUT's own value and reasoning)
# is generous headroom without meaningfully delaying setup in the
# normal case.
STORAGE_LOAD_TIMEOUT = timedelta(seconds=10)

# v1.3.19 (Defect V/Finding 8, independent ICS audit): bound for the
# maximum-power validation read in services.py's _validate_power_value(),
# performed BEFORE any write, while the per-device write lock (Defect R,
# v1.3.15) is already held for the entire service call. An unbounded read
# here doesn't just risk hanging this one service call -- it now also
# blocks every OTHER write action for the same device for as long as it
# takes, since Defect R made this lock genuinely exclusive. A single
# register read; a healthy device answers in well under a second (see the
# same reasoning as WRITE_PERMISSION_CHECK_TIMEOUT / STATIC_BOUND_READ_TIMEOUT
# elsewhere in this file), so there's nothing to gain by waiting longer.
SERVICE_VALIDATION_READ_TIMEOUT = timedelta(seconds=10)

# v1.3.11 (Defect J, reported by an independent ICS audit and confirmed
# against source): bound for the static min/max register reads performed
# once per number entity, during NUMBER PLATFORM SETUP
# (number.py:HuaweiSolarNumberEntity.create()), to populate a fixed
# native_min_value/native_max_value. Like WRITE_PERMISSION_CHECK_TIMEOUT
# above, these are raw device.client.get() calls -- outside ModbusGuard,
# with no timeout and no exception handling at the call site -- awaited
# once per entity before async_add_entities() returns. On an installation
# with several number entities carrying static bounds, and/or a device that
# is slow or busy (the exact condition this session's field investigation
# found to be common right after a reload), this extends platform setup
# per entity and adds avoidable, unpaced Modbus traffic during the same
# startup window Defects H and I already targeted.
STATIC_BOUND_READ_TIMEOUT = timedelta(seconds=5)

# When the inverter is in night/sleep mode (PV power ≈ 0) all coordinators
# slow to this interval.  Most registers are frozen at night so polling faster
# than 5 minutes is wasteful and stresses the Modbus interface.
NIGHT_POLL_INTERVAL = timedelta(minutes=5)

# ── Modbus timeout / retry back-off ─────────────────────────────────────────
# After this many consecutive timeouts the coordinator starts backing off to
# avoid hammering an unresponsive inverter and let the Modbus bus recover.
MAX_CONSECUTIVE_TIMEOUTS = 3

# Initial wait after the first burst of timeouts (seconds).  Subsequent
# bursts double the wait up to MODBUS_RETRY_MAX_WAIT.
MODBUS_RETRY_BASE_WAIT = timedelta(seconds=10)
MODBUS_RETRY_MAX_WAIT = timedelta(seconds=120)

# v2.0.0a (F11, external ICS audit -- confirmed, refined during
# verification): the backoff delay's jitter used to be purely
# proportional (±10% of the delay), which is weakest exactly where a
# common-failure scenario needs it most -- at the FIRST retry
# (consecutive=1, delay=MODBUS_RETRY_BASE_WAIT=10s), proportional jitter
# is only ±1s, so several coordinators hitting the same shared bus/device
# failure simultaneously would all wake up and retry within a ~2-second
# window of each other. This floor guarantees a meaningful absolute
# spread even at the shortest delays; deep backoff's proportional jitter
# already exceeds this floor on its own (±12s at the 120s cap) and is
# left untouched. 2.0s chosen as a reasoned, deliberately moderate
# doubling of the previous effective jitter at the base delay (±1s ->
# ±2s) -- wide enough to meaningfully de-cluster simultaneous first
# retries, not so wide it delays legitimate recovery for its own sake.
MIN_BACKOFF_JITTER_S: float = 2.0

# ── Service names ────────────────────────────────────────────────────────────
SERVICE_FORCIBLE_CHARGE = "forcible_charge"
SERVICE_FORCIBLE_DISCHARGE = "forcible_discharge"
SERVICE_FORCIBLE_CHARGE_SOC = "forcible_charge_soc"
SERVICE_FORCIBLE_DISCHARGE_SOC = "forcible_discharge_soc"
SERVICE_STOP_FORCIBLE_CHARGE = "stop_forcible_charge"

SERVICE_RESET_MAXIMUM_FEED_GRID_POWER = "reset_maximum_feed_grid_power"
SERVICE_SET_DI_ACTIVE_POWER_SCHEDULING = "set_di_active_power_scheduling"
SERVICE_SET_ZERO_POWER_GRID_CONNECTION = "set_zero_power_grid_connection"
SERVICE_SET_MAXIMUM_FEED_GRID_POWER = "set_maximum_feed_grid_power"
SERVICE_SET_MAXIMUM_FEED_GRID_POWER_PERCENT = "set_maximum_feed_grid_power_percent"
SERVICE_SET_TOU_PERIODS = "set_tou_periods"
SERVICE_SET_CAPACITY_CONTROL_PERIODS = "set_capacity_control_periods"
SERVICE_SET_FIXED_CHARGE_PERIODS = "set_fixed_charge_periods"
# v2.0.12 (Battery Phase 5B, this release): see set_pack_install_date's
# own docstring (services.py).
SERVICE_SET_PACK_INSTALL_DATE = "set_pack_install_date"

# v2.0.15 (experimental identification release): control services for the
# opt-in GAP/POLL excitation schedule -- see excitation_controller.py's
# own module docstring for the full design. Deliberately three separate
# services rather than one with a mode parameter: enable/disable/resume
# are conceptually distinct actions with different prerequisites (resume
# only makes sense after a halt), and separate services let each get its
# own clear description and validation in the HA services UI rather than
# a single service whose behavior depends on an opaque mode string.
SERVICE_ENABLE_EXCITATION = "enable_excitation"
SERVICE_DISABLE_EXCITATION = "disable_excitation"
SERVICE_RESUME_EXCITATION_AFTER_HALT = "resume_excitation_after_halt"

SERVICES = (
    SERVICE_FORCIBLE_CHARGE,
    SERVICE_FORCIBLE_DISCHARGE,
    SERVICE_FORCIBLE_CHARGE_SOC,
    SERVICE_FORCIBLE_DISCHARGE_SOC,
    SERVICE_STOP_FORCIBLE_CHARGE,
    SERVICE_RESET_MAXIMUM_FEED_GRID_POWER,
    SERVICE_SET_DI_ACTIVE_POWER_SCHEDULING,
    SERVICE_SET_ZERO_POWER_GRID_CONNECTION,
    SERVICE_SET_MAXIMUM_FEED_GRID_POWER,
    SERVICE_SET_MAXIMUM_FEED_GRID_POWER_PERCENT,
    SERVICE_SET_TOU_PERIODS,
    SERVICE_SET_CAPACITY_CONTROL_PERIODS,
    SERVICE_SET_FIXED_CHARGE_PERIODS,
    # v2.0.12 (Battery Phase 5B, this release): a real gap this exact
    # completeness test caught before shipping -- without this, the
    # service would have registered correctly but never been
    # unregistered on integration unload, a genuine (if minor) resource
    # leak this test exists specifically to prevent.
    SERVICE_SET_PACK_INSTALL_DATE,
    # v2.0.15 (experimental identification release): see these constants'
    # own comment above for why three separate services rather than one.
    SERVICE_ENABLE_EXCITATION,
    SERVICE_DISABLE_EXCITATION,
    SERVICE_RESUME_EXCITATION_AFTER_HALT,
)

# ── Adaptive Modbus learning ──────────────────────────────────────────────────
# The adaptive controller divides the 24-hour day into 15-minute time slots
# and learns optimal Modbus parameters (poll interval, gap, timeout) for each.
# Parameters below define the learning model and parameter bounds.

# Slot granularity: 96 slots × 15 min = 24 hours
#: v1.2.2 - shared settling period for BOTH learning subsystems (battery
#: health and adaptive Modbus). During HA startup the event loop is congested
#: by other integrations, recorder migration and database work; Modbus RTT and
#: timeouts observed then reflect HOME ASSISTANT, not the inverter. The
#: adaptive learner cannot distinguish the two, so it must not learn from them.
LEARNING_SETTLING_PERIOD_S: float = 300.0

#: v1.2.3 (Defect B) — cold-start baseline for max_queue_depth.
#:
#: Deliberately 2, not the fully-conservative 1. Queue depth does NOT create
#: concurrency (ModbusGuard holds a single asyncio.Lock); it only decides how
#: many callers may WAIT before requests are shed. With up to five
#: sub-coordinators per inverter, and more than one inverter on a shared bus,
#: a depth of 1 sheds aggressively on exactly the unproven slots this blending
#: exists to protect. 2 keeps a cautious posture without turning cold start
#: into a shedding machine.
ADAPTIVE_QUEUE_DEPTH_COLD_START: int = 2

ADAPTIVE_SLOT_MINUTES: int = 15
ADAPTIVE_SLOT_COUNT: int = 96          # 24 * 60 // ADAPTIVE_SLOT_MINUTES

# Daily decay applied to all slot statistics on each new day.
# 0.85^1 = 85 % retained, 0.85^14 ≈ 10 % — 14-day effective memory.
ADAPTIVE_DECAY_FACTOR: float = 0.85

# Number of weighted requests per slot for "full" confidence (1.0).
# At 20–30 s polling: ~30 requests/slot/day → ~5 days for full confidence.
# 150 is the sweet spot: statistically stable (a single bad day contributes
# < 33 % weight at full confidence) while adapting meaningfully within a week.
# Gemini suggested 60 (too fast — one bad day dominates); we use 150.
ADAPTIVE_FULL_CONFIDENCE_N: float = 150.0

# How many raw RTT samples to store per slot for P95 estimation.
ADAPTIVE_RTT_SAMPLE_SIZE: int = 50

# Duration after a detected state transition during which elevated parameters
# are maintained regardless of the slot's historical failure rate.
ADAPTIVE_TRANSITION_DURATION_MINUTES: int = 10

# ── Adaptive parameter bounds ─────────────────────────────────────────────────
# Poll interval: 20 s (healthy slots) → 180 s (high-failure slots).
# Night mode (5 min) always takes precedence.
# 20 s: meaningful improvement for power-flow card; safe with bus-level guard.
# 180 s: significant daytime back-off without reaching night-mode territory.
# Gemini suggested 15 s min (too aggressive for inverter CPU) and 300 s max
# (indistinguishable from night mode; confusing to users).
ADAPTIVE_POLL_MIN = timedelta(seconds=20)
ADAPTIVE_POLL_MAX = timedelta(seconds=180)

# Cold-start (zero-confidence) poll baseline — expressed as a SEPARATE
# constant so it is independent of ADAPTIVE_POLL_MIN.  At confidence=0 the
# blending formula uses this value rather than ADAPTIVE_POLL_MIN, ensuring
# unknown slots default to a moderate rate rather than the fastest rate.
# Lowering ADAPTIVE_POLL_MIN to 20 s must not change cold-start behaviour.
ADAPTIVE_POLL_COLD_START = timedelta(seconds=60)

# Inter-request gap: 150 ms (normal) to 500 ms (high-RTT / transition).
# INTENTIONALLY NOT REDUCED BELOW 150 ms despite Gemini's 30 ms suggestion.
# The SUN2000 Modbus FSM needs ~100 ms to reset its receive buffer after each
# response, regardless of TCP link quality.  150 ms is the safe hardware floor.
# Lowering to 30 ms causes pervasive 0x06 SLAVE_DEVICE_BUSY responses —
# the exact failure mode the BUSY retry logic (opt. 2) is designed to handle.
ADAPTIVE_GAP_MIN = timedelta(milliseconds=150)
ADAPTIVE_GAP_MAX = timedelta(milliseconds=500)

# Per-request timeout: 15 s (healthy) → 60 s (stressed inverter).
# 15 s min: safe floor for transition-window slow responses.  Gemini's 10 s
# would fire during legitimate 8–12 s responses on a loaded inverter.
# 60 s max: the keep-alive probe (opt. 3) now handles dead-connection
# detection within 45 s, so the coordinator timeout is purely a
# 'live-but-slow' guard.  60 s covers multi-chunk slow reads; 90 s was
# needed only when the timeout was also the dead-socket detector.
ADAPTIVE_TIMEOUT_MIN = timedelta(seconds=15)
ADAPTIVE_TIMEOUT_MAX = timedelta(seconds=60)

# Failure rate thresholds used to derive queue depth and poll interval.
# Above HIGH → use max params; between LOW and HIGH → interpolate.
ADAPTIVE_FAILURE_RATE_LOW: float = 0.03    # 3 %
ADAPTIVE_FAILURE_RATE_HIGH: float = 0.15   # 15 %

# ── Optimisation 1: Bus-level guard ──────────────────────────────────────────
# ModbusGuard is keyed on connection endpoint (host:port) rather than serial
# number for multi-inverter (sub-device) topologies.  All slaves on the same
# physical RS485 bus share one guard so their requests never overlap on the wire.
# (No runtime constant needed — the key is derived in __init__.py)

# ── Optimisation 2: SLAVE_DEVICE_BUSY (0x06) retry ───────────────────────────
# On Modbus exception 0x06, pause this long then retry once before counting
# the request as a failure.  The 0x06 response means the inverter is alive but
# its CPU is saturated — a brief pause almost always succeeds on the retry.
BUSY_RETRY_PAUSE = timedelta(milliseconds=600)
# Maximum number of 0x06 retries per original request before giving up.
BUSY_MAX_RETRIES: int = 2

# ── Optimisation 3: Keep-alive / connection health probe ─────────────────────
# A lightweight background task reads a single static register every
# KEEPALIVE_INTERVAL seconds to prevent the SUN2000 from silently dropping the
# TCP connection after ~60 s of idle.  Also used as a health probe: if the
# read fails the task triggers a reconnect before the next poll cycle hits a
# dead socket.
KEEPALIVE_INTERVAL = timedelta(seconds=45)

# Periodic aggregate telemetry snapshot cadence (telemetry_capture.py's
# TelemetryCapture, opt-in via its own switch). Tied to roughly the main
# coordinator's own poll cadence (30s) so each snapshot reflects genuinely
# new data, not a repeat of the same numbers -- a much finer interval
# would just write duplicate-looking snapshots between real polls; a much
# coarser one would blur short-lived spikes the whole point of this
# capture is to see. At 10GB of available log space (per the operator's
# own stated budget) even a multi-day capture at this cadence is
# trivial -- this was chosen for signal resolution, not to conserve disk.
TELEMETRY_CAPTURE_INTERVAL = timedelta(seconds=30)

# Cadence for the periodic aggregate telemetry-capture switch
# (telemetry_capture.py). Chosen deliberately fine-grained rather than
# coarse: the whole point is a real time series to assess the Physical
# Demand Planner question without a second deployment, and resolution
# cannot be recovered after the fact if a coarser interval missed a
# short-lived spike. Not tied to any single coordinator's own poll
# interval -- this captures ACROSS all of them on one shared timer, so a
# combined snapshot always reflects the same moment for every coordinator
# on the entry, not whichever one happened to poll most recently.
TELEMETRY_CAPTURE_INTERVAL = timedelta(seconds=30)
# Register used for the keep-alive read (must be STATIC and single-word).
# Model ID is 1 register, always readable, never causes side effects.
KEEPALIVE_REGISTER = "model_id"

# ── Optimisation 4: Batch chunking ───────────────────────────────────────────
# Stale register lists larger than this threshold are split into chunks before
# being passed to batch_update(), with a short pause between chunks.  This
# prevents a single Modbus burst from occupying the inverter CPU for > ~300 ms,
# which is a primary trigger for 0x06 BUSY responses during high-load windows.
BATCH_CHUNK_SIZE: int = 40
# v2.0.9 FIX (today's ICS audit, §21/Priority 3 -- confirmed): this
# comment previously said "inside the guard lock (gap enforced by
# guard)" -- stale and wrong. The actual pause (update_coordinator.py,
# where BATCH_INTER_CHUNK_PAUSE is used) runs OUTSIDE the guard lock,
# deliberately, so other queued clients can be served between chunks of
# the same logical poll rather than being blocked out for the pause's
# own duration too. Confirmed directly against the real call site's own
# comment, which was already correct -- only this constant's definition
# site had drifted out of sync with it.
BATCH_INTER_CHUNK_PAUSE = timedelta(milliseconds=80)

# v2.0.11 (Phase 5.3, this release -- service-time-aware chunking):
# confirmed via real field data across three independent captures
# (2h/6h/8.9h/20.5h, spanning a full day-night cycle) that certain
# register groups are structurally slow regardless of chunk size --
# battery per-pack telemetry, second-inverter status registers, and
# storage-config parameters together were only ~44% of total traffic
# but accounted for over 80% of all service-time-tail events (>3s).
# BATCH_CHUNK_SIZE above is a single, uniform cap applied regardless
# of what a chunk actually contains -- this pair of constants lets
# chunking respond to EMPIRICAL, per-register service-time history
# instead: a register with a learned EWMA service time (see
# HuaweiSolarUpdateCoordinator._register_service_ewma) above the
# threshold below gets a smaller chunk cap, so a chunk built around it
# is both cheaper to retry and blocks the rest of the poll for less
# time if it IS slow.
#
# SERVICE_TIME_SLOW_THRESHOLD_MS matches the exact 3000ms ">3s" bar
# already used consistently throughout this whole investigation's own
# analysis (both external ICS audits and this project's own field
# reviews), not a newly-invented number.
SERVICE_TIME_SLOW_THRESHOLD_MS: float = 3000.0
# SERVICE_TIME_AWARE_CHUNK_SIZE is a judgment call, not derived from
# the field data the way the threshold above is -- chosen as a
# meaningfully smaller fraction of BATCH_CHUNK_SIZE (roughly a
# quarter) so a known-slow group's own chunks are genuinely cheaper to
# retry and block less of the poll, without being so small that a
# structurally slow group needs an excessive number of separate
# chunks (each with its own admission/gap overhead) to get through.
# Flag if a different value is wanted once this has been observed
# running for a while.
SERVICE_TIME_AWARE_CHUNK_SIZE: int = 10
# Per-observation EWMA decay for the register service-time tracker.
# Deliberately a DIFFERENT value from BUS_HEALTH_EWMA_DECAY (0.98,
# modbus_guard.py, Phase 5.2) -- that signal updates on every admission
# (~11/minute in real field data), so a slow decay still reflects
# recent minutes. A given register is read far less often (once per
# its own tier's TTL, often every 30s-several minutes), so the SAME
# 0.98 would give this tracker an effective memory spanning multiple
# hours to keep a "recent" character -- too slow to reflect a register
# group's own current behaviour. A faster decay (shorter observation-
# count half-life) keeps this responsive on a comparable WALL-CLOCK
# timescale despite far fewer observations per register.
REGISTER_SERVICE_TIME_EWMA_DECAY: float = 0.9

# v2.0.0a (F03, external ICS audit -- confirmed): the whole-poll deadline.
# Each chunk already has its own timeout (effective_timeout, adaptive,
# 15-60s) and BUSY retries add further delay on top -- but nothing bounded
# the SUM across every chunk in one poll. A cold-start or post-reconnect
# poll (every register simultaneously stale after invalidate_all()) can
# realistically produce 15-25+ chunks, given the register map's scattered
# address layout (see Defect E) -- the audit's own worked example (20
# chunks at a 30s effective timeout = ~10 minutes before the first
# failure is even reported) is a realistic worst case, not a strawman,
# confirmed by checking BATCH_CHUNK_SIZE and the real chunk count this
# session's own field captures have shown.
#
# 120s (2 minutes) chosen deliberately: generous enough that a legitimate
# multi-chunk cold-start poll (fast per-chunk pace under normal, healthy
# conditions) can complete without ever approaching it, while preventing
# the pathological case (every chunk genuinely timing out for its full
# adaptive budget) from consuming many multiples of the coordinator's own
# ~30s update_interval -- which would otherwise risk overlapping into the
# NEXT poll cycle before this one even finishes.
BATCH_POLL_DEADLINE = timedelta(seconds=120)

# ── Optimisation 5: Write-back verification ───────────────────────────────────
# Delay before the post-write verification read is issued.  Long enough for the
# inverter to apply the setting, short enough to catch a missed write quickly.
WRITE_VERIFY_DELAY = timedelta(seconds=3)
# Maximum number of re-read retries if the first verification read still shows
# the old value (covers slow-applying settings like working-mode changes).
WRITE_VERIFY_RETRIES: int = 2

# v2.0.0b (MOD-05, external ICS audit -- confirmed): every write call site
# routed writes through ModbusGuard (v2.0.0a, F05) but never bounded the
# underlying device.set() call itself with a timeout of its own -- the
# guard provides serialisation, not a deadline. A stalled write held the
# guard indefinitely, starving every other coordinator on the endpoint.
# 15s chosen deliberately at the LOWER end of the adaptive read timeout
# range (15-60s, effective_timeout) rather than reusing that range
# directly: a write is a single-register set, a structurally simpler and
# faster operation than a full batch read, so it doesn't need the same
# generous adaptive ceiling a multi-register read does.
WRITE_TIMEOUT = timedelta(seconds=15)

# v2.0.0b (MOD-06/MOD-19, external ICS audit -- confirmed): the
# whole-sequence deadline for a multi-register logical write command
# (e.g. "start forcible charge": 4 sequential writes that must apply
# together or not at all). Holding the guard across a sequence already
# guarantees atomicity against other bus traffic (per Defect P's
# fairness reasoning, unchanged); this bounds how long that exclusive
# hold may last. Deliberately NOT simply len(writes) * WRITE_TIMEOUT --
# that would scale unboundedly with sequence length and defeat its own
# purpose as a genuine ceiling. A fixed, generous budget covering the
# longest real sequence in this codebase (5 writes, stop_forcible_charge)
# with headroom, not a per-write multiple.
WRITE_SEQUENCE_TIMEOUT = timedelta(seconds=30)

# ── Optimisation 6: Priority polling during back-off ─────────────────────────
# Tier names eligible for reduced-frequency reads during back-off.
# FAST registers are always read; NORMAL may be polled at BACKOFF_NORMAL_DIVISOR
# (every Nth poll cycle); SLOW/STATIC are deferred entirely until recovery --
# UNLESS a register has gone unread for longer than
# REGISTER_STARVATION_CEILING_S past its own due-time (Defect Y, v1.3.21).
BACKOFF_FAST_ALWAYS: bool = True
BACKOFF_NORMAL_DIVISOR: int = 4   # read NORMAL registers every 4th back-off cycle

# v1.3.21 (Defect Y): SLOW/STATIC deferral during back-off has no ceiling of
# its own -- a register can, in principle, be deferred for as long as
# back-off itself persists, which field evidence showed can run past 20
# minutes under real, sustained contention (one register, BMS temperature,
# was observed to go completely unread across a 107-minute capture). Every
# affected register is read-only telemetry -- there is no control-loop
# consequence to it being briefly stale, so the fix favours guaranteeing an
# upper bound over preserving tier purity during a rough patch.
#
# Measured against `overdue_by()` (time PAST the register's own due-time,
# not raw age since read -- see register_cache.py for why that distinction
# matters), not a flat "5 minutes since last read": SLOW's own 900s base TTL
# already means a SLOW register is >=900s old the instant it first becomes
# due at all, so thresholding raw age at 300s would trigger on every single
# SLOW/STATIC register the moment back-off started, defeating tier-based
# deferral entirely rather than merely bounding it.
#
# 300s (5 min) chosen directly per the operator's own stated tolerance: "if
# they haven't been updated for 5 minutes, they become more important."
REGISTER_STARVATION_CEILING_S: float = 300.0

# v2.0.0 (V2_ARCHITECTURE_DESIGN.md §8.1) — energy counters get their own,
# TIGHTER promotion ceiling than REGISTER_STARVATION_CEILING_S above,
# extending the same starvation-promotion mechanism with a shorter fuse
# specifically for energy-relevant registers. First line of defence: try
# harder to get a fresh read quietly, before anything is ever visible to a
# user, given how time-sensitive energy data is for the Energy Dashboard's
# hourly rollup (a hard operator constraint: a missing reading breaks the
# calculation outright; a delayed one doesn't). 90s is roughly 2-3x the
# base NORMAL-tier TTL (30s) -- aggressive enough to resolve most
# contention well within the availability ceiling below, not so aggressive
# it fights unnecessarily with legitimate back-off pacing.
ENERGY_PROMOTION_CEILING_S: float = 90.0

# v2.0.0 (V2_ARCHITECTURE_DESIGN.md §8.1) — the SECOND stage: how long an
# energy counter can stay UNCERTAIN (served, not unavailable) before it
# lazily becomes BAD/EXPIRED. Deliberately LONGER than
# REGISTER_STARVATION_CEILING_S, not shorter -- reasoned directly against
# how HA's statistics engine actually works (checked, not assumed): the
# sum delta is value-to-value, not time-weighted, so a late-but-genuine
# reading still lands on the correct total regardless of how long the gap
# was; the real risk is short-term-statistics snapshot misattribution
# (a fixed 5-minute clock sampling whatever the current state happens to
# be), which scales with gap LENGTH. 600s = two snapshot windows, bounding
# the worst case to "the two most recent windows absorb the eventual
# jump" rather than one window absorbing an hour's growth. This should
# rarely be reached in practice -- ENERGY_PROMOTION_CEILING_S above is
# designed to resolve the great majority of contention well before this
# fires at all.
ENERGY_AVAILABILITY_CEILING_S: float = 600.0

# Caps how many starved registers get promoted into a single back-off cycle.
# Deliberately small: several SLOW/STATIC registers read together in the same
# original batch tend to share similar timestamps, so they can cross the
# starvation ceiling within moments of each other. Promoting all of them at
# once would inject a sudden burst of expensive SLOW/STATIC reads into a
# cycle that, by definition, is already in back-off because the bus is
# struggling -- working directly against the reason back-off exists.
# Promoting the single most-overdue one per cycle instead guarantees forward
# progress (every cycle drains the worst offender) without a burst, at the
# cost of the whole starved cohort clearing gradually rather than at once --
# an explicit, deliberate trade given every affected register is read-only.
REGISTER_STARVATION_PROMOTIONS_PER_CYCLE: int = 1

# ── Battery Health Index (v1.1.5) ─────────────────────────────────────────────
# Options-flow keys for the tunable constants (spec §10).  Defaults live in
# battery_health.BatteryHealthConfig; these keys override them per entry.
# Master kill switch (v1.1.7): lets a user disable the entire battery-health
# subsystem from the UI without editing files, if it ever misbehaves.
CONF_BH_ENABLED = "bh_enabled"

# v2.0.9 (Phase 3.1, this release): SynchronizedPowerCoordinator's own
# dedicated physical reads, made optional. Confirmed against two
# independent full-day field telemetry captures (2.0.7 and 2.0.8): the
# temporal alignment this coordinator's dedicated reads exist to
# guarantee is achieved essentially never in practice (96.5%-97.09%
# "temporally uncertain" across both captures) -- and separately
# confirmed that hourly/daily energy accuracy already comes entirely
# from accumulated device counters (ACCUMULATED_YIELD_ENERGY, GRID_
# ACCUMULATED_ENERGY, STORAGE_TOTAL_CHARGE/DISCHARGE), independent of
# this coordinator's own instantaneous-power alignment work. Defaults to
# True (dedicated reads ON), preserving today's behaviour for every
# existing installation unless a user explicitly opts out -- this
# integration serves more installations than the one this specific
# finding was field-validated against, and the live power-flow card
# some installations genuinely want is still a legitimate use case this
# default protects.
CONF_SYNC_POWER_DEDICATED_READS = "sync_power_dedicated_reads"

CONF_BH_RATED_CAPACITY_KWH = "bh_rated_capacity_kwh"
#: Finding D: true battery install/commissioning date (ISO yyyy-mm-dd).
#: Without it the calendar-aging forecast treats an already-aged battery as
#: new from the moment this integration first ran.
CONF_BH_INSTALL_DATE = "bh_install_date"
#: OPTIONAL entity_id of an ambient temperature sensor in the battery room.
#: Enables thermal-rise diagnostics (pack temperature above ambient), which
#: measure heat GENERATION directly - unlike inter-pack spread, which cannot
#: see all packs ageing together. Configurable rather than hardcoded so the
#: sensor can be replaced without code changes; absent or unavailable simply
#: disables the derived attributes.
CONF_BH_AMBIENT_ENTITY = "bh_ambient_entity"

#: v1.3.3 — refresh interval for SLOW-tier registers, seconds.
#: These are categorically expensive on Huawei hardware (~2.9 s fixed cost per
#: exchange that touches them, vs ~6 ms for FAST/NORMAL-only chunks), and they
#: are by definition slow-changing. Raising this is the only lever that reduces
#: TOTAL Modbus cost; tier separation only stops them delaying other reads.
CONF_SLOW_TIER_TTL_S = "slow_tier_ttl_s"
DEFAULT_SLOW_TIER_TTL_S = 900

#: v1.3.4's coalesce/night-defer options were REMOVED in v1.3.5. Coalescing
#: caused a production outage (every battery entity unavailable) within hours
#: of deployment — see AUDIT_1.3.5.md. Retired rather than merely defaulted
#: off, so a future edit cannot silently re-enable a mechanism now understood
#: to be actively harmful.
CONF_BH_WARRANTY_THROUGHPUT_KWH = "bh_warranty_throughput_kwh"
CONF_BH_WEIGHT_CAPACITY = "bh_weight_capacity"
CONF_BH_WEIGHT_EFFICIENCY = "bh_weight_efficiency"
CONF_BH_WEIGHT_BALANCE = "bh_weight_balance"
CONF_BH_WINDOW_DAYS = "bh_window_days"
CONF_BH_MIN_SEGMENT_DELTA_SOC = "bh_min_segment_delta_soc"

BH_OPTION_KEYS = (
    CONF_BH_ENABLED,
    CONF_BH_RATED_CAPACITY_KWH,
    CONF_BH_INSTALL_DATE,
    CONF_BH_AMBIENT_ENTITY,
    CONF_BH_WARRANTY_THROUGHPUT_KWH,
    CONF_BH_WEIGHT_CAPACITY,
    CONF_BH_WEIGHT_EFFICIENCY,
    CONF_BH_WEIGHT_BALANCE,
    CONF_BH_WINDOW_DAYS,
    CONF_BH_MIN_SEGMENT_DELTA_SOC,
)
