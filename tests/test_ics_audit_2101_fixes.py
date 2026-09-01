"""Regression tests for v2.1.0.1 external-ICS-audit remediation.

Covers the findings that do not live in services.py (those are in
test_services.py):

  ICS-002 (HIGH)   diagnostics.py  — entity registry leaked serial-derived
                                     unique IDs past the redaction boundary
  ICS-003 (MEDIUM) update_coordinator.py — unguarded telemetry call on the
                                     BUSY retry path
  ICS-004 (MEDIUM) services.py, battery_health_manager.py — ISO-8601 offsets
                                     relabelled instead of converted
  ICS-005 (LOW)    strings.json + translations — "voltagey" typo
  ICS-008 (HIGH)   update_coordinator.py — tmodbus transport exceptions
                                     escaped every handler

Real execution wherever the code under test can be exercised directly;
source-level only where loading the full Home Assistant entity stack
would be required to reach one small piece of logic (the established
convention in this project — see test_v2_quality_attrs.py's own header).
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import pathlib
import unittest

_ROOT = pathlib.Path(__file__).parent.parent


# ═══════════════════════════════════════════════════════════════════════════════
# ICS-002 — diagnostics must not leak serial-derived identifiers
# ═══════════════════════════════════════════════════════════════════════════════

class TestDiagnosticsSerialRedaction(unittest.TestCase):
    """The entity registry was exported via entity_entry.extended_dict
    completely unredacted, while the same module already pseudonymised
    serials appearing in register VALUES. Entity unique_ids in this
    integration are built directly from device serials
    (f"{device.serial_number}_{description.key}" and similar, across
    sensor/number/select/switch/button/date/battery_health_entities), and
    diagnostics are explicitly intended to be exported and attached to
    support requests.

    The audit's stated invariant, tested here directly: a generated
    diagnostics payload must contain no raw serial number anywhere in its
    serialised representation.
    """

    def _redact(self):
        """Load _redact_entity_entry directly from the source file.

        A plain `from ..diagnostics import ...` works when this file runs
        alone but fails in a full-suite run: another test module installs
        a stub `homeassistant.components` into sys.modules, after which
        diagnostics.py's own top-level
        `from homeassistant.components.diagnostics import async_redact_data`
        raises ModuleNotFoundError. That is pre-existing suite pollution,
        not a defect in the code under test -- so this loads the function
        in a way that does not depend on test execution order, rather
        than leaving a genuine ICS-002 regression test order-dependent.
        """
        import ast
        import types as _pytypes

        src = (_ROOT / "diagnostics.py").read_text()
        tree = ast.parse(src)
        wanted = {"_redact_entity_entry", "_redact_serial_number"}
        nodes = [
            n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in wanted
        ]
        assert any(n.name == "_redact_entity_entry" for n in nodes), (
            "_redact_entity_entry not found in diagnostics.py"
        )
        module = _pytypes.ModuleType("_diag_under_test")
        from ..bus_diagnostics import pseudonym
        module.__dict__["pseudonym"] = pseudonym
        from typing import Any
        module.__dict__["Any"] = Any
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "<diag>", "exec"),
             module.__dict__)
        return module.__dict__["_redact_entity_entry"]

    def test_unique_id_serial_is_redacted(self):
        redact = self._redact()
        out = redact(
            {"unique_id": "SUN2000-TEST-123456_input_power"},
            {"SUN2000-TEST-123456"},
        )
        self.assertNotIn("SUN2000-TEST-123456", json.dumps(out))

    def test_redaction_is_recursive(self):
        """extended_dict nests (device identifiers, options,
        capabilities) and a serial can appear at any depth."""
        redact = self._redact()
        entry = {
            "unique_id": "SUN2000-TEST-123456_x",
            "device_identifiers": [("huawei_solar", "LUNA-TEST-654321")],
            "nested": {"k": ["SUN2000-TEST-123456_stop_forcible_charge", 42]},
        }
        out = json.dumps(
            redact(entry, {"SUN2000-TEST-123456", "LUNA-TEST-654321"}),
            default=str,
        )
        self.assertNotIn("SUN2000-TEST-123456", out)
        self.assertNotIn("LUNA-TEST-654321", out)

    def test_audit_regression_scenario_zero_raw_serials(self):
        """The audit's own prescribed regression: create entities with
        known synthetic serials, generate diagnostics, recursively search
        the complete representation, expect 0 raw occurrences."""
        redact = self._redact()
        serials = {"SUN2000-TEST-123456", "LUNA-TEST-654321", "PACK-TEST-111111"}
        entry = {
            "unique_id": "SUN2000-TEST-123456_input_power",
            "device_identifiers": [("huawei_solar", "LUNA-TEST-654321")],
            "capabilities": {"packs": ["PACK-TEST-111111_soh"]},
        }
        out = json.dumps(redact(entry, serials), default=str)
        for serial in serials:
            self.assertNotIn(serial, out)

    def test_pseudonym_is_stable_so_captures_stay_comparable(self):
        """Redaction must not destroy a maintainer's ability to compare
        two captures -- the same serial must map to the same pseudonym,
        matching the scheme bus_diagnostics.py already uses."""
        redact = self._redact()
        a = redact({"unique_id": "SN-A_x"}, {"SN-A"})
        b = redact({"unique_id": "SN-A_y"}, {"SN-A"})
        self.assertEqual(
            a["unique_id"].split("_")[0], b["unique_id"].split("_")[0]
        )

    def test_non_serial_content_is_untouched(self):
        """Adversarial: redaction keyed on KNOWN serials, not a guess at
        what a serial looks like -- a pattern guess would both miss real
        serials and mangle unrelated strings."""
        redact = self._redact()
        out = redact(
            {"name": "Inverter input power", "n": 42, "flag": True},
            {"SUN2000-TEST-123456"},
        )
        self.assertEqual(out["name"], "Inverter input power")
        self.assertEqual(out["n"], 42)
        self.assertIs(out["flag"], True)

    def test_empty_serial_set_does_not_crash(self):
        """A config entry whose devices have no serial yet (early setup)
        must not break diagnostics generation."""
        redact = self._redact()
        out = redact({"unique_id": "abc"}, set())
        self.assertEqual(out["unique_id"], "abc")

    def test_diagnostics_uses_the_redactor_on_the_entity_registry(self):
        """Structural: the helper existing is not enough -- the payload
        must actually route through it. That gap (mechanism present but
        never invoked) is one this project has hit before."""
        src = (_ROOT / "diagnostics.py").read_text()
        idx = src.find('"entities": {')
        self.assertNotEqual(idx, -1)
        window = src[idx: idx + 500]
        self.assertIn("_redact_entity_entry(", window)
        self.assertNotIn(
            "entity_entry.entity_id: entity_entry.extended_dict", window,
            "the raw extended_dict export is the ICS-002 defect",
        )


# ═══════════════════════════════════════════════════════════════════════════════
# ICS-003 — BUSY retry path must not crash when telemetry is detached
# ═══════════════════════════════════════════════════════════════════════════════

class TestBusyRetryTelemetryGuard(unittest.TestCase):
    """self.telemetry is declared ModbusTelemetry | None and every other
    call site in update_coordinator.py already guards it. The BUSY retry
    path did not, so a 0x06 SLAVE_DEVICE_BUSY on a coordinator with
    telemetry detached turned a recoverable, expected physical condition
    into an AttributeError.
    """

    def test_busy_retry_call_is_guarded(self):
        # Anchors on the actual CALL, not the first textual mention --
        # record_busy_retry() also appears in comments earlier in the
        # file, and searching from those looks backwards at unrelated
        # code. (Found by this test failing while the AST sweep below
        # correctly passed.)
        src = (_ROOT / "update_coordinator.py").read_text()
        idx = src.find("self.telemetry.record_busy_retry()")
        self.assertNotEqual(idx, -1)
        window = src[max(0, idx - 200): idx]
        self.assertIn("if self.telemetry:", window)

    def test_no_unguarded_telemetry_call_remains(self):
        """Adversarial sweep of the whole module rather than the one line
        the audit found -- checked by walking the AST so an enclosing
        guard several lines up is correctly recognised."""
        import ast

        src = (_ROOT / "update_coordinator.py").read_text()
        tree = ast.parse(src)
        unguarded: list[int] = []

        class Visitor(ast.NodeVisitor):
            def __init__(self):
                self.guard_depth = 0

            def visit_If(self, node: ast.If):
                test_src = ast.dump(node.test)
                guards = "attr='telemetry'" in test_src
                if guards:
                    self.guard_depth += 1
                    for child in node.body:
                        self.visit(child)
                    self.guard_depth -= 1
                    for child in node.orelse:
                        self.visit(child)
                else:
                    self.generic_visit(node)

            def visit_Call(self, node: ast.Call):
                f = node.func
                if (
                    isinstance(f, ast.Attribute)
                    and f.attr.startswith("record_")
                    and isinstance(f.value, ast.Attribute)
                    and f.value.attr == "telemetry"
                    and self.guard_depth == 0
                ):
                    unguarded.append(node.lineno)
                self.generic_visit(node)

        Visitor().visit(tree)
        self.assertEqual(
            unguarded, [],
            f"unguarded self.telemetry.record_*() call(s) at line(s) "
            f"{unguarded} -- instrumentation must never turn a "
            f"recoverable Modbus condition into a secondary exception",
        )


# ═══════════════════════════════════════════════════════════════════════════════
# ICS-004 — ISO-8601 offsets must be converted, not relabelled
# ═══════════════════════════════════════════════════════════════════════════════

class TestTimezoneConversion(unittest.TestCase):
    """.replace(tzinfo=utc) RELABELS an aware datetime rather than
    CONVERTING it: "2026-01-01T00:00:00-05:00" became
    2026-01-01T00:00:00Z instead of 2026-01-01T05:00:00Z -- a 5-hour
    error in battery age and install-date calculations.
    """

    @staticmethod
    def _fixed(value: str) -> float:
        """The corrected conversion, mirroring both fixed call sites."""
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.timestamp()

    def test_negative_offset_converts_correctly(self):
        self.assertEqual(
            self._fixed("2026-01-01T00:00:00-05:00"),
            datetime(2026, 1, 1, 5, 0, tzinfo=timezone.utc).timestamp(),
        )

    def test_positive_offset_converts_correctly(self):
        self.assertEqual(
            self._fixed("2026-01-01T00:00:00+10:00"),
            datetime(2025, 12, 31, 14, 0, tzinfo=timezone.utc).timestamp(),
        )

    def test_explicit_utc_is_unchanged(self):
        self.assertEqual(
            self._fixed("2026-01-01T00:00:00Z"),
            datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc).timestamp(),
        )

    def test_naive_input_still_treated_as_utc(self):
        """The documented service contract for naive input is unchanged --
        only aware input behaves differently after the fix."""
        self.assertEqual(
            self._fixed("2026-01-01T00:00:00"),
            datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc).timestamp(),
        )

    def test_old_behaviour_would_have_been_wrong_by_the_offset(self):
        """Adversarial: pins the actual defect, so a revert is visible."""
        value = "2026-01-01T00:00:00-05:00"
        old = datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()
        self.assertEqual((self._fixed(value) - old) / 3600, 5.0)

    def test_both_call_sites_use_the_guarded_form(self):
        for name in ("services.py", "battery_health_manager.py"):
            src = (_ROOT / name).read_text()
            self.assertIn("_dt.tzinfo is None", src, f"{name} not fixed")
            self.assertIn("astimezone(timezone.utc)", src, f"{name} not fixed")


# ═══════════════════════════════════════════════════════════════════════════════
# ICS-005 — translation typo
# ═══════════════════════════════════════════════════════════════════════════════

class TestChargerPhaseBTypo(unittest.TestCase):

    def test_typo_absent_from_every_translation_file(self):
        """The audit cited two files; the typo was actually present in
        18, so this checks all of them rather than the two reported."""
        offenders = []
        for path in [_ROOT / "strings.json", *(_ROOT / "translations").glob("*.json")]:
            if "voltagey" in path.read_text():
                offenders.append(path.name)
        self.assertEqual(offenders, [], f"'voltagey' still present in {offenders}")


# ═══════════════════════════════════════════════════════════════════════════════
# ICS-008 — tmodbus transport exceptions must reach a controlled path
# ═══════════════════════════════════════════════════════════════════════════════

class TestTmodbusExceptionCoverage(unittest.TestCase):
    """The audit's stated cause was wrong but the finding stands.

    It listed ReadException / ServerDeviceBusy / DecodeError as
    unhandled; all of those subclass HuaweiSolarException and were
    already caught. The real gap: tmodbus transport exceptions
    (ServerDeviceBusyError, CRCError, ModbusConnectionError, ...) do NOT
    subclass HuaweiSolarException and escaped every handler.
    Corroborating evidence that they genuinely surface at this layer:
    config_flow.py already imports ModbusConnectionError from tmodbus.
    """

    def test_tmodbus_exceptions_are_not_huawei_solar_exceptions(self):
        """Pins the premise -- if the library hierarchy ever changes so
        that these ARE covered, this test should fail and prompt a
        re-read rather than silently leaving a redundant catch."""
        from huawei_solar.exceptions import HuaweiSolarException
        from tmodbus.exceptions import ServerDeviceBusyError, CRCError

        self.assertFalse(issubclass(ServerDeviceBusyError, HuaweiSolarException))
        self.assertFalse(issubclass(CRCError, HuaweiSolarException))

    def test_tmodbuserror_covers_every_tmodbus_exception(self):
        """The fix catches the base class; verify nothing sits outside
        it, rather than assuming."""
        import tmodbus.exceptions as te
        from tmodbus.exceptions import TModbusError

        outside = [
            n for n in dir(te)
            if not n.startswith("_")
            and isinstance(getattr(te, n), type)
            and issubclass(getattr(te, n), BaseException)
            and not issubclass(getattr(te, n), TModbusError)
        ]
        self.assertEqual(outside, [], f"tmodbus exceptions outside TModbusError: {outside}")

    def test_both_coordinators_catch_tmodbuserror(self):
        """The optimizer coordinator keeps its own separate copy of this
        bookkeeping, so it needs the fix applied separately -- exactly as
        an earlier audit finding (MOD-09) noted for the equivalent fix."""
        src = (_ROOT / "update_coordinator.py").read_text()
        self.assertEqual(
            src.count("except (HuaweiSolarException, TModbusError) as err:"), 2,
            "both the main and optimizer coordinators must catch tmodbus "
            "transport exceptions",
        )

    def test_tmodbuserror_is_imported(self):
        src = (_ROOT / "update_coordinator.py").read_text()
        self.assertIn("from tmodbus.exceptions import TModbusError", src)


if __name__ == "__main__":
    unittest.main()
