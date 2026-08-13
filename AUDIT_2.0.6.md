# Release Audit — huawei_solar v2.0.6

**Date:** 2026-08-13 · **Auditor:** Claude (Anthropic)
**Baseline:** v2.0.5
**Type:** the fully implemented battery health rework, postponed since the
project's own `PHASE1_BATTERY_HEALTH_DESIGN.md` was parked pending a
prerequisite that has since been built by unrelated work earlier this same
session. One deliberate release, as agreed with the operator up front — no
intermediate deployments for this feature.

---

## 1. Why this release exists, and how it was scoped

The operator asked to look at architecture before any code, given how much
had changed since battery health was originally designed. That review
turned up more than expected, and shaped everything that follows:

- The prerequisite the parked Phase 1 design was explicitly waiting for —
  a "quality-aware data model," so a stale or corrupted reading can't
  silently corrupt a stateful tracker — already exists and is already
  wired into `battery_health_manager.py`'s own `_value()` helper
  (`V2_ARCHITECTURE_DESIGN.md` §10.4 documents this as a deliberate
  decision, made specifically because of this exact parked design). Phase
  1 could be safely resumed.
- A genuinely live defect was found during the review, unrelated to
  anything new: calibration-overlapping segments were receiving a 4x
  weight *boost* in production, the opposite of what the parked design's
  own careful analysis had concluded was safe.
- The operator's own knowledge of available per-pack registers (current,
  power, SOC, lifetime charge/discharge) changed the right design for the
  balance term — from the parked design's simpler "blend SOC into the
  existing proxy" plan to a direct, measured per-pack capacity tracker,
  once those registers were confirmed to exist with the right units.

Work was organized into four tiers, agreed with the operator before any
code: Tier 1 (fix the live defect), Tier 2 (extend the same fix's
reasoning to the two trackers that share its risk), Tier 3 (the new
functionality — per-pack capacity, temperature/rate normalization), Tier 4
(polling cadence, confidence dispersion — deliberately not attempted,
unchanged from the parked design's own "wait for real data" reasoning).

## 2. Tier 1 — calibration weighting corrected

**Confirmed live in production, not hypothetical**: `_seg_calibration_seen`
→ `golden=True` → `weight()` multiplied by `golden_weight_boost` (4.0x) —
directly inflating that segment's influence on `soh_capacity`'s own
weighted mean. The parked design's own analysis, done specifically because
the calibration register can't distinguish "calibrating now" from "just
finished" from one reading, concluded the opposite: exclusion, not a
boost.

**Fix**: full exclusion (`weight()` returns `0.0`), with proper edge
detection — the nonzero→zero transition marks a completion, and a
`calibration_settle_s` window (300s, reusing the same reasoning as the
existing `settling_period_s`) covers the ambiguity a single reading can't
resolve. Given the operator's own explicit choice (no migration needed,
history not worth preserving), the old `golden`/`golden_weight_boost`
mechanism was removed entirely rather than kept as unused, confusing
infrastructure — a clean replacement, not a patch alongside dead code.

## 3. Tier 2 — calibration-awareness extended to efficiency and balance

**Confirmed as more than a symmetry argument**: the codebase's own
existing comment on `EfficiencyTracker`'s tier-1 anchor condition
explicitly describes it as sitting "at a BMS recalibration point" — a
structural, not incidental, overlap with calibration timing, unlike
capacity's case.

**Fix**: required a real refactor of Tier 1's own code, done deliberately
and verified carefully — the calibration edge-detection state moved from
`SegmentTracker` up to `BatteryHealthEngine.update()`, computed once per
tick and passed to all three trackers, since three consumers now needed
it. `EfficiencyTracker` disqualifies a reading as an anchor entirely
during the uncertain window (simpler and safer than excluding only the
resulting window after the fact). `BalanceTracker` mirrors its own
existing `learn=False` philosophy exactly: raw values still recorded for
display, scoring/baseline capture skipped.

## 4. Tier 3 — per-pack capacity tracking and normalization

### 4.1 Per-pack capacity (`PackCapacityTracker`)

