# Design — Modbus Bus Scheduler

**Status:** proposal for review. No code written.
**Date:** 2026-07-28
**Baseline:** v1.2.4 (running, stable — the rollback target)
**Supersedes:** Defect C (§7 of the bug report) and §9.5, which are absorbed here.

---

## 1. Why re-architect

### 1.1 What the field data established

Three days of hour-by-hour data across both inverters, plus battery and
per-inverter batch-size telemetry:

| Observation | Value |
|---|---|
| 10 kW master batch size, night → day | 1.5 → **4.3** registers/request |
| 5 kW slave batch size, night → day | 1.5 → **1.6** (flat) |
| corr(10 kW failure rate, **master** batch size) | **+0.935** |
| corr(5 kW failure rate, **master** batch size) | **+0.909** |
| corr(either failure rate, **slave** batch size) | +0.20 |
| Failure rate, night vs day (both inverters) | ~2% vs ~8% |
| Day/night effect size | 3.78 × within-regime stdev |
| Reproducibility across 3 days | hour-by-hour profiles near-identical |

**Both inverters' failure rates track the MASTER's workload.** The slave's own
traffic is flat all day while failures quadruple. The slave is roughly 20% of
demand and is not the constraint.

Physically coherent: the master hosts the battery, power meter, dongle and its
own PV strings. In daylight all of those carry live data, so their registers
expire together, producing larger and burstier batches — on the same
consumer-grade CPU that must also relay every frame for the slave.

### 1.2 Two things the current design gets structurally wrong

**(a) It adapts the wrong device.** Each `AdaptiveModbusController` learns
per-inverter and backs *itself* off. But the slave's failures are caused by the
master; slowing the slave changes nothing, because the slave is not the load.

**(b) It is reactive, and failures are the cost.** The controller observes
failure rate, then increases poll interval. By then requests have already timed
out, entities have gone unavailable and battery-health segments have been
discarded. The control signal is a *lagging effect* of the thing we want to
avoid.

The replacement is **feedforward**: pace from *observed master load* — which is
measurable, leading, and causal — instead of from failure rate.

### 1.3 What was tried and did not work

Recorded so it isn't re-attempted:

* Per-inverter adaptive tuning (current design) — adapts the wrong device.
* Defect A's RTT rescale (v1.2.3/1.2.4) — a genuine bug fix, but the
  like-for-like morning comparison (26th pre-deploy vs 27th/28th post) shows
  **no change in failure rate**. It was not the cause.
* Bus-contention/load-increase theory — refuted: request rate was flat
  (1,514 → 1,428/h) across a gap change.
* "Per-chunk RTT is genuinely 12 s" — that was the post-migration relearning
  transient, not steady state.

---

## 2. Goals and non-goals

### Goals

**G1 — Time-critical registers are served promptly.** Power/energy reads must
not queue behind bulk refreshes. Today `register_cache` classifies registers
FAST/NORMAL/SLOW/STATIC and that knowledge is **discarded** at the lock:
`asyncio.Lock` is strictly FIFO, and the existing `priority` flag only lets the
keep-alive probe bypass *shedding*, not reordering.

**G2 — Demand never exceeds what the master can serve.** Not a bandwidth
ceiling — the line is serialised, so the failure mode is queue build-up →
latency → timeouts → recorded failures → back-off.

**G3 — Separate lock-wait from service time.** The one measurement that
distinguishes queueing on our lock from saturation of the master's CPU. It does
not exist today and comes free with this design.

### Non-goals

* **More throughput.** The line is at its service ceiling; nothing here creates
  headroom. The deliverable is predictability, not speed.
* **Inter-inverter fair-share arbitration.** Arbitrating between an 80%
  consumer and a 20% one solves nothing. Defect C's last-writer-wins is fixed
  as an internal detail, not as a feature.
* **Determinism.** Vendor firmware behaviour is undocumented and the hardware
  is consumer-grade. Robust and self-explaining is achievable; deterministic is
  not.

---

## 3. Target architecture

### 3.1 Component change

Today every coordinator independently acquires a shared lock and pushes its own
learned parameters onto it (last writer wins). The scheduler inverts this:
coordinators **submit intents**, the scheduler **decides order and timing**.

This inversion is the enabling change. Priority ordering and admission control
are impossible while each coordinator independently grabs a lock.

```
  BEFORE                              AFTER
  coordinator ──► guard.request()     coordinator ──► scheduler.submit(intent)
       │            (FIFO lock)                          │
       └──► guard.update_gap()                    scheduler decides:
            (last writer wins)                      order (priority + ageing)
                                                    timing (occupancy budget)
                                                    ↓
                                                  executes on the one line
```

### 3.2 Request intent

A coordinator declares *what it needs*, not *when*:

| Field | Meaning |
|---|---|
| `registers` | the set required |
| `priority` | from the existing `register_cache` tier |
| `max_staleness` | how old a cached value may be before a read is required |
| `device` | which unit ID (for accounting, not fairness) |

### 3.3 Scheduling policy

* **Priority ordering with ageing.** A job's effective priority rises with wait
  time, so SLOW work cannot starve behind a FAST stream. Ageing rate is a
  tunable with a documented default.
* **Occupancy budget.** The scheduler tracks the fraction of wall-clock time it
  is holding the line. Above a high-water mark it defers low-priority work and
  lengthens refresh intervals *before* the queue builds — admission control
  rather than shedding.
* **Demand shaping (TTL phase spreading).** The measured driver is that the
  master's battery, meter and PV registers expire *together* and burst.
  Refreshes are phase-staggered across their interval instead of synchronising.
  This is cheap and attacks the driver directly.

### 3.4 Load model

Replaces failure-rate-driven adaptation:

* **Input:** measured service time per request, attributed by device and
  register tier.
