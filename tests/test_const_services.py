"""Tests for const.py bug fix.

Bug 6 — SERVICE_SET_MAXIMUM_FEED_GRID_POWER_PERCENT was missing from the
SERVICES tuple.  Services not in this tuple may not be cleaned up during
integration unload, leaving orphaned HA service registrations.

Test strategy
-------------
We import the const module directly (it has no HA dependencies) and verify
that SERVICES is a superset of all SERVICE_* constants defined in the module,
and that every name in ALL_SERVICES (services.py) also appears in SERVICES.
"""

from __future__ import annotations

import importlib, pathlib, sys, types

# const.py has no Home Assistant or third-party dependencies — import directly.
_src = pathlib.Path(__file__).parent.parent / "const.py"
_spec = importlib.util.spec_from_file_location("huawei_solar_const", _src)
const = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(const)  # type: ignore[union-attr]


class TestServicesTupleCompleteness:
    """SERVICES must contain every SERVICE_* constant defined in const.py."""

    def _all_service_constants(self) -> dict[str, str]:
        """Return {constant_name: value} for every SERVICE_* name in const."""
        return {
            name: getattr(const, name)
            for name in dir(const)
            if name.startswith("SERVICE_") and isinstance(getattr(const, name), str)
        }

    def test_services_tuple_contains_all_service_constants(self):
        """Every SERVICE_* constant defined in const.py must appear in SERVICES."""
        all_constants = self._all_service_constants()
        services_set = set(const.SERVICES)

        missing = {
            name: value
            for name, value in all_constants.items()
            if value not in services_set
        }

        assert not missing, (
            f"The following SERVICE_* constants are missing from the SERVICES tuple:\n"
            + "\n".join(f"  {name} = {value!r}" for name, value in sorted(missing.items()))
            + "\n\nServices not in SERVICES may not be unregistered on integration unload."
        )

    def test_set_maximum_feed_grid_power_percent_present(self):
        """Specific regression test: SERVICE_SET_MAXIMUM_FEED_GRID_POWER_PERCENT in SERVICES."""
        assert const.SERVICE_SET_MAXIMUM_FEED_GRID_POWER_PERCENT in const.SERVICES, (
            "SERVICE_SET_MAXIMUM_FEED_GRID_POWER_PERCENT is not in SERVICES. "
            "This was the original bug: the service would not be cleaned up on unload."
        )

    def test_services_tuple_has_no_duplicates(self):
        """SERVICES must not contain duplicate entries."""
        from collections import Counter

        counts = Counter(const.SERVICES)
        duplicates = {svc: cnt for svc, cnt in counts.items() if cnt > 1}
        assert not duplicates, f"Duplicate entries in SERVICES: {duplicates}"

    def test_services_values_are_strings(self):
        """All entries in SERVICES must be non-empty strings."""
        for svc in const.SERVICES:
            assert isinstance(svc, str) and svc, (
                f"Non-string or empty entry in SERVICES: {svc!r}"
            )

    def test_all_services_in_services_py_covered(self):
        """ALL_SERVICES list in services.py must be a subset of SERVICES in const.py.

        services.py defines its own ALL_SERVICES list used for registration.
        Both must agree so nothing slips through.
        """
        import ast

        services_src = pathlib.Path(__file__).parent.parent / "services.py"
        tree = ast.parse(services_src.read_text())

        # Find the ALL_SERVICES list assignment.
        all_services_node = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == "ALL_SERVICES"
                    for t in node.targets
                )
            ):
                all_services_node = node
                break

        if all_services_node is None:
            return  # ALL_SERVICES not defined — nothing to check

        # Extract the referenced names (e.g. SERVICE_FORCIBLE_CHARGE).
        referenced_names: list[str] = []
        if isinstance(all_services_node.value, ast.List):
            for elt in all_services_node.value.elts:
                if isinstance(elt, ast.Name):
                    referenced_names.append(elt.id)

        const_services_set = set(const.SERVICES)
        for name in referenced_names:
            value = getattr(const, name, None)
            assert value is not None, f"{name} is in ALL_SERVICES but not defined in const.py"
            assert value in const_services_set, (
                f"{name} ({value!r}) is in ALL_SERVICES (services.py) but missing from "
                "SERVICES (const.py). It will not be unregistered on integration unload."
            )
