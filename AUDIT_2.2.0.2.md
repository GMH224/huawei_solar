# AUDIT — huawei_solar v2.2.0.2

**Status: IMPLEMENTATION COMPLETE.** A defect-remediation release built
directly on 2.2.0.1, in response to a third external audit — an "ICS
Penetration Test Report" against 2.2.0.1 — conducted with knowledge of
this project's own two prior audit rounds and their fixes.

**Framing.** This audit's own verdict was that it would not sign off
2.2.0.1 for "ICS-style high-reliability control." That framing is worth
addressing directly rather than either accepting or dismissing
wholesale: some of what it describes is a genuine, permanent ceiling —
`asyncio.CancelledError` being `BaseException` rather than `Exception`
is a fact about the Python language, not this codebase, and Home
Assistant itself does not guarantee `async_unload_entry()` runs after a
failed `async_setup_entry()`. No amount of application code makes a
Home Assistant integration a certified safety-instrumented system, and
this release does not claim otherwise. But the report's *concrete,
checkable* findings are not all instances of that ceiling — most of
them are ordinary, unrelated-to-HA's-architecture input-validation and
resource-bookkeeping gaps, verified against source exactly as real
either way. Five such findings are fixed in this release. Three are
either duplicates of already-deferred findings from the prior round or
not something a source change addresses at all — see §3.

**Testing methodology.** Unchanged from the prior two releases:
standalone per-file execution (`python3 tests/test_X.py`, no pytest
required), cross-checked file-by-file against an untouched 2.2.0.1
extraction to separate genuine regressions from this sandbox's own
pre-existing dependency gaps. **One new test file, 35 new tests, all
passing:** `tests/test_ics_pentest_2202_fixes.py`. Two pre-existing
tests needed updating, both for reasons unrelated to correctness —
window sizes made stale by an intentionally-added comment block, and a
hard-coded prior-version string:

- `test_defects_l_m_n_o.py::test_source_has_dedicated_timeout_handler`
  — a fixed-size text window used to locate a pre-existing
  `except TimeoutError:` handler no longer reached it, because
  HS-ICS-002's own new comment block (registering the optimizer
  coordinator's first-refresh-task cleanup) was inserted between the
  window's start point and that handler. The handler itself, and the
  code it wraps, are unchanged — only the fixed window size was stale.
  Widened.
- `test_ics_audit_2201_fixes.py::test_manifest_version_bumped` —
  literally asserted `manifest.json`'s version equals the *previous*
  release's own version number. Updated to `2.2.0.2`.

**Zero unexplained regressions** — confirmed by running every
pre-existing test file against both this release and a fresh,
untouched 2.2.0.1 extraction and diffing the two result sets directly;
the two items above were the only differences, and both are explained
above.

---

## 1. Fixes in this release

### HS-ICS-002 — detached first-refresh tasks not cancelled on
setup-failure rollback

**Confirmed exactly as reported, across all three sites named in the
report.** `SynchronizedPowerCoordinator`'s own first-refresh task, the
optimizer coordinator's own first-refresh task, and battery-health's
own initialization task were each scheduled via `create_task(...)`
(or, on older HA cores, `hass.async_create_task(...)`) with the
returned task handle discarded immediately — not stored, not added to
`cleanup_callbacks`. A setup attempt that scheduled one of these, then
failed on a *later* step, left that task alive: it would wake up after
its own delay and call `async_request_refresh()` (or run its own
`_initialize()` body) against a coordinator or state the failed setup
attempt intended to abandon — well after `_run_cleanup_callbacks()` and
`_bounded_device_stop()` had already run.

**Fix, per site:**

- **Synchronized power coordinator** — task handle captured
  (`sync_first_refresh_task`), its `.cancel` registered directly with
  `cleanup_callbacks` at the same call site.
- **Optimizer coordinator** — `create_optimizer_update_coordinator()`
  now stores the task on the coordinator object itself
  (`coordinator._first_refresh_task`, initialized to `None` in
  `__init__`), since the task is created inside that function while
  `cleanup_callbacks` lives in its caller (`__init__.py`). The caller
  registers `optimizer_update_coordinator._first_refresh_task.cancel`
  immediately after the function returns — the same "register cleanup
  right next to resource creation" discipline already used for every
  other per-device resource in that same function (telemetry, adaptive,
  keepalive), rather than a signature change to thread a callback
  through an extra function boundary.
