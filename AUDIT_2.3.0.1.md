# AUDIT — huawei_solar v2.3.0.1

**Status: IMPLEMENTATION COMPLETE.** A fix release on top of 2.3.0.0. It
adds no new functionality apart from one user-requested option that makes
an existing recurring write optional (off by default).

**Origin.** A field log (2026-09-16, two cascaded SUN2000-10KTL-M1, serials
…926 and …950, one SDongle) showed an integration reload that never
completed. Every finding below was confirmed against the 2.3.0.0 source
before any change.

**Design constraint from the user.** Do not add Modbus load. Section 3
accounts for every change in terms of bus traffic.

---

## 1. Field-log analysis

| Time | Observation | Meaning |
|---|---|---|
| 08:37:06 / 08:37:11 | `write_permission_check … timed out after 5s` on both inverters | Startup probe gave no result |
| 08:37:07 | tmodbus discarded a late frame `01 06 A7 FE 00 3C` | The probe's write (FC 06, register 43006 = time zone, value 60) **did reach the inverter** despite the timeout |
| 08:37–12:20 | ~30 coordinator timeouts (15–49 s without response) on both inverters; frequent "unexpected Transaction ID" discards | The gateway answers, but late: an overloaded or slow link (environmental, see OBS-2301-01) |
| 12:11 / 12:16 | Battery coordinator hit its 120 s whole-poll deadline | Same cause |
| 12:21:01 | 15 × "Unable to remove unknown service" | HS-2301-002 |
| 12:22:00 | "Setup of config entry … cancelled", traceback inside `create_sub_device_instance` (unit 2) under `asyncio.wait_for` | Cancelled from outside before the integration's own 45 s bound; `CancelledError`, not `TimeoutError` → HS-2301-001 |

---

## 2. Findings and fixes

### HS-2301-001 — cancelled setup skipped all rollback (High)

**Defect.** Every rollback handler in `async_setup_entry` catches
`ConnectionInterruptedException`, `ConnectionException`, `TimeoutError`,
`HuaweiSolarException` or `Exception`. `asyncio.CancelledError` derives from
`BaseException`, so none of them run when Home Assistant cancels a setup
attempt. The cancellation can come from a second reload click, a restart,
or a setup timeout.

**What stayed alive** for everything already started in the cancelled
attempt:

- the Modbus TCP connection;
- the keep-alive probe task;
- the detached first-refresh tasks (2.2.0.2's HS-ICS-002 cancellation
  relies on this same rollback path);
- telemetry;
- the adaptive controller's push subscription;
- the ModbusGuard endpoint reference count.

These kept talking to an already slow gateway and competed with the next
setup attempt. This matches the "reload doesn't work" symptom.

The identification step had the same gap one level down. A cancellation
during `create_device_instance()` left the raw client connected, and
`primary_device` was still `None`, so the outer handler could not reach
it.

**Fix (`__init__.py`).**

- **Identification step:** new `except asyncio.CancelledError` that
  disconnects the raw client with the existing bounded
  `_bounded_client_disconnect()`, then re-raises.
- **Outer try:** new `except asyncio.CancelledError` that runs exactly
  the `except Exception` rollback (`_run_cleanup_callbacks` +
  `_bounded_device_stop`), then re-raises.
- **New helper `_await_rollback_shielded()`,** which:
  - runs the rollback in its **own task**, so the `wait_for`/`timeout`
    bounds inside it see a clean cancellation count and fire normally;
  - awaits it through **`asyncio.shield`**, so if the caller is cancelled
    again, the rollback still completes in the background instead of
    stopping half-way;
  - keeps a **strong reference** (`_ROLLBACK_TASKS`), because asyncio only
    holds weak references to tasks;
  - never raises; errors are logged.
- **Cancellation is always re-raised unchanged,** which asyncio requires.

**Boundedness of the rollback.** Every step is already bounded or local:

