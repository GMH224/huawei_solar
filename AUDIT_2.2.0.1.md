# AUDIT — huawei_solar v2.2.0.1

**Status: IMPLEMENTATION COMPLETE.** A defect-remediation release built
directly on 2.2.0.0, in response to two independent external ICS
(Industrial Control Systems methodology) audits conducted against the
2.2.0.0 release: an initial four-finding test report, and a much larger
follow-up 22-finding "Mission-Critical Code Defect Report" covering
three separate adversarial review passes. Every finding from both
reports was independently re-verified against the actual 2.2.0.0 source
before any fix was written — not taken on trust — including running
the real `voluptuous` library directly to test one specific claim. One
finding (see §4, "Dropped") did not survive that verification and is
not fixed. Sixteen findings were confirmed and are fixed in this
release. Seven findings are confirmed real but explicitly deferred (see
§3), with the reasoning for each documented rather than silently
dropped.

**Testing methodology.** This project's test suite is designed for
standalone, per-file execution (`python3 tests/test_X.py`, no pytest
required — see `tests/conftest.py`). That is the methodology used to
validate this release: every fix has dedicated regression tests, run
standalone, and the entire pre-existing suite (every file this release
touched or could plausibly affect) was run both against this release
and against a fresh, untouched extraction of 2.2.0.0, file-by-file, to
distinguish genuine regressions from this sandbox's own pre-existing
dependency gaps (`tmodbus`/`huawei-solar`/`homeassistant` are not
pip-installed in the verification environment used here; five test
files — `test_adaptive_modbus.py`, `test_battery_health_entities.py`,
`test_battery_health_isolation.py`, `test_date.py`, `test_entities.py`
— fail identically on both the original and this release for exactly
that reason, confirmed by direct comparison). **Two new test files, 72
new tests, all passing:** `tests/test_ics_audit_2201_fixes.py` (23
tests, HVC-001 through HVC-004) and
`tests/test_ics_second_pass_2201_fixes.py` (49 tests, the second
report's confirmed findings). Three pre-existing tests
(`test_ics_audit_v3_findings.py::test_telemetry_stop_is_registered`,
`test_modbus_keepalive.py`'s generic-handler assertion, and
`test_tier_separation.py::test_ttl_override_is_clamped`) asserted the
exact literal old (defective) behaviour this release intentionally
changes; all three were updated to verify the new, corrected behaviour
instead, preserving each test's original intent. Two more
(`test_update_coordinator.py`'s verify-write done-callback tests) were
updated to check the same guarantees against the new, extracted
`_on_verify_write_task_done` method instead of the removed inline
lambda. **Zero unexplained regressions** — every other difference
between this release's test results and 2.2.0.0's was traced to one of
the above.

A pre-existing, unrelated fragility was also observed and is
specifically called out so it is not mistaken for something this
release introduced: running the *entire* test suite together in one
`pytest` process interrupts collection on the same 5 files as above,
and produces cross-file module-stub leakage between some other test
files (confirmed identical — down to the specific failing test names —
between this release and a fresh, untouched 2.2.0.0 extraction, with
the single exception of one test whose pass/fail outcome is itself an
artifact of that same leakage, not a real behavioural difference). This
is a pre-existing characteristic of the test suite's module-naming
approach at full-suite scale, not a defect in this release, and is out
of scope for this remediation pass.

---

## 1. Provenance of the two audits

**Audit A** (`huawei_solar-2_2_0_0_ICS_Test_Report.md`): 4 findings
(HVC-001 through HVC-004), all confirmed, all fixed. One additional
claim in that same report (a `hacs.json` version-metadata mismatch,
originally analyzed as HVC-002) was refined during review: every other
reference to this codebase's own release baseline (its internal
`AUDIT_2.2.0.0.md`, `test_ha_2026_forward_compat.py`, and the v2.2.0.0
code comments in `__init__.py`/`config_flow.py`/`services.py`
themselves) says "2026.9" — `hacs.json`'s "2025.9.0" was the *only*
occurrence of "2025.9" anywhere in the entire package, strongly
indicating a single-digit typo rather than a deeper compatibility
defect. The underlying defect and its fix are the same regardless of
root cause: HACS would let an installation happen on any HA version
from 2025.9 onward, when the actual minimum (the version introducing
`async_get_device_and_config_entry_for_domain()`, the API
`services.py` depends on) is 2026.8.

**Audit B** (`huawei_solar_2_2_0_0_ICS_Mission_Critical_Code_Defect_
Report (3).md`): 22 findings (ICS-001 through ICS-022) across three
adversarial passes. Two (ICS-002, ICS-006) are the identical defects as
HVC-003/HVC-004 under different numbering — independent cross-
confirmation, not double-counting. One (ICS-013) is dropped — see §4.
Twelve are confirmed and fixed in this release. Seven are confirmed
real but deferred to a future release — see §3.

Every finding in both reports was checked against the actual 2.2.0.0
source directly — line numbers verified, control flow traced by hand,
and in one case (ICS-013) the actual third-party library involved was
installed and executed directly rather than trusting the report's
description of its behaviour.

---

## 2. Fixes in this release

### HVC-001 — `diagnostics.py`: `_redact_coordinator_data` undefined

**Confirmed exactly as reported, and sharper than reported.** The
function was called at four separate sites in `diagnostics.py`, but its
own `def` line had been lost — the function body that should have
followed it survived only as dead, unreachable text sitting *inside*
`_redact_entity_entry`'s own function, after that function's own
`return` statement, at the same indentation (so it parsed without
error, but never ran). Every download of a config entry's diagnostics
for any entry with an inverter raised `NameError`, unconditionally —
Home Assistant's own "Download diagnostics" button, a core support-
request workflow.

The existing regression test that should have caught this
(`test_audit_v4_findings.py::test_coordinator_data_dumps_go_through_
the_redaction_helper`) only checked that the *string*
`"_redact_coordinator_data("` appeared near each call site in the
source text — it would, and did, pass even with the function
undefined. That gap is exactly what this release's own new test avoids
repeating: `test_ics_audit_2201_fixes.py` actually imports and calls
`async_get_config_entry_diagnostics()` end-to-end against a minimal but
realistic inverter coordinator payload, verifying both that no
`NameError` occurs and that serial-bearing register values are
genuinely redacted to plain, JSON-safe strings.

**Fix:** restored `_redact_coordinator_data()` as a proper module-level
function, placed between `_redact_serial_number()` and
`_redact_entity_entry()` — matching the comment inside
`_redact_entity_entry()` itself, which already said "(see
`_redact_coordinator_data` above)", confirming this was its original
intended position. Removed the orphaned dead code from inside
`_redact_entity_entry()`.

