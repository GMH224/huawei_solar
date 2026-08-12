# Release Audit — huawei_solar v2.0.4

**Date:** 2026-08-12 · **Auditor:** Claude (Anthropic)
**Baseline:** v2.0.3
**Type:** targeted fix for two findings (F-02, F-03) from a third external
ICS audit, selected specifically because they compromise the reliability
of the telemetry the next architecture decision depends on. The
remaining findings from that audit (F-01, F-04 through F-07) are
deliberately deferred pending a further, longer telemetry capture — see
§3.

---

## 1. Why this release exists, and why only these two findings

A third external audit, incorporating direct analysis of a production
log and a second real telemetry capture, identified seven findings
(F-01 through F-07). All seven were independently re-verified against
actual source and the supplied log/telemetry data before any fix was
written — including one genuine correction to this review's own initial
analysis: `tmodbus` was briefly, incorrectly treated as an external,
out-of-scope vendor dependency partway through verification, when it is
this project's own code from an earlier session. That was caught and
corrected before any conclusion was drawn from the mistaken framing.

Of the seven, two were selected for this release specifically because
they compromise the *instrumentation* the operator's own planned
next step — a longer, more deliberate telemetry capture — depends on to
be trustworthy:

- **F-02** meant SyncPower's startup behavior was worse than intended on
  every single boot and reload, polluting the startup window of every
  future capture.
- **F-03** meant the single most obvious health metric
  (`failure_rate_percent`) could silently read 0.0% during a real,
  ongoing timeout problem — exactly the kind of number someone would
  check first when interpreting a new capture.

Fixing the instrument before trusting what it measures next matches this
project's own established sequencing from the prior remediation round.

## 2. Findings fixed

### F-02 — `async_config_entry_first_refresh()` called outside its valid lifecycle state

**Confirmed exactly**, via a genuine, reproducible traceback in the
supplied log: `ConfigEntryError: async_config_entry_first_refresh
called when config entry state is ConfigEntryState.LOADED, but should
only be called in state ConfigEntryState.SETUP_IN_PROGRESS` — raised
from `__init__.py:494`, inside `_sync_first_refresh()`.

**Root cause, confirmed and owned plainly**: this was introduced by this
project's own v2.0.0b MOD-03 fix. That fix deliberately deferred
SyncPower's first refresh to a background task — specifically so it
would not block the setup critical path — without accounting for the
fact that `async_config_entry_first_refresh()`'s own contract requires
the entry to still be in `SETUP_IN_PROGRESS`. Since this background task
only ever runs *after* `async_setup_entry()` has already returned and
the entry has transitioned to `LOADED`, the call was destined to fail on
every single invocation from the moment that deferral was introduced —
not an intermittent or conditional failure.

**Practical impact**: caught by the integration's own exception handler
(no crash), but SyncPower's synchronized power-flow sensors never
actually received a genuine first refresh — they reported `unknown`
until the *next* regular scheduled poll (10 seconds later) on every boot
and every reload, rather than getting the deliberately-staggered fast
first read MOD-03 was originally built to provide.

**Fix**: replaced with `async_request_refresh()` — the ordinary,
valid-after-setup refresh mechanism every other coordinator in this
integration already uses for its own non-first polls. Unlike the
setup-only API, this does not raise on failure at all (it records the
failure on the coordinator itself and notifies listeners); the existing
`try`/`except` around the call is kept regardless, as a defensive
measure consistent with this background task's own "must never raise"
contract, not because the new call is expected to need it.

### F-03 — `failure_rate_percent` silently blind to timeout-only failure patterns

**Confirmed, with exact numbers matched against the actual supplied
telemetry capture** before any fix was written: `record_timeout()`
always increments `total_failures` (the lifetime counter) but only ever
appends to `self._timeouts`, never `self._failures` — and the rolling,
windowed `failure_rate_percent` is computed from `self._failures` alone.
The supplied capture showed exactly the masking this produces: one
device with 24 timeouts and 24 total failures reporting `failure_rate_
percent: 0.0`; a second device with 7/7/0.0 — both devices' entire
failure history was timeouts, and the single most obvious health metric
showed a clean bill of health regardless.

