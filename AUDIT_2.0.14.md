# huawei_solar 2.0.14 — Release Audit

**Scope:** Battery Pack Physical Grouping — Phase A+B of an uploaded architecture study, implemented together in one pass based on a retroactive analysis of real field data that independently confirmed the study's own core hypothesis before any new code was written.

**Discipline applied throughout, unchanged from every prior release:** verify every claim against real data and real source before writing a fix; adversarial test proving the specific behavior each addition provides; full suite re-run after every change; final verification from a fresh, independent extraction of the packaged zip.

**Final verification:** 1,262 passed, 1 skipped — confirmed identically from a fresh, independent extraction of `huawei_solar-2.0.14.zip`, matching the established pre-existing baseline (5 failed / 12 errored, documented since 2.0.7) with zero new regressions, and exactly 18 more passing tests than the 2.0.13 baseline (1,244), matching the 18 new tests this release adds.

---

## Background — an uploaded study, verified before acting on it

An uploaded architecture document ("Architecture Recommendation — Battery Pack Physical Grouping and Polling Model") proposed splitting battery-pack Modbus reads into three protected physical groups — DYNAMIC (working status, SOC, power, voltage, current), ENERGY (total charge/discharge counters), DIAGNOSTIC (SOH/calibration, temperature, serial) — based on field evidence that battery transactions disproportionately drive the long service-time tail already observed this session.

Every checkable claim in that document was verified independently before treating it as reliable:
- **Field-evidence numbers**: transaction count, duration, error rate, queue depth range, max service time all reproduced exactly against the actual 2.0.13 capture the document itself used.
- **Tier-classification table**: every register's claimed current tier (FAST/NORMAL/SLOW/STATIC) confirmed exactly against `register_cache.py`'s own `_classify()`, run directly.

## Retroactive classification — de-risking the decision before writing code

