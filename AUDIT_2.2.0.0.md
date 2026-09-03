# AUDIT — huawei_solar v2.2.0.0

**Status: IMPLEMENTATION COMPLETE.** HA 2026.9 / 2026.12 forward-
compatibility release. Built on 2.2.0.0 as the direct successor to
2.1.0.1, in response to a field-reported production incident: after
upgrading to Home Assistant 2026.9, all core sensors (Active power,
Daily yield, Efficiency, Internal temperature, ...) went unavailable.

**Baseline (2.1.0.1):** 1,338 passed, 5 failed, 12 errored, 1 skipped.
**Final (2.2.0.0, fresh independent extraction of the delivered zip):**
**1,366 passed, 5 failed, 12 errored, 1 skipped.**

28 tests added. The 5 failures and 12 errors are the same pre-existing
ones documented since 2.0.7 — **zero regressions**. Manifest version
validated against Home Assistant's own `ensure_strategy` check before
packaging.

---

## 1. Root cause of the field incident

Traced directly from the user's own HA log, not assumed. The traceback
showed `homeassistant.helpers.entity_platform._async_add_entity()`
raising `RuntimeError` while adding a `huawei_solar` sensor entity, with
the message *"Detected code that calls `device_registry.
async_get_or_create` with a deprecated `via_device` parameter; use
`via_device_id` instead"*, and the log line immediately above it:
*"Error adding entity None for domain sensor with platform
huawei_solar."*

Six sites in `__init__.py` constructed `DeviceInfo` with the deprecated
`via_device=(DOMAIN, identifier)` keyword: the main inverter, power
meter, battery, battery 1, battery 2, and optimizers. Every entity
belonging to any of these devices inherits its `DeviceInfo` via its own
`device_info` property, which is why the failure was so broad — not a
handful of related sensors, but effectively every entity on the
affected devices.

**Mechanism, confirmed precisely.** HA's own `report_usage()` logs a
warning when it can attribute a deprecated call to an identifiable
integration stack frame. When the call instead originates from HA's own
`entity_platform.py` processing an entity's `device_info` (as opposed to
the integration calling the registry directly), no such frame exists,
and HA falls back to raising `RuntimeError` instead of warning. The
entity is never added; the sensor shows unavailable. This is also
corroborated independently: `homematicip_local` (an unrelated, real
integration) hit the identical error text in its own 2.10.0 release
notes, with the identical explanation.

Confirmed as pre-existing since 2.0.14, not introduced by 2.1.0.0 or
2.1.0.1 — the identical 6 occurrences, same pattern, verified directly
against the original 2.0.14 base before any fix was written.

---

## 2. Fix 1 — `via_device` → `via_device_id`

**All 6 sites migrated.** Two distinct patterns:

- **5 sites** (power meter, battery, battery 1, battery 2, optimizers)
  reuse the main inverter's own `DeviceEntry.id`, captured from the
  return value of its `async_get_or_create()` call (previously
  discarded, since nothing needed it before this fix). This is the
  officially documented pattern (HA developer blog, "More device
  registry deprecations", 2026-08-24): *"When your integration creates
  the via device itself, skip the lookup and read `.id` from the
  `DeviceEntry` that `async_get_or_create` returned for it."*
- **1 site** (`connecting_inverter_device_id`, the main inverter's own
  potential link to a master device) resolves via the official
  `async_get_device_id_by_identifier()` helper, wrapped in
  `try`/`except ValueError` per its documented raise-on-missing
  behaviour.

**A mistake in the author's own first draft, caught and corrected before
being presented for review.** The first attempt resolved
`connecting_inverter_device_id` via `DeviceRegistry.async_get_device()`
— which is *itself* one of the deprecated APIs in this same HA change,
per the same developer blog post. Fixing one deprecated call with
another would not have closed the deprecation. Caught by fetching and
reading the official blog post directly rather than assuming the first,
plausible-looking fix was correct.

**This parameter is untested in production.** `connecting_inverter_
device_id` is always `None` at the single call site in this codebase
today — this integration does not currently link a secondary inverter
to a master via this mechanism. The parameter and its resolution logic
are kept, not dropped, since the parameter's own existence signals
intended future support; but the `try`/`except ValueError` path itself
has never been exercised against real data.

**Testing limitation, stated plainly.** `async_get_device_id_by_
identifier()` was introduced around HA 2026.8. The newest `homeassistant`
package available via PyPI in this environment was 2025.1.4, which
predates it — real execution against this specific helper was not
possible. Its signature and behaviour were instead verified directly
against the official HA developer blog post, quoted at the call site.
This is a genuine gap between "verified against the authoritative
source" and "verified by execution," recorded here rather than implied
away.

**Tests (9).** Structural: no `via_device=` keyword remains; the correct
site/syntax counts for both the keyword-argument sites (5) and the
conditional dict-key site (1); the 5 sub-device sites all reuse
`inverter_device_entry.id`; the main inverter's own `async_get_or_
create()` return value is captured; the also-deprecated `async_get_
device()` is not used anywhere in this function; `config_entry_id` is
passed to the official helper; the call is guarded against `ValueError`;
the `connecting_inverter_device_id` parameter itself is not silently
dropped.

---

## 3. Fix 2 — options-flow listener/reload (HA 2026.12 hard error)

Surfaced independently, from a user-supplied forward-compatibility
review document, and verified against HA's own developer blog before
acting on it: *"using a config entry listener together with any
reloading methods in a config flow is deprecated and will result in an
error from 2026.12"* (developer blog, 2026-05-07).

**`BatteryHealthOptionsFlowHandler`** now subclasses
`OptionsFlowWithReload` instead of `config_entries.OptionsFlow`. Its
`async_create_entry()` call is unchanged; the base-class swap alone
makes it perform the reload directly, matching the official migration
pattern (developer docs example fetched and compared directly).

**The update listener (`_async_options_updated`) is removed entirely**
from `__init__.py`, not merely bypassed for the options flow.

**A gap in the supplied review document, found and closed before
implementing its narrower fix.** That document identified only the
options-flow listener/reload pair. Checking the full config flow found
the *same* listener also combined with reloading methods in three more
places, all inside `ConfigFlow._create_or_update_entry()`: the reauth
path (`async_reload`), the reconfigure path (`async_reload`), and the
new-entry path's `_abort_if_unique_id_configured(updates=data)` (which
defaults to `reload_on_update=True`). Removing the listener entirely —
not just its options-flow use — is what actually closes all four sites
at once: with no listener registered for the entry, none of the
remaining three reloading-method calls are "combined with a listener"
either. This reasoning is recorded here because the supplied document's
narrower fix happened to be *sufficient*, but for a reason it did not
itself state.

**Tests (8).** The class subclasses the reload variant, not the plain
one; the import is present; the listener registration and its handler
function are both removed from `__init__.py`; the options-commit
mechanism (`async_create_entry`) is behaviourally unchanged; the reauth
path's own `async_reload()` is confirmed untouched (correctly — the fix
is removing the listener, not touching every reload call site) while no
listener remains anywhere for it to be paired with; a whole-tree sweep
confirms no `add_update_listener` anywhere in production code.

---

## 4. Fix 3 — `DeviceEntry.config_entries` → `async_get_device_and_config_entry_for_domain`

`services.py::async_get_entry_id_for_service_call()` previously iterated
`device_entry.config_entries` by hand. Since HA 2026.8 a device belongs
to a single config entry; `DeviceEntry.config_entries` is deprecated
(removal scheduled for HA 2027.8), and HA's official replacement is used
instead.

**Behavioural distinctions preserved deliberately, not lost to the
migration.** The new helper collapses "no such device" and "device
exists but isn't owned by this domain" into the same `(None, None)` —
both were previously distinguishable error messages (`invalid_device_id`
vs `config_entry_not_found`). The explicit `device_registry.async_get(
device_id)` check is kept first, so both remain reachable, in the
correct order. The helper also does not check whether the config entry
is loaded (documented HA behaviour, not an oversight) — the existing
`entry_not_loaded`/`ConfigEntryState.LOADED` check is kept for the same
reason.

**Tests (5).** The manual iteration is removed (checked against the
actual executable statement shape, not a bare substring — see §6 on why
that distinction mattered); the official helper is used with `domain=
DOMAIN`; both distinct error paths remain reachable in the correct
order; the loaded-check is preserved.

---

## 5. Fix 4 — `serial.tools.list_ports` → `serialx`

Verified against the official HA developer blog (2026-04-27,
"Serious about serial: migrating from pyserial to serialx") and the
HA 2026.9 changelog, which lists this deprecation directly.

**Verified by real execution, not just source inspection** — the one
fix in this release where that was possible. `serialx` 1.9.0 installs
cleanly from PyPI; `async_list_serial_ports()` was actually called
against this sandbox's own `/dev/ttyS0`, confirmed to enumerate it
correctly; the full field-mapping and `human_readable_device_name()`
call chain was executed end-to-end against that same port, including
its all-`None` USB-metadata case (the adversarial case this sandbox's
one available port happens to exercise for free).

**A genuine improvement, not a mechanical rename.** `serialx` is
natively async; the `async_add_executor_job()` wrapping the old
sync-only `pyserial` call required is removed entirely.

**The field mapping is not 1:1, and this was checked directly rather
than assumed.** `SerialPortInfo` has no `description` field; `product`
was confirmed as the closest semantic equivalent by inspecting the
dataclass fields directly. `vid`/`pid` are `int | None` on
`SerialPortInfo`, where `human_readable_device_name()` expects
`str | None` — converted explicitly rather than relying on the target
function's own string formatting to paper over the mismatch.

**`serialx>=1.0.0` added as an explicit `manifest.json` requirement.**
Unlike `serial.tools.list_ports`, which was only ever implicitly
available via HA core's own bundled `pyserial`, this integration now
depends on `serialx` directly.

**Scope, stated precisely.** This migration covers port *enumeration*
for the config-flow UI only. The integration's actual Modbus serial I/O
never used `pyserial` at all — it runs entirely through `tmodbus`. The
HA 2026.9 changelog deprecation ("pyserial-asyncio in favor of
serialx") does not, strictly, apply to this codebase's serial
communication path; it applies only to this one UI-facing call.

**Tests (7).** The old import and the old executor-job call are both
removed (checked against the real assignment statement, not the
explanatory comment's own mention of the old API — see §6); `serialx`
is imported and the native async call is used; the `product` field
mapping is present and `description` is not used; `vid`/`pid` are
explicitly converted to `str`; `serialx` is declared in
`manifest.json`; the manifest remains valid JSON.

---

## 6. Testing discipline: a recurring test-authoring bug, caught and fixed at its root

Nine of the initial 28 tests failed on first run — not because any fix
was wrong (each was independently re-verified against the real code
before being dismissed as a test artifact), but because of one repeated
mistake in the tests themselves: this project's own convention of
explaining a fix's *before* state in a docstring or `#`-comment block
means that prose necessarily *names the old, deprecated API*. A plain
substring search over a function's whole source text matches that
explanatory mention before it ever reaches the real code below it.

