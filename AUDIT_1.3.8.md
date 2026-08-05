# Release Audit — huawei_solar v1.3.8

**Date:** 2026-08-05 · **Auditor:** Claude (Anthropic)
**Baseline:** v1.3.7 (not yet deployed — superseded by this release; see §8)
**Type:** defect fix, two production files changed (`__init__.py`,
`update_coordinator.py`), plus one new test file.

---

## 1. The report, precisely stated

The operator clarified a symptom that had been misattributed in the v1.3.7
discussion: Home Assistant's own generic "please wait, `huawei_solar` is
still starting up" notification — the one shown for *every* integration
during boot, normally for on the order of 20 seconds — was taking **2-3
minutes** for this integration specifically. A reload of the config entry
takes a similarly long time. This is a distinct symptom from the
learning-gate's deliberate ~180 s window (see §2) and from v1.3.7's
double-unsub defect, though — as this audit sets out — it is mechanistically
connected to the same incident v1.3.7 addressed.

## 2. Ruling out the wrong culprit first

Before investigating, the obvious-but-wrong candidate was checked and
excluded: `AdaptiveModbusController`'s learning-gate suppression window
(`ADAPTIVE_POLL_MAX = 180s`, v1.2.2) is a real ~3-minute-scale window, but
it does not block Home Assistant's setup notification. Its own docstring is
explicit: *"Nothing stops POLLING here — only learning from what is
observed."* Coordinators continue their normal, non-blocking, scheduled
polling throughout that window; nothing about it holds `async_setup_entry`
open. Home Assistant's "still starting up" banner is shown specifically
while a config entry's `async_setup_entry()` coroutine has not yet
returned — a different mechanism entirely, and the one actually
investigated here.

## 3. Root cause — two blocking first-refresh calls on the setup path

Every `await` reachable from `async_setup_entry` was walked directly rather
than guessed at. Two calls stood out as genuine, first-class Modbus round
trips awaited **inline**, before setup can proceed to the next step:

### 3.1 `create_optimizer_update_coordinator()` — `update_coordinator.py`

```python
coordinator = HuaweiSolarOptimizerUpdateCoordinator(...)
await coordinator.async_config_entry_first_refresh()
return coordinator
```

Called once per inverter that has optimizers (`_setup_inverter_device_data`,
`__init__.py`), immediately after `device.get_optimizer_system_information_data()`
(a single vendor-library file-read call — checked, and not itself a
per-optimizer loop, so not implicated here). `async_config_entry_first_refresh()`
performs the coordinator's real first poll: actual register reads for every
detected optimizer, awaited synchronously.

### 3.2 The `SynchronizedPowerCoordinator` block — `__init__.py`

```python
sync_coordinator = SynchronizedPowerCoordinator(...)
...
await sync_coordinator.async_config_entry_first_refresh()
```

Runs once per config entry that has a meter, a battery, or a second
(daisy-chained) inverter — i.e. the common case for any non-trivial
installation. This reads every instantaneous power register across up to
two inverters plus meter/battery in one coordinated pass, awaited
synchronously before `entry.runtime_data` is even assigned.

### 3.3 Why these two, and not others

Every other coordinator in the codebase (`HuaweiSolarUpdateCoordinator` for
main/battery/power-meter/config data) is constructed in `__init__.py`/
`update_coordinator.py` **without** an explicit first-refresh call anywhere
in setup — confirmed by grep across every `.py` file for
`async_config_entry_first_refresh` and `async_refresh(`: the only other
matches are inside `services.py`'s user-invoked service handlers (expected —
a service call is supposed to block until its own explicit refresh
completes) and the two sites above. These two coordinators were, structurally,
the exception to an otherwise-consistent pattern, not an isolated one-off.

### 3.4 Why this adds up to minutes, not seconds

Both blocking calls are genuine Modbus exchanges, not cache hits — cold on
first boot, cold again on every reload. Stacked with sequential (not
parallel) per-device setup for daisy-chained inverters, and each device's
own optimizer-coordinator refresh running before the entry-level
synchronized refresh even starts, the total is naturally additive across
however many optimizers, batteries, and daisy-chained devices an
installation has. This also explains why reload costs the same 2-3 minutes
as a cold boot: nothing here is cached across a reload; both calls perform
a completely fresh read every time `async_setup_entry` runs.

## 4. Connection to the v1.3.7 (Defect F) incident — strengthened, not replaced

`configuration_update_coordinator` is constructed *after* the optimizer
setup block within the same per-device function
(`_setup_inverter_device_data`). If a slow setup were ever cancelled
part-way through — by Home Assistant itself or a supervising timeout, while
still blocked on an earlier device's optimizer first-refresh — whatever
code had not yet run, including a later device's configuration coordinator
construction, would simply never execute. Confirmed from the field capture
(see `AUDIT_1.3.7.md` §2): the affected coordinator
(`devdc46_config_data_update_coordinator`) produced zero requests for the
full length of a subsequent 9.25 h capture, having polled normally
beforehand, with its last recorded request timestamped within minutes of
the incident's onset — exactly the signature a mid-setup cancellation would
leave behind.

**This does not retract v1.3.7.** The double-unsub listener defect fixed
there is real, independently reproduced, and stays fixed. What this release
adds is a second, plausible, and arguably more direct mechanism for the
*same* incident — a multi-minute blocking setup sequence is a much larger
target for an external cancellation to land inside than a single listener-
unload exception is. Both are true; both are now fixed; §6 states the
combined evidentiary limit honestly.

