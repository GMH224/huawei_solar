# Release Audit — huawei_solar v2.0.1

**Date:** 2026-08-10 · **Auditor:** Claude (Anthropic)
**Baseline:** v2.0.0b
**Type:** full remediation of a second-round external ICS re-audit,
scoped specifically to v2.0.0b (four mandatory High findings, H-01
through H-04, plus one borderline High/Medium finding, H-05, flagged
for correction in the same pass). No architectural work in this release
— the re-audit itself explicitly excluded AR-1/2/3/5/6/8 and deferred
telemetry-led architecture questions, consistent with the plan already
in progress (deploy 2.0.0b, gather real telemetry, decide from data).

---

## 1. Why this release exists

The operator's own plan was: external re-audit → quick review → deploy →
telemetry-driven architecture decision. The re-audit (received as a
`.docx`, reviewed against source before any code changed, matching this
project's established practice for every prior external report) found
five defects in v2.0.0b that the operator agreed should be fixed before
that deployment, not after — specifically because two of them (H-01,
H-02) directly corrupt the kind of evidence the planned telemetry
analysis depends on: H-01 can generate false connection-loss events that
look like real instability, and H-02 can make SyncPower's cache-hit
numbers look better than they honestly are. Deploying with either open
would have meant the "fact-based, not feeling" architecture decision the
operator specifically wants would have been reading from polluted data.

**Every finding was independently verified against actual source before
any fix was written** — the same discipline applied to every audit this
project has gone through. All five were confirmed genuine, not merely
plausible.

## 2. Findings — verification and remediation

### H-01 — ModbusQueueShed still classified as device connection failure

**Confirmed.** `ModbusQueueShed` is a `TimeoutError` subclass, and two
mechanisms built in prior sessions — F18's priority-lane queue-depth cap
and AR-4's airtime-budget demotion path — can genuinely raise it for a
keep-alive probe (`priority=True`). `modbus_keepalive.py`'s `_probe()`
only special-cased `ModbusAdmissionTimeout` (F08); `ModbusQueueShed` fell
through to the generic `TimeoutError` handler and was treated as a real
device failure — incrementing `_failure_count`, flipping `_healthy`
False, firing `_on_connection_lost()`.

**Fix:** a dedicated `except ModbusQueueShed as exc:` handler, positioned
before the generic one (required, since it's a subclass), mirroring
`ModbusAdmissionTimeout`'s exact treatment — logged, failure count and
health left untouched. Root cause: F08 only considered the
admission-*wait* case for this specific probe; the *shed* case became
reachable through two mechanisms added later without this file being
revisited.

### H-02 — Keep-alive connection-loss invalidation didn't propagate to all coordinators

**Confirmed.** `on_connection_lost`/`on_connection_restored` were wired
only to the main coordinator (`update_coordinator.on_connection_lost`).
`on_connection_lost()` only calls `self.cache.invalidate_all()` — its
own cache. Power-meter/energy-storage/configuration coordinators each
own a separate `RegisterCache` and never learned about a keep-alive-
detected outage. Pre-existing gap, but v2.0.0b's own MOD-01 fix made the
consequence materially worse: SyncPower's fallback now *actively and
aggressively* reuses whatever is `Quality.GOOD` in those un-invalidated
caches, so stale pre-outage values could look like legitimate fresh data.

**Fix:** callbacks re-wired once every coordinator for a device is known
(`ModbusKeepAlive`'s `_on_connection_lost`/`_on_connection_restored` are
plain, reassignable attributes) to closures reaching all four
`RegisterCache`-owning coordinators. Original main-coordinator-only
wiring kept as the initial value, not removed, so the main coordinator
stays covered even if the re-wiring step is never reached. Optimizer
coordinator deliberately excluded — verified it's a separate class with
a `dict`-based data model and no `on_connection_lost` method at all,
not an oversight. Each coordinator's own callback individually
exception-guarded so one's failure doesn't skip the rest. The original,
factually incorrect comment claiming this "already" reset all
coordinators was also corrected.

### H-03 — Finish-network discovery bypassed ModbusGuard entirely

**Confirmed.** `_connect_to_discovered_devices()` — the "finish network"
config-flow step, reachable while an entry's runtime coordinators may
already be actively polling the same endpoint — is a genuinely separate
function from everything the prior F01 fix covered, and had zero
`ModbusGuard` involvement.

**Fix:** guard acquired, every device-communication call
(`create_device_instance`, `has_write_permission`, `create_sub_device_
instance` per sub-device) wrapped in `guard.request()`, matching the
established pattern. Structured deliberately to avoid H-04's mistake
while fixing H-03: guard acquired, `try:` begins *immediately*, with
client construction and connect both inside it — nothing able to raise
in the gap between acquire and the cleanup envelope.

### H-04 — validate_network_setup leaked the endpoint guard on connect() failure

**Confirmed.** `guard = ModbusGuard.acquire_endpoint(endpoint)` was
followed by `client.connect()` — both *before* the `try:`/`finally:`
that releases the guard. Any exception, timeout, or cancellation from
`connect()` skipped the release entirely.

**Fix:** restructured with the same "nothing before the envelope"
pattern as H-03's fix — `client = None` initialized, `try:` begins
immediately after acquisition, client construction/connect/every
device call inside it, release unconditional in `finally:`. Applied the
same hardening as a precaution to `validate_network_setup_login()`,
which was checked and found *not* to have H-04's specific bug (its
`connect()` was already inside `try:`) — but its client construction was
still technically outside the envelope; fixed for consistency with the
now-established standard, including updating the `finally:` block's own
cleanup logic to correctly handle `client` now also possibly being
`None`.

### H-05 — Config-flow teardown unbounded (borderline)

**Confirmed, and found to be broader than the audit's own citation.**
The cited site (`validate_network_setup_login`'s `bridge.stop()`/
`client.disconnect()`) had no timeout at all. Checking for the same
pattern elsewhere found **seven more occurrences** of the identical
unbounded `client.disconnect()` shape across `validate_serial_setup`,
both auto-discovery wrappers, both scan-discovery wrappers, and the two
functions just fixed for H-03/H-04.

**Fix:** all eight sites bounded with `DISCONNECT_TIMEOUT` (the same
constant and reasoning already established for the normal unload path,
Defect U, and setup-failure cleanup, MOD-16) — not just the one the
audit's citation pointed at. Fault-isolation contract preserved
throughout: bounded failures are still swallowed (`contextlib.suppress`
or logged-and-continued), never allowed to replace the function's own
real return value or exception with a cleanup-phase problem.

## 3. Process notes, recorded honestly

- **Two of these five findings trace directly back to gaps in this
  project's own earlier work** (H-01 to F08/F18/AR-4; H-04 to F04/MOD-08),
  not external, unrelated defects — recorded here plainly rather than
  reframed. A prior fix addressing one aspect of a problem (F08's
  admission-timeout handling) does not guarantee the sibling aspect
  (shed handling) was also covered, especially when the sibling
  mechanism (F18's queue depth, AR-4's budget) was built in a later,
  separate session.
- **The same self-inflicted false-positive pattern occurred three
  times** while writing this release's adversarial tests: explanatory
  comments describing the *old*, buggy behavior for context happened to
  contain the exact string a test's naive `.find()` was searching for,
  matching the comment before the real code. Caught each time by the
  test failing with a confusing result and traced to the actual cause
  rather than assumed correct; fixed by searching for patterns unique to
  the real call site (e.g. `asyncio.wait_for(client.connect()` instead
  of the bare, comment-ambiguous `client.connect()`).
- **A pre-existing test-helper fragility was found and fixed at its
  source, not just patched around**: `_function_body()`'s fallback
  window (used when a function has no following `async def` for its
  primary boundary search) was already silently relying on a fixed
  4000-character limit for `validate_network_setup_login` specifically.
  H-04's fix added enough new comment text to push real content past it.
  Widened the helper itself, not just the one failing call site.

## 4. Test evidence

- **826 passed, 1 skipped, 0 failed** (was 804 at the v2.0.0b baseline;
  22 new tests, all against real implementations or precise structural
  checks, none padding).
- H-03's and H-05's most rigorous checks are AST-level sweeps (confirming
  zero bare, unguarded calls remain anywhere in the file), not per-site
  spot checks that a partial fix could slip past.
- H-01's tests directly exercise the real exception-handling code path
  with an injected `ModbusQueueShed`, not just a structural check that a
  handler exists.

## 5. Safety properties

- v2.0.0b remains available and was not modified; this release was built
  in its own working tree.
- No architectural changes — this release is entirely a defect-remediation
  pass, consistent with the re-audit's own explicit scope.
- Every fix's reasoning is recorded directly in the code, at the site of
  the change, not only in this document.

## 6. Recommended next step

Proceed with the operator's own plan, unchanged in sequence: deploy
2.0.1 (not 2.0.0b), then let AR-9's telemetry capture run and decide the
Physical Demand Planner question from that data. This release removes
the two specific ways v2.0.0b could have quietly corrupted that data
(H-01's false connection-loss events, H-02's stale-cache masquerading as
cache hits) — the fact-based decision the operator wants can now proceed
against clean measurements from the start, without the extra step of
having to first determine whether prior telemetry was itself trustworthy.

**Verdict:** all five re-audit findings fixed and adversarially tested.
No defects known to be open. Ready for deployment.
