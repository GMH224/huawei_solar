"""Source-level tests for the v2.0.15 fix moving the elevated-permissions
checkbox off the connection-setup/Reconfigure flow and onto the Configure
(options) screen.

Follows this project's own established convention for config_flow.py
(test_ics_audit_v3_findings.py's own TestF01ConfigFlowRoutesThroughGuard):
source-level checks via a small function/class-body extraction helper,
not full runtime execution -- config_flow.py pulls in a large and
frequently-shifting slice of Home Assistant's own config-entry machinery
that a synthetic stub would need to track indefinitely for little benefit
over checking the actual source directly.

Background: CONF_ENABLE_PARAMETER_CONFIGURATION previously lived only in
the connection-setup screen (async_step_setup_network), which is shared
between initial setup AND "Reconfigure" -- a flow intended specifically
for changing host/port/slave IDs on an already-working deployment. A user
opening Reconfigure only to update an IP address had no way to do so
without also being re-asked to decide this flag, on the same screen, with
no visual distinction between the two. Flagged directly by report after
being pointed there in error.
"""
from __future__ import annotations

import pathlib
import unittest

_CONFIG_FLOW_SRC = pathlib.Path(__file__).parent.parent / "config_flow.py"


def _function_body(name: str, is_async: bool = True) -> str:
    source = _CONFIG_FLOW_SRC.read_text()
    prefix = "async def " if is_async else "def "
    idx = source.find(f"{prefix}{name}(")
    assert idx > -1, f"{name} not found in config_flow.py"
    end = source.find(f"\n{prefix}", idx + len(prefix) + len(name))
    return source[idx: end if end > -1 else idx + 8000]


class TestSetupNetworkOmitsCheckboxDuringReconfigure(unittest.TestCase):
    """async_step_setup_network's own form must not ask for elevated
    permissions when self._reconfigure_entry is set."""

    def setUp(self):
        self.body = _function_body("async_step_setup_network")

    def test_reconfigure_mode_is_detected(self):
        self.assertIn("is_reconfigure = self._reconfigure_entry is not None", self.body)

    def test_elevated_permissions_only_read_from_user_input_when_not_reconfiguring(self):
        idx = self.body.find("if not is_reconfigure:")
        self.assertGreater(idx, -1)
        window = self.body[idx: idx + 200]
        self.assertIn(
            "self._elevated_permissions = user_input[CONF_ENABLE_PARAMETER_CONFIGURATION]",
            window,
        )

    def test_schema_field_is_added_conditionally_not_unconditionally(self):
        """The checkbox must be built into the form's own schema dict
        only inside an `if not is_reconfigure:` guard -- NOT as a bare
        vol.Required(...) entry in the base schema, which would show it
        on every visit to this screen regardless of mode."""
        # There must be no unconditional "vol.Required(\n    CONF_ENABLE_
        # PARAMETER_CONFIGURATION," inside the base schema_dict literal.
        schema_dict_start = self.body.find("schema_dict: dict[Any, Any] = {")
        self.assertGreater(schema_dict_start, -1)
        conditional_start = self.body.find("if not is_reconfigure:", schema_dict_start)
        self.assertGreater(conditional_start, -1)
        base_schema_block = self.body[schema_dict_start:conditional_start]
        self.assertNotIn("CONF_ENABLE_PARAMETER_CONFIGURATION", base_schema_block)
        # ...but it IS present after that guard, inside the conditional block.
        conditional_block = self.body[conditional_start:conditional_start + 300]
        self.assertIn("CONF_ENABLE_PARAMETER_CONFIGURATION", conditional_block)

    def test_reconfigure_path_never_key_errors_on_missing_form_field(self):
        """Adversarial: since the field is absent from user_input during
        reconfigure, any unconditional user_input[CONF_ENABLE_PARAMETER_
        CONFIGURATION] access left in the body (e.g. a leftover from a
        partial edit) would KeyError at runtime. Confirms exactly one
        such access exists, and it's the one already proven guarded
        above -- not a second, unguarded one elsewhere in the function.
        """
        occurrences = self.body.count(
            "user_input[CONF_ENABLE_PARAMETER_CONFIGURATION]"
        )
        self.assertEqual(occurrences, 1)


class TestOptionsFlowHostsTheCheckbox(unittest.TestCase):
    """BatteryHealthOptionsFlowHandler.async_step_init must expose
    exactly one CONF_ENABLE_PARAMETER_CONFIGURATION field, positioned
    alongside CONF_BH_ENABLED and CONF_SYNC_POWER_DEDICATED_READS."""

    def setUp(self):
        self.body = _function_body("async_step_init")

    def test_appears_exactly_once(self):
        # 3 occurrences expected: the vol.Optional(...) key, the
        # options.get(...) default lookup, and the data.get(...) fallback
        # inside that same default expression.
        count = self.body.count("CONF_ENABLE_PARAMETER_CONFIGURATION")
        self.assertEqual(
            count, 3,
            f"expected exactly 3 occurrences (field key + options default + "
            f"data fallback), found {count} -- possible duplicate field "
            f"definition reintroduced",
        )

    def test_positioned_after_sync_power_dedicated_reads_not_at_the_end(self):
        """Pins the deliberate grouping with the other two prominent
        entry-level toggles, confirmed directly with the report author,
        rather than left at the end of the schema after the battery-
        health numeric tuning fields."""
        bh_enabled_idx = self.body.find("CONF_BH_ENABLED")
        sync_power_idx = self.body.find("CONF_SYNC_POWER_DEDICATED_READS")
        elevated_idx = self.body.find("CONF_ENABLE_PARAMETER_CONFIGURATION")
        window_days_idx = self.body.find("CONF_BH_WINDOW_DAYS")  # a numeric tuning field
        self.assertGreater(bh_enabled_idx, -1)
        self.assertGreater(sync_power_idx, -1)
        self.assertGreater(elevated_idx, -1)
        self.assertGreater(window_days_idx, -1)
        self.assertLess(bh_enabled_idx, sync_power_idx)
        self.assertLess(sync_power_idx, elevated_idx)
        self.assertLess(
            elevated_idx, window_days_idx,
            "CONF_ENABLE_PARAMETER_CONFIGURATION should be grouped with the "
            "other prominent toggles, before the numeric tuning fields, "
            "not left at the end of the schema",
        )

    def test_default_falls_back_to_entry_data_for_backward_compatibility(self):
        """Every installation that existed before this release has its
        value in entry.data (set during initial setup), not entry.
        options -- the options-flow default must fall back to it, or
        every existing user would see this silently reset to False the
        first time they open Configure."""
        idx = self.body.find("CONF_ENABLE_PARAMETER_CONFIGURATION")
        window = self.body[idx: idx + 400]
        self.assertIn("self.config_entry.data.get(", window)

    def test_write_permission_validation_tradeoff_is_documented(self):
        """The options flow does not reconnect to the device, so it
        cannot re-validate write permission the way initial setup does
        -- this must remain an explicit, documented trade-off in the
        source, not something a future reader has to rediscover."""
        idx = self.body.find("CONF_ENABLE_PARAMETER_CONFIGURATION")
        preceding_comment = self.body[max(0, idx - 1800): idx]
        self.assertIn("has_write_permission", preceding_comment)


if __name__ == "__main__":
    unittest.main()
