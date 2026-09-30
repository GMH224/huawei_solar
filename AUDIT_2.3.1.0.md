# AUDIT — huawei_solar v2.3.1.0

**Status: IMPLEMENTATION COMPLETE.** This is the bus-side release ("stage 2")
on top of 2.3.0.2. It implements the five agreed items:

1. slow-path load;
2. one retry layer;
3. protect power readings;
4. dawn peer wake;
5. external-audit fixes F-04, F-05 and F-06.

Items 1–4 are each behind an option that defaults to ON, so a regression can
be isolated in the field without a new build. Item 5 consists of plain bug
fixes.

**Evidence base.** The design rests on four sources:

- the 2.3.0.1 capture (18.5 h);
- the 2.3.0.2 capture (49.7 h, 28,400 bus records);
- the Home Assistant log of the second capture window;
- the diagnostics dump.

Every number below was recomputed from these files.

**User constraints:**

- no added Modbus load;
- no additional storage (EEPROM) writes;
- ICS/OT discipline: code carefully, test, document, audit.

---

## 1. Summary of the mechanism (from the captures)

1. The inverter serves a few address regions through a slow internal path,
   about 1–3 s per read on every read. Its neighbours answer in about 5 ms.
2. The integration merged cheap registers with those regions, following the
   vendor library's own grouping rule (gap < 16). The worst case is the
   unmapped addresses between each battery pack's `state_of_capacity` and
   `charge_discharge_power`. Reading them is pure cost: nothing lives there.
3. These reads are where the trouble starts. Reads that touch a slow region
   fail **28× as often** as other reads (3.69 % vs 0.13 %).
4. When such a read is slow, the vendor library re-sends it after 10 s,
   inside our request. The dongle then works on the same request twice. The
   HA log shows **352 late answers** in 50 h: answers that arrived after we
   had given up, a median of 17.6 s after sending.
5. A single busy reply started the 10-minute bus-wide throttle (queue depth
   1). This caused 87 % of the 526 sheds, and those sheds hit power readings
   as well.
6. At dawn, INV1 stayed on 5-minute night polling until about 08:00. INV2
   had already woken at about 07:40. So two missed polls cost 10 minutes.

---

## 2. Evidence for item 1 (read in full before §3)

**2a. Pack gaps (addresses 38230–38232, 38272–38274, 38314–38316).**

| Read | n (both captures) | Service time |
|---|---|---|
| spans a pack gap | 4,769 | all but **1** slow, p50 ≈ 2.1 s |
| SOC / working status alone (same registers, no gap) | 24 | **3.7–8.5 ms** |
| charge/discharge power … total discharge (after the gap) | > 5,000 | p50 ≈ 4.5 ms |

In the 2.3.0.2 capture, pack-gap reads were:

- 73 per hour;
- 243 s/h of bus time, which is **56 % of all bus service time**;
- the source of **117 of the 226** logged errors.

**2b. Power block vs status block (INV main coordinator).**

| Read | n | Service time | Errors |
|---|---|---|---|
| 32064–32087 alone (input/active power, efficiency, temperature) | 2,819 | all fast, p50 4.9 ms | **0** |
| merged with 32088–32105 (device status, fault code, start/stop time) | 1,336 | all slow, p50 2.4 s | 68 |

The finding covers both captures: **every one of the 68 failed power reads
was a read merged with the status block**. No power read made on its own
failed.

**2c. Always-slow registers.** These are slow on every read, alone or merged:

- the state/alarm block 32000–32015;
- the SOH-calibration status registers 37920–37926 (about 2.4 s each).

The configuration coordinator (settings) accounts for about 13 % of bus
time. Each of its 21 registers was read about 3.9 times per hour.

**Deviation from the agreed plan, stated explicitly.** The plan agreed on
30.09 was to *slow down* the battery pack reads (SOC every 10 min, working
status every 30 min). The data above showed a better fix. The pack readings
themselves are cheap; the cost comes from reading the gap next to them.
Avoiding the gap removes the cost with **no loss of freshness** for battery
health or the pack sensors. Pack SOC and working status therefore keep their
cadence. The agreed cadence reduction is kept only where the registers
themselves are slow: SOH calibration and configuration.

---

## 3. Findings and fixes

### HS-2310-001a — slow-region isolation (option `slow_path_isolation`, default ON)

**Change** (`update_coordinator.py`):

- `_SLOW_ADDRESS_RANGES` holds:
  - the fixed regions 32000–32015, 32088–32105 and 37920–37926;
  - the pack gaps, derived at import from the real register map (both
    storage units, all three pack slots).
