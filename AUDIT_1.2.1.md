# Release Audit — huawei_solar v1.2.1

**Date:** 2026-07-26 · **Auditor:** Claude (Anthropic)
**Baseline:** v1.2.0 (368 passed / 1 skipped)
**Scope:** `battery_health.py`, `battery_health_manager.py`, `switch.py`,
`manifest.json`, `tests/`, documentation.
**Type:** robustness release. No measurement formula changed — verified by
replaying the 6-month field dataset and obtaining identical results.

---

## 1. Origin

Raised by the operator, not by testing: Huawei firmware updates run about an
hour, four times a year, and the vendor publishes nothing about which
registers remain meaningful during the cycle. Their empirical assessment —
worth recording because it shaped the response — is that the hardware is never
harmed and all values are correct once the cycle completes, but not every
sensor is trustworthy mid-flight. A residential inverter is not held to
avionics-grade reset sequencing, and it would be unreasonable to expect it.

The correct engineering posture is therefore not to model the vendor's reboot
sequence, but to ensure nothing **irreversible** happens on data that may be
untrustworthy.

## 2. Finding — a reboot could destroy learned baselines

**Severity: High.** Latent in v1.2.0; would have manifested at the next
firmware cycle.

v1.2.0 made a charge-ceiling change a *destructive* signal: it restarts the
efficiency and balance baseline epochs, because the ceiling shifts η and the
SOC operating band systematically (Finding O, measured at 6.5 SOH-point
equivalent). That design was correct, but the input was validated only as
0–100%. A reboot returning **0** was therefore accepted as a legitimate
setting change and would have wiped baselines representing weeks of
accumulation — up to four times a year.

This is a good example of a guard introduced in one release creating exposure
in another: Finding O protected against a *real* ceiling change corrupting a
baseline, and in doing so created a path where a *spurious* one could.

## 3. Resolution — three independent layers

| Layer | Covers | Mechanism |
|---|---|---|
| Ceiling validation | garbage register reads | plausibility floor (20%) + 3-sample debounce |
| Maintenance inhibit | **planned** work | user-controlled switch, persisted |
| Settling period | **unplanned** reboots | automatic 5 min after any recovery |

Deliberately layered: the switch cannot cover a 3 a.m. reboot, and the
settling period cannot know a firmware update is about to start. Ceiling
validation is independent of both.

**Invariant in all three:** measurement and display continue; only
irreversible operations (segment recording, baseline capture, epoch changes)
are suspended. A frozen learning phase costs a day of data; a poisoned
baseline costs months.

## 4. Second finding — thermal-rise baseline confounded by load history

Reported from the operator's first v1.2.0 deployment:
`thermal_rise_baseline_max: 7.47` against a current reading of 5.09.

Analysis of **48 undisturbed rest windows** in the existing field data
measured pack cooling at roughly **−0.4 °C/hour**:

| Time after charging stops | Δ pack temperature |
|---|---|
| +30 min | −0.01 °C |
| +60 min | −0.29 °C |
| +120 min | −0.86 °C |

**My initially proposed fix was wrong and was discarded.** I had suggested a
short settling period; the measured time constant is *hours*, so no practical
settling delay would help. Twenty consecutive samples from a single afternoon
encode that afternoon's load history regardless of spacing.

The correct fix is a **multi-day span requirement** — the same lesson as
Finding J (capacity reference) applied at a shorter timescale. Thermal rise is
still measured and displayed immediately; only the baseline defers.

Recording this because the diagnostic value is in the correction: a plausible
fix was proposed, measured against real data, found not to address the
mechanism, and replaced.

## 5. Safety properties (re-verified)

* **Read-only:** unchanged. The new switch mutates only local engine state and
  the local Store; it is registered **outside** the parameter-configuration
  gate precisely because that gate guards register-writing entities.
  `TestReadOnlyGuarantee` passes unchanged.
* **Fault isolation (v1.1.7 contract):** all 18 structural tests pass. Switch
  registration is wrapped in try/except so it cannot abort the switch platform.
* **Register set:** unchanged from v1.2.0 — golden-list test passes untouched.
* **Bounded resources:** no new unbounded state. `CeilingMonitor` holds three
  scalars.
* **Persistence:** learning state and ceiling state are persisted, so a
  maintenance inhibit survives an HA restart — important, since a restart is
  likely *during* the maintenance it was set for.

## 6. Test evidence

* **382 passed, 1 skipped, 0 failed**, deterministic across repeated runs.
* **Adversarial verification:** 15 tests fail against the pristine v1.2.0 tree.
* **Regression check:** replaying the 6-month field dataset through v1.2.1
  produces results **identical** to v1.2.0 (162 segments, 22.75 kWh estimate,
  reference 22.59 kWh, SOH cap 100.7, BHI 100.4) — confirming this release
  changes robustness, not measurement.
* T28 includes an **end-to-end reboot-glitch scenario**: establish a baseline,
  feed five polls of `ceiling = 0`, assert the baseline survives and the
  rejection counter increments.
* One test scenario was found to be wrong during development
  (`test_no_learning_during_settling` drove a 21-minute discharge through a
  5-minute settling window, so learning correctly resumed part-way). The test
  was corrected, not the code.

## 7. Operational notes

* `learning_enabled`, `learning_active`, `settling_events` and
  `ceiling_rejected_readings` are exposed in the Battery health index
  attributes, so a maintenance window can be audited after the fact.
* Re-enabling learning deliberately triggers a settling period, so a user who
  flips the switch immediately after a firmware update still gets protection.
* Expected on the reporting installation: `ceiling_rejected_readings` should
  remain 0 in normal operation and may increment during the next firmware
  cycle — which would be the guard working, not a fault.

**Verdict:** release-ready. A latent high-severity path from a routine
maintenance event to destroyed baselines is closed by three independent
layers; a second confounded baseline is corrected with a measured constant
rather than a guessed one; measurement behaviour is provably unchanged against
real field data.
