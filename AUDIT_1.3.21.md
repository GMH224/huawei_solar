# Release Audit — huawei_solar v1.3.21

**Date:** 2026-08-06 · **Auditor:** Claude (Anthropic)
**Baseline:** v1.3.20
**Type:** one architectural change, requested directly with an explicit
trade-off and tolerance stated up front, three production files changed.

---

## 1. The request and the evidence behind it

A 107-minute field capture (analyzed live, not from a test file) showed
`HV2220098926`'s battery pack temperature registers refreshing at a
median of ~9 minutes apart, worst case ~1308s (21.8 minutes), and
`BMS temperature` specifically never successfully read even once across
the entire capture. The operator, having reviewed this data directly, made
two things explicit before any design work began:

- **A stated tolerance:** *"if they haven't been updated for 5 minutes,
  they become more important."*
- **A stated trade:** *"I rather have lower fast frequency than constant
  errors."*
- **A stated safety constraint, from earlier discussion:** *"most of the
  sensors are read only, so it is not like controlling a chemical plant
  when things go wrong when we reduce frequency of fast ones"* — narrowing
  the acceptable design space to something that trades responsiveness for
  reliability, since nothing here writes to an actuator based on this
  data.

## 2. Diagnosis — why the current design allows unbounded staleness

`register_cache.py`'s `RegisterTier` model is sound (FAST/NORMAL/SLOW/STATIC,
with SLOW's 900s base TTL itself already a deliberate, field-evidenced
choice from v1.3.3 — see that constant's own extensive history comment,
left untouched here). The gap is specifically in `update_coordinator.py`'s
back-off priority filter:

```python
for n in stale_names:
    tier = self.cache.tier_of(n)
    if tier == RegisterTier.FAST:
        priority_names.append(n)
    elif tier == RegisterTier.NORMAL:
        if self._backoff_cycle % BACKOFF_NORMAL_DIVISOR == 0:
            priority_names.append(n)
    # SLOW and STATIC are skipped entirely during back-off
```

This is a correct, deliberate design for *brief* back-off — reduce load
during a rough patch by deferring the expensive stuff. But it has no
ceiling: if back-off itself persists (field evidence this session shows it
can, for many consecutive cycles under sustained contention), a
SLOW/STATIC register can be deferred indefinitely, with nothing in the
system ever forcing it back onto the read path.

## 3. Design — why the fix is two paired changes, not one

The operator's own framing made clear this needed to be a *trade*, not a
pure addition. Simply adding "force a read every 5 minutes regardless of
tier" on top of the existing rules would only add demand to a bus that
tonight's own analysis showed is already periodically saturated even at
the guard's most conservative setting — net worse, not better. The fix
therefore pairs a give with a take:

**The give — `register_cache.py`:** `RegisterTier.FAST`'s base TTL changed
from `0.0` to `3.0` seconds. A `0.0` TTL means "always due whenever the
coordinator wakes up"; in ordinary operation this is already bounded by
the coordinator's own ~30s interval, so it mostly mattered at the edges —
back-off's own accelerated retry cycling, or overlapping refresh triggers,
could re-request the identical FAST-tier register only seconds apart for
no benefit. 3.0s is imperceptible to any dashboard consumer of
"instantaneous" power, while eliminating genuinely wasteful re-reads.

**The take, bounded — `update_coordinator.py` / `register_cache.py` /
`const.py`:** a starvation ceiling that guarantees no SLOW/STATIC register
can be deferred indefinitely, capped tightly enough not to reintroduce the
demand the "give" just removed.

## 4. `overdue_by()` — why "time past due," not "time since read"

New `RegisterCache.overdue_by(name)`:

```python
def overdue_by(self, name: RegisterName) -> float | None:
    entry = self._store.get(name)
    if entry is None:
        return None
    age = time.monotonic() - entry.ts
    return age - self._effective_ttl(entry)
```

This was not the first design considered. The naive version — "has this
register's raw age since last read exceeded 300s?" — was rejected after
working through the arithmetic explicitly, not just by instinct: SLOW's
own base TTL is 900s, meaning a SLOW register is *already* ≥900s old the
instant it first becomes due at all. A 300s raw-age ceiling would
therefore already be satisfied the moment back-off started for every
single SLOW/STATIC register — defeating tier-based deferral entirely
rather than merely bounding it, the opposite of the intended effect.

