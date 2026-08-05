# Release Audit — huawei_solar v1.3.12

**Date:** 2026-08-05 · **Auditor:** Claude (Anthropic), acting on a finding
from an independent ICS audit addendum (report v2, a deeper pre-deployment
pass against the v1.3.10 package; tests and AUDIT_*.md files excluded from
that review, same as the v1 report).
**Baseline:** v1.3.11
**Type:** single defect fix, one production file changed (`sensor.py`).
Small, proportionate audit for a small, proportionate fix.

---

## 1. The report

**Defect V2-1 (Medium):** in `create_sun2000_entities()`, the bounded
write-permission probe (`_has_write_permission_bounded`, Defect H, v1.3.9)
was evaluated before the free `ucs.configuration_update_coordinator`
eligibility check, because Python's `and` short-circuits left to right and
the probe was listed second. On any device with
`CONF_ENABLE_PARAMETER_CONFIGURATION` off, the guarded entity could never
be added regardless of the probe's result — the probe still ran, spending
real Modbus traffic and up to `WRITE_PERMISSION_CHECK_TIMEOUT` for nothing,
once per ineligible device on every boot/reload.

## 2. Verified

Confirmed exactly against the actual v1.3.11 source (line numbers shifted
slightly from the report's 1244-1248 due to intervening changes in this
session, content identical):

```python
if (
    not isinstance(ucs.device.primary_device, (EMMADevice, SmartLoggerDevice))
    and await _has_write_permission_bounded(ucs.device, ucs.device.serial_number)
    and ucs.configuration_update_coordinator
):
```

This ordering predates this session entirely — confirmed present, unchanged,
in the pristine v1.3.6 baseline (with the pre-Defect-H unbounded call in
the same position). Defects H (v1.3.9) and V2-1 are related but distinct:
H made the probe itself safe to run (bounded, isolated); V2-1 is about
*whether it should run at all* for a given device. Fixing H did not fix
V2-1, and fixing V2-1 does not change H's bound — both remain necessary.

## 3. The fix

Reordered so both free checks run first:

```python
if (
    not isinstance(ucs.device.primary_device, (EMMADevice, SmartLoggerDevice))
    and ucs.configuration_update_coordinator
    and await _has_write_permission_bounded(ucs.device, ucs.device.serial_number)
):
```

No change to `_has_write_permission_bounded` itself, its timeout, or its
exception handling — this release is purely about not calling it
unnecessarily.

## 4. Adversarial verification

New `tests/test_write_permission_ordering.py`:

- An AST check locates the specific `if` condition in
  `create_sun2000_entities` (identified structurally, by containing both an
  `Await` and a `ucs.configuration_update_coordinator` attribute check
  among a `BoolOp`'s values, so it does not depend on exact line numbers)
  and asserts the coordinator check's position precedes the await's
  position. Run against the pristine pre-fix ordering (present since this
  code was first written), it fails correctly. Run against this release,
  it passes.
- A companion behavioural test confirms the general short-circuit
  semantics this fix relies on (an expensive check is not invoked when an
  earlier cheap check already fails).

## 5. Safety properties

- No change to `_has_write_permission_bounded`, its timeout, or its
  exception handling (Defect H, v1.3.9, untouched).
- No change to entity behaviour for any device that IS eligible — for
  those devices the probe still runs, with the same bound, same outcome.
  Only ineligible devices change behaviour (they now skip the probe
  entirely).
- Defects F, G, H, I, and J (v1.3.7-v1.3.11) are untouched and still in
  place.

## 6. Test evidence

- **500 passed, 1 skipped, 0 failed**, deterministic across 3 repeated
  runs (was 498; 2 new tests).
- Adversarial: fails against the pristine pre-fix ordering; passes against
  this release.
- Static: `py_compile` clean; manifest version = 1.3.12.
- Diffed against the v1.3.11 tree to confirm only `sensor.py` changed.

## 7. Recommended deployment procedure

1. Delete `/config/custom_components/huawei_solar` entirely.
2. Extract v1.3.12 fresh into `custom_components/`.
3. Restart Home Assistant once.
4. No new validation specific to this release beyond what v1.3.11 already
   required — this is a small efficiency/correctness fix on the setup
   path, not expected to be independently observable in timing on a
   system with `CONF_ENABLE_PARAMETER_CONFIGURATION` enabled for all
   devices. On an installation with a mix of eligible/ineligible devices,
   ineligible devices should show marginally faster sensor-platform setup.

**Verdict:** release-ready. Small, well-scoped, adversarially verified fix.
Second consecutive release built directly from an independent external
audit's findings, both fully confirmed against source before any code was
written — worth continuing to take these seriously going forward.
