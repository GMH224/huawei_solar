# Release Audit — huawei_solar v1.3.19

**Date:** 2026-08-05 · **Auditor:** Claude (Anthropic)
**Baseline:** v1.3.18
**Type:** ten defect fixes (deduplicated from fourteen reported findings
across two independent audits), eight production files changed.
Explicitly requested with full rigor: "let's get them all done carefully."

---

## 1. The report

This release closes the full remaining backlog: the four findings
deferred from the v1.3.17 audit (tracked explicitly in `AUDIT_1.3.18.md`
§5, not overlooked) plus all ten findings from a third independent audit,
this time of v1.3.18. After deduplication (several findings were reported
independently by more than one audit — most notably the switch-polling
issue, now confirmed a third time across this session), ten unique fixes
remained. Every finding was independently verified against actual v1.3.18
source before any code was written, per this project's standing rule.

## 2. Finding 1 — cleanup-on-setup-failure incomplete

**Reported:** Defect U (v1.3.18) registered only `keepalive.stop` for
rollback if a later setup step failed; telemetry and the adaptive
controller were not registered, despite both having real teardown state.

**This is a gap in this project's own prior reasoning, stated plainly.**
`AUDIT_1.3.18.md` §3.1 explicitly argued these two were "idempotent,
registry-based singletons with no independent ongoing work." Checked
directly against source for this release: both `ModbusTelemetry.stop()`
and `AdaptiveModbusController.stop()` cancel a real, running periodic
timer, and the adaptive controller additionally manages a real background
save task and dirty-state flush. That prior reasoning was incomplete.

**Fix.** Both registered via the existing `register_cleanup` mechanism,
immediately after creation, matching `keepalive.stop`'s existing pattern.
The adaptive controller is registered with its new `async_unload` (Finding
10, §10 below) rather than the plain `stop()` — `_run_cleanup_callbacks`
already supports awaiting async callables, so the more reliable option
costs nothing extra here.

## 3. Finding 2 — `bridge` referenced before guaranteed assignment

**Reported:** `validate_network_setup_login`'s `finally` block checked
`if bridge is not None`, but `bridge` was only assigned inside the `try`,
after `client.connect()`. If `create_device_instance(client)` failed
first, `UnboundLocalError` would replace the real connection failure with
an unrelated cleanup error, and the raw TCP client was never explicitly
disconnected either way (only `bridge.stop()` was called, and only when
`bridge` existed).

**Verified** exactly as reported.

