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
Quality        = _MOD.Quality
Reason         = _MOD.Reason
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


class TestUserRequestedTierReclassification(unittest.TestCase):  # v2.0.9
    """User-requested register tier reclassification, this release.
    Every entry verified directly against _classify() before being
    added -- not applied blindly from the user's own category labels.
    See register_cache.py's own comments at each override for the full
    per-register reasoning."""

    def test_day_active_power_peak_moved_to_slow(self):
        """A daily peak-tracking statistic, not an instantaneous power
        reading. Adversarial: my own first attempt at this fix put it
        in the wrong list (_SLOW_SUBSTRINGS, checked after FAST) and it
        silently stayed FAST -- this confirms the corrected placement
        (_SLOW_PRIORITY_SUBSTRINGS, checked before FAST) actually works."""
        self.assertEqual(_classify("day_active_power_peak"), RegisterTier.SLOW)

    def test_grid_accumulated_reactive_power_moved_to_slow(self):
        """A genuine repeat of BUG-3's own documented pattern: a
        lifetime accumulator reaching FAST via a bare 'reactive_power'
        substring match before ever reaching its own 'grid_accumulated'
        SLOW substring."""
        self.assertEqual(
            _classify("grid_accumulated_reactive_power"), RegisterTier.SLOW,
        )

    def test_active_power_settings_moved_to_normal(self):
        """Control-relevant SETTINGS (a mode enum, two derating
        parameters), not continuously-varying power VALUES -- FAST's
        freshness window buys nothing for these at real bus cost."""
        for name in (
            "active_power_control_mode",
            "active_power_fixed_value_derating",
            "active_power_percentage_derating",
        ):
            self.assertEqual(
                _classify(name), RegisterTier.NORMAL,
                f"{name} should be NORMAL, not FAST",
            )

    def test_used_energy_counters_moved_to_normal(self):
        """Genuinely-used lifetime energy counters, each verified
        against its real register_names constant before being added --
        not assumed from the user's own category labels."""
        for name in (
            "total_pv_energy_yield", "accumulated_yield_energy",
            "total_energy_consumption", "grid_accumulated_energy",
            "inverter_total_energy_yield", "inverter_total_absorbed_energy",
        ):
            self.assertEqual(
                _classify(name), RegisterTier.NORMAL,
                f"{name} should be NORMAL, not SLOW",
            )

    def test_dashboard_backing_registers_are_unaffected_and_stay_fast(self):
        """Negative case: the actual real-time power registers backing
        a live power-flow dashboard (PV input, grid meter, battery
        charge/discharge) must be completely untouched by this batch of
        changes."""
        for name in (
            "input_power", "power_meter_active_power",
            "storage_charge_discharge_power",
        ):
            self.assertEqual(
                _classify(name), RegisterTier.FAST,
                f"{name} must remain FAST -- it backs a live dashboard value",
            )

    def test_site_wide_totals_are_not_accumulators_and_stay_fast(self):
        """Negative case confirming a deliberately-NOT-made change:
        sdongle_total_active/input/battery_power look superficially
        similar to the accumulator-mislabelled registers fixed above
        (same 'total_' + power-substring shape), but 'total' here means
        spatial aggregation (site-wide sum right now), not temporal
        accumulation -- verified against real register_names before
        concluding these were correctly classified already, not changed."""
        for name in (
            "sdongle_total_active_power", "sdongle_total_input_power",
            "sdongle_total_battery_power",
        ):
            self.assertEqual(
                _classify(name), RegisterTier.FAST,
                f"{name} is a real-time site-wide sum, not an accumulator "
                f"-- must remain FAST",
            )

    def test_already_correct_registers_are_unaffected(self):
        """Negative case: grid_exported_energy (already NORMAL, by not
        matching any SLOW substring at all) and the battery total_
        charge/discharge overrides (already NORMAL from a prior
        release) must be completely unaffected by this batch."""
        self.assertEqual(_classify("grid_exported_energy"), RegisterTier.NORMAL)
        self.assertEqual(_classify("storage_total_charge"), RegisterTier.NORMAL)
        self.assertEqual(_classify("storage_total_discharge"), RegisterTier.NORMAL)

    def test_bug3_original_fix_still_holds(self):
        """Adversarial: confirms this batch of additions to
        _SLOW_PRIORITY_SUBSTRINGS didn't accidentally break the
        original BUG-3 fix it shares a list with."""
        for name in (
            "phase_a_active_power", "phase_b_active_power",
            "phase_c_active_power", "active_power_built_in",
            "active_power_external",
        ):
            self.assertEqual(
                _classify(name), RegisterTier.SLOW,
                f"{name}: BUG-3's original fix regressed",
            )


