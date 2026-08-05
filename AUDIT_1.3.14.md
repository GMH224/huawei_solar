# Release Audit — huawei_solar v1.3.14

**Date:** 2026-08-05 · **Auditor:** Claude (Anthropic)
**Baseline:** v1.3.13
**Type:** four defect fixes from one review pass (three operator-reported,
one bonus find), four production files changed.

---

## 1. The report

After v1.3.13 closed Defect K with direct field evidence, the operator
reviewed the resulting code independently and reported three additional
issues, plus agreed with a `ConfigEntryNotReady`-wrapper idea raised in the
same discussion. A fourth was found while verifying the third. All four
were independently verified against the actual v1.3.13 source before any
fix was written, per this project's standing rule that nothing is taken on
trust — including the operator's own analysis.

## 2. Defect L — deferred first-poll task had no lifecycle of its own

**Reported:** `_schedule_deferred_first_poll()` creates a background task
with `self.hass.async_create_task(_deferred())` and keeps no handle. If the
entry reloads or unloads before the stagger delay expires, the old task
still wakes up and calls `async_request_refresh()` on a coordinator that is
no longer the active one.

**Verified.** Confirmed exactly: no stored task reference anywhere in
`_schedule_deferred_first_poll` (introduced one release earlier, v1.3.13,
as part of Defect K's fix), and `hass.async_create_task` — unlike
`entry.async_create_background_task` — is not tied to any specific config
entry's lifecycle at all. Combined with Defect J1 (v1.3.11), which made
every coordinator's `ModbusGuard` correctly resolve to the same shared
instance per physical bus, a stale task firing after reload would compete
for the exact same queue slot as the new setup's own traffic — stray,
uncoordinated reads landing at the moment the bus is busiest.

**Fix, two independent layers:**
1. `entry.async_create_background_task()` replaces the bare
   `hass.async_create_task()` call (falling back to the latter if no entry
   was supplied, matching the fallback convention already used elsewhere
   in this codebase for the same idiom). Home Assistant cancels
   entry-scoped background tasks automatically on unload — this is the
   primary fix.
2. A new `self._shutdown` flag, set via `entry.async_on_unload()`
   (registered in `HuaweiSolarUpdateCoordinator.__init__`), checked inside
   the deferred coroutine immediately before calling
   `async_request_refresh()`. This is a second, independent guard against
   the narrow race where the sleep completes right as unload begins but
   before task cancellation has propagated — belt-and-braces, not
   redundant, since either layer alone leaves a small window the other
   closes.

`HuaweiSolarUpdateCoordinator.__init__` gained an `entry: ConfigEntry | None`
parameter, threaded through from all six construction call sites in
`__init__.py` (the four inverter-path coordinators, plus the two
non-inverter-path coordinators in `_setup_device_data`, added for
consistency even though the latter never currently use a nonzero
`start_delay` and so would never exercise this code path today).

## 3. Defect M — `create_device_instance()` had no bound of its own

**Reported (by the auditor, agreed by the operator):** the traceback
captured for the v1.3.13 investigation showed
`asyncio.exceptions.CancelledError` arriving inside
`create_device_instance()`'s own vendor-library call chain — an external
cancellation from Home Assistant's own config-entry setup timeout, not
anything raised by our code.

**Verified.** `create_device_instance()` is the very first `await` in
`async_setup_entry`, with no timeout of its own. The function already has
several `except` clauses converting ordinary exceptions
(`ConnectionInterruptedException`, `ConnectionException`, `TimeoutError`,
`HuaweiSolarException`, a catch-all `Exception`) into a clean
`ConfigEntryNotReady` — but `asyncio.CancelledError` is a `BaseException`
in Python 3.8+, not an `Exception`, so none of these existing handlers
could ever have caught it. This was confirmed directly: the exact
traceback from the v1.3.13 investigation ends inside
`tmodbus/transport/async_tcp.py`'s own `asyncio.wait_for(read_future,
timeout=self.timeout)`, cancelled externally.

**Fix.** Rather than attempting to intercept and reinterpret an external
cancellation (which would fight normal asyncio cancellation semantics and
risk confusing Home Assistant's own bookkeeping), the call is now wrapped
in our own, shorter timeout:

```python
primary_device = await asyncio.wait_for(
    create_device_instance(client),
    timeout=DEVICE_CONNECT_TIMEOUT.total_seconds(),
)
```

`DEVICE_CONNECT_TIMEOUT` (45s, new in `const.py`) is chosen generously
above the ~30-40s worst case for this phase directly observed in the
field (multiple individual slow register reads during vendor-library
device detection), while remaining meaningfully shorter than the ~50s+ at
which the external cancellation was observed to fire for the whole entry
setup. On timeout, we now give up first, in a controlled way, raising our
own `ConfigEntryNotReady` with a message explaining the likely cause (the
device still completing its own reconnect) — converting a raw, alarming
external cancellation into Home Assistant's normal, well-tested retry
path.

### 3.1 A pre-existing gap, not newly introduced, noted rather than fixed

Neither this new `except TimeoutError` handler nor any of the pre-existing
ones close the underlying `client` (the raw TCP/RTU connection object) when
`create_device_instance()` fails before returning a device — because
`primary_device` is `None` in that case, and every existing handler's
cleanup step is `if primary_device is not None: await primary_device.stop()`.
This was true before this release for every exception type this phase can
raise, not something this release introduces or worsens. Flagged
explicitly for a future look, deliberately not expanded into scope
tonight.

## 4. Defect N — optimizer discovery had the same gap

**Reported:** `_setup_inverter_device_data()` awaits
`device.get_optimizer_system_information_data()` directly, outside
`ModbusGuard`, with no explicit timeout, before the optimizer coordinator
(whose own first refresh was already backgrounded — Defect G) even exists.

**Verified.** Confirmed exactly. The existing `except Exception` guard
around this call prevents it from crashing setup on an ordinary failure,
but does nothing to bound how long it can run before failing or
succeeding — the same structural gap as Defect M, one call earlier fixed,
found here because it was checked for specifically after M was identified.

**Fix.** Same pattern as Defect M:

```python
optimizer_system_infos = await asyncio.wait_for(
    device.get_optimizer_system_information_data(),
    timeout=OPTIMIZER_DISCOVERY_TIMEOUT.total_seconds(),
)
```

`OPTIMIZER_DISCOVERY_TIMEOUT` (30s, new in `const.py`) is a reasoned,
moderate bound — no direct field measurement exists yet for this specific
call's typical duration, unlike Defect M's directly-observed figures, so
this is stated as an estimate, not a measured value. A dedicated
`except TimeoutError` handler logs clearly and allows the existing
control flow to skip optimizer entities for this setup pass — identical in
effect to the pre-existing `except Exception` fallback, retried
automatically on the next reload.

## 5. Defect O — constant/comment mismatch in `switch.py`

**Found** while verifying the operator's separate report about
`switch.py`'s guard-bypassing polling loop (deferred, see §6):
`MAX_STATUS_CHANGE_TIME_SECONDS = 3000` sits beside a comment reading
*"Maximum status change time is 5 minutes"* in both `async_turn_on` and
`async_turn_off`. 3000 seconds is 50 minutes, not 5 — a 10x mismatch.

**Fix.** Corrected the constant to `300`, matching the comment's stated
intent and the physically reasonable figure for a SUN2000's actual
startup/shutdown sequence (the comments were judged correct, not the
constant, since a 50-minute worst-case poll loop was almost certainly
never the intended design).

## 6. Deliberately out of scope for this release

The operator's third finding — `switch.py`'s `async_turn_on`/
`async_turn_off` polling loop calling `device.client.get(rn.DEVICE_STATUS)`
directly, outside `ModbusGuard`, every `POLL_FREQUENCY_SECONDS` for up to
`MAX_STATUS_CHANGE_TIME_SECONDS` — was explicitly agreed to defer. It is
real and worth fixing, but structurally lower-severity than L/M/N (bounded
to a user-triggered action rather than the setup/reload critical path,
matching the same category already documented and deferred for other write
paths in `AUDIT_1.3.9.md` §5 and `AUDIT_1.3.11.md` §5). Tracked as
outstanding, not forgotten.

## 7. Adversarial verification

New `tests/test_defects_l_m_n_o.py` (13 tests), covering all four defects
in one file since they came from one review pass:

**Defect L:** a reproduction of the OLD pattern (bare task, no shutdown
concept) is shown to still fire its refresh after a simulated "unload" —
the adversarial proof that the hazard is real. A reproduction of the NEW
pattern (shutdown flag checked before refreshing) is shown to correctly
skip the refresh under the same simulated unload, and to behave
identically to before when the entry never unloads (no regression to
normal operation). Static (AST) checks confirm the real
`_schedule_deferred_first_poll` uses `entry.async_create_background_task`
(or its string name, since the actual code reaches it via `getattr`), that
the deferred coroutine checks `self._shutdown`, and that `__init__`
registers an `async_on_unload` callback.

**Defect M:** a behavioural check confirms `asyncio.wait_for` against a
never-resolving coroutine raises promptly rather than hanging. Static
checks confirm the real `async_setup_entry` wraps
`create_device_instance(...)` in `asyncio.wait_for(...)` and that a nearby
`except TimeoutError` raises `ConfigEntryNotReady`.

**Defect N:** the same behavioural pattern, plus static checks confirming
`get_optimizer_system_information_data(...)` is wrapped the same way with
its own dedicated `except TimeoutError` handler.

**Defect O:** a direct AST check of the assigned value, asserting it
equals `300`.

**Run against the pristine baseline that predates this entire session**
(v1.3.6, before any of Defects F through O existed), all four static
checks fail correctly — three with "mechanism not found" (these did not
exist before this session), and Defect O's with the exact wrong value
(3000, not 300) still present.

## 8. Safety properties

- No change to `ModbusGuard`, the adaptive controller, `register_cache.py`,
  `sensor.py`, `number.py`, or `synchronized_power_coordinator.py`. Only
  `update_coordinator.py`, `__init__.py`, `const.py`, and `switch.py`
  changed.
- Defects F through K (v1.3.7-v1.3.13) are untouched and still in place.
- Zero behavioural change for the common cases: a coordinator whose entry
  never unloads behaves identically (Defect L); a device that connects
  promptly never notices `DEVICE_CONNECT_TIMEOUT` exists (Defect M); an
  inverter with no optimizers never reaches the new bound (Defect N); a
  switch action that completes within 5 minutes behaves identically
  (Defect O, since the old 50-minute ceiling was never actually being
  exercised in practice for a working status change).
- All four fixes extend the same isolation-contract principle already
  established repeatedly this session (Defects G, H, I, J2, K): nothing on
  the setup/reload critical path should be able to block indefinitely,
  outlive its owning entry, or fail in a way Home Assistant can't retry
  cleanly.

## 9. Test evidence

- **520 passed, 1 skipped, 0 failed**, deterministic across 3 repeated
  runs (was 507; 13 new tests).
- Adversarial: all four static checks fail against the pristine
  pre-session baseline; pass against this release.
- Static: `py_compile` clean on all four changed files; manifest version =
  1.3.14.
- Confidentiality sweep: clean.
- Diffed against the v1.3.13 tree to confirm only `update_coordinator.py`,
  `__init__.py`, `const.py`, and `switch.py` changed.

## 10. Recommended deployment procedure

1. Delete `/config/custom_components/huawei_solar` entirely.
2. Extract v1.3.14 fresh into `custom_components/`.
3. Restart Home Assistant once.
4. **Required validation, specific to this release:**
   - Trigger a reload shortly after boot (within the stagger window) and
     confirm no stray coordinator activity appears for a device that
     should no longer exist post-reload — the direct test of Defect L.
   - If a slow-device-connect incident recurs, confirm the log now shows
     a clean `ConfigEntryNotReady` ("Timed out connecting to and
     identifying the inverter...") rather than a raw
     `asyncio.CancelledError` traceback — the direct test of Defect M.
   - If an optimizer-equipped installation reloads slowly, check for the
     new "optimizer discovery took longer than 30s" warning rather than a
     silent multi-ten-second stall — the direct test of Defect N.
5. No validation needed for Defect O beyond normal switch on/off use
   continuing to work as before.

**Verdict:** release-ready. Four real defects fixed from one focused
review pass — three caught independently by the operator, continuing this
session's pattern of external review finding what internal review missed,
and one more found in the course of verifying them. Nothing in this
release depends on a guess about Home Assistant's internals or the
device's exact timing characteristics that hasn't been either directly
measured (Defect M's bound) or reasoned conservatively where measurement
isn't yet available (Defect N's bound), consistent with this project's
standing rule against shipping on a prediction.
