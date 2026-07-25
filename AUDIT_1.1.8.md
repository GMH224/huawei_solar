# Release Audit — huawei_solar v1.1.8

**Date:** 2026-07-25 · **Auditor:** Claude (Anthropic)
**Baseline:** v1.1.7 (326 passed / 1 skipped).
**Scope:** `battery_health.py` (segment + efficiency gap handling),
`tests/test_battery_health.py`, `manifest.json`, documentation.
**Type:** design correction to measurement logic. No changes to the entity
layer, setup path, register set, or fault-isolation contract established in
v1.1.7.

---

## 1. Field evidence

From the reporting installation, the `battery_health_index` attributes:

```
segment_count: 0            discarded_segment_count: 11
efficiency_window_count: 0  balance_sample_count: 20
contributing_terms: [balance]      counter_resets: 0
soh_balance: 91.4    soh_capacity: null    soh_efficiency: null
```

The discriminating observation is that **balance worked and the other two did
not**. Balance is a point-in-time measurement — a single sample at rest at
high SOC. Capacity and efficiency are interval measurements spanning hours to
days. Eleven segments started; eleven were destroyed; none completed. That
pattern isolates the fault to interval termination, not to data availability,
register access, or the entity layer.

Corroborating: `counter_resets: 0` rules out counter resets as the cause, and
`reported_rated_capacity_wh: 20700` confirms register reads were succeeding.

---

## 2. Finding — discard-on-gap was a design error (v1.1.5 spec)

**Severity:** High. Rendered two of the three measured health terms
permanently uncomputable on any installation with intermittent Modbus
timeouts.

`SegmentTracker.mark_gap()` discarded the in-progress segment on any
coordinator read failure; `EfficiencyTracker.invalidate_anchor()` dropped the
open window on the same trigger.

**Why the original justification fails.** The spec rationale was "do not guess
what happened during the outage." Nothing needs to be guessed:

* `storage_state_of_capacity` is an **absolute state** reading — the value
  after the gap is directly comparable to the value before it.
* `storage_total_discharge` is a **cumulative counter** — the difference
  across the gap is exact regardless of missing intermediate samples.

Therefore ΔSOC and Δenergy — the only two quantities entering the capacity
calculation — survive a gap intact. The one genuine risk (unobserved activity
inflating the counter relative to the SOC change) is already caught by the
implied-capacity plausibility band on close. The discard rule was redundant
over-engineering layered on top of a guard that already handled the case.

**Why the impact was structural rather than marginal.** With timeouts at
roughly one per 25 minutes and a slow overnight discharge of ~3 SOC points per
hour, a segment could accumulate ~1.25 SOC points between failures against a
10-point minimum. The threshold was **mathematically unreachable**, not merely
slow. No amount of additional waiting would have produced a result — which is
why "let it run another day" repeatedly failed to change the outcome.

---

## 3. Change and its bounds

| Property | v1.1.7 | v1.1.8 |
|---|---|---|
| Coordinator read failure mid-segment | segment discarded | gap marked pending, bridged on next good sample |
| Gap longer than `max_gap_bridge_s` (new, default 3600 s) | n/a | segment discarded; a fresh segment starts if still discharging |
| Efficiency anchor on gap | invalidated | preserved (cumulative counters are gap-immune) |
| Lifetime-counter reset | segment discarded, anchor invalidated | **unchanged** — hard discard via new explicit `discard_active()`; anchor restarts from the post-reset sample |
| Stress accumulator on gap | gap Δt excluded | **unchanged** — it integrates over *time*, where an outage is genuinely not a calm period |
| Validation on close | implied-capacity band, η band | **unchanged** — the guards that make bridging safe |

Trust is bounded, not removed: the bridge limit caps how much unobserved time
a single segment may span, and every bridged segment still passes the same
plausibility validation as an unbridged one.

**Diagnostics added:** `DischargeSegment.gap_bridged` (per segment) and
`gap_bridged_count` (cumulative, exposed in entity attributes and persisted),
so the mechanism is observable in the field rather than taken on trust.

---

## 4. Backward compatibility

* `DischargeSegment.from_dict` defaults `gap_bridged` to 0, so segment logs
  persisted by v1.1.5–v1.1.7 load unchanged
  (`test_pre_1_1_8_persisted_segments_still_load`).
