# huawei_solar 2.0.12 — Release Audit

**Scope:** Battery Phase 5B — pack-level analysis promoted to be the authoritative source of truth, replacing unit-level as the primary reported number while retaining unit-level as an independent cross-check, plus the supporting infrastructure this required: serial-identified pack tracking through replacement, preserve-on-replacement archival, per-pack install dates, and a new service to set them.

**Motivation, in the user's own words, driving every design decision in this release:** pack-level is chemically the correct unit of analysis (each pack ages independently); the resulting data should be able to reveal drift and identify a genuinely bad pack for a warranty claim; and a pack's own historical data — once a physical pack is replaced — is gone forever if not preserved at the time, unlike Modbus telemetry which can always be recaptured.

**Discipline applied throughout, unchanged from every prior release:** verify every claim against real source before writing a fix; adversarial test proving the specific behavior each addition provides; full suite re-run after every change; final verification from a fresh, independent extraction of the packaged zip.

**Final verification:** 1,178 passed, 1 skipped — confirmed identically from a fresh, independent extraction of `huawei_solar-2.0.12.zip`, matching the same known pre-existing baseline (5 failed / 12 errored, documented since 2.0.7) with zero new regressions.

---

## Design discussion preceding implementation

Before any code was written, several design questions were worked through explicitly with the user:

1. **Should pack-level fully replace unit-level, or coexist?** Resolved as: pack-level becomes the primary, authoritative source; unit-level is retained specifically as an *independent* cross-check (a genuinely separate measurement path — the unit's own total charge/discharge counters, not derived from the pack estimates) rather than discarded, since a disagreement between the two is itself valuable information.
2. **What does the "unit-level overview" actually mean once pack-level drives the real computation?** Clarified as three distinct things, not two: pack-level detail (the real computation), a unit-level *overview* (a rollup derived from pack data, for drill-down UX), and unit-level *independent measurement* (the validation cross-check) — kept genuinely separate from the overview rather than conflated.
3. **How should packs be identified through a replacement?** By serial number, not slot position or install date — confirmed this was already correctly built (TOPO-01, an earlier release) before extending it.
4. **Should install date move to per-pack, not just unit-level config?** Yes, and further refined mid-discussion: **serial-keyed**, not slot-keyed, matching the exact same "age is a property of the physical pack" reasoning as replacement detection itself.
5. **An uploaded architecture-review document** (independently authored, pre-dating this session) was consulted as confirming context, not a requirement, per the user's own explicit instruction — its target architecture independently named "weakest pack" as the system-health output and explicitly warned against a naive average, which matched and validated the fusion design already being worked toward, rather than introducing new direction.

---

## What was built

### Preserve-on-replacement
A confirmed, previously-live risk: on a detected pack replacement, the outgoing pack's entire accumulated `SegmentTracker` state was being silently discarded (Python garbage collection) with only a bare integer (`pack_replaced_count`) surviving. `retired_pack_history` now archives a snapshot — serial, replacement timestamp, final SOH/capacity, segment count, first/last segment timestamps — before the fresh tracker overwrites the slot. Persisted unconditionally across a topology change (it's a historical log, self-describing via its own `slot_label` per entry, unlike slot-indexed current-state fields).

### Per-pack install dates, serial-keyed
`pack_first_detected` (automatic, first-observation timestamp per serial) and `pack_install_dates` (explicit, user-set override) combine via `effective_pack_install_ts()`'s three-tier fallback: explicit override → unit-level date (for a slot that has *never* been replaced, presumed original) → automatic first-detected (for a replaced pack, where the unit-level date would be actively wrong). A new `set_pack_install_date` service lets the user record a specific pack's own real install date after a replacement, resolved and persisted through the existing `BatteryHealthManager` chain.

