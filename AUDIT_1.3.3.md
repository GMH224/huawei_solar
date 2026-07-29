# Release Audit — huawei_solar v1.3.3 (tier-aware Modbus reads)

**Date:** 2026-07-29 · **Auditor:** Claude (Anthropic)
**Baseline:** v1.3.2 · **Rollback target:** v1.2.4
**Type:** behavioural change, driven by measurement. First release in the
1.3.x series that alters how the bus is used.

---

## 1. Evidence

Phase 0 capture: **3,400 requests over 28.8 h**, shared two-inverter bus, with
`regs` and `prio` populated (v1.3.1) and serials pseudonymised.

### 1.1 Cost is categorical, not marginal

| Chunk contents | Service time |
|---|---|
| FAST/NORMAL only | **~6 ms**, independent of size |
| Contains SLOW/STATIC | **~2,900 ms + 377 ms/register** |

Median service by register count (excluding the FAST-only `power_meter`
coordinator) shows a cliff, not a slope:

```
regs 1-7 :     6 - 62 ms
regs 8   :    234 ms      (bimodal; p90 8,639 ms)
regs 9   :  5,634 ms
regs 27  : 10,745 ms
```

**The controlled comparison:** `power_meter_data_update_coordinator`, which
reads only FAST-tier registers, stayed at **6.2 ms through 18-register
chunks** in the same window that put `battery` chunks of the *same size* at
**6,353 ms**. Same device, same period, ~1,000x apart. Register count cannot
explain this; tier can.

### 1.2 Where the cost sits

* 99% of all service time was in the 20.7% of requests touching SLOW content.
* `data_update_coordinator` 52%, `battery` 31%, `config` 17%,
  `power_meter` **0.1%**.
* Bus occupancy 15.4% — no throughput ceiling. A tail-latency problem.

### 1.3 The correction that reversed a recommendation

An initial fit on ~900 records suggested a pure per-register slope
(~685 ms/reg through the origin). The full 3,400-record set gives

```
service_ms = 2,924 + 377 x regs        (regs >= 9, n = 678)
```

The **~2.9 s fixed entry cost dominates**, and that inverts the natural
conclusion:

| 27 expensive registers | Cost |
|---|---|
| 1 chunk of 27 | **13.1 s** |
| 4 chunks of 7 | **22.2 s** |

Splitting is ~9 s *worse*. A flat `BATCH_CHUNK_SIZE` reduction — the obvious
first instinct — would have made the problem worse while appearing to address
it.

---

## 2. Changes

### (1) `_chunk_tier()` reported the wrong tier — instrumentation defect

It returned `min(tiers)`, the **fastest** tier present. A chunk of 1 FAST +
26 SLOW registers was labelled `FAST`. **All 3,400 records came back `FAST`**,
including every 19+ register chunk and a 51.5 s outlier.

The field existed precisely to correlate stalls with content, and could not.
The tier composition had to be reconstructed from register counts and
coordinator names instead.

Now reports the **slowest** tier plus composition (`SLOW:F1/N2/S24`). `max` is
correct because cost follows the slowest content: one SLOW register drags the
whole exchange onto the inverter's slow path.

### (2) Tier-separated chunking

`_split_by_cost()` partitions a poll into cheap (FAST/NORMAL) and expensive
(SLOW/STATIC) sets, chunked separately. A routine power/SOC read is no longer
trapped behind a multi-second exchange.

**The expensive set is kept together**, at full `BATCH_CHUNK_SIZE`, for the
reason in §1.3. `test_expensive_set_is_not_fragmented` and
`test_batch_chunk_size_not_reduced` pin this so it cannot be "optimised" away
by someone who sees a 27-register chunk and reaches for a smaller cap.

Cost of the extra request: a cheap chunk is ~6 ms against a ~2,900 ms entry
cost — under 0.3%.

### (3) SLOW-tier refresh 300 s -> 900 s

Separation stops expensive reads *delaying* other traffic; it does not reduce
their total cost. Only frequency does — and these are, by their own
classification, slow-changing: temperatures, alarms, device status, daily and
lifetime counters.

900 s is a deliberately moderate ~3x reduction rather than the 1,800 s cap, so
the effect is measurable before going further. Exposed as
`Slow-register refresh interval` (300–3600 s), clamped in
`set_slow_tier_ttl()` so a mistyped option can neither hammer the bus nor
effectively disable slow data.

---

## 3. Safety

* **No register writes** introduced.
* **No data lost.** Tier separation changes *how* registers are grouped into
  requests, not *which* are read. Every register still reaches the same merged
  result dict.
* **Failure paths unchanged.** Per-chunk timeout, BUSY retry and the
  shed/timeout discrimination all operate per chunk exactly as before.
* **FAST/NORMAL TTLs untouched** — asserted by test. Only the expensive tier
  is slowed.
* **Storage untouched**; no `Store` version change.
* Fault isolation (v1.1.7), learning gate (v1.2.2) and class-integrity checks
  (v1.3.2) all pass unchanged.
* Battery-health replay against the 6-month dataset: unchanged.

### 3.1 Accepted trade-off

SLOW-tier data is now up to 15 minutes stale rather than 5. Affected: pack
temperatures, alarm and status registers, daily/lifetime counters. For alarms
this is a genuine detectability delay, and it is a deliberate choice — the
alternative is those reads continuing to consume 99% of bus service time and
delaying power and SOC data. The interval is user-tunable if a shorter alarm
latency matters more.

**Battery-health impact considered:** the engine's segment detection uses SOC,
power and the lifetime counters. The counters are SLOW-tier, so segment
endpoints may now be up to 15 min stale — but v1.2.3 already established that
only *endpoints* enter the capacity arithmetic and that `CounterMonitor` marks
carried-forward values stale, with segments refusing to open on them. The
existing guard covers this; worth re-checking `stale_endpoint_skips` in the
field.

---

## 4. Verification

* **455 passed, 1 skipped, 0 failed**, deterministic.
* **Adversarial:** 8 of 12 new tests fail against v1.3.2.
* New `tests/test_tier_separation.py` pins the prio fix (excluding the
  docstring, which deliberately names the old `min()` bug), the split, the
  no-fragmentation decision, the TTL change and clamping, and the cost
  arithmetic itself.
* Static: all Python files parse; all JSON valid; manifest = 1.3.3.

---

## 5. Limits of the evidence

* **No night data.** The capture spans 04:00–15:00 UTC. Stall rates were flat
  across those hours (14.6–28.3%), but the earlier failure-rate work showed a
  strong day/night difference this file cannot speak to. If expensive-register
  cost is constant around the clock, something else drives that pattern.
* **One installation, one firmware.** The ~2.9 s figure is this hardware's;
  the *structure* (tier-driven, fixed-cost-dominated) is likely general, the
  constants are not.
* **Effect unmeasured.** The next capture should show `prio` labels varying,
  expensive exchanges roughly 3x less frequent, and cheap chunks no longer
  mixed with SLOW content.

---

## 6. Roadmap consequence

`DESIGN_bus_scheduler.md` is already marked as having its core assumption
invalidated. This release reinforces that: there is no queue to schedule, and
the fix was in *what we ask for and how we group it*, not *when*. Phases 2 and
3 as originally written should be formally dropped rather than deferred.

**Verdict:** release-ready. An instrumentation defect that masked the
mechanism is fixed; the behavioural change follows directly from measurement;
and the counter-intuitive result — that splitting expensive reads makes them
worse — is pinned by test so it is not undone by a plausible-looking future
change.
