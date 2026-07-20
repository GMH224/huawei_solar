"""Tests for battery_health.py — pure engine, no HA runtime required.

Covers (ICS-style audit traceability — see AUDIT_1.1.5.md):
  T1  Input validation: implausible values discarded (not clipped), per-field
  T2  SOH_cap: spec §11 two-segment weighted-average vector (v2 weighting)
  T3  Segment detection: qualification, shallow-segment drop, charging blip
  T4  SOC-correction guard: implausible implied capacity → segment discarded
  T5  Freshness weighting + golden (Huawei SOH calibration) boost
  T6  Data-gap handling: mid-segment gap discards segment (no guessing)
  T7  Counter reset detection: decrease = reset event, never negative energy
  T8  SOH_eff: baseline capture, drift → score, implausible η discarded
  T9  SOH_bal: spec §11 vector; offline pack excluded, <2 packs → no sample
  T10 Stress accumulator: Q10/f(SOC) math, long-gap Δt exclusion, pruning
  T11 Composite: full vector; renormalization on missing terms; never 0-crater
  T12 Confidence: low / normal / stale transitions
  T13 Persistence: to_dict/restore round-trip; unknown schema → fresh start
  T14 Forecast: predicted SOH decreases with age; divergence sign
"""
from __future__ import annotations

import importlib.util
import math
import pathlib
import sys
import unittest

# ── Import battery_health directly (avoid package-level HA imports) ──────────
_BASE = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "battery_health", _BASE / "battery_health.py"
)
bh = importlib.util.module_from_spec(_spec)
sys.modules["battery_health"] = bh
_spec.loader.exec_module(bh)

HOUR = 3600.0
DAY = 86_400.0


def _cfg(**overrides) -> "bh.BatteryHealthConfig":
    cfg = bh.BatteryHealthConfig()
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _sample(ts, soc=None, power=None, temp=None, chg=None, dis=None,
            packs=None, calib=False) -> "bh.HealthSample":
    return bh.HealthSample(
        timestamp=ts, soc=soc, power_w=power, battery_temp_c=temp,
        lifetime_charge_kwh=chg, lifetime_discharge_kwh=dis,
        packs=packs or [], soh_calibration_active=calib,
    )


def _run_discharge(engine, t0, soc0, soc1, dis0, dis1, steps=20, power=-2500.0,
                   chg=1000.0, temp=20.0, calib=False):
    """Drive the engine through a clean discharge segment then close it."""
    for i in range(steps + 1):
        frac = i / steps
        engine.update(_sample(
            t0 + i * 60, soc=soc0 + (soc1 - soc0) * frac, power=power,
            temp=temp, chg=chg, dis=dis0 + (dis1 - dis0) * frac, calib=calib,
        ))
    # Close: idle tick
    engine.update(_sample(t0 + (steps + 1) * 60, soc=soc1, power=0.0,
                          temp=temp, chg=chg, dis=dis1, calib=calib))
    return t0 + (steps + 1) * 60


# ═════════════════════════════════════════════════════════════════════════════
class TestValidation(unittest.TestCase):  # T1
    def test_implausible_soc_discarded_not_clipped(self):
        s = bh.validate_sample(_sample(0, soc=127.0, power=100.0))
        self.assertIsNone(s.soc)              # discarded, not clipped to 100
        self.assertEqual(s.power_w, 100.0)    # other fields survive

    def test_power_beyond_hardware_limit_discarded(self):
        s = bh.validate_sample(_sample(0, power=99_999.0))
        self.assertIsNone(s.power_w)

    def test_temperature_bounds(self):
        self.assertIsNone(bh.validate_sample(_sample(0, temp=75.0)).battery_temp_c)
        self.assertEqual(bh.validate_sample(_sample(0, temp=25.0)).battery_temp_c, 25.0)

    def test_non_numeric_and_nan_discarded(self):
        self.assertIsNone(bh.validate_sample(_sample(0, soc="x")).soc)
        self.assertIsNone(bh.validate_sample(_sample(0, soc=float("nan"))).soc)

    def test_pack_fields_validated_individually(self):
        s = bh.validate_sample(_sample(0, packs=[
            bh.PackSample(voltage=26.4, temp_max=900.0, temp_min=20.0, online=True)
        ]))
        self.assertEqual(s.packs[0].voltage, 26.4)
        self.assertIsNone(s.packs[0].temp_max)


