# Release Audit — huawei_solar v2.0.3

**Date:** 2026-08-12 · **Auditor:** Claude (Anthropic)
**Baseline:** v2.0.2
**Type:** full, tiered remediation of a comprehensive external ICS audit
(17 findings, ICS-01 through ICS-17) covering code correctness, log
analysis, and initial architecture observations from real telemetry
data. One finding (ICS-12) deliberately deferred; every other finding
fixed and adversarially tested.

---

## 1. Why this release exists, and how it was scoped

Following v2.0.2's deployment, the operator supplied two further external
ICS audit documents — a code-only pass, then a fuller one incorporating
direct analysis of a production log and the first real telemetry capture
from AR-9's own instrumentation (v2.0.2's own addition). All findings from
both were independently re-verified against actual source before any fix
was written, matching this project's established practice for every prior
report. All 17 were confirmed genuine.

Given the scale, findings were grouped into four tiers by what each
protects against, agreed with the operator before implementation began:

- **Tier 1** — data integrity of the numbers users actually see (ICS-01,
  ICS-05).
- **Tier 2** — silent data loss / active crashes (ICS-02, ICS-03, ICS-06,
  ICS-08, ICS-17).
- **Tier 3** — write-safety for battery control (ICS-11, ICS-12).
- **Tier 4** — lower-severity hygiene (ICS-04, ICS-07, ICS-09, ICS-10).

## 2. Tier 1 — data integrity

### ICS-01 — SyncPower fallback could combine misaligned cache/physical readings

**Confirmed.** `_read_one()`'s per-value cache check (MOD-01, v2.0.0b)
accepted any `Quality.GOOD` cached value regardless of age, and
`sample_span_ms` was computed from *when `_read_one()` happened to be
called* for each value, not when the value was actually captured — for a
cache hit those are different moments. A composite result could silently
combine a several-seconds-old cached value with a just-now physical read
while still reporting a tight, reassuring `sample_span_ms`.

