# Release Audit — huawei_solar v1.2.0

**Date:** 2026-07-26 · **Auditor:** Claude (Anthropic)
**Baseline:** v1.1.8 (337 passed / 1 skipped)
**Scope:** `battery_health.py`, `battery_health_manager.py`,
`battery_health_entities.py`, `button.py`, `config_flow.py`, `const.py`,
`strings.json`, `translations/en.json`, `manifest.json`, `tests/`
**Type:** measurement-correctness release. No change to the fault-isolation
contract (v1.1.7) or the read-only guarantee.

---

## 1. Why this is 1.2.0 and not 1.1.9

Several changes alter what the numbers **mean**, not merely their accuracy:

* SOH capacity is now measured against a learned reference rather than the
  nameplate — the same battery will report a different number than under
  v1.1.8, and that is the point.
* SOH balance is now deviation from a learned baseline rather than an absolute
  threshold.
* The composite may include *held* sub-scores across a season.

Anyone reading the changelog needs to know their history is not directly
comparable across this boundary. A patch bump would have concealed that.

## 2. Evidence base and its limits

All findings derive from replaying a real 6-month dataset offline
(`tests/FIELD_VALIDATION.md`). That data is **confidential and is not
included in this repository.**

Deliberately recorded weaknesses, because a validation claim without its
limits is a marketing claim:

* Balance and thermal conclusions rest on **6 weeks of summer data at a single
  charge ceiling**. The winter behaviour of the ceiling-relative balance gate
  is designed by analogy to the efficiency anchors and is **not yet validated**.
* BMS temperature spans only 3.2 °C, so η temperature compensation could not be
  fitted and was **abandoned rather than guessed** (r = +0.18, 2% noise
  reduction).
* One installation, one hardware generation. Every derived constant is exposed
  as a tunable rather than hardcoded.

## 3. Findings and resolutions

| # | Finding | Severity | Resolution |
|---|---|---|---|
| H | SOH capacity anchored to nameplate (20.7) vs measured (22.75) — pinned at the 100% clip, hiding the first ~10% of degradation | **High** | Measured BOL reference, auto-captured, persisted, re-anchorable; clip raised to 110 |
| J | Implied capacity varies ~2% with SOC operating band; usage is seasonal | **High** | Segments record `soc_midpoint` + ceiling; reference capture requires a 45-day span |
| L | Efficiency anchors admitted ±3 SOC points of mismatch — the dominant noise source | **High** | Tiered anchors; stdev 0.0101 → 0.0018 with zero windows lost |
| O | Changing the charge ceiling shifts η by 6.5 SOH-point equivalent | **High** | Automatic new baseline epoch on ceiling change |
| A1 | Balance ΔT penalised a fixed 2.4 °C thermal gradient | **High** | Baseline-relative scoring |
| A2 | Balance ΔV dominated by 0.1 V quantisation (1 LSB = 11 points) | **High** | Baseline-relative, span widened past quantisation |
| N | Seasonal term availability stepped the composite | Medium | Sub-scores held ≤ 90 days, reported in `held_terms` |
| F | A single near-zero power blip ended a segment | Medium | Only charging ends a segment; 6 h idle cap |
| C | Segments could open on carried-forward counter values | Medium | `is_stale` flag; refuse to open |
| D | Forecast age measured from first observation, not install | Medium | `bh_install_date` option; `battery_age_source` reported |
| E | Persistence under-triggered; efficiency baseline losable | Medium | `dirty` on all material state changes |

## 4. Safety properties (re-verified, unchanged)

* **Read-only:** no register writes added. The one new register
  (`storage_charging_cutoff_capacity`, 47081) is read-only here; the writable
  end-of-charge entity already existed separately. Verified by
  `TestReadOnlyGuarantee`.
* **Fault isolation (v1.1.7 contract):** all 18 structural tests pass
  unchanged — setup still never awaits battery-health work, every call site
  remains guarded, the kill switch still short-circuits first.
* **Bounded resources:** every new series is a bounded `deque` or a bounded
  epoch list. No new tasks, threads, or I/O paths.
* **New external dependency is optional and defensive:** the ambient
  temperature entity is read through `hass.states.get`, never blocks, and
  degrades silently when missing, renamed, or unavailable.

## 5. Recalibration safety

Three baselines now exist (capacity, balance, efficiency). All follow one rule:

> **Append an epoch; never overwrite. Never touch raw data.**

* Raw ΔV/ΔT, raw thermal rise, and the raw capacity estimate are always
  exposed and are never re-zeroed by any recalibration.
* Every baseline operation appends to an epoch list retaining the previous
  value, and logs at **WARNING** — silent recalibration would be the ICS
  finding here.
* Capacity re-anchoring **refuses** on thin data (segment count *and* time
  span) so a reference cannot be captured from noise or from one season.
* The capacity re-anchor button is **disabled by default**: it redefines what
  100% means and is correct only after hardware replacement.

The documented residual: re-anchoring *after* genuine degradation will hide
that degradation in the score. The raw series and epoch history are the audit
trail, and this is stated in both the entity docs and BATTERY_HEALTH.md.

## 6. Test evidence

* **368 passed, 1 skipped, 0 failed**, deterministic across repeated runs.
* **Adversarial verification (required practice since v1.1.7):** the suite run
  against the pristine v1.1.8 tree fails **80 tests**. The tests are
  load-bearing, not tautological.
* **Two real bugs were caught by the new tests during development**, which is
  the strongest evidence they work: a falsy-zero install timestamp
  (`0.0 or fallback` silently discarded an epoch-origin install date), and a
  winter-biased capacity reference that left SOH reading 103.8% on real data.
* Tests asserting superseded designs (absolute balance thresholds, gap
  discarding) were **replaced, not deleted** — assertion counts in both areas
  increased.
* Static: all Python files parse clean; all JSON valid; `manifest.json` = 1.2.0.

## 7. Confidentiality review

Performed at the operator's request before packaging:

* No serial numbers, hostnames, IP addresses, locations, or personal
  identifiers anywhere in the repository. The only IP literals are Huawei's
  documented AP address (192.168.200.1) and RFC-1918 values in unit tests.
* One inverter **model** reference in `AUDIT_1.1.7.md` was genericised.
* **No field data is shipped.** Aggregate statistics quoted in documentation
  (e.g. "162 segments, spread 0.31 kWh") carry no timestamps and no
  identifiers, and cannot be tied to an installation.
* `tests/FIELD_VALIDATION.md` documents the method and the source *series*, so
  the work is reproducible from a reader's own export without redistributing
  anyone's data.

## 8. Expected behaviour on upgrade

1. Persisted state from v1.1.5–v1.1.8 loads unchanged (schema version
   unaffected; new segment fields default).
2. **SOH capacity will read differently** once a measured reference is
   captured — 20 segments spanning 45 days. Until then it falls back to the
   nameplate, so on the reference installation expect ~110 (clipped) briefly,
   then settling near 100 once the reference lands.
3. **SOH balance will report nothing until 20 samples establish a baseline**,
   then sit near 100 for a stable installation — the fixed offset now cancels
   instead of scoring ~81.
4. Efficiency baseline should form in ~24 days rather than ~47, and reset once
   if the charge ceiling is changed.
5. `held_terms` will populate in winter as balance and efficiency go
   seasonally unavailable. That is correct behaviour, not a fault.

**Verdict:** release-ready. Eleven findings resolved, every constant derived
from measurement rather than assumption, safety and isolation properties
re-verified unchanged, recalibration made non-destructive by construction, and
the validation's own limits documented rather than glossed.
