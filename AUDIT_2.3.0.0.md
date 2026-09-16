# AUDIT — huawei_solar v2.3.0.0

**Status: IMPLEMENTATION COMPLETE.** A feature release built directly on
2.2.0.2, plus one defect fix found while building it.

**Scope in one line.** A directly-connected LUNA2000 battery's time-of-use
(TOU) periods become editable from the battery device page, in every
storage working mode, through a write path held to the same ICS/OT
discipline as every other write in this integration.

**Origin.** User report, confirmed against the source before any change:
the storage working mode select offers "Time of use", but the periods
that mode acts on had no editable UI. They were only visible as a
read-only sensor (`HuaweiSolarTOUSensorEntity`: a period count plus
attributes, filed under *Sensors*) and only writable through the
`set_tou_periods` service. Nothing in the integration hid anything based
on the working mode; the fields simply did not exist.

**Version.** Minor bump (2.2.0.2 → 2.3.0.0): a new platform and new
entities, with no change to existing entities, services or behaviour
other than HS-230-001's stricter input cap.

---

## 1. Changes

### 1.1 New: per-slot TOU period entities (`text.py`, `tou_periods.py`)

Fourteen `text` entities, **TOU period 1 … TOU period 14**, one per slot
of `STORAGE_HUAWEI_LUNA2000_TIME_OF_USE_CHARGING_AND_DISCHARGING_PERIODS`
(register 47255, 43 registers wide, up to 14 periods). They are attached
to the battery device, `EntityCategory.CONFIG`. Slots 1–4 are visible by
default. Slots 5–14 are enabled but hidden by default.

**Deliberately not gated on the working mode.** A schedule has to exist
before switching to TOU mode is useful. The inverter keeps the periods in
any mode, and they only take effect in TOU mode.

**Created only when all of these hold** (`tou_slot_entities_eligible()`):

1. Parameter configuration is enabled for the entry.
2. The entry contains **no EMMA**. When present, the EMMA is the battery
   manager; this matches the existing rule that withholds forcible
   charge/discharge services in EMMA installations.
3. The device is a SUN2000 inverter with a configuration coordinator.
4. It has a connected battery, and that battery is a Huawei LUNA2000.

**Per-slot text format.** One line of the existing service format, so
users learn a single grammar: `HH:MM-HH:MM/DAYS/FLAG`, for example
`00:00-06:00/1234567/+`. An empty string clears the slot.

**Slot semantics.** The device stores a packed list, so a slot is a
position in that list:

| Action | Result |
|---|---|
| set an existing slot | replaced in place |
| set a slot past the end | appended (becomes slot *len*+1) |
| clear an existing slot | removed; later periods shift up one slot |
| clear an empty slot | no-op, no write |

### 1.2 Fix: HS-230-001 — `set_tou_periods` schema had no length cap

**Found during this release's own review. Confirmed.**

2.2.0.2 (HS-ICS-006) added `vol.Length(max=MAX_PERIODS_STRING_LENGTH)` to
`BATTERY_TOU_PERIODS_SCHEMA` and `EMMA_TOU_PERIODS_SCHEMA`. Neither schema
has been registered with Home Assistant since 2.1.0.1 (ICS-010), when both
were replaced by the single dispatcher `set_tou_periods_dispatch` and its
own `TOU_PERIODS_DISPATCH_SCHEMA`. That dispatcher schema is the only one
actually guarding the service, and it never received the cap. An
oversized `periods` string therefore still reached full regex evaluation.
HS-ICS-006's own regression tests exercised `BATTERY_TOU_PERIODS_SCHEMA`,
which is why they passed.

**Fix.** The same cap, in the same position (before `vol.Match`), is now
applied to `TOU_PERIODS_DISPATCH_SCHEMA`.

**Tests.** The new tests assert three things:

- this schema is the one passed to `async_register` for `set_tou_periods`;
- an oversized value fails with a length error;
- normal LUNA2000, LG and empty values still pass, and the length check
  precedes the match.

Removing the cap makes two of them fail (verified by mutation).

**Not changed.** The two unregistered schemas are left in place. Existing
tests import them, and removing dead code is a separate cleanup.

### 1.3 Supporting changes

