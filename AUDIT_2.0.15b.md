# huawei_solar 2.0.15b — Release Audit

**Scope:** Corrective release fixing two real, reported defects in 2.0.15: telemetry that reported a per-device requested value under a field name implying it was the real, measured one governing the shared bus; and three features (excitation enable/disable/resume-after-halt) that required a Developer Tools service call, with no standalone config-driven path. Both were reported directly against a real, live deployment, not found by internal review first.

**This release does not change 2.0.15's own core excitation mechanism** — `ExcitationController`'s schedule, its go/no-go safety monitor, its bounds. Nothing about *why* GAP/POLL excitation exists or how the schedule progresses changes here. This release fixes how the system reports what it is actually doing, and how a person controls it, without touching the underlying safety-critical logic itself.

**Final verification:** 1,332 passed, 1 skipped — confirmed identically from a fresh, independent extraction of `huawei_solar-2.0.15b.zip`, matching the established pre-existing baseline (5 failed / 12 errored, documented since 2.0.7) with zero new regressions. 1,332 = 1,262 (2.0.14 baseline) + 35 (`ExcitationController` tests, 2.0.15) + 19 (elevated-permissions relocation, 2.0.15) + 2 (telemetry-fix tests, this release) + 7 (button entity tests, this release) + 7 (toggle-wiring tests, this release), with the 10 tests for the removed services (`test_excitation_services.py`) deleted outright rather than left failing against functionality that no longer exists.

---

## Defect 1 — telemetry reported requested values as if they were measured ones

### What was found, and how

A real field capture showed a device's own telemetry reporting `gap_ms: 150.0` while the real, measured inter-chunk timing on the physical bus for that exact device was 505-545ms — confirmed directly by computing real gaps between consecutive chunk timestamps in the same multi-chunk poll, not inferred.

### Root cause

`ModbusGuard.update_gap()`/`update_max_queue_depth()` (`modbus_guard.py`) combine values across every device sharing a physical bus/endpoint: `max()` for gap (the more conservative, slower request wins), `min()` for queue depth. This is a legitimate, deliberate safety property — a shared bus should not be paced faster than the most conservative device sharing it wants — and this release does not touch it. `AdaptiveModbusController.snapshot()` reported `params.request_gap`/`params.max_queue_depth` — this device's own request, computed entirely locally, with no awareness of what any sibling device on the same bus was also requesting — under field names (`gap_ms`, `max_queue_depth`) that gave no indication they might differ from what was actually happening on the wire.

### Fix

`snapshot()` now reports two explicit, separately-named pairs: `gap_requested_ms`/`gap_effective_ms` and `max_queue_depth_requested`/`max_queue_depth_effective`. The effective values come from `ModbusGuard`'s own already-existing `effective_gap_ms`/`queue_depth` properties, looked up via a new `bus_endpoint` field threaded through `AdaptiveModbusController.get_or_create()` from the same `bus_endpoint` string `__init__.py` already computes once per config entry and already passes to other components (`ModbusGuard.acquire_endpoint()`, `SynchronizedPowerCoordinator`) — reused, not reinvented. `bus_endpoint` is always reassigned on `get_or_create()`, even when returning an existing instance, specifically so a reconfigure that changes the connection's own host/port cannot leave a stale endpoint behind that would make `snapshot()` report the wrong guard's own effective values after that reconfigure.

The requested-only fallback (`_bus_endpoint` unset) is a genuinely reachable state, not a defensive placeholder — every test fixture across this project that constructs `AdaptiveModbusController` via `object.__new__()` (bypassing `__init__` entirely) has never set this field, and `snapshot()` must not raise for any of them. Verified directly: effective values correctly report `None` rather than silently falling back to the requested value under a name that promises it is the real one.

### Verified directly against the exact scenario that caused the original defect

Real execution, reproducing the field capture: master device requesting 150ms, second inverter requesting 500ms, both sharing one endpoint → `gap_requested_ms=150.0`, `gap_effective_ms=500.0`. Confirmed correct.

### A real regression found and fixed during this work

`test_adaptive_modbus.py` uses a standalone module loader that manually pre-registers `huawei_solar.const` in `sys.modules` before executing `adaptive_modbus.py`, since that file's relative imports need it. It did not know about the new `from .modbus_guard import ModbusGuard` import this fix adds, and failed to collect at all (`ModuleNotFoundError`) until fixed by mirroring the exact same pre-loading pattern already used for `const.py`.

## Defect 2 — Developer Tools dependency for excitation control

### What was reported

Enabling, disabling, and resuming excitation after a safety halt each required a Home Assistant service call via Developer Tools, including picking the correct device from a generic picker. Reported directly as inconsistent with this project's own established design: every other config item (`enable_parameter_configuration`, `sync_power_dedicated_reads`) works standalone, with no service call required.

### Fix — enable/disable via config toggle

New `CONF_EXCITATION_ENABLED` option, added to `BatteryHealthOptionsFlowHandler`'s own schema immediately after `CONF_ENABLE_PARAMETER_CONFIGURATION` — the same "Configure" screen as `CONF_BH_ENABLED` and `CONF_SYNC_POWER_DEDICATED_READS`, confirmed with the report author before implementing rather than assumed. Wired into `_setup_inverter_device_data()` (`__init__.py`), which already runs on every config-entry reload (the mechanism this integration already uses for every other options-flow change): `adaptive.enable_excitation()` is called when both this option and `elevated_permissions_enabled(entry)` are true, `adaptive.disable_excitation()` otherwise — both already idempotent no-ops in their already-shipped-in-2.0.15 form, so calling either unconditionally on every setup/reload is safe.

