# Release Audit — huawei_solar v2.0.0

**Date:** 2026-08-07 · **Auditor:** Claude (Anthropic)
**Baseline:** v1.3.21
**Type:** major architectural rebuild — the register cache's data model,
the batch executor's failure handling, every entity platform's attribute
surface, `battery_health_manager.py`'s value extraction, and two
Modbus-traffic optimizations, all in one coordinated release. Full design
record in `V2_ARCHITECTURE_DESIGN.md`; this document audits what was
actually implemented and verified against that design, not a repeat of
the design reasoning itself.

---

## 1. The problem this release exists to fix

v1.3.21 and every prior version conflated two genuinely different
situations through a single `_CacheEntry.dirty` boolean: "we wrote to
this register, we *know* the cached value is now wrong" and "the
connection dropped, we *don't know* if this changed." `RegisterCache.merge()`
treated both identically — a register invalidated by either cause and not
immediately re-read was dropped from `coordinator.data` entirely, showing
`Unknown` in Home Assistant even when a recent, probably-still-accurate
reading existed. Traced directly against source during this session, not
assumed from an external report (one was reviewed and found to have the
right instinct but an overstated diagnosis of this specific codebase —
see `V2_ARCHITECTURE_DESIGN.md` §1-§2).

A parallel finding, from the operator's own hardware-architecture review,
extended the same insight further: with per-register quality and age now
knowable, several places in the codebase that had been polling
defensively — to compensate for not knowing what they actually had — no
longer need to, and two of those (energy-counter availability,
synchronized-power reads) were addressed directly in this release
(§8.1, §8.2 below).

## 2. What shipped — by design-document section

### §3-§6: the quality model itself (`register_cache.py`)

- `Quality` (GOOD/UNCERTAIN/BAD) and `Reason` enums replace `dirty`
  entirely. `_CacheEntry` carries `quality`/`reason` instead of a boolean.
- `merge()` and `get()` now serve a cached value whenever quality is
  `GOOD` **or** `UNCERTAIN` — only `BAD` withholds it. This is the direct
  fix for §1's root defect.
- `invalidate_all()` (reconnect) now produces `UNCERTAIN/LINK_DOWN`
  (servable). `invalidate()` (write) stays `BAD/WRITE_PENDING`
  (correctly withheld — we *know* the value is wrong, not merely
  unverified; researched against real OPC UA/Siemens S7 practice and
  found this distinction has no direct precedent there, since that
  architecture's write path doesn't have the same asynchronous gap —
  §6).
- New `record_attempt()`/`quality_of()` — an additive accessor, not a
  replacement of `coordinator.data`'s shape. `coordinator.data` remains
  exactly `dict[RegisterName, Result[Any]]`; five existing consumers
  (`sensor.py`, `number.py`, `select.py`, `switch.py`, and
  `battery_health_manager.py`) needed zero changes to their value-reading
  logic as a result (§10.4).
- `_live_quality()` implements the lazy `EXPIRED` transition
  (`UNCERTAIN` → `BAD` past a ceiling), with `STATIC` tier exempted
  entirely (§10.3 — a real semantic bug caught during the design sweep,
  not shipped: immutable-by-design data such as serial numbers should
  not auto-degrade from elapsed time) and energy counters given a
  longer, not shorter, ceiling (§8.1).

### §5.2, §10.5: `update_coordinator.py`'s batch executor

- `_execute_batch()` rebuilt for best-effort-per-chunk execution. Traced
  against the pre-rebuild control flow before changing it: a failing
  chunk's specific register names were only ever in scope *inside* the
  per-chunk loop; a bare `raise` propagated out of the whole function,
  discarding both which chunk failed and any already-succeeded earlier
  chunks in the same batch (a local `merged` that was never returned).
  Both fixed: `cache.record_attempt()`/`cache.update()` now run inline,
  per chunk, and a failing chunk no longer aborts chunks not yet
  attempted. The coordinator-level back-off contract is preserved by
  still raising the first failure — but only after every chunk has had
  its chance, not instead of running them.
