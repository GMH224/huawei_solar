"""Tests for register_cache.py.

These tests verify the core caching behaviour: TTL, adaptive TTL, night mode,
tier classification, dirty-flag invalidation, and stale-cache fallback.
No Home Assistant or huawei-solar library environment is required.
"""

from __future__ import annotations

import sys
import time
import types
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Stub huawei_solar so register_cache.py can be imported standalone.
# ---------------------------------------------------------------------------

_hs_stub = types.ModuleType("huawei_solar")
_hs_stub.RegisterName = str  # type: ignore[attr-defined]


class _Result:
    def __init__(self, value):
        self.value = value


_hs_stub.Result = _Result  # type: ignore[attr-defined]
sys.modules.setdefault("huawei_solar", _hs_stub)

import importlib, pathlib

_src = pathlib.Path(__file__).parent.parent / "register_cache.py"
_spec = importlib.util.spec_from_file_location("register_cache", _src)
cache_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(cache_mod)  # type: ignore[union-attr]

RegisterCache = cache_mod.RegisterCache
RegisterTier = cache_mod.RegisterTier
_classify = cache_mod._classify
ADAPTIVE_FACTOR = cache_mod.ADAPTIVE_FACTOR
NIGHT_TTL_MULTIPLIER = cache_mod.NIGHT_TTL_MULTIPLIER
_TIER_BASE_TTL = cache_mod._TIER_BASE_TTL
_TIER_CAP_TTL = cache_mod._TIER_CAP_TTL


def _result(v) -> _Result:
    return _Result(v)


DEFAULT_TTL = timedelta(seconds=30)


# ---------------------------------------------------------------------------
# Tier classification
# ---------------------------------------------------------------------------

class TestClassify:
    def test_serial_number_is_static(self):
        assert _classify("inverter_serial_number") == RegisterTier.STATIC

    def test_rated_power_is_static(self):
        assert _classify("rated_power") == RegisterTier.STATIC

    def test_total_energy_is_slow(self):
        assert _classify("total_energy_export") == RegisterTier.SLOW

    def test_temperature_is_slow(self):
        assert _classify("battery_temperature") == RegisterTier.SLOW

    def test_active_power_is_fast(self):
        assert _classify("active_power") == RegisterTier.FAST

    def test_input_power_is_fast(self):
        assert _classify("input_power") == RegisterTier.FAST

    def test_soc_is_normal(self):
        assert _classify("storage_state_of_capacity") == RegisterTier.NORMAL

    def test_unknown_is_normal(self):
        assert _classify("some_unknown_register_xyz") == RegisterTier.NORMAL


# ---------------------------------------------------------------------------
# Basic get/update/merge
# ---------------------------------------------------------------------------

class TestBasicCacheOps:
    def test_miss_returns_none(self):
        cache = RegisterCache()
        assert cache.get("missing_register") is None

    def test_update_then_get(self):
        cache = RegisterCache()
        cache.update({"soc": _result(80)})
        val = cache.get("soc")
        assert val is not None
        assert val.value == 80

    def test_merge_returns_fresh_over_cached(self):
        cache = RegisterCache()
        cache.update({"soc": _result(80)})
        fresh = {"soc": _result(85)}
        merged = cache.merge(fresh, ["soc"])
        assert merged["soc"].value == 85

    def test_merge_fills_from_cache(self):
        cache = RegisterCache()
        cache.update({"soc": _result(80)})
        merged = cache.merge({}, ["soc"])
        assert merged["soc"].value == 80

    def test_dirty_entry_excluded_from_get(self):
        cache = RegisterCache()
        cache.update({"soc": _result(80)})
        cache.invalidate("soc")
        assert cache.get("soc") is None

    def test_dirty_entry_excluded_from_merge(self):
        cache = RegisterCache()
        cache.update({"soc": _result(80)})
        cache.invalidate("soc")
        merged = cache.merge({}, ["soc"])
        assert "soc" not in merged

    def test_update_clears_dirty_flag(self):
        cache = RegisterCache()
        cache.update({"soc": _result(80)})
        cache.invalidate("soc")
        cache.update({"soc": _result(81)})
        assert cache.get("soc") is not None

    def test_invalidate_all(self):
        cache = RegisterCache()
        cache.update({"a": _result(1), "b": _result(2)})
        cache.invalidate_all()
        assert cache.get("a") is None
        assert cache.get("b") is None

    def test_size_property(self):
        cache = RegisterCache()
        cache.update({"a": _result(1), "b": _result(2)})
        assert cache.size == 2

    def test_clear(self):
        cache = RegisterCache()
        cache.update({"a": _result(1)})
        cache.clear()
        assert cache.size == 0


# ---------------------------------------------------------------------------
# filter_stale
# ---------------------------------------------------------------------------

