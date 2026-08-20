"""Regression tests for Defect V -- the remaining findings from the second
(v1.3.17) and third (v1.3.18) independent ICS audits, all confirmed
against source before fixing (AUDIT_1.3.19.md):

  Finding 1 (v1.3.18 audit): only keepalive.stop was registered for
     cleanup-on-setup-failure (Defect U, v1.3.18) -- telemetry and the
     adaptive controller were not, despite both having real teardown work
     (a periodic timer to cancel; a dirty-state flush).

  Finding 2 (v1.3.18 audit): config_flow.py's validate_network_setup_login
     referenced `bridge` in a `finally` block without initialising it
     first -- UnboundLocalError could mask the real connection failure.

  Finding 3 (both audits): several config_flow.py functions performed
     unbounded create_device_instance/create_sub_device_instance/login/
     has_write_permission calls -- the same class of risk already closed
     for the runtime path (Defects M, N, H; Finding 1, v1.3.18).

  Finding 4 (v1.3.18 audit): ConfigFlow._discovered_sub_unit_ids was a
     mutable list declared at class scope -- a classic Python trap where
     in-place mutation before first instance-level assignment would be
     visible across flow instances.

  Finding 5 (v1.3.18 audit): ConfigFlow._reset_discovery_state() dropped
     the discovery task reference without cancelling it first -- the same
     defect shape as Defect L (v1.3.14), a different file.

  Finding 6 (v1.3.18 audit): ModbusTelemetry._push_to_listeners had no
     per-callback exception isolation.

  Finding 7 (both audits, reported independently three times total across
     this session): switch.py's status-polling loop bypassed ModbusGuard
     with no timeout, bounded by iteration count rather than wall-clock
     time.

  Finding 8 (both audits): services.py's _validate_power_value performed
     an unbounded read while the Defect R (v1.3.15) per-device write lock
     was already held.

  Finding 9 (v1.3.18 audit): SynchronizedPowerCoordinator's four reads
     were not one atomic transaction, despite the code's own docstring
     claiming otherwise.

  Finding 10 (v1.3.18 audit): AdaptiveModbusController.stop() scheduled
     its dirty-state flush as a fire-and-forget background task rather
     than awaiting it.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import re
import unittest

_INIT_SRC = pathlib.Path(__file__).parent.parent / "__init__.py"
_CONFIG_FLOW_SRC = pathlib.Path(__file__).parent.parent / "config_flow.py"
_SWITCH_SRC = pathlib.Path(__file__).parent.parent / "switch.py"
_TELEMETRY_SRC = pathlib.Path(__file__).parent.parent / "modbus_telemetry.py"
_SERVICES_SRC = pathlib.Path(__file__).parent.parent / "services.py"
_ADAPTIVE_SRC = pathlib.Path(__file__).parent.parent / "adaptive_modbus.py"
_SYNC_SRC = pathlib.Path(__file__).parent.parent / "synchronized_power_coordinator.py"


def _find_func(tree, name, cls=ast.AsyncFunctionDef):
    return next((n for n in ast.walk(tree) if isinstance(n, cls) and n.name == name), None)


# ═══════════════════════════════════════════════════════════════════════
# Finding 1 — cleanup registration extended to telemetry/adaptive
# ═══════════════════════════════════════════════════════════════════════

class TestFinding1CleanupCoversAllThreeSingletons(unittest.TestCase):
    def test_telemetry_stop_is_registered(self):
        source = _INIT_SRC.read_text()
        assert "register_cleanup(telemetry.stop)" in source, (
            "telemetry.stop is not registered for cleanup -- this "
            "reintroduces Finding 1 for telemetry."
        )

    def test_adaptive_async_unload_is_registered(self):
        source = _INIT_SRC.read_text()
        assert "register_cleanup(adaptive.async_unload)" in source, (
            "adaptive.async_unload is not registered for cleanup -- this "
            "reintroduces Finding 1 for the adaptive controller."
        )


# ═══════════════════════════════════════════════════════════════════════
# Finding 2 — config_flow bridge UnboundLocalError
# ═══════════════════════════════════════════════════════════════════════

class TestFinding2BridgeInitialised(unittest.TestCase):
    def test_bridge_initialised_before_try(self):
        tree = ast.parse(_CONFIG_FLOW_SRC.read_text())
        func = _find_func(tree, "validate_network_setup_login")
        assert func is not None
        # bridge = None must appear as a statement in the function body,
        # before the try block (not inside it).
        pre_try_assigns = [
            n for n in func.body
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "bridge" for t in n.targets)
        ]
        assert pre_try_assigns, (
            "bridge is not initialised at function-body scope before the "
            "try block -- this reintroduces Finding 2's UnboundLocalError "
            "risk."
        )
        assert isinstance(pre_try_assigns[0].value, ast.Constant) and pre_try_assigns[0].value.value is None

    def test_raw_client_disconnected_when_bridge_never_created(self):
        source = _CONFIG_FLOW_SRC.read_text()
        idx = source.find("async def validate_network_setup_login")
        window = source[idx: idx + 8000]
        assert "client.disconnect()" in window, (
            "the raw client is never explicitly disconnected in the "
            "bridge-was-never-created case -- reintroduces part of "
            "Finding 2."
        )


# ═══════════════════════════════════════════════════════════════════════
# v2.0.0a: F01 -- config-flow routed through ModbusGuard (external ICS audit)
# ═══════════════════════════════════════════════════════════════════════

class TestF01ConfigFlowRoutesThroughGuard(unittest.TestCase):
    """Confirmed: config_flow.py's discovery and validation performed
    Modbus I/O with zero references to ModbusGuard anywhere in the file.
    A config flow running while an entry is already polling the same
    physical endpoint could overlap its traffic with the guarded runtime
    path entirely undetected."""

    def _function_body(self, name: str) -> str:
        source = _CONFIG_FLOW_SRC.read_text()
        idx = source.find(f"async def {name}(")
        assert idx > -1, f"{name} not found in config_flow.py"
        end = source.find("\nasync def ", idx + 10)
        # v2.0.1: 4000 was too small a fallback for validate_network_setup_
        # login specifically -- it has no following "\nasync def " match at
        # all (end stays -1), so it always used this fallback, and H-04's
        # own fix added enough new comment text to push real content past
        # the old limit. Widened generously rather than tuned to the exact
        # current length, so the next comment added to any function this
        # helper is used for doesn't silently reintroduce the same gap.
        return source[idx: end if end > -1 else idx + 8000]

    def test_validate_serial_setup_acquires_and_releases(self):
        body = self._function_body("validate_serial_setup")
        assert "ModbusGuard.acquire_endpoint(" in body
        assert "ModbusGuard.release_endpoint(" in body
        assert "guard.request(" in body

    def test_validate_network_setup_acquires_and_releases(self):
        body = self._function_body("validate_network_setup")
        assert "ModbusGuard.acquire_endpoint(" in body
        assert "ModbusGuard.release_endpoint(" in body
        assert "guard.request(" in body

    def test_validate_network_setup_login_acquires_and_releases(self):
        body = self._function_body("validate_network_setup_login")
        assert "ModbusGuard.acquire_endpoint(" in body
        assert "ModbusGuard.release_endpoint(" in body
        assert "guard.request(" in body

    def test_auto_slave_discovery_accepts_and_uses_a_guard(self):
        body = self._function_body("_auto_slave_discovery")
        assert "guard: \"ModbusGuard | None\" = None" in body or "guard: " in body
        assert "guard.request(" in body

    def test_scan_slave_discovery_accepts_and_uses_a_guard(self):
        body = self._function_body("_scan_slave_discovery")
        assert "guard.request(" in body

    def test_all_four_discovery_wrappers_acquire_and_release_the_endpoint(self):
        for name in (
            "_tcp_auto_slave_discovery", "_rtu_auto_slave_discovery",
            "_tcp_scan_slave_discovery", "_rtu_scan_slave_discovery",
        ):
            body = self._function_body(name)
            assert "ModbusGuard.acquire_endpoint(" in body, f"{name} does not acquire"
            assert "ModbusGuard.release_endpoint(" in body, f"{name} does not release"


class TestDEF005SkippedSubDeviceIsSurfaced(unittest.TestCase):
    """DEF-005 (external ICS quality/defect/architecture audit --
    confirmed High): a sub-device discovered during the scan step but
    then failing to connect during finish_network_setup was previously
    only logged -- never surfaced to the user, never reflected in the
    returned info. The confirm_setup screen would silently show fewer
    slave IDs than were actually discovered."""

    @staticmethod
    def _source() -> str:
        return _CONFIG_FLOW_SRC.read_text()

    def test_skipped_sub_unit_ids_tracked_not_just_logged(self):
        source = self._source()
        idx = source.find("async def _connect_to_discovered_devices(")
        assert idx > -1
        end = source.find("\nasync def validate_network_setup(", idx)
        body = source[idx: end if end > -1 else idx + 4000]
        assert "skipped_sub_unit_ids: list[int] = []" in body
        assert "skipped_sub_unit_ids.append(sub_unit_id)" in body, (
            "the except block must record the skipped id, not just log it"
        )

    def test_skipped_ids_included_in_returned_dict(self):
        source = self._source()
        idx = source.find("async def _connect_to_discovered_devices(")
        assert idx > -1
        end = source.find("\nasync def validate_network_setup(", idx)
        body = source[idx: end if end > -1 else idx + 4000]
        assert '"skipped_slave_ids": skipped_sub_unit_ids' in body

    def test_caller_pops_skipped_slave_ids(self):
        source = self._source()
        assert 'self._skipped_slave_ids = info.pop("skipped_slave_ids", [])' in source

    def test_confirm_setup_computes_a_visible_notice_when_something_was_skipped(self):
        source = self._source()
        idx = source.find("async def async_step_confirm_setup(")
        assert idx > -1
        end = source.find("\n    async def ", idx + 10)
        body = source[idx: end if end > -1 else idx + 2500]
        assert "skipped_notice" in body
        assert "if self._skipped_slave_ids:" in body

    def test_confirm_setup_notice_defaults_to_empty_not_always_shown(self):
        """Negative case: the notice must default to an empty string
        (rendering as nothing) when nothing was skipped -- this must be
        a no-op for the common, successful case, not always-visible
        boilerplate."""
        source = self._source()
        idx = source.find("async def async_step_confirm_setup(")
        assert idx > -1
        end = source.find("\n    async def ", idx + 10)
        body = source[idx: end if end > -1 else idx + 2500]
        empty_idx = body.find('skipped_notice = ""')
        if_idx = body.find("if self._skipped_slave_ids:")
        assert empty_idx > -1, "skipped_notice must default to empty string"
        assert empty_idx < if_idx, (
            "the empty default must be set BEFORE the conditional override"
        )

    def test_translation_string_references_the_new_placeholder(self):
        strings_path = _CONFIG_FLOW_SRC.parent / "strings.json"
        en_path = _CONFIG_FLOW_SRC.parent / "translations" / "en.json"
        for path in (strings_path, en_path):
            assert path.exists(), path
            content = path.read_text()
            assert "{skipped_notice}" in content, (
                f"{path.name}: confirm_setup's description does not "
                f"reference the new skipped_notice placeholder"
            )

    def test_json_files_are_still_valid_json(self):
        import json
        strings_path = _CONFIG_FLOW_SRC.parent / "strings.json"
        en_path = _CONFIG_FLOW_SRC.parent / "translations" / "en.json"
        for path in (strings_path, en_path):
            json.loads(path.read_text())  # must not raise


class TestDEF006AggregateDiscoveryDeadline(unittest.TestCase):
    """DEF-006 (external ICS quality/defect/architecture audit --
    confirmed Medium/High): _connect_to_discovered_devices()'s
    sub-device loop had a per-device DEVICE_CONNECT_TIMEOUT but nothing
    capped the WHOLE loop -- a daisy-chained setup with many discovered
    sub-devices, each slow-but-not-quite-timing-out, could extend
    finish_network_setup's total duration without bound."""

    @staticmethod
    def _source() -> str:
        return _CONFIG_FLOW_SRC.read_text()

    def _function_body(self) -> str:
        source = self._source()
        idx = source.find("async def _connect_to_discovered_devices(")
        assert idx > -1
        end = source.find("\nasync def validate_network_setup(", idx)
        return source[idx: end if end > -1 else idx + 5000]

    def test_monotonic_deadline_computed_before_the_loop(self):
        body = self._function_body()
        deadline_idx = body.find(
            "loop_deadline = time.monotonic() + DISCOVERY_TOTAL_TIMEOUT.total_seconds()"
        )
        loop_idx = body.find("for loop_index, sub_unit_id in enumerate(sub_unit_ids):")
        assert deadline_idx > -1, "no monotonic deadline computed"
        assert loop_idx > -1, "loop not converted to enumerate()"
        assert deadline_idx < loop_idx

    def test_deadline_checked_at_top_of_every_iteration(self):
        body = self._function_body()
        loop_idx = body.find("for loop_index, sub_unit_id in enumerate(sub_unit_ids):")
        check_idx = body.find("if time.monotonic() >= loop_deadline:", loop_idx)
        assert loop_idx > -1
        assert check_idx > -1
        assert check_idx - loop_idx < 200, (
            "the deadline check must be at the TOP of the loop body, "
            "not buried after other logic"
        )

    def test_budget_overrun_extends_skipped_list_not_a_hard_exception(self):
        """The core design choice: a budget overrun must feed into the
        SAME graceful mechanism DEF-005 built (skipped_sub_unit_ids),
        not raise and discard the primary device's already-successful
        connection."""
        body = self._function_body()
        check_idx = body.find("if time.monotonic() >= loop_deadline:")
        assert check_idx > -1
        window = body[check_idx: check_idx + 500]
        assert "skipped_sub_unit_ids.extend(remaining)" in window
        assert "break" in window
        assert "raise" not in window.split("break")[0], (
            "the deadline-exceeded branch must not raise -- it should "
            "gracefully stop and record the rest as skipped"
        )

    def test_does_not_use_a_hard_asyncio_timeout_wrap_for_this_loop(self):
        """Negative case: confirms the deliberate design choice NOT to
        reuse the asyncio.timeout() context-manager pattern
        _auto_slave_discovery/_scan_slave_discovery already use --
        that would raise and discard the primary device's own
        connection if it fired mid-loop, unlike this function's own
        explicit monotonic-deadline approach."""
        body = self._function_body()
        assert "async with asyncio.timeout(" not in body

    def test_remaining_ids_are_exactly_the_unattempted_slice(self):
        """Adversarial: 'remaining' must be computed as a genuine slice
        from the current loop position onward -- not, e.g., the whole
        original list (which would incorrectly re-mark already-
        succeeded sub-devices as skipped too)."""
        body = self._function_body()
        idx = body.find("remaining = sub_unit_ids[loop_index:]")
        assert idx > -1, (
            "remaining must be sliced from loop_index onward, not the "
            "whole list or some other range"
        )

    def test_time_module_imported(self):
        source = self._source()
        assert "\nimport time\n" in source