`overdue_by()` instead measures time *past the register's own due-time*:
zero the instant it becomes due, growing only from there. A register
therefore still gets the deferral tier-based back-off intends — it only
breaks through once it has been overdue for an *additional* 300s beyond
that, matching the operator's stated number precisely as "how much extra
delay is tolerable," not "how long before this counts as needing
attention at all." Confirmed adversarially in the test suite, not just
reasoned about (§7).

`None` (never successfully read) is treated as maximally overdue —
directly closing the `BMS temperature` case without first needing to
diagnose why that specific register was never reached; the mechanism
doesn't need to know why, only that it's true.

## 5. The promotion cap — deliberately not "promote everything starved"

`REGISTER_STARVATION_PROMOTIONS_PER_CYCLE = 1`: only the single
most-overdue starved candidate is promoted per back-off cycle, not the
whole starved cohort at once. Reasoning: several SLOW/STATIC registers
read together in their original batch tend to share similar timestamps,
so they can cross the starvation ceiling within moments of each other.
Promoting all of them simultaneously would inject a sudden burst of
expensive SLOW/STATIC reads into a cycle that is, by definition, already
in back-off because the bus is struggling — directly undermining the
reason back-off exists in the first place. Promoting one per cycle instead
guarantees forward progress (every cycle drains the single worst offender)
without a burst, at the explicit, deliberate cost of the whole starved
cohort clearing gradually rather than at once — judged an acceptable
trade given every affected register is read-only telemetry.

## 6. A mistake caught during development, recorded plainly

An early draft of the priority filter classified registers with
`self.cache.tier_of(n)`. This method returns `None` for any register that
has never yet been cached — *regardless of its actual tier*. Since the
starvation-tracking branch is reached by anything that isn't FAST or
NORMAL, a brand-new FAST-tier register (first poll ever for a newly added
entity, or the very first poll after a fresh install) would have been
misclassified into the starvation path and capped to one promotion per
cycle — instead of the "always included" guarantee FAST tier is supposed
to have. This was caught on review, before any test was written against
it, by working through what `tier_of()` returns for an uncached entry
specifically. Fixed by switching to `classify_register(n)` — a pure,
name-based classification already used elsewhere in this exact file
(`update_coordinator.py:209`, inside `_execute_batch`) for precisely this
reason: it is correct whether or not the register has ever been cached.
Both the bug and the fix are reproduced directly in the test suite (§7),
not merely described after the fact.

## 7. Adversarial verification

New `tests/test_starvation_ceiling.py` (18 tests):

- **`overdue_by()` semantics:** `None` for never-read; negative
  immediately after a read; zero exactly at the due-time; grows only
  after that. The rejected naive alternative is reproduced directly
  alongside the fix and shown, adversarially, to already exceed the
  ceiling the instant a SLOW register becomes due — proving the design
  decision in §4 was necessary, not stylistic.
- **The starvation hazard, proven real:** a SLOW register deferred through
  50 consecutive simulated back-off cycles is never once included under
  the pre-fix logic, regardless of how overdue it becomes.
- **The fix, proven correct:** promotes only once the ceiling is crossed,
  not before; only one promotion per cycle even with ten simultaneously-starved
  candidates; always promotes the single most-overdue one when several
  qualify; a never-read register is promoted as maximally starved.
- **No regression:** FAST is still always included and NORMAL's own
  divisor rule is unaffected by any of the above.
- **The `tier_of`/`classify_register` bug, reproduced directly:** a
  cache-lookup-style classifier is shown to genuinely misroute an uncached
  FAST-tier register; the real, name-based classifier is shown not to.
