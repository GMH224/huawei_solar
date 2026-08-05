"""Regression test for Defect G -- blocking first-refresh calls on the
config-entry setup critical path (AUDIT_1.3.8.md).

Two call sites used to `await coordinator.async_config_entry_first_refresh()`
directly inside the synchronous setup sequence:

  1. `create_optimizer_update_coordinator()` (update_coordinator.py) -- once
     per inverter that has optimizers.
  2. The SynchronizedPowerCoordinator setup block in `async_setup_entry()`
     (__init__.py) -- once per config entry with a meter, battery, or a
     second inverter.

Both are full, real Modbus reads. Awaiting them directly means
`async_setup_entry` cannot return until both complete, on every setup AND
every reload -- the identified cause of Home Assistant's "waiting for
Huawei Solar to start up" banner lasting 2-3 minutes instead of the ~20 s
typical of most integrations, and a plausible reason a slow/cancelled setup
could leave a coordinator created later in the same sequence
(configuration_update_coordinator) never constructed at all.

This is a static (AST) check, following the same project convention used
for Defect F and v1.3.6's TestConstImportsAreDefined: dependency-free, fast,
and targeted at the specific defect class rather than full coverage. It
checks that neither function calls `.async_config_entry_first_refresh()` as
a *direct, top-level-awaited statement of the function's own body* --
walking only each function's immediate body (not descending into nested
async function definitions), since the fix's whole point is that the real
`await ...first_refresh()` call is only reachable from *inside* a nested
background-task coroutine, never inlined into the outer function directly.
"""
from __future__ import annotations

import ast
import pathlib
import unittest

_INIT_SRC = pathlib.Path(__file__).parent.parent / "__init__.py"
_COORD_SRC = pathlib.Path(__file__).parent.parent / "update_coordinator.py"


def _direct_first_refresh_awaits(func_body: list[ast.stmt]) -> list[int]:
    """Find `await X.async_config_entry_first_refresh()` reachable from a
    function's own body through ordinary control flow (if/try/for/while),
    but NOT from inside a nested function definition -- which is exactly
    where the fix puts it on purpose (the background-task wrapper)."""
    violations: list[int] = []

    def _scan(stmts: list[ast.stmt]) -> None:
        for stmt in stmts:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Nested def (the background-task wrapper) -- deliberately
                # not descended into. An await here is the fix, not the bug.
                continue

            # Recurse into ordinary control-flow bodies.
            for field in ("body", "orelse", "finalbody"):
                child = getattr(stmt, field, None)
                if isinstance(child, list):
                    _scan(child)
            for handler in getattr(stmt, "handlers", []) or []:
                _scan(getattr(handler, "body", []))

            # Does this statement contain a nested def anywhere? If so, its
            # non-def content was already handled by the recursion above;
            # skip the flat walk below to avoid double-flagging awaits that
            # live inside that nested def.
            contains_nested_def = any(
                isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                for n in ast.walk(stmt)
                if n is not stmt
            )
            if contains_nested_def:
                continue

            for node in ast.walk(stmt):
                if (
                    isinstance(node, ast.Await)
                    and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Attribute)
                    and node.value.func.attr == "async_config_entry_first_refresh"
                ):
                    violations.append(node.lineno)

    _scan(func_body)
    return sorted(set(violations))


class TestNoBlockingFirstRefreshInSetup(unittest.TestCase):
    def test_optimizer_coordinator_factory_does_not_block_on_first_refresh(self):
        tree = ast.parse(_COORD_SRC.read_text())
        func = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef)
                and n.name == "create_optimizer_update_coordinator"
            ),
            None,
        )
        assert func is not None, "create_optimizer_update_coordinator not found"
        violations = _direct_first_refresh_awaits(func.body)
        assert not violations, (
            f"create_optimizer_update_coordinator awaits "
            f"async_config_entry_first_refresh() directly at line(s) "
            f"{violations} -- this blocks entry setup on a real Modbus read. "
            "Schedule it as a background task instead (Defect G)."
        )

    def test_async_setup_entry_does_not_block_on_sync_coordinator_refresh(self):
        tree = ast.parse(_INIT_SRC.read_text())
        func = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_setup_entry"
            ),
            None,
        )
        assert func is not None, "async_setup_entry not found"
        violations = _direct_first_refresh_awaits(func.body)
        assert not violations, (
            f"async_setup_entry awaits async_config_entry_first_refresh() "
            f"directly at line(s) {violations} -- this blocks entry setup "
            "(and every reload) on a full synchronised power-flow read. "
            "Schedule it as a background task instead (Defect G)."
        )


if __name__ == "__main__":
    unittest.main()
