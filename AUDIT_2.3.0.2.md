# AUDIT — huawei_solar v2.3.0.2

**Status: IMPLEMENTATION COMPLETE.** A fix release on top of 2.3.0.1
("stage 1"). It changes how the integration *interprets* a missed refresh.
It adds no Modbus reads or writes. Pacing, queue depth, back-off,
busy-retry and the adaptive transition mode are unchanged.

**Origin.** Sensors sometimes showed `unknown` for about 5 minutes (once
10 minutes). A template sensor that adds both inverters' outputs made the
gaps visible. A one-day diagnostic capture (bus + telemetry, 26.09 22:56 to
27.09 17:26, two cascaded SUN2000 on one SDongle, LUNA2000, power meter)
and the matching Home Assistant entity history were analysed. All five
`unknown` episodes inside the capture window were traced request by
request.

**User constraints.** Do not add Modbus load. Treat the change as ICS/OT:
code carefully, document, audit, test.

---

## 1. Root cause (from the capture)

| Step | Evidence |
|---|---|
| 1. The dongle answers some requests with busy or a timeout, mostly on battery per-pack registers. | 64 errors in 11,000 bus records; 63 of 64 recover within 2 polls. |
| 2. A busy response starts the adaptive "transition" mode for 10 minutes. The effective bus queue depth becomes 1. | Queue depth 1 in every transition episode. |
| 3. With depth 1 the guard sheds the integration's own requests. | 140 of 154 sheds occur inside transition episodes. |
| 4. A shed chunk marks its registers UNCERTAIN. | `record_attempt(..., SHED)`. |
| 5. At night the poll interval is 300 s, the same as the 300 s ceiling. A value is therefore already at the ceiling when its next refresh is due. One shed → age 301 s → BAD/EXPIRED → entity `unknown` until the next good poll. | 4 of 5 episodes: 301 s, 301 s, 301 s, 601 s ages at the moment of blanking. |
| 6. Amplifier: after any failed poll, the next successful poll called `cache.invalidate_all()`. Every value became UNCERTAIN, so old-but-valid values expired at once, and the following poll re-read everything. | Section 3. |

All 5 episodes fall inside transition episodes and follow a shed. The
chance of that pattern occurring at random is about 0.04 %.

The network was ruled out earlier (median round trip 6 ms, 2.5 Gbit/s
backbone). The dongle is not expected to be perfect. The aim of this
release is to tolerate an isolated miss without blanking a sensor.

---

## 2. Findings and fixes

### HS-2302-001 — a single missed refresh blanked a sensor (Medium)

**Before.** `RegisterCache._live_quality()` withheld an UNCERTAIN value
as soon as its age exceeded the ceiling (300 s, or 600 s for energy
counters). No other condition applied. At night the value is already
about 300 s old when it is due. One shed or busy poll therefore blanked
the sensor for a full poll interval, even though the value was one poll
old and the next poll succeeded.

**After.** An UNCERTAIN, non-STATIC value becomes BAD/EXPIRED when
**either**

- (a) its age is above the ceiling **and** at least
  `MIN_MISSED_REFRESHES_BEFORE_EXPIRY` (2) consecutive refresh attempts
  for that register have not delivered a value; **or**
- (b) it has been UNCERTAIN continuously for longer than
  `UNCERTAIN_HARD_LIMIT_FACTOR` (3) × its ceiling: 900 s generic, 1800 s
  for energy counters.

(b) is defence in depth. It covers any path that degrades a value without
recording a miss. It is measured from the moment the value became
uncertain, not from its read time. Otherwise a SLOW register that is
legitimately old but GOOD would expire the moment it degrades.

**What counts as a miss.** Any non-GOOD `record_attempt()` counts as a
miss: shed, admission timeout, timeout, busy, link-down, back-off
deferral, or the poll deadline. The coordinator records each register at
most once per poll. The three call sites (per-chunk outcome, deadline
reconciliation of the remainder, back-off deferral) cover disjoint sets,
so the count equals the number of consecutive polls that did not deliver
the register. A successful read resets the count.
`invalidate_all()` is not an attempt and does not count.

**Bound on staleness.** The rule serves a value until the ceiling is
passed **and** two polls have been missed, whichever comes later:

- in the daytime, roughly 60–90 s;
- at night, about 600 s;
- never longer than rule (b).

While a value is served in this state, its `data_quality` attribute is
`uncertain`, with a reason and an age. The value is never presented as
fresh.

**Unchanged.**

