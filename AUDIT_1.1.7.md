# Release Audit — huawei_solar v1.1.7 (bug fix: confidence entity)

**Date:** 2026-07-24 · **Auditor:** Claude (Anthropic) · **Trigger:** a user
reported "Battery health confidence: no longer provided" and later supplied a
Home Assistant log, which contained the actual root cause.

This audit does something the two prior ones didn't have the chance to: trace
a real field failure back through the code, rather than reviewing code in the
abstract. That is a more valuable signal than another clean self-review, so
it's recorded here in full.

## 1. What the log showed

```
2026-07-22 04:41:12 ERROR [homeassistant.components.sensor] Error adding
  entity sensor.heating_batteries_battery_health_confidence for domain
  sensor with platform huawei_solar
ValueError: Sensor ... has device class 'None', state class 'None' unit
  'None' and suggested precision '1' thus indicating it has a numeric
  value; however, it has the non-numeric value: 'low' (<class 'str'>)

[repeats on every subsequent tick, from custom_components.huawei_solar.
 battery_health_manager: "listener failed", same ValueError]
```

Two Modbus-timeout patterns also appear in the same log (`HV2220098926`,
`HV2220080950` update coordinators backing off after consecutive timeouts).
Those are pre-existing, unrelated to the battery-health subsystem, and out of
scope for this audit — they affect all registers on those coordinators, not
something introduced by v1.1.5/1.1.6.

## 2. Root cause

`HuaweiSolarBatteryHealthSensorEntity` (battery_health_entities.py, all
versions ≤ 1.1.6) declared:

```python
class HuaweiSolarBatteryHealthSensorEntity(SensorEntity):
    _attr_suggested_display_precision = 1     # class attribute — ALL instances
```

Home Assistant's `SensorEntity.state` property (see traceback) treats the
mere *presence* of `suggested_display_precision` (or a unit, or a state
class) as a declaration that the entity's value is numeric, and raises
`ValueError` if the actual value is a non-numeric string. `confidence`'s
native value is `"low"` / `"normal"` / `"stale"` — a string by design (it's
a category, not a measurement) — so every write to that entity's state
raised.

**Failure sequence, exactly as the log shows:**
1. On integration setup, HA tries to add the entity → raises → entity never
   gets added → the entity registry is left with a stale/orphaned reference
   from any earlier successful run → surfaces to the user as "no longer
   provided."
2. Every subsequent coordinator tick calls the manager's listener callback →
   the entity's `_on_health_update` → `async_write_ha_state()` → the same
   `ValueError`, caught by the manager's per-listener `except Exception`
   guard (§4 of AUDIT_1.1.5.md) → logged as `listener failed`, execution
   continues, **but this specific entity never recovers** — the guard
   correctly protected the other nine sensors (which is why the rest of the
   panel kept working), but had no way to fix or re-add the one that failed
   at setup.

This explains every symptom reported in this conversation: the orphaned
"no longer provided" message, the `Unavailable` confidence sensor, and the
fact that waiting longer could never have helped — the entity was crashing
on every update, not merely slow to accumulate data.

## 3. Why this wasn't caught before shipping

The v1.1.5/1.1.6 test suites (283, then 296 tests) exercised the
**engine** (`battery_health.py`) exhaustively — every formula, edge case,
and persistence path. They did not instantiate the actual
`SensorEntity` subclasses from `battery_health_entities.py` against HA's own
state-validation contract. That was a real coverage gap: correct engine
output does not guarantee a correct HA entity declaration. A dataclass field
can be perfectly correct and still violate an API contract in the layer that
displays it.

## 4. The fix

- `confidence` is now declared with `device_class: SensorDeviceClass.ENUM`
  and an explicit `options` list — Home Assistant's documented, idiomatic way
  to represent a fixed-set string sensor. This is a discoverability
  improvement too: HA's UI can now render it as a proper enum/select in
  places that support it, rather than a plain, untyped string.
- `_attr_suggested_display_precision` moved from a class attribute to a
  **per-instance** assignment in `__init__`, explicitly skipped for any
  attr_key in a new `_STRING_VALUED_KEYS` frozenset. This closes the
  mechanism, not just the one symptom — any future string-valued sensor
  added to the table is protected by construction as long as its key is
  added to that set (and the new test suite checks every table entry, not
  just `confidence`, against HA's real rule).

## 5. Test evidence

`tests/test_battery_health_entities.py` (new, 7 tests) re-implements HA's
actual `SensorEntity.state` numeric-value rule from the traceback (not a
mock of it) and runs every real entity's resolved attributes through it for
every value it can plausibly report, including all three confidence strings
and representative numeric values for the rest.

**This test was verified to actually catch the original bug**, not just to
pass: the class-level `_attr_suggested_display_precision = 1` was temporarily
reintroduced during this fix, the new test failed with the same shape of
assertion the log's traceback represents, and passed again once reverted.
That is stronger evidence than a clean pass alone.

Full suite: **303 passed, 1 skipped, 0 failed** (was 296/1), deterministic
across repeated runs. All prior tests (T1–T17) pass unmodified — this was a
pure entity-declaration fix; no engine formula or behavior changed.

## 6. Scope check — anything else in the entity table at risk?

Reviewed every other `_BATTERY_HEALTH_SENSORS` entry against the same rule:
`bhi`, `soh_capacity/efficiency/balance`, `efc`, `warranty_consumed_pct` are
all genuinely numeric (percent or count) and correctly keep the precision
hint. `stress_index`, `predicted_soh`, `health_divergence` are numeric.
`confidence` was the only string-valued entity in the table, and is now
covered by both the `_STRING_VALUED_KEYS` mechanism and the new test's
blanket check over the whole table (`test_all_string_valued_keys_pass_ha_numeric_check`)
so a future addition doesn't require remembering to update this audit.

## 7. Note on the unrelated Modbus timeouts in the log

`HV2220098926_battery_data_update_coordinator` and several sibling
coordinators show frequent `Modbus timeout (no response in N s)` with
backoff, on both inverters, throughout the log window. This predates and is
independent of the battery-health subsystem — it affects the underlying
coordinator all registers share, not something these changes introduced or
can fix. Flagged for the user's own network/RTU-bridge investigation, out of
scope here.

**Verdict:** root cause identified and fixed at the mechanism level (not just
patched for `confidence`), regression-tested against the real bug (proven to
catch it), and the coverage gap that let it ship is closed for this file
going forward.
