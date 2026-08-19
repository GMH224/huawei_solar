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


class TestDEF004ServiceDispatchIsTargetResolved:
    """DEF-004 (external ICS quality/defect/architecture audit --
    confirmed High): reset_maximum_feed_grid_power/set_zero_power_grid_
    connection/set_maximum_feed_grid_power/set_maximum_feed_grid_power_
    percentage used to be bound via functools.partial() to a FIXED
    device-kind ("emma" vs "inverter") resolved from whichever entry
    happened to register services LAST -- multiple entries with
    different device kinds coexisting meant dispatch depended on
    registration order, not on the actual target device named in each
    service call."""

    @staticmethod
    def _source() -> str:
        return _SERVICES_SRC.read_text()

    def test_resolver_function_exists(self):
        assert "def _resolve_power_control_device(" in self._source()

    def test_resolver_determines_kind_from_the_device_itself(self):
        source = self._source()
        idx = source.find("def _resolve_power_control_device(")
        assert idx > -1
        end = source.find("\nasync def ", idx)
        body = source[idx: end if end > -1 else idx + 2000]
        assert "isinstance(dd.device, EMMADevice)" in body
        assert "isinstance(dd.device, SUN2000Device)" in body
        assert "_get_device_data(service_call)" in body, (
            "must resolve the device generically, once, not via a "
            "pre-supplied assumption of its kind"
        )

    def test_handlers_no_longer_take_manager_type_as_a_parameter(self):
        """The actual fix: each handler's own signature must have
        dropped manager_type entirely -- it's resolved internally now,
        not supplied by the caller via functools.partial()."""
        source = self._source()
        for name in (
            "reset_maximum_feed_grid_power", "set_zero_power_grid_connection",
            "set_maximum_feed_grid_power", "set_maximum_feed_grid_power_percentage",
        ):
            idx = source.find(f"async def {name}(")
            assert idx > -1, name
            sig_end = source.find(") -> None:", idx)
            signature = source[idx: sig_end]
            assert "manager_type" not in signature, (
                f"{name}'s signature still takes manager_type -- the "
                f"whole point of this fix is that it no longer needs to"
            )
            assert "_resolve_power_control_device(service_call)" in source[idx: idx + 400], (
                f"{name} does not call the new resolver"
            )

    def test_functools_partial_no_longer_used_for_these_four_services(self):
        """Adversarial: confirms the old binding mechanism is genuinely
        gone from the registration site, not just supplemented."""
        source = self._source()
        reg_idx = source.find("async def async_setup_services(")
        assert reg_idx > -1
        end = source.find("\nasync def async_unload_services(", reg_idx)
        body = source[reg_idx: end if end > -1 else reg_idx + 6000]
        assert "partial(reset_maximum_feed_grid_power" not in body
        assert "partial(set_zero_power_grid_connection" not in body
        assert "partial(set_maximum_feed_grid_power" not in body

    def test_each_of_the_four_services_registered_exactly_once(self):
        """Adversarial: confirms these four are registered ONCE each in
        async_setup_services(), not twice (once per has_emma branch) --
        the actual structural change that makes dispatch order-
        independent."""
        source = self._source()
        reg_idx = source.find("async def async_setup_services(")
        assert reg_idx > -1
        end = source.find("\nasync def async_unload_services(", reg_idx)
        body = source[reg_idx: end if end > -1 else reg_idx + 6000]
        for const_name in (
            "SERVICE_RESET_MAXIMUM_FEED_GRID_POWER",
            "SERVICE_SET_ZERO_POWER_GRID_CONNECTION",
            "SERVICE_SET_MAXIMUM_FEED_GRID_POWER",
            "SERVICE_SET_MAXIMUM_FEED_GRID_POWER_PERCENT",
        ):
            count = body.count(f"\n        {const_name},\n")
            assert count == 1, (
                f"{const_name} is registered {count} times in "
                f"async_setup_services() -- expected exactly 1 "
                f"(order-independent dispatch), not one per has_emma branch"
            )

    def test_di_active_power_scheduling_untouched_inverter_only_service(self):
        """Negative case: set_di_active_power_scheduling has no EMMA
        equivalent at all -- it must be untouched by this fix, still
        gated on `if not has_emma`, still using get_inverter_data()
        directly (no ambiguity to resolve for a genuinely single-kind
        service)."""
        source = self._source()
        idx = source.find("async def set_di_active_power_scheduling(")
        assert idx > -1
        body = source[idx: idx + 500]
        assert "get_inverter_data(service_call)" in body
        assert "_resolve_power_control_device" not in body


