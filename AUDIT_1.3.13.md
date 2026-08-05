# Release Audit — huawei_solar v1.3.13

**Date:** 2026-08-05 · **Auditor:** Claude (Anthropic)
**Baseline:** v1.3.12
**Type:** defect fix, single production file changed (`update_coordinator.py`).
This release closes an investigation open since before this session began.

---

## 1. The report

A field reload was captured with debug logging enabled. The user reported
the first reload attempt **failed**; a second, immediate reload succeeded.
The captured log contained, for the first time in this project's history,
a complete traceback for "Setup of config entry ... cancelled" — an error
the original 2026-08-04 handoff document had already flagged as recurring
for 4+ hours with its causal attribution explicitly marked unresolved
(*"Honest status: not resolved... Do not assume either explanation without
more evidence"*).

## 2. The traceback

```
2026-08-05 05:50:54.259 ERROR [custom_components.huawei_solar] Setup of config entry 'SUN2000-10KTL-M1' for huawei_solar integration cancelled
Traceback (most recent call last):
  File ".../homeassistant/config_entries.py", line 798, in __async_setup_with_context
    result = await component.async_setup_entry(hass, self)
  File ".../homeassistant/components/sensor/__init__.py", line 103, in async_setup_entry
    return await hass.data[DATA_COMPONENT].async_setup_entry(entry)
  File ".../homeassistant/helpers/entity_component.py", line 194, in async_setup_entry
    return await self._platforms[key].async_setup_entry(config_entry)
  File ".../homeassistant/helpers/entity_platform.py", line 467, in async_setup_entry
    return await self._async_setup_platform(async_create_setup_awaitable)
  File ".../homeassistant/helpers/entity_platform.py", line 508, in _async_setup_platform
    await asyncio.gather(*pending)
  File ".../homeassistant/helpers/entity_platform.py", line 773, in async_add_entities
    await self._async_add_and_update_entities(entities, timeout, config_subentry_id)
  File ".../homeassistant/helpers/entity_platform.py", line 672, in _async_add_and_update_entities
    results = await asyncio.gather(...)
  File ".../homeassistant/helpers/entity_platform.py", line 858, in _async_add_entity
    await entity.async_device_update(warning=False)
  File ".../homeassistant/helpers/entity.py", line 1378, in async_device_update
    await self.async_update()
  File ".../homeassistant/helpers/update_coordinator.py", line 711, in async_update
    await self.coordinator.async_request_refresh()
  File ".../homeassistant/helpers/update_coordinator.py", line 309, in async_request_refresh
    await self._debounced_refresh.async_call()
  File ".../homeassistant/helpers/debounce.py", line 128, in async_call
    await task
  File ".../homeassistant/helpers/update_coordinator.py", line 435, in _async_refresh
    self.data = await self._async_update_data()
  File "/config/custom_components/huawei_solar/update_coordinator.py", line 616, in _async_update_data
    await asyncio.sleep(self._start_delay.total_seconds())
  File ".../asyncio/tasks.py", line 704, in sleep
    return await future
asyncio.exceptions.CancelledError
```

This is the exact code line (616) of this coordinator's own first-poll
stagger delay (`_COORDINATOR_START_DELAYS`, introduced v1.0.3), caught by
a `CancelledError` mid-sleep.

## 3. Root cause

The stagger delay was written assuming `_async_update_data()` would only
ever be invoked by the coordinator's own internal, background-scheduled
refresh loop — a context where sleeping inline is harmless, since nothing
else is waiting on the result. The traceback disproves that assumption:
Home Assistant's own entity-add machinery, while adding an entity during
platform setup, synchronously awaited that entity's coordinator performing
a full refresh (`entity_platform._async_add_entity` →
`entity.async_device_update` → `async_update` →
`coordinator.async_request_refresh()`) — a path this project had not
identified or accounted for.

The sleep therefore directly extended a real, synchronous Home Assistant
setup call by up to the configured stagger delay. Defect I (v1.3.10) made
the worst case larger for any device beyond the first: `energy_storage`'s
delay for device index 1 became `14s + (1 × 5s stride) = 19s`, up from 14s
before that fix. When the cumulative time this setup call was already
taking (device detection, per-entity setup, other coordinators' own first
polls) plus this sleep exceeded whatever timeout Home Assistant enforces
on config entry setup, Home Assistant cancelled the entire setup task —
landing, in the captured incident, exactly inside this sleep.

**This is a genuinely new causal finding, not previously established with
this level of confidence.** Prior sessions (and the original 2026-08-04
handoff) suspected a connection between load and the cancellation error
but explicitly declined to assert one without direct evidence. This
traceback is that evidence.

## 4. What this release does NOT claim

The precise reason this particular entity's initial add ended up on the
synchronous `async_device_update` → `async_update` path — rather than the
push-based, non-blocking pattern most `CoordinatorEntity` usage in this
codebase relies on (`async_add_listener`, no forced initial fetch) — was
not independently root-caused against the specific Home Assistant version
in use. Investigating that further (a `should_poll` gap on a specific
entity class, a behavioural change in this Home Assistant version's entity
platform, or something else) remains open. This release does not depend
on knowing the answer: it closes the risk structurally, by making the
stagger delay incapable of blocking *any* caller, synchronous or not,
rather than by patching whichever specific code path triggered it in this
one captured incident.

## 5. The fix

`_async_update_data()`'s first-poll stagger block no longer sleeps inline.
On the first call:

```python
if not self._first_poll_done:
    self._first_poll_done = True
    if self._start_delay.total_seconds() > 0:
        self._schedule_deferred_first_poll()
        return dict(self.data) if self.data else {}
```

`_schedule_deferred_first_poll()` runs the actual delay and real first
poll as a background task:

```python
def _schedule_deferred_first_poll(self) -> None:
    async def _deferred() -> None:
        try:
            await asyncio.sleep(self._start_delay.total_seconds())
            await self.async_request_refresh()
        except Exception:
            _LOGGER.exception(...)
    self.hass.async_create_task(_deferred())
```

The immediate return value (empty dict, or a copy of existing cached data)
is not a new pattern invented for this fix — it mirrors an existing line a
few statements later in the same method (`if not all_names: return {}`,
used when there is nothing to poll at all), and every entity in this
codebase already handles "no data yet" via `if self.coordinator.data and
...` checks. The deferred task's later call to `async_request_refresh()`
re-enters `_async_update_data()` with `_first_poll_done` already `True`,
so it proceeds straight to real work — the actual first poll still happens
after exactly the same stagger delay as before, just never blocking
whoever called the method the first time.

### 5.1 What is deliberately unchanged

- The stagger *values* themselves (`_COORDINATOR_START_DELAYS`,
  `_MULTI_DEVICE_STAGGER_STRIDE`, Defect I) are untouched — this release
  is about how the delay is honoured, not what the delay should be.
- The bus-serialisation effect the delay exists for is fully preserved: no
  real Modbus traffic happens before the deadline, exactly as before.
- Nothing about `ModbusGuard`, the adaptive controller, or any other
  coordinator's behaviour changed.

## 6. Adversarial verification

New `tests/test_deferred_first_poll.py`, two angles:

**Behavioural**, via an isolated reproduction of the exact fixed logic
(this project's established trade-off for files too heavy to import
directly — see `test_learning_gate_unsub.py`'s precedent):
- The first call returns well within 0.5s even against a 10-second
  configured delay — proving non-blocking behaviour directly, not by
  assertion.
- No real work happens on that first call.
- A companion adversarial test reproduces the OLD (inline-sleep) pattern
  and confirms it genuinely blocks for the full delay (times out a 0.5s
  `wait_for` against a 10s sleep) — proving the fixed pattern's pass above
  is meaningful, not a fake that could never fail.
- The deferred background task is confirmed to perform the real work once
  its delay elapses.
- The zero-delay case (device 0, the common case) is confirmed to do real
  work immediately, exactly as before this fix — no behavioural change for
  the primary device.

**Static (AST)**: confirms the real `update_coordinator.py`'s first-poll
stagger block does not contain a direct `await asyncio.sleep(...)` (only
reachable from inside the deferred task's own nested coroutine, which is
where the fix intentionally puts it), and that
`_schedule_deferred_first_poll` exists. Run against the pre-fix file, both
checks fail at the exact original line (616). Run against this release,
both pass.

## 7. Safety properties

- No change to `_COORDINATOR_START_DELAYS`, `_MULTI_DEVICE_STAGGER_STRIDE`,
  `ModbusGuard`, the adaptive controller, or any other coordinator logic.
  `__init__.py`, `modbus_guard.py`, `adaptive_modbus.py`,
  `register_cache.py`, `sensor.py`, `number.py`,
  `synchronized_power_coordinator.py` are byte-identical to the audited
  v1.3.12 tree.
- Defects F, G, H, I, J, and V2-1 (v1.3.7-v1.3.12) are untouched and still
  in place.
- The deferred task follows the same "must never raise" convention already
  established throughout this session (Defects G, H, J2) — wrapped in
  try/except, logged, never propagated.
- Zero behavioural change for `start_delay=0` (device 0, the primary
  device on every installation) — verified directly, not just asserted.

## 8. Test evidence

- **507 passed, 1 skipped, 0 failed**, deterministic across 3 repeated
  runs (was 500; 7 new tests in `test_deferred_first_poll.py`).
- Adversarial: both static checks fail against the pristine pre-fix file
  at the exact original line (616); pass against this release.
- Static: `py_compile` clean; manifest version = 1.3.13.
- Confidentiality sweep: clean.
- Diffed against the v1.3.12 tree to confirm only `update_coordinator.py`
  changed.

## 9. Recommended deployment procedure

1. Delete `/config/custom_components/huawei_solar` entirely.
2. Extract v1.3.13 fresh into `custom_components/`.
3. Restart Home Assistant once.
4. **Required validation, specific to this release:** the real test of
   this fix is a reload attempt during conditions similar to the captured
   incident (slow/still-settling device response). There is no way to
   force this condition on demand; the practical validation is simply
   **not seeing "Setup of config entry ... cancelled" recur** across
   normal use going forward. If it does recur, capture debug logging
   again immediately — the traceback will show definitively whether it's
   the same mechanism (in which case this fix did not fully close it) or
   a different one (in which case this is a second, independent cause
   worth its own investigation, same as this session found for the
   startup-latency question with Defects G, H, I, and J).
5. This release is not expected to be independently visible in aggregate
   startup timing the way Defects G/H/I/J were — its effect is on
   *robustness* (preventing a specific failure mode), not on the total
   duration of a successful setup.

**Verdict:** release-ready. This closes, with direct evidence for the
first time, an issue that predates this entire session and was explicitly
left unresolved in the original 2026-08-04 handoff. Six real defects have
now been found and fixed across this investigation (F, G, H, I, J, K),
each identified from progressively better evidence — a full-day capture,
precise HA log timestamps, then two independent external audits, then
finally a debug-level traceback of the exact failure — rather than from a
fixed starting hypothesis. That progression, not any single fix, is the
actual result of this session.
