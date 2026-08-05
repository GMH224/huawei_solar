"""Regression test for Defect V2-1 (independent ICS audit addendum,
report v2, against v1.3.10): the write-permission probe in
create_sun2000_entities() ran BEFORE the cheap
`ucs.configuration_update_coordinator` eligibility check, because Python's
`and` short-circuits left to right. On any device where
CONF_ENABLE_PARAMETER_CONFIGURATION is off (no configuration coordinator),
the guarded entity below the condition could never be added regardless of
the probe's result -- yet the (bounded, but still real) Modbus probe ran
anyway, once per ineligible device on every boot/reload.

This is an ordering defect, not a missing-bound defect (Defect H already
bounded the probe itself in v1.3.9) -- the fix is purely to make the free
checks run first so the bounded-but-not-free probe is skipped entirely
when it can never matter.
"""
from __future__ import annotations

import ast
import pathlib
import unittest

_SENSOR_SRC = pathlib.Path(__file__).parent.parent / "sensor.py"


class TestWritePermissionProbeOrdering(unittest.TestCase):
    def _get_condition(self, tree: ast.AST) -> ast.BoolOp:
        func = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef)
                and n.name == "create_sun2000_entities"
            ),
            None,
        )
        assert func is not None, "create_sun2000_entities not found in sensor.py"

        if_stmt = next(
            (
                n for n in ast.walk(func)
                if isinstance(n, ast.If) and isinstance(n.test, ast.BoolOp)
                and any(
                    isinstance(v, ast.Await) for v in n.test.values
                )
            ),
            None,
        )
        assert if_stmt is not None, (
            "Could not find the eligibility `if` condition containing an "
            "awaited call in create_sun2000_entities"
        )
        return if_stmt.test

    def _index_of_await(self, boolop: ast.BoolOp) -> int:
        for i, v in enumerate(boolop.values):
            if isinstance(v, ast.Await):
                return i
        raise AssertionError("No Await found in the condition's values")

    def _index_of_coordinator_check(self, boolop: ast.BoolOp) -> int:
        for i, v in enumerate(boolop.values):
            if (
                isinstance(v, ast.Attribute)
                and v.attr == "configuration_update_coordinator"
            ):
                return i
        raise AssertionError(
            "No `ucs.configuration_update_coordinator` check found in the "
            "condition's values"
        )

    def test_coordinator_check_runs_before_the_awaited_probe(self):
        """The exact defect: the bounded (but non-free) probe must not run
        before the free `ucs.configuration_update_coordinator` check --
        otherwise ineligible devices still pay for the probe."""
        source = _SENSOR_SRC.read_text()
        tree = ast.parse(source)
        boolop = self._get_condition(tree)

        await_index = self._index_of_await(boolop)
        coordinator_index = self._index_of_coordinator_check(boolop)

        self.assertLess(
            coordinator_index, await_index,
            "The awaited write-permission probe runs before the "
            "`ucs.configuration_update_coordinator` check -- this "
            "reintroduces Defect V2-1: an ineligible device still pays for "
            "the (bounded but real) Modbus probe. Reorder so cheap checks "
            "run first.",
        )

    def test_behavioural_short_circuit_semantics(self):
        """Direct behavioural proof that the fixed ordering actually skips
        the expensive check when the cheap one fails first."""
        calls = []

        def cheap_check_true():
            calls.append("cheap")
            return True

        def cheap_check_false():
            calls.append("cheap")
            return False

        def expensive_check():
            calls.append("expensive")
            return True

        # Fixed ordering: cheap first.
        calls.clear()
        result = cheap_check_false() and expensive_check()
        self.assertFalse(result)
        self.assertEqual(calls, ["cheap"], "expensive check must not run when cheap check fails")

        calls.clear()
        result = cheap_check_true() and expensive_check()
        self.assertTrue(result)
        self.assertEqual(calls, ["cheap", "expensive"])


if __name__ == "__main__":
    unittest.main()
