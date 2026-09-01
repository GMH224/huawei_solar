# AUDIT — huawei_solar v2.1.0.1

**Status: EXTERNAL-AUDIT REMEDIATION COMPLETE.**

Remediates the external ICS Quality Bug Testing Report against
`huawei_solar-2.1.0.0.zip` (assessment date 2026-09-01, overall
disposition **FAIL**). That report raised 13 findings: one CRITICAL,
four HIGH, six MEDIUM (or MEDIUM/HIGH), two LOW.

**Baseline (2.1.0.0):** 1,304 passed / 5 failed / 12 errored / 1 skipped.
**Final (2.1.0.1, fresh independent extraction of the delivered zip):**
**1,338 passed / 5 failed / 12 errored / 1 skipped.**

34 tests added. The 5 failures and 12 errors are the same pre-existing
ones documented since 2.0.7 — **zero regressions**. Manifest version
validated against Home Assistant's own `ensure_strategy` check
(SIMPLEVER) before packaging.

## Disposition summary

| ID | Severity | Disposition |
|---|---|---|
| ICS-001 | CRITICAL | **Fixed** |
| ICS-002 | HIGH | **Fixed** |
| ICS-003 | MEDIUM | **Fixed** |
| ICS-004 | MEDIUM | **Fixed** |
| ICS-005 | LOW | **Fixed** (in 18 files, not the 2 reported) |
| ICS-006 | HIGH | **Fixed** (shared remediation with ICS-001) |
| ICS-007 | MEDIUM/HIGH | **Fixed** (shared remediation with ICS-001) |
| ICS-008 | HIGH | **Fixed** — but the report's stated cause was wrong; see §7 |
| ICS-009 | MEDIUM/HIGH | **Declined with evidence** — premise does not hold for this codebase; see §8 |
| ICS-010 | MEDIUM | **Fixed** (both halves) |
| ICS-011 | MEDIUM/HIGH | **Already mitigated** — the report's own remediation option 1 is implemented; test added to pin it; see §10 |
| ICS-012 | LOW/MEDIUM | **Declined with reasoning** — the fix is more dangerous than the defect; see §11 |
| ICS-013 | LOW/MEDIUM | Not a distinct finding in the report body (listed in the summary table only) |

Every finding was **verified against the actual source before being
acted on**, rather than accepted from the report. Three did not survive
that check in the form stated: ICS-008 (right finding, wrong cause),
ICS-009 (premise false here), ICS-011 (already mitigated in a file the
report did not examine).

---

## 1. ICS-001 / ICS-006 / ICS-007 — target-capability validation (CRITICAL + HIGH)

**Confirmed.** These are one structural defect applied to different
services, so they receive one shared remediation.

Service *availability* was decided from entry-wide aggregates computed
once at setup (`has_emma`, `has_lg_battery`, `has_capacity_control`),
while the *handlers* resolved a target via `_get_battery_device_data()`
and went straight to physical writes. That resolver matches device
identifiers and checks only that the inverter has *some* connected
energy storage — it never checks the resolved target's battery type, its
capacity-control support, or whether it is EMMA-managed.

In a heterogeneous installation (one entry with a direct-LUNA inverter
and an EMMA-managed inverter; or two entries with different battery
models), an `any()` true for one device registers the service globally
and a call aimed at the *other* device reaches the handler and writes to
it. These are not read-only services: they write
`STORAGE_FORCIBLE_CHARGE_POWER`,
`STORAGE_FORCIBLE_CHARGE_DISCHARGE_SETTING_MODE` and related registers to
physical equipment.

**Fix.** New `_validate_battery_target()` and
`get_validated_battery_device_data()` in `services.py`, applied to all
eight direct-control handlers: `forcible_charge`, `forcible_discharge`,
`forcible_charge_soc`, `forcible_discharge_soc`, `stop_forcible_charge`,
`set_battery_tou_periods`, `set_capacity_control_periods`,
`set_fixed_charge_periods`. Rejects, in order: non-SUN2000 targets
(EMMA reached through a battery service); targets with no battery;
battery-type mismatch where a specific model is required (ICS-006 —
LG_RESU and LUNA2000 use different period register encodings, so writing
one model's format to the other is a malformed physical write, not a
no-op); and missing capacity-control support (ICS-007).

Two deliberate choices worth stating:

- **Validation runs before the write lock**, in every handler. Rejecting
  an ineligible target must not first serialise behind a lock held by a
  legitimate in-flight write to the same device. Pinned by a test.
