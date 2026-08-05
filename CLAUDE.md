# CLAUDE.md — Huawei Solar Integration

> **Maintained by Claude (Anthropic) on behalf of the community.**
> Current version: **1.3.14** — see `manifest.json`.

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
        ├── synchronized_power_coordinator.py  # Coherent multi-inverter power snapshot
        │
        ├── battery_health.py                  # ← NEW (1.1.5): BHI v2 pure engine (no HA imports)
        ├── battery_health_manager.py          # ← NEW (1.1.5): coordinator glue + Store persistence
        ├── battery_health_entities.py         # ← NEW (1.1.5): push-based BHI sensor entities
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
            ├── test_synchronized_power_coordinator.py
            ├── test_battery_health.py
            ├── test_battery_health_entities.py          ← NEW (1.1.7)
            └── test_battery_health_isolation.py         ← NEW (1.1.7)
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

### v1.1.4 (2026-06-20)
**Code optimization + entity-layer test coverage**

Follow-up to the v1.1.3 audit. No behavioural change; cleanup, one safe
performance optimization, and new tests for the previously-untested entity layer.

#### Optimizations
- **Dead code removed:** 12 unused imports across 7 modules and 5 unreferenced
  constants in `const.py` (`DEFAULT_SLAVE_ID`, `DEFAULT_SERIAL_SLAVE_ID`,
  `DEFAULT_PASSWORD`, `DATA_UPDATE_COORDINATORS`, `CONFIGURATION_UPDATE_TIMEOUT`),
  plus a dead `try/except` `_HAS_RN` block in `night_mode.py`. `pyflakes` is now
  clean on all production modules.
- **`update_coordinator._modbus_address` memoised** (`@lru_cache`): the
  reflection-heavy attribute walk used to sort each batch by Modbus address ran
  on every poll though a register's address never changes. Now a one-time cost
  per register (same pattern as `register_cache._classify`).
- **Outcome-recording consolidated** (`_record_timeout` / `_record_failure`):
  the timeout and failure bookkeeping (consecutive counters + telemetry feed +
  adaptive RTT tuner) was duplicated byte-for-byte across 8 `except` blocks. It
  now lives in two single-dispatch helper methods. The deliberately-split
  success path (telemetry counts immediately; adaptive records later with the
  accumulated RTT — the BUG-4/BUG-10 fixes) is intentionally left intact.
  Guarded by new regression tests that fail if the duplication returns.

> **Optimizations deliberately NOT made** (evaluated and rejected on merit, not
> just caution): (a) per-poll caching of the register-name set — negligible
> gain, and correct invalidation would require hooking HA listener internals,
> risking stale entities; (b) de-duplicating the per-poll `guard.update_gap()`
> push — the `ModbusGuard` is **shared by endpoint** across coordinators, so
> "push every poll, last-writer-wins" is *required* for the guard to track the
> active params; caching would let it drift; (c) merging the four health/timing
> subsystems (telemetry, adaptive, keep-alive, back-off) into one object — they
> have distinct responsibilities (diagnostic sensors, RTT tuning, socket
> probing, poll suppression); merging would couple unrelated concerns and change
> the diagnostic/persistence data model for no runtime benefit.

#### Entity-layer test coverage (new `tests/test_entities.py`, 14 tests)
The number/switch/select/button entities — which contain the user-facing **write**
paths to the inverter — previously had no executable tests. Added coverage for:
read/availability (`_handle_coordinator_update` populates value and goes
unavailable when the register is absent), write success (calls `device.set`,
invalidates the cache, requests a refresh), write failure (no cache invalidation),
number min/max precedence (static vs dynamic vs description vs default), the
switch `check_is_available_func` override, and the button stop-forcible-charge
write sequence. Also added regression tests locking in the outcome-recording
consolidation (timeout/failure bookkeeping must appear exactly once).

### v1.1.3 (2026-06-19)
**Independent industrial-grade audit — 12 bugs fixed (2 HIGH), test suite made runnable**

Full static + dynamic audit. Every fix was verified by executing the production
code. Two findings were treated as deployment blockers.

#### Bugs fixed

| ID | Sev | File | Root cause | Fix |
|----|-----|------|------------|-----|
| A1 | **HIGH** | `modbus_guard.py` | `__aenter__` cleanup used `except Exception`, which does not catch `asyncio.CancelledError`. A cancellation during the inter-request gap sleep (after the lock was acquired) leaked the lock **and** the queue counter, permanently deadlocking the whole Modbus bus until an HA restart. The gap runs on every request, so the window is present on every poll. | Catch `BaseException`; track `lock_acquired` and release the lock in the cleanup path before re-raising. Regression test cancels a task mid-gap and asserts the lock + counter are released and the bus is still usable. |
| A2 | **HIGH** | `register_cache.py` ↔ `sensor.py` | `is_energy_counter()` matched a hand-maintained substring list that had drifted from the `TOTAL_INCREASING` energy sensors in `sensor.py`. **23 of 47** energy accumulators (incl. `STORAGE_TOTAL_CHARGE/DISCHARGE`, `TOTAL_DC_INPUT_POWER`, every `*_today` counter, `GRID_EXPORTED_ENERGY`) were unrecognised, so the stale-cache exclusion **and** the suspicious-zero guard silently did not protect them — re-introducing the sunrise/sunset Energy-dashboard corruption. | Added an authoritative `_ENERGY_COUNTER_NAMES` frozenset (source of truth). New `tests/test_energy_counter_coverage.py` re-derives the set from `sensor.py` via AST and fails if the two ever drift again. |
| A3 | MED | `__init__.py` | `async_unload_entry` called the **global** `clear_registry()` on all four singleton registries, wiping instances owned by *other* still-loaded config entries → broken bus serialisation + leaked keep-alive tasks for the surviving entry. | Added targeted `remove()` to each registry; unload now removes only this entry's per-serial / per-endpoint instances. |
| A4 | MED | `update_coordinator.py` | Suspicious-zero guard dropped the register from `fresh` but did not invalidate the cache entry, so `cache.merge()` re-injected the stale prior value — the sensor showed a flat value instead of going unavailable (contradicting the documented design and the timeout path). | Call `cache.invalidate(name)` when dropping, so `merge` skips it and the sensor goes unavailable. |
| A5 | MED | `synchronized_power_coordinator.py` | `home_consumption` substituted `0` for a failed **battery** read on a battery system (off by the real battery power); `pv_power_total` silently dropped a failed INV2 — both reported a wrong number instead of unavailable. | Added `has_inv2` / `has_meter` / `has_battery` topology flags; derived properties return `None` when an *installed* input failed to read this tick. |
| A6 | MED | `services.py` | Forcible charge/discharge/stop services were registered whenever a battery was present, even under an EMMA — contradicting the "no direct battery control with EMMA" design and allowing writes that conflict with EMMA. | Registered only when `not has_emma`, consistent with the TOU-period split. |
| A7 | MED | `register_cache.py` | `TOTAL_DC_INPUT_POWER` (a kWh accumulator) was in `_FAST_SUBSTRINGS` → polled every cycle, TTL 0. | Added to `_SLOW_PRIORITY_SUBSTRINGS` (checked before FAST) and removed from FAST → classified `SLOW`. |
| A8 | LOW | `services.py` | `_parse_time` accepted `24:00` and minutes `60–99`. | Strict HH:MM (`(?:[01]\d\|2[0-3]):[0-5]\d`) in all period regexes; `_parse_time` validates components (00:00–23:59). |
| A9 | LOW | `services.py` | TOU/capacity/fixed-charge regexes accepted empty input that then crashed the parser with an unhandled `ValueError`; day field accepted zero days. | Parsers skip blank lines (empty input safely clears periods); day field requires `[1-7]{1,7}`. |
| A10 | LOW | `services.py` | `_validate_power_value` raised `TypeError` if the max-power register read returned `None`. | Explicit `None` guard with a clear error. |
| A11 | LOW | `update_coordinator.py` | Suspicious-zero guard `_prior.value > 0` could `TypeError` on a cached `None`. | Guard `_prior.value is not None` before comparing. |
| A12 | LOW | `update_coordinator.py` | Back-off priority fallback `stale_names[:BATCH_CHUNK_SIZE]` could read SLOW/STATIC despite "deferred entirely". | Serve the cached snapshot when no priority registers are due. |

#### Test suite — now runnable in a clean environment

| File | Change |
|---|---|
| `tests/test_modbus_keepalive.py` | Registered the missing `huawei_solar.modbus_guard` stub — all 18 tests now run (were dead at import). |
| `tests/test_synchronized_power_coordinator.py` | Provided `HomeAssistant`/`callback` on the core stub, registered the module in `sys.modules` before exec, made the coordinator stub generic — module now loads and collects all tests (was skipped at module level). |
| `tests/test_modbus_guard.py` | +2 regression tests (cancellation deadlock, targeted `remove`). |
| `tests/test_energy_counter_coverage.py` | **New** — asserts every `TOTAL_INCREASING` energy sensor is recognised by `is_energy_counter` (prevents A2 from recurring). |

**Files changed:** `modbus_guard.py`, `register_cache.py`, `update_coordinator.py`,
`synchronized_power_coordinator.py`, `services.py`, `modbus_telemetry.py`,
`adaptive_modbus.py`, `modbus_keepalive.py`, `__init__.py`, `manifest.json`,
plus tests. **No behavioural change for a single-entry, non-EMMA, healthy-bus
install** beyond the energy-counter protection now working as documented.

### v1.1.2 (2026-06-05)
**Energy dashboard negative-bar fix — state_class audit + suspicious-zero guard**

Two distinct but related bugs caused negative kWh bars and corrupted hourly
totals in the HA Energy dashboard, both visible at sunset/sunrise transitions.

#### Bug 1 — Wrong `state_class` on 24 lifetime-accumulator energy sensors (`sensor.py`)

**Root cause:** 24 kWh registers that can only ever increase were declared as
`state_class=TOTAL` instead of `TOTAL_INCREASING`.

With `TOTAL`, Home Assistant computes the hourly bar as `new_value − old_value`
and records the result verbatim — including **negative** deltas. When the
inverter briefly returns `0` for any of these registers (sleep entry, startup
flush, state-transition race), HA computes `0 − prev_value` and writes a large
negative bar to the statistics database.

With `TOTAL_INCREASING`, HA detects the downward movement and treats it as a
counter reset instead of a negative contribution, preventing the negative bar.

**Fix:** Changed all 24 affected sensors to `SensorStateClass.TOTAL_INCREASING`:

| Register | Description |
|---|---|
| `ACCUMULATED_YIELD_ENERGY` | Inverter lifetime yield |
| `TOTAL_DC_INPUT_POWER` | DC input energy total |
| `accumulated_energy_yield` | Secondary inverter accumulated yield |
| `STORAGE_TOTAL_CHARGE` | Battery lifetime charge |
| `STORAGE_TOTAL_DISCHARGE` | Battery lifetime discharge |
| `INVERTER_TOTAL_ABSORBED_ENERGY` | EMMA total absorbed |
| `TOTAL_CHARGED_ENERGY` | EMMA total charged |
| `TOTAL_ENERGY_CONSUMPTION` | EMMA total consumption |
| `TOTAL_FEED_IN_TO_GRID` | EMMA total feed-in |
| `TOTAL_SUPPLY_FROM_GRID` | EMMA total supply |
| `INVERTER_TOTAL_ENERGY_YIELD` | EMMA inverter yield |
| `TOTAL_PV_ENERGY_YIELD` | EMMA PV yield |
| `TOTAL_ACTIVE/POSITIVE/NEGATIVE_ENERGY_BUILT_IN` | Built-in meter totals (×3) |
| `TOTAL_ACTIVE/POSITIVE/NEGATIVE_ENERGY_EXTERNAL` | External meter totals (×3) |
| `SMARTLOGGER_TOTAL_POWER_SUPPLY_FROM_GRID` | SmartLogger grid supply |
| `SMARTLOGGER_TOTAL_ENERGY_CHARGED` | SmartLogger battery charge |
| `SMARTLOGGER_TOTAL_ENERGY_DISCHARGE_D` | SmartLogger battery discharge |
| `SMARTLOGGER_TOTAL_ENERGY_YIELD` | SmartLogger yield |
| `SMARTLOGGER_EXTERNAL_METER_TOTAL_ACTIVE/REACTIVE` | External meter (×2) |

#### Bug 2 — Suspicious-zero guard for live Modbus reads (`update_coordinator.py`)

**Root cause:** The v1.0.3 stale-cache exclusion correctly withholds energy
counters during Modbus *timeouts*, but a *successful* live read returning `0`
(e.g., the SUN2000 flushing registers during sleep-mode entry) bypassed that
protection entirely. The `0` was cached and forwarded to HA as a valid value.

- With `TOTAL` sensors (Bug 1): produced a **negative bar** (immediate, visible)
- With `TOTAL_INCREASING` sensors: produced a **positive spike** in the wrong
  hourly bucket on recovery (subtler, but still corrupts totals)

**Fix:** In the success path of `_async_update_data()`, before `cache.update()`,
any energy-counter register that arrives as `0` from a live read is dropped from
`fresh` if the cache already holds a non-zero value for that register. The
sensor entity then finds the register absent from `coordinator.data` and marks
itself `unavailable` — an honest gap that HA interpolates correctly, consistent
with the v1.0.3 design philosophy.

A genuine midnight reset of a daily counter is **not** affected: by the time
the inverter clocks midnight, the cached prior value is already at or near `0`
from declining end-of-day production, so the guard condition (`prior.value > 0`)
does not fire.

#### Files changed

| File | Change |
|---|---|
| `sensor.py` | 24 × `TOTAL` → `TOTAL_INCREASING` for kWh lifetime accumulators |
| `update_coordinator.py` | Step 11: suspicious-zero guard before `cache.update()` |
| `manifest.json` | Version bumped to `1.1.2` |

#### HA statistics database note

The `state_class` change is **not retroactive**. Existing long-term statistics
rows already recorded as `TOTAL` will remain in the database unchanged. Going
forward, HA will use the new `TOTAL_INCREASING` logic for all new rows. If
historical negative bars are visible in your Energy dashboard, they can be
cleared via **Developer Tools → Statistics → Fix issue** for each affected
sensor, or by deleting the statistics rows for those entities.

### v1.1.1 (2026-05-29)
**7-bug runtime fix release — adaptive Modbus controller hardening**

Second-pass audit of the adaptive Modbus learning subsystem (`adaptive_modbus.py`)
and the write-verification path (`update_coordinator.py`).  All 7 confirmed runtime
bugs fixed; 19 new regression tests added; full 177-test suite passes.

#### Bugs fixed

