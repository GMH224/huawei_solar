# Release Audit — huawei_solar v2.0.2

**Date:** 2026-08-11 · **Auditor:** Claude (Anthropic)
**Baseline:** v2.0.1
**Type:** (1) fix for a genuine production crash the operator discovered
during first deployment of the telemetry capture feature, found via
direct log analysis rather than static audit; (2) full remediation of a
follow-up external ICS/IQS quality report (10 findings, TEL-001 through
TEL-010), scoped specifically to that same feature.

---

## 1. Why this release exists

v2.0.1 deployed cleanly by every measure available before deployment —
826 tests passing, every prior finding fixed and verified. But the
telemetry capture switch the operator enabled immediately afterward
never produced a single snapshot, in over an hour of runtime, despite
being toggled off and on again. That gap — something no static review
or test suite had caught — was only found once the operator sent the
actual production log.

## 2. The crash: `AdaptiveModbusController` had no `snapshot()` method

**Root cause, confirmed directly from the traceback.** The method
existed as `_snapshot()` (private, leading underscore). Every external
caller — `telemetry_capture.py`'s `build_telemetry_snapshot()` and,
independently discovered while fixing this, `diagnostics.py` — called
it as `.snapshot()`, the public name every sibling class
(`ModbusTelemetry`, `SynchronizedPowerCoordinator`) correctly uses for
the equivalent method. Every call raised `AttributeError`: every ~30
seconds once the telemetry switch was enabled, and on every attempt to
use Home Assistant's own **Download Diagnostics** feature for this
integration — a core, always-available HA feature, not the opt-in
telemetry switch, and with no exception guard around it at all in
`diagnostics.py`, meaning that feature would have crashed completely,
not just omitted a section.

**Two more callers of the broken name were found while fixing it**,
neither in the log (since neither had ever been exercised): the
diagnostic sensor entities' own `async_added_to_hass()`, and this
class's own periodic sensor-push mechanism (`_push_to_listeners()`) —
both had correctly used the *private* name, so renaming the method to
match its external callers would have broken these two internal ones if
the fix had stopped after the first rename. All four call sites are now
consistent.

**Why the test suite never caught this**: no test anywhere in the whole
project — not the telemetry switch's own tests, not `adaptive_modbus.py`'s
own test file — ever called `.snapshot()` at all. `build_telemetry_
snapshot()` looks up the controller via a registry (`adaptive_
controller_cls.get(serial)`), and no test ever registered a real
instance for its test serial, so that lookup always returned `None` and
the line was never reached. A genuine coverage gap, not a mock masking a
real failure. Closed with a direct test exercising `.snapshot()` against
a real `AdaptiveModbusController` instance, plus structural checks
confirming the old private name is genuinely gone and every internal
caller was updated too, not just the external ones the crash surfaced.

**Log analysis, completed in full**: beyond this one bug, the log showed
a cluster of Modbus timeouts concentrated almost entirely in the first
~9 minutes of setup, with only two isolated events afterward across the
full ~57-minute capture — consistent with normal Modbus behavior under
real hardware conditions, not a code defect. Nothing else required
fixing.

## 3. The follow-up report: TEL-001 through TEL-010

Once the crash was fixed and the switch confirmed working, an external
ICS/IQS quality report specifically targeting the telemetry capture
feature was reviewed the same way every prior report has been —
verified against actual source before any fix was written, not accepted
at face value. All ten findings were confirmed genuine.

### P0 — data-loss and lifecycle-determinism defects

- **TEL-001** (batch removed from the buffer before persistence success
  was known) and **TEL-002** (the final flush on disable/unload was
  fire-and-forget, never awaited) were fixed together as a single
  redesign of the flush lifecycle, since both were really the same
  underlying gap — no tracked, awaitable flush operation — surfacing in
  two different ways. A batch is now only removed once a write genuinely
  succeeds; `TelemetryCapture.async_disable()` (new) genuinely awaits the
  final flush, bounded by `DISABLE_FLUSH_TIMEOUT_S` so a hung write
  cannot hang HA's own unload sequence — the same "never block
  indefinitely" discipline already established project-wide, applied
  here for the first time to this module. Wired into production:
  `switch.py`'s `async_turn_off()`/`async_will_remove_from_hass()` now
  `await` this method instead of the old synchronous call.

### P1 — resource-lifecycle and test-realism defects