| File | Change |
|---|---|
| `__init__.py` | `Platform.TEXT` added to `PLATFORMS` |
| `strings.json`, `translations/en.json` | `entity.text.tou_period` (`"TOU period {slot}"`) and 11 `exceptions.tou_period_*` messages. Insertions only; other languages fall back to English, as they already do for recently added exception keys (e.g. `number_value_out_of_range`, absent from `nl.json`) |
| `manifest.json` | `2.2.0.2` → `2.3.0.0` |
| `tests/test_ics_audit_2201_fixes.py` | `test_manifest_version_bumped` now expects `2.3.0.0`. This assertion pins the current version by design |
| `tests/test_tou_period_text.py` | New, 96 tests |
| `README.md`, `CLAUDE.md` | User and developer documentation. CLAUDE.md's header, platform table and file tree were still at 1.3.21 and have been brought current |

---

## 2. Design decisions

### D1 — fourteen entities, not one

The first proposal was a single text entity holding the whole schedule.
It was rejected after checking Home Assistant's real source
(`homeassistant/components/text/__init__.py`, `homeassistant/const.py`).
Every state is capped at `MAX_LENGTH_STATE_STATE = 255`, and
`TextEntity.max` is `min(native_max, 255)`. A full schedule of 14 × 21
characters plus separators needs about 307 characters. A single entity
could not faithfully *display* a legitimately full schedule, which for
control equipment is disqualifying.

The other alternative was separate start, end, days and flag entities per
slot, about 56 entities. It was rejected as unusable on the device page.

### D2 — read-modify-write under one guard hold, with a fresh read

The configuration coordinator refreshes this register only on its slow
cadence, and the cache may extend that further. Editing the coordinator's
copy could silently revert a change made in the FusionSolar app since the
last poll (a lost update). Each edit therefore reads the register from
the device inside the same `_guarded_write_sequence()` hold that performs
the write. No other bus traffic, and in particular no other writer, can
interleave. One `WRITE_SEQUENCE_TIMEOUT` (30 s) bounds the read and the
write together.

### D3 — shared logical write lock

The entity takes `types.get_device_write_lock(serial)`. This is the same
registry that `services._get_device_write_lock()` delegates to, so an
entity edit and a concurrent `set_tou_periods`, forcible-charge or
similar service call on the same inverter are serialised as whole
commands.

Input validation happens **before** the lock is taken, following the
2.1.0.1 ICS-001 "validate before serialising" rule. Invalid input is
rejected immediately and never queues behind a legitimate write.

### D4 — no-op suppression

If the edited schedule equals the one just read, nothing is written and
no verification or refresh is scheduled. This avoids needless writes to
the device's configuration storage and needless bus traffic.

### D5 — two independent validation gates

`tou_periods.validate_periods()` runs before the write. It checks the
count (at most 14), `0 ≤ start < end ≤ 1440`, and pairwise overlap per
weekday. Its errors name both clashing slots and the day.

The library's own `HUAWEI_LUNA2000_TimeOfUseRegisters.encode()` then
re-validates inside `device.set()` before any bytes are sent.

A seeded randomized test (3000 schedules) shows the two gates accept
exactly the same schedules. A separate test disables the first gate and
confirms the second still blocks an overlapping schedule.

### D6 — strict input, faithful display

Input must match the service grammar exactly. The grammar was tightened
further for the entity in these ways:

| Rule | Detail |
|---|---|
| ASCII digits only | `[0-9]`, not `\d`, which accepts non-ASCII digits that `int()` parses |
| No duplicate days | e.g. `11` is rejected |
| Raw length cap | 64 characters, checked before the regex |
| No `24:00` | refused on input, as `services.py`'s `_TIME` already does |

Display, by contrast, is faithful. A device-held value this grammar would
refuse (an end time of 24:00 set by FusionSolar, an empty day set) is
still shown. For that reason the entity does **not** set HA's `pattern`
attribute: HA also applies it to the displayed state, and a legitimate
device value would then fail to render. Only a nonsensical value longer
than 21 characters (hours > 99) is shown as `unknown`, with a warning
logged.

### D7 — availability

`CoordinatorEntity.available` in real HA returns only
`coordinator.last_update_success` and ignores `_attr_available`. The
entity therefore overrides `available` to require both a healthy
coordinator and the TOU register actually being present with a list
value. See OBS-230-01 for the same issue elsewhere.

### D8 — pure core

