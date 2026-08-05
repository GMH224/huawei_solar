# Release Audit — huawei_solar v1.3.17

**Date:** 2026-08-05 · **Auditor:** Claude (Anthropic)
**Baseline:** v1.3.16
**Type:** single defect fix, one production file changed (`update_coordinator.py`).
Explicitly requested with full rigor: "we cannot use current version."

---

## 1. The report

Following v1.3.16's deployment, screenshots showed `Inverter 10K`
(`HV2220098926`) with most sensors `Unknown` — efficiency, daily yield,
alarms, off-grid status, internal temperature, DSP data collection —
while `Inverter 5K` (`HV2220080950`), on the same shared bus, showed real,
current values. A Home Assistant debug log covering the same window was
provided, followed by a second, more complete capture of the same
incident.

## 2. Diagnosis

The second log capture made the mechanism unambiguous. Tracing
`HV2220098926_battery_data_update_coordinator` and
`HV2220098926_data_update_coordinator` continuously for over ten minutes:

```
back-off cycle 1 — sleeping 9.6 s   →  Finished ... in 9.617 seconds (success: True)
back-off cycle 2 — sleeping 9.1 s   →  Finished ... in 9.149 seconds (success: True)
back-off cycle 3 — sleeping 10.2 s  →  Finished ... in 10.202 seconds (success: True)
...
back-off cycle 11 — sleeping 10.7 s →  Finished ... in 10.662 seconds (success: True)
```

Two things stand out, together conclusive:

1. **The back-off cycle counter climbs monotonically (1 through 11+)
   with zero interleaved shed, timeout, or failure messages anywhere in
   between.** If the device were genuinely still failing intermittently,
   there would be visible failures between successes. There are none.
2. **Every "successful" cycle's total duration matches its own back-off
   sleep almost exactly**, with no meaningful time left over. A real
   Modbus exchange — even a fast one under good conditions — takes
   measurable time on top of the sleep; here there is essentially none.
   This means no real communication was happening after the sleep ended.

Both observations point to the same conclusion: these "successes" were
not the result of a real, completed Modbus exchange. Something was
returning successfully without ever actually talking to the device.

## 3. Root cause

`_async_update_data()` has two branches that can return a cached snapshot
without raising an exception:

**Step 4 — "everything is still within its cache TTL":**
```python
if not stale_names:
    ...
    return {n: v for n in all_names if (v := self.cache.get(n)) is not None}
```

**Step 5 — "priority filtering during back-off emptied the read set":**
```python
priority_names = []
for n in stale_names:
    tier = self.cache.tier_of(n)
    if tier == RegisterTier.FAST:
        priority_names.append(n)
    elif tier == RegisterTier.NORMAL:
        if self._backoff_cycle % BACKOFF_NORMAL_DIVISOR == 0:
            priority_names.append(n)
    # SLOW and STATIC are skipped entirely during back-off
stale_names = priority_names
...
if not stale_names:
    return {n: v for n in all_names if (v := self.cache.get(n)) is not None}
```

Both are reasonable-looking optimisations on their own — don't poll the
bus if nothing needs it. The defect is in what they interact with: Home
Assistant's `DataUpdateCoordinator` considers any call that doesn't raise
an exception a success, and logs it as such. But the actual reset of
back-off state —

```python
# ── 10. Success path ──────────────────────────────────────────────────
...
self._consecutive_timeouts = 0
self._consecutive_failures = 0
self._backoff_cycle = 0
```

— sits much further down, reachable only after a real `_execute_batch()`
call completes. Both early-return branches skip it entirely. A coordinator
that has entered back-off (`_consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS`)
can therefore hit either branch every single cycle, indefinitely, without
ever attempting a genuine Modbus exchange — meaning it can never observe
that the device has actually recovered. Since back-off deliberately defers
NORMAL/SLOW/STATIC-tier registers (by design, to reduce load while
genuinely struggling), a coordinator wedged this way stops reading
essentially everything except a handful of FAST-tier registers, forever —
exactly matching the field screenshots.

## 4. Why this is distinct from anything fixed earlier this session

Defects G, H, J2, K, M, and N all concerned code that could **block** a
caller or **fail to bound** a slow operation. This is different: nothing
here blocks or hangs. The coordinator returns promptly, every time, and
reports success. The defect is that "returned without raising" was treated
as equivalent to "the connection is healthy," when for two specific code
paths it wasn't — the function actively chose not to test the connection
at all.

## 5. The fix

New `_pick_backoff_canary(candidates, cache)`:

```python
def _pick_backoff_canary(candidates, cache):
    for n in candidates:
        if cache.tier_of(n) == RegisterTier.FAST:
            return n
    return candidates[0] if candidates else None
```

`in_backoff` is now computed once, immediately after the cache filter, so
both early-return sites can share the same check. At step 4, if
`in_backoff` and nothing is stale, a canary is drawn from `all_names` and
substituted in, so the function falls through to a real attempt instead of
returning immediately. At step 5, if priority-filtering empties
`priority_names`, the same substitution happens, again drawn from
`all_names` (not the already-exhausted `stale_names`) so the pick still
prefers a cheap FAST-tier register over an arbitrary SLOW/STATIC one.