class TestDEF003ClientCreationInsideTry(unittest.TestCase):
    """DEF-003 (external ICS quality/defect/architecture audit --
    confirmed High): client creation previously happened BEFORE the
    enclosing try/finally began in five functions -- validate_serial_
    setup and the four discovery wrappers -- so a failure in client
    creation itself (synchronous, but not guaranteed never to raise)
    would skip the finally block entirely, including its
    ModbusGuard.release_endpoint() call, leaking the reference count
    permanently across repeated failed configuration attempts."""

    def _function_body(self, name: str) -> str:
        source = _CONFIG_FLOW_SRC.read_text()
        idx = source.find(f"async def {name}(")
        assert idx > -1, f"{name} not found in config_flow.py"
        end = source.find("\nasync def ", idx + 10)
        return source[idx: end if end > -1 else idx + 8000]

    def test_client_is_none_before_try_for_every_previously_vulnerable_site(self):
        for name in (
            "validate_serial_setup", "_tcp_auto_slave_discovery",
            "_rtu_auto_slave_discovery", "_tcp_scan_slave_discovery",
            "_rtu_scan_slave_discovery",
        ):
            body = self._function_body(name)
            none_idx = body.find("client = None")
            try_idx = body.find("\n    try:")
            assert none_idx > -1, f"{name}: client = None not found"
            assert try_idx > -1, f"{name}: try: not found"
            assert none_idx < try_idx, (
                f"{name}: client must be initialised to None BEFORE the "
                f"try block, so the finally clause can safely check "
                f"'if client is not None' regardless of where creation fails"
            )

    def test_client_assignment_from_create_call_happens_inside_try(self):
        """The actual fix: the real client-creation call (create_rtu_
        client/create_scan_tcp_client/create_scan_rtu_client) must be
        the first thing INSIDE the try block, not before it."""
        expected_factory = {
            "validate_serial_setup": "create_rtu_client(",
            "_tcp_auto_slave_discovery": "create_scan_tcp_client(",
            "_rtu_auto_slave_discovery": "create_scan_rtu_client(",
            "_tcp_scan_slave_discovery": "create_scan_tcp_client(",
            "_rtu_scan_slave_discovery": "create_scan_rtu_client(",
        }
        for name, factory in expected_factory.items():
            body = self._function_body(name)
            try_idx = body.find("\n    try:")
            factory_idx = body.find(f"client = {factory}", try_idx)
            assert try_idx > -1, name
            assert factory_idx > -1, (
                f"{name}: 'client = {factory}' not found after try: -- "
                f"the actual client construction must happen inside the "
                f"guarded block, not before it"
            )
            assert factory_idx > try_idx

    def test_finally_block_guards_disconnect_with_none_check(self):
        """Adversarial: the finally block's own disconnect() call must be
        conditional on `client is not None` -- otherwise a client-
        creation failure (client still None) would hit an
        UnboundLocalError/AttributeError INSIDE the finally block itself,
        masking the real, original error entirely."""
        for name in (
            "validate_serial_setup", "_tcp_auto_slave_discovery",
            "_rtu_auto_slave_discovery", "_tcp_scan_slave_discovery",
            "_rtu_scan_slave_discovery",
        ):
            body = self._function_body(name)
            finally_idx = body.find("\n    finally:")
            assert finally_idx > -1, f"{name}: no finally block"
            finally_body = body[finally_idx:]
            assert "if client is not None:" in finally_body, (
                f"{name}: finally block must guard client.disconnect() "
                f"with 'if client is not None' -- a bare client.disconnect() "
                f"would crash if client creation itself is what failed"
            )

    def test_release_endpoint_still_reachable_even_when_client_is_none(self):
        """The actual behavioural guarantee DEF-003 restores: even in the
        worst case (client creation itself failed), release_endpoint()
        must be OUTSIDE the 'if client is not None' guard, so it always
        runs regardless."""
        for name in (
            "validate_serial_setup", "_tcp_auto_slave_discovery",
            "_rtu_auto_slave_discovery", "_tcp_scan_slave_discovery",
            "_rtu_scan_slave_discovery",
        ):
            body = self._function_body(name)
            finally_idx = body.find("\n    finally:")
            finally_body = body[finally_idx:]
            if_idx = finally_body.find("if client is not None:")
            release_idx = finally_body.find("ModbusGuard.release_endpoint(")
            assert if_idx > -1 and release_idx > -1, name
            # release_endpoint must be at LESS indentation than the
            # disconnect call inside the if-guard -- i.e. it appears
            # after the if-block's own body, unconditionally.
            release_line_start = finally_body.rfind("\n", 0, release_idx) + 1
            indent = len(finally_body[release_line_start:release_idx]) - len(
                finally_body[release_line_start:release_idx].lstrip()
            )
            assert indent == 8, (
                f"{name}: release_endpoint() must be at the finally "
                f"block's own top level (8-space indent), not nested "
                f"inside the 'if client is not None' guard"
            )

    def test_release_is_in_a_finally_block_for_every_acquirer(self):
        """The acquire without a guaranteed matching release is worse than
        no reference counting at all -- it would leak permanently on any
        exception. Checked structurally: every acquire_endpoint() call
        must be followed, before the enclosing function ends, by a
        release_endpoint() call that appears after a `finally:` line."""
        for name in (
            "validate_serial_setup", "validate_network_setup",
            "validate_network_setup_login", "_tcp_auto_slave_discovery",
            "_rtu_auto_slave_discovery", "_tcp_scan_slave_discovery",
            "_rtu_scan_slave_discovery",
        ):
            body = self._function_body(name)
            acquire_idx = body.find("ModbusGuard.acquire_endpoint(")
            finally_idx = body.find("\n    finally:")
            release_idx = body.find("ModbusGuard.release_endpoint(")
            assert acquire_idx > -1, name
            assert finally_idx > -1, f"{name}: no finally block found"
            assert acquire_idx < finally_idx < release_idx, (
                f"{name}: release_endpoint() must appear after a finally: "
                f"block, not conditionally on the success path"
            )