- **Entry-level registration gating is left in place.** It is still
  correct as a UI affordance (do not offer LG-only services where no LG
  battery exists), and removing it would expose services that were
  previously hidden. It is simply no longer relied on as the safety
  boundary — which was the report's actual point.

`set_pack_install_date` deliberately does *not* get capability
validation: it writes local metadata, not a physical register.

Raises `ServiceValidationError` (not `ValueError`) — a caller/target
mismatch is a validation problem Home Assistant surfaces to the user,
not an integration crash. Four new translation keys added to
`strings.json` and `translations/en.json`; a test asserts they exist,
since a missing key surfaces to the user as a raw key string.

---

## 2. ICS-002 — diagnostics serial leakage (HIGH)

**Confirmed.** The entity registry was exported via
`entity_entry.extended_dict` completely unredacted, while the same module
already pseudonymises serials appearing in register *values*
(`_redact_coordinator_data`). Entity unique IDs in this integration are
built directly from device serials — verified at the reported lines
(`sensor.py`, `number.py`, `select.py`, `switch.py`, `button.py`,
`date.py`) — and diagnostics are explicitly intended to be exported and
attached to support requests. This was an internal inconsistency in the
file's own privacy model, not a deliberate exemption.

**Fix.** New `_redact_entity_entry()`, applied to every registry entry,
reusing the existing `pseudonym()` scheme so a maintainer comparing two
captures still sees a stable, matchable identifier. Redaction is keyed
on the **known serials for that entry**, collected from the device
objects themselves, rather than a pattern guess at what a serial looks
like — a guess would both miss real serials and mangle unrelated
strings. Applied recursively, because `extended_dict` nests and a serial
can appear at any depth.

Verified against the report's own prescribed regression (synthetic
serials, recursive search of the complete representation): **0 raw serial
occurrences**, pseudonyms stable across calls, non-serial content
untouched.

---

## 3. ICS-003 — unguarded telemetry call on the BUSY retry path (MEDIUM)

**Confirmed, and clearly an oversight rather than an invariant.**
`self.telemetry` is declared `ModbusTelemetry | None = None`, and *every
other* call site in `update_coordinator.py` guards with
`if self.telemetry:`. Line 1154 was the sole exception, so a
`0x06 SLAVE_DEVICE_BUSY` on a coordinator with telemetry detached turned
a recoverable, expected physical condition into an `AttributeError`.

**Fix.** Guarded.

**Additional verification beyond the report.** All 18 telemetry call
sites in the module were swept. Five more appeared unguarded to a
naive proximity check but are genuinely guarded by enclosing blocks
several lines up. Line 1154 was the only real one — the report was
correct. The regression test uses an **AST walk** rather than text
proximity, so an enclosing guard is recognised correctly; this was
validated by the AST test passing while a first, text-based version of
the same test produced a false positive on a comment.

---

## 4. ICS-004 — ISO-8601 offsets relabelled instead of converted (MEDIUM)

**Confirmed and demonstrated by direct execution.**
`.replace(tzinfo=timezone.utc)` relabels an aware datetime rather than
converting it: `2026-01-01T00:00:00-05:00` became `2026-01-01T00:00:00Z`
instead of `2026-01-01T05:00:00Z` — **exactly a 5-hour error**, verified
by running both forms.

**Fix.** Both sites (`services.py::set_pack_install_date`,
`battery_health_manager.py`) now branch: naive input is still treated as
UTC (unchanged, documented service contract); aware input is genuinely
converted with `astimezone()`. Tested across naive, `Z`, `-05:00` and
`+10:00` inputs, plus an adversarial test that pins the *old* behaviour's
error magnitude so a revert is visible.

---

## 5. ICS-005 — `voltagey` typo (LOW)

**Confirmed, and broader than reported.** The report cited
`strings.json` and `translations/en.json`. It is present in **18 files** —
every translation carrying that key. All fixed; a test scans
`strings.json` plus every file in `translations/` rather than the two
reported.

---

## 6. ICS-010 — order-dependent TOU service registration (MEDIUM)

