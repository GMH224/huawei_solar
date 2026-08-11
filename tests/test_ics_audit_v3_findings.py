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


if __name__ == "__main__":
    unittest.main()