class TestSegmentCapacity(unittest.TestCase):  # T2, T3
    def test_spec_vector_two_segments(self):
        """Spec §11 vector, freshness = 1 for both segments (fresh full charge
        before each): weighted avg = 20.41 kWh → SOH_cap = 98.6."""
        cfg = _cfg(freshness_tau_kwh=1e12)  # neutralize freshness for the vector
        eng = bh.BatteryHealthEngine(cfg)
        t = 0.0
        # Segment 1: ΔSOC 15 (95→80), 2.85 kWh
        t = _run_discharge(eng, t, 95.0, 80.0, 100.0, 102.85)
        # Segment 2: ΔSOC 60 (98→38), 12.30 kWh
        t = _run_discharge(eng, t + 600, 98.0, 38.0, 102.85, 115.15)
        soh, attrs = eng.segments.soh_capacity()
        self.assertEqual(attrs["segment_count"], 2)
        self.assertAlmostEqual(attrs["estimated_capacity_kwh"], 20.41, places=2)
        self.assertAlmostEqual(soh, 20.41 / 20.7 * 100.0, places=1)  # 98.6

    def test_shallow_segment_dropped(self):
        eng = bh.BatteryHealthEngine(_cfg())
        _run_discharge(eng, 0.0, 90.0, 85.0, 0.0, 1.0)  # ΔSOC 5 < 10
        self.assertEqual(len(eng.segments.segments), 0)

    def test_charging_blip_closes_segment_at_last_low_point(self):
        eng = bh.BatteryHealthEngine(_cfg())
        t = 0.0
        for i, soc in enumerate([80, 75, 70, 65, 60]):
            eng.update(_sample(t + i * 60, soc=float(soc), power=-3000.0,
                               chg=0.0, dis=float(i)))
        # PV blip: SOC rises → segment must close using last decreasing point
        eng.update(_sample(t + 5 * 60, soc=63.0, power=1500.0, chg=0.5, dis=4.0))
        self.assertEqual(len(eng.segments.segments), 1)
        seg = eng.segments.segments[0]
        self.assertEqual(seg.soc_start, 80.0)
        self.assertEqual(seg.soc_end, 60.0)


class TestSocCorrectionGuard(unittest.TestCase):  # T4
    def test_implied_capacity_out_of_band_discarded(self):
        """BMS SOC snap mid-segment → implied capacity implausible → discard."""
        eng = bh.BatteryHealthEngine(_cfg())
        t = 0.0
        # SOC drops 40 points but only 2 kWh flowed → implied 5 kWh < 8 kWh min
        for i, (soc, dis) in enumerate([(90, 0.0), (80, 0.5), (65, 1.0),
                                        (55, 1.5), (50, 2.0)]):
            eng.update(_sample(t + i * 60, soc=float(soc), power=-3000.0,
                               chg=0.0, dis=dis))
        eng.update(_sample(t + 5 * 60, soc=50.0, power=0.0, chg=0.0, dis=2.0))
        self.assertEqual(len(eng.segments.segments), 0)
        self.assertGreaterEqual(eng.segments.discarded_segments, 1)


class TestFreshnessAndGolden(unittest.TestCase):  # T5
    def test_freshness_decays_with_throughput_since_full(self):
        cfg = _cfg(freshness_tau_kwh=40.0)
        eng = bh.BatteryHealthEngine(cfg)
        # Full charge first (resets throughput), then 40 kWh of discharge
        eng.update(_sample(0, soc=100.0, power=0.0, chg=0.0, dis=0.0))
        t = _run_discharge(eng, 60.0, 90.0, 70.0, 0.0, 4.0)      # fresh
        t = _run_discharge(eng, t + 600, 70.0, 30.0, 4.0, 12.0)  # 4 kWh used
        seg1, seg2 = eng.segments.segments
        self.assertGreater(seg1.freshness, seg2.freshness)
        self.assertAlmostEqual(seg1.freshness, 1.0, places=2)
        self.assertAlmostEqual(seg2.freshness, math.exp(-4.0 / 40.0), places=3)

    def test_golden_segment_weight_boost(self):
        cfg = _cfg(freshness_tau_kwh=1e12)
        eng = bh.BatteryHealthEngine(cfg)
        _run_discharge(eng, 0.0, 95.0, 75.0, 0.0, 4.0, calib=True)
        seg = eng.segments.segments[0]
        self.assertTrue(seg.golden)
        self.assertAlmostEqual(seg.weight(cfg), 20.0 ** 2 * 4.0, places=1)


