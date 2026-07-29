# Release Audit — huawei_solar v1.3.1 (Phase 0 instrumentation fixes)

**Date:** 2026-07-28 · **Auditor:** Claude (Anthropic)
**Baseline:** v1.3.0 · **Rollback target:** v1.2.4
**Type:** instrumentation correctness. No behavioural change to polling,
adaptation or scheduling.

---

## 1. Why this release exists

Both defects were found by reading the **first real capture file**. Neither was
visible in 433 passing tests. That is the recurring lesson of this project, in
a new place: the tests asserted the properties I thought to check, and the
defects lived in the properties I did not.

---

## 2. Defect 1 — serial numbers leaked into the capture

**Severity: High (confidentiality).**

Coordinator names are constructed as
`f"{device.serial_number}_..._update_coordinator"`. The guard was passed
`coordinator.name` verbatim, so **every record contained a real serial number**:

```
{"src":"<SERIAL>_battery_data_update_coordinator", ...}
```

This directly contradicted AUDIT_1.3.0 §4, which stated that records carry "a
coordinator name … which contains no identifiers", and the module's own
docstring. The existing test asserted only that the **endpoint** was absent —
so the leak passed.

The design intent was right (endpoint pseudonymised, salted hash, explicit
confidentiality section). The failure was assuming a field was safe without
checking what actually went into it.

**Fix.** `sanitise_label()` pseudonymises any underscore-separated token
containing a run of 6+ digits, applied **inside `record()`** rather than at the
call site — so every future caller is protected by default rather than having
to know. Distinct inverters remain distinguishable (`dev8c0f_` vs `devdc46_`)
and the diagnostically useful part of the label survives.

**A second-order note worth recording:** the first sanitiser used an anchored
regex (`\b[A-Z]{2,}\d{6,}...\b`) and **silently matched nothing** — `_` is a
word character, so `\b` never fires at the digit/underscore boundary. It was
caught by testing against the real leaked string rather than a constructed one.
Pinned by `test_serial_survives_no_word_boundary`.

**Operator impact:** the capture already produced contains real serials and
should be treated as identifying.

---

## 3. Defect 2 — `regs` and `prio` never populated

**Severity: Medium (diagnostic blind spot).**

Both fields existed in the record schema and were written as `null` for all 400
records, because nothing set them. The consequence is that a stall could not be
correlated with *what was being read* — which is exactly the question that
follows the wait/service split.

**Fix.** The guard's request context now exposes `registers` and
`priority_tier`, set by the coordinator per chunk.
`register_cache.classify_register()` is a new public wrapper so the tier can be
read without reaching into a private helper.

---

## 4. Findings from the first capture — these change the roadmap

400 records over 2.5 h, both inverters, one shared bus.

| | median | p90 | p99 | max |
|---|---|---|---|---|
| `wait_ms` | **0.0** | 0.1 | 8,500 | 10,405 |
| `service_ms` | **61.7** | 9,780 | 27,291 | 32,786 |

* **Service time is 90% of all elapsed request time.**
* The queue is empty in **371 of 400** records (`qd = 0`).
* Median wait is **zero**.
* Single exchanges reach **33 seconds**.
* Worst offender: `battery_data_update_coordinator` — median 173 ms but p95
  **20.7 s**. Master accumulates 885 s of service vs the slave's 273 s.

**Mechanism (b) is confirmed: the device is slow, not our queueing.**

### 4.1 A correction to an earlier conclusion

This vindicates the `rtt_p95_ms ≈ 12000` figure that AUDIT_1.2.4 dismissed as a
post-migration relearning transient. It was real. The subsequent claim that
per-chunk RTT was "sub-second" — inferred from settled-slot gap values rather
than measured — was wrong. Direct measurement overturned an inference that had
already been used to draw conclusions twice.

### 4.2 Consequence for the roadmap

* **Phase 3 (occupancy-based admission control) is largely unnecessary.** Its
  premise was that demand exceeds what the bus can serve, causing queue
  build-up. There is no queue. Throttling demand cannot speed up a device that
  intermittently takes 30 s to answer.
* **Phase 2 (priority ordering) loses most of its value.** Priority reorders a
  queue; with `qd = 0` in 93% of records there is nothing to reorder. It would
  help only in the ~1% tail, and only by choosing which request stalls.
* **The open question has moved** to *which registers or access patterns
  trigger multi-second stalls*. The next capture can answer this now that
  `regs` and `prio` are populated.

`DESIGN_bus_scheduler.md` should be revised before any Phase 1 work: its core
assumption did not survive contact with measurement. Better established now
than after building a scheduler.

---

## 5. Safety and verification

* **No behavioural change.** Poll intervals, gap, timeout, queue depth,
  adaptation and scheduling are untouched; the full pre-existing suite passes.
* **No register writes** introduced.
* **Instrumentation cannot break I/O:** the tier lookup is exception-guarded
  and falls back to `"unknown"`; a failing sink is already covered by
  `test_failing_sink_never_breaks_modbus_io`.
* **Storage untouched** — no `Store` version change.
* **Tests: 440 passed, 1 skipped, 0 failed**, deterministic.
* **Adversarial verification:** against the shipped v1.3.0 tree, **7 of 7** new
  tests fail — five confidentiality, two on the unpopulated fields.
* Battery-health replay against the 6-month dataset: unchanged.

---

## 6. Process finding

The confidentiality property was *designed for*, *documented*, *asserted in an
audit* — and still violated, because the test checked one field and the leak
was in another. Two rules follow:

1. **Test the property against real artefacts, not constructed ones.** The
   regex bug was found only by running the sanitiser against the actual leaked
   string from the field capture.
2. **Enforce invariants at the narrowest point.** Sanitising inside `record()`
   protects every caller; sanitising at the call site protects only the callers
   that remember.

---

## 7. Operating instructions for the next capture

1. Deploy v1.3.1 and re-enable `Modbus diagnostic capture`.
2. Run across a full day/night cycle including dawn and dusk.
3. `regs` and `prio` will now be populated — the data needed to identify which
   reads stall.
4. The previous capture file contains serials; delete or handle it accordingly.

**Verdict:** release-ready. Two instrumentation defects fixed, the
confidentiality guarantee now enforced where it cannot be bypassed, and the
first measurement has already invalidated the central assumption of the
scheduler design — which is exactly what Phase 0 was for.
