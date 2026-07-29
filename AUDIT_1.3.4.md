# Release Audit — huawei_solar v1.3.4 (SLOW-tier coalescing)

**Date:** 2026-07-29 · **Auditor:** Claude (Anthropic)
**Baseline:** v1.3.3 (built, not deployed) · **Rollback target:** v1.2.4
**Type:** behavioural change to register refresh scheduling, driven by
re-analysis of the same Phase 0 capture.

---

## 1. What deeper analysis found

Same 3,400-record capture as v1.3.3, examined for *frequency* rather than
*cost per exchange*.

| Coordinator | Expensive reads/h | Interval |
|---|---|---|
| `data_update_coordinator` | **37.9** | every **1.6 min** |
| `battery_data_update_coordinator` | 18.3 | every 3.3 min |
| `config_data_update_coordinator` | 5.9 | every 10.2 min |

A 300 s SLOW TTL permits at most ~12/h. `data` was doing **38/h** — three
times more expensive exchanges than the TTL should allow.

**Mechanism:** TTLs are timestamped per register. A coordinator's ~26 SLOW
registers were last read at ~26 different moments, so they expire at ~26
different moments. Nearly every 30 s poll finds one or two newly due and pays
the full ~2.9 s fixed entry cost to fetch them. The spread of expensive chunk
sizes (9–27 registers) is the fingerprint.

### 1.1 This corrects a claim made in v1.3.3

v1.3.3's changelog stated the TTL rise to 900 s would give "~3x fewer
expensive exchanges". That is wrong. Raising the TTL reduces each register's
own rate, but not the number of *distinct expiry moments*: 26 registers at
900 s still expire ~1.7 times per minute, so most polls would still include an
expensive read. The TTL change helps, but far less than claimed.

The error was reasoning about a per-register rate when the cost is
per-request. Recorded because the same slip — attributing a fixed cost to a
marginal quantity — has now appeared twice in this work.

---

## 2. Changes

### (1) SLOW-tier coalescing — the substantive change

When any expensive register is due, the whole SLOW/STATIC cohort for that cache
is refreshed in the same exchange, giving them a shared expiry.

```
today     : 37.9/h x (2.9 s + 13 x 0.377 s) ~ 296 s/h   (8.2% of wall clock)
coalesced :  4.0/h x (2.9 s + 26 x 0.377 s) ~  51 s/h   (~6x reduction)
```

This is the same structural insight that makes *splitting* expensive reads a
pessimisation (v1.3.3 §1.3), applied in the opposite direction: pay the fixed
entry toll as rarely as possible and carry as much as possible each time.

**Cheap registers are deliberately excluded** from coalescing — pulling
FAST/NORMAL registers into an expensive exchange would undo the tier
separation shipped in v1.3.3. Asserted by
`test_cheap_registers_are_not_pulled_in`.

Default ON, disableable via options without a code change.

### (2) Queueing instrumentation

The full capture contradicts an earlier 900-record sample which suggested
knock-on blocking was rare (8 of 9 stalls clean). Across 3,400 records:

* **210 of 467** long exchanges (45%) had another request waiting at completion
* **291 requests waited >1 s**, totalling **1,362 s**

This is now the main justification for v1.3.3's tier separation, so it needs
measuring rather than assuming. New `bus_requests_waited` and
`bus_total_wait_s` sensors, plus `coalesce_events` / `coalesced_registers`.

### (3) Optional night deferral — DEFAULT OFF

Holds non-urgent expensive refreshes until night mode, bounded at
`SLOW_DEFER_MAX_TTL_MULTIPLE` (3x TTL) so deferral can never become
starvation.

**Off by default, deliberately.** The capture spans 04:00–15:00 UTC only.
There is no night data showing expensive reads are cheaper then. Shipping this
on by default would repeat the pattern of acting ahead of measurement that has
already cost this project two wrong conclusions.

