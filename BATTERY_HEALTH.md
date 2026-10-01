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
>
> **v2.3.2.0** reworked the corrections and the scoring windows after a
> review against field data and the LiFePO4 literature (§2, §7c, §8 and
> `AUDIT_2.3.2.0.md`). In short: cell temperatures instead of the BMS board
> temperature, no warm-side or rate correction, a lifetime effective age for
> the forecast, a 10-window efficiency baseline with a seasonal deadband, one
> balance sample per rest period, and a thermal-rise baseline that actually
> gets set.

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
  non-zero is **excluded** from the capacity estimate (since v2.0.6; the
  original 4× "golden" boost was dropped because the BMS re-scales SOC during
  its own calibration, which corrupts ΔkWh/ΔSOC). Efficiency anchors and
  balance samples are not taken during a calibration either. Since v2.3.2.0
  the calibration-status registers are read at most hourly, so the settle
  window after a calibration is 65 min and anything closed in the hour before
  a calibration is noticed is excluded retroactively.
- Register **37758 (`storage_rated_capacity`, Wh)** is logged and watched: if
  it steps after a calibration event, that is very likely Huawei's own updated
  capacity estimate (unverified hypothesis — logged at WARNING, not yet used
  in any formula).
- Practical tip: Huawei's own check (manual, or automatic one year after the
  last one) keeps the BMS SOC scale honest; this integration does not use the
  calibration cycle itself.

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
charging (rest does not end it; 6 h of rest does). Qualification: implied
capacity within [8, 35] kWh (unit; packs scaled by pack count). The same
segment logic runs once for the whole unit and once per pack, on each pack's
own SOC and lifetime counters.

```
implied_capacity_i   = ΔkWh_i / (ΔSOC_i / 100)
normalized_i         = implied_i / f_cold(T_i)          (v2.3.2.0)
f_cold(T)            = 1 − min(10, 0.5 · max(0, 15 − T)) / 100
T_i                  = mean CELL temperature during the segment
                       (pack max/min sensors; BMS board only as fallback)
weight_i             = ΔSOC_i² × exp(−throughput_since_full / 40 kWh)
eligible             = not calibration, ΔSOC ≥ 15 (option), and within ±25 %
                       of the window median
estimate             = weighted trimmed mean(normalized) over eligible
                       (10 % of weight trimmed from each tail, ≥ 5 segments)
SOH_cap (per pack)   = clip(estimate / reference × 100, 0, 110)
SOH_cap (reported)   = the WEAKEST pack's value; the unit-level value is an
                       independent cross-check (attribute)
```

* **No warm-side and no rate correction (v2.3.2.0).** LiFePO4 usable capacity
  is nearly flat from ~20 to ~40 °C and drops noticeably only in the cold; at
  the ≤ 0.25 C rates of a home battery the rate effect is ~1 %. The earlier
  symmetric temperature factor, fed with the BMS board temperature (~9 °C
  above the cells), booked a ~23 kWh battery as ~32 kWh.
* The correction is applied when the estimate is computed, from each stored
  segment's temperature, so a formula change applies to all stored segments.
* **Reference** = the same estimator over the same eligible set, captured
  automatically once ≥ 20 eligible segments span ≥ 45 days (so a fresh
  reference reads exactly 100 %).

### SOH_eff — round-trip efficiency drift
Anchors are rest ticks (|power| ≤ 100 W) at a BMS recalibration point
(SOC ≥ 99 %, tier 1) or at the configured charge ceiling (tier 2, matched
pairs, ≤ 21 days). Between successive anchors with ≥ 15 kWh of charge:
```
η_window  = Δ(lifetime_discharge) / Δ(lifetime_charge)    (valid 0.50–1.00)
            rejected if > 1.5 % from the median of the last 10 windows
            (once ≥ 5 exist)
baseline  = median of the first 10 valid windows of the epoch
current   = median of the last 6 valid windows
loss      = max(0, (baseline − current)·100 − 0.25)        (deadband, %-pts)
SOH_eff   = clip(100 − loss × 8)
```
The 0.25 %-point deadband exists because η depends on cell temperature: the
field data fell ~0.2 %-pts from August to September with cooler cells, and
without winter data a seasonal swing that size cannot be told apart from
ageing. Raw `efficiency_baseline`, `efficiency_current` and
`efficiency_loss_pct` stay visible.