* **Regime detection** from measured occupancy, not the clock. This realises
  the operator's four-regime model (night / dawn / day / dusk) without needing
  a sunrise table, and self-adjusts for season, cloud and future hardware.
* **Failure rate is retained as a safety net only** — a hard back-off trigger,
  not the primary control input.

### 3.5 What is kept unchanged

`register_cache` tiering and night mode; `ModbusTelemetry`; `ModbusKeepAlive`;
the v1.1.7 fault-isolation contract; the v1.2.2 learning gate (`Adaptive
learning` switch, settling periods); the battery-health subsystem in its
entirety.

---

## 4. Invariants (must hold at every phase)

1. **One request in flight.** The line is serialised; this never changes.
2. **No register writes** are introduced by the scheduler.
3. **Setup can never be aborted** by scheduler failure — v1.1.7 contract.
4. **Storage compatibility with v1.2.4.** No HA `Store` version bump without a
   migration callable (the v1.2.3 outage).
5. **Fallback to v1.2.4 behaviour at runtime**, via flag, without redeployment.
6. **Keep-alive always admitted**, regardless of occupancy state.
7. **No starvation:** every submitted intent is eventually served.
8. **Learning gate honoured:** the existing switch and settling periods
   suspend scheduler learning exactly as they do today.

---

## 5. Migration — strangler, not big-bang

Given a production outage from a two-line change, the critical path is not
rewritten in one step.

**Phase 0 — Instrumentation (ships first, standalone).**
Sensors that v1.2.3 promised but never wired up (`rtt_p95_ms`,
`last_chunk_count`, `shed_count`) — the adaptive module has **no
`extra_state_attributes` anywhere**, so each needs its own sensor definition.
Plus the default-off diagnostic capture: per-request records with **lock-wait
and service time separated** (G3). Bounded ring buffer, executor-flushed —
**never disk I/O on the event loop**, or the instrument distorts what it
measures. Hard size cap, rotation, no serial numbers in the file.

*Exit criterion:* one week of capture data confirming whether the bottleneck is
lock-wait (queueing) or service time (master CPU).

**Phase 1 — Scheduler behind the existing interface.**
`ModbusGuard.request()` keeps its signature; internally it becomes a scheduler
with a **single admitted job** and FIFO ordering — i.e. behaviourally identical
to today. No coordinator changes. This is a refactor with no functional delta,
verified by the existing suite.

**Phase 2 — Priority ordering (G1).**
Propagate the `register_cache` tier into `submit()`. Enable priority + ageing.
First functional change; independently valuable and independently revertible.

**Phase 3 — Occupancy budget and demand shaping (G2).**
Enable the load model, TTL phase spreading, and occupancy-based admission.
Retire per-inverter failure-rate adaptation as the primary control. Defect C
disappears here: the scheduler computes effective parameters from aggregate
state, so last-writer-wins has nowhere to live.

**Phase 4 — Cleanup.**
§9.4 (unify the two RTT measurement paths — the structural reason Defect A
stayed hidden), §9.1 (inter-chunk pause vs guard gap: note the 80 ms sleep sits
*outside* the lock and affects fairness), §9.2, §9.3.

Each phase is a separate release, deployed in a night maintenance window, with
v1.2.4 as the rollback target.

---

## 6. Test strategy

Driven by the three defect classes already shipped in this project.

* **Import and instantiation tests.** `test_module_imports.py` now imports
  every module; coordinator/scheduler **instantiation** is still a gap and is
  mandatory here. Source-string assertions are not coverage — they let three
  defects through.
* **Adversarial verification is mandatory.** Every new test is run against a
  deliberately broken tree and shown to fail.
* **Upgrade-path tests with v1.2.4 fixtures.** The v1.2.3 outage occurred only
  on installations with existing persisted data — i.e. every real user, and no
  test.
* **Deterministic scheduler simulation.** A virtual clock with synthetic
  request streams (night profile, day profile, dawn/dusk transitions) asserting
  the invariants of §4 — especially no-starvation and occupancy bounds. This is
  where a scheduler can be tested properly without hardware.
* **Replay against captured field data** once Phase 0 has produced it.
* **Battery-health regression** unchanged: the 6-month replay must stay
  identical across every phase.

---

## 7. Risks

| Risk | Mitigation |
|---|---|
| Blast radius: this is the most critical path in the integration | Strangler phases; Phase 1 has zero functional delta; runtime fallback flag |
| Mechanism (b) unproven — master CPU vs queueing | Phase 0 measures it before any behavioural change; **the same architecture serves both** outcomes, so the design is not a bet |
| Scheduler bugs cause starvation or deadlock | Explicit no-starvation invariant; deterministic simulation with a virtual clock |
| Instrumentation perturbs the measurement | Ring buffer + executor flush; capture is default-off |
| Another storage-migration outage | Invariant 4; no `Store` bump without a callable |
| Complexity outlives its usefulness | Occupancy model replaces per-inverter learning rather than adding to it — net removal of moving parts |

---

## 8. Expected outcome, stated honestly

**Likely:** time-critical registers served promptly under load (G1); daytime
failure rate reduced by spreading the master's bursts; failures no longer used
as the primary control signal, so the system stops paying in timeouts to learn;
one bus model instead of two per-inverter models.

**Not expected:** materially more throughput, a lower *floor* on failure rate if
the master's CPU is genuinely the limit, or elimination of dawn/dusk
instability — that is a property of the hardware and is best handled by
*expecting* degraded service in those regimes rather than tuning through them.

**Open question Phase 0 answers:** if the bottleneck is the master's CPU rather
than queueing, demand shaping and priority deliver most of the benefit and the
scheduler is primarily an enabler. If it is queueing, the scheduler itself does
the work. Both are served by this design.