This surfaced in three shapes across the run:

1. A count assertion (`via_device_id=` should appear 6 times) was
   simply wrong — one of the six sites uses conditional dict-key syntax
   (`**({"via_device_id": ...} if ... else {})`) to omit the key
   entirely when there is no via-device relationship, not a plain
   keyword argument. Fixed by counting each syntax form separately.
2. A shared `_function_body()` helper stripped a triple-quoted
   docstring, but `_setup_inverter_device_data()` has no docstring at
   all — its explanatory prose is a `#`-comment block that does not
   even start at the top of the function body (one real line of code
   precedes it). The helper was rewritten to strip every comment-only
   line throughout the body, not just a leading run of them.
3. Two whole-file substring checks (`device_entry.config_entries`,
   `serial.tools.list_ports.comports`) matched this same project's own
   explanatory comments about what those fixes replaced. Rewritten to
   check for the specific executable-statement shape (`for entry_id in
   device_entry.config_entries`, `ports = await self.hass.
   async_add_executor_job(serial`) rather than the bare API-name
   substring.

Each failure was checked directly against the real code before being
attributed to the test (see §2's mistake for a case where a first-draft
*fix* really was wrong, by contrast) — the general discipline throughout
this session has been: verify against the source before deciding
whether the code or the test is at fault, not assume in either
direction.

