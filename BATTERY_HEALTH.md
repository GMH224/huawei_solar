# Battery Health Index (BHI) v2

> Added in **v1.1.5**. Read-only battery health estimation for Huawei
> LUNA2000-S1 (and structurally similar) storage systems, computed locally in
> Home Assistant from registers the integration already polls.
>
> **Honest framing:** every value produced here is a *self-referential trend
> proxy* built on BMS-reported data, not a validated laboratory diagnostic.
> Its purpose is to make degradation *visible as a trend* years before it
> matters, and to raise an early warning if the battery ages faster than
> expected. Track the change over time — do not over-interpret the absolute
> number.

---

## 1. Why v2 looks the way it does

The design is grounded in Huawei's own disclosures and the LFP aging
literature. Three findings shaped it:

### Finding 1 — Huawei already runs its own SOH calibration
Per the LUNA2000-S1 user manual, the system calculates SOH (max charge ÷
rated capacity) over **complete charge/discharge sessions** (100% SOC down to
a low-SOC release limit). If natural conditions are never met, an **automatic
check runs one year after the last one** (every 3 months near end of life),
and a **manual check** can be triggered from FusionSolar. The `huawei-solar`
library exposes the per-pack **SOH calibration status registers
(37920–37926)** and the calibration SOC release limit (37927).

Consequences implemented here:
- A discharge segment during which any SOH-calibration status register is
  non-zero is flagged **golden** and gets a **4× weight boost** — it is the
  BMS's own controlled full-cycle measurement, the best data we will ever see.
- Register **37758 (`storage_rated_capacity`, Wh)** is logged and watched: if
  it steps after a calibration event, that is very likely Huawei's own updated
  capacity estimate (unverified hypothesis — logged at WARNING, not yet used
  in any formula).
- Practical tip: triggering a manual health check once a year (winter, when a
  deep discharge is natural anyway) guarantees at least one golden anchor per
  year even if your PV/load pattern never produces natural full cycles.

### Finding 2 — Module+ optimizers invalidate voltage-sag resistance
Each LUNA2000-S1 module contains its own DC/DC energy optimizer. Every
voltage/current you can read sits *behind* active power electronics, so
ΔV/ΔI "sag" measures converter control behaviour, not cell resistance. The
v1 spec's `SOH_res` was therefore **dropped entirely** and replaced with
**round-trip efficiency drift (`SOH_eff`)**: rising internal resistance shows
up as rising I²R losses, i.e. declining discharge/charge energy ratio between
full-charge states — measured through lifetime counters (37780/37782) the
optimizers cannot distort. Efficiency erosion typically precedes visible
capacity fade, making this an *earlier* warning channel too.

### Finding 3 — LFP's flat OCV curve and Huawei's SOC Correction
LFP open-circuit voltage is nearly flat from ~20–90% SOC, so the BMS
coulomb-counts and periodically **snaps** SOC to a known point (S1 manual §
"SOC Correction", typically at full charge where voltage finally rises).
Consequences implemented here:
- **SOC-correction guard:** any segment whose implied capacity falls outside
  a plausibility band (8–35 kWh by default) is discarded — a mid-segment SOC
  snap, not real physics.
- **Freshness weighting:** coulomb-count drift grows with throughput since
  the last full charge, so segment weight includes
  `exp(−throughput_since_full / τ)`, τ = 40 kWh. Segments right after a 100%
  charge (fresh SOC anchor) dominate; three-weeks-into-a-cloudy-stretch
  segments barely count.

### Structural principle — measurement ≠ prediction ≠ bookkeeping
For a lightly cycled, indoor (17–21 °C) home battery, **calendar aging
dominates cycle aging**, driven mainly by high-SOC dwell and temperature and
growing roughly with √t. v1 mixed measured state, stress exposure, and cycle
bookkeeping into one number, which would dip in summer for reasons that are
not degradation. v2 keeps three separate outputs:

| Output | Nature | Entities |
|---|---|---|
| **BHI** | measured health only | `battery_health_index` + 3 SOH sub-sensors |
| **Aging forecast** | heuristic model | `battery_predicted_soh`, `battery_health_divergence`, `battery_stress_index` |
| **Bookkeeping** | counters | `battery_equivalent_full_cycles`, `battery_warranty_throughput_consumed` |

The **divergence** (measured − predicted) is the real early-warning signal:
measured SOH falling faster than the model expects is far more sensitive than
any absolute threshold.

---

## 2. Formulas

### Composite
```
BHI = w_cap·SOH_cap + w_eff·SOH_eff + w_bal·SOH_bal
      (defaults 0.60 / 0.20 / 0.20, auto-normalized,
       renormalized over the AVAILABLE terms — a missing term
       never enters as an implicit 0)
```

### SOH_cap — capacity from harvested discharge segments
A segment starts when `storage_charge_discharge_power < −50 W` and ends on
idle/charging. Qualification: ΔSOC ≥ 10 (configurable), no data gap, no
missing field, implied capacity within [8, 35] kWh.

```
implied_capacity_i = ΔkWh_i / (ΔSOC_i / 100)
weight_i           = ΔSOC_i² × exp(−throughput_since_full/τ) × (4 if golden)
SOH_cap            = clip( trimmed_weighted_mean(implied) / C_rated × 100 )
```
Aggregation over a 90-day rolling window uses a **weighted trimmed mean**
(10% of total weight cut from each tail once ≥5 segments exist) plus a
reported spread — a single glitch segment cannot drag the estimate.

### SOH_eff — round-trip efficiency drift
Anchors are ticks at SOC ≥ 97% and |power| ≤ 100 W. Between successive
anchors with ≥ 30 kWh of charge:
```
η_window  = Δ(lifetime_discharge) / Δ(lifetime_charge)      (valid 0.50–1.05)
baseline  = median of first 3 valid windows   (persisted; button resets it)
current   = median of last 6 valid windows
SOH_eff   = clip(100 − (baseline − current)·100 × 8)
```
The baseline is captured automatically over the first weeks of operation —
**the younger the battery when v1.1.5 is installed, the better the baseline.**

### SOH_bal — pack balance
Sampled at rest (|power| ≤ 50 W) and SOC ≥ 95%, ≥ 2 packs with
`working_status == running` (offline packs are excluded, never compared
against stale readings):
```
score_V = 100 → 0 linearly over ΔV 0.05 → 0.50 V
score_T = 100 → 0 linearly over ΔT 1.0 → 8.0 °C
SOH_bal = median of last 20 samples of (score_V + score_T)/2
```

### Stress ratio & forecast (informational)
```
stress(t) = Q10^((T−25)/10) × f(SOC),  f = 1 → 2.5 linearly above SOC 80
stress_ratio = time-weighted mean over 90 days (hourly buckets;
               outages > 15 min excluded from the denominator)
predicted_SOH = 100 − 2.5·stress_ratio·√age_years − 0.004·EFC
divergence    = SOH_cap − predicted_SOH        (negative = aging faster)
EFC           = lifetime_discharge / C_rated
warranty %    = lifetime_discharge / 28 840 kWh × 100
```
The warranty sensor is a **legal reference** (CH/EEA terms: 28.84 MWh to 60%
retention), *not* "% of real battery life" — real LFP cycle life is typically
far higher.

---

## 3. Registers used (all read-only)

| Register | Address | Use |
|---|---|---|
| `storage_state_of_capacity` | 37760 | SOC (segments, gating, freshness) |
| `storage_charge_discharge_power` | 37765 | +charge/−discharge W |
| `storage_unit_1_battery_temperature` | 37022 | stress model |
| `storage_total_charge` / `_discharge` | 37780/37782 | efficiency, EFC, energy |
| `storage_rated_capacity` | 37758 | logged (recalibration watch) |
| `storage_unit_1_battery_pack_{1..3}_voltage` | 38235/38277/38319 | balance |
| `..._pack_{1..3}_maximum/minimum_temperature` | 38452+ | balance |
| `..._pack_{1..3}_working_status` | 38228+ | pack online gating |
| `..._pack_{1..3}_soh_calibration_status` + unit | 37920–37926 | golden segments |