- `_address_group()` takes the ranges as a parameter. Two registers are
  merged only when both hold:
  - they lie in the same slow region, or both lie outside every region;
  - the addresses between them touch no slow region.

**Properties, each tested:**

- with the option off (empty ranges), the grouping is **exactly** the
  pre-2.3.1.0 rule (300 random register sets compared with an independent
  re-implementation);
- isolation only ever **splits** legacy groups, never merges;
- no register is lost or duplicated;
- gap/span limits hold;
- registers inside a region are still read, as their own exchange;
- the pack gaps contain no register at all.

**Load.** Fewer bus seconds and no new registers. Some logical requests now
take two exchanges instead of one; each is about 5 ms instead of about
2.1 s.

### HS-2310-001b — slow-register cadence (option `slow_register_cadence`, default ON)

**Change** (`register_cache.py`, `update_coordinator.py`, `__init__.py`):
cadence floors on the refresh interval.

- **SOH-calibration status** (pack and unit registers, both storage units):
  refreshed at most hourly.
- **Configuration coordinator:** every register refreshed at most every
  30 min.

**Where floors apply.** Floors only lengthen the time until a **GOOD** value
is re-read. They never apply to:

- an UNCERTAIN entry;
- a BAD entry, including WRITE_PENDING after our own write. A written
  setting is re-read on the next poll, as before;
- STATIC entries.

Negative values are clamped to 0.

**Load.** Fewer reads. Battery health uses SOH-calibration status to detect
BMS calibration, which runs for hours; hourly detection is sufficient.

### HS-2310-002 — one retry layer, one timeout (option `single_retry_layer`, default ON)

**Change** (`bus_policy.apply_single_retry_layer`, applied in
`async_setup_entry` before the first exchange). The runtime client's
`TimeoutAwareSmartTransport` gets a retry strategy that retries **only
after a lost connection**, at most once. This uses tmodbus' own
`_retry_with_new_connection_if_needed`: the request never reached the
device, and the transport reconnects.

- Timeouts, busy replies and device-failure replies now surface immediately
  to the integration, which owns the retry policy.
- One attempt may take up to `MODBUS_RESPONSE_TIMEOUT` = 20 s. It is set on
  the base transport before connect; tmodbus reads it when the protocol is
  created (verified in its source).

**Timeout consistency.** Outer bounds that wrap one attempt on steady-state
paths are now ≥ 20 s + 3 s margin, so the transport, not a wrapper, ends a
slow attempt:

| Bound | Before | After |
|---|---|---|
| per-chunk poll timeout | 15–60 s | ≥ 23 s when the option is on |
| keep-alive probe timeout | 20 s | 25 s |
| `WRITE_TIMEOUT` | 15 s | 25 s |

**Fail-safe.** If the client does not have exactly the expected structure
(a future library version), nothing changes and a warning is logged.

**Writes (important for the EEPROM constraint).** The library used to
re-send a **write** whose answer was merely late: up to 3 attempts. With
this option a write is sent exactly once. The existing read-back
verification decides the outcome.

**Tested against the real tmodbus/tenacity objects:**

- controls showing the library default re-sends on timeout (3 attempts) and
  on busy (2 attempts);
- with the option: timeout → 1 attempt; busy → 1 attempt; lost connection →
  retried once; a second loss gives up; success unchanged;
- fail-safe on foreign objects and on an invalid timeout.

### HS-2310-003 — protect power readings (option `protect_power_reads`, default ON)

**Change 1: priority lane.** A chunk that contains a power-flow register is
admitted on the guard's existing priority lane: never shed, still
serialised and paced.

- The power-flow registers are exactly `input_power`, `active_power`,
  `power_meter_active_power` and `storage_charge_discharge_power`.
- A read that touches a slow region never gets priority, even with
  isolation off.
- Battery **pack** power is deliberately excluded, so that pack traffic can
  still be throttled.
- `MAX_PRIORITY_QUEUE_DEPTH` goes from 2 to 4. On this plant there are six
  priority producers: 2 keep-alives, 2 main coordinators, the meter and the
  battery power-flow read.
- The unchanged 20 %/10 s airtime budget and the 10 s admission wait still
  bound the lane.

**Change 2: transition trigger.** A busy reply starts the throttle only when
it **persisted** through the integration's own busy retries, and then for
`BUSY_TRANSITION_DURATION` = 5 min instead of 10.

- `notify_transition()` takes an optional duration and never *shortens* a
  transition already in force.
- The day↔night transitions keep 10 min.
- With the option off, the old behaviour (first busy → 10 min) is kept.

### HS-2310-004 — dawn peer wake (option `dawn_peer_wake`, default ON)