- New `_classify_failure()` maps every exception type the coordinator
  already distinguishes (`ModbusQueueShed`, `TimeoutError`,
  `ReadException` including the 0x06 busy case, `ConnectionInterruptedException`,
  the `HuaweiSolarException` catch-all) to the right `Reason`.
- A real bug in this session's own restructuring was caught and fixed
  before shipping: moving `cache.update()` inline made the old post-batch
  `self.cache.update(fresh)` call not just redundant but actively
  harmful — it would have compared every successful value against
  itself and silently doubled the adaptive TTL-stretch rate on every
  poll. Removed.
- `BACKOFF_DEFERRED` capture fix: the priority filter's
  `stale_names = priority_names` is an in-place reassignment: the
  pre-filter set is snapshotted first (`pre_filter_names`), and the
  deferred set is computed *after* the starvation-promotion and
  canary-forcing logic completes, so a canary drawn from outside the
  original due-set doesn't get miscounted as deferred.

### §8: entity layer (`types.py`, `sensor.py`, `number.py`, `select.py`, `switch.py`)

- New `HuaweiSolarEntity._quality_attrs()` — implemented exactly once, on
  the shared mixin every platform inherits, exposing
  `data_quality`/`data_quality_reason`/`data_age_seconds`.
- `available` needed **zero logic changes anywhere** — since `merge()`
  already includes `UNCERTAIN` and only omits `BAD`, every platform's
  existing `key in coordinator.data` presence check already implements
  the correct rule for free.
- Four multi-register aggregate sensors (two alarm sensors, forcible-
  charge status, active-power-control-mode) deliberately excluded from
  `_quality_attrs()` in this pass — a multi-register aggregation policy
  (worst-quality-wins? primary-register-only?) was not specified in the
  design and isn't guessed at here. Documented explicitly at each site,
  not silently skipped.

### §8.1: energy-counter policy — the one section rewritten twice after implementation began

Shipped policy is a **two-stage bound**, materially different from the
first two drafts of this same section:

1. `ENERGY_PROMOTION_CEILING_S` (90s) — a tightened starvation-promotion
   fuse for energy-relevant `SLOW`-tier registers specifically (most
   energy counters classify `SLOW`; a few, e.g.
   `storage_total_charge`/`discharge`, are `NORMAL` tier and take the
   existing every-4th-cycle path instead — noted explicitly, not silently
   assumed covered).
2. `ENERGY_AVAILABILITY_CEILING_S` (600s) — energy counters stay
   available at `UNCERTAIN` quality for *longer* than the generic 300s
   ceiling, not shorter. Reasoned directly against how HA's statistics
   engine actually works, checked via web search rather than assumed:
   the `sum` delta is value-to-value, not time-weighted (a late-but-genuine
   reading still lands on the correct total regardless of gap length);
   the recorder already deduplicates unchanged writes (an originally-proposed
   "suppress redundant writes" mechanism was found unnecessary and
   dropped); the real risk is short-term-statistics snapshot
   misattribution on a fixed 5-minute clock, which scales with gap
   length, not staleness as a binary property.

The original entity-level "withhold unless `GOOD`" fallback logic — built
earlier in this same implementation phase — was found to directly
undermine this once the longer cache-level ceiling existed, and was
removed. The policy now lives entirely in `RegisterCache`, keyed by
register type; the coordinator's stale-cache fallback is uniform,
identical for every register.

### §8.2: `SynchronizedPowerCoordinator` cache shortcut

`_try_cache_shortcut()` checks `quality_of()` and age across up to four
per-device caches (main ×2, power meter, battery) before performing the
dedicated read; if all are `GOOD` and aligned within
`SYNC_POWER_CACHE_ALIGNMENT_TOLERANCE_S` (3.0s), the dedicated read is
skipped entirely. The tolerance is derived, not guessed: the device's own
Modbus register refreshes at 1 Hz (readings within ~1s are the literal
same value, not merely close); the four devices are not phase-locked but
each device's own ~40-50 Hz internal sampling adds only ~20-25ms of
cross-device jitter; sensor accuracy is Class 1.0 (±1%) for power on
three of the four channels; and 3s is cross-checked against the dedicated
read's own measured `sample_span_ms` (Defect V/Finding 9) so the shortcut
never accepts materially worse alignment than what it replaces.

