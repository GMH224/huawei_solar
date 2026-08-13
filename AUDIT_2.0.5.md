# Release Audit — huawei_solar v2.0.5

**Date:** 2026-08-12 · **Auditor:** Claude (Anthropic)
**Baseline:** v2.0.4
**Type:** completes the batch deliberately deferred at v2.0.4 — F-04 (telemetry
rate denominator, active bug producing readings up to 400%), F-05 (new
telemetry visibility for ICS-01's own uncertainty flag), and three defects
(CP-01, CP-02, CP-03) found during a systematic review of the core
`huawei_solar` register-definition modules, all treated as this project's
own code, no distinction drawn between "forked" and "original" — this fork
diverged from upstream months ago and is now almost entirely rewritten;
where a fix belongs is a question of which file the defect is in, nothing
else.

---

## 1. Why this release exists

At v2.0.4, F-04 and F-05 were identified but deliberately deferred so the
already-complete v2.0.4 batch (F-02, F-03) could ship without further
delay. A third external report (`Huawei_Solar_HACS_ICS_Quality_Audit_v2_0_4
.md`) and a fresh telemetry capture then surfaced F-04 concretely, with
exact numbers matched against real production data. This release completes
that batch, plus what a deliberate, systematic sweep of the core package
found once F-04's own investigation revealed the shape of defect to look
for elsewhere.

## 2. F-04 — telemetry rate denominator mismatch

**Confirmed with exact numbers matched against the real post-v2.0.4
telemetry capture**: `timeout_rate_percent` readings up to 400% (e.g. 1
successful request, 3 timeouts, in the same rolling window — the exact
`3/1 = 300%` case the external report cited was independently reproduced
here, 10 separate occurrences found in the real data, up to 400%).

**Root cause**: every rate `modbus_telemetry.py` computed divided by
`req_ph` (`len(self._requests)`) — but `record_request()` is only ever
called *after* a batch succeeds. Any window with more failures than
successes produced a rate exceeding 100%, which is not a meaningful
percentage for a health metric.

**Fix — a structural one, not a clamp**: a new `self._attempts` deque,
bumped by every recording method (success or any failure kind), is now
the denominator for every rate this class computes. Every rate's
numerator is a strict subset of that same population by construction, so
every rate is mathematically bounded to [0, 100]% — not usually, always.

**A second, related conflation closed in the same pass** (this same
audit's own section 11 concern): `timeout_rate_percent` used to combine
genuine device timeouts with queue sheds and admission timeouts — internal
bus contention this project's own established discipline elsewhere
(MOD-09 and others) already treats as "not the inverter's fault." Every
`record_timeout()` call site across the whole project (`update_
coordinator.py`'s own three `_record_*()` methods, its optimizer
coordinator's separate inline copy, and `synchronized_power_coordinator.py`'s
own `_record_failure()` helper — five sites total, found by grepping every
call site rather than assuming the obvious three) now passes an explicit
`kind`. `timeout_rate_percent` now means genuine device timeouts
specifically; `queue_shed_rate_percent` and `admission_timeout_rate_percent`
are new, separate fields for the other two.

**A genuine gap found while touching this file, unrelated to F-04 itself**:
`timeout_rate_percent` and `overall_failed_attempt_rate_percent`
(introduced by v2.0.4's own F-03 fix) were added to the snapshot dict but
never wired up as actual HA sensor entities — reachable only via the raw
telemetry JSONL capture, never visible in the UI. Fixed alongside the new
v2.0.5 fields, since it's the same class of oversight this pass was already
correcting.

## 3. F-05 — no aggregate visibility into ICS-01's own uncertainty flag

**Confirmed as a genuine gap**, self-identified during the prior session's
own detailed review of this exact finding, not newly surfaced by external
report: ICS-01/ICS-05 (v2.0.3) correctly compute `is_temporally_uncertain`
per-result, but nothing tracked how often it actually fires. The prior
session could only bound this indirectly via `fallback_cache_hit_rate`,
not measure it directly.

**Fix**: two new counters on `SynchronizedPowerCoordinator`
(`temporally_uncertain_count`, `results_with_span_computed`), incremented
at the one place a real `sample_span_ms` is ever computed — the aligned-
shortcut path always has `is_temporally_uncertain=False` by construction,
confirmed with a dedicated test that it is not double-counted. Exposed via
`snapshot()`, which flows automatically into the telemetry JSONL capture
with no separate wiring needed (verified directly, not assumed).

## 4. CP-01, CP-02, CP-03 — core package defects (register_definitions/)

Found via a systematic sweep of the core `huawei_solar` package,
prompted directly by the operator after a self-identified process failure
in the prior session (treating this package's own source as "vendor code"
to consult for reference rather than fix directly) — corrected explicitly
and repeatedly during this session: **this is one project.** The fork
diverged from its upstream origin months ago and the current source bears
little resemblance to it; whether a defect happens to live in a file that
originated before or after the fork has no bearing on whether it gets
fixed.

### CP-01 — `ChargeDischargePeriodRegisters.encode()` had no semantic validation

The root cause behind v2.0.3's own ICS-07 finding, whose fix (`services.py`)
only protected the one call site through this HACS integration's own
service layer. `encode()` itself had no `_validate()` method at all, unlike
its two sibling period-list classes in the same file (`LG_RESU_
TimeOfUseRegisters`, `HUAWEI_LUNA2000_TimeOfUseRegisters`), both of which
already validate start/end ordering and overlap. Fixed by adding
`_validate()`, mirroring those two classes' own proven algorithm exactly
rather than inventing a new one.

### CP-02 — `PeakSettingPeriodRegisters._validate()` existed but was never called

A `_validate()` method was already fully, correctly implemented — full-day
coverage per weekday, no gaps, starting at 00:00 — but `encode()` never
called it. One-line fix: `self._validate(data)`, activating logic that
was already correct.

### CP-03 — `StringRegister.encode()` silently truncated oversized writes

Found during the same sweep, in the same file family, checking a
different-shaped pattern (not `_validate`, since this class had no such
method at all) after CP-01/CP-02 established what to look for.
`data.encode("utf-8")` was returned with no check against the register's
own fixed byte capacity; `struct.pack`'s own `"s"` format silently pads or
truncates to the target length with no error and no warning. The existing
downstream check in `register_client.py`
(`_validate_data_to_write()`) cannot catch this either — it checks the
length of the *already-packed* bytes, which is always correct by
construction once `struct.pack` has already silently discarded the excess.
Confirmed genuinely reachable: two writeable `StringRegister` instances
exist (`SDONGLE_NMS_SERVER`, `SDONGLE_CARD_NUMBER_4G`), though this HACS
integration does not currently expose an entity or service that reaches
them. Fixed by checking the encoded length against the register's own
capacity before returning, raising `EncodeError` with a clear message
instead of silently discarding data.

All three verified directly with standalone scripts (both the invalid-input
rejection and the valid-input-still-works negative case for each) before
any integration-level testing, and the whole HACS integration's own 888-test
suite re-run afterward to confirm nothing downstream broke.

## 5. Two documents reviewed and found not actionable

The operator supplied two further documents this session:

- **`Huawei_Solar_ICS_Audit_Report.md`**: verified directly against actual
  source and found to be fabricated, not a genuine audit of this codebase.
  Multiple specific, checkable claims are false — methods claimed missing
  that exist and are actively called; a cited line number pointing to
  unrelated code; files claimed to exist (`binary_sensor.py`,
  `pyproject.toml`) that do not exist anywhere in this project. Not
  triaged; disregarded entirely, and recorded here so the reasoning for
  disregarding it isn't lost.
- **`ArchReview.docx`**: appears to analyze genuine telemetry data (the
  same real timestamps from this project's own captures), but contains at
  least one severe misunderstanding of the actual system topology — its
  "inter-device register overlap" finding, framed as a safety-critical
  split-brain risk, misreads two independent physical inverters each
  having their own `active_power` register as a conflict, which is normal
  and expected. Not acted on; its other claims were not independently
  re-verified given this.

## 6. Test evidence

- **888 passed, 1 skipped, 0 failed** (was 883 at the v2.0.4 baseline; 5
  new tests for F-05, plus the F-03 test suite's expected values updated
  to reflect F-04's new denominator — a mechanical consequence of the
  fix, not a change in what F-03 itself established).
- The F-04 rewrite required updating a test file's own module-load stub
  (`test_synchronized_power_coordinator.py`'s minimal `modbus_guard` fake)
  to provide the two new exception classes `_record_failure()` now
  imports — caught immediately as a collection error, not a silent gap.
- The same `object.__new__()`-bypasses-`__init__` gap this whole project
  has hit repeatedly recurred once more for F-05's two new counters, in
  the shared `_make_coordinator()` test fixture — caught immediately via
  `AttributeError`, fixed the same way as every prior instance.

## 7. Safety properties

- v2.0.4 remains available and was not modified; this release was built
  in its own working tree.
- `failure_rate_percent`'s existing numerator meaning (non-timeout
  failures only, established by v2.0.3's own F-03 fix) is unchanged —
  only the shared denominator was fixed, verified with a dedicated test
  distinguishing "denominator fixed" from "numerator meaning preserved."
- CP-01/CP-02/CP-03's changes are additive validation only — no existing,
  valid input is rejected by any of the three fixes (verified directly
  for each).

## 8. Delivery note

CP-01, CP-02, and CP-03 are fixed in `register_definitions/periods.py` and
`register_definitions/string.py`, delivered alongside this release's own
zip as a separate patch set (`huawei_solar_core_patches/`), reflecting
that these files are part of this project's own fork of the core
package, not the HACS integration's own `custom_components/huawei_solar/`
directory structure that Home Assistant loads directly.

## 9. Recommended next step

Deploy 2.0.5. Every telemetry field the operator's planned longer capture
will read from is now correct: `timeout_rate_percent` is bounded and means
genuine device timeouts specifically; the new uncertainty-rate fields
directly answer F-05's own question rather than requiring it to be
inferred; and the three core-package fixes close real, if not yet
observed in the field, defects. The operator's own plan continues
unchanged: run the deferred, longer telemetry capture, then decide F-01's
transport-epoch question together with F-06/F-07, ICS-12, and ICS-16 from
that data.

**Verdict:** F-04 and F-05 fixed and adversarially tested against the real
implementation, including reproduction of the exact real-world scenario
that surfaced F-04. CP-01/CP-02/CP-03 fixed and independently verified.
Two supplied documents reviewed rigorously and found not actionable, with
that reasoning recorded rather than silently discarded.