| ID | File | Root cause | Fix |
|----|------|------------|-----|
| BUG-003 | `adaptive_modbus.py` | `_push_to_listeners` iterated directly over `self._listeners`; a callback calling `remove_listener()` during dispatch caused subsequent listeners to be skipped | Iterate over `list(self._listeners)` (snapshot copy) so mid-iteration removal is safe |
| BUG-004 | `adaptive_modbus.py` | `stop()` cancelled the debounced save task without flushing; up to 60 s of adaptive learning data was lost on every reload/restart | Set `_dirty` guard; after cancelling the task, schedule an immediate `_async_save()` if `_dirty` is set |
| BUG-005 | `adaptive_modbus.py` | `TimeSlotStats.label` always returned `""` because the dataclass did not store the slot index; the comment admitted it was broken | Added `slot_index: int` field to `TimeSlotStats`; updated `_reset_slots()`, `from_dict()`, and all construction sites to propagate the index; `label` now returns the correct `HH:MM–HH:MM` string |
| BUG-008 | `update_coordinator.py` | `verify_write` called `cache.update()` directly after a live read without first calling `cache.invalidate()`; a concurrent cache write between the read and the update could leave a stale value | Call `self.cache.invalidate(name)` immediately before `cache.update()` in the verification success path |
| BUG-009 | `adaptive_modbus.py` | `_deferred_save` did not handle `asyncio.CancelledError`; cancellation during `stop()` could suppress the exception and leave cleanup incomplete | Added explicit `except asyncio.CancelledError: raise` so cancellation propagates correctly |
| BUG-010 | `adaptive_modbus.py` | `_schedule_save` returned early if a debounce task was already in-flight, silently discarding the dirty flag; data recorded after task creation but before its 60 s sleep expired was never persisted | Set `self._dirty = True` unconditionally before the early-return guard; the sleeping task re-checks the flag on wake and persists the latest state |
| BUG-011 | `adaptive_modbus.py` | `_push_to_listeners` had no exception isolation; one failing callback would abort delivery to all subsequent listeners | Wrap each `cb_fn(snap)` call in `try/except Exception` with `_LOGGER.exception`; all listeners always receive their update |

#### Test suite — 177 tests, 0 failures

| Test file | Tests | New | Covers |
|---|---|---|---|
| `test_adaptive_modbus.py` | 58 | +19 | BUG-003/004/005/009/010/011 regressions, slot label correctness, flush-on-stop, debounce dirty-flag, CancelledError propagation, listener isolation |
| `test_update_coordinator.py` | 28 | +5 | BUG-008 invalidate-before-update, verify_write concurrency path |
| `test_register_cache.py` | 48 | 0 | Unchanged — all 48 pass |
| `test_modbus_guard.py` | 22 | 0 | Unchanged — all 22 pass |
| `test_modbus_keepalive.py` | 18 | 0 | Unchanged — all 18 pass |
| `test_modbus_telemetry.py` | 8 | 0 | Unchanged — all 8 pass |
| `test_synchronized_power_coordinator.py` | (existing) | 0 | Unchanged |

### v1.1.0 (2026-05-28)
**Production-ready: 10-bug audit, full fix, and 158-test suite**

Full systematic audit of all new code introduced in v1.0.2–v1.0.6.  Ten bugs
found and fixed, 158 unit tests written and verified passing (stdlib unittest,
no external dependencies).

#### Bugs fixed

| ID | File | Bug | Fix |
|----|------|-----|-----|
| BUG-1 | `modbus_keepalive.py` | `self._last_ok` was updated *before* computing `time.monotonic() - self._last_ok`, so "was down for" always logged 0 s and "connection healthy" always showed 0 ms RTT | Capture `down_for` and `rtt_ms` from `probe_start` *before* updating `_last_ok` |
| BUG-2 | `modbus_keepalive.py` | `asyncio.ensure_future()` deprecated since Python 3.10 | Replaced with `_create_task()` helper that uses `loop.create_task()` with `ensure_future` as fallback |
| BUG-3 | `register_cache.py` | `_classify()` checked `_FAST_SUBSTRINGS` before `_SLOW_SUBSTRINGS`; registers like `phase_a_active_power_built_in` and `active_power_external` contain `"active_power"` so were classified FAST (polled every 30 s) instead of SLOW (every 5 min) | Added `_SLOW_PRIORITY_SUBSTRINGS` checked before `_FAST_SUBSTRINGS`; 7 regression tests confirm the fix |
| BUG-4 | `update_coordinator.py` | `_execute_batch()` called `self._adaptive.record_request()` on each chunk, then `_async_update_data()` called it again in the success path — every successful batch was double-counted in the adaptive learning model | Removed all `record_request` calls from `_execute_batch()`; single authoritative call in the outer success/failure paths |
| BUG-5 | `adaptive_modbus.py` | `async_load()` error path called `_reset_slots()` but left `_last_decay_date` and `_first_data_date` intact; stale dates from a prior session would cause wrong decay on fresh zero slots | Reset both date fields to `None` in the except block before `_apply_startup_decay()` runs |
| BUG-6 | `adaptive_modbus.py` | `async_load()` created a new `async_track_time_interval` subscription without cancelling the previous one; a config-entry reload would leak subscriptions indefinitely | Guard with `if self._unsub_push: self._unsub_push()` before creating the new subscription |
| BUG-7 | `adaptive_modbus.py` | `days_of_data` property could return negative values if `_first_data_date` is in the future (clock skew / NTP correction) | Clamp result with `max(0, ...)` |
| BUG-8 | Tests | `test_modbus_guard.py` used `guard.serial_number` but v1.0.5 changed the guard to use `guard.endpoint` | Tests rewritten to use `.endpoint` |
| BUG-9 | `modbus_keepalive.py` | `RegisterName[KEEPALIVE_REGISTER]` raises `KeyError` if the constant is wrong (e.g., library version mismatch) and would crash the keep-alive loop | `_get_keepalive_register()` wraps in try/except, logs a warning, returns `None`; `_probe()` skips if `None` |
| BUG-10 | `update_coordinator.py` | `telemetry.record_request(N)` called *before* `_execute_batch()`; BUSY retries and multi-chunk execution meant the actual request count and RTT were unknown at that point. `_execute_batch()` also returned only `merged` dict, discarding the accumulated RTT | `_execute_batch()` now returns `(merged, total_rtt_ms)` tuple; caller records telemetry and feeds adaptive controller *after* the batch completes |

#### Test suite — 158 tests, 0 failures

| Test file | Tests | Covers |
|---|---|---|
| `test_modbus_guard.py` | 22 | Queue depth, load shedding, priority, adaptive setters, gap enforcement, bus-level registry (BUG-8) |
| `test_register_cache.py` | 48 | All tiers, BUG-3 regression (9 cases), energy counter detection, adaptive TTL, night mode, filter_stale, set_telemetry |
| `test_update_coordinator.py` | 23 | BUG-4/10 tuple return, telemetry ordering, energy counter exclusion, priority back-off, verify_write, keepalive callbacks |
| `test_modbus_telemetry.py` | 8 | Deque eviction (BUG implied), lifetime totals, batch cache hits |
| `test_adaptive_modbus.py` | 39 | BUG-5/6/7 fixes, TimeSlotStats, parameter bounds, cold-start 60 s, _derive_params, persistence |
| `test_modbus_keepalive.py` | 18 | BUG-1/2/9 fixes, probe paths, lifecycle, registry |

### v1.0.6 (2026-05-27)
**Adaptive parameter bound tuning — evidence-based review of Gemini's proposal**

Reviewed all six parameter bounds proposed by Gemini Pro against hardware
constraints, statistical theory, and the v1.0.5 architecture.  Accepted 3,
rejected 2, modified 1, and introduced one new structural constant.

| Parameter | v1.0.5 | Gemini | v1.0.6 | Decision rationale |
|---|---|---|---|---|
| Poll interval min | 30 s | 15 s | **20 s** | 15 s excessive inverter CPU load; 20 s safe with bus-level guard, meaningful for power-flow card |
| Poll interval max | 120 s | 300 s | **180 s** | 300 s indistinguishable from night mode during daytime; 180 s allows real back-off without confusion |
| Modbus gap min | 150 ms | 30 ms | **150 ms (unchanged)** | **Rejected.** 150 ms is a hardware FSM reset constraint, not a network variable. 30 ms causes pervasive 0x06 BUSY on all SUN2000 hardware |
| Modbus gap max | 500 ms | 500 ms | **500 ms (unchanged)** | Agreed |
| Timeout min | 35 s | 10 s | **15 s** | 10 s fires during legitimate 8–12 s transition-window responses; 15 s is the safe floor |
| Timeout max | 90 s | 45 s | **60 s** | Keep-alive (v1.0.5) now handles dead-socket detection; 60 s covers multi-chunk slow reads |
| Queue depth max | 3 | 4 | **3 (unchanged)** | **Rejected.** Guard is a serialiser, not a thread pool; depth 4 worsens outage pile-up |
| Cold-start baseline | 30 s (= POLL_MIN) | 60 s | **60 s as `ADAPTIVE_POLL_COLD_START`** | Accepted direction; implemented as a *separate* constant so lowering POLL_MIN never affects unknown-slot behaviour |
| Confidence ceiling | 300 samples | 60 samples | **150 samples (~5 days)** | 60 too fast (single bad day = 67% weight at full confidence); 300 too slow (10 days); 150 balances stability and adaptation speed |

**Files changed:** `const.py`, `adaptive_modbus.py`, `modbus_guard.py`, `update_coordinator.py`
**New constant:** `ADAPTIVE_POLL_COLD_START = timedelta(seconds=60)`

### v1.0.5 (2026-05-27)
**Six high-impact Modbus reliability improvements**

Targets the two structural failure modes identified in telemetry:
(A) RS485 bus collisions between 10K and 5K inverters sharing the same physical
    wire — the direct cause of the 5K's 20× higher failure rate.
(B) Silent TCP connection death during night-mode idle gaps causing 35–90 s dead
    timeouts on the first post-night poll.

| # | Opt | Files changed | What & why |
|---|-----|---------------|------------|
| 1 | Bus-level guard | `modbus_guard.py`, `update_coordinator.py`, `__init__.py` | Guard registry key changed from `serial_number` to `connection_endpoint` (host:port or rtu:port). All sub-devices on the same RS485 bus now share one guard. `endpoint_for(entry.data)` derives the key once in `async_setup_entry`; it is passed through `_setup_device_data` → `_setup_inverter_device_data` → every coordinator constructor and `create_optimizer_update_coordinator`. **Expected result: 5K failure rate drops from ~13% to near-baseline.** |
| 2 | 0x06 BUSY retry | `update_coordinator.py`, `const.py` | `ReadException` with `modbus_exception_code == 0x06` (SLAVE_DEVICE_BUSY) is now handled separately in `_execute_batch()`. On first BUSY: pause `BUSY_RETRY_PAUSE` (600 ms) then retry the chunk. Up to `BUSY_MAX_RETRIES` (2) retries before counting as a failure. First BUSY also calls `notify_transition()` on the adaptive controller — BUSY at runtime is a reliable signal of an inverter state change (MPPT ramp, mode switch, BMS wake). **Expected result: transition-period failure spikes turn into slow-but-successful requests.** |
| 3 | Keep-alive + health probe | `modbus_keepalive.py` *(new)*, `__init__.py`, `update_coordinator.py` | `ModbusKeepAlive` runs a background task per inverter that reads `model_id` (1 static register) every 45 s via `guard.request(priority=True)`. Keeps TCP alive through night-mode idle gaps. On failure: calls `on_connection_lost()` → cache invalidated, failure counters reset. On recovery: calls `on_connection_restored()`. Priority requests bypass queue-depth shedding but still wait for the lock and respect the inter-request gap. **Expected result: post-night poll no longer hits a dead socket; eliminates the 35–90 s reconnect timeout.** |
| 4 | Batch chunking | `update_coordinator.py`, `const.py` | `_execute_batch()` splits stale register lists into chunks of ≤ `BATCH_CHUNK_SIZE` (40) registers. Between chunks: 80 ms pause (`BATCH_INTER_CHUNK_PAUSE`) outside the guard lock, letting other clients interleave. Limits each Modbus burst to ~300 ms of inverter CPU time, reducing the probability of triggering 0x06 BUSY responses during the burst. |
| 5 | Write-back verification | `update_coordinator.py`, `const.py` | `verify_write(name, expected)` reads the register back 3 s after a write and compares against the expected value. Up to `WRITE_VERIFY_RETRIES` (2) additional retries with 3 s spacing. Logs a warning if the inverter did not apply the setting (common for working-mode changes during state transitions). Callers: number/select/switch entities after any `set_*` call. |
| 6 | Priority polling during back-off | `update_coordinator.py`, `const.py` | During exponential back-off (`_consecutive_timeouts ≥ MAX_CONSECUTIVE_TIMEOUTS`), stale registers are filtered by tier: FAST always read (real-time power, SOC, grid values), NORMAL read every `BACKOFF_NORMAL_DIVISOR` (4th) cycle, SLOW/STATIC deferred entirely. This keeps the most critical HA automations (battery rules, grid limits) informed even during a partial outage, while reducing Modbus traffic when the inverter is under stress. |

**New file:** `modbus_keepalive.py`
**New constants:** `BUSY_RETRY_PAUSE`, `BUSY_MAX_RETRIES`, `KEEPALIVE_INTERVAL`,
`KEEPALIVE_REGISTER`, `BATCH_CHUNK_SIZE`, `BATCH_INTER_CHUNK_PAUSE`,
`WRITE_VERIFY_DELAY`, `WRITE_VERIFY_RETRIES`, `BACKOFF_FAST_ALWAYS`,
`BACKOFF_NORMAL_DIVISOR`

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

### v1.3.14 (2026-08-05)
**Defects L, M, N, O — four fixes from one review pass: three reported
independently by the operator (L, M, N), one bonus find while reviewing
switch.py for a separate reason (O). All four target the same theme this
session has been working through all night: nothing on the setup/reload
critical path should be able to block indefinitely, outlive the entry that
created it, or fail in a way Home Assistant can't retry cleanly.**

**Defect L — the Defect K deferred-poll task had no lifecycle of its own.**
`_schedule_deferred_first_poll()` (v1.3.13) called
`self.hass.async_create_task(_deferred())` with no stored handle and no tie
to the config entry. If the entry reloaded or unloaded before the stagger
delay elapsed, the old task kept running regardless — and since Defect J1
made every coordinator's guard correctly resolve to the SAME shared
`ModbusGuard` for a bus, a stale task's eventual `async_request_refresh()`
could inject uncoordinated traffic into that same shared queue at exactly
the moment a fresh setup was trying to establish itself.

Fixed with two independent layers: the task is now created via
`entry.async_create_background_task()` (falling back to
`hass.async_create_task()` if no entry was supplied), which Home Assistant
cancels automatically on unload — and, as a second, independent guard
against the narrow race where the sleep completes right as unload begins,
the deferred coroutine checks a new `self._shutdown` flag (set via
`entry.async_on_unload()`) before calling `async_request_refresh()`.
`HuaweiSolarUpdateCoordinator.__init__` gained an `entry` parameter,
threaded through from all six construction call sites in `__init__.py`.

**Defect M — `create_device_instance()` had no bound of its own.** The
very first `await` in `async_setup_entry` — establishing the connection and
running the vendor library's device-detection sequence — had no timeout.
A field traceback this session confirmed this can be slow enough, right
after a reconnect, that Home Assistant's own external setup timeout
cancels the whole entry with an unhandled `asyncio.CancelledError` —
which, being a `BaseException` rather than an `Exception`, bypassed every
existing `ConfigEntryNotReady` handler already in this function (all of
which catch ordinary exceptions like `TimeoutError` and
`ConnectionException` just fine).

