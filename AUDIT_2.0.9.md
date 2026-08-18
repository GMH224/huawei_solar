# huawei_solar 2.0.9 — Release Audit

**Scope:** Phases 0–4 of the Master Consolidated Recommendation compiled after the 23h telemetry capture and second-round external audit — the complete backlog of small fixes, observability instrumentation, well-justified architecture decisions, and pre-existing lifecycle/ownership defects, plus several items raised directly during this session's own review of the results.

**Discipline applied throughout, unchanged from every prior release this project:** verify every claim against real source before writing a fix; adversarial test proving the specific failure mode each fix closes; full suite re-run after every individual change; final verification from a fresh, independent extraction of the packaged zip, not the working tree.

**Final verification:** 1,091 passed, 1 skipped — confirmed identically from a fresh, independent extraction of `huawei_solar-2.0.9.zip`, matching the same known pre-existing baseline (5 failed / 12 errored, all confirmed pre-existing test-isolation-order artifacts, documented since 2.0.7) with zero new regressions introduced across the entire release.

---

## Phase 1 — Small, well-scoped fixes

| # | Item | What was found and fixed |
|---|---|---|
| 1.1 | Battery current-share threshold bug | `PackCapacityTracker.current_share_deviation_pct()`'s near-zero-mean guard (`abs(mean) < 1e-6`) was far too tight to prevent the ill-conditioning it was written to guard against. Field telemetry showed values up to ~4,020% from ordinary near-idle currents. Replaced with a real, physically-reasoned floor (`pack_current_share_min_mean_a`, a new `BatteryHealthConfig` field, default 2.0 A — a judgment call, documented as such). |
| 1.2 | Retry/BUSY telemetry counters | `0x06 SLAVE_DEVICE_BUSY` retry logic existed since v1.0.6 with zero dedicated counters. Added `total_busy_events`, `total_physical_attempts`, a `logical_attempts` alias, and computed `retry_amplification` to `ModbusTelemetry`, wired into the real retry branch in `_execute_batch()`. |
| 1.3 | Stale comment correction | `const.py`'s own comment on `BATCH_INTER_CHUNK_PAUSE` claimed it ran "inside the guard lock" — the real call site's own comment (already correct) said the opposite. Corrected the stale one. |

## Phase 2 — Observability instrumentation

A significant discovery shaped this phase: `BusDiagnostics` — a mature, tested, per-request ring-buffer capture mechanism with its own dedicated HA switch entity — already existed (since v1.3.0), separate from the aggregate telemetry switch that was actually enabled for the 23h capture. Much of what both external audits called "Priority 0/missing" was not absent from the code, only not enabled during that specific capture. Phase 2 extended this existing mechanism with the genuinely missing fields, rather than building a parallel system.

| # | Item | What was added |
|---|---|---|
| 2.1/2.4 | Transaction-level attribution | `chunk_index`, `chunk_count`, `retry_count`, `logical_request_id` (one ID shared by every physical attempt within one poll — a new per-coordinator monotonic counter), and `transition_reason` added to `_RequestContext` and threaded into `BusDiagnostics.record()`. Lets a captured file attribute an extreme event (e.g. the field-observed 102.47s/8-chunk batch) to a specific poll, chunk, and retry sequence, rather than an anonymous data point. |
| 2.2 | Transition reason retained | `notify_transition()`'s reason string used to be logged at DEBUG and discarded. Now retained (`_last_transition_reason`, `_last_transition_ts`) and exposed via a new `current_transition_reason()` accessor and in `snapshot()`. Also found, and documented without expanding scope: the class's own docstring claims three transition triggers (day/night, battery reversal, working-mode change) but only two are actually wired — battery-reversal/working-mode detection was never implemented. |
| 2.3 | Batch deadline margin | `note_batch()` now accepts and stores `deadline_margin_ms` (remaining headroom against the 120s `BATCH_POLL_DEADLINE`), computed at the real call site and exposed in `snapshot()`. |

