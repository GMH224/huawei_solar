# Release Audit — huawei_solar v1.3.11

**Date:** 2026-08-05 · **Auditor:** Claude (Anthropic), acting on findings
from an independent third-party ICS audit report of the v1.3.10 runtime
package (report dated 2026-08-05, scope: runtime code only, tests and
AUDIT_*.md files explicitly excluded from that review).
**Baseline:** v1.3.10
**Type:** defect fixes (three, independently reported), four production
files changed.

---

## 1. The report

An independent audit reviewed the v1.3.10 runtime package — deliberately
without access to this project's own test suite or audit history — and
traced `modbus_guard.py`, `synchronized_power_coordinator.py`, `number.py`,
`__init__.py`, `update_coordinator.py`, and `modbus_keepalive.py` in
detail. It reported three findings: one Critical (a guard-key mismatch
defeating bus-level lock sharing), one High (unbounded setup-time Modbus
reads in the number platform), and one Medium (stale dynamic entity
bounds).

**Per this project's standing rule (adversarial verification, nothing
taken on trust — including from an external source), every finding was
independently verified against the actual v1.3.10 source before any fix
was written.** All three were confirmed exactly as reported, including
exact line numbers.

## 2. Finding J1 (Critical) — guard key mismatch

**Reported:** `synchronized_power_coordinator.py` lines 212 and 219 create
`ModbusGuard` instances keyed on `inv1_device.serial_number` /
`inv2_device.serial_number`, not the shared connection endpoint every other
coordinator in the codebase uses.

**Verified.** Confirmed at the exact reported lines:

```python
self._primary_guard = ModbusGuard.get_or_create(inv1_device.serial_number)
...
self._secondary_guard = ModbusGuard.get_or_create(inv2_device.serial_number)
```

Cross-checked against the correct, established convention used everywhere
else in the codebase (`update_coordinator.py`):

```python
endpoint = bus_endpoint or device.serial_number
self.guard = ModbusGuard.get_or_create(endpoint)
```

`ModbusGuard.get_or_create()` is a registry lookup keyed by the exact
string passed in. `bus_endpoint` (e.g. `"192.168.7.22:502"`) and a
device's `serial_number` (e.g. `"HV2220098926"`) are different strings —
so this coordinator was resolving to an entirely separate `ModbusGuard`
object from the one every other coordinator on the same physical bus
shares, for both inverters. The code's own comment states the intended
behaviour precisely — *"Because both inverters are on the same
SmartLogger/SDongle TCP connection, holding the primary guard prevents
interleaving on the shared physical bus"* — but the implementation did not
achieve it: an unrelated guard object, unaware of the shared bus's queue
depth, in-flight requests, or pacing, was created and used instead.

**Impact.** On any installation with more than one device sharing a
physical bus (this project's own field installation among them — two
daisy-chained inverters), `SynchronizedPowerCoordinator`'s reads (a 10s
polling interval, continuously) were never actually serialized against the
rest of the bus's traffic. This is a plausible, additional contributor to
the multi-coordinator shedding pattern this session's field investigation
found only partially resolved by Defect I's device-aware stagger fix
(`AUDIT_1.3.10.md` §2) — Defect I addressed same-type-across-devices
collisions in the four `HuaweiSolarUpdateCoordinator`-based coordinators,
but could never have addressed collisions against a coordinator running on
an entirely different, unrelated guard object.

**Fix.** `bus_endpoint` threaded into `SynchronizedPowerCoordinator.__init__`
(from `__init__.py`, where it is already computed once per entry) and used
via the same `bus_endpoint or device.serial_number` convention as
`update_coordinator.py`. Both inverters now resolve to the exact same
shared `ModbusGuard` instance as every other coordinator on the entry.

## 3. Finding J2 (High) — unbounded number-platform setup reads