| Step | Bound |
|---|---|
| Task cancels | local |
| Adaptive controller | local storage flush |
| Keep-alive stop | local |
| Telemetry removal | local |
| Static bound cache clear | local |
| Device stop | `DISCONNECT_TIMEOUT` |
| Guard release | local |

**Deliberately not changed.** No bare `except:` and no `BaseException`
handler (both asserted by tests): `KeyboardInterrupt`/`SystemExit` must
never be intercepted.

### HS-2301-002 — noisy service unload (Low, cosmetic)

`async_unload_services()` removed capability services that were never
registered for this installation (LG-only, capacity control). Its final
unregister-all pass then removed services the per-capability pass had
already removed. Home Assistant logs a warning for each such call, 15 per
reload in the log.

**Fix (`services.py`).** New helper `_remove_service_if_registered()`
checks `hass.services.has_service()` first. It is now the only
`async_remove` call site (asserted by a test). Which services end up
removed is unchanged. A stale comment that claimed such removals were
silent was corrected.

### HS-2301-003 — write-permission probe is now an option, default OFF

**Background.** `huawei-solar`'s `has_write_permission()` reads the
time-zone register (43006) and **writes the same value back**. The sensor
platform ran it once per inverter on every start and reload, only to
decide whether to create the read-only *Active Power Control Mode*
sensor. The field log proves the write executes even when the probe
reports a timeout.

**Change.**

- **New option `write_permission_probe`** (`const.CONF_WRITE_PERMISSION_PROBE`,
  default `DEFAULT_WRITE_PERMISSION_PROBE = False`). It appears in the
  existing options form with a label in `strings.json` and `en.json`.
- **`create_sun2000_entities(ucs, *, probe_write_permission=…)`:** the
  eligibility condition is now
  `eligible and (not probe_write_permission or await probe)`.
  - The cheap eligibility checks still run first, so an ineligible device
    never probes (the Defect V2-1 invariant).
  - With the probe off, eligibility alone decides.
- **Safety of the "off" default:**
  - **Network setups:** the config flow only completes an
    elevated-permission setup after write access was verified, directly or
    after the installer login.
  - **Serial setups:** these have always assumed write access (line 607,
    unchanged).
  - **Access withdrawn later:** the sensor becomes unavailable; nothing
    writes.
- **Unchanged:** the config flow's own probe during setup, reconfigure and
  re-authentication. It is user-initiated and validates the credentials
  the user just entered.

**Existing test adapted.** `tests/test_write_permission_ordering.py`
located the `await` as a direct value of the top-level `and`; it is now
nested in `(not … or await …)`. The helper now selects the value that
*contains* the await. The asserted invariant (the free coordinator check
comes before the probe) is unchanged and still enforced.

---

## 3. Modbus load accounting

| Change | Reads | Writes |
|---|---|---|
| HS-2301-001 | none; the rollback **stops** activity and closes the connection | none |
| HS-2301-002 | none (service registry only) | none |
| HS-2301-003, default | **−2 bus exchanges per inverter per start/reload** (probe read and write-back) | **−1 stored write per inverter per start/reload** |

**Active Power Control Mode sensor.** It is created **disabled by
default**. Disabled entities are not added and request no registers.

- **Default case:** no new reads.
- **Only if a user enabled that sensor earlier:** in this installation it
  had been skipped because the probe timed out, and it now returns. Its 3
  adjacent registers (47415, 47416–47417 as one 32-bit value, 47418; 4
  words in total) are then read again on the slow configuration cycle.
  Disabling the entity removes that read.

**TOU slot entities (2.3.0.0), re-checked on request.** Coordinators merge
all entities' register lists into one set (`update_coordinator.py`,
`list(set(chain.from_iterable(...)))`). The TOU slots request register
47255, which the pre-existing TOU sensor already requests, so they add no
reads. If a user disables both the TOU sensor and all slots, 47255 drops
out of polling entirely.

