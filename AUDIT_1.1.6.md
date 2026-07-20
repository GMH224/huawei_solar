# Release Audit — huawei_solar v1.1.6 (optimization pass)

**Date:** 2026-07-20 · **Auditor:** Claude (Anthropic) · **Baseline:**
AUDIT_1.1.5.md (all v1.1.5 findings remain valid; this addendum covers only
the delta). Scope: `register_cache.py`, `battery_health.py`,
`battery_health_manager.py`, `manifest.json`, tests, docs.

## 1. What was profiled and why it changed

| Cost | v1.1.5 behaviour | v1.1.6 |
|---|---|---|
| **Counter staleness** (data quality) | `storage_total_charge/discharge` hit the generic `total_` → SLOW tier (5-min TTL): segment/efficiency energy endpoints up to ~0.2 kWh stale ⇒ up to ±20 % error on a minimum-size (2 kWh) segment | Exact-name override → NORMAL (30 s, coordinator cadence). Bus cost ≈ 0: addresses 37780–83 sit in the same contiguous PDU chunk as the SOC/power registers read every poll; the adaptive TTL still stretches to 5 min while idle |
| **Recalibration watch** | `storage_rated_capacity` → STATIC: never re-read in-session and skipped by `invalidate_all()` — the 37758 watch could only fire after an HA restart | Exact-name override → SLOW (5–30 min re-reads, PDU-adjacent) |
| **Per-tick CPU** | Every 30 s tick re-sorted all segments (trimmed mean), re-summed up to ~2 160 stress buckets, recomputed the balance median | Cached aggregations invalidated only on data change; O(1) running stress totals; oldest-first prune fast-paths. **Benchmark: ~13 µs/idle-tick with a full 90-day window (~75 k ticks/s)** |
| **HA recorder churn** | 10 entities wrote (mostly identical) states every 30 s | `HealthReport.signature()` change detection in the manager; entities notified only on real change. Stress index quantized to integer steps in the signature because the rolling-window mixture creeps ~0.01/tick and would defeat detection |

## 2. Correctness safeguards on the optimizations

- **Cache coherence:** the segment-aggregation cache is invalidated on every
  mutating path — segment append, prune removal, discard-counter increment,
  and `restore()`. Callers receive an isolated copy of the attrs dict, so an
  entity mutating attributes cannot poison the cache (test T15).
- **Running totals vs. recompute:** stress totals after mixed feed + prune
  match a full bucket sweep to 9 decimals; totals recomputed on restore;
  zeroed when the window empties so float drift cannot accumulate in an empty
  window (T16).
- **No behavioural drift:** all v1.1.5 engine tests (T1–T14) pass unmodified,
  including every spec vector — formulas and outputs are unchanged; only when
  they are recomputed changed.
- **Signature completeness:** the digest covers every sensor-facing field,
  confidence, contributing terms, segment/golden/discard/reset counters, and
  the watched rated capacity — a BMS recalibration step or a confidence
  transition always propagates (T17). Baseline reset force-clears the
  signature so the button always produces a visible update.
- **Tier overrides are exact-name only:** regression test asserts other
  `total_*` / `rated_capacity` registers keep their previous tiers; the
  energy-counter timeout-fallback protection (`is_energy_counter`) is
  unaffected (matching is independent of tier).

## 3. Evidence

- Full suite: **296 passed, 1 skipped, 0 failed** (was 283/1), deterministic
  across repeated runs. 13 new tests: T15 (3), T16 (3), T17 (3), tier
  overrides (4).
- Static: `ast.parse` clean over all Python files; all JSON valid;
  `manifest.json` = 1.1.6.
- Control-path safety unchanged: the delta introduces no register writes, no
  new tasks/threads, no new I/O paths.

## 4. Consciously NOT changed

- Balance/efficiency gating registers stay in their default tiers (pack
  temps SLOW, pack voltages NORMAL): rest-state sampling tolerates 5-min data
  and re-tiering them FAST would add real bus load for no accuracy gain.
- No debounce/batching changes to Store persistence — already event-driven
  and ≥ 5 min apart.
- No micro-optimization of `_evaluate()`'s report/dict construction
  (~µs-scale; readability wins).

**Verdict:** release-ready. The pass measurably improves counter-derived data
quality, removes the only unbounded-with-history per-tick costs, and cuts
recorder churn by ~three orders of magnitude in steady state, with cache
coherence and equivalence explicitly tested.