### SOH_bal — pack balance
One sample per **rest period** (v2.3.2.0): the pack must rest (|power| ≤ 50 W)
at SOC ≥ ceiling − 10 (floor 60) for 10 min; the next sample needs a new rest
period at least 4 h later. ≥ 2 online packs.
```
baseline  = median ΔV, ΔT of the first 20 samples        (~2–3 weeks)
dev_V     = max(0, ΔV − baseline_V),  score_V: 100 → 0 over 0.15 → 0.40 V
dev_T     = max(0, ΔT − baseline_T),  score_T: 100 → 0 over 1.0 → 6.0 °C
SOH_bal   = median of the last 20 samples of (score_V + score_T)/2
```
Until 2.3.2.0 every qualifying tick (~1/min) was a sample, so score and
baseline described ~20 minutes of a single rest period.

### Stress ratio & forecast (informational)
```
S(t)          = Q10^((T_cell − 25)/10) × f(SOC),  Q10 = 2,
                f = 1 → 2.5 linearly above SOC 80
stress_ratio  = time-weighted mean of S over 90 days (display)
effective age = ∫ S² dt over the battery's life                (v2.3.2.0)
                (time before observation started counts at the observed
                 mean S², frozen after 7 days of observation)
predicted_SOH = 100 − 2.5·√(effective_age_years) − 0.004·EFC
divergence    = SOH_cap − predicted_SOH        (negative = ageing faster)
EFC           = lifetime_discharge / C_rated
warranty %    = lifetime_discharge / 28 840 kWh × 100
```
∫S²dt is the equivalent-time form of a √t law whose rate constant scales with
S (Q = k_ref·√∫(k/k_ref)²dt). At constant S it equals the old
`2.5·S·√age`; under changing conditions it accumulates, so the prediction can
only fall once the prior is frozen. The old form (90-day S × √total age)
projected one season onto the whole life and would have risen in winter.
`cold_charge_hours` counts charging with cells below 10 °C (lithium-plating
risk, not modelled in S) for visibility only.

The warranty sensor is a **legal reference** (CH/EEA terms: 28.84 MWh to 60%
retention), *not* "% of real battery life" — real LFP cycle life is typically
far higher.

---

## 3. Registers used (all read-only)

| Register | Address | Use |
|---|---|---|
| `storage_state_of_capacity` | 37760 | SOC (segments, gating, freshness) |
| `storage_charge_discharge_power` | 37765 | +charge/−discharge W |
| `storage_unit_1_battery_temperature` | 37022 | BMS board temperature; fallback only (v2.3.2.0) |
| `storage_total_charge` / `_discharge` | 37780/37782 | efficiency, EFC, energy |
| `storage_rated_capacity` | 37758 | logged (recalibration watch) |
| `storage_unit_1_battery_pack_{1..3}_voltage` | 38235/38277/38319 | balance |
| `..._pack_{1..3}_maximum/minimum_temperature` | 38452+ | balance; cell temperature for capacity and stress (v2.3.2.0) |
| `..._pack_{1..3}_working_status` | 38228+ | pack online gating |
| `..._pack_{1..3}_soh_calibration_status` + unit | 37920–37926 | calibration exclusion |

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
| Battery health confidence | on | `low` / `normal` / `stale` (declared as an HA ENUM sensor since v1.1.7) |
| Battery SOH capacity / efficiency / balance | on (diagnostic) | sub-scores |
| Battery health divergence | on (diagnostic) | measured − predicted; watch for sustained negative |
| Battery equivalent full cycles | on | |
| Battery warranty throughput consumed | on (diagnostic) | legal reference only |
| Battery stress index | **off** | exposure, not health |
| Battery predicted SOH | **off** | heuristic model output |
| Reset efficiency baseline (button) | on | local action, no register writes |

`unknown` states are intentional: with no computable term the sensors report
unknown, never a fake 0 or 100.

## 4b. Fault isolation (v1.1.7)

This subsystem is **additive**: it must never degrade the integration that
existed before it. As of v1.1.7 that is enforced structurally, not by
convention (see `tests/test_battery_health_isolation.py`):