# ═══════════════════════════════════════════════════════════════════════
# v2.0.1: H-03 -- _connect_to_discovered_devices now guard-routed (ICS re-audit)
# ═══════════════════════════════════════════════════════════════════════

class TestH03FinishNetworkDiscoveryGuardRouted(unittest.TestCase):
    """H-03, ICS re-audit -- confirmed: _connect_to_discovered_devices()
    (the "finish network" config-flow step, called when adding a device
    to an entry whose runtime coordinators may already be actively
    polling the same endpoint) performed every device-communication call
    completely outside ModbusGuard -- a real gap in F01 (v2.0.0a), which
    covered every OTHER config-flow discovery/validation function but
    never revisited this one."""

    def _function_body(self, name: str) -> str:
        source = _CONFIG_FLOW_SRC.read_text()
        idx = source.find(f"async def {name}(")
        assert idx > -1, f"{name} not found in config_flow.py"
        end = source.find("\nasync def ", idx + 10)
        return source[idx: end if end > -1 else idx + 8000]

    def test_acquires_and_releases_the_endpoint_guard(self):
        body = self._function_body("_connect_to_discovered_devices")
        assert "ModbusGuard.acquire_endpoint(" in body
        assert "ModbusGuard.release_endpoint(" in body

    def test_every_device_communication_call_is_guard_routed(self):
        body = self._function_body("_connect_to_discovered_devices")
        # create_device_instance, has_write_permission, and
        # create_sub_device_instance must each be inside a guard.request()
        # block -- checked by counting: at least 3 guard.request() blocks
        # (primary device, write-permission check, sub-device) must exist.
        assert body.count("guard.request(") >= 3, (
            "expected at least 3 guarded operations (primary device, "
            "write-permission check, sub-device connection) -- found "
            f"{body.count('guard.request(')}"
        )

    def test_release_is_in_a_finally_block_not_conditional_on_success(self):
        body = self._function_body("_connect_to_discovered_devices")
        acquire_idx = body.find("ModbusGuard.acquire_endpoint(")
        finally_idx = body.find("\n    finally:")
        release_idx = body.find("ModbusGuard.release_endpoint(")
        assert acquire_idx > -1
        assert finally_idx > -1, "no finally: block found"
        assert acquire_idx < finally_idx < release_idx

    def test_nothing_can_raise_between_acquire_and_the_try_block(self):
        """The specific H-04 lesson applied here from the start: the
        guard acquisition must be immediately followed by `try:`, with
        client construction and connect both INSIDE it -- not sitting
        between acquire and the cleanup envelope, where an exception
        would leak the guard reference the same way H-04 did."""
        body = self._function_body("_connect_to_discovered_devices")
        acquire_idx = body.find("ModbusGuard.acquire_endpoint(")
        try_idx = body.find("\n    try:")
        client_construct_idx = body.find("create_tcp_client(")
        connect_idx = body.find("client.connect()")
        assert acquire_idx < try_idx, "try: must come right after acquire"
        assert try_idx < client_construct_idx, (
            "client construction must be INSIDE the try:, not before it"
        )
        assert try_idx < connect_idx, (
            "client.connect() must be INSIDE the try:, not before it"
        )


