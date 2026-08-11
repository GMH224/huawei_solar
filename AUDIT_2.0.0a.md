# Release Audit — huawei_solar v2.0.0a

**Date:** 2026-08-08 · **Auditor:** Claude (Anthropic)
**Baseline:** v2.0.0
**Type:** full remediation of every finding in the independent external
ICS/defect audit of v2.0.0 (21 findings: 5 Critical, 9 High, 6 Medium,
3 Observation) — "a" release, not a full "2.0.0" bump, deliberately: this
has not yet been through a second external audit pass, and the operator
explicitly wants that before treating it as release-final.

---

## 1. Process

Per the operator's explicit instruction, this release followed three
steps in order: (1) an independent internal re-audit of v2.0.0's source,
performed without reading the external report first, to avoid anchoring;
(2) validation of every one of the external report's 21 findings against
actual source, not accepted at face value; (3) this remediation. All
three steps, plus this remediation itself, are recorded in full in the
conversation history; this document summarizes the outcome and the
evidence, not the process of arriving at it.

**Validation outcome, briefly restated:** all 21 findings were confirmed
genuine defects or real risks. One (F19) was found to be *understated* by
the external report's own "Observation" rating — direct tracing showed it
was a certain, live regression (introduced by this session's own §5.2
best-effort chunking work), not merely something needing runtime
confirmation. One (F11) required a real refinement during verification:
the external report's framing implied no jitter existed at all, which
wasn't true (±10% proportional jitter already existed) — but checking the
actual numbers surfaced a *different*, genuine problem the report hadn't
identified: the risk was concentrated at shallow backoff, not deep. One
citation (F06's `switch.py:396`) was found to already be correctly
guarded, contradicting that specific line reference, though the broader
finding (`number.py`, `sensor.py`) remained valid.

## 2. What shipped, by finding

### Tier 0 — live regressions, fixed first

- **F19 (understated by the external audit — confirmed as certain, not
  merely a risk)**: the suspicious-zero guard for energy counters had
  been silently disabled by this session's own §5.2 restructuring —
  `cache.get()` at the check's location could only ever see the cache
  *after* it had already been overwritten with the same cycle's fresh
  value, never the prior one. Fixed by snapshotting prior energy-counter
  values before `_execute_batch()` runs and mutates the cache, not
  reading the cache again afterward.
- **F16**: `_dirty` cleared before, not after, `async_save()` succeeded —
  a failed save could silently lose the "still needs saving" indication.
  Fixed: cleared only after the save genuinely succeeds.

### Tier 1 — the unified endpoint scheduler

The structural fix the majority of the external report converges on: no
production Modbus operation should bypass `ModbusGuard`.

- **F04/F17**: `ModbusGuard`'s registry now uses reference counting
  (`acquire_endpoint()`/`release_endpoint()`), not unconditional removal
  on a single entry's unload. A surviving entry sharing the same physical
  endpoint is no longer at risk of a second, uncoordinated guard object
  being created later. Wired into both the success and failure paths of
  `async_setup_entry()` (the latter via the existing Defect U
  cleanup-callback mechanism, reused rather than duplicated) and the
  success path of `async_unload_entry()`.
- **F05**: every write call site now routes through the guard —
  `services.py`'s `_set_and_invalidate()` (covering ~39 call sites with
  one fix), `switch.py` (both entities), `select.py` (both entities),
  `number.py`, and `button.py` (with a defensive fallback, since that
  entity is not a `CoordinatorEntity` and holds an explicitly optional
  coordinator reference).
- **F06**: `number.py`'s `_read_static_bound()` and `sensor.py`'s
  `_has_write_permission_bounded()` — already time-bounded, now also
  guard-routed, with the *entire* probe treated as one logical guarded
  operation (no visibility into how many internal exchanges the
  underlying `create_device_instance()`-style calls perform).
- **F01/F02**: `config_flow.py`'s discovery (`_auto_slave_discovery`,
  `_scan_slave_discovery`) and validation (`validate_serial_setup`,
  `validate_network_setup`, `validate_network_setup_login`) now acquire
  the endpoint's guard for their duration and route every device
  communication through it. New `DISCOVERY_PROBE_TIMEOUT` (5s) and
  `DISCOVERY_TOTAL_TIMEOUT` (90s, the honest worst case of 18 probes at
  the per-probe bound, not a separately guessed tighter number) close the
  previously-unbounded scan duration.