Rather than trying to intercept and reinterpret an external cancellation,
the call is now wrapped in `asyncio.wait_for(..., timeout=DEVICE_CONNECT_TIMEOUT)`
(45s — generously above the ~30-40s worst case directly observed for this
phase, but meaningfully shorter than the ~50s+ at which the external
cancellation was seen to fire). On timeout, we now give up first, in a
controlled way, raising our own `ConfigEntryNotReady` — which Home
Assistant already retries cleanly — instead of being caught by an external
cancellation with a raw, alarming traceback.

**Defect N — optimizer discovery had the same gap.**
`device.get_optimizer_system_information_data()` in
`_setup_inverter_device_data` — a one-time vendor-library file read,
outside `ModbusGuard`, run before the optimizer coordinator (whose own
first refresh was already backgrounded for Defect G) even exists — had no
bound either. Its existing `except Exception` guard stops it from
crashing setup, but does nothing to stop it from extending the overall
setup duration before failing. Fixed the same way as Defect M: wrapped in
`asyncio.wait_for(..., timeout=OPTIMIZER_DISCOVERY_TIMEOUT)` (30s — a
reasoned, moderate bound; no direct field measurement exists yet for this
specific call, unlike Defect M's create_device_instance figures). On
timeout, optimizer entities are simply skipped for this setup pass —
identical in effect to the existing `except Exception` fallback — and
retried automatically on the next reload.

**Defect O — a 10x constant/comment mismatch in `switch.py`.**
`MAX_STATUS_CHANGE_TIME_SECONDS = 3000` sat beside a comment reading
*"Maximum status change time is 5 minutes"* — 3000s is 50 minutes, not 5.
Found while reviewing this file for the operator's separate (deferred,
lower-priority) guard-bypass finding. Corrected to `300`, matching both
the comment's stated intent and the physically reasonable figure for a
SUN2000's actual startup/shutdown sequence.

**What stayed deliberately out of scope for this release.** The operator
also flagged that `switch.py`'s on/off polling loop bypasses `ModbusGuard`
entirely — a real, lower-severity finding (bounded to a user-triggered
action, not the setup path) that was explicitly agreed to defer rather
than bundle in here. Also unchanged: the underlying reason a client-level
TCP/RTU connection object is not explicitly closed when
`create_device_instance()` fails during initial connection — an existing
gap (present before this release, for every exception type this phase can
raise, not something this release introduces or worsens) noted for a
future look rather than expanded into scope tonight.

**Adversarial verification.** New `tests/test_defects_l_m_n_o.py` (13
tests) covering all four defects together: behavioural reproductions of
the old vs. new deferred-poll pattern (proving the old pattern fires stray
refreshes after "unload" and the new one doesn't, with no regression to
normal operation); behavioural confirmation that a slow connect/discovery
call raises promptly under `asyncio.wait_for` rather than hanging; static
(AST) checks that the real source uses `entry.async_create_background_task`
and checks `self._shutdown`, that `create_device_instance(...)` and
`get_optimizer_system_information_data(...)` are both wrapped in
`asyncio.wait_for` with a nearby `except TimeoutError` → `ConfigEntryNotReady`
conversion, and a direct value check that `MAX_STATUS_CHANGE_TIME_SECONDS`
equals 300. Run against the pristine pre-session baseline, all four static
checks fail correctly (three with "not found" — these mechanisms did not
exist before this session — and Defect O's with the exact wrong value,
3000).

**Tests: 507 -> 520 passed, 1 skipped**, deterministic across 3 repeated
runs. `update_coordinator.py`, `__init__.py`, `const.py`, and `switch.py`
changed among production files.

### v1.3.13 (2026-08-05)
**Defect K — the coordinator first-poll stagger delay blocked a
synchronous caller, confirmed as the direct mechanism behind a real
"Setup of config entry ... cancelled" field incident: the exact error this
project has been trying to explain since the original 2026-08-04 handoff.**

**The evidence — a full traceback, captured for the first time.** A field
reload was captured with debug logging on and produced, for the first time
in this entire investigation, a complete traceback for the cancellation
error:

```
File ".../homeassistant/helpers/entity_platform.py", line 858, in _async_add_entity
    await entity.async_device_update(warning=False)
File ".../homeassistant/helpers/entity.py", line 1378, in async_device_update
    await self.async_update()
File ".../homeassistant/helpers/update_coordinator.py", line 711, in async_update
    await self.coordinator.async_request_refresh()
  ...
File "/config/custom_components/huawei_solar/update_coordinator.py", line 616, in _async_update_data
    await asyncio.sleep(self._start_delay.total_seconds())
File ".../asyncio/tasks.py", line 704, in sleep
    return await future
asyncio.exceptions.CancelledError
```

**Root cause.** `_async_update_data()`'s first-poll stagger
(`_COORDINATOR_START_DELAYS`, v1.0.3) slept inline:
`await asyncio.sleep(self._start_delay.total_seconds())`. This delay was
designed with only the coordinator's own background polling schedule in
mind. The traceback proves `_async_update_data()` is also reachable
**synchronously**, from Home Assistant's own entity-add machinery during
platform setup — meaning the sleep directly extended a real, in-progress
Home Assistant setup call by up to the full stagger delay. Worse still for
a second daisy-chained device since Defect I (v1.3.10) added a per-device
offset on top of the per-type one (up to 19s for `energy_storage` on
device index 1, versus 14s before). When cumulative setup time (device
detection, per-entity setup, this sleep) exceeded whatever timeout Home
Assistant enforces on config entry setup, Home Assistant cancelled the
in-progress setup — landing, in the captured incident, exactly mid-sleep.

**This closes an investigation that has spanned this entire session and
predates it.** The original 2026-08-04 handoff flagged "Setup of config
entry cancelled" recurring for 4+ hours with causal attribution explicitly
marked unresolved; this is the first time a full traceback has actually
been caught for it.

**Fix.** The sleep no longer runs inline. `_async_update_data()`'s first
call now returns immediately — a copy of any existing cached data, or an
empty dict on a genuine first call (already an established, safe pattern
in this same method for "nothing to poll," and identical to how every
entity already handles "no data yet") — and schedules the real first poll
as a background task (`_schedule_deferred_first_poll()`) that sleeps for
the stagger delay and then calls `async_request_refresh()` itself. This
preserves the exact same effect on bus traffic (nothing real hits the bus
before the stagger deadline — the whole point of `_COORDINATOR_START_DELAYS`
and Defect I's device-aware extension of it) without ever occupying a
caller's stack for the delay, synchronous or not.

**Adversarial verification.** New `tests/test_deferred_first_poll.py`:
an isolated reproduction of the exact fixed logic proves the first call
returns near-instantly even with a 10-second configured delay, that no
real work happens on that first call, and that the deferred background
task performs the real work once the delay elapses. A companion
adversarial test proves the OLD (inline-sleep) pattern really does block
the caller for the full delay, confirming the fixed pattern's pass is
meaningful. A static (AST) check confirms the real `update_coordinator.py`
no longer awaits `asyncio.sleep(...)` directly inside the stagger block
(only reachable from inside the deferred task's own coroutine) and that
`_schedule_deferred_first_poll` exists. Run against the pre-fix file, both
static checks fail at the exact original line (616); against this release,
they pass.

**What this release does NOT claim.** The exact reason a particular
entity's add ended up on this synchronous path (rather than the push-based
pattern most `CoordinatorEntity` usage relies on) was not independently
root-caused against Home Assistant's own internals — this fix closes the
risk regardless of that mechanism, by making the delay structurally
incapable of blocking any caller, synchronous or not, rather than by
patching whichever specific entity triggered it this time.

**Tests: 500 -> 507 passed, 1 skipped**, deterministic across 3 repeated
runs. Only `update_coordinator.py` changed among production files.

### v1.3.12 (2026-08-05)
**Defect V2-1 — a second, deeper pass from the same independent ICS audit
(addendum report, against v1.3.10) found the write-permission probe in
`sensor.py` was evaluated too early in its own eligibility check, wasting
the bounded-but-real probe on devices that could never use it. Medium
severity, small fix.**

**Reported and verified.** `create_sun2000_entities()`'s guard for the
optional Active Power Control Mode entity read:

```python
if (
    not isinstance(ucs.device.primary_device, (EMMADevice, SmartLoggerDevice))
    and await _has_write_permission_bounded(ucs.device, ucs.device.serial_number)
    and ucs.configuration_update_coordinator
):
```

Python's `and` short-circuits left to right, so the bounded probe (Defect
H, v1.3.9) ran *before* the free `ucs.configuration_update_coordinator`
check. On any device with `CONF_ENABLE_PARAMETER_CONFIGURATION` off (no
configuration coordinator at all), the entity below could never be added
regardless of the probe's outcome — yet the probe still ran, spending real
Modbus traffic and up to `WRITE_PERMISSION_CHECK_TIMEOUT` for nothing. A
multi-inverter installation pays this once per ineligible device, on every
boot and reload.

**Fix.** Reordered so both cheap, free checks (`isinstance`,
`ucs.configuration_update_coordinator`) run first; the bounded probe now
only executes once the entity is already known to be eligible. Defect H's
bound (v1.3.9) remains in place unchanged — this is purely an ordering fix
on top of it, not a re-litigation of whether the probe needs a timeout.

**Adversarial verification.** New `tests/test_write_permission_ordering.py`:
an AST check confirms the `ucs.configuration_update_coordinator` check's
position in the condition precedes the awaited probe's position — run
against the pristine pre-fix ordering (present since this code was first
written, unchanged by any of v1.3.9 through v1.3.11), it fails correctly;
against this release, it passes. A companion behavioural test confirms the
general short-circuit semantics this fix relies on.

**Tests: 498 -> 500 passed, 1 skipped**, deterministic across 3 repeated
runs. Only `sensor.py` changed among production files.

### v1.3.11 (2026-08-05)
**Defect J — three findings from an independent ICS audit of the v1.3.10
package, each verified against source before fixing (nothing taken on
trust). One Critical, one High, one Medium.**

**J1 (Critical) — SynchronizedPowerCoordinator's ModbusGuard was keyed on
the wrong thing, silently defeating bus-level lock sharing.**
`synchronized_power_coordinator.py` created its guards with
`ModbusGuard.get_or_create(inv1_device.serial_number)` /
`(inv2_device.serial_number)` — confirmed exactly as reported, at the
reported lines. Every other coordinator in this codebase keys its guard on
the shared bus endpoint (`bus_endpoint or device.serial_number`, per
`update_coordinator.py`). Keying on serial_number instead meant this
coordinator's reads resolved to a **completely separate `ModbusGuard`
instance**, with zero awareness of the queue depth, pacing, or in-flight
requests every other coordinator on the same physical bus was tracking.
On any installation with daisy-chained inverters sharing one RS485 bus —
this project's own field installation among them — this coordinator's
traffic was never actually serialized against the rest of the bus at all.
This is a plausible, additional contributor to the broader multi-coordinator
shedding pattern this session's field investigation found only partially
explained by Defect I (`AUDIT_1.3.10.md`).

Fixed by threading `bus_endpoint` into `SynchronizedPowerCoordinator`
(from `__init__.py`, which already computes it once per entry) and using
the same `bus_endpoint or device.serial_number` convention as every other
coordinator. Both inverters on one entry now correctly resolve to the
exact same shared `ModbusGuard` instance as all the others.

**J2 (High) — number.py performed unbounded, unhandled raw Modbus reads
during platform setup.** `HuaweiSolarNumberEntity.create()` called
`await device.client.get(...)` directly, twice, once per number entity
with static min/max bounds — confirmed at the reported lines. This is the
same class of defect as Defect H (`sensor.py`'s write-permission probe,
`AUDIT_1.3.9.md`): no timeout, no exception handling, on the NUMBER
PLATFORM's own setup critical path, awaited once per affected entity
before `async_add_entities()` returns.

Fixed with the same pattern as Defect H: new `_read_static_bound()` bounds
each read to a new `STATIC_BOUND_READ_TIMEOUT` (5s) and catches every
exception, never letting one propagate into platform setup. On timeout or
failure the bound is simply left unset — identical in effect to the
entity's own existing "no static key configured" case.

**J3 (Medium) — dynamic min/max bounds were never cleared when their
source register disappeared.** `_handle_coordinator_update()` only assigned
`_dynamic_min_value`/`_dynamic_max_value` when the corresponding register
was present in the coordinator's data; when absent (a transient bus issue,
or a capability that stops being reported), the previous value was left in
place indefinitely, potentially misleading UI validation. Fixed: both are
now assigned via `register.value if register else None`, clearing the
bound explicitly rather than leaving it stale.

**A wider check was also done**, per this project's existing convention:
`select.py` and `button.py` have no similar setup-time raw-read pattern.
`switch.py`'s two `device.client.get()` calls are inside a bounded,
user-action polling loop (`async_turn_on`/`async_turn_off`, waiting for a
device state change), already covered by the write-path audit in
`AUDIT_1.3.9.md` §5 — not a new instance of this defect class.

**Adversarial verification.** New `tests/test_ics_audit_findings.py`:
static (AST) checks for all three findings, each run against the pre-fix
files and confirmed to fail at the exact reported line numbers (212, 219
for J1; 394, 400 for J2), then confirmed to pass against this release.
Behavioural tests for J1 (guard-key resolution arithmetic), J2 (bounded
read against fake slow/failing/healthy devices, mirroring Defect H's
verification style), and J3 (the reset-on-absence semantics).

**Tests: 487 -> 498 passed, 1 skipped**, deterministic across 3 repeated
runs. `synchronized_power_coordinator.py`, `number.py`, `const.py`, and
`__init__.py` changed among production files.

### v1.3.10 (2026-08-05)
**Defect I — the first-poll stagger scheme was device-blind. Confirmed by
name from a real debug capture of a two-inverter reload: ModbusGuard's
adaptively-learned queue depth (1, correct for steady-state traffic) was
overwhelmed by same-type coordinators on two daisy-chained devices waking
for their first poll at the identical offset, forcing shed+retry cycles
that cost ~20s per collision — the largest identified remaining
contributor to the multi-minute startup window.**

**The evidence.** Debug logging on a v1.3.9 reload (two inverters,
`HV2220098926` and `HV2220080950`, sharing one `ModbusGuard` endpoint,
`192.168.7.22:502`) showed, right where the config and battery
coordinators' first fetches stalled:

```
ModbusGuard[192.168.7.22:502]: queue full (2/1) — shedding request
HV2220080950_config_data_update_coordinator: request shed by bus guard (queue full (2/1))
HV2220080950_config_data_update_coordinator: Modbus timeout #1
Retry after triggered. Scheduling next update in 10 second(s)
Error fetching HV2220080950_config_data_update_coordinator data: Timeout... no response in 20 s
```

`config_data_update_coordinator`'s first fetch took 19.454s;
`battery_data_update_coordinator`'s took 20.472s — both matching two
shed-and-retry-after-10s rounds exactly.

