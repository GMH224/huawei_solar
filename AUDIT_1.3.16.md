# Release Audit — huawei_solar v1.3.16

**Date:** 2026-08-05 · **Auditor:** Claude (Anthropic)
**Baseline:** v1.3.15
**Type:** single defect fix, one production file changed
(`modbus_keepalive.py`), plus a non-behavioural test-infrastructure fix in
`tests/test_tier_separation.py`.

---

## 1. The report

A screenshot of the `Inverter 5K` device page (`HV2220080950`) showed every
sensor as `Unknown` after more than an hour of runtime, alongside adaptive
diagnostics indicating chronic distress: RTT p95 of 6,251.6ms, 613.2s of
cumulative bus wait, 62 delayed requests, back-off cycle 70+. A Home
Assistant log covering the same window was requested and provided.

## 2. Diagnosis

The log showed, recurring every 15-45 seconds without a single successful
cycle:

```
2026-08-05 10:42:29.094 DEBUG [custom_components.huawei_solar.modbus_keepalive] ModbusKeepAlive[HV2220080950]: unexpected error in run loop: 'NewType' object is not subscriptable
```

Traced to `_get_keepalive_register()`:

```python
try:
    _KEEPALIVE_REGISTER_NAME = RegisterName[KEEPALIVE_REGISTER]
    return _KEEPALIVE_REGISTER_NAME
except KeyError:
    ...
```

Confirmed directly against the actual installed package:

```
>>> type(RegisterName)
<class 'typing.NewType'>
>>> RegisterName["model_id"]
TypeError: 'NewType' object is not subscriptable
```

`RegisterName[KEEPALIVE_REGISTER]` cannot succeed — `RegisterName` is a
`typing.NewType`, which does not support subscripting under any
circumstances. The `except KeyError:` clause was written for a different
failure mode (an Enum member-name lookup failing) and does not catch
`TypeError`. The exception therefore propagated out of this function, out
of `_probe()` (called before `_probe()`'s own try block begins), and was
only ever caught by `_run()`'s generic outer handler — logging "unexpected
error in run loop" every cycle, forever, since this code was written.

**Effect:** the keep-alive probe — a lightweight, fast connection-loss
detector distinct from the coordinators' own regular polling — has never
once completed successfully. It has been silently non-functional. The
coordinators' own polling is a separate code path and continued working
independently; the chronic distress visible in the field screenshot's
adaptive diagnostics is a real, separate observation (discussed with the
operator; not attributed to this defect, and not further investigated in
this release — see §7).

## 3. On attribution — corrected during this investigation

This defect was initially, and incorrectly, characterised as originating
in "the vendor library" during the live investigation. The operator
corrected this directly: the `huawei_solar` package this integration
depends on is not an arm's-length third-party dependency in the sense that
phrasing implied — it is a fork, substantially rewritten since its
original fork point, maintained across sessions of this same kind of work.

This matters for how the defect is recorded, not for the fix itself. The
bug is, and has always been, in **our own integration code**:
`_get_keepalive_register()` made an assumption about `RegisterName`'s
runtime shape (Enum-like member lookup) that was never verified against
how the type actually behaves, and paired that assumption with an
exception handler narrow enough to miss the real failure the moment the
assumption was wrong. That is true regardless of which package defines
`RegisterName`, regardless of that package's own history, and regardless
of whether its type choice changed at the fork point, during the rewrite,
or was never what BUG-9's original author (an earlier session of this same
project) assumed in the first place. Framing this as "a vendor library
issue" would have been an inaccurate way to avoid owning a bug that is
entirely ours. The BUG-9 FOLLOW-UP comment in `modbus_keepalive.py`, this
changelog entry, and this audit are all written to reflect that plainly.

**A related, practical limitation, also stated plainly:** this sandbox
does not have access to the operator's actual fork's source — only a
vanilla `pip install huawei_solar==3.0.5` installed earlier in this
session purely to obtain real register data for unrelated analysis. The
fix does not depend on inspecting the fork's internals: it works by
checking `RegisterName`'s actual runtime behaviour and validating against
the real, authoritative `REGISTERS` table, rather than by assuming
anything about the type's declared shape. This is deliberately robust to
not knowing exactly what the fork's `RegisterName` looks like internally.

## 4. The fix

```python
def _get_keepalive_register() -> RegisterName | None:
    global _KEEPALIVE_REGISTER_NAME
    if _KEEPALIVE_REGISTER_NAME is not None:
        return _KEEPALIVE_REGISTER_NAME

    from huawei_solar.registers import REGISTERS

    if KEEPALIVE_REGISTER not in REGISTERS:
        _LOGGER.warning(...)
        return None

    _KEEPALIVE_REGISTER_NAME = RegisterName(KEEPALIVE_REGISTER)
    return _KEEPALIVE_REGISTER_NAME
```