---

## 7. Verification performed

- Every `.py` file compiles; `manifest.json`, `strings.json`, all
  `translations/*.json` and `services.yaml` all parse.
- Manifest version checked against Home Assistant's own
  `ensure_strategy` validation, not assumed.
- `serialx` verified by real, direct execution against this sandbox's
  own serial device — the one fix in this release verified this way,
  rather than by source inspection alone.
- Full suite run from a **fresh, independent extraction of the
  delivered zip**: 1,366 passed / 5 failed / 12 errored / 1 skipped —
  identical to the working tree, matching the 2.1.0.1 baseline's
  failure set exactly.
- Each of the four fixes was checked against the full suite
  individually before the next was started.

## 8. Honest confidence statement

**High, and field-motivated** — Fix 1 (`via_device_id`). This closes a
real, reported production incident, traced to a precise mechanism (not
inferred), and the fix follows the officially documented pattern.

**High** — Fix 2 (options flow). The core claim was independently
verified against HA's own developer blog before acting on a
user-supplied review document, and the full scope (four combined
sites, not one) was found and accounted for before implementation.

**High** — Fix 3 (`config_entries`). Straightforward migration to an
official replacement helper, with the pre-existing behavioural
distinctions explicitly re-verified as preserved.

**Good, with one explicitly stated scope limitation** — Fix 4
(`serialx`). Verified by real execution. The scope is narrower than a
first reading of the HA 2026.9 changelog entry suggests: this
integration's actual Modbus I/O was never on `pyserial` in the first
place.

**Stated as unknown** — the `try`/`except ValueError` path for
`connecting_inverter_device_id` (untested in production, since the
parameter is always `None` today), and `async_get_device_id_by_
identifier()`'s exact runtime behaviour under a live HA 2026.8+ instance
(verified against documentation, not executed, in this environment).

All open items carried forward unchanged from `AUDIT_2.1.0.1.md`
(the write path remains unmeasured; the `ADAPTIVE_FIRMWARE_CHANGE_
DECAY_FACTOR` remains an engineering estimate) are still open. This
release did not touch that code and did not attempt to close them.
