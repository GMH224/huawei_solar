# huawei_solar 2.0.13 — Release Audit

**Scope:** a full defect batch confirmed via two independent external ICS audits of 2.0.12 (a general audit and a dedicated Modbus deep-dive) plus this project's own independent code review, together with a field-data-justified register-tier optimization identified from a real 5.7h capture and independently proposed by an external analysis document.

**Discipline applied throughout, unchanged from every prior release:** verify every finding against real source before writing a fix — several claims from the uploaded audits were checked and found stale or already addressed; adversarial test proving the specific behavior each fix provides; full suite re-run after every individual change; final verification from a fresh, independent extraction of the packaged zip.

**Final verification:** 1,244 passed, 1 skipped — confirmed identically from a fresh, independent extraction of `huawei_solar-2.0.13.zip`, matching the same known pre-existing baseline (5 failed / 12 errored, documented since 2.0.7) with zero new regressions.

---

## Audit findings reviewed but NOT acted on, with reasoning

Before implementing anything, every finding from the two uploaded audit reports was checked directly against the actual 2.0.12/2.0.13 source, not taken at face value:

- **DEF-002** ("raw Modbus client cleanup gap") — checked precisely: both exception branches at the cited location already call `_bounded_client_disconnect(client)`, a fix that shipped in 2.0.9 and remains intact. This finding is stale, most likely from the audit tool examining cached or outdated source.
- **SEM-003** (fire-and-forget save task in `stop()`) and **SEM-006** (unguarded write fallback in `button.py`) — both confirmed technically accurate, but both are pre-existing, explicitly documented, deliberate tradeoffs (the former reviewed by an earlier audit, v1.3.19; the latter documented in its own source comment as "today's existing defensive behaviour, not a new risk"). Neither represents new information; both left as-is.

## Fixes implemented, in the order built

### Optimization 2 — `state_1/state_2/state_3` moved to SLOW tier

