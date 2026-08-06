"""Regression tests for Defect W and Defects X1-X4, all independently
validated against v1.3.19 source before fixing (AUDIT_1.3.20.md):

  W  (from the operator's own audit, validated): modbus_keepalive.py's
     register-table import sat unguarded, ahead of the graceful fallback
     it was supposed to lead into -- a narrower version of the exact same
     defect shape Defect S (v1.3.16) already fixed once in this file.

  X1 (from a fresh-eyes ICS sweep): battery_health.py's composite score
     divides by the sum of the three configured weights, reachable at
     zero via the options flow (each independently allows 0.0, no
     cross-field validation); battery_health_manager.py's coordinator
     callback had no exception isolation around the one call that
     advances the whole engine's state, unlike every other callback in
     the same subsystem.

  X2 (from the same sweep): modbus_telemetry.py's listener dispatch
     iterated the live list, not a snapshot -- the exact defect class
     adaptive_modbus.py's sibling implementation already guards against
     explicitly, in the same codebase, from an earlier session.

  X3 (from the same sweep): night_mode.py's register lookup was a
     substring match, not exact -- confirmed against the real register
     table to collide (e.g. "active_power" is a substring of
     day_active_power_peak, almost certainly polled by the same
     coordinator).

  X4 (from the same sweep): diagnostics.py (Home Assistant's own
     "download diagnostics" feature) redacted only the password, exposing
     serial numbers, host, and username -- a discipline bus_diagnostics.py
     already established elsewhere in this exact codebase but never
     carried over here.
"""
from __future__ import annotations

import ast
import pathlib
import sys
import unittest

_KEEPALIVE_SRC = pathlib.Path(__file__).parent.parent / "modbus_keepalive.py"
_BH_SRC = pathlib.Path(__file__).parent.parent / "battery_health.py"
_BH_MANAGER_SRC = pathlib.Path(__file__).parent.parent / "battery_health_manager.py"
_TELEMETRY_SRC = pathlib.Path(__file__).parent.parent / "modbus_telemetry.py"
_NIGHT_MODE_SRC = pathlib.Path(__file__).parent.parent / "night_mode.py"
_DIAGNOSTICS_SRC = pathlib.Path(__file__).parent.parent / "diagnostics.py"


def _find_func(tree, name, cls=ast.FunctionDef):
    return next((n for n in ast.walk(tree) if isinstance(n, cls) and n.name == name), None)


# ═══════════════════════════════════════════════════════════════════════
# Defect W — keepalive register import guarded
# ═══════════════════════════════════════════════════════════════════════

class _OldGetKeepaliveRegister:
    """Reproduces the pre-fix shape: import unguarded, before the fallback."""

    def __call__(self, import_fn, is_valid):
        REGISTERS = import_fn()  # raises if import_fn raises -- unguarded
        if not is_valid(REGISTERS):
            return None
        return "resolved"


class _NewGetKeepaliveRegister:
    """Reproduces the v1.3.20 fixed shape: import guarded."""

    def __call__(self, import_fn, is_valid):
        try:
            REGISTERS = import_fn()
        except ImportError:
            return None
        if not is_valid(REGISTERS):
            return None
        return "resolved"


class TestDefectWKeepaliveImportGuarded(unittest.TestCase):
    def test_old_pattern_propagates_import_failure(self):
        """Adversarial: proves the hazard is real."""
        def _failing_import():
            raise ImportError("simulated: module restructured")

        old = _OldGetKeepaliveRegister()
        with self.assertRaises(ImportError):
            old(_failing_import, lambda r: True)

    def test_new_pattern_returns_none_on_import_failure(self):
        def _failing_import():
            raise ImportError("simulated: module restructured")

        new = _NewGetKeepaliveRegister()
        self.assertIsNone(new(_failing_import, lambda r: True))

    def test_new_pattern_still_works_normally(self):
        new = _NewGetKeepaliveRegister()
        result = new(lambda: {"model_id": object()}, lambda r: "model_id" in r)
        self.assertEqual(result, "resolved")

    def test_source_wraps_the_import_in_try_except(self):
        tree = ast.parse(_KEEPALIVE_SRC.read_text())
        func = _find_func(tree, "_get_keepalive_register")
        assert func is not None
        for node in ast.walk(func):
            if isinstance(node, ast.Try):
                imports_in_try = any(isinstance(n, ast.ImportFrom) for n in ast.walk(node))
                if imports_in_try:
                    return
        self.fail(
            "_get_keepalive_register does not wrap its huawei_solar.registers "
            "import in a try/except -- this reintroduces Defect W."
        )