---

## 4. Observations (not changed)

**OBS-2301-01 — gateway overload (environmental).**
Timeouts and late frames started hours before the reload. Recommended
steps:

- make sure the integration is the only Modbus client of the dongle;
- disable unused entities, especially on the second inverter;
- prefer a wired dongle link over WLAN where possible;
- keep the dongle firmware current.

**OBS-2301-02 — cancellation after platform forwarding.**
If cancellation arrives after `async_forward_entry_setups` has started,
the rollback (like the pre-existing `except Exception` path) does not
unload platforms. Home Assistant's own entry-state handling governs that.
Behaviour is unchanged from earlier releases.

**OBS-2301-03 — unload-path cancellation not reviewed.**
`async_unload_entry` under cancellation was not part of this review.

**OBS-230-01 — still open.**
`_attr_available` is ignored on several entity classes (see
AUDIT_2.3.0.0.md).

**Comparison with 2.2.0.0 not possible.**
The user reports 2.2.0.0 as very stable. Its source was not available
here, so it could not be compared. The known deltas since then are
2.2.0.1 (16 fixes), 2.2.0.2 (5 fixes), 2.3.0.0 (TOU slots and
HS-230-001) and this release.

---

## 5. Testing

**New tests.** `tests/test_ics_2301_fixes.py`, 26 tests, passing under
pytest and standalone. The real helpers (`_run_cleanup_callbacks`,
`_await_rollback_shielded`, `async_unload_services`,
`_remove_service_if_registered`) are extracted from source and executed.
The real eligibility expression of `create_sun2000_entities` is wrapped
and executed with fakes.

Behaviour covered:

- rollback completes;
- sync and async rollbacks both work;
- errors are logged, not raised;
- the original cancellation is re-raised;
- a **second cancellation** does not abandon the rollback, and the
  reference is released afterwards;
- `wait_for` bounds still fire inside the rollback;
- the field scenario (battery, no EMMA/LG/capacity control) removes each
  registered service exactly once and never an unknown one;
- probe off → entity eligible with **zero** probe calls;
- probe on → the probe result decides;
- ineligible devices never probe;
- default is off.

Structural checks on the real `async_setup_entry`:

- every try whose `except Exception` performs rollback also handles
  cancellation and re-raises;
- no bare `except:` or `BaseException` handlers;
- the rollback makes no bus I/O calls.

**Mutation check.** 11 deliberate faults were injected one at a time and
all 11 were caught:

- outer cancellation handler removed;
- identification cancellation handler removed;
- cancellation swallowed;
- device not stopped on cancel;
- no shield;
- no strong reference;
- rollback errors propagate;
- unguarded service removal;
- probe always runs;
- probe default on;
- option not passed.

Sources were restored afterwards.

**Regression.** Every test file was run per file against 2.3.0.0 in the
same environment (`voluptuous`, `huawei-solar` 3.0.7 installed). Results
are identical except the new file. The pre-existing failing test IDs are
identical (the same environment-limited set documented in
AUDIT_2.3.0.0.md). All 73 `.py` files parse, all 22 JSON files load, and
`pyflakes` reports nothing on the changed files (identical to 2.3.0.0).

**Not tested here.** A live Home Assistant 2026.x runtime and real
hardware.

Suggested field checks:

1. Restart Home Assistant; the log should show no `write_permission_check`
   lines.
2. Reload the integration; there should be no "Unable to remove unknown
   service" warnings.
3. If a reload is ever cancelled, check afterwards that only one Modbus
   connection to the dongle exists.

---

## 6. Upgrade notes

- **No migration.** The new option defaults to off; existing options are
  untouched.
- **Restart rather than reload after installing.** If a previous
  cancelled setup left orphaned tasks, only a restart clears them; the fix
  prevents new ones.
- **Re-enabling the probe:** *Configure → Test write permission at every
  start*. Saving options reloads the entry.
