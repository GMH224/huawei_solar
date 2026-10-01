# AUDIT — huawei_solar v2.3.2.0

**Status: IMPLEMENTATION COMPLETE.** This is a battery-health release on top
of 2.3.1.0. It changes calculations inside Home Assistant only:

- **No Modbus change.** The polled register set is the same (golden-list
  test unchanged), as are the cadence and the transport.
- **No inverter writes.** The battery-health subsystem still writes nothing.

**Why.** On 01.10.2026 the 2.3.1.0 field acceptance showed a battery-health
capacity reference of 32.8 kWh for a 20.7 kWh (rated) battery. That prompted
a full review of the battery-health logic, a cross-check against the
LiFePO4/graphite literature, and the plan agreed with the operator. The plan
includes an automatic one-time re-learn on upgrade (operator's decision).

**Evidence base:**
- the operator's battery-health store (`.storage/huawei_solar_battery_health_<serial>`,
  schema 3, as of 01.10.2026): 28 unit segments and 30 per pack
  (14.08–30.09), 21 efficiency windows, and the balance buffers;
- HA history exports: BMS and pack temperatures, pack SOC and voltages,
  room temperature (31.08–01.10); unit SOC, power and lifetime counters
  (Jan–Jul 2026);
- the HA log of 28–30.09 (reference capture lines);
- the 2.3.1.0 telemetry capture (`battery_health` section).

The field data is confidential and is **not** shipped. A replay test reads it
only when `HUAWEI_BH_FIELD_STORE` points to the file (§6).

---

## 1. Field acceptance of 2.3.1.0 (context)

Compared with the 49.7 h 2.3.0.2 capture, the 32 h 2.3.1.0 capture showed:

| | 2.3.0.2 | 2.3.1.0 |
|---|---|---|
| Read errors | 226 | 0 |
| Bus time | 438 s/h | 76 s/h |
| Pack-gap reads | 3,634 | 0 |
| Late Transaction-ID replies in the log | 352 | 0 |
| Busy replies | 103 | 0 |
| Sheds | 526 | 19 |

The power sensors went `unknown` only during the restart itself. 2.3.1.0 was
accepted; the operator decided not to record this in `AUDIT_2.3.1.0.md`.

---

## 2. What the store showed (read before §3)