Validates the configured register name against `huawei_solar.registers.REGISTERS`
— the same authoritative source this project already uses elsewhere
(`update_coordinator.py`'s `_modbus_span()`) — rather than assuming
anything about `RegisterName`'s own type. Constructs the value with
`RegisterName(...)`, a call, which is the correct, already-established
idiom used elsewhere in this codebase (`sensor.py`) and works identically
regardless of whether the underlying type is Enum-like or a plain
`NewType`. Confirmed KEEPALIVE_REGISTER ("model_id") is a valid entry in
the real register table before shipping this.

The original defensive intent of BUG-9 (skip the probe gracefully on an
invalid configuration rather than crash the loop) is preserved — corrected
this time to actually match the failure mode that occurs.

## 5. Adversarial verification

New `tests/test_modbus_keepalive_registername.py`, deliberately exercising
the genuine, installed `huawei_solar` package rather than a fake — the
whole point of this defect is a mismatch between an assumption in our code
and that package's real behaviour, which a stand-in could not prove either
way:

- Confirms `RegisterName` really is non-subscriptable, reproducing the
  exact original crash directly against the real type.
- Confirms the new call-based resolution succeeds for the real,
  configured `KEEPALIVE_REGISTER` value and correctly returns `None`
  (rather than raising) for a genuinely invalid register name.
- Static (AST) checks confirm `_get_keepalive_register` no longer
  subscripts `RegisterName` anywhere in its body, and does reference
  `REGISTERS`. Run against the pre-fix source, both fail at the exact
  original line (93). Run against this release, both pass.

## 6. Test-infrastructure fix, non-behavioural

Writing the above test surfaced a genuine constraint in the real
`huawei_solar` package: importing it has a non-idempotent side effect
(registering PDU classes into `tmodbus`'s shared registry) that raises if
its real modules are ever imported a second time within one process. This
collided with `test_tier_separation.py`'s existing `TestRealRegisterMap`,
which already has a working pattern for safely accessing the real package
amid this suite's many lightweight stubs (purge any stubbed
`huawei_solar*` entries, import fresh, restore afterward) — a pattern that
had only ever needed to run once per session before a second consumer
existed.

Fixed by adding an "is the real package already cached? reuse it, don't
force a second import" guard to both `test_tier_separation.py` and the new
test file, and by removing the restore-on-teardown step. That restore was
not just unnecessary (every stub-creating file in this suite unconditionally
overwrites `sys.modules["huawei_solar"]` at its own import time regardless
of prior state, so nothing depends on inheriting a particular prior stub)
— once a second consumer of the real package existed, it was actively
harmful, undoing each class's own successful import and causing whichever
class ran next to see a stub again and re-trigger the exact collision the
guard exists to avoid. Verified stable by running the full suite
repeatedly and by explicitly running both files in each possible order.

`test_tier_separation.py`'s own assertions and behaviour are completely
unchanged — only its real-library bootstrapping mechanics were touched.

## 7. What this release does not address

The chronic adaptive-controller distress visible in the field screenshot
(RTT p95 6.25s, 613s cumulative wait, back-off cycle 70+) for
`HV2220080950` is a real, separate observation, discussed directly with
the operator during this investigation and explicitly not attributed to
Defect S. Whether it reflects a genuinely weaker device on the shared bus,
firmware or hardware characteristics specific to that inverter, or
something else, remains open and is not resolved by this release. Also
separately unresolved and unrelated: the hardware/protocol-architecture
discussion (RS485 gateway alternatives) had by the operator and this
assistant, explicitly filed as a future consideration, not acted on now.

## 8. Safety properties

- No change to `update_coordinator.py`, `modbus_guard.py`,
  `adaptive_modbus.py`, `register_cache.py`, or any entity platform file.
- Defects F through R (v1.3.7-v1.3.15) are untouched and still in place.
- The keep-alive probe's role (fast, auxiliary connection-loss detection)
  is unchanged; this fix makes it able to run at all, nothing about its
  intended behaviour when it does run.
- `test_tier_separation.py`'s test assertions are byte-identical in
  effect; only its module-level real-library bootstrapping changed.

## 9. Test evidence

- **547 passed, 1 skipped, 0 failed**, deterministic across 3 repeated
  runs, and confirmed stable across multiple explicit file-collection
  orderings (a genuine risk surfaced during this fix's own development,
  not merely a theoretical concern).
- Adversarial: both static checks fail against the pre-fix
  `modbus_keepalive.py` at the exact original line (93); pass against this
  release.
- Static: `py_compile` clean; manifest version = 1.3.16.
- Confidentiality sweep: clean.
- Diffed against the v1.3.15 tree to confirm only `modbus_keepalive.py`
  changed among production files, and only
  `tests/test_tier_separation.py`'s bootstrapping (not its assertions)
  changed among existing test files.

## 10. Recommended deployment procedure

1. Delete `/config/custom_components/huawei_solar` entirely.
2. Extract v1.3.16 fresh into `custom_components/`.
3. Restart Home Assistant once.
4. **Required validation, specific to this release:** confirm the
   `ModbusKeepAlive[...]: unexpected error in run loop` line no longer
   appears in the log at all, for any device, over a reasonable observation
   window (it previously recurred every 15-45 seconds, so its absence
   should be obvious quickly). This is the direct test of Defect S.
5. Separately, continue observing `HV2220080950`'s adaptive diagnostics
   (RTT, back-off cycle count, bus wait) to determine whether §7's open
   question is a persistent characteristic of that specific device or
   something that resolves on its own — this release does not claim to
   affect it either way.

**Verdict:** release-ready. A real, confirmed, long-standing defect in this
project's own integration code, found through field evidence and fixed at
its root rather than papered over, with the investigation's own framing
corrected mid-stream to properly own it rather than attribute it
elsewhere.
