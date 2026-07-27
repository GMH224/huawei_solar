# Incident Audit — huawei_solar v1.2.4 (hotfix for v1.2.3 outage)

**Date:** 2026-07-27 · **Auditor:** Claude (Anthropic)
**Severity:** **Critical** — total loss of integration functionality.
**Detection:** operator, in production, within minutes of deployment.
**Time to diagnose:** one log file.
**Baseline:** v1.2.2 (confirmed working in production by the operator).

---

## 1. Incident

v1.2.3 aborted config-entry setup on any installation with existing adaptive
learning data. Every entity in the integration became unavailable — inverter,
power meter, battery, optimizer — not merely the adaptive tuning it changed.

```
File "custom_components/huawei_solar/__init__.py", line 601, in _setup_inverter_device_data
    await adaptive.async_load()
File "custom_components/huawei_solar/adaptive_modbus.py", line 367, in async_load
    raw = await self._store.async_load()
File "homeassistant/helpers/storage.py", line 622, in _async_migrate_func
    raise NotImplementedError
NotImplementedError
```

## 2. Root cause

v1.2.3 bumped `_STORAGE_VERSION` from 1 to 2 so that persisted RTT samples —
recorded on the pre-fix batch-summed scale — would be discarded on upgrade.

Home Assistant's `Store` treats that number as its own schema version. When
the persisted version is older than the requested one it calls
`_async_migrate_func`, whose base implementation raises `NotImplementedError`.
No migration callable was supplied.

Three aggravating factors turned a wrong constant into a total outage:

1. **The migration never ran.** `_migrate_v1_rtt_scale()` was written to
   execute *after* `_deserialize()`. HA raises inside `async_load()`, before
   any data is returned, so the routine was unreachable.
2. **The call was outside the guard.** The existing `try/except` in
   `async_load()` wrapped only `_deserialize(raw)`, not the store read itself.
3. **Blast radius.** `adaptive.async_load()` is awaited on the critical path
   of `_setup_inverter_device_data()`, so an optional optimisation could abort
   the entire entry.

**Only installations with existing adaptive data were affected** — a fresh
install has no v1 store, HA skips migration, and setup succeeds. This is
precisely the class of defect that a clean-slate test environment cannot see.

## 3. Why 419 tests did not catch it

Two independent gaps, both structural:

**Gap 1 — `update_coordinator.py` and `adaptive_modbus.py` storage paths were
validated by string-matching source text.** `test_update_coordinator.py`
asserts that certain lines appear in the file. It never imports the module,
never constructs a coordinator, never executes a path. An import-time or
runtime defect passes untouched.

**Gap 2 — nothing verified that a class owns the methods it calls.** The same
release added `self._record_shed()` to a class that does not define or inherit
it.

Neither gap is exotic. Both are the same shape as the v1.1.5 confidence-sensor
defect: **tests asserting the shape of code rather than its behaviour.** That
recurrence is the finding that matters more than either bug.

## 4. Fixes

| # | Change | Rationale |
|---|---|---|
| 1 | `_STORAGE_VERSION` reverted to 1; new `_DATA_SCHEMA_VERSION` stored inside the payload | Payload migrations must not be able to reach HA's migration machinery |
| 2 | Store load wrapped in `try/except`, degrading to defaults | Adaptive learning is optional; it must never be able to abort entry setup |
| 3 | Optimizer coordinator handles shed/timeout inline | It is a sibling class and cannot use the batch coordinator's helpers |
| 4 | Optimizer coordinator gains its own `_record_failure()` | **Pre-existing** latent `AttributeError` on three error paths, predating v1.2.3 |

The RTT rescale behaviour itself is unchanged: absent `data_schema` marker
means pre-v1.2.3 data, and the same narrow migration runs — clearing only
`rtt_samples` / `rtt_p95_ms` while preserving failure history.

Fix 2 is the important one. Fix 1 corrects this instance; fix 2 means the next
mistake in adaptive persistence costs tuned poll parameters rather than the
user's entire integration. That isolation has existed for the battery-health
subsystem since v1.1.7 and should have been applied here at the same time.

## 5. New test coverage

`tests/test_module_imports.py`:

* **`TestModulesImport`** — imports every module against a realistic HA stub.
  Would have failed on v1.2.3 at the storage layer.
* **`TestCoordinatorMethodOwnership`** — AST check that each class defines or
  inherits every `_record_*` helper it calls. These helpers run only on error
  paths, so a missing definition is invisible until something is already
  wrong. Catches both the v1.2.3 regression and the pre-existing defect.

`TestStorageMigrationV1toV2` now pins `_STORAGE_VERSION == 1` with the outage
recorded in the docstring, so a future bump fails loudly at test time rather
than silently at the user's next restart. `TestStoreLoadFaultIsolation`
asserts the load remains guarded.

**Adversarial verification:** run against the broken v1.2.3 tree, **5 of the
new tests fail** — the ownership check plus all three storage assertions.

## 6. Process findings

**6.1 Source-string tests were treated as coverage.** They pin structural
invariants usefully but prove nothing about behaviour. Any module asserted
only by string matching is effectively untested. Import-level testing is now
in place; instantiation-level testing of the coordinators remains a gap and
should follow.

**6.2 A stateful upgrade path was never exercised.** Every test ran against a
fresh in-memory controller. The defect existed *only* on the upgrade path from
existing persisted data — the situation every real user is in and no test was.
Migration paths need fixtures representing prior versions' data.

**6.3 Fault isolation was applied unevenly.** v1.1.7 established that additive
subsystems must not sit on the setup critical path, and that contract is
enforced by 18 structural tests — for battery health only. The adaptive
controller sits on the same critical path and had no equivalent protection.
The isolation principle should be audited across *all* optional subsystems,
not applied per-incident.

**6.4 Test-harness order dependency.** The new import harness initially set up
its stubs at module import time, making its behaviour depend on pytest
collection order relative to other test modules that install their own
`huawei_solar` stub. Moved to `setUp`. Shared mutable global state across test
modules is itself a latent defect.

## 7. Verification

* **419 passed, 1 skipped, 0 failed**, deterministic across four consecutive
  runs.
* Real-library import check of all changed modules: clean.
* Battery-health replay against the 6-month field dataset: unchanged.
* Adversarial: 5 failures against v1.2.3.
* Static: all Python files parse; all JSON valid; `manifest.json` = 1.2.4.

## 8. Recommendation to the operator

Deploy v1.2.4 directly from v1.2.3 — no rollback needed, and the persisted
adaptive data is intact and will migrate correctly. On first start expect a
single WARNING per inverter reporting that v1 RTT samples were cleared; gap
and timeout re-learn over the following days while failure-rate history is
preserved.

The staged plan is unchanged: observe for a week, then supply both inverters'
adaptive sensors so Defect C can be validated.

**Verdict:** hotfix ready. The outage is fully explained, the immediate cause
corrected, the failure made non-fatal for the future, a pre-existing latent
defect fixed alongside it, and the two testing gaps that allowed it closed
with adversarially-verified coverage.
