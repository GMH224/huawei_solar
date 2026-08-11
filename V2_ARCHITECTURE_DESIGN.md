# v2.0.0 — Quality-Aware Modbus Telemetry Architecture

**Status: DESIGN COMPLETE, including a final architectural sweep for
interactions with unchanged subsystems (§10) — not yet implemented.** This document is the
full record of the architectural redesign agreed following the discovery
that v1.3.21 (and every prior version) conflates *transport/link health*
with *sensor/payload health* at the data-model level. It exists so this
design does not have to be reconstructed or re-argued later — every
decision below was reached deliberately, several after real investigation
(source verification, literature/standards research), not by default.

**v1.3.21 is the last version of the old architecture and remains
untouched, deployed, and stable.** This is a clean-break rewrite, not an
incremental patch — hence the major version bump. Backward compatibility
of historical sensor *data* and entity IDs is explicitly NOT a design
goal (see §7).

---

## 1. The problem, precisely

Home Assistant's `CoordinatorEntity` availability model, as used
throughout this integration, collapses two fundamentally different
situations into one binary flag:

1. "This sensor's underlying payload is genuinely unknown" (never read,
   or known to have changed since our last confirmation).
2. "The physical/transport link to the device is currently degraded, but
   the last value we have is probably still true."

Traced to its exact mechanism in v1.3.21: `_CacheEntry.dirty` is a single
boolean used for both write-invalidation ("we changed this, the old value
is *known* wrong") and reconnect-invalidation ("the link dropped, we
*don't know* if this changed"). `RegisterCache.merge()` treats both
identically — a dirty-and-unrefreshed register is dropped from
`coordinator.data` entirely rather than served as a stale-but-probably-true
value, and the entity goes `Unknown`.

This was traced independently against real source (not accepted from an
external report's framing) and confirmed as a genuine, reachable defect,
not a theoretical concern — it explains a meaningful share of the
`Unknown` pattern observed across many field captures this session,
separate from and in addition to genuine bus contention.

## 2. Why the fix is a rebuild, not a patch (Decision, with reasoning)

Three options were considered:

- **Patch `merge()` alone** (split `dirty` into write-dirty vs.
  link-dirty, serve the latter). Rejected: this makes the *symptom*
  (blank sensors) go away without making the underlying data honestly
  quality-aware. Critically, this would have made the parallel Phase 1
  battery-health work (see `PHASE1_BATTERY_HEALTH_DESIGN.md`) *more*
  wrong, not less — a segment-based capacity estimator that infers
  changes between readings needs to know whether a reading it just
  consumed was fresh or stale-served, and a silent patch would hide that
  distinction rather than expose it.
- **Full rewrite with genuine quality semantics** (this document).
  Chosen. Larger scope, but the actual correct fix for the actual
  diagnosed problem.

## 3. Severity model

Three severities, deliberately fewer than OPC UA's full `StatusCode`
taxonomy — the *vocabulary* is borrowed (this integration's design
explicitly draws on OPC UA's `Good`/`Uncertain`/`Bad` distinction,
verified against the real OPC UA specification, not assumed), but scoped
down to exactly what this integration's real failure modes need.

| Severity | Meaning |
|---|---|
| **GOOD** | Read succeeded within its tier's current cadence. Fully current. |
| **UNCERTAIN** | A real, previously-confirmed value exists. We currently cannot verify it's still accurate — but nothing indicates it's wrong. Directly maps to OPC UA's `Uncertain_LastUsableValue`. |
| **BAD** | No usable value. Either never read, known-wrong (write issued, not yet reconfirmed), or so old even "probably still true" no longer applies. |

**The distinction that matters most:** BAD means "positive reason to
distrust this." UNCERTAIN means "simply can't currently check." Collapsing
these was the root cause being fixed.

## 4. Timestamp model — a deliberate deviation from OPC UA

OPC UA separates `SourceTimestamp` (when the device produced the value)
from `ServerTimestamp` (when the gateway received it). **Decision:
adopt a single timestamp** ("when we last successfully cached this
value"). Reasoning: this integration's polling architecture (synchronous
batch reads over TCP/RTU, no device-side buffering we have visibility
into) cannot honestly populate a distinct source timestamp — inventing
one would be false precision, not rigor. Copying a standard's structure
without the ability to fill it in honestly was explicitly rejected.

## 5. Failure-reason vocabulary — consolidated across three subsystems

Prior to this design, `ModbusGuard`, the coordinator's back-off logic, and
`RegisterCache` each had partial, disconnected notions of "why didn't
this register get a fresh value this cycle." Consolidated into one
canonical vocabulary, **owned by the cache** (where quality is stored),
not duplicated across the other two:

**No degradation** (nothing recorded, quality carries forward unchanged):
- Register simply wasn't due yet (TTL hasn't elapsed).

**`UNCERTAIN` reasons** (an attempt was warranted, but didn't complete —
or was deliberately skipped for pacing):
- `SHED` — `ModbusGuard` itself declined to admit the request (already a
  distinct exception type, `ModbusQueueShed`, not conflated with a
  generic timeout, in the existing codebase).
- `BACKOFF_DEFERRED` — tier-based deferral during back-off, or the
  starvation-ceiling logic didn't promote this register this cycle.
- `TIMEOUT` — request sent, no response within budget.
- `LINK_DOWN` — keep-alive-detected connection loss, or an exchange
  failing due to a dead socket.
- `DEVICE_BUSY` — the device explicitly responded "busy" (Modbus
  exception code 0x06) — a distinct, real signal from a generic timeout
  (the device is alive and responding, just overloaded). **Detection
  mechanism confirmed against real source, not left open**: `ReadException`
  already carries an optional `modbus_exception_code` attribute; the
  existing BUSY-retry logic already checks
  `getattr(exc, "modbus_exception_code", None) == _EXC_SLAVE_DEVICE_BUSY`
  (`update_coordinator.py`, already shipped in v1.3.21) for exactly this
  purpose. `record_attempt()`'s `DEVICE_BUSY` path reuses this identical
  check — not a dedicated exception subclass, a generic exception with an
  optional attribute, confirmed by reading the existing working code
  rather than assumed.

**`BAD` reasons:**
- `NEVER_READ` — no cache entry exists at all.
- `WRITE_PENDING` — invalidated by our own write, not yet reread (see §6
  for why this is BAD, not UNCERTAIN, despite what OPC UA's own
  convention might suggest).
- `EXPIRED` — a pure function of elapsed time: an `UNCERTAIN` entry that
  has aged past `REGISTER_STARVATION_CEILING_S` (the same constant
  already shipped and field-validated in Defect Y, v1.3.21 — reused
  deliberately rather than inventing a second, disconnected threshold).

### 5.1 Architectural placement

`ModbusGuard` and the coordinator's back-off logic do not need to know
about `GOOD`/`UNCERTAIN`/`BAD` at all — they keep owning what they already
own (admission control, pacing). **The coordinator becomes the
translator**: at the exact point it already distinguishes `ModbusQueueShed`
vs. `TimeoutError` vs. a device-busy response (today, only to update its
own aggregate `_consecutive_timeouts`-style counters), it now *also*
reports the outcome per-register into the cache via a new method —
`record_attempt(names, quality, reason, now)` — called once per chunk
outcome (a Modbus read succeeds or fails atomically for the whole
register range requested together).

**`BACKOFF_DEFERRED` requires an explicit call even though nothing was
sent** — silently leaving an entry unchanged and explicitly recording
"deliberately skipped, here's why" look identical in the stored *value*
but materially different in what a consumer sees in `reason`/`age`.
Explicit recording is what makes deferral genuinely visible rather than
merely inferred by absence.

### 5.2 `_execute_batch()` restructuring — traced against real source, not assumed

Before committing to `record_attempt(names, quality, reason, now)`'s
signature, the actual current control flow was traced precisely, since a
wrong assumption here would have required a second, disruptive change
later once real hardware exposed it.

**Finding 1 — the specific failing chunk's register names are NOT in
scope at the point failure is currently recorded.** In v1.3.21,
`_execute_batch()`'s per-chunk `try/except` (where `chunk`, the specific
failing register-name list, *is* in scope) re-raises on non-retryable
failure; the exception propagates out of the entire function and is only
caught by the caller, in `_async_update_data()`. At that point, the only
list in scope is `stale_names` — every register requested that whole
cycle, across every chunk, not the specific one that failed. Recording
quality at the caller (as the plan originally assumed) would have
incorrectly marked registers whose chunk had already succeeded moments
earlier in the same batch with the same failure reason as the chunk that
actually failed.

