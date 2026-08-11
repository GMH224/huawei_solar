# Phase 1 Battery Health Revision — Parked Design Document

**Status: PARKED, not implemented.** This document exists to preserve the
analysis and findings from a full design-and-implementation pass on
`battery_health.py` so none of it has to be rediscovered later. The
*code* from this pass was written, compiled, and passed the full test
suite (633/633) — but was **deliberately reverted, not shipped**, after a
separate architectural investigation (documented in `CLAUDE.md`'s v2.0.0
entry once it exists) found that the register cache conflates transport
(Modbus link) health with sensor (payload) health at the data-model level.
Battery health specifically was the case that made this concrete: a
segment-based estimator that infers capacity from *changes between
readings* is exactly the kind of consumer most exposed to silently
treating a stale, connection-blip-affected reading as current. Phase 1
should not be rebuilt until it can consume quality-aware inputs (value +
timestamp + link-quality + failure count) from that rebuilt layer, rather
than the bare values the cache exposes today.

**When resuming this work:** re-read this document in full before writing
any code. Most of the design decisions below were reached only after
checking specific claims against real source and real register data, not
assumed — that verification work does not need to be repeated, only the
final wiring against whatever the 2.0.0 quality-aware data model ends up
looking like.

---

## 1. Where this started

An external "battery health architecture proposal" (third draft,
literature-referenced) recommended three changes to the existing
segment-based capacity/efficiency/balance engine: calibration-aware
learning, temperature/C-rate normalization of capacity and efficiency,
and confidence/uncertainty reporting. The proposal was independently
verified, not accepted at face value:

- Its core Huawei calibration-behaviour claims checked out against real
  Huawei support documentation.
- Its academic citations were spot-checked; real, correctly attributed,
  with one date error found (Beckers et al. is 2023, not 2026 as cited).
- Its characterisation of "missing telemetry" was checked against the
  real register table and found to be **more pessimistic than reality**
  in several places (see §2).

## 2. The full register inventory — what's actually available

Every `storage`/`battery` register the vendor library exposes was pulled
directly (156 total) and cross-referenced against what
`battery_health_manager.py` actually reads. Key findings, independent of
the external proposal:

| Signal | Proposal's claim | What was actually found |
|---|---|---|
| Per-pack current | "Needed, if Huawei exposes it" | **Exists** (`storage_unit_1_battery_pack_{1,2,3}_current`), already polled for existing `pack_1/2/3 current` sensor entities — zero new Modbus traffic to wire it in |
| Calibration release-limit register | "Register 37927, if exposed" (hedged) | **Exists** (`storage_unit_soh_calibration_release_lower_limit_of_soc`) |
| Per-pack SOC | Not mentioned | **Exists** (`storage_unit_1_battery_pack_{1,2,3}_state_of_capacity`), unused. Likely a *better* balance signal than voltage spread for LFP specifically — see §4 |
| Per-pack total charge/discharge | Not mentioned | **Exists**, unused. Would enable pack-level (not just unit-level) throughput/stress tracking |
| Ambient temperature | "Missing, Huawei needs to expose it" | **Already supported** — but from a user-configured HA entity (`CONF_BH_AMBIENT_ENTITY`), not a Huawei register. Already computes "thermal rise above ambient" as a **diagnostic-only** attribute, never fed into capacity/efficiency calculations |
| Cell-level voltage/temperature spread | "Needed, unavailable" | Confirmed genuinely unavailable — only pack-level granularity exists anywhere in the register map |

## 3. The calibration register's real limitation, and how it was resolved

The raw `storage_unit_soh_calibration_status` register (and the per-pack
equivalents) is a plain `U16`, not a named enum in the vendor library.
This project's own prior investigation, already documented in
`battery_health_manager.py`'s own comments, established: *"0 = not
started/idle. Any non-zero = check in progress **or just completed**."*

