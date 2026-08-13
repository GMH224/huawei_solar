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
import time
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
    # v2.0.6 (Tier 3, battery health architecture review): neutralizes
    # capacity temperature/rate normalization for every test using this
    # shared helper, unless a test explicitly overrides these fields
    # itself. Matches PHASE1_BATTERY_HEALTH_DESIGN.md's own documented
    # lesson from the prior attempt at this exact feature, word for
    # word: _run_discharge() (below) compresses a full discharge into 20
    # simulated minutes, implying unrealistic rates (tens of kW against
    # a 5 kW residential capacity_rate_ref_w) that a working rate-
    # normalization correctly reacts to strongly -- these pre-existing
    # tests predate normalization and are not testing it, so the fix is
    # neutralizing it here, not weakening the normalization logic
    # itself. Confirmed this was genuinely needed, not assumed from the
    # design doc alone: five pre-existing tests failed with exactly this
    # symptom (estimated_capacity_kwh roughly doubled) before this fix.
    cfg.capacity_temp_sigma_c = 1e9
    cfg.capacity_rate_ref_w = 1e9
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _sample(ts, soc=None, power=None, temp=None, chg=None, dis=None,
            packs=None, calib=False, ceiling=100.0) -> "bh.HealthSample":
    return bh.HealthSample(
        timestamp=ts, soc=soc, power_w=power, battery_temp_c=temp,
        lifetime_charge_kwh=chg, lifetime_discharge_kwh=dis,
        packs=packs or [], soh_calibration_active=calib,
        charge_ceiling_soc=ceiling,
    )


#: v1.2.0 - idle no longer ends a segment (Finding F); only charging does.
CLOSE_POWER = 1500.0