- **Static (AST) checks** confirm the real source: `overdue_by()` uses
  `_effective_ttl()` (not raw age); the priority filter calls
  `classify_register(n)` and does not call `.tier_of()` anywhere in that
  function (an AST-based check, deliberately, so it isn't tripped by this
  very audit's own explanatory comment mentioning `tier_of` by name); both
  new constants exist with the operator's own stated values; FAST's base
  TTL is `3.0` in `_TIER_BASE_TTL`.

**Run against the pristine pre-session baseline, all 4 applicable static
checks fail correctly** (predating both this defect's fix and the feature
it modifies).

## 8. Pre-existing test maintenance

Two pre-existing tests asserted the old `FAST TTL == 0.0` behavior
directly and needed updating for the intentional change:

- `test_register_cache.py::test_fast_always_stale` — split into two tests
  (`test_fast_not_immediately_stale`, `test_fast_stale_after_its_ttl_elapses`)
  rather than simply adjusting the one assertion, so both halves of the
  new behaviour (not stale immediately after a read; stale again once its
  own small TTL elapses) remain independently covered, not just the
  behaviour that happened to keep passing.
- `test_tier_separation.py::test_fast_and_normal_unchanged` — updated to
  assert `3.0`, with a comment explaining why, rather than silently
  changing the expected value.

Both are test-maintenance for an intentional, documented API change, not a
weakening of coverage — confirmed by the adversarial run in §7 still
failing correctly against the pre-change behaviour.

## 9. Scope boundary, stated explicitly

This release touches only the coordinators' passive polling cadence.
`services.py`, `number.py`, and `switch.py`'s status-polling loop
(redesigned for Defect V, v1.3.19) are unchanged — a write-verification
read has a legitimate, different need (confirm a just-issued command
promptly) from a passive background poll nobody is actively waiting on,
and conflating the two was deliberately avoided.

## 10. Safety properties

- No change to `ModbusGuard`, the adaptive controller, any coordinator's
  batch/chunk execution logic, or any entity platform file.
- Defects F through W (v1.3.7-v1.3.20) are untouched and still in place.
- SLOW tier's own base TTL (900s, itself a deliberate field-evidenced
  choice — see that constant's history in `const.py`) is unchanged; this
  release only bounds how much *additional* delay back-off can add on top
  of it, not the tier system's own steady-state cadence.
- Zero behavioural change for a coordinator that is never in back-off:
  the entire starvation-tracking path only executes inside the
  `if in_backoff:` branch: `filter_stale()`'s ordinary TTL-based staleness
  check, used every cycle regardless of back-off state, is untouched.
- The FAST TTL change is bounded and small (3.0s) relative to every
  existing coordinator interval (30s minimum) — verified directly, not
  assumed, via the split test in §8.

## 11. Test evidence

- **633 passed, 1 skipped, 0 failed**, deterministic across 3 repeated
  runs (was 615; 18 new tests).
- Adversarial: all 4 applicable static checks fail against the pristine
  pre-session baseline; the full 18-test suite passes against this
  release.
- Static: `py_compile` clean on all three changed files; manifest version
  = 1.3.21.
- Confidentiality sweep: clean.
- Diffed against the v1.3.20 tree to confirm only `register_cache.py`,
  `update_coordinator.py`, and `const.py` changed among production files.

## 12. Recommended deployment procedure

1. Delete `/config/custom_components/huawei_solar` entirely.
2. Extract v1.3.21 fresh into `custom_components/`.
3. Restart Home Assistant once.
4. **Required validation, specific to this release:** the direct test of
   this fix is the field observation that motivated it. Watch
   `BMS temperature` (and the other battery pack temperature registers)
   specifically — under the same kind of sustained contention observed in
   the 107-minute capture that prompted this release, they should now
   never go more than roughly `SLOW_TTL + REGISTER_STARVATION_CEILING_S`
   (currently 900s + 300s = 1200s, 20 minutes, in the worst case a
   registers's own TTL hasn't already adapted downward) without at least
   one successful read — and, in the specific case that motivated this
   release, `BMS temperature` should no longer be capable of going an
   entire multi-hour session without ever being read at all. A
   `%s: promoting %d starved SLOW/STATIC register(s)...` INFO-level log
   line will appear whenever the mechanism actually fires, making this
   directly observable in a debug capture rather than only inferable from
   entity state.

**Verdict:** release-ready. A genuine architectural change, scoped
tightly to the specific failure mode that motivated it, built around an
explicit trade-off the operator stated up front rather than one inferred
or assumed, with a real mistake in an early draft caught before it ever
reached a test and both the mistake and its fix reproduced directly in
the adversarial suite rather than only described in prose.