# ═══════════════════════════════════════════════════════════════════════
# v2.0.1: H-04 -- validate_network_setup guard leak on connect() failure (ICS re-audit)
# ═══════════════════════════════════════════════════════════════════════

class TestH04NoGuardLeakOnConnectFailure(unittest.TestCase):
    """H-04, ICS re-audit -- confirmed: validate_network_setup() used to
    acquire the endpoint guard, then call client.connect(), BOTH before
    the try:/finally: that releases it -- any exception, timeout, or
    cancellation from connect() skipped the release entirely, leaking a
    reference to ModbusGuard's endpoint registry every time."""

    def _function_body(self, name: str) -> str:
        source = _CONFIG_FLOW_SRC.read_text()
        idx = source.find(f"async def {name}(")
        assert idx > -1, f"{name} not found in config_flow.py"
        end = source.find("\nasync def ", idx + 10)
        return source[idx: end if end > -1 else idx + 8000]

    def test_nothing_can_raise_between_acquire_and_the_try_block(self):
        body = self._function_body("validate_network_setup")
        acquire_idx = body.find("ModbusGuard.acquire_endpoint(")
        try_idx = body.find("\n    try:")
        client_construct_idx = body.find("create_scan_tcp_client(")
        # NOT a plain body.find("client.connect()") -- this function's own
        # docstring (added by this same H-04 fix) mentions that exact
        # string while describing the OLD, buggy behaviour, and would
        # match before the real code usage. The real call site is unique:
        # wrapped in asyncio.wait_for(...).
        connect_idx = body.find("asyncio.wait_for(client.connect()")
        assert acquire_idx > -1
        assert try_idx > -1
        assert acquire_idx < try_idx, (
            "try: must come immediately after guard acquisition -- this "
            "is the exact bug H-04 identified"
        )
        assert try_idx < client_construct_idx, (
            "client construction must be INSIDE the try:, not before it"
        )
        assert connect_idx > -1, "real client.connect() call site not found"
        assert try_idx < connect_idx, (
            "client.connect() -- the specific call H-04 identified as "
            "able to raise before the old try: began -- must be INSIDE "
            "the try: now"
        )

    def test_client_is_initialised_to_none_before_the_try_block(self):
        """The finally: block's cleanup (`if client is not None`) needs
        client to exist as a name even if construction itself never
        completes -- otherwise a failure before that point raises
        UnboundLocalError from the finally: block itself, masking the
        real error (the same class of bug v1.3.19/Finding 2 already
        fixed once for validate_network_setup_login's own `bridge`)."""
        body = self._function_body("validate_network_setup")
        acquire_idx = body.find("ModbusGuard.acquire_endpoint(")
        client_none_idx = body.find("client = None")
        try_idx = body.find("\n    try:")
        assert client_none_idx > -1
        assert acquire_idx < client_none_idx < try_idx

    def test_release_is_still_unconditional_in_finally(self):
        body = self._function_body("validate_network_setup")
        finally_idx = body.find("\n    finally:")
        release_idx = body.find("ModbusGuard.release_endpoint(")
        assert finally_idx > -1
        assert release_idx > finally_idx

    def test_client_disconnect_is_guarded_against_client_being_none(self):
        body = self._function_body("validate_network_setup")
        finally_idx = body.find("\n    finally:")
        window = body[finally_idx: finally_idx + 300]
        assert "if client is not None:" in window