### HVC-002 — `hacs.json`: declared minimum HA version

**Fix:** `"homeassistant": "2025.9.0"` → `"2026.9.0"`. See §1 for the
typo analysis.

### HVC-003 / ICS-002 — `__init__.py`: setup rollback ends before
platform/service registration

**Confirmed exactly as reported by both audits independently.**
`async_forward_entry_setups()` and `async_setup_services()` sat *after*
the try/except block that runs `_run_cleanup_callbacks()` and
`_bounded_device_stop()` on every earlier setup failure. A failure in
either call — an entity-platform exception, a future platform
incompatibility, or `async_setup_services()` itself raising — bypassed
cleanup entirely for every resource already created earlier in that
same setup attempt (Modbus guard, keep-alive tasks, telemetry
registries, battery-health managers).

**Fix:** moved both calls to the end of the try block's own body, so
every one of the five existing except handlers (including the
catch-all `except Exception`) covers them for free — no new, separate
cleanup path was added, avoiding a second mechanism that could drift
out of sync with the first over time.

### HVC-004 / ICS-006 — `battery_health.py` /
`battery_health_manager.py`: unbounded `pack_install_dates`

**Confirmed exactly as reported by both audits independently.**
`set_pack_install_date` (services.py) deliberately accepts a pack
serial the engine has never observed — by design, per its own
docstring — and wrote it straight into the persisted
`pack_install_dates` dict with no cap of its own.
`_prune_retired_history_and_stale_serials()` already exists and already
prunes this same dict, but only as a side effect of an actual physical
pack replacement being archived; a unit that never has a pack replaced
never runs it.

