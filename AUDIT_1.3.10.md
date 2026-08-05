# Release Audit — huawei_solar v1.3.10

**Date:** 2026-08-05 · **Auditor:** Claude (Anthropic)
**Baseline:** v1.3.9 (deployed; sensor-platform bounded fix confirmed
present in the field debug capture used as evidence for this release)
**Type:** defect fix, one production file changed (`__init__.py`).

---

## 1. The report

v1.3.9 was deployed. The startup banner was still ~2 minutes. Debug logging
was enabled via the UI (Settings → Devices & Services → Huawei Solar →
Enable debug logging) and a reload was triggered, producing a full
DEBUG-level trace of `async_setup_entry` for the first time this
investigation had one.

## 2. Diagnosis from the debug trace

Key timestamps from the captured reload (`Bus endpoint: 192.168.7.22:502`
at `04:42:29.873` marks the start of `async_setup_entry`):

```
04:42:29.873  Bus endpoint: 192.168.7.22:502                    (t0)
04:42:34.279  SynchronizedPowerCoordinator enabled               (+4.4s — v1.3.8's fix confirmed: fires immediately, not after a blocking read)
04:42:44.282  "sensor platform taking over 10 seconds"           (+14.4s)
04:42:49.853  power_meter first fetch: 7.001s
04:43:09.312  config_data_update_coordinator first fetch: 19.454s (+39.4s)
04:43:24.899  battery_data_update_coordinator first fetch: 20.472s (+55.0s)
```

Immediately preceding the two ~20s fetches:

```
ModbusGuard[192.168.7.22:502]: queue full (2/1) — shedding request
HV2220080950_config_data_update_coordinator: request shed by bus guard (ModbusGuard[192.168.7.22:502] queue full (2/1)); not recorded as an inverter failure
HV2220080950_config_data_update_coordinator: Modbus timeout #1
Retry after triggered. Scheduling next update in 10 second(s)
Error fetching HV2220080950_config_data_update_coordinator data: Timeout communicating with HV2220080950: no response in 20 s (consecutive: 1)
Finished fetching HV2220080950_config_data_update_coordinator data in 10.004 seconds (success: False)
...
HV2220080950_config_data_update_coordinator: communication restored (after 1 timeout(s) / 1 failure(s))
```

This is a direct, named mechanism, not an inference: `ModbusGuard`'s
in-flight/queued request slot (depth 1 at the time) was already occupied
when a second coordinator's first-poll request arrived; the second request
was **shed** (rejected outright, not queued), which triggered that
coordinator's own "Modbus timeout" → 10-second retry-after cycle. Two such
rounds ≈ 20s, matching both affected coordinators' first-fetch durations
and the "no response in 20 s" wording precisely.

## 3. Root cause

`_COORDINATOR_START_DELAYS` (introduced v1.0.3) staggers one device's four
coordinator types across their first poll to avoid exactly this kind of
collision — but the offsets are **fixed per coordinator type, identical
across every device**. This installation has two daisy-chained SUN2000
inverters sharing one `ModbusGuard` endpoint (both route through
`192.168.7.22:502`). Both inverters' `configuration` coordinators wake for
their first poll at the identical +10s offset from their own setup; both
`energy_storage` coordinators both at +14s; and so on for all four types.
The stagger scheme has no representation of "a second device exists on
this same bus" — it was designed and has only ever been exercised (in this
project's own history) against single-device installations.

## 4. Why the fix is a targeted stagger, not a wider queue

`ModbusGuard`'s queue depth is not a fixed constant chosen once — it is
computed continuously in `adaptive_modbus.py`, blending a confidence-
weighted learned value against a cold-start default, then clamped to
`[1, MAX_QUEUE_DEPTH=3]`. The depth of 1 observed in the field capture was
not a default or a coincidence: the log confirms `AdaptiveModbus[...]:
loaded 71 days of learning data` for this exact bus immediately before the
collision. Depth=1 is what this project's own adaptive layer has
determined, from real operating history, is correct for this bus's
steady-state traffic.