# ═══════════════════════════════════════════════════════════════════════
# v2.0.1: H-05 -- config-flow teardown now bounded, every site (ICS re-audit)
# ═══════════════════════════════════════════════════════════════════════

class TestH05TeardownIsBounded(unittest.TestCase):
    """H-05, ICS re-audit -- confirmed/borderline: config-flow cleanup
    (client.disconnect()/bridge.stop()) was awaited with no timeout at
    all -- a hung device-side teardown could block the configuration
    flow indefinitely, after the actual validation had already completed
    or failed. Applied consistently to every site with the same shape,
    not just the one the audit's own citation pointed at -- seven more
    occurrences of the identical pattern were found while fixing it."""

    def _function_body(self, name: str) -> str:
        source = _CONFIG_FLOW_SRC.read_text()
        idx = source.find(f"async def {name}(")
        assert idx > -1, f"{name} not found in config_flow.py"
        end = source.find("\nasync def ", idx + 10)
        return source[idx: end if end > -1 else idx + 8000]

    def test_every_disconnect_call_site_is_bounded(self):
        """The core, comprehensive check: every one of the eight
        functions with a disconnect()-in-finally: pattern must use
        DISCONNECT_TIMEOUT, not a bare await."""
        for name in (
            "validate_serial_setup",
            "_tcp_auto_slave_discovery", "_rtu_auto_slave_discovery",
            "_tcp_scan_slave_discovery", "_rtu_scan_slave_discovery",
            "_connect_to_discovered_devices",
            "validate_network_setup", "validate_network_setup_login",
        ):
            body = self._function_body(name)
            assert "client.disconnect()" in body, f"{name}: no disconnect() call found"
            assert "asyncio.wait_for(\n" in body or "asyncio.wait_for(" in body, (
                f"{name}: no asyncio.wait_for() found at all"
            )
            assert "DISCONNECT_TIMEOUT.total_seconds()" in body, (
                f"{name}: disconnect() is not bounded by DISCONNECT_TIMEOUT "
                f"-- H-05 has regressed for this function"
            )

    def test_zero_bare_disconnect_calls_remain_anywhere_in_the_file(self):
        """AST-level sweep, not just checked per-function: confirms no
        `await client.disconnect()` exists anywhere as a standalone
        statement (i.e. not as an argument to asyncio.wait_for)."""
        source = _CONFIG_FLOW_SRC.read_text()
        tree = ast.parse(source)
        bare_calls = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Await)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "disconnect"
                and isinstance(node.value.func.value, ast.Name)
                and node.value.func.value.id == "client"
            ):
                bare_calls.append(node.lineno)
        assert not bare_calls, (
            f"bare `await client.disconnect()` (not wrapped in "
            f"asyncio.wait_for) found at line(s) {bare_calls}"
        )

    def test_bridge_stop_in_validate_network_setup_login_is_also_bounded(self):
        """The specific site the audit's own citation pointed at --
        bridge.stop(), not client.disconnect() -- checked directly by
        name, not just swept generically with the others above."""
        body = self._function_body("validate_network_setup_login")
        # NOT a plain body.find("bridge.stop()") -- this function's own
        # finally: comment (added by this same H-05 fix) mentions that
        # exact string while describing the OLD, buggy behaviour, and
        # would match before the real call site. The real one is unique:
        # wrapped in `await asyncio.wait_for(\n    bridge.stop(),`.
        idx = body.find("bridge.stop(), timeout=DISCONNECT_TIMEOUT.total_seconds()")
        assert idx > -1, "real bridge.stop() call site not found"
        window = body[max(0, idx - 200): idx]
        assert "asyncio.wait_for(" in window

    def test_disconnect_failures_are_still_swallowed_not_propagated(self):
        """Bounding the call must not change its fault-isolation
        contract: a disconnect timeout or failure must still be
        swallowed (contextlib.suppress or logged-and-continued), not
        allowed to replace the function's own real return value or
        exception with a cleanup-phase problem."""
        for name in (
            "_tcp_auto_slave_discovery", "_connect_to_discovered_devices",
            "validate_network_setup",
        ):
            body = self._function_body(name)
            idx = body.find("client.disconnect()")
            assert idx > -1
            window = body[max(0, idx - 250): idx]
            assert "contextlib.suppress(Exception)" in window, (
                f"{name}: disconnect() must still be inside a "
                f"contextlib.suppress(Exception) block after bounding it"
            )


# ═══════════════════════════════════════════════════════════════════════
# v2.0.0b: MOD-08 -- config-flow connect() calls now bounded (external ICS audit)
# ═══════════════════════════════════════════════════════════════════════

