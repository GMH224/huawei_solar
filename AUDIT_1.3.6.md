# Release Audit — huawei_solar v1.3.6

**Date:** 2026-08-05 · **Auditor:** Claude (Anthropic)
**Baseline:** v1.3.5 (reported failing to load in the field)
**Type:** test-coverage gap closure. **No production code changed** — this
release adds one test file's worth of verification and re-packages.

---

## 1. The report

```
ImportError: cannot import name 'CONF_COALESCE_SLOW_TIER' from
'custom_components.huawei_solar.const'
  File ".../huawei_solar/__init__.py", line 40, in <module>
    from .const import (...)
```

Reported immediately after deploying v1.3.5: the integration failed to load
at all.

## 2. Root cause — deployment, not the shipped package

The v1.3.5 archive was re-verified during this investigation by extracting
it fresh and diffing `__init__.py` directly against the audited working
tree: **identical, and containing no reference to `CONF_COALESCE_SLOW_TIER`
anywhere.**

The error itself is diagnostic of what actually happened. It requires
`__init__.py` to import a name from `const.py` that `const.py` does not
define — meaning the deployed installation combined an `__init__.py` from
**before** the v1.3.5 removal with a `const.py` from **after** it. A clean
extraction of one consistent archive cannot produce that combination; only a
partial overwrite can (a stale `__init__.py` left on disk from v1.3.4, or a
cached compiled `.pyc` not invalidated by the new source).

**Recommended and communicated to the operator:** delete
`/config/custom_components/huawei_solar` entirely before extracting v1.3.6,
rather than extracting over the existing directory. This eliminates both
candidate causes at once.

## 3. The real gap this exposed

Investigating the report surfaced a genuine, pre-existing weakness, independent of whether the report's proximate cause was deployment-side:
**`__init__.py` — Home Assistant's actual entry point for this integration,
and the single file whose failure takes down every entity — was not covered
by `test_module_imports.py`**, the harness added in v1.2.4 specifically to
catch import-time defects after the v1.2.3 outage.

### 3.1 Why it was excluded, and why that reasoning doesn't hold up

`__init__.py`'s transitive dependency graph — Home Assistant's config-entry,
service-registration, and entity-platform machinery, plus the full
`huawei_solar` device class hierarchy — is far larger than the leaf modules
`test_module_imports.py` already covers (`const`, `night_mode`,
`modbus_guard`, `register_cache`, `modbus_telemetry`, `adaptive_modbus`,
`battery_health`, `bus_diagnostics`). A full runtime import was attempted
during this investigation and, after substantial stub construction (Home
Assistant core/const/config_entries/exceptions/helpers surfaces, the real
installed `huawei_solar` package for its own side, and `voluptuous`),
**succeeded** — proving the shipped `__init__.py` genuinely does import
without error. But the stub surface required was large and single-purpose,
tied to `__init__.py`'s specific import graph rather than reusable
verification infrastructure, so it was not retained as a permanent fixture.

### 3.2 The fix adopted instead

`TestConstImportsAreDefined`, added to `test_module_imports.py`: for every
`.py` file in the package, every name it imports via
`from .const import ...` is checked against an AST-derived list of names
actually defined in `const.py`. This is:

- **Dependency-free.** No Home Assistant, no vendor library, no stub
  construction. Pure `ast.parse` on two files.
- **Fast.** Runs in milliseconds as part of the existing suite.
- **General, not just a reproduction.** It catches this defect class in
  *any* file that imports from `const.py`, not only `__init__.py`, and not
  only this specific set of names.

A second, narrower test —
`test_the_specific_v1_3_5_incident_names_stay_gone` — pins the exact four
retired identifiers (`CONF_COALESCE_SLOW_TIER`, `CONF_PREFER_NIGHT_FOR_SLOW`,
and their `DEFAULT_` counterparts) as absent from both `const.py` and the
full text of every production file, closing the loop on this specific
incident as well as the general class.

## 4. Adversarial verification (mandatory per project convention)

The exact reported failure was **reproduced**, not merely reasoned about:

1. A working copy of the audited (already-correct) `__init__.py` was
   temporarily edited to reintroduce
   `CONF_COALESCE_SLOW_TIER` into its `from .const import (...)` block —
   i.e., manufacturing precisely the "old `__init__.py`, new `const.py`"
   combination the report implies.
2. `test_module_imports.py` was run against this modified tree.
3. Both new tests **failed**, with `TestConstImportsAreDefined` reporting
   exactly the missing name and file: `{'CONF_COALESCE_SLOW_TIER'} is not
   false : __init__.py imports ['CONF_COALESCE_SLOW_TIER'] from const, but
   const.py does not define them`.
4. The modification was reverted; the full suite was re-run three times to
   confirm a return to deterministic passing.

This is stronger evidence than a hypothetical: the new test is proven to
catch the specific reported failure mode, not merely assumed to.

### 4.1 A methodological note

An initial attempt to reproduce this adversarially by copying the tree to a
new directory and running `pytest .` there hit an unrelated pytest
rootdir/conftest resolution issue (`__init__.py` — the package's own file —
being unintentionally imported during conftest collection when invoked from
certain directory depths, unrelated to Home Assistant or this defect).
Rather than debug pytest's directory-resolution internals, the adversarial
test was performed by mutating and reverting the file in place inside the
already-working `tests/` invocation context, which is simpler, faster, and
uses infrastructure already proven reliable throughout this project. This is
recorded because it is the kind of tooling detour that has previously
consumed disproportionate effort in this project, and side-stepping it
promptly here reflects that lesson.

## 5. Test evidence

- **469 passed, 1 skipped, 0 failed**, deterministic across repeated runs
  (was 467; two new methods in `TestConstImportsAreDefined`).
- No production code file changed in this release. `register_cache.py`,
  `update_coordinator.py`, `adaptive_modbus.py`, `const.py`, `__init__.py`,
  `config_flow.py` are byte-identical to the audited v1.3.5 tree.
- Static: all Python files parse; all JSON valid; manifest = 1.3.6.
- Confidentiality sweep: clean, no field data or real serials present.

## 6. Safety properties

Unaffected — no behavioural code changed. All properties verified in
`AUDIT_1.3.5.md` §7 (read-only, no data loss, fault isolation, learning gate,
storage, bounded resources) continue to hold, since the underlying code is
identical to what that audit already covered.

## 7. Outstanding housekeeping (unrelated to this release, noted for
completeness)

`AUDIT_1.3.2.md` does not exist in the archived release history — it was
never written for that hotfix, though `CLAUDE.md`'s changelog entry for
v1.3.2 is complete and accurate. Not reconstructed here to avoid introducing
speculative detail into the audit trail; flagged for the record.

## 8. Recommended deployment procedure

1. **Delete** `/config/custom_components/huawei_solar` entirely.
2. Extract v1.3.6 fresh into `custom_components/`.
3. Restart Home Assistant.
4. Confirm via the integration's log that setup completes without an
   `ImportError`.

**Verdict:** release-ready. The proximate cause of the outage was most
likely a partial deployment rather than a defect in the shipped v1.3.5
package (verified directly), but investigating it surfaced and closed a
genuine, previously-unrecognised coverage gap — the actual HA entry point
was untested — with a general, dependency-free structural check, adversarially
proven against a faithful reproduction of the reported failure.