**Root cause.** `_COORDINATOR_START_DELAYS` (v1.0.3) staggers a *single*
device's four coordinator types (main/power_meter/energy_storage/
configuration) across their first poll — a good scheme for one device, but
applied identically to every device sharing a bus. Two daisy-chained
inverters' `configuration` coordinators both wake at +10s; both
`energy_storage` coordinators both at +14s; and so on — a guaranteed
same-type collision, once per coordinator type, on every single boot and
reload.

**Why the fix is a smarter stagger, not a bigger queue.** `ModbusGuard`'s
actual queue depth isn't a fixed constant — `adaptive_modbus.py` computes
it continuously from a confidence-weighted blend of learned history and a
cold-start default. The observed depth of 1 came from **71 days of real
learned history** on this bus, i.e. depth=1 is the value this project's own
adaptive layer determined is correct for steady-state traffic. Widening it
globally would override that learned value for the other 99.9% of the time
to paper over a ~20s startup-only collision — the same class of mistake
this project's process rules already warn against (a plausible-sounding
global change, unvalidated against the case that actually matters). The
fix instead targets the actual, specific, confirmed mechanism: eliminate
the collision at its source.

**Fix.** `_staggered_start_delay(kind, device_index)` adds
`device_index * _MULTI_DEVICE_STAGGER_STRIDE` (5s) on top of the existing
per-type offset. Device 0 (the primary device) is completely unaffected —
identical timing to before. Device 1 (and any further daisy-chained
device) gets its own, non-overlapping stagger window. `device_index` is
threaded from the existing primary/slave-device setup loop in
`async_setup_entry` through `_setup_device_data` and
`_setup_inverter_device_data`. Scoped entirely to the one-time first-poll
delay (`if not self._first_poll_done`, unchanged) — nothing about
steady-state adaptive behaviour is touched.

**Adversarial verification.** New `tests/test_multi_device_stagger.py`:
behavioural tests confirm device 0's offsets are byte-identical to the
pre-fix values (no behaviour change for the common single-device case),
that every coordinator type gets a distinct offset for device 1 vs device
0, and that this scales cleanly to a third and fourth device. Static (AST)
checks confirm every `start_delay=` call site goes through
`_staggered_start_delay(...)`, not a raw `_COORDINATOR_START_DELAYS[...]`
lookup, and that `_setup_inverter_device_data` accepts `device_index`. Run
against the pre-fix `__init__.py`, both static tests fail correctly; against
this release, both pass.

**Tests: 481 -> 487 passed, 1 skipped**, deterministic across 3 repeated
runs. Only `__init__.py` changed among production files.

### v1.3.9 (2026-08-05)
**Defect H — an unbounded, unhandled write-permission probe on the SENSOR
PLATFORM setup critical path. Deployed after v1.3.8 still showed the
"still starting up" banner lasting ~2 minutes; field log confirmed it was
here, not in anything v1.3.7/v1.3.8 touched.**

**The evidence.** A Home Assistant core log from a v1.3.8 boot showed:

```
04:10:52 WARNING [homeassistant.components.sensor] Setup of sensor platform huawei_solar is taking over 10 seconds.
04:11:29 ERROR [custom_components.huawei_solar] Error fetching ..._config_data_update_coordinator data: Timeout... no response in 20 s
04:11:29 ERROR [custom_components.huawei_solar] Error fetching ..._battery_data_update_coordinator data: Timeout... no response in 21 s
```

The first line is Home Assistant's own watchdog for the *sensor platform's*
`async_setup_entry` (in `sensor.py`) specifically — a different function
from the entry-level `async_setup_entry` (in `__init__.py`) that v1.3.7 and
v1.3.8 both worked on. The two 20-21s timeouts in the same window
independently confirm the device genuinely was not responding quickly at
that point — not a coincidence, corroborating evidence.

**Root cause.** `create_sun2000_entities()` (`sensor.py`), called once per
inverter from the sensor platform's own setup, contained:

```python
and await ucs.device.has_write_permission()
```

— a raw device-level probe (read a test register, write it back) used only
to decide whether to add one optional entity (Active Power Control Mode).
This call:
- Bypasses `ModbusGuard` and the adaptive controller entirely — no pacing,
  no shared backoff intelligence, nothing this project built over the past
  two weeks applies to it.
- Had **no timeout of its own** — bounded only by whatever the vendor
  library's internal per-request timeout happens to allow (up to ~20-30s
  across the read+write pair, matching the timeouts logged for other
  coordinators in the same window).
- Had **no exception handling at the call site** — an exception other than
  the two the vendor library already catches internally
  (`PermissionDeniedError`, `WriteException`) would have propagated up and
  taken down the entire sensor platform setup — every sensor entity on the
  entry, not just this one optional one.

Run once per SUN2000 device, sequentially, on **every single boot and
reload** — a real, material contributor to the 2-3 minute startup window,
independent of and in addition to the two mechanisms v1.3.7 and v1.3.8
already fixed.

**Fix.** New `_has_write_permission_bounded()` wraps the call in
`asyncio.wait_for()` against a new `WRITE_PERMISSION_CHECK_TIMEOUT` (5s —
a healthy device answers in well under a second; there is nothing to gain
by waiting as long as a real data poll would), and catches every exception,
never letting one propagate into platform setup. On timeout or failure the
optional entity is simply skipped for this pass (identical in effect to the
vendor library's own existing "no permission" outcome) and re-attempted
automatically on the next reload.

**A second, smaller thing this fix incidentally corrects:** `sensor.py` had
no `_LOGGER` defined anywhere, despite one existing call site
(`_LOGGER.exception(...)` inside the battery-health fault-isolation
handler, v1.1.7) already using it — a latent `NameError` waiting to fire
the first time that handler's `except` block was ever actually exercised,
silently defeating the very fault-isolation it exists to provide. Adding
`_LOGGER = logging.getLogger(__name__)` for this release's own use also
fixes that latent bug as a side effect. Flagged explicitly rather than
buried, per project convention.

**Audited, not fixed, in this release — a related question worth asking
after finding Defect H:** every write-capable entity (`number.py`,
`switch.py`, `select.py`, `button.py`) and every write flow in
`services.py` also calls `device.set(...)` directly, also bypassing
`ModbusGuard`/the adaptive controller, also with no explicit local
exception handling. **This is a structurally different, lower-severity
situation, not the same defect:** these calls run in response to explicit
user actions or service calls, not during entry/platform setup, so Home
Assistant's own service-call machinery isolates a failure to that single
action — it cannot take down other entities or the whole integration the
way Defect H could. The vendor library's own transport-level
`DEFAULT_TIMEOUT` (10s) also already bounds each underlying request, so
these calls are not literally unbounded. Real, smaller gaps remain (worst
case ~20-30s per write with no local timeout of our own; several
multi-step `services.py` sequences — e.g. forcible charge/discharge — could
leave a partially-applied write sequence if a later step in the same
sequence fails; no locally friendly error message on failure). None of
this was part of what was reported or reproduced this session, and fixing
it is deliberately deferred rather than bundled in here — see
`AUDIT_1.3.9.md` §5 for the full audit and the reasoning for not touching
it now.

**Adversarial verification.** New `tests/test_write_permission_bounded.py`:
a fake device that never resolves confirms the wrapper times out instead of
hanging (and a companion test proves the fake actually reproduces the
original hazard when called the old, unwrapped way); a fake device that
raises confirms the exception never propagates; a healthy fake device
confirms the check still works normally. A static check confirms
`create_sun2000_entities` no longer calls `.has_write_permission()`
directly — run against the pre-fix file, it fails at the exact original
line (1188); against this release, it passes.

**Tests: 475 -> 481 passed, 1 skipped**, deterministic across 3 repeated
runs. Only `sensor.py` and `const.py` changed among production files.

### v1.3.8 (2026-08-05)
**Defect G — the config entry's own setup was blocking on real Modbus reads,
almost certainly the cause of Home Assistant's "waiting for Huawei Solar to
start up" banner lasting 2-3 minutes (vs the ~20 s typical of most
integrations) on every boot AND every reload.**

**The report, precisely stated.** Not the learning-gate's ~180 s window
(that one is deliberate and unrelated — see the v1.2.2 changelog entry and
`AdaptiveModbusController`'s learning-gate docstring; it lets polling
continue through Home Assistant's own start-up congestion by design). This
is Home Assistant's *own*, generic "integration is still initialising"
notification — the one shown for every integration, normally for a few
seconds, here for 2-3 minutes — meaning `async_setup_entry()` itself was not
*returning* for that long. Reload calls the same function, so reload paid
the identical cost.

**Root cause — two blocking first-refresh calls on the setup critical
path,** found by walking every `await` in `async_setup_entry` and its
factory helpers:

1. `create_optimizer_update_coordinator()` (`update_coordinator.py`) awaited
   `coordinator.async_config_entry_first_refresh()` directly — a full,
   real read of every optimizer's registers, once per inverter that has
   optimizers, before setup could proceed.
2. The `SynchronizedPowerCoordinator` setup block in `async_setup_entry`
   (`__init__.py`) awaited its own `async_config_entry_first_refresh()`
   directly — a full read of every instantaneous power register across up
   to two inverters plus meter/battery, once per entry.

Both are genuine, first-class Modbus round trips, stacked sequentially
(per-device optimizer reads, then daisy-chain devices in sequence, then the
cross-device sync read) — a natural multi-minute total, and the same total
on every reload since nothing here was cached or skipped.

**Why this connects to the reload/coordinator-dies incident audited in
v1.3.7 (Defect F).** `configuration_update_coordinator` is constructed
*after* the optimizer setup block in the same per-device function. If a
slow setup were ever cancelled part-way through (by Home Assistant or a
supervising timeout) while still working through an earlier device's
optimizer read, whatever hadn't been reached yet — including a later
device's config coordinator — would never be constructed at all. This is
consistent with, and a stronger candidate for, the exact symptom v1.3.7
addressed (§2.2/§2.3 in the 2026-08-04 handoff): one coordinator dead after
reload while its siblings recovered. v1.3.7's fix (the double-unsub
listener bug) remains a real, independently-verified defect and stays
fixed; this release does not retract it, but the two together are a more
complete account of the incident than either alone.

**Fix.** Both calls now schedule the real first refresh as a background
task (`entry.async_create_background_task`, falling back to
`hass.async_create_task` on older cores — the exact pattern already
established for battery-health initialisation in v1.1.7's
`_async_setup_battery_health`) instead of awaiting it inline. Entities fed
by these two coordinators show unavailable until the background refresh
completes, identically to how every other coordinator in this integration
(main data, power meter, battery, configuration) already behaves — this
brings the two exceptions in line with the existing pattern rather than
introducing a new one.

**Trade-off, stated plainly (not hidden).** Both coordinators lose
`ConfigEntryNotReady` propagation on a failed *first* attempt specifically —
a failure there now behaves like any later transient failure (retried on
the coordinator's own schedule) rather than failing the whole entry setup.
Given the primary device connection has already succeeded by the time
either factory runs, a first-attempt failure here is far more likely to be
"this specific read timed out" than "the device is unreachable," so this is
judged an acceptable, and consistent, trade-off — the same one already
accepted for battery-health since v1.1.7.

**Adversarial verification.** New `tests/test_setup_critical_path.py`: an
AST check that walks each function's own body (explicitly *not* descending
into nested function definitions, since the fix's whole point is that the
real `await …first_refresh()` call only exists inside the background-task
wrapper now) looking for a direct, blocking
`await X.async_config_entry_first_refresh()`. Run against the pre-fix
files, both tests **fail**, correctly reporting the exact original line
numbers (1013 in `update_coordinator.py`, 217 in `__init__.py`). Run
against this release, both **pass**.

**What this release does NOT claim.** The causal link to the v1.3.7
incident (above) is offered as the most complete account assembled so far,
not as independently proven beyond the evidence available — no traceback
from the actual incident was captured (a gap already flagged in the
2026-08-04 handoff). See `AUDIT_1.3.8.md` §6 for the same caveat stated in
full, and §9 for the validation this release specifically requires before
treating either incident as closed.

**Tests: 473 -> 475 passed, 1 skipped**, deterministic across 3 repeated
runs. No production file changed other than `update_coordinator.py` and
`__init__.py`.

### v1.3.7 (2026-08-05)
**Defect F — "Unable to remove unknown job listener" on reload; suspected
cause of a reload leaving one coordinator dead until a full restart.**

```
ERROR [homeassistant.core] Unable to remove unknown job listener
  (<Job onetime listen homeassistant_started
   _async_register_learning_gates.<locals>._on_started ...>, None)
```

**Root cause.** `_async_register_learning_gates()` handed the unsub callable
from `hass.bus.async_listen_once()` directly to `entry.async_on_unload()`.
`async_listen_once()` already self-unsubscribes the instant its event
fires — so if the entry unloads *after* the event already fired (the normal
case for `EVENT_HOMEASSISTANT_STOP`, re-armed on every setup and rarely
still pending by a later reload), the same unsub runs a second time against
an already-empty listener slot.

**Why this mattered beyond a log line.** An unhandled exception raised
during a config entry's unload sequence can abort whatever unload/re-setup
work for that entry had not yet completed. This is a plausible, concrete
mechanism for a field incident (2026-08-04 overnight) where one coordinator
(a battery-attached inverter's configuration/number-entity coordinator)
stopped polling entirely after a reload attempt and did not resume — while
every other coordinator on the same entry recovered normally — and only a
full Home Assistant restart brought it back. Confirmed from the field
capture: the affected coordinator produced zero `bus_diagnostics` records
for the full length of a subsequent 9.25 h capture, `t=0` of that capture
landing within minutes of the reload window; the same coordinator was
polling normally, at its normal rate, in the capture immediately preceding
the incident.

**Fix.** `_guarded_once()` tracks whether the wrapped event already fired
and skips the redundant removal in that case, while still cleanly
cancelling a listener that never fired. Applied to both the
`EVENT_HOMEASSISTANT_STARTED` and `EVENT_HOMEASSISTANT_STOP` listeners
inside `_async_register_learning_gates()` — the only two call sites of this
pattern in the codebase.

**Adversarial verification.** New `tests/test_learning_gate_unsub.py`:
- A fake event bus reproducing Home Assistant's real self-unsubscribe-on-fire
  semantics (a second removal after the event fired raises, matching the
  field error exactly) proves the OLD pattern reproduces the defect and the
  NEW guarded pattern does not — both directions checked, not just the fix.
- A static AST check pins that `_async_register_learning_gates` never again
  hands `hass.bus.async_listen_once(...)` directly to
  `entry.async_on_unload(...)` — confirmed to fail against the pre-fix
  `__init__.py` and pass against this release.

**What this release does NOT claim to fix.** The ~3-minute window of
elevated Modbus timing instability immediately after Home Assistant starts
is a separate, by-design characteristic (the learning gate deliberately lets
polling continue through Home Assistant's own start-up congestion — see
`AdaptiveModbusController`'s learning-gate docstring — it only prevents that
congestion from poisoning the adaptive model). This release does not
shorten or remove that window. The connection to this fix is operational:
this week's instability forced repeated full restarts (each one re-entering
that 3-minute window) because reload was not reliable; if this fix holds,
reload becomes viable again and full restarts — and the congestion window
that comes with each one — should become rare rather than routine.

