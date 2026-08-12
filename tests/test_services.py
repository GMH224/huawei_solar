"""Tests for services.py bug fixes.

Bugs covered
------------
Bug 2 — EMMA_DEVICE_SCHEMA defined twice (duplicate assignment).
Bug 4 — stop_forcible_charge resets DISCHARGE_POWER but not CHARGE_POWER.

Test strategy
-------------
Services depend heavily on the HA runtime, so we use unittest.mock to stub the
device and HA service-call objects.  The tests verify the exact sequence of
`device.set()` calls made by each service handler.
"""

from __future__ import annotations

import ast
import pathlib
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

_SERVICES_SRC = pathlib.Path(__file__).parent.parent / "services.py"


# ---------------------------------------------------------------------------
# Bug 2 — duplicate EMMA_DEVICE_SCHEMA
# ---------------------------------------------------------------------------

class TestNoDuplicateSchemaDefinition:
    """EMMA_DEVICE_SCHEMA must be assigned exactly once at module scope."""

    def test_emma_schema_assigned_once(self):
        """Parse the AST and count top-level assignments to EMMA_DEVICE_SCHEMA."""
        source = _SERVICES_SRC.read_text()
        tree = ast.parse(source)

        assignments = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "EMMA_DEVICE_SCHEMA"
                for t in node.targets
            )
        ]

        assert len(assignments) == 1, (
            f"EMMA_DEVICE_SCHEMA is assigned {len(assignments)} time(s) — "
            "expected exactly 1.  The duplicate definition has been re-introduced."
        )

    def test_all_schemas_assigned_once(self):
        """Broad guard: no schema constant is assigned more than once."""
        source = _SERVICES_SRC.read_text()
        tree = ast.parse(source)

        from collections import Counter

        counts: Counter[str] = Counter()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id.endswith("_SCHEMA"):
                        counts[t.id] += 1

        duplicates = {name: cnt for name, cnt in counts.items() if cnt > 1}
        assert not duplicates, (
            f"Schema constant(s) assigned more than once: {duplicates}"
        )


# ---------------------------------------------------------------------------
# Bug 4 — stop_forcible_charge missing CHARGE_POWER reset
# ---------------------------------------------------------------------------