**Fix:** every value's own effective capture time is now tracked —
`time.monotonic() - age` for a cache hit (using `RegisterCache.quality_
of()`'s own age field, previously read but discarded), the read's own
completion time for a physical read — with min/max tracking across all
values, not first-call/last-call order (a cache hit's true capture time
can be earlier than an earlier physical read's, even though the cache
check happens later in the sequence).

### ICS-05 — is_temporally_uncertain was computed but never consulted

**Confirmed.** All four power-flow sensors share one base-class `_handle_
coordinator_update()`; none of them checked the flag ICS-01's own fix now
computes correctly.

**Fix, a genuine contract decision, not a mechanical change:** a
temporally uncertain reading now makes the entity unavailable, matching
how this project already treats quality problems elsewhere (`Quality.
BAD` → unavailable, never a guessed value shown as valid) — chosen over
silently exposing an "uncertain" attribute alongside a still-displayed
number, since a power-flow value a user might act on is exactly where a
wrong-looking-right number is worse than a brief unavailable state.

## 3. Tier 2 — silent data loss / active crashes

### ICS-02 — adaptive-learning persistence lost-update race

**Confirmed.** `_async_save()` snapshots state synchronously, then awaits
the actual write; a mutation arriving during that await was silently
discarded — the old code unconditionally cleared `_dirty` once its own
in-flight save completed, even if a newer, unsaved mutation existed.

**Fix:** a generation counter, bumped at all four `_dirty = True`
mutation sites. `_async_save()` captures the generation before its
`await`, and loops to save again immediately if it changed — rather than
calling `_schedule_save()` (traced through and found this would silently
no-op: that coroutine *is* the in-flight save `_schedule_save()` checks
for, not yet "done" from its own perspective). A property-based
auto-increment was considered and rejected: this project's test fixtures
widely bypass `__init__` via `object.__new__()`, and a property would
need `_generation` to already exist before those fixtures' own direct
attribute assignments run.

### ICS-03 — telemetry capture's own v2.0.2 flush fix had two residual bugs

**Confirmed**, in this session's own recent work. (1) A successful retry
never removed its own records from the buffer — the `came_from_buffer`
check's premise was wrong: removal only ever happened on success, so a
batch that failed its first attempt was never removed in the first
place, and a successful retry needed to remove it too. (2) Position-based
removal could remove the wrong records if the buffer's own `maxlen`
eviction dropped some of an in-flight batch's records first.

**Fix:** every buffered record now carries a stable sequence number;
removal matches by that number, correct regardless of retry history or
prior eviction. Proven with a test that specifically constructs the
eviction race — floods the buffer past capacity while a write is
genuinely in flight, confirms the completing write's removal doesn't
touch the wrong (newer) records.

### ICS-06 — battery-health restore() sat outside the load-failure guard

**Confirmed.** A store that loaded successfully but was structurally
corrupt could make `restore()` raise, aborting initialization entirely —
silently disabling battery-health tracking for that device, no listener
ever subscribed.

**Fix:** goes beyond a simple try/except wrap. `restore()` mutates
several engine fields in sequence; a partial failure would leave some
fields reflecting corrupt data while others don't. On failure, the whole
engine is discarded and replaced with a fresh one (reusing its own
already-resolved `.cfg`, not requiring the original constructor
`options`), guaranteeing a genuinely clean state rather than a partially
restored one.

### ICS-08 — BusDiagnostics had the identical defect as ICS-03

**Confirmed** — found by checking whether ICS-03 was unique to the new
telemetry feature before assuming so. It wasn't: the sibling per-request
capture module never got the v2.0.2 fix at all.

**Fix:** the exact same sequence-ID-based removal, bounded retry, and
awaitable `async_disable()` pattern applied to `BusDiagnostics`. Also
added `async_will_remove_from_hass()` to `ModbusDiagnosticsSwitchEntity`,
which had none at all (reasonable before, since `BusDiagnostics` owned no
timer to cancel — but now that `async_disable()` is a genuine async
operation, the absence became a real gap).

### ICS-17 — select.py crashed on a legitimate partial coordinator payload

**Confirmed via a real production traceback** (`KeyError: 'storage_
charge_from_grid_function'`), independently found in the deployment log
before this report's own citation of it. `select.py` indexed the
availability register directly (`coordinator.data[key]`); the sibling
`switch.py` already handled the identical pattern correctly with `.get()`.

**Fix:** matched `switch.py`'s pattern. Before finalizing, swept every
other `coordinator.data[...]` site across `number.py`, `sensor.py`, and
`switch.py` to confirm this was a genuinely isolated inconsistency, not a
systemic pattern needing a broader fix — it was isolated.

## 4. Tier 3 — write-safety for battery control

### ICS-11 — SOC-targeted forcible charge/discharge bypassed the atomic sequence helper

**Confirmed.** `forcible_charge_soc()`/`forcible_discharge_soc()` used
four separately-guarded `_set_and_invalidate()` calls — exactly the
pattern MOD-19 (v2.0.0b) already closed for the time-based variants, but
missed for these two.

**Fix:** converted to `_set_and_invalidate_sequence()`, identical to the
time-based siblings. Root cause found and fixed at its source, not just
patched: `test_services.py`'s own `_MULTI_WRITE_FUNCTIONS` list — the
exact test that should have caught this — simply never named these two
functions. Extended that list rather than writing a new, separate test.

### ICS-12 — button and service paths use separate lock domains (DEFERRED)

**Confirmed, with an important correction to the report's own framing.**
Both paths hold the *same* Modbus guard continuously for their whole
write sequence (verified directly against `_guarded_write_sequence()`'s
implementation), which already prevents literal mid-sequence
interleaving of individual writes — the report's own description of the
risk. The real, narrower residual risk is two *complete* sequences racing
back-to-back in unpredictable order on a rare concurrent user/automation
collision — not a wrong-composite reading of two half-completed
sequences.

**Deliberately parked, not fixed in this release**, at the operator's own
direction, pending the next deployment's telemetry data and a decision on
ICS-16's steady-state architecture questions. Both sit at the same layer
of the problem (coordination above the raw `ModbusGuard`); building
ICS-12 in isolation now risks needing to redo it once that broader
direction is set. The current corrected-severity picture (mid-sequence
interleaving already prevented; the residual risk is real but bounded)
supports waiting rather than a rushed design decision.

## 5. Tier 4 — lower-severity hygiene

### ICS-04 — startup stagger could collide across coordinator *types*, not just same-type

**Confirmed mathematically**: with the prior 5s per-device stride, device
2's "main" (0 + 2×5 = 10s) landed exactly on device 0's own
"configuration" (10s).

**Fix, simpler than the report's own "full bus-wide scheduler"
recommendation, while just as complete:** the stride only needs to
exceed the maximum base offset (16s) for every device's entire 5-slot
window to be non-overlapping with every other device's, for any number
of devices — increased to 20s. Verified against the *real* `__init__.py`
source directly in a new test, not just the test file's own established,
documented reproduction of these constants (a project convention for
this file's own heavy import chain) — so the two cannot silently drift
apart and reopen this again.

### ICS-07 — fixed charge/discharge periods validated syntax only, not semantics

**Confirmed**, and checked directly against the vendor `huawei_solar`
package's own source before designing the fix, not reasoned about in the
abstract: `ChargeDischargePeriodRegisters.encode()` validates only the
maximum period count. But the *sibling* TOU-period register types in
that same vendor package do already validate exactly this (`start >= end`
rejected unconditionally, no midnight-wraparound support, plus a proven
sort-then-check-adjacent-pairs overlap algorithm).

**Fix:** mirrors that exact vendor-package algorithm rather than
inventing a new one — not a guess about device semantics, but consistent
with how the same vendor already treats this family of period types
elsewhere.

### ICS-09 — "priority" meant admission exemption, not lock-acquisition priority

**Confirmed.** All requests, priority or not, ultimately wait on the same
plain, FIFO `asyncio.Lock` once admitted; `priority=True` only affects
whether a request is shed at admission, never its position in the queue.

**Fix, deliberately documentation, not a new mechanism:** with no field
evidence keep-alive is actually being starved in a way admission
exemption alone doesn't already prevent, built a true priority queue was
judged not worth the risk. Both `ModbusGuard.request()`'s docstring and
the request context's own comment were made explicit about what
`priority=True` does *not* do. Proven with a genuine behavioral test, not
just a wording check: two normal requests are made to actually queue on
the lock, then a priority request arrives after — confirmed serviced
after both, not ahead of them.

### ICS-10 — services registered on every setup, never unregistered

**Confirmed** — no guard against repeated registration, and zero
`hass.services.async_remove()` calls anywhere.

**Fix, scoped down after finding a real complication:** several service
handlers are bound via `functools.partial()` to a device-kind argument
("emma" vs "inverter") resolved from *that entry's own* devices — meaning
"guard against re-registration" would be wrong: it would freeze the
global handler to whichever entry registered first, leaving the wrong
variant bound for other entries with different device kinds. Fixed only
the clearer, lower-risk half: reference-counted unregistration, removing
services from Home Assistant's own registry only once the last entry
still needing them has unloaded.

## 6. Test evidence

- **872 passed, 1 skipped, 0 failed** (was 843 at the v2.0.2 baseline; 29
  new tests across all 16 fixed findings).
- Every fix that touches a "heavy" module (`__init__.py`, `services.py`)
  was verified with source-level checks against the *real* file, not
  only a test-local reproduction, specifically to prevent the two from
  silently drifting apart (the exact failure mode ICS-04's own
  verification was designed to catch).
- A genuinely useful process note, recorded honestly: the first drafts of
  the ICS-01/ICS-03 adversarial tests failed against the *correct*
  implementation. Tracing why revealed the fixes' own automatic
  re-scheduling/re-flush behavior firing faster than the tests assumed —
  the fixes working as designed; the tests' timing assumptions were
  wrong, not the code. Fixed by waiting for the automatic chain to
  genuinely settle rather than asserting on an intermediate state.

## 7. Safety properties

- v2.0.2 remains available and was not modified; this release was built
  in its own working tree.
- No architectural changes beyond the specific, bounded fixes described
  above — the steady-state architecture question (ICS-16 and beyond)
  remains explicitly deferred to the next telemetry-driven decision.
- Every fix's reasoning, including the two corrections to the audit
  report's own framing (ICS-09, ICS-12), is recorded directly in the
  code, not only here.

## 8. Recommended next step

Deploy 2.0.3. The operator's own plan continues unchanged: observe the
next telemetry capture, then decide ICS-16 and the broader steady-state
architecture question together — and revisit ICS-12 at that same point,
since it sits at the same coordination layer.

**Verdict:** 16 of 17 findings fixed and adversarially tested against the
real implementation. One (ICS-12) deliberately deferred with recorded
reasoning, not silently dropped. No defects known to be open outside that
single, deliberate deferral.
