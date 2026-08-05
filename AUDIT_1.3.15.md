# Release Audit — huawei_solar v1.3.15

**Date:** 2026-08-05 · **Auditor:** Claude (Anthropic)
**Baseline:** v1.3.14
**Type:** three defect fixes from one requested full-codebase sweep, five
production files changed. No rush — this release was explicitly scoped as
"take the time it takes."

---

## 1. The request

After six defects (F through O) surfaced incrementally across a single long
session, each found only when a specific symptom forced a look at a
specific file, the operator asked for the opposite approach: a full,
unhurried, fresh-eyes sweep of the entire runtime codebase, deliberately
without reference to any existing test or audit file, looking for bugs,
reliability issues, and safe optimisation opportunities. Three findings
came out of that sweep, all confirmed against source; the operator asked
for all three to be fixed with the same rigor as every prior release this
session, explicitly without time pressure.

## 2. Defect P — ModbusGuard multi-device last-writer-wins

### 2.1 The finding

Every `HuaweiSolarUpdateCoordinator` and `HuaweiSolarOptimizerUpdateCoordinator`
calls, every poll:

```python
if self._adaptive:
    params = self._adaptive.get_params()
    self.guard.update_gap(params.request_gap.total_seconds())
    self.guard.update_max_queue_depth(params.max_queue_depth)
```

`self._adaptive` is a per-device singleton (`AdaptiveModbusController`,
keyed by serial number). `self.guard` is the shared, per-bus singleton
(`ModbusGuard`, keyed by connection endpoint — correctly shared across
every device on one physical bus since Defect J1, v1.3.11). The setters
themselves were plain, unconditional overwrites:

```python
def update_gap(self, gap_seconds: float) -> None:
    self._effective_gap = max(MIN_INTER_REQUEST_GAP.total_seconds(), min(gap_seconds, 0.500))
```

On any installation with more than one device sharing a bus, the guard's
actual operating parameters at any instant were whichever device's
coordinator happened to poll most recently — not a reconciled view of
every device's learned conditions.

### 2.2 Why this matters, and its history

This is **"Defect C"**, named explicitly in the original 2026-08-04
handoff document (*"multi-inverter shared-bus last-writer-wins on
ModbusGuard.update_gap()/update_max_queue_depth()"*), and marked there as
absorbed into a future bus-scheduler redesign, itself pending the
request-volume investigation this session eventually completed (Defects
I, J1). This sweep re-confirmed it independently, from source, without
having consulted that document during the sweep itself.

Given the whole point of Defect J1 was to make every device's traffic
correctly share ONE guard, this defect became more consequential after
J1, not less: before J1, the synchronized-power-coordinator's traffic ran
on an entirely separate guard and couldn't participate in this clobbering
at all; after J1, every coordinator for every device genuinely shares one
guard object, so this last-writer-wins behaviour now applies to the whole
bus's real traffic, not a subset of it.

### 2.3 The fix

`update_gap(source, gap_seconds)` and `update_max_queue_depth(source, depth)`
now take the reporting device's serial number as `source`, clamp the value
exactly as before, and store it in a per-source dict
(`_gap_contributions`, `_depth_contributions`). The guard's effective
value is recomputed as the **safest** option across all current
contributors:

```python
self._effective_gap = max(self._gap_contributions.values())      # widest = safest
self._max_queue_depth = min(self._depth_contributions.values())   # shallowest = safest
```

A device needing more caution now makes the whole shared bus more
cautious, rather than being silently overridden by a sibling device's more
optimistic view.

New `remove_source(source)` drops a device's contribution (called from
both coordinator classes' unload cleanup — see §2.4) and reverts to the
guard's original single-device starting defaults (150ms gap, `MAX_QUEUE_DEPTH`
depth) once the last contributor is removed, so a torn-down device's
parameters cannot pin the aggregate forever.

### 2.4 Caller changes

Both `HuaweiSolarUpdateCoordinator._async_update_data` and
`HuaweiSolarOptimizerUpdateCoordinator._async_update_data` now pass
`self.device.serial_number` as the source. `HuaweiSolarUpdateCoordinator`'s
existing Defect L unload callback (`_mark_shutdown`, v1.3.14) was renamed
to `_on_entry_unload` and extended to also call
`self.guard.remove_source(self.device.serial_number)` — one callback, two
independent cleanup actions, both already necessary for correctness, now
co-located since they fire on the identical trigger.
`HuaweiSolarOptimizerUpdateCoordinator` is a sibling class (not a
subclass) with no stored `entry` reference; its cleanup is registered
directly in the `create_optimizer_update_coordinator()` factory function,
which already receives `entry`.

### 2.5 Why aggregation, not a bigger queue

This project already once considered, and rejected, widening
`ModbusGuard`'s queue depth globally to solve a startup-specific collision
(Defect I's investigation, `AUDIT_1.3.10.md` §4) — because doing so would
override a value learned from real conditions for the other 99.9% of the
time the bus is not in that specific state. The same reasoning applies
here even more directly: the fix does not touch what values devices
report or how they're clamped, only how multiple devices' reports combine
— a narrower, more targeted change than adjusting any single parameter's
range or default.

## 3. Defect Q — cache invalidation gaps across three files

### 3.1 The finding

`register_cache.py`'s dirty-flag mechanism (`RegisterCache.invalidate(name)`,
setting `entry.dirty = True`, consulted by the staleness filter regardless
of TTL) requires an explicit call per register name — it is not triggered
automatically by a write happening somewhere. Auditing every write path in
the codebase against this requirement found three gaps:

- `select.py`'s `StorageModeSelectEntity.async_select_option` — wrote
  `STORAGE_WORKING_MODE_SETTINGS`, called only `async_request_refresh()`.
- `button.py`'s stop-forcible-charge `async_press` — wrote four registers,
  called only `async_request_refresh()`.
- Every one of `services.py`'s ~15 write functions (forcible
  charge/discharge and their SOC variants, stop, both power-control
  managers' reset/DI-scheduling/zero-power/max-feed-grid-power (watt and
  percentage) variants, battery and EMMA TOU periods, capacity control
  periods, fixed charge periods) — called only `async_refresh()`.

`number.py`, the *other* `select.py` entity, and two classes in `switch.py`
were already correct, calling `invalidate_cache()` before requesting a
refresh — confirming this is a real, if inconsistently applied, project
convention that these paths had simply not followed.

### 3.2 Impact

`async_refresh()`/`async_request_refresh()` triggers a normal poll, which
still consults the cache's own staleness filter. A register whose TTL has
not yet naturally expired — up to 30 minutes for SLOW-tier registers per
`register_cache.py`'s own documented tiers — is served its pre-write
cached value regardless of the refresh. A user or automation triggering,
say, `forcible_charge` could see the corresponding sensor continue showing
the pre-command state for as long as that register's TTL lasted, with no
indication the command itself had failed.

### 3.3 The fix

`select.py` and `button.py`: `invalidate_cache()` (or a loop over it, for
button.py's four registers) added directly before the existing refresh
call, matching the established correct pattern from `number.py`.

`services.py`: a new helper,

```python
async def _set_and_invalidate(dd, name, value):
    result = await dd.device.set(name, value)
    if dd.configuration_update_coordinator is not None:
        dd.configuration_update_coordinator.invalidate_cache(name)
    return result
```

replaces all 39 individual `dd.device.set(...)` call sites (every one of
which is read back through `dd.configuration_update_coordinator`,
confirmed by inspection before making this centralising choice). This was
chosen over annotating each of the 39 sites individually specifically to
reduce the chance of the exact class of gap this defect already
demonstrates recurring at a fortieth site added later.

**A mistake caught and corrected during this fix, stated plainly:** the
bulk find-and-replace used to convert all 39 call sites initially matched
its own helper function's body too (`await dd.device.set(name, value)`
inside `_set_and_invalidate` itself), which would have created infinite
recursion. Caught by inspection immediately after the replace, before any
test was run against it, and corrected. Mentioned here because this
project's own process rules call for surfacing mistakes rather than
quietly folding them into a clean final diff.

## 4. Defect R — no locking between concurrent service calls

### 4.1 The finding

`switch.py`'s `HuaweiSolarOnOffSwitchEntity` holds `self._change_lock`
(an `asyncio.Lock`) across its entire on/off sequence specifically to
prevent overlapping calls to the same entity. `services.py`'s ~15
module-level write functions — each performing several sequential writes
representing one logical command — have no equivalent. Two automations
(or a user action racing an automation) calling, for example,
`forcible_charge` and `stop_forcible_charge` on the same device at nearly
the same time could have their multi-step sequences genuinely interleave.

### 4.2 Why this is a distinct risk from the already-documented one

`AUDIT_1.3.9.md` §5 already documented that a single sequence failing
partway through leaves a partially-applied state, with no rollback. This
is different: two sequences that each individually **succeed** can still
combine into a state neither one intended, because their steps landed in
an order neither caller controlled. A failed sequence is at least visible
as a failure; an interleaved pair of successful sequences is not visibly
wrong at all.

### 4.3 The fix

A per-device-serial `asyncio.Lock` registry,
`_get_device_write_lock(serial_number)`, following the same
singleton-registry pattern already used throughout this codebase
(`ModbusGuard`, `AdaptiveModbusController`, `ModbusKeepAlive`,
`ModbusTelemetry`, `BusDiagnostics`). Locks are deliberately never removed
from the registry — an idle `asyncio.Lock` costs negligible memory, and
removing one while a call might still be queued on it would be
categorically more dangerous than never removing it.

All 14 write functions (every one except the internal `_validate_power_value`
and the new `_set_and_invalidate` helper) now resolve their target device
first (a synchronous, non-blocking lookup — confirmed by inspection of
`get_battery_device_data`, `get_inverter_data`, `get_emma_device`, and
`_get_power_control_device_data`, none of which perform I/O), then wrap
everything from validation through the final coordinator refresh in
`async with _get_device_write_lock(dd.device.serial_number):`. Two calls
targeting different devices never block each other; only two calls
targeting the *same* device now serialise.

## 5. Adversarial verification