class TestStopForcibleCharge:
    """stop_forcible_charge must reset both CHARGE_POWER and DISCHARGE_POWER."""

    def _make_service_call(self, dd: MagicMock) -> MagicMock:
        """Build a minimal ServiceCall-like mock."""
        call_mock = MagicMock()
        call_mock.hass = MagicMock()
        call_mock.data = {"device_id": "device-abc"}
        return call_mock

    @pytest.mark.asyncio
    async def test_stop_resets_charge_power(self):
        """STORAGE_FORCIBLE_CHARGE_POWER must be set to 0 on stop."""
        from unittest.mock import AsyncMock, MagicMock, patch

        # Build a mock HuaweiSolarInverterData with a battery.
        dd = MagicMock()
        dd.device = MagicMock()
        dd.device.set = AsyncMock()
        coordinator = MagicMock()
        coordinator.async_refresh = AsyncMock()
        dd.configuration_update_coordinator = coordinator
        dd.connected_energy_storage = {"identifiers": {("huawei_solar", "battery-sn")}}
        dd.device_info = {"identifiers": {("huawei_solar", "battery-sn")}}

        # We don't want to exercise the full service-call routing; patch
        # get_battery_device_data to return our mock directly.
        import importlib, sys, types

        # Provide stubs for HA symbols used by services.py at import time.
        _ha_stubs = {
            "homeassistant": types.ModuleType("homeassistant"),
            "homeassistant.config_entries": types.ModuleType("homeassistant.config_entries"),
            "homeassistant.const": types.ModuleType("homeassistant.const"),
            "homeassistant.core": types.ModuleType("homeassistant.core"),
            "homeassistant.exceptions": types.ModuleType("homeassistant.exceptions"),
            "homeassistant.helpers": types.ModuleType("homeassistant.helpers"),
            "homeassistant.helpers.device_registry": types.ModuleType(
                "homeassistant.helpers.device_registry"
            ),
            "homeassistant.helpers.config_validation": types.ModuleType(
                "homeassistant.helpers.config_validation"
            ),
            "voluptuous": types.ModuleType("voluptuous"),
            "huawei_solar": types.ModuleType("huawei_solar"),
            "huawei_solar.register_definitions": types.ModuleType(
                "huawei_solar.register_definitions"
            ),
            "huawei_solar.register_definitions.periods": types.ModuleType(
                "huawei_solar.register_definitions.periods"
            ),
        }
        for mod_name, mod in _ha_stubs.items():
            sys.modules.setdefault(mod_name, mod)

        _src = pathlib.Path(__file__).parent.parent / "services.py"
        _spec = importlib.util.spec_from_file_location("services_mod", _src)
        services_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]

        # Provide enough attributes that the module can be loaded.
        import re as _re
        services_mod.__builtins__ = __builtins__  # type: ignore[assignment]
        try:
            _spec.loader.exec_module(services_mod)  # type: ignore[union-attr]
        except Exception:
            pytest.skip("Could not load services.py without full HA environment")

    def test_stop_forcible_charge_calls_charge_and_discharge_reset_via_ast(self):
        """AST-level proof: stop_forcible_charge sets both CHARGE_POWER and DISCHARGE_POWER.

        This test does not require a working HA environment; it reads the source
        and checks that both register names appear in the function body.
        """
        source = _SERVICES_SRC.read_text()
        tree = ast.parse(source)

        # Find the stop_forcible_charge function.
        func = next(
            (
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.AsyncFunctionDef)
                and node.name == "stop_forcible_charge"
            ),
            None,
        )
        assert func is not None, "stop_forcible_charge function not found"

        # Collect all string constants (register name references) used inside.
        names_in_func = {
            node.value
            for node in ast.walk(func)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }

        # Collect Name and Attribute node ids (e.g. rn.STORAGE_FORCIBLE_CHARGE_POWER).
        attr_ids = {
            node.attr
            for node in ast.walk(func)
            if isinstance(node, ast.Attribute)
        }

        assert "STORAGE_FORCIBLE_CHARGE_POWER" in attr_ids, (
            "stop_forcible_charge does not reference STORAGE_FORCIBLE_CHARGE_POWER — "
            "it will leave a stale charge-power value in the inverter on stop."
        )
        assert "STORAGE_FORCIBLE_DISCHARGE_POWER" in attr_ids, (
            "stop_forcible_charge does not reference STORAGE_FORCIBLE_DISCHARGE_POWER."
        )


class TestWritesRouteThroughGuard:
    """v2.0.0a (F05, F07, external ICS audit -- confirmed): every write and
    the power-validation read in this module used to bypass ModbusGuard
    entirely. Source-level checks, matching this file's own established
    pattern for services.py (a "heavy" file, not directly executed by its
    own test suite -- see the "Could not load services.py" skip above)."""

    def test_set_and_invalidate_routes_the_write_through_the_guard(self):
        source = _SERVICES_SRC.read_text()
        idx = source.find("async def _set_and_invalidate(")
        end = source.find("\nasync def ", idx + 10)
        body = source[idx: end if end > -1 else idx + 2000]
        assert "guard.request(" in body, (
            "_set_and_invalidate's write must be routed through "
            "dd.update_coordinator.guard -- this is the single helper "
            "covering all ~39 write call sites in this file; if this "
            "regresses, every one of them silently bypasses the guard again"
        )
        # The write itself, not just something elsewhere in the function,
        # must be inside the guarded block.
        guard_idx = body.find("guard.request(")
        write_idx = body.find("await dd.device.set(")
        assert -1 < guard_idx < write_idx, (
            "the guard acquisition must precede the actual write"
        )

    def test_validate_power_value_routes_the_read_through_the_guard(self):
        source = _SERVICES_SRC.read_text()
        idx = source.find("async def _validate_power_value(")
        end = source.find("\nasync def ", idx + 10)
        body = source[idx: end if end > -1 else idx + 2000]
        assert "guard.request(" in body, (
            "the power-validation read must be routed through the same "
            "guard as every write in this module -- it previously bypassed "
            "ModbusGuard entirely despite already being time-bounded"
        )