**Change** (`update_coordinator.py`, `night_mode.py`). When a coordinator
leaves night mode on its **own** evidence, the other coordinators on the same
bus leave night mode too. They then ignore night-entry evidence (PV power
or a standby status) for `PEER_WAKE_HOLD` = 60 min. After the hold the
normal per-device rules apply again.

**Safety properties, each tested:**

- only the same bus is affected;
- a peer-caused wake does not propagate (no cascade);
- night **entry** is never propagated;
- a shut-down coordinator is not woken;
- with the option off, nothing propagates;
- the registry uses weak references and is left on unload.

**Load and measured basis.**

- Load: more polls at dawn, but for cheap power-block reads. Night TTL
  stretching also ends earlier for the woken coordinators, which is the
  intended effect.
- Measured: INV2 woke at 07:39 and 07:46; INV1 only at 08:00 and 08:07. At
  most 15–20 min of dawn is affected; the 07:27 episode on 29.09 lies before
  INV2 woke and is not helped by this item.

### HS-2310-005 — external-audit fixes

**F-04: reconfigure restore** (`config_flow.py`).

- The unload result is no longer ignored.
- An unload performed by the reconfigure step is remembered.
  `ConfigFlow.async_remove()`, which Home Assistant calls whenever a flow
  ends, reloads the entry unless the reconfiguration was committed. The
  commit path reloads the entry itself.
- Before this fix, Reconfigure followed by Cancel left the whole plant
  offline until a manual reload.

**F-05: awaited keep-alive stop** (`modbus_keepalive.py`, `__init__.py`).
The new `ModbusKeepAlive.async_stop(timeout)` cancels the task **and waits**
(bounded, 5 s) until it has exited. Both unload (before the transport
disconnect) and the setup rollback use it.

**F-06: raw `TModbusError` per chunk** (`update_coordinator.py`). The
per-chunk handler now also catches raw `TModbusError`. It is classified
UNCERTAIN, recorded as a miss, and the batch continues with the next chunk.

---

## 4. Load accounting

| Change | Reads | Writes |
|---|---|---|
| 001a isolation | **fewer bus seconds**; no new registers; some reads split into two 5 ms exchanges | none |
| 001b cadence | **fewer** (SOH-calibration hourly, settings every 30 min) | none |
| 002 single retry | **fewer** (no hidden re-sends) | **fewer**: a write is never re-sent |
| 003 protect power | none (order and admission only) | none |
| 004 peer wake | **more at dawn**, cheap power-block reads, ≤ ~20 min/day | none |
| 005 | none | none |

**Estimate from the 2.3.0.2 capture** (438 s/h of bus service time):

| Source | Saving |
|---|---|
| pack gaps | ~243 s/h |
| configuration floor | ~27 s/h |
| SOH floor | ~17 s/h |
| **Total** | **~65 % less bus time** |

The share of errors on reads that 2.3.1.0 no longer makes (pack gaps) or
isolates (power block) is 170 of 226 (~75 %). This is an estimate from the
capture, not a measurement of 2.3.1.0; §7 defines how to measure it.

---

## 5. Observations (not changed)

- **OBS-2310-01 — one-shot bounds shorter than one attempt.** Some one-off,
  user-initiated or setup paths keep bounds below 20 s. There such a bound
  can end an attempt early, exactly as before this release. The paths are:
  - service validation read: 10 s;
  - number static bounds: 5 s;
  - write-permission probe: 5 s, and off by default;
  - switch status poll: its own bound.
- **OBS-2310-02 — optimizer coordinator.** Its poll bound is the adaptive
  timeout (15–60 s) and was not raised. This plant has no optimizers.
- **OBS-2310-03 — library heartbeat.** With installer credentials stored,
  the library runs a 15 s heartbeat write outside the guard. It stops for
  good after its first failure, which in this bus is likely early. Not
  changed; verify with a short debug log if needed.
- **OBS-2310-04 — battery-health capacity reference.** The references
  auto-captured on 29.09 (about 7.9 kWh per pack, 32.8 kWh in total, against
  20.7 kWh rated) look wrong. This is unrelated to Modbus and left for a
  separate investigation.
- **External audit F-01, F-02 (setup), F-03, F-07, F-08, F-09, OR-01.**
  Assessed on 28.09 and accepted as documented. Unchanged here.

---

## 6. Testing

**New tests: `tests/test_ics_2310_fixes.py`, 88 tests.** The real code runs
against the **real** huawei-solar register map and the **real**
tmodbus/tenacity transport objects. The HA-bound files
(`update_coordinator.py`, `config_flow.py`, `adaptive_modbus.py`) have their
pure functions and methods extracted from source and executed; their wiring
is checked with `ast`.

