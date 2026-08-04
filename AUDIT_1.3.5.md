# Release Audit — huawei_solar v1.3.5

**Date:** 2026-08-04 · **Auditor:** Claude (Anthropic)
**Baseline:** v1.3.4, deployed with SLOW-tier coalescing subsequently
**disabled** in the field after an outage (confirmed stable in that
configuration)
**Type:** correctness fix + retirement of a mechanism that caused a
production incident. **Not yet deployed** — built and validated offline per
the operator's explicit instruction to fix and validate before any further
deployment.

---

## 1. Incident this release addresses

v1.3.4 shipped SLOW-tier coalescing, default on. Enabled in the field, it
caused **every battery entity to go unavailable** within hours. The operator
disabled it via the options flow; the integration recovered within minutes.
No rollback deployment was required because the mitigation was a runtime
toggle, not a code change — the one piece of defensive design from v1.3.4
that worked exactly as intended.

## 2. Root-cause finding: the tier-cost model was a confound

### 2.1 What was believed (v1.3.3/v1.3.4)

Field measurement (a 3,400-request capture) showed:

| Chunk contents | Service time |
|---|---|
| FAST/NORMAL only | ~6 ms, independent of size |
| Contains SLOW/STATIC | ~2,900 ms + 377 ms/register |

This was read as: **register tier drives cost.** v1.3.3 separated cheap and
expensive tiers into different requests; v1.3.4 went further and coalesced
the entire expensive cohort into one request to amortise the fixed cost.

### 2.2 What is actually true

A larger capture (**29,000 requests over 4 days**, taken during the
coalescing incident and its recovery, with `regs`/`prio` populated per v1.3.1)
showed the cost step is a function of **register count crossing a threshold
around 7-8**, essentially **independent of tier**:

```
regs=7  (any tier)  : ~7-60 ms
regs=8  (any tier)  : ~85-97% of requests >1000 ms, median 2,800-4,600 ms
```

Retrieved the vendor library's actual constants directly:

```python
MAX_BATCHED_REGISTERS_COUNT = 64   # max address span per physical exchange
MAX_BATCHED_REGISTERS_GAP   = 16   # max address gap between registers
```

`huawei_solar.device.base.batch_update()` silently splits the registers it is
given into multiple **sequential physical Modbus exchanges** whenever they
exceed this span or gap — invisible to the caller, who only ever sees the
sum. Each additional physical exchange costs roughly one further
~2,900-3,000 ms fixed toll.

### 2.3 Direct confirmation against the real register map

A representative main-inverter register set (`input_power`, `active_power`,
`day_active_power_peak`, `efficiency`, `internal_temperature`,
`daily_yield_energy`, `accumulated_yield_energy`, PV strings, `grid_voltage`,
`reactive_power`, `power_factor`, `grid_frequency`) was resolved against the
real `huawei_solar==3.0.5` `REGISTERS` table:

```
pv_01_voltage .. pv_02_current        32016-32019   (4 registers, contiguous)
input_power .. internal_temperature   32064-32087   (9 registers, contiguous)
accumulated_yield_energy .. daily_yield_energy   32106-32115   (2 registers)
```

`accumulated_yield_energy` sits **18 addresses** past the 9-register block —
just past the 16-address gap threshold, forcing a second physical exchange.
This reproduces the field's regs=7-vs-8 threshold structurally, corroborating
rather than exactly matching it (the representative set is one register
larger than the field's exact boundary, because the real per-coordinator
entity list is data-driven — built from which HA entities are enabled — and
is not statically enumerable; see `update_coordinator._collect_register_names`
and the module docstring for `_address_group`).

**This is the direct evidentiary link** between the incident and the fix: not
an inference from data shape, but a computation against the actual register
addresses the integration polls.

### 2.4 Why coalescing was actively harmful, not merely ineffective

Coalescing gathered a coordinator's *entire* SLOW/STATIC cohort into one
request specifically to amortise what was believed to be a per-register-tier
fixed cost. But SLOW/STATIC registers are, by their nature, scattered across
unrelated functional blocks (alarms, device status, daily/lifetime counters,
temperatures). Deliberately gathering all of them **maximises address
scatter**, which is precisely what forces the *most* internal physical
sub-exchanges. The fix designed to reduce cost mechanically maximised it.

### 2.5 A plausible secondary consequence: transaction desync

Recorded for context, not as a new claim requiring separate proof: the
integration's own adaptive gap pacing is enforced *between* calls to
`guard.request()`. When the vendor library splits internally, it issues its
second, third, and further physical sub-reads **inside one guard hold, with
no pacing between them** — that logic lives entirely inside a library this
project does not control. This is consistent with (though not proven to be
the sole cause of) the transaction-ID desync symptom investigated earlier
(`modbus_failure.md`): a reply arriving late for an abandoned request,
discarded, corrupting the next exchange.

## 3. Fix

### 3.1 `_address_group()` — reproduces the vendor library's own rule