**Finding 2 — a second, related defect the same trace surfaced.** Because
the exception propagates out of the whole function, `merged` (the dict of
already-successful chunk results, built up before the failing chunk) is a
local variable that is never returned. Already-fresh results from earlier
chunks in the same poll are silently discarded on a later chunk's
failure, falling back to a previous cycle's cached value instead of the
one just successfully read moments before. Not the primary quality-tracking
issue, but the same restructuring needed to fix Finding 1 fixes this too.

**Resolution:** `_execute_batch()`'s control flow changes from
"exceptions propagate, caller handles generically" to "each chunk's
outcome is recorded at the exact point it's known, inside the loop."
`record_attempt(chunk, ...)` is called inline, per chunk, where `chunk`'s
specific names are genuinely in scope — replacing the bare `raise` at the
non-retryable-failure point.

**Decision: best-effort per chunk, not abort-on-first-failure.** This
naturally raised a real behavioural question, not just a refactor: should
one chunk's failure still abort every remaining chunk in that cycle's
batch (matching v1.3.21's existing behaviour), or should later chunks
still be attempted regardless? **Decided: best-effort** — a failing chunk
does not stop subsequent chunks from being attempted, maximising how many
registers get a fresh read even during partial trouble. Consistent with
this session's general direction (reducing unnecessary staleness) rather
than preserving the old all-or-nothing semantics by default.

## 6. The `WRITE_PENDING` question — researched, not assumed

Investigated directly against how OPC UA is actually implemented on a
real, current "gold standard" (Siemens S7-1200/1500), not assumed by
analogy. Finding: the S7 OPC UA server is a thin, **synchronous**
interface directly over live PLC memory — a `Write` request writes
directly into the same memory a subsequent `Read` immediately reflects.
Siemens's architecture essentially does not have a "just wrote, not yet
confirmed" gap to design for, because writing *is* the confirmation.

This integration's write path is structurally different — a command sent
over a shared, contended, comparatively slow bus to a remote device, with
a real, sometimes multi-second gap before independent confirmation is
possible. **Decision: `WRITE_PENDING` stays `BAD`, not `UNCERTAIN`**,
specifically because of this structural difference — showing a user the
pre-write value immediately after they commanded a change risks reading
as "the command didn't register," a worse outcome than a clearly-marked
pending state. OPC UA's own convention (keep showing stale values while
uncertain) was determined not to be a real precedent here, since it was
designed for a different case (remote read uncertainty) that this
integration already maps correctly to `LINK_DOWN`/`TIMEOUT`/etc.