**Fix:** added `MAX_PACK_INSTALL_DATE_OVERRIDES = 64` and a new single
write-path method, `PackCapacityTracker.set_pack_install_date_
override()`, enforcing the cap (evicting the oldest *irrelevant*
entries first, falling back to oldest-overall only if every remaining
entry is still relevant) while leaving the deliberate permissiveness —
accepting a never-seen serial at all — completely unchanged.
`BatteryHealthManager.set_pack_install_date()` now calls this method
instead of writing the dict directly.

### ICS-001 — `__init__.py`: unload cleanup skipped on platform-unload
failure

**Confirmed exactly as reported.** The entire body of
`async_unload_entry()` — keep-alive teardown, the shared transport
disconnect, telemetry/diagnostics/adaptive/battery-health registry
cleanup, the ModbusGuard endpoint release, the static-bound cache clear
— sat *inside* `if unload_ok := await hass.config_entries.async_
unload_platforms(entry, PLATFORMS):`. If even one platform failed to
unload, `unload_ok` was `False`, the condition was never entered, and
every one of those cleanup steps was skipped — leaving exactly the kind
of stale background state (surviving tasks, registry references) most
likely to race a subsequent reload's fresh setup.

**Fix:** de-nested the entire cleanup body so it runs unconditionally;
`entry.runtime_data` is only ever cleared by this same function's own
code, never by `async_unload_platforms()` itself, so accessing it
regardless of `unload_ok` is safe. `unload_ok` is still returned
unchanged at the end.

### ICS-003 — `number.py`: zero-value truthiness