Field-data-driven: a real 5.7h capture measured this exact three-register group (the second inverter's own status/fault registers) at a median service time of 2.63s, P95 9.4s, maximum 20.5s — an extremely poor cost/freshness ratio at NORMAL tier's own ~30s cadence. Independently proposed by an uploaded analysis document and cross-validated by this project's own earlier Phase 5.3 register-group analysis, arriving at the same specific group through a separate method. Explicitly documented as a judgment call in one respect: this project has no independent visibility into whether Huawei's own safety/control logic depends on sub-minute detection of a state transition here — flagged directly in the source comment rather than silently assumed safe.

### BH-016 — `date.py` install-date entity could display the wrong calendar day

The entity reconstructed a date from `pack_age_days`, already rounded to 1 decimal day by the engine before the entity ever saw it — up to ±72 minutes of lost precision, which could show the wrong calendar day near a UTC midnight boundary. Fixed by exposing the raw install timestamp directly (`pack_install_ts`, a new `HealthReport` attribute) and binding the entity to that instead. A dedicated regression test confirms the exact failure mode: a pack installed at 23:50 UTC now correctly shows that date, not the following day.

### BH-014 — multi-storage-unit capacity denominator was wrong

`PackCapacityTracker` divided the configured `rated_capacity_kwh` (documented as one unit's own nameplate capacity) by the *total* pack count across all storage units, not packs-per-unit. A two-unit installation therefore divided one unit's worth of capacity across both units' packs (20.7 ÷ 6 ≈ 3.45 kWh/pack) instead of each unit's own 20.7 kWh across its own 3 packs (20.7 ÷ 3 ≈ 6.9 kWh/pack) — a healthy pack could read close to 200% SOH before the configured clip. Fixed by deriving packs-per-unit directly from `slot_labels` (parsing the existing "uNpM" format), rather than importing the `PACK_COUNT` constant from `battery_health_manager.py`, which would have created a circular import (that module already imports from this one). Falls back safely to the original, pre-fix behavior for the legacy "1".."pack_count" label format, which genuinely represents a single unit and needs no correction. Does not affect the current single-unit installation this project has real field data from; confirmed via a dedicated test that the steady-state, learned-reference-capacity path (once a pack has real segments) is entirely unaffected — this fix only touches the early-life fallback.

### BH-015 — unbounded persistent state

`retired_pack_history`, `pack_first_detected`, and `pack_install_dates` grew without any retention limit, forever. Fixed with a new, deliberately generous bound (`MAX_RETIRED_PACK_HISTORY = 50`, a judgment call documented as such — comfortably covers even an unusually replacement-heavy multi-decade or multi-unit installation) and a pruning method that trims the oldest history entries and removes any serial from the two dicts that is neither currently live in any slot nor still referenced by a retained history entry — keeping the archive self-consistent rather than leaving orphaned dict entries for history that's already been trimmed away. Wired in at the one point this state actually grows (right after a new replacement is archived), confirmed end-to-end with a real replacement event, not just tested in isolation.

### An additional finding from this project's own independent review — retired pack's own lifetime discharge total was being discarded

Found during a deliberate, independent code review of this project's own recent changes (explicitly requested to ignore prior audit knowledge and look at both recent changes and less-traveled paths) — not from either external audit. The outgoing pack's own `CounterMonitor` (tracking its lifetime discharge) was replaced with a fresh, zeroed one on every detected replacement, with its final accumulated value never captured first — silently discarding exactly the number Huawei's own actual warranty terms are based on (throughput to a capacity-retention threshold). Fixed by capturing `.value` (the offset-corrected lifetime total, accounting for any counter-reset events during the pack's own service life) into the archive before the counter is replaced. A dedicated adversarial test confirms the read happens before the reset, not after.

### NEW-002 — manual multi-slave configuration validation had no aggregate deadline

`validate_serial_setup()` and `validate_network_setup()` bounded each individual sub-device connection attempt, but the overall loop had no deadline of its own — an arbitrarily long, manually-supplied slave/unit-ID list could take roughly `len(ids) × DEVICE_CONNECT_TIMEOUT` in the worst case, unbounded as a function of user input. `_connect_to_discovered_devices()` already received this exact protection for the newer auto-discovery path (DEF-006, an earlier release) — these two manual-entry paths were missed at the time. Fixed by reusing the same `DISCOVERY_TOTAL_TIMEOUT` budget in both functions, surfaced via the same `DeviceException` each function already raises for an individual connection failure, matching each function's own established error-handling style rather than introducing a new partial-success mechanism neither function was designed for.

### MOD-021 — Modbus telemetry undercounted physical wire transactions for chunked polls

`ModbusTelemetry.record_request()` was called once per *logical* poll (after `_execute_batch()` returns), incrementing `total_physical_attempts` by exactly one — regardless of how many chunks that poll actually needed. An 8-chunk poll (real examples exist in this project's own field data) contributed one physical attempt to telemetry despite generating eight real Modbus exchanges. This directly affects `total_physical_attempts` and the derived `retry_amplification` ratio — metrics this project has already used to draw real architectural conclusions (that queue sheds dominate over device-level failures) earlier this session.

Fixed carefully, given `record_request()`/`record_failure()`/`record_timeout()` are shared between the chunked main coordinator and the *unchunked* optimizer coordinator (which correctly counts 1:1 today, and needed to keep doing so): physical-attempt counting was made explicit via a new `record_physical_attempt()` method, removed from the three shared methods' own implicit behavior, called once per chunk from `_execute_batch()`'s own loop (before the outcome is known, so it's counted regardless of eventual success/failure), and added explicitly to the optimizer coordinator's own five call sites to preserve its already-correct behavior without regression. Placed once per *chunk*, not once per BUSY-retry-loop iteration, since `record_busy_retry()` already separately and correctly counts each individual retry as its own additional physical attempt — confirmed directly against that method's own pre-existing implementation before finalizing this design, specifically to avoid double-counting a retried chunk. Matches the audit's own recommended model exactly: `N chunks → N physical attempts (+ each BUSY retry)`.

### MOD-022 — adaptive circadian slots used naive host-local time, not Home Assistant's own configured timezone

`_current_slot_index()` used `datetime.now()` — naive, host-local wall-clock time, not Home Assistant's own configured timezone. Since this controller's whole architecture is explicitly circadian (learning conditions around sunrise, midday, sunset, night transitions), a host OS timezone that doesn't match HA's own configured one (common in a containerized/cloud deployment defaulting to UTC) would shift every learned slot by the timezone offset, applying parameters learned for one local operating period to a different one.

The independent review that found this also checked the rest of the file for the same underlying pattern rather than fixing only the one call site the audit named: five additional `date.today()` occurrences in the same file had the identical problem (daily-decay tracking, first-data-date bookkeeping). All six fixed together using Home Assistant's own `homeassistant.util.dt.now()` utility, confirmed to require no `hass` instance to call (it reads from a module-level default timezone HA itself sets during startup). The now-unused `datetime` import was removed; `date` remains genuinely needed elsewhere in the file (type annotations, `date.fromisoformat`). Tested with real execution (not just source pattern matching) — including a test confirming the same real moment expressed in two different configured timezones produces two genuinely different slot indices, directly proving the fix's own core guarantee rather than just that the right function name appears in source.

---

## Process notes from this batch, disclosed in the same spirit as every prior release

- Two of my own test-writing mistakes were caught and fixed during this batch: a naive `body.find("for ")` in one NEW-002 test matched the wrong occurrence in `config_flow.py` (caught by the test itself failing with a nonsensical assertion), and a MOD-022 test's own "no bare `datetime.now()`/`date.today()` remain" sweep didn't correctly track docstring continuation lines, briefly flagging legitimate explanatory prose as a live violation. Both fixed by correcting the test, not by weakening what it checks.
- The MOD-021 fix in particular required deliberately tracing the *existing*, already-correct `record_busy_retry()` behavior before designing the new per-chunk counting, specifically to avoid introducing a double-counting bug while fixing an undercounting one — documented in this audit and in the source itself, not just assumed safe.

## Final verification

- Every file in the packaged `huawei_solar-2.0.13.zip` compiles cleanly; `strings.json`, `translations/en.json`, and `services.yaml` all validate.
- Full suite, run from a **fresh, independent extraction** of that exact zip: **1,244 passed, 1 skipped**, matching the working tree and the established pre-existing baseline exactly — zero drift, zero new regressions across the entire batch.