class TestDEF015PackCounterTierCoverage(unittest.TestCase):  # v2.0.8
    """DEF-015 (external ICS quality/defect/architecture audit --
    confirmed): _TIER_OVERRIDES only had NORMAL entries for unit 1's
    pack counters -- v2.0.7's own TOPO-01 work added real support for a
    genuine second storage unit, but this dict was never updated to
    match, leaving unit 2's identical counters to silently fall through
    to the generic SLOW tier."""

    def test_unit_1_pack_counters_are_normal_tier(self):
        for pack in (1, 2, 3):
            for suffix in ("total_charge", "total_discharge"):
                name = f"storage_unit_1_battery_pack_{pack}_{suffix}"
                self.assertEqual(
                    _classify(name), RegisterTier.NORMAL,
                    f"{name} must be NORMAL tier",
                )

    def test_unit_2_pack_counters_are_normal_tier(self):
        """The actual regression this closes."""
        for pack in (1, 2, 3):
            for suffix in ("total_charge", "total_discharge"):
                name = f"storage_unit_2_battery_pack_{pack}_{suffix}"
                self.assertEqual(
                    _classify(name), RegisterTier.NORMAL,
                    f"{name} must be NORMAL tier -- unit 2 must match "
                    f"unit 1's own tier exactly, not fall through to SLOW",
                )

    def test_unit_1_and_unit_2_tiers_are_identical(self):
        """Adversarial: the two units' equivalent registers must
        classify identically -- the whole point being that two
        physically equivalent storage units feed the capacity-learning
        algorithm with the SAME counter freshness, not a biased one."""
        for pack in (1, 2, 3):
            for suffix in ("total_charge", "total_discharge"):
                tier_1 = _classify(f"storage_unit_1_battery_pack_{pack}_{suffix}")
                tier_2 = _classify(f"storage_unit_2_battery_pack_{pack}_{suffix}")
                self.assertEqual(tier_1, tier_2)

    def test_tier_coverage_matches_resolved_topology_not_a_second_hand_list(self):
        """The audit's own secondary recommendation: cross-check tier
        coverage against whatever required_register_names() actually
        resolves for a given topology, rather than trusting a second,
        independently-maintained list to stay in sync by hand -- the
        exact pattern that let DEF-015 happen in the first place."""
        bhm_src = pathlib.Path(__file__).parent.parent / "battery_health_manager.py"
        spec = importlib.util.spec_from_file_location("bhm_test_def015", str(bhm_src))
        bhm = importlib.util.module_from_spec(spec)
        bhm.__package__ = "huawei_solar"
        # battery_health_manager.py needs more of the huawei_solar/
        # homeassistant surface than this file's own lightweight stub
        # provides; only pack_slots_for_units()/required_register_names()
        # are needed here, both defined before any heavier import in
        # that module executes, so a partial/best-effort exec is
        # acceptable -- fall back to a hand-built equivalent if the full
        # module can't load in this lightweight test environment.
        try:
            spec.loader.exec_module(bhm)
            required = bhm.required_register_names([1, 2])
        except Exception:
            required = [
                f"storage_unit_{unit}_battery_pack_{pack}_{suffix}"
                for unit in (1, 2) for pack in (1, 2, 3)
                for suffix in (
                    "voltage", "maximum_temperature", "minimum_temperature",
                    "working_status", "soh_calibration_status",
                    "state_of_capacity", "charge_discharge_power",
                    "total_charge", "total_discharge", "current",
                    "serial_number",
                )
            ]
        counter_names = [
            n for n in required
            if n.endswith("_total_charge") or n.endswith("_total_discharge")
        ]
        self.assertGreater(len(counter_names), 0, "test setup invalid")
        for name in counter_names:
            self.assertEqual(
                _classify(name), RegisterTier.NORMAL,
                f"{name} is required by the resolved topology but is not "
                f"NORMAL tier -- tier coverage has drifted from actual "
                f"topology again",
            )

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

    # ── v1.1.6 exact-name tier overrides (battery-health data quality) ───────
    def test_override_storage_total_charge_normal(self):
        """BHI segment/efficiency energy needs 30 s counters, not 5-min SLOW."""
        self.assertEqual(_classify("storage_total_charge"), RegisterTier.NORMAL)

    def test_override_storage_total_discharge_normal(self):
        self.assertEqual(_classify("storage_total_discharge"), RegisterTier.NORMAL)

    def test_override_storage_rated_capacity_slow(self):
        """Recalibration watch needs periodic re-reads; STATIC would be blind."""
        self.assertEqual(_classify("storage_rated_capacity"), RegisterTier.SLOW)

    def test_override_is_exact_name_only(self):
        """Other total_*/rated_capacity names keep their substring tiers."""
        self.assertEqual(_classify("total_energy_export"), RegisterTier.SLOW)
        self.assertEqual(_classify("battery_1_rated_capacity"),
                         RegisterTier.STATIC)
        self.assertEqual(_classify("storage_unit_1_total_charge"),
                         RegisterTier.SLOW)


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
        # v2.0.0: invalidate_all() marks non-STATIC entries UNCERTAIN, not
        # unservable -- this IS the fix (V2_ARCHITECTURE_DESIGN.md §1/§10.4's
        # root defect). get() now correctly SERVES an UNCERTAIN value; the
        # degradation is visible via quality_of(), not by the value vanishing.
        c = RegisterCache()
        c.update({"inverter_serial_number": _r("SN1"), "soc": _r(80)})
        c.invalidate_all()

        self.assertIsNotNone(c.get("inverter_serial_number"), "STATIC must survive")
        static_q, static_r, _ = c.quality_of("inverter_serial_number")
        self.assertEqual(static_q, Quality.GOOD, "STATIC must be fully unaffected")

        self.assertIsNotNone(
            c.get("soc"),
            "NORMAL must still be SERVED (the whole point of the v2.0.0 fix) "
            "-- it should be degraded, not dropped",
        )
        soc_q, soc_r, soc_age = c.quality_of("soc")
        self.assertEqual(soc_q, Quality.UNCERTAIN, "NORMAL must be degraded to UNCERTAIN")
        self.assertEqual(soc_r, Reason.LINK_DOWN)
        self.assertIsNotNone(soc_age)

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

    def test_fast_not_immediately_stale(self):
        """v1.3.21 (Defect Y): FAST's base TTL changed from 0.0 to 3.0s --
        a register just updated is no longer considered stale again
        instantly, only after its (small) TTL actually elapses. See
        test_fast_stale_after_its_ttl_elapses for the other half."""
        c = RegisterCache()
        c.update({"active_power": _r(1000)})
        self.assertNotIn("active_power", c.filter_stale(["active_power"], DEFAULT_TTL))

    def test_fast_stale_after_its_ttl_elapses(self):
        c = RegisterCache()
        c.update({"active_power": _r(1000)})
        c._store["active_power"].ts -= 10  # older than FAST's 3.0s base TTL
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