**Fix.** `bridge = None` before the `try` block. `finally` now branches:
if `bridge` exists, `await bridge.stop()` as before; if it was never
created, explicitly `await client.disconnect()` (suppressing exceptions,
matching this file's existing cleanup convention elsewhere) so the raw
client's connection state is never left ambiguous.

## 4. Finding 3 — the runtime unbounded-connect pattern, still present in the setup wizard

**Reported by both audits independently.** `create_device_instance`,
`create_sub_device_instance`, `bridge.login`, and `has_write_permission()`
were all called with no bound across several `config_flow.py` functions —
the identical risk already closed for the runtime setup path (Defect M,
v1.3.14; Finding 1 of the v1.3.17 audit, closed in v1.3.18).

**Verified** across `validate_network_setup` (lines 304-356 in the
audited tree), `validate_network_setup_login` (387-473), and
`_connect_to_discovered_devices`. A sweep for the same shape (not
explicitly named in either audit's evidence lines) also found it in
`validate_serial_setup`, the RTU/serial equivalent of
`validate_network_setup` — fixed identically for consistency.

**Fix.** All four functions now wrap the relevant calls in
`asyncio.wait_for(..., timeout=DEVICE_CONNECT_TIMEOUT)`, reusing the same
45-second bound and reasoning already established for the runtime path.
Deliberately no new exception handling was added: every caller of these
functions already catches bare `TimeoutError` alongside
`ConnectionException`/`ModbusConnectionError` as an expected, user-facing
"could not connect" case — confirmed directly by inspection before relying
on it, not assumed.

### 4.1 Scope decision: the auto/scan-discovery family left untouched

`_auto_slave_discovery` and its TCP/RTU wrappers were checked and
deliberately not modified. They use `create_scan_tcp_client`, a
purpose-built client with its own short scan-specific timeout (confirmed
in the installed vendor package: `DEFAULT_SCAN_TIMEOUT = 3` — *"short
timeout for scanning — responding devices reply in milliseconds"*),
unlike the general-purpose client the fixed functions use for a one-time
connect-and-validate. Their existing exception handling already includes
`TimeoutError` in every catch clause, confirming the original authors
anticipated timeouts as a normal, expected part of scanning. Adding a
redundant `wait_for` here would not be wrong, but is not a fix for
anything currently unbounded.

## 5. Finding 4 — mutable discovery state at class scope

**Reported:** `_discovered_sub_unit_ids: list[int] = []` was declared as a
mutable default directly on the `ConfigFlow` class — the standard Python
trap where in-place mutation before an instance's own value is first
assigned would be visible to every other flow instance sharing the class.

**Verified**, and checked further: every actual use in the current code
either reassigns the whole list (tuple-unpacking a discovery result) or
only reads it (`*self._discovered_sub_unit_ids`, passed by reference) — no
`.append()`/`.extend()` in-place mutation was found anywhere. The risk is
therefore latent in the current code, not actively triggered — but exactly
the kind of trap that's easy to reintroduce unnoticed in a future change,
which the audit correctly identified as worth closing regardless of
whether it's live today.

**Fix.** Added `ConfigFlow.__init__`, giving each instance its own list at
construction. Every other class-level attribute on `ConfigFlow` (all
immutable defaults — `None`, `False`, `int`) was left exactly as-is: only
a mutable default carries this risk, and changing the others would be
unnecessary churn.

## 6. Finding 5 — discovery task never cancelled on reset

**Reported:** `_reset_discovery_state()` set `self._discovery_task = None`
without calling `.cancel()` on the task first. A reset or abandoned flow's
background scan could keep running, continuing to probe the bus after the
UI had already moved on — exactly the kind of hidden traffic that causes
contention and confusing retry behaviour on a shared bus.

**Verified** exactly as reported. This is the same defect *shape* as
Defect L (v1.3.14) — a task reference dropped without cancelling the
underlying task — in a different file, found independently by a different
audit.

**Fix.** `_reset_discovery_state()` now cancels the task (if not already
done) before dropping the reference.

## 7. Finding 6 — telemetry listener fan-out had no exception isolation

**Reported:** `ModbusTelemetry._push_to_listeners()` called each
registered callback directly in a loop, with no `try`/`except`. One
misbehaving listener raising would stop iteration entirely — every
listener registered after the failing one silently stopped receiving
updates for that push (and, since the failure isn't logged either, with no
visible indication why).

**Verified** exactly: `for cb_fn in self._listeners: cb_fn(snap)`, bare.

**Fix.** Each callback now individually wrapped; a failure is logged and
the loop continues to the next listener.

## 8. Finding 7 — switch status polling, closed for good this time

**Reported independently three separate times** across this session's
history: `AUDIT_1.3.9.md` §5 (deferred), `AUDIT_1.3.15.md` §4 / Defect O
(only the documented-duration mismatch was fixed there, not the
architecture), and now both remaining audits of v1.3.17 and v1.3.18. Two
distinct problems in the same code: `device.client.get(rn.DEVICE_STATUS)`
called directly, bypassing `ModbusGuard` entirely with no timeout of its
own; and the surrounding loop bounded by an iteration count
(`MAX_STATUS_CHANGE_TIME_SECONDS // POLL_FREQUENCY_SECONDS`) rather than
actual wall-clock time, so the real duration could exceed the stated
5-minute limit by an arbitrary amount if any individual read blocked.

**Fix — a full redesign, not a patch.** New
`_poll_device_status_bounded()` routes the read through
`self.coordinator.guard.request()` with its own `asyncio.wait_for` bound,
returning `None` on any failure rather than raising (a failed poll is
simply "don't know yet, try again next cycle," exactly like a normal
coordinator poll already behaves). New `_wait_for_status()` tracks an
explicit `time.monotonic()` deadline, enforced around both the sleep and
the read, replacing the old `for _ in range(...)` loop entirely — the
total operation genuinely cannot exceed `MAX_STATUS_CHANGE_TIME_SECONDS`
now, regardless of how long any individual read takes.

## 9. Finding 8 — service validation read, now consequential because of Defect R

**Reported by both audits.** `services.py`'s `_validate_power_value()`
performed an unbounded `await dd.device.get(max_value_key)` before any
write — and does so *while* the per-device write lock introduced by
Defect R (v1.3.15) is already held for the entire service call. A slow
validation read doesn't just risk hanging this one call; since Defect R
made that lock genuinely exclusive, it now blocks every other write action
for the same device too, for as long as it takes. Worth naming plainly:
this is a case where a previous, correct fix (Defect R closed a real
concurrency risk) measurably raised the stakes of a pre-existing, separate
gap that had gone unnoticed until now.

**Fix.** Bounded with a new `SERVICE_VALIDATION_READ_TIMEOUT` (10s,
matching the reasoning already established for similar bounded-probe
constants elsewhere in this file). On timeout, raises `ValueError` with a
clear, user-facing message — matching this function's existing error
convention for "could not read the maximum allowed power," rather than
letting a raw `TimeoutError` propagate.

## 10. Finding 9 — synchronized power reads are not one atomic transaction

**Reported:** `SynchronizedPowerCoordinator`'s four power-flow reads each
acquire and release their guard separately (`async with
self._primary_guard.request(): ...` four times, one of them via
`_secondary_guard`), despite the method's own docstring claiming the block
is "uninterrupted" and the dataclass's docstring implying a single
simultaneous snapshot. Other coordinators sharing the same guard can
genuinely interleave between the four reads, so the result can be
time-skewed under load.

**Verified** exactly: four separate `async with guard.request()` blocks.

**Decision: documented as best-effort, not forced atomic — with reasoning
stated explicitly, not just asserted.** Holding one guard acquisition
across the full four-read sequence was considered and rejected. Doing so
would block every *other* coordinator sharing that guard for the entire
sequence's duration — directly undermining the multi-device fairness
Defect P (v1.3.15) was specifically built to guarantee across devices on a
shared bus. The audit itself offered this as an acceptable alternative
("...or explicitly document that the result is only a best-effort grouped
sample"), and given the trade-off, this was judged the safer choice:
introducing new lock-contention risk in the name of a display-value's
correctness is a worse trade than accepting bounded, measured skew.

**Fix, beyond documentation alone.** Both docstrings corrected to state
the accurate behaviour (best-effort, near-simultaneous, not atomic). A new
`sample_span_ms` field on `SynchronizedPowerData` measures the actual
wall-clock time between the first and last successful read in a tick, so
the real skew is visible and measured rather than silently assumed away —
a consumer (or future diagnostic) can judge how tightly grouped a given
sample actually was.

## 11. Finding 10 — adaptive controller's flush was fire-and-forget

**Reported:** `AdaptiveModbusController.stop()`'s docstring claims to
"persist synchronously," but the implementation schedules the save via
`hass.async_create_task(self._async_save())` — genuinely fire-and-forget.
The task could be cancelled (by whatever runs immediately after `stop()`)
or simply never get a chance to run before teardown finishes, losing
recently-learned adaptive parameters exactly when the system is unstable
and that data matters most.

**Verified** exactly, including the documentation/implementation mismatch.

**Fix.** New `async def async_unload(self)` performs the identical cancel
of the push-timer and any in-flight save task, then, if dirty, **awaits**
`self._async_save()` directly rather than scheduling it. This mirrors
`BatteryHealthManager.async_unload()`'s existing two-method pattern in
this exact codebase (a synchronous `stop()` for lower-stakes callers,
an async `async_unload()` where the caller can await and losing data isn't
acceptable) — not a new pattern invented for this fix, an existing one
extended to a second class that needed it. `stop()` itself is unchanged
and kept for the setup-failure rollback path (Finding 1), where losing a
few minutes of not-yet-persisted learning is judged an acceptable
trade-off for keeping that path simple. `async_unload_entry`'s teardown
loop now calls `controller.async_unload()` with the same fault-isolation
wrapper already used for `BatteryHealthManager` a few lines below it: a
failed flush must never prevent the rest of the entry from unloading
cleanly.

## 12. Adversarial verification

New `tests/test_ics_audit_v3_findings.py` (26 tests), covering all ten
findings:

- **Static (AST)**, against the real source, for every finding's
  structural shape — call sites wrapped in `wait_for`, methods existing
  with the right behaviour, docstrings no longer making inaccurate claims,
  the new dataclass field present.
- **Behavioural**, where a static check alone wouldn't be convincing:
  - Finding 5's hazard is proven real adversarially — an unguarded reset
    genuinely leaves a background task running (confirmed via direct
    `asyncio.Task` inspection), and the fixed version is confirmed to
    cancel it.
  - Finding 6's isolation is proven directly — a failing listener does not
    prevent later ones from running.
  - Finding 10's determinism is proven directly — `async_unload()`'s
    return is confirmed to happen only *after* the simulated save
    completes, not merely after it's scheduled.

**Run against the pristine pre-session baseline** (predating every defect
fixed across this entire session), **20 of the file's 23 static checks
fail correctly** (the remaining 3 tests in that same run are pure
behavioural reproductions with no static counterpart, so "failing against
old code" isn't the applicable measure for them — their adversarial proof
is the explicit old-vs-new comparison within the behavioural tests
themselves, e.g. Finding 5's `test_old_pattern_leaves_task_running`).

## 13. Safety properties

- No change to `ModbusGuard`'s own internals, the coordinator update
  logic, `register_cache.py`, or any file not listed above.
- Defects F through U (v1.3.7-v1.3.18) are untouched and still in place.
- Findings 2-8 and 10 are all additive/defensive — bounding, isolating, or
  correcting documentation — with no change to the successful-path
  behaviour of any function touched.
- Finding 9 deliberately makes no behavioural change to bus scheduling or
  locking at all — only documentation and a new, purely observational
  field.
- Finding 1 and 10 together: the adaptive controller's `stop()` method and
  its call sites elsewhere in the codebase (if any exist outside this
  release's own new call sites) are unchanged; only `async_unload_entry`'s
  teardown and the new setup-failure registration use the new
  `async_unload()`.

## 14. Test evidence

- **592 passed, 1 skipped, 0 failed**, deterministic across 3 repeated
  runs (was 566; 26 new tests).
- Adversarial: 20 of 23 applicable static checks fail against the pristine
  pre-session baseline; the full 26-test suite passes against this
  release.
- Static: `py_compile` clean on all eight changed files; manifest version
  = 1.3.19.
- Confidentiality sweep: clean.
- Diffed against the v1.3.18 tree to confirm only `__init__.py`,
  `config_flow.py`, `switch.py`, `modbus_telemetry.py`, `services.py`,
  `adaptive_modbus.py`, `synchronized_power_coordinator.py`, and
  `const.py` changed among production files.

## 15. Recommended deployment procedure

1. Delete `/config/custom_components/huawei_solar` entirely.
2. Extract v1.3.19 fresh into `custom_components/`.
3. Restart Home Assistant once.
4. No single validation step covers all ten findings — most target rare
   or failure-path behaviour (a slow config-flow probe, a reset discovery
   scan, a listener callback bug, a wedged validation read) that isn't
   part of a normal, healthy boot. The one finding independently
   observable in normal use: if `SynchronizedPowerCoordinator`'s
   diagnostics or entity attributes are ever surfaced, `sample_span_ms`
   should now be visible and typically small (well under a second) under
   healthy conditions — a large or growing value would itself be a useful
   early signal of bus contention affecting this specific coordinator.

**Verdict:** release-ready. Ten real, independently-reported defects
closed in one deliberate pass, including honest correction of this
project's own earlier scoping call (Finding 1) and a case where the audit
report itself offered two valid resolutions and the reasoning for choosing
between them is recorded explicitly (Finding 9) rather than left implicit.
