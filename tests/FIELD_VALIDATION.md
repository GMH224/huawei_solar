# Field Validation Methodology (v1.2.0)

Every measurement change in v1.2.0 was derived from **real operating data**
replayed through the engine offline, not from synthetic reasoning. The dataset
itself belongs to the reporting operator and is **confidential — it is not
included in this repository and must not be committed to it.**

This document records what was used, how, and what came out, so the analysis is
reproducible by anyone with an equivalent export from their own system.

---

## 1. Why replay at all

Three defects shipped in v1.1.5–v1.1.8 despite a green test suite:

| Defect | Why unit tests missed it |
|---|---|
| `confidence` entity crashed on every update | Tests never instantiated a real `SensorEntity` against HA's own state validation |
| Data gaps destroyed every discharge segment | Tests asserted the *implemented* behaviour, never whether the pipeline could produce output under realistic link quality |
| SOH capacity pinned at the 100% clip | No test compared the nameplate against a measured value |

The common thread: the suite verified that the code did what it was written to
do, never that the resulting system produced a usable measurement under real
conditions. Replay closes that gap.

## 2. Sensors used

Exported from Home Assistant history / long-term statistics.

| Series | Purpose | Coverage in the reference dataset |
|---|---|---|
| `state_of_capacity` | segment detection, anchor gating, SOC-band analysis | 6 months, hourly + 8 days at recorder resolution |
| `charge_discharge_power` | segment start/stop, idle-blip analysis | 6 months hourly; 8 days at ~20 s |
| `total_charge`, `total_discharge` | segment energy, efficiency windows, EFC | 6 months |
| pack 1–3 `voltage` | balance ΔV, quantisation analysis | 6 weeks |
| pack 1–3 `maximum/minimum_temperature` | balance ΔT, independent-channel check | 6 weeks |
| `battery_temperature` (BMS) | stress model, η temperature correlation | 2 months |
| ambient room temperature (external sensor) | thermal-rise analysis | 6 months |

Coverage differs per series because several sensors were only added part-way
through. **This matters:** conclusions drawn from a 6-week summer-only series
are weaker than those from the 6-month series, and are flagged as such in §5.

## 3. Method

1. **Merge** all series onto a common timeline with forward-fill, preserving
   `unknown` / `unavailable` as genuine gaps rather than interpolating them.
2. **Replay** at a fixed tick (5 min for the 6-month set, 30 s for the
   high-resolution week) into the real `BatteryHealthEngine`, calling
   `mark_gap()` wherever the source data shows the coordinator had failed.
3. **Compare engines**: run the same data through the previous release and the
   candidate to quantify what actually changed.
4. **Derive constants from measurement**, not assumption — e.g. sweep the
   efficiency window threshold and anchor tolerance and read off the noise.

The harness is ~150 lines and reads only CSV exports; it is not shipped because
it is coupled to one export format, but §2 plus this method is enough to
rebuild it.

## 4. Results that set constants in this release

| Constant | Value | Evidence |
|---|---|---|
| Capacity anchored to measurement, not nameplate | reference ≈ 22.75 kWh vs 20.7 nameplate | 162 segments, spread 0.31 kWh |
| `capacity_reference_min_span_days` | 45 | anchoring to first-N-only gave 21.9 kWh (winter-biased) → SOH read 103.8% |
| Efficiency anchor tightened | stdev 0.0101 → 0.0018 | 23 windows, 187 days; zero windows lost |
| `eff_min_window_charge_kwh` | 30 → 15 | baseline in 24 days vs 47, and quieter; below 15 kWh noise rises sharply |
| Ceiling-relative anchoring | required | 122 consecutive days below SOC 100%; 78 below SOC 95% |
| New epoch on ceiling change | required | η 0.9801 at a 93% cap vs 0.9883 at 100% = 6.5 SOH points |
| Balance scored vs baseline | required | ΔT 2.33 °C idle vs 2.52 °C at >1 kW — not battery heat; scored healthy packs 81/100 |
| Voltage span widened | required | 0.1 V register resolution; 1 LSB was worth 11 score points |
| Idle no longer closes a segment | required | 15 near-zero blips in 8 days; 43 fragmented runs → 23 |
| Sub-score hold window | 90 days | capacity is the only year-round term |

**Linearity check (important):** implied capacity by segment depth —
23.11 kWh (20–35 SOC points), 23.22 (35–60), 23.36 (60+). Deep winter cycles
spanning nearly the full range agree with shallow summer ones to within 1%, so
extrapolating capacity from partial-range segments is sound.

**Degradation check:** no measurable fade across 6 months at ~82 equivalent
full cycles, as expected for a battery of that age.

## 5. Known weaknesses of this validation

- **Balance and thermal conclusions rest on 6 weeks of summer data** at a
  single (100%) charge ceiling. The winter behaviour of the ceiling-relative
  balance gate is designed from the same principle as the efficiency anchors
  but is **not yet validated against data.**
- **BMS temperature spans only 3.2 °C**, so temperature compensation of η could
  not be fitted (correlation r = +0.18, 2% noise reduction — not useful). A
  wider winter range may change this.
- One dataset, one installation, one hardware generation. Constants derived
  here are defaults, not universal truths — every one of them is exposed as a
  tunable.
- Hourly statistics average away sub-minute behaviour, so the idle-blip
  analysis relies on the shorter high-resolution series.

## 6. Reproducing this

Export the §2 series over as long a window as your recorder and long-term
statistics allow, merge them on a common timeline preserving gaps, and feed
`HealthSample` objects into `BatteryHealthEngine` at a fixed tick. Compare
`report.attributes` between releases. Any constant in `BatteryHealthConfig`
can then be swept the same way the table in §4 was produced.