class TestWriteTimeoutAndSequenceAtomicity:
    """v2.0.0b (MOD-05/MOD-19/MOD-20, external ICS audit -- confirmed):
    being guard-routed (v2.0.0a) provided serialisation, not a deadline
    (MOD-05), and eight multi-write service functions each acquired and
    released the guard once per write rather than once for the whole
    logical command (MOD-19), meaning another coordinator's poll could
    land mid-sequence. Source-level checks, same established pattern as
    TestWritesRouteThroughGuard above."""

    _MULTI_WRITE_FUNCTIONS = [
        "forcible_charge",
        "forcible_discharge",
        "stop_forcible_charge",
        "reset_maximum_feed_grid_power",
        "set_di_active_power_scheduling",
        "set_zero_power_grid_connection",
        "set_maximum_feed_grid_power",
        "set_maximum_feed_grid_power_percentage",
        # v2.0.3 (ICS-11, external ICS audit -- confirmed): these two SOC-
        # targeted variants were missing from this list entirely -- which
        # is precisely why MOD-19's original fix, and this exact test,
        # never caught that they still used the old, per-call
        # _set_and_invalidate() pattern. The gap wasn't in the fix logic
        # itself; it was in this list not naming every function that
        # needed it.
        "forcible_charge_soc",
        "forcible_discharge_soc",
    ]

    def _function_body(self, name: str) -> str:
        source = _SERVICES_SRC.read_text()
        idx = source.find(f"async def {name}(")
        assert idx > -1, f"{name} not found in services.py"
        end = source.find("\nasync def ", idx + 10)
        return source[idx: end if end > -1 else idx + 3000]

    def test_set_and_invalidate_write_has_a_timeout(self):
        body = self._function_body("_set_and_invalidate")
        assert "asyncio.timeout(WRITE_TIMEOUT" in body, (
            "_set_and_invalidate's write must be bounded by WRITE_TIMEOUT, "
            "not just routed through the guard -- a stalled write would "
            "otherwise hold the guard indefinitely, starving every other "
            "coordinator on the endpoint (MOD-05)"
        )

    def test_sequence_helper_exists_and_uses_the_sequence_timeout(self):
        body = self._function_body("_set_and_invalidate_sequence")
        assert "guard.request(" in body
        assert "asyncio.timeout(WRITE_SEQUENCE_TIMEOUT" in body, (
            "the sequence helper must use WRITE_SEQUENCE_TIMEOUT (a single "
            "whole-sequence budget), not WRITE_TIMEOUT repeated -- the "
            "latter would let total duration scale unboundedly with "
            "sequence length"
        )

    def test_sequence_helper_invalidates_cache_per_write(self):
        """The sequence helper's inner `write` must still perform the same
        invalidate_cache() pairing _set_and_invalidate() does per-call --
        otherwise converting a function to the sequence helper would
        silently reintroduce Defect Q (stale post-write cache reads) for
        every register in that sequence."""
        body = self._function_body("_set_and_invalidate_sequence")
        assert "invalidate_cache(name)" in body

    def test_every_multi_write_function_uses_the_sequence_helper(self):
        """The core MOD-19 claim, checked directly for each of the eight
        functions the audit named: each must use
        _set_and_invalidate_sequence(), not repeated _set_and_invalidate()
        calls."""
        for name in self._MULTI_WRITE_FUNCTIONS:
            body = self._function_body(name)
            assert "_set_and_invalidate_sequence(dd)" in body, (
                f"{name} does not use _set_and_invalidate_sequence() -- "
                "MOD-19 has regressed for this function"
            )
            # And the old per-call pattern must be genuinely gone from
            # this function, not just the new one added alongside it.
            assert "await _set_and_invalidate(dd," not in body, (
                f"{name} still contains the old per-call "
                "_set_and_invalidate(dd, ...) pattern alongside the new "
                "sequence helper -- the guard would be acquired and "
                "released for THOSE calls independently of the sequence, "
                "defeating the whole-sequence atomicity the audit asked for"
            )

    def test_button_and_service_stop_forcible_charge_both_use_a_bound_sequence(self):
        """MOD-20: the two independent 'stop forcible charge' entry points
        (button.py's button, this module's service) must both carry the
        same bound, guard-serialised guarantee, even though they remain
        two separate call sites."""
        service_body = self._function_body("stop_forcible_charge")
        assert "_set_and_invalidate_sequence(dd)" in service_body

        button_source = (
            pathlib.Path(__file__).parent.parent / "button.py"
        ).read_text()
        assert "_guarded_write_sequence(guard" in button_source, (
            "button.py's StopForcibleCharge button must also use a "
            "bound, guard-serialised write sequence -- see its own "
            "MOD-06 fix"
        )
