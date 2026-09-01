# AUDIT — huawei_solar v2.1.0.0

**Status: IMPLEMENTATION COMPLETE, pending external audit.**

Built on the **2.0.14** codebase, per the standing agreement that the
entire 2.0.15.x line was throwaway instrumentation contributing evidence
only, no code. Implements `V2_1_ARCHITECTURE_DESIGN.md` §2, §3 and §4.

**Baseline (2.0.14, measured before any change):** 1,262 passed, 5
failed, 12 errored, 1 skipped.
**Final (2.1.0.0, from a fresh independent extraction of the delivered
zip):** **1,304 passed, 5 failed, 12 errored, 1 skipped.**

42 tests added; the 5 failures and 12 errors are the same pre-existing
ones documented since 2.0.7 — **zero regressions**. Manifest version
validated against Home Assistant's own `AwesomeVersion(...,
ensure_strategy=[...])` check before packaging (SIMPLEVER), not assumed:
the 2.0.15b incident, where an invalid version string blocked loading in
the field, is the reason this is now checked every release.

---

## 1. §2.1 — Stale-cache fallback applied to every failure branch

**Change.** The fallback body moved out of the `TimeoutError` branch of
`HuaweiSolarUpdateCoordinator._async_update_data()` into a shared
`_stale_cache_fallback()` helper, now invoked by all four failure
branches (`TimeoutError`, `ReadException`,
`ConnectionInterruptedException`, `HuaweiSolarException`).

**Why this was a defect, not a scoping choice.**
`V2_ARCHITECTURE_DESIGN.md` §8 already committed to `available`
following *"one rule, uniformly, across every entity platform … `True`
for both `GOOD` and `UNCERTAIN` … `False` only for `BAD`"*. A fallback
present in one branch of four violates that stated uniformity. Field
evidence confirms the other branches genuinely fire: `ServerDeviceBusyError`
(a `ReadException`) and connection interruptions both appear in the HA
log at the exact timestamps of real sensor dropouts.

**A correction made during implementation.** The design document's
initial reading — that these three branches blank an entity on a single
failure — was **wrong**, and was corrected by reading Home Assistant's
own `DataUpdateCoordinator` source rather than assuming. `self.data` is
only reassigned *inside* the `try`, so a raised exception leaves the
previous data intact. The real defect is subtler: HA notifies listeners
on the *first* transition into failure but returns early on every
subsequent consecutive failure, and `data_age_seconds` plus the
availability re-check are recomputed only on a listener notification.
During a *sustained* outage, reported staleness therefore **froze at its
first-failure value** and the designed 300 s/600 s ceilings never fired.
Returning a dict keeps `self.data` changing, so listeners keep firing and
the ceilings work as designed. The `TimeoutError` branch only ever got
this right incidentally, as a side effect of returning successfully.

**One deliberate exemption, added during implementation and not in the
design document.** `ILLEGAL_DATA_ADDRESS` still raises immediately with
no fallback. Every other failure means *"we could not reach the value"*;
this one means *"this register does not exist on this device"* — a
configuration error the operator is explicitly instructed to diagnose by
disabling sensors one at a time. Serving cached data would mask exactly
that signal, and no amount of waiting makes the register appear. This
judgment was made while implementing and is documented at the call site
rather than left silent.

**Tests (8).** Six behavioural against the real helper (empty cache →
`None` so genuine unavailability still works; partial cache serves the
available subset rather than failing all-or-nothing; no quality gate
applied on top of the cache's own policy; no leakage of unrequested
names), one structural (all four branches invoke it), one pinning the
`ILLEGAL_DATA_ADDRESS` exemption.

**An existing test was updated, not weakened.**
`test_fallback_policy_lives_in_the_cache_not_here` inspected a source
window that the refactor moved. Its `assertNotIn` (the real guard: no
GOOD-only gate reappearing) still passed; only its `assertIn` broke. It
now inspects the helper — where the policy actually lives — and carries
an explicit note telling a future maintainer to re-point it rather than
delete it, because the regression it guards against is still real.

---

## 2. §2.2 — Synchronized power: degrade, don't blank

**Field evidence drove the split.** The HA history export covering the
4-day window contains **2,951 `unknown`/`unavailable` transitions across
14 entities**, ~90 % of them on the four `*_synchronised` entities.
Breaking those down by *which* entities dropped out together:

| Entities failing simultaneously | Occurrences |
|---|---|
| 1 | 114 |
| 2 | 453 |
| 3 | 373 |
| 4 | 129 |

**940 of 1,069 events were partial**, following dependency chains
(`pv_power` failing takes `home_consumption` with it, since consumption
is derived from it). That is the signature of an individual read
returning `None` — not the temporal-alignment gate, which drops all four
at once and accounts for only 129. Both paths needed fixing; the first
is the dominant one.

**Half 1 — `_read_one()` UNCERTAIN fallback.** After a physical read
fails, an `UNCERTAIN`-quality cached value is served rather than `None`.
The `GOOD`-only gate at the *top* of the method is deliberately
untouched: it answers a different question (*should we skip a physical
read?*), where a high freshness bar is correct. This is the question
asked only after the read has already failed.

This does not reintroduce "silently wrong". The stale value's real age
flows into `_mark_success()` exactly as a cache hit's does, widening
`sample_span_ms`, which drives `is_temporally_uncertain`. A stale value
therefore either sits within the alignment tolerance (genuinely fine to
combine) or trips the uncertainty flag on its own merits. It is bounded:
`RegisterCache`'s own ceilings expire the entry, after which it is no
longer `UNCERTAIN` and a genuinely dead link still ends in unavailable.

**Half 2 — sensor entity degradation.** ICS-05 (v2.0.3) made a
temporally-misaligned composite *unavailable*, explicitly rejecting *"an
extra 'uncertain' attribute alongside a still-displayed value"*. That
reasoning was sound **for its time** — it predates the quality model
reaching the entity layer, so "carry the value and label it" was not an
available option. With `data_quality` / `data_quality_reason` /
`data_age_seconds` now implemented, the composite case can do what every
other sensor already does. The entity now shows the value with
`data_quality: UNCERTAIN`, `data_quality_reason: TEMPORAL_MISALIGNMENT`
and the measured span.

ICS-05's actual safety property is preserved: a value is never
*invented*. `_get_value()` returning `None` still blanks. Only "we have a
real composite number whose inputs were further apart in time than the
tolerance" degrades — and it degrades loudly. The legacy
`temporally_uncertain` attribute key is retained so existing dashboards
and automations do not break silently on upgrade.

**A coverage gap found.** Nothing tested the sensor entity's *reaction*
to the uncertainty flag — every existing test covered the coordinator
*computing* it. That is why a behavioural change of this size initially
passed the suite silently. Now covered (5 tests).

**Tests (10).** Five behavioural against the real coordinator (UNCERTAIN
served on read failure; stale value widens the measured span and trips
the uncertainty flag; `BAD` never served; no cache reference fails
cleanly; `GOOD` still short-circuits before any read), five on the entity
contract.

**Two of the author's own test expectations were wrong and were
corrected.** Two adversarial cases asserted a `None` result, but with a
single inverter and no other inputs, refusing to serve the value means
*every* read failed, so the coordinator correctly takes its documented
"if ALL reads fail" path and raises. The raise **is** the assertion — it
can only happen if the value was correctly withheld. Both were rewritten
to assert on the raise.

---

## 3. §4.2 — Firmware-change detection

**Change.** `AdaptiveModbusController.note_firmware_version()`, called
from `__init__.py` immediately after `async_load()` so the comparison
runs against the version persisted from the previous session. On a
genuine change it does two things: an aggressive one-off decay
(`ADAPTIVE_FIRMWARE_CHANGE_DECAY_FACTOR = 0.25`) of accumulated slot
statistics, and `mark_recovery()` to suppress learning while the device
stabilises.

**Why both.** Suppression alone would not help — the poisoned counts are
*already stored*. This module's own pre-existing comments quantify the
consequence: a ~1 h firmware outage produces ~120 consecutive failures
across four slots, lifting a mature slot from ~3 % to ~12 % failure rate
(~137 s polling instead of 20–30 s), and because `apply_decay()` scales
confidence and failures by the *same* factor, only fresh successes
dilute it — which now accrue 4–5× more slowly precisely because polling
has degraded. Its own conclusion: *"a single maintenance window
therefore costs weeks of degraded polling."* The mitigation existed but
was only ever called from HA's own start/stop hooks, never for the
inverter changing underneath a running HA.

**The 0.25 factor is an engineering choice, not an evidence-derived
value, and is documented as such in `const.py`.** No capture in this
project spans a firmware update, so there is nothing to fit it to.
Deliberately not 0.0: a full wipe drops confidence to zero and forces a
cold-start back-off, trading one degraded-polling failure mode for
another. **This is a legitimate target for external audit challenge.**

**Scope limit, stated plainly.** This handles *timing* changes only. A
firmware update that renumbers registers is a data-correctness problem
no timing adaptation can detect; the log message says so explicitly and
directs the operator to review the register map manually.

**Upgrade safety.** A pre-2.1.0.0 store has no `firmware_version` key.
Treating that as a change would discard 75 % of every existing
installation's learned statistics on upgrade. `None` means "no version
known" and records without decaying. Tested explicitly. A `None` version
reading is likewise never treated as a change — the version is read from
a register like any other and can legitimately be absent for a poll or
two.

**Tests (10).** Including the upgrade path, the `None`-version case,
persistence round-trip, and both mutation paths bumping `_generation`.

**Two real bugs in the author's own code, caught by the existing suite.**
(1) The new persisted field broke 11 tests that build controllers via
`object.__new__()` — the "`__new__` bypasses `__init__`" gap this
codebase's own comments call out repeatedly. Fixed with `getattr()` in
`_serialize()`. (2) `test_generation_increments_on_every_known_mutation_site`
caught that the first-observation branch set `_dirty = True` without
bumping `_generation` — a genuine ICS-02 race where a mutation landing
during an in-flight save is silently lost. That test did exactly its job.

---

## 4. §3 — Load-regime gap conditioning (the filter)

**Change.** A time-based EWMA (τ = `ADAPTIVE_LOAD_REGIME_TAU_S` = 1 h) of
bus occupancy from the shared per-endpoint `ModbusGuard`, with a latched
high/low regime and ±5 pp hysteresis. In the HIGH regime the derived gap
is multiplied by `ADAPTIVE_LOAD_REGIME_HIGH_GAP_FACTOR` (0.8); in LOW it
is untouched.

**Evidence for conditioning gap.** From the 4-day random-excitation
capture (module 11: `WELL_IDENTIFIED`, so the levers are separately
estimable for the first time in this project), pooled for `devdc46`
(18,326 night / 34,393 day bus events):

| Effective gap | Night error rate | Day error rate |
|---|---|---|
| 150–200 ms | 0.144 % | 0.345 % |
| 250–300 ms | 0.343 % | 0.649 % |
| 350–400 ms | 0.069 % | 0.707 % |
| 450–500 ms | 0.202 % | 0.938 % |
| 500–550 ms | 0.334 % | 1.722 % |

Flat at night; monotonic ~5× rise during the day. Because gap was drawn
**randomly and independently** in that capture, reverse causation is
excluded by construction.

**Evidence for NOT conditioning timeout or poll.** Timeout showed no
monotonic relationship with error rate in either regime (night
0.09–0.34 %, day 0.53–1.02 %, no trend). Corroborated independently by
module 17, which finds timeout has by far the lowest between/within
device-variance ratio (0.20, against 21.80 for gap). Two unrelated
analyses agreeing. Explicit tests pin that timeout, poll interval and
queue depth are **not** affected, so a future change cannot silently
broaden the conditioning.

**Direction is counterintuitive and evidence-derived.** A *larger* gap
correlated with a *higher* error rate during the day, so the correction
under load is to **tighten**, not widen.

**Why a time-based EWMA rather than per-sample.** This is called once
per poll, and poll interval is *itself* an adaptive parameter
(20–180 s). A fixed per-sample alpha would silently change the effective
smoothing horizon whenever poll rate changed — and poll rate correlates
with load, the very signal being smoothed. Weighting by elapsed
wall-clock time keeps the horizon fixed regardless.

**Why 1 h.** Module 19's measured error-rate autocorrelation decays from
0.42 at 1 h to ~0 by 4–5 h. Sub-hour reaction would chase noise the data
shows has no persistence.

**Why a threshold with hysteresis rather than a continuous function.**
Module 20 found **no reliable out-of-sample predictive structure** (R²
proxy 0.019–0.049), and this did **not** improve across 3× more holdout
data (351 windows vs 119). A finely-shaped response would be fitting
noise. The hysteresis band is an explicit engineering choice — module 6
could not derive a gap deadband from the excitation data (returns `NaN`;
gap moved in large discrete jumps rather than drifting, so chatter was
not measurable).

**Additive-first, verified rather than asserted.** One test deletes
`_load_regime_high` entirely, simulating the pre-2.1.0.0 path, and
confirms the LOW-regime gap is bit-identical. **A deployment that never
reaches high load sees no behavioural change from 2.0.14 at all.**

**Deliberately not persisted**, with a test pinning it: a 1–4 h EWMA
restored from an arbitrary time ago describes a bus state that may no
longer exist, and would apply a real gap correction on that basis.

**Tests (14).** Regime tracking (seeding, hysteresis in both directions,
smoothing rejects single spikes) and parameter effects (LOW is
bit-identical; HIGH tightens; envelope never exceeded even with
pathological slot statistics; timeout/poll/queue-depth unaffected).

**Same fixture bug as §3, caught again.** 39 tests broke on
`__new__`-built controllers missing the new fields. Fixed with
`getattr()` at both consumption sites — and here the `False` default is
semantically correct, not merely safe: no observed load should mean no
correction.

---

## 5. §4.1 — Device-join trigger: closed as ALREADY SATISFIED, not implemented

**This section is the one most in need of external scrutiny, because it
reverses a design decision on the basis of analysis done during
implementation.**

§4.1 specified three re-derivation triggers. Firmware change is §4.2
above. "Sustained regime shift" is precisely what the §3 filter already
is — the EWMA with hysteresis *is* the slower backstop for load changes;
a second mechanism would duplicate it. That leaves device join/leave.

**Finding 1: the guard already re-derives correctly on join and leave.**
Verified by direct execution against the real `ModbusGuard`, not
inspection: effective gap went 150 → 200 ms (DEV-A joins) → 450 ms
(DEV-B joins) → 200 ms (DEV-B leaves). Immediate and correct in 2.0.14,
unmodified.

**Finding 2: a device joining does not invalidate learned statistics.**
§1.3 of the design document established real cross-device contention.
Implementation analysis asked a sharper question — *which component* it
affects — and the answer changes the conclusion. Three progressively
harder tests, all on the 4-day capture:

| Test | Result |
|---|---|
| `dev8c0f` service vs. `devdc46` request *rate* in prior 60 s (30× range, not binary) | Flat: 7.8 → 6.9 ms median; p90 *decreases* with more activity |
| Same, stratified by day/night to rule out regime confounding | Day (n = 962 / 4,088 / 593): flat 6.7–7.3 ms |
| Reverse direction, using **error rate** — the outcome the learner actually gates on (n = 22,756 / 27,113 / 2,850) | Error rate *falls* with other-device activity (0.668 % → 0.035 %); service flat 6.7–7.4 ms |

**Mechanism, confirmed in source.** `t0` is set *inside* the
`guard.request()` block, after the wait completes, so the learner's RTT
measures device response time only. `ModbusGuard` tracks `wait_ms`
separately and the learner never sees it.

**Conclusion.** Cross-device contention is real (§1.3 stands: wait time
0 → 730 ms) but is **entirely a queueing effect**, already aggregated by
the guard and already re-derived on join/leave. It does not touch what
the learner has learned. Applying a firmware-style statistics decay on
device-join would **discard valid learning to correct for something that
is not wrong**, and would do so on every future device addition — making
the EV-charger scenario worse, not better.

No code was written for this trigger. The design document's reasoning
was sound in the abstract; the implementation investigation found the
mechanism already in place.

---

## 6. Scope explicitly NOT changed

- **The write path.** Unmeasured — zero writes occurred in the 4-day
  capture (verified two ways: no `entity_write`-labelled events among
  72,900 bus records, and `types.py::_guarded_write()` does route
  through `guard.request(label=...)`, so writes *would* appear; and no
  write activity in the HA log). Existing protections (fixed 15 s
  `WRITE_TIMEOUT` independent of the adaptive read timeout, plus
  `schedule_verify_write()` read-back verification) are sound by design,
  but **"sound by design" and "verified under load" are different
  claims** and only the first is made here.
- **`MIN_INTER_REQUEST_GAP` (150 ms).** A documented hardware constraint
  (SUN2000 Modbus FSM reset ≈ 100 ms), already tested at 30 ms and found
  to cause *"pervasive 0x06 `SLAVE_DEVICE_BUSY` responses on all SUN2000
  hardware"*. The guard clamps to it unconditionally.
- **The retry layer.** 8.91 % of transactions hit a busy response and
  **97.3 % are silently recovered** below the coordinator layer. It
  works, and its timing is not fully characterised (the `retries` field
  reads 0 for 97.6 % of busy-hit records, so recovery happens below the
  layer that field tracks). Changing a working, incompletely-understood
  mechanism is not justified by this evidence.
- **FAST-tier scheduling.** The apparent starvation asymmetry was
  retracted in the design document (§1.9) as a **labelling artefact**
  that survived two independent datasets before mechanism tracing
  overturned it. Actual read cadence is equivalent across devices
  (64.8 s vs 66.0 s median). No fix warranted; implementing one would
  add mechanism against a defect that does not exist.

---

## 7. Open questions for the external audit

1. **`ADAPTIVE_FIRMWARE_CHANGE_DECAY_FACTOR = 0.25`** — an engineering
   choice with no measurement behind it. Is the reasoning against 0.0
   (cold-start back-off) sound, and is 0.25 the right magnitude?
2. **§5's reversal of the device-join trigger.** The evidence is
   presented in full above specifically so it can be challenged. Is the
   wait-vs-service separation argument sound?
3. **`ADAPTIVE_LOAD_REGIME_HIGH_PCT = 40 %`** — derived from module 8's
   observed occupancy knee (~40–48 %), but the day/night split that
   produced the gap evidence is not *exactly* an occupancy threshold.
   The mapping is reasonable, not proven.
4. **The write path (§6).** Requires a capture containing real write
   traffic during a high-occupancy period. Until then its adequacy is
   assumed.
5. **Behaviour with three or more devices on one bus.** All contention
   evidence is two-device; extrapolation is not evidence.
6. **Startup/reload behaviour.** Every analysis applies a 2 h warm-up
   exclusion, so startup is deliberately unexamined — even though
   "sensors going unknown at startup" falls within §2's own priority.

---

## 8. Verification performed

- Every `.py` file compiles; `strings.json`, `translations/en.json`,
  `manifest.json` and `services.yaml` all parse.
- Manifest version checked against Home Assistant's own
  `ensure_strategy` validation, not assumed.
- Full suite run from a **fresh, independent extraction of the delivered
  zip**: 1,304 passed / 5 failed / 12 errored / 1 skipped — identical to
  the working tree and matching the 2.0.14 baseline's failure set
  exactly.
- Each of the four implemented changes was run against the full suite
  individually before the next was started, so any regression is
  attributable to a specific change rather than to the release as a
  whole.

## 9. Honest confidence statement

**High** — §2.1 and §2.2. Both root causes were read from source, one
design-document claim was corrected against HA's own source during
implementation, and the fixes reuse mechanisms already working here.

**Good** — §3's *shape*. Conditioning gap but not timeout is supported
by two independent analyses that agree, on well-identified data, and
LOW-regime behaviour is bit-identical to 2.0.14 by construction.

**Deliberately bounded** — §3's *tuning*. Module 20 says plainly that
out-of-sample structure is weak, so precise optimality is not achievable
from this evidence and is not claimed. The target is "clearly better
than 2.0.14, and safe" — not optimal.

**Stated as unknown** — everything in §7, particularly the write path.

The most useful thing this implementation pass did was **decline to
build something the design document specified** (§5), on evidence
gathered while implementing it. Consistency across datasets is not
correctness — the same standard that retracted the FAST-tier finding in
the design document applies to everything above.