- **Battery health initialization** — `_async_setup_battery_health()`
  gained a `register_cleanup` parameter, threaded through from its one
  caller in `async_setup_entry` (`cleanup_callbacks.append`). Both of
  its own two HA-core branches now register cleanup: the modern
  `async_create_background_task()` branch (previously had no cleanup
  tie-in of any kind) and the older-HA-cores fallback branch (which
  already tied its task to `entry.async_on_unload()` — a *different*
  lifecycle hook that only fires on normal unload of an already-LOADED
  entry, not for a setup attempt failing before ever reaching LOADED;
  both hooks are now registered, since they cover different scenarios
  and neither makes the other redundant).

**Deliberately scoped:** this fix calls `.cancel()` on each task from
the rollback path — it does not additionally `await` each task's actual
termination with a bounded deadline before proceeding. That fuller
treatment is part of the cancellation-safety work already deferred as
HS-ICS-001/ICS-016/017/018 (see §2) — bundling it here would have
widened this fix from "stop three specific orphaned tasks" into a
piece of that larger, deliberately-deferred redesign.

### HS-ICS-003 — slave-ID parser had no size, range, or uniqueness
limit

**Confirmed, and the actual root cause was sharper than the report's
own framing.** `parse_unit_ids()` did `list(map(int, unit_ids.split
(",")))` with only a bare `ValueError` catch for non-integer tokens —
no length cap, no per-value range check, no de-duplication. Separately,
and not previously identified as a *duplicate*: the manual
(non-auto-discovery) network setup step had its own, completely
separate inline `list(map(int, ...))` call, bypassing
`parse_unit_ids()` — and therefore every validation check — entirely,
even though every other slave-ID entry point in the same file already
calls the shared function.

**Fix:** `parse_unit_ids()` now enforces:

- **Count**: 1–`MAX_SLAVE_ID_COUNT` (32) — RS-485's own
  electrical/addressing limits make a real daisy chain longer than this
  exceedingly unlikely.
- **Range**: `MIN_SLAVE_ID` (0) – `MAX_SLAVE_ID` (247) — the standard
  Modbus RTU/TCP unit-ID range. 0 is kept as valid rather than excluded,
  matching this same file's own existing auto-discovery scan list
  (`unit_ids_to_scan = [0, 100, *range(1, 17)]`), which already treats 0
  as a legitimate target.
- **Uniqueness**: duplicate IDs rejected outright.

The manual network-setup step now calls `parse_unit_ids()` instead of
its own separate inline parsing, so it inherits all three checks for
free, and any future change to the rules only needs to be made once.

**Severity note:** this is a config-flow-only input — it requires
access to this Home Assistant instance's own setup wizard, not a
remote or unauthenticated surface. The realistic trigger is a
copy-paste accident, not an attacker. Fixed for hygiene, not urgency.

### HS-ICS-004 — `MAXIMUM_FEED_GRID_POWER_SCHEMA` accepted negative
power

**Confirmed.** `vol.Range(min=-1000)` — an asymmetric, undocumented
negative floor with no stated reason anywhere in the file for what is a
*maximum* power setpoint. `_validate_power_value()` only ever enforces
an *upper* bound against the device's own reported maximum; nothing in
this file rejected a negative value at all.

**Fix:** floored at `min=0`. Chosen deliberately over removing the
bound entirely — if a legitimate negative use for this register turns
up later (e.g. a documented vendor sentinel value), that should be a
deliberate, evidenced change, not the absence of any floor.

### HS-ICS-006 — free-form service strings had no size budget