`tou_periods.py` has no Home Assistant imports (asserted by a test). It is
exercised against the real `huawei-solar` 3.0.7 library, not a stub. The
test includes an encode/decode round trip that proves the value handed to
`schedule_verify_write()` compares equal to the value read back.

---

## 3. Hazard analysis (new write path)

| # | Hazard | Control | Evidence |
|---|---|---|---|
| H1 | Malformed or oversized input reaches the device | Length cap before regex; exact-grammar fullmatch; start < end; no duplicate days; rejected before lock or bus | `TestParseInvalid` (23 malformed inputs incl. non-ASCII digits and NUL); `test_invalid_text_rejected_before_any_bus_traffic` |
| H2 | Overlapping or excess periods written | `validate_periods()` pre-write; library `encode()` second gate | `TestValidatePeriods`, `test_equivalent_to_library_validator`, `test_overlap_rejected_nothing_written`, `test_library_validation_is_second_gate` |
| H3 | Lost update (overwrites a FusionSolar change) | Fresh device read inside the write's guard hold | `test_uses_fresh_device_read_not_stale_coordinator_data`; mutation "stale coordinator data" caught |
| H4 | Concurrent edits or service calls interleave | Shared per-serial write lock; single guard hold covering read and write | `test_shared_write_lock_serialises_with_services`, `test_concurrent_slot_edits_do_not_lose_updates`, `test_read_and_write_share_one_guard_hold`; mutation "no write lock" caught |
| H5 | Stalled device holds the bus indefinitely | `WRITE_SEQUENCE_TIMEOUT` over the whole sequence; guard admission timeouts mapped | `test_timeout_bounds_whole_sequence` (guard released afterwards), `test_guard_admission_timeout_mapped` |
| H6 | Silent failure (device ignores or rejects write) | `set()` False → error, no cache or verify side effects; library exceptions surfaced; background read-back verification | `test_write_returning_false`, `test_write_library_error`, `test_read_library_error`, `test_read_returning_non_list` |
| H7 | Stale value displayed after write | Cache invalidation, coalesced verify, refresh for all slots | `test_replace_slot`; mutations "no cache invalidation" and "no verify" caught |
| H8 | Write aimed at the wrong battery model or an EMMA-managed system | Eligibility gate: LUNA2000 only, no EMMA in entry | `TestSetupEligibility`; mutations "EMMA not excluded" and "LG not excluded" caught |
| H9 | Editable entity shown while its data is missing or BAD | `available` override | `TestDisplay` availability tests; mutation caught |
| H10 | Lock left held after an error | `async with` for lock and guard | `test_write_lock_released_after_failure`, `test_timeout_bounds_whole_sequence` |
| H11 | Unnecessary writes | No-op suppression | `test_unchanged_value_issues_no_write`, `test_clear_empty_slot_issues_no_write` |

**Mutation check.** 12 deliberate faults were injected into `text.py`,
one at a time. All 12 were caught by the new test file:

- validation removed
- stale data used
- lock removed
- no-op not skipped
- write rejection ignored
- availability override removed
- invalidation removed
- verification removed
- EMMA gate removed
- LG gate removed
- timeout mapping removed
- input parsing bypassed

Removing HS-230-001's cap is caught as well. `text.py` and `services.py`
were restored byte-for-byte afterwards.

---

## 4. Observations and limitations (not changed in this release)

**OBS-230-01 — `_attr_available` is ignored on many existing entities.**
Severity: Low/Medium.

Nine existing `CoordinatorEntity` classes set `_attr_available = False`
when their register is absent but do not override `available`, and two
more override it only conditionally:

- `HuaweiSolarNumberEntity`
- `HuaweiSolarSensorEntity`
- `HuaweiSolarTOUSensorEntity`
- `HuaweiSolarPricePeriodsSensorEntity`
- `HuaweiSolarCapacityControlPeriodsSensorEntity`
- `HuaweiSolarForcibleChargeEntity`
- `HuaweiSolarActivePowerControlModeEntity`
- `HuaweiSolarOnOffSwitchEntity`
- `StorageModeSelectEntity`
- `HuaweiSolarSwitchEntity` and `HuaweiSolarSelectEntity`, whose overrides
  apply only when `check_is_available_func` is set