class TestGapHandling(unittest.TestCase):  # T6
    def test_gap_mid_segment_discards(self):
        eng = bh.BatteryHealthEngine(_cfg())
        for i, soc in enumerate([90, 80, 70]):
            eng.update(_sample(i * 60, soc=float(soc), power=-3000.0,
                               chg=0.0, dis=float(i * 2)))
        eng.mark_gap()  # coordinator failure
        # Discharge "ends" after the gap — must NOT produce a segment
        eng.update(_sample(600, soc=55.0, power=0.0, chg=0.0, dis=7.0))
        self.assertEqual(len(eng.segments.segments), 0)
        self.assertGreaterEqual(eng.segments.discarded_segments, 1)

    def test_missing_field_mid_segment_discards(self):
        eng = bh.BatteryHealthEngine(_cfg())
        for i, soc in enumerate([90, 80, 70]):
            eng.update(_sample(i * 60, soc=float(soc), power=-3000.0,
                               chg=0.0, dis=float(i * 2)))
        eng.update(_sample(300, soc=None, power=-3000.0, chg=0.0, dis=6.0))
        eng.update(_sample(360, soc=55.0, power=0.0, chg=0.0, dis=7.0))
        self.assertEqual(len(eng.segments.segments), 0)


class TestCounterReset(unittest.TestCase):  # T7
    def test_reset_detected_and_offset_applied(self):
        mon = bh.CounterMonitor("test")
        self.assertEqual(mon.feed(100.0), 100.0)
        self.assertEqual(mon.feed(105.0), 105.0)
        # Firmware update resets the counter
        self.assertEqual(mon.feed(2.0), 107.0)  # 105 offset + 2
        self.assertEqual(mon.reset_count, 1)

    def test_small_jitter_not_a_reset(self):
        mon = bh.CounterMonitor("test")
        mon.feed(100.0)
        mon.feed(99.5)  # within tolerance
        self.assertEqual(mon.reset_count, 0)

    def test_engine_reset_invalidates_open_segment(self):
        eng = bh.BatteryHealthEngine(_cfg())
        for i, soc in enumerate([90, 80, 70]):
            eng.update(_sample(i * 60, soc=float(soc), power=-3000.0,
                               chg=50.0, dis=100.0 + i * 2))
        # Counter reset mid-segment
        eng.update(_sample(300, soc=60.0, power=-3000.0, chg=50.0, dis=1.0))
        eng.update(_sample(360, soc=55.0, power=0.0, chg=50.0, dis=2.0))
        self.assertEqual(len(eng.segments.segments), 0)
        self.assertEqual(eng.report.attributes["counter_resets"], 1)