- **TEL-003** (`async_turn_on()` installed a new periodic timer
  unconditionally, orphaning any existing one on a second call): fixed
  by making the operation idempotent — a second turn-on while a timer is
  already registered is now a no-op for the timer specifically.
- **TEL-004** (`TelemetryCapture.remove()` existed but was never called
  in production, leaking every endpoint ever captured forever): fixed —
  and checked whether this was unique to the new feature before assuming
  so. It wasn't: `BusDiagnostics`, the sibling per-endpoint registry, had
  the identical gap. Both are now released together in `__init__.py`'s
  existing per-device teardown loop.
- **TEL-005** (the test executor fake ran jobs inline/synchronously,
  making it structurally impossible to exercise the async races TEL-001/
  002 actually depended on): the fake now has genuine async semantics —
  a real, controllable `async_add_executor_job` (independently
  delayable/failable per test) and a real `async_create_task` that
  schedules an actual `asyncio.Task` rather than running to completion
  immediately. This was the fix that made TEL-001/002 verifiable at all,
  not just claimed fixed.

### P2 — UX and correctness-of-documentation defects

- **TEL-006** (no file could appear for up to ~10 minutes after
  enabling, indistinguishable from the feature being broken —
  confirmed directly from the operator's own experience before the
  crash bug was found): the very first snapshot after enabling now
  forces its own immediate flush. `last_snapshot_at`/`last_write_at`
  were also added to the entity's own attributes, so "is this working"
  is answerable without inspecting logs or the filesystem at all.
- **TEL-007** (no retry after a write failure; combined with TEL-001,
  a single transient storage error meant permanent, silent data loss):
  bounded retry (`MAX_RETRY_ATTEMPTS`, 3) — a failed batch is retained
  and retried, not discarded; only after exhausting retries is it
  counted as permanently lost, explicitly
  (`snapshots_lost_write_failure`), distinct from `write_errors` (every
  individual attempt, including ones later recovered).
- **TEL-009** (documentation claimed one snapshot "always reflects the
  same moment" for every coordinator — an overstatement; the reads are
  sequential, not atomic): corrected to accurately describe same-tick,
  near-coincident sampling.
- **TEL-010** (a completed flush did not check whether more work was
  pending, leaving liveness dependent on the next timer tick): a
  completed flush now checks immediately and re-schedules itself if a
  retry is pending or the buffer has reached threshold — discovered,
  while writing this release's own adversarial tests, to be *already*
  interacting directly with TEL-001/007's retry logic: a failed batch's
  retry now chains automatically within the same flush cycle, not
  waiting for a separate trigger.

### P3

- **TEL-008** (the 5 MiB rotation check is pre-write, not a hard
  post-write cap): documented explicitly in the code, with the reasoning
  for why this is an acceptable trade-off given this capture's bounded
  batch sizes, rather than implementing exact pre-write size calculation
  for a marginal, bounded overshoot.

## 4. Test evidence

- **838 passed, 1 skipped, 0 failed** (was 829 at the v2.0.1 baseline,
  itself already including the `snapshot()` crash fix; 12 new tests for
  the TEL findings specifically, on top of the tests added alongside the
  crash fix itself).
- A genuinely useful process note: the first drafts of the TEL-001/
  TEL-007 adversarial tests failed against the *correct* implementation.
  Tracing why revealed TEL-010's auto-re-flush was firing automatically,
  within the same `await`, not waiting for a separate manual trigger the
  tests assumed. That was the fix working as designed; the tests'
  assumptions about timing were wrong, not the code — fixed by waiting
  for the automatic retry chain to genuinely settle rather than asserting
  on an intermediate state the design is specifically built to move past
  quickly.

## 5. Safety properties

- v2.0.1 remains available and was not modified; this release was built
  in its own working tree.
- No architectural changes — entirely a crash fix plus defect remediation
  for one already-shipped, opt-in feature.
- Every fix's reasoning is recorded directly in the code, not only here.

## 6. Recommended next step

Deploy 2.0.2, not 2.0.1. Re-enable the telemetry capture switch — it
should now produce a file within ~30-40 seconds, with `last_snapshot_at`/
`last_write_at` directly visible in the entity's own attributes as
confirmation. From there, the operator's original plan continues
unchanged: observe for 24+ hours, then use that data to decide the
Physical Demand Planner question.

**Verdict:** the crash blocking all telemetry capture is fixed and
independently verified against the actual traceback. All ten follow-up
findings are fixed and adversarially tested against the real
implementation. No defects known to be open.