**Design conflict found during implementation:** night mode multiplies every
non-FAST TTL by `NIGHT_TTL_MULTIPLIER` (10x). "Defer expensive reads to night"
would therefore defer them into a window where they were *also* not due — the
feature would have added latency and nothing else. Resolved by judging the
expensive tier against its **base** TTL when night-preference is active and
night mode is on. Surfaced by a failing test; the mechanism was corrected
rather than the expectation.

### (4) Errors — deliberately not actioned

18 of 3,400 requests (0.5%) ended in `error`. Below the threshold that would
justify changing behaviour, and the capture already records them if that
changes.

---

## 3. Safety

* **No register writes** introduced.
* **No data loss.** Coalescing changes *when* registers are read, never
  *whether*. Every register still reaches the same merged result.
* **Bounded staleness.** Coalescing only ever makes data *fresher* — it pulls
  refreshes forward. Night deferral can delay, but is bounded at 3x TTL and
  is off by default.
* **Cheap tiers untouched** — FAST/NORMAL cadence is unaffected, asserted by
  test.
* **Both behaviours disableable** from the options flow without redeployment.
* **Storage untouched**; no `Store` version change.
* Fault isolation (v1.1.7), learning gate (v1.2.2) and class-integrity checks
  (v1.3.2) pass unchanged.
* Battery-health replay against the 6-month dataset: unchanged.

### 3.1 Interaction worth noting

Coalescing makes expensive exchanges **larger but rarer**. A single exchange
of the full cohort is predicted at ~12.7 s versus ~7.8 s today. Since the
guard holds the bus lock for the duration, individual blocking events get
longer even as their total cost falls sharply — which is precisely why
v1.3.3's tier separation matters, and why `bus_total_wait_s` is worth
watching. If mean wait rises while total service falls, the trade needs
revisiting.

---

## 3.2 A latent bug found by test flakiness

The capture tests began failing intermittently in the full suite while passing
in isolation. Root cause was in the shipped code, not the tests:
`BusDiagnostics._last_flush` was initialised to `0.0`, and the rate-limit check
read that as "flushed at monotonic time 0". On a host whose `time.monotonic()`
was still below `MIN_FLUSH_INTERVAL_S`, the **first** flush was suppressed and
records sat in the buffer until 30 s of uptime had passed.

In the field this would have presented as "the diagnostic file sometimes
doesn't appear" — intermittent and silent. Fixed with a `None` sentinel meaning
"never flushed", and pinned by `TestFlushRateLimit`, which also asserts that a
burst still *is* rate-limited.

Recorded because the instinct on an intermittent test failure is to stabilise
the test; here the test was right and the code was wrong.

## 4. Verification

* **467 passed, 1 skipped, 0 failed**, deterministic across six consecutive
  full-suite runs.
* **Adversarial:** 7 of 21 tests in `test_tier_separation.py` fail against
  v1.3.3.
* New tests cover: cohort pull-forward, cheap registers excluded, no
  coalescing when nothing is due, the disable switch, the cost arithmetic, and
  all four night-deferral behaviours including the starvation bound.
* Static: all Python files parse; all JSON valid; manifest = 1.3.4.

---

## 5. What the next capture should show

* `data_update_coordinator` expensive reads down from ~38/h toward ~4–8/h
* `coalesce_events` climbing; `coalesced_registers` showing the pull-forward
* Expensive chunk sizes clustering near the full cohort instead of 9–27
* `bus_total_wait_s` growing more slowly than the 1,362 s per 28.8 h baseline
* `prio` labels varying (v1.3.3 fix) rather than uniformly `FAST`

**Still missing: night data.** Until a capture spans darkness we cannot say
whether expensive-read cost is constant around the clock, which is what would
justify enabling night deferral.

**Verdict:** release-ready. The dominant remaining cost is addressed by a
change that follows directly from the measured cost structure; an overstated
claim in the previous release is corrected in the changelog rather than left
standing; and the one genuinely speculative element is shipped disabled.