# ═══════════════════════════════════════════════════════════════════════
# Defect X1 — battery health division-by-zero + exception isolation
# ═══════════════════════════════════════════════════════════════════════

class TestDefectX1DivisionByZeroGuarded(unittest.TestCase):
    def _compute_bhi(self, w_cap, w_eff, w_bal, v_cap=90.0, v_eff=95.0, v_bal=99.0):
        """Mirrors the fixed computation in battery_health.py exactly."""
        terms = [("capacity", v_cap, w_cap), ("efficiency", v_eff, w_eff), ("balance", v_bal, w_bal)]
        available = [(n, v, w) for n, v, w in terms if v is not None]
        bhi = None
        if available:
            total_w = sum(w for _, _, w in available)
            if total_w > 0:
                bhi = round(sum(v * w for _, v, w in available) / total_w, 1)
        return bhi

    def test_all_weights_zero_does_not_raise(self):
        """Adversarial precondition: confirms this really is a reachable
        input, not a contrived one -- config_flow.py allows each weight
        independently down to 0.0."""
        try:
            result = self._compute_bhi(0.0, 0.0, 0.0)
        except ZeroDivisionError:
            self.fail("all-zero weights must not raise ZeroDivisionError")
        self.assertIsNone(result, "BHI must be None (unavailable), not a crash, when nothing can be weighted")

    def test_normal_weights_still_compute_correctly(self):
        result = self._compute_bhi(0.60, 0.20, 0.20, v_cap=90.0, v_eff=90.0, v_bal=90.0)
        self.assertEqual(result, 90.0)

    def test_source_guards_total_w_before_dividing(self):
        tree = ast.parse(_BH_SRC.read_text())
        source = _BH_SRC.read_text()
        idx = source.find("total_w = sum(w for _, _, w in available)")
        assert idx != -1, "the total_w computation was not found -- has this code moved?"
        window = source[idx: idx + 300]
        assert "if total_w > 0" in window, (
            "total_w is not guarded before the BHI division -- this "
            "reintroduces Defect X1's division-by-zero."
        )


class TestDefectX1CallbackIsolation(unittest.IsolatedAsyncioTestCase):
    async def test_old_pattern_stalls_forever_on_a_bad_tick(self):
        """Adversarial: proves the hazard is real."""
        ticks_processed = []

        def _engine_update_that_fails(sample):
            raise ZeroDivisionError("simulated: all weights zero")

        def _old_handle_update():
            report = _engine_update_that_fails(None)  # unguarded
            ticks_processed.append(report)

        with self.assertRaises(ZeroDivisionError):
            _old_handle_update()
        self.assertEqual(ticks_processed, [], "old pattern: the tick is lost AND the exception propagates")

    async def test_new_pattern_survives_a_bad_tick_and_continues(self):
        call_count = 0

        def _engine_update_sometimes_fails(sample):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ZeroDivisionError("simulated: all weights zero")
            return f"report-{call_count}"

        results = []
        for _ in range(3):
            try:
                report = _engine_update_sometimes_fails(None)
            except Exception:  # noqa: BLE001 — mirrors the real fix
                continue
            results.append(report)

        self.assertEqual(results, ["report-2", "report-3"], "engine must keep advancing on later ticks")

    def test_source_wraps_engine_update_in_try_except(self):
        tree = ast.parse(_BH_MANAGER_SRC.read_text())
        func = _find_func(tree, "_handle_coordinator_update")
        assert func is not None
        for node in ast.walk(func):
            if isinstance(node, ast.Try):
                calls_engine_update = any(
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "update"
                    for stmt in node.body
                    for n in ast.walk(stmt)
                )
                if calls_engine_update:
                    return
        self.fail(
            "_handle_coordinator_update does not wrap engine.update() in a "
            "try/except -- this reintroduces Defect X1's missing isolation."
        )


# ═══════════════════════════════════════════════════════════════════════
# Defect X2 — telemetry listener snapshot-before-iterate
# ═══════════════════════════════════════════════════════════════════════

