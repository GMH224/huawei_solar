"""Tests for the v2.0.15b excitation options-toggle wiring in __init__.py.

Replaces the enable_excitation/disable_excitation SERVICES (removed
entirely) with a config-driven toggle (CONF_EXCITATION_ENABLED, set on
the "Configure" options screen) -- no Developer Tools action required.

Following this project's own established trade-off for __init__.py
(too heavy to import directly for real execution -- see
test_setup_unload_robustness.py's own precedent, and its own docstring
citing test_learning_gate_unsub.py before it): verified statically
(AST) against the real source, using ast.get_source_segment() rather
than a fragile string-offset helper -- properly bounded by the AST's
own understanding of where the function actually ends, not a "find the
next async def, or fall back to N characters" heuristic (a heuristic
that a related test file, test_elevated_permissions_relocation.py, was
itself found to need fixing because of, during this same release).

The actual RUNTIME behaviour this wiring depends on -- enable_excitation()/
disable_excitation() being idempotent, excitation_is_enabled()/
excitation_is_halted() being correct -- is already covered by real
execution in test_excitation_controller.py and test_button_excitation.py;
this file's own job is narrower: confirming __init__.py actually calls
the right method, with the right condition, in the right place.
"""
from __future__ import annotations

import ast
import pathlib
import unittest

_INIT_SRC = pathlib.Path(__file__).parent.parent / "__init__.py"


def _get_function_source(func_name: str) -> str:
    tree = ast.parse(_INIT_SRC.read_text())
    func = next(
        (
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == func_name
        ),
        None,
    )
    assert func is not None, f"{func_name} not found in __init__.py"
    return ast.get_source_segment(_INIT_SRC.read_text(), func) or ""


class TestExcitationTogglingWiring(unittest.TestCase):
    """_setup_inverter_device_data's own excitation wiring -- the direct
    replacement for the now-removed enable_excitation/disable_excitation
    services."""

    def setUp(self):
        self.body = _get_function_source("_setup_inverter_device_data")

    def test_reads_the_excitation_option(self):
        self.assertIn("CONF_EXCITATION_ENABLED", self.body)

    def test_gated_on_elevated_permissions_too(self):
        """Excitation is a write-capable, real-bus-behaviour-changing
        feature -- it must require BOTH the excitation option AND
        elevated_permissions_enabled(entry), matching every other
        write-capable feature in this integration (button.py, number.py,
        select.py, switch.py, services.py all share this exact gate)."""
        idx = self.body.find("CONF_EXCITATION_ENABLED")
        window = self.body[max(0, idx - 300): idx + 100]
        self.assertIn("elevated_permissions_enabled(entry)", window)

    def test_calls_enable_excitation_when_condition_is_true(self):
        self.assertIn("adaptive.enable_excitation()", self.body)

    def test_calls_disable_excitation_in_the_else_branch(self):
        """Adversarial: confirms disable_excitation() is reachable, not
        just present somewhere in the file -- must be the else-branch
        of the same if/else as enable_excitation(), so the option
        genuinely controls both directions, not just turning it on."""
        enable_idx = self.body.find("adaptive.enable_excitation()")
        disable_idx = self.body.find("adaptive.disable_excitation()")
        self.assertGreater(enable_idx, -1)
        self.assertGreater(disable_idx, -1)
        # else: must appear between the two calls, confirming they are
        # genuinely the two branches of one if/else, not two independent,
        # unconditional calls that would both run every time.
        between = self.body[enable_idx:disable_idx]
        self.assertIn("else:", between)

    def test_excitation_wiring_runs_after_async_load(self):
        """async_load() restores any persisted excitation state (see
        AdaptiveModbusController._deserialize()'s own excitation-
        restoration logic) -- the enable/disable decision must be made
        AFTER that restoration, not before, or a restored, in-progress
        schedule could be silently discarded by a disable_excitation()
        call that ran before the restored state was even visible."""
        load_idx = self.body.find("await adaptive.async_load()")
        excitation_idx = self.body.find("CONF_EXCITATION_ENABLED")
        self.assertGreater(load_idx, -1)
        self.assertGreater(excitation_idx, -1)
        self.assertLess(
            load_idx, excitation_idx,
            "async_load() must run before the excitation enable/disable "
            "decision, not after",
        )

    def test_no_leftover_reference_to_the_removed_services(self):
        """Adversarial: confirms the old service-based approach was
        genuinely removed from this function, not left dangling
        alongside the new toggle."""
        self.assertNotIn("SERVICE_ENABLE_EXCITATION", self.body)
        self.assertNotIn("SERVICE_DISABLE_EXCITATION", self.body)


class TestNoExcitationServicesRemainAnywhereInInit(unittest.TestCase):
    """Whole-file check, not scoped to one function -- confirms the
    removed services left no trace anywhere in __init__.py at all."""

    def test_no_service_registration_leftovers(self):
        source = _INIT_SRC.read_text()
        self.assertNotIn("SERVICE_ENABLE_EXCITATION", source)
        self.assertNotIn("SERVICE_DISABLE_EXCITATION", source)
        self.assertNotIn("SERVICE_RESUME_EXCITATION_AFTER_HALT", source)


if __name__ == "__main__":
    unittest.main()
