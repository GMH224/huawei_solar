# Release Audit — huawei_solar v1.3.7

**Date:** 2026-08-05 · **Auditor:** Claude (Anthropic)
**Baseline:** v1.3.6 (currently deployed, stable)
**Type:** defect fix, single production file changed (`__init__.py`), plus
one new test file. No chunking/coordinator-cost behaviour touched.

---

## 1. The report

Overnight 2026-08-04, a config-entry reload during a period of repeated
"Setup of config entry cancelled" errors (see the 2026-08-04 handoff, §2.2)
left one coordinator — the configuration/number-entity coordinator for the
battery-attached inverter — dead. Every other coordinator on the same entry
recovered on subsequent reload attempts; this one did not, and only a full
Home Assistant restart brought it back. The operator independently confirmed
this from the Home Assistant UI the following day: the affected inverter's
Configuration card (End-of-charge SOC, grid charge cutoff/max, maximum
charge/discharge power) still showed empty fields roughly 24 hours later —
not stale, never-populated.

A separate, already-logged error from the same incident window
(`Unable to remove unknown job listener`,
`_async_register_learning_gates.<locals>._on_started`) was flagged in the
2026-08-04 handoff (§2.3) as "not yet root-caused… independent of §2.1/§2.2,
worth fixing regardless." This audit connects the two.

## 2. Confirming the dead coordinator from field data, not assumption

Before writing any code, the field evidence was checked directly rather than
inferred from the operator's screenshot alone (project rule: don't ship on
a prediction).

A 9.25 h `bus_diagnostics` capture taken the following day contains **zero**
records from the affected coordinator (`devdc46_config_data_update_coordinator`),
across the entire capture. The immediately preceding capture (same session,
pre-outage) shows it polling normally — 78.7 requests/hour, consistent with
its configured interval. Its last recorded request is timestamped within
minutes of the incident's stated onset. Every sibling coordinator on the
same config entry (`devdc46_data_update_coordinator`,
`devdc46_battery_data_update_coordinator`,
`devdc46_power_meter_data_update_coordinator`) is present and polling
normally throughout the same 9.25 h window. This rules out a bus-wide or
entry-wide failure: the fault is specific to one coordinator's setup path,
not the connection, the entry, or the device.

## 3. Root cause

`_async_register_learning_gates()` (in `__init__.py`, introduced v1.2.2)
registers Home Assistant lifecycle listeners to gate the adaptive controller's
learning across start-up and shutdown:

```python
entry.async_on_unload(
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_started)
)
...
entry.async_on_unload(
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop)
)
```

`hass.bus.async_listen_once()` self-unsubscribes the instant its event
fires — that is the entire meaning of "once." Handing its unsub callable
directly to `entry.async_on_unload()` means that same callable is invoked a
**second time** whenever the entry unloads after the event has already
fired. For `EVENT_HOMEASSISTANT_STOP` specifically, this is not an edge
case: the listener is re-armed on every single setup, and is almost never
still pending by the time of a later reload (Home Assistant does not stop
between a setup and a subsequent reload in ordinary operation). The second
removal call hits an already-empty listener slot, and Home Assistant logs
exactly the reported error.

**Why a log line becomes a dead coordinator.** This runs inside the entry's
unload path. An unhandled exception raised there can abort whatever
unload/re-setup work for that entry had not yet completed at that point in
the sequence — a plausible, concrete mechanism for exactly the observed
symptom: one coordinator silently failing to re-arm on reload while others,
whose setup happened to complete before the exception, recovered normally.
This is offered as the most likely mechanism given the evidence assembled,
not asserted as independently proven beyond it — see §6.

## 4. The fix

`_guarded_once()`, a small local helper, wraps the listen-once/unload
pattern: it tracks (via a closure flag) whether the wrapped event has
already fired, and skips the redundant unsub call in that case. A listener
that has *not* yet fired is still cleanly cancelled on unload — the
original, correct behaviour for that case is unchanged. Applied to both of
the pattern's only two call sites in the codebase
(`EVENT_HOMEASSISTANT_STARTED`, `EVENT_HOMEASSISTANT_STOP`), both inside
`_async_register_learning_gates()`. No other function in the codebase uses
this pattern (checked: `grep -n "async_on_unload"` across every `.py` file
returns exactly these two sites plus the unrelated options-update listener,
which is a persistent, not one-shot, listener and is not exposed to this
hazard).

Nothing about the learning-gate *semantics* changes: suppression and
settling still key off the same events, at the same times, for the same
reasons documented in the existing docstring.

## 5. Adversarial verification (mandatory per project convention)

New `tests/test_learning_gate_unsub.py`, two independent angles:

**5.1 Behavioural**, via a fake event bus purpose-built to reproduce Home
Assistant's actual self-unsubscribe-on-fire semantics (not a bus that merely
allows double-removal — one that raises on it, matching the field error
precisely):
- `test_unguarded_pattern_reproduces_the_field_bug` — the OLD (unguarded)
  pattern raises `KeyError` when unload runs after the event has fired.
  This is the adversarial check confirming the fake bus actually reproduces
  the hazard, so a pass on the next test is meaningful rather than a fake bus
  that trivially can't fail.
