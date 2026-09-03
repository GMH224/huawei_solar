"""Regression tests for v2.2.0.0 HA 2026.9 / 2026.12 forward-compatibility.

Covers four fixes, all triggered by real HA core deprecations with
verified sources (developer blog posts, fetched and quoted in the
commit-level comments at each call site):

  1. `via_device` -> `via_device_id` (__init__.py) -- the original
     production incident: sensors going unavailable in the field.
  2. Options-flow config-entry listener + reloading methods, deprecated
     in favour of OptionsFlowWithReload (config_flow.py, __init__.py) --
     will ERROR from HA 2026.12.
  3. `DeviceEntry.config_entries` manual iteration, replaced by
     `async_get_device_and_config_entry_for_domain()` (services.py).
  4. `serial.tools.list_ports` -> `serialx.async_list_serial_ports()`
     (config_flow.py).

Static (AST/source) checks, following this project's own established
convention for __init__.py and config_flow.py setup logic (see
test_setup_critical_path.py's own header) -- these files are deeply
intertwined with real HA infrastructure that is expensive to mock fully,
and this project's existing tests for this exact file already take this
approach rather than full behavioural execution.

One fix (#1, the via_device_id resolution path) could not be exercised
against a real, current homeassistant.helpers.device_registry in this
environment: the newest package available via PyPI at review time was
2025.1.4, which predates async_get_device_id_by_identifier() (introduced
around HA 2026.8). That helper's signature and behaviour were verified
instead directly against the official HA developer blog post ("More
device registry deprecations, new helpers and validation",
2026-08-24) -- quoted in __init__.py's own comment at the call site.
Fix #4 (serialx) WAS verified by real execution: serialx 1.9.0 installs
cleanly from PyPI and async_list_serial_ports() was actually called
against this sandbox's own serial device.
"""
from __future__ import annotations

import ast
import pathlib
import unittest

_ROOT = pathlib.Path(__file__).parent.parent
_INIT_SRC = (_ROOT / "__init__.py").read_text()
_CONFIG_FLOW_SRC = (_ROOT / "config_flow.py").read_text()
_SERVICES_SRC = (_ROOT / "services.py").read_text()