def _run_discharge(engine, t0, soc0, soc1, dis0, dis1, steps=20, power=-2500.0,
                   chg=1000.0, temp=20.0, calib=False):
    """Drive the engine through a clean discharge segment then close it."""
    for i in range(steps + 1):
        frac = i / steps
        engine.update(_sample(
            t0 + i * 60, soc=soc0 + (soc1 - soc0) * frac, power=power,
            temp=temp, chg=chg, dis=dis0 + (dis1 - dis0) * frac, calib=calib,
        ))
    # Close with a CHARGING tick (v1.2.0: idle no longer closes a segment)
    engine.update(_sample(t0 + (steps + 1) * 60, soc=soc1, power=CLOSE_POWER,
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
        eng.update(_sample(t + 5 * 60, soc=50.0, power=1500.0, chg=0.0, dis=2.0))
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

    def test_calibration_overlap_excludes_the_segment(self):
        """v2.0.6 (Tier 1, battery health architecture review): replaces
        the old test_golden_segment_weight_boost, which checked the
        OPPOSITE of what's now correct. Calibration overlap must fully
        EXCLUDE a segment (weight 0.0), not boost it -- see
        DischargeSegment.exclude_calibration's own comment for why."""
        cfg = _cfg(freshness_tau_kwh=1e12)
        eng = bh.BatteryHealthEngine(cfg)
        _run_discharge(eng, 0.0, 95.0, 75.0, 0.0, 4.0, calib=True)
        seg = eng.segments.segments[0]
        self.assertTrue(seg.exclude_calibration)
        self.assertEqual(seg.weight(cfg), 0.0)

    def test_segment_with_no_calibration_overlap_is_not_excluded(self):
        """Negative case: a completely ordinary segment must not be
        excluded -- confirms the fix didn't make exclusion the default."""
        cfg = _cfg(freshness_tau_kwh=1e12)
        eng = bh.BatteryHealthEngine(cfg)
        _run_discharge(eng, 0.0, 95.0, 75.0, 0.0, 4.0, calib=False)
        seg = eng.segments.segments[0]
        self.assertFalse(seg.exclude_calibration)
        self.assertGreater(seg.weight(cfg), 0.0)

    def test_segment_starting_within_settle_window_after_completion_is_excluded(self):
        """The edge-detection half of Tier 1, not covered by the old
        test at all: a segment starting shortly after calibration
        COMPLETES (not while still active) must also be excluded -- the
        raw register can't distinguish "calibrating" from "just
        finished" from one reading, so the settle window covers this
        ambiguity."""
        cfg = _cfg(freshness_tau_kwh=1e12, calibration_settle_s=300.0)
        eng = bh.BatteryHealthEngine(cfg)
        # Calibration active, then completes (nonzero -> zero edge).
        eng.update(_sample(0, soc=50.0, power=0.0, chg=0.0, dis=0.0, calib=True))
        eng.update(_sample(60, soc=50.0, power=0.0, chg=0.0, dis=0.0, calib=False))
        # A segment starting 100s later -- well within the 300s settle window.
        _run_discharge(eng, 160.0, 95.0, 75.0, 0.0, 4.0, calib=False)
        seg = eng.segments.segments[0]
        self.assertTrue(
            seg.exclude_calibration,
            "a segment starting within the settle window after a "
            "detected calibration completion must be excluded",
        )

    def test_segment_starting_well_after_settle_window_is_not_excluded(self):
        """Negative case: the exclusion must not linger forever -- once
        the settle window has genuinely passed, a new segment is
        ordinary again."""
        cfg = _cfg(freshness_tau_kwh=1e12, calibration_settle_s=300.0)
        eng = bh.BatteryHealthEngine(cfg)
        eng.update(_sample(0, soc=50.0, power=0.0, chg=0.0, dis=0.0, calib=True))
        eng.update(_sample(60, soc=50.0, power=0.0, chg=0.0, dis=0.0, calib=False))
        # A segment starting 1000s later -- well past the 300s settle window.
        _run_discharge(eng, 1060.0, 95.0, 75.0, 4.0, 8.0, calib=False)
        seg = eng.segments.segments[0]
        self.assertFalse(seg.exclude_calibration)


class TestGapHandling(unittest.TestCase):  # T6 (contract corrected in v1.1.8)
    """Gap handling.

    NOTE ON THE v1.1.8 CHANGE: two tests here previously asserted that ANY
    data gap discards the in-progress segment. That encoded a design error,
    not a requirement — SOC is an absolute reading and lifetime discharge is a
    cumulative counter, so ΔSOC and Δenergy across a gap are still exact, and
    the implied-capacity band already rejects intervals where something
    unobserved happened. The old rule made capacity measurement structurally
    impossible on a link with intermittent Modbus timeouts (observed in the
    field: 11 segments started, 11 discarded, 0 completed).

    They are replaced below with the corrected contract: short gaps are
    bridged, over-limit gaps are still discarded.
    """

    def test_short_gap_mid_segment_is_bridged(self):
        eng = bh.BatteryHealthEngine(_cfg(freshness_tau_kwh=1e12))
        for i, soc in enumerate([90, 85, 80]):
            eng.update(_sample(i * 60, soc=float(soc), power=-3000.0,
                               chg=0.0, dis=float(i)))
        eng.mark_gap()                       # coordinator failure
        # Resume 5 min later, still discharging, then close normally.
        eng.update(_sample(480, soc=72.0, power=-3000.0, chg=0.0, dis=3.7))
        eng.update(_sample(540, soc=72.0, power=1500.0, chg=0.0, dis=3.7))
        self.assertEqual(len(eng.segments.segments), 1)
        seg = eng.segments.segments[0]
        self.assertEqual(seg.soc_start, 90.0)
        self.assertEqual(seg.soc_end, 72.0)
        self.assertEqual(seg.gap_bridged, 1)
        self.assertEqual(eng.segments.gap_bridged_count, 1)

    def test_missing_field_mid_segment_is_bridged(self):
        """A register missing from coordinator data mid-segment.

        Note: the lifetime counters are NOT a valid probe here — CounterMonitor
        deliberately carries the last value forward on a failed read so EFC and
        warranty stay populated, so the tracker never sees None for them (only
        the segment's start/end endpoints matter, and those are real readings).
        SOC has no such carry-forward, so it is the genuine None path.
        """
        eng = bh.BatteryHealthEngine(_cfg(freshness_tau_kwh=1e12))
        for i, soc in enumerate([90, 85, 80]):
            eng.update(_sample(i * 60, soc=float(soc), power=-3000.0,
                               chg=0.0, dis=float(i)))
        eng.update(_sample(300, soc=None, power=-3000.0, chg=0.0, dis=2.5))
        eng.update(_sample(360, soc=72.0, power=-3000.0, chg=0.0, dis=3.7))
        eng.update(_sample(420, soc=72.0, power=1500.0, chg=0.0, dis=3.7))
        self.assertEqual(len(eng.segments.segments), 1)
        self.assertEqual(eng.segments.segments[0].gap_bridged, 1)

    def test_counter_carry_forward_does_not_corrupt_segment_energy(self):
        """Stale carried-forward counter values mid-segment are harmless.

        Only the segment endpoints enter the capacity arithmetic, so a flat
        counter during an outage cannot distort the result as long as the
        closing sample is a real reading.
        """
        cfg = _cfg(freshness_tau_kwh=1e12)
        eng = bh.BatteryHealthEngine(cfg)
        eng.update(_sample(0, soc=100.0, power=-2000.0, chg=0.0, dis=0.0))
        # Mid-segment reads fail: counter carried forward (flat) while SOC drops
        for i, soc in enumerate([90.0, 85.0, 80.0], start=1):
            eng.update(_sample(i * 60, soc=soc, power=-2000.0, chg=0.0, dis=None))
        # Real closing reading
        eng.update(_sample(300, soc=75.0, power=-2000.0, chg=0.0, dis=5.175))
        eng.update(_sample(360, soc=75.0, power=1500.0, chg=0.0, dis=5.175))
        self.assertEqual(len(eng.segments.segments), 1)
        seg = eng.segments.segments[0]
        self.assertAlmostEqual(seg.implied_capacity_kwh, 20.7, delta=0.1)

    def test_gap_beyond_bridge_limit_still_discards(self):
        cfg = _cfg(max_gap_bridge_s=3600.0)
        eng = bh.BatteryHealthEngine(cfg)
        for i, soc in enumerate([90, 85, 80]):
            eng.update(_sample(i * 60, soc=float(soc), power=-3000.0,
                               chg=0.0, dis=float(i)))
        eng.mark_gap()
        # Resume 3 hours later — beyond the trust horizon.
        eng.update(_sample(3 * 3600, soc=60.0, power=1500.0, chg=0.0, dis=8.0))
        self.assertEqual(len(eng.segments.segments), 0)
        self.assertGreaterEqual(eng.segments.discarded_segments, 1)
        self.assertEqual(eng.segments.gap_bridged_count, 0)

    def test_over_limit_gap_starts_a_fresh_segment_if_still_discharging(self):
        cfg = _cfg(max_gap_bridge_s=3600.0, freshness_tau_kwh=1e12)
        eng = bh.BatteryHealthEngine(cfg)
        eng.update(_sample(0, soc=90.0, power=-3000.0, chg=0.0, dis=0.0))
        eng.mark_gap()
        eng.update(_sample(3 * 3600, soc=60.0, power=-3000.0, chg=0.0, dis=8.0))
        # Old segment gone, but a new one must be open from this sample.
        eng.update(_sample(3 * 3600 + 3600, soc=45.0, power=-3000.0,
                           chg=0.0, dis=11.1))
        eng.update(_sample(3 * 3600 + 3660, soc=45.0, power=1500.0,
                           chg=0.0, dis=11.1))
        self.assertEqual(len(eng.segments.segments), 1)
        seg = eng.segments.segments[0]
        self.assertEqual(seg.soc_start, 60.0)
        self.assertEqual(seg.soc_end, 45.0)

    def test_counter_reset_still_discards_and_is_not_bridged(self):
        """A reset is NOT a gap: unknown energy may have flowed."""
        eng = bh.BatteryHealthEngine(_cfg())
        for i, soc in enumerate([90, 85, 80]):
            eng.update(_sample(i * 60, soc=float(soc), power=-3000.0,
                               chg=50.0, dis=100.0 + i))
        eng.update(_sample(300, soc=75.0, power=-3000.0, chg=50.0, dis=1.0))
        eng.update(_sample(360, soc=70.0, power=1500.0, chg=50.0, dis=2.0))
        self.assertEqual(len(eng.segments.segments), 0)
        self.assertEqual(eng.segments.gap_bridged_count, 0)

    def test_field_report_scenario_timeouts_no_longer_prevent_measurement(self):
        """Regression test reproducing the reported failure.

        Slow overnight discharge (~3 SOC points/hour) on a link timing out
        roughly every 25 minutes. Under the v1.1.7 rule this could never
        produce a segment; it must now complete one.
        """
        cfg = _cfg(freshness_tau_kwh=1e12)
        eng = bh.BatteryHealthEngine(cfg)
        soc, dis, t = 100.0, 0.0, 0.0
        tick = 30.0
        # 8 hours of discharge at ~3 SOC points/hour => ~24 points total
        for i in range(int(8 * 3600 / tick)):
            t = i * tick
            soc = 100.0 - (t / 3600.0) * 3.0
            dis = (100.0 - soc) / 100.0 * 20.7
            if i % 50 == 0 and i > 0:          # a timeout every 25 min
                eng.mark_gap()
                eng.update(_sample(t, soc=soc, power=-700.0, chg=0.0, dis=None))
                continue
            eng.update(_sample(t, soc=soc, power=-700.0, chg=0.0, dis=dis))
        eng.update(_sample(t + tick, soc=soc, power=1500.0, chg=0.0, dis=dis))

        self.assertEqual(len(eng.segments.segments), 1,
                         "a qualifying segment must survive intermittent timeouts")
        seg = eng.segments.segments[0]
        self.assertGreater(seg.delta_soc, 20.0)
        self.assertGreater(seg.gap_bridged, 10)
        # And the capacity estimate must be accurate despite the gaps.
        soh, attrs = eng.segments.soh_capacity()
        self.assertAlmostEqual(attrs["estimated_capacity_kwh"], 20.7, delta=0.3)
        self.assertAlmostEqual(soh, 100.0, delta=1.5)


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
        eng.update(_sample(360, soc=55.0, power=1500.0, chg=50.0, dis=2.0))
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


class TestBalance(unittest.TestCase):  # T9 (contract replaced in v1.2.0)
    """Pack balance, now scored against a learned baseline.

    NOTE ON THE v1.2.0 CHANGE: the previous tests asserted absolute dV/dT
    thresholds (e.g. dT=2.7 C -> 87.9). Field data showed that design was
    unusable: a fixed 2.4 C inter-pack offset - present at idle (2.33 C) just
    as much as under >1 kW charge (2.52 C), so demonstrably not
    battery-generated heat - scored a healthy pack set at ~81/100, and the
    0.1 V voltage register resolution made one LSB worth 11 score points.
    Those tests encoded the flawed design; they are replaced, not weakened.
    """

    def _packs(self, volts, temps, online=(True, True, True)):
        return [bh.PackSample(voltage=v, temp_max=t, temp_min=t - 1.0, online=o)
                for v, t, o in zip(volts, temps, online)]

    def _feed(self, eng, n, volts, temps, soc=98.0, ceiling=100.0, t0=0.0):
        for i in range(n):
            eng.balance.feed(_sample(t0 + i * 60, soc=soc, power=0.0,
                                     packs=self._packs(volts, temps),
                                     ceiling=ceiling))

    def test_no_score_until_baseline_captured(self):
        cfg = _cfg(balance_baseline_min_samples=20)
        eng = bh.BatteryHealthEngine(cfg)
        self._feed(eng, 19, [26.4, 26.4, 26.5], [26.0, 28.4, 27.2])
        soh, attrs = eng.balance.soh_balance()
        self.assertIsNone(soh)
        self.assertIsNone(attrs["balance_baseline_dv"])

    def test_fixed_offset_cancels_after_baseline(self):
        """THE key property: a constant offset must score ~100, not ~81."""
        cfg = _cfg(balance_baseline_min_samples=20)
        eng = bh.BatteryHealthEngine(cfg)
        # 2.4 C fixed spread + 0.1 V quantisation step, exactly as measured.
        self._feed(eng, 25, [26.4, 26.4, 26.5], [26.0, 28.4, 27.2])
        soh, attrs = eng.balance.soh_balance()
        self.assertIsNotNone(soh)
        self.assertGreaterEqual(soh, 99.0)
        self.assertAlmostEqual(attrs["balance_baseline_dt"], 2.4, places=2)
        self.assertAlmostEqual(attrs["balance_baseline_dv"], 0.1, places=3)

    def test_drift_away_from_baseline_lowers_score(self):
        cfg = _cfg(balance_baseline_min_samples=20)
        eng = bh.BatteryHealthEngine(cfg)
        self._feed(eng, 25, [26.4, 26.4, 26.5], [26.0, 28.4, 27.2])
        base, _ = eng.balance.soh_balance()
        # One pack now runs 4 C hotter than its established norm.
        self._feed(eng, 25, [26.4, 26.4, 26.5], [26.0, 32.4, 27.2], t0=10_000.0)
        drifted, _ = eng.balance.soh_balance()
        self.assertLess(drifted, base - 20.0)

    def test_raw_values_always_exposed(self):
        """Ground truth survives any recalibration."""
        cfg = _cfg(balance_baseline_min_samples=5)
        eng = bh.BatteryHealthEngine(cfg)
        self._feed(eng, 10, [26.4, 26.4, 26.5], [26.0, 28.4, 27.2])
        _, attrs = eng.balance.soh_balance()
        self.assertAlmostEqual(attrs["balance_raw_dt"], 2.4, places=2)
        eng.reset_balance_baseline()
        _, attrs2 = eng.balance.soh_balance()
        self.assertAlmostEqual(attrs2["balance_raw_dt"], 2.4, places=2)
        self.assertIsNone(attrs2["balance_baseline_dt"])
        self.assertGreaterEqual(attrs2["balance_baseline_epochs"], 1)

    def test_adaptive_gate_samples_below_95_when_ceiling_is_lower(self):
        """Field: a 93% configured cap meant SOC>=95 was unreachable for 78 days."""
        cfg = _cfg(balance_baseline_min_samples=5)
        eng = bh.BatteryHealthEngine(cfg)
        self._feed(eng, 10, [26.4, 26.4, 26.5], [26.0, 28.4, 27.2],
                   soc=92.5, ceiling=93.0)
        self.assertIsNotNone(eng.balance.baseline_dt)

    def test_hard_floor_rejects_mid_range_samples(self):
        """LFP's flat mid-range OCV makes dV uninformative low down."""
        cfg = _cfg(balance_baseline_min_samples=5)
        eng = bh.BatteryHealthEngine(cfg)
        self._feed(eng, 10, [26.4, 26.4, 26.5], [26.0, 28.4, 27.2],
                   soc=45.0, ceiling=50.0)
        self.assertIsNone(eng.balance.baseline_dt)

    def test_ceiling_change_starts_new_epoch(self):
        cfg = _cfg(balance_baseline_min_samples=5)
        eng = bh.BatteryHealthEngine(cfg)
        self._feed(eng, 10, [26.4, 26.4, 26.5], [26.0, 28.4, 27.2], ceiling=95.0)
        self.assertIsNotNone(eng.balance.baseline_dt)
        self._feed(eng, 1, [26.4, 26.4, 26.5], [26.0, 28.4, 27.2],
                   ceiling=100.0, t0=50_000.0)
        self.assertIsNone(eng.balance.baseline_dt)
        self.assertGreaterEqual(len(eng.balance.baseline_epochs), 2)

    def test_offline_pack_excluded(self):
        cfg = _cfg(balance_baseline_min_samples=5)
        eng = bh.BatteryHealthEngine(cfg)
        for i in range(10):
            eng.balance.feed(_sample(i * 60, soc=98.0, power=0.0,
                                     packs=self._packs([26.4, 0.0, 26.5],
                                                       [26.0, 0.0, 26.5],
                                                       online=(True, False, True))))
        _, attrs = eng.balance.soh_balance()
        self.assertEqual(attrs["packs_included"], [1, 3])
        self.assertEqual(attrs["packs_excluded"], [2])

    def test_fewer_than_two_online_packs_no_sample(self):
        eng = bh.BatteryHealthEngine(_cfg())
        eng.balance.feed(_sample(0, soc=98.0, power=0.0,
                                 packs=self._packs([26.4, 0.0, 0.0],
                                                   [26.0, 0.0, 0.0],
                                                   online=(True, False, False))))
        self.assertIsNone(eng.balance.soh_balance()[0])

    def test_gating_rejects_loaded_or_low_soc(self):
        eng = bh.BatteryHealthEngine(_cfg())
        packs = self._packs([26.4, 26.4, 26.4], [26.0, 26.0, 26.0])
        eng.balance.feed(_sample(0, soc=98.0, power=3000.0, packs=packs))
        eng.balance.feed(_sample(0, soc=20.0, power=0.0, packs=packs))
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
        # balance: baseline-relative, so feed enough samples to establish it
        # and then score at the same (stable) spread -> ~100
        for i in range(cfg.balance_baseline_min_samples + 5):
            eng.balance.feed(_sample(t + DAY + i, soc=99.0, power=0.0, packs=[
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


# ═════════════════════════════════════════════════════════════════════════════
# v1.1.8 — gap bridging for the efficiency tracker
# ═════════════════════════════════════════════════════════════════════════════
class TestEfficiencyGapTolerance(unittest.TestCase):  # T20
    """Round-trip efficiency must survive intermittent read failures.

    Field symptom under v1.1.7: efficiency_window_count stuck at 0 forever,
    because every coordinator failure invalidated the open anchor.
    """

    def _anchor(self, eng, ts, chg, dis):
        eng.efficiency.feed(_sample(ts, soc=99.0, power=0.0, chg=chg, dis=dis))

    def test_gap_does_not_invalidate_open_anchor(self):
        cfg = _cfg(eff_baseline_windows=1, eff_rolling_windows=1)
        eng = bh.BatteryHealthEngine(cfg)
        self._anchor(eng, 0.0, 0.0, 0.0)
        eng.mark_gap()                      # coordinator failure
        eng.mark_gap()                      # and another
        self._anchor(eng, DAY, 40.0, 38.4)  # η = 0.96
        self.assertEqual(len(eng.efficiency.windows), 1)
        self.assertAlmostEqual(eng.efficiency.baseline, 0.96, places=3)

    def test_counter_reset_restarts_the_anchor(self):
        """A reset drops the open window; measurement restarts cleanly.

        The anchor is re-established from the post-reset state in the same
        tick, so no window ever spans the reset, but measurement resumes
        immediately rather than stalling.
        """
        cfg = _cfg(eff_baseline_windows=1)
        eng = bh.BatteryHealthEngine(cfg)
        eng.update(_sample(0.0, soc=99.0, power=0.0, chg=100.0, dis=95.0,
                           temp=20.0))
        self.assertEqual(eng.efficiency._anchor[0], 0.0)

        eng.update(_sample(60.0, soc=99.0, power=0.0, chg=1.0, dis=1.0,
                           temp=20.0))
        # No window may be recorded across the reset ...
        self.assertEqual(len(eng.efficiency.windows), 0)
        # ... and (v1.2.1) a reset is treated as a recovery, so learning is
        # suspended for the settling period rather than re-anchoring
        # immediately on data that may still be stale.
        self.assertFalse(eng.learning_active(60.0))
        self.assertIsNone(eng.efficiency._anchor)

        # After settling, measurement resumes and re-anchors normally.
        eng.update(_sample(60.0 + 400.0, soc=99.0, power=0.0, chg=1.0, dis=1.0,
                           temp=20.0))
        self.assertTrue(eng.learning_active(60.0 + 400.0))
        self.assertIsNotNone(eng.efficiency._anchor)

        eng.update(_sample(DAY, soc=99.0, power=0.0, chg=41.0, dis=39.4,
                           temp=20.0))
        self.assertEqual(len(eng.efficiency.windows), 1)
        self.assertAlmostEqual(eng.efficiency.windows[0], 0.96, places=3)

    def test_baseline_captured_despite_frequent_gaps(self):
        """End-to-end: baseline must form on a flaky link."""
        cfg = _cfg(eff_baseline_windows=3, eff_rolling_windows=3)
        eng = bh.BatteryHealthEngine(cfg)
        chg = dis = 0.0
        self._anchor(eng, 0.0, chg, dis)
        for i in range(1, 6):
            eng.mark_gap()                  # a failure between every anchor
            chg += 40.0
            dis += 40.0 * 0.96
            self._anchor(eng, i * DAY, chg, dis)
        self.assertIsNotNone(eng.efficiency.baseline)
        soh, _ = eng.efficiency.soh_efficiency()
        self.assertAlmostEqual(soh, 100.0, delta=0.5)


class TestGapBridgingDiagnostics(unittest.TestCase):  # T20
    def test_gap_bridged_count_exposed_in_attributes(self):
        cfg = _cfg(freshness_tau_kwh=1e12)
        eng = bh.BatteryHealthEngine(cfg)
        eng.update(_sample(0, soc=95.0, power=-3000.0, chg=0.0, dis=0.0))
        eng.mark_gap()
        eng.update(_sample(300, soc=80.0, power=-3000.0, chg=0.0, dis=3.1))
        eng.update(_sample(360, soc=80.0, power=1500.0, chg=0.0, dis=3.1))
        _, attrs = eng.segments.soh_capacity()
        self.assertIn("gap_bridged_count", attrs)
        self.assertEqual(attrs["gap_bridged_count"], 1)

    def test_gap_counter_survives_persistence(self):
        cfg = _cfg(freshness_tau_kwh=1e12)
        eng = bh.BatteryHealthEngine(cfg)
        eng.update(_sample(0, soc=95.0, power=-3000.0, chg=0.0, dis=0.0))
        eng.mark_gap()
        eng.update(_sample(300, soc=80.0, power=-3000.0, chg=0.0, dis=3.1))
        eng.update(_sample(360, soc=80.0, power=1500.0, chg=0.0, dis=3.1))
        data = eng.to_dict()
        import json
        json.dumps(data)
        eng2 = bh.BatteryHealthEngine(cfg)
        eng2.restore(data)
        self.assertEqual(eng2.segments.gap_bridged_count, 1)
        self.assertEqual(eng2.segments.segments[0].gap_bridged, 1)

    def test_pre_1_1_8_persisted_segments_still_load(self):
        """Backward compatibility: segments saved before the field existed."""
        cfg = _cfg()
        eng = bh.BatteryHealthEngine(cfg)
        legacy = {
            "schema_version": bh.SCHEMA_VERSION,
            "first_seen_ts": 0.0,
            "segments": {
                "segments": [{
                    "start_ts": 0.0, "end_ts": 100.0, "soc_start": 95.0,
                    "soc_end": 75.0, "energy_kwh": 4.14,
                    "implied_capacity_kwh": 20.7, "freshness": 1.0,
                    "golden": False,
                }],
                "throughput_since_full": 0.0, "last_discharge": 4.14,
                "last_segment_ts": 0.0, "discarded": 3,
            },
            "efficiency": {}, "balance": {}, "stress": {},
            "charge_counter": {}, "discharge_counter": {},
        }
        eng.restore(legacy)
        self.assertEqual(len(eng.segments.segments), 1)
        self.assertEqual(eng.segments.segments[0].gap_bridged, 0)
        self.assertEqual(eng.segments.discarded_segments, 3)


# ═════════════════════════════════════════════════════════════════════════════
# v1.2.0 — findings H, C, D, N, L/O
# ═════════════════════════════════════════════════════════════════════════════
class TestCapacityReference(unittest.TestCase):  # T21 / Finding H
    """SOH_cap must be anchored to MEASURED capacity, not the nameplate.

    Field evidence: a pack rated 20.7 kWh measured a consistent ~22.8 kWh
    across 162 segments over 6 months (spread 0.31). Anchoring to the
    nameplate pinned SOH_cap at the 100% clip, so the first ~10% of any real
    degradation would have been invisible.
    """

    def _fill(self, eng, n, cap_kwh=22.8, t0=0.0, spacing=None):
        """Run n daily-ish segments. Spacing matters: the reference capture
        requires the segments to SPAN time, not merely to exist."""
        t = t0
        step = spacing if spacing is not None else DAY
        for i in range(n):
            energy = cap_kwh * 0.23
            t = _run_discharge(eng, t0 + i * step, 100.0, 77.0,
                               i * energy, (i + 1) * energy)
        return t0 + n * step

    def test_reference_auto_captured_from_measurement(self):
        cfg = _cfg(freshness_tau_kwh=1e12, capacity_reference_min_segments=10,
                   capacity_reference_min_span_days=5.0)
        eng = bh.BatteryHealthEngine(cfg)
        self._fill(eng, 12)
        soh, attrs = eng.segments.soh_capacity()
        self.assertTrue(attrs["capacity_reference_is_measured"])
        self.assertAlmostEqual(attrs["capacity_reference_kwh"], 22.8, delta=0.3)
        self.assertAlmostEqual(soh, 100.0, delta=1.0)

    def test_nameplate_mismatch_no_longer_hides_degradation(self):
        """With a 20.7 nameplate and 22.8 real capacity, a 5% fade must show."""
        cfg = _cfg(freshness_tau_kwh=1e12, capacity_reference_min_segments=10,
                   capacity_reference_min_span_days=5.0)
        eng = bh.BatteryHealthEngine(cfg)
        self._fill(eng, 12, cap_kwh=22.8)
        healthy, _ = eng.segments.soh_capacity()
        # Now the same battery delivers 5% less per SOC point.
        eng2 = bh.BatteryHealthEngine(cfg)
        eng2.segments.set_reference(22.8, reason="test")
        self._fill(eng2, 12, cap_kwh=22.8 * 0.95)
        faded, _ = eng2.segments.soh_capacity()
        self.assertAlmostEqual(healthy, 100.0, delta=1.0)
        self.assertAlmostEqual(faded, 95.0, delta=1.5)
        self.assertLess(faded, 97.0, "a 5% fade must be visible, not clipped")

    def test_clip_allows_headroom_above_100(self):
        cfg = _cfg(freshness_tau_kwh=1e12, capacity_reference_min_segments=100)
        eng = bh.BatteryHealthEngine(cfg)
        eng.segments.set_reference(20.7, reason="nameplate")
        self._fill(eng, 3, cap_kwh=22.8)
        soh, _ = eng.segments.soh_capacity()
        self.assertGreater(soh, 100.0)
        self.assertLessEqual(soh, cfg.soh_capacity_clip_max)

    def test_reanchor_appends_epoch_and_refuses_on_thin_data(self):
        cfg = _cfg(freshness_tau_kwh=1e12, capacity_reference_min_segments=10,
                   capacity_reference_min_span_days=5.0)
        eng = bh.BatteryHealthEngine(cfg)
        self._fill(eng, 3)
        self.assertFalse(eng.reanchor_capacity_reference())   # too few segments
        # Enough segments, but crammed into a single day -> still refused,
        # because a reference must average out seasonal operating-range
        # effects (Finding J).
        eng2 = bh.BatteryHealthEngine(cfg)
        eng2.cfg.capacity_window_days = 400.0
        self._fill(eng2, 12, spacing=600.0)
        self.assertFalse(eng2.reanchor_capacity_reference())
        self._fill(eng, 12, t0=10 * DAY)
        self.assertTrue(eng.reanchor_capacity_reference())
        self.assertGreaterEqual(len(eng.segments.reference_epochs), 1)
        self.assertIsNotNone(eng.segments.reference_epochs[-1]["reason"])

    def test_reference_survives_persistence(self):
        cfg = _cfg(freshness_tau_kwh=1e12, capacity_reference_min_segments=10)
        eng = bh.BatteryHealthEngine(cfg)
        self._fill(eng, 12)
        import json
        data = json.loads(json.dumps(eng.to_dict()))
        eng2 = bh.BatteryHealthEngine(cfg)
        eng2.restore(data)
        self.assertAlmostEqual(eng2.segments.reference_capacity_kwh,
                               eng.segments.reference_capacity_kwh, places=6)

    def test_segment_records_soc_midpoint_and_ceiling(self):
        """Finding J: the operating band must be recorded alongside capacity."""
        cfg = _cfg(freshness_tau_kwh=1e12)
        eng = bh.BatteryHealthEngine(cfg)
        _run_discharge(eng, 0.0, 100.0, 77.0, 0.0, 5.24)
        seg = eng.segments.segments[0]
        self.assertAlmostEqual(seg.soc_midpoint, 88.5, places=1)
        self.assertEqual(seg.charge_ceiling, 100.0)
        _, attrs = eng.segments.soh_capacity()
        self.assertAlmostEqual(attrs["segment_soc_midpoint_mean"], 88.5, places=1)


class TestStaleCounterEndpoints(unittest.TestCase):  # T22 / Finding C
    def test_segment_does_not_open_on_carried_forward_counter(self):
        eng = bh.BatteryHealthEngine(_cfg())
        # Establish counters, then a failed read carries the value forward.
        eng.update(_sample(0, soc=95.0, power=0.0, chg=10.0, dis=10.0, temp=20.0))
        eng.update(_sample(60, soc=94.0, power=-3000.0, chg=None, dis=None,
                           temp=20.0))
        self.assertEqual(len(eng.segments.segments), 0)
        self.assertGreaterEqual(eng.segments.stale_endpoint_skips, 1)

    def test_segment_opens_normally_on_fresh_counter(self):
        eng = bh.BatteryHealthEngine(_cfg())
        eng.update(_sample(0, soc=95.0, power=0.0, chg=10.0, dis=10.0, temp=20.0))
        eng.update(_sample(60, soc=94.0, power=-3000.0, chg=10.0, dis=10.1,
                           temp=20.0))
        self.assertTrue(eng.segments._active)


class TestInstallDate(unittest.TestCase):  # T23 / Finding D
    def test_install_date_drives_forecast_age(self):
        year = 365.25 * DAY
        cfg = _cfg(battery_install_ts=0.0)
        eng = bh.BatteryHealthEngine(cfg)
        # Integration only starts observing one year after installation.
        eng.update(_sample(year, soc=50.0, power=0.0, chg=0.0, dis=0.0, temp=25.0))
        r = eng.report
        self.assertEqual(r.attributes["battery_age_source"], "install_date")
        self.assertAlmostEqual(r.attributes["battery_age_days"], 365, delta=2)
        self.assertAlmostEqual(r.predicted_soh, 100.0 - 2.5, delta=0.4)

    def test_falls_back_to_first_seen(self):
        eng = bh.BatteryHealthEngine(_cfg())
        eng.update(_sample(0.0, soc=50.0, power=0.0, chg=0.0, dis=0.0, temp=25.0))
        self.assertEqual(eng.report.attributes["battery_age_source"], "first_seen")


class TestSubscoreHold(unittest.TestCase):  # T24 / Finding N
    """Seasonal term availability must not step the composite."""

    def test_balance_held_when_seasonally_unavailable(self):
        cfg = _cfg(freshness_tau_kwh=1e12, balance_baseline_min_samples=5,
                   subscore_hold_days=90.0)
        eng = bh.BatteryHealthEngine(cfg)
        t = _run_discharge(eng, 0.0, 100.0, 77.0, 0.0, 5.24)
        packs = [bh.PackSample(voltage=26.4, temp_max=25.0, temp_min=24.0, online=True)
                 for _ in range(3)]
        for i in range(10):
            eng.balance.feed(_sample(t + i, soc=99.0, power=0.0, packs=packs))
        eng.update(_sample(t + 100, soc=80.0, power=0.0, chg=6.0, dis=5.24, temp=20.0))
        with_bal = eng.report
        self.assertIn("balance", with_bal.attributes["contributing_terms"])

        # Winter: no more balance samples for 30 days. Term must be HELD.
        eng.balance.scores.clear()
        eng.balance._median_cache = None
        eng.update(_sample(t + 30 * DAY, soc=60.0, power=0.0, chg=6.0,
                           dis=5.24, temp=20.0))
        held = eng.report
        self.assertIn("balance", held.attributes["contributing_terms"])
        self.assertIn("balance", held.attributes["held_terms"])
        self.assertAlmostEqual(held.bhi, with_bal.bhi, delta=0.2)

    def test_hold_expires_after_configured_window(self):
        cfg = _cfg(freshness_tau_kwh=1e12, balance_baseline_min_samples=5,
                   subscore_hold_days=10.0)
        eng = bh.BatteryHealthEngine(cfg)
        t = _run_discharge(eng, 0.0, 100.0, 77.0, 0.0, 5.24)
        packs = [bh.PackSample(voltage=26.4, temp_max=25.0, temp_min=24.0, online=True)
                 for _ in range(3)]
        for i in range(10):
            eng.balance.feed(_sample(t + i, soc=99.0, power=0.0, packs=packs))
        eng.update(_sample(t + 100, soc=80.0, power=0.0, chg=6.0, dis=5.24, temp=20.0))
        eng.balance.scores.clear()
        eng.balance._median_cache = None
        eng.update(_sample(t + 40 * DAY, soc=60.0, power=0.0, chg=6.0,
                           dis=5.24, temp=20.0))
        self.assertNotIn("balance", eng.report.attributes["contributing_terms"])


class TestEfficiencyAnchorTiers(unittest.TestCase):  # T25 / Findings L, O
    """Anchors must sit at EQUAL stored energy, with a winter fallback."""

    def _anchor(self, eng, ts, chg, dis, soc=100.0, ceiling=100.0):
        eng.efficiency.feed(_sample(ts, soc=soc, power=0.0, chg=chg, dis=dis,
                                    ceiling=ceiling))

    def test_tier1_anchors_at_recalibration_point(self):
        cfg = _cfg(eff_baseline_windows=1)
        eng = bh.BatteryHealthEngine(cfg)
        self._anchor(eng, 0.0, 0.0, 0.0)
        self._anchor(eng, DAY, 20.0, 19.8)
        self.assertEqual(len(eng.efficiency.windows), 1)
        self.assertEqual(eng.efficiency.window_tiers[-1], 1)

    def test_tier2_requires_matched_soc(self):
        """A 93% ceiling still yields anchors, but only matched pairs."""
        cfg = _cfg(eff_baseline_windows=1)
        eng = bh.BatteryHealthEngine(cfg)
        self._anchor(eng, 0.0, 0.0, 0.0, soc=93.0, ceiling=93.0)
        # Mismatched partner (91 vs 93) must NOT produce a window.
        self._anchor(eng, DAY, 20.0, 19.8, soc=91.0, ceiling=93.0)
        self.assertEqual(len(eng.efficiency.windows), 0)

    def test_tier2_matched_pair_produces_window(self):
        cfg = _cfg(eff_baseline_windows=1)
        eng = bh.BatteryHealthEngine(cfg)
        self._anchor(eng, 0.0, 0.0, 0.0, soc=93.0, ceiling=93.0)
        self._anchor(eng, DAY, 20.0, 19.8, soc=93.0, ceiling=93.0)
        self.assertEqual(len(eng.efficiency.windows), 1)
        self.assertEqual(eng.efficiency.window_tiers[-1], 2)

    def test_no_anchor_below_min_ceiling(self):
        cfg = _cfg(eff_baseline_windows=1)
        eng = bh.BatteryHealthEngine(cfg)
        self._anchor(eng, 0.0, 0.0, 0.0, soc=45.0, ceiling=50.0)
        self._anchor(eng, DAY, 20.0, 19.8, soc=45.0, ceiling=50.0)
        self.assertEqual(len(eng.efficiency.windows), 0)

    def test_tier2_window_is_time_capped(self):
        cfg = _cfg(eff_baseline_windows=1, eff_tier2_max_window_days=5.0)
        eng = bh.BatteryHealthEngine(cfg)
        self._anchor(eng, 0.0, 0.0, 0.0, soc=93.0, ceiling=93.0)
        self._anchor(eng, 10 * DAY, 20.0, 19.8, soc=93.0, ceiling=93.0)
        self.assertEqual(len(eng.efficiency.windows), 0)

    def test_ceiling_change_starts_new_epoch(self):
        """Field: eta 0.9801 at a 93% cap vs 0.9883 at 100% = 6.5 SOH points."""
        cfg = _cfg(eff_baseline_windows=1)
        eng = bh.BatteryHealthEngine(cfg)
        self._anchor(eng, 0.0, 0.0, 0.0, soc=93.0, ceiling=93.0)
        self._anchor(eng, DAY, 20.0, 19.6, soc=93.0, ceiling=93.0)
        self.assertIsNotNone(eng.efficiency.baseline)
        # User raises the cap to 100%: the old baseline must not carry over.
        self._anchor(eng, 2 * DAY, 21.0, 20.6, soc=100.0, ceiling=100.0)
        self.assertIsNone(eng.efficiency.baseline)
        self.assertGreaterEqual(len(eng.efficiency.baseline_epochs), 2)
        self.assertEqual(len(eng.efficiency.windows), 0)

    def test_lower_threshold_still_requires_min_charge(self):
        cfg = _cfg(eff_baseline_windows=1, eff_min_window_charge_kwh=15.0)
        eng = bh.BatteryHealthEngine(cfg)
        self._anchor(eng, 0.0, 0.0, 0.0)
        self._anchor(eng, DAY, 10.0, 9.9)      # below threshold
        self.assertEqual(len(eng.efficiency.windows), 0)
        self._anchor(eng, 2 * DAY, 16.0, 15.8)
        self.assertEqual(len(eng.efficiency.windows), 1)


class TestTier2CalibrationAwareness(unittest.TestCase):  # v2.0.6, battery health architecture review
    """Tier 2: EfficiencyTracker and BalanceTracker must not treat a
    reading captured during calib_uncertain as trustworthy, for the same
    reasoning Tier 1 already established for SegmentTracker -- see
    DischargeSegment.exclude_calibration's own comment for the full
    background. Confirmed directly from this project's own code comments
    (not assumed) that this matters MORE for efficiency specifically:
    tier-1 anchors are described as being "at a BMS recalibration point"
    -- a structural, not incidental, overlap.
    """

    def test_efficiency_anchor_is_disqualified_during_calibration(self):
        """A reading that would otherwise fully qualify as a tier-1
        anchor must not, while calib_uncertain -- confirms
        _anchor_tier() itself rejects it, not just the resulting window."""
        cfg = _cfg(eff_baseline_windows=1)
        eng = bh.BatteryHealthEngine(cfg)
        self.assertEqual(
            eng.efficiency._anchor_tier(
                _sample(0.0, soc=100.0, power=0.0, chg=0.0, dis=0.0),
                calib_uncertain=True,
            ),
            0,
            "a reading during calib_uncertain must never qualify as an "
            "anchor, regardless of how well it otherwise satisfies the "
            "tier-1/tier-2 conditions",
        )

    def test_efficiency_window_not_built_from_calibration_overlap_anchor(self):
        """End-to-end: a window whose FIRST anchor was captured during
        calibration must never form, even once a later, genuinely clean
        anchor arrives."""
        cfg = _cfg(eff_baseline_windows=1)
        eng = bh.BatteryHealthEngine(cfg)
        eng.efficiency.feed(
            _sample(0.0, soc=100.0, power=0.0, chg=0.0, dis=0.0),
            calib_uncertain=True,
        )
        eng.efficiency.feed(
            _sample(DAY, soc=100.0, power=0.0, chg=20.0, dis=19.8),
            calib_uncertain=False,
        )
        self.assertEqual(
            len(eng.efficiency.windows), 0,
            "no window should form -- the first anchor was never "
            "actually recorded (disqualified at calib_uncertain time), "
            "so the second reading became the FIRST anchor instead, not "
            "the end of a window",
        )

    def test_efficiency_unaffected_when_not_calibrating(self):
        """Negative case: ordinary anchors must still work exactly as
        before -- confirms Tier 2 didn't break Tier 2's own prerequisite
        (unmodified default behaviour)."""
        cfg = _cfg(eff_baseline_windows=1)
        eng = bh.BatteryHealthEngine(cfg)
        eng.efficiency.feed(_sample(0.0, soc=100.0, power=0.0, chg=0.0, dis=0.0))
        eng.efficiency.feed(_sample(DAY, soc=100.0, power=0.0, chg=20.0, dis=19.8))
        self.assertEqual(len(eng.efficiency.windows), 1)

    def test_balance_score_not_accumulated_during_calibration(self):
        """Raw dV/dT are still recorded (matching learn=False's own
        established behaviour) but no score/baseline capture happens."""
        cfg = _cfg(balance_baseline_min_samples=1)
        eng = bh.BatteryHealthEngine(cfg)
        packs = [bh.PackSample(voltage=53.0, temp_max=25.0, temp_min=24.0, online=True),
                 bh.PackSample(voltage=53.1, temp_max=25.1, temp_min=24.1, online=True)]
        eng.balance.feed(
            _sample(0.0, soc=100.0, power=0.0, packs=packs, ceiling=100.0),
            calib_uncertain=True,
        )
        self.assertEqual(len(eng.balance.raw_dv), 1, "raw values must still be recorded")
        self.assertIsNone(
            eng.balance.baseline_dv,
            "no baseline may be captured from a sample during calib_uncertain",
        )

    def test_balance_unaffected_when_not_calibrating(self):
        cfg = _cfg(balance_baseline_min_samples=1)
        eng = bh.BatteryHealthEngine(cfg)
        packs = [bh.PackSample(voltage=53.0, temp_max=25.0, temp_min=24.0, online=True),
                 bh.PackSample(voltage=53.1, temp_max=25.1, temp_min=24.1, online=True)]
        eng.balance.feed(_sample(0.0, soc=100.0, power=0.0, packs=packs, ceiling=100.0))
        self.assertIsNotNone(eng.balance.baseline_dv)

    def test_engine_wires_calib_uncertain_into_both_trackers_end_to_end(self):
        """Full integration through BatteryHealthEngine.update() itself,
        not the trackers called directly -- confirms the centralized
        edge-detection (moved here from SegmentTracker in this same
        refactor) actually reaches efficiency and balance, not just
        capacity."""
        cfg = _cfg(eff_baseline_windows=1, balance_baseline_min_samples=1)
        eng = bh.BatteryHealthEngine(cfg)
        packs = [bh.PackSample(voltage=53.0, temp_max=25.0, temp_min=24.0, online=True),
                 bh.PackSample(voltage=53.1, temp_max=25.1, temp_min=24.1, online=True)]
        eng.update(_sample(0.0, soc=100.0, power=0.0, chg=0.0, dis=0.0,
                           packs=packs, ceiling=100.0, calib=True))
        self.assertIsNone(
            eng.balance.baseline_dv,
            "the engine must propagate calibration state to balance, "
            "not just capacity",
        )


class TestBalanceDiagnosticChannels(unittest.TestCase):  # T26
    """Independent MIN-sensor channel and physical-unit deviations."""

    def _feed(self, eng, n, tmax, tmin, volts=(26.4, 26.4, 26.5)):
        for i in range(n):
            packs = [bh.PackSample(voltage=v, temp_max=a, temp_min=b, online=True)
                     for v, a, b in zip(volts, tmax, tmin)]
            eng.balance.feed(_sample(i * 60, soc=98.0, power=0.0, packs=packs))

    def test_min_sensor_spread_tracked_independently(self):
        cfg = _cfg(balance_baseline_min_samples=5)
        eng = bh.BatteryHealthEngine(cfg)
        # Field-like: max spread 2.6, min spread 2.7, same ordering.
        self._feed(eng, 10, (25.9, 28.5, 27.2), (23.6, 26.2, 25.1))
        _, attrs = eng.balance.soh_balance()
        self.assertAlmostEqual(attrs["balance_raw_dt"], 2.6, places=1)
        self.assertAlmostEqual(attrs["balance_raw_dt_min_sensors"], 2.6, places=1)
        self.assertLess(attrs["balance_channel_disagreement"], 0.3)

    def test_channel_disagreement_flags_sensor_fault(self):
        """Max and min channels diverging points at a sensor, not a thermal, issue."""
        cfg = _cfg(balance_baseline_min_samples=5)
        eng = bh.BatteryHealthEngine(cfg)
        self._feed(eng, 10, (25.9, 33.0, 27.2), (23.6, 26.2, 25.1))
        _, attrs = eng.balance.soh_balance()
        self.assertGreater(attrs["balance_channel_disagreement"], 3.0)

    def test_deviation_exposed_in_physical_units(self):
        cfg = _cfg(balance_baseline_min_samples=5)
        eng = bh.BatteryHealthEngine(cfg)
        self._feed(eng, 10, (25.9, 28.5, 27.2), (23.6, 26.2, 25.1))
        _, base = eng.balance.soh_balance()
        self.assertAlmostEqual(base["balance_dt_deviation"], 0.0, places=1)
        self._feed(eng, 5, (25.9, 30.5, 27.2), (23.6, 26.2, 25.1))
        _, drift = eng.balance.soh_balance()
        self.assertAlmostEqual(drift["balance_dt_deviation"], 2.0, places=1)


class TestThermalRise(unittest.TestCase):  # T27 / optional ambient input
    """Rise above ambient measures heat GENERATION.

    Inter-pack spread is blind to all packs ageing together; rise above
    ambient is not.  Field baseline (max sensors): +2.6 / +5.2 / +3.9 C.
    """

    def _packs(self, temps):
        return [bh.PackSample(voltage=26.4, temp_max=t, temp_min=t - 2.3,
                              online=True) for t in temps]

    def test_thermal_rise_computed_when_ambient_present(self):
        cfg = _cfg(balance_baseline_min_samples=5)
        eng = bh.BatteryHealthEngine(cfg)
        for i in range(10):
            s = _sample(i * 60, soc=98.0, power=0.0,
                        packs=self._packs([25.9, 28.5, 27.2]))
            s.ambient_temp_c = 23.3
            eng.balance.feed(s)
        _, attrs = eng.balance.soh_balance()
        rise = attrs["thermal_rise_above_ambient"]
        self.assertEqual(len(rise), 3)
        self.assertAlmostEqual(rise[0], 2.6, places=1)
        self.assertAlmostEqual(rise[1], 5.2, places=1)
        self.assertAlmostEqual(attrs["thermal_rise_max"], 5.2, places=1)

    def test_absent_ambient_degrades_silently(self):
        cfg = _cfg(balance_baseline_min_samples=5)
        eng = bh.BatteryHealthEngine(cfg)
        for i in range(10):
            eng.balance.feed(_sample(i * 60, soc=98.0, power=0.0,
                                     packs=self._packs([25.9, 28.5, 27.2])))
        _, attrs = eng.balance.soh_balance()
        self.assertIsNone(attrs["thermal_rise_above_ambient"])
        self.assertIsNotNone(attrs["balance_raw_dt"])   # everything else works

    def test_rise_deviation_detects_uniform_ageing(self):
        """All packs hotter by the same amount: spread unchanged, rise up.

        Samples are spread over days because the thermal-rise baseline
        requires a multi-day span (v1.2.1): pack cooling runs ~-0.4 C/h, so
        consecutive samples carry one afternoon's load history, not a norm.
        """
        cfg = _cfg(balance_baseline_min_samples=5,
                   thermal_rise_baseline_min_span_days=3.0)
        eng = bh.BatteryHealthEngine(cfg)
        for i in range(10):
            s = _sample(i * DAY, soc=98.0, power=0.0,
                        packs=self._packs([25.9, 28.5, 27.2]))
            s.ambient_temp_c = 23.3
            eng.balance.feed(s)
        _, base = eng.balance.soh_balance()
        self.assertAlmostEqual(base["thermal_rise_deviation"], 0.0, places=1)
        for i in range(5):
            s = _sample((11 + i) * DAY, soc=98.0, power=0.0,
                        packs=self._packs([27.9, 30.5, 29.2]))
            s.ambient_temp_c = 23.3
            eng.balance.feed(s)
        _, later = eng.balance.soh_balance()
        self.assertAlmostEqual(later["balance_dt_deviation"], 0.0, places=1)
        self.assertAlmostEqual(later["thermal_rise_deviation"], 2.0, places=1)

    def test_thermal_rise_baseline_deferred_until_span_reached(self):
        """20 samples from one afternoon must NOT anchor the rise baseline."""
        cfg = _cfg(balance_baseline_min_samples=5,
                   thermal_rise_baseline_min_span_days=3.0)
        eng = bh.BatteryHealthEngine(cfg)
        for i in range(10):
            s = _sample(i * 60, soc=98.0, power=0.0,
                        packs=self._packs([25.9, 28.5, 27.2]))
            s.ambient_temp_c = 23.3
            eng.balance.feed(s)
        _, attrs = eng.balance.soh_balance()
        self.assertIsNotNone(attrs["thermal_rise_max"])      # still measured
        self.assertIsNone(attrs["thermal_rise_baseline_max"])  # not anchored


# ═════════════════════════════════════════════════════════════════════════════
# v1.2.1 — maintenance inhibit, settling, ceiling validation
# ═════════════════════════════════════════════════════════════════════════════
class TestCeilingValidation(unittest.TestCase):  # T28
    """Register 47081 must not be able to destroy baselines during a reboot.

    A ceiling change restarts the efficiency AND balance baseline epochs, so a
    spurious change is expensive.  Firmware cycles take ~1 h and the vendor
    does not document which registers stay meaningful throughout.
    """

    def test_implausible_ceiling_rejected(self):
        cfg = _cfg()
        mon = bh.CeilingMonitor(cfg)
        self.assertEqual(mon.feed(100.0), 100.0)
        # Reboot artefact: register reads 0.
        self.assertEqual(mon.feed(0.0), 100.0)
        self.assertEqual(mon.feed(0.0), 100.0)
        self.assertEqual(mon.rejected_count, 2)

    def test_transient_change_debounced(self):
        cfg = _cfg(ceiling_debounce_samples=3)
        mon = bh.CeilingMonitor(cfg)
        mon.feed(100.0)
        # A single odd-but-plausible reading must not be accepted.
        self.assertEqual(mon.feed(50.0), 100.0)
        self.assertEqual(mon.feed(100.0), 100.0)
        self.assertEqual(mon.debounced_count, 0)

    def test_genuine_change_accepted_after_debounce(self):
        cfg = _cfg(ceiling_debounce_samples=3)
        mon = bh.CeilingMonitor(cfg)
        mon.feed(100.0)
        for _ in range(3):
            mon.feed(93.0)
        self.assertEqual(mon.value, 93.0)
        self.assertEqual(mon.debounced_count, 1)

    def test_reboot_glitch_does_not_wipe_baselines(self):
        """End-to-end: the failure mode this guard exists for."""
        cfg = _cfg(eff_baseline_windows=1, ceiling_debounce_samples=3)
        eng = bh.BatteryHealthEngine(cfg)
        eng.update(_sample(0, soc=100.0, power=0.0, chg=0.0, dis=0.0,
                           temp=20.0, ceiling=100.0))
        eng.update(_sample(600, soc=100.0, power=0.0, chg=20.0, dis=19.8,
                           temp=20.0, ceiling=100.0))
        self.assertIsNotNone(eng.efficiency.baseline)
        baseline = eng.efficiency.baseline
        # Firmware cycle: register returns 0 for a few polls.
        for i in range(5):
            eng.update(_sample(700 + i * 30, soc=100.0, power=0.0, chg=20.0,
                               dis=19.8, temp=20.0, ceiling=0.0))
        self.assertEqual(eng.efficiency.baseline, baseline,
                         "a glitched ceiling must not restart the epoch")
        self.assertGreater(eng.report.attributes["ceiling_rejected_readings"], 0)


class TestLearningInhibit(unittest.TestCase):  # T29
    """Maintenance inhibit: freeze learning, keep measuring."""

    def test_disabled_learning_records_no_segments(self):
        cfg = _cfg(freshness_tau_kwh=1e12)
        eng = bh.BatteryHealthEngine(cfg)
        eng.set_learning_enabled(False)
        _run_discharge(eng, 0.0, 100.0, 77.0, 0.0, 5.24)
        self.assertEqual(len(eng.segments.segments), 0)

    def test_disabled_learning_still_updates_counters(self):
        """Sensors must keep displaying during maintenance."""
        eng = bh.BatteryHealthEngine(_cfg())
        eng.set_learning_enabled(False)
        eng.update(_sample(0, soc=50.0, power=0.0, chg=3300.0, dis=3105.0,
                           temp=20.0))
        self.assertAlmostEqual(eng.report.efc, 150.0, places=1)
        self.assertFalse(eng.report.attributes["learning_enabled"])

    def test_disabled_learning_blocks_ceiling_epoch(self):
        """The whole point: maintenance cannot poison baselines."""
        cfg = _cfg(eff_baseline_windows=1)
        eng = bh.BatteryHealthEngine(cfg)
        eng.update(_sample(0, soc=100.0, power=0.0, chg=0.0, dis=0.0,
                           temp=20.0, ceiling=100.0))
        eng.update(_sample(600, soc=100.0, power=0.0, chg=20.0, dis=19.8,
                           temp=20.0, ceiling=100.0))
        baseline = eng.efficiency.baseline
        self.assertIsNotNone(baseline)
        eng.set_learning_enabled(False)
        for i in range(5):
            eng.update(_sample(700 + i * 30, soc=100.0, power=0.0, chg=20.0,
                               dis=19.8, temp=20.0, ceiling=50.0))
        self.assertEqual(eng.efficiency.baseline, baseline)

    def test_state_survives_persistence(self):
        eng = bh.BatteryHealthEngine(_cfg())
        eng.set_learning_enabled(False)
        import json
        data = json.loads(json.dumps(eng.to_dict()))
        eng2 = bh.BatteryHealthEngine(_cfg())
        eng2.restore(data)
        self.assertFalse(eng2.learning_enabled)

    def test_reenable_triggers_settling(self):
        eng = bh.BatteryHealthEngine(_cfg(settling_period_s=300.0))
        eng.set_learning_enabled(False)
        eng.set_learning_enabled(True)
        now = time.time()
        self.assertTrue(eng.learning_enabled)
        self.assertFalse(eng.learning_active(now))
        self.assertTrue(eng.learning_active(now + 400.0))


class TestSettlingPeriod(unittest.TestCase):  # T30
    """Unplanned recoveries cannot be prepared for, so settle automatically."""

    def test_no_learning_during_settling(self):
        """A discharge occurring entirely inside the settling window is not
        recorded.  (The window is deliberately long here: the default 300 s is
        shorter than a realistic discharge, so learning correctly resumes
        part-way through one - see test_learning_resumes_after_settling.)"""
        cfg = _cfg(freshness_tau_kwh=1e12, settling_period_s=7200.0)
        eng = bh.BatteryHealthEngine(cfg)
        eng.mark_recovery("test restart", now=0.0)
        _run_discharge(eng, 60.0, 100.0, 77.0, 0.0, 5.24)
        self.assertEqual(len(eng.segments.segments), 0)
        self.assertFalse(eng.learning_active(1200.0))

    def test_learning_resumes_after_settling(self):
        cfg = _cfg(freshness_tau_kwh=1e12, settling_period_s=300.0)
        eng = bh.BatteryHealthEngine(cfg)
        eng.mark_recovery("test restart", now=0.0)
        _run_discharge(eng, 400.0, 100.0, 77.0, 0.0, 5.24)
        self.assertEqual(len(eng.segments.segments), 1)

    def test_counter_reset_triggers_settling(self):
        eng = bh.BatteryHealthEngine(_cfg(settling_period_s=300.0))
        eng.update(_sample(0, soc=50.0, power=0.0, chg=100.0, dis=95.0, temp=20.0))
        eng.update(_sample(60, soc=50.0, power=0.0, chg=1.0, dis=1.0, temp=20.0))
        self.assertFalse(eng.learning_active(60.0))
        self.assertGreaterEqual(eng.report.attributes["settling_events"], 1)

    def test_settling_does_not_stop_measurement(self):
        eng = bh.BatteryHealthEngine(_cfg(settling_period_s=300.0))
        eng.mark_recovery("test", now=0.0)
        eng.update(_sample(60, soc=50.0, power=0.0, chg=3300.0, dis=3105.0,
                           temp=20.0))
        self.assertAlmostEqual(eng.report.efc, 150.0, places=1)
        self.assertFalse(eng.report.attributes["learning_active"])


class TestTier3PackCapacityTracker(unittest.TestCase):  # v2.0.6, battery health architecture review
    """PackCapacityTracker: a direct, measured per-pack capacity estimate,
    reusing SegmentTracker exactly as-is per pack. See that class's own
    docstring for the full reasoning behind choosing this over the
    parked design's simpler per-pack-SOC-blended-into-balance plan."""

    @staticmethod
    def _pack(soc, power=-2500.0, chg=0.0, dis=0.0, online=True,
              voltage=53.0, temp_max=25.0, temp_min=24.0):
        return bh.PackSample(
            voltage=voltage, temp_max=temp_max, temp_min=temp_min, online=online,
            soc=soc, power_w=power, lifetime_charge_kwh=chg,
            lifetime_discharge_kwh=dis,
        )

    def _run_pack_discharge(self, eng, t0, soc0, soc1, dis0, dis1, steps=20):
        """Drive all three packs identically through one discharge, unless
        a test overrides a specific pack afterward. Closes with a
        CHARGING tick, matching _run_discharge()'s own established
        pattern (v1.2.0: idle no longer closes a segment).

        dis0/dis1 are pack-scale kWh (a single pack's own nameplate is
        roughly 1/3 of the whole unit's ~20.7 kWh, hence PackCapacity
        Tracker's own scaled implied-capacity plausibility band) -- NOT
        the same magnitude as _run_discharge()'s own unit-scale values.
        """
        for i in range(steps + 1):
            frac = i / steps
            soc = soc0 + (soc1 - soc0) * frac
            dis = dis0 + (dis1 - dis0) * frac
            packs = [self._pack(soc, dis=dis) for _ in range(3)]
            eng.update(_sample(t0 + i * 60, soc=soc0, power=-2500.0,
                               chg=0.0, dis=0.0, packs=packs))
        packs = [self._pack(soc1, power=CLOSE_POWER, dis=dis1) for _ in range(3)]
        eng.update(_sample(t0 + (steps + 1) * 60, soc=soc1, power=CLOSE_POWER,
                           chg=0.0, dis=0.0, packs=packs))

    def test_pack_segment_is_detected_and_contributes_to_that_packs_soh(self):
        """A genuine per-pack discharge must be detected by that pack's
        own SegmentTracker instance and contribute to its own
        soh_capacity -- confirms the reused-as-is wiring actually works
        end to end, not just compiles."""
        cfg = _cfg(freshness_tau_kwh=1e12)
        eng = bh.BatteryHealthEngine(cfg)
        self._run_pack_discharge(eng, 0.0, 95.0, 75.0, 0.0, 1.6)
        for i, tracker in enumerate(eng.pack_capacity.trackers):
            self.assertEqual(
                len(tracker.segments), 1,
                f"pack {i + 1}'s own tracker must have exactly one segment",
            )

    def test_packs_track_independently(self):
        """One pack's own segment must not affect another's -- confirms
        genuine independence, not a shared/aliased tracker instance."""
        cfg = _cfg(freshness_tau_kwh=1e12)
        eng = bh.BatteryHealthEngine(cfg)
        for i in range(21):
            frac = i / 20
            soc = 95.0 - 20.0 * frac
            # Pack 1 discharges 4 kWh; pack 2 discharges 8 kWh (weaker
            # implied capacity); pack 3 stays flat (no segment at all).
            packs = [
                self._pack(soc, dis=1.6 * frac),
                self._pack(soc, dis=1.0 * frac),  # weaker: less energy for the same SOC drop
                self._pack(100.0, power=0.0, dis=0.0),  # never discharges
            ]
            eng.update(_sample(i * 60, soc=95.0, power=-2500.0, chg=0.0,
                               dis=0.0, packs=packs))
        # Close packs 1/2 with a charging tick (v1.2.0: idle alone doesn't
        # close a segment); pack 3 never opened one, so this is a no-op
        # for it either way.
        packs = [
            self._pack(75.0, power=CLOSE_POWER, dis=1.6),
            self._pack(75.0, power=CLOSE_POWER, dis=1.0),
            self._pack(100.0, power=CLOSE_POWER, dis=0.0),
        ]
        eng.update(_sample(21 * 60, soc=75.0, power=CLOSE_POWER, chg=0.0,
                           dis=0.0, packs=packs))
        self.assertEqual(len(eng.pack_capacity.trackers[0].segments), 1)
        self.assertEqual(len(eng.pack_capacity.trackers[1].segments), 1)
        self.assertEqual(
            len(eng.pack_capacity.trackers[2].segments), 0,
            "a pack that never discharges must have no segments at all",
        )

    def test_pack_counter_reset_is_isolated_to_that_pack(self):
        """A single pack's own lifetime counter resetting (e.g. that pack
        being physically replaced) must discard only that pack's own
        active segment -- not the other packs', and not the unit-level
        tracker's."""
        cfg = _cfg(freshness_tau_kwh=1e12)
        eng = bh.BatteryHealthEngine(cfg)
        packs = [self._pack(90.0, dis=0.0) for _ in range(3)]
        eng.update(_sample(0, soc=90.0, power=-2500.0, chg=0.0, dis=0.0, packs=packs))
        # All three packs progress normally -- pack 1 past 1.0 kWh
        # specifically, so its own genuine reset (below) exceeds
        # COUNTER_RESET_TOLERANCE_KWH (1.0).
        packs = [
            self._pack(85.0, dis=1.5),
            self._pack(85.0, dis=0.5),
            self._pack(85.0, dis=0.5),
        ]
        eng.update(_sample(60, soc=85.0, power=-2500.0, chg=0.0, dis=0.0, packs=packs))
        # Pack 1's own counter genuinely resets (1.5 -> 0.01, a 1.49 kWh
        # decrease, exceeding the 1.0 kWh tolerance); packs 2/3 continue
        # discharging normally and uninterrupted.
        packs = [
            self._pack(80.0, dis=0.01),
            self._pack(80.0, dis=0.7),
            self._pack(80.0, dis=0.7),
        ]
        eng.update(_sample(120, soc=80.0, power=-2500.0, chg=0.0, dis=0.0, packs=packs))
        # Pack 1's ORIGINAL segment (start ts=0) must have been discarded
        # (counter reset detected) -- confirmed via discarded_segments,
        # not _active: the same tick's own data still shows pack 1
        # discharging, so a genuinely NEW segment correctly starts right
        # away (active=True again is expected -- it's a different
        # segment now, not the discarded one).
        self.assertGreaterEqual(eng.pack_capacity.trackers[0].discarded_segments, 1)
        self.assertEqual(
            len(eng.pack_capacity.trackers[0].segments), 0,
            "the discarded segment must never have contributed to soh_capacity",
        )
        # Packs 2 and 3 must be unaffected -- still actively tracking their
        # own, uninterrupted discharge, with nothing discarded.
        self.assertTrue(eng.pack_capacity.trackers[1]._active)
        self.assertTrue(eng.pack_capacity.trackers[2]._active)
        self.assertEqual(eng.pack_capacity.trackers[1].discarded_segments, 0)
        self.assertEqual(eng.pack_capacity.trackers[2].discarded_segments, 0)

    def test_spread_metric_reflects_the_weaker_pack(self):
        """The headline diagnostic this whole tracker exists for: a
        measurably weaker pack must show up as a nonzero spread."""
        cfg = _cfg(freshness_tau_kwh=1e12, eff_baseline_windows=999,
                   balance_baseline_min_samples=999)
        eng = bh.BatteryHealthEngine(cfg)
        for i in range(21):
            frac = i / 20
            soc = 95.0 - 20.0 * frac
            packs = [
                self._pack(soc, dis=1.6 * frac),   # normal pack
                self._pack(soc, dis=1.0 * frac),   # weaker: less energy for the same SOC drop
                self._pack(soc, dis=1.6 * frac),   # normal pack
            ]
            eng.update(_sample(i * 60, soc=95.0, power=-2500.0, chg=0.0,
                               dis=0.0, packs=packs))
        packs = [
            self._pack(75.0, power=CLOSE_POWER, dis=1.6),
            self._pack(75.0, power=CLOSE_POWER, dis=1.0),
            self._pack(75.0, power=CLOSE_POWER, dis=1.6),
        ]
        eng.update(_sample(21 * 60, soc=75.0, power=CLOSE_POWER, chg=0.0,
                           dis=0.0, packs=packs))
        report = eng.report
        spread = report.attributes.get("pack_capacity_spread_pct")
        self.assertIsNotNone(spread)
        self.assertGreater(spread, 0.0)

    def test_pack_capacity_persists_and_restores(self):
        """Round-trips through to_dict()/restore() -- confirms Tier 3's
        new state survives a restart the same way every other tracker's
        own state already does."""
        cfg = _cfg(freshness_tau_kwh=1e12)
        eng = bh.BatteryHealthEngine(cfg)
        self._run_pack_discharge(eng, 0.0, 95.0, 75.0, 0.0, 1.6)
        data = eng.to_dict()
        eng2 = bh.BatteryHealthEngine(cfg)
        eng2.restore(data)
        for i in range(3):
            self.assertEqual(
                len(eng2.pack_capacity.trackers[i].segments),
                len(eng.pack_capacity.trackers[i].segments),
            )

    def test_old_schema_version_starts_fresh_not_crashes(self):
        """v2.0.6's SCHEMA_VERSION bump (1 -> 2, for pack_capacity) must
        make old, pre-Tier-3 persisted data start fresh cleanly, not
        crash -- matching the operator's own explicit choice (no
        migration needed, OK to lose history)."""
        eng = bh.BatteryHealthEngine(_cfg())
        old_data = {"schema_version": 1, "first_seen_ts": 123.0}
        eng.restore(old_data)  # must not raise
        self.assertIsNone(eng.first_seen_ts)  # started fresh, not "restored" v1 data


class TestTier3CapacityNormalization(unittest.TestCase):  # v2.0.6, battery health architecture review
    """Temperature/rate normalization, from PHASE1_BATTERY_HEALTH_DESIGN
    .md's own §6.2. Formula tested directly against DischargeSegment.
    normalized_capacity_kwh() with hand-built segments -- isolates the
    formula itself from segment-detection mechanics, matching this
    file's own established pattern for testing weight() directly."""

    @staticmethod
    def _seg(implied=20.0, avg_temp_c=None, energy_kwh=4.0, duration_h=1.0):
        return bh.DischargeSegment(
            start_ts=0.0, end_ts=duration_h * 3600.0, soc_start=95.0,
            soc_end=75.0, energy_kwh=energy_kwh, implied_capacity_kwh=implied,
            freshness=1.0, exclude_calibration=False, avg_temp_c=avg_temp_c,
        )

    def test_no_temperature_defaults_to_neutral_f_temp(self):
        cfg = bh.BatteryHealthConfig()  # normalization NOT neutralized here -- testing it directly
        seg = self._seg(implied=20.0, avg_temp_c=None, energy_kwh=0.001, duration_h=1000.0)
        # near-zero average power too (via a tiny energy/long duration),
        # so f_rate is also neutral -- isolates f_temp's own None handling.
        self.assertAlmostEqual(seg.normalized_capacity_kwh(cfg), 20.0, places=2)

    def test_at_reference_temperature_f_temp_is_neutral(self):
        cfg = bh.BatteryHealthConfig()
        seg = self._seg(implied=20.0, avg_temp_c=cfg.capacity_temp_ref_c,
                        energy_kwh=0.001, duration_h=1000.0)
        self.assertAlmostEqual(seg.normalized_capacity_kwh(cfg), 20.0, places=2)

    def test_cold_segment_normalized_capacity_exceeds_raw(self):
        """A genuinely cold segment's RAW implied capacity understates the
        pack's true health -- normalization must correct upward."""
        cfg = bh.BatteryHealthConfig()
        seg = self._seg(implied=20.0, avg_temp_c=0.0, energy_kwh=0.001, duration_h=1000.0)
        normalized = seg.normalized_capacity_kwh(cfg)
        self.assertGreater(normalized, 20.0)

    def test_high_power_segment_normalized_capacity_exceeds_raw(self):
        """A genuine high-rate discharge understates true capacity the
        same way -- normalization must correct upward here too."""
        cfg = bh.BatteryHealthConfig()
        # 8 kWh over 0.5h = 16 kW average power, well above the 5 kW reference.
        seg = self._seg(implied=20.0, avg_temp_c=cfg.capacity_temp_ref_c,
                        energy_kwh=8.0, duration_h=0.5)
        normalized = seg.normalized_capacity_kwh(cfg)
        self.assertGreater(normalized, 20.0)

    def test_clamp_floor_is_respected_for_extreme_temperature(self):
        cfg = bh.BatteryHealthConfig()
        seg = self._seg(implied=20.0, avg_temp_c=-40.0, energy_kwh=0.001, duration_h=1000.0)
        normalized = seg.normalized_capacity_kwh(cfg)
        # f_temp clamped to capacity_norm_factor_floor (0.5): normalized
        # capacity must not exceed raw / floor.
        self.assertLessEqual(normalized, 20.0 / cfg.capacity_norm_factor_floor + 1e-6)

    def test_avg_temp_c_accumulates_only_valid_readings(self):
        """A segment with some missing temperature ticks must average
        only the valid ones -- not treat a missing reading as zero."""
        cfg = _cfg(freshness_tau_kwh=1e12)
        eng = bh.BatteryHealthEngine(cfg)
        for i in range(21):
            frac = i / 20
            soc = 95.0 - 20.0 * frac
            dis = 4.0 * frac
            # Alternate valid/missing temperature readings.
            temp = 20.0 if i % 2 == 0 else None
            eng.update(_sample(i * 60, soc=soc, power=-2500.0, chg=0.0,
                               dis=dis, temp=temp))
        eng.update(_sample(21 * 60, soc=75.0, power=CLOSE_POWER, chg=0.0, dis=4.0, temp=20.0))
        self.assertEqual(len(eng.segments.segments), 1)
        self.assertAlmostEqual(eng.segments.segments[0].avg_temp_c, 20.0, places=1)

    def test_avg_temp_c_is_none_when_never_valid(self):
        cfg = _cfg(freshness_tau_kwh=1e12)
        eng = bh.BatteryHealthEngine(cfg)
        for i in range(21):
            frac = i / 20
            soc = 95.0 - 20.0 * frac
            dis = 4.0 * frac
            eng.update(_sample(i * 60, soc=soc, power=-2500.0, chg=0.0, dis=dis, temp=None))
        eng.update(_sample(21 * 60, soc=75.0, power=CLOSE_POWER, chg=0.0, dis=4.0, temp=None))
        self.assertEqual(len(eng.segments.segments), 1)
        self.assertIsNone(eng.segments.segments[0].avg_temp_c)

    def test_reference_capacity_capture_uses_normalized_not_raw(self):
        """The auto-captured reference must match the SAME normalization
        soh_capacity()'s own mean_cap uses -- confirms the consistency
        fix (not comparing a normalized numerator against a raw
        reference) actually took effect, not just that it compiles."""
        cfg = _cfg(freshness_tau_kwh=1e12, capacity_reference_min_segments=1,
                   capacity_reference_min_span_days=0.0,
                   capacity_temp_sigma_c=20.0, capacity_rate_ref_w=5000.0)
        eng = bh.BatteryHealthEngine(cfg)
        # A cold, high-power segment: normalized capacity must exceed raw.
        # 6.0 kWh over a 20% SOC drop (implied=30.0, within the unit-scale
        # [8,35] plausibility band) across ~21 simulated minutes -> ~17 kW
        # average power, well above the 5 kW reference.
        for i in range(21):
            frac = i / 20
            soc = 95.0 - 20.0 * frac
            dis = 6.0 * frac
            eng.update(_sample(i * 60, soc=soc, power=-2500.0, chg=0.0,
                               dis=dis, temp=0.0))
        eng.update(_sample(21 * 60, soc=75.0, power=CLOSE_POWER, chg=0.0,
                           dis=6.0, temp=0.0))
        seg = eng.segments.segments[0]
        raw = seg.implied_capacity_kwh
        normalized = seg.normalized_capacity_kwh(cfg)
        self.assertGreater(normalized, raw)
        self.assertAlmostEqual(
            eng.segments.reference_capacity_kwh, normalized, places=2,
            msg="the captured reference must equal the NORMALIZED value, not raw",
        )