**Recommended deployment procedure (per project convention: one change at a
time, clean install, don't assume recovery):**
1. Delete `/config/custom_components/huawei_solar` entirely.
2. Extract v1.3.7 fresh.
3. Restart Home Assistant once (clean baseline — do not reload-test on top
   of the outage-recovery state).
4. **Validation this release specifically requires:** trigger a plain
   reload of the config entry (not a restart) and confirm (a) the
   previously-affected coordinator's entities update, and (b) the "unknown
   job listener" error does not appear in the log. This is the actual test
   of whether Defect F was the mechanism — a clean boot alone does not
   exercise the reload path this fix targets.

**Tests: 469 -> 473 passed, 1 skipped**, deterministic across 3 repeated
runs. No other production file changed.

### v1.3.6 (2026-08-05)
**HOTFIX — the shipped v1.3.5 package failed to load. Upgrade immediately by
performing a CLEAN install (delete the integration directory first, do not
extract over it).**

```
ImportError: cannot import name 'CONF_COALESCE_SLOW_TIER' from
'custom_components.huawei_solar.const'
```

**What actually happened.** The v1.3.5 zip that was delivered was verified
byte-for-byte against the working tree before packaging and does **not**
reference `CONF_COALESCE_SLOW_TIER` anywhere — confirmed again here by
diffing the archived zip's `__init__.py` directly. The error's own shape
confirms this: it describes `__init__.py` importing a name that `const.py`
no longer defines — i.e. a `__init__.py` from **before** the v1.3.5 removal,
paired with a `const.py` from **after** it. That combination cannot occur
from a clean extraction of one consistent zip; it is the signature of an
install where `__init__.py` was not actually overwritten (a stale file left
behind from v1.3.4, or a cached compiled version), while other files were.

**This does not change the recommendation:** delete
`/config/custom_components/huawei_solar` entirely, then extract v1.3.6 fresh.

**What this release actually fixes — a real, separate gap.** Investigating
the report exposed that `__init__.py` — the actual Home Assistant entry
point, and the one file whose failure takes down the entire integration —
was **never covered** by any test in this suite. `test_module_imports.py`
(added in v1.2.4 for exactly this class of defect) imports every production
module for real except this one, because `__init__.py`'s transitive
dependency graph (HA's config-entry, service-registration and entity-platform
machinery, plus the full `huawei_solar` device hierarchy) is too large to
stub cheaply. A full runtime import was attempted here and succeeded once
that graph was fully stubbed — but the stub surface required was large,
single-purpose, and orthogonal to the actual defect class, so it was not kept.

**Fix — a materially better test than a full import.** New
`TestConstImportsAreDefined` in `test_module_imports.py` performs an AST
cross-check: for every `.py` file in the package, every name it imports
`from .const import ...` must actually be defined in `const.py`. This is
dependency-free, runs in milliseconds, and — critically — catches **any**
future instance of this defect class in **any** file, not just a scripted
reproduction of this one incident. Adversarially verified: the exact reported
failure was reproduced by temporarily reintroducing the stale import into a
working copy of `__init__.py`, confirmed to fail the new test, then reverted
— rather than merely asserted to work.

**Also pinned:** `test_the_specific_v1_3_5_incident_names_stay_gone` — the
four retired v1.3.4 option names must not appear in `const.py` or in the text
of any production file, closing the loop on the specific incident as well as
the general class.

**Tests: 467 -> 469 passed, 1 skipped**, deterministic across repeated runs.

**Process note.** This is the second time in the 1.3.x line that a real
deployment failure traced back to a file outside the coverage of
`test_module_imports.py` (v1.3.2's truncated-class bug was inside a covered
file but a different failure shape; this one was in an uncovered file
entirely). `__init__.py` remains too expensive to import fully in this
suite's fast, dependency-light test style — the AST cross-check adopted here
is offered as the general pattern for closing this kind of gap without that
cost: structural properties that are true of correct code can often be
checked directly, without needing the code to actually run.

### v1.3.5 (2026-08-04)
**Address-aware Modbus chunking — replaces the tier-based cost model
entirely.** v1.3.4's SLOW-tier coalescing was enabled in the field and caused
every battery entity to go unavailable within hours (see AUDIT_1.3.5.md for
the full incident record). Recovery data — a 29,000-request capture spanning
4 days, taken with coalescing off — overturned the model both v1.3.3 and
v1.3.4 were built on.

**The finding.** Whether a request costs ~7-60 ms or ~2,900+ ms was believed
to depend on register TIER (FAST/NORMAL vs SLOW/STATIC). It doesn't. The real
driver, confirmed against the actual Huawei register address map
(`huawei_solar` 3.0.5, `MAX_BATCHED_REGISTERS_GAP=16`,
`MAX_BATCHED_REGISTERS_COUNT=64`): `batch_update()` silently splits a request
into multiple sequential physical Modbus exchanges whenever the registers it
is given span more than 64 addresses or contain a gap of 16 or more — and
each additional physical exchange costs roughly one further ~2,900-3,000 ms
fixed toll, **regardless of tier**. A representative main-inverter register
set (`input_power` .. `internal_temperature`, real addresses 32064-32087)
forms one contiguous 9-register block corroborating the field's own
directly-measured regs=7-vs-8 threshold. Tier correlated with cost only
because this integration's SLOW/STATIC registers happen to be large and
address-scattered — a confound, not a cause.

**This explains the whole incident.** Coalescing deliberately gathered a
coordinator's entire SLOW/STATIC cohort into one request, which **guarantees
maximum address scatter** (alarms, device status, daily/lifetime counters are
scattered across unrelated functional blocks) — forcing the most possible
internal sub-exchanges per poll. It also plausibly explains the transaction-ID
desync symptom investigated earlier (`modbus_failure.md`): our adaptive gap
is enforced between calls to `guard.request()`, but NOT between the vendor
library's internal sub-splits — by the time it decides to split, our pacing
can no longer apply between the pieces.

**Fix — address-aware chunking.** `_address_group()` reproduces the vendor
library's own grouping rule in our own code, applied to ALL stale registers
before `batch_update()` is ever called. Each address-contiguous group becomes
its own, separately-paced `guard.request()`. This closes the pacing gap
above, and — unlike tier-based splitting — never fragments a group that could
have shared one physical exchange: field evidence shows 744 requests were
forced into their own small exchange purely by the old tier split, even
though they were small enough to potentially share a physical read with a
same-poll cheap group under the real address rule.

**Removed outright, not merely disabled:** SLOW-tier coalescing and
night-deferral (`register_cache.py`, `const.py`, `config_flow.py`,
`__init__.py`, `adaptive_modbus.py` sensors, UI strings). Both were built on
the disproven tier-cost model; re-adding either requires fresh evidence, not
a config flag flip. `_split_by_cost()` is gone; `_modbus_address()` is
replaced by `_modbus_span()`, a direct lookup against the library's own
`REGISTERS` table (previously a best-effort reflection walk over several
guessed attribute paths that never resolved register *length* at all).

**Kept:** SLOW-tier TTL raised 300 s -> 900 s (v1.3.3) — a caching decision
(how often slow-changing data needs refreshing) that is orthogonal to the
per-request cost model and remains valid regardless of which model is
correct. `_chunk_tier()`'s slowest-tier-plus-composition label (v1.3.3 fix)
is retained as a diagnostic field only — informative, no longer load-bearing
for chunking decisions.

**Tests: 467 total** (rewrote `test_tier_separation.py` in place — same
filename, entirely new content, since "tier separation" is the retired
concept). New coverage: the grouping algorithm against a synthetic address
table (empty input, single register, tight packing, gap-at-boundary,
span-over-limit, the exact scattered shape that caused the incident, no
register lost or duplicated); validation against the REAL installed
`huawei_solar` register table (skipped, not failed, if unavailable);
`_modbus_span` robustness on garbage input; and explicit regression guards
asserting coalescing/night-deferral cannot silently return.

**Process note — a genuine off-by-one, caught before shipping.** Early
analysis (and this changelog's first draft) described the representative
register block as "exactly 8 registers." Re-verification found it is 9. Every
claim above was corrected to match, including the test assertions — an
example of exactly the kind of imprecision this project has been bitten by
before, caught this time by writing the test against the real address table
rather than trusting the recollected number.

**Cross-test infrastructure fix.** The new real-register-map test initially
used `try: import huawei_solar except ImportError: <stub>` to prefer the real
library, which failed intermittently depending on pytest collection order —
other test files in this suite install incomplete `huawei_solar` stubs, and
this file (sorting late alphabetically) often collected after one was already
cached. Fixed with an explicit save/purge/import-real/restore pattern scoped
to `setUpClass`/`tearDownClass`, so the real library is used when validating
and every other test file's stub is left untouched.

**Deployment note.** This is offline, validated work — not yet deployed. The
operator's live system remains on v1.2.4 with v1.3.4's coalescing left off,
which is confirmed stable. Recommended validation before deployment: enable
`Modbus diagnostic capture`, run a full day/night cycle, and confirm
`last_chunk_count` drops and `prio` labels show real variation (a regression
back to uniform `FAST` would indicate the fix did not take effect).

### v1.3.4 (2026-07-29)
**SLOW-tier coalescing.** Deeper analysis of the same 3,400-record capture
found the dominant remaining cost — and showed that v1.3.3's TTL change alone
would deliver far less than its changelog claimed.

**The measurement that changed the picture.** Expensive reads (regs >= 9,
excluding the FAST-only power_meter coordinator):

| coordinator | expensive reads/h | interval |
|---|---|---|
| `data_update_coordinator` | **37.9** | every **1.6 min** |
| `battery_data_update_coordinator` | 18.3 | every 3.3 min |
| `config_data_update_coordinator` | 5.9 | every 10.2 min |

A 300 s SLOW TTL should permit at most ~12/h. `data` was doing **38/h**.

**Why:** TTLs are timestamped per register. A coordinator's ~26 SLOW registers
were last read at ~26 different moments, so they expire at ~26 different
moments, and nearly every 30 s poll drags one or two newly-due registers in —
paying the full ~2.9 s fixed entry cost each time.

**Correcting v1.3.3's claim:** raising the TTL to 900 s cuts each register's
rate 3x but NOT the number of distinct expiry moments — 26 registers at 900 s
still expire ~1.7x/minute. The "~3x fewer expensive exchanges" stated in the
v1.3.3 changelog was wrong.

**(1) Coalescing.** When any expensive register comes due, the WHOLE
SLOW/STATIC cohort for that cache is refreshed in the same exchange, giving
them a shared expiry. A continuous dribble of ~13-register reads becomes one
larger read per TTL period:

```
today     : 37.9/h x (2.9 + 13 x 0.377) s ~ 296 s/h   (8.2% of wall clock)
coalesced :  4.0/h x (2.9 + 26 x 0.377) s ~  51 s/h   (~6x less)
```

Same insight that makes splitting expensive reads a pessimisation, applied in
reverse: pay the entry toll as rarely as possible, carry as much as possible
each time. Cheap (FAST/NORMAL) registers are deliberately NOT pulled in — that
would undo v1.3.3's tier separation. Default ON, disableable via options.

**(2) Queueing instrumentation.** The full capture shows knock-on blocking is
worse than an earlier 900-record sample suggested: **210 of 467** long
exchanges had another request waiting, and **291 requests waited >1 s for a
total of 1,362 s**. New `bus_requests_waited` and `bus_total_wait_s` sensors
make v1.3.3's tier separation measurable rather than assumed.

**(3) Optional night deferral — DEFAULT OFF.** Holds non-urgent expensive
refreshes until night mode, bounded at 3x TTL so deferral can never become
starvation. Off by default because the capture spans 04:00-15:00 UTC only:
there is no evidence yet that expensive reads are cheaper at night.

*A conflict found while building it:* night mode multiplies every non-FAST TTL
by 10, so "defer to night" would have deferred reads into a window where they
were not due either — the feature would only have added delay. When
night-preference is active and night mode is on, the expensive tier is now
judged against its BASE TTL. Found by a failing test; the mechanism was fixed
rather than the expectation.

**(4) Errors not actioned.** 18 of 3,400 requests (0.5%) ended in `error`.
Below the threshold worth changing behaviour for; the capture already records
them if that changes.

**New sensors:** `bus_requests_waited`, `bus_total_wait_s`,
`coalesce_events`, `coalesced_registers`.
**New options:** batch slow-register refreshes (on), defer to night (off).

**Also fixed — latent first-flush bug in the diagnostic capture.**
`BusDiagnostics._last_flush` was initialised to `0.0`, which the rate-limit
check read as "flushed at monotonic time 0". On a host whose `time.monotonic()`
was still below `MIN_FLUSH_INTERVAL_S` (a freshly booted machine or container)
the **first** flush was suppressed and records sat in the buffer until 30 s of
uptime had elapsed. Now a `None` sentinel meaning "never flushed".

Found because the capture tests failed intermittently in the full suite while
passing in isolation. Worth recording: in the field this would have shown up as
"the capture file sometimes doesn't appear" — silent, and only sometimes.

**Tests: 455 -> 467 passed, 1 skipped**, deterministic across six consecutive
full-suite runs. Adversarial: 7 of 21 fail against v1.3.3.

**Expected next capture:** expensive reads down from ~38/h toward ~4-8/h on
`data_update_coordinator`, `coalesce_events` climbing, chunk sizes clustering
near the full cohort size instead of spreading 9-27, and `bus_total_wait_s`
growing more slowly than the 1,362 s per 28.8 h baseline.

### v1.3.3 (2026-07-29)
**Tier-aware Modbus reads.** First release driven by the Phase 0 capture —
3,400 requests over 28.8 h on a shared two-inverter bus. First release in this
series that changes *behaviour*.

**The measurement.** Cost is categorical, not marginal:

| chunk contents | service time |
|---|---|
| FAST/NORMAL only | **~6 ms**, independent of size (18 registers: 6.2 ms) |
| contains SLOW/STATIC | **~2,900 ms + 377 ms/register** |

`power_meter_data_update_coordinator` (FAST-tier only) stayed at 6.2 ms through
18-register chunks in the same window that put `battery` chunks of the *same
size* at 6,353 ms. Same device, ~1,000x apart — the driver is **tier, not
count**. 99% of all service time was spent in the 20.7% of requests touching
SLOW-tier content; `data_update_coordinator` alone was 52% of it.

**(1) `_chunk_tier()` reported the WRONG tier.** It returned `min(tiers)` — the
*fastest* tier present — so a chunk of 1 FAST + 26 SLOW registers was labelled
`FAST`. **All 3,400 field records came back `FAST`**, including every 19+
register chunk and a 51.5 s outlier. The field added in v1.3.1 to correlate
stalls with content could not do so. Now reports the **slowest** tier plus
composition, e.g. `SLOW:F1/N2/S24`.

**(2) Tier-separated chunking.** Cheap (FAST/NORMAL) and expensive
(SLOW/STATIC) registers are now read in separate requests, so a routine
power/SOC read is never trapped behind an exchange on the inverter's slow
internal path.

The expensive set is deliberately kept **together**. Because cost is ~2.9 s
fixed + ~377 ms/register, splitting 27 expensive registers into four chunks of
seven costs **~22.2 s against ~13.1 s as one** — each sub-request pays the
entry toll again. A flat `BATCH_CHUNK_SIZE` reduction would therefore make
things *worse*; `test_batch_chunk_size_not_reduced` guards against that being
"optimised" later.

**(3) SLOW-tier refresh interval 300 s -> 900 s.** Tier separation stops
expensive reads *delaying* other traffic but does not reduce their total cost;
only frequency does. These are by their own classification slow-changing
registers — temperatures, alarms, device status, daily and lifetime counters.
900 s is a deliberately moderate ~3x reduction rather than the 1,800 s cap, so
the effect can be measured before going further. Tunable via the options flow
(`Slow-register refresh interval`, 300-3600 s, clamped in code).

**Tests: 443 -> 455 passed, 1 skipped.** New `tests/test_tier_separation.py`
pins the prio fix, the tier split, the "do not fragment the expensive set"
decision, the TTL change and its clamping, and the cost arithmetic itself.
**Adversarial verification: 8 of 12 fail against v1.3.2.**

**Analysis credit:** the tier-not-count diagnosis was the operator's, from
their own capture analysis. The larger 3,400-record set then corrected the cost
model from a pure per-register slope (~685 ms/reg) to fixed-plus-marginal —
which reversed the chunk-splitting recommendation.

**Expected effect:** time-critical reads no longer blocked behind multi-second
exchanges; total expensive-exchange count down ~3x. **Not** expected: lower
per-exchange cost — that is the inverter's firmware and is not ours to fix.

**Open:** the capture covers 04:00-15:00 UTC only. No night data, so it cannot
say whether the expensive-register cost is constant around the clock. If it is,
something *else* drives the day/night failure pattern seen earlier.

### v1.3.2 (2026-07-29)
**HOTFIX — v1.3.1 broke every sensor. Upgrade immediately from v1.3.1.**

```
NotImplementedError: Update method not implemented
  homeassistant/helpers/update_coordinator.py:314 in _async_update_data
```

**Root cause.** The `_chunk_tier()` helper added in v1.3.1 was inserted as a
module-level `def` (column 0) **inside the class body** of
`HuaweiSolarUpdateCoordinator`. In Python that TERMINATES the class: every
method defined after it — including `_async_update_data` — silently became a
module-level function. Home Assistant then reached its base-class stub and
raised on every entity update.

`ast.parse` accepted the file. All 440 tests passed. **No test instantiates a
coordinator** — a gap recorded as open in AUDIT_1.2.4 §6.1, which then caused
the very next outage.

**Fix.** `_chunk_tier()` moved to true module level, before the class. Verified
structurally: `HuaweiSolarUpdateCoordinator` again has 14 methods and
`HuaweiSolarOptimizerUpdateCoordinator` 5, both including
`_async_update_data`.

Credit to the operator, who diagnosed and patched this independently; the fix
here is the same relocation, applied cleanly.

**New tests — `TestCoordinatorClassIntegrity`:**
- required methods are actually *inside* their class (the direct check);
- classes are not suspiciously small (catches a truncated body generally);
- **no method in ANY module of the package sits at `col_offset` 0 relative to
  its class** — this pins the exact failure mode across the whole codebase, not
  just the file that broke.

Structural (AST) rather than runtime, so it needs no HA environment and cannot
rot.

**Adversarial verification:** run against the broken v1.3.1 tree,
`test_required_methods_are_inside_their_class` fails.

**Tests: 440 -> 443 passed, 1 skipped.**

**Process note.** This is the third defect of the same family: tests asserting
*shape* (source strings, `ast.parse`, imports) rather than *behaviour*. Import
tests were added in v1.2.4 and were not enough — a module can import perfectly
while a class inside it has been silently truncated. Coordinator
*instantiation* tests remain the outstanding gap.

### v1.3.1 (2026-07-28)
**Phase 0 instrumentation fixes.** Both defects were found by reading the
FIRST real capture file — neither was visible in testing.

**Defect 1 — serial numbers leaked into the capture (confidentiality).**
Coordinator names are built as
``f"{device.serial_number}_..._update_coordinator"``, and the guard was passed
``coordinator.name`` verbatim. Every record therefore contained a real serial,
despite this module pseudonymising the endpoint and AUDIT_1.3.0 §4 asserting
no serials were present. The existing test only checked that the *endpoint*
was absent, so the leak shipped.

- `sanitise_label()` now pseudonymises any underscore-separated token
  containing a run of 6+ digits, applied inside `record()` so every future
  caller is covered by default rather than having to know to be careful.
- Two inverters remain distinguishable (`dev8c0f_` vs `devdc46_`) and the
  useful part of the label survives.
- **Note on the first attempt:** an anchored regex (`\b...\b`) silently
  matched nothing, because "_" is a word character so `\b` never fires at the
  digit/underscore boundary. Pinned by
  `test_serial_survives_no_word_boundary`.

**Defect 2 — `regs` and `prio` were always null.** The fields existed in the
record schema but nothing populated them, so a stall could not be correlated
with *what* was being read — precisely the next question after the wait/service
split. The request context now exposes `registers` and `priority_tier`, set by
the coordinator per chunk. `register_cache.classify_register()` is a new public
wrapper so the tier can be read without reaching into a private helper.

**Findings from the first capture (400 records, 2.5 h)** — these change the
plan and are recorded in full in AUDIT_1.3.1.md:

| | median | p90 | p99 | max |
|---|---|---|---|---|
| wait_ms | **0.0** | 0.1 | 8,500 | 10,405 |
| service_ms | **61.7** | 9,780 | 27,291 | 32,786 |

Service time is **90%** of all elapsed request time and the queue is empty in
371 of 400 records. **Mechanism (b) is confirmed: the device is slow, not our
queueing.** Single exchanges reach 33 seconds.

This also vindicates the `rtt_p95_ms ≈ 12000` figure that v1.2.4's audit
dismissed as a relearning transient — it was real. The later conclusion that
per-chunk RTT was "sub-second", inferred from settled-slot gap values, was
wrong.

**Consequence for the roadmap:** Phase 3 (occupancy-based admission control)
is largely unnecessary — its premise was queue build-up, and there is no
queue. Phase 2 (priority ordering) loses most of its value for the same
reason: there is no queue to reorder. The open question is now *which
registers or access patterns trigger multi-second stalls*, which the next
capture can answer now that `regs`/`prio` are populated.

**Tests: 433 -> 440 passed, 1 skipped.** Adversarial verification against the
shipped v1.3.0 tree: **7 of 7** new tests fail.

### v1.3.0 (2026-07-28)
**Bus scheduler rework — Phase 0: instrumentation.** First of a staged series
(see `DESIGN_bus_scheduler.md`). The 1.3.x line is the work-in-progress
rework; **v2.0.0** will mark it complete. Rollback target: **v1.2.4**.

**No behavioural change.** Nothing about polling, adaptation or scheduling is
altered. This release only makes the system measurable.

**Why.** Three days of field data across both inverters established that BOTH
inverters' failure rates track the MASTER's workload (r = +0.935 / +0.909)
while the 5 kW slave's own batch size is flat all day (1.5 → 1.6 vs the
master's 1.5 → 4.3). The slave is ~20% of demand and is not the constraint.
But the *mechanism* remained ambiguous between two possibilities that call for
opposite fixes:

  (a) requests queueing on our shared lock  -> a scheduler fixes it;
  (b) the master's own CPU saturating (it relays the slave's frames on top of
      its battery/meter/PV workload) -> only demand reduction helps.

No sensor available today separates **time waiting for admission** from **time
talking to the device**, which is exactly the split that settles it.

**Added:**
- `ModbusGuard` now measures wait and service time separately, tracks how long
  it holds the line (`occupancy()`), and exposes `wait_service_split()` (p95 of
  each). Requests carry a `label` for attribution. Accounting is correct on
  error paths, verified by test.
- `bus_diagnostics.py` — default-off per-request capture. Bounded ring buffer,
  executor-thread writes (**never disk I/O on the event loop**, which would
  inflate the very service times being measured), hard file cap with rotation,
  and salted-hash pseudonyms so a capture can be shared without exposing the
  installation. Every entry point is exception-guarded: a diagnostics fault
  costs diagnostics and nothing else.
- New `Modbus diagnostic capture` switch, **one per BUS** rather than per
  inverter (the capture is a property of the shared connection). Disabled by
  default in the entity registry, and deliberately NOT restored across
  restarts so a capture can never be silently left running.
- **The sensors v1.2.3 promised but never delivered.** That release added
  `rtt_p95_ms`, `last_chunk_count`, `last_batch_ms` and `shed_count` to
  `_snapshot()` but gave them no sensor definitions, and this module has no
  `extra_state_attributes` anywhere — so they were unreachable, and diagnosing
  the gap/timeout ceiling required back-solving `rtt_p95_ms` from saturation
  thresholds. Now real sensors.
- Bus-level sensors: `bus_occupancy_pct` (the feedforward signal the scheduler
  will eventually pace from — it LEADS the problem, unlike failure rate which
  lags it), plus `bus_wait_p95_ms` and `bus_service_p95_ms`.

**Tests: 419 -> 433 passed, 1 skipped.** New `tests/test_bus_diagnostics.py`
covers default-off behaviour, bounded memory with counted drops, JSONL output,
write-failure containment, pseudonymisation (asserting no endpoint appears in
the file), and the wait/service split under contention, when idle, and when an
exception is raised inside the request context.

**Adversarial verification:** run against v1.2.4 the file fails to collect at
all (the module does not exist); with `bus_diagnostics.py` copied in but the
old guard retained, **5 of 5** wait/service tests fail.

**How to use it.** Enable `Modbus diagnostic capture` for a bounded window,
then read `config/huawei_solar_diagnostics/bus_<tag>.jsonl`. Wait-dominated
records mean queueing; service-dominated records mean the device itself is
slow. That answer determines whether Phase 3 does the heavy lifting or whether
priority and demand shaping already suffice.

### v1.2.4 (2026-07-27)
**HOTFIX — v1.2.3 could not load. Upgrade immediately from v1.2.3.**

v1.2.3 aborted config-entry setup on any installation with existing adaptive
learning data, taking down **every entity in the integration**, not just
adaptive tuning:

    File "adaptive_modbus.py", line 367, in async_load
        raw = await self._store.async_load()
    File "homeassistant/helpers/storage.py", line 622, in _async_migrate_func
        raise NotImplementedError

**Root cause.** v1.2.3 bumped `_STORAGE_VERSION` 1 -> 2 to trigger the RTT
rescale migration. Home Assistant's `Store` calls `_async_migrate_func`
whenever the persisted version is older than the requested one, and the base
implementation raises `NotImplementedError`. No migration callable was
supplied, so `async_load()` raised before returning any data — meaning the
`_migrate_v1_rtt_scale()` routine never even ran. The call also sat OUTSIDE
the existing `try/except` (which wrapped only `_deserialize`), so the
exception propagated through `_setup_inverter_device_data()` and out of
`async_setup_entry()`.

**Fixes:**
- `_STORAGE_VERSION` reverted to **1**. Payload migrations are now driven by
  `_DATA_SCHEMA_VERSION`, stored *inside* our own data dict and therefore
  incapable of tripping HA's migration machinery. An absent marker means
  pre-v1.2.3 data. The RTT rescale behaviour is unchanged.
- **The Store load is now fault-isolated.** Adaptive learning is an optional
  optimisation: losing it costs tuned poll parameters, whereas an exception
  costs the user every entity. A corrupt or version-incompatible store now
  logs and continues with defaults. This isolation existed for the
  battery-health subsystem since v1.1.7 but had never been applied to the
  adaptive controller.

**Also fixed — method ownership across sibling coordinator classes.**
`HuaweiSolarOptimizerUpdateCoordinator` is a SIBLING of
`HuaweiSolarUpdateCoordinator`, not a subclass, so helpers defined on one are
not available on the other:
- v1.2.3 regression: it called `self._record_shed()`, which does not exist
  there. Now handled inline, including the shed/timeout discrimination.
- **Pre-existing latent defect** (present well before v1.2.3): the same class
  called `self._record_failure()` on three error paths without defining it —
  an `AttributeError` masking the real error on every optimizer read failure.
  It now owns a `_record_failure()`.

**Tests: 412 -> 419 passed, 1 skipped.**

New `tests/test_module_imports.py` closes the two gaps that let this ship:
1. **Modules are now actually imported**, not merely parsed or string-matched.
   `test_update_coordinator.py` validates that file by searching its source
   text — it never imports it, constructs a coordinator, or executes a path,
   so an import-time or attribute-level defect passes untouched.
2. **AST-based method-ownership check**: every class must define (or inherit)
   the `_record_*` helpers it calls. These run only on error paths, so a
   missing definition stays invisible until something is already going wrong.

`TestStorageMigrationV1toV2` now pins `_STORAGE_VERSION == 1` with the outage
recorded in the docstring, and `TestStoreLoadFaultIsolation` asserts the load
is guarded.

**Adversarial verification:** the new tests were run against the broken
v1.2.3 tree — **5 fail**, including the ownership check and all three storage
assertions.

**Test-harness fix:** stub installation in `test_module_imports.py` runs in
`setUp`, not at import time. Other test modules install their own
`huawei_solar` stub during collection and collection order is not fixed, so
import-time setup made the file's behaviour order-dependent.

**Process note.** Three defects in this release class (the v1.1.5 confidence
sensor, and both faults here) share one shape: tests asserting the *shape* of
code rather than its *behaviour*. Source-string assertions are useful for
pinning structural invariants but cannot substitute for importing and running
the module.

### v1.2.3 (2026-07-27)
**Adaptive Modbus measurement correctness.** Origin: an operator bug report
backed by two months of adaptive-sensor history. Staged release — Defect C
(multi-inverter shared-bus race) and the §9 optimizations are deliberately
NOT included; see "Deferred" below.

**Defect A — batch total consumed as a per-request RTT.**
`_execute_batch()` summed every chunk's round trip into `total_rtt_ms` and
passed it to `record_request()`, where `_derive_params()` treats it as ONE
Modbus exchange (`gap = rtt x 0.4`, `timeout = rtt / 1000 x 5`). Both
parameters were therefore inflated by the chunk count.

Field evidence (2 months, one inverter, 11,113 aligned samples):

| | value |
|---|---|
| Gap at 500 ms ceiling (time-weighted) | **84%** (87.7% at full confidence) |
| Timeout at 60 s ceiling | **42%** |
| Timeout >= poll interval | **11.3%** |
| "poll at 20 s floor" AND "gap >= 480 ms", at 100% confidence | **1,071 samples** |

Gap saturates at rtt >= 1250 ms and timeout at rtt >= 12000 ms, so the stored
`rtt_p95_ms` exceeded **twelve seconds** for nearly half the window — not a
physically possible single Modbus round trip.

*Note on the original report's figures:* it quoted 28% gap-ceiling and 2.3%
timeout>=poll from event counts. Those understate the problem, because a
pinned value stops emitting state changes. Time-weighting the same data gives
84% and 11.3%.

- `_execute_batch()` now returns `max_chunk_rtt_ms` and tracks `total_batch_ms`
  separately. **MAX, not mean**: `effective_timeout` is applied per chunk, so
  the driving value must cover the slowest chunk, not the average.
- **The observation unit stays one poll.** An earlier proposal to call
  `record_request()` per chunk was rejected: successes would then be counted
  per chunk while failures remained per poll, deflating `failure_rate` by the
  chunk count and making the controller *most* aggressive when the bus is
  sickest. Only the RTT value is rescaled; `n`, `failures`, confidence and the
  0.85/day decay keep their tuned per-poll semantics.
- **Storage v1 -> v2 migration.** `rtt_samples` is FIFO-trimmed, not
  time-windowed, so pre-fix batch-summed values would have dominated the P95
  for weeks and made the fix look ineffective. v1 RTT state is discarded;
  failure/timeout counts, slot occupancy and decay dates are scale-independent
  and are KEPT.

**Defect D (new, found during review) — shed requests recorded as inverter
timeouts.** `ModbusGuard` raised a bare `asyncio.TimeoutError` when shedding,
which reached the learner as `record_request(success=False, timeout=True)`.
Internal contention between our own sub-coordinators was thus taught to the
circadian model as inverter misbehaviour.

This closed a positive feedback loop: shed -> recorded failure -> higher
failure rate -> lower queue depth -> more shedding. **Defect B would have
triggered it**, which is why B could not ship alone.
- New `ModbusQueueShed(asyncio.TimeoutError)`. Subclassing preserves every
  existing `except asyncio.TimeoutError` path (back-off, stale-cache fallback,
  entity availability); only the adaptive bookkeeping differs. Discrimination
  happens *inside* the existing handler so no downstream logic is duplicated.
- Sheds still reach telemetry and still advance the consecutive-failure
  counters; they no longer reach the learner.

**Defect B — queue depth had no cold-start blending.** It was the only one of
the four outputs without it, and `failure_rate` returns 0.0 for an unseen slot
(n < 1), so a zero-observation slot fell through to the MOST permissive value
(3) — the exact case the blending exists to protect.
- Same `confidence x derived + (1 - confidence) x baseline` blend as the other
  three, with `ADAPTIVE_QUEUE_DEPTH_COLD_START = 2`.
- **Baseline 2, not the report's 1.** Queue depth creates no concurrency (the
  guard holds a single lock); it only bounds how many callers may wait. With
  up to five sub-coordinators per inverter on a shared bus, 1 sheds
  aggressively on precisely the unproven slots being protected.