class TestEfficiency(unittest.TestCase):  # T8
    def _anchor(self, eng, ts, chg, dis):
        eng.efficiency.feed(_sample(ts, soc=99.0, power=0.0, chg=chg, dis=dis))

    def test_baseline_capture_and_perfect_efficiency(self):
        cfg = _cfg(eff_baseline_windows=3, eff_min_window_charge_kwh=30.0)
        eng = bh.BatteryHealthEngine(cfg)
        chg = dis = 0.0
        self._anchor(eng, 0, chg, dis)
        for i in range(1, 5):
            chg += 40.0
            dis += 40.0 * 0.96
            self._anchor(eng, i * DAY, chg, dis)
        self.assertIsNotNone(eng.efficiency.baseline)
        self.assertAlmostEqual(eng.efficiency.baseline, 0.96, places=3)
        soh, _ = eng.efficiency.soh_efficiency()
        self.assertAlmostEqual(soh, 100.0, places=1)

    def test_efficiency_drift_lowers_score(self):
        cfg = _cfg(eff_baseline_windows=2, eff_rolling_windows=2,
                   eff_pts_per_pct_loss=8.0)
        eng = bh.BatteryHealthEngine(cfg)
        chg = dis = 0.0
        self._anchor(eng, 0, chg, dis)
        for i in range(1, 3):                       # baseline windows @ 96%
            chg += 40.0; dis += 40.0 * 0.96
            self._anchor(eng, i * DAY, chg, dis)
        for i in range(3, 6):                       # degraded windows @ 94%
            chg += 40.0; dis += 40.0 * 0.94
            self._anchor(eng, i * DAY, chg, dis)
        soh, attrs = eng.efficiency.soh_efficiency()
        # 2 %-points loss × 8 pts = 84
        self.assertAlmostEqual(soh, 100.0 - 2.0 * 8.0, delta=0.5)
        self.assertAlmostEqual(attrs["efficiency_current"], 0.94, places=3)

    def test_implausible_eta_discarded(self):
        eng = bh.BatteryHealthEngine(_cfg())
        self._anchor(eng, 0, 0.0, 0.0)
        self._anchor(eng, DAY, 40.0, 80.0)  # η = 2.0 → impossible
        self.assertEqual(len(eng.efficiency.windows), 0)

    def test_reset_baseline(self):
        cfg = _cfg(eff_baseline_windows=1)
        eng = bh.BatteryHealthEngine(cfg)
        self._anchor(eng, 0, 0.0, 0.0)
        self._anchor(eng, DAY, 40.0, 38.0)
        self.assertIsNotNone(eng.efficiency.baseline)
        eng.reset_efficiency_baseline()
        self.assertIsNone(eng.efficiency.baseline)


class TestBalance(unittest.TestCase):  # T9
    def _packs(self, volts, temps, online=(True, True, True)):
        return [bh.PackSample(voltage=v, temp_max=t, temp_min=t - 1.0, online=o)
                for v, t, o in zip(volts, temps, online)]

    def test_spec_vector(self):
        """Spec §11: ΔV=0 → 100; ΔT=2.7 → 75.7; SOH_bal = 87.9."""
        eng = bh.BatteryHealthEngine(_cfg())
        eng.balance.feed(_sample(0, soc=98.0, power=0.0,
                                 packs=self._packs([26.4, 26.4, 26.4],
                                                   [26.2, 28.9, 27.5])))
        soh, _ = eng.balance.soh_balance()
        self.assertAlmostEqual(soh, 87.9, delta=0.15)

    def test_offline_pack_excluded(self):
        eng = bh.BatteryHealthEngine(_cfg())
        eng.balance.feed(_sample(0, soc=98.0, power=0.0,
                                 packs=self._packs([26.4, 0.0, 26.5],
                                                   [26.0, 0.0, 26.5],
                                                   online=(True, False, True))))
        soh, attrs = eng.balance.soh_balance()
        self.assertEqual(attrs["packs_included"], [1, 3])
        self.assertEqual(attrs["packs_excluded"], [2])
        self.assertIsNotNone(soh)

    def test_fewer_than_two_online_packs_no_sample(self):
        eng = bh.BatteryHealthEngine(_cfg())
        eng.balance.feed(_sample(0, soc=98.0, power=0.0,
                                 packs=self._packs([26.4, 0.0, 0.0],
                                                   [26.0, 0.0, 0.0],
                                                   online=(True, False, False))))
        soh, _ = eng.balance.soh_balance()
        self.assertIsNone(soh)

    def test_gating_rejects_loaded_or_low_soc(self):
        eng = bh.BatteryHealthEngine(_cfg())
        packs = self._packs([26.4, 26.4, 26.4], [26.0, 26.0, 26.0])
        eng.balance.feed(_sample(0, soc=98.0, power=3000.0, packs=packs))  # loaded
        eng.balance.feed(_sample(0, soc=50.0, power=0.0, packs=packs))     # low SOC
        self.assertEqual(len(eng.balance.scores), 0)