def _function_body(source: str, signature_prefix: str) -> str:
    """Return one function's body, bounded by the next top-level def,
    with the function's own docstring and every `#`-comment-only line
    stripped out.

    This project's convention is to explain a fix's *before* state in
    a docstring or a `#`-comment block, which naturally means mentioning
    the old, deprecated API by name -- a plain substring search over the
    whole body would match that explanatory mention rather than the
    real code. Comment lines are stripped throughout the body, not just
    a leading run of them: this project's comment blocks are not always
    the very first thing after the signature (some functions have a
    line or two of real code first, then a comment block explaining a
    later section), so "skip only a leading run" is not sufficient.
    """
    idx = source.find(signature_prefix)
    assert idx != -1, f"{signature_prefix!r} not found"
    nxt = source.find("\nasync def ", idx + 1)
    nxt2 = source.find("\ndef ", idx + 1)
    end = min(x for x in (nxt, nxt2, len(source)) if x > 0)
    body = source[idx:end]

    first = body.find('"""')
    if first != -1:
        second = body.find('"""', first + 3)
        if second != -1:
            body = body[second + 3:]

    return "\n".join(
        line for line in body.split("\n")
        if not line.strip().startswith("#")
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Fix 1 — via_device -> via_device_id
# ═══════════════════════════════════════════════════════════════════════════════

class TestViaDeviceIdMigration(unittest.TestCase):
    """The original production incident: HA 2026.9 escalated this from a
    warning to a hard RuntimeError for the specific call path where HA's
    own entity_platform.py resolves an entity's device_info with no
    attributable integration frame -- confirmed directly from a field
    log: "Error adding entity None for domain sensor with platform
    huawei_solar". Sensors (Active power, Daily yield, Efficiency, ...)
    went unavailable because entity setup was raising, not because of
    any 2.1.0.1 defect.
    """

    def test_no_via_device_keyword_remains_in_production_code(self):
        # The deprecated keyword itself, not the (harmless) identifier
        # substring "via_device_id" which legitimately contains it.
        self.assertNotIn("via_device=", _INIT_SRC)

    def test_six_via_device_id_sites_present(self):
        """All six original sites (main inverter, power meter, battery,
        battery 1, battery 2, optimizers) must be migrated, not just
        the one from the original field report.

        The main inverter's own site uses conditional dict-unpacking
        (`**({"via_device_id": ...} if ... else {})`, dict-key syntax)
        rather than a plain keyword argument, specifically to omit the
        key entirely when there is no via-device relationship -- so it
        is counted separately from the other five, which all reuse the
        already-captured inverter_device_entry directly as a keyword
        argument.
        """
        self.assertEqual(_INIT_SRC.count("via_device_id="), 5)
        self.assertEqual(_INIT_SRC.count('"via_device_id"'), 1)

    def test_five_sub_device_sites_reuse_the_parent_registry_entry(self):
        """Official HA guidance (developer blog, 2026-08-24): "When your
        integration creates the via device itself, skip the lookup and
        read .id from the DeviceEntry that async_get_or_create returned
        for it." Power meter, battery, battery 1, battery 2 and
        optimizers all share the main inverter as their via-device and
        must reuse its already-captured DeviceEntry, not perform a
        separate lookup each."""
        self.assertEqual(
            _INIT_SRC.count("via_device_id=inverter_device_entry.id"), 5,
        )

    def test_inverter_device_entry_is_captured_from_async_get_or_create(self):
        idx = _INIT_SRC.find("inverter_device_entry = device_registry.async_get_or_create(")
        self.assertNotEqual(
            idx, -1,
            "the main inverter's own async_get_or_create() call must "
            "capture its return value -- it was previously discarded, "
            "since nothing needed it before this fix",
        )

    def test_does_not_use_the_also_deprecated_async_get_device(self):
        """Regression guard for the author's own mistake during
        development: an earlier draft resolved connecting_inverter_
        device_id via device_registry.async_get_device(), which is
        ITSELF one of the deprecated APIs in this same HA change
        (escalated in the same core PR as via_device, per the developer
        blog). Fixing one deprecated call with another would not
        actually close the deprecation."""
        body = _function_body(_INIT_SRC, "async def _setup_inverter_device_data(")
        self.assertNotIn(
            "device_registry.async_get_device(", body,
            "async_get_device() is deprecated in this same HA change; "
            "use async_get_device_id_by_identifier() instead",
        )

    def test_uses_the_official_lookup_helper_for_the_master_relationship(self):
        body = _function_body(_INIT_SRC, "async def _setup_inverter_device_data(")
        self.assertIn("dr.async_get_device_id_by_identifier(", body)

    def test_official_helper_call_passes_config_entry_id(self):
        """The official signature is `async_get_device_id_by_identifier(
        hass, identifier, *, config_entry_id)` -- identifiers are only
        unique within a config entry, so omitting this makes the lookup
        ambiguous by construction."""
        body = _function_body(_INIT_SRC, "async def _setup_inverter_device_data(")
        idx = body.find("async_get_device_id_by_identifier(")
        window = body[idx: idx + 300]
        self.assertIn("config_entry_id=", window)

    def test_official_helper_call_is_guarded_against_valueerror(self):
        """Documented behaviour: "It raises ValueError if no matching
        device exists". connecting_inverter_device_id is always None at
        the single call site today, so this path is genuinely untested
        in production -- defensive handling is not optional here."""
        body = _function_body(_INIT_SRC, "async def _setup_inverter_device_data(")
        idx = body.find("async_get_device_id_by_identifier(")
        self.assertNotEqual(idx, -1)
        window = body[:idx]
        # nearest enclosing try before the call
        try_idx = window.rfind("try:")
        self.assertNotEqual(try_idx, -1, "the lookup must be wrapped in try/except")
        between = body[try_idx:idx + 300]
        self.assertIn("except ValueError:", between)

    def test_connecting_inverter_device_id_parameter_kept_not_dropped(self):
        """The parameter is always None at the one call site today, but
        its own existence signals intended future support for linking a
        secondary inverter to a master -- dropping it silently would
        remove that capability rather than just modernising its
        implementation."""
        self.assertIn(
            "connecting_inverter_device_id: tuple[str, str] | None", _INIT_SRC,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Fix 2 — options-flow listener/reload (HA 2026.12 hard error)
# ═══════════════════════════════════════════════════════════════════════════════

class TestOptionsFlowReloadMigration(unittest.TestCase):
    """Verified against the official HA developer blog post
    (2026-05-07): "using a config entry listener together with any
    reloading methods in a config flow is deprecated and will result in
    an error from 2026.12."

    The full scope in this codebase is broader than the options flow
    alone: the same listener was also combined with reloading methods on
    the reauth path, the reconfigure path, and the new-entry path's
    _abort_if_unique_id_configured() (which defaults to
    reload_on_update=True) -- all three inside
    ConfigFlow._create_or_update_entry(). Removing the listener entirely,
    not just its most visible use, is what closes all of them at once:
    with no listener registered for the entry, none of those three
    reloading-method calls are "combined with a listener" any more
    either.
    """

    def test_options_flow_subclasses_reload_variant(self):
        self.assertIn(
            "class BatteryHealthOptionsFlowHandler(OptionsFlowWithReload):",
            _CONFIG_FLOW_SRC,
        )
        self.assertNotIn(
            "class BatteryHealthOptionsFlowHandler(config_entries.OptionsFlow):",
            _CONFIG_FLOW_SRC,
        )

    def test_options_flow_with_reload_is_imported(self):
        self.assertIn(
            "from homeassistant.config_entries import ConfigFlowResult, "
            "OptionsFlowWithReload",
            _CONFIG_FLOW_SRC,
        )

    def test_update_listener_registration_removed_from_init(self):
        self.assertNotIn("entry.add_update_listener(", _INIT_SRC)

    def test_options_updated_handler_removed_from_init(self):
        """The handler function itself, not just its registration --
        dead code left behind is exactly the kind of thing that gets
        silently re-wired back in during a future refactor."""
        self.assertNotIn("async def _async_options_updated(", _INIT_SRC)

    def test_options_flow_still_creates_the_entry_the_same_way(self):
        """Behavioural continuity: the options-commit mechanism itself
        (async_create_entry) is unchanged -- only the base class and the
        (now implicit) reload trigger changed."""
        body = _function_body(_CONFIG_FLOW_SRC, "async def async_step_init(")
        self.assertIn('self.async_create_entry(title="", data=user_input)', body)

    def test_reauth_reload_untouched_but_no_longer_paired_with_a_listener(self):
        """Adversarial: confirm the reauth path's own async_reload() call
        was NOT modified (it doesn't need to be -- the fix is removing
        the listener, not touching every reload call site), and that no
        listener remains anywhere in the production code for it to be
        paired with."""
        self.assertIn(
            "await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)",
            _CONFIG_FLOW_SRC,
        )
        self.assertNotIn("add_update_listener", _CONFIG_FLOW_SRC)

    def test_no_add_update_listener_anywhere_in_production_code(self):
        """Sweep of the whole production tree, not just __init__.py --
        the deprecation is about the config entry as a whole, not one
        file."""
        for path in _ROOT.glob("*.py"):
            self.assertNotIn(
                "add_update_listener", path.read_text(),
                f"unexpected add_update_listener() in {path.name}",
            )


# ═══════════════════════════════════════════════════════════════════════════════
# Fix 3 — DeviceEntry.config_entries -> async_get_device_and_config_entry_for_domain
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeviceEntryConfigEntriesMigration(unittest.TestCase):
    """DeviceEntry.config_entries is deprecated (removal scheduled for HA
    2027.8) since a device now belongs to a single config entry as of
    HA 2026.8. HA's official replacement,
    async_get_device_and_config_entry_for_domain(), is used instead.
    """

    def test_manual_config_entries_iteration_removed(self):
        # Checks for the actual EXECUTABLE statement shape (a for-loop
        # over the attribute), not the bare attribute-access substring:
        # this project's own docstrings explain a fix's *before* state
        # by naming the old API in prose, which a plain substring search
        # would match before ever reaching real code.
        self.assertNotIn("for entry_id in device_entry.config_entries", _SERVICES_SRC)

    def test_uses_the_official_domain_lookup_helper(self):
        body = _function_body(
            _SERVICES_SRC, "def async_get_entry_id_for_service_call("
        )
        self.assertIn(
            "dr.async_get_device_and_config_entry_for_domain(", body,
        )

    def test_domain_keyword_is_this_integrations_domain(self):
        body = _function_body(
            _SERVICES_SRC, "def async_get_entry_id_for_service_call("
        )
        idx = body.find("async_get_device_and_config_entry_for_domain(")
        window = body[idx: idx + 200]
        self.assertIn("domain=DOMAIN", window)

    def test_invalid_device_id_is_still_distinguished_from_wrong_domain(self):
        """The new helper collapses "no such device" and "device exists
        but isn't ours" into the same (None, None) -- both distinct
        error messages (invalid_device_id vs config_entry_not_found)
        must still be reachable, or a user gets a less specific error
        than before."""
        body = _function_body(
            _SERVICES_SRC, "def async_get_entry_id_for_service_call("
        )
        self.assertIn('"invalid_device_id"', body)
        self.assertIn('"config_entry_not_found"', body)
        # invalid_device_id must be checked FIRST, via a direct registry
        # lookup, before the domain-specific helper is even called --
        # otherwise a bogus device_id would surface as the less precise
        # config_entry_not_found instead.
        invalid_idx = body.find('"invalid_device_id"')
        helper_idx = body.find("async_get_device_and_config_entry_for_domain(")
        self.assertLess(invalid_idx, helper_idx)

    def test_entry_loaded_check_is_preserved(self):
        """Documented behaviour: the new helper does NOT check whether
        the config entry is loaded. The existing entry_not_loaded check
        must be kept explicitly, or an unloaded entry's device could be
        used in a service call."""
        body = _function_body(
            _SERVICES_SRC, "def async_get_entry_id_for_service_call("
        )
        self.assertIn("ConfigEntryState.LOADED", body)
        self.assertIn('"entry_not_loaded"', body)


# ═══════════════════════════════════════════════════════════════════════════════
# Fix 4 — serial.tools.list_ports -> serialx
# ═══════════════════════════════════════════════════════════════════════════════

class TestSerialxMigration(unittest.TestCase):
    """HA developer blog (2026-04-27): "Existing integrations and
    libraries communicating with serial ports should migrate from
    pyserial, pyserial-asyncio, and pyserial-asyncio-fast to serialx."

    Verified by real execution (not just source inspection) that
    serialx installs from PyPI and async_list_serial_ports() actually
    runs -- see this module's own header.
    """

    def test_pyserial_import_removed(self):
        self.assertNotIn("import serial.tools.list_ports", _CONFIG_FLOW_SRC)
        # Checks the actual assignment statement, not the bare API-name
        # substring: this file's own explanatory comment on the fix
        # names the old API in prose ("was serial.tools.list_ports.
        # comports wrapped in ..."), which a plain substring search
        # would match before ever reaching the real call below it.
        self.assertNotIn(
            "ports = await self.hass.async_add_executor_job(serial",
            _CONFIG_FLOW_SRC,
        )

    def test_serialx_is_imported(self):
        self.assertIn("import serialx", _CONFIG_FLOW_SRC)

    def test_uses_the_native_async_helper_not_an_executor_job(self):
        """A genuine improvement, not just a rename: serialx is natively
        async, so the async_add_executor_job wrapping this integration
        needed for the old sync-only pyserial call is no longer
        necessary and must be removed, not merely have its target
        function swapped."""
        self.assertIn("await serialx.async_list_serial_ports()", _CONFIG_FLOW_SRC)
        self.assertNotIn(
            "async_add_executor_job(serial", _CONFIG_FLOW_SRC,
        )

    def test_field_mapping_uses_product_not_a_nonexistent_description_field(self):
        """SerialPortInfo has no `description` field (verified against
        the real installed package) -- `product` is the closest semantic
        equivalent (the USB product string). Using the wrong attribute
        name here would raise AttributeError at runtime on every serial
        setup attempt."""
        idx = _CONFIG_FLOW_SRC.find("usb.human_readable_device_name(")
        window = _CONFIG_FLOW_SRC[idx: idx + 400]
        self.assertIn("port.product", window)
        self.assertNotIn("port.description", window)

    def test_vid_pid_are_converted_to_str(self):
        """SerialPortInfo.vid/.pid are int | None (verified against the
        real installed package); human_readable_device_name() expects
        str | None. Converted explicitly rather than relying on
        formatting inside that function to paper over the mismatch."""
        idx = _CONFIG_FLOW_SRC.find("usb.human_readable_device_name(")
        window = _CONFIG_FLOW_SRC[idx: idx + 400]
        self.assertIn("str(port.vid) if port.vid is not None else None", window)
        self.assertIn("str(port.pid) if port.pid is not None else None", window)

    def test_serialx_declared_in_manifest_requirements(self):
        import json
        manifest = json.loads((_ROOT / "manifest.json").read_text())
        reqs = manifest["requirements"]
        self.assertTrue(
            any(r.startswith("serialx") for r in reqs),
            "serialx must be an explicit requirement -- unlike "
            "serial.tools.list_ports, which was only implicitly "
            "available via HA core's own bundled pyserial, this "
            "integration now depends on serialx directly",
        )

    def test_manifest_requirements_is_valid_json(self):
        """Adversarial: a hand-edit to manifest.json is exactly the kind
        of change that can silently break JSON syntax."""
        import json
        json.loads((_ROOT / "manifest.json").read_text())


if __name__ == "__main__":
    unittest.main()