In real HA (verified against the 2025.1.4 source; the newest version
installable on this sandbox's Python 3.12) `CoordinatorEntity.available`
ignores that attribute. These entities therefore show `unknown` rather
than `unavailable` and **stay operable** (writable number, select and
switch entities included) while their register is BAD or missing.

Recommended: a dedicated release that adds the same override as the new
entity, with per-platform tests. It was not bundled here because it
changes the behaviour of dozens of existing entities.

**LIM-230-01 — LG RESU is not covered.** It uses a different register and
format (`STORAGE_LG_RESU_TIME_OF_USE_PRICE_PERIODS`, 10 price periods).
The `set_tou_periods` service still supports it.

**LIM-230-02 — EMMA installations are not covered.** The EMMA register
(`EMMA_TOU_PERIODS`) and its control ownership deserve their own
decision. The service still supports it.

**LIM-230-03 — 24:00 end time is refused on input** by both the entity and
the service. The library accepts 1440 and the device may hold it. This
remains a deliberate earlier project decision (`services.py` `_TIME`).
Relaxing it for both paths is a separable change.

**LIM-230-04 — slot numbers shift.** Clearing a slot moves later periods
up, and appending lands in the next free slot. This is inherent to the
packed register and is documented in the README.

**LIM-230-05 — dead schemas.** `BATTERY_TOU_PERIODS_SCHEMA` and
`EMMA_TOU_PERIODS_SCHEMA` remain unregistered dead code (see HS-230-001).

**Inherited ceiling (unchanged).** As 2.2.0.2's audit states, a Home
Assistant integration is not a certified safety-instrumented system.
This release applies the project's existing controls; it does not claim
more.

---

## 5. Testing

**Methodology.** Unchanged from 2.2.0.2: per-file execution, compared
against a fresh, untouched 2.2.0.2 extraction run in the same
environment. `voluptuous` 0.16.0 and `huawei-solar` 3.0.7 (with `tmodbus`)
were installed for **both** runs; they are runtime requirements, and
installing them turned several previously environment-blocked tests into
real passes on both sides.

**New tests.** `tests/test_tou_period_text.py`: 96 tests, all passing,
under pytest and standalone (`python3 test_tou_period_text.py`). The
integration modules are loaded under a private package name (`hs_integ`)
so that `huawei_solar` resolves to the real library. This avoids the
package-name collision documented in `test_date.py`.

**Regression.** Every pre-existing test file gives the same pass, fail and
error counts on 2.2.0.2 and 2.3.0.0. The **failing test IDs are
identical** (17 on each side, compared directly). None involve code this
release touched; all are environment limits of this sandbox:

| Tests | Cause |
|---|---|
| `test_adaptive_modbus.py`, `test_battery_health_entities.py`, `test_battery_health_isolation.py`, `test_date.py`, `test_entities.py` (collection errors) | Need a real HA package or collide with the library name |
| `test_ics_audit_2101_fixes.py::TestDiagnosticsSerialRedaction` (6) | Expect a `custom_components` import path |
| `test_update_coordinator.py::TestStaleCacheFallbackHelper` (6) | Relative-import limitation of the harness |

The one updated pre-existing assertion (manifest version) passes.

**Static checks.**

- All 72 `.py` files parse.
- All 22 JSON files load.
- `pyflakes` reports nothing on the three new files.
- A test verifies that every `translation_key` and `TouPeriodError` key
  used in the new code exists in `strings.json` and `en.json` with
  exactly matching placeholders, and that the two files agree.

**Not tested here.** A live Home Assistant 2026.9 runtime (requires
Python ≥ 3.13, unavailable in this sandbox) and real hardware.

Recommended field validation before relying on the entities:

1. On a test schedule, set slot 1 and confirm it in FusionSolar.
2. Edit in FusionSolar, then edit a different slot in HA, and confirm
   nothing is lost.
3. Clear a slot and confirm the others shift.
4. Enter an overlapping period and confirm the error appears and the
   schedule is unchanged.
5. Check the log for the `write verification OK` debug line.

---

## 6. Upgrade notes

- No configuration or migration needed. With parameter configuration
  enabled and a directly connected LUNA2000, 14 new entities appear on
  the battery device after restart; 10 of them are hidden.
- `set_tou_periods` now rejects a `periods` value longer than 1000
  characters. The longest legitimate value is under 400.
- The existing read-only "TOU charging and discharging periods" sensor is
  unchanged.