* Config-entry setup **never awaits** battery-health work. Manager objects are
  constructed inline (no I/O); the Store load and coordinator-listener attach
  run as a background task. A slow or failing init cannot contribute to Home
  Assistant cancelling a platform setup — which would take down *all* of the
  integration's entities, not just these.
* Every failure mode is contained and logged: manager creation, background
  init, entity creation in the sensor/button platforms, entity callbacks, and
  unload. A battery-health failure costs you battery-health sensors and
  nothing else.
* **Kill switch:** set the *Enable battery health monitoring* option to off to
  disable the whole subsystem from the UI, without editing files.
* The polled register set is **pinned by a golden-list test** so Modbus load
  cannot grow silently between releases.

## 5. Options (Settings → Integrations → Huawei Solar → Configure)

Rated usable capacity (kWh), warranty throughput (kWh), the three composite
weights (auto-normalized), capacity rolling window (days) and minimum segment
ΔSOC. Changing options reloads the entry; persisted raw segments stay valid —
only the aggregation applied to them changes. All other constants live in
`battery_health.BatteryHealthConfig` with documented defaults.

## 6. Persistence

State is stored via HA's `Store` helper
(`.storage/huawei_solar_battery_health_<serial>`), schema-versioned
(`schema_version: 4` since v2.3.2.0), saved debounced (≥ 5 min apart, plus on
unload). Registered migrations run forward (3 → 4 keeps all learned history
and triggers a one-time re-derivation, see §7c); versions without a migration
start fresh rather than guessing. Restart behaviour: open segments are never
resumed across a restart; the rolling windows are.

## 7. Failure handling (spec §9 heritage)

- Implausible values are **discarded per-field, never clipped**.
- **Data gaps are bridged, not discarded (v1.1.8).** A coordinator read
  failure no longer destroys an in-progress discharge segment or the open
  efficiency window. SOC is an absolute state reading and the lifetime
  counters are cumulative, so ΔSOC and Δenergy across a gap stay exact without
  the samples in between; anything anomalous is caught by the implied-capacity
  band and the η plausibility band on close. Gaps longer than
  `max_gap_bridge_s` (default 1 h) still terminate the segment.

  *Why this matters:* the previous discard-on-gap rule made capacity and
  efficiency measurement **structurally impossible** on a link with
  intermittent Modbus timeouts — a slow overnight discharge could never
  accumulate the minimum ΔSOC between failures. Balance was unaffected
  because it is a point-in-time measurement, which is exactly the fingerprint
  the field report showed (balance populated, capacity/efficiency stuck at
  `Unknown` with `discarded_segment_count` climbing).
- The stress accumulator still **excludes** gap time, because it integrates
  over *time* — an outage genuinely is not a calm period.
- Lifetime-counter decreases > 1 kWh are treated as **reset events** (offset
  carried forward, active segment hard-discarded, efficiency anchor restarted
  from the post-reset sample, WARNING logged) — never as negative energy. A
  reset is the one event that genuinely invalidates interval arithmetic, so
  unlike a data gap it is not bridged.
- One misbehaving entity listener cannot break the others.

## 7b. Diagnosing "why is SOH capacity still Unknown?"

Open the **Battery health index** entity's attributes (Developer Tools →
States, or the entity detail dialog) and read these counters:

| Attribute | Meaning |
|---|---|
| `segment_count` | qualifying segments currently in the 90-day window |
| `segments_used` | of those, eligible for the estimate (depth, calibration, outlier rules; v2.3.2.0) |
| `discarded_segment_count` | segments started and thrown away |
| `gap_bridged_count` | data gaps spanned mid-segment (v1.1.8+) |
| `efficiency_window_count` | completed full-charge-to-full-charge windows |
| `balance_sample_count` | rest-at-high-SOC samples collected |

Interpretation:

* `segment_count: 0` **and** `discarded_segment_count` climbing → segments are
  being started but destroyed. Before v1.1.8 this meant Modbus gaps; from
  v1.1.8 the remaining causes are over-limit gaps (> 1 h), counter resets, or
  implied capacity outside the plausibility band.