class TestStress(unittest.TestCase):  # T10
    def test_reference_conditions_ratio_one(self):
        eng = bh.BatteryHealthEngine(_cfg())
        for i in range(10):
            eng.stress.feed(_sample(i * 300.0, soc=50.0, temp=25.0))
        self.assertAlmostEqual(eng.stress.stress_ratio(), 1.0, places=3)

    def test_q10_and_soc_factor(self):
        eng = bh.BatteryHealthEngine(_cfg())
        # 35°C at SOC 100 → Q10 factor 2.0 × SOC factor 2.5 = 5.0
        for i in range(10):
            eng.stress.feed(_sample(i * 300.0, soc=100.0, temp=35.0))
        self.assertAlmostEqual(eng.stress.stress_ratio(), 5.0, places=2)

    def test_long_gap_excluded_from_denominator(self):
        eng = bh.BatteryHealthEngine(_cfg())
        eng.stress.feed(_sample(0.0, soc=50.0, temp=25.0))
        eng.stress.feed(_sample(300.0, soc=50.0, temp=25.0))
        # 2h outage, then hot samples — the outage Δt must not dilute them
        eng.stress.feed(_sample(300.0 + 2 * HOUR, soc=50.0, temp=35.0))
        eng.stress.feed(_sample(600.0 + 2 * HOUR, soc=50.0, temp=35.0))
        ratio = eng.stress.stress_ratio()
        self.assertAlmostEqual(ratio, (1.0 * 300 + 2.0 * 300) / 600, places=2)

    def test_pruning_drops_old_buckets(self):
        cfg = _cfg(stress_window_days=1.0)
        eng = bh.BatteryHealthEngine(cfg)
        eng.stress.feed(_sample(0.0, soc=50.0, temp=25.0))
        eng.stress.feed(_sample(300.0, soc=50.0, temp=25.0))
        eng.stress.prune(3 * DAY)
        self.assertIsNone(eng.stress.stress_ratio())


class TestComposite(unittest.TestCase):  # T11
    def test_renormalization_missing_terms(self):
        """SOH_eff & SOH_bal unavailable → BHI = SOH_cap (weight 1.0),
        never cratered by implicit zeros."""
        cfg = _cfg(freshness_tau_kwh=1e12)
        eng = bh.BatteryHealthEngine(cfg)
        t = _run_discharge(eng, 0.0, 95.0, 80.0, 100.0, 102.85)
        _run_discharge(eng, t + 600, 98.0, 38.0, 102.85, 115.15)
        r = eng.report
        self.assertEqual(r.attributes["contributing_terms"], ["capacity"])
        self.assertAlmostEqual(r.bhi, r.soh_capacity, places=1)
        self.assertGreater(r.bhi, 90.0)

    def test_no_terms_bhi_none(self):
        eng = bh.BatteryHealthEngine(_cfg())
        eng.update(_sample(0, soc=50.0, power=0.0, chg=0.0, dis=0.0, temp=20.0))
        self.assertIsNone(eng.report.bhi)   # unavailable, NOT 0

    def test_full_composite_weighting(self):
        """All three terms present → weighted by normalized 0.6/0.2/0.2."""
        cfg = _cfg(freshness_tau_kwh=1e12, eff_baseline_windows=1,
                   eff_rolling_windows=1)
        eng = bh.BatteryHealthEngine(cfg)
        # capacity ≈ 98.6
        t = _run_discharge(eng, 0.0, 95.0, 80.0, 100.0, 102.85)
        t = _run_discharge(eng, t + 600, 98.0, 38.0, 102.85, 115.15)
        # efficiency: baseline @ η=0.96 then current identical → 100
        eng.efficiency.feed(_sample(t + 100, soc=99.0, power=0.0, chg=200.0, dis=150.0))
        eng.efficiency.feed(_sample(t + DAY, soc=99.0, power=0.0, chg=240.0, dis=188.4))
        # balance: perfect packs → 100
        eng.balance.feed(_sample(t + DAY, soc=99.0, power=0.0, packs=[
            bh.PackSample(voltage=26.4, temp_max=25.0, temp_min=24.0, online=True),
            bh.PackSample(voltage=26.4, temp_max=25.0, temp_min=24.0, online=True),
            bh.PackSample(voltage=26.4, temp_max=25.5, temp_min=24.0, online=True),
        ]))
        eng.update(_sample(t + DAY + 60, soc=99.0, power=0.0, chg=240.0,
                           dis=188.4, temp=20.0))
        r = eng.report
        self.assertEqual(sorted(r.attributes["contributing_terms"]),
                         ["balance", "capacity", "efficiency"])
        expected = 0.6 * r.soh_capacity + 0.2 * r.soh_efficiency + 0.2 * r.soh_balance
        self.assertAlmostEqual(r.bhi, expected, delta=0.1)

    def test_weight_auto_normalization(self):
        cfg = _cfg(weight_capacity=3.0, weight_efficiency=1.0, weight_balance=1.0)
        w = cfg.normalized_weights()
        self.assertAlmostEqual(sum(w), 1.0, places=9)
        self.assertAlmostEqual(w[0], 0.6, places=9)

    def test_efc_and_warranty(self):
        """Spec §11: 3105 kWh lifetime discharge → EFC 150, SOH_cyc-equivalent
        warranty consumption = 3105/28840 = 10.77%."""
        eng = bh.BatteryHealthEngine(_cfg())
        eng.update(_sample(0, soc=50.0, power=0.0, chg=3300.0, dis=3105.0, temp=20.0))
        r = eng.report
        self.assertAlmostEqual(r.efc, 150.0, places=1)
        self.assertAlmostEqual(r.warranty_consumed_pct, 10.8, delta=0.1)


