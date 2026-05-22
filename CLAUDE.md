# CLAUDE.md — Huawei Solar Integration

> AI-maintained developer reference.  Version tracking: `manifest.json → version`.

---

## 1 · Project overview

Home Assistant custom integration (HACS) for Huawei SUN2000 solar inverters and
LUNA2000 / LG RESU batteries via Modbus TCP or RTU.  Built on the
[`huawei-solar`](https://github.com/wlcrs/huawei-solar) Python library.

---

## 2 · Architecture

```
custom_components/huawei_solar/
├── __init__.py              Entry setup, coordinator wiring, stagger offsets
├── manifest.json            Version, HACS metadata
├── const.py                 All tuneable constants
├── types.py                 Typed runtime-data dataclasses
│
├── modbus_guard.py          asyncio lock + dynamic RTT-derived gap
├── modbus_telemetry.py      Rolling-window stats + 16 HA diagnostic sensors
├── register_cache.py        Tier/adaptive-TTL cache, StaticRegisterCache,
│                            LiveRegisterBus, sort_by_address
├── night_mode.py            PV-power night/day singleton with HA persistence
├── update_coordinator.py    HuaweiSolarUpdateCoordinator (all optimisations)
│
├── sensor/number/select/switch/button.py   HA entity platforms
├── services.py              Service handlers (guarded writes + cache invalidation)
├── config_flow.py           UI configuration
├── diagnostics.py           HA diagnostics dump
└── tests/
    └── test_register_classifier.py
```

---

## 3 · Modbus optimisation layer (cumulative)

### ModbusGuard — dynamic inter-request gap (v2.12.2)

Maintains a rolling median of the last 10 round-trip times (RTTs) and sets:

```
gap = clamp(median_rtt × 1.5,  MIN=50 ms,  MAX=500 ms)
```

On a healthy wired LAN (RTT ≈ 50 ms) the gap shrinks from the 150 ms default
to ~75 ms, cutting the dead-time across 4 coordinators from ~600 ms to ~300 ms.
On a noisy RS485 link it self-heals upward.  RTT samples and the current gap
are reported as HA diagnostic sensors.

`reset_rtt()` is called after connection failures to restart from the
conservative 150 ms default.

### RegisterCache — speculative pre-fetch (v2.12.2)

`filter_stale()` now also includes registers whose remaining TTL is less than
`PREFETCH_LEAD_FRACTION × poll_interval` (default 50 %).  These registers
would be stale on the next cycle anyway; fetching them now during an existing
batch avoids a dedicated round-trip.

### RegisterCache — sort_by_address (v2.12.2)

`sort_by_address(names)` sorts the stale register list by Modbus address before
passing to `batch_update()`.  The underlying library merges contiguous-address
reads into a single frame; sorted input maximises that merging, reducing total
frame count by ~5 %.

### LiveRegisterBus — cross-coordinator deduplication (v2.12.2)

A per-inverter singleton.  After every successful `batch_update()` the
coordinator publishes results here with TTL = one poll interval.  Sibling
coordinators that need the same register in the same poll window consume it
from the bus instead of issuing a second Modbus request.  Typical savings:
5–8 % for registers shared between the main and power-meter coordinators.

Cleared on integration unload (`LiveRegisterBus.clear_registry()`).

### Coordinator phase-staggering (v2.12.2)

Each coordinator is offset from the previous by `COORDINATOR_STAGGER_SECONDS`
(default 7 s) applied as a one-time `asyncio.sleep()` on the very first poll.
With 4 coordinators the last one starts at second 21, well within the 30 s
window.  Eliminates the thundering-herd that previously caused all four to
pile into the ModbusGuard queue simultaneously.

| Coordinator     | Stagger | Fires at (approx) |
|-----------------|---------|-------------------|
| Main inverter   | 0 s     | t + 0 s           |
| Power meter     | 7 s     | t + 7 s           |
| Energy storage  | 14 s    | t + 14 s          |
| Configuration   | 21 s    | t + 21 s          |

### Adaptive poll interval self-tuning (v2.12.2)

After `POLL_AUTOTUNE_HIGH_THRESHOLD` (10) consecutive polls with failures the
interval doubles (up to `POLL_AUTOTUNE_MAX_INTERVAL` = 5 min).  After
`POLL_AUTOTUNE_LOW_THRESHOLD` (60) consecutive healthy polls it halves back
toward the configured minimum (30 s).  Night mode has its own fixed 5 min
interval and is unaffected.  Transitions are logged and reported as
`auto_tune_events` in telemetry.

### TCP keep-alive (v2.12.2, night mode)

SUN2000 closes idle TCP connections after ~30 s.  In night mode (5-min polls)
every poll would require a new TCP handshake, adding 200–500 ms overhead.  A
single-register ping is sent every `KEEP_ALIVE_INTERVAL` (25 s) to keep the
socket open.  Zero extra Modbus data traffic — the register is always STATIC
and already cached.

### NightModeDetector singleton (v2.12.1)

Per-inverter singleton with `register_callback()` broadcast.  All coordinators
(main, power meter, battery, configuration) receive DAY/NIGHT transitions and
adjust their poll interval simultaneously.  State persisted to HA storage so a
night-time restart does not poll at 30 s for 90 s before sleeping.

`"fault"` deliberately excluded from `_NIGHT_STATUS_SUBSTRINGS` — faulted
inverter keeps 30 s polling for timely alarm updates.

### StaticRegisterCache (v2.12.1)

STATIC registers (serial number, firmware version, rated power, …) shared
across all coordinators — read once per session, not once per coordinator.

### RegisterCache — adaptive TTL (v2.12.0)

| Tier   | Base TTL | Cap    | Night cap |
|--------|----------|--------|-----------|
| FAST   | 0 s      | 60 s   | 60 s      |
| NORMAL | 30 s     | 5 min  | 50 min    |
| SLOW   | 5 min    | 30 min | 5 h       |
| STATIC | 60 min   | 24 h   | 24 h      |

TTL doubles each poll where value is unchanged; resets on change.

---

## 4 · Traffic reduction summary

| Optimisation                    | Reduction (day)  | Notes                |
|---------------------------------|------------------|----------------------|
| RegisterCache static tier       | ~25 %            | v2.11.0              |
| Adaptive TTL                    | ~15 % additional | v2.12.0              |
| Night-mode slow-polling         | ~45 %            | v2.12.0 (12 h/night) |
| Shared STATIC cache             | ~3 %             | v2.12.1              |
| Dynamic gap (150→80 ms)         | wall-clock only  | v2.12.2              |
| LiveRegisterBus cross-coord     | ~5–8 %           | v2.12.2              |
| Address sort / frame merging    | ~5 %             | v2.12.2              |
| **Combined day**                | **~48–55 %**     |                      |
| **Combined full day (24 h)**    | **~65–75 %**     |                      |

---

## 5 · Key constants (`const.py`)

| Constant | Default | Description |
|---|---|---|
| `INVERTER_UPDATE_INTERVAL` | 30 s | Main poll rate |
| `UPDATE_TIMEOUT` | **25 s** | Per-request timeout (must be < poll interval) |
| `MAX_CONSECUTIVE_TIMEOUTS` | 3 | Back-off threshold |
| `MODBUS_RETRY_BASE_WAIT` | 10 s | Back-off base |
| `MODBUS_RETRY_MAX_WAIT` | 120 s | Back-off cap |
| `NIGHT_POLL_INTERVAL` | 5 min | Sleep-mode poll rate |
| `COORDINATOR_STAGGER_SECONDS` | 7 s | Phase offset per coordinator |
| `KEEP_ALIVE_INTERVAL` | 25 s | TCP ping in night mode |
| `POLL_AUTOTUNE_HIGH_THRESHOLD` | 10 | Failures before backing off |
| `POLL_AUTOTUNE_LOW_THRESHOLD` | 60 | Healthy polls before recovering |
| `POLL_AUTOTUNE_STEP` | 2.0 | Back-off / recovery multiplier |
| `POLL_AUTOTUNE_MIN_INTERVAL` | 30 s | Auto-tune floor |
| `POLL_AUTOTUNE_MAX_INTERVAL` | 5 min | Auto-tune ceiling |

### ModbusGuard tuning (`modbus_guard.py`)

| Constant | Default | Description |
|---|---|---|
| `MIN_INTER_REQUEST_GAP` | 50 ms | Hard floor for dynamic gap |
| `MAX_INTER_REQUEST_GAP` | 500 ms | Hard ceiling |
| `DEFAULT_INTER_REQUEST_GAP` | 150 ms | Startup / post-failure gap |
| `RTT_GAP_FACTOR` | 1.5 | gap = median_rtt × factor |
| `RTT_WINDOW` | 10 | Rolling RTT sample count |
| `QUEUE_WAIT_TIMEOUT` | 10 s | Max queue wait before abandoning |

---

## 6 · Telemetry sensors (16 total)

Diagnostic sensors under the inverter device (enabled by default unless noted):

| Sensor | Unit | Notes |
|---|---|---|
| Modbus requests / hour | — | |
| Modbus failures / hour | — | |
| Modbus timeouts / hour | — | |
| Modbus cache hits / hour | — | |
| Modbus failure rate | % | |
| Avg Modbus batch size | — | |
| **Modbus median RTT** | ms | New v2.12.2 |
| **Modbus P95 RTT** | ms | New v2.12.2 |
| **Modbus inter-request gap** | ms | Dynamic gap (new v2.12.2) |
| **Modbus poll interval** | s | Current auto-tuned interval |
| **Modbus auto-tune events** | — | Count of interval changes |
| Inverter night mode | — | |
| Modbus total requests | — | Disabled by default |
| Modbus total failures | — | Disabled by default |
| Modbus total cache hits | — | Disabled by default |
| Modbus skipped polls | — | Disabled by default |

---

## 7 · Changelog

### v2.12.3 (2026-05-23) — Bug fixes

**B1 — Dead code: `request_start` variable** (`update_coordinator.py`)
`request_start = time.monotonic()` was assigned but never read; RTT is already
captured by `ModbusGuard._RequestContext.__aexit__`. Removed.

**B2 — Stagger re-fires after every failed first poll** (`update_coordinator.py`)
The stagger guard used `self.last_update_success is None`, which remains `None`
after a failed poll.  A coordinator that fails its first poll would re-sleep for
the full stagger offset on every subsequent poll until one succeeds.  Fixed with
a dedicated `_stagger_fired: bool` flag that is set to `True` on the first
entry, regardless of outcome.

**B3 — Keep-alive acquires guard inside poll path** (`update_coordinator.py`)
`_maybe_keepalive()` was called at the top of `_async_update_data()` and then
acquired `guard.request()` itself.  While asyncio's single-thread model prevents
a true deadlock, a slow inverter response to the ping could delay the main poll
by up to 5 s per cycle.  Fixed by refactoring keep-alive as an independent HA
time-interval task (`async_track_time_interval`) scheduled in `_on_mode_change`
when entering NIGHT mode and cancelled on exit.  The timer runs completely
outside the poll path.  The unsub handle is stored in `_keepalive_unsub` and
cancelled in `async_unload_entry`.

**B4 — `invalidate_cache()` nuked all static registers on every write**
(`update_coordinator.py`)
`invalidate_cache(name)` called `static_cache.invalidate_all()` unconditionally
whenever the static cache was non-empty.  This erased serial number, firmware
version, rated power, and every other STATIC register on any parameter write
(e.g. setting a charge power limit), forcing 4–15 unnecessary re-reads on the
next poll.  Fixed: `invalidate_all()` is now called only when the written
register name matches a `_STATIC_SUBS` substring.

**B9 — `stale_static` O(n) list membership test in hot path**
(`update_coordinator.py`)
`if n in stale_static` was called for every entry in `fresh.items()` where
`stale_static` was a `list`.  Converted `stale_static` to a `set` for O(1)
lookup.  Also fixed the `sort_by_address()` call to pass `list(stale_static)`
since `sort_by_address` expects a `list`.

**B10 — Failed keep-alive ping silenced the next window** (`update_coordinator.py`)
`self._last_keepalive = now` was set *before* `device.get()`, so a failed ping
(timeout, disconnect) still advanced the timestamp and suppressed the next
keep-alive for a full `KEEP_ALIVE_INTERVAL`.  With the B3 refactor the
timestamp is now updated only on success; the next scheduled callback fires
normally after `KEEP_ALIVE_INTERVAL` regardless of the previous outcome.

**B15 — Unused `UnitOfTime` import** (`modbus_telemetry.py`)
`UnitOfTime` was imported from `homeassistant.const` but never referenced.
Removed to silence the linter warning.

### v2.12.2 (2026-05-23) — Performance improvements

**Dynamic ModbusGuard gap**
- `ModbusGuard.record_rtt()` maintains a rolling 10-sample median RTT.
- Gap = `clamp(median × 1.5, 50 ms, 500 ms)`.  Shrinks to ~75 ms on fast links.
- `reset_rtt()` called after any connection failure.
- `current_gap_ms` and `median_rtt_ms` properties for telemetry.
- `ModbusGuard._RequestContext.__aexit__` records RTT on success only.

**Coordinator phase-staggering**
- `HuaweiSolarUpdateCoordinator` accepts `stagger_offset: timedelta`.
- Applied as a one-time `asyncio.sleep()` on the first poll only.
- `__init__.py` assigns 0 / 7 / 14 / 21 s offsets to the four coordinators.

**LiveRegisterBus**
- `LiveRegisterBus` singleton in `register_cache.py`.
- `publish(results, ttl)` after each successful `batch_update()`.
- `query(names)` consulted before issuing a request; hits skip Modbus entirely.
- `evict_expired()` called after each publish to keep memory bounded.
- Cleared in `async_unload_entry` alongside other singletons.

**Address-sorted batches**
- `sort_by_address(names)` in `register_cache.py` sorts by `name.register` /
  `name.address` attribute (falls back to 0 if absent).
- Applied to `stale_names` in `_async_update_data()` before `batch_update()`.

**Speculative pre-fetch**
- `filter_stale()` includes registers with remaining TTL < 50 % of poll interval.
- Smooths burst profile; prevents solo round-trips for near-expiry registers.

**Adaptive poll interval self-tuning**
- `_adapt_poll_interval(had_failure)` in coordinator.
- Backed by `POLL_AUTOTUNE_*` constants in `const.py`.
- Reported via `record_poll_interval()` / `auto_tune_events` in telemetry.

**TCP keep-alive (night mode)**
- `_maybe_keepalive()` called at the top of every `_async_update_data()`.
- Sends a single `device.get()` on a cached STATIC register every 25 s at night.
- Guards with `guard.request()` and a 5 s timeout.

**Telemetry additions**
- `record_rtt()`, `record_gap()`, `record_poll_interval()` methods.
- 5 new sensors: median RTT, P95 RTT, inter-request gap, poll interval,
  auto-tune events.

**Tests**
- `test_register_classifier.py` extended with `sort_by_address` and
  `LiveRegisterBus` test classes (singleton, publish/query, expiry, isolation).

### v2.12.1 (2026-05-22) — Bug fixes
- `_queue_depth` double-decrement on `TimeoutError` in `ModbusGuard`.
- `"active_power"` in `_FAST_SUBSTRINGS` shadowing SLOW registers.
- `UPDATE_TIMEOUT` 35 s > 30 s poll interval; corrected to 25 s.
- `"fault"` in `_NIGHT_STATUS_SUBSTRINGS` suppressing fault polling.
- `services.py` writes bypassed `ModbusGuard`; now guarded.
- Service writes did not invalidate cache; now do.
- `NightModeDetector` promoted to per-inverter singleton with broadcast.
- Night mode state persisted to HA storage.
- `StaticRegisterCache` shared across all coordinators.
- `write_optimistic()` for immediate entity feedback after writes.
- `_evict()` called from every `record_*()` in telemetry.

### v2.12.0 (2026-05-21)
Adaptive TTL tier system, night-mode detection, stale-cache fallback.

### v2.11.0 (2026-05-21)
ModbusGuard (fixed 150 ms gap), RegisterCache (static TTL), telemetry sensors.

### v2.10b (2026-05-20)
Exponential back-off, `retry_after`, tiered logging.

---

## 8 · Developer guide

### Adding a register

1. Add `HuaweiSolarSensorEntityDescription` to the appropriate tuple in `sensor.py`.
2. Add translation string to `strings.json` and `translations/en.json`.
3. If the register is high-frequency real-time data, add to `_FAST_SUBSTRINGS`
   in `register_cache.py` — use a precise, scoped name.
4. Add a `(name, RegisterTier.FAST)` assertion to `test_register_classifier.py`.
5. Run the tests.

### Running checks locally

```bash
# Syntax check all Python files + manifest version
python3 -c "
import ast, json, pathlib, sys
base = pathlib.Path('huawei_solar')
for f in sorted(base.glob('*.py')):
    ast.parse(f.read_text())
data = json.loads((base/'manifest.json').read_text())
print('version:', data['version'])
print('All OK')
"

# Classifier + LiveRegisterBus tests (no pytest needed)
python3 huawei_solar/tests/test_register_classifier.py
```