- STATIC exemption.
- Longer energy ceiling.
- WRITE_PENDING is BAD at once (see also HS-2302-004).
- An UNCERTAIN value is still returned by `filter_stale()`, so every poll
  keeps trying to read it. Serving a value longer does **not** mean
  reading it less.

**Constants and injection.** New constants live in `const.py`. They are
injected into `RegisterCache` by the coordinator, following the same
dependency-light pattern as the two ceilings. The cache defaults are
pinned to `const.py` by a test. Bad values are clamped to at least 1 miss
and a factor of at least 1.0, so a bad value can never disable expiry.

**Other consumers of cache quality** were checked:

- `battery_health_manager` uses GOOD only, so it is unaffected.
- The synchronized power coordinator's cache shortcut and per-read
  substitution use GOOD only, so they are unaffected.
- Its cache-only mode and the UNCERTAIN fallback after a failed physical
  read follow the same bound as the entities.
- `types.py` (entity attributes) reports the live quality correctly.

### HS-2302-002 — success path invalidated the whole cache (Medium)

**Before.** At the end of every successful poll that followed any timeout,
busy, shed or admission timeout, `_async_update_data()` logged
"communication restored" and called `self.cache.invalidate_all()`.
`_record_shed()` and `_record_admission_timeout()` also increment
`_consecutive_timeouts`, so this fired after every shed. No audit
documents a rationale for this call. AUDIT_2.0.0 describes
`invalidate_all()` only as the reconnect case.

Effects of that call:

1. Every non-STATIC value became UNCERTAIN/LINK_DOWN. Old values (SLOW
   tier, night) immediately exceeded the age-only ceiling and went
   `unknown`, right after communication was restored.
2. The next poll re-read **everything**, because UNCERTAIN means stale.
   See section 3 for the bus cost.
3. For the tick after recovery, `battery_health_manager` (GOOD-only) got
   no values. The synchronized power coordinator's GOOD-only cache
   shortcut also failed, forcing its dedicated physical reads.

**After.** The call is removed. The INFO log and the counter resets stay.
Nothing is lost:

- registers that failed were already recorded individually and are
  re-read;
- every other register was just read, or is GOOD within its own TTL.

Genuine link loss is still handled: `on_connection_lost()`, called by
the keep-alive probe, keeps its `invalidate_all()`.

### HS-2302-003 — strings (Low, cosmetic)

- `options.step.init.data.sync_power_dedicated_reads` had no label in
  `strings.json` or `translations/en.json`, so the options form showed the
  raw key. A label was added.
- 14 translations (`bg`, `ca`, `de`, `es`, `fr`, `hu`, `it`, `nl`, `pl`,
  `pt`, `ro`, `ru`, `sv`, `ur`) lacked the `{skipped_notice}` placeholder
  in `config.step.confirm_setup.description`. The warning about unreachable
  slave IDs never appeared for those users. The placeholder was added at
  the same position as in English.
- A full placeholder audit was run across all keys and all 18 translation
  files. No other mismatches were found. Each file's diff is one line, and
  the original indentation is preserved.

### HS-2302-004 — pre-write value came back after a failed re-read (Medium)

**Found while reviewing HS-2302-001.** V2_ARCHITECTURE_DESIGN.md §6
decides that a register invalidated by our own write is BAD, not
UNCERTAIN. The pre-write value must not be shown, because it reads as
"the command didn't register".

In 2.3.0.1, `record_attempt()` and `invalidate_all()` overwrote
BAD/WRITE_PENDING with UNCERTAIN. The first failed re-read after a write
therefore served the pre-write value again. This was confirmed by running
the real 2.3.0.1 cache: after `invalidate()` + one failed attempt it
returned `UNCERTAIN/TIMEOUT` and the old value.

The age-only rule used to hide this by accident for old configuration
values. The new miss rule (HS-2302-001) would have widened the window.

**Fix.** Both methods now leave BAD/WRITE_PENDING entries unchanged. Misses
are still counted. Only a successful read (`update()`) clears the entry.
The register stays due for a re-read on every poll.

---

## 3. Modbus load accounting

| Change | Reads | Writes |
|---|---|---|
| HS-2302-001 | none. Quality interpretation only; `filter_stale()` still re-attempts every UNCERTAIN register each poll | none |
| HS-2302-002 | **fewer**: removes the full re-read after every recovery | none |
| HS-2302-003 | none (UI strings) | none |
| HS-2302-004 | none. A WRITE_PENDING register was already due every poll | none |