**What happens next is exactly the existing machinery, unchanged:** if the
canary read succeeds, execution reaches the real success path and
back-off state resets correctly — for the first time, an actual test of
recovery. If it fails, the existing exception handling applies exactly as
it already does for any other timeout — back-off correctly continues,
because the device genuinely has not recovered. Nothing about the
failure-handling path changed at all; only the two paths that were
previously skipping the test entirely.

### 5.1 A mistake caught before shipping

An earlier draft of the step-5 fallback wrote
`_pick_backoff_canary(stale_names, cache) or _pick_backoff_canary(all_names, cache)`,
intending "prefer a FAST-tier pick from the stale set, else fall back to
the wider set." This was wrong: by construction, `stale_names` at that
point contains no FAST-tier register (any FAST-tier stale register would
already be in `priority_names`, so `priority_names` wouldn't be empty) —
meaning the first call's own internal fallback (`candidates[0]`) would
almost always fire instead, silently picking an arbitrary SLOW/STATIC
register from `stale_names` and defeating the "prefer cheap FAST-tier"
intent, with the second call never running at all (`or` short-circuits on
a truthy result). Caught on review before any test was written against
it, and corrected to draw directly from `all_names` in both places.
Recorded here per this project's standing practice of surfacing mistakes
rather than folding them into a clean final diff.

## 6. Scope check

`HuaweiSolarOptimizerUpdateCoordinator` (a sibling class, not a subclass)
has its own, separate `_async_update_data()` and was checked for the same
pattern. It does not have one: its back-off handling only delays the
single real read it always performs —

```python
if self._consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
    wait = _backoff_seconds(...)
    await asyncio.sleep(wait)
# ... always proceeds to a real read below, no early-return exists here
```

— so it was never exposed to this defect. No change needed there.

## 7. Adversarial verification

New `tests/test_backoff_canary.py`: following this project's established
trade-off for files too heavy to import directly (`update_coordinator.py`
pulls in a large part of Home Assistant and the device layer — see
`test_deferred_first_poll.py`'s precedent), the exact branching logic
(steps 3-5) is reproduced in an isolated mini-coordinator, in both its old
and new form:

- **Adversarial, both hazards:** the old pattern is shown to wedge for all
  20 simulated cycles when nothing is stale, and separately when
  priority-filtering empties the read set to nothing — proving the hazard
  is real and reproducible, not theoretical.
- **Fix confirmed, both hazards:** the new pattern forces a real attempt
  in both scenarios.
- **Recovery confirmed:** after a successful canary, back-off state
  resets to zero and correctly stays reset on the next cycle (does not
  silently re-enter back-off).
- **No regression, explicitly checked:** when the device genuinely is
  still failing, the new pattern keeps testing every cycle and correctly
  remains in back-off — the fix does not make the coordinator give up or
  stop retrying, only ensures it keeps genuinely trying.
- **Static (AST):** confirms `_pick_backoff_canary` exists and is called
  at least twice inside `HuaweiSolarUpdateCoordinator._async_update_data`
  specifically (disambiguated from the sibling optimizer coordinator's
  identically-named method by return type annotation). Run against the
  pristine pre-session baseline, both fail correctly.

## 8. Safety properties

- No change to the real failure-handling path, `_execute_batch()`,
  `ModbusGuard`, the adaptive controller, or any other coordinator.
  `modbus_guard.py`, `adaptive_modbus.py`, `register_cache.py`, and
  `HuaweiSolarOptimizerUpdateCoordinator` are byte-identical to the
  audited v1.3.16 tree.
- Defects F through S (v1.3.7-v1.3.16) are untouched and still in place.
- Zero behavioural change outside back-off: `in_backoff` being computed
  earlier doesn't change its value or timing for a coordinator that isn't
  in back-off; the `else: self._backoff_cycle = 0` branch is unchanged.
- The canary read is cheap by design (prefers a single FAST-tier
  register) and only ever fires when back-off would otherwise skip
  everything — no additional load in the normal, non-degraded case.

## 9. Test evidence

- **555 passed, 1 skipped, 0 failed**, deterministic across 3 repeated
  runs (was 547; 8 new tests).
- Adversarial: both static checks fail against the pristine pre-session
  baseline; pass against this release.
- Static: `py_compile` clean; manifest version = 1.3.17.
- Confidentiality sweep: clean.
- Diffed against the v1.3.16 tree to confirm only `update_coordinator.py`
  changed.

## 10. Recommended deployment procedure

1. Delete `/config/custom_components/huawei_solar` entirely.
2. Extract v1.3.17 fresh into `custom_components/`.
3. Restart Home Assistant once.
4. **Required validation, specific to this release:** if a coordinator
   enters back-off again under real conditions, confirm (with debug
   logging) that it eventually shows a genuine `"communication restored
   (after N timeout(s) / M failure(s))"` message and its back-off cycle
   counter resets to a low number afterward, rather than climbing
   indefinitely. This is the direct test of Defect T — the previous
   symptom (most entities permanently `unknown` while the log shows
   endless rising back-off cycles with no visible failures) should not
   recur.

**Verdict:** release-ready. A real, field-confirmed defect in the
back-off/recovery state machine — not a hang, not a timeout, but a
logical hole that let the coordinator report success while quietly never
testing whether the thing it was reporting success about was actually
true. Traced to its exact mechanism from two field debug captures, fixed
at the root, and verified adversarially in both directions (the hazard is
real; the fix doesn't overcorrect into masking genuine ongoing failures).
