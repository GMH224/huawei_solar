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
        ├── modbus_telemetry.py    # Rolling-window traffic stats + HA sensors (11 sensors)
        ├── register_cache.py      # Tier-aware + adaptive TTL register cache
        ├── night_mode.py          # NEW: PV-power-based night/day mode detector
        ├── update_coordinator.py  # Optimised DataUpdateCoordinator implementations
        │
        ├── sensor.py              # SensorEntity implementations + descriptions
        ├── number.py              # NumberEntity (writable numeric registers)
        ├── select.py              # SelectEntity (enum registers)
        ├── switch.py              # SwitchEntity (boolean registers)
        ├── button.py              # ButtonEntity (one-shot actions)
        ├── services.py            # HA service definitions (TOU, forcible charge, …)
        ├── config_flow.py         # UI-based configuration flow
        └── diagnostics.py        # HA diagnostics dump
```

---

## 3. Modbus optimisation layer

> **Context:** The Huawei SUN2000 Modbus interface is single-threaded, supports
> only one TCP/RTU connection at a time, and will return error codes (or silently
> drop responses) when requests arrive too fast or overlap.

Three new modules address this problem at different levels:

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

All coordinators for the same inverter share one `ModbusGuard` singleton.  When
a coordinator poll fires, it acquires the lock, performs the request, releases
the lock, then the next coordinator in the queue starts — respecting the 150 ms
gap.

**Why this reduces errors:**
The original code had four independent coordinators (inverter, power meter,
battery, config) each calling `batch_update()` without coordination.  During an
HA startup or after a network hiccup all four would fire within milliseconds of
each other, causing the inverter to see overlapping Modbus frames and respond
with error codes.

---

### 3.2 `register_cache.py` — skip redundant reads

```
RegisterCache (per coordinator instance)
│
├── filter_stale(names, ttl)    →  returns only names that need a fresh read
│                                  (others are served from cache → 0 Modbus traffic)
│
├── update(fresh_results)       →  stores new values with a timestamp
│
├── merge(fresh, all_requested) →  combines fresh results with cached values
│                                  for a complete response to the coordinator
│
└── invalidate(name)            →  marks a register dirty after a write so it
                                   is unconditionally re-read next cycle
```

**TTL rules:**

| Register group | TTL | Rationale |
|---|---|---|
| Static registers (`rated_power`, `storage_maximum_charge_power`, …) | 5 minutes | These are set at factory / commissioning and almost never change |
| All other registers | Coordinator's own `update_interval` | Default: 30 s for inverter/battery/meter, 15 min for config |

**Traffic reduction (typical 30 s poll, 5-min static TTL):**
- Static registers (~10–15 registers): polled 12 times/hour instead of 120 → **~90 % reduction for static group**
- All registers: on a quiet network with no write operations, up to **30–40 % fewer Modbus frames** overall

**Cache invalidation:**
After every successful `number`, `select`, or `switch` write, the affected
register is marked dirty so the next poll fetches a fresh value.  This prevents
stale readings after configuration changes.

---

### 3.3 Exponential back-off (in `update_coordinator.py`)

```
Consecutive timeouts → sleep before next attempt

