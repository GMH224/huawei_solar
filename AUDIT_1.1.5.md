# Release Audit — huawei_solar v1.1.5 (Battery Health Index v2)

**Date:** 2026-07-20 · **Auditor:** Claude (Anthropic) · **Scope:** full
repository at v1.1.5, with emphasis on the new battery-health subsystem
(`battery_health.py`, `battery_health_manager.py`,
`battery_health_entities.py`) and every file it touches (`__init__.py`,
`sensor.py`, `button.py`, `config_flow.py`, `const.py`, `strings.json`,
`translations/en.json`, `manifest.json`, `tests/`).

Methodology loosely follows industrial-control-system review practice
(IEC 62443-flavoured): control-path safety, input validation, fail-safe
behaviour, data integrity, resource bounds, and full test-evidence
traceability. This is a code-level audit in a container without inverter
hardware or a live HA runtime; runtime behaviour on real hardware is verified
to the extent the standalone test harness allows.

---

## 1. Control-path safety (highest-priority property)

**Claim:** the battery-health subsystem is strictly read-only with respect to
the inverter/BMS.

**Verification:** `grep`-level and structural review of all three new modules:
no call to `device.set(...)`, no write-register names, no service
registrations that write. The subsystem consumes coordinator data only. The
new `Reset efficiency baseline` button mutates only local engine state and the
local Store file, which is why it is registered *before* the
`CONF_ENABLE_PARAMETER_CONFIGURATION` gate — that gate exists to guard
register-writing entities, and this button writes none. The options flow
writes only `entry.options`. **PASS.**

**Bus-load impact:** no new poll loop and no new Modbus connection. The
manager subscribes to the *existing* energy-storage coordinator (30 s cadence)
via the same `{"register_names": [...]}` context mechanism entities use; its
~23 registers join the coordinator's batched, cached, rate-limited reads
(ModbusGuard/RegisterCache unchanged). Worst-case addition is a few extra
contiguous registers per storage poll. **PASS.**

## 2. Input validation

- All external values pass `validate_sample()`: per-field plausibility bounds
  (SOC 0–100 %, pack V 10–800 V, temps −20–60 °C, |power| ≤ 15 750 W), with
  **discard-not-clip** semantics; non-numeric and NaN/inf rejected
  (tests T1: 5 tests).
- Implied segment capacity constrained to 8–35 kWh — rejects BMS
  SOC-correction artifacts and counter glitches (T4).
- Efficiency windows constrained to η ∈ [0.50, 1.05] (T8).
- Lifetime counters: decrease > 1 kWh ⇒ reset event with offset carry-forward,
  never negative energy; ≤ 1 kWh jitter tolerated (T7).
- Coordinator `Result` unwrapping tolerates missing registers, enum values,
  and non-numeric payloads (`_value`/`int()` guarded by try/except).
**PASS.**

## 3. Fail-safe behaviour

- Coordinator read failure ⇒ `mark_gap()`: active segment discarded,
  efficiency anchor invalidated, stress Δt for the outage excluded from the
  denominator (an outage must not masquerade as a calm period) (T6, T10).
- Missing critical field mid-segment ⇒ segment discarded, no interpolation
  (T6).
- No computable term ⇒ `BHI = None` ⇒ HA `unknown`, **never 0** (T11).
- Missing sub-terms ⇒ weight renormalization over available terms — a missing
  term cannot crater the composite as an implicit 0 (T11).
- Corrupt/unknown persistence schema ⇒ logged, fresh start (T13); Store load
  exceptions caught so setup cannot be blocked.
- One failing entity listener cannot break other listeners (guarded dispatch).
**PASS.**

## 4. Data integrity & persistence

- Engine state round-trips exactly through `to_dict()`/`restore()`
  (JSON-serializability asserted in T13; SOH_cap identical to 6 decimals after
  restore).
- Schema versioned (`schema_version: 1`); open segments intentionally not
  resumed across restarts.
- Persistence is debounced: writes only on material events (segment closed,
  baseline captured, counter reset), ≥ 5 min apart, `async_delay_save(10 s)`,
  plus a final flush in `async_unload`. Storage growth is bounded: 90-day
  segment window pruned each tick; stress history bucketed hourly
  (≤ ~2 160 buckets); efficiency windows capped (deque maxlen 64); balance
  samples capped (20). **PASS.**