This means a 4-state calibration state machine (idle / calibrating /
recently-completed / unknown) **cannot be built from a single reading of
this register** — "calibrating" and "just completed" share the same
encoding.

**Resolution reached (still valid, worth keeping):** don't try to
distinguish the *state* from one reading — detect the *edge*. A
transition from nonzero → zero is an unambiguous "calibration just
finished" event, regardless of what the intermediate value means. This
gives a fully workable 3-state design (idle / active / just-completed)
built entirely on data already read reliably, with **no dependency on the
unverified release-limit register at all**.

## 4. The balance term's real weakness, and why per-pack SOC is the right fix

`BalanceTracker`'s own field-investigation history (already in the code's
comments) documents that pack **voltage** spread is dominated by
quantisation noise: *"Pack voltage has 0.1 V resolution... one
least-significant bit moved the score 11 points, so the metric mostly
reported quantisation noise."*

The external proposal's fix for balance-term weakness was cell-level
voltage spread — confirmed genuinely unavailable on this hardware (§2).
**Per-pack SOC targets the identical, already-documented weakness**,
without the quantisation problem, and isn't confounded by LFP's flat
mid-range OCV plateau the way a raw voltage reading is. This is a better
fix than what was proposed, cheaper than what was proposed (zero new
telemetry), and independently derived from this project's own history
rather than from the external report.

## 5. Capacity vs. efficiency normalization — a real asymmetry, not equal-cost work

Capacity segments are relatively short, single continuous discharges — a
temperature/rate reading averaged over the segment is a reasonable proxy
for "conditions during it."

Efficiency windows are structurally different: `EfficiencyTracker`
compares two **anchor points** that can be days apart (tier-2 windows are
explicitly time-capped in *days*, not hours, in the existing field-tuned
design). Temperature/C-rate normalization for efficiency would need a
genuine time-weighted running average across that whole multi-day window,
not a point sample — real new accumulator infrastructure, not a variant
of the capacity-side change.

**Decision reached:** efficiency-term normalization should be excluded
from whatever "Phase 1"-equivalent ships in the 2.0.0 world too — not
because it's uncertain, but because it's a different shape and size of
work from the other three changes, and bundling it in would mean either
rushing its design or delaying everything else.

## 6. The actual implementation that was built, compiled, tested, and then reverted

For reference — none of this code exists in the tree anymore
(`battery_health.py` and `battery_health_manager.py` are back to their
exact clean v1.3.21 state), but the design is preserved here in enough
detail to reconstruct quickly once quality-aware inputs exist.

### 6.1 Data model extensions needed
- `PackSample` gained `soc: float | None`.
- `DischargeSegment` gained `avg_temp_c: float | None` (accumulated
  live during the segment, counting only valid/non-`None` samples — the
  segment's temperature is `None`, not zero or a guess, if too few valid
  readings were captured) and `exclude_calibration: bool` (see §6.3).
- Both required careful, tolerant `from_dict()` handling so segments
  persisted before this change continue to load correctly (defaulting the
  new fields to `None`/`False` rather than crashing or silently
  corrupting old data).

### 6.2 Capacity normalization
```
f_T(T)  = exp(-(T - T_ref)^2 / sigma_T^2)     # Gaussian, T_ref = 25 C
f_rate(P) = 1 / (1 + (P / P_ref)^gamma)        # power-based, not current-based
C_normalized = C_raw / (f_T * f_rate)          # both factors clamped >= 0.5
```
Key design decisions, still valid:
- **`T_ref = 25°C` is not a guess** — it's the near-universal battery
  industry reference temperature, and `battery_health.py` already
  independently anchors `stress_ref_temp_c` at the same value elsewhere.
- **Rate normalization uses power (`storage_charge_discharge_power`,
  already reliably read), not current.** Average segment power is
  recoverable exactly from `energy_kwh / duration_hours`, both already
  present on every segment via cumulative-counter differences that
  already tolerate gaps by construction. This avoids needing per-pack
  current summed across potentially-partially-unavailable packs — a
  direct application of the stability requirement this whole phase was
  built around.