0–2  : no back-off (first few timeouts are normal during night shutdown)
3    : ~10 s ± 1 s jitter
4    : ~20 s ± 2 s
5    : ~40 s ± 4 s
6+   : 120 s ± 12 s  (cap)
```

**Jitter** (±10 %) prevents multiple inverters (or a HA restart storm) from all
retrying at exactly the same moment.

After a successful poll the counter resets and the full cache is invalidated
to ensure a fresh read after an outage.

---

### 3.4 Stale-cache fallback

On a timeout the coordinator now tries to return the last cached values before
raising `UpdateFailed`.  This keeps HA entities available (instead of showing
"unavailable") during brief network interruptions or inverter night-mode sleeps.

---

## 4. Coordinator decomposition

The integration uses **four independent coordinators** per SUN2000 inverter,
each with its own poll interval:

| Coordinator | Poll interval | Registers |
|---|---|---|
| `update_coordinator` | 30 s | PV strings, AC output, alarms, optimizer summary |
| `power_meter_update_coordinator` | 30 s | Grid import/export, voltage, current |
| `energy_storage_update_coordinator` | 30 s | Battery SOC, power, temperature |
| `configuration_update_coordinator` | 15 min | Working mode, TOU periods, storage settings |

All four share:
- The same `ModbusGuard` singleton → serialised Modbus access
- The same `ModbusTelemetry` singleton → aggregated traffic stats
- Independent `RegisterCache` instances → per-group TTL management

The optimizer coordinator is separate (5-min interval) and also uses the shared
`ModbusGuard`.

---

## 5. Battery entities

### Sensor entities (read-only, always available)

| Entity ID suffix | Unit | Notes |
|---|---|---|
| `storage_maximum_charge_power` | W | Hardware-rated max — from device register |
| `storage_maximum_discharge_power` | W | Hardware-rated max |
| `storage_charging_cutoff_capacity` | % | End-of-charge SOC (read-only view) |

### Number entities (writable, requires parameter configuration enabled)

| Entity ID suffix | Unit | Range | Step | Notes |
|---|---|---|---|---|
| `storage_maximum_charging_power` | W | 0 – rated max | 100 W | Soft limit ≤ hardware max |
| `storage_maximum_discharging_power` | W | 0 – rated max | 100 W | Soft limit |
| `storage_charging_cutoff_capacity` | % | 90–100 | 0.1 % | Battery stops charging at this SOC |

All three number entities are **enabled by default** (previous versions had them
hidden).

---

## 6. Modbus telemetry sensors

A new set of diagnostic sensors (grouped under the inverter device) provides
visibility into the Modbus communication health.  All values are **rolling
1-hour windows** except the `total_*` counters which are lifetime.

| Sensor name | Unit | Notes |
|---|---|---|
| Modbus requests / hour | — | Total batch_update() calls in the last hour |
| Modbus failures / hour | — | Timeouts + other errors |
| Modbus timeouts / hour | — | Timeout-specific subset of failures |
| Modbus cache hits / hour | — | Registers served from cache (no Modbus traffic) |
| Modbus failure rate | % | `failures / requests × 100` |
| Avg Modbus batch size | — | Average registers per request |
| Modbus total requests | — | Lifetime total (disabled by default) |
| Modbus total failures | — | Lifetime total (disabled by default) |
| Modbus total cache hits | — | Lifetime total (disabled by default) |
| Modbus skipped polls | — | Polls skipped because all registers were cached (disabled by default) |

> **Tip:** Use **Modbus failure rate** and **timeouts / hour** to tune your
> polling intervals.  A healthy installation should see 0 failures/hour under
> normal conditions.  If you see a consistent failure rate, consider:
> - Increasing `INVERTER_UPDATE_INTERVAL` in `const.py`
> - Checking for other Modbus clients connecting to the inverter
> - Verifying network stability (packet loss, high latency)

---

## 7. Changelog (AI-maintained)

### v2.12.0 (2026-05-21)
**Adaptive TTL + Night-mode polling + Register tier system**

- **New:** `night_mode.py` — `NightModeDetector` watches `INPUT_POWER` and
  `DEVICE_STATUS` every poll cycle.  After 3 consecutive polls with PV power
  ≤ 50 W the coordinator transitions to NIGHT mode: poll interval → 5 min,
  all cache TTLs × 10.  Wakes up instantly when power rises above 100 W.
- **New:** Register tier system in `register_cache.py`:
  - `STATIC` (serial/firmware/rated-power) — read once per session (1 h TTL, uncapped)
  - `SLOW` (totals/daily counters/temperature/status) — 5 min base, 30 min cap
  - `NORMAL` (SOC, voltage, current) — 30 s base, 5 min cap
  - `FAST` (grid power, battery power, PV input) — always read, no caching
- **New:** Adaptive TTL — after each poll where a register value is unchanged,
  its TTL doubles (capped at tier max).  Resets to base TTL the moment the
  value changes.  A stable battery at 80 % SOC at midday organically slows
  from 30 s → 60 s → 120 s → 300 s.
- **New:** `Inverter night mode` diagnostic sensor shows DAY/NIGHT state in HA.
- **Improved:** Traffic model (typical 12 h day / 12 h night installation):
  - Day-only saving vs v2.11.0: ~35–40 % fewer Modbus transactions
  - Night saving vs v2.11.0: ~80 % (5 min vs 30 s × 3 coordinators)
  - Combined daily saving vs original v2.1.0: ~60–70 %

### v2.11.0 (2026-05-21)
**Aggressive Modbus optimisation + telemetry sensors**

- **New:** `modbus_guard.py` — per-inverter asyncio lock with 150 ms
  inter-request gap.
- **New:** `register_cache.py` — TTL-aware register cache with static-prefix
  detection.  Up to 40 % fewer Modbus frames on a quiet system.
- **New:** `modbus_telemetry.py` — rolling 1-hour Modbus traffic statistics
  with 10 new HA diagnostic sensor entities per inverter.
- **Improved:** `update_coordinator.py` integrates guard, cache, and telemetry.
  Stale-cache fallback on timeout.
- **Improved:** Cache invalidation in `number.py`, `select.py`, `switch.py`.
- **Docs:** New `CLAUDE.md`.

### v2.10b (2026-05-20)
**Timeout hardening + battery entity improvements**

- `UPDATE_TIMEOUT` raised to 35 s, `OPTIMIZER_UPDATE_TIMEOUT` to 120 s.
- Exponential back-off with jitter after 3 consecutive timeouts.
- `retry_after` hints on all `UpdateFailed` raises.
- Battery number entities enabled by default with step values.
- New read-only end-of-charge SOC sensor.

### v2.1.0 (upstream)
Original HACS release.

---

## 8. Developer guide

### Adding a new register sensor

1. Find the register name constant in `huawei-solar`'s `register_names.py`.
2. Add a `HuaweiSolarSensorEntityDescription` entry to the appropriate
   `*_SENSOR_DESCRIPTIONS` tuple in `sensor.py`.
3. Add a translation string to `strings.json` and `translations/en.json`
   under `entity.sensor.<translation_key>.name`.
4. Run `python3 .github/verify_translation_strings.py` from the repo root.

### Adding a new writable number entity

1. Add a `HuaweiSolarNumberEntityDescription` to `ENERGY_STORAGE_NUMBER_DESCRIPTIONS`
   (or the appropriate group) in `number.py`.
2. Set `entity_registry_enabled_default=True` if it should be visible without
   extra configuration.
3. Add translation strings as above (under `entity.number.<key>`).

### Running syntax + JSON checks locally

```bash
# From repo root (not from inside the custom_components directory)
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

### Key constants to tune (`const.py`)

| Constant | Default | Effect |
|---|---|---|
| `INVERTER_UPDATE_INTERVAL` | 30 s | Main inverter poll rate |
| `UPDATE_TIMEOUT` | 35 s | Per-request timeout |
| `MAX_CONSECUTIVE_TIMEOUTS` | 3 | Back-off activation threshold |
| `MODBUS_RETRY_BASE_WAIT` | 10 s | Back-off base delay |
| `MODBUS_RETRY_MAX_WAIT` | 120 s | Back-off cap |

### `ModbusGuard` tuning (`modbus_guard.py`)

| Constant | Default | Effect |
|---|---|---|
| `MIN_INTER_REQUEST_GAP` | 150 ms | Minimum pause between requests |
| `QUEUE_WAIT_TIMEOUT` | 10 s | Max queue wait before abandoning |

### `RegisterCache` static TTL (`register_cache.py`)

Extend `_STATIC_PREFIXES` to add more register names that should be cached for
5 minutes.  Use lowercase prefixes matching the start of the register name.