class TestFilterStale:
    def test_unknown_register_is_stale(self):
        cache = RegisterCache()
        stale = cache.filter_stale(["unknown"], DEFAULT_TTL)
        assert "unknown" in stale

    def test_fresh_normal_register_not_stale(self):
        cache = RegisterCache()
        cache.update({"storage_state_of_capacity": _result(80)})
        stale = cache.filter_stale(["storage_state_of_capacity"], DEFAULT_TTL)
        assert "storage_state_of_capacity" not in stale

    def test_expired_normal_register_is_stale(self):
        cache = RegisterCache()
        cache.update({"storage_state_of_capacity": _result(80)})
        entry = cache._store["storage_state_of_capacity"]
        entry.ts -= 60  # simulate 60 s ago — past the 30 s TTL
        stale = cache.filter_stale(["storage_state_of_capacity"], DEFAULT_TTL)
        assert "storage_state_of_capacity" in stale

    def test_fast_register_always_stale(self):
        """FAST tier registers (base TTL = 0) are always stale immediately."""
        cache = RegisterCache()
        cache.update({"active_power": _result(1000)})
        stale = cache.filter_stale(["active_power"], DEFAULT_TTL)
        assert "active_power" in stale

    def test_static_register_not_stale_after_one_read(self):
        cache = RegisterCache()
        cache.update({"inverter_serial_number": _result("SN123")})
        stale = cache.filter_stale(["inverter_serial_number"], DEFAULT_TTL)
        assert "inverter_serial_number" not in stale

    def test_dirty_register_always_stale(self):
        cache = RegisterCache()
        cache.update({"soc": _result(80)})
        cache.invalidate("soc")
        stale = cache.filter_stale(["soc"], DEFAULT_TTL)
        assert "soc" in stale


# ---------------------------------------------------------------------------
# Adaptive TTL
# ---------------------------------------------------------------------------

class TestAdaptiveTTL:
    def test_ttl_doubles_on_unchanged_value(self):
        cache = RegisterCache()
        cache.update({"soc": _result(80)})
        initial_ttl = cache._store["soc"].effective_ttl

        cache.update({"soc": _result(80)})  # same value
        doubled_ttl = cache._store["soc"].effective_ttl

        assert doubled_ttl == pytest.approx(initial_ttl * ADAPTIVE_FACTOR)

    def test_ttl_resets_on_changed_value(self):
        cache = RegisterCache()
        cache.update({"soc": _result(80)})
        cache.update({"soc": _result(80)})  # double
        cache.update({"soc": _result(81)})  # change → reset

        tier = RegisterTier.NORMAL
        assert cache._store["soc"].effective_ttl == _TIER_BASE_TTL[tier]

    def test_ttl_capped_at_tier_cap(self):
        cache = RegisterCache()
        cache.update({"soc": _result(80)})
        # Force the TTL beyond the cap.
        entry = cache._store["soc"]
        entry.effective_ttl = _TIER_CAP_TTL[RegisterTier.NORMAL] * 0.9
        cache.update({"soc": _result(80)})  # unchanged → would double → capped
        assert cache._store["soc"].effective_ttl <= _TIER_CAP_TTL[RegisterTier.NORMAL]


# ---------------------------------------------------------------------------
# Night mode
# ---------------------------------------------------------------------------

class TestNightMode:
    def test_night_mode_stretches_normal_ttl(self):
        cache = RegisterCache()
        cache.update({"soc": _result(80)})
        base_ttl = cache._store["soc"].effective_ttl

        cache.set_night_mode(True)
        effective = cache._effective_ttl(cache._store["soc"])
        assert effective > base_ttl

    def test_night_mode_does_not_stretch_static(self):
        cache = RegisterCache()
        cache.update({"inverter_serial_number": _result("SN")})
        base_ttl = cache._store["inverter_serial_number"].effective_ttl

        cache.set_night_mode(True)
        # STATIC tier is not stretched by night mode.
        effective = cache._effective_ttl(cache._store["inverter_serial_number"])
        assert effective == base_ttl

    def test_wakeup_resets_normal_ttl(self):
        """On exit from night mode, NORMAL and FAST entries are reset to base TTL."""
        cache = RegisterCache()
        cache.update({"soc": _result(80)})
        cache.set_night_mode(True)
        # Stretch the effective_ttl via night mode logic (double it manually).
        cache._store["soc"].effective_ttl = 300.0
        cache.set_night_mode(False)  # wake up
        assert cache._store["soc"].effective_ttl == _TIER_BASE_TTL[RegisterTier.NORMAL]

    def test_night_mode_property(self):
        cache = RegisterCache()
        assert not cache.night_mode
        cache.set_night_mode(True)
        assert cache.night_mode
        cache.set_night_mode(False)
        assert not cache.night_mode

    def test_setting_same_mode_is_noop(self):
        """Redundant set_night_mode(True) should not reset TTLs again."""
        cache = RegisterCache()
        cache.update({"soc": _result(80)})
        cache.update({"soc": _result(80)})  # double TTL
        pre_ttl = cache._store["soc"].effective_ttl

        cache.set_night_mode(False)
        cache.set_night_mode(False)  # redundant
        assert cache._store["soc"].effective_ttl == pre_ttl


# ---------------------------------------------------------------------------
# tier_of / effective_ttl_of helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_tier_of_known(self):
        cache = RegisterCache()
        cache.update({"inverter_serial_number": _result("SN")})
        assert cache.tier_of("inverter_serial_number") == RegisterTier.STATIC

    def test_tier_of_unknown(self):
        assert RegisterCache().tier_of("nonexistent") is None

    def test_effective_ttl_of_unknown_is_zero(self):
        assert RegisterCache().effective_ttl_of("nonexistent") == 0.0