**Reported:** `number.py`'s `async_setup_entry()` awaits
`HuaweiSolarNumberEntity.create()` for every entity (lines 261-305);
`create()` performs direct `await device.client.get(...)` calls (lines
391-400) to populate static min/max values, before `async_add_entities()`
returns, not routed through `ModbusGuard`.

**Verified.** Confirmed exactly:

```python
static_max_value = (await device.client.get(description.static_maximum_key)).value
...
static_min_value = (await device.client.get(description.static_minimum_key)).value
```

No timeout, no exception handling at the call site. This is structurally
identical to Defect H (`sensor.py`'s `has_write_permission()` probe,
`AUDIT_1.3.9.md`) — a raw, unguarded device-level read on a **platform**
setup critical path, capable of stalling that entire platform's setup for
as long as the vendor library's own per-request timeout allows, and capable
of taking down every number entity on the entry if an exception other than
what the library itself catches were ever raised.

**Fix.** New `_read_static_bound()`, matching `sensor.py`'s
`_has_write_permission_bounded()` pattern exactly: bounds the read to a new
`STATIC_BOUND_READ_TIMEOUT` (5s, matching the reasoning already established
for `WRITE_PERMISSION_CHECK_TIMEOUT` — a healthy device answers quickly,
there is nothing to gain by waiting longer for a setup-time convenience
read), and catches every exception, never letting one propagate. On
timeout or failure, the bound is simply left unset — identical in effect
to the entity's own existing "no static key configured" case.

### 3.1 Deliberately not adopted: the audit's alternative remediation

The audit's suggested remediation includes *"create entities with
provisional bounds and update them asynchronously"* — architecturally the
more thorough fix, but a larger behavioural change (introducing a new
async update path for values that currently have none) than this project's
convention of shipping the smallest change that closes the confirmed risk.
The bounded-timeout approach adopted here directly matches the
already-proven, already-audited pattern from Defect H, keeping this
release's footprint consistent with the rest of this session's work. The
fuller refactor remains a reasonable candidate for a future, separately-
scoped release if setup-time cost from this specific read proves to still
matter after this fix (see `AUDIT_1.3.9.md` §7 for the project's existing
practice of tracking deferred, lower-urgency findings explicitly rather
than letting them go unrecorded).

## 4. Finding J3 (Medium) — stale dynamic bounds

**Reported:** `_handle_coordinator_update()` (lines 423-437) only assigns
`_dynamic_min_value`/`_dynamic_max_value` when the corresponding register
is present in the coordinator's data; when absent, the previous value is
retained rather than cleared.

**Verified.** Confirmed exactly:

```python
if min_register:
    self._dynamic_min_value = min_register.value
# (no else — value from a previous update is left in place)
```

**Impact.** After a transient bus issue, or a capability that stops being
reported, the entity continues advertising a stale bound indefinitely,
which can mislead UI validation or make a later write attempt fail
inconsistently.

**Fix.** Both assignments now use `register.value if register else None`,
explicitly clearing the bound when its source register disappears rather
than silently retaining a stale value.

## 5. Scope check beyond the three reported findings

The audit's own scope note states it did not review every file. Given J2's
pattern (a setup-time raw read structurally identical to Defect H, but in
a different platform file) had already slipped past this project's own
earlier review of `sensor.py` alone, the remaining platform files were
checked for the same shape:

- `select.py`, `button.py`: no `device.client.get(...)` calls of any kind.
- `switch.py`: two `device.client.get(rn.DEVICE_STATUS)` calls exist, but
  inside `async_turn_on`/`async_turn_off`'s bounded polling loop (waiting
  for a device state change after a start/stop command) — a **user-action**
  code path, not setup, and already covered by the write-path audit
  in `AUDIT_1.3.9.md` §5. Not a new instance of Defect H/J2's class.

No further instances found.

## 6. Adversarial verification

New `tests/test_ics_audit_findings.py`, organised by finding:

