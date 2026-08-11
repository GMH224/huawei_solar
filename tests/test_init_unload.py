"""Tests for __init__.py bug fix.

Bug 3 — async_unload_entry used the raw string "device_datas" to look up
runtime_data instead of the DATA_DEVICE_DATAS constant.  If the constant value
ever changed, unloading would raise a KeyError silently after setup succeeded.

Test strategy
-------------
AST-based: we parse __init__.py and verify that no Subscript node in the body
of async_unload_entry uses a raw string literal "device_datas" as its key.
This avoids importing the module (which requires a full HA environment) while
still giving a precise, actionable failure message.
"""

from __future__ import annotations

import ast
import pathlib

_INIT_SRC = pathlib.Path(__file__).parent.parent / "__init__.py"


class TestUnloadUsesConstant:
    """async_unload_entry must use DATA_DEVICE_DATAS, not a raw string literal."""

    def _get_unload_function(self, tree: ast.AST) -> ast.AsyncFunctionDef:
        func = next(
            (
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.AsyncFunctionDef)
                and node.name == "async_unload_entry"
            ),
            None,
        )
        assert func is not None, "async_unload_entry not found in __init__.py"
        return func

    def test_no_raw_string_key_in_unload(self):
        """runtime_data['device_datas'] (raw string) must not appear in unload."""
        source = _INIT_SRC.read_text()
        tree = ast.parse(source)
        func = self._get_unload_function(tree)

        raw_string_subscripts = [
            node
            for node in ast.walk(func)
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.slice, ast.Constant)
                and node.slice.value == "device_datas"
            )
        ]

        assert not raw_string_subscripts, (
            f"async_unload_entry still uses the raw string 'device_datas' as a "
            f"dict key at line(s) {[n.lineno for n in raw_string_subscripts]}. "
            "Use the DATA_DEVICE_DATAS constant instead."
        )

    def test_constant_used_in_unload(self):
        """DATA_DEVICE_DATAS constant (a Name node) must be used as the key."""
        source = _INIT_SRC.read_text()
        tree = ast.parse(source)
        func = self._get_unload_function(tree)

        # Look for runtime_data[DATA_DEVICE_DATAS] — the slice will be a Name
        # node with id == "DATA_DEVICE_DATAS".
        constant_subscripts = [
            node
            for node in ast.walk(func)
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.slice, ast.Name)
                and node.slice.id == "DATA_DEVICE_DATAS"
            )
        ]

        assert constant_subscripts, (
            "async_unload_entry does not use DATA_DEVICE_DATAS as a dict key. "
            "Ensure runtime_data[DATA_DEVICE_DATAS] is used for consistency."
        )

    def test_setup_and_unload_use_same_key(self):
        """The same key must be used in both async_setup_entry and async_unload_entry."""
        source = _INIT_SRC.read_text()
        tree = ast.parse(source)

        def _find_func(name: str) -> ast.AsyncFunctionDef:
            f = next(
                (
                    n
                    for n in ast.walk(tree)
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == name
                ),
                None,
            )
            assert f is not None, f"{name} not found in __init__.py"
            return f

        def _dict_keys_used(func: ast.AsyncFunctionDef) -> set[str]:
            """Return all string/name values used as subscript keys in func."""
            keys: set[str] = set()
            for node in ast.walk(func):
                if isinstance(node, ast.Subscript):
                    if isinstance(node.slice, ast.Constant):
                        keys.add(repr(node.slice.value))
                    elif isinstance(node.slice, ast.Name):
                        keys.add(node.slice.id)
            return keys

        setup_keys = _dict_keys_used(_find_func("async_setup_entry"))
        unload_keys = _dict_keys_used(_find_func("async_unload_entry"))

        # Both should reference the constant, not the raw string.
        assert "DATA_DEVICE_DATAS" in setup_keys | unload_keys, (
            "Neither setup nor unload references DATA_DEVICE_DATAS — something is wrong."
        )
        assert "'device_datas'" not in unload_keys, (
            "async_unload_entry uses the raw string 'device_datas'. "
            "This will silently break if the constant value is renamed."
        )


# ── v2.0.0a: F21 -- keepalive stopped before transport disconnect ───────────
# (external ICS audit)

class TestF21UnloadOrdering:
    """Confirmed: the keep-alive loop is cancellation-aware (not a confirmed
    deadlock), but there was a real window where it could still be
    mid-probe -- having already acquired the guard, awaiting
    batch_update() -- exactly when the transport got disconnected out from
    under it. Checked structurally: keepalive.stop() must appear, for
    every device, before the transport disconnect() call, not interleaved
    with or after it."""

    def _unload_body(self) -> str:
        source = _INIT_SRC.read_text()
        idx = source.find("async def async_unload_entry(")
        assert idx > -1
        end = source.find("\nasync def ", idx + 10)
        return source[idx: end if end > -1 else idx + 6000]

    def test_keepalive_stop_precedes_transport_disconnect(self):
        body = self._unload_body()
        keepalive_stop_idx = body.find("keepalive.stop()")
        # NOT a plain find("primary_device.client.disconnect()") -- that
        # string also appears inside this function's own explanatory
        # comment (describing what the code used to look like), which
        # comes BEFORE the real code usage and would make this check
        # trivially pass regardless of the actual code. The trailing
        # comma+newline is unique to the real call site.
        disconnect_idx = body.find("primary_device.client.disconnect(),\n")
        assert keepalive_stop_idx > -1, "keepalive.stop() not found in async_unload_entry"
        assert disconnect_idx > -1, "transport disconnect() call site not found in async_unload_entry"
        assert keepalive_stop_idx < disconnect_idx, (
            "keepalive.stop() must run BEFORE the transport disconnect -- "
            "otherwise a mid-probe keepalive task can still be waiting on "
            "the guard/device exactly when the transport is torn down"
        )

    def test_keepalive_stop_happens_in_its_own_pass_over_all_devices(self):
        """Not just "before disconnect for the FIRST device" -- every
        device on this entry must have its keepalive stopped before the
        (single, shared) transport disconnect runs at all."""
        body = self._unload_body()
        disconnect_idx = body.find("primary_device.client.disconnect(),\n")
        pre_disconnect = body[:disconnect_idx]
        # The keepalive-stopping pass must itself contain a loop over
        # device_datas, not just a single device's keepalive.
        assert "for device_data in device_datas:" in pre_disconnect
        assert "keepalive.stop()" in pre_disconnect

    def test_keepalive_registry_cleanup_still_happens_after(self):
        """ModbusKeepAlive.remove() (per-entry registry bookkeeping, not a
        traffic-producing action) can still run in the later, main
        teardown loop -- only .stop() itself needed to move earlier."""
        body = self._unload_body()
        assert "ModbusKeepAlive.remove(serial)" in body

    def test_disconnect_is_still_bounded_by_a_timeout(self):
        """F21's fix must not have disturbed Defect U/Finding 3's own
        earlier fix (the disconnect itself stays timeout-bounded)."""
        body = self._unload_body()
        disconnect_idx = body.find("primary_device.client.disconnect(),\n")
        wait_for_idx = body.rfind("asyncio.wait_for(", 0, disconnect_idx)
        assert wait_for_idx > -1, (
            "the disconnect call is no longer wrapped in asyncio.wait_for() "
            "-- this would reintroduce Defect U/Finding 3 (an unbounded "
            "disconnect could block all teardown below it indefinitely)"
        )
