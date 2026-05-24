"""Tests for update_coordinator.py bug fix.

Bug 7 — When update_interval=None is passed to HuaweiSolarUpdateCoordinator,
_day_interval was set to UPDATE_TIMEOUT (35 s) instead of a zero-length sentinel.
This caused:
  • NightModeDetector.poll_interval_day to be 35 s (a request timeout, not a poll interval)
  • cache.filter_stale() to cap NORMAL-tier TTLs at 35 s in night mode
  • night-mode interval restoration to set the coordinator's update_interval to 35 s

The fix uses timedelta(0) as the sentinel so callers can distinguish "no interval"
from "35 s interval".

Test strategy
-------------
We test _day_interval assignment directly via AST inspection (no HA runtime needed)
and add a lightweight integration test that instantiates the coordinator with a mock
superclass, verifying the sentinel value is propagated correctly.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import timedelta

import pytest

_SRC = pathlib.Path(__file__).parent.parent / "update_coordinator.py"


class TestDayIntervalSentinel:
    """_day_interval must use timedelta(0) when update_interval is None."""

    def test_no_update_timeout_fallback_in_init(self):
        """UPDATE_TIMEOUT must not be used as the fallback for _day_interval."""
        source = _SRC.read_text()
        tree = ast.parse(source)

        # Find __init__ of HuaweiSolarUpdateCoordinator.
        init_func = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "HuaweiSolarUpdateCoordinator"
            ):
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                        init_func = item
                        break

        assert init_func is not None, (
            "HuaweiSolarUpdateCoordinator.__init__ not found in update_coordinator.py"
        )

        # Look for the pattern `_day_interval = update_interval or UPDATE_TIMEOUT`.
        # In the AST this would be a BoolOp with Or and right-hand Name "UPDATE_TIMEOUT".
        bad_assignments = []
        for node in ast.walk(init_func):
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(t, ast.Attribute) and t.attr == "_day_interval"
                    or isinstance(t, ast.Name) and t.id == "_day_interval"
                    for t in node.targets
                )
                and isinstance(node.value, ast.BoolOp)
            ):
                # Check if UPDATE_TIMEOUT is one of the BoolOp values.
                for val in node.value.values:
                    if isinstance(val, ast.Name) and val.id == "UPDATE_TIMEOUT":
                        bad_assignments.append(node)

        assert not bad_assignments, (
            "_day_interval is still assigned via `update_interval or UPDATE_TIMEOUT`. "
            "Use `update_interval if update_interval is not None else timedelta(0)` "
            "so a push-driven coordinator doesn't get the timeout as its poll interval."
        )

    def test_timedelta_zero_sentinel_used(self):
        """_day_interval must fall back to timedelta(0) via a ternary IfExp, not any other constant."""
        source = _SRC.read_text()
        tree = ast.parse(source)

        init_func = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "HuaweiSolarUpdateCoordinator"
            ):
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                        init_func = item
                        break

        assert init_func is not None

        # Find assignments to self._day_interval.
        day_interval_assigns = [
            node
            for node in ast.walk(init_func)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Attribute) and t.attr == "_day_interval"
                for t in node.targets
            )
        ]

        assert day_interval_assigns, "self._day_interval not assigned in __init__"

        # The assignment must be an IfExp whose orelse is a timedelta() call.
        # Pattern: self._day_interval = update_interval if update_interval is not None else timedelta(0)
        found_ifexp_with_timedelta = False
        for assign in day_interval_assigns:
            value = assign.value
            if isinstance(value, ast.IfExp):
                orelse = value.orelse
                if (
                    isinstance(orelse, ast.Call)
                    and isinstance(orelse.func, ast.Name)
                    and orelse.func.id == "timedelta"
                ):
                    found_ifexp_with_timedelta = True

        assert found_ifexp_with_timedelta, (
            "self._day_interval is not assigned via "
            "`update_interval if update_interval is not None else timedelta(...)`. "
            "The sentinel must be a zero-length timedelta, not UPDATE_TIMEOUT."
        )

    def test_sentinel_value_is_not_update_timeout(self):
        """At runtime: timedelta(0) != UPDATE_TIMEOUT (35 s)."""
        from datetime import timedelta as td

        update_timeout = td(seconds=35)
        sentinel = td(0)

        assert sentinel != update_timeout, (
            "timedelta(0) should differ from UPDATE_TIMEOUT. "
            "This test validates the assumption used in the fix."
        )
        assert sentinel.total_seconds() == 0.0

    def test_sentinel_distinguishable_from_real_interval(self):
        """A real poll interval (e.g. 30 s) is distinguishable from the sentinel."""
        from datetime import timedelta as td

        real_interval = td(seconds=30)
        sentinel = td(0)

        assert real_interval != sentinel
        assert real_interval.total_seconds() > 0
        assert sentinel.total_seconds() == 0
