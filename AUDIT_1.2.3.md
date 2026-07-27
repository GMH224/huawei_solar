# Release Audit — huawei_solar v1.2.3

**Date:** 2026-07-27 · **Auditor:** Claude (Anthropic)
**Baseline:** v1.2.2 (391 passed / 1 skipped)
**Scope:** `update_coordinator.py`, `adaptive_modbus.py`, `modbus_guard.py`,
`const.py`, `manifest.json`, `tests/`, documentation.
**Type:** measurement-correctness release for the adaptive Modbus controller.
No change to the battery-health engine — verified unchanged by replay.

---

## 1. Origin and evidence

An operator-supplied defect report, backed by two months of adaptive sensor
history (gap, timeout, poll interval, confidence; 11,113 aligned samples).
Unlike previous releases, the primary evidence here is **field data**, and the
code review was used to confirm the mechanism rather than to find it.

The report identified five observable problems tracing to three defects. This
audit confirms all three, adds a fourth found during review, and corrects the
report in two places.

## 2. Defect A — batch total consumed as a per-request RTT

**Severity: High.** Present since the BUG-10 fix. Defeats the controller's
purpose: it pins gap and timeout near their ceilings regardless of actual
inverter health.

`_execute_batch()` accumulated every chunk's round trip into `total_rtt_ms`
and returned it; `record_request()` stored it as one Modbus exchange.
`_derive_params()` then computed `gap = rtt x 0.4` and
`timeout = rtt / 1000 x 5` from a number inflated by the chunk count.

### 2.1 Confirmation from field data

| Metric | Observed |
|---|---|
| Gap at 500 ms ceiling (time-weighted) | 84.1% (87.7% at full confidence) |
| Timeout at 60 s ceiling | 42.2% |
| Timeout >= poll interval | 11.3% (worst: 60 s timeout vs 20 s poll) |
| Poll at 20 s floor AND gap >= 480 ms, at 100% confidence | 1,071 samples |

The two parameters saturate at **different** RTT thresholds (gap at
>= 1250 ms, timeout at >= 12000 ms), which allows `rtt_p95_ms` to be bounded
without observing it directly: a stored value above **twelve seconds** for
~42% of the window. That is not a physically possible single Modbus TCP
exchange, and is decisive independent of any modelling assumption.

### 2.2 Two corrections to the report

1. **The report's figures understate the problem.** It quoted 28%
   gap-ceiling and 2.3% timeout>=poll from *event counts*. A pinned value
   stops emitting state changes, so event counting systematically
   under-represents the very condition being measured. Time-weighting the same
   export gives 84% and 11.3%.
2. **`total_rtt_ms` does not include inter-chunk pauses or BUSY-retry waits.**
   `t0` resets inside each chunk after the guard's gap wait; the inter-chunk
   sleep precedes it; BUSY retries `continue` without accumulating. The
   inflation is purely chunk summation — which rules out pause overhead as a
   partial explanation and confirms chunk count is the whole mechanism.

### 2.3 Resolution, and a rejected option

`_execute_batch()` returns `max_chunk_rtt_ms`; `total_batch_ms` and
`chunk_count` are retained for diagnostics.

**Max, not mean.** `effective_timeout` is applied *per chunk*, so the value
driving it must cover the slowest chunk in a cycle. The report favoured mean
on a docstring reading; the code structure decides it.

**The report's Option C was rejected as specified.** It proposed per-chunk
`record_request()` calls. Successes are recorded once per poll, failures once
per poll (`update_coordinator.py` lines 343, 351, 582). Making successes
per-chunk while failures stayed per-poll would deflate `failure_rate` by the
chunk count — a genuine 15% failure rate reporting as ~3% — making the
controller *most permissive when the bus is sickest*, and inverting the
safety property. It would also breach `ADAPTIVE_FULL_CONFIDENCE_N` ~5x faster
and invalidate the 0.85/day decay tuning.

Option C's actual goal — stopping the conflation of two quantities — is
achieved by naming and returning them separately while keeping the
**observation unit at one poll**.

### 2.4 Migration (absent from the report)

`rtt_samples` is persisted and **FIFO-trimmed, not time-windowed**. Post-fix
per-chunk samples would have been mixed with pre-fix batch-summed ones, and
the old inflated values would have dominated the P95 for weeks — the fix would
have appeared not to work.

Storage version 1 -> 2 discards `rtt_samples` / `rtt_p95_ms` only. Failure and
timeout counts, slot occupancy and decay dates are scale-independent and
represent months of learning; they are preserved. Deliberately narrow.

## 3. Defect D (new) — shed requests recorded as inverter timeouts

**Severity: High**, and a blocker for Defect B.

`ModbusGuard` raised a bare `asyncio.TimeoutError` on queue-full shedding.
The coordinator's handler called `_record_timeout()` ->
`record_request(success=False, timeout=True)`. Contention among our own
sub-coordinators was therefore recorded in the circadian model as *inverter*
misbehaviour — the same class of misattribution as Defect A.

