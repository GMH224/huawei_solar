"""Tests for register_cache.py — stdlib unittest, no pytest required.

Covers:
  • BUG-3: SLOW-priority patterns checked before FAST to prevent misclassification
  • Tier classification for STATIC / FAST / SLOW / NORMAL
  • Energy counter detection (is_energy_counter)
  • Basic get/update/merge/invalidate operations
  • filter_stale with tier-aware TTL
  • Adaptive TTL doubling and reset on value change
  • Night-mode TTL stretching and wakeup reset
  • invalidate_all() skips STATIC tier (reconnect optimisation)
  • set_telemetry() preserves _store (v1.0.3 fix)
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import time
import types
import unittest
from datetime import timedelta
from unittest.mock import MagicMock

# ── Stub huawei_solar ─────────────────────────────────────────────────────────
_hs = types.ModuleType("huawei_solar")
_hs.RegisterName = str  # type: ignore[attr-defined]
class _Result:
    def __init__(self, v): self.value = v
_hs.Result = _Result  # type: ignore[attr-defined]
sys.modules.setdefault("huawei_solar", _hs)

_SRC = pathlib.Path(__file__).parent.parent / "register_cache.py"
_SPEC = importlib.util.spec_from_file_location("register_cache_test", str(_SRC))
_MOD = importlib.util.module_from_spec(_SPEC)
_MOD.__package__ = "huawei_solar"
_SPEC.loader.exec_module(_MOD)

RegisterCache  = _MOD.RegisterCache
RegisterTier   = _MOD.RegisterTier
_classify      = _MOD._classify
is_energy_counter = _MOD.is_energy_counter
ADAPTIVE_FACTOR   = _MOD.ADAPTIVE_FACTOR
_TIER_BASE_TTL    = _MOD._TIER_BASE_TTL
_TIER_CAP_TTL     = _MOD._TIER_CAP_TTL

DEFAULT_TTL = timedelta(seconds=30)

def _r(v): return _Result(v)


# ── Tier classification ───────────────────────────────────────────────────────

class TestClassify(unittest.TestCase):

    def test_serial_number_static(self):
        self.assertEqual(_classify("inverter_serial_number"), RegisterTier.STATIC)

    def test_rated_power_static(self):
        self.assertEqual(_classify("rated_power"), RegisterTier.STATIC)

    def test_firmware_static(self):
        self.assertEqual(_classify("software_version"), RegisterTier.STATIC)

    def test_total_energy_slow(self):
        self.assertEqual(_classify("total_energy_export"), RegisterTier.SLOW)

    def test_temperature_slow(self):
        self.assertEqual(_classify("battery_temperature"), RegisterTier.SLOW)

    def test_working_mode_slow(self):
        self.assertEqual(_classify("storage_working_mode"), RegisterTier.SLOW)

    def test_active_power_fast(self):
        self.assertEqual(_classify("active_power"), RegisterTier.FAST)

    def test_input_power_fast(self):
        self.assertEqual(_classify("input_power"), RegisterTier.FAST)

    def test_charge_discharge_fast(self):
        self.assertEqual(_classify("storage_charge_discharge_power"), RegisterTier.FAST)

    def test_soc_normal(self):
        self.assertEqual(_classify("storage_state_of_capacity"), RegisterTier.NORMAL)

    def test_unknown_normal(self):
        self.assertEqual(_classify("some_unknown_register_xyz"), RegisterTier.NORMAL)

    # BUG-3 regression tests ──────────────────────────────────────────────────

    def test_bug3_phase_a_built_in_is_slow_not_fast(self):
        result = _classify("phase_a_active_power_built_in")
        self.assertEqual(result, RegisterTier.SLOW,
            f"BUG-3 regression: phase_a_active_power_built_in → {result.name}, expected SLOW")

    def test_bug3_phase_b_built_in_is_slow(self):
        self.assertEqual(_classify("phase_b_active_power_built_in"), RegisterTier.SLOW)

    def test_bug3_phase_c_built_in_is_slow(self):
        self.assertEqual(_classify("phase_c_active_power_built_in"), RegisterTier.SLOW)

    def test_bug3_active_power_external_is_slow(self):
        result = _classify("active_power_external")
        self.assertEqual(result, RegisterTier.SLOW,
            f"BUG-3 regression: active_power_external → {result.name}, expected SLOW")

    def test_bug3_phase_active_power_external_is_slow(self):
        self.assertEqual(_classify("phase_a_active_power_external"), RegisterTier.SLOW)

    def test_bug3_plain_active_power_still_fast(self):
        """Plain 'active_power' must remain FAST after BUG-3 fix."""
        self.assertEqual(_classify("active_power"), RegisterTier.FAST)

    def test_bug3_inverter_active_power_still_fast(self):
        self.assertEqual(_classify("inverter_active_power"), RegisterTier.FAST)

    def test_bug3_reactive_power_external_slow(self):
        self.assertEqual(_classify("reactive_power_external"), RegisterTier.SLOW)

    def test_bug3_reactive_power_fast(self):
        self.assertEqual(_classify("reactive_power"), RegisterTier.FAST)


# ── Energy counter detection ──────────────────────────────────────────────────

class TestIsEnergyCounter(unittest.TestCase):

    _COUNTERS = [
        "daily_yield", "total_yield", "total_energy", "accumulated_energy",
        "yearly_yield", "total_charged_energy", "total_discharged_energy",
        "grid_accumulated_power", "total_feed_in_energy", "total_pv_energy",
        "current_day_yield", "current_day_charge", "current_day_discharge",
    ]
    _NON_COUNTERS = [
        "active_power", "storage_state_of_capacity", "battery_temperature",
        "inverter_serial_number", "working_mode", "alarm_status",
    ]

    def test_energy_counters_detected(self):
        for name in self._COUNTERS:
            with self.subTest(name=name):
                self.assertTrue(is_energy_counter(name),
                    f"{name} should be an energy counter")

    def test_non_energy_counters(self):
        for name in self._NON_COUNTERS:
            with self.subTest(name=name):
                self.assertFalse(is_energy_counter(name),
                    f"{name} should NOT be an energy counter")


# ── Basic operations ──────────────────────────────────────────────────────────

class TestBasicOps(unittest.TestCase):

    def test_miss_returns_none(self):
        self.assertIsNone(RegisterCache().get("missing"))

    def test_update_then_get(self):
        c = RegisterCache()
        c.update({"soc": _r(80)})
        self.assertEqual(c.get("soc").value, 80)

    def test_merge_prefers_fresh(self):
        c = RegisterCache()
        c.update({"soc": _r(80)})
        self.assertEqual(c.merge({"soc": _r(85)}, ["soc"])["soc"].value, 85)

    def test_merge_fills_from_cache(self):
        c = RegisterCache()
        c.update({"soc": _r(80)})
        self.assertEqual(c.merge({}, ["soc"])["soc"].value, 80)

    def test_dirty_excluded_from_get(self):
        c = RegisterCache()
        c.update({"soc": _r(80)})
        c.invalidate("soc")
        self.assertIsNone(c.get("soc"))

    def test_dirty_excluded_from_merge(self):
        c = RegisterCache()
        c.update({"soc": _r(80)})
        c.invalidate("soc")
        self.assertNotIn("soc", c.merge({}, ["soc"]))

    def test_update_clears_dirty(self):
        c = RegisterCache()
        c.update({"soc": _r(80)})
        c.invalidate("soc")
        c.update({"soc": _r(81)})
        self.assertIsNotNone(c.get("soc"))

    def test_invalidate_nonexistent_noop(self):
        RegisterCache().invalidate("nonexistent")  # must not raise

    def test_invalidate_all_skips_static(self):
        c = RegisterCache()
        c.update({"inverter_serial_number": _r("SN1"), "soc": _r(80)})
        c.invalidate_all()
        self.assertIsNotNone(c.get("inverter_serial_number"), "STATIC must survive")
        self.assertIsNone(c.get("soc"), "NORMAL must be invalidated")

    def test_invalidate_all_including_static(self):
        c = RegisterCache()
        c.update({"inverter_serial_number": _r("SN1")})
        c.invalidate_all_including_static()
        self.assertIsNone(c.get("inverter_serial_number"))

    def test_size_and_clear(self):
        c = RegisterCache()
        c.update({"a": _r(1), "b": _r(2)})
        self.assertEqual(c.size, 2)
        c.clear()
        self.assertEqual(c.size, 0)

    def test_set_telemetry_preserves_store(self):
        """v1.0.3 fix: set_telemetry must not discard cached values."""
        c = RegisterCache()
        c.update({"soc": _r(80)})
        c.set_telemetry(MagicMock())
        self.assertIsNotNone(c.get("soc"), "Cache store must survive set_telemetry()")


# ── filter_stale ──────────────────────────────────────────────────────────────

class TestFilterStale(unittest.TestCase):

    def test_unknown_is_stale(self):
        self.assertIn("unknown", RegisterCache().filter_stale(["unknown"], DEFAULT_TTL))

    def test_fresh_normal_not_stale(self):
        c = RegisterCache()
        c.update({"storage_state_of_capacity": _r(80)})
        self.assertNotIn("storage_state_of_capacity",
            c.filter_stale(["storage_state_of_capacity"], DEFAULT_TTL))

    def test_expired_normal_is_stale(self):
        c = RegisterCache()
        c.update({"storage_state_of_capacity": _r(80)})
        c._store["storage_state_of_capacity"].ts -= 60
        self.assertIn("storage_state_of_capacity",
            c.filter_stale(["storage_state_of_capacity"], DEFAULT_TTL))

    def test_fast_always_stale(self):
        c = RegisterCache()
        c.update({"active_power": _r(1000)})
        self.assertIn("active_power", c.filter_stale(["active_power"], DEFAULT_TTL))

    def test_static_not_stale_after_first_read(self):
        c = RegisterCache()
        c.update({"inverter_serial_number": _r("SN1")})
        self.assertNotIn("inverter_serial_number",
            c.filter_stale(["inverter_serial_number"], DEFAULT_TTL))

    def test_dirty_always_stale(self):
        c = RegisterCache()
        c.update({"soc": _r(80)})
        c.invalidate("soc")
        self.assertIn("soc", c.filter_stale(["soc"], DEFAULT_TTL))

    def test_cache_hit_reported_to_telemetry(self):
        tel = MagicMock()
        c = RegisterCache(telemetry=tel)
        c.update({"storage_state_of_capacity": _r(80)})
        c.filter_stale(["storage_state_of_capacity"], DEFAULT_TTL)
        tel.record_cache_hits.assert_called_once_with(1)


# ── Adaptive TTL ──────────────────────────────────────────────────────────────

class TestAdaptiveTTL(unittest.TestCase):

    def test_ttl_doubles_on_unchanged_value(self):
        c = RegisterCache()
        c.update({"soc": _r(80)})
        before = c._store["soc"].effective_ttl
        c.update({"soc": _r(80)})
        self.assertAlmostEqual(c._store["soc"].effective_ttl,
            before * ADAPTIVE_FACTOR, places=4)

    def test_ttl_resets_on_changed_value(self):
        c = RegisterCache()
        c.update({"soc": _r(80)})
        c.update({"soc": _r(80)})    # stretch
        c.update({"soc": _r(81)})    # change → reset
        self.assertEqual(c._store["soc"].effective_ttl,
            _TIER_BASE_TTL[RegisterTier.NORMAL])

    def test_ttl_capped_at_tier_cap(self):
        c = RegisterCache()
        c.update({"soc": _r(80)})
        c._store["soc"].effective_ttl = _TIER_CAP_TTL[RegisterTier.NORMAL] * 0.9
        c.update({"soc": _r(80)})
        self.assertLessEqual(c._store["soc"].effective_ttl,
            _TIER_CAP_TTL[RegisterTier.NORMAL])


# ── Night mode ────────────────────────────────────────────────────────────────

class TestNightMode(unittest.TestCase):

    def test_night_mode_stretches_normal_ttl(self):
        c = RegisterCache()
        c.update({"soc": _r(80)})
        base = c._store["soc"].effective_ttl
        c.set_night_mode(True)
        self.assertGreater(c._effective_ttl(c._store["soc"]), base)

    def test_night_mode_does_not_stretch_static(self):
        c = RegisterCache()
        c.update({"inverter_serial_number": _r("SN")})
        base = c._store["inverter_serial_number"].effective_ttl
        c.set_night_mode(True)
        self.assertEqual(c._effective_ttl(c._store["inverter_serial_number"]), base)

    def test_wakeup_resets_normal_ttl(self):
        c = RegisterCache()
        c.update({"soc": _r(80)})
        c.set_night_mode(True)
        c._store["soc"].effective_ttl = 600.0
        c.set_night_mode(False)
        self.assertEqual(c._store["soc"].effective_ttl,
            _TIER_BASE_TTL[RegisterTier.NORMAL])

    def test_night_mode_property(self):
        c = RegisterCache()
        self.assertFalse(c.night_mode)
        c.set_night_mode(True)
        self.assertTrue(c.night_mode)