**A real gap caught by the test suite itself, not manual review**: `const.py` has a second, separate `SERVICES` tuple (distinct from `services.py`'s own `ALL_SERVICES`) that specifically gates which services get unregistered on integration unload. The new service was correctly wired everywhere else but missed this tuple — without it, the service would have registered fine but leaked on unload. A dedicated completeness test in `test_const_services.py` caught this before it shipped.

### Pack-fusion promotion — the core of this release
`soh_capacity` (the value that drives the reported BHI composite) is now **the worst eligible pack's own SOH**, not a weighted average and not the old unit-level estimate. This was a deliberate choice, confirmed by both direct reasoning and the independently-consulted architecture document: a weaker pack's true health must never be diluted behind healthier siblings in the headline number — the chemically honest position (usable system capacity is, in practice, gated by the weakest pack) and the one that actually serves the stated warranty-detection goal.

The original unit-level estimator is retained, computed exactly as before, now exposed as `soh_capacity_unit_independent` — an explicit cross-check, with `capacity_cross_check_diverged` flagging a meaningful disagreement (threshold: 10 percentage points, a documented judgment call, not a derived constant). Falls back gracefully to the unit-level estimate when no pack has accumulated enough data yet (a fresh install, or a persisted state predating this feature) — confirmed via the full existing 187-test suite passing unchanged, since the fallback path exactly reproduces prior behavior.

**Confidence tied to the actual driving evidence**: previously, the report's `confidence` field always used the unit-level estimator's own segment count and staleness — a genuine mismatch once `soh_capacity` was promoted to reflect pack-fused data instead. Now follows whichever source actually drives the number: the worst pack's own segment count/staleness when pack-fused is active, the unit-level figures on the fallback path (unchanged for that case). Verified with paired positive/negative tests — confirming confidence follows the pack even when the unit-level estimator alone would say otherwise, and vice versa on the fallback path.

### A UI-exposure gap, found by the user's own follow-up question and fixed in the same pass
After the fusion logic was built and tested, the user asked directly whether the new data was actually visible in the HA UI. Checking precisely revealed it was not: `battery_health_entities.py`'s `soh_capacity` sensor builds its `extra_state_attributes` from an explicit allowlist of key names, not "show everything in the report" — and none of the new Phase 5B keys had been added to it. The internal computation was correct throughout; none of it was reaching the user.

Fixed by adding all the new keys (`soh_capacity_source`, `weakest_pack_slot`, `soh_capacity_unit_independent`, `capacity_cross_check_diverged`) to the allowlist, and — while auditing this same code path — found that `effective_pack_install_ts()` (built and unit-tested earlier in this same release) had never actually been wired into the report's own attribute pipeline at all, meaning a user could *set* a pack's install date via the new service but never *see* the resulting age anywhere. Added `pack_age_days`/`pack_age_source`, computed per pack in `_evaluate()`, plus `retired_pack_history` itself (also previously absent from the report pipeline, only ever a persisted internal field).

**A second gap, unrelated to this release's own new work, found in the same pass**: `pack_replaced_count` and `pack_slot_labels` — pre-existing, computed fields from an earlier release — turned out to have this exact same "never in any allowlist" gap. Fixed together in the same edit, since it's the identical "what's the story on my packs" narrative as everything else in this group, rather than deferred as a separate concern.

Verified with 8 new tests that construct a real Home Assistant sensor entity (via this test file's own established stub-HA harness) and call its real `_apply()` method — confirming the values genuinely reach `_attr_extra_state_attributes`, not just that source text mentions the right key names.

---

## Process notes from this release, disclosed in the same spirit as every prior one

- Fixed the recurring class-header-clobbering `str_replace` mistake twice more this release (both times in `test_battery_health.py`) — each caught immediately via `grep` verification of class headers after insertion, or via a resulting test collection failure, and repaired before proceeding.
- One genuine test-precision bug of my own: an early fusion test compared a full-precision internal value against its own rounded, display-only attribute counterpart with exact equality — caught by the test itself failing, not by manual review, and fixed by widening the tolerance rather than papering over it.
- The order in which gaps were found in this release is itself worth being honest about: the UI-exposure gap was not caught by the extensive test suite built alongside the fusion logic — it took the user's own direct question to surface it. The test suite proved the *computation* was correct; it took a targeted, different kind of check (real entity construction, not just internal report assertions) to prove the *delivery* was correct. Both are now covered.

## Post-release addendum: per-pack entity placement restructuring

After the initial 2.0.12 release above, a direct user question ("what are the 3 battery health indicators for each pack, where do I find them?") led to a substantial follow-up review and restructuring, documented here in full rather than silently folded into the release notes above.

### What the review found

Checking precisely where every new per-pack attribute from this release actually lived revealed a real design inconsistency, confirmed directly with the user rather than assumed: this integration's own established, pre-existing convention for per-pack data is **individual entities**, one per specific pack number (e.g. `translation_key="pack_1_state_of_capacity"`), each attached to that pack's own physical storage-unit device ("Battery 1"/"Battery 2", per `sensor.py`'s own `BATTERY_TEMPLATE_SENSOR_DESCRIPTIONS`). Every new per-pack value this release added — `pack_capacity_soh_percent`, `pack_capacity_segment_count`, `pack_replaced_count`, `pack_age_days`, `pack_age_source` — had instead been bundled as list-valued attributes on the aggregate `soh_capacity` sensor, under a separate "Batteries" device. A user's own reasonable expectation (that pack data should all be in one place) did not match actual behavior.

A second, related gap was found in the same review: setting a pack's own install date was only reachable via the `set_pack_install_date` service (Developer Tools → Actions, or an automation) — there was no entity a user could simply click and set, unlike this integration's own established pattern for other writable settings (`number.py`'s own working, tested `async_set_native_value()`). No `date.py` platform existed at all prior to this addendum.

### What was restructured

- **Individual per-pack sensor entities**, one set of five per pack (SOH capacity, segment count, replaced count, age in days, age source), each attached to that specific pack's own "Battery 1"/"Battery 2" device — parsed from each pack's own slot label (`u1p2` → Battery 1) via a shared, public regex (`SLOT_LABEL_RE`) rather than duplicated logic. Entity names include the pack number (`"Pack 2 SOH capacity"`) since multiple packs can share one device.
- **A new `date.py` platform** — this integration's first — providing one writable install-date entity per pack, showing whichever date is currently in effect (including a fallback-derived one, not just an explicitly-set one, so a user can see and correct what's being used rather than see "unknown" until they've acted). Registered in `PLATFORMS` alongside the four existing platforms.
- **A shared write path**: `BatteryHealthManager.set_pack_install_date()`, called by both the pre-existing service and the new date entity, so the two can never drift out of sync by each reimplementing the same three steps (write, mark dirty, save) separately. `services.py`'s own implementation was refactored to use it.
- **The aggregate `soh_capacity` sensor's own attributes were cleaned up**: the five genuinely per-pack values were removed (now living as individual entities instead); only genuinely aggregate values remain (`weakest_pack_slot`, `pack_capacity_spread_pct`, `soh_capacity_source`, `soh_capacity_unit_independent`, `capacity_cross_check_diverged`, `pack_slot_labels` as index-reference context, `retired_pack_history` since a retired pack has no live slot to attach an individual entity to).

### Testing

30 tests for the new per-pack sensor entities (`test_battery_health_entities.py`) and 18 new tests for the new `date.py` platform (`test_date.py`), covering correct index reading, correct per-unit device attachment, graceful handling of a missing device or an unparseable slot label, the date/age-days round-trip math, the shared write path (including resolving a pack's *current* serial at call time, not one captured at entity construction, so a later replacement is handled correctly), and fault isolation matching every other battery-health entity class's own established pattern. Three pre-existing tests (written for the original, incorrect placement) were updated to test the corrected behavior rather than the old one.

A genuine import-collision obstacle was hit and resolved while building `test_date.py`: this project's own package is named `huawei_solar`, which collides with the separately-installed `huawei_solar` PyPI library (the underlying Modbus driver) that `battery_health_manager.py` also needs. Confirmed directly by attempting a real, unstubbed import first (which failed exactly this way) before falling back to the same minimal-stub approach `test_battery_health_entities.py` already established — plus loading `const.py` and `register_cache.py` for real (both have zero HA dependencies of their own), rather than hand-stubbing their many constants individually.

## Final verification

- Every file in the packaged `huawei_solar-2.0.12.zip` compiles cleanly; `strings.json`, `translations/en.json`, and `services.yaml` all validate.
- Full suite, run from a **fresh, independent extraction** of that exact zip: **1,207 passed, 1 skipped**, matching the established pre-existing baseline (5 failed / 12 errored) exactly — zero drift, zero new regressions — up from the original release's 1,178 passed, reflecting the net effect of this addendum's new and rewritten tests.