**Size of the removed burst (capture, 18.5 h).** The table compares the
poll after a recovery poll with an ordinary poll:

| Coordinator | Registers, after recovery vs normal | Service time, after vs normal |
|---|---|---|
| INV1 main | 20.6 vs 6.8 | 7.8 s vs 1.0 s |
| INV1 battery | 37.0 vs 13.1 | 19.2 s vs 4.2 s |
| INV1 config | 19.9 vs 7.2 | 12.2 s vs 4.3 s |
| INV2 main | 15.9 vs 4.6 | 9.0 s vs 1.2 s |
| Power meter | 8.0 vs 4.4 | ≈ same |

Totals:

- about 1,950 extra register reads;
- about 1,100 s of extra service time, which is **14 % of all bus service
  time** in the capture (~24 min per day).

This is an upper bound: some of the excess may have other causes. The
mechanism explains it fully, because every invalidated register is stale
by definition.

**Not changed, stated explicitly.**

- `ModbusGuard` pacing and queue depth.
- The adaptive controller, including the 10-minute transition mode and
  its trigger on the first busy response.
- Busy retry.
- Back-off.
- Keep-alive.
- Shed accounting.

Shed counts are therefore expected to stay similar. They may fall
somewhat because the burst is gone, but this release does not claim that.

---

## 4. Expected effect on the five captured episodes

The episodes were replayed through the **real** cache code as anonymised
event sequences: offsets from the last good read, no serials, no
addresses. `tests/test_ics_2302_fixes.py` contains them.

| Episode (local time) | Entity | Observed in HA | 2.3.0.1 code, replayed | 2.3.0.2 |
|---|---|---|---|---|
| 02:07 night | INV1 active power | 306 s | 306 s | **0** |
| 07:39 dawn | INV1 active power | 602 s | 601 s | **301 s** (two sheds in a row) |
| 07:44 dawn | INV1 total yield | 302 s | 301 s | **0** (see note) |
| 08:00 morning | INV1 active power | 308 s | 308 s | **0** |
| 12:32 midday | INV2 active power | 150 s | 151 s | 151 s (unchanged) |
| **Total** | | **1,668 s** | 1,667 s | **452 s** |

The replay of the real 2.3.0.1 code reproduces every observed duration to
within 1 s. This validates both the event sequences and the reference
model the test uses for the old behaviour.

**Note on 07:44.** The result depends on timing. The register (NORMAL tier,
night TTL 300 s) was about 299 s old at the 07:39 shed poll, so it was not
yet due and no miss was recorded. Had it been due, it would still have
blanked for 301 s. A test pins this boundary.

**12:32 is out of reach for stage 1.** It had four shed polls in a row in
the daytime. Only a change to the bus side, reviewing the transition
trigger ("stage 2"), can help there. That change is deliberately not in
this release.

Summary:

- 3 of 5 episodes are removed (one of them depends on timing);
- 1 is halved;
- 1 is unchanged;
- total `unknown` time in the sample is down 73 %.

---

## 5. Observations (not changed)

**OBS-2302-01 — transition mode is bus-wide and long.**
One busy response on battery registers puts the whole bus at queue depth 1
for 10 minutes. That is the source of the sheds. Stage 2 would review
this, only if the post-install capture shows it is still needed.

**OBS-2302-02 — the misses rule does not know the poll interval.**
"Two misses" means about 60 s in the daytime and about 600 s at night.
This is intended: night values change slowly. It is stated here so that
nobody mistakes it for a fixed time bound.

**OBS-2301-01, -02, -03, OBS-230-01** remain as documented in
AUDIT_2.3.0.1.md.

---

## 6. Testing

**New tests.** `tests/test_ics_2302_fixes.py` has 44 tests (plus subtests).
`register_cache.py` is executed for real with a fake monotonic clock.
`update_coordinator.py` is checked structurally (AST), because it needs a
live Home Assistant to import.

Behaviour covered:

- one miss past the ceiling is served, with an adversarial control showing
  the old predicate would withhold it;
- two misses past the ceiling expire;
- two misses within the ceiling are served;
- every non-GOOD reason counts as a miss;
- both `update()` branches reset the count;
- energy counters use their own ceiling;
- STATIC is exempt;
- WRITE_PENDING is BAD at once;
- an absent entry is untouched;
- a GOOD attempt clears the counters;
- a served UNCERTAIN value is still due for a read;
- `invalidate_all()` is not a miss;
- two failed polls after link loss expire the value;
- the hard limit applies with zero and with one recorded miss, is measured
  from degradation and not from read time, and its clock is not restarted
  by repeated degradation;