class TestConfidence(unittest.TestCase):  # T12
    def test_low_without_segments_or_baseline(self):
        eng = bh.BatteryHealthEngine(_cfg())
        eng.update(_sample(0, soc=50.0, power=0.0, chg=0.0, dis=0.0, temp=20.0))
        self.assertEqual(eng.report.confidence, "low")

    def test_normal_with_enough_segments_and_baseline(self):
        cfg = _cfg(freshness_tau_kwh=1e12, confidence_min_segments=2,
                   eff_baseline_windows=1)
        eng = bh.BatteryHealthEngine(cfg)
        t = _run_discharge(eng, 0.0, 95.0, 80.0, 100.0, 103.0)
        t = _run_discharge(eng, t + 600, 90.0, 70.0, 103.0, 107.0)
        eng.efficiency.feed(_sample(t + 100, soc=99.0, power=0.0, chg=200.0, dis=150.0))
        eng.efficiency.feed(_sample(t + DAY, soc=99.0, power=0.0, chg=240.0, dis=188.0))
        eng.update(_sample(t + DAY + 60, soc=99.0, power=0.0, chg=240.0,
                           dis=188.0, temp=20.0))
        self.assertEqual(eng.report.confidence, "normal")

    def test_stale_after_60_days_without_segment(self):
        cfg = _cfg(freshness_tau_kwh=1e12, confidence_min_segments=1,
                   eff_baseline_windows=1, capacity_window_days=365.0)
        eng = bh.BatteryHealthEngine(cfg)
        t = _run_discharge(eng, 0.0, 95.0, 80.0, 100.0, 103.0)
        eng.update(_sample(t + 61 * DAY, soc=50.0, power=0.0, chg=103.0,
                           dis=103.0, temp=20.0))
        self.assertEqual(eng.report.confidence, "stale")


class TestPersistence(unittest.TestCase):  # T13
    def test_round_trip(self):
        cfg = _cfg(freshness_tau_kwh=1e12, eff_baseline_windows=1)
        eng = bh.BatteryHealthEngine(cfg)
        t = _run_discharge(eng, 0.0, 95.0, 80.0, 100.0, 103.0)
        eng.efficiency.feed(_sample(t + 100, soc=99.0, power=0.0, chg=200.0, dis=150.0))
        eng.efficiency.feed(_sample(t + DAY, soc=99.0, power=0.0, chg=240.0, dis=188.0))
        eng.stress.feed(_sample(t, soc=50.0, temp=25.0))
        eng.stress.feed(_sample(t + 300, soc=50.0, temp=25.0))
        data = eng.to_dict()

        # JSON-serializable check (Store requirement)
        import json
        json.dumps(data)

        eng2 = bh.BatteryHealthEngine(cfg)
        eng2.restore(data)
        self.assertEqual(len(eng2.segments.segments), len(eng.segments.segments))
        self.assertEqual(eng2.efficiency.baseline, eng.efficiency.baseline)
        self.assertEqual(eng2.first_seen_ts, eng.first_seen_ts)
        s1, _ = eng.segments.soh_capacity()
        s2, _ = eng2.segments.soh_capacity()
        self.assertAlmostEqual(s1, s2, places=6)

    def test_unknown_schema_starts_fresh(self):
        eng = bh.BatteryHealthEngine(_cfg())
        eng.restore({"schema_version": 999, "first_seen_ts": 123.0})
        self.assertIsNone(eng.first_seen_ts)

    def test_none_restore_is_noop(self):
        eng = bh.BatteryHealthEngine(_cfg())
        eng.restore(None)
        self.assertIsNone(eng.first_seen_ts)