Rather than deploying an observation-only build first (the study's own proposed Phase A), a retroactive classification was run directly against real telemetry already in hand — including a 20.5-hour capture from earlier in this project's history, predating this session's own Phase 5.3 chunking work. That capture's own class-by-class distribution was checked in isolation first and found consistent with the newer captures before being pooled in, confirming the underlying address-grouping behavior being measured has been stable across the codebase's own history.

Pooling four captures (~31 hours combined, 4,922 real battery-pack transactions):

| Class | n | Median service time | Error rate |
|---|---|---|---|
| MIXED(DYNAMIC+ENERGY) — today's default | 3,121 | 2,291ms | 1.99% |
| DYNAMIC alone | 1,453 | 5ms | 0.34% |
| DIAGNOSTIC alone | 326 | 8ms | 0.61% |

A 458x difference in median service time and a 5.9x difference in error rate between transactions that happen to mix DYNAMIC and ENERGY registers today versus ones that don't — occurring naturally, since nothing in 2.0.13 currently prevents the mix. This is not a controlled experiment (documented explicitly as a limitation, both in the supporting analysis and here), but a large, consistent effect visible in real, already-captured data materially de-risks the decision to implement physical separation, without needing a "wait and observe" deployment cycle first.

## What was implemented

**`physical_group_for(register_name) -> str | None`** (`update_coordinator.py`) — derives a protected physical-group id for a battery-pack register from its own name, parsing the existing `storage_unit_{unit}_battery_pack_{pack}_{suffix}` construction (matching `battery_health_manager.py`'s own `_pack_register_name()` format) rather than hand-maintaining a second address list that could drift from the real topology — the same convention already established this session for `SLOT_LABEL_RE`. Returns `None` (no protection, falls through to ordinary address grouping unchanged) for every non-battery register and any battery-pack register whose own suffix isn't in one of the three known category sets — deliberately conservative rather than guessing at an unrecognized suffix's category.

**`_split_by_physical_group()`** — partitions an address-sorted register list into address-order-preserving runs sharing the same protected group id, before `_address_group()` ever sees the list. A simple, order-preserving partition is correct here specifically because registers sharing one protected group are already naturally address-adjacent (confirmed directly against the real register address map in the study document) — two runs of the same group id never need to be merged back together.

**Wired into `_execute_batch()`**: `_address_group()` now runs once per protected-group run, rather than once for the whole register list — the existing gap/span rule, service-time-aware chunking, and every downstream mechanism (guard, adaptive pacing, physical-attempt telemetry) are completely unchanged; only where a run boundary gets drawn *before* that rule ever runs is different. For every register outside the three protected categories, this is a provable no-op — `_split_by_physical_group()` returns exactly the single run `_address_group()` would have received directly.

**A genuine, distinct mechanism from DEFECT E's own earlier finding** (an existing code comment, predating this release): DEFECT E is about exchange *count* — a group forced into 2+ physical exchanges costs a fixed toll per exchange, independent of content. The DYNAMIC+ENERGY registers this release separates are address-adjacent enough to already fit in a *single* exchange under the existing rule — the cost this release addresses tracks specific register content within one exchange, not exchange count. Confirmed directly by computing the real address span (13 registers, well under the 64-register cap) before concluding the two mechanisms are additive, not overlapping.

## What was deliberately NOT changed

No register's own tier or freshness cadence changed in this release. `charge_discharge_power` stays FAST; `total_charge`/`total_discharge` stay NORMAL. This is the study's own explicit Phase C, held back specifically so a future capture can isolate whether physical separation *alone* already improves the BUSY/error rate, before cadence is also changed — the same "one variable at a time" discipline this project has followed for every release this session.

No telemetry schema change was made. `regs_l` (the full register list per recorded chunk) already flows through unchanged regardless of how chunks were formed, so it will automatically reflect the new, smaller protected groups in the next real capture — sufficient to verify the fix's actual effect using the same retroactive-classification approach used to justify building it, without adding a new field that would only duplicate what's already derivable.

## Testing

18 new tests, using a hybrid approach chosen to match this project's own established conventions: `test_update_coordinator.py` has always tested this specific file via source-level checks (its surrounding coordinator class has a heavy HA/internal-package import chain unsuited to isolated execution) — but `physical_group_for()` and `_split_by_physical_group()` are genuinely self-contained, stdlib-only functions, so their own source was extracted and `exec()`'d in an isolated namespace for real execution testing, stronger than pattern-matching alone, without needing the full file's import chain.

Coverage includes: every suffix correctly mapped to its category; different packs and different units never sharing a group even for the same category; non-battery and unit-level (non-per-pack) registers correctly falling through unprotected; an unrecognized pack suffix correctly falling through rather than being guessed into a category; malformed register names never raising; the core guarantee directly (`DYNAMIC` and `ENERGY` registers for the same pack always land in separate output runs); order preservation within each run; the no-op guarantee for ordinary registers; and source-level confirmation the split is genuinely wired into the real chunk-building call site with service-time-aware chunking still applied afterward.

One of my own bugs was caught and fixed during this work: assigning the extracted functions directly as class attributes in `setUpClass` caused Python's descriptor protocol to auto-bind `self` as an unwanted first argument when called via `self._split(...)` — caught immediately by all 16 new tests failing with an identical `TypeError`, fixed by wrapping both in `staticmethod()`.

## Final verification

- Every file in the packaged `huawei_solar-2.0.14.zip` compiles cleanly; `strings.json`, `translations/en.json`, and `services.yaml` all validate.
- Full suite, run from a **fresh, independent extraction** of that exact zip: **1,262 passed, 1 skipped**, matching the working tree and the established pre-existing baseline exactly — zero drift, zero new regressions.

## What comes next

This release is deliberately scoped to grouping only. The next deployment's own capture is the actual test of Phase B's hypothesis under real, enforced separation — not just the retroactive prior this release was built on. If the BUSY rate, error rate, and service-time distribution for isolated DYNAMIC transactions in that capture look like the retroactive DYNAMIC-only numbers above, that's the evidence needed to consider Phase C (moving `charge_discharge_power` from FAST to NORMAL) as its own, separately-evaluated change — not before.
