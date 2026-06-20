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
# (every Nth poll cycle); SLOW/STATIC are deferred entirely until recovery.
BACKOFF_FAST_ALWAYS: bool = True
BACKOFF_NORMAL_DIVISOR: int = 4   # read NORMAL registers every 4th back-off cycle