**J1:** a static (AST) check that `get_or_create()` is never called with a
bare `device.serial_number` (no `bus_endpoint` fallback) inside
`SynchronizedPowerCoordinator.__init__`; a check that `__init__` accepts a
`bus_endpoint` parameter; a check that `__init__.py`'s construction call
passes it; and a behavioural test confirming the actual fallback arithmetic
resolves both inverters to the identical key a normal coordinator would use
for the same bus.

**J2:** behavioural tests against fake devices (slow/failing/healthy),
mirroring Defect H's verification style exactly — confirms the bounded
helper times out instead of hanging, absorbs an exception instead of
propagating it, and still returns the correct value for a healthy device.
A static check confirms `create()` no longer calls `device.client.get(...)`
directly, and that `_read_static_bound()` exists and genuinely uses
`asyncio.wait_for`.

**J3:** a static check that both dynamic-bound assignments use the
`X.value if X else None` conditional form (the fixed shape) rather than an
`if X: assign` statement (the shape that leaves stale values in place); a
direct behavioural check of the same semantics.

**Run against the pre-fix files (the pristine, never-modified v1.3.6
baseline, which still contains all three original defects unchanged), all
six static checks fail — correctly, at the exact reported/original line
numbers (212, 219 for J1; 394, 400 for J2).** Run against this release, the
full 11-test suite passes.

## 7. Safety properties

- No change to `ModbusGuard`'s own internals, the adaptive controller, or
  register-cache logic. `modbus_guard.py`, `adaptive_modbus.py`,
  `register_cache.py`, `update_coordinator.py`, `sensor.py` are
  byte-identical to the audited v1.3.10 tree.
- Defects F, G, H, and I (v1.3.7-v1.3.10) are untouched and still in place.
- J1's fix is purely a key-resolution change — no change to guard
  acquisition/release ordering, timeout behaviour, or the
  primary/secondary two-guard-reference pattern the original code used
  (preserved exactly, now correctly resolving to one shared object instead
  of two unrelated ones).
- J2 and J3 both extend the same isolation-contract pattern already
  established three times this session (Defects G, H, and now J2) —
  nothing optional should be able to block or crash a platform's setup.

## 8. Test evidence

- **498 passed, 1 skipped, 0 failed**, deterministic across 3 repeated runs
  (was 487; 11 new tests in `test_ics_audit_findings.py`).
- Adversarial: all 6 static checks fail against the pristine pre-fix
  baseline at the exact reported lines; pass against this release.
- Static: `py_compile` clean on all four changed files; manifest version =
  1.3.11.
- Confidentiality sweep: clean.
- Diffed against the v1.3.10 tree to confirm only
  `synchronized_power_coordinator.py`, `number.py`, `const.py`, and
  `__init__.py` changed.

## 9. Recommended deployment procedure

1. **Delete** `/config/custom_components/huawei_solar` entirely.
2. Extract v1.3.11 fresh into `custom_components/`.
3. Restart Home Assistant once.
4. **Required validation, specific to this release:** with debug logging
   enabled, trigger a reload and check for `ModbusGuard[...]` log lines
   referencing the synchronized-power-coordinator's traffic — they should
   now show the SAME endpoint key (`bus_endpoint`, e.g.
   `192.168.7.22:502`) as every other coordinator's guard messages, not a
   device serial number. This is the direct confirmation that J1 is fixed.
5. Continue the startup-timing investigation from `AUDIT_1.3.10.md` §9:
   time the "still starting up" banner and check whether the broader
   multi-coordinator shedding pattern (not just the specific pairing
   Defect I targeted) has reduced, now that the synchronized coordinator's
   traffic is properly serialized against the rest of the bus for the
   first time.

**Verdict:** release-ready. Three independently-reported, independently-
verified defects fixed — one of them (J1) potentially the most significant
single defect found in this entire investigation, since it meant a
continuously-polling coordinator had been running outside this project's
core bus-serialization guarantee since `synchronized_power_coordinator.py`
was introduced, on every installation with a shared physical bus. Credit to
the external audit for finding it — this project's own review across four
prior releases in this session did not.