* `segment_count: 0` **and** `discarded_segment_count: 0` → no segment ever
  qualified. Usually the discharge is fragmented into runs shallower than
  `min_segment_delta_soc`.
* `gap_bridged_count` rising while segments complete → bridging is doing its
  job on an imperfect link.
* Balance populated while capacity/efficiency are `Unknown` → interval
  measurements are failing while the point-in-time one succeeds; check the
  first two rows.

## 7c. Baselines and recalibration (v1.2.0)

Three sub-scores are measured against **learned per-installation baselines**
rather than absolute constants, because field data showed absolute thresholds
measure the installation, not the battery:

| Baseline | What it anchors | Captured |
|---|---|---|
| Capacity reference (unit + each pack) | what 100% SOH capacity means | automatically, once ≥20 eligible segments span ≥45 days |
| Pack-balance baseline | the normal resting ΔV/ΔT spread | automatically, after 20 rest periods (~2–3 weeks) |
| Thermal-rise baseline | the normal rise above ambient | with or after the balance baseline, once samples span ≥3 days |
| Efficiency baseline | the normal round-trip η | automatically, after 10 valid windows |

**Upgrade to v2.3.2.0 (one time, automatic).** Capacity references (unit and
packs) are re-derived from the stored segments with the new correction and
estimator; the efficiency baseline is re-derived from the stored windows; the
balance baseline and thermal rise re-learn over ~3 weeks (the last balance
score is held meanwhile); the 90-day stress window restarts (it was integrated
with the BMS temperature). Every previous value stays in the epoch history.
If you ever saved the battery-health options, *Min segment ΔSOC* keeps the
saved value (the old default was 10); set it to 15 to follow the new default.

Rules that make this safe:

* **Raw values are never re-zeroed.** `balance_raw_dv`, `balance_raw_dt`,
  `estimated_capacity_kwh` and the thermal-rise figures are ground truth and
  survive every recalibration — only the *derived* score is re-anchored.
* **Recalibration appends an epoch**, retaining previous values and dates, and
  logs at WARNING. Nothing is silently overwritten.
* **Automatic epochs** start when the configured end-of-charge SOC changes,
  because that shifts both η and the SOC operating band systematically.
* Capacity re-anchoring **refuses** unless enough segments span enough time.
  Since v2.3.2.0 the button re-anchors the unit **and every pack**, from the
  same eligible segments the estimate uses.

⚠️ Re-anchoring *after* real degradation has occurred will hide that
degradation in the score. That is why the raw series and the epoch history
exist — they remain the record. Recalibrate after hardware changes, not as
routine maintenance.

## 7d. Optional: ambient temperature

If you configure an ambient temperature sensor for the battery room
(*Ambient temperature sensor entity* in the options), the integration derives
each pack's **rise above ambient**. This measures heat *generation*, which
inter-pack spread cannot see when all packs age together — a genuinely
independent degradation channel. It is optional, configurable (so replacing
the sensor needs no code change), and degrades silently when unavailable.

## 7e. Maintenance and reboots (v1.2.1)

Learned baselines take weeks to build, so anything that could corrupt them
during maintenance matters more than it first appears.

**Before planned work** (e.g. a Huawei firmware update, which takes about an
hour): turn **off** the *Adaptive learning* switch. Since v1.2.2 this single
switch governs **both** learners — the battery-health engine and the adaptive
Modbus controller. Sensors keep
displaying and raw values keep updating; only irreversible learning freezes -
no segments recorded, no baselines captured, no charge-ceiling epoch. Turn it
back on once the system is stable. A day without learning costs nothing.

**Unplanned reboots** cannot be prepared for, so the engine suspends learning
automatically for a settling period (default 5 minutes) after any integration
start, coordinator recovery, or lifetime-counter reset. Since v2.3.2.0 an
ordinary coordinator recovery keeps the open discharge segment (it is bridged
like any data gap ≤ 1 h); a counter reset or re-enabling learning still ends
it.

**Charge-ceiling guard:** because a ceiling change restarts baseline epochs, a
reading below 20% is rejected as a reboot artefact, and any change must
persist across several consecutive polls before it is accepted.