**Confirmed.** One global service name (`set_tou_periods`) was bound to
one of two different handlers depending on whether the entry happened to
contain an EMMA. With two entries of differing composition, whichever
registered last silently won for **both** — so a call aimed at a
direct-battery inverter could execute the EMMA path, or vice versa. The
existing code comment already stated the correct intent ("no direct
control of the battery is possible" with EMMA present); nothing enforced
it.

**Fix.** Registered exactly once, unconditionally, with a single
`set_tou_periods_dispatch()` that resolves the **target** and routes per
call. Registration order can no longer affect behaviour because there is
only one registration.

The dispatch schema is the permissive union of the two it replaces (the
target is unknown until the call arrives). Per-target strictness is not
lost — each underlying handler already re-checks the pattern it supports
against its own target, so EMMA still rejects a non-LUNA period string
and the direct-battery path still accepts both LUNA2000 and LG_RESU
forms.

---

## 7. ICS-008 — tmodbus transport exceptions (HIGH) — *fixed, cause corrected*

**Finding confirmed; the report's stated cause is wrong.**

The report lists `ReadException`, `ServerDeviceBusy`, `DecodeError` and
similar as unhandled. Checking the class hierarchy directly: **all of
those subclass `HuaweiSolarException`**, which *is* caught. Every
exception the report names was already covered.

The real gap, found by enumerating the installed library's exception
hierarchy: **24 tmodbus transport exceptions**
(`ServerDeviceBusyError`, `CRCError`, `ModbusConnectionError`,
`RequestRetryFailedError`, …) do **not** subclass `HuaweiSolarException`
and escaped all four handlers in both coordinators.

Corroborating evidence that these genuinely surface at this layer:
`config_flow.py` already imports `ModbusConnectionError` from tmodbus
directly. The maintainers knew; the coordinators did not.

**Fix.** Both the main and optimizer coordinators now catch
`(HuaweiSolarException, TModbusError)`. `TModbusError` was verified to
cover all 24 with none outside it. A transport failure now takes the
same controlled `UpdateFailed` + stale-cache-fallback path as every
other communication failure. The optimizer coordinator keeps its own
separate copy of this bookkeeping, so the fix was applied separately
there — as an earlier audit finding (MOD-09) noted for the equivalent
earlier fix.

---

## 8. ICS-009 — missing EMMA register migration — *declined with evidence*

**Premise does not hold for this codebase.** The report inferred this
from an upstream 2.1.1 changelog entry ("Add migration for changed EMMA
register names"), not from evidence in the audited artifact.

Verified directly: **every `rn.*` register name referenced across all
production files resolves against the installed library.** The only two
apparent misses (`rn.SOME_REGISTER`, `rn.OTHER_REGISTER`) are docstring
examples. There has been no rename here to migrate from, and
`VERSION = 1, MINOR_VERSION = 1` has never been bumped, so there is no
version gap for a migration to bridge.

Writing a migration anyway would be **worse than not writing one**: it
would iterate the entity registry and rewrite unique IDs from a mapping
table with nothing valid in it, creating exactly the
duplicate/orphaned-entity risk the finding warns about.

If this codebase later adopts an upstream library version that does
rename EMMA registers, a migration becomes genuinely necessary at that
point — this is a "not yet applicable", not a "never".

---

## 9. ICS-011 — duplicate device identity — *already mitigated*

The report rated this "Strongly indicated; requires runtime regression
test" rather than Confirmed. The reason it could not confirm it is that
the guard lives in `config_flow.py`, which the finding did not examine.

The report's own **remediation option 1** ("reject duplicate physical
device identities across entries") is already implemented, using Home
Assistant's standard mechanism:

```python
await self.async_set_unique_id(inverter_info["serial_number"])
self._abort_if_unique_id_configured(updates=data)
```

Verified to sit on the entry-creation path, immediately preceding
`async_create_entry`. Options 2 and 3 would be substantial rewrites of
target resolution to close a gap HA already closes.

Residual risk is narrow (an entry created before the guard existed, or
manually edited) and is not addressable by a code change. Two tests
added to pin the guard in place, since silent removal is the real
residual risk.

---

## 10. ICS-012 — write-lock registry never reclaims — *declined with reasoning*

The report itself states this is "not an immediate production blocker",
notes the memory cost is small, and acknowledges the code deliberately
documents the choice. Its own recommendation warns: *"Do not remove locks
while active/queued operations can still reference them"* — which is
precisely the hazard the existing docstring cites as the reason for never
removing them.

An `asyncio.Lock` is a few hundred bytes; the realistic worst case is a
handful of replaced inverters over a system's lifetime. Adding
reclamation would introduce a genuine use-after-free-style race to save
kilobytes. **That trade is bad**, and the change is declined rather than
making the code more dangerous than the defect.

---

## 11. Testing

34 tests added across two files:

**`tests/test_services.py`** (14) — the validator exists and is used by
every one of the eight write handlers; validation precedes the write
lock; LG-only and capacity-control services require their respective
capabilities; non-SUN2000 and no-battery targets are rejected;
`ServiceValidationError` not `ValueError`; all four translation keys
exist; TOU registered exactly once and not conditional on `has_emma`;
dispatcher routes by resolved target; dispatch schema is the permissive
union; the ICS-011 duplicate-serial guard exists and is on the
entry-creation path.

**`tests/test_ics_audit_2101_fixes.py`** (20) — diagnostics redaction
(unique_id, recursive, the report's own zero-raw-serials scenario,
pseudonym stability, non-serial content untouched, empty serial set,
and that the payload actually routes through the redactor); the BUSY
telemetry guard plus an AST sweep for any other unguarded call;
timezone conversion across four input forms plus an adversarial test
pinning the old error magnitude; the typo absent from all 18 files;
tmodbus exceptions are genuinely outside `HuaweiSolarException`,
`TModbusError` covers all 24, both coordinators catch it, and it is
imported.

**Three of the author's own test bugs were caught and fixed during this
work**, each worth recording because each would have produced a
misleading pass or fail:

1. A TOU-registration count matched every *mention* of the service
   constant (import block, unregister list) rather than actual
   `async_register()` calls — rewritten to count real registrations.
2. A BUSY-guard test anchored on the first textual occurrence of
   `record_busy_retry()`, which is a *comment* ~130 lines earlier, and
   looked backwards from there at unrelated code. Caught because the AST
   sweep in the same file correctly passed while this one failed.
3. Six diagnostics tests passed in isolation but failed in the full
   suite: another test module installs a stub `homeassistant.components`
   into `sys.modules`, after which `diagnostics.py`'s top-level import
   raises. That is **pre-existing suite pollution, not a defect in the
   code under test** — resolved by loading the function directly from
   source so a genuine ICS-002 regression test is not left
   order-dependent.

**One pre-existing test was updated, not weakened.**
`test_writes_via_the_shared_manager_method` inspected a fixed
2,700-character source window that the ICS-004 comment additions pushed
its assertion outside of. The assertion is unchanged; the window is now
bounded by the next function definition, so correct code growing a
comment can no longer fail it.

**Two self-inflicted edit errors were caught by compile checks** during
this work: two no-op `str_replace` operations silently deleted a
newline, joining a `def` line to its first statement. Both were caught
on the next compile, fixed, and then all four modified files were swept
for the same pattern (none remaining). Recorded because a corrupted
import line could otherwise have shipped.

---

## 12. Verification performed

- Every `.py` file compiles; `manifest.json`, `strings.json`, **all 18
  translation files** and `services.yaml` parse.
- Manifest version checked against Home Assistant's own
  `AwesomeVersion(..., ensure_strategy=[...])` validation, not assumed.
- Full suite run from a **fresh, independent extraction of the delivered
  zip**: 1,338 passed / 5 failed / 12 errored / 1 skipped — identical to
  the working tree, and matching the 2.1.0.0 baseline's failure set
  exactly.
- Each fix was run against the full suite before the next was started,
  so any regression is attributable to a specific change.

## 13. Confidence and residual risk

**High** — ICS-001/006/007, ICS-002, ICS-003, ICS-004, ICS-005,
ICS-010. Each was verified in source, fixed, and pinned by tests
exercising the real behaviour or the real structural property.

**High, with a corrected rationale** — ICS-008. The fix is broader than
the report asked for, because the report's stated cause was already
covered and the real gap was elsewhere.

**Declined, with evidence recorded** — ICS-009, ICS-012. Both
declinations are argued from this codebase rather than from the report's
framing, and both are stated here specifically so a re-auditor can
challenge them.

**Unchanged from 2.1.0.0's own open questions** — the write path remains
unmeasured under load (zero writes occurred in the 4-day field capture),
the `ADAPTIVE_FIRMWARE_CHANGE_DECAY_FACTOR = 0.25` remains an
engineering choice with no measurement behind it, and the §5 device-join
reasoning in `AUDIT_2.1.0.0.md` was **not reviewed by this external
audit** — no finding touches any code introduced in 2.1.0.0. Those
remain open rather than endorsed.