An inaccurate claim was caught and corrected before it shipped: an early draft comment claimed "turning this off and back on again resumes progress." Re-checked directly against `disable_excitation()`'s own existing, documented behavior (which explicitly discards progress by design) before writing anything final — the comment was wrong, not the code; fixed to state precisely what actually survives a reload (an unrelated option changing, with this toggle staying on throughout) versus what does not (deliberately toggling this off, which still discards progress, matching `disable_excitation()`'s pre-existing, unchanged semantics).

### Fix — resume-after-halt via button entity

New `ResumeExcitationAfterHaltButtonEntity` (`button.py`), one per inverter device, gated behind `elevated_permissions_enabled(entry)` matching the existing buttons' own convention. Deliberately scoped to *every* inverter, not just battery-equipped ones (unlike `StopForcibleChargeButtonEntity`, correctly gated to battery presence) — excitation targets a device's own adaptive controller directly, with no battery dependency. A real bug was caught before it shipped: an initial version referenced `ucs.device_info`, assumed not to exist on `HuaweiSolarInverterData` from a shallow read of that dataclass's own directly-declared fields; verified directly (real dataclass construction, not just re-reading source) that the field is legitimately inherited from the `HuaweiSolarDeviceData` base class before either keeping or discarding the assumption.

### Services removed entirely, not deprecated or left registered-but-unused

`enable_excitation`, `disable_excitation`, `resume_excitation_after_halt` — all three removed from `services.py` (handlers, registration calls, the now-dead `AdaptiveModbusController` import), `const.py` (`SERVICE_*` constants, both the `SERVICES` completeness tuple and the separate `ALL_SERVICES`/`_ALL_SERVICE_NAMES` lists that same file's own history shows can drift independently), `strings.json`, `translations/en.json`, and `services.yaml`. The two exception keys those handlers used (`adaptive_controller_not_found`, `excitation_not_enabled`) were confirmed unused anywhere else in the codebase before being removed too, rather than left as dead entries.

## Testing

**Deleted, not fixed**: `test_excitation_services.py` (10 tests from 2.0.15) tested the now-removed service handlers directly — testing functionality that no longer exists by design would certify the wrong thing, so the file was deleted outright.

**New, this release:**
- `test_adaptive_modbus.py`: 2 new tests confirming the old, misleading `gap_ms`/`max_queue_depth` keys are genuinely gone (not left alongside the new names) and that the no-`bus_endpoint` fallback path is real, not just present in source.
- `test_button_excitation.py` (7 tests, real execution — `button.py` has no prior test coverage in this project at all, confirmed directly; this file covers only what this release adds, not a retroactive audit of the pre-existing buttons): unique-id correctness, safe no-op with no controller registered, safe no-op when excitation was never enabled, no-op when enabled-but-not-halted, the genuine resume case, and — adversarial — that pressing one device's own button never affects a different device's own excitation state.
- `test_excitation_toggle_wiring.py` (7 tests, AST-based against the real source via `ast.get_source_segment()`, matching this project's own established `__init__.py` convention — too HA-heavy to import for real execution): confirms the option is read, gated on `elevated_permissions_enabled` too, that enable/disable are genuinely the two branches of one if/else rather than two independent unconditional calls, that the decision runs after `async_load()` (so restored excitation state is never clobbered by a stale pre-restoration read), and that no reference to the removed services remains anywhere in `__init__.py`.

**A test-infrastructure gap found and fixed along the way, not routed around**: `test_elevated_permissions_relocation.py`'s own `_function_body()` helper fell back to a hardcoded, arbitrary 8000-character window when no next `async def` bounded a function — `async_step_init` has no such boundary (confirmed directly: it is `BatteryHealthOptionsFlowHandler`'s own last method), and this release's own new, deliberately thorough comment pushed the real distance to `CONF_BH_WINDOW_DAYS` past that limit (measured directly: ~9091 characters). Rather than shorten a legitimate, valuable comment in `config_flow.py` to fit an arbitrary test constant, the constant was fixed (8000 → 20000, with real headroom) and the "expected exactly 3 occurrences" check in the same file was replaced with a check for the actual field-key-definition pattern, so it can no longer be thrown off by a neighboring field's own comment legitimately mentioning the same name.

## Final verification

- Every file in the packaged `huawei_solar-2.0.15b.zip` compiles cleanly; `strings.json`, `translations/en.json`, and `services.yaml` all validate.
- Full suite, run from a **fresh, independent extraction** of that exact zip: **1,332 passed, 1 skipped**, matching the working tree and the established pre-existing baseline exactly — zero drift, zero new regressions.

## What comes next

Deploy 2.0.15b for the planned excitation run. `gap_effective_ms`/`max_queue_depth_effective` in the resulting telemetry capture are now the correct fields to analyze for real, physical bus behavior — `gap_requested_ms`/`max_queue_depth_requested` remain available for understanding *why* the effective value is what it is (e.g., confirming which sibling device's own request is currently dominating a shared bus), not for standing in as the real one. Per the earlier agreement: this release, and 2.0.15 before it, exist to answer the controller/filter question — once that answer exists, the real implementation is built on top of 2.0.14, carrying forward genuine, independent fixes discovered along the way (this release's telemetry and Developer-Tools-removal fixes among them) deliberately, not by inheriting the experimental branch's own codebase wholesale.