- `test_guarded_pattern_survives_unload_after_fire` — the same sequence
  against the v1.3.7 `_guarded_once` pattern does not raise.
- `test_guarded_pattern_still_cancels_pending_listener` — confirms the fix
  does not regress the other case: a listener that never fires is still
  removed on unload.

**5.2 Static (AST)**, following the same dependency-free convention
established in v1.3.6's `TestConstImportsAreDefined`:
`test_async_listen_once_not_passed_directly_to_async_on_unload` walks
`_async_register_learning_gates`'s AST and fails if
`hass.bus.async_listen_once(...)` is ever passed as a direct argument to
`entry.async_on_unload(...)` — the exact shape of the original defect. This
guards against the fix being silently reverted or the pattern being
reintroduced elsewhere in the same function later.

**Adversarial confirmation against the actual pre-fix file, not a
hypothetical:** the AST test was run against the exact v1.3.6 `__init__.py`
(preserved for this comparison before editing) and **fails**, correctly
identifying both original call sites (lines 389 and 401 of that file). Run
against the v1.3.7 file, it passes. The behavioural test's "unguarded"
comparison function is a direct transcription of the pre-fix code path
(not the same file, but the same pattern), included so the fix's mechanism,
not just its output, is exercised.

## 6. Limits of this verification — read before assuming this is fully closed

- **The mechanism connecting the log error to the dead coordinator is
  plausible and consistent with all available evidence, not independently
  proven end-to-end.** No traceback from the actual incident was captured
  (the handoff already flags this gap in §2.2/§2.4); this audit's causal
  claim rests on (a) the code-level defect being real and reproducible, and
  (b) the field capture confirming the *symptom* (one coordinator dead,
  siblings fine) matches what an unload-time exception could produce — not
  on having caught the exception firing at the moment it disabled that
  specific coordinator.
- **This is why §8's deployment procedure requires an explicit reload test,
  not just a clean restart.** A clean boot does not exercise the reload
  path this fix targets at all; the only real test of whether this closes
  the incident is triggering a reload and confirming both the previously-
  dead coordinator recovers and the log error is absent.
- If a reload after this deploy still leaves any coordinator dead, that
  disproves this specific mechanism (or shows it was one of several
  contributing causes) and §2.2's "not resolved" status should stay open
  rather than being closed on the strength of this fix alone.

## 7. Safety properties

- **No behavioural change to Modbus chunking, adaptive control logic, or
  battery-health logic.** `update_coordinator.py`, `adaptive_modbus.py`,
  `battery_health.py`, `battery_health_manager.py`, `register_cache.py` are
  byte-identical to the audited v1.3.6 tree.
- **No storage/`Store` version change.** No persisted-data migration risk.
- Learning-gate *timing* is unchanged — same events, same suppression/settle
  reasons, same log messages on those transitions. Only the unload-time
  double-removal hazard is closed.
- Fault isolation (v1.1.7) and the battery-health engine's isolation
  contract are untouched — this fix is entirely inside
  `_async_register_learning_gates`, which the isolation contract already
  requires to never break entry setup (wrapped in try/except at its call
  site in `async_setup_entry`; unaffected by this change).

## 8. Test evidence

- **473 passed, 1 skipped, 0 failed**, deterministic across 3 repeated runs
  (was 469; 4 new tests in `test_learning_gate_unsub.py`).
- Static: all Python files parse (`py_compile` clean on `__init__.py` and
  the new test file); manifest version = 1.3.7.
- Confidentiality sweep: clean, no field data or real serials present in
  any changed or added file.
- Only `__init__.py` changed among production files. Diffed directly against
  the v1.3.6 archive to confirm no unrelated changes crept in.

## 9. Recommended deployment procedure

1. **Delete** `/config/custom_components/huawei_solar` entirely (standing
   project convention — never extract over an existing install).
2. Extract v1.3.7 fresh into `custom_components/`.
3. Restart Home Assistant once, to establish a clean baseline **not**
   inherited from the outage-recovery state.
4. **Required validation specific to this release:** trigger a plain
   *reload* of the config entry (Settings → Devices & Services → this
   integration → Reload — not a full restart). Confirm:
   - The `homeassistant.core` "Unable to remove unknown job listener" error
     does not appear in the log.
   - The battery-attached inverter's Configuration entities (End-of-charge
     SOC, grid charge cutoff/max, maximum charge/discharge power) populate
     with real values, not blank, within one poll cycle of the reload.
5. If both hold, this closes the coordinator-dies-on-reload symptom. If
   either does not, keep §2.2 open per §6 above rather than assuming this
   release resolved it.

**Verdict:** release-ready, on the specific and limited claim in §6 — a
real, adversarially-verified code defect is fixed, and it is the most
plausible mechanism connecting a previously-flagged log error to a real
field incident, but the reload-vs-restart validation in §9.4 is the actual
test of whether this closes the incident, and has not yet been performed
against a live system.