- **F03**: `_execute_batch()` now has a whole-poll deadline
  (`BATCH_POLL_DEADLINE`, 120s), wrapping the entire chunk loop, not just
  each chunk individually. Implemented via a reconciliation approach
  (track what was recorded, diff against what should have been) rather
  than fragile tracking of exactly which chunk was in flight at
  cancellation time — robust regardless of exactly where the cut-off
  lands.

### Tier 2/3 — the rest

- **F08**: new `ModbusAdmissionTimeout` exception, mirroring
  `ModbusQueueShed`'s existing pattern — distinguishes "the bus was busy"
  from "the device didn't answer" at the guard's admission-wait timeout.
  `ModbusKeepAlive` no longer manufactures a false connection-lost event
  (and the resulting full cache invalidation) out of ordinary bus
  congestion.
- **F09/F20**: `SynchronizedPowerData` gained `is_temporally_uncertain`,
  set when `sample_span_ms` exceeds the same hardware-derived
  `SYNC_POWER_CACHE_ALIGNMENT_TOLERANCE_S` the §8.2 cache-shortcut
  already uses — `sample_span_ms` is now a genuine quality gate, not
  instrumentation nobody reads.
- **F10**: addressed by the same fix as F09/F20, deliberately, not by
  realigning the coordinator stagger schedule. The external report
  offered two alternatives; adjusting the existing, already-tuned
  startup-collision-avoidance stagger for a marginal shortcut-hit-rate
  improvement was judged riskier than it was worth, given the explicit
  quality gate already guarantees correctness regardless of hit rate.
- **F11**: `_backoff_seconds()` gained a minimum absolute jitter floor
  (`MIN_BACKOFF_JITTER_S`, 2.0s), refining the original finding: the real
  risk was concentrated at *shallow* backoff (±1s at the 10s base delay —
  a ~2-second clustering window on the very first retry after a shared
  failure), not deep backoff as the report's framing implied. Deep
  backoff's proportional jitter (±12s at the 120s cap) already exceeded
  the new floor and is unaffected.
- **F12**: `verify_write()` — fully built, already guarded, zero
  production callers — wired into `number.py`, `select.py` (both
  entities), and the register-write `switch.py` entity (the
  STARTUP/SHUTDOWN entity deliberately excluded: it already has a
  better-suited verification mechanism of its own, polling `DEVICE_STATUS`
  for the actual state transition). Fired as a background task, not
  awaited directly — a real UX decision (~3-9s to confirm, whole value is
  a warning log on silent failure), not an oversight. The immediate
  `invalidate_cache()` call was kept alongside it as the baseline
  guarantee, since `verify_write()`'s own failure path does not touch the
  cache at all.
- **F13**: the cache-shortcut's success path no longer resets
  `_consecutive_failures` or logs "communication restored" — no I/O
  occurred, so nothing about communication health was actually verified.
- **F14**: deliberately deferred, not fixed. Documented directly in
  `AdaptiveModbusController`'s own docstring, not left as a silent gap —
  see §3 below for the full reasoning.
- **F15**: `record_request()`'s own docstring — "record **one** completed
  Modbus request" — confirmed the old once-per-poll,
  worst-chunk-RTT-only usage was the actual misuse. Moved inline, per
  chunk, mirroring the pattern already used for `cache.update()`/
  `record_attempt()`. `note_batch()` (genuinely poll-level) is untouched.
- **F18**: priority requests (currently only `ModbusKeepAlive`) now have
  their own bounded lane (`MAX_PRIORITY_QUEUE_DEPTH`, 2), independent of
  and not reducing the normal queue-depth check they correctly bypass.
- **F21**: `keepalive.stop()` moved to its own pass over every device on
  the entry, before the shared transport disconnects — not interleaved
  with the rest of teardown afterward. Only keepalive needed to move: it
  is the only active traffic producer among everything torn down in that
  function.

## 3. F14 — the one item deliberately not fixed, and why

