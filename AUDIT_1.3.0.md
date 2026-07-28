# Release Audit — huawei_solar v1.3.0 (Bus scheduler, Phase 0)

**Date:** 2026-07-28 · **Auditor:** Claude (Anthropic)
**Baseline:** v1.2.4 (running in production; the rollback target)
**Scope:** `modbus_guard.py`, `bus_diagnostics.py` (new), `adaptive_modbus.py`,
`update_coordinator.py`, `switch.py`, `manifest.json`, `tests/`
**Type:** instrumentation only. **No behavioural change** to polling,
adaptation or scheduling.
**Series:** first of the staged rework in `DESIGN_bus_scheduler.md`. The 1.3.x
line is work-in-progress; **v2.0.0** marks completion.

---

## 1. Purpose

Phase 0 exists to answer one question that three days of field data could not.

**Established by the data:** both inverters' failure rates track the **master's**
workload — r = +0.935 (10 kW) and +0.909 (5 kW) against master batch size,
versus +0.20 against the slave's. The slave's own batch size is flat all day
(1.5 → 1.6) while the master's triples (1.5 → 4.3). The slave is roughly 20% of
demand and is not the constraint.

**Not established:** the mechanism. Two candidates remain, and they call for
opposite responses:

| | Mechanism | Implication |
|---|---|---|
| (a) | Requests queue on our shared lock | a scheduler fixes it |
| (b) | The master's CPU saturates — it relays the slave's frames on top of its own battery/meter/PV workload | only demand reduction helps; scheduling alone will not |

No sensor available today separates **time waiting for admission** from **time
talking to the device**. That split settles it, and building it is this release.

Deliberately *not* a bet on the answer: the target architecture serves both
outcomes. Phase 0 determines whether Phase 3 does the heavy lifting or whether
priority and demand shaping already suffice.

---

## 2. Changes and their justification

| Change | Justification |
|---|---|
| `ModbusGuard` records wait vs service time, `occupancy()`, `wait_service_split()` | the measurement that discriminates (a) from (b) |
| `bus_diagnostics.py` — default-off per-request capture | per-request granularity; the recorder only stores state *changes*, so sub-threshold movement is invisible, and 15-minute slot aggregation hides the within-slot distribution |
| Per-**bus** capture switch | the capture is a property of the shared physical connection, not of an inverter |
| Sensors for `rtt_p95_ms`, `last_chunk_count`, `last_batch_ms`, `shed_count` | v1.2.3 added these to `_snapshot()` but never gave them sensor definitions, and the module has no `extra_state_attributes` anywhere — they were unreachable |
| `bus_occupancy_pct`, `bus_wait_p95_ms`, `bus_service_p95_ms` | occupancy is the feedforward signal the scheduler will pace from: it **leads** the problem, whereas failure rate lags it and is paid for in real timeouts |

### 2.1 A prior claim corrected

v1.2.3's changelog stated the instrumentation would be readable as entity
attributes. It was not: the keys existed only inside a dict that feeds
per-key *sensors*, and no sensors were defined for them. Diagnosing the
gap/timeout ceiling therefore required back-solving `rtt_p95_ms` from
saturation thresholds. This release makes them real sensors.

---

## 3. Safety properties

* **No behavioural change.** No alteration to poll intervals, gap, timeout,
  queue depth, adaptation or scheduling. The full pre-existing suite passes
  unchanged.
* **No register writes** introduced.
* **Event loop never blocked.** Records go to a bounded in-memory ring buffer;
  writes run in an executor thread. Inline disk I/O would inflate the very
  service times being measured — the instrument would distort the experiment.
  Flushes are additionally rate-limited (≥30 s apart) so a burst cannot cause
  continuous I/O.
* **Bounded memory.** `deque(maxlen=500)`; overflow is *counted*
  (`records_dropped`) rather than silently lost, so a gap is visible in the
  data rather than being mistaken for quiet.
* **Bounded disk.** 5 MB per file, 2 rotations. A diagnostics file that fills
  the disk would be a worse failure than the one being diagnosed.
* **Cannot break Modbus I/O.** The guard's call into the sink is
  exception-guarded and verified by test
  (`test_failing_sink_never_breaks_modbus_io`): a sink that raises leaves queue
  depth at 0 and the lock released.
* **Accounting correct on error paths** (`test_accounting_survives_an_exception_inside_the_context`)
  — service time is still attributed, the lock is released, queue depth
  unwinds. This matters because the BaseException handler protecting against
  lock leaks is the most safety-critical code in the module.
* **Storage:** untouched. No `Store` version change (invariant established
  after the v1.2.3 outage).
* **Fault isolation (v1.1.7) and learning gate (v1.2.2):** unchanged; all
  structural tests pass.

---

## 4. Confidentiality

Explicitly designed for, given the operator's requirement that nothing
identifying leaves the installation:

* File names and every record use a **salted SHA-256 pseudonym** (8 hex chars)
  of the endpoint — never the host, port or serial.
* `label` carries a coordinator name (e.g. `battery_data_update_coordinator`),
  which contains no identifiers.
* `test_records_contain_no_endpoint_or_serial` asserts the written file
  contains no host string.
* Capture is **opt-in** and **not restored across restarts**, so it cannot be
  silently left running.

---

## 5. Test evidence

* **433 passed, 1 skipped, 0 failed**, deterministic across three consecutive
  runs (was 419).
* 14 new tests: default-off behaviour, bounded buffer with counted drops, JSONL
  content, write-failure containment, pseudonymisation, and the wait/service
  split measured under contention, when idle, and when an exception is raised
  inside the request context.
* **Adversarial verification:** against pristine v1.2.4 the new test file fails
  to *collect* (the module does not exist) — a stronger signal than a failure.
  With `bus_diagnostics.py` copied in but the old guard retained, **5 of 5**
  wait/service tests fail.
* `test_module_imports.py` extended to cover the new module — the harness added
  after v1.2.3 shipped an unimportable release.
* Battery-health replay against the 6-month field dataset: unchanged.

---

## 6. Residual risk

| Risk | Assessment |
|---|---|
| Capture perturbs the measurement | Mitigated (ring buffer + executor + rate limiting), but **not zero** — enabling capture adds a dict construction and an append per request. Compare captured runs against sensor baselines rather than treating them as identical. |
| Executor thread contention on a busy HA instance | Flushes are ≥30 s apart and batched; impact expected to be negligible, unverified in production. |
| Mechanism still unresolved | By design — this release measures rather than fixes. |

**Not addressed here (by design):** Defect C (last-writer-wins on the shared
guard) and §9 optimisations. Both are absorbed into later phases; fixing
Defect C separately would build a mechanism Phase 3 replaces.

---

## 7. Operating instructions

1. Enable the `Modbus diagnostic capture` switch (disabled by default in the
   entity registry — enable the entity first).
2. Let it run across a full day/night cycle, including dawn and dusk.
3. Read `config/huawei_solar_diagnostics/bus_<tag>.jsonl`.
4. Turn the switch off.

**Interpretation:** wait-dominated records ⇒ mechanism (a), queueing, and a
scheduler is the fix. Service-dominated ⇒ mechanism (b), the device itself is
slow, and demand shaping is the lever. Check `records_dropped` in the switch
attributes — a non-zero value means the buffer overflowed and the file has
gaps.

**Verdict:** release-ready. Instrumentation only, no behavioural change, safety
and confidentiality properties explicitly tested, and the adversarial check
confirms the tests are load-bearing.
