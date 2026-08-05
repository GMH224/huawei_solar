"""Regression test for Defect I -- coordinator first-poll stagger offsets
were device-blind, causing same-type coordinators on different devices
sharing one ModbusGuard endpoint (daisy-chained inverters) to collide on
their first poll after every boot/reload (AUDIT_1.3.10.md).

Confirmed directly from a debug capture of a real two-inverter reload:
ModbusGuard's adaptively-learned queue depth (1, from 71 days of real
history) is correct for steady-state traffic, but a same-type first-poll
collision between two devices forces shedding + a 10s coordinator retry,
observed costing ~20s per colliding coordinator type.

Two things are tested:

1. BEHAVIOURAL: `_staggered_start_delay()`'s actual arithmetic (reproduced
   here, per this project's established trade-off for __init__.py -- see
   test_learning_gate_unsub.py's HISTORY docstring -- since __init__.py's
   import graph is too heavy for this fast suite) never returns the same
   delay for the same coordinator type on two different device indices,
   and device_index=0 reproduces today's existing, unchanged values exactly
   (no behaviour change for single-device installations).
2. STATIC (AST): __init__.py's four start_delay= call sites must go
   through _staggered_start_delay(kind, device_index), not the raw
   _COORDINATOR_START_DELAYS[kind] dict lookup directly -- the exact shape
   of the original defect.
"""
from __future__ import annotations

import ast
import pathlib
import unittest
from datetime import timedelta

_INIT_SRC = pathlib.Path(__file__).parent.parent / "__init__.py"


# ── 1. Behavioural: the staggering arithmetic itself ───────────────────────

_COORDINATOR_START_DELAYS = {
    "main":          timedelta(seconds=0),
    "power_meter":   timedelta(seconds=7),
    "energy_storage": timedelta(seconds=14),
    "configuration": timedelta(seconds=10),
}
_MULTI_DEVICE_STAGGER_STRIDE = timedelta(seconds=5)


def _staggered_start_delay(kind: str, device_index: int) -> timedelta:
    return _COORDINATOR_START_DELAYS[kind] + device_index * _MULTI_DEVICE_STAGGER_STRIDE


class TestStaggeredStartDelay(unittest.TestCase):
    def test_device_zero_matches_existing_unchanged_offsets(self):
        """Single-device installations (device_index=0, the common case)
        must see byte-identical behaviour to before this fix."""
        for kind, delay in _COORDINATOR_START_DELAYS.items():
            self.assertEqual(_staggered_start_delay(kind, 0), delay)

    def test_second_device_does_not_collide_with_first(self):
        """The actual defect: same-type coordinators on two devices sharing
        one bus must not wake for their first poll at the same offset."""
        for kind in _COORDINATOR_START_DELAYS:
            delay_device0 = _staggered_start_delay(kind, 0)
            delay_device1 = _staggered_start_delay(kind, 1)
            self.assertNotEqual(
                delay_device0, delay_device1,
                f"device 0 and device 1 both get {delay_device0} for "
                f"'{kind}' -- this is the exact collision Defect I fixes.",
            )

    def test_third_device_also_gets_its_own_window(self):
        """Confirms this scales past two devices, not just a hardcoded pair."""
        offsets = {
            _staggered_start_delay("main", i) for i in range(4)
        }
        self.assertEqual(len(offsets), 4, "four devices must get four distinct offsets")

    def test_stride_is_large_enough_to_clear_a_healthy_first_poll(self):
        """A healthy first-poll exchange was observed well under 1s in the
        field capture; the stride must comfortably exceed that with margin,
        or the fix wouldn't actually separate the bursts in practice."""
        self.assertGreaterEqual(_MULTI_DEVICE_STAGGER_STRIDE.total_seconds(), 2.0)


# ── 2. Static: the real call sites must use the staggered helper ──────────

class TestInitPyUsesStaggeredHelper(unittest.TestCase):
    def test_no_raw_coordinator_start_delays_lookup_outside_helper(self):
        source = _INIT_SRC.read_text()
        tree = ast.parse(source)

        helper = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_staggered_start_delay"
            ),
            None,
        )
        assert helper is not None, (
            "_staggered_start_delay() not found in __init__.py -- the "
            "Defect I device-aware stagger helper is missing entirely."
        )

        violations = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id == "_COORDINATOR_START_DELAYS"
            ):
                # The lookup inside the helper itself is fine and expected;
                # anything else is the original, device-blind pattern.
                if not (helper.lineno <= node.lineno <= (helper.end_lineno or helper.lineno)):
                    violations.append(node.lineno)

        assert not violations, (
            f"_COORDINATOR_START_DELAYS[...] is looked up directly, outside "
            f"_staggered_start_delay(), at line(s) {violations} -- this "
            "reintroduces Defect I (device-blind first-poll stagger, "
            "colliding across daisy-chained devices on reload). Route "
            "through _staggered_start_delay(kind, device_index) instead."
        )

    def test_setup_inverter_device_data_accepts_device_index(self):
        tree = ast.parse(_INIT_SRC.read_text())
        func = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef)
                and n.name == "_setup_inverter_device_data"
            ),
            None,
        )
        assert func is not None, "_setup_inverter_device_data not found"
        arg_names = [a.arg for a in func.args.args] + [a.arg for a in func.args.kwonlyargs]
        assert "device_index" in arg_names, (
            "_setup_inverter_device_data no longer accepts device_index -- "
            "without it, per-device staggering cannot be computed."
        )


if __name__ == "__main__":
    unittest.main()
