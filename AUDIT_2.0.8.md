# huawei_solar 2.0.8 — Patch Addendum (pre-redeploy fixes for the telemetry run)

**Context:** after the 2.0.7 release, a full external ICS quality/defect/architecture audit was run against the shipped 2.0.7 zip, and the first ~2h15m of production log from the actual deployment was reviewed. This addendum covers only the five items scoped for immediate patching before restarting the 24h telemetry window — see the conversation record for the full audit triage (17 defects + 4 architecture risks reviewed; 5 patched here, the rest deliberately deferred as pre-existing and independent of what this telemetry run measures).

**Discipline applied:** identical to every prior release this project — every claim verified directly against real source (including the installed Home Assistant core source itself, where relevant) before any fix was written; every fix given a dedicated adversarial test; full suite re-run after every change; final verification from a **fresh, independent extraction** of the packaged zip, not the working tree.

**Final verification:** 989 passed, 1 skipped, zero new regressions — confirmed identically from the packaged `huawei_solar-2.0.8.zip`, matching the exact same pre-existing baseline documented in `AUDIT_2.0.7.md` §7.

---

## What's patched

### 1. DEF-011/012 — `validate_sample()` dropped `current_a`/`serial_number`

**Confirmed severity:** this made TOPO-01's entire pack-replacement-detection feature (the flagship deliverable of the 2.0.7 release) non-functional in production. `validate_sample()` sits at the top of `BatteryHealthEngine.update()` — the universal entry point for every real sample — and its `PackSample` reconstruction loop simply never carried the two newest fields through, exactly repeating the class of bug the code's own v2.0.6 comment already documents for a *different* set of fields.

**Root cause on the test side:** every existing test (including this project's own) exercised `PackCapacityTracker.feed()` directly or built `HealthSample`/`PackSample` objects by hand — none went through `validate_sample()`/`engine.update()`, the actual production path. This was a real gap in verification discipline, not just bad luck, and is called out explicitly rather than glossed over.

**Fix:** both fields now carried through, each with a real, own-purpose validity check:
- `current_a` — new `PACK_CURRENT_LIMIT_A = 600.0` plausibility band (generously derived from the existing `POWER_LIMIT_W` divided by a conservative low pack voltage — a judgment call, flagged as such).
- `serial_number` — new `_valid_serial_or_none()` helper (non-empty string, whitespace-stripped, capped at 64 chars) since the existing numeric `_valid_or_none()` doesn't apply to strings.

**Test coverage:** 5 new tests, including the one that should have existed before this shipped — a true end-to-end replacement-detection test through `BatteryHealthEngine.update()`, not just direct tracker calls.

### 2. DEF-013 — corrupt-recovery engine rebuild lost topology

**Confirmed:** the fallback path after a structural `restore()` failure rebuilt the engine with only `self.engine.cfg`, silently defaulting to `pack_count=3` regardless of real resolved topology — a second call site that needed the same topology-aware construction `__init__` already uses, missed when TOPO-01 shipped.

**Fix:** the fallback now passes `pack_count=len(self._pack_slots)` and `pack_slot_labels=[...]`, identical to the real construction.

**Test coverage:** adversarial test against a genuine 2-unit/6-pack topology, confirming the fallback engine matches.

### 3. Store-version conflation (found independently in the production log, not in either audit)

**What happened, confirmed against the real, installed Home Assistant source:** `Store(hass, SCHEMA_VERSION, key)` passed our own internal schema number directly as HA's own storage-format version. HA's `Store` class has its own separate version-mismatch protocol — the base class's `_async_migrate_func()` unconditionally raises `NotImplementedError` unless overridden via subclassing (there is no constructor parameter for it). Bumping `SCHEMA_VERSION` 2→3 for TOPO-01 therefore made `self._store.async_load()` itself raise, *before* our own `restore()` — and therefore before BH-09's own reset-visibility logic — ever ran. The exact scenario BH-09 was built to make visible was itself silently swallowed one layer up.

**Fix:** new `BatteryHealthStore(Store)` subclass whose `_async_migrate_func()` returns the old data completely unchanged, making HA's own version machinery a permanent no-op. A new, deliberately frozen `_HA_STORE_FORMAT_VERSION` constant replaces the direct `SCHEMA_VERSION` coupling — all future schema evolution now routes exclusively through the internal `schema_version` key + `_SCHEMA_MIGRATIONS` registry, where BH-09's machinery can actually see it.

**One thing flagged, not hidden:** the very next load after this patch deploys will still show one more "fresh start," because the currently-live deployment already went through the broken path once. After that single transition, this class of problem is gone for good — not a recurring cost.

**Test coverage:** direct behavioral test of the override (including the exact major-version-gap scenario from the real log), plus source-level confirmation the real construction site uses the fix, not just that it exists unused. Building a full round-trip test through a real `HomeAssistant()` instance was judged disproportionate for this one fix; noted as a scope limitation rather than silently skipped.

### 4. DEF-007 — optimizer background refresh used the wrong coordinator API

**Confirmed against the real, installed HA source:** `async_config_entry_first_refresh()` explicitly checks `self.config_entry.state == SETUP_IN_PROGRESS` and reports incorrect usage otherwise — already logged as a warning for custom integrations today, explicitly flagged by HA itself to become a hard break in a future version. This background task always runs after setup has already returned, so it never satisfied that precondition. Separately: on failure, that method's real purpose (raising `ConfigEntryNotReady`) was already dead code here, since the surrounding `except Exception` swallows it regardless.

**Scope note:** predates this session (from the original v1.3.8 background-task change); this session's START-01 fix added a stagger sleep in front of this exact call without touching the call itself.

**Fix:** `async_config_entry_first_refresh()` → `async_request_refresh()`, confirmed as a clean drop-in against the real HA source (no state precondition, no side effects being relied upon).

**Test coverage:** existing START-01 tests updated to match (they were asserting the old, now-fixed call), plus a new dedicated test confirming the correct API is used and the old one is gone.

### 5. DEF-015 — unit-2 pack counter tier coverage

**Confirmed:** `_TIER_OVERRIDES` only had `NORMAL`-tier entries for unit 1's pack counters. TOPO-01 added real support for a genuine second storage unit, but this list was never updated — unit 2's identical counters would silently fall through to the generic `SLOW` tier, biasing pack-quality comparisons between two physically equivalent units.

**Fix:** generated (not hand-duplicated a second time) for both units × all 3 packs, closing the exact "list grows, override doesn't" pattern that caused this.

**Test coverage:** unit-1/unit-2 parity check, plus — per the audit's own secondary recommendation — a test that cross-checks tier coverage against whatever `required_register_names()` actually resolves for a real topology, rather than trusting a second, independently-maintained list to stay in sync by hand.

---

## Deliberately not in this patch

Everything else from the external audit (DEF-001 through 006, 008, 009, 010, 016, 017, and all four ARCH items) — confirmed real where checked, but genuinely pre-existing and independent of what the Modbus/battery-health telemetry run is measuring. Deferred to its own dedicated pass, per the reasoning already agreed: forcing a much larger scope into this patch right before redeploying would repeat exactly the kind of scope creep this project has avoided throughout.

---

## Final verification

- All 4 patched files (`battery_health.py`, `battery_health_manager.py`, `update_coordinator.py`, `register_cache.py`) compile cleanly.
- Full suite: **989 passed, 1 skipped**, confirmed identically from a fresh, independent extraction of the packaged `huawei_solar-2.0.8.zip` — zero drift between the working tree and the shipped artifact.
- `manifest.json` confirms version `2.0.8`.