**Instrumentation.** `rtt_p95_ms`, `last_batch_ms`, `last_chunk_count` and
`shed_count` are exposed on the adaptive diagnostic sensor. During this
investigation `rtt_p95_ms` had to be back-solved from saturation thresholds;
the next export can verify the fix directly, and `last_chunk_count` gives the
true inflation factor, which analysis could only bound.

**Tests: 391 -> 412 passed, 1 skipped.** Adversarial verification against
pristine v1.2.2: 9 failures in the two collectable modules, and
`test_adaptive_modbus.py` cannot even collect (the new constant does not
exist there) — a stronger signal than a failure.

Four tests that **encoded the defect** were replaced, not weakened —
`test_returns_total_rtt_ms` asserted `return merged, total_rtt_ms`, pinning
the bug in place. Each new assertion is paired with a **control case**
(`test_batch_scale_rtt_would_saturate_both`) proving the healthy-case
assertion could actually fail.

**Deferred (by agreement):** Defect C (shared-bus last-writer-wins on
`update_gap`/`update_max_queue_depth`) needs both inverters' sensor history to
validate. §9 optimizations follow in v1.3.0, led by 9.4 (unifying the two
independent RTT measurement paths — the structural reason Defect A went
undetected).

**Expected after deployment:** gap should fall toward its 150 ms floor and
timeout toward 15 s during healthy, high-confidence slots. This roughly
triples the request rate on a shared bus, so `slot_failure_rate` is the metric
to watch before Defect C ships.