**Why the Modbus learner matters most here.** An hour of unreachable inverter
is roughly 120 consecutive failed requests spread over four 15-minute
circadian slots. On a mature slot that lifts the failure rate from ~3% to
~12%, which maps to a poll interval near 137 s instead of 20–30 s. Daily decay
does **not** undo this: decay scales failures and sample count equally, so it
lowers confidence but leaves the ratio intact. Only new successful
observations dilute it — and those accrue 4–5× more slowly precisely because
polling has slowed. A single unguarded maintenance window can cost weeks of
degraded polling, with nothing to tell you it happened.

Check `learning_enabled`, `learning_active`, `settling_events` and
`ceiling_rejected_readings` in the Battery health index attributes, and
`suppressed_observations` on the adaptive diagnostic sensors, to confirm what
happened across a maintenance window.

## 7f. Modbus tier separation (v1.3.3)

Since v1.3.3 the integration reads SLOW/STATIC registers in separate requests
from FAST/NORMAL ones, and refreshes them every 15 minutes rather than 5.
Field measurement showed any request touching SLOW-tier content costs
~2.9 s + 377 ms/register, versus ~6 ms for a FAST/NORMAL-only request of the
same size.

Since v1.3.4 the whole SLOW/STATIC cohort is also refreshed **together** when
any of them comes due, rather than dribbling in one or two at a time — measured
~6x less expensive-exchange cost.

**Effect on battery health:** the lifetime charge/discharge counters are
SLOW-tier, so segment endpoints may be up to 15 minutes stale. Coalescing works
in your favour here: because the cohort refreshes as one, the counters and the
other slow registers now share a timestamp instead of being scattered across a
15-minute window. This is already
handled — `CounterMonitor` flags carried-forward values and segments refuse to
open on them (v1.2.3, Finding C) — but `stale_endpoint_skips` in the health
index attributes is the number to watch if capacity segments stop completing.

## 8. Known limitations

1. **Circularity:** SOH_cap depends on BMS SOC. Freshness weighting, the
   depth minimum and the outlier rules mitigate but cannot remove this. The
   BMS SOC of LiFePO4 can be several percent off between full charges (flat
   OCV, hysteresis), which is why segments shallower than 15 points are not
   used.
2. **Forecast is heuristic.** The √t + throughput model uses literature-typical
   LFP constants, not fitted cell data. It exists to make *divergence*
   computable, not to predict warranty outcomes. It cannot foresee a late
   "knee" (sudden acceleration of fade); a sustained negative divergence is
   how one would show up.
3. Only **storage unit 1** (up to 3 packs) is currently processed.
4. Options changes require the automatic entry reload to take effect.
5. **Reversible capacity swings (anode overhang).** Measured capacity can move
   by about a percent with the recent SOC history (strongest in the first
   100–200 days and after a change of operating pattern) without any real
   ageing. Treat changes below ~2 % as noise. A reference learned after a
   summer at high SOC may read slightly above 100 % in winter.
6. **Winter behaviour not yet field-validated** (revisit Feb–Mar 2027):
   balance sampling at lower ceilings; temperature dependence of η (the
   deadband covers the size seen Aug→Sep; per-window cell temperatures are now
   recorded for this review); the cold-side capacity correction (0.5 %/°C
   below 15 °C, literature-based, untested on this hardware).
7. **Huawei rated-capacity register (37758)** is watched for a step after
   Huawei's own calibration; not yet observed, not used in any formula.
8. If PV/load patterns never produce ΔSOC ≥ 15 discharge segments,
   confidence stays `low`/`stale` — by design. (15 is a segment *depth*, not
   an absolute SOC floor: a 100% → 77% overnight run qualifies comfortably.)

## 9. Byproduct: one actionable aging lever

Indoor placement and PV-only 0.2C charging already put this battery near
best-case. The one significant modifiable stressor is **long summer dwell at
100% SOC** — the strongest calendar-aging accelerator for LFP. A seasonal
end-of-charge cutoff of ~90% in summer, with a full charge every 2–4 weeks
(the BMS needs periodic full charges for SOC correction, and Huawei's natural
SOH calculation needs full sessions), meaningfully slows calendar aging. The
integration's existing `end-of-charge SOC` number entity (register 47081) can
automate this; the BHI subsystem itself deliberately stays read-only.