### §8.3: battery-health polling cadence — explicitly not shipped

Flagged as real, believable headroom (now that `battery_health_manager.py`'s
value extraction is quality-gated, the tight polling that used to
compensate for that risk at the polling layer is likely redundant) but
deliberately left undesigned pending observed field data rather than a
guessed number. No code changes in this release for this item.

### §10.4: `battery_health_manager.py` — the deliberate exception, and the reason this rebuild exists

`_value()` is now quality-gated: a register whose quality isn't `GOOD` is
treated exactly like a genuinely missing one (returns `None`), rather
than silently trusting a stale-served `UNCERTAIN` value as current. All
15 call sites updated (14 in `_build_sample()`, 1 in the rated-capacity
diagnostic log check). This is the specific consumer distinguished from
every display entity in §10.4: it builds stateful deltas from sequential
readings (SOC drop across a segment, energy accumulated between anchors),
where silently trusting a stale reading can corrupt internal state that
persists and compounds — the concrete vulnerability that motivated
abandoning the parked Phase 1 battery-health work and starting this
rebuild in the first place (see `PHASE1_BATTERY_HEALTH_DESIGN.md`'s
opening paragraph).

## 3. Deliberately out of scope, stated explicitly (§10.1, §10.6)

- `SynchronizedPowerCoordinator`'s and the optimizer coordinator's own
  read paths never touch `RegisterCache` at all — confirmed directly,
  not assumed (`_execute_batch()`'s restructuring cannot leak into
  either; verified via the real class hierarchy and an existing code
  comment).
