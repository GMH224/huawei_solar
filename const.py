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

INVERTER_UPDATE_INTERVAL      = timedelta(seconds=30)
POWER_METER_UPDATE_INTERVAL   = timedelta(seconds=30)
ENERGY_STORAGE_UPDATE_INTERVAL = timedelta(seconds=30)

# UPDATE_TIMEOUT must be shorter than update intervals so a hung request is
# cancelled before the next poll cycle begins.  25 s leaves a 5 s buffer.
UPDATE_TIMEOUT = timedelta(seconds=25)

CONFIGURATION_UPDATE_INTERVAL = timedelta(minutes=15)
CONFIGURATION_UPDATE_TIMEOUT  = timedelta(minutes=1)

OPTIMIZER_UPDATE_INTERVAL = timedelta(minutes=5)
OPTIMIZER_UPDATE_TIMEOUT  = timedelta(minutes=2)

# Night / sleep mode: all coordinators slow to this when PV power ≈ 0.
NIGHT_POLL_INTERVAL = timedelta(minutes=5)

# ── Modbus back-off ──────────────────────────────────────────────────────────
MAX_CONSECUTIVE_TIMEOUTS = 3
MODBUS_RETRY_BASE_WAIT   = timedelta(seconds=10)
MODBUS_RETRY_MAX_WAIT    = timedelta(seconds=120)

# ── v2.12.2: Phase staggering ────────────────────────────────────────────────
# Each subsequent coordinator is delayed by this offset on its very first poll
# so all four coordinators don't pile into the ModbusGuard queue together.
# With 4 coordinators and 7 s stagger the last one starts at ~21 s, well
# within the 30 s poll window.
COORDINATOR_STAGGER_SECONDS = 7

# ── v2.12.2: TCP keep-alive (night mode) ────────────────────────────────────
# The SUN2000 drops idle TCP connections after ~30 s.  In night mode (5-min
# polls) we send a single-register ping at this interval to keep the socket
# open and avoid the ~200–500 ms TCP handshake overhead on every poll.
KEEP_ALIVE_INTERVAL = timedelta(seconds=25)

# ── v2.12.2: Adaptive poll interval self-tuning ──────────────────────────────
# Number of consecutive high-failure polls before backing off.
POLL_AUTOTUNE_HIGH_THRESHOLD = 10
# Number of consecutive healthy polls before recovering toward minimum.
POLL_AUTOTUNE_LOW_THRESHOLD  = 60
# Multiplicative step applied when backing off or recovering.
POLL_AUTOTUNE_STEP           = 2.0
# Hard floor / ceiling for auto-tuned intervals (day mode only).
POLL_AUTOTUNE_MIN_INTERVAL   = timedelta(seconds=30)
POLL_AUTOTUNE_MAX_INTERVAL   = timedelta(minutes=5)

# ── Service names ────────────────────────────────────────────────────────────
SERVICE_FORCIBLE_CHARGE                  = "forcible_charge"
SERVICE_FORCIBLE_DISCHARGE               = "forcible_discharge"
SERVICE_FORCIBLE_CHARGE_SOC              = "forcible_charge_soc"
SERVICE_FORCIBLE_DISCHARGE_SOC           = "forcible_discharge_soc"
SERVICE_STOP_FORCIBLE_CHARGE             = "stop_forcible_charge"
SERVICE_RESET_MAXIMUM_FEED_GRID_POWER    = "reset_maximum_feed_grid_power"
SERVICE_SET_DI_ACTIVE_POWER_SCHEDULING   = "set_di_active_power_scheduling"
SERVICE_SET_ZERO_POWER_GRID_CONNECTION   = "set_zero_power_grid_connection"
SERVICE_SET_MAXIMUM_FEED_GRID_POWER      = "set_maximum_feed_grid_power"
SERVICE_SET_MAXIMUM_FEED_GRID_POWER_PERCENT = "set_maximum_feed_grid_power_percent"
SERVICE_SET_TOU_PERIODS                  = "set_tou_periods"
SERVICE_SET_CAPACITY_CONTROL_PERIODS     = "set_capacity_control_periods"
SERVICE_SET_FIXED_CHARGE_PERIODS         = "set_fixed_charge_periods"

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
    SERVICE_SET_TOU_PERIODS,
    SERVICE_SET_CAPACITY_CONTROL_PERIODS,
    SERVICE_SET_FIXED_CHARGE_PERIODS,
)
