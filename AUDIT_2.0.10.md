# huawei_solar 2.0.10 — Release Audit

**Scope:** a genuine production defect (a self-inflicted regression from 2.0.9's own register tier reclassification, found and root-caused via real field data investigation) plus finer-grained per-request instrumentation, added to close a real coverage gap found during that same investigation.

**Discipline applied throughout, unchanged from every prior release:** verify every claim against real source before writing a fix; adversarial test proving the specific failure mode each fix closes; full suite re-run after every change; final verification from a fresh, independent extraction of the packaged zip.

**Final verification:** 1,103 passed, 1 skipped — confirmed identically from a fresh, independent extraction of `huawei_solar-2.0.10.zip`, matching the same known pre-existing baseline (5 failed / 12 errored, documented since 2.0.7) with zero new regressions.

---

## The production defect

### How it was found

A user reported `sensor.power_meter_consumption` (EMMA's `total_energy_consumption` register) showing a stair-step pattern — long flat stretches, then sudden jumps — rather than smooth updates, with gaps of up to ~44 minutes observed directly in a real Home Assistant history export. Investigation proceeded methodically, ruling out several plausible explanations before landing on the real cause:

1. **Ruled out**: the register living on a separate, slower-cadence coordinator — confirmed via source that `create_emma_entities()` explicitly assigns it to the main, fast coordinator, not the slow `configuration_update_coordinator`.
2. **A genuine tangent, caught and corrected**: initially chased a *different* entity (a user-built template sensor depending on `SynchronizedPowerCoordinator`'s own outputs) after a log search matched on a similarly-named but unrelated entity. Recognized as off-topic once the user clarified, and set aside — still a real, separate finding worth acting on later, but not the cause of the reported symptom.
3. **The actual cause**: traced precisely through the real starvation/promotion branching logic in `_execute_batch()`.

### The mechanism

The starvation-promotion safety net — a 90-second ceiling (`ENERGY_PROMOTION_CEILING_S`) that force-rescues an energy counter register that's gone too long without a successful read — only ever covered the SLOW/STATIC code path:

```python
if tier == RegisterTier.FAST:
    priority_names.append(n)
elif tier == RegisterTier.NORMAL:
    if self._backoff_cycle % BACKOFF_NORMAL_DIVISOR == 0:
        priority_names.append(n)
    # nothing else — no starvation tracking, no ceiling, no promotion
else:
    # SLOW/STATIC only: the 90s-ceiling protection lived here
```

2.0.9's own register tier reclassification moved six energy counters (including `total_energy_consumption`) from SLOW to NORMAL, intending fresher data under normal conditions. That change unintentionally removed them from this protection: during back-off specifically (confirmed present and recurring in the field — 17 events across a 6-hour capture), a NORMAL-tier register is simply skipped on 3 of every 4 back-off cycles, with nothing to rescue it if it keeps missing. A stale, pre-existing code comment had explicitly (and incorrectly) claimed NORMAL-tier energy counters were "still protected by the same lengthened availability ceiling regardless of which path they take" — that claim was never actually enforced by any code; the comment itself was corrected as part of this fix.

**Honest limitation, not fully closed by data alone**: `bus_diagnostics.py` (prior to this release) only recorded per-chunk outcomes, not which individual registers were inside a given chunk — so the mechanism, while confirmed correct by tracing the actual branching logic, could not be fully confirmed as *the* cause of this specific register's 44-minute gaps from the available field data alone. The fix proceeds on the strength of the code-level mechanism itself, which is unambiguous, and the new instrumentation (below) closes exactly this observability gap for future investigations.

### The fix

NORMAL-tier registers that are also energy counters (`is_energy_counter()`, the existing authoritative classifier) now get the same starvation check SLOW-tier energy counters already had — but only on a cycle where the register would otherwise be skipped (an `elif`, not a duplicate check), and sharing the exact same `starved` pool and per-cycle promotion cap as the SLOW/STATIC path, rather than a second, separate promotion budget that could double promotion traffic during back-off. Deliberately scoped to energy counters specifically, not every NORMAL-tier register — ordinary NORMAL-tier values don't carry the same freshness expectation that justified the SLOW→NORMAL move in the first place.

**Test coverage**: 6 new adversarial tests, including a negative case confirming ordinary (non-energy-counter) NORMAL-tier registers are completely unaffected, and a check that the stale, incorrect comment is genuinely gone. (Two of these tests initially failed against my own too-narrow search windows — my explanatory comments were longer than the string windows I'd written to check for them; caught immediately via test failure, fixed by widening the check rather than shortening the comment.)

---

## Finer-grained instrumentation

Found during the same investigation: `bus_diagnostics.py` recorded a register *count* per chunk, never which registers. This is precisely the gap that prevented fully closing the loop on the defect above from field data alone.

### What was added

- **`register_names: list[str] | None`** — a new field on `_RequestContext`, threaded through `BusDiagnostics.record()` as a new `regs_l` key (distinct from the existing `regs` count field, so nothing existing changes shape), wired into the real `_execute_batch()` call site. The full list, not a summary — the whole point is being able to see exactly which register was in a slow chunk.

### A second gap, closed in the same pass

While reviewing real field data for this release, `SynchronizedPowerCoordinator`'s own dedicated reads — confirmed to account for **45% of all captured bus traffic** in a real 6-hour capture — were found to carry none of 2.0.9's own per-request attribution fields (`logical_request_id`, `transition_reason`, etc.) at all, only their own label string. This wasn't part of the original defect investigation's scope, but is the same category of gap, found via the same real-data review, and closed in the same release:

- New `_next_logical_request_id` counter on `SynchronizedPowerCoordinator`, generating one ID per update cycle (matching the main coordinator's own established "one ID per poll" convention exactly).
- `_read_one()` (a nested function with closure access to that ID) now sets `logical_request_id` and `register_names` on its own `guard.request()` context, for every one of its four dedicated reads.

**A real test-fixture bug found and fixed along the way**: the existing `_FakeGuard._Ctx.__aenter__` test double returned `None` (an untyped `pass`), which had never mattered before since no caller previously tried to set attributes on the yielded context. Once `_read_one()` started doing exactly that, every dedicated-read test in the suite failed — not because the fix was wrong, but because attribute assignment on `None` raises `AttributeError`, which was then silently swallowed by `_read_one()`'s own broad exception handler and misreported as "all reads failed." Fixed by having the fake yield a `types.SimpleNamespace()` instead — the minimal, dependency-free stand-in that actually supports attribute assignment, matching what the real `_RequestContext` provides.

**Test coverage**: 6 new tests, including a genuine behavioral test (not just source-pattern matching) confirming all four dedicated reads within one update cycle share exactly one `logical_request_id`, and that `register_names` correctly reflects the actual register read.

---

## Final verification

- Every file in the packaged `huawei_solar-2.0.10.zip` compiles cleanly.
- Full suite, run from a **fresh, independent extraction** of that exact zip: **1,103 passed, 1 skipped**, matching the working tree and the established pre-existing baseline exactly — zero drift, zero new regressions.