## Phase 3 — Well-justified architecture decisions

| # | Item | Basis and implementation |
|---|---|---|
| 3.1 | `SynchronizedPowerCoordinator` made optional | Confirmed independently across two full-day field captures (96.5% and 97.09% "temporally uncertain") that the coordinator's dedicated reads almost never achieve genuine alignment, and separately confirmed hourly-energy accuracy already comes entirely from accumulated device counters, independent of this coordinator. New `CONF_SYNC_POWER_DEDICATED_READS` option (default `True`, preserving existing behaviour for every current installation) and a new `_cache_only_snapshot()` path — more lenient than the existing strict shortcut (per-field tolerance, not all-or-nothing; accepts `GOOD`/`UNCERTAIN` quality; always honestly reports `is_temporally_uncertain=True`). |
| 3.2 | MOD-02 confidence separation | Confirmed from real field data (14,473:44 and 2,815:10 transaction:poll ratios) that confidence saturated to 100% within hours purely from per-chunk observations. New `poll_n`/`poll_failures`/`poll_confidence` fields on `TimeSlotStats`, fed only by genuine poll-granularity calls. Each of the four adaptive parameters was traced individually: only `poll_interval` (genuinely a poll-level decision) was switched to blend against `poll_confidence`; `gap_ms`/`timeout_s`/`max_queue_depth` (genuinely transaction-pacing concepts) correctly continue using the original transaction-based `confidence`, unchanged — a precise, per-formula split, not a blanket replacement. |

## Phase 4 — Pre-existing lifecycle/ownership defects (11 items, all closed)