The test groups:

- slow regions;
- isolation grouping, including the equivalence with the legacy rule and
  property tests;
- the field cost model;
- power-flow priority eligibility;
- the guard priority lane (a priority read is admitted while the normal lane
  sheds);
- cadence floors in the real `RegisterCache`;
- options (`BusPolicy`);
- the single retry layer (real transport, with controls);
- timeout consistency;
- `notify_transition` duration semantics;
- chunk-loop wiring;
- the night-detector hold;
- peer wake, using the real `_on_mode_change` / `_wake_bus_peers` /
  `_peer_wake` methods;
- the awaited keep-alive stop (a real task that unwinds, a task that ignores
  cancellation, ordering);
- reconfigure restore (the real `async_remove`);
- options UI and labels;
- version.

**Existing tests changed (5 files), each with a reason in the file:**

- `test_update_coordinator.py`: two tests pin the exact `_address_group(...)`
  call text; it now passes the ranges.
- `test_init_unload.py`: two tests pin `keepalive.stop()`; it is now
  `await keepalive.async_stop(...)`, a stronger guarantee.
- `test_setup_unload_robustness.py`: one test pins the rollback registration;
  it is now the awaited stop.
- Version pins moved to 2.3.1.0 in `test_ics_2301_fixes.py`,
  `test_ics_2302_fixes.py`, `test_ics_audit_2201_fixes.py` and
  `test_tou_period_text.py`.

**Mutation check.** 24 deliberate faults were injected one at a time, and all
24 were caught. Two initially survived (truthiness coercion of options;
cadence not gated by the option); the tests were strengthened, then both
were caught. Sources were restored afterwards.

1. Isolation ignored.
2. Gap check removed.
3. Pack gaps not derived.
4. No priority.
5. A slow read may get priority.
6. The first busy always triggers the transition.
7. The persistent-busy trigger is removed.
8. A transition can be shortened.
9. The chunk timeout is not raised.
10. An extra attempt.
11. Retry on everything.
12. The timeout is not set.
13. Floors ignored.
14. Peer cascade.
15. Hold ignored.
16. Stop not awaited.
17. Reload after commit.
18. Priority depth 2.
19. F-06 reverted.
20. No unregister.
21. Cadence not gated.
22. Label removed.
23. Unload result ignored.
24. Option coerced by truthiness.

**Regression.** Every test file was run per file against 2.3.0.2 in the same
environment. The failing test IDs are **identical**: the same
environment-limited set as before, which needs a Home Assistant runtime.
The only addition is the new file, with 88 passed.

**Single-process run (`cd tests && pytest .`).** This mode was checked too
and is strictly better than 2.3.0.2:

| | 2.3.0.2 | 2.3.1.0 |
|---|---|---|
| failed | 13 | 12 |
| passed | 1,254 | 1,350 |
| skipped | 8 | 1 |
| errors | 11 | 11 |

The new file loads the real library at test time, not at collection time.
As a result, the real-library tests of `test_tier_separation.py` and
`test_modbus_keepalive_registername.py`, which used to be skipped in this
mode, now run and pass.

**Static checks.** `pyflakes` is clean on all production files and the new
test file. All 76 `.py` files parse and all 22 JSON files load.

**Not tested here.** A live Home Assistant 2026.9 runtime and the real
plant.

---

## 7. Acceptance capture (48 h, same procedure as before)

**Pass criteria:**

1. **No read spans a pack gap.** Grep the bus capture for chunks containing
   both `state_of_capacity` and `charge_discharge_power` of the same pack.
   Expected: none.
2. **Bus service time per hour** clearly lower. Expected about −50 to −65 %,
   from 438 s/h.
3. **Power-block reads never merged with the status block,** and power-read
   errors close to 0.
4. **No `unexpected response with Transaction ID` lines** in the HA log,
   or very few.
5. **Sheds of power reads: none.** The bus log shows the priority requests.
   Total sheds should drop with fewer and shorter transitions.
6. **`unknown` episodes on the power sensors:** only during genuine
   multi-minute outages.

**If something regresses,** switch off only the matching option (Configure →
the five new switches) and capture again.

---

## 8. Upgrade notes

- **No migration.** The five new options default to ON; existing options are
  untouched.
- Restart after installing, rather than reloading.
- In the options dialog, the five switches are labelled:
  - *Isolate slow register regions*;
  - *Read slow settings less often*;
  - *Single retry layer*;
  - *Protect power readings*;
  - *Wake all devices on the bus when one wakes up at dawn*.

  Saving the options reloads the integration.