## 7. Persistence and backward compatibility (Decision)

- **Sensor values are NOT persisted across restarts.** Every register is
  legitimately `NEVER_READ` after a fresh start — no `RestoreEntity`-style
  "show the last value from before restart" carryover. This is simpler
  than the old model, not a regression: the new state accurately
  describes what's true (nothing has been read yet), rather than showing
  a value of unknown remaining validity.
- **Adaptive learning (bus timing model) DOES continue to persist** —
  this is a fundamentally different kind of data (a learned model of
  system behaviour, not current sensor state) and there is no reason to
  discard it on principle. However: **the accumulated ~72 days of
  existing learning data will NOT be migrated** to whatever new
  persistence format 2.0.0 uses. Explicit, deliberate decision: the
  underlying Modbus logic changed too many times over the preceding weeks
  for that historical data to be considered trustworthy anyway. Adaptive
  learning starts fresh; the system will run conservatively for a
  settling period post-upgrade until it relearns.
- **Historical sensor data (InfluxDB/MSSQL) is explicitly NOT a migration
  concern.** The operator has independently assessed that pre-2.0.0 data
  is unusable for the ML/training-data goal this data pipeline exists
  for, given how much the Modbus layer changed during this session. No
  migration tooling will be built.
- **Entity IDs and `unique_id`s are explicitly NOT guaranteed stable**
  across the 1.3.21 → 2.0.0 upgrade. Accepted trade-off: dashboards and
  automations referencing specific entity IDs will need manual
  reconfiguration after upgrade. Explicitly chosen in favour of clean
  architecture over migration-driven compromise.
- **Device connection configuration (host, port, slave IDs, login
  credentials) IS preserved — and this is a genuinely different
  guarantee from the ones above, not an extension of "we don't care about
  migration."** This data lives in Home Assistant's own
  `config_entries` storage (`.storage/core.config_entries`), owned by HA
  core, not by this integration — replacing the integration's files on
  disk and restarting does not touch it. The only real risk is v2.0.0
  changing the *shape* of that stored data (renamed keys, restructured
  options) without a proper migration handler, which could cause the new
  code to misread an otherwise-intact entry. Decision: **leave
  `config_flow.py`'s data/options schema unchanged unless there is a
  specific, deliberate reason to extend it** (e.g. a new user-configurable
  threshold analogous to the existing SLOW-tier TTL option); any such
  addition goes through HA's standard `async_migrate_entry()` versioning
  with a sensible default for existing entries, never a forced
  reconfiguration. This is a stronger, structurally different guarantee
  than the sensor-data/entity-ID decisions above, not merely a smaller
  version of "don't worry about it."

## 8. Entity layer

- **`available` follows one rule, uniformly, across every entity
  platform** (sensor, number, select, switch — not different logic per
  platform): `True` for both `GOOD` and `UNCERTAIN` (a servable value
  exists in both cases); `False` only for `BAD`.
- Writable entities (number/select/switch) follow the same rule — staying
  available under `UNCERTAIN` is correct even for interactive entities,
  since HA does not gate write actions on "is the currently displayed
  value fully fresh," and there's no reason this integration's model
  should either.
- **Exposed via `extra_state_attributes`:**
  ```
  data_quality: "good" | "uncertain" | "bad"
  data_quality_reason: <reason code>   (omitted when good)
  data_age_seconds: float
  ```
- **Side effect, not a separate design goal, but real:** because
  `available` stays `True` through `UNCERTAIN`, the recorder keeps
  writing the retained numeric value during transient link blips instead
  of an `unavailable` gap — directly addressing the Energy Dashboard /
  statistics-corruption concern raised by the external DLSTE-style report
  reviewed during this investigation, as a natural consequence of the
  design rather than something requiring separate handling.

### 8.1 Energy counters — superseded twice since the original design pass; this is the final policy

Cache-level severity computation stays uniform for every register type —
an energy counter under `LINK_DOWN` is `UNCERTAIN`, exactly like anything
else; the underlying data-quality fact doesn't depend on what kind of
sensor consumes it. What differs is entity-level policy on top of that.
This section's policy changed twice after the original design pass, each
time for a real, evidenced reason — recorded here in full rather than
silently overwritten, since the reasoning that got superseded is still
worth understanding.

**Original policy (this section, first draft):** treat `UNCERTAIN` as
unavailable for energy counters specifically, continuing the Defect Q
precedent (v1.3.15) — a stale cumulative counter risks a fresh read later
appearing as a misattributed surge.

**First refinement (during implementation, §5.2 interaction):** not a
blanket per-register-type exclusion — with best-effort chunking, a
specific energy counter's own chunk can genuinely succeed even when the
overall cycle is reported as failed due to some unrelated chunk. Only
withhold when that specific register's own `quality_of()` isn't `GOOD`.