Groups an address-sorted register list using the identical gap/span rule the
library uses internally (`_ADDRESS_GROUP_MAX_GAP = 16`,
`_ADDRESS_GROUP_MAX_SPAN = 64`), applied to **all** stale registers
regardless of tier, *before* `batch_update()` is called. Each resulting group
is issued as its own `guard.request()` — meaning:

- Groups that fit in one physical exchange stay cheap (~7-60 ms), regardless
  of tier.
- Groups that must split now do so **visibly**, as separate, individually
  paced requests, rather than as an invisible, unpaced internal split.
- A group is never fragmented smaller than the library's own physical
  exchange boundary — the same principle that made splitting expensive
  reads a pessimisation in v1.3.3 is preserved, now derived from the correct
  variable (address, not tier).

### 3.2 `_modbus_span()` replaces `_modbus_address()`

The prior lookup was a best-effort reflection walk over several guessed
attribute paths (`register_definition.register`, `register_definition.address`,
`address`, `value`), returning only a start address and never resolving
register *length*. Address-aware grouping needs the true span of every
register, so this is replaced with a direct lookup against
`huawei_solar.registers.REGISTERS` — the exact table the vendor library
itself uses. Falls back to `(0, 0)` on any lookup failure (unknown register,
future library version) rather than raising; memoised per the existing
`@lru_cache` pattern.

### 3.3 Coalescing and night-deferral removed outright

Not disabled — removed. State, methods, config options, UI strings, and
sensors (`coalesce_events`, `coalesced_registers`) are gone from
`register_cache.py`, `const.py`, `config_flow.py`, `__init__.py`, and
`adaptive_modbus.py`. `filter_stale()` is restored to simple TTL-check logic.
`test_tier_separation.py`'s `TestCoalescingAndNightDeferralAreGone` class
pins their absence structurally, so a future merge or copy-paste cannot
silently reintroduce a mechanism that caused a real outage without a
deliberate, evidenced decision to do so.

### 3.4 Deliberately kept

- **SLOW-tier TTL (900 s, v1.3.3)** — a caching decision, orthogonal to the
  per-request cost model correction. How often slow-changing data needs
  refreshing is a separate question from how expensive any one read is, and
  remains valid under either model.
- **`_chunk_tier()`'s slowest-tier-plus-composition label** — retained as a
  diagnostic field on captured requests. No longer drives chunking decisions,
  but remains informative (e.g., for a future investigation correlating tier
  composition with something else).

## 4. Test evidence

- **467 tests total, 1 skipped, 0 failed**, deterministic across repeated
  runs, including runs that reproduce the exact collection order where a
  cross-test stub collision (see §5) previously caused silent skips.
- `test_tier_separation.py` rewritten in place (same filename — content is
  entirely new, since "tier separation" is the retired concept):
  - `TestAddressGroupAlgorithm` — the grouping rule against a synthetic
    address table: empty input, single register, tightly-packed block stays
    one group, gap-at-boundary splits, gap-just-under stays together,
    span-over-limit splits despite zero internal gaps, the exact scattered
    shape that caused the incident (four groups where coalescing forced one),
    and a check that no register is ever lost or duplicated.
  - `TestRealRegisterMap` — validates against the genuine installed
    `huawei_solar` package (see §5 for the isolation mechanism); skipped,
    not failed, if the library is unavailable, since it is a runtime
    dependency of the integration, not of the test suite.
  - `TestModbusSpanRobustness` — never raises, with or without the real
    library available, including on garbage/unicode input.
  - `TestSlowTierTTL` — the 900 s TTL and its clamping, retained from v1.3.3.
  - `TestCoalescingAndNightDeferralAreGone` — structural regression guard.