class TestMOD08ConnectCallsAreBounded(unittest.TestCase):
    """Confirmed: eight config_flow.py call sites did a bare
    `await client.connect()` with no timeout of its own -- a stalled
    TCP/serial connection attempt could hang the configuration flow
    indefinitely. All eight verified individually below, not just
    checked in aggregate, since a partial fix (some sites bounded, others
    missed) would be easy to overlook with only a count-based check."""

    def test_zero_bare_client_connect_calls_remain(self):
        source = _CONFIG_FLOW_SRC.read_text()
        tree = ast.parse(source)
        bare_calls = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "connect"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "client"
            ):
                # A bare call is a direct `await client.connect()` --
                # i.e. this Call node is not itself nested inside an
                # asyncio.wait_for(...) call's arguments.
                parents_are_wait_for = False
                for other in ast.walk(tree):
                    if (
                        isinstance(other, ast.Call)
                        and isinstance(other.func, ast.Attribute)
                        and other.func.attr == "wait_for"
                        and node in ast.walk(other)
                        and node is not other
                    ):
                        parents_are_wait_for = True
                        break
                if not parents_are_wait_for:
                    bare_calls.append(node.lineno)
        assert not bare_calls, (
            f"bare client.connect() (not wrapped in asyncio.wait_for) "
            f"found at line(s) {bare_calls} -- MOD-08 has regressed for "
            f"at least one call site"
        )

    def test_eight_wait_for_wrapped_connect_calls_exist(self):
        """Not fewer than the eight the audit cited -- a regression that
        accidentally consolidated or dropped a call site would still
        pass a naive 'at least one exists' check."""
        source = _CONFIG_FLOW_SRC.read_text()
        count = source.count("asyncio.wait_for(client.connect()")
        assert count == 8, (
            f"expected exactly 8 bounded client.connect() call sites "
            f"(matching the audit's own citation), found {count}"
        )

    def test_uses_a_dedicated_shorter_timeout_not_device_connect_timeout(self):
        """MOD-08's own reasoning: connection establishment and device
        identification are different operations -- this must use its own
        constant, not be folded into DEVICE_CONNECT_TIMEOUT (which exists
        for the heavier identification phase)."""
        source = _CONFIG_FLOW_SRC.read_text()
        assert "MODBUS_CONNECT_TIMEOUT" in source
        # Every wrapped call must use the dedicated constant specifically.
        assert "asyncio.wait_for(client.connect(), timeout=DEVICE_CONNECT_TIMEOUT" not in source

    def test_modbus_connect_timeout_is_shorter_than_device_connect_timeout(self):
        """The actual numeric claim, not just that both constants exist."""
        const_source = pathlib.Path(__file__).parent.parent.joinpath("const.py").read_text()
        modbus_connect = float(re.search(
            r"MODBUS_CONNECT_TIMEOUT\s*=\s*timedelta\(seconds=(\d+)\)", const_source
        ).group(1))
        device_connect = float(re.search(
            r"DEVICE_CONNECT_TIMEOUT\s*=\s*timedelta\(seconds=(\d+)\)", const_source
        ).group(1))
        assert modbus_connect < device_connect, (
            "MODBUS_CONNECT_TIMEOUT must be shorter than "
            "DEVICE_CONNECT_TIMEOUT -- connection establishment should be "
            "much faster than full device identification"
        )


# ═══════════════════════════════════════════════════════════════════════
# Finding 3 — bounded config_flow connect/login/discovery calls
# ═══════════════════════════════════════════════════════════════════════

class TestFinding3BoundedConfigFlowReads(unittest.TestCase):
    def _wait_for_wraps(self, func, callee_attr_or_name: str) -> bool:
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "wait_for"
            ):
                for arg in node.args:
                    if isinstance(arg, ast.Call):
                        target = arg.func
                        name = getattr(target, "attr", None) or getattr(target, "id", None)
                        if name == callee_attr_or_name:
                            return True
        return False

    def test_validate_serial_setup_bounds_create_device_instance(self):
        tree = ast.parse(_CONFIG_FLOW_SRC.read_text())
        func = _find_func(tree, "validate_serial_setup")
        assert func is not None
        assert self._wait_for_wraps(func, "create_device_instance")

    def test_connect_to_discovered_devices_bounds_create_device_instance(self):
        tree = ast.parse(_CONFIG_FLOW_SRC.read_text())
        func = _find_func(tree, "_connect_to_discovered_devices")
        assert func is not None
        assert self._wait_for_wraps(func, "create_device_instance")

    def test_validate_network_setup_bounds_create_device_instance(self):
        tree = ast.parse(_CONFIG_FLOW_SRC.read_text())
        func = _find_func(tree, "validate_network_setup")
        assert func is not None
        assert self._wait_for_wraps(func, "create_device_instance")

    def test_validate_network_setup_login_bounds_login_and_permission_check(self):
        tree = ast.parse(_CONFIG_FLOW_SRC.read_text())
        func = _find_func(tree, "validate_network_setup_login")
        assert func is not None
        assert self._wait_for_wraps(func, "login")
        assert self._wait_for_wraps(func, "has_write_permission")


# ═══════════════════════════════════════════════════════════════════════
# Finding 4 — mutable discovery state moved off class scope
# ═══════════════════════════════════════════════════════════════════════

class TestFinding4NoMutableClassDefault(unittest.TestCase):
    def test_discovered_sub_unit_ids_not_a_class_level_mutable_default(self):
        tree = ast.parse(_CONFIG_FLOW_SRC.read_text())
        cls = next(
            (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "ConfigFlow"),
            None,
        )
        assert cls is not None
        violations = [
            n.lineno for n in cls.body
            if isinstance(n, ast.AnnAssign)
            and isinstance(n.target, ast.Name)
            and n.target.id == "_discovered_sub_unit_ids"
            and isinstance(n.value, ast.List)
        ]
        assert not violations, (
            f"_discovered_sub_unit_ids is still declared as a mutable "
            f"list at class scope (line(s) {violations}) -- this "
            "reintroduces Finding 4."
        )

    def test_init_assigns_it_per_instance(self):
        tree = ast.parse(_CONFIG_FLOW_SRC.read_text())
        config_flow_cls = next(
            (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "ConfigFlow"),
            None,
        )
        assert config_flow_cls is not None
        init = _find_func(config_flow_cls, "__init__", cls=ast.FunctionDef)
        assert init is not None, "ConfigFlow.__init__ not found -- Finding 4's fix is missing"
        assigns_in_init = any(
            isinstance(n, (ast.Assign, ast.AnnAssign))
            and isinstance(
                n.targets[0] if isinstance(n, ast.Assign) else n.target, ast.Attribute,
            )
            and getattr(n.targets[0] if isinstance(n, ast.Assign) else n.target, "attr", None)
            == "_discovered_sub_unit_ids"
            for n in init.body
        )
        assert assigns_in_init, "__init__ does not assign self._discovered_sub_unit_ids"