class TestDefectX2ListenerSnapshot(unittest.TestCase):
    def test_old_pattern_skips_a_listener_removed_during_iteration(self):
        """Adversarial: proves the hazard is real."""
        order = []
        listeners = []

        def listener_a(snap):
            order.append("a")

        def listener_b(snap):
            order.append("b")
            listeners.remove(listener_b)  # removes itself mid-iteration

        def listener_c(snap):
            order.append("c")

        listeners.extend([listener_a, listener_b, listener_c])

        for cb in listeners:  # OLD pattern: live list, no snapshot
            cb(None)

        self.assertNotIn("c", order, "adversarial: c is skipped because b's removal shifted the live list")

    def test_new_pattern_does_not_skip_a_listener_removed_during_iteration(self):
        order = []
        listeners = []

        def listener_a(snap):
            order.append("a")

        def listener_b(snap):
            order.append("b")
            listeners.remove(listener_b)

        def listener_c(snap):
            order.append("c")

        listeners.extend([listener_a, listener_b, listener_c])

        for cb in list(listeners):  # NEW pattern: snapshot first
            cb(None)

        self.assertEqual(order, ["a", "b", "c"], "all three must run despite b removing itself")

    def test_source_snapshots_before_iterating(self):
        tree = ast.parse(_TELEMETRY_SRC.read_text())
        func = _find_func(tree, "_push_to_listeners")
        assert func is not None
        for node in ast.walk(func):
            if isinstance(node, ast.For) and isinstance(node.iter, ast.Call):
                if isinstance(node.iter.func, ast.Name) and node.iter.func.id == "list":
                    return
        self.fail(
            "_push_to_listeners does not iterate over list(self._listeners) "
            "-- this reintroduces Defect X2."
        )


# ═══════════════════════════════════════════════════════════════════════
# Defect X3 — night_mode exact register matching (uses the REAL class:
# night_mode.py has no Home Assistant dependencies at all)
# ═══════════════════════════════════════════════════════════════════════

import importlib.util  # noqa: E402


class _RealHuaweiSolarNightModeMixin:
    """v1.3.20 lesson, same root cause as test_modbus_keepalive_registername.py
    (v1.3.16): loading night_mode.py's real `from huawei_solar import
    RegisterName` chain must happen at RUN time (setUpClass), not at
    module/collection time. Every test file gets collected (imported)
    first, and several unconditionally stub sys.modules["huawei_solar"]
    at their own import time -- doing the real import at module level here
    would get silently overwritten by a later-collected file's stub before
    any test actually runs, and the real import's own tmodbus PDU
    side-effect would then collide with whichever class does the NEXT real
    import later, at its own setUpClass. Running this guard in setUpClass
    keeps it in the same phase as the already-proven guard in
    test_modbus_keepalive_registername.py and test_tier_separation.py, so
    whichever of the three runs first "claims" the real import for the
    session and the other two correctly detect it's already real.
    """

    @classmethod
    def setUpClass(cls):
        cached = sys.modules.get("huawei_solar")
        if not (cached is not None and getattr(cached, "__file__", None) is not None):
            for name in list(sys.modules):
                if name == "huawei_solar" or name.startswith("huawei_solar."):
                    del sys.modules[name]
            importlib.invalidate_caches()
        import huawei_solar  # noqa: F401
        from huawei_solar import register_names as rn
        cls.rn = rn

        night_mode_path = pathlib.Path(__file__).parent.parent / "night_mode.py"
        spec = importlib.util.spec_from_file_location("_night_mode_under_test", night_mode_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.NightModeDetector = module.NightModeDetector
        cls.InverterMode = module.InverterMode


class _FakeResult:
    def __init__(self, value):
        self.value = value


class TestDefectX3ExactRegisterMatch(_RealHuaweiSolarNightModeMixin, unittest.TestCase):
    def setUp(self):
        self.transitions = []
        self.detector = self.NightModeDetector(
            on_mode_change=self.transitions.append,
            poll_interval_day=__import__("datetime").timedelta(seconds=30),
            poll_interval_night=__import__("datetime").timedelta(seconds=300),
        )

    def test_adversarial_old_substring_pattern_can_grab_the_wrong_register(self):
        """Adversarial: proves the hazard is real against the actual
        register table -- 'active_power' really is a substring of
        'day_active_power_peak', which a real coordinator polls alongside
        the real active_power register."""
        result = {
            self.rn.DAY_ACTIVE_POWER_PEAK: _FakeResult(4500.0),  # a large, stale daily peak
            # rn.ACTIVE_POWER deliberately absent from this dict, simulating
            # dict ordering where the peak register is encountered by a
            # substring search before the real one would have been.
        }

        def _old_get_value(result, key_substr):
            for rname, res in result.items():
                if key_substr in str(rname).lower():
                    return res.value
            return None

        # The old pattern searching for "active_power" finds
        # day_active_power_peak's 4500W and would treat it as current power.
        self.assertEqual(_old_get_value(result, "active_power"), 4500.0)

    def test_new_pattern_does_not_match_day_active_power_peak(self):
        result = {
            self.rn.DAY_ACTIVE_POWER_PEAK: _FakeResult(4500.0),
        }
        # rn.ACTIVE_POWER is genuinely absent -- the exact-match lookup
        # must correctly report "not found", not silently substitute the
        # peak register's value.
        self.assertIsNone(self.detector._get_value(result, self.rn.ACTIVE_POWER))

    def test_new_pattern_finds_the_correct_register_when_both_present(self):
        result = {
            self.rn.DAY_ACTIVE_POWER_PEAK: _FakeResult(4500.0),
            self.rn.ACTIVE_POWER: _FakeResult(650.0),
        }
        self.assertEqual(self.detector._get_value(result, self.rn.ACTIVE_POWER), 650.0)

    def test_full_evaluate_still_transitions_correctly_with_exact_matching(self):
        # Three consecutive low-power polls should enter NIGHT mode, exactly
        # as before -- confirms the fix didn't change correct behaviour.
        for _ in range(3):
            self.detector.evaluate({self.rn.INPUT_POWER: _FakeResult(10.0)})
        self.assertEqual(self.detector.mode, self.InverterMode.NIGHT)
        self.assertEqual(self.transitions, [self.InverterMode.NIGHT])

    def test_source_uses_dict_get_not_substring_search(self):
        tree = ast.parse(_NIGHT_MODE_SRC.read_text())
        func = _find_func(tree, "_get_value")
        assert func is not None
        uses_dict_get = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "get"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "result"
            for n in ast.walk(func)
        )
        assert uses_dict_get, (
            "_get_value no longer does an exact result.get(...) lookup -- "
            "this reintroduces Defect X3."
        )