* Storage schema version is unchanged; no migration required.
* No change to the register set, entity set, unique IDs, or options.

---

## 5. Verification

**Test evidence: 337 passed, 1 skipped, 0 failed** — identical across 3
consecutive runs.

*Tests changed, and why that is not a weakening.* Two tests in `TestGapHandling`
asserted that any gap discards the segment. Those tests encoded the design
error itself; leaving them green would have meant preserving the bug by test.
They were replaced with the corrected contract (short gaps bridged, over-limit
gaps discarded, fresh segment after over-limit gap, counter reset still
hard-discards) rather than deleted, so the number of assertions about gap
behaviour increased rather than decreased.

*New coverage (11 tests):*

* `test_field_report_scenario_timeouts_no_longer_prevent_measurement` —
  reproduces the reported installation directly: 8 h of discharge at
  ~3 SOC points/h with a timeout every 25 min. Asserts a qualifying segment
  completes **and** that the capacity estimate is accurate despite the gaps
  (20.7 kWh ± 0.3, SOH ≈ 100 ± 1.5). This is the test that would have caught
  the defect before release.
* `test_counter_carry_forward_does_not_corrupt_segment_energy` — confirms that
  `CounterMonitor` carrying stale counter values forward during outages cannot
  distort the result, since only segment endpoints enter the arithmetic.
* `TestEfficiencyGapTolerance` (3) — anchor survives gaps; baseline forms on a
  flaky link; counter reset restarts the anchor without spanning it.
* `TestGapBridgingDiagnostics` (3) — attribute exposure, persistence
  round-trip, pre-1.1.8 compatibility.

**Adversarial verification (required practice since v1.1.7):** the new tests
were run against the pristine v1.1.7 tree. **10 failed**, including the
field-scenario test. The tests are load-bearing, not tautological.

**Regression surface:** all 30 v1.1.7 entity-contract and fault-isolation
tests pass unchanged, confirming this release does not disturb the isolation
guarantees or the entity layer.

**Static:** all Python files parse clean; all JSON valid; `manifest.json` =
1.1.8.

---

## 6. Process finding

This defect was not caught by 326 tests because every gap-related test asserted
the *implemented* behaviour rather than a *required* one. The suite verified
"a gap discards the segment" — which the code did faithfully — without ever
asking whether discarding was correct, or whether the resulting system could
produce a measurement at all under realistic conditions.

The missing test class was **end-to-end feasibility under realistic degraded
conditions**: given a plausible duty cycle and a plausible link quality, does
the pipeline ever produce output? That is now covered by the field-scenario
test, and is the pattern to apply to any future measurement path.

Second observation: the diagnostic counters (`segment_count`,
`discarded_segment_count`) that made this a five-minute diagnosis were already
present from v1.1.5. Exposing internal pipeline state in entity attributes
paid for itself; future measurement features should do the same by default.

---

## 7. Expected behaviour after deployment

On the reporting installation, with the same link quality, a qualifying
overnight discharge should now complete. Expected progression:

1. `gap_bridged_count` begins rising (bridging active).
2. `segment_count` reaches 1 after the first qualifying discharge closes, and
   `soh_capacity` populates — likely within 24 h.
3. `confidence` remains `low` until 5 segments **and** an efficiency baseline
   exist; `efficiency_window_count` needs three ~30 kWh full-charge windows,
   so `soh_efficiency` remains weeks away by design.
4. `discarded_segment_count` may still increase occasionally (over-limit gaps,
   implausible intervals) — that is correct filtering, not failure, provided
   `segment_count` also grows.

**Residual risk:** if `segment_count` remains 0 while
`discarded_segment_count` continues climbing and `gap_bridged_count` stays 0,
the cause is *not* gap handling and the diagnosis must be re-opened — most
likely fragmentation below `min_segment_delta_soc`, or gaps exceeding the 1 h
bridge limit. §7b of BATTERY_HEALTH.md documents this decision tree for the
user.

**Verdict:** release-ready. A confirmed design error corrected with bounded
scope, adversarially verified tests, unchanged safety and isolation
properties, and field-observable diagnostics for confirming the fix works in
situ.