| Finding | Evidence (store / history) |
|---|---|
| Unit capacity inflated | Raw implied capacity of the unit segments: 22.6–25.1 kWh, median ~23.1. Stored `avg_temp_c` 34.5–37.4 °C (BMS board, register 37022). The Gaussian factor at 36.6 °C is 0.71, so the normalised value was ~32 kWh. The reference captured 29.09 was 32.81 kWh |
| BMS ≠ cells | 31.08–01.10, 10-min grid. BMS median 35.8 °C. Pack max sensors 26.7 / 29.0 / 27.8 °C, min sensors 24.6 / 27.0 / 25.9 °C. Room 23.3 °C. BMS − cell mean: +8.0 °C (corr 0.84) |
| Packs fine | Pack references 7.84 / 8.00 / 7.88 kWh. Pack-segment temperatures are 25.7 / 28.1 / 26.9 °C (the packs' own sensors), so the correction was ≤ 3 %. Raw pack medians 7.785 / 7.790 / 7.759 kWh. The headline (worst pack) was therefore sound |
| Shallow segments noisy | Packs: ΔSOC 10–15 has stdev 0.475 kWh (6 %); 30–50 has 0.091 (1.2 %). Unit: 10–15 has 0.918; 30–50 has 0.284 |
| Efficiency baseline thin | The baseline pool was the first 3 windows, `[1.0025, 0.9714, 0.9896]`, i.e. two outliers. η > 1 is impossible. The remaining windows run Aug ≈ 0.989–0.991, Sep ≈ 0.987–0.989 |
| Balance = one rest period | 20 scores = 100. `raw_dv` 0.1 V ×20. `sample_soc` 100 ×20. The thermal-rise samples span **0.51 h**. The baseline was captured 13.08 from 20 consecutive ticks |
| Thermal-rise baseline never set | `baseline_rise: null` 7 weeks after the balance baseline. It is attempted only inside `set_baseline()`, at a moment when the samples spanned < 3 days |
| Forecast | Live stress ratio 3.38. Recomputed from the history with the BMS temperature: 3.33; with the cell temperature: 1.82. This gives predicted SOH 92.2 vs 95.5. Divergence 7.7, of which ~4.4 points come from the sensor choice |
| BHI step 29.09 | 105.8 → 99.5 when the references were first captured (before that, the comparison was to the nameplate, clipped at 110). Not a health change |

---

## 3. Findings and fixes

### 3.1 BH-2320-01 — cell temperature instead of the BMS board temperature (High)

**Defect.** The unit segments (normalisation) and the stress model used
`storage_unit_1_battery_temperature` (37022). On this hardware that register
reads ~8–9 °C above the cells.

**Fix.**
- `BatteryHealthEngine.update()` fills the new `HealthSample.cell_temp_c` with
  the mean over packs of (max+min)/2 (`_mean_pack_cell_temp`).
- Segments and stress use `_effective_temp_c()`: the cell temperature, with
  the BMS reading only as the fallback when no pack temperature exists.
- The attribute `temperature_source` reports `pack_cells` or `bms_fallback`.
- Per-pack trackers already used their own pack sensors.

### 3.2 BH-2320-02 — cold-only capacity correction, no rate correction (High)

**Defect.** `f_T = exp(−(T−25)²/400)` penalised warm cells as much as cold
ones. The rate factor `1/(1+(P/5 kW)²)` doubled capacity at 5 kW.

**What the literature says (sources in §9):**
- LiFePO4/graphite usable capacity is nearly flat from ~20 to ~40 °C and drops
  noticeably only in the cold;
- the rate effect at ≤ 0.25 C is ~1 %.

**Fix.** `cold_capacity_factor()`:

```
f = 1 − min(10 %, 0.5 %/°C × max(0, 15 °C − T))
```

- No correction at or above 15 °C, or without a temperature.
- The rate is ignored.
- The old fields stay in `BatteryHealthConfig` (unused) so that stored
  options and old tests still load.
- `normalized_capacity_kwh()` is evaluated on demand from the stored
  `avg_temp_c`, so the fix applies to every stored segment at once.

### 3.3 BH-2320-03 — forecast from lifetime effective age (Medium)

**Defect.** The calendar loss was computed as `2.5·S̄₉₀·√age`. That projects
the last 90 days' stress onto the battery's whole life, and the forecast
would have *risen* in winter when S̄ falls (control test
`test_old_model_would_rise_in_winter`).

**Fix.** The calendar loss is now `2.5·√(∫S²dt)`, the equivalent-time form of
a √t law whose rate constant scales with S. At constant S this equals the old
expression.
- Time before observation started (install date → first observation) counts
  at the observed mean S².
- That mean is frozen after 7 days of observation. From then on the
  forecast can only fall (`test_forecast_never_rises_after_freeze`).
- Lifetime accumulators are persisted in the stress block.
- New attributes: `effective_age_days` and `effective_age_prior_frozen`.

### 3.4 BH-2320-04 — efficiency: plausibility, outliers, baseline, deadband (Medium)

**Changes:**
- `eff_valid_max` 1.05 → **1.00**.
- A window more than **1.5 %** from the median of the last 10 windows (once
  at least 5 exist) is not recorded.
- The baseline is the median of the first **10** windows, not 3.
- A loss below **0.25 %-points** scores 100.

**Why the deadband.** In the store, η fell ~0.2 %-points from August to
September while the cells cooled. Cell resistance is temperature dependent,
and without winter data that cannot be told apart from ageing. The raw
baseline, the current value and the new `efficiency_loss_pct` stay exposed.

**Recorded for the winter review.** Each window now records its close
timestamp and the cell temperature at the closing anchor. Neither is used in
any score.

**Correction of an earlier statement in this conversation.** The old
3-window baseline (0.98958) was *not* simply a lucky draw. It matches the
August windows; the September windows are lower.

### 3.5 BH-2320-05 — balance: one sample per rest period (Medium)

A sample is now taken once per rest period:
- the pack must have rested for 10 min (`balance_rest_settle_s`);
- the next sample needs a new rest period at least 4 h later
  (`balance_min_sample_spacing_s`).

So the 20-sample baseline and score cover ~2–3 weeks of distinct rests.
- `balance_raw_dv` and `balance_raw_dt` still show the live spread.
- Setting the spacing to ≤ 0 restores per-tick sampling. The pre-2.3.2.0
  tests use that mode (see §6).

### 3.6 BH-2320-06 — minimum segment depth 15 (Low)

- The default is now 15 (was 10), in both the engine and the options form.
- The depth rule is applied at aggregation, so stored shallow segments stop
  counting at once.
- A value saved explicitly in the options still wins (see §8).

### 3.7 BH-2320-07 — thermal-rise baseline retried (Medium)

`maybe_capture_thermal_baseline()` is now called on every learned balance
sample until the baseline is set. It needs the samples to span ≥ 3 days.
Together with §3.5 the span is reachable; before, the buffer spanned
minutes.

### 3.8 BH-2320-08 — ordinary recovery bridges (Medium)

`mark_recovery(..., hard=True)` gained a `hard` flag.
- The manager's "coordinator recovered" path uses `hard=False`: the open unit
  segment gets a pending gap and is bridged (≤ 1 h). This is what v1.1.8
  intended and v2.0.7 (BH-03) overrode.
- A counter reset, re-enabling learning and integration start stay hard.
- The store showed 14 discarded unit segments.

### 3.9 BH-2320-09 — one eligible set, one estimator; re-anchor covers packs (Low)

**Changes:**
- `eligible_segments()` is the single definition of the segments used.
- `reference_candidate()` uses the same weighted trimmed mean as the
  estimate, so a fresh reference reads exactly 100 % (was 99.8 % because the
  reference used a median).
- The button re-anchors the unit **and every pack** (previously the unit
  only), and leaves out calibration segments (previously included).
- A tracker without enough data is left unchanged. On the upgrade path it is
  cleared instead, for automatic re-capture.

### 3.10 BH-2320-10 — calibration latency (Low, side effect of 2.3.1.0)

Since 2.3.1.0 the calibration-status registers are re-read at most hourly
(`SOH_CALIBRATION_MIN_TTL`). The fixes:
- **End of a calibration:** `calibration_settle_s` 300 → 3,900 s.
- **Start of a calibration:** on the rising edge,
  `BatteryHealthEngine._on_calibration_start()` flags unit and pack segments
  that ended within `CALIBRATION_DETECTION_LATENCY_S` (3,600 s) and drops the
  efficiency windows closed in that time.
- A test pins `CALIBRATION_DETECTION_LATENCY_S` to `const.SOH_CALIBRATION_MIN_TTL`.

### 3.11 BH-2320-11 — median-relative outlier exclusion (Low)

A segment more than 25 % from the window median is left out of aggregation:
- it is kept in storage and the exclusion is reversible;
- because the rule is relative to the median, it follows genuine fade
  (`test_uniform_fade_is_never_filtered`).

### 3.12 BH-2320-12 — schema 3 → 4 with a migration (process)

`_migrate_3_to_4` keeps everything except the stress buckets, which were
integrated with the BMS temperature, and sets a flag.
`restore()` then runs `_apply_upgrade_2320()` exactly once:
- capacity references (unit and packs) are re-derived, or cleared where data
  is too thin;
- the efficiency epoch is replayed through the new rules;
- the balance epoch is restarted with empty buffers.

**Safeguards.**
- Every previous value stays in the epoch logs.
- The flag is not written back.
- If the upgrade raises, the restored state is kept unchanged
  (`test_upgrade_failure_keeps_restored_state`).

### 3.13 N3 — cold-charge hours (visibility only)

The `cold_charge_hours` attribute counts time spent charging with cells below
10 °C, where lithium plating is a risk (literature). It is not part of any
score.

---

## 4. Before / after on the operator's store (same `now`)

Produced by the replay script (`report.py`, not shipped) against the real
store:

| Value | 2.3.1.0 | 2.3.2.0 (right after upgrade) |
|---|---|---|
| BHI | 99.5 | 100.0 |
| SOH capacity (worst pack) | 99.8 | 100.0 |
| Pack SOH | 99.8 / 99.8 / 99.9 | 100.0 / 100.0 / 100.0 |
| Pack references (kWh) | 7.837 / 7.997 / 7.876 | 7.788 / 7.764 / 7.759 |
| Unit estimate / reference (kWh) | 32.09 / 32.81 | **23.12 / 23.12** |
| Unit SOH (cross-check) | 97.8 | 100.0 |
| SOH efficiency | 98.13 | 100.0 (loss 0.21 %-pts, inside the deadband) |
| Efficiency baseline | 0.98958 (3 windows) | 0.98934 (10 windows, 1.0025 dropped) |
| SOH balance | 100 | re-learning; last value 100 held |
| Stress ratio | 3.38 | restarts (new 90-day window) |
| Predicted SOH / divergence | 92.1 / 7.7 | 97.4 / 2.6 at first, then provisional; frozen after 7 days |

The predicted SOH after 7 days depends on the real conditions. In a 28-day
simulation on the upgraded store (synthetic cycle with daily full charge,
cells 25 → 18 °C), the value settled at **95.0** and stayed non-increasing.
The cell-temperature estimate from the history (§2) was 95.5.

---

## 5. Load and safety accounting

- **Modbus.** No register added or removed; the golden-list test is
  unchanged. No new reads, no writes.
- **CPU.** ~70–80 µs per coordinator tick in the 28-day simulation with full
  windows.
- **Storage.**
  - New persisted fields: 3 small efficiency lists (≤ 64), 5 stress scalars,
    and 1 balance timestamp.
  - The stress buckets restart (≤ 2,160 entries, as before).
  - Restore of the new fields is type- and range-checked
    (`test_stress_lifetime_persistence`).
- **HA isolation.** `battery_health.py` stays free of Home Assistant imports
  (test). The manager's fault isolation is unchanged.

---

## 6. Testing

- **New file:** `tests/test_ics_2320_fixes.py` — 55 tests plus 1 field-replay
  test.
  - The replay is skipped unless `HUAWEI_BH_FIELD_STORE` is set. It passes
    against the operator's store.
  - Everything runs the real engine.
  - HA-bound wiring (manager, entities, config flow, `const` latency) is
    checked by AST or source inspection.
- **`tests/test_battery_health.py`** — 216 tests; 6 deliberate edits:
  1. **Shared `_cfg()` helper:** sets `balance_min_sample_spacing_s =
     balance_rest_settle_s = 0`, i.e. the pre-2.3.2.0 per-tick balance
     sampling. These tests feed balance tick by tick and test the scoring
     rules, not the cadence. The new cadence is tested in the new file. This
     is the same pattern the helper already used to neutralise the
     normalisation.
  2. **`test_efficiency_drift_lowers_score`:** the expected value includes
     the deadband: (2.0 − 0.25) × 8.
  3. **`test_reference_value_excludes_calibration_tainted_segments`:** keeps
     `calibration_settle_s = 300`.
     - The scenario jumps a whole day between ticks, so the 65-min settle
       would correctly also exclude the first clean segment.
     - The test is about which segments define the reference.
  4. **`test_high_power_segment_normalized_capacity_exceeds_raw`:** replaced
     by `test_high_power_segment_is_not_rate_corrected`, because it asserted
     the removed behaviour.
  5. **`test_adversarial_combined_cold_and_high_rate…`:** keeps the 2× bound
     check. The binding value is now the 10 % cold cap.
  6. **`test_adversarial_combined_floor_hit_is_counted`:** configures a 60 %
     cold cap so the counter can still be exercised; the default never binds.
- **Version pins** moved to 2.3.2.0 in `test_ics_2301_fixes.py`,
  `test_ics_2302_fixes.py`, `test_ics_2310_fixes.py`,
  `test_ics_audit_2201_fixes.py` and `test_tou_period_text.py`.
- **Regression, per file vs 2.3.1.0:** the failing IDs are identical
  (17 pre-existing: environment and collection issues in this sandbox, e.g.
  `homeassistant.const.UnitOfTime` missing). The only addition is the new
  file. Single process: 2.3.1.0 = 12 failed / 1350 passed / 1 skipped /
  11 errors; 2.3.2.0 = 12 / **1405** / 2 / 11.
- **Mutation check:** 32 hand-written mutations of the new logic; **31 are
  caught**.
  - The one that survives (M32: not passing `cell_temp_c` to the per-pack
    sample) is *equivalent*. The pack sample's `battery_temp_c` already
    carries the same pack temperature, which `_effective_temp_c()` falls back
    to.
- **Lint:** pyflakes is clean on all changed modules and the new test.
- **Not tested here:** HA entity instantiation (`test_battery_health_entities.py`
  cannot import the HA stubs in this sandbox, a pre-existing condition). The
  allowlist additions are checked by source inspection.

---

## 7. Acceptance after install

**Day 1:**
- The log shows one WARNING block "2.3.2.0 upgrade applied". It is preceded
  by four "capacity reference set to …" lines: unit ~23.1 kWh, packs
  ~7.76–7.79 kWh.
- On the BHI entity: `estimated_capacity_kwh` ≈ 23.1 and `temperature_source`
  = `pack_cells`.
- `soh_efficiency` = 100 with `efficiency_loss_pct` ≈ 0.2.
- `held_terms` = `["balance"]`.

**Day 7–8:** `effective_age_prior_frozen` = true; the predicted SOH settles,
expected ~95.

**After ~3 weeks:**
- `balance_sample_count` is rising, `balance_baseline_epochs` has
  incremented, and the balance score is live again.
- `thermal_rise_baseline_max` is not null, because an ambient sensor is
  configured.

**Next review:** Feb–Mar 2027 (winter items in BATTERY_HEALTH.md §8.6) and
after Huawei's first calibration (~Dec 2026).

---

## 8. Upgrade notes

- Install as usual and restart Home Assistant. No configuration change is
  needed.
- **One check.** If the battery-health options were ever saved, *Min segment
  ΔSOC* keeps the saved value (the old default was 10). Open Configure →
  battery health and set it to 15 to follow the new default. A deliberate
  10 is respected.
- Downgrade to 2.3.1.0 is not supported for the battery-health store:
  schema 4 is unknown to 2.3.1.0, which would start fresh. Keep a copy of
  `.storage/huawei_solar_battery_health_<serial>` if a downgrade might be
  needed.

---

## 9. Literature cross-check (sources)

**What the literature supports in the design:**
- the √t calendar law with Arrhenius temperature dependence;
- an equivalent-time treatment of varying conditions;
- calendar ageing rising stepwise with SOC;
- moderate cycle fade at low C-rates;
- capacity flat at 20–40 °C, falling in the cold;
- LFP SOC uncertainty from the flat OCV and hysteresis;
- reversible capacity changes from the anode overhang.

**Sources:**
- Naumann et al., *Analysis and modeling of calendar aging of a commercial
  LiFePO4/graphite cell*, J. Energy Storage 2018.
- Naumann et al., *Analysis and modeling of cycle aging of a commercial
  LiFePO4/graphite cell*, J. Power Sources 2020.
- Wang et al., *Cycle-life model for graphite-LiFePO4 cells*, J. Power
  Sources 2011.
- Sarasketa-Zabala et al., *Calendar ageing analysis of a LiFePO4/graphite
  cell with dynamic model validations*.
- Lewerenz et al., *Systematic aging of commercial LiFePO4|Graphite
  cylindrical cells including a theory explaining rise of capacity during
  aging*, J. Power Sources 2017.
- Lewerenz et al., *Irreversible calendar aging and quantification of the
  reversible capacity loss caused by anode overhang*, J. Energy Storage 2018.
- *Capacity Recovery Effect in Commercial LiFePO4/Graphite Cells*,
  J. Electrochem. Soc. 2020.
- KIT 2024, *Towards robust state estimation for LFP batteries*.
- *Arrhenius plots for Li-ion battery ageing as a function of temperature,
  C-rate, and ageing state*, J. Power Sources 2022.