class TestForecast(unittest.TestCase):  # T14
    def test_predicted_soh_decreases_with_age(self):
        cfg = _cfg()
        eng = bh.BatteryHealthEngine(cfg)
        eng.update(_sample(0.0, soc=50.0, power=0.0, chg=0.0, dis=0.0, temp=25.0))
        p0 = eng.report.predicted_soh
        eng.update(_sample(365.25 * DAY, soc=50.0, power=0.0, chg=0.0,
                           dis=0.0, temp=25.0))
        p1 = eng.report.predicted_soh
        self.assertLess(p1, p0)
        # ~2.5% calendar loss after 1 year at stress ratio 1
        self.assertAlmostEqual(p1, 100.0 - 2.5, delta=0.3)

    def test_divergence_sign(self):
        cfg = _cfg(freshness_tau_kwh=1e12)
        eng = bh.BatteryHealthEngine(cfg)
        t = _run_discharge(eng, 0.0, 95.0, 80.0, 100.0, 102.85)
        _run_discharge(eng, t + 600, 98.0, 38.0, 102.85, 115.15)
        r = eng.report
        self.assertIsNotNone(r.health_divergence)
        self.assertAlmostEqual(
            r.health_divergence, r.soh_capacity - r.predicted_soh, places=1
        )


if __name__ == "__main__":
    unittest.main()


# ═════════════════════════════════════════════════════════════════════════════
# v1.1.6 optimization pass
# ═════════════════════════════════════════════════════════════════════════════
class TestAggregationCache(unittest.TestCase):  # T15
    def test_capacity_cache_invalidated_by_new_segment(self):
        cfg = _cfg(freshness_tau_kwh=1e12)
        eng = bh.BatteryHealthEngine(cfg)
        t = _run_discharge(eng, 0.0, 95.0, 80.0, 100.0, 102.85)
        soh1, attrs1 = eng.segments.soh_capacity()
        # Cached call returns identical values
        soh1b, _ = eng.segments.soh_capacity()
        self.assertEqual(soh1, soh1b)
        # New segment must invalidate the cache and shift the estimate
        _run_discharge(eng, t + 600, 98.0, 38.0, 102.85, 115.15)
        soh2, attrs2 = eng.segments.soh_capacity()
        self.assertNotEqual(soh1, soh2)
        self.assertEqual(attrs2["segment_count"], attrs1["segment_count"] + 1)

    def test_cached_attrs_are_isolated_copies(self):
        cfg = _cfg(freshness_tau_kwh=1e12)
        eng = bh.BatteryHealthEngine(cfg)
        _run_discharge(eng, 0.0, 95.0, 80.0, 100.0, 102.85)
        _, attrs = eng.segments.soh_capacity()
        attrs["segment_count"] = 999            # caller mutates its copy
        _, attrs2 = eng.segments.soh_capacity()
        self.assertEqual(attrs2["segment_count"], 1)

    def test_segment_end_ts_set_from_closing_sample(self):
        eng = bh.BatteryHealthEngine(_cfg(freshness_tau_kwh=1e12))
        _run_discharge(eng, 0.0, 95.0, 80.0, 100.0, 102.85)
        seg = eng.segments.segments[0]
        self.assertGreater(seg.end_ts, seg.start_ts)


