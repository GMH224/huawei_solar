# CLAUDE.md — Huawei Solar Integration

> **Maintained by Claude (Anthropic) on behalf of the community.**
> Current version: **1.0.4** — see `manifest.json`.

---

## Table of contents

1. [Project overview](#1-project-overview)
2. [Architecture](#2-architecture)
3. [Modbus optimisation layer](#3-modbus-optimisation-layer)
4. [Coordinator decomposition](#4-coordinator-decomposition)
5. [Synchronized power-flow coordinator](#5-synchronized-power-flow-coordinator)
6. [Battery entities](#6-battery-entities)
7. [Modbus telemetry sensors](#7-modbus-telemetry-sensors)
8. [Changelog](#8-changelog)
9. [Developer guide](#9-developer-guide)
10. [Bug fixes reference](#10-bug-fixes-reference)

---

## 1. Project overview

A [Home Assistant](https://www.home-assistant.io/) custom integration (HACS) for
monitoring and controlling Huawei SUN2000 series solar inverters and LUNA2000 /
LG RESU batteries via **Modbus TCP** (LAN) or **Modbus RTU** (USB).

Built on the [`huawei-solar`](https://github.com/wlcrs/huawei-solar) Python
library. Exposes:

| HA platform | Examples |
|---|---|
| `sensor` | PV power/energy, grid power, battery SOC, optimizer data |
| `number` | Max charge/discharge power, end-of-charge SOC |
| `select` | Storage working mode, TOU settings |
| `switch` | Grid-tied switch, forcible charge |
| `button` | Reset / trigger actions |

### Supported hardware

| Class | Examples |
|---|---|
| Inverter | SUN2000-2KTL … SUN2000-330KTL |
| Battery | LUNA2000 (5/10/15 kWh), LG RESU |
| Meter | DTSU666-H, DDSU666-H |
| Dongle | SDongle (A/E series) |
| Logger | SmartLogger 3000A |
| EMMA | SUN2000-MB0 |

---

## 2. Architecture

```
homeassistant/
└── custom_components/
    └── huawei_solar/
        ├── __init__.py                        # Entry setup, device discovery, coordinator wiring
        ├── manifest.json                      # HACS / HA metadata, version
        ├── const.py                           # All constants
        ├── types.py                           # Typed dataclasses for runtime data
        │
        ├── modbus_guard.py                    # asyncio lock + inter-request rate limiter
        ├── modbus_telemetry.py                # Rolling-window traffic stats + HA sensors
        ├── register_cache.py                  # Tier-aware + adaptive TTL register cache
        ├── night_mode.py                      # PV-power-based night/day mode detector
        ├── update_coordinator.py              # Optimised DataUpdateCoordinator
        ├── synchronized_power_coordinator.py  # ← NEW: coherent multi-inverter power snapshot
        │
        ├── sensor.py        # SensorEntity (includes 4 fused power-flow sensors)
        ├── number.py        # NumberEntity (writable numeric registers)
        ├── select.py        # SelectEntity (enum registers)
        ├── switch.py        # SwitchEntity (boolean registers)
        ├── button.py        # ButtonEntity (one-shot actions)
        ├── services.py      # HA service definitions
        ├── config_flow.py   # UI-based config flow
        ├── diagnostics.py   # HA diagnostics dump
        │
        └── tests/
            ├── conftest.py
            ├── test_modbus_guard.py
            ├── test_modbus_telemetry.py
            ├── test_register_cache.py
            ├── test_services.py
            ├── test_init_unload.py
            ├── test_const_services.py
            ├── test_update_coordinator.py
            └── test_synchronized_power_coordinator.py   ← NEW
```

---

## 3. Modbus optimisation layer

### 3.1 `modbus_guard.py` — serialise & rate-limit all traffic

```
ModbusGuard (singleton per serial_number)
│
├── asyncio.Lock  — one request in-flight at a time per inverter
└── MIN_INTER_REQUEST_GAP (150 ms) — reset time for the SUN2000 Modbus FSM
```

**`_queue_depth` accounting (v1.0.0 fix):**
The counter is incremented at the start of `__aenter__` and decremented exactly
once in the outer `except Exception` block. A former inner `except TimeoutError`
block that double-decremented it has been removed.

### 3.2 `register_cache.py` — skip redundant reads

| Tier | Base TTL | Adaptive cap | Examples |
|---|---|---|---|
| STATIC | 60 min | session | serial, firmware, rated_power |
| SLOW | 5 min | 30 min | daily totals, alarms, temperature |
| NORMAL | 30 s | 5 min | SOC, voltage, current |
| FAST | 0 s (always) | 60 s night | grid power, PV input, battery power |

**Adaptive TTL:** unchanged value → TTL × 2 (capped at tier max); changed value → reset to base.

### 3.3 Exponential back-off

```
Consecutive timeouts:  0–2 → no delay
                       3   → ~10 s ± 10 % jitter
                       4   → ~20 s ± 10 %
                       5   → ~40 s ± 10 %
                       6+  → 120 s ± 10 % (cap)
```

**`_day_interval` sentinel (v1.0.0 fix):**
Push-driven coordinators (no `update_interval`) now store `timedelta(0)` instead
of `UPDATE_TIMEOUT` (35 s), preventing the request timeout from being misused as
a poll cadence.

### 3.4 Night mode

`NightModeDetector` watches `INPUT_POWER`. After 3 consecutive polls ≤ 50 W:
- Poll interval → `NIGHT_POLL_INTERVAL` (5 min)
- All cache TTLs × 10

Wakes up instantly when power rises above 100 W.

---

## 4. Coordinator decomposition

Four independent coordinators per SUN2000 inverter (unchanged from v2.12):

| Coordinator | Interval | Registers |
|---|---|---|
| `update_coordinator` | 30 s | PV strings, AC output, alarms |
| `power_meter_update_coordinator` | 30 s | Grid import/export, voltage, current |
| `energy_storage_update_coordinator` | 30 s | Battery SOC, power, temperature |
| `configuration_update_coordinator` | 15 min | Working mode, TOU, storage settings |

All four share one `ModbusGuard` and one `ModbusTelemetry` per inverter.

---

## 5. Synchronized power-flow coordinator

### Problem

With two inverters on the same Modbus bus, the standard per-device coordinators
fire at staggered times. ModbusGuard serialises them correctly but the resulting
wall-clock spread between the first and last reading reaches **3–4 seconds**.

When HA's Energy dashboard power-flow card samples entity states it reads a
snapshot of values measured at different moments. During ramp events (cloud,
EV charger, kettle) those values don't add up and the card shows wrong numbers.

### Solution: `SynchronizedPowerCoordinator`

A dedicated `DataUpdateCoordinator` (10 s interval) reads exactly the four
registers needed for power-flow in one **contiguous Modbus block**, serialised
behind the primary inverter's `ModbusGuard`:

```
Poll sequence (≈ 1.2–1.7 s total vs 3–4 s before)
────────────────────────────────────────────────────
[primary guard acquired]
  1. INV1 → INPUT_POWER                  (PV string DC power)
  2. INV1 → POWER_METER_ACTIVE_POWER     (grid import/export, signed W)
  3. INV1 → STORAGE_CHARGE_DISCHARGE_POWER (battery, signed W)
[primary guard released]
[secondary guard acquired]
  4. INV2 → INPUT_POWER                  (standalone inverter PV)
[secondary guard released]
```

All four HA sensor entities update in the **same coordinator tick** — their
`last_updated` timestamps are identical. No arithmetic errors on the power-flow
card.

### Entities created

| Entity | Unit | Sign convention |
|---|---|---|
| `sensor.huawei_solar_pv_power_total` | W | always ≥ 0 |
| `sensor.huawei_solar_grid_power` | W | + = import, − = export |
| `sensor.huawei_solar_battery_power` | W | + = charging, − = discharging |
| `sensor.huawei_solar_home_consumption` | W | always ≥ 0 (clamped) |

### Home consumption formula

```
home = PV_total + grid_power − battery_power
```

Derivation from energy conservation:

```
PV + grid_import = home + grid_export + battery_charge
→ home = PV + (grid_import − grid_export) − battery_charge + battery_discharge
       = PV + grid_power − battery_power
```

Small negative results (transient noise) are clamped to 0.

### Activation conditions

The coordinator is created automatically when:
- At least one `SUN2000Device` is configured, **and**
- Any of the following: meter present, battery present, or second inverter present

Single inverter with no meter and no battery has nothing to synchronise —
`SynchronizedPowerCoordinator` is not created in that case.

### Configuring the HA Energy dashboard

**Power-flow card** — use the synchronised sensors:
- Solar: `sensor.huawei_solar_pv_power_total`
- Grid: `sensor.huawei_solar_grid_power`
- Battery: `sensor.huawei_solar_battery_power`
- Home: `sensor.huawei_solar_home_consumption`

**Energy card (kWh totals)** — do **not** use power-based integration. Use the
inverter's own cumulative registers instead:

| Slot | Register sensor |
|---|---|
| Solar production | `sensor.*_total_yield_energy` (sum INV1 + INV2 via template) |
| Grid consumption | `sensor.*_power_meter_*_energy_import` |
| Grid return | `sensor.*_power_meter_*_energy_export` |
| Battery in | `sensor.*_storage_total_charged_energy` |
| Battery out | `sensor.*_storage_total_discharged_energy` |

These are monotonically increasing kWh counters written by the inverter's own
metering IC — a 4-second polling offset doesn't affect their accuracy.

### Partial failure handling

If one device is temporarily unreachable, the coordinator logs a DEBUG warning
and marks that sensor `unavailable`, while the others continue updating normally.
Only when **all** reads fail does the coordinator raise `UpdateFailed`.

### Architecture note — same-IP setup

Because both inverters connect through the same SmartLogger/SDongle TCP endpoint,
the physical Modbus bus is implicitly serialised at the TCP connection layer.
Holding the primary guard for reads 1–3 prevents other coordinators on INV1 from
interleaving. Read 4 goes through INV2's own guard, which is a separate asyncio
lock (different serial number) but operates on the same physical bus — the
inter-request gap enforced by each guard ensures correct timing.

---

## 6. Battery entities

### `stop_forcible_charge` (v1.0.0 fix)

Now resets **both** `STORAGE_FORCIBLE_CHARGE_POWER` and
`STORAGE_FORCIBLE_DISCHARGE_POWER` to 0 on stop. Previously only the discharge
register was cleared, leaving a stale charge-power value in the inverter.

### Number entities

| Entity | Unit | Range | Step |
|---|---|---|---|
| `storage_maximum_charging_power` | W | 0–rated | 100 W |
| `storage_maximum_discharging_power` | W | 0–rated | 100 W |
| `storage_charging_cutoff_capacity` | % | 90–100 | 0.1 % |

---

## 7. Modbus telemetry sensors

All diagnostic sensors poll a rolling 1-hour window and update alongside their
inverter coordinator. The deques are bounded during outages thanks to the v1.0.0
fix that calls `_evict()` from `record_failure()` and `record_timeout()`.

| Sensor | Notes |
|---|---|
| Requests / hour | Total `batch_update()` calls |
| Failures / hour | Timeouts + other errors |
| Timeouts / hour | Timeout-specific subset |
| Cache hits / hour | Registers served from cache |
| Failure rate % | `failures / requests × 100` |
| Avg batch size | Average registers per request |
| Total requests | Lifetime total |
| Total failures | Lifetime total |
| Total cache hits | Lifetime total |
| Skipped polls | Polls where all registers were cached |
| Night mode active | DAY / NIGHT |

---

## 8. Changelog

### v1.0.4 (2026-05-27)
**Circadian adaptive Modbus learning — reliability over speed**

Addresses the root cause of time-of-day Modbus failure spikes (midday MPPT
saturation, sunset battery handover, pre-dawn BMS wake-up) by learning optimal
parameters from observed history rather than reacting only to immediate failures.

**How many days to full learning?**
Each 15-minute slot reaches useful predictions after ~2 days (50 weighted
requests), good predictions after ~5 days (150), and full confidence after
~10 days (300) at a 30 s poll interval.  Plan for **7 days** as the practical
minimum for stable circadian patterns.  The controller is beneficial from day 1
via its immediate-reaction path; time-of-day pre-emption improves over two weeks.

| # | File | Change |
|---|------|--------|
| 1 | `adaptive_modbus.py` *(new)* | `AdaptiveModbusController`: 96 × 15-min time slots, each storing weighted failure rate, timeout rate, and P95 RTT (up to 50 samples). Daily decay factor 0.85 gives 14-day effective memory. On HA start, persisted statistics are loaded from `.storage/huawei_solar.adaptive.<serial>` and decay is applied for elapsed days. `get_params()` derives `poll_interval` (30–120 s), `request_gap` (150–500 ms), `request_timeout` (35–90 s), `max_queue_depth` (1–3) from each slot's statistics, blended with a conservative baseline when confidence < 30 %. `notify_transition()` forces maximum-tolerance parameters for 10 min on any inverter state change (day↔night, battery reversal). 10 HA diagnostic sensor entities expose the controller's internal state. |
| 2 | `modbus_guard.py` | `update_gap(s)` and `update_max_queue_depth(n)` allow the coordinator to push adaptive values each poll cycle. Gap clamped to [150 ms, 500 ms]; depth clamped to [1, 3]. |
| 3 | `update_coordinator.py` | `attach_adaptive()` wires the controller. At the start of every poll: `get_params()` is called, guard gap and depth are updated, and the effective timeout is taken from params. RTT is measured around `batch_update()` and fed back via `record_request()`. Failures (timeout, read, connection) also feed back. `_on_mode_change()` calls `notify_transition()` on day↔night switches so elevated params fire immediately. Poll interval is updated dynamically from params (outside night mode). |
| 4 | `__init__.py` | `AdaptiveModbusController.get_or_create()` + `await adaptive.async_load()` called once per inverter in `_setup_inverter_device_data()`. `attach_adaptive()` called on all five coordinators (main, power_meter, energy_storage, config, optimizer). `controller.stop()` + `clear_registry()` called in `async_unload_entry()`. |
| 5 | `sensor.py` | `create_adaptive_entities()` registered after telemetry entities. |
| 6 | `const.py` | All adaptive tuning constants added (`ADAPTIVE_POLL_MIN/MAX`, `ADAPTIVE_GAP_MIN/MAX`, `ADAPTIVE_TIMEOUT_MIN/MAX`, `ADAPTIVE_FAILURE_RATE_LOW/HIGH`, `ADAPTIVE_DECAY_FACTOR`, `ADAPTIVE_FULL_CONFIDENCE_N`, `ADAPTIVE_SLOT_COUNT`, `ADAPTIVE_TRANSITION_DURATION_MINUTES`). |

**10 new diagnostic sensor entities per inverter:**
`adaptive_poll_interval_s`, `adaptive_gap_ms`, `adaptive_timeout_s`,
`adaptive_max_queue_depth`, `adaptive_confidence_pct`,
`adaptive_slot_failure_rate_pct`, `inverter_state_transition`,
`adaptive_days_of_data`, `adaptive_time_slot`, `adaptive_slot_requests`

### v1.0.3 (2026-05-26)
**Energy dashboard accuracy + Modbus traffic smoothing**

Root cause fixed: incorrect hourly consumption bars in the HA Energy dashboard
(negative corrections, wrong-bucket spikes) were caused by stale energy counter
values being served during Modbus outages and by all four per-inverter
coordinators firing simultaneously at peak load moments.

| # | Severity | Change | File(s) |
|---|---|---|---|
| 1 | High | Energy-counter stale-cache exclusion: `daily_yield`, `total_yield`, `total_energy`, `grid_accumulated_*`, `storage_total_charged/discharged_energy` and all other kWh accumulator registers are now **never** returned from the stale-cache fallback after a timeout. HA receives `unavailable` (honest gap) instead of a flat line + jump, which it interpolates correctly. | `register_cache.py`, `update_coordinator.py` |
| 2 | High | Coordinator start-time jitter: main=0 s, power_meter=7 s, energy_storage=14 s, configuration=10 s. Eliminates the simultaneous guard-queue spike at t=0 and every 30 s interval boundary, reducing peak `_queue_depth` from 4 → 1 under normal operation. Directly addresses the failure spikes seen at midday and sunrise/sunset transitions. | `__init__.py`, `update_coordinator.py` |
| 3 | Medium | Contiguous register sorting: `stale_names` sorted by Modbus address before `batch_update()` to maximise register adjacency. Adjacent addresses collapse into fewer Read Holding Registers PDUs, reducing TCP round-trips per poll. Includes a multi-path address resolver (`register_definition.register`, `.address`, `.value`) with silent fallback so it is safe across library versions. | `update_coordinator.py` |
| 4 | Low | `RegisterCache.set_telemetry()`: swaps the telemetry reference without discarding `_store`, so cached values and adaptive TTLs survive `attach_telemetry()`. Previously the entire cache was replaced, causing a full re-read on the first post-attach poll. | `register_cache.py`, `update_coordinator.py` |

### v1.0.2 (2026-05-26)
**New feature: SynchronizedPowerCoordinator**

Solves the multi-inverter power-flow card timing problem. All four instantaneous
power readings (INV1 PV, grid, battery, INV2 PV) are now sampled in one
contiguous Modbus block so all four HA entities share the same `last_updated`
timestamp, eliminating arithmetic errors on the power-flow card.

- **New:** `synchronized_power_coordinator.py` — `SynchronizedPowerCoordinator`
  and `SynchronizedPowerData` dataclass with `pv_power_total` and
  `home_consumption` derived properties.
- **New:** Four fused sensor entities in `sensor.py` —
  `SynchronizedPowerSensorEntity`, `_PvTotalSensor`, `_GridPowerSensor`,
  `_BatteryPowerSensor`, `_HomeConsumptionSensor`,
  `create_synchronized_power_entities`.
- **New:** `DATA_SYNC_POWER_COORDINATOR` runtime-data key and
  `SYNC_POWER_UPDATE_INTERVAL = timedelta(seconds=10)` constant in `const.py`.
- **New:** Coordinator wired into `async_setup_entry` (auto-enabled when meter,
  battery, or second inverter is present) and cleanly torn down in
  `async_unload_entry`.
- **New:** Translation entries for the four new sensors in `strings.json` and
  `translations/en.json`.
- **New:** `tests/test_synchronized_power_coordinator.py` — 22 tests covering
  derived properties (all edge cases), happy path, partial failure, all-fail,
  consecutive failure counter, and telemetry recording.

### v1.0.0 (2026-05-24)
**Bug fix release — 7 correctness issues resolved**

| # | Severity | Fix |
|---|---|---|
| 1 | High | `modbus_guard.py`: `_queue_depth` double-decremented on `TimeoutError` |
| 2 | High | `services.py`: `EMMA_DEVICE_SCHEMA` defined twice |
| 3 | High | `__init__.py`: `async_unload_entry` used raw string instead of `DATA_DEVICE_DATAS` |
| 4 | Medium | `services.py`: `stop_forcible_charge` only zeroed `DISCHARGE_POWER`, not `CHARGE_POWER` |
| 5 | Medium | `modbus_telemetry.py`: `record_failure/timeout` never called `_evict()` |
| 6 | Medium | `const.py`: `SERVICE_SET_MAXIMUM_FEED_GRID_POWER_PERCENT` missing from `SERVICES` |
| 7 | Low | `update_coordinator.py`: `_day_interval` fell back to `UPDATE_TIMEOUT` instead of `timedelta(0)` |

**New:** `tests/` — standalone unit test suite (8 files, ~80 tests, no HA runtime required).

### v2.12.0 (upstream)
Adaptive TTL + Night-mode polling + Register tier system.

### v2.11.0 (upstream)
ModbusGuard, RegisterCache, ModbusTelemetry.

### v2.10b (upstream)
Timeout hardening, exponential back-off, battery entity improvements.

---

## 9. Developer guide

### Running the tests

```bash
# From the integration directory — no HA environment required
pip install pytest pytest-asyncio
pytest tests/ -v
```

All tests stub HA imports and the `huawei-solar` library.

### Adding a new register sensor

1. Find the register name in `huawei-solar`'s `register_names.py`.
2. Add a `HuaweiSolarSensorEntityDescription` to the appropriate
   `*_SENSOR_DESCRIPTIONS` tuple in `sensor.py`.
3. Add translation strings to `strings.json` and `translations/en.json`
   under `entity.sensor.<key>.name`.

### Adding a new HA service

1. `SERVICE_<NAME> = "name"` in `const.py`.
2. Add to the `SERVICES` tuple in `const.py`.
3. Implement handler and register in `async_setup_services` in `services.py`.
4. The test `test_const_services.py::test_services_tuple_contains_all_service_constants`
   will catch a missing step 2.

### Key constants (`const.py`)

| Constant | Default | Effect |
|---|---|---|
| `INVERTER_UPDATE_INTERVAL` | 30 s | Main inverter poll rate |
| `SYNC_POWER_UPDATE_INTERVAL` | 10 s | Synchronized power-flow poll rate |
| `UPDATE_TIMEOUT` | 35 s | Per-request timeout |
| `MAX_CONSECUTIVE_TIMEOUTS` | 3 | Back-off activation threshold |
| `MODBUS_RETRY_BASE_WAIT` | 10 s | Back-off base delay |
| `MODBUS_RETRY_MAX_WAIT` | 120 s | Back-off cap |
| `NIGHT_POLL_INTERVAL` | 5 min | Poll interval in night/sleep mode |

### `ModbusGuard` tuning (`modbus_guard.py`)

| Constant | Default | Effect |
|---|---|---|
| `MIN_INTER_REQUEST_GAP` | 150 ms | Minimum pause between requests |
| `QUEUE_WAIT_TIMEOUT` | 10 s | Max wait before abandoning a queued request |

### Syntax + JSON audit

```bash
cd /path/to/parent
python3 -c "
import ast, json, pathlib
base = pathlib.Path('huawei_solar')
for f in base.glob('**/*.py'):
    if '__pycache__' not in str(f):
        ast.parse(f.read_text())
        print('OK', f.name)
for f in list(base.glob('*.json')) + list(base.glob('translations/*.json')):
    json.loads(f.read_text())
    print('OK', f.name)
"
```

---

## 10. Bug fixes reference

| # | Sev | File | Root cause | Symptom |
|---|---|---|---|---|
| 1 | High | `modbus_guard.py` | Double decrement on `TimeoutError` | `_queue_depth` goes negative; `is_busy` unreliable |
| 2 | High | `services.py` | Duplicate `EMMA_DEVICE_SCHEMA` assignment | Silent shadow; future divergence risk |
| 3 | High | `__init__.py` | Raw string `"device_datas"` in unload | `KeyError` if constant renamed |
| 4 | Med | `services.py` | `stop_forcible_charge` skips `CHARGE_POWER` reset | Stale inverter register after stop |
| 5 | Med | `modbus_telemetry.py` | `record_failure/timeout` skip `_evict()` | Unbounded deques during outages |
| 6 | Med | `const.py` | `SERVICE_SET_MAXIMUM_FEED_GRID_POWER_PERCENT` missing from `SERVICES` | Service leaks on unload |
| 7 | Low | `update_coordinator.py` | `_day_interval` falls back to `UPDATE_TIMEOUT` | Night-mode and cache use request timeout as poll interval |