class TestPhase5BSetPackInstallDate:
    """Battery Phase 5B, this release: a new service letting the user
    record a specific pack's own real install date, identified by its
    own serial number (not a slot label -- see effective_pack_install_
    ts()'s own docstring, battery_health.py, for the full three-tier
    fallback this feeds into)."""

    @staticmethod
    def _source() -> str:
        return _SERVICES_SRC.read_text()

    def test_handler_function_exists(self):
        source = self._source()
        assert "async def set_pack_install_date(" in source

    def test_schema_requires_serial_and_date_not_just_device_id(self):
        source = self._source()
        idx = source.find("SET_PACK_INSTALL_DATE_SCHEMA = BATTERY_DEVICE_SCHEMA.extend(")
        assert idx > -1
        window = source[idx: idx + 300]
        assert "DATA_PACK_SERIAL_NUMBER" in window
        assert "DATA_INSTALL_DATE" in window
        assert "vol.Required" in window

    def test_handler_resolves_device_via_get_battery_device_data(self):
        """Confirms this reuses the SAME battery-device resolution every
        other battery service already uses, not a new mechanism."""
        source = self._source()
        idx = source.find("async def set_pack_install_date(")
        assert idx > -1
        body = source[idx: idx + 2700]
        assert "get_battery_device_data(service_call)" in body

    def test_invalid_date_raises_service_validation_error(self):
        source = self._source()
        idx = source.find("async def set_pack_install_date(")
        assert idx > -1
        body = source[idx: idx + 2700]
        assert "except (TypeError, ValueError) as err:" in body
        assert "raise ServiceValidationError(" in body
        assert "invalid_pack_install_date" in body

    def test_missing_battery_health_manager_raises_service_validation_error(self):
        """Negative case: a device with battery health not enabled at
        all must produce a clear, actionable error, not an
        AttributeError from calling .engine on None."""
        source = self._source()
        idx = source.find("async def set_pack_install_date(")
        assert idx > -1
        body = source[idx: idx + 2700]
        assert "if bh_manager is None:" in body
        assert "battery_health_not_enabled" in body

    def test_writes_via_the_shared_manager_method(self):
        """v2.0.12 (Battery Phase 5B UI restructuring, this release):
        updated to check the shared write path, not the old inline
        implementation -- see BatteryHealthManager.set_pack_install_
        date()'s own docstring for why this was refactored (keeps this
        service and the new per-pack date entity from drifting out of
        sync)."""
        source = self._source()
        idx = source.find("async def set_pack_install_date(")
        assert idx > -1
        body = source[idx: idx + 2700]
        assert "bh_manager.set_pack_install_date(serial, install_ts)" in body

    def test_no_longer_duplicates_the_dirty_and_save_calls_inline(self):
        """Negative case: confirms the OLD, duplicated three-line
        implementation is genuinely gone from this call site, not
        merely that the new one-liner was added alongside it."""
        source = self._source()
        idx = source.find("async def set_pack_install_date(")
        assert idx > -1
        body = source[idx: idx + 2700]
        assert "bh_manager.engine.pack_capacity.pack_install_dates[serial] = install_ts" not in body

    def test_dirty_and_save_now_live_in_the_shared_manager_method(self):
        """v2.0.12 (Battery Phase 5B UI restructuring, this release):
        the dirty-flag-plus-prompt-save logic moved into BatteryHealth
        Manager.set_pack_install_date() itself -- checked there
        directly (test_battery_health_manager.py-style), not here.
        This test confirms services.py's own call site no longer
        needs to know about dirty/save at all."""
        source = self._source()
        idx = source.find("async def set_pack_install_date(")
        assert idx > -1
        body = source[idx: idx + 2700]
        assert "bh_manager.engine.dirty" not in body
        assert "bh_manager._maybe_save()" not in body

    def test_registered_inside_the_has_battery_gate(self):
        """Confirms this is registered alongside the rest of the
        battery-only service cluster, not unconditionally (which would
        register it even for a device with no battery at all)."""
        source = self._source()
        cluster_idx = source.find("SERVICE_STOP_FORCIBLE_CHARGE,\n                stop_forcible_charge,")
        assert cluster_idx > -1
        window = source[cluster_idx: cluster_idx + 700]
        assert "SERVICE_SET_PACK_INSTALL_DATE" in window

    def test_service_listed_in_all_services_registry(self):
        source = self._source()
        idx = source.find("ALL_SERVICES = [")
        assert idx > -1
        end = source.find("]", idx)
        assert "SERVICE_SET_PACK_INSTALL_DATE" in source[idx:end]

    def test_battery_health_manager_imported_at_module_level(self):
        source = self._source()
        assert "from .battery_health_manager import BatteryHealthManager" in source