New `tests/test_sweep_findings_p_q_r.py` (21 tests), covering all three
defects in one file since they came from one sweep:

**Defect P:** a reproduction of the old (plain-overwrite) pattern is shown
to let a later, more optimistic report silently undo an earlier device's
more conservative one — the adversarial proof the hazard is real. A
reproduction of the new (per-source aggregation) pattern is shown to
always resolve to the safest option regardless of report order, for both
gap and depth; `remove_source()` is shown to correctly recompute the
aggregate after removal, revert to defaults when the last contributor is
gone, and no-op safely for an unknown source. Static (AST) checks confirm
`update_gap`/`update_max_queue_depth` both take a `source` parameter,
`remove_source` exists, and both real coordinator call sites pass
`self.device.serial_number`.

**Defect Q:** a reproduction of `_set_and_invalidate` confirms it
invalidates the written register and tolerates a missing coordinator.
Static checks confirm `StorageModeSelectEntity.async_select_option` calls
`invalidate_cache`, the button's `async_press` invalidates (directly or via
a loop over) at least four registers, `services.py` has no raw
`dd.device.set(...)` call outside the helper's own body, and the helper
itself genuinely calls `invalidate_cache`.

**Defect R:** a reproduction confirms two un-locked concurrent operations
really do interleave their steps (the adversarial proof), that the same
two operations sharing a lock always fully serialise (one completes
entirely before the other starts, regardless of which acquires first),
and that two operations on *different* devices do **not** block each
other (confirming the fix doesn't over-serialise). Static checks confirm
the lock registry helper exists and that every one of the 14 named write
functions genuinely acquires it.

**Run against the pristine pre-session baseline (v1.3.6, before any of
Defects F through R existed), all 10 static checks fail correctly.** Run
against this release, the full 21-test suite passes.

## 6. Pre-existing test maintenance

Defect P's signature change broke 10 pre-existing tests in
`tests/test_modbus_guard.py` that called `update_gap`/`update_max_queue_depth`
with the old single-argument form, plus the file's own `_fresh_guard()`
helper (which constructs a `ModbusGuard` via `object.__new__` to avoid
touching the class registry, and therefore needed the two new
`_gap_contributions`/`_depth_contributions` fields added manually). All 10
call sites updated to pass a `"test-device"` source string; the helper
updated to initialise the new fields. This is test maintenance for an
intentional API change, not a weakening of coverage — confirmed by the
adversarial run in §5 still failing correctly against the old behaviour.

## 7. Safety properties

- No change to `adaptive_modbus.py`, `register_cache.py`, `sensor.py`,
  `number.py`, `synchronized_power_coordinator.py`, `__init__.py`, or
  `const.py`.
- Defects F through O (v1.3.7-v1.3.14) are untouched and still in place.
- Defect P: zero behavioural change for single-device installations —
  with exactly one contributor, `max()`/`min()` over a one-element dict
  returns that element's own value, identical to a plain overwrite.
  Verified directly via the adversarial and static tests, not just
  asserted.
- Defect Q: no change to write semantics or return values — purely
  additive cache-invalidation calls.
- Defect R: no change to what any function writes or in what order within
  itself — purely additive serialisation against other calls to the same
  device. Different devices are verified, not just assumed, to remain
  unblocked by each other.

## 8. Test evidence

- **541 passed, 1 skipped, 0 failed**, deterministic across 3 repeated
  runs (was 520; 21 new tests).
- Adversarial: all 10 static checks fail against the pristine pre-session
  baseline; pass against this release.
- Static: `py_compile` clean on all five changed files; manifest version =
  1.3.15.
- Confidentiality sweep: clean.
- Diffed against the v1.3.14 tree to confirm only `modbus_guard.py`,
  `update_coordinator.py`, `select.py`, `button.py`, and `services.py`
  changed among production files (plus the necessary test-file update
  documented in §6).

## 9. Recommended deployment procedure

1. Delete `/config/custom_components/huawei_solar` entirely.
2. Extract v1.3.15 fresh into `custom_components/`.
3. Restart Home Assistant once.
4. **Required validation, specific to this release:**
   - On a multi-device installation, with debug logging on, confirm
     `ModbusGuard[...]` log lines reflect a stable, reconciled gap/depth
     rather than visibly flapping between values as different devices
     poll — the direct signal Defect P is taking effect. This is a subtle,
     statistical effect, not a one-shot pass/fail check; worth observing
     over a longer window, not just one boot.
   - Trigger any service call (e.g. `forcible_charge`) and confirm the
     corresponding sensor updates promptly rather than continuing to show
     the pre-command value — the direct test of Defect Q.
   - No practical way to validate Defect R without deliberately firing two
     overlapping service calls at the same device; low urgency to test
     explicitly given the fix's low risk profile (verified directly via
     the "different devices don't block" test in §5).

**Verdict:** release-ready. This closes three real defects surfaced by a
deliberate, unhurried fresh review rather than by chasing a specific
field symptom — including one, Defect P, that had been correctly
identified in this project's very first handoff document and had gone
unaddressed since. Establishing that as fixed, alongside Defects Q and R,
is exactly the "clean base" this release was requested to establish before
further work continues.