The subsystem **never writes** a register. It subscribes to the existing
energy-storage coordinator (30 s cadence) with a register-name context, so it
adds those registers to the coordinator's batched reads — no extra poll loop,
no extra Modbus connections.

Since **v1.1.6** the register cache carries exact-name tier overrides for
this subsystem: the lifetime counters read at NORMAL cadence (30 s, matching
the coordinator — SLOW's 5-min staleness distorted segment energy), and
`storage_rated_capacity` reads at SLOW cadence so the recalibration watch
actually sees in-session steps. Also since v1.1.6, sensor entities are
notified only when a sensor-facing value actually changes, so the ten sensors
do not write identical states into the HA recorder every 30 s.

## 4. Entities

| Entity | Default | Notes |
|---|---|---|
| Battery health index | on | composite; rich attributes incl. sub-scores, spread, segment counts |
| Battery health confidence | on | `low` / `normal` / `stale` |
| Battery SOH capacity / efficiency / balance | on (diagnostic) | sub-scores |
| Battery health divergence | on (diagnostic) | measured − predicted; watch for sustained negative |
| Battery equivalent full cycles | on | |
| Battery warranty throughput consumed | on (diagnostic) | legal reference only |
| Battery stress index | **off** | exposure, not health |
| Battery predicted SOH | **off** | heuristic model output |
| Reset efficiency baseline (button) | on | local action, no register writes |

`unknown` states are intentional: with no computable term the sensors report
unknown, never a fake 0 or 100.

## 5. Options (Settings → Integrations → Huawei Solar → Configure)

Rated usable capacity (kWh), warranty throughput (kWh), the three composite
weights (auto-normalized), capacity rolling window (days) and minimum segment
ΔSOC. Changing options reloads the entry; persisted raw segments stay valid —
only the aggregation applied to them changes. All other constants live in
`battery_health.BatteryHealthConfig` with documented defaults.

## 6. Persistence

State is stored via HA's `Store` helper
(`.storage/huawei_solar_battery_health_<serial>`), schema-versioned
(`schema_version: 1`), saved debounced (≥ 5 min apart, plus on unload).
Unknown schema versions start fresh rather than guessing. Restart behaviour:
open segments are never resumed across a restart; the rolling windows are.

## 7. Failure handling (spec §9 heritage)

- Implausible values are **discarded per-field, never clipped**.
- A coordinator read failure discards the active segment and excludes the gap
  from stress time-weighting (outages must not look like calm periods).
- Lifetime-counter decreases > 1 kWh are treated as **reset events** (offset
  carried forward, efficiency window invalidated, WARNING logged) — never as
  negative energy.
- One misbehaving entity listener cannot break the others.

## 8. Known limitations

1. **Circularity:** SOH_cap depends on BMS SOC. Freshness weighting and the
   SOC-correction guard mitigate but cannot remove this. Golden segments are
   the strongest counterweight.
2. **Forecast is heuristic.** The √t + throughput model uses literature-typical
   LFP constants, not fitted cell data. It exists to make *divergence*
   computable, not to predict warranty outcomes.
3. Only **storage unit 1** (up to 3 packs) is currently processed.
4. Options changes require the automatic entry reload to take effect.
5. If PV/load patterns never produce ΔSOC ≥ 10 discharge segments, confidence
   stays `low`/`stale` — by design. Trigger a manual Huawei health check to
   feed the estimator a golden cycle.

## 9. Byproduct: one actionable aging lever

Indoor placement and PV-only 0.2C charging already put this battery near
best-case. The one significant modifiable stressor is **long summer dwell at
100% SOC** — the strongest calendar-aging accelerator for LFP. A seasonal
end-of-charge cutoff of ~90% in summer, with a full charge every 2–4 weeks
(the BMS needs periodic full charges for SOC correction, and Huawei's natural
SOH calculation needs full sessions), meaningfully slows calendar aging. The
integration's existing `end-of-charge SOC` number entity (register 47081) can
automate this; the BHI subsystem itself deliberately stays read-only.