- Write-verification reads (`switch.py`'s post-write status poll) still
  bypass the cache entirely — a real, named enhancement opportunity, not
  a correctness gap; nothing breaks by leaving this for a later release.

## 4. Mistakes caught during this implementation, and how

Consistent with this project's standing practice of documenting mistakes
plainly rather than smoothing them over:

1. **A bare `cfg` reference bug** in Phase 1's (since-reverted) manual
   re-anchor handler — would have been a `NameError` on first use. Caught
   on review before any test ran against it.
2. **The redundant post-batch `cache.update(fresh)` call** (§2 above) —
   caught by tracing the data flow after restructuring `_execute_batch()`,
   not by a test failure.
3. **Three separate test-infrastructure collisions**, all from the same
   underlying cause (this test suite's `sys.modules` namespace is
   process-global and shared across every test file in a run), each
   requiring a different fix once properly understood rather than papered
   over with a broader, riskier one:
   - `test_battery_health_isolation.py`: an overly broad "clear the whole
     `huawei_solar.*` namespace" fix was tried, found to break other test
     files that depended on state a prior file had cached, and replaced
     with a minimal, dependency-free stub matching `register_cache.py`'s
     own test file's established pattern.
   - `test_v2_quality_attrs.py`: `setdefault()` silently reused an
     incompatible existing stub from another test file (missing
     attributes this file needed); fixed by adding only what was missing
     to whatever was already there.
   - The same file, a second and subtler layer: the reused `Result` stub
     had an incompatible zero-argument constructor. Resolved by using an
     independent local class for constructing test values — nothing in
     this codebase does `isinstance()` checks against the real `Result`
     type, so there was never a need to share class identity in the
     first place.
4. **An editing mistake, twice**: two separate `str_replace` edits to
   `V2_ARCHITECTURE_DESIGN.md` accidentally consumed the section heading
   line itself while inserting new content before it, both caught by
   checking the document's full heading structure before considering the
   edit done, not assumed correct.

## 5. Test evidence

- **675 passed, 1 skipped, 0 failed**, deterministic across repeated runs
  (was 633 at the v1.3.21 baseline; 42 new tests added this release,
  across the quality model, best-effort chunking, `BACKOFF_DEFERRED`,
  the synchronized-power shortcut, `battery_health_manager.py`'s
  quality-gating, and the entity `_quality_attrs()` helper).
- **Adversarial, not just additive**: the new `TestBestEffortChunking`
  and `TestBackoffDeferredCapture` suites (11 tests) were run directly
  against the pristine pre-session v1.3.6 baseline and confirmed to fail
  there — proving they test something this release actually introduced,
  not tautologies that would pass against any code.
- The energy-counter policy has a dedicated adversarial test asserting
  the *relationship* between the new constants
  (`ENERGY_AVAILABILITY_CEILING_S > REGISTER_STARVATION_CEILING_S >
  ENERGY_PROMOTION_CEILING_S`), not just their presence — the actual
  claim being made by this release, checked directly.
- `battery_health_manager.py`'s quality-gating has a dedicated
  adversarial test reproducing the *old*, pre-rebuild `_value()`
  signature with no quality check, proving it would have trusted a
  stale-served value, alongside the fixed version correctly not doing
  so — the closest thing to direct evidence that this release's
  motivating vulnerability was real, not theoretical.
- Static: `py_compile` clean on every changed file (`register_cache.py`,
  `update_coordinator.py`, `const.py`, `types.py`, `sensor.py`,
  `number.py`, `select.py`, `switch.py`, `synchronized_power_coordinator.py`,
  `battery_health_manager.py`, `__init__.py`).

## 6. Safety properties

- 1.3.21 remains untouched, deployed, and stable throughout this entire
  design-and-build process — this release was built in a separate working
  tree from the start.
- Explicitly NOT backward compatible, and deliberately so, by prior
  agreement: sensor value history, entity IDs, and ~72 days of
  accumulated adaptive-learning data are not migrated
  (`V2_ARCHITECTURE_DESIGN.md` §7). Device connection configuration
  (host/port/slave IDs/credentials) IS preserved — a structurally
  different guarantee, since it lives in Home Assistant core's own
  `config_entries` storage, untouched by this release's schema (no
  changes were made to `config_flow.py`'s data/options shape at all).
- No changes to `ModbusGuard`, the adaptive controller's learning model,
  or the register tier classification system itself.
- Every constant introduced carries its reasoning in its own comment,
  matching this project's established convention — none are bare
  numbers.

## 7. Recommended deployment procedure

1. Delete `/config/custom_components/huawei_solar` entirely.
2. Extract v2.0.0 fresh into `custom_components/`.
3. Restart Home Assistant.
4. **Expected, deliberate, one-time effects of this being a clean-break
   major version**: adaptive learning restarts from its conservative
   baseline (no migration of prior learning data — agreed explicitly);
   entity IDs may not match v1.3.21's, requiring dashboard/automation
   reconfiguration (also agreed explicitly) — device connection settings
   themselves do not need re-entry.
5. **Direct validation for this release's core fix**: after a genuine
   connection blip (a real one, or `on_connection_lost()` firing
   naturally under contention), check `data_quality`/`data_quality_reason`
   attributes on any previously-affected sensor — it should show
   `uncertain`/`link_down` with a real value still present, not
   `Unknown`.
6. **Direct validation for the energy-counter policy**: an energy-counter
   sensor should remain available through normal contention; check its
   `data_age_seconds` attribute during a rough patch rather than expecting
   it to go unavailable.
7. **Direct validation for the synchronized-power shortcut**: watch for
   the coordinator skipping its dedicated read during stable conditions —
   observable via reduced Modbus traffic to those four registers, or by
   instrumenting `_try_cache_shortcut()`'s return value in a debug
   session.

**Verdict:** release-ready. The largest single-release scope of this
project's history, built and verified with the same incremental,
adversarially-tested discipline as every smaller release before it —
foundation first, then outward to the coordinator, then the entity
layer, then the specific consumer that motivated the whole rebuild, then
the operator-identified optimizations, each validated against the full
suite before the next began, with every mistake found along the way
documented rather than quietly fixed and forgotten.