**Chosen over the parked design's own simpler plan** once per-pack
lifetime charge/discharge counters were confirmed to exist (the
operator's own knowledge), with the same units/gain as their unit-level
equivalents, and PDU-adjacent to the already-polled per-pack voltage
register (low expected marginal traffic cost, though genuinely new
traffic — confirmed directly against the real register map, not assumed).

**Design**: three `SegmentTracker` instances, one per pack, reused exactly
as written rather than reimplemented — every existing guard (SOC-
correction, freshness weighting, and now Tier 1/2's own calibration
exclusion) applies identically per pack via a synthetic per-pack
`HealthSample` view, with zero duplicated logic. Each pack gets its own
pair of `CounterMonitor` instances, since a single pack replacement — a
real, plausible maintenance event — resets only that pack's own counters,
not the unit's or the other two packs'.

**A real, structural bug found and fixed during implementation, not
assumed away**: `SegmentTracker`'s plausibility band
(`implied_capacity_min/max_kwh`, 8-35 kWh) is calibrated for the whole
unit's nameplate capacity. Reusing it unscaled for per-pack trackers would
have systematically rejected genuine per-pack segments, since a single
pack's true capacity is roughly a third of that. Fixed with a scaled
config copy specific to the per-pack trackers.

**A second bug, found by direct end-to-end testing, not code review**:
`validate_sample()`'s own `PackSample` reconstruction loop only copied the
original four fields — every new per-pack field was silently dropped to
`None` on every tick, meaning no segment could ever have formed. Caught
because the very first end-to-end test produced zero segments where one
was expected, and traced to the actual cause rather than adjusted to
match the wrong behavior.

New registers required `NORMAL` polling tier for their two counter fields
specifically (`register_cache.py`'s own `_TIER_OVERRIDES`), matching the
exact, already-established reasoning for the unit-level counters: stale
readings introduce segment-energy error at each endpoint.

**Given no migration constraint** (operator's own explicit choice),
`SCHEMA_VERSION` was bumped (1→2) rather than adding defensive tolerance
for old persisted data — old data is discarded cleanly via the existing
`restore()` guard, confirmed with a dedicated test.

### 4.2 Temperature/rate normalization

Implements `PHASE1_BATTERY_HEALTH_DESIGN.md` §6.2 exactly:
`f_T(T) = exp(-(T-T_ref)²/σ_T²)`, `f_rate(P) = 1/(1+(P/P_ref)^γ)`,
`C_normalized = C_raw / (f_T · f_rate)`, both factors clamped to a floor
(default 0.5), each independently defaulting to neutral when its own
input is unavailable. `T_ref = 25°C` matches this project's own
`stress_ref_temp_c`, confirmed already anchored at the identical value
elsewhere, not a fresh, unrelated choice. Rate normalization uses power
(exactly recoverable from `energy_kwh / duration_hours`, already tolerant
of gaps by construction) rather than current, per the parked design's own
reasoning — avoiding a need for per-pack current summed across
potentially-partially-unavailable packs.

**A consistency bug caught while implementing, not after**: the segment's
own captured reference capacity (used as the SOH% denominator) was still
being computed from raw `implied_capacity_kwh` even after the main
aggregation switched to normalized values — comparing a normalized
numerator against a raw-valued reference would have been an inconsistent
comparison, not just a smaller inaccuracy. Fixed in both the automatic
capture path and the manual re-anchor handler the parked design itself
specifically flagged as a place a bug had been caught during the original,
reverted attempt.

**The parked design's own documented lesson, confirmed and applied, not
just cited**: the existing test suite's compressed-time discharge helper
implies unrealistic power (tens of kW against a 5 kW reference) that a
working rate-normalization correctly reacts to. The shared test `_cfg()`
helper now neutralizes normalization by default, exactly as recommended —
confirmed necessary directly, not assumed: 5 pre-existing tests failed
with precisely the predicted symptom before this fix, all passing after.

Per-pack normalization applies automatically through the same shared
`SegmentTracker` code, once the synthetic per-pack sample was updated to
also carry each pack's own averaged temperature.

### 4.3 Entity exposure

Per-pack results (SOH%, segment count, spread) exposed as attributes on
the existing `soh_capacity` sensor entity, matching this project's own
established pattern (related diagnostics grouped under their parent
sensor) rather than new top-level entity infrastructure for this addition.

## 5. Test evidence

- **911 passed, 1 skipped, 0 failed** (was 897 at the v2.0.5 baseline; 14
  new tests directly for Tier 3 — 6 for `PackCapacityTracker`, 8 for
  normalization).
- The debugging process for this release's own new tests surfaced real,
  useful findings in its own right, not just confirmed working code: the
  `validate_sample()` gap (§4.1), the plausibility-band scaling issue
  (§4.1), and two rounds of the author's own test data mixing unit-scale
  and pack-scale values before landing on correct, verified numbers.
  Recorded honestly here rather than presented as if everything worked on
  the first attempt.
- A structural, source-level verification swept every tier's own core
  mechanism (`exclude_calibration`, `calib_uncertain`, `pack_capacity`,
  the dual normalization call sites, `SCHEMA_VERSION`) directly against
  the final source, not only the test suite's own pass/fail result.

## 6. Safety properties

- v2.0.5 remains available and was not modified; this release was built
  in its own working tree.
- Tier 1's removal of `golden`/`golden_weight_boost` is a deliberate,
  irreversible break from old persisted data, matching the operator's own
  explicit choice — not an oversight.
- Every new config field (`calibration_settle_s`, `capacity_temp_ref_c`,
  `capacity_temp_sigma_c`, `capacity_rate_ref_w`, `capacity_rate_gamma`,
  `capacity_norm_factor_floor`) carries a reasoned default explained
  directly in its own comment, not an arbitrary number — several
  explicitly tied to existing, already-field-validated values elsewhere
  in this same engine.

## 7. Recommended next step

Deploy 2.0.6. This is the one deployment the operator planned for this
feature — no staged rollout. Tier 4 (polling cadence, confidence
dispersion) remains deliberately undesigned, per the same "wait for real
segment-tracker behavior, don't guess" reasoning the parked design itself
already established; nothing in this release's own data collection
changes that.

**Verdict:** all three tiers implemented, tested, and internally
consistent with each other and with this project's own established
practices. Two structural bugs and one consistency bug were found and
fixed during this same pass's own implementation and testing, not
discovered later — recorded here plainly rather than omitted.