class TestStressRunningTotals(unittest.TestCase):  # T16
    def test_totals_match_bucket_recompute_after_feed_and_prune(self):
        cfg = _cfg(stress_window_days=2.0)
        eng = bh.BatteryHealthEngine(cfg)
        # 3 days of samples spanning the prune horizon, varying stress
        for i in range(3 * 24 * 4):                       # 15-min cadence
            t = i * 900.0
            temp = 25.0 + (5.0 if i % 7 == 0 else 0.0)
            soc = 95.0 if i % 5 == 0 else 50.0
            eng.stress.feed(_sample(t, soc=soc, temp=temp))
        eng.stress.prune(3 * DAY)
        ratio_fast = eng.stress.stress_ratio()
        num = sum(v[0] for v in eng.stress._buckets.values())
        den = sum(v[1] for v in eng.stress._buckets.values())
        self.assertAlmostEqual(ratio_fast, num / den, places=9)

    def test_totals_survive_persistence_round_trip(self):
        cfg = _cfg()
        eng = bh.BatteryHealthEngine(cfg)
        for i in range(20):
            eng.stress.feed(_sample(i * 300.0, soc=90.0, temp=30.0))
        r1 = eng.stress.stress_ratio()
        eng2 = bh.BatteryHealthEngine(cfg)
        eng2.restore(eng.to_dict())
        self.assertAlmostEqual(eng2.stress.stress_ratio(), r1, places=9)

    def test_empty_after_prune_resets_totals(self):
        cfg = _cfg(stress_window_days=1.0)
        eng = bh.BatteryHealthEngine(cfg)
        eng.stress.feed(_sample(0.0, soc=50.0, temp=25.0))
        eng.stress.feed(_sample(300.0, soc=50.0, temp=25.0))
        eng.stress.prune(10 * DAY)
        self.assertIsNone(eng.stress.stress_ratio())
        self.assertEqual(eng.stress._total_dt, 0.0)


class TestReportSignature(unittest.TestCase):  # T17
    def test_signature_stable_across_idle_ticks(self):
        # Homogeneous stress conditions (below-knee SOC, constant temp) so the
        # rolling-window mixture is constant; sub-integer stress drift is
        # additionally absorbed by the signature's integer quantization.
        cfg = _cfg(freshness_tau_kwh=1e12)
        eng = bh.BatteryHealthEngine(cfg)
        t = _run_discharge(eng, 0.0, 75.0, 60.0, 100.0, 102.85)
        r1 = eng.update(_sample(t + 60, soc=60.0, power=0.0, chg=103.0,
                                dis=102.85, temp=20.0))
        sig1 = r1.signature()
        r2 = eng.update(_sample(t + 120, soc=60.0, power=0.0, chg=103.0,
                                dis=102.85, temp=20.0))
        self.assertEqual(sig1, r2.signature())

    def test_signature_changes_on_new_segment(self):
        cfg = _cfg(freshness_tau_kwh=1e12)
        eng = bh.BatteryHealthEngine(cfg)
        t = _run_discharge(eng, 0.0, 95.0, 80.0, 100.0, 102.85)
        sig1 = eng.report.signature()
        _run_discharge(eng, t + 600, 98.0, 38.0, 102.85, 115.15)
        self.assertNotEqual(sig1, eng.report.signature())

    def test_signature_changes_on_confidence_transition(self):
        cfg = _cfg(freshness_tau_kwh=1e12, confidence_min_segments=1,
                   eff_baseline_windows=1, capacity_window_days=365.0)
        eng = bh.BatteryHealthEngine(cfg)
        t = _run_discharge(eng, 0.0, 95.0, 80.0, 100.0, 103.0)
        eng.efficiency.feed(_sample(t + 100, soc=99.0, power=0.0, chg=200.0, dis=150.0))
        eng.efficiency.feed(_sample(t + DAY, soc=99.0, power=0.0, chg=240.0, dis=188.0))
        eng.update(_sample(t + DAY + 60, soc=50.0, power=0.0, chg=240.0,
                           dis=188.0, temp=20.0))
        sig_normal = eng.report.signature()
        eng.update(_sample(t + 61 * DAY, soc=50.0, power=0.0, chg=240.0,
                           dis=188.0, temp=20.0))
        self.assertNotEqual(sig_normal, eng.report.signature())
        self.assertEqual(eng.report.confidence, "stale")