## 5. The fix

Both first-refresh calls now run as background tasks instead of being
awaited inline, using the identical pattern already established in this
codebase for exactly this situation — v1.1.7's battery-health
initialisation (`_async_setup_battery_health`):
`entry.async_create_background_task(...)`, falling back to
`hass.async_create_task(...)` on older Home Assistant cores that lack the
former. `create_optimizer_update_coordinator()` gained an `entry` parameter
(threaded through from its one call site) to support this.

Entities fed by these two coordinators now show unavailable until their
background refresh completes — identical to how every other coordinator in
this integration already behaves without complaint (see §3.3). No new
behaviour is introduced; two exceptions are brought into line with the
existing, working pattern.

### 5.1 Trade-off, stated plainly

Both coordinators lose `ConfigEntryNotReady` propagation specifically on a
**first-attempt** failure — such a failure now behaves like any later
transient failure (handled by the coordinator's own retry/backoff) rather
than failing the whole entry setup. Given the primary device connection has
already succeeded earlier in `async_setup_entry` by the time either factory
runs, a first-attempt failure here is judged far more likely to be "this
specific read timed out" than "the device is unreachable" — the same
judgement already made, and accepted, for battery-health since v1.1.7.

## 6. Limits of this verification — read before treating either incident as closed

- **The causal link in §4 is the most complete account assembled from
  available evidence, not an independently proven mechanism.** No
  traceback from the actual 2026-08-04 incident was captured (flagged
  already in the handoff and in `AUDIT_1.3.7.md` §6). This audit's
  contribution is a second, code-confirmed, plausible mechanism — not a
  replacement for having caught the actual failure in the act.
- **Whether Home Assistant (or some other layer) actually imposes a setup
  timeout capable of cancelling a 2-3 minute `async_setup_entry` call has
  not been independently confirmed against this specific Home Assistant
  version.** The reasoning in §4 assumes such a mechanism exists and is
  reachable in this deployment; it has not been proven to be the actual
  trigger on 2026-08-04.
- Fixing the two blocking calls in this release should shrink the "still
  starting up" window toward the ~20 s baseline typical of other
  integrations, but the exact new figure has not been measured yet — see
  §9 for what to check.

## 7. Safety properties

- **No behavioural change to Modbus chunking, adaptive control logic, or
  battery-health logic.** `adaptive_modbus.py`, `battery_health.py`,
  `battery_health_manager.py`, `register_cache.py`, `modbus_guard.py` are
  byte-identical to the audited v1.3.7 tree.
- **No storage/`Store` version change.**
- **v1.3.7's Defect F fix is untouched and still in place** — this release
  builds on it, not around it.
- The isolation contract this fix extends (v1.1.7: nothing optional should
  be able to block or fail core entry setup) is reinforced, not weakened,
  by bringing these two coordinators in line with it.

## 8. Relationship to v1.3.7

v1.3.7 was built, tested (473 passed, 1 skipped), and packaged in this same
session but **not yet deployed** to the operator's system before this
second, related defect was identified. Rather than ship two zips for two
undeployed versions, this release supersedes v1.3.7 directly — it contains
the v1.3.7 fix unchanged, plus this one. The version history remains
accurate and sequential (v1.3.7's audit and changelog entry stay in place
for the record); the operator deploys v1.3.8 only.

## 9. Test evidence

- **475 passed, 1 skipped, 0 failed**, deterministic across 3 repeated runs
  (was 473; 2 new tests in `test_setup_critical_path.py`).
- Adversarial: both new tests, run against the pre-fix files, fail and
  correctly report the exact original line numbers (1013 in
  `update_coordinator.py`; 217 in `__init__.py`). Run against this release,
  both pass.
- Static: all Python files parse (`py_compile` clean on both changed
  files); manifest version = 1.3.8.
- Confidentiality sweep: clean.
- Diffed against the v1.3.7 tree to confirm only `__init__.py` and
  `update_coordinator.py` changed among production files.

## 10. Recommended deployment procedure

1. **Delete** `/config/custom_components/huawei_solar` entirely.
2. Extract v1.3.8 fresh into `custom_components/`.
3. Restart Home Assistant once, to establish a clean baseline.
4. **Required validation, specific to this release:**
   - Time how long the "waiting for Huawei Solar to start up" notification
     is visible on this boot. It should be markedly shorter than the
     previously observed 2-3 minutes — record the actual figure.
   - Confirm the optimizer and synchronized-power-flow entities (if
     present on this installation) briefly show unavailable, then populate
     within roughly one poll cycle — this is the expected, accepted
     trade-off from §5.1, not a regression.
5. **Required validation carried over from v1.3.7 (still outstanding):**
   trigger a plain *reload* of the config entry (not a restart) and confirm
   the previously-affected coordinator's entities update and the "unknown
   job listener" error does not reappear in the log.
6. If steps 4 and 5 both hold, this closes the reload/coordinator-dies
   incident with two independently-verified contributing fixes. If either
   does not, keep the underlying incident (§2.2 of the 2026-08-04 handoff)
   open rather than assuming this release resolved it — per §6 above.

**Verdict:** release-ready, on the specific and limited claim in §6 — two
real, adversarially-verified defects are fixed, together forming the most
complete account of the 2026-08-04 incident assembled so far, but the
validation in §10.4-10.5 is the actual test of whether it is fully closed,
and has not yet been performed against a live system.
