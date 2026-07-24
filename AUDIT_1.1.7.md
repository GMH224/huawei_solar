# Release Audit — huawei_solar v1.1.7

**Date:** 2026-07-25 · **Auditor:** Claude (Anthropic)
**Baseline:** the user-supplied v1.1.6 archive (verified intact: 37 Python
files, 296 passed / 1 skipped before any modification).
**Scope:** `battery_health_entities.py`, `__init__.py`, `sensor.py`,
`button.py`, `const.py`, `config_flow.py`, `strings.json`,
`translations/en.json`, `manifest.json`, `tests/`.

Both defects addressed here were found in **production logs from a real
installation**, not in internal testing. That is itself an audit finding and
is treated as one in §5.

---

## 1. Evidence base

Two Home Assistant logs from the reporting installation:

| Symptom | Log evidence |
|---|---|
| `confidence` sensor `Unavailable`, 160 × `listener failed` | `Error adding entity sensor...battery_health_confidence` + `ValueError: ... indicating it has a numeric value; however, it has the non-numeric value: 'low'` |
| All battery entities `Unknown`, both inverters affected | `Setup of config entry 'SUN2000-10KTL-M1' ... cancelled` → `entity_platform ... asyncio.exceptions.CancelledError` → `Config entry ... for huawei_solar.<platform> has already been setup!` (× 5 platforms) |
| Underlying stressor (pre-existing, not introduced by this subsystem) | 156 × `Modbus timeout (no response in NN s)` across ~63 h, on `power_meter`, `config`, and `battery` coordinators alike |

**Diff evidence.** v1.1.6 → the previously-shipped v1.1.7 differed **only** in
`battery_health_entities.py`, `manifest.json`, `CLAUDE.md`, and one test file.
The register set and all setup-path code were byte-identical. An earlier
working hypothesis (that newly-required SOH-calibration registers were
unsupported by the hardware and poisoning batched reads) is therefore
**disproven** and was withdrawn. No Modbus exception for an illegal/unsupported
address appears anywhere in the logs.

---

## 2. Finding 1 — HA API contract violation (confirmed defect)

**Severity:** High (entity permanently dead; error on every 30 s tick).

`HuaweiSolarBatteryHealthSensorEntity` declared
`_attr_suggested_display_precision = 1` at **class** scope. HA's
`SensorEntity.state` raises `ValueError` when any numeric-implying hint is
present alongside a non-numeric value. `confidence` returns
`"low"`/`"normal"`/`"stale"`, so it violated the contract for every value it
could ever hold.

The manager's per-listener exception guard behaved **correctly** here: it
contained the fault to a single entity and kept the other nine plus the
button working. The visible symptom (`Unavailable`, "no longer provided") was
the entity failing to be added at startup.

**Fix:** `device_class: SensorDeviceClass.ENUM` + explicit `options`, and the
precision hint applied per-instance and skipped for `_STRING_VALUED_KEYS`.

**Verification (T18, 12 tests):** `tests/test_battery_health_entities.py`
re-implements HA's real validation rule — including its error text — and runs
every entity's resolved attributes through it for every plausible value
(`None`, boundaries, typical). It also asserts engine-produced confidence
values are a subset of the declared `options`, so the engine and entity cannot
drift apart.

**Load-bearing proof:** the v1.1.6 defect was temporarily reintroduced; 4
tests failed with the production error text, and passed again on revert.

---

## 3. Finding 2 — architectural: additive subsystem on the critical path

**Severity:** High (blast radius = every entity in the integration).

`await bh_manager.async_initialize()` — which performs Store disk I/O and
attaches a coordinator listener — ran inline inside `async_setup_entry`.
Whatever triggered the observed cancellation, an additive, read-only feature
must not be able to add time to, or raise from, a path that Home Assistant
cancels on timeout, because a cancelled platform setup takes down **all** of
the integration's entities.

This audit does **not** claim the subsystem caused the reported cancellation;
the evidence is insufficient to attribute it, and the same Modbus timeouts
appear in logs predating it. The correct engineering response is to remove the
possibility rather than argue about attribution.

**Isolation contract now enforced:**

