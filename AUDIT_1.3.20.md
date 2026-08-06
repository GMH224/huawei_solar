# Release Audit — huawei_solar v1.3.20

**Date:** 2026-08-06 · **Auditor:** Claude (Anthropic)
**Baseline:** v1.3.19
**Type:** five defect fixes from two sources — one independent ICS audit
(Defect W) and a full, deliberate fresh-eyes sweep of the entire runtime
codebase (Defects X1-X4), explicitly requested without time pressure and
performed without reference to any test or audit file. Six production
files changed.

---

## 1. The two inputs to this release

**Input 1 — an independent ICS audit of v1.3.19**, explicitly stricter
than an earlier draft of the same report: it separated one confirmed code
defect from a set of operational risks (queue saturation, non-atomic
synchronized reads, bounded-but-slow startup) that the report itself
declined to classify as bugs unless the code proved a broken control flow.
Every "risk" item was spot-checked against source before accepting the
report's own judgment, not merely trusted: all four matched conclusions
this project had already and independently reached (Defect P's guard
fairness, Finding 9's documented best-effort sync coordinator, Defects
M/U/V's bounded startup, Defect Q's write-invalidation). The one confirmed
defect (Defect W) was verified directly against source before being
accepted.

**Input 2 — a full fresh-eyes codebase sweep**, requested explicitly as
"deep and comprehensive... no rush job," assuming no access to this
project's own tests or audit files. Every file that hadn't already
received repeated scrutiny this session was read in full:
`battery_health.py` (1876 lines, the largest previously under-audited
file), `battery_health_manager.py`, `battery_health_entities.py`,
`register_cache.py`, `bus_diagnostics.py`, `night_mode.py`,
`diagnostics.py`, `types.py`, plus targeted re-reads of `adaptive_modbus.py`
and `modbus_telemetry.py`, and cross-cutting sweeps for every bug *shape*
already proven real elsewhere this session (mutable class-level defaults,
unguarded `@callback` methods, division risks, list-mutation-during-
iteration). Four findings (X1-X4) came out of this pass; several files
(`register_cache.py`, `bus_diagnostics.py`, `types.py`,
`battery_health_entities.py`) were read in full and found clean.

## 2. Defect W — a gap in the Defect S fix itself

**Reported:** `modbus_keepalive.py`'s `_get_keepalive_register()` imports
`huawei_solar.registers.REGISTERS` with no guard around the import,
before the function's own intended graceful-fallback check.