| # | Item | Fix |
|---|---|---|
| 4.1 | DEF-001 (Critical) — device construction before guard acquisition | Reordered so `bus_endpoint`/`ModbusGuard.acquire_endpoint()` happen before any client/device construction; the identification read itself now runs inside `async with guard.request(...)`. |
| 4.2 | DEF-002 — raw client cleanup ambiguity | New `_bounded_client_disconnect()` helper (mirroring the existing `_bounded_device_stop()` pattern), called on both the `TimeoutError` and generic-exception paths around `create_device_instance()`. |
| 4.3 | DEF-003 — config-flow endpoint reference leak | Five vulnerable call sites in `config_flow.py` (client creation happening before the enclosing `try:`) fixed to match the already-correct pattern proven elsewhere in the same file (`client = None` before `try`, assignment inside). |
| 4.4 | DEF-004 — service dispatch order-dependence | New `_resolve_power_control_device()` resolves the target device's real kind (EMMA vs. inverter) from the service call itself. All four power-control services now register exactly once, unconditionally — dispatch no longer depends on which config entry set up first. |
| 4.5 | DEF-005 — silent discovery skip | A sub-device discovered but then failing during `finish_network_setup` is now tracked and surfaced on the `confirm_setup` screen the user already reviews before committing (empty/no-op when nothing was skipped). `strings.json`/`translations/en.json` updated. |
| 4.6 | DEF-006 — no aggregate discovery timeout | `_connect_to_discovered_devices()`'s sub-device loop now has an explicit monotonic deadline. Deliberately not a hard `asyncio.timeout()` wrap (would discard the primary device's already-successful connection if it fired mid-loop) — a budget overrun instead feeds into the same graceful "skipped" mechanism DEF-005 built. |
| 4.7 | Write-verification coalescing (old DEF-010) | New `schedule_verify_write()` with a per-register task-tracking dict. A rapid second write to the same register now cancels the previous write's still-running verification rather than completing a wasted Modbus read for an already-superseded value. All three call sites (`number.py`, `select.py` ×2, `switch.py` ×2) updated. |
| 4.8 | Endpoint-teardown reference counting (old DEF-011) | `BusDiagnostics` and `TelemetryCapture` given real `acquire_endpoint()`/`release_endpoint()` pairs, mirroring `ModbusGuard`'s own already-established pattern exactly (including keeping the old `remove()` as an explicitly-deprecated unconditional fallback). Setup (`switch.py`) now acquires; unload (`__init__.py`) now releases instead of unconditionally removing. |
| 4.9 | Adaptive-persistence load timeout (old DEF-012) | New shared `STORAGE_LOAD_TIMEOUT` constant (10s, matching `DISCONNECT_TIMEOUT`'s own value and reasoning). Applied to **both** `AdaptiveModbusController.async_load()` *and* `BatteryHealthManager.async_initialize()` — the second site was found to have the identical gap while fixing the first, not part of the original audit's own citation. |
| 4.10 | Battery-health init task ownership | Found during this session's own log review, not either external audit. The legacy `hass.async_create_task()` fallback (for HA cores old enough to lack `async_create_background_task`) had no tie to the entry's own lifecycle. Fixed using `entry.async_on_unload()` — a much older, more universally-available HA API already relied on elsewhere in the same file — to register `task.cancel` as a matching cleanup callback. |
| 4.11 | Battery-health shutdown persistence boundary | Also found during this session's own log review. `async_unload()`'s final flush was a bare `await` with no timeout and no exception handling at all. Now wrapped in `asyncio.wait_for(..., timeout=STORAGE_LOAD_TIMEOUT)` with proper exception handling, reusing the same shared constant as 4.9 rather than inventing a second, separately-tuned one for the same underlying kind of operation. |

---

## Other items addressed during this session, outside the formal phase structure

- **Diagnostics file size limits** — both `bus_diagnostics.py` and `telemetry_capture.py`'s `MAX_FILE_BYTES` were 5MB (not 100MB, as had been assumed); bumped to 100MB per explicit request, with the real rotation-multiplier total (~600MB combined worst case, `KEEP_ROTATIONS=2`) documented in the code comment so it isn't a later surprise.
- **Register tier reclassification** — 11 registers reclassified (2 FAST→SLOW, 3 FAST→NORMAL, 6 SLOW→NORMAL), each individually verified against `_classify()` before and after. One genuine repeat of the codebase's own documented "BUG-3" pattern found and fixed (`grid_accumulated_reactive_power`, an accumulator reaching FAST via a bare substring match before its own SLOW check could run) — plus a mid-fix self-correction (an initial attempt at `day_active_power_peak` was placed in the wrong list, verified not to work, and corrected). Checked beyond the requested list per instruction: `sdongle_total_active/input/battery_power` looked like the same bug pattern but were verified as genuinely real-time site-wide sums, not accumulators — correctly left unchanged.

## Deliberately not addressed this release

- **Poll-interval "max age" question** — investigation found the main inverter/meter/battery coordinators' steady-state poll cadence is already adaptive at runtime (20–180s, this project's own Phase 3.2 work), not fixed at the 30s constant initially assumed; several different candidate constants could be "the 30s" referred to. Set aside per explicit instruction rather than guess which one and change the wrong thing.
- **Phase 5 / 5B (the bigger Modbus scheduler redesign, and pack-level battery-health promotion)** — both remain deliberately gated on data this release's own Phase 2 instrumentation and continued runtime will produce, per the Master Consolidated Recommendation's own stated dependency ordering.

## A note on this session's own process

Three separate instances of the same self-inflicted editing mistake occurred during this release (a `str_replace` operation swallowing an adjacent class's header while inserting a new test class into a large test file) — each one caught via test failures immediately after, traced precisely to its cause, and fully repaired with nothing lost. Documented here in the same spirit as every other honestly-disclosed limitation in this project's audit history, rather than omitted.

## Final verification

- Every file in the packaged `huawei_solar-2.0.9.zip` compiles cleanly.
- Full suite, run from a **fresh, independent extraction** of that exact zip: **1,091 passed, 1 skipped**, matching the working tree and the established pre-existing baseline exactly — zero drift, zero new regressions across the entire release.