**Confirmed, with an important severity nuance the report did not
draw out.** `native_min_value`/`native_max_value` used plain Python
truthiness (`if native_max_value:`) to decide whether a bound was
"configured" — indistinguishable from "configured as exactly 0". HA's
own `DEFAULT_MIN_VALUE` is `0.0`, so the **min**-side bug was
accidentally harmless in the common static case (falling through to
the default coincidentally reproduces the correct value) — but
`DEFAULT_MAX_VALUE` is `100.0`, so a device reporting a legitimate
dynamic or static **maximum** of exactly 0 ("no output permitted right
now") was silently replaced with 100. The min-side bug is not fully
harmless either: a *dynamic* minimum of 0 combined with a *negative*
static fallback (e.g. `ACTIVE_POWER_PERCENTAGE_DERATING`'s own real
`native_min_value=-100`, elsewhere in this same file) was also wrong
under the old code.

**Fix:** every check rewritten as an explicit `is not None` test.
Added a defense-in-depth finite/range check directly in
`async_set_native_value()` (two new translation keys,
`invalid_number_value` / `number_value_out_of_range`, added to
`strings.json` and `translations/en.json`), matching this project's own
established "duplicate validation at the final write boundary"
convention (see `SOC_SCHEMA`'s own write-boundary check).

### ICS-004 — `register_cache.py`: process-global SLOW-tier TTL

**Confirmed exactly as reported.** `_TIER_BASE_TTL` was a bare
module-level dict, mutated in place by a free function
`set_slow_tier_ttl()` called from every config entry's own setup. Two
separate inverters (two config entries) on the same Home Assistant
instance would silently fight over each other's SLOW-tier polling
interval — whichever entry set up or reloaded most recently would
change the *other* entry's cache behaviour too, with no log, no event,
and no way to detect it short of noticing an unexplained change in poll
cadence.

**Fix:** each `RegisterCache` instance now owns its own
`_tier_base_ttl` dict, seeded from the shared (and now never mutated)
defaults. `set_slow_tier_ttl()` is now an instance method.
`update_coordinator.py`'s own `RegisterCache(...)` construction reads
`entry.options.get(CONF_SLOW_TIER_TTL_S, ...)` directly at construction
time. The old module-level free function, and `__init__.py`'s own call
to it, were removed entirely — verified that a full entry reload (the
mechanism this project already uses for every options change, per its
own pre-existing v2.2.0.0 fix removing the live options-update
listener) is sufficient for a changed TTL option to take effect,
since reload tears down and reconstructs the coordinator (and hence its
`RegisterCache`) from scratch.

### ICS-007 — future pack install dates accepted without limit

**Confirmed.** Neither `date.py`'s `async_set_value()` nor `services.py`'s
`set_pack_install_date` service handler rejected a date in the future —
only a date string that failed to *parse* was ever caught. A future
`age_origin` flows into `HealthReport`'s own age computation: `age_years`
is floored at 0.0 there, but the adjacent `battery_age_days` attribute is
not — producing a visibly *negative* "age in days" reported right
alongside a zero-clamped-to-"brand new" health forecast for the same
pack, with no indication anywhere that the input date itself was the
actual problem.

**Fix:** added `BatteryHealthManager.FUTURE_INSTALL_DATE_TOLERANCE_S`
(86400 seconds / 1 day — deliberately not zero, since this is a
date-only value with no time-of-day, and someone in a UTC+12 timezone
picking "today" on their own wall clock is already past midnight UTC).
`set_pack_install_date()` — the single shared write path already used
by both callers — now raises `ValueError` for a date beyond that
tolerance. `services.py` translates this into a `ServiceValidationError`
(new `pack_install_date_in_future` translation key); `date.py` logs a
warning and refuses, matching that file's own existing local convention
for its other two guard clauses in the same method.

### ICS-009 — service (un)registration lifecycle is not
capability-aware

**Confirmed.** `_entries_with_services` was a single flat set answering
only "does *any* loaded entry need *any* service" — shared by every
independently-gated service cluster (`has_battery`, `has_lg_battery`,
`has_capacity_control`, EMMA-vs-direct-battery). Two entries with
different capabilities meant unloading one never unregistered services
specific to *its own* capability while the other stayed loaded, and,
conversely, a capability-specific service whose own last provider had
unloaded stayed registered indefinitely as long as *any* entry needed
*any* service. Impact is bounded — every affected handler still
performs its own target/device-capability validation independently —
but the discrepancy between what's registered and what's actually
usable is real and user-visible in the service picker.

**Fix:** added `_CAPABILITY_SERVICES` (a static map from capability name
to the service names it gates) and `_capability_entries` (per-capability
entry-id reference counting), populated in `async_setup_services()`
alongside the pre-existing flags, and consumed in
`async_unload_services()` to unregister exactly the clusters whose last
provider just unloaded. Deliberately additive, not a replacement: the
pre-existing flat `_entries_with_services` tracker (and its own
existing test coverage) is completely unchanged.

**Bonus fix found during this work:** `SERVICE_SET_PACK_INSTALL_DATE`
was registered (in the `has_battery and not has_emma` cluster) but had
never been included in the pre-existing flat `_ALL_SERVICE_NAMES`
unregister-all list, unlike every one of its five siblings registered
in that same block — it stayed registered indefinitely even after the
very last battery-capable entry unloaded and every other service
correctly unregistered. Added to both the new capability cluster and
the flat list.

### ICS-010 — `modbus_keepalive.py`'s probe doesn't catch `TModbusError`

**Confirmed.** `_probe()`'s own exception handler covered
`(TimeoutError, HuaweiSolarException, OSError)` — `TModbusError` was
never imported in this file at all, despite the main coordinator
(`update_coordinator.py`) explicitly importing and handling it as a
distinct, verified-separate exception hierarchy from
`HuaweiSolarException` (per that file's own comment). A `TModbusError`
raised during a keep-alive probe fell through to the outer `_run()`
loop's generic `except Exception`, which logs at DEBUG and does
nothing else — no failure-count increment, no health-state transition,
no `_on_connection_lost()` call. The health state machine this class
exists to drive silently stopped tracking connection health for an
entire class of real transport faults.

**Fix:** imported `TModbusError` and added it to `_probe()`'s handler
tuple.

### ICS-011 — telemetry singleton left permanently stopped after a
failed setup

**Confirmed.** Setup-failure rollback registered only
`telemetry.stop()` — the normal (successful) unload path always pairs
`.stop()` with `ModbusTelemetry.remove(serial)`, but the rollback path
never called `remove()`. `.stop()` only cancels the periodic-push timer
(`_unsub = None`); it does not clear the registry entry, and
`get_or_create()` never re-arms an already-registered (and now
permanently stopped) instance's timer. A retry after a failed setup got
back the exact same, permanently-dead `ModbusTelemetry` object — no
telemetry sensor for that serial would ever update again until a full
Home Assistant restart.

**Fix:** setup-failure rollback now registers a small combined
closure that calls both `.stop()` and `ModbusTelemetry.remove(serial)`,
matching the normal-unload pairing exactly, rather than a second,
separate cleanup callback that could drift out of sync with the first.

### ICS-012 — static bound cache not cleared on setup-failure rollback

**Confirmed.** `clear_static_bound_cache()` was called from exactly one
place in the whole file — the successful-unload path — never from
setup-failure rollback. A retry after a firmware update or hardware
swap that failed setup partway through would silently reuse a stale
cached bound instead of issuing a fresh Modbus read for it, for as long
as the Home Assistant process kept running.

**Fix:** setup-failure rollback now also registers
`clear_static_bound_cache(serial)` as a cleanup callback.

### ICS-014 / ICS-022 — six unbounded `battery_health.py` restore()
paths

**Confirmed as a systemic pattern, not an isolated instance — traced
across six separate spots.** `SegmentTracker.restore()`
(`segments`, `reference_epochs`), `EfficiencyTracker.restore()`
(`_baseline_pool`, `baseline_epochs`), `BalanceTracker.restore()`
(`baseline_epochs`, `_pool_dv`, `_pool_dt`), `StressAccumulator.
restore()` (`_buckets`), and the engine's own `restore()` (`_held`) all
constructed a collection directly from persisted JSON with no bound of
any kind, before any runtime pruning logic ever got a chance to run —
even for the two fields (`segments`, `_buckets`) that already have a
real, existing, time-based `prune()` method bounding them during normal
operation.

**Fix, in two parts:**

- **`reference_epochs` / `_baseline_pool` / `baseline_epochs` (x2) /
  `_pool_dv` / `_pool_dt`** — no other bound exists for these at all
  (rare-event logs, or pools cleared only once a baseline is captured).
  Added `MAX_RESTORED_EPOCH_LOG_LENGTH = 500` and a shared
  `_bounded_epoch_log()` helper, truncating to the most *recent* (not
  arbitrary) entries.
- **`segments` / `_buckets`** — both already have a real, time-based
  `prune()` method. The first implementation of this fix called that
  `prune()` directly, immediately after restore, reusing real logic
  rather than inventing a new bound — but this broke
  `test_battery_health.py`'s own round-trip test, because that test (and
  this project's own test-writing convention generally) uses synthetic,
  relative timestamps starting at `0.0`, which `prune()`'s real
  wall-clock cutoff treats as infinitely stale. Corrected to a plain
  **count** cap instead (`MAX_RESTORED_COLLECTION_LENGTH = 5000`,
  keeping the most recent entries by list order / bucket key), applied
  *before* any objects are constructed — sidestepping the synthetic-
  vs-real-time mismatch entirely while still bounding the pathological
  case. Each tracker's own real `prune()` still runs normally on the
  very next tick, on top of this, unchanged.
- **`_held`** — unlike the others, this dict's legitimate key space is
  small and fully known (exactly `"capacity"`, `"efficiency"`,
  `"balance"`, per the composite's own `live = {...}` dict elsewhere in
  this same class) — filtered to exactly that set on restore, a precise
  fix rather than a size cap, since the valid domain doesn't need one.

### ICS-019 — `verify_write()`: incomplete exception coverage and
silently-discarded task exceptions

**Confirmed, two related gaps.** `verify_write()`'s own retry loop
caught `(TimeoutError, HuaweiSolarException)` — not `TModbusError`,
unlike both of the other register-write exception handlers in this same
file. Separately, `schedule_verify_write()`'s task `add_done_callback`
only popped the task out of its own tracking dict — it never called
`task.exception()`, so any exception `verify_write()` itself didn't
already catch would surface only as asyncio's own generic, unstructured
"Task exception was never retrieved" warning instead of this
coordinator's own structured failure handling.

**Fix:** added `TModbusError` to `verify_write()`'s handler tuple.
Extracted the done-callback into its own method,
`_on_verify_write_task_done()`, which retrieves (and, in doing so,
suppresses asyncio's own generic warning for) any exception and logs it
with the same register-name context every other failure path in this
file already provides — without re-raising, since a done-callback
running inside the event loop is not somewhere a caller of the original
fire-and-forget `schedule_verify_write()` could ever observe a
re-raised exception anyway.

### ICS-020 — non-finite persisted adaptive-Modbus statistics

**Confirmed, with the exact crash traced and reproduced directly.**
`TimeSlotStats.from_dict()` did a bare `float(...)` on every persisted
field, with no finiteness check — Python's own `float()` constructor
happily accepts the strings `"nan"` / `"inf"`. `_derive_params()`'s own
`timedelta(seconds=round(poll_s))` was confirmed, by direct execution,
to raise `ValueError` for `round(float("nan"))` and `OverflowError` for
`round(float("inf"))` — a single corrupted or hand-edited persisted
value could raise out of the entire adaptive-parameter derivation.

**Fix:** added a `_finite_float()` helper (coerces to float, falling
back to a default for anything non-finite or unparseable), used
throughout `TimeSlotStats.from_dict()`. Added a second, defense-in-depth
finite check immediately before the actual `round(poll_s)` call site
itself, matching this project's own "duplicate validation at the final
command sink" convention — `poll_confidence`/`t` are already clamped
upstream in the same method, but `poll_s_derived`/`poll_s_baseline`
both ultimately trace back to module-level constants a future edit
could, in principle, disturb some other way than the one this audit
found.

### ICS-021 — optimizer coordinator telemetry double-counted on failure

**Confirmed, traced through the full call site.** `record_request(1)`
and `record_physical_attempt()` were called *before* the actual device
await — i.e. before the outcome was known at all — despite
`record_request()`'s own docstring describing it as recording a
*successful* request. Every failure path below it (`record_timeout()`'s
`"device"` kind, and every branch reaching `_record_failure()`) *also*
independently increments the exact same two counters
(`total_attempts`/`total_physical_attempts`) for its own, correct
classification of the same outcome — so a single failed optimizer poll
was counted twice. (Queue-shed and admission-timeout outcomes were
never affected: `guard.request()`'s own context manager raises before
the speculative pre-await calls are ever reached for those two kinds.)

**Fix:** moved both calls to fire exactly once, immediately *after* the
device await succeeds, rather than speculatively before it. Confirmed
by direct inspection that `modbus_telemetry.py`'s own
`self.total_attempts += 1` occurs exactly 3 times in the file (once
each in `record_request`/`record_failure`/`record_timeout`) — the fact
that made the old ordering a double-count, and that makes the new
ordering correct.

---

## 3. Confirmed real, deliberately deferred to a future release

Every finding in this section was independently verified against
source — these are not being dismissed, only deprioritized. Rushing a
cancellation-safety or transactional-rollback redesign into the same
release as sixteen other fixes, in the exact code paths this release is
otherwise trying to harden, risks introducing new lifecycle bugs faster
than it removes old ones.

- **ICS-016 / ICS-017 / ICS-018 (cancellation safety)** — confirmed as a
  real, systemic pattern: `asyncio.CancelledError` is `BaseException`,
  not `Exception`, since Python 3.8 (confirmed directly:
  `issubclass(asyncio.CancelledError, Exception)` is `False`) — every
  `except Exception:` handler across `__init__.py`'s setup/unload,
  `config_flow.py`'s 8 `contextlib.suppress(Exception)` blocks around
  awaited disconnects, and `_run_cleanup_callbacks()` itself, genuinely
  never catches it. ICS-016 additionally revealed a broader, related gap
  during verification: `await adaptive.async_load()` and `await
  keepalive.start()` both sit between resource creation and their own
  `register_cleanup(...)` call, so *any* exception (not only
  cancellation) raised exactly there orphans that resource. A correct
  fix needs careful, deliberate use of `asyncio.shield()` and/or
  `BaseException`-aware cleanup throughout several files at once — a
  focused follow-up with its own dedicated test campaign, not a
  same-release addition.
- **ICS-015 (multi-register command non-atomicity)** — confirmed:
  `_set_and_invalidate_sequence` provides mutual exclusion and a
  deadline, but no compensating write exists if, e.g.,
  `forcible_charge()`'s second register write fails after its first
  succeeded. The report's own hedge is accurate — the exact physical
  consequence of a partial write depends on Huawei's own register
  protocol semantics, which this project does not control or fully
  document — so a correct fix (compensating writes, or a documented
  "read back and reconcile" step) needs protocol-level care beyond what
  this remediation pass covers.
- **ICS-005 (untracked setup-failure save task)** — confirmed as
  described, but the code's own existing comment already documents this
  as a *deliberate*, reasoned trade-off scoped specifically to the
  setup-failure rollback path (accepting a few minutes of possible
  data loss in exchange for not blocking rollback on a save). Worth
  revisiting, not urgent.
- **ICS-008 (per-serial write-lock registry never reclaimed)** —
  confirmed as described, but the code's own docstring already
  documents this as the same deliberate pattern used for
  `ModbusGuard`/`AdaptiveModbusController` elsewhere in this codebase.
  Growth is bounded by the number of distinct physical device serials a
  household ever configures over the integration's lifetime — realistic
  worst case is dozens, not attacker-controlled input.

---

## 4. Dropped

**ICS-013 ("Critical" — SOC service schema accepts NaN/Infinity) —
independently disproven, not fixed.** The report's claimed mechanism —
"a range check implemented using ordinary `<`/`>` comparisons does not
reject NaN" — was tested directly against the actual `voluptuous`
0.15.2 library used by this project: `vol.Range(min=0, max=100)`
**correctly rejects** both `float("nan")` and `float("inf")`. Inspecting
`voluptuous.Range.__call__`'s own source confirms why: it uses the
negated comparisons `not v >= self.min` / `not v <= self.max` — the
standard NaN-safe idiom, since `NaN >= 0` is `False` in Python, making
`not False` `True`, correctly raising `Invalid`. No code change was
made in response to this finding.

---

## 5. Version

`manifest.json`: `2.2.0.0` → `2.2.0.1`. `hacs.json`:
`"homeassistant": "2025.9.0"` → `"2026.9.0"` (HVC-002, §2).
