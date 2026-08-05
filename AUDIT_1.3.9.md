# Release Audit — huawei_solar v1.3.9

**Date:** 2026-08-05 · **Auditor:** Claude (Anthropic)
**Baseline:** v1.3.8 (deployed; reload/listener fix confirmed working in the
field — no "unknown job listener" error, previously-dead coordinator
recovered)
**Type:** defect fix (one production file changed, `sensor.py`, plus a new
constant in `const.py`), plus a requested audit of every other Modbus-write
code path in the codebase (§5).

---

## 1. The report

v1.3.8 was deployed and validated for its own claims: the reload/listener
defect (v1.3.7) held up in the field, and no coordinator was left dead after
a reload. However, Home Assistant's "still starting up" banner for
`huawei_solar` was **still ~2 minutes**, unchanged from before v1.3.7/v1.3.8.
Since both of those releases only touched `__init__.py`'s entry-level setup
and two coordinator factories, this meant the actual bottleneck was
somewhere neither had looked.

## 2. Diagnosis from a real Home Assistant core log

A `home-assistant.log` capture from the v1.3.8 boot in question contained:

```
04:10:52.596 WARNING [homeassistant.components.sensor] Setup of sensor platform huawei_solar is taking over 10 seconds.
04:11:18.923 WARNING [homeassistant.bootstrap] Waiting for integrations to complete setup: {('huawei_solar', ...): ...}
04:11:29.644 ERROR   [custom_components.huawei_solar] Error fetching HV2220080950_config_data_update_coordinator data: Timeout communicating with HV2220080950: no response in 20 s (consecutive: 1)
04:11:29.743 ERROR   [custom_components.huawei_solar] Error fetching HV2220098926_battery_data_update_coordinator data: Timeout communicating with HV2220098926: no response in 21 s (consecutive: 1)
```

Two things this pinpointed:

1. **The slow watchdog line names the `sensor` platform specifically** —
   Home Assistant's generic "platform is taking over 10 seconds" warning
   fires per-platform, for whichever platform's own `async_setup_entry`
   (in `sensor.py`, a file neither v1.3.7 nor v1.3.8 touched) hasn't
   returned yet. This is categorically different from the entry-level
   setup those two releases fixed.
2. **The two independent 20-21s coordinator timeouts in the same window**
   are corroborating evidence, not the cause themselves: they confirm the
   device genuinely was slow to respond at that point in time, which is the
   condition the code found in §3 is specifically vulnerable to.

## 3. Root cause

`create_sun2000_entities()` in `sensor.py` (called once per inverter from
the sensor platform's own setup — which runs *after* `__init__.py`'s
entry-level `async_setup_entry` has already returned, via
`await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)`)
contained:

```python
if (
    not isinstance(ucs.device.primary_device, (EMMADevice, SmartLoggerDevice))
    and await ucs.device.has_write_permission()
    and ucs.configuration_update_coordinator
):
    entities_to_add.append(HuaweiSolarActivePowerControlModeEntity(...))
```

`has_write_permission()` (vendor library, `device/base.py`) performs a real
read of a test register followed by writing the same value back — genuine
Modbus traffic, not a cached property. Three properties of this call site
made it a serious latent defect:

1. **Outside the entire optimisation layer.** Called directly on
   `ucs.device`, not through `ModbusGuard` or the adaptive controller — none
   of the pacing, backoff, or shared-bus serialisation logic built across
   this project's history applies to it.
2. **No timeout of our own.** Bounded only by the vendor library's internal
   per-request timeout (confirmed in the installed `huawei_solar` package:
   `DEFAULT_TIMEOUT = 10` seconds per underlying request — the call chain
   here is a read then a write, so up to ~20s, plausibly more with retry/
   re-login paths inside the library's own `set()` implementation for
   login-capable devices).
3. **No exception handling at the call site.** The vendor library's
   `has_write_permission()` only catches `PermissionDeniedError` and
   `WriteException` internally; anything else (a raw `TimeoutError`,
   `ConnectionException`, etc.) would propagate straight out of
   `create_sun2000_entities()`, out of `async_setup_entry` in `sensor.py`,
   and **crash the entire sensor platform's setup** — every sensor entity
   on the entry, not just the one optional entity this check decides
   whether to add.

Run once per SUN2000 device, sequentially (the calling loop in `sensor.py`
awaits `create_sun2000_entities()` per device, one at a time), on **every
single boot and reload** — no caching, no persistence across reloads.

## 4. The fix

`_has_write_permission_bounded()`, new in `sensor.py`:

```python
async def _has_write_permission_bounded(device, serial_number):
    try:
        return await asyncio.wait_for(
            device.has_write_permission(),
            timeout=WRITE_PERMISSION_CHECK_TIMEOUT.total_seconds(),
        )
    except TimeoutError:
        _LOGGER.warning(...)
        return False
    except Exception:
        _LOGGER.exception(...)
        return False
```

`WRITE_PERMISSION_CHECK_TIMEOUT` (new, `const.py`) is set to **5 seconds** —
deliberately shorter than `UPDATE_TIMEOUT` (35s, used for real data polls):
a healthy, responsive device answers this in well under a second (per the
vendor library's own comment elsewhere: *"responding devices reply in
milliseconds"*), so there is no reason to wait as long as a genuine data
poll would for a check that only gates one optional entity.

On timeout or any other failure, the optional entity is simply skipped for
this setup pass — identical in effect to the vendor library's own existing
"no permission" outcome — and is re-attempted automatically on the next
reload, once the device is responsive again.

### 4.1 An incidental fix, flagged rather than buried

While adding the logger needed for this fix, a pre-existing latent defect
was found: `sensor.py` had **no `_LOGGER` defined anywhere in the file**,
despite one existing call site (`_LOGGER.exception(...)`, inside the
battery-health fault-isolation `except` block from v1.1.7) already
referencing it. That handler exists specifically to contain failures
without crashing the platform — and would itself have raised `NameError`
the first time it actually fired, silently defeating its own purpose. This
release's `_LOGGER = logging.getLogger(__name__)` addition (needed for
Defect H's own logging) fixes this as a side effect. Noted explicitly per
project convention rather than left implicit.

## 5. Requested: audit of every other Modbus-write code path

The operator asked, correctly, whether the same class of problem could
recur wherever the integration writes to the inverter — number, switch,
select, and button entities, plus every service in `services.py`. Every
`await device.set(...)` / `await dd.device.set(...)` call site in the
codebase was enumerated and checked against the same three properties that
made Defect H dangerous (§3).

**Finding: this is a real, related, but structurally different and lower-
severity situation. Not fixed in this release — reasoning below.**

| Property | Defect H (sensor.py setup) | Write entities / services.py |
|---|---|---|
| Bypasses ModbusGuard/adaptive controller | Yes | Yes — same gap, confirmed |
| Local exception handling at call site | None | None — confirmed, same gap |
| Runs during entry/platform **setup** | Yes | **No** — runs later, in response to a user action or a service call |
| Blast radius of an unhandled exception | Entire sensor platform (every entity, this entry) | Contained to that single service call by Home Assistant's own service-dispatch machinery |
| Bounded by *something* | No (our layer); yes (vendor `DEFAULT_TIMEOUT=10s`, same as Defect H) | Same vendor-level 10s bound applies |

**Why the blast-radius difference matters enough to defer, not ignore.**
Home Assistant's entity/service-call dispatch catches exceptions raised
from `async_turn_on`, `async_set_native_value`, `async_select_option`,
`async_press`, and service handlers, surfacing them as a failed call to the
user or calling automation — it does not cancel other entities or the
entry. This is standard, supported behaviour, not a gap this project needs
to patch. The single defining property that made Defect H urgent — an
unhandled failure taking down *everything*, during a phase Home Assistant
itself is watching with a hard setup-timeout — genuinely does not apply
here.

**What *is* still a real, smaller gap, for the record:**

- **No local timeout on writes.** Each `device.set(...)` is bounded by the
  vendor library's own ~10s transport timeout, not by anything this project
  chose deliberately for the use case — a user pressing a button or an
  automation calling a service could stall up to that long with no
  intermediate feedback.
- **No locally-friendly error handling.** A failure surfaces as the raw
  vendor-library exception, not a message written with the operator in
  mind, unlike the pattern established everywhere else in this codebase
  (`ConfigEntryNotReady` messages in `__init__.py`, the new
  `_has_write_permission_bounded` messages in this release).
- **Multi-step `services.py` sequences are not transactional.** Several
  services (e.g. the forcible charge/discharge helpers, `services.py`
  ~lines 403-508) issue multiple sequential `.set()` calls. A failure
  partway through a sequence — device goes briefly busy between the second
  and third write — leaves the inverter in whatever partial state the
  completed writes left it in, with no rollback and no explicit warning
  that the sequence was incomplete. For commands that affect real battery
  charge/discharge behaviour, this is worth fixing, but is a meaningfully
  larger design question (does a partial sequence need to be rolled back?
  retried? does the operator need an explicit "sequence incomplete"
  notification?) than a bounded-timeout wrapper, and deserves its own
  investigation and audit rather than being bundled into this one.

**Decision: audited and documented now, not fixed now.** Per project
convention (ship on a measurement, one change at a time, don't stack
unrelated fixes into a single release) and because none of this was part
of what was reported or reproduced this session — unlike Defect H, nothing
here has an observed field incident behind it yet. Recommended as the next
piece of work, tracked in §7.

## 6. Adversarial verification

New `tests/test_write_permission_bounded.py`, two angles:

**Behavioural**, against fake devices with controllable latency/failure:
- A device whose `has_write_permission()` never resolves — the wrapper
  times out at the configured bound instead of hanging. A companion
  adversarial test confirms this same fake device, called the OLD
  (unwrapped) way, actually hangs past a much larger bound — proving the
  fake reproduces the real hazard, so the wrapper's pass is meaningful.
- A device whose `has_write_permission()` raises `ConnectionError` (a
  failure mode the vendor library does *not* catch internally) — the
  wrapper absorbs it and returns `False` rather than propagating.
- A healthy device — the wrapper still returns the correct result promptly,
  confirming the fix does not change behaviour in the normal case.

**Static (AST)**: confirms `create_sun2000_entities` no longer calls
`.has_write_permission()` directly anywhere in its body, and that
`_has_write_permission_bounded()` exists and genuinely uses
`asyncio.wait_for` (not merely wrapping the call without bounding it). Run
against the pre-fix `sensor.py`, both fail — correctly, at the exact
original line (1188). Run against this release, both pass.

## 7. Outstanding, for the next session (not this release)

- §5's write-path gaps: local timeouts on `device.set(...)` calls in
  number/switch/select/button entities and `services.py`; friendlier error
  surfacing; whether multi-step `services.py` sequences need transactional
  handling. No field incident yet — audit only, no urgency assigned beyond
  "worth doing."
- Everything already outstanding from the 2026-08-04 handoff and
  `AUDIT_1.3.8.md` §10 remains outstanding: the register-cache/startup-
  persistence work (handoff §5), the bus-scheduler redesign (handoff §5,
  itself pending the request-volume question), and §2.4's still-uncaptured
  button-platform setup traceback.

## 8. Safety properties

- No change to Modbus chunking, adaptive control, or battery-health logic.
  `update_coordinator.py`, `adaptive_modbus.py`, `battery_health.py`,
  `battery_health_manager.py`, `register_cache.py`, `modbus_guard.py`,
  `__init__.py` are byte-identical to the audited v1.3.8 tree.
- v1.3.7's and v1.3.8's fixes are untouched and still in place.
- The isolation contract this fix extends (v1.1.7: nothing optional should
  be able to block or fail core setup) is reinforced here for the third
  time this session (v1.3.8 for two coordinator refreshes, this release for
  the write-permission probe) — establishing it as a recurring pattern
  worth checking for explicitly in any future addition to setup-path code.

## 9. Test evidence

- **481 passed, 1 skipped, 0 failed**, deterministic across 3 repeated runs
  (was 475; 6 new tests in `test_write_permission_bounded.py`).
- Adversarial: static tests fail against the pre-fix `sensor.py` at the
  exact original line (1188); pass against this release.
- Static: `py_compile` clean on both changed files; manifest version =
  1.3.9.
- Confidentiality sweep: clean.
- Diffed against the v1.3.8 tree to confirm only `sensor.py` and `const.py`
  changed among production files.

## 10. Recommended deployment procedure

1. **Delete** `/config/custom_components/huawei_solar` entirely.
2. Extract v1.3.9 fresh into `custom_components/`.
3. Restart Home Assistant once.
4. **Required validation, specific to this release:** time the "still
   starting up" banner again. It should now be materially shorter than the
   ~2 minutes observed on v1.3.8 — record the new figure for comparison.
5. **Still outstanding from v1.3.8, re-confirm on this version too:**
   trigger a plain reload and confirm no coordinator is left dead and no
   "unknown job listener" error appears (already validated once on v1.3.8;
   worth re-checking since this release changed a different file, to rule
   out any interaction).
6. If the startup window is now close to the ~20s baseline typical of other
   integrations, this closes the multi-minute-startup investigation begun
   after v1.3.8. If a meaningful delay remains, that is real information —
   the next place to look, per this same log-driven method, would be
   `create_device_instance()` (the very first awaited call in
   `__init__.py`'s `async_setup_entry`, not yet individually timed) or the
   sequential (non-parallel) per-device setup for daisy-chained inverters.

**Verdict:** release-ready. A real, adversarially-verified defect with a
directly-observed field signature (the sensor-platform watchdog line,
corroborated by two independent coordinator timeouts in the same window) is
fixed. The requested audit of every other write path found a related but
structurally lower-severity gap, documented in full rather than bundled in
as an unrequested, unmeasured change — consistent with this project's
standing rule not to stack changes without a measurement behind each one.