**Second, larger revision (hardware-constraint pass): the "unavailable
whenever not fully fresh" framing itself was wrong, not just its
granularity.** The operator identified a hard constraint this section had
not accounted for: for energy specifically (not power), a *gap* feeding
the Energy Dashboard's hourly rollup is worse than a *delayed* value — a
missing reading breaks the calculation outright; a late one doesn't.
Checked directly against how Home Assistant's statistics engine actually
works (not assumed) before designing around it:

- The `sum` column's delta is computed purely as `new_state -
  previous_state` — NOT time-weighted. A genuinely fresh reading after any
  gap, however long, still captures the exactly correct real cumulative
  growth. Staleness itself does not corrupt the delta's eventual
  correctness.
- Home Assistant's recorder already refuses to write a new `states` row
  when the value hasn't changed (confirmed directly, not assumed) — so
  repeatedly serving an unchanged stale value costs nothing extra; it's
  already deduplicated for us. The originally-proposed "suppress redundant
  writes" mechanism was consequently unnecessary and dropped.
- The REAL risk is narrower than the original framing: short-term
  statistics are compiled on a **fixed 5-minute clock**, sampling whatever
  the current state happens to be at that instant, regardless of when it
  was last genuinely updated. A value sitting flat across *multiple*
  5-minute boundaries, then jumping once a fresh read finally lands, has
  that whole jump attributed to the single 5-minute window in which it
  was observed — not spread across the real elapsed time. This is a real
  risk, but it scales with gap LENGTH, not with staleness as a binary
  property. A 90-second gap crosses at most one boundary (negligible); a
  20+-minute gap (genuinely observed this session, pre-Defect-Y) crosses
  four or more (real, meaningful misattribution).

**Final policy — a bounded, two-stage design, not a single threshold:**

1. **A tightened, energy-specific promotion ceiling**, extending Defect Y's
   existing starvation-promotion mechanism with a shorter fuse for
   energy-relevant registers specifically: **60-90s past due** (roughly
   2-3x the base NORMAL-tier TTL), rather than the generic 300s ceiling.
   First line of defence — resolve contention quietly, before it's ever
   visible to a user.
2. **An availability ceiling of 600s (10 minutes) — two short-term
   snapshot windows.** Below this, an energy counter stays available even
   at `UNCERTAIN` quality, directly honouring the hard constraint (a
   delayed value beats a gap). Above it, fall back to unavailable rather
   than risk a jump spanning many snapshot windows — bounding the
   worst-case misattribution to "the two most recent windows absorb it,"
   not "one window absorbs an hour's growth." Reasoned from the 5-minute
   snapshot mechanism directly, not picked arbitrarily — though, unlike
   the snapshot interval itself (a fixed HA fact), the choice of exactly
   two windows is a judgement call, open to revision against real
   field data.

This should rarely reach stage 2 in practice: stage 1 is designed to
resolve the great majority of contention well within the 600s ceiling,
matching the same layered philosophy as Defect Y itself (a promotion
mechanism as the common-case fix, an absolute ceiling as the rare-case
backstop).

### 8.2 Synchronized power — skip the dedicated read when the cache is already aligned

The follow-up flagged in the §10.1 addendum, now designed concretely
rather than left as a named idea. `SynchronizedPowerCoordinator` currently
always performs its own dedicated, guard-serialized 4-register read.
With per-register age now available cheaply from the regular cache, it
doesn't have to.

**Mechanism:** before issuing its own read, check `quality_of()` and age
for all four registers (`inv1_pv_power`, `inv2_pv_power`, `grid_power`,
`battery_power`) in the regular cache. If all four are `GOOD` and their
ages span no more than the tolerance below, use the cached values
directly and skip the dedicated read entirely; otherwise, fall back to
today's behaviour unchanged.

**Tolerance: 3 seconds**, derived from the operator's hardware
constraints rather than picked arbitrarily:
- The device's Modbus register itself only refreshes at 1 Hz — readings
  within roughly a second aren't merely "close," they're the literal same
  underlying value. 3 seconds is comfortably wider than that floor, so
  the check never rejects an alignment the hardware couldn't have beaten
  anyway.
- The four devices are not phase-locked (no PTP), but each device's own
  internal sampling loop runs at ~40-50 Hz — the resulting cross-device
  jitter this could add is on the order of 20-25 ms, negligible against a
  3-second tolerance.
- Sensor accuracy is Class 1.0 (±1%) for power/energy on three of the four
  channels (the power meter is utility-grade, tighter). A few seconds of
  age-spread is well inside what the sensors themselves could resolve as
  materially different moments — chasing tighter alignment than this
  would be solving a precision problem the hardware can't back up, the
  same principle that ruled out OPC UA's dual-timestamp model in §4.
- Cross-checked against the dedicated read's own real-world performance
  (`sample_span_ms`, Defect V/Finding 9): a healthy dedicated read
  typically already achieves sub-second spread, but is still four
  sequential guard-serialized round-trips with its own real, measured
  spread — 3 seconds keeps the cache-derived path from ever accepting
  materially worse alignment than what it's replacing.

### 8.3 Battery health polling cadence — real headroom, deliberately not designed on a guess

Flagged during this pass, not designed in it. Now that
`battery_health_manager.py`'s value extraction is quality-gated (§10.4 —
a stale-served reading is already correctly treated as `None`, never
silently trusted as current), the tight polling that used to compensate
for that risk at the polling layer is likely redundant: the same
protection now exists at the data layer instead. Discharge segments span
minutes to hours, not seconds, so the current NORMAL/SLOW tier cadence is
plausibly tighter than the segment tracker actually needs.

**Deliberately not given a specific number here.** Unlike §8.1 (a hard
constraint) and §8.2 (clean hardware-derived math), this benefits from
watching real segment-tracker behaviour at the current cadence first, so
any relaxation is tuned against observed data rather than a guess. Tracked
as a genuine, believable follow-up — see also `PHASE1_BATTERY_HEALTH_DESIGN.md`
for the related, still-parked capacity-normalisation work this would sit
alongside.

## 9. Deployment topology — decided, with reasoning preserved

Considered: keep entirely inside the HA/HACS process, vs. a standalone
service (e.g. on the operator's existing Proxmox/EPYC infrastructure).

**Decision: build inside HACS first.** Reasoning:
- The quality/reason vocabulary above is fully portable regardless of
  where it eventually runs — none of §3-§8 changes based on deployment
  location.
- Building it in HACS first fixes the concrete, current problem (the
  motivating example: a `Battery_SOC` reading that's probably still true
  during a bus blip, currently shown as flatly `Unknown`) without waiting
  on a separate, larger infrastructure decision.
- A standalone service remains the more architecturally correct
  *long-term* destination (a persistent connection outside HA's own
  lifecycle would eliminate a whole class of defect this session spent
  significant effort mitigating rather than eliminating — Defects K, L,
  M and others are all downstream of HA's own restart/reload tearing down
  the Modbus link). The operator's Proxmox/EPYC environment substantially
  reduces the two real costs of that path (network-partition risk is
  much smaller VM-to-VM on one host than over a real network; operational
  overhead is negligible given an existing practice of running many VMs).
  Deferred, not rejected — a distinct, larger initiative to revisit once
  the vocabulary itself is proven inside HACS.
- Separately, the operator has an independent security-architecture
  reason to keep HA in place for roughly a 2-year horizon regardless of
  this integration's own architecture: HA does not fit the operator's
  enterprise security stack (no 802.1x, no XSIAM, no clean web/app/DB
  separation), and the actual plan is a pull-only data path (an external
  SQL Server job polling HA's InfluxDB export, HA treated as an untrusted
  source) rather than trusting HA directly — a legitimate, deliberate
  security pattern (comparable to a data historian pull or a data-diode
  design), not a compromise. This reasoning is independent of the
  Modbus/OPC-UA architecture question above but was recorded here since
  it further supports the same "build in HACS now" conclusion via a
  completely separate line of argument.
- **Data pipeline note, unrelated to this integration's own code but
  worth preserving:** for the new `data_quality`/`data_quality_reason`/
  `data_age_seconds` attributes to actually reach the operator's MSSQL
  training-data goal, two external configuration points need explicit
  verification once these attributes exist: (1) the HA Recorder's `states`
  table (not the aggregated `statistics` tables, which have no room for
  attributes at all) needs to be the source the ingestion job queries,
  with retention extended appropriately; (2) the InfluxDB integration's
  own config needs to explicitly include these attribute keys, since
  attribute passthrough is not automatic by default. Neither is this
  integration's responsibility to implement, but both would silently
  defeat the point of this whole redesign for the ML-training use case if
  left unchecked.

## 10. Scope boundaries and interaction with unchanged subsystems — final architectural sweep

A deliberate final pass, checking the design against real source for
interactions with subsystems this rebuild does NOT touch, before
declaring the concept finished. Three findings:

### 10.1 Two coordinators never touch `RegisterCache` at all — explicitly out of scope, not forgotten

`SynchronizedPowerCoordinator` reads directly via `guard.request()` with
zero references to `self.cache` anywhere in that file. The optimizer
coordinator does the same, returning `dict[int, OptimizerRealTimeData]`
— a different shape entirely (keyed by optimizer ID, not `RegisterName`).
The quality model as designed does not reach either.

**Decision: this is a stated scope boundary, not a gap to close in this
rebuild.** `SynchronizedPowerCoordinator` already has its own honest
best-effort caveat (`sample_span_ms`, Defect V/Finding 9, v1.3.19) serving
a related but distinct purpose (measuring time-skew across a
near-simultaneous multi-register sample, not per-register link quality).
The optimizer coordinator is moot for this operator's installation (no
optimizers present) but would need its own extension of this design if
ever brought into scope for installations that have them. v2.0.0's core
rebuild covers the four `RegisterCache`-backed coordinators (main,
battery, power meter, config) explicitly; these two are named as
deliberately excluded.

**Verified this boundary is actually clean, not just asserted**: §5.2
restructures `_execute_batch()`, the method used by every in-scope
coordinator. Checked directly whether the optimizer coordinator also
inherits or calls it — it does not (`HuaweiSolarOptimizerUpdateCoordinator`
is a separate class with its own inline request logic; the existing code
already comments "Optimizer measures rtt_ms directly, not via
_execute_batch"). `SynchronizedPowerCoordinator` lives in a different
file entirely. §5.2's restructuring cannot leak into either coordinator
this section excludes — confirmed, not assumed from the two sections
simply not mentioning each other.

**Addendum (raised during implementation): a genuine follow-up
optimization for `SynchronizedPowerCoordinator`, deliberately not folded
into this rebuild.** Quality/age and time-alignment are different
problems, even though they sound related, and the new quality model does
NOT make this coordinator's reason for existing obsolete. `quality_of()`
answers "is this individual reading trustworthy right now" per register;
`SynchronizedPowerCoordinator` answers "do several individually-trustworthy
readings actually describe the same moment." Concretely: on a stable,
sunny midday, PV power may not have changed in a while, so its adaptive
TTL correctly stretches -- it could be genuinely 2-3 minutes old and still
fully `GOOD`. Grid power, fluctuating with household load, gets re-read
roughly every 30s. Combining a 3-minute-old PV reading with a 10-second-old
grid reading to compute something like home consumption can be
meaningfully wrong even though neither individual reading is degraded by
any quality check -- adaptive TTL stretching (a deliberate, good feature
for reducing bus load) actively works against time-alignment across
different registers, since each stretches independently based on its own
volatility. So this coordinator's dedicated, guard-direct read still has
a real job quality tracking doesn't replace.

What IS new, and worth a real follow-up: `SynchronizedPowerCoordinator`
could check the *ages* of the regular cache's values first -- cheaply, no
Modbus traffic -- and see whether they already happen to be well-aligned
this cycle (e.g. everything genuinely read within the last few seconds
because normal polling happened to line up). If so, it could skip its own
dedicated synchronized read entirely and just use the cache; only fall
back to a real synchronized read when the cache is poorly aligned. A
concrete, genuine way to reduce Modbus traffic using information that
simply did not exist before this rebuild -- but new scope, not a small
addition to work already mid-implementation, and deliberately deferred
as a named follow-up rather than folded in here, the same treatment given
to battery health's deferred efficiency-normalization work.

**Update: designed concretely in the hardware-constraint pass that
followed.** No longer just a named follow-up — see §8.2 for the actual
mechanism (check quality + age of all four registers before the dedicated
read) and the 3-second tolerance, derived from the operator's real
hardware constraints (1 Hz register refresh, Class 1.0 sensor accuracy,
no inter-device phase lock) rather than picked arbitrarily.

### 10.2 Coordinator-level back-off and per-register quality are deliberately separate layers

Checked specifically because the two could look redundant to someone
encountering the code fresh: the existing `_consecutive_timeouts`-driven
back-off state machine governs *whether an attempt is even made*; the
quality model reports *what is known, given what happened*. These are
complementary, not duplicated — back-off decides the action, quality
reports the outcome of whatever action was taken (or explicitly records
that none was, via `BACKOFF_DEFERRED`, per §5.1). Recorded here explicitly
so this separation is not "simplified" away by a future reader who
doesn't have this context.

### 10.3 `EXPIRED` must not apply to `STATIC` tier — a real semantic bug caught in this sweep, not a hypothetical

`STATIC` tier's entire purpose is registers that are genuinely immutable
within a session (serial number, model name) — its adaptive TTL already
grows up to 86400s (24h) precisely because re-reading them is pointless.
But `REGISTER_STARVATION_CEILING_S` (§5, 300s) is a single flat constant
applied uniformly regardless of tier. Left unaddressed, a STATIC register
that simply hasn't needed re-reading recently would be marked
`BAD/EXPIRED` after roughly an hour of not being touched — for a value
that is, by definition, not expected to change at all. Treating hardware
identity as "no longer trustworthy" purely because time passed is
semantically wrong in a way it isn't for FAST/NORMAL/SLOW tiers, where
genuine change over time is real and staleness represents genuine risk.

**Decision: `STATIC`-tier registers are exempt from the `EXPIRED`
transition entirely.** They can still become `UNCERTAIN` if genuinely
unverifiable (e.g. never successfully read at all yet), but do not
auto-escalate to `BAD` purely from elapsed time. `get()`'s `EXPIRED`
computation (§5, the lazy-on-read check against
`REGISTER_STARVATION_CEILING_S`) must check `entry.tier != RegisterTier.STATIC`
before applying it — mirroring the existing precedent for exactly this
kind of tier-conditional exemption already in `register_cache.py`
(`filter_stale()` already special-cases STATIC tier's interaction with
night mode at line 413 and dirty-invalidation at line 525 — this is the
same category of exemption, not a novel pattern).

### 10.4 The value interface and the quality interface stay separate — but not for every consumer identically

Checked how many places in the codebase directly consume `coordinator.data`,
assuming its current shape (`dict[RegisterName, Result[Any]]`):
`number.py`, `select.py`, `sensor.py`, `switch.py`, and
`battery_health_manager.py` (via its `_value()` helper, which explicitly
extracts `.value` from an expected bare `Result`) — five files, all
reading the primary data path directly.

An earlier sketch of the technical shape (not written into this document
until now) proposed `merge()` return a new `TelemetryPoint` object
(value + quality + reason + age bundled together) in place of the bare
`Result`. Checked against the finding above, this would have required
rewriting the value-extraction logic in all five consumers — not because
their actual job changed, but purely because the wrapper type around it
did. A significantly wider ripple than §5.2's finding, which touched one
function.

**Decision: `coordinator.data` stays exactly `dict[RegisterName, Result[Any]]`,
completely unchanged.** Every existing consumer keeps working with zero
modification to its read logic. Quality/reason/age are exposed through a
**separate, additive, opt-in accessor** on the cache —
`quality_of(name) -> (Quality, Reason | None, float | None)` — called
only by whichever entity code specifically chooses to expose
`data_quality`/`data_quality_reason`/`data_age_seconds` attributes (§8).
`night_mode.py` and services' write-verification logic never need to
touch quality at all, since they only ever needed the value.

**Correction, caught in the third sweep — `battery_health_manager.py` is
NOT part of that "unchanged" list, and an earlier draft of this section
was wrong to include it.** Cross-checking this document against
`PHASE1_BATTERY_HEALTH_DESIGN.md` directly (not just re-reading this
document in isolation) surfaced a genuine contradiction: that document's
own opening paragraph states, in effect, that a segment-based estimator
inferring capacity from *changes between sequential readings* is exactly
the consumer most exposed to silently treating a stale, connection-blip-
affected reading as current — and names this as the specific reason
Phase 1's code was reverted rather than shipped, and the founding
motivation for this entire rebuild. Putting `battery_health_manager.py`
on the same "just needs the value" list as display entities directly
contradicted the reason this redesign exists.

The distinction that actually matters: `number.py`/`select.py`/`sensor.py`/
`switch.py` display *current state* — a stale-but-unlabeled value showing
for one extra poll cycle is a cosmetic concern. `battery_health_manager.py`
builds *stateful deltas* from sequential readings (SOC drop across a
segment, energy accumulated between anchors) — silently consuming a
stale-served value as if it were fresh doesn't just look slightly wrong,
it can corrupt the segment tracker's internal state in a way that
persists and compounds. **`battery_health_manager.py` calls `quality_of()`
alongside `_value()` for every reading it feeds into `SegmentTracker`**,
exactly as `PHASE1_BATTERY_HEALTH_DESIGN.md` §7 already specifies — this
section is corrected to match that document, not the other way around.

`night_mode.py` was checked for the same concern and found genuinely
different in kind, not merely assumed safe: its day/night transition
logic gates *polling frequency*, not a cumulative measurement. A stale
reading during an actual dusk transition means night mode engages
slightly later than ideal — a minor efficiency loss, not corrupted state
the way a bad segment reading is. `night_mode.py` correctly stays on the
unchanged list.

### 10.5 Where `BACKOFF_DEFERRED` actually gets recorded — a real implementation detail the design glossed over

§5.1 states `BACKOFF_DEFERRED` needs an explicit `record_attempt()` call
even though nothing was sent. Checked precisely against the real back-off
priority-filter code (`update_coordinator.py`, the same block Defect Y's
starvation-ceiling promotion lives in) to confirm exactly where that call
belongs — and found the variable holding what's needed no longer exists
by the point it would naturally be called.

The priority filter builds `priority_names` (the subset of `stale_names`
actually selected for this cycle — FAST always, NORMAL every Nth cycle,
starved SLOW/STATIC promoted per Defect Y), then does
**`stale_names = priority_names`** — reassigning, not merely reading, the
original variable. By the time `_execute_batch(stale_names, ...)` is
called a few lines later, the pre-filter set (everything that was due,
before deferral) is gone; nothing holds "what got filtered out" anymore.

**Resolution:** capture the pre-filter set explicitly, before the
reassignment — e.g. `pre_filter_names = list(stale_names)` at the top of
the `if in_backoff:` block — and compute
`deferred = set(pre_filter_names) - set(priority_names)` immediately
after the filter completes, calling
`self.cache.record_attempt(deferred, Quality.UNCERTAIN, Reason.BACKOFF_DEFERRED, now)`
right there — before the `stale_names = priority_names` reassignment,
not after, and not inside `_execute_batch()` at all (which never receives
the deferred names in the first place, only whatever survived filtering).
A small, precise fix once identified, but exactly the kind of detail that
would otherwise have been rediscovered mid-implementation rather than
settled here.

### 10.6 Write-verification reads bypass the cache entirely — a real gap, but an optional one, not a correctness bug

Checked whether any of this integration's write-verification polling —
`switch.py`'s post-write status loop (redesigned for Defect V, v1.3.19)
is the clearest example — feeds its outcome back into the quality model
at all. It does not: `_poll_device_status_bounded()` reads
`rn.DEVICE_STATUS` directly via the guard (`self.device.client.get(...)`,
wrapped in `guard.request()`), with zero references to `self.cache`
anywhere in that file.

This means a *successful* write-verification read is currently invisible
to the cache. The next regular poll cycle has no way to know
`DEVICE_STATUS` was just confirmed fresh moments ago via a different
path, and will re-read it as if nothing happened. `RegisterCache` is
instantiated once per coordinator (`self.cache = RegisterCache()`, not
shared across coordinators), so there's no cross-coordinator concern here
— `DEVICE_STATUS` belongs to the main coordinator's register set, and
`switch.py`, as a `CoordinatorEntity`, already has a reference to that
same coordinator via `self.coordinator`.

**Assessed as a real but optional finding, not a correctness gap** — this
is different in kind from §10.5. Nothing breaks by leaving this as-is;
the register simply gets re-read slightly more often than strictly
necessary. Worth doing (`self.coordinator.cache.record_attempt([rn.DEVICE_STATUS], Quality.GOOD, None, now)`
on a successful verification read, freshening the cache "for free" and
sparing the next poll cycle a redundant read) but explicitly not required
before implementation can begin — a genuine enhancement opportunity found
during this sweep, not a defect the rebuild would otherwise ship with.

## 11. What's still open — not yet designed

- Confidence/dispersion computation for the battery-health engine
  specifically (deferred alongside the rest of Phase 1 — see
  `PHASE1_BATTERY_HEALTH_DESIGN.md` §7 for how Phase 1's own accumulators
  need to change once this data model exists).
- Battery health polling cadence (NORMAL/SLOW tier TTLs) — real,
  believable headroom now that value extraction is quality-gated, but
  deliberately left undesigned pending observed segment-tracker behaviour
  rather than a guessed number. §8.3.
- The full implementation-level mapping from this design to
  `register_cache.py`'s actual class structure — largely built already
  (§5, §5.2, §8, §10.4-10.6 all reflect real implementation, not just
  concept) — remaining: §8.1's two-stage energy policy, §8.2's
  synchronized-power alignment check, and their adversarial tests.

## 12. Summary of firm decisions (for quick reference)

1. Three severities: GOOD / UNCERTAIN / BAD. §3
2. One timestamp, not OPC UA's dual source/server model. §4
3. Consolidated reason vocabulary, owned by the cache; guard/coordinator
   translate their existing signals into it. §5
4. `_execute_batch()` restructured: per-chunk outcome recorded inline
   (the specific failing chunk's names are only in scope INSIDE the
   per-chunk loop, not at the caller — traced against real source before
   committing); best-effort chunking, not abort-on-first-failure. §5.2
5. `WRITE_PENDING` is BAD, not UNCERTAIN — researched against real OPC UA
   practice, found structurally inapplicable, decided independently. §6
6. No sensor-value persistence across restart; adaptive learning persists
   but the existing ~72 days is not migrated. §7
7. No entity-ID/historical-data backward compatibility guaranteed — but
   device connection configuration (host/port/slave IDs/credentials) IS
   preserved, since it lives in HA core's own config_entries storage, a
   structurally different guarantee from the data/entity-ID one. §7
8. `available` = GOOD or UNCERTAIN; quality/reason/age exposed as
   attributes, uniformly across platforms. §8
9. Energy counters keep their existing stricter policy via entity-level
   override, not a new cache-level severity tier. §8.1
10. Build inside HACS first; standalone Proxmox deployment is the likely
   long-term destination, deliberately deferred. §9
11. `SynchronizedPowerCoordinator` and the optimizer coordinator are
    explicitly out of scope for this rebuild (neither uses `RegisterCache`
    at all) — a stated boundary, not an oversight. §10.1
12. Coordinator-level back-off and per-register quality are deliberately
    separate, complementary layers — not to be "simplified" into one. §10.2
13. `STATIC`-tier registers are exempt from the `EXPIRED` transition —
    immutable-by-design data should not auto-degrade to BAD purely from
    elapsed time. A real semantic bug caught during the final sweep, not
    a hypothetical. §10.3
14. `coordinator.data` stays exactly `dict[RegisterName, Result[Any]]` for
    display/write-verification consumers (number, select, sensor, switch,
    night_mode, services) — unchanged, no ripple. **But
    `battery_health_manager.py` is the deliberate exception**: it calls
    the new `quality_of()` accessor alongside its existing value reads,
    since it builds stateful deltas from sequential readings rather than
    displaying current state — exactly the vulnerability that motivated
    this whole rebuild in the first place. An earlier draft of this
    section put battery health on the "unchanged" list; caught and
    corrected in the third sweep by cross-checking this document against
    `PHASE1_BATTERY_HEALTH_DESIGN.md` directly. §10.4
15. `_execute_batch()`'s scope boundary (item 11) confirmed clean by
    direct check, not assumption — the optimizer coordinator has its own
    separate request logic and never calls it. §10.1
16. `BACKOFF_DEFERRED` recording must happen where the pre-filter
    `stale_names` set is captured, before it gets reassigned to the
    filtered `priority_names` — a concrete implementation detail traced
    against the real priority-filter code, not left to be rediscovered
    mid-implementation. §10.5
17. Write-verification reads (e.g. `switch.py`'s post-write status poll)
    currently bypass the cache entirely — a real gap, but an optional
    enhancement rather than a correctness requirement; worth doing, not
    blocking implementation. §10.6
18. Energy counters: final policy is a two-stage bound, not a binary
    "unavailable when not GOOD" — a 60-90s tightened promotion ceiling,
    then a 600s (two short-term-statistics-window) availability ceiling,
    replacing two earlier, superseded drafts of this same section. Derived
    from checking HA's actual statistics mechanics directly (the sum delta
    is value-to-value, not time-weighted; the recorder already dedupes
    unchanged writes; the real risk is short-term-snapshot misattribution,
    which scales with gap length) rather than assumed. §8.1
19. Synchronized power: skip the dedicated read when all four registers
    are GOOD and within 3 seconds of each other in the regular cache — a
    tolerance derived from the hardware's own 1 Hz register-refresh floor,
    Class 1.0 sensor accuracy, and lack of inter-device phase lock, not
    picked arbitrarily. §8.2
20. Battery health polling cadence: real, believable headroom flagged but
    deliberately left undesigned, pending observed data rather than a
    guessed number. §8.3
