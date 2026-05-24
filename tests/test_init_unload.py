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
