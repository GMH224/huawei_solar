# Release Audit — huawei_solar v1.2.2

**Date:** 2026-07-26 · **Auditor:** Claude (Anthropic)
**Baseline:** v1.2.1 (382 passed / 1 skipped)
**Scope:** `adaptive_modbus.py`, `switch.py`, `__init__.py`, `const.py`,
`manifest.json`, `tests/`, documentation.
**Type:** robustness release. No measurement logic changed — verified by
replaying the 6-month field dataset and obtaining identical results.

---

## 1. Origin

Raised by the operator after reviewing v1.2.1: the battery-health engine now
has a maintenance inhibit, but **the Modbus layer also learns**, and Home
Assistant's own start-up behaviour is not trustworthy enough to learn from.
Their framing — HA is "more like a hobby device than an ICS" during start-up —
is the correct calibration, and led directly to this release.

## 2. Finding — the adaptive learner cannot attribute cause

**Severity: High.** Latent since the adaptive controller was introduced.

`record_request(rtt_ms, success, timeout)` carries no notion of *why* a request
was slow or failed. An RTT inflated by Home Assistant's event-loop congestion
is recorded identically to a genuinely slow inverter. That directly violates
the module's founding premise — that failure patterns cluster at times of day
reflecting **inverter** state transitions.

Three properties make this materially worse than a poisoned health number:

| Property | Consequence |
|---|---|
| **Blast radius** | Poisoned parameters change *real* polling behaviour (interval 20–180 s, timeout 15–60 s, gap 150–500 ms), degrading the data collection everything else depends on |
| **Ratchet** | Failures push toward slower polling → fewer observations per slot per day → slower recovery. Descent fast, recovery slow |
| **Temporal clustering** | Restarts are not uniformly distributed: scheduled updates and evening maintenance hit the same circadian slots repeatedly, refreshing the poison faster than decay removes it |

### 2.1 The dominant exposure is planned maintenance, not restarts

A Huawei firmware update leaves the inverter unreachable for about an hour. At
30 s polling that is **~120 consecutive failed requests** across **four
15-minute slots**. Applied to a mature slot (n ≈ 300, ~3 % failures):

| | Before | After one update |
|---|---|---|
| Failure rate | ~3 % | **~12 %** |
| Derived poll interval | 20–30 s | **~137 s** |

Four updates a year, clustered in business-hours slots, would leave those slots
degraded for a substantial fraction of the year.

### 2.2 Daily decay does **not** repair it

This is the non-obvious part, and the reason the guard is necessary rather than
merely tidy. Decay multiplies `n` **and** `failures` by the same factor, so it
lowers *confidence* but leaves the *ratio* untouched. Only new **successful**
observations dilute a poisoned failure rate — and those accrue 4–5× more slowly
precisely because polling has slowed.

Pinned by `test_decay_does_not_repair_a_poisoned_failure_rate`: a fortnight of
decay applied to a poisoned slot leaves the failure rate unchanged to six
decimal places.

## 3. Resolution

| Layer | Covers | Mechanism |
|---|---|---|
| Manual inhibit | planned maintenance | `Adaptive learning` switch, persisted, governs **both** learners |
| HA start-up gate | event-loop congestion | suppressed until `EVENT_HOMEASSISTANT_STARTED`, then a settling period |
| HA shutdown gate | teardown-order artefacts | suppressed on `EVENT_HOMEASSISTANT_STOP` |

**Suppression is total, not down-weighted.** A weight would be another
unvalidated constant; "recorded or not" is directly verifiable, and blocked
observations are counted and surfaced rather than silently dropped.

**Polling is never stopped** — only *learning from what is observed*. During
suppression the controller continues to apply its existing parameters.

### 3.1 Gates keyed on HA's lifecycle, not integration setup

Integration setup routinely completes while HA is still working through
recorder migration and other integrations. A window measured from setup could
therefore expire *before* the congestion does, defeating its purpose. The gates
key on `EVENT_HOMEASSISTANT_STARTED` / `EVENT_HOMEASSISTANT_STOP`, with a
fallback to immediate settling when the integration is (re)loaded into an
already-running HA.

### 3.2 Deliberate asymmetry between the two learners

One control for the operator; different automatic reflexes underneath:

| Trigger | Battery health | Adaptive Modbus |
|---|---|---|
| HA start-up / shutdown | suppress | suppress |
| Manual switch | suppress | suppress |
| **Coordinator recovery** | **suppress** | **keep recording** |
| Counter reset | suppress | — |

The battery-health engine suppresses on coordinator recovery because stale
register *values* would corrupt it. The Modbus controller does **not**, because
Modbus *timing* is precisely what it exists to measure — a recovering link is
genuine signal, not noise. Suppressing there would blind it to its own purpose.
This asymmetry is documented in the code, not just here.

## 4. Safety properties (re-verified)

* **Read-only:** unchanged. The switch mutates only local controller state and
  the local Store.
* **Fault isolation (v1.1.7 contract):** all 30 structural and entity-contract
  tests pass unchanged. Switch construction remains wrapped so it cannot abort
  the switch platform; the new gate registration is itself wrapped so it can
  never break entry setup.
* **Register set:** unchanged — golden-list test passes untouched.
* **Bounded resources:** three scalars and two counters added; no new
  collections, tasks, or I/O paths.
* **Persistence:** gate state is persisted for both learners, so an inhibit set
  before maintenance survives the restart that maintenance usually involves.

## 5. Test evidence

* **391 passed, 1 skipped, 0 failed**, deterministic across repeated runs.
* **Adversarial verification:** 6 tests fail against the pristine v1.2.1 tree.
* **Control case included.**
  `test_unguarded_firmware_update_would_have_poisoned_the_slot` runs the same
  scenario with the guard *disabled* and asserts the slot **is** poisoned
  (failure rate > 3× healthy, > 10 %). Without it, the positive test would pass
  even if the scenario were incapable of causing harm.
* **Regression check:** replaying the 6-month field dataset produces results
  identical to v1.2.1 (162 segments, reference 22.59 kWh, SOH cap 100.7, BHI
  100.4).
* Static: all Python files parse clean; all JSON valid; `manifest.json` = 1.2.2.

## 6. Test-harness finding

`test_entities.py` maintained a hand-written stub of `huawei_solar.const`
listing individual constants. As `adaptive_modbus` imported more of them, the
stub drifted and broke collection. It now loads the **real** `const.py` by
path. Parallel constant lists in test harnesses are a maintenance trap; this
removes one.

## 7. Operational notes

* The switch is renamed but its `unique_id` is unchanged, so existing entity
  registry entries and any automations referencing it survive.
* `suppressed_observations` on the adaptive diagnostic sensors is the audit
  trail: it should stay 0 in steady operation, rise during start-up windows,
  and rise sharply during a maintenance window with the switch off — which is
  the guard working.
* Recommended workflow: switch **off** before a firmware update, **on** once
  the system is stable. Re-enabling deliberately triggers a settling period, so
  flipping it immediately after an update is still safe.

**Verdict:** release-ready. A latent path from routine scheduled maintenance to
weeks of silently degraded polling is closed by three layers; the guard is
proven load-bearing by an explicit control case; the asymmetry between the two
learners is deliberate and documented; and measurement behaviour is unchanged
against real field data.