**Confirmed for the two fields the report named, and a related,
independently-discovered issue fixed alongside them.**
`DATA_PACK_SERIAL_NUMBER` and `DATA_INSTALL_DATE` were plain
`cv.string` with no length limit. Fixed with explicit, generous caps:
`MAX_PACK_SERIAL_LENGTH = 64` (a real Huawei battery pack serial is a
few dozen characters at most, per this project's own test fixtures)
and `MAX_INSTALL_DATE_LENGTH = 40` (a full ISO-8601 datetime with
fractional seconds and a UTC offset is under 35 characters).

While fixing this, a related and independently-verified issue was
found in the four TOU/capacity/fixed-charge period-string schemas:
their `vol.Match(...)` patterns bound the *number of records* a string
can contain (`{0,14}`/`{0,10}` repetition), but not each record's own
token lengths, and — checked directly against `voluptuous`'s own
`Match` implementation — `vol.Match` uses `re.match()`, which anchors
only at the *start* of the string, not the end. A string with a short,
valid, matching prefix followed by an arbitrary amount of trailing
content still passes validation. Fixed with a coarse `vol.Length(max=
MAX_PERIODS_STRING_LENGTH)` (1000, generously above the longest string
any of these four patterns' own record limits could ever legitimately
produce) applied *before* the regex, on all four schemas — confirmed
directly that `vol.All` short-circuits at the length check without
reaching the regex at all for an oversized value.

**Deliberately not fixed in this release:** narrowing each pattern's
own internal `\d+` tokens to an explicit digit-count bound (e.g.
`\d{1,6}`), or adding an explicit `$` end-anchor to each pattern
directly. Both are real, further hardening — but editing these
specific, already-validated, live-device-control regexes carries real
risk of rejecting a legitimate value if the replacement bound is even
slightly wrong, and that risk is better absorbed in a dedicated,
carefully-tested pass of its own than folded into a broader patch
release. The `vol.Length` pre-check already closes the practical
concern (unbounded string size reaching regex evaluation, and reaching
this file's own exception text) without touching that internal
structure at all.

### HS-ICS-007 — two gaps in the *prior* release's own restore-bounds
fix

**Confirmed — and both are genuine gaps in this project's own v2.2.0.1
work, not new territory.**

- **`adaptive_modbus.py`'s `TimeSlotStats.rtt_samples`** — v2.2.0.1's
  own `_finite_float()` fix addressed *finiteness* (rejecting
  NaN/Infinity) but never added a *length* cap. `record()`'s own
  runtime FIFO trim only pops one sample per new observation once the
  list exceeds `max_samples`, so a persisted `rtt_s` list far larger
  than the real runtime bound (`ADAPTIVE_RTT_SAMPLE_SIZE = 50`) stayed
  oversized for a long time after restore. Fixed: `from_dict()` now
  caps `rtt_samples` to `ADAPTIVE_RTT_SAMPLE_SIZE`, keeping the most
  recent entries (the list is append-ordered) — exactly matching what
  the FIFO trim itself would already have converged to.
- **`battery_health.py`'s `condition_coverage`** — missed entirely in
  v2.2.0.1's six-spot restore-bounds sweep. Its legitimate key space is
  small and fully known — exactly the same situation as
  `BatteryHealthEngine._held`, which *was* fixed in v2.2.0.1 with a
  precise key-set filter rather than a size cap. Given the same
  treatment here: `_CONDITION_TEMP_BUCKETS` (6 values) and
  `_CONDITION_RATE_BUCKETS` (3 values) are now named constants, and
  `_VALID_CONDITION_BUCKET_KEYS` (their 18-combination cross product)
  filters `condition_coverage` on restore. A dedicated regression test
  (`test_every_real_bucket_key_is_in_the_valid_set`) exercises the real
  `_condition_bucket_key()` function across a spread of inputs and
  asserts every string it can actually produce is a member of the new
  filter set, guarding against the two enumerations silently drifting
  apart in the future.

---

## 2. Confirmed real, not new information, still deferred

- **HS-ICS-001 (cancellation safety)** — this is the same finding as
  ICS-016/017/018 from the prior audit round, already independently
  confirmed and documented as deferred in `AUDIT_2.2.0.1.md` §3. Not
  new information; this report is independently agreeing with a
  decision already made and already explained there. No change to that
  decision: still deferred to a dedicated follow-up with its own
  focused test campaign, for the reasons already given (a correct fix
  needs careful, deliberate use of `asyncio.shield()` and/or
  `BaseException`-aware cleanup across several files at once, not a
  same-release addition on top of five other fixes).
- **HS-ICS-005 (multi-register command non-atomicity)** — the same
  finding as ICS-015 from the prior round, already deferred for the
  same reason: the correct fix depends on Huawei's own register
  protocol semantics, which this project does not control or fully
  document.

## 3. Not a source-code finding

- **HS-ICS-008 (test-assurance gap)** — an external observation that
  several of this project's own test files cannot fully execute in an
  environment lacking real `tmodbus`/`huawei-solar`/`homeassistant`
  pip packages. This is accurate, and was already disclosed, in exactly
  those terms, in `AUDIT_2.2.0.1.md`'s own "Testing methodology"
  section — not new information, and not something a source-code change
  addresses (it is resolved by running this project's test suite in a
  properly provisioned CI environment, which is outside this
  repository's own files).

---

## 4. Version

`manifest.json`: `2.2.0.1` → `2.2.0.2`. `hacs.json` unchanged (no new
Home Assistant version dependency introduced by this release).
