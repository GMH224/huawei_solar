# CLAUDE.md — Huawei Solar Integration

> **Maintained by Claude (Anthropic) on behalf of the community.**
> Version tracking: see `manifest.json → version`.

---

## Table of contents

1. [Project overview](#1-project-overview)
2. [Architecture](#2-architecture)
3. [Modbus optimisation layer](#3-modbus-optimisation-layer)
4. [Coordinator decomposition](#4-coordinator-decomposition)
5. [Battery entities](#5-battery-entities)
6. [Modbus telemetry sensors](#6-modbus-telemetry-sensors)
7. [Changelog (AI-maintained)](#7-changelog-ai-maintained)
8. [Developer guide](#8-developer-guide)
9. [Bug fixes reference](#9-bug-fixes-reference)

---

## 1. Project overview

This is a [Home Assistant](https://www.home-assistant.io/) custom integration
(HACS) for monitoring and controlling Huawei SUN2000 series solar inverters and
LUNA2000 / LG RESU batteries via **Modbus TCP** (LAN) or **Modbus RTU** (USB).

It builds on the [`huawei-solar`](https://github.com/wlcrs/huawei-solar) Python
library and exposes:

| HA platform | Examples |
|---|---|
| `sensor`  | PV power/energy, grid power, battery SOC, optimizer data |
| `number`  | Maximum charge/discharge power, end-of-charge SOC |
| `select`  | Storage working mode, TOU settings |
| `switch`  | Grid-tied switch, forcible charge |
| `button`  | Reset / trigger actions |

### Supported hardware

| Device class | Examples |
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
        ├── __init__.py            # Entry setup, device discovery, coordinator wiring
        ├── manifest.json          # HACS / HA metadata, version
        ├── const.py               # All constants (intervals, timeouts, back-off params)
        ├── types.py               # Typed dataclasses for runtime data
        │
        ├── modbus_guard.py        # asyncio lock + inter-request rate limiter
        ├── modbus_telemetry.py    # Rolling-window traffic stats + HA sensors
        ├── register_cache.py      # Tier-aware + adaptive TTL register cache
        ├── night_mode.py          # PV-power-based night/day mode detector
        ├── update_coordinator.py  # Optimised DataUpdateCoordinator implementations
        │
        ├── sensor.py              # SensorEntity implementations + descriptions
        ├── number.py              # NumberEntity (writable numeric registers)
        ├── select.py              # SelectEntity (enum registers)
        ├── switch.py              # SwitchEntity (boolean registers)
        ├── button.py              # ButtonEntity (one-shot actions)
        ├── services.py            # HA service definitions (TOU, forcible charge, …)
        ├── config_flow.py         # UI-based configuration flow
        ├── diagnostics.py         # HA diagnostics dump
        │
        └── tests/                 # Standalone unit tests (no HA runtime required)
            ├── conftest.py
            ├── test_modbus_guard.py
            ├── test_modbus_telemetry.py
            ├── test_register_cache.py
            ├── test_services.py
            ├── test_init_unload.py
            ├── test_const_services.py
            └── test_update_coordinator.py
```

---

## 3. Modbus optimisation layer

> **Context:** The Huawei SUN2000 Modbus interface is single-threaded, supports
> only one TCP/RTU connection at a time, and will return error codes (or silently
> drop responses) when requests arrive too fast or overlap.

Three modules address this at different levels:

---

### 3.1 `modbus_guard.py` — serialise & rate-limit all traffic

```
ModbusGuard (singleton per serial_number)
│
├── asyncio.Lock  — ensures only one request is in-flight at a time
│                   for a given inverter, even when multiple coordinators
│                   (inverter + battery + power meter + config) run concurrently.
│
└── inter-request gap (MIN_INTER_REQUEST_GAP = 150 ms)
                  — enforced between every consecutive request pair;
                    the SUN2000 needs ~100 ms to reset its Modbus FSM.
```

**Usage in coordinators:**
```python
async with self.guard.request():
    result = await device.batch_update(stale_names)
```

**`_queue_depth` accounting (v1.0.0 fix):**
The `_queue_depth` counter tracks how many callers are currently waiting for the
lock.  It is incremented at the start of `__aenter__` and decremented exactly
once in the outer `except Exception` block.  A previous inner `except TimeoutError`
block that also decremented it has been removed — it caused `_queue_depth` to go
negative on lock-acquire timeouts.

---

### 3.2 `register_cache.py` — skip redundant reads

```
RegisterCache (per coordinator instance)
│
├── filter_stale(names, ttl)    →  returns only names that need a fresh read
├── update(fresh_results)       →  stores new values with a timestamp
├── merge(fresh, all_requested) →  combines fresh results with cached values
└── invalidate(name)            →  marks a register dirty after a write
```

**Volatility tiers:**

| Tier | Base TTL | Adaptive cap | Examples |
|---|---|---|---|
| STATIC | 60 min | session | serial, firmware, rated_power |
| SLOW | 5 min | 30 min | daily totals, alarm, temperature |
| NORMAL | 30 s | 5 min | SOC, voltage, current |
| FAST | 0 s (always) | 60 s (night) | grid power, PV input, battery power |

**Adaptive TTL algorithm:**
After each poll, for SLOW/NORMAL registers:
- Value **unchanged** → `new_ttl = min(current_ttl × 2, tier_cap)`
- Value **changed** → `new_ttl = tier_base_ttl` (reset)

---

### 3.3 Exponential back-off (in `update_coordinator.py`)

```
Consecutive timeouts → sleep before next attempt

0–2  : no back-off
3    : ~10 s ± 10 % jitter
4    : ~20 s ± 10 %
5    : ~40 s ± 10 %
6+   : 120 s ± 10 % (cap)
```

**`_day_interval` sentinel (v1.0.0 fix):**
When no `update_interval` is passed (push-driven coordinators), `_day_interval`
is now set to `timedelta(0)` rather than `UPDATE_TIMEOUT` (35 s).  This prevents
the 35 s request timeout from being misused as a poll cadence by
`NightModeDetector` and `RegisterCache.filter_stale()`.

---

### 3.4 Stale-cache fallback

On timeout the coordinator returns the last cached values before raising
`UpdateFailed`, keeping HA entities available during brief outages.

---

## 4. Coordinator decomposition

Four independent coordinators per SUN2000 inverter:

| Coordinator | Poll interval | Registers |
|---|---|---|
| `update_coordinator` | 30 s | PV strings, AC output, alarms |
| `power_meter_update_coordinator` | 30 s | Grid import/export, voltage, current |
| `energy_storage_update_coordinator` | 30 s | Battery SOC, power, temperature |
| `configuration_update_coordinator` | 15 min | Working mode, TOU, storage settings |

All four share one `ModbusGuard` singleton and one `ModbusTelemetry` singleton.

---

## 5. Battery entities

### Sensor entities (read-only)

| Entity ID suffix | Unit | Notes |
|---|---|---|
| `storage_maximum_charge_power` | W | Hardware-rated max |
| `storage_maximum_discharge_power` | W | Hardware-rated max |
| `storage_charging_cutoff_capacity` | % | End-of-charge SOC |

### Number entities (writable, requires parameter configuration)

| Entity ID suffix | Unit | Range | Step |
|---|---|---|---|
| `storage_maximum_charging_power` | W | 0–rated max | 100 W |
| `storage_maximum_discharging_power` | W | 0–rated max | 100 W |
| `storage_charging_cutoff_capacity` | % | 90–100 | 0.1 % |

### `stop_forcible_charge` service (v1.0.0 fix)

`stop_forcible_charge` now resets **both** `STORAGE_FORCIBLE_CHARGE_POWER` and
`STORAGE_FORCIBLE_DISCHARGE_POWER` to 0 when stopping a forcible charge or
discharge operation.  Previously only the discharge register was cleared, leaving
a stale power value in the inverter.

---

## 6. Modbus telemetry sensors

Diagnostic sensors (grouped under the inverter device) with rolling 1-hour windows:

| Sensor name | Unit | Notes |
|---|---|---|
| Modbus requests / hour | — | Total batch_update() calls |
| Modbus failures / hour | — | Timeouts + other errors |
| Modbus timeouts / hour | — | Timeout-specific subset |
| Modbus cache hits / hour | — | Registers served from cache |
| Modbus failure rate | % | `failures / requests × 100` |
| Avg Modbus batch size | — | Average registers per request |
| Modbus total requests | — | Lifetime total |
| Modbus total failures | — | Lifetime total |
| Modbus total cache hits | — | Lifetime total |
| Modbus skipped polls | — | Polls where all registers were cached |
| Night mode active | bool | DAY/NIGHT state |

**Rolling-window eviction (v1.0.0 fix):**
`record_failure()` and `record_timeout()` now call `_evict()` so the
`_failures` and `_timeouts` deques are pruned even during prolonged outages
where no successful requests are made.  Previously only `record_request()` and
`snapshot()` triggered eviction, allowing unbounded deque growth.

---

## 7. Changelog (AI-maintained)

### v1.0.0 (2026-05-24)
**Bug fix release — 7 correctness issues resolved**

- **Fix (High):** `modbus_guard.py` — `_queue_depth` was decremented twice on
  lock-acquire timeout (inner `except TimeoutError` + outer `except Exception`),
  driving the counter negative and making `queue_depth` / `is_busy` unreliable.
  Removed the inner decrement; the outer handler is now the sole cleanup path.

- **Fix (High):** `services.py` — `EMMA_DEVICE_SCHEMA` was assigned on two
  consecutive lines (identical content).  The duplicate assignment has been
  removed.

- **Fix (High):** `__init__.py` — `async_unload_entry` accessed
  `entry.runtime_data["device_datas"]` via a raw string literal instead of the
  `DATA_DEVICE_DATAS` constant.  Changed to use the constant so a rename cannot
  silently break unloading.

- **Fix (Medium):** `services.py` — `stop_forcible_charge` reset
  `STORAGE_FORCIBLE_DISCHARGE_POWER` to 0 but left `STORAGE_FORCIBLE_CHARGE_POWER`
  at its previous value.  Both registers are now explicitly zeroed on stop.

- **Fix (Medium):** `modbus_telemetry.py` — `record_failure()` and
  `record_timeout()` did not call `_evict()`, so `_failures` and `_timeouts`
  deques could grow unboundedly during prolonged inverter outages.  Both methods
  now call `_evict(now)`.

- **Fix (Medium):** `const.py` — `SERVICE_SET_MAXIMUM_FEED_GRID_POWER_PERCENT`
  was missing from the `SERVICES` tuple used for bulk service
  registration/deregistration, meaning it would not be cleaned up on integration
  unload.

- **Fix (Low):** `update_coordinator.py` — `_day_interval` fell back to
  `UPDATE_TIMEOUT` (35 s) when `update_interval=None`, causing
  `NightModeDetector` and `RegisterCache.filter_stale()` to treat the request
  timeout as a poll interval.  Changed to `timedelta(0)` sentinel.

- **New:** `tests/` — standalone unit test suite covering all 7 fixes plus
  `RegisterCache` tier/adaptive-TTL/night-mode behaviour.  Tests run without a
  Home Assistant environment (`pytest tests/`).

### v2.12.0 (2026-05-21)
**Adaptive TTL + Night-mode polling + Register tier system**

- **New:** `night_mode.py` — `NightModeDetector` watches `INPUT_POWER` and
  `DEVICE_STATUS`.  After 3 consecutive polls with PV power ≤ 50 W the
  coordinator transitions to NIGHT mode: poll interval → 5 min, all cache TTLs
  × 10.  Wakes up instantly when power rises above 100 W.
- **New:** Register tier system (`STATIC` / `SLOW` / `NORMAL` / `FAST`) in
  `register_cache.py`.
- **New:** Adaptive TTL — TTL doubles each poll cycle the value is unchanged,
  capped at the tier maximum.
- **New:** `Inverter night mode` diagnostic sensor.
- **Improved:** Combined daily Modbus traffic saving vs v2.1.0: ~60–70 %.

### v2.11.0 (2026-05-21)
**Aggressive Modbus optimisation + telemetry sensors**

- **New:** `modbus_guard.py`, `register_cache.py`, `modbus_telemetry.py`.
- **Improved:** Stale-cache fallback on timeout; cache invalidation on write.
- **Docs:** Initial `CLAUDE.md`.

### v2.10b (2026-05-20)
**Timeout hardening + battery entity improvements**

- `UPDATE_TIMEOUT` raised to 35 s, exponential back-off with jitter.
- Battery number entities enabled by default.

### v2.1.0 (upstream)
Original HACS release.

---

## 8. Developer guide

### Running the tests

```bash
# From the integration directory (no HA environment required)
pip install pytest pytest-asyncio
pytest tests/ -v
```

All tests are designed to run in isolation — they stub HA imports and the
`huawei-solar` library so the test environment needs only the Python standard
library plus `pytest` and `pytest-asyncio`.

### Adding a new register sensor

1. Find the register name constant in `huawei-solar`'s `register_names.py`.
2. Add a `HuaweiSolarSensorEntityDescription` entry to the appropriate
   `*_SENSOR_DESCRIPTIONS` tuple in `sensor.py`.
3. Add a translation string to `strings.json` and `translations/en.json`
   under `entity.sensor.<translation_key>.name`.

### Adding a new HA service

1. Define `SERVICE_<NAME> = "name"` in `const.py`.
2. Add the constant to the `SERVICES` tuple in `const.py`.
3. Add the constant to `ALL_SERVICES` in `services.py`.
4. Implement the handler and register it in `async_setup_services`.
5. The test `tests/test_const_services.py::test_services_tuple_contains_all_service_constants`
   will fail if you forget step 2 or 3.

### Syntax + JSON checks

```bash
cd /path/to/parent
python3 -c "
import ast, json, pathlib, sys
base = pathlib.Path('huawei_solar')
for f in base.glob('*.py'):
    ast.parse(f.read_text())
    print('OK', f.name)
for f in ['strings.json', 'manifest.json']:
    json.loads((base/f).read_text())
    print('OK', f)
"
```

### Key constants (`const.py`)

| Constant | Default | Effect |
|---|---|---|
| `INVERTER_UPDATE_INTERVAL` | 30 s | Main inverter poll rate |
| `UPDATE_TIMEOUT` | 35 s | Per-request timeout |
| `MAX_CONSECUTIVE_TIMEOUTS` | 3 | Back-off activation threshold |
| `MODBUS_RETRY_BASE_WAIT` | 10 s | Back-off base delay |
| `MODBUS_RETRY_MAX_WAIT` | 120 s | Back-off cap |
| `NIGHT_POLL_INTERVAL` | 5 min | Poll interval in night/sleep mode |

### `ModbusGuard` tuning (`modbus_guard.py`)

| Constant | Default | Effect |
|---|---|---|
| `MIN_INTER_REQUEST_GAP` | 150 ms | Minimum pause between requests |
| `QUEUE_WAIT_TIMEOUT` | 10 s | Max wait before abandoning queued request |

---

## 9. Bug fixes reference

Quick-reference table for the 7 bugs fixed in v1.0.0:

| # | Severity | File | Root cause | Symptom |
|---|---|---|---|---|
| 1 | High | `modbus_guard.py` | `_queue_depth` decremented twice on `TimeoutError` | Counter goes negative; `is_busy`/`queue_depth` unreliable |
| 2 | High | `services.py` | `EMMA_DEVICE_SCHEMA` assigned twice | Latent: second assignment silently shadows first |
| 3 | High | `__init__.py` | Raw string `"device_datas"` in unload instead of `DATA_DEVICE_DATAS` | `KeyError` on unload if constant is renamed |
| 4 | Medium | `services.py` | `stop_forcible_charge` only zeros `DISCHARGE_POWER` | Stale charge-power value left on inverter after stop |
| 5 | Medium | `modbus_telemetry.py` | `record_failure`/`record_timeout` never call `_evict()` | Deques grow unboundedly during outages; inaccurate rolling rates |
| 6 | Medium | `const.py` | `SERVICE_SET_MAXIMUM_FEED_GRID_POWER_PERCENT` not in `SERVICES` | Service not unregistered on integration unload |
| 7 | Low | `update_coordinator.py` | `_day_interval` falls back to `UPDATE_TIMEOUT` (35 s) when `None` | Night-mode and cache use request timeout as poll interval |