- **Both correction factors default to neutral (1.0) when their input is
  unavailable** — never a reason to discard a segment or fail. This is
  the core of "logic needs to be stable" given how often battery
  registers were observed going `unknown` under real bus contention this
  session.
- **A real bug was caught in testing, not shipped**: the manual
  re-anchor button's handler referenced a bare `cfg` where the method
  actually uses `self.cfg` — would have been a `NameError` on first use.
  Caught on review before any test ran against it.
- **A genuine test-fixture insight, not a logic bug**: the existing test
  suite's `_run_discharge()` helper compresses a full discharge into 20
  simulated minutes, implying unrealistic rates (~37 kW against a 5 kW
  residential reference) that the new rate-normalization correctly reacted
  to strongly. Fixed by neutralising normalization in the shared `_cfg()`
  test helper for pre-existing tests (which predate normalization and
  aren't testing it), not by weakening the normalization logic itself.

### 6.3 Calibration state machine
- `SegmentTracker` tracked `_calib_prev_active` and `_calib_settle_until`
  across every tick (not just while a segment is active — the edge can
  occur at any time).
- A segment **starting** while calibration is active, or within
  `calibration_settle_s` (reasoned default: 300s, reusing the existing
  `settling_period_s` value since both express "don't trust data
  immediately after a known disruption") of a detected completion, is
  marked `exclude_calibration=True` — `weight()` returns `0.0` for such
  segments, i.e. full exclusion from the trimmed-mean aggregation, not a
  boost.
- **The existing `golden`/`golden_weight_boost` mechanism was
  deliberately left in place but unused going forward** — old persisted
  segments with `golden=True` keep their existing weighting exactly as
  before (backward compatible), but no *new* segment sets `golden=True`
  from calibration overlap. The proposal's second calibration
  recommendation — "treat a **completed** calibration as a strong anchor"
  — was **deliberately not implemented**, since doing so with confidence
  would require cross-referencing `storage_rated_capacity`'s stepping
  behaviour with the detected completion event, a meaningfully more
  uncertain piece of design than the exclusion/settle mechanism, and the
  "100% certainty before implementing" bar set for this phase wasn't met
  for it.

### 6.4 Balance term
- Per-pack SOC spread blended additively alongside the existing dV/dT
  signal (`balance_soc_weight = 0.6`), not a full replacement — restraint,
  consistent with every other change made throughout this project rather
  than a wholesale substitution of working code.

### 6.5 Confidence reporting
- Not substantially built. Discovered during this pass that a 3-level
  categorical confidence (`stale`/`low`/`normal`, gated on staleness and
  segment count) **already exists** in `soh_capacity()`'s attributes —
  the proposal's "add confidence reporting" recommendation was already
  half-implemented. The remaining work (adding dispersion as a finer
  signal) was not started.

## 7. What must change before this can be rebuilt

Once the 2.0.0 quality-aware data model exists, revisit every accumulator
above with the following question: **does this need to know the quality
of the reading it just consumed, not just its value?** Concretely:

- `SegmentTracker`'s temperature accumulator should likely skip (not just
  tolerate) a reading whose link-quality indicates it may be stale-served,
  not merely `None` — a served-but-degraded value is a different case from
  a genuinely-missing one and may deserve different handling.
- The same applies to every SOC/power/discharge reading the segment
  detector consumes for delta calculations — a stale SOC treated as
  current is exactly the failure mode that motivated abandoning Phase 1's
  code in the first place (see the top of this document).
- `PackSample.soc` (for balance) and the capacity normalization inputs
  should carry their own quality alongside the value, not just be read as
  bare numbers, once that's available to read.

Everything else in §6 — the formulas, the reasoned constants, the
calibration edge-detection design, the balance blend — is expected to
still be correct and reusable; only the *inputs* need to become
quality-aware.
