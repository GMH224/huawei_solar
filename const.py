"""Constants for the Huawei Solar integration."""

from datetime import timedelta

DOMAIN = "huawei_solar"
DEFAULT_PORT = 502
DEFAULT_SLAVE_ID = 0
DEFAULT_SERIAL_SLAVE_ID = 1
DEFAULT_USERNAME = "installer"
DEFAULT_PASSWORD = "00000a"

CONF_SLAVE_IDS = "slave_ids"
CONF_ENABLE_PARAMETER_CONFIGURATION = "enable_parameter_configuration"

DATA_DEVICE_DATAS = "device_datas"
DATA_UPDATE_COORDINATORS = "update_coordinators"
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
CONFIGURATION_UPDATE_TIMEOUT = timedelta(minutes=1)

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
# At 30 s polling: 30 requests/slot/day → ~10 days for full confidence.
ADAPTIVE_FULL_CONFIDENCE_N: float = 300.0

# How many raw RTT samples to store per slot for P95 estimation.
ADAPTIVE_RTT_SAMPLE_SIZE: int = 50

# Duration after a detected state transition during which elevated parameters
# are maintained regardless of the slot's historical failure rate.
ADAPTIVE_TRANSITION_DURATION_MINUTES: int = 10

# ── Adaptive parameter bounds ─────────────────────────────────────────────────
# Poll interval: 30 s (normal) to 120 s (high-failure slots).
# Night mode (5 min) takes precedence when active.
ADAPTIVE_POLL_MIN = timedelta(seconds=30)
ADAPTIVE_POLL_MAX = timedelta(seconds=120)

# Inter-request gap: 150 ms (normal) to 500 ms (high-RTT / transition).
ADAPTIVE_GAP_MIN = timedelta(milliseconds=150)
ADAPTIVE_GAP_MAX = timedelta(milliseconds=500)

# Per-request timeout: 35 s (normal) to 90 s (slow/busy inverter).
ADAPTIVE_TIMEOUT_MIN = timedelta(seconds=35)
ADAPTIVE_TIMEOUT_MAX = timedelta(seconds=90)

# Failure rate thresholds used to derive queue depth and poll interval.
# Above HIGH → use max params; between LOW and HIGH → interpolate.
ADAPTIVE_FAILURE_RATE_LOW: float = 0.03    # 3 %
ADAPTIVE_FAILURE_RATE_HIGH: float = 0.15   # 15 %