| # | Property | Mechanism |
|---|---|---|
| 1 | Setup never awaits battery-health work | `_async_setup_battery_health()` is synchronous; init runs via `entry.async_create_background_task` |
| 2 | Manager creation failure contained | try/except + `BatteryHealthManager.remove()` cleanup; loop continues to next inverter |
| 3 | Background init failure contained | coroutine has its own guard; cannot raise into the event loop |
| 4 | Platform setup never aborted | `sensor.py` / `button.py` entity creation wrapped in try/except |
| 5 | Entity callbacks never raise into HA's state machine | `async_added_to_hass` / `_on_health_update` guarded |
| 6 | Unload never blocked | flush guarded, falls back to `stop()` |
| 7 | User escape hatch | `bh_enabled` option (default True), evaluated **before** any manager work |
| 8 | Modbus footprint cannot grow silently | golden register-set test |

**Verification (T19, 18 tests):** `tests/test_battery_health_isolation.py`
asserts these **structurally, via AST inspection of the source**, because the
property being protected is architectural ("no code path here can delay or
fail entry setup") rather than behavioural. A future refactor that reintroduces
an inline `await`, drops a guard, or moves the kill switch after manager
creation fails the suite.

**Load-bearing proof:** run against the pristine v1.1.6 tree, **13 of 18 fail**.
The 5 that pass are the 3 golden-register-set tests and the 2 read-only tests —
which is precisely the evidence that v1.1.7 changes **no** Modbus behaviour.

---

## 4. Deliberately NOT changed

- **The register set.** Byte-identical to v1.1.6, which the reporter confirms
  operates correctly. Changing it while investigating an instability would
  confound the variables. It is now pinned by test, capped at 25 registers,
  and any future change must be a conscious edit to the golden list.
- **All engine formulas and thresholds.** T1–T17 pass unmodified; BHI values
  are computed identically to v1.1.6.
- **Coordinator, cache, guard, and keepalive internals.** Out of scope; the
  Modbus timeouts observed in the logs are an installation-side matter
  (RS485 wiring, dongle, bus contention between the two inverters) and are
  not addressed by a code change here.

---

## 5. Process finding (self-assessment)

Both defects were invisible to the v1.1.5/v1.1.6 suites for structural
reasons, and that is the more important finding than either bug:

- **T18's class** — an API contract violated for certain *values* rather than
  certain *code paths*. 296 tests exercised the engine exhaustively but never
  instantiated a real `SensorEntity` against HA's own state validation.
- **T19's class** — a property about *where in the lifecycle* code runs.
  Nothing in the suite asserted anything about setup-path shape or blast
  radius, so "additive feature cannot break existing entities" was an
  assumption, never a test.

Both classes are now covered by construction. Additionally, this release
adopted **adversarial verification as a required step**: every new test file
was run against a deliberately-broken tree to prove it fails, rather than
trusting a green result on the fixed tree. Green tests that cannot fail are
worse than no tests, and that check was missing from prior releases.

---

## 6. Evidence summary

- Baseline (unmodified upload): **296 passed, 1 skipped** — archive verified
  intact, no corruption.
- Final: **326 passed, 1 skipped, 0 failed**, identical across 3 consecutive
  runs (deterministic).
- Static: 39 Python files parse clean (`ast.parse`); 22 JSON files valid
  (manifest, hacs, icons, strings, 20 translations, en.json options mirrored).
- `manifest.json` = **1.1.7**.
- Adversarial: T18 4/12 fail on reintroduced defect; T19 13/18 fail on
  pristine v1.1.6.

**Verdict:** release-ready. Finding 1 is a confirmed defect with a verified
fix. Finding 2 removes an architectural risk rather than a proven cause — the
subsystem is now structurally incapable of delaying, cancelling, or failing
config-entry setup, and ships with a user-accessible kill switch if it ever
misbehaves again.

**Residual risk (installation-side, unaddressed by this release):** the
recurring Modbus timeouts. Until those are resolved, discharge segments will
continue to be discarded by `mark_gap()` and `SOH capacity`/`efficiency` may
remain `Unknown` regardless of the fixes here. That is correct conservative
behaviour by the engine, not a defect, but it means this release should not be
expected to make those two sensors populate on its own.