Every other finding was either fixed directly or (F10) addressed by an
equally-valid alternative fix the external report itself offered. F14 is
different: it is the one item genuinely deferred, and that decision is
recorded in three places — this document, `AdaptiveModbusController`'s
own docstring, and the design record this session maintained throughout
(the same treatment already given to battery-health polling cadence and
the synchronized-power cadence realignment).

The reasoning: making the adaptive learner bus-wide (feeding it from
writes, config-flow discovery, and keep-alive, not just the main
coordinator's polling) is a materially larger and differently-shaped
piece of work than everything else in this pass. It would mean revisiting
every one of this session's own newly-guarded call sites again, deciding
how to weight fundamentally different traffic types (a write's RTT
characteristics are not obviously comparable to a read's), and doing so
without destabilizing a learning model that has been tuned and
field-validated against real data across many prior sessions — not a risk
to take on as the last item in an already-large remediation pass.

## 4. Test evidence

- **731 passed, 1 skipped, 0 failed** (was 675 at the v2.0.0 baseline; 56
  new tests added across this remediation, none of them padding — every
  new test either proves a specific numerical/structural claim from a
  finding or was confirmed adversarial against the pre-fix source).
- **26 files changed**: 14 production files, 12 test files.
- Every new adversarial test for a live-regression or structural fix
  (F19, F04/F17, F03, F08, F11, F13, F15, F21) was checked directly
  against the pre-fix source and confirmed to fail there — not assumed to
  be meaningful.
- **Three genuine test-infrastructure gaps found and fixed during this
  pass, not papered over**: `test_entities.py`'s shared fixture needed
  both `self.hass` (for the new `verify_write()` background-task calls)
  and `MagicMock` imported at module level (a `NameError` waiting to
  happen the first time anything needed it); `MockCoordinator` needed a
  real `verify_write()` method since it's a hand-written class, not an
  auto-attribute mock; `_fresh_guard()`'s `object.__new__()`-based
  construction needed the two new priority-lane attributes added
  explicitly, the same class of gap hit twice more this session
  (`_make_coordinator` in the synchronized-power tests, `_make()` in the
  entity tests).
- **Two false-positive tests caught and fixed before being left broken**:
  a test searching for `primary_device.client.disconnect()` matched this
  function's own explanatory comment (which contains that exact string)
  before ever reaching the real code — the test would have passed
  regardless of whether the fix was correct. And a fixed-size text window
  around a search anchor kept needing widening as explanatory comments
  grew during the same editing pass — resolved for good by switching to
  targeted, anchor-relative searches instead of arbitrary character
  counts.

## 5. Safety properties

- v2.0.0 (the pre-audit baseline) remains available and was not modified;
  this release was built in its own working tree throughout.
- No changes to the v2.0.0 quality model itself (`Quality`/`Reason`,
  `merge()`/`get()`'s serve-unless-BAD rule) — this pass is entirely about
  the transport/scheduling layer sitting underneath it and the entity
  write paths sitting on top of it.
- No new backward-compatibility guarantees beyond what v2.0.0 already
  established (device connection config preserved via HA core's own
  storage; sensor history, entity IDs, and adaptive learning data not
  migrated — all by prior explicit agreement, unchanged here).
- Every new constant carries its reasoning in its own comment, matching
  this project's established convention.

## 6. Recommended next step

Per the operator's own stated plan: this release is intended for a
**second external audit pass**, not direct deployment. The remediation
plan's own acceptance criteria (from the original report's P0-P2 table)
are believed satisfied by the fixes above; an independent re-audit is the
appropriate way to confirm that rather than this document asserting it
unilaterally. The stress-test matrix from the original report (two
entries sharing an endpoint, config-flow during active polling, an
18-unit silent-bus discovery, a 20+ chunk poll with every request timing
out, simultaneous coordinator failures, keep-alive waking during a
held-bus request, a write during active polling, sync-power under
saturation, mixed-traffic adaptive statistics, unload during an active
keep-alive probe) remains the right validation set to run against this
build specifically, now that every mechanism it exercises has changed.

**Verdict:** remediation complete for every finding raised, with one
(F14) deliberately and explicitly deferred rather than silently dropped.
Ready for the second independent audit pass the operator has planned,
not yet declared release-final pending that review.