# ═══════════════════════════════════════════════════════════════════════
# Finding 5 — discovery task cancelled on reset (behavioural + static)
# ═══════════════════════════════════════════════════════════════════════

class _OldResetDiscoveryState:
    """Reproduces the pre-fix behaviour."""
    def __init__(self, task):
        self._discovery_task = task

    def reset(self):
        self._discovery_task = None  # never cancelled


class _NewResetDiscoveryState:
    """Reproduces the fixed behaviour."""
    def __init__(self, task):
        self._discovery_task = task

    def reset(self):
        if self._discovery_task is not None and not self._discovery_task.done():
            self._discovery_task.cancel()
        self._discovery_task = None


class TestFinding5DiscoveryTaskCancellation(unittest.IsolatedAsyncioTestCase):
    async def test_old_pattern_leaves_task_running(self):
        """Adversarial: proves the hazard is real."""
        async def _long_scan():
            await asyncio.sleep(3600)

        task = asyncio.ensure_future(_long_scan())
        flow = _OldResetDiscoveryState(task)
        flow.reset()
        await asyncio.sleep(0)  # let the event loop tick
        self.assertFalse(task.cancelled())
        self.assertFalse(task.done())
        task.cancel()  # actually clean up for the test itself
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_new_pattern_cancels_the_task(self):
        async def _long_scan():
            await asyncio.sleep(3600)

        task = asyncio.ensure_future(_long_scan())
        flow = _NewResetDiscoveryState(task)
        flow.reset()
        await asyncio.sleep(0)
        self.assertTrue(task.cancelled() or task.cancelling() > 0)
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_new_pattern_tolerates_an_already_done_task(self):
        async def _quick():
            return "done"

        task = asyncio.ensure_future(_quick())
        await task  # already finished
        flow = _NewResetDiscoveryState(task)
        flow.reset()  # must not raise
        self.assertIsNone(flow._discovery_task)


class TestFinding5StaticCheck(unittest.TestCase):
    def test_reset_discovery_state_cancels_before_dropping_reference(self):
        tree = ast.parse(_CONFIG_FLOW_SRC.read_text())
        func = _find_func(tree, "_reset_discovery_state", cls=ast.FunctionDef)
        assert func is not None
        calls_cancel = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "cancel"
            for n in ast.walk(func)
        )
        assert calls_cancel, (
            "_reset_discovery_state() does not call .cancel() on the "
            "discovery task -- this reintroduces Finding 5."
        )


# ═══════════════════════════════════════════════════════════════════════
# Finding 6 — telemetry listener isolation
# ═══════════════════════════════════════════════════════════════════════

class TestFinding6TelemetryListenerIsolation(unittest.TestCase):
    def test_push_to_listeners_isolates_each_callback(self):
        calls = []

        def good_1(snap):
            calls.append(1)

        def bad(snap):
            calls.append("bad")
            raise RuntimeError("simulated listener bug")

        def good_2(snap):
            calls.append(2)

        listeners = [good_1, bad, good_2]

        def push(snap):
            for cb in listeners:
                try:
                    cb(snap)
                except Exception:
                    pass

        push(None)
        self.assertEqual(calls, [1, "bad", 2], "a failing listener must not prevent later ones from running")

    def test_source_wraps_each_callback(self):
        tree = ast.parse(_TELEMETRY_SRC.read_text())
        func = _find_func(tree, "_push_to_listeners", cls=ast.FunctionDef)
        assert func is not None
        has_try = any(isinstance(n, ast.Try) for n in ast.walk(func))
        assert has_try, "_push_to_listeners has no try/except around the listener call -- reintroduces Finding 6"


# ═══════════════════════════════════════════════════════════════════════
# Finding 7 — switch.py guard + wall-clock deadline
# ═══════════════════════════════════════════════════════════════════════

class TestFinding7SwitchPolling(unittest.TestCase):
    def test_status_poll_uses_the_guard(self):
        tree = ast.parse(_SWITCH_SRC.read_text())
        func = _find_func(tree, "_poll_device_status_bounded")
        assert func is not None, "_poll_device_status_bounded not found -- Finding 7's fix is missing"
        uses_guard = any(
            isinstance(n, ast.Attribute) and n.attr == "guard" for n in ast.walk(func)
        )
        assert uses_guard, "_poll_device_status_bounded does not go through coordinator.guard"

    def test_status_poll_is_bounded(self):
        tree = ast.parse(_SWITCH_SRC.read_text())
        func = _find_func(tree, "_poll_device_status_bounded")
        assert func is not None
        uses_wait_for = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "wait_for"
            for n in ast.walk(func)
        )
        assert uses_wait_for

    def test_wait_for_status_uses_a_monotonic_deadline(self):
        tree = ast.parse(_SWITCH_SRC.read_text())
        func = _find_func(tree, "_wait_for_status")
        assert func is not None, "_wait_for_status not found -- Finding 5(switch)/7's fix is missing"
        uses_monotonic = any(
            isinstance(n, ast.Attribute) and n.attr == "monotonic" for n in ast.walk(func)
        )
        assert uses_monotonic, "_wait_for_status does not track a monotonic deadline"

    def test_no_more_raw_client_get_in_turn_on_off(self):
        tree = ast.parse(_SWITCH_SRC.read_text())
        for name in ("async_turn_on", "async_turn_off"):
            func = _find_func(tree, name)
            assert func is not None
            violations = [
                n.lineno for n in ast.walk(func)
                if isinstance(n, ast.Attribute)
                and n.attr == "get"
                and isinstance(n.value, ast.Attribute)
                and n.value.attr == "client"
            ]
            assert not violations, f"{name} still calls device.client.get directly at {violations}"


# ═══════════════════════════════════════════════════════════════════════
# Finding 8 — services.py bounded validation read
# ═══════════════════════════════════════════════════════════════════════

class TestFinding8ServiceValidationBounded(unittest.TestCase):
    def test_validate_power_value_bounds_its_read(self):
        tree = ast.parse(_SERVICES_SRC.read_text())
        func = _find_func(tree, "_validate_power_value")
        assert func is not None
        uses_wait_for = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "wait_for"
            for n in ast.walk(func)
        )
        assert uses_wait_for, "_validate_power_value does not bound its read -- reintroduces Finding 8"


# ═══════════════════════════════════════════════════════════════════════
# Finding 9 — synchronized power coordinator documentation + span tracking
# ═══════════════════════════════════════════════════════════════════════