**The feedback loop:** shedding becomes more likely as `max_queue_depth`
falls, and `max_queue_depth` falls as the failure rate rises. Recording sheds
as failures closes the loop: shed -> failure -> higher failure rate -> lower
depth -> more shedding. Defect B's cold-start blending would have *triggered*
it, which is why B could not ship independently.

**Resolution.** `ModbusQueueShed(asyncio.TimeoutError)`. Subclassing preserves
every existing `except asyncio.TimeoutError` path — back-off, stale-cache
fallback, entity availability all behave exactly as before. Discrimination
happens *inside* the existing handler rather than in a parallel branch, so no
downstream fallback logic is duplicated (an earlier draft that added a
separate `except` clause was discarded for skipping the stale-cache path).

Sheds still reach telemetry, where "we shed a request" is legitimately useful,
and still advance the consecutive-failure counters. They no longer reach the
learner.

## 4. Defect B — queue depth had no cold-start blending

`TimeSlotStats.failure_rate` returns `0.0` when `n < 1`, and the queue-depth
block had no confidence term, so a slot with **zero observations** received
`max_queue_depth = 3`, the most permissive value — the precise case the
blending strategy exists to protect.

Resolved with the same blend as the other three outputs.

**Baseline is 2, not the report's 1.** Queue depth creates no concurrency —
`ModbusGuard` holds a single `asyncio.Lock` — it only bounds how many callers
may wait before shedding. With up to five sub-coordinators per inverter and
more than one inverter on a shared bus, a depth of 1 sheds aggressively on
exactly the unproven slots being protected. With Defect D fixed that no longer
poisons the model, but it still degrades availability for no benefit.

## 5. Why A, B and D ship together, and C does not

A acts at **high** confidence (where 87.7% of the saturation occurs); B acts
at **low** confidence, on unproven slots. Their domains are nearly disjoint,
so post-deployment effects remain attributable by segmenting on confidence.
D is a prerequisite for B, not an independent behavioural change.

Defect C alters the **shared** resource both inverters contend for and would
confound all of the above. It also cannot yet be validated: only one
inverter's sensor history is available. Deferred by agreement, pending both
inverters' exports.

## 6. Instrumentation

`rtt_p95_ms`, `last_batch_ms`, `last_chunk_count` and `shed_count` are now
exposed on the adaptive diagnostic sensor. This audit had to **back-solve**
`rtt_p95_ms` from saturation thresholds because it was not observable; that
should not be necessary twice. `last_chunk_count` also yields the true
inflation factor, which the analysis could only bound.

## 7. Safety properties

* **Read-only:** unchanged; no register writes added.
* **Fault isolation (v1.1.7) and learning gate (v1.2.2):** all structural
  tests pass unchanged.
* **Battery-health engine:** untouched. Replay of the 6-month field dataset is
  byte-identical to v1.2.2.
* **Bounded resources:** four scalar counters added.
* **Failure semantics preserved:** back-off, stale-cache fallback and entity
  availability behave identically for shed and timeout; only learning differs.

## 8. Test evidence

* **412 passed, 1 skipped, 0 failed**, deterministic across repeated runs.
* **Adversarial verification against pristine v1.2.2:** 9 failures across
  `test_modbus_guard.py` and `test_update_coordinator.py`;
  `test_adaptive_modbus.py` fails to *collect* because
  `ADAPTIVE_QUEUE_DEPTH_COLD_START` does not exist there — a stronger signal
  than a test failure.
* **Four tests that encoded the defect were replaced, not weakened.**
  `test_returns_total_rtt_ms` asserted `return merged, total_rtt_ms`,
  pinning the bug in place. Assertion count in that area increased.
* **Control cases included.** `test_batch_scale_rtt_would_saturate_both` feeds
  a batch-scale RTT and asserts both parameters *do* saturate, proving the
  healthy-case assertion is capable of failing.
* During development the control case initially failed at 60 samples because
  confidence was 0.4 and the cold-start blend masked saturation — the test was
  corrected (200 samples), not the code. Recorded because it demonstrates the
  blending working as designed.

## 9. Expected behaviour and residual risk

Gap should fall toward its 150 ms floor and timeout toward 15 s during
healthy, high-confidence slots. If per-chunk RTT is genuinely multi-second the
formulas self-correct and timeout settles above the floor — the fix is robust
to the true chunk count either way.

**Residual risk:** this roughly triples the request rate on a bus shared with
a second inverter (gap 500 -> 150 ms). `slot_failure_rate` and the new
`shed_count` are the metrics to watch. One week of observation is recommended
before Defect C is addressed, and this is the reason for the staged plan.

Validation on the next export: the fraction of samples with gap >= 495 ms at
confidence >= 90% should drop toward ~0%, and `rtt_p95_ms` should read in the
hundreds of milliseconds rather than above 12 seconds.

**Verdict:** release-ready. Three confirmed defects resolved plus one found
during review that was a prerequisite for a fourth; one proposed fix rejected
on analysis with the reasoning recorded; a required migration added that the
report omitted; scope deliberately staged so effects remain attributable.