## 5. Lifecycle & concurrency

- Manager follows the established per-serial registry pattern
  (ModbusTelemetry); created in `async_setup_entry`, removed and flushed in
  `async_unload_entry` — a second config entry unloading cannot orphan or
  wipe another entry's manager.
- All engine work runs synchronously inside the coordinator callback on the
  event loop (pure Python, O(window) with small windows — no blocking I/O);
  Store I/O is async/debounced. No new tasks, threads, or locks introduced.
- Options update triggers a clean entry reload via `add_update_listener`
  registered with `entry.async_on_unload`. **PASS.**

## 6. Static checks

- `ast.parse` over all **37** Python files: clean.
- All JSON files (manifest, hacs, icons, strings, 20 translations): valid.
- `manifest.json` version = **1.1.5**.
- New/changed strings mirrored into `translations/en.json` (options flow).

## 7. Test evidence

Full suite (standalone harness, HA stubbed): **283 passed, 1 skipped,
0 failed** — deterministic across repeated runs. The skip is pre-existing and
documented (`test_services.py`: needs a full HA environment).

New coverage: `tests/test_battery_health.py`, **40 tests**, traceability
T1–T14 mapped in the file header, including the original spec §11 vectors
adapted to v2: two-segment capacity vector (20.41 kWh → SOH_cap 98.6),
balance vector (ΔV=0, ΔT=2.7 → 87.9), EFC/warranty vector (3 105 kWh →
150 EFC, 10.8 %), plus composite weighting, renormalization, confidence
transitions, persistence round-trip, forecast/divergence, and every failure
mode listed in §2–§3.

## 8. Findings & resolutions

| # | Sev | Finding | Resolution |
|---|---|---|---|
| 1 | Med (test infra, pre-existing) | Modern pytest (≥ 8) collects the repo root as a Package (root `__init__.py`) and imports it, requiring a full HA runtime — entire suite errored under pytest 8/9 | Scoped `tests/pytest.ini` (rootdir = `tests/`); documented `cd tests && pytest .` in CLAUDE.md §9 |
| 2 | Med (pre-existing) | `test_synchronized_power_coordinator.py` asserted pre-fail-safe semantics (`pv_power_total == 4000` with INV2 failed), contradicting the implementation's documented behaviour | Test updated to documented fail-safe semantics (`None`); implementation unchanged — reporting a silently wrong total would be the actual defect |
| 3 | Low (pre-existing) | `test_update_coordinator.py` used `asyncio.get_event_loop().run_until_complete()` → order-dependent failure on Python 3.12 once earlier async tests close the loop | `asyncio.run()` |
| 4 | Low (test infra) | Cross-module stub collision: `test_entities.py` installed a `huawei_solar` stub without `Result`, breaking `test_register_cache.py` in full-suite order | Shared stub extended (`Result`, `helpers.storage.Store`, `CONF_BH_*`) |
| 5 | Info | First composite tick after fresh install reports `confidence: low` with capacity-only BHI | By design; documented in BATTERY_HEALTH.md §8 |

## 9. Residual risks / known limitations (accepted, documented)

1. SOH_cap inherits BMS SOC error (circularity) — mitigated by freshness
   weighting, plausibility guard, and golden anchors; cannot be eliminated
   from outside the BMS.
2. The aging forecast is a heuristic (literature-typical LFP constants), used
   only to make divergence computable.
3. Only storage unit 1 (≤ 3 packs) is processed.
4. `storage_rated_capacity` (37758) recalibration behaviour is a
   log-and-watch hypothesis, not used in any formula.
5. Register-name strings are validated against `huawei-solar` 3.0.5; a future
   library rename would surface as missing (not wrong) data — sensors go
   `unknown`, confidence goes `stale`.

**Verdict:** release-ready. The battery-health subsystem cannot affect
inverter/BMS operation by construction, fails toward `unknown` rather than
wrong numbers, bounds all memory and disk usage, and ships with reproducible
test evidence.