- parameter clamping;
- HS-2302-004: sticky through 4 miss reasons and through link loss, still
  due for a read, and cleared by a successful read;
- bus effect: with the old success path the next poll re-reads all 10
  registers, with the new one none;
- field replay: the old model matches Home Assistant within 2 s, the new
  results are as in §4, the totals are 1,668 s → 452 s, and 3 episodes
  are eliminated;
- timing-sensitivity boundary and the 12:32 case unchanged;
- structure:
  - no `invalidate_all` in `_async_update_data`;
  - exactly one call site, in `on_connection_lost`;
  - the recovery log and counter resets are kept;
  - the constructor receives both new constants;
  - busy→transition and shed accounting are untouched;
- strings: label present, every option field labelled, no placeholder
  mismatch in any translation, `strings.json` and `en.json` agree;
- version.

**Existing tests changed (2), with reason.**
`test_v2_energy_aware_ceiling.py::test_energy_counter_does_expire_past_its_own_longer_ceiling`
and `::test_non_energy_register_uses_the_generic_shorter_ceiling` recorded
one miss and expected expiry. That is exactly the old rule. They now
record two misses, so they keep testing what they are about: which ceiling
applies. The single-miss case is asserted in the new file. Version-pin
assertions were moved to 2.3.0.2 in `test_ics_2301_fixes.py`,
`test_ics_audit_2201_fixes.py` and `test_tou_period_text.py`.

**Mutation check.** 16 deliberate faults were injected one at a time, and
all 16 were caught. Sources were restored afterwards.

1. Age-only rule.
2. Default of 1 miss.
3. No reset in `update()`.
4. `invalidate_all()` counts as a miss.
5. No hard limit.
6. `record_attempt()` does not count.
7. Uncertainty clock restarts on every miss.
8. Hard limit measured from read time.
9. Success-path `invalidate_all()` restored.
10. Constructor parameter dropped.
11. No clamp.
12. German placeholder dropped.
13. GOOD attempt does not clear.
14. `record_attempt()` overwrites WRITE_PENDING.
15. `invalidate_all()` overwrites WRITE_PENDING.
16. Option label removed.

**Adversarial runs against 2.3.0.1.**

- The five episodes replayed through the real 2.3.0.1 `RegisterCache` with
  its success-path `invalidate_all()` give 306 / 601 / 301 / 308 / 151 s,
  matching Home Assistant's history.
- The HS-2302-004 defect was reproduced on the real 2.3.0.1 cache.
- The new test file fails in 58 places on 2.3.0.1.

**Regression.** Every test file was run per file against 2.3.0.1 in the
same environment (Python 3.12, pytest 9, `voluptuous`, `huawei-solar`
3.0.7). The set of failing test IDs is **identical**: the same
environment-limited set documented in AUDIT_2.3.0.0.md (13 failures and
5 collection errors, which need a Home Assistant runtime). The pass count
went from 1,319 to 1,359; the only new file is `test_ics_2302_fixes.py`.
All `.py` files parse, all 22 JSON files load, and `pyflakes` reports
nothing on the changed files.

**Not tested here.** A live Home Assistant runtime and real hardware.

---

## 7. Acceptance check after installing

Run the same one-day diagnostic capture as before, including a night.

1. **`unknown` episodes** on the power and yield sensors should drop
   sharply. Isolated single sheds at night should no longer blank
   anything. Remaining episodes should coincide with two or more shed
   polls in a row.
2. **Shed and busy counts** are expected to stay roughly the same
   (~150/day). This release does not change the bus side.
3. **Post-recovery polls** should look like ordinary polls. The
   register count and service time after a "communication restored" log
   line should no longer jump.
4. **Decision for stage 2** (transition-mode trigger): only if (1) still
   shows daytime blanks like 12:32, or if the shed count itself becomes a
   concern.

---

## 8. Upgrade notes

- **No migration and no new options.** The only UI change is the new
  label.
- A restart is recommended, as usual. A reload is also fine.
- Behaviour visible to users: during an isolated missed refresh, a sensor
  keeps its last value (`data_quality: uncertain`) instead of going
  `unknown`. Sustained failures still end in `unknown`, as before.
- After a write, if the confirming re-read fails, the entity stays
  unavailable until the re-read succeeds. Before, the pre-write value came
  back.