### v1.2.2 (2026-07-26)
**Learning gate extended to the adaptive Modbus controller.** Raised by the
operator: the Modbus layer also learns, and Home Assistant's own start-up
behaviour is not trustworthy enough to learn from.

**Why this matters more than the battery-health case.** `record_request()`
records `(rtt_ms, success, timeout)` with no notion of CAUSE. An RTT inflated
by HA's event-loop congestion is indistinguishable from a slow inverter, which
violates the module's founding premise that failure patterns reflect INVERTER
state. Three properties make the consequences worse than a wrong dashboard
number:

1. **Blast radius.** Poisoned parameters change real polling behaviour
   (interval 20-180 s, timeout 15-60 s, gap 150-500 ms), degrading the data
   collection everything else depends on - including battery-health segment
   resolution.
2. **A ratchet.** Failures push toward SLOWER polling, which yields fewer
   observations per slot per day, which makes recovery slower still. Descent
   is fast; recovery is slow.
3. **Restarts are not uniformly distributed in time.** Scheduled updates and
   evening maintenance cluster in the same circadian slots, so the same slots
   are poisoned repeatedly - faster than decay removes it.

**The larger exposure is planned maintenance, not restarts.** A Huawei
firmware update leaves the inverter unreachable for ~1 h: at 30 s polling that
is ~120 consecutive failures across four 15-minute slots. Applied to a mature
slot (n~300, ~3% failures) it lifts the failure rate to ~12%, mapping to a
poll interval near 137 s instead of 20-30 s.

**And daily decay does not repair it.** Decay multiplies `n` and `failures` by
the SAME factor, so it lowers confidence but leaves the RATIO intact. Only new
successful observations dilute a poisoned failure rate, and those accrue 4-5x
more slowly precisely because polling has slowed. One maintenance window can
cost weeks of degraded polling, with no visible signal that it happened.
`test_decay_does_not_repair_a_poisoned_failure_rate` pins this.

**Changes:**
- `AdaptiveModbusController` gains the same learning gate as the battery-health
  engine: `learning_enabled`, `mark_recovery()`, `suppress_indefinitely()`,
  `learning_active()`. Blocked observations are counted
  (`suppressed_observations`) and surfaced on the diagnostic sensors.
  Suppression is total rather than down-weighted - a weight would be another
  unvalidated constant, whereas "recorded or not" is directly verifiable.
- **Switch renamed** `Battery health learning` -> **`Adaptive learning`**, and
  it now governs BOTH learners. `unique_id` is deliberately unchanged so
  existing registry entries and automations survive the rename.
- **Gates keyed on Home Assistant's own lifecycle events**
  (`EVENT_HOMEASSISTANT_STARTED` / `EVENT_HOMEASSISTANT_STOP`), not on
  integration setup time: setup routinely completes while HA is still working
  through recorder migration and other integrations, so a window measured from
  setup could expire before the congestion does.
- **Deliberate asymmetry, documented in code:** the battery-health engine also
  settles after a COORDINATOR recovery (stale register values would corrupt
  it); the Modbus controller does NOT, because Modbus timing is precisely what
  it measures - a recovering link is genuine signal, not noise. One control for
  the operator; different automatic reflexes underneath.
- Gate state is persisted, so an inhibit set before maintenance survives the
  restart that maintenance usually involves.

**Tests: 382 -> 391 passed, 1 skipped.** New `TestLearningGate` covers
suppression, settling expiry, indefinite suppression, persistence, and the
firmware-update scenario end to end - including a **control case**
(`test_unguarded_firmware_update_would_have_poisoned_the_slot`) proving the
guard is load-bearing rather than decorative. Adversarial verification: 6 fail
against pristine v1.2.1. Replay against the 6-month field dataset is unchanged
(162 segments, reference 22.59 kWh, BHI 100.4) - this release alters
robustness, not measurement.

**Test-harness change:** `test_entities.py` now loads the REAL `const.py`
rather than a hand-maintained constant stub, which had begun to drift as
`adaptive_modbus` imported more constants.

### v1.2.1 (2026-07-26)
**Maintenance robustness.** Raised by the operator: Huawei firmware updates
take about an hour, four times a year, and the vendor does not document which
registers stay meaningful *during* the cycle. Empirically the hardware is
never harmed and values are correct once the cycle completes - but not every
sensor makes sense mid-flight. Unplanned reboots are more frequent and cannot
be prepared for at all.

**The vulnerability this closed.** A charge-ceiling change is a *destructive*
signal: it restarts the efficiency and balance baseline epochs (v1.2.0,
Finding O). Register 47081 was validated only as 0-100%, so a reboot returning
**0** was accepted as a legitimate setting change - and would have wiped
baselines that take weeks to rebuild, up to four times a year.

- **Ceiling plausibility floor + debounce** (`CeilingMonitor`): readings below
  `ceiling_min_plausible` (20%) are rejected as artefacts - nobody configures
  a ceiling that low - and a change must persist for
  `ceiling_debounce_samples` (3) consecutive polls before it is accepted. A
  genuine setting change persists; a transient does not.
- **Maintenance inhibit** (`switch.<device>_battery_health_learning`, default
  on, persisted): turn it off before planned work. Sensors keep displaying and
  raw values keep updating, but nothing irreversible happens - no segments
  recorded, no baselines captured, no epoch can fire. Writes no registers, and
  registered outside the parameter-configuration gate for that reason.
- **Automatic settling period** (`settling_period_s`, default 300 s): after
  ANY recovery - integration start, coordinator returning from an outage, or a
  lifetime-counter reset - measurement resumes immediately but irreversible
  learning waits. This covers the unplanned reboots a manual switch cannot.

**Thermal-rise baseline now requires a multi-day span.** The operator's first
v1.2.0 deployment produced `thermal_rise_baseline_max: 7.47` against a current
5.09. Analysis of 48 undisturbed rest windows showed pack cooling runs at
roughly **-0.4 C/hour**, so thermal rise carries *hours* of load history:
twenty consecutive samples from one afternoon are that afternoon, not a norm.
A short settling period would NOT have fixed this - the constant is hours, not
minutes - so the fix is `thermal_rise_baseline_min_span_days` (3), the same
lesson as Finding J applied at a shorter timescale. Rise is still *measured*
immediately; only the baseline defers.

**Tests: 368 -> 382 passed, 1 skipped.** New: T28 ceiling validation
(including an end-to-end reboot-glitch scenario asserting baselines survive),
T29 learning inhibit, T30 settling period. Adversarial verification: 15 fail
against pristine v1.2.0. Replay against the 6-month field dataset produces
results identical to v1.2.0 - no measurement regression.

### v1.2.0 (2026-07-26)
**Field-calibrated measurement release.** Every change below was derived from
6 months of real operating data from a production installation, replayed
through the engine offline. The data itself is NOT redistributed (see
"Validation data" below).

**Finding H - SOH capacity was anchored to the wrong number.**
The nameplate (20.7 kWh) did not match measured capacity (~22.75 kWh,
162 segments, spread 0.31). SOH_cap was therefore pinned at the 100% clip and
the first ~10% of any real degradation would have been invisible. Capacity is
now anchored to a MEASURED beginning-of-life reference, auto-captured once
enough segments span enough time, persisted, re-anchorable via a button, and
clipped at 110 rather than 100 so headroom is not hidden.

**Finding J - the operating band shifts the measurement.**
Implied capacity varies ~2% with where in the SOC range a segment sat
(22.98 kWh at midpoint 85-100% vs 23.49 at 50-65%). Usage is seasonal, so
that shift would read as degradation. Segments now record `soc_midpoint` and
the prevailing charge ceiling, and the reference capture requires a minimum
TIME SPAN (not just a segment count) so it averages across conditions - the
first attempt anchored to 21.9 kWh from winter-only segments and left SOH
reading 103.8%.

**Findings L + O - efficiency anchors were the dominant noise source.**
eta is only meaningful between states of EQUAL stored energy; "SOC >= 97"
admitted up to 3 SOC points of mismatch, worth ~4.5% on a window. Measured:
stdev 0.0101 at SOC>=97 vs 0.0018 at SOC>=100 - 5.6x quieter, zero windows
lost. But an absolute gate is unusable in winter (122 consecutive days below
100% in the field), so anchors are now defined relative to the CONFIGURED
end-of-charge SOC in two tiers: tier 1 at a BMS recalibration point, tier 2
matched pairs at the prevailing ceiling (time-capped, flagged). Window
threshold 30 -> 15 kWh: baseline in 24 days instead of 47, and quieter.
Changing the charge ceiling shifts eta systematically (0.9801 at a 93% cap vs
0.9883 at 100% = 6.5 SOH points), so a ceiling change now starts a new
baseline epoch automatically.

**Findings A1 + A2 - balance scoring measured the wrong things.**
A 2.4 C inter-pack spread was present at idle (2.33 C) as much as under >1 kW
charge (2.52 C), so it is not battery-generated heat; it scored a healthy pack
set at ~81/100. Separately, pack voltage has 0.1 V resolution against a
0.05-0.50 V band, making one LSB worth 11 score points - the observed 90.5 /
84.9 / 79.4 "swings" were pure quantisation. Balance is now scored as
deviation from a learned per-installation baseline, with raw dV/dT always
exposed and never re-zeroed. Sampling is gated relative to the charge ceiling
(the absolute SOC>=95 gate was unreachable for 78 consecutive days).

**Finding N - seasonal term availability stepped the composite.**
Capacity is the only term available year-round, so the renormalised composite
would jump at the seasonal boundary with no health change. Sub-scores are now
HELD at their last good value for up to 90 days and reported in `held_terms`.

**Finding F - idle was ending discharge segments.**
Capacity arithmetic is dkWh/dSOC, unaffected by the battery resting. A single
near-zero power reading (15 such blips in 8 days, median 130 s) split a
10-hour discharge into marginal halves. Only genuine charging now ends a
segment; 6 h of continuous rest still does. Measured on real power data:
43 fragmented runs -> 23, of which 8 are clean full-night segments.

**Finding C - segments could start on stale counter values.**
`CounterMonitor` carries the last value forward on a failed read, so a segment
opening on such a tick got a stale energy endpoint. It now exposes `is_stale`
and segments refuse to open on carried-forward values.

**Finding D - forecast age used the wrong origin.**
`first_seen_ts` records when the integration started observing, not battery
age. New `bh_install_date` option; falls back to first-seen and reports which
via `battery_age_source`.

**Finding E - persistence was under-triggered.**
`dirty` was set on only four events, none of which occur before the first
segment closes, so the once-in-a-lifetime efficiency baseline could be lost on
an unclean restart. Now set on baseline capture, balance samples, bridges and
discards (the existing 5-minute debounce still prevents write churn).

**New diagnostics.**
- Independent MIN-temperature-sensor spread channel. Field data shows max and
  min channels agree (2.61 vs 2.73 C, same ordering) - which is what proved
  the offset is a real thermal gradient, not miscalibration. Divergence
  between channels therefore indicates a SENSOR fault.
- Deviations exposed in physical units (V, C) alongside the 0-100 scores.
- OPTIONAL ambient temperature input (`bh_ambient_entity`, configurable so the
  sensor can be replaced): pack rise above ambient measures heat GENERATION,
  which inter-pack spread cannot see when all packs age together. Degrades
  silently when absent or unavailable.

**New entities.** Buttons: recalibrate pack-balance baseline; re-anchor
capacity reference (disabled by default - it redefines what 100% means).
All baseline operations append epochs, never overwrite, and log at WARNING.

