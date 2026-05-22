"""Regression tests for register_cache._classify() and related utilities.

v2.12.2: added tests for sort_by_address() and LiveRegisterBus.

Run:
    python -m pytest custom_components/huawei_solar/tests/test_register_classifier.py -v
"""
import sys, types as _stl_types, importlib.util, time

# ── minimal stubs so register_cache imports without HA or huawei-solar ───────
hs = _stl_types.ModuleType("huawei_solar")
class RegisterName(str): pass
class Result:
    def __init__(self, value): self.value = value
hs.RegisterName = RegisterName
hs.Result = Result
sys.modules.setdefault("huawei_solar", hs)

spec = importlib.util.spec_from_file_location(
    "register_cache",
    __file__.replace("test_register_classifier.py", "../register_cache.py"),
)
rc = importlib.util.module_from_spec(spec)
sys.modules["register_cache"] = rc
spec.loader.exec_module(rc)

from register_cache import (  # noqa: E402
    RegisterTier, LiveRegisterBus, RegisterCache, StaticRegisterCache,
    _FAST_SUBSTRINGS, _SLOW_SUBSTRINGS, _STATIC_SUBSTRINGS, _classify,
    sort_by_address,
)

import pytest


# ── shadowing ─────────────────────────────────────────────────────────────────

class TestNoShadowing:
    @pytest.mark.parametrize("slow_sub", _SLOW_SUBSTRINGS)
    def test_slow_not_shadowed_by_fast(self, slow_sub):
        name = f"_test__{slow_sub}__reg"
        for fast_sub in _FAST_SUBSTRINGS:
            assert fast_sub not in name, (
                f"SLOW '{slow_sub}' shadowed by FAST '{fast_sub}'"
            )

    @pytest.mark.parametrize("static_sub", _STATIC_SUBSTRINGS)
    def test_static_not_shadowed_by_fast(self, static_sub):
        name = f"_test__{static_sub}__reg"
        for fast_sub in _FAST_SUBSTRINGS:
            assert fast_sub not in name, (
                f"STATIC '{static_sub}' shadowed by FAST '{fast_sub}'"
            )


# ── explicit tier expectations ────────────────────────────────────────────────

_EXPECTED = [
    ("inverter_active_power",           RegisterTier.FAST),
    ("inverter_reactive_power",         RegisterTier.FAST),
    ("power_meter_active_power",        RegisterTier.FAST),
    ("power_meter_reactive_power",      RegisterTier.FAST),
    ("storage_charge_discharge_power",  RegisterTier.FAST),
    ("input_power",                     RegisterTier.FAST),
    ("total_dc_input_power",            RegisterTier.FAST),
    ("phase_a_active_power_built_in",   RegisterTier.SLOW),
    ("phase_b_active_power_built_in",   RegisterTier.SLOW),
    ("phase_c_active_power_built_in",   RegisterTier.SLOW),
    ("active_power_built_in",           RegisterTier.SLOW),
    ("phase_a_active_power_external",   RegisterTier.SLOW),
    ("active_power_external",           RegisterTier.SLOW),
    ("daily_energy_yield",              RegisterTier.SLOW),
    ("total_energy_yield",              RegisterTier.SLOW),
    ("device_status",                   RegisterTier.SLOW),
    ("running_status",                  RegisterTier.SLOW),
    ("alarm_1",                         RegisterTier.SLOW),
    ("temperature",                     RegisterTier.SLOW),
    ("serial_number",                   RegisterTier.STATIC),
    ("firmware_version",                RegisterTier.STATIC),
    ("rated_power",                     RegisterTier.STATIC),
    ("storage_rated_capacity",          RegisterTier.STATIC),
    ("storage_maximum_charge_power",    RegisterTier.STATIC),
    ("storage_maximum_discharge_power", RegisterTier.STATIC),
    ("battery_soc",                     RegisterTier.NORMAL),
    ("grid_voltage",                    RegisterTier.NORMAL),
    ("pv_1_voltage",                    RegisterTier.NORMAL),
]

class TestExplicitTiers:
    @pytest.mark.parametrize("name,expected", _EXPECTED)
    def test_tier(self, name, expected):
        assert _classify(name) == expected, (
            f"'{name}': expected {expected.name}, got {_classify(name).name}"
        )


# ── substring list sanity ─────────────────────────────────────────────────────

class TestSubstringLists:
    def test_no_empty_fast(self):
        for s in _FAST_SUBSTRINGS:
            assert s.strip(), f"Empty entry in _FAST_SUBSTRINGS: {s!r}"

    def test_no_empty_slow(self):
        for s in _SLOW_SUBSTRINGS:
            assert s.strip(), f"Empty entry in _SLOW_SUBSTRINGS: {s!r}"

    def test_no_empty_static(self):
        for s in _STATIC_SUBSTRINGS:
            assert s.strip(), f"Empty entry in _STATIC_SUBSTRINGS: {s!r}"

    def test_no_duplicate_fast(self):
        assert len(_FAST_SUBSTRINGS) == len(set(_FAST_SUBSTRINGS))

    def test_no_duplicate_slow(self):
        assert len(_SLOW_SUBSTRINGS) == len(set(_SLOW_SUBSTRINGS))


# ── sort_by_address ───────────────────────────────────────────────────────────

class TestSortByAddress:
    def test_returns_same_names(self):
        names = [RegisterName("b"), RegisterName("a"), RegisterName("c")]
        result = sort_by_address(names)
        assert set(result) == set(names)

    def test_sorts_by_register_attr(self):
        a = RegisterName("a"); a.register = 300  # type: ignore[attr-defined]
        b = RegisterName("b"); b.register = 100  # type: ignore[attr-defined]
        c = RegisterName("c"); c.register = 200  # type: ignore[attr-defined]
        assert sort_by_address([a, b, c]) == [b, c, a]

    def test_no_address_attr_is_stable(self):
        names = [RegisterName("z"), RegisterName("a"), RegisterName("m")]
        result = sort_by_address(names)
        assert len(result) == 3


# ── LiveRegisterBus ───────────────────────────────────────────────────────────

class TestLiveRegisterBus:
    def setup_method(self):
        LiveRegisterBus.clear_registry()

    def _bus(self, serial="TEST001"):
        return LiveRegisterBus.get_or_create(serial)

    def test_singleton(self):
        assert self._bus() is self._bus()

    def test_publish_and_query(self):
        bus = self._bus()
        r1 = Result(42)
        bus.publish({RegisterName("power"): r1}, ttl=60)
        found = bus.query([RegisterName("power")])
        assert RegisterName("power") in found
        assert found[RegisterName("power")] is r1

    def test_expiry(self):
        bus = self._bus()
        bus.publish({RegisterName("power"): Result(42)}, ttl=0.001)
        time.sleep(0.05)
        found = bus.query([RegisterName("power")])
        assert RegisterName("power") not in found

    def test_unknown_name_not_returned(self):
        bus = self._bus()
        bus.publish({RegisterName("power"): Result(42)}, ttl=60)
        found = bus.query([RegisterName("unknown")])
        assert not found

    def test_evict_expired_clears_store(self):
        bus = self._bus()
        bus.publish({RegisterName("x"): Result(1)}, ttl=0.001)
        time.sleep(0.05)
        bus.evict_expired()
        assert bus.size == 0

    def test_separate_serials_isolated(self):
        bus_a = LiveRegisterBus.get_or_create("AAA")
        bus_b = LiveRegisterBus.get_or_create("BBB")
        bus_a.publish({RegisterName("p"): Result(1)}, ttl=60)
        assert not bus_b.query([RegisterName("p")])
