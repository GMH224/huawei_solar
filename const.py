"""Constants for the Huawei Solar integration."""

from datetime import timedelta

DOMAIN = "huawei_solar"
DEFAULT_PORT = 502
DEFAULT_USERNAME = "installer"

CONF_SLAVE_IDS = "slave_ids"
CONF_ENABLE_PARAMETER_CONFIGURATION = "enable_parameter_configuration"

DATA_DEVICE_DATAS = "device_datas"
DATA_SYNC_POWER_COORDINATOR = "sync_power_coordinator"

INVERTER_UPDATE_INTERVAL = timedelta(seconds=30)
POWER_METER_UPDATE_INTERVAL = timedelta(seconds=30)
ENERGY_STORAGE_UPDATE_INTERVAL = timedelta(seconds=30)
SYNC_POWER_UPDATE_INTERVAL = timedelta(seconds=10)

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

# v1.3.18 (Defect U/Finding 3, independent ICS audit of v1.3.17): bound for
# primary_device.client.disconnect() during async_unload_entry. This runs
# BEFORE every teardown loop that follows it (telemetry, the adaptive
# controller, keep-alive, battery health, the shared guard) -- a wedged or
# half-dead transport blocking here would prevent ALL of that cleanup from
# ever running. A clean disconnect should be near-instant; 10s is generous
# headroom without meaningfully delaying unload in the normal case.
DISCONNECT_TIMEOUT = timedelta(seconds=10)

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
# Register used for the keep-alive read (must be STATIC and single-word).
# Model ID is 1 register, always readable, never causes side effects.
KEEPALIVE_REGISTER = "model_id"

# ── Optimisation 4: Batch chunking ───────────────────────────────────────────
# Stale register lists larger than this threshold are split into chunks before
# being passed to batch_update(), with a short pause between chunks.  This
# prevents a single Modbus burst from occupying the inverter CPU for > ~300 ms,
# which is a primary trigger for 0x06 BUSY responses during high-load windows.
BATCH_CHUNK_SIZE: int = 40
# Pause inserted between chunks (inside the guard lock — gap enforced by guard).
BATCH_INTER_CHUNK_PAUSE = timedelta(milliseconds=80)

# ── Optimisation 5: Write-back verification ───────────────────────────────────
# Delay before the post-write verification read is issued.  Long enough for the
# inverter to apply the setting, short enough to catch a missed write quickly.
WRITE_VERIFY_DELAY = timedelta(seconds=3)
# Maximum number of re-read retries if the first verification read still shows
# the old value (covers slow-applying settings like working-mode changes).
WRITE_VERIFY_RETRIES: int = 2

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