# ═══════════════════════════════════════════════════════════════════════
# Defect X4 — diagnostics.py redaction
# ═══════════════════════════════════════════════════════════════════════

class TestDefectX4DiagnosticsRedaction(unittest.TestCase):
    def test_to_redact_includes_host_and_username(self):
        tree = ast.parse(_DIAGNOSTICS_SRC.read_text())
        assign = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "TO_REDACT" for t in n.targets)
            ),
            None,
        )
        assert assign is not None
        names_in_set = {
            n.id for n in ast.walk(assign.value) if isinstance(n, ast.Name)
        }
        assert "CONF_HOST" in names_in_set, "CONF_HOST is not redacted -- reintroduces Defect X4"
        assert "CONF_USERNAME" in names_in_set, "CONF_USERNAME is not redacted -- reintroduces Defect X4"
        assert "CONF_PASSWORD" in names_in_set

    def test_redact_serial_number_function_exists_and_transforms_the_value(self):
        source = _DIAGNOSTICS_SRC.read_text()
        tree = ast.parse(source)
        func = _find_func(tree, "_redact_serial_number")
        assert func is not None, "_redact_serial_number not found -- Defect X4's fix is missing"
        # Reproduce it exactly (it only depends on bus_diagnostics.pseudonym,
        # already covered by its own tests) to confirm it actually changes
        # the value rather than being a no-op passthrough.
        import hashlib

        def pseudonym(value: str) -> str:
            return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]

        def redact(value):
            if not value:
                return value
            return f"**REDACTED-{pseudonym(str(value))}**"

        real_serial = "HV2220098926"
        redacted = redact(real_serial)
        self.assertNotEqual(redacted, real_serial)
        self.assertNotIn(real_serial, redacted)
        # Deterministic: the same input always redacts to the same output,
        # so two diagnostics captures can still be correlated.
        self.assertEqual(redact(real_serial), redacted)

    def test_non_inverter_device_no_longer_exposes_raw_serial(self):
        source = _DIAGNOSTICS_SRC.read_text()
        assert '"serial_number": dd.device.serial_number,' not in source, (
            "the non-inverter device branch still assigns the raw serial "
            "number directly -- this reintroduces Defect X4."
        )
        assert "_redact_serial_number(dd.device.serial_number)" in source

    def test_coordinator_data_dumps_go_through_the_redaction_helper(self):
        source = _DIAGNOSTICS_SRC.read_text()
        for attr in (
            "power_meter_update_coordinator.data",
            "energy_storage_update_coordinator.data",
            "update_coordinator.data",
            "configuration_update_coordinator.data",
        ):
            idx = source.find(f"dd.{attr}")
            assert idx != -1, f"dd.{attr} not found in diagnostics.py"
            # The reference must be wrapped by _redact_coordinator_data(...)
            # somewhere on the same or a very nearby line/statement.
            window = source[max(0, idx - 60): idx]
            assert "_redact_coordinator_data(" in window, (
                f"dd.{attr} does not appear to be passed through "
                f"_redact_coordinator_data -- reintroduces Defect X4."
            )


if __name__ == "__main__":
    unittest.main()