**Widening the queue depth globally was considered and rejected.** Doing
so would override a learned value — arrived at from 71 days of real
conditions — for the other 99.9% of the time the bus is not in a
just-reloaded state, to solve a collision that only exists in a ~20-second
window right after boot/reload. This is precisely the shape of mistake
this project's own process rules (see `HANDOFF_2026-08-04.md` §7, rule 5)
already warn against: a plausible-sounding global parameter change, made
without measuring what it does to the case that actually matters (steady
state, which is the overwhelming majority of the bus's life). The 2026-08-05
session had already found the ModbusGuard's `MAX_QUEUE_DEPTH=3` ceiling and
`ADAPTIVE_QUEUE_DEPTH_COLD_START=2` default while first investigating this
mechanism (§2 of the corresponding conversation), and confirmed the current
1 is the *learned*, not default, value — reinforcing the same conclusion.

The fix instead eliminates the collision at its source, leaving the
learned queue-depth model completely untouched.

## 5. The fix

New in `__init__.py`:

```python
_MULTI_DEVICE_STAGGER_STRIDE = timedelta(seconds=5)

def _staggered_start_delay(kind: str, device_index: int) -> timedelta:
    return _COORDINATOR_START_DELAYS[kind] + device_index * _MULTI_DEVICE_STAGGER_STRIDE
```

`device_index` (0 for the primary device, incrementing for each additional
daisy-chained slave device on the same entry) is threaded from the
existing device-setup loop in `async_setup_entry`, through
`_setup_device_data`, into `_setup_inverter_device_data`, where all four
`start_delay=` call sites (main, power_meter, energy_storage,
configuration) now call `_staggered_start_delay(kind, device_index)`
instead of looking up `_COORDINATOR_START_DELAYS[kind]` directly.

**Device 0 is unaffected — byte-identical timing to before this release.**
Only a second (or further) device sharing the same bus gets shifted into
its own, non-overlapping window. This is scoped entirely to the one-time
first-poll delay (`if not self._first_poll_done` in
`update_coordinator.py`, unchanged by this release) — nothing about
steady-state adaptive scheduling, queue depth, or per-request pacing is
touched.

### 5.1 Scope note

This release does not touch the non-inverter `_setup_device_data` path
(standalone meter/SDongle/SmartLogger devices not behind a SUN2000), which
does not currently apply any `start_delay` at all. That path was not
implicated by the field evidence in §2 and is left alone, consistent with
this project's convention of fixing the confirmed mechanism rather than
speculatively expanding scope.

## 6. Adversarial verification

New `tests/test_multi_device_stagger.py`, two angles:

**Behavioural**, against the staggering arithmetic itself (reproduced in
the test file per this project's established trade-off for `__init__.py`'s
heavy import graph — see `test_learning_gate_unsub.py`'s precedent):
- Device 0 gets exactly today's existing offsets, unchanged — confirms no
  behaviour change for the common single-device case.
- Device 1 gets a distinct offset from device 0 for every coordinator
  type — the exact collision this release fixes.
- A third and fourth simulated device each get their own distinct offset —
  confirms the fix scales past the two-device case actually observed, not
  just a hardcoded pair.
- The stride (5s) is asserted to comfortably exceed a healthy first-poll
  exchange's observed duration (well under 1s in the field capture), so
  the separation is real, not just numerically nonzero.

**Static (AST)**: confirms every `start_delay=` call site in
`_setup_inverter_device_data` routes through `_staggered_start_delay(...)`
rather than a raw `_COORDINATOR_START_DELAYS[...]` lookup (the exact shape
of Defect I), and that `_setup_inverter_device_data` accepts a
`device_index` parameter at all. Run against the pre-fix `__init__.py`,
both fail correctly. Run against this release, both pass.

## 7. Safety properties

- No change to `ModbusGuard`, the adaptive controller, or any learned
  parameter. `modbus_guard.py`, `adaptive_modbus.py`,
  `register_cache.py`, `update_coordinator.py`, `sensor.py`, `const.py`
  are byte-identical to the audited v1.3.9 tree.
- v1.3.7's, v1.3.8's, and v1.3.9's fixes are untouched and still in place.
- Single-device installations (the majority case across this project's
  history) see zero behavioural change — verified directly in §6, not just
  asserted.
- The change is purely additive to existing, working scheduling logic — no
  existing code path was removed or restructured, only the source of one
  input (`start_delay`) to four already-existing constructor calls.

## 8. Test evidence

- **487 passed, 1 skipped, 0 failed**, deterministic across 3 repeated runs
  (was 481; 6 new tests in `test_multi_device_stagger.py`).
- Adversarial: static tests fail against the pre-fix `__init__.py`; pass
  against this release.
- Static: `py_compile` clean; manifest version = 1.3.10.
- Confidentiality sweep: clean (device serials in this document and the
  originating conversation are pseudonymised/real-but-already-shared by the
  operator in their own debug capture, consistent with prior audits'
  handling of field data).
- Diffed against the v1.3.9 tree to confirm only `__init__.py` changed.

## 9. Recommended deployment procedure

1. **Delete** `/config/custom_components/huawei_solar` entirely.
2. Extract v1.3.10 fresh into `custom_components/`.
3. Restart Home Assistant once.
4. **Required validation, specific to this release:** with debug logging
   still enabled (or re-enabled), trigger a reload and check specifically
   for the absence of `ModbusGuard[...]: queue full ... — shedding
   request` lines involving the second device's coordinators during
   startup, and that neither `config_data_update_coordinator` nor
   `battery_data_update_coordinator`'s first fetch takes anywhere near
   19-20s this time.
5. Time the overall "still starting up" banner once more. Between v1.3.9
   and this release, the two coordinators responsible for roughly 40 of
   the observed ~75-90 seconds should no longer stall — if the banner
   drops close to the ~20s baseline typical of other integrations, this
   closes the multi-minute-startup investigation. If a smaller but real
   delay remains, `create_device_instance()` (the very first awaited call,
   still never individually timed) remains the next place to look, per
   the same log-driven method used throughout this investigation.

**Verdict:** release-ready. A real, precisely-identified, adversarially-
verified defect is fixed at its actual mechanism, with an explicit,
documented decision not to take the simpler-looking but riskier path of
overriding a learned system parameter. This is the fourth defect fixed
in this investigation (F: reload listener; G: blocking coordinator
refreshes; H: unbounded write-permission probe; I: device-blind stagger),
each identified from progressively more precise evidence — full-day
capture, HA log timestamps, then a debug-level trace — rather than from
a fixed starting hypothesis.
