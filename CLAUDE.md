# CLAUDE.md — Huawei Solar Integration

> **Maintained by Claude (Anthropic) on behalf of the community.**
> Current version: **1.1.6** — see `manifest.json`.

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
            └── test_battery_health.py                   ← NEW (1.1.5)
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