- **Adversarial verification:** the new `test_tier_separation.py`, run
  against the pristine v1.3.4 tree, **fails to collect at all**
  (`ValueError: substring not found`, because `_address_group` /
  `_ADDRESS_GROUP_MAX_GAP` do not exist in v1.3.4's source) — the strongest
  possible signal that the function under test is genuinely novel, not a
  cosmetic rename of existing behaviour.
- All production files (`register_cache.py`, `update_coordinator.py`,
  `adaptive_modbus.py`, `__init__.py`, `const.py`, `config_flow.py`)
  confirmed `ast.parse`-clean. `strings.json` / `translations/en.json`
  confirmed valid JSON after the removals.

## 5. Test-infrastructure finding: cross-file stub collision

The initial `TestRealRegisterMap` design used
`try: import huawei_solar; import huawei_solar.registers except ImportError: <fallback>`
to prefer the real library when available. This **failed intermittently**
depending on pytest collection order: other test files in this suite install
their own incomplete `huawei_solar` stubs into `sys.modules`, and
`test_tier_separation.py` sorts alphabetically late, so a fake was often
already cached by the time this file's real-import attempt ran — and the
fake happened to also satisfy the `import huawei_solar.registers` check
(several other stubs fake a `.registers` submodule too), so the "proof it's
real" test passed against an incomplete fake.

**Fixed** with:
1. The top-level module stub reverted to the simple, unconditional form used
   by every other test file in this suite (needed only to load
   `register_cache.py`, which references `RegisterName`/`Result` as type
   placeholders).
2. `TestRealRegisterMap` given explicit `setUpClass`/`tearDownClass` that
   force-purges every `huawei_solar*` entry from `sys.modules`, performs a
   genuinely fresh import of the installed real package, runs its tests,
   then restores exactly what was there before — isolating this one class's
   need for the real library from the rest of the suite's stub pollution,
   without affecting any other test file.

Confirmed working: the full suite run three times shows `467 passed, 1
skipped` deterministically, and `pytest -v` confirms all three
`TestRealRegisterMap` tests genuinely **pass** (not skip) when run as part of
the full suite.

## 6. Process finding: an off-by-one, caught before shipping

Early analysis (verbal, to the operator, and this changelog's first draft)
described the representative register block as "exactly 8 registers." Direct
re-verification against the real `REGISTERS` table found it is **9**:
`input_power` (2 registers), `grid_voltage`, `day_active_power_peak` (2),
`active_power` (2), `reactive_power` (2), `power_factor`, `grid_frequency`,
`efficiency`, `internal_temperature` — nine named registers, addresses
32064-32087.

Every claim referencing this number was corrected: the `_address_group`
docstring in `update_coordinator.py`, the module docstring and test-method
docstring in `test_tier_separation.py`, and the test assertion itself (which
initially asserted `8 in sizes` and would have passed on the *wrong* number
had the test been written to match the recollected figure rather than the
computed one). The test now asserts the exact group-size list `[2, 4, 9]`.

Recorded because it is precisely the class of error this project has been
bitten by before — a plausible, round, unverified number stated with
confidence — caught this time only because the test was written against the
real address table rather than trusting the number that had already been
said aloud.

## 7. Safety properties (re-verified)

- **Read-only:** no register writes introduced. `_address_group` only
  reorders and partitions read requests.
- **No data loss:** grouping changes how registers are batched into
  requests, never which registers are read — every stale register still
  reaches the merged result.
- **Fault isolation (v1.1.7):** unaffected; no changes to setup-path
  isolation.
- **Learning gate (v1.2.2) and class-integrity checks (v1.3.2):** unaffected,
  all pass.
- **Storage:** untouched, no `Store` version change.
- **Bounded resources:** `_address_group` operates on the same bounded
  register-name lists as before; no new unbounded state.

## 8. Residual risk and what remains unvalidated

- **No live field data yet.** This release is built and tested offline
  against a 29,000-request capture and the real register address table; it
  has not run against the actual inverters. The operator's live system
  remains on v1.2.4-with-coalescing-off, which is confirmed stable, and
  should stay there until this release is reviewed and deployed
  deliberately.
- **The representative register set is an approximation.** The real
  per-coordinator entity list is built at runtime from which HA entities are
  enabled (`_collect_register_names`), not statically enumerable — so the
  exact group sizes a real deployment produces will differ from the
  illustrative 15-register set validated here, though the *algorithm* is
  identical to the vendor library's own and is independently verified
  against the synthetic test table.
- **Startup-time investigation remains open**, and is explicitly deferred to
  a separate future piece of work per the operator's own prioritisation this
  session (register-cache persistence across restarts is the leading
  hypothesis, based on the same address-grouping mechanism now understood:
  a cold cache requires every coordinator's full register set on first
  refresh, several of which will now correctly split into paced multi-second
  sequences rather than fast-but-fragile unpaced ones — likely still slow,
  but for a now-understood and address-able reason).

## 9. Recommended validation before deployment

1. Enable `Modbus diagnostic capture` for a full day/night cycle after
   deployment.
2. Confirm `prio` labels on captured requests show genuine variation (not
   uniformly `FAST`, which was the symptom of the v1.3.3-era labelling bug
   and would indicate this release's chunking logic is not taking effect).
3. Confirm `last_chunk_count` trends downward relative to the pre-v1.3.5
   baseline for coordinators with large register sets
   (`data_update_coordinator`, `battery_data_update_coordinator`).
4. Watch `bus_total_wait_s` — if per-group pacing meaningfully changes
   queueing behaviour, it should be visible here.
5. A night-inclusive capture remains the one genuinely missing dataset
   across this entire investigation; if this deployment produces one, it
   would finally answer whether expensive-register cost is constant around
   the clock.

**Verdict:** ready for operator review and staged deployment. The mechanism
believed to explain the cost/tier correlation has been shown to be a
confound and replaced with one confirmed directly against the vendor
library's own source and the real register address map; the release that
caused the incident is retired outright rather than merely disabled; and the
fix is validated offline against real field data before touching production,
per explicit instruction following the outage.