**Verified** exactly, at the reported lines. This function exists
specifically because of Defect S (v1.3.16): the original bug was
`RegisterName[...]` subscript access raising `TypeError` uncaught, because
the `except KeyError` written for it didn't match the real failure mode.
The v1.3.16 fix replaced the subscript with a validated, exact-match
lookup against `REGISTERS` — but the import of `REGISTERS` itself was
never wrapped. If it ever raised (a future restructuring of the vendor
package's module layout), the exception would propagate uncaught out of
this function, past `_probe()`'s own try block — the identical shape as
the original bug, in the code that fixed it.

**Severity, and why "narrow but real" is the right characterization** (matching the reporting audit's own classification): `huawei_solar.registers.REGISTERS` is relied on extensively elsewhere in this codebase (`register_cache.py`, `sensor.py`, `__init__.py`, `config_flow.py`). If this import path ever genuinely broke, the *isolated* impact here would be smaller than it first appears, since much of the rest of the integration would be failing simultaneously, not uniquely at this one call site. The code-level gap is exactly as reported regardless.

**Fix.** Wrapped in `try/except ImportError`, resolving to the same clean
warning-and-skip behavior the surrounding function already provides for
an invalid register name.

## 3. Defect X1 — battery health: reachable division-by-zero, missing fault isolation

### 3.1 The division

`battery_health.py`'s composite-score computation:
```python
if available:
    total_w = sum(w for _, _, w in available)
    r.bhi = round(sum(v * w for _, v, w in available) / total_w, 1)
```
`available` is non-empty whenever a term's *value* is not `None` —
independent of its *weight*. The options flow schema allows
`weight_capacity`, `weight_efficiency`, and `weight_balance` to each
independently reach `0.0` (`vol.Range(min=0.0, max=1.0)` on all three, no
cross-field validation). Confirmed directly against the schema: a user
setting all three to zero via the UI is a real, reachable action, not a
hypothetical one. If they do, `total_w` is `0` and this raises.

### 3.2 The missing isolation

The call site, `battery_health_manager.py`'s `_handle_coordinator_update`,
is a bare `@callback` with no exception handling around
`self.engine.update(sample)`. Checked against the rest of this exact
subsystem: `battery_health_entities.py`'s `_on_health_update`,
`async_added_to_hass`, and `adaptive_modbus.py`'s listener dispatch (see
Defect X2) are all explicitly wrapped, citing this project's own "v1.1.7
fault isolation" convention by name in their comments. This one call was
the sole exception to a pattern the codebase otherwise applies
consistently — which is what made it stand out during the sweep, rather
than reading as an isolated, unrelated gap.

**Impact if unfixed:** an unguarded raise here doesn't fail once — it
fails on *every* subsequent coordinator tick, forever, since the engine
never advances past the failing call. The only visible symptom would be a
repeating log entry and battery health data going permanently stale, with
nothing pointing a user toward the actual cause (three sliders set to
zero) unless they already suspected it.

**Fix, two independent layers, deliberately not just one:**
1. **At the root** (`battery_health.py`): `total_w > 0` is now checked
   before dividing; on failure, a clear warning is logged and `bhi` is
   left unset (matching this module's existing convention — "no
   sub-scores computable" renders as `unknown`, never `0`) rather than
   raising.
2. **At the call site** (`battery_health_manager.py`): `engine.update()`
   is now wrapped in the same fault-isolation pattern already used
   everywhere else in this file — a second, independent line of defence
   against any other input this method might someday receive that the
   root fix doesn't anticipate.

## 4. Defect X2 — a gap in a fix from earlier in this same release cycle

**Found during the sweep**, by checking whether the same bug shape existed
elsewhere after Defect V (v1.3.19) added exception isolation to
`modbus_telemetry.py`'s `_push_to_listeners`. It did:
`adaptive_modbus.py`'s sibling implementation already snapshots the
listener list before iterating (`for cb_fn in list(self._listeners):`),
citing its own historical `BUG-003` fix and explaining why. The telemetry
file's version, freshly touched one release earlier for a different fix,
iterated the live list.

**Verified adversarially**, not just by inspection: reproduced both
patterns directly. The old (live-iteration) pattern genuinely skips a
listener that removes itself (or another) mid-callback, due to Python's
list-mutation-during-iteration semantics. No currently-registered listener
in this codebase does this, so it was not actively misbehaving — but it's
the exact defect class this project had already named and fixed once, in
a sibling file, and the lesson didn't carry over.

**Fix.** `for cb_fn in self._listeners:` → `for cb_fn in list(self._listeners):`, matching `adaptive_modbus.py` exactly.

## 5. Defect X3 — a real substring collision in night-mode register lookup

**Found during the sweep**, by checking `night_mode.py`'s substring-based
register matching (`key_substr in str(rname).lower()`) against the actual
register table rather than assuming it was safe:

```
"input_power"  is a substring of: total_dc_input_power, sdongle_total_input_power, smartlogger_input_power
"active_power" is a substring of: 61 different real register names, including day_active_power_peak
```

`day_active_power_peak` is a real SUN2000 register almost certainly polled
by the same coordinator this detector watches — this is not an obscure,
unreachable collision. Since dict iteration order depends on how the
coordinator assembled a given result, the search could non-deterministically
return the day's peak power instead of the instantaneous reading the code
intended, feeding the day/night threshold logic a value that wasn't what
it thought it was.

**Verified adversarially**: reproduced a result dict containing both
`day_active_power_peak` and `active_power`, confirmed the old substring
search can return the wrong one depending on which happens to be
encountered first, and confirmed the fix cannot, by construction.

**Fix.** `_get_value()` now takes a `RegisterName` and does an exact
`result.get(register_name)` lookup; `_get_power()`'s candidate list and
the `DEVICE_STATUS` check both pass the real `rn.*` constants instead of
raw strings. This eliminates the entire collision class rather than
narrowing it — there is no substring search left to collide.

## 6. Defect X4 — Home Assistant's built-in diagnostics leaked identifying data

**Found during the sweep**, prompted specifically by `bus_diagnostics.py`'s
own extensive, historically-documented privacy discipline — an explicit
design constraint ("No identifying data. Serial numbers and endpoints are
replaced by a stable salted pseudonym"), with a paragraph in that file
describing a past incident where a serial number leaked despite the
stated intent. Checking whether that same discipline extended to
`diagnostics.py` — Home Assistant's own built-in "download diagnostics"
feature, a *different* file from `bus_diagnostics.py`'s opt-in capture,
and one that routinely gets attached to public GitHub issues for
troubleshooting — found it did not:

- `TO_REDACT = {CONF_PASSWORD}` only. `CONF_HOST` (the device's IP/hostname) and `CONF_USERNAME` (if login is configured) were exposed raw.
- The non-inverter device branch explicitly assigned the raw `dd.device.serial_number`.
- Every coordinator's `.data` dict (main, power meter, battery, config) was dumped completely unredacted — and SUN2000/LUNA2000 installations expose several serial-number-bearing registers this way, independent of whatever the top-level summary fields did or didn't include.

**Fix.**
- `TO_REDACT` now also covers `CONF_HOST` and `CONF_USERNAME`.
- New `_redact_serial_number()` reuses `bus_diagnostics.py`'s existing
  `pseudonym()` scheme directly (imported from that module, not
  reimplemented) — a stable, salted, non-reversible short identifier — so
  two shared diagnostics captures can still be compared by a maintainer
  without either exposing the real number.
- The non-inverter branch's `serial_number` field is now redacted.
- New `_redact_coordinator_data()` scans every register in a coordinator's
  raw data dump for a name indicating it carries a serial number
  (`"serial_number"` as a substring — deliberately broad so it stays
  correct if the vendor library adds more such registers, e.g. a third
  storage unit, without this list needing to be updated) and redacts just
  those values, leaving the rest of the diagnostic data intact and useful.
  Applied to the main, power meter, battery, and config coordinators.
- Optimizer data was checked directly, not assumed, and deliberately left
  unredacted: it is keyed by numeric optimizer ID mapping to
  `OptimizerRealTimeData`, a different shape than `RegisterName -> Result`,
  and that type was confirmed to carry no serial-number-like field, so the
  redaction helper's register-name matching does not apply to it.

## 7. Adversarial verification

New `tests/test_audit_v4_findings.py` (22 tests):

- **Defect W:** confirms the new pattern returns `None` cleanly on a
  simulated import failure and still works normally otherwise; confirms
  (adversarially) that an unguarded version of the same logic really does
  let the failure propagate; static check confirms the real source wraps
  the import.
- **Defect X1:** confirms an all-zero-weights input no longer raises and
  normal weights still compute correctly; confirms (adversarially) that
  an unguarded callback really does stall forever after one bad tick,
  while the fixed version survives it and continues; static checks
  confirm both the root-cause guard and the call-site isolation are
  present in real source.
- **Defect X2:** confirms (adversarially) the old live-iteration pattern
  really does skip a listener that removes itself mid-callback, and the
  new snapshot-based pattern doesn't; static check confirms the real
  source snapshots.
- **Defect X3:** confirms (adversarially) the old substring pattern really
  can return `day_active_power_peak`'s value when asked for
  `active_power`, given a result dict containing both — and confirms a
  full `evaluate()` call still transitions day/night correctly with exact
  matching; static check confirms no substring search remains.
- **Defect X4:** confirms `_redact_serial_number` exists and actually
  transforms a value; confirms `TO_REDACT` includes `CONF_HOST` and
  `CONF_USERNAME`; confirms the non-inverter branch no longer assigns the
  raw serial number; confirms coordinator data dumps are routed through
  the redaction helper.

**Run against the pristine pre-session baseline** (predating every
defect fixed across this entire session), **all 9 applicable static
checks fail correctly** (the remaining tests in that same run are
behavioural reproductions whose adversarial proof is the explicit
old-vs-new comparison built into the test itself, not a source-text
check against old code).

## 8. Safety properties

- No change to `ModbusGuard`, `update_coordinator.py`, `register_cache.py`,
  `synchronized_power_coordinator.py`, or any entity platform file.
- Defects F through V (v1.3.7-v1.3.19) are untouched and still in place.
- Defect W, X2, X3: purely defensive/correctness fixes with no change to
  successful-path behaviour.
- Defect X1: `bhi` now reads `unknown` instead of raising in the
  all-zero-weights case — a behavioural change, but strictly a
  improvement (silent permanent stall → a specific logged warning and a
  correctly-unavailable sensor), and only reachable via a configuration
  no working installation would have without deliberately choosing it.
- Defect X4: diagnostics output is smaller in the specific fields
  redacted; no change to what's captured for actual troubleshooting value
  elsewhere in the file.

## 9. Test evidence

- **614 passed, 1 skipped, 0 failed**, deterministic across 3 repeated
  runs (was 592; 22 new tests).
- Adversarial: all 9 applicable static checks fail against the pristine
  pre-session baseline; the full 22-test suite passes against this
  release.
- Static: `py_compile` clean on all six changed files; manifest version =
  1.3.20.
- Confidentiality sweep: clean.
- Diffed against the v1.3.19 tree to confirm only `modbus_keepalive.py`,
  `battery_health.py`, `battery_health_manager.py`, `modbus_telemetry.py`,
  `night_mode.py`, and `diagnostics.py` changed among production files.

## 10. Recommended deployment procedure

1. Delete `/config/custom_components/huawei_solar` entirely.
2. Extract v1.3.20 fresh into `custom_components/`.
3. Restart Home Assistant once.
4. No single validation step covers all five findings — most target rare
   or configuration-dependent paths (a vendor-library import failure, all
   three battery-health weights set to zero, a downloaded diagnostics
   file) that aren't part of normal operation. The one finding worth a
   direct look if convenient: if `battery_health.md`/the battery health
   options have ever had any of the three weight sliders set unusually
   low, confirm the BHI sensor still reports a value rather than
   `unknown` — a genuinely `unknown` reading with a corresponding warning
   in the log would now be the correct, informative behaviour rather than
   a crash, which is the direct, if narrow, test of Defect X1.

**Verdict:** release-ready. One externally-reported defect and four found
through a genuinely unhurried, comprehensive internal review — including
two (X1's call-site half, X2) that are honest corrections of gaps in this
project's own recent work, not just newly-discovered pre-existing issues.