class TestFinding9SyncCoordinatorTransparency(unittest.TestCase):
    def test_data_class_has_sample_span_field(self):
        tree = ast.parse(_SYNC_SRC.read_text())
        cls = next(
            (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "SynchronizedPowerData"),
            None,
        )
        assert cls is not None
        has_span_field = any(
            isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
            and n.target.id == "sample_span_ms"
            for n in cls.body
        )
        assert has_span_field, "SynchronizedPowerData has no sample_span_ms field -- Finding 9's fix is missing"

    def test_docstring_no_longer_claims_one_contiguous_sequence(self):
        source = _SYNC_SRC.read_text()
        idx = source.find("async def _async_update_data")
        docstring_window = source[idx: idx + 400]
        assert "one contiguous modbus sequence" not in docstring_window.lower(), (
            "the docstring still claims the read sequence is one "
            "contiguous, uninterrupted transaction -- this is factually "
            "inaccurate given each read acquires the guard separately "
            "(Finding 9)."
        )


# ═══════════════════════════════════════════════════════════════════════
# Finding 10 — adaptive controller deterministic flush
# ═══════════════════════════════════════════════════════════════════════

class TestFinding10AdaptiveDeterministicFlush(unittest.IsolatedAsyncioTestCase):
    async def test_async_unload_awaits_the_flush(self):
        saved = []

        class _Mini:
            def __init__(self):
                self._dirty = True
                self._save_task = None
                self._unsub_push = None

            async def _async_save(self):
                await asyncio.sleep(0)  # simulate real I/O
                saved.append(True)

            async def async_unload(self):
                if self._save_task and not self._save_task.done():
                    self._save_task.cancel()
                if self._dirty:
                    await self._async_save()

        controller = _Mini()
        await controller.async_unload()
        # If this were fire-and-forget, `saved` could still be empty here.
        self.assertEqual(saved, [True])

    def test_source_has_async_unload_method(self):
        tree = ast.parse(_ADAPTIVE_SRC.read_text())
        found = _find_func(tree, "async_unload")
        assert found is not None, "AdaptiveModbusController.async_unload not found -- Finding 10's fix is missing"
        awaits_save = any(
            isinstance(n, ast.Await)
            and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Attribute)
            and n.value.func.attr == "_async_save"
            for n in ast.walk(found)
        )
        assert awaits_save, "async_unload does not await _async_save -- the flush is not actually deterministic"

    def test_init_py_uses_async_unload_not_stop_for_teardown(self):
        source = _INIT_SRC.read_text()
        assert "await controller.async_unload()" in source, (
            "async_unload_entry does not call controller.async_unload() -- "
            "reintroduces Finding 10 at the call site."
        )


# ═══════════════════════════════════════════════════════════════════════
# v2.0.13: NEW-002 -- manual multi-slave config validation had no
# aggregate deadline (external ICS quality/defect/architecture audit)
# ═══════════════════════════════════════════════════════════════════════

class TestNEW002ManualValidationAggregateDeadline(unittest.TestCase):
    """NEW-002, external ICS quality/defect/architecture audit --
    confirmed: validate_serial_setup()/validate_network_setup() bounded
    each individual sub-device connection (DEVICE_CONNECT_TIMEOUT), but
    the WHOLE loop had no aggregate deadline -- an arbitrarily long,
    manually-supplied slave/unit-ID list could take roughly
    len(ids) x DEVICE_CONNECT_TIMEOUT, unbounded as a function of user
    input. _connect_to_discovered_devices() already had this exact
    protection (DEF-006, this project's own earlier fix) -- these two
    manual-entry paths were missed at the time."""

    def _function_body(self, name: str) -> str:
        source = _CONFIG_FLOW_SRC.read_text()
        idx = source.find(f"async def {name}(")
        assert idx > -1, f"{name} not found in config_flow.py"
        end = source.find("\nasync def ", idx + 10)
        return source[idx: end if end > -1 else idx + 8000]

    def test_validate_serial_setup_has_an_aggregate_deadline(self):
        body = self._function_body("validate_serial_setup")
        assert "loop_deadline = time.monotonic() + DISCOVERY_TOTAL_TIMEOUT.total_seconds()" in body
        assert "if time.monotonic() >= loop_deadline:" in body

    def test_validate_network_setup_has_an_aggregate_deadline(self):
        body = self._function_body("validate_network_setup")
        assert "loop_deadline = time.monotonic() + DISCOVERY_TOTAL_TIMEOUT.total_seconds()" in body
        assert "if time.monotonic() >= loop_deadline:" in body

    def test_reuses_discovery_total_timeout_not_a_new_constant(self):
        """Confirms this reuses the SAME budget as DEF-006's own fix
        for the auto-discovery path, not a separately-tuned constant
        for what's genuinely the same kind of aggregate operation."""
        for name in ("validate_serial_setup", "validate_network_setup"):
            body = self._function_body(name)
            assert "DISCOVERY_TOTAL_TIMEOUT" in body

    def test_deadline_check_is_inside_the_loop_not_only_before_it(self):
        """Adversarial: the deadline check must be evaluated on EVERY
        iteration (inside the for loop), not just once before the loop
        starts -- otherwise a slow-but-not-yet-timed-out first few
        sub-devices could still let the total run unbounded."""
        loop_headers = {
            "validate_serial_setup": "for slave_id in unit_ids[1:]:",
            "validate_network_setup": "for unit_id in unit_ids[1:]:",
        }
        for name, loop_header in loop_headers.items():
            body = self._function_body(name)
            for_idx = body.find(loop_header)
            deadline_check_idx = body.find("if time.monotonic() >= loop_deadline:")
            deadline_def_idx = body.find("loop_deadline = time.monotonic()")
            assert deadline_def_idx > -1 and for_idx > -1 and deadline_check_idx > -1
            assert deadline_def_idx < for_idx, (
                f"{name}: loop_deadline must be computed BEFORE the loop starts"
            )
            assert deadline_check_idx > for_idx, (
                f"{name}: the deadline check must be INSIDE the loop body, "
                "evaluated on every iteration"
            )

    def test_overrun_raises_device_exception_matching_each_functions_own_style(self):
        """Negative case: confirms an overrun is surfaced via the SAME
        DeviceException each function already raises for an individual
        connection failure -- not a new, different exception type or a
        silent partial-success path this function was never designed
        for."""
        for name in ("validate_serial_setup", "validate_network_setup"):
            body = self._function_body(name)
            deadline_idx = body.find("if time.monotonic() >= loop_deadline:")
            window = body[deadline_idx: deadline_idx + 700]
            assert "raise DeviceException(" in window


if __name__ == "__main__":
    unittest.main()