**Fix, deliberately not a silent redefinition**: `failure_rate_percent`
keeps its existing meaning (non-timeout failures only) unchanged — per
the audit's own explicit recommendation, and consistent with this
project's own established discipline elsewhere (v2.0.0a F16 and others)
of never changing an existing field's semantics out from under whatever
already consumes it. Two new, unambiguously-named fields close the blind
spot instead: `timeout_rate_percent` (timeouts as a fraction of
requests) and `overall_failed_attempt_rate_percent` (both failure types
combined) — the number that would have directly surfaced the masking in
the real capture.

## 3. Findings deliberately not addressed in this release

**F-01** (transaction-boundary desynchronization): confirmed as a real,
reproducible transport-layer event — `tmodbus`'s `send_and_receive()`
pops a transaction's pending-request entry in its own `finally:` block
when `asyncio.wait_for()` times out; a device response arriving after
that point is correctly (though wastefully) identified as unmatched and
discarded. Traced through carefully before deciding this: the practical
corruption risk (a late response being mismatched to a *different*,
newer request) is low in practice, bounded by the 16-bit transaction ID
wraparound window (65,536 requests) relative to the observed few-second
gap between a timeout and its late response — this is real, wasted
device effort and a signal of timeout/response-time miscalibration, not
an active data-corruption risk today. Per the agreed sequencing, the
minimal first step (an explicit `transport_desync_count`-style counter,
so the next capture can show the actual *rate* of this happening) is
deferred to the same telemetry-driven decision point as F-04 through
F-07 and the already-parked ICS-12/ICS-16, not built speculatively here.

**F-04, F-05, F-06**: confirmed as accurate descriptions of existing,
already-documented, deliberate architectural trade-offs (e.g. F-04's own
code comment already states that holding the SyncPower guard across the
whole read sequence was "considered and rejected" to preserve
cross-coordinator fairness) — not newly discovered defects. These sit at
the same coordination layer as the already-deferred Physical Demand
Planner question and stay grouped with it.

**F-07** (downstream `unknown` propagation into HA templates): per the
audit's own assessment, this is a consuming-template responsibility, not
an integration defect — recommended to the operator as a documentation
note (`| default(0)` on the affected templates) rather than a code fix.

## 4. Test evidence

- **877 passed, 1 skipped, 0 failed** (was 872 at the v2.0.3 baseline; 6
  new tests: 1 structural check for F-02 rewritten to also positively
  confirm the new call and negatively confirm the old one is gone, and 5
  behavioral tests for F-03, including one that reproduces the real
  capture's exact 24-timeout scenario directly).
- The same self-inflicted false-positive pattern this whole session has
  hit repeatedly recurred once more while writing F-02's test: an
  explanatory comment describing the *old* behavior for context happened
  to contain the exact string a naive `assertNotIn` was checking for.
  Caught immediately by the test failing with a legible, obviously-wrong
  reason, and fixed by checking the precise call-syntax pattern
  (`await coord.async_config_entry_first_refresh()`) rather than the
  bare method name a comment could also plausibly contain.

## 5. Safety properties

- v2.0.3 remains available and was not modified; this release was built
  in its own working tree.
- No architectural changes — both fixes are narrow, targeted corrections
  to existing, specific defects.
- `failure_rate_percent`'s existing semantics are explicitly,
  deliberately unchanged — verified with a dedicated negative test.

## 6. Recommended next step

Deploy 2.0.4. The operator's own plan continues exactly as agreed: run
the next, longer telemetry capture against this release specifically
(SyncPower's startup behavior and the failure-rate metrics are now
trustworthy from the very first snapshot), then decide F-01's transport-
epoch question together with F-04 through F-06, ICS-12, and ICS-16 from
that data — not before it.

**Verdict:** both selected findings fixed and adversarially tested
against the real implementation, including reproduction of the exact
real-world scenario that surfaced F-03. Five findings deliberately
deferred, with their reasoning recorded here, not silently dropped.