**Register set:** +1 (`storage_charging_cutoff_capacity`, 47081), deliberate
and justified in the golden-list test.

**Tests: 337 -> 368 passed, 1 skipped.** New: T21 capacity reference, T22
stale endpoints, T23 install date, T24 sub-score hold, T25 efficiency anchor
tiers, T26 balance diagnostic channels, T27 thermal rise. Balance and gap
tests that asserted superseded designs were replaced, not weakened - the
number of assertions in both areas increased.

**Adversarial verification:** the new suite was run against the pristine
v1.1.8 tree - 80 tests fail. Two real bugs were caught by the new tests during
development: a falsy-zero install timestamp, and the winter-biased reference
capture described under Finding J.

**Validation data.** All findings were derived by replaying a real 6-month
dataset offline. That data is confidential and is NOT included in this
repository. `tests/FIELD_VALIDATION.md` documents which sensors were used, how
the analysis was performed, and the numeric results, so the work is
reproducible by anyone with their own equivalent export.

### v1.1.8 (2026-07-25)
**Design correction: data gaps are bridged, not discarded**

Field-diagnosed from a reporting installation. Capacity and efficiency sat at
`Unknown` indefinitely while balance worked. Attributes told the story:

    segment_count: 0        discarded_segment_count: 11
    efficiency_window_count: 0    balance_sample_count: 20

Balance is a *point-in-time* measurement (one instant at rest) and worked.
Capacity and efficiency are *interval* measurements, and every interval was
being destroyed before it could complete.

**Root cause — a design error in the v1.1.5 spec, not an environmental issue.**
`SegmentTracker.mark_gap()` discarded the in-progress segment on any
coordinator read failure, and `EfficiencyTracker.invalidate_anchor()` dropped
the open efficiency window on the same trigger. The stated justification ("we
cannot know what happened during the outage") does not hold: SOC is an
*absolute* state reading and `storage_total_discharge` is a *cumulative*
counter, so ΔSOC and Δenergy across a gap remain exact without the intervening
samples. If something unobserved did occur, the implied-capacity plausibility
band already rejects the segment on close — that guard exists for exactly this
case, and the discard rule was redundant over-engineering on top of it.

The consequence was structural, not marginal: on a link with intermittent
Modbus timeouts (~1 per 25 min in the field), a slow overnight discharge
(~3 SOC points/hour) could accumulate only 1-2 SOC points before being killed,
making the 10-point minimum **mathematically unreachable**. No amount of
waiting would ever have produced a capacity reading.

**Changes:**
- `SegmentTracker.mark_gap()` now marks the gap *pending*; the next good
  sample bridges it and the segment continues. Gaps beyond `max_gap_bridge_s`
  (new config, default 3600 s) still discard — bounded trust rather than
  unbounded stitching.
- New `SegmentTracker.discard_active()` for events that genuinely invalidate
  interval arithmetic. Only a **lifetime-counter reset** uses it (energy of
  unknown magnitude may have flowed before the counter restarted).
- `EfficiencyTracker` anchors survive data gaps; `invalidate_anchor()` is now
  called only on counter reset. On reset the anchor restarts from the
  post-reset sample in the same tick, so no window spans a reset and
  measurement resumes immediately rather than stalling.
- `BatteryHealthEngine.mark_gap()` no longer touches the efficiency tracker.
  The stress accumulator still excludes gap time (it integrates over *time*,
  where an outage genuinely is not a calm period).
- `DischargeSegment.gap_bridged` records bridges per segment;
  `gap_bridged_count` is exposed in the Battery health index attributes and
  persisted. `from_dict` tolerates pre-1.1.8 persisted segments.

**Tests: 326 -> 337 passed, 1 skipped.**
- `TestGapHandling` rewritten. Two tests previously asserted "any gap
  discards" — they encoded the design error and were replaced, not weakened:
  the suite now asserts short gaps are bridged, over-limit gaps still discard,
  a fresh segment starts after an over-limit gap, and counter resets still
  hard-discard.
- `test_field_report_scenario_timeouts_no_longer_prevent_measurement`
  reproduces the reported failure directly: 8 h of slow discharge with a
  timeout every 25 min must now yield one qualifying segment and an accurate
  capacity estimate (20.7 kWh +/- 0.3).
- `TestEfficiencyGapTolerance` (3) and `TestGapBridgingDiagnostics` (3) cover
  baseline capture on a flaky link, reset semantics, attribute exposure,
  persistence round-trip, and pre-1.1.8 backward compatibility.
- **Adversarial verification:** the new tests were run against the pristine
  v1.1.7 tree — **10 fail**, including the field-scenario test. All v1.1.7
  fault-isolation and entity-contract tests (30) still pass unchanged.

**Note on `CounterMonitor` carry-forward:** on a failed read the lifetime
counters carry the last value forward (so EFC/warranty stay populated), so the
segment tracker never sees `None` for them. Only segment endpoints enter the
capacity arithmetic, so a flat counter mid-segment is harmless — covered by
`test_counter_carry_forward_does_not_corrupt_segment_energy`.

### v1.1.7 (2026-07-25)
**Bug fix + fault isolation: `confidence` entity crash, and making the
battery-health subsystem incapable of affecting anything else**

Both issues came from a user's production Home Assistant logs, not from
internal testing. See AUDIT_1.1.7.md for the full writeup.

**Issue 1 — `battery_health_confidence` crashed on every update (real bug).**
- *Root cause:* `HuaweiSolarBatteryHealthSensorEntity` set
  `_attr_suggested_display_precision = 1` as a **class** attribute, so every
  battery-health sensor inherited it — including `confidence`, whose native
  value is the string `"low"`/`"normal"`/`"stale"`. HA's
  `SensorEntity.state` treats any numeric-implying hint (unit, state_class,
  device_class, or a precision hint) as a promise the value is numeric and
  raises `ValueError` otherwise. Production effect: `Error adding entity
  ...battery_health_confidence` at startup, then the same `ValueError` on
  every coordinator tick (`battery_health_manager: listener failed`). The
  manager's per-listener exception guard correctly kept all other entities
  working, which is why only this one sensor showed `Unavailable`.
- *Fix:* `confidence` is declared with `device_class: SensorDeviceClass.ENUM`
  and an explicit `options` list — HA's idiomatic string-valued sensor — and
  the precision hint is applied **per-instance**, skipped for keys in the new
  `_STRING_VALUED_KEYS` frozenset.

**Issue 2 — the subsystem sat on the config-entry setup critical path.**
A user reported a whole-entry setup cancellation
(`Setup of config entry ... cancelled` → `CancelledError` in
`entity_platform` → `... has already been setup!` across all five platforms)
while the Modbus link was timing out. A cancelled platform setup takes down
**all** of the integration's entities. Regardless of the trigger, an additive
read-only feature must not be able to contribute to that at all.
- `async_setup_entry` no longer awaits any battery-health work. Manager
  construction (pure object creation, no I/O) stays inline so the sensor and
  button platforms can resolve `BatteryHealthManager.get`; `async_initialize()`
  (Store load + coordinator listener attach) now runs as a **background task**
  via `entry.async_create_background_task`.
- New `_async_setup_battery_health()` helper contains **every** failure mode:
  manager creation, task scheduling, and the background init coroutine are
  each guarded, and a half-created manager is removed from the registry.
- `sensor.py` and `button.py` wrap battery-health entity creation in
  try/except so it can never abort a platform.
- Entity `async_added_to_hass` and `_on_health_update` are guarded.
- Unload is guarded so a failed state flush cannot block entry teardown.
- **New kill switch:** `bh_enabled` option (default True, exposed in the
  options flow) disables the whole subsystem from the UI without editing
  files.
- **Register set deliberately unchanged** from v1.1.6 (confirmed working on
  the reporter's hardware) and now **pinned by a golden-list test**, so Modbus
  load cannot grow silently.

**Tests: 296 → 326 passed, 1 skipped.**
- `tests/test_battery_health_entities.py` (12 tests, T18) re-implements HA's
  *actual* `SensorEntity.state` validation rule (not a mock) and runs every
  real entity through it for every value it can report — a value-domain test,
  because this bug class manifests for certain *values*, not certain *code
  paths*. Verified load-bearing: reintroducing the v1.1.6 defect made 4 tests
  fail exactly as production did, and reverting made them pass.
- `tests/test_battery_health_isolation.py` (18 tests, T19) enforces the
  isolation contract structurally (AST-based), so it cannot regress in a
  future refactor: no `await` on battery-health work in `async_setup_entry`,
  guarded call sites in every platform file, background-task scheduling,
  kill-switch ordering, the golden register set, and the read-only/no-writes
  guarantee. Verified load-bearing: 13 of 18 fail against pristine v1.1.6.
  The 5 that pass are the golden-register-set and read-only tests — which is
  the evidence that v1.1.7 does **not** change the Modbus footprint.

**Process note:** T18's bug class was invisible to T1–T17 because those tests
exercised the engine thoroughly but never instantiated a real `SensorEntity`
against HA's own validation. T19's bug class was invisible because no test
asserted anything about *where* code runs in the setup lifecycle. Both gaps
are now closed by construction.

### v1.1.6 (2026-07-20)
**Optimization pass over the v1.1.5 battery-health subsystem**

Profiled three runtime costs and fixed all of them; behaviour/formulas
unchanged (all v1.1.5 tests still pass unmodified except where noted):

- **Data quality / Modbus (register_cache.py):** new exact-name
  `_TIER_OVERRIDES` checked first in `_classify()`:
  `storage_total_charge`/`storage_total_discharge` SLOW→**NORMAL** (5-min-stale
  counter endpoints caused up to ±20% error on minimum-size 2 kWh segments;
  addresses 37780–83 are PDU-contiguous with always-read registers ⇒ ≈ zero
  added bus cost) and `storage_rated_capacity` STATIC→**SLOW** (the BMS
  recalibration watch was blind in-session because STATIC is never re-read and
  `invalidate_all()` skips it). Exact-name matching only — all other
  `total_*`/`rated_capacity` registers keep their substring tiers (regression
  test included).
- **CPU (battery_health.py):** per-tick evaluation is now O(1) amortized —
  `SegmentTracker` caches its trimmed-mean aggregation (invalidated on
  append/prune/discard/restore; callers receive isolated attr copies),
  `BalanceTracker` caches its median, `StressAccumulator` keeps running
  Σstress·Δt / ΣΔt totals with a prune fast-path via oldest-bucket tracking
  (totals zeroed when the window empties to stop float drift; recomputed on
  restore). Segment prune has an oldest-first fast path. Benchmark: ~13 µs
  per idle tick with a full 90-day window (60+ segments), ~75 k ticks/s.
- **HA recorder churn (battery_health_manager.py / battery_health.py):**
  `HealthReport.signature()` digests every sensor-facing value (stress index
  quantized to integer steps — the rolling-window mixture otherwise creeps
  ~0.01/tick and defeats change detection); the manager notifies entities only
  when the signature (incl. watched rated capacity) changes. Ten sensors no
  longer write identical states every 30 s. Baseline-reset forces a push.
- **Cleanups:** `CounterMonitor.value` property replaces the `feed(None)` read
  hack; `DischargeSegment.end_ts` is now set by `_close()` from the closing
  sample (engine-side patching removed); redundant double `Result` unwrapping
  in `_build_sample` removed.
- **Tests:** +13 (T15 aggregation-cache invalidation & attr isolation & end_ts,
  T16 stress running-total consistency vs. recompute after feed+prune +
  persistence round-trip + empty-window reset, T17 signature
  stability/segment/confidence transitions; 4 tier-override tests incl.
  exact-name-only regression). Suite: **296 passed, 1 skipped**.

### v1.1.5 (2026-07-20)
**Battery Health Index (BHI) v2 — read-only local battery health estimation**

- **New:** `battery_health.py` — pure computation engine (no HA imports):
  discharge-segment harvesting with ΔSOC²·freshness weighting, SOC-correction
  plausibility guard, Huawei SOH-calibration "golden" anchor boost (4×),
  weighted trimmed-mean aggregation; round-trip efficiency drift (`SOH_eff`,
  replaces invalid voltage-sag resistance under Module+ optimizers); pack
  balance scoring; Q10×f(SOC) stress accumulator (hourly-bucketed, gap-aware);
  √t calendar + throughput aging forecast with measured-vs-model divergence;
  EFC + warranty-throughput bookkeeping; lifetime-counter reset detection;
  versioned to_dict/restore persistence. Composite renormalizes over available
  terms — missing terms never enter as implicit zeros.
- **New:** `battery_health_manager.py` — per-serial singleton (ModbusTelemetry
  registry pattern); subscribes to the energy-storage coordinator with a
  `register_names` context (no extra poll loop); Store persistence
  (`huawei_solar_battery_health_<serial>`, schema v1, debounced ≥5 min);
  read-failure gap propagation; watches `storage_rated_capacity` (37758) for
  post-calibration steps (logged, not yet used). **Writes no registers.**
- **New:** `battery_health_entities.py` — 10 push-based sensors (BHI,
  confidence, 3 SOH sub-scores, stress index*, predicted SOH*, divergence,
  EFC, warranty %; * = disabled by default) + `Reset efficiency baseline`
  button in `button.py` (registered before the parameter-configuration gate —
  it performs no register writes).
- **New:** Options flow (`BatteryHealthOptionsFlowHandler`): rated capacity,
  warranty throughput, composite weights (auto-normalized), window days, min
  segment ΔSOC. Options change triggers an entry reload
  (`_async_options_updated` in `__init__.py`).
- **New:** `BATTERY_HEALTH.md` — full design rationale (Huawei SOH
  calibration registers 37920–37927, Module+ optimizer implications, LFP SOC
  correction), formulas, register table, entities, limitations.
- **New:** `tests/test_battery_health.py` — 40 tests (T1–T14 audit
  traceability), full suite now 283 passed / 1 skipped.
- **Fix (test infra):** modern pytest (≥8) imports the integration root
  `__init__.py` as a Package during collection (repo root has `__init__.py`),
  requiring a full HA runtime. Added scoped `tests/pytest.ini` so rootdir =
  `tests/`; run the suite with `cd tests && pytest .`.
- **Fix (pre-existing):** `test_synchronized_power_coordinator.py` asserted
  pre-fail-safe semantics for `pv_power_total` with a failed INV2 —
  contradicting the documented behaviour (None instead of a silently wrong
  total). Test updated to the documented semantics.
- **Fix (pre-existing):** `test_update_coordinator.py` used
  `asyncio.get_event_loop().run_until_complete()` (order-dependent failure on
  Python 3.12 after async tests close the loop) → `asyncio.run()`.
- **Fix (test infra):** shared `huawei_solar` stub in `test_entities.py` now
  provides `Result` (cross-module stub collision with `test_register_cache`);
  stubs extended with `homeassistant.helpers.storage.Store` and the
  `CONF_BH_*` constants.

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
# From the tests/ directory — no HA environment required.
# (Running from the repo root makes modern pytest import the integration's
#  __init__.py as a Package, which needs a full HA runtime — see tests/pytest.ini.)
pip install pytest pytest-asyncio
cd tests && pytest . -v
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
