"""v2.3.2.0 battery-health fixes (AUDIT_2.3.2.0.md).

Every test drives the REAL battery_health.py engine (pure module, no Home
Assistant imports), loaded under its own module name so it cannot collide
with test_battery_health.py's copy. Wiring in HA-bound files
(battery_health_manager.py, battery_health_entities.py, config_flow.py,
const.py) is checked by AST/source inspection.

Traceability (AUDIT_2.3.2.0.md §3):
  BH-2320-01  cell temperature instead of the BMS board temperature
  BH-2320-02  cold-only capacity correction; rate correction removed
  BH-2320-03  forecast from lifetime effective age (∫S²dt)
  BH-2320-04  efficiency: η<=1, outlier filter, 10-window baseline, deadband
  BH-2320-05  balance: one sample per rest period
  BH-2320-06  minimum segment depth 15, applied retroactively
  BH-2320-07  thermal-rise baseline retried until set
  BH-2320-08  ordinary coordinator recovery bridges, does not discard
  BH-2320-09  reference = same estimator/eligible set; re-anchor covers packs
  BH-2320-10  calibration: 65-min settle + retro-exclusion of the last hour
  BH-2320-11  median-relative outlier exclusion for capacity segments
  BH-2320-12  schema 3 -> 4 migration keeps history; one-time upgrade
  N3          cold-charge hours counter (visibility only)
"""
from __future__ import annotations

import ast
import copy
import importlib.util
import json
import math
import os
import pathlib
import sys
import unittest

_BASE = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "battery_health_2320", _BASE / "battery_health.py")
bh = importlib.util.module_from_spec(_spec)
sys.modules["battery_health_2320"] = bh
_spec.loader.exec_module(bh)

HOUR = 3600.0
DAY = 86_400.0
CHARGE_W = 1500.0


def _pack(temp_max=None, temp_min=None, soc=None, power=None, dis=None,
          chg=None, volt=26.8, online=True, serial=None):
    return bh.PackSample(voltage=volt, temp_max=temp_max, temp_min=temp_min,
                         online=online, soc=soc, power_w=power,
                         lifetime_charge_kwh=chg, lifetime_discharge_kwh=dis,
                         serial_number=serial)


def _sample(ts, soc=None, power=None, bms=None, chg=None, dis=None, packs=None,
            calib=False, ceiling=100.0, ambient=None):
    return bh.HealthSample(timestamp=ts, soc=soc, power_w=power, battery_temp_c=bms,
                           lifetime_charge_kwh=chg, lifetime_discharge_kwh=dis,
                           packs=packs or [], soh_calibration_active=calib,
                           charge_ceiling_soc=ceiling, ambient_temp_c=ambient)


def _seg(implied, start, dsoc=30.0, temp=None, calib=False, energy=None):
    energy = implied * dsoc / 100.0 if energy is None else energy
    return bh.DischargeSegment(
        start_ts=start, end_ts=start + 8 * HOUR, soc_start=100.0,
        soc_end=100.0 - dsoc, energy_kwh=energy, implied_capacity_kwh=implied,
        freshness=1.0, exclude_calibration=calib, avg_temp_c=temp,
        soc_midpoint=100.0 - dsoc / 2)


def _discharge(eng, t0, soc0, soc1, dis0, dis1, bms=36.0, cell=26.0, steps=20,
               dt=60.0, power=-600.0, calib=False):
    packs = [_pack(cell + 1, cell - 1), _pack(cell + 1, cell - 1), _pack(cell + 1, cell - 1)]
    for i in range(steps + 1):
        f = i / steps
        eng.update(_sample(t0 + i * dt, soc=soc0 + (soc1 - soc0) * f, power=power,
                           bms=bms, chg=1000.0, dis=dis0 + (dis1 - dis0) * f,
                           packs=packs, calib=calib))
    eng.update(_sample(t0 + (steps + 1) * dt, soc=soc1, power=CHARGE_W, bms=bms,
                       chg=1000.0, dis=dis1, packs=packs, calib=calib))
    return t0 + (steps + 1) * dt


# ═════════════════════════════════════════════════════════════════════════
class TestCellTemperature(unittest.TestCase):  # BH-2320-01
    def test_mean_pack_cell_temp(self):
        packs = [_pack(28, 26), _pack(30, None), _pack(None, 24), _pack()]
        self.assertAlmostEqual(bh._mean_pack_cell_temp(packs), (27 + 30 + 24) / 3)
        self.assertIsNone(bh._mean_pack_cell_temp([_pack()]))

    def test_segment_records_cell_not_bms_temperature(self):
        eng = bh.BatteryHealthEngine(bh.BatteryHealthConfig())
        _discharge(eng, 0, 100, 60, 0.0, 9.2, bms=36.0, cell=26.0)
        self.assertEqual(len(eng.segments.segments), 1)
        self.assertAlmostEqual(eng.segments.segments[0].avg_temp_c, 26.0, places=6)
        self.assertEqual(eng.report.attributes["temperature_source"], "pack_cells")

    def test_bms_is_fallback_without_pack_temperatures(self):
        eng = bh.BatteryHealthEngine(bh.BatteryHealthConfig())
        for i in range(21):
            eng.update(_sample(i * 60, soc=100 - 2 * i, power=-600, bms=36.0,
                               chg=1000.0, dis=0.46 * i))
        eng.update(_sample(22 * 60, soc=60, power=CHARGE_W, bms=36.0, chg=1000.0, dis=9.2))
        self.assertAlmostEqual(eng.segments.segments[0].avg_temp_c, 36.0)
        self.assertEqual(eng.report.attributes["temperature_source"], "bms_fallback")

    def test_stress_uses_cell_temperature(self):
        cfg = bh.BatteryHealthConfig()
        eng = bh.BatteryHealthEngine(cfg)
        packs = [_pack(25, 25)] * 3
        for i in range(10):
            eng.update(_sample(i * 60, soc=50.0, power=0.0, bms=45.0, packs=packs))
        # cells at the 25 C reference and SOC below the knee: stress 1.0
        self.assertAlmostEqual(eng.stress.stress_ratio(), 1.0, places=9)

    def test_explicit_cell_temp_is_validated(self):
        raw = _sample(0, soc=50)
        raw.cell_temp_c = 400.0
        self.assertIsNone(bh.validate_sample(raw).cell_temp_c)


# ═════════════════════════════════════════════════════════════════════════
class TestColdOnlyCorrection(unittest.TestCase):  # BH-2320-02
    def setUp(self):
        self.cfg = bh.BatteryHealthConfig()

    def test_factor_shape(self):
        f = lambda t: bh.cold_capacity_factor(self.cfg, t)  # noqa: E731
        self.assertEqual(f(None), 1.0)
        self.assertEqual(f(15.0), 1.0)
        self.assertEqual(f(37.0), 1.0)          # warm: no correction at all
        self.assertAlmostEqual(f(10.0), 1 - 0.025)
        self.assertAlmostEqual(f(5.0), 1 - 0.05)
        self.assertAlmostEqual(f(-30.0), 1 - 0.10)   # capped at 10 %

    def test_warm_segment_taken_as_measured(self):
        seg = _seg(23.1, 0, temp=36.6)
        self.assertAlmostEqual(seg.normalized_capacity_kwh(self.cfg), 23.1)

    def test_rate_is_ignored(self):
        seg = _seg(20.0, 0, temp=25.0, energy=50.0)   # absurd 6 kW average
        self.assertAlmostEqual(seg.normalized_capacity_kwh(self.cfg), 20.0)

    def test_field_regression_unit_capacity_no_longer_inflated(self):
        """Field store 14.08-30.09: raw unit capacity ~23.1 kWh at BMS
        36-37 C was booked as ~32 kWh by the Gaussian factor."""
        old_gauss = math.exp(-((36.6 - 25.0) ** 2) / 400.0)
        self.assertGreater(23.1 / old_gauss, 31.0)          # the old defect
        self.assertAlmostEqual(_seg(23.1, 0, temp=36.6).normalized_capacity_kwh(self.cfg), 23.1)


# ═════════════════════════════════════════════════════════════════════════
class TestEligibleSegments(unittest.TestCase):  # BH-2320-06 / -11
    def _tracker(self, **kw):
        cfg = bh.BatteryHealthConfig(**kw)
        return bh.SegmentTracker(cfg)

    def test_default_depth_is_15(self):
        self.assertEqual(bh.BatteryHealthConfig().min_segment_delta_soc, 15.0)

    def test_shallow_stored_segments_excluded_retroactively(self):
        t = self._tracker()
        t.segments = [_seg(23.0, i * DAY, dsoc=30) for i in range(5)] + [
            _seg(26.0, 10 * DAY, dsoc=10), _seg(26.0, 11 * DAY, dsoc=14.9)]
        self.assertEqual(len(t.eligible_segments()), 5)
        t2 = self._tracker(min_segment_delta_soc=10.0)
        t2.segments = list(t.segments)
        self.assertEqual(len(t2.eligible_segments()), 7)

    def test_outlier_relative_to_window_median(self):
        t = self._tracker()
        t.segments = [_seg(23.0, i * DAY) for i in range(6)] + [_seg(31.0, 7 * DAY)]
        used = t.eligible_segments()
        self.assertEqual(len(used), 6)
        self.assertNotIn(31.0, [s.implied_capacity_kwh for s in used])

    def test_uniform_fade_is_never_filtered(self):
        """A median-relative filter follows genuine fade: every segment at
        70 % of the old value stays eligible."""
        t = self._tracker()
        t.segments = [_seg(16.0, i * DAY) for i in range(8)]
        self.assertEqual(len(t.eligible_segments()), 8)

    def test_calibration_segments_not_eligible(self):
        t = self._tracker()
        t.segments = [_seg(23.0, i * DAY) for i in range(5)] + [_seg(23.0, 9 * DAY, calib=True)]
        self.assertEqual(len(t.eligible_segments()), 5)


# ═════════════════════════════════════════════════════════════════════════
class TestReferenceConsistency(unittest.TestCase):  # BH-2320-09
    def _filled(self, n=24, cfg=None, values=None):
        cfg = cfg or bh.BatteryHealthConfig()
        t = bh.SegmentTracker(cfg)
        vals = values or [23.0 + 0.3 * math.sin(i) for i in range(n)]
        t.segments = [_seg(v, i * 2 * DAY, dsoc=20 + (i % 20)) for i, v in enumerate(vals)]
        return t

    def test_fresh_reference_reads_exactly_100(self):
        t = self._filled()
        soh, attrs = t.soh_capacity()
        self.assertTrue(attrs["capacity_reference_is_measured"])
        self.assertAlmostEqual(soh, 100.0, places=9)

    def test_candidate_requires_count_and_span(self):
        self.assertIsNone(self._filled(n=19).reference_candidate())
        t = bh.SegmentTracker(bh.BatteryHealthConfig())
        t.segments = [_seg(23.0, i * HOUR, dsoc=30) for i in range(30)]   # 1.2 days
        self.assertIsNone(t.reference_candidate())

    def test_reanchor_covers_unit_and_packs(self):
        eng = bh.BatteryHealthEngine(bh.BatteryHealthConfig())
        eng.segments.segments = [_seg(23.0, i * 2 * DAY) for i in range(24)]
        for tr in eng.pack_capacity.trackers:
            tr.segments = [_seg(7.8, i * 2 * DAY) for i in range(24)]
        self.assertTrue(eng.reanchor_capacity_reference())
        self.assertAlmostEqual(eng.segments.reference_capacity_kwh, 23.0)
        for tr in eng.pack_capacity.trackers:
            self.assertAlmostEqual(tr.reference_capacity_kwh, 7.8)
            self.assertEqual(tr.reference_epochs[-1]["reason"][:16], "manual re-anchor")

    def test_reanchor_ignores_calibration_segments(self):
        eng = bh.BatteryHealthEngine(bh.BatteryHealthConfig())
        eng.segments.segments = [_seg(23.0, i * 2 * DAY) for i in range(24)] + [
            _seg(30.0, 60 * DAY, calib=True) for _ in range(10)]
        eng.reanchor_capacity_reference()
        self.assertAlmostEqual(eng.segments.reference_capacity_kwh, 23.0)

    def test_reanchor_refuses_thin_data_without_clearing(self):
        eng = bh.BatteryHealthEngine(bh.BatteryHealthConfig())
        eng.segments.reference_capacity_kwh = 22.0
        self.assertFalse(eng.reanchor_capacity_reference())
        self.assertEqual(eng.segments.reference_capacity_kwh, 22.0)
        eng.reanchor_capacity_reference("x", clear_if_insufficient=True)
        self.assertIsNone(eng.segments.reference_capacity_kwh)
        self.assertIsNone(eng.segments.reference_epochs[-1]["value"])
        self.assertEqual(eng.segments.reference_epochs[-1]["previous"], 22.0)


# ═════════════════════════════════════════════════════════════════════════
class TestEfficiency(unittest.TestCase):  # BH-2320-04
    FIELD_WINDOWS = [1.0025, 0.9714, 0.9896, 0.9913, 0.9844, 0.9894, 0.9865,
                     0.9912, 0.9874, 0.9893, 0.9911, 0.9886, 0.9882, 0.9894,
                     0.9892, 0.9868, 0.988, 0.9869, 0.9871, 0.9896, 0.9874]

    def _eng(self, **kw):
        return bh.BatteryHealthEngine(bh.BatteryHealthConfig(**kw))

    def _anchor(self, eng, ts, chg, dis):
        eng.efficiency.feed(_sample(ts, soc=100.0, power=0.0, chg=chg, dis=dis))

    def _run(self, eng, etas, t0=0.0, chg=0.0, dis=0.0):
        self._anchor(eng, t0, chg, dis)
        for i, e in enumerate(etas, start=1):
            chg += 16.0
            dis += 16.0 * e
            self._anchor(eng, t0 + i * DAY, chg, dis)
        return chg, dis

    def test_defaults(self):
        cfg = bh.BatteryHealthConfig()
        self.assertEqual(cfg.eff_baseline_windows, 10)
        self.assertEqual(cfg.eff_valid_max, 1.0)

    def test_eta_above_one_rejected(self):
        eng = self._eng()
        self._run(eng, [1.0025, 0.99])
        self.assertEqual([round(w, 4) for w in eng.efficiency.windows], [0.99])

    def test_outlier_rejected_after_enough_history(self):
        eng = self._eng()
        self._run(eng, [0.989] * 6 + [0.9714, 0.988])
        ws = [round(w, 4) for w in eng.efficiency.windows]
        self.assertNotIn(0.9714, ws)
        self.assertEqual(eng.efficiency.rejected_outlier_windows, 1)

    def test_baseline_needs_ten_windows(self):
        eng = self._eng()
        chg, dis = self._run(eng, [0.989] * 9)
        self.assertEqual(len(eng.efficiency.windows), 9)
        self.assertIsNone(eng.efficiency.baseline)
        self._run(eng, [0.989], t0=20 * DAY, chg=chg, dis=dis)
        self.assertEqual(len(eng.efficiency.windows), 10)
        self.assertAlmostEqual(eng.efficiency.baseline, 0.989)

    def test_deadband(self):
        eng = self._eng()
        eng.efficiency.baseline = 0.9893
        eng.efficiency.windows.extend([0.98725] * 6)
        soh, attrs = eng.efficiency.soh_efficiency()
        self.assertEqual(soh, 100.0)                       # 0.205 %-pts < 0.25
        self.assertAlmostEqual(attrs["efficiency_loss_pct"], 0.205, places=3)
        eng.efficiency.windows.extend([0.9843] * 6)       # 0.5 %-pts
        soh, _ = eng.efficiency.soh_efficiency()
        self.assertAlmostEqual(soh, 100.0 - (0.5 - 0.25) * 8.0, places=6)

    def test_rederive_field_windows(self):
        """The 21 stored windows of the field store re-derive to the median
        of the first 10 surviving ones (1.0025 dropped)."""
        eng = self._eng()
        ef = eng.efficiency
        ef.windows.extend(self.FIELD_WINDOWS)
        ef.window_tiers.extend([1] * len(self.FIELD_WINDOWS))
        ef.window_end_ts.extend([None] * len(self.FIELD_WINDOWS))
        ef.window_temp_c.extend([None] * len(self.FIELD_WINDOWS))
        ef.baseline = 0.98958
        ef.baseline_epochs.append({"ts": 1.0, "value": 0.98958, "reason": "auto: first 3 windows"})
        ef.rederive("2.3.2.0 upgrade", ts=5.0)
        kept = [w for w in self.FIELD_WINDOWS if w <= 1.0][:10]
        self.assertAlmostEqual(ef.baseline, sorted(kept)[4] / 2 + sorted(kept)[5] / 2, places=9)
        self.assertEqual(len(ef.windows), 20)
        self.assertEqual(ef.baseline_epochs[0]["value"], 0.98958)   # history kept
        self.assertEqual(ef.baseline_epochs[-1]["reason"], "2.3.2.0 upgrade")
        self.assertEqual(ef.baseline_epochs[-1]["previous"], 0.98958)

    def test_drop_windows_since(self):
        eng = self._eng()
        ef = eng.efficiency
        for i, ts in enumerate([1.0, 2.0, 3.0]):
            ef._record_window(0.989, 1, ts * DAY, 25.0)
        self.assertEqual(ef.drop_windows_since(2.0 * DAY), 2)
        self.assertEqual(list(ef.window_end_ts), [1.0 * DAY])
        self.assertIsNone(ef._anchor)

    def test_persistence_pads_old_stores(self):
        eng = self._eng()
        eng.efficiency.restore({"windows": [0.98, 0.99], "window_tiers": [1, 1]})
        self.assertEqual(list(eng.efficiency.window_end_ts), [None, None])
        eng.efficiency._record_window(0.985, 1, 9.0, 24.0)
        d = eng.efficiency.to_dict()
        eng2 = self._eng()
        eng2.efficiency.restore(json.loads(json.dumps(d)))
        self.assertEqual(list(eng2.efficiency.window_end_ts), [None, None, 9.0])
        self.assertEqual(list(eng2.efficiency.window_temp_c), [None, None, 24.0])


# ═════════════════════════════════════════════════════════════════════════
class TestBalanceRestSampling(unittest.TestCase):  # BH-2320-05 / -07
    PACKS = [_pack(26.0, 25.0, volt=26.8), _pack(28.2, 27.0, volt=26.8),
             _pack(27.0, 26.0, volt=26.9)]

    def _rest(self, eng, t0, minutes, ambient=23.0):
        for m in range(minutes + 1):
            eng.update(_sample(t0 + m * 60, soc=100.0, power=0.0, packs=self.PACKS,
                               ambient=ambient))

    def _break(self, eng, ts):
        eng.update(_sample(ts, soc=90.0, power=-800.0, packs=self.PACKS))

    def test_one_sample_per_rest_period(self):
        eng = bh.BatteryHealthEngine(bh.BatteryHealthConfig())
        self._rest(eng, 0, 120)                     # 2 h rest
        self.assertEqual(len(eng.balance.raw_dv), 1)
        self.assertEqual(eng.balance.last_sample_ts, 600.0)   # after 10 min settle

    def test_settle_time_required(self):
        eng = bh.BatteryHealthEngine(bh.BatteryHealthConfig())
        self._rest(eng, 0, 9)
        self.assertEqual(len(eng.balance.raw_dv), 0)
        self.assertIsNotNone(eng.balance.live_dv)     # display still live

    def test_spacing_between_rest_periods(self):
        eng = bh.BatteryHealthEngine(bh.BatteryHealthConfig())
        self._rest(eng, 0, 20)
        self._break(eng, 30 * 60)
        self._rest(eng, 60 * 60, 20)                  # 1 h later: too soon
        self.assertEqual(len(eng.balance.raw_dv), 1)
        self._break(eng, 2 * HOUR)
        self._rest(eng, 5 * HOUR, 20)                 # > 4 h later
        self.assertEqual(len(eng.balance.raw_dv), 2)

    def test_legacy_per_tick_when_spacing_zero(self):
        eng = bh.BatteryHealthEngine(bh.BatteryHealthConfig(
            balance_min_sample_spacing_s=0.0, balance_rest_settle_s=0.0))
        self._rest(eng, 0, 4)
        self.assertEqual(len(eng.balance.raw_dv), 5)

    def test_baseline_and_thermal_rise_span_days(self):
        """20 daily rest periods: the balance baseline forms from samples on
        20 different days, and the thermal-rise baseline is set once the
        samples span 3 days (was: never, BH-2320-07)."""
        eng = bh.BatteryHealthEngine(bh.BatteryHealthConfig())
        for d in range(25):
            self._rest(eng, d * DAY + 12 * HOUR, 20)
            self._break(eng, d * DAY + 20 * HOUR)
        b = eng.balance
        self.assertIsNotNone(b.baseline_dv)
        self.assertIsNotNone(b.baseline_rise)
        span = (b.thermal_rise[-1][0] - b.thermal_rise[0][0]) / DAY
        self.assertGreaterEqual(span, 3.0)
        self.assertIsNotNone(eng.report.attributes["thermal_rise_baseline_max"])

    def test_thermal_retry_after_deferral(self):
        """A baseline that could not be set at balance-baseline time is set
        by a later sample (the pre-2.3.2.0 code tried exactly once)."""
        cfg = bh.BatteryHealthConfig(balance_baseline_min_samples=2)
        eng = bh.BatteryHealthEngine(cfg)
        for d in range(2):                        # baseline after 2 samples, 1 day span
            self._rest(eng, d * DAY, 20)
            self._break(eng, d * DAY + HOUR)
        self.assertIsNotNone(eng.balance.baseline_dv)
        self.assertIsNone(eng.balance.baseline_rise)
        for d in range(2, 5):
            self._rest(eng, d * DAY, 20)
            self._break(eng, d * DAY + HOUR)
        self.assertIsNotNone(eng.balance.baseline_rise)

    def test_restart_sampling_clears_buffers_keeps_epochs(self):
        eng = bh.BatteryHealthEngine(bh.BatteryHealthConfig(
            balance_min_sample_spacing_s=0.0, balance_rest_settle_s=0.0))
        self._rest(eng, 0, 25)
        self.assertIsNotNone(eng.balance.baseline_dv)
        n_epochs = len(eng.balance.baseline_epochs)
        eng.balance.restart_sampling("2.3.2.0 upgrade", ts=1.0)
        b = eng.balance
        self.assertEqual((len(b.raw_dv), len(b.thermal_rise), len(b.scores)), (0, 0, 0))
        self.assertIsNone(b.baseline_dv)
        self.assertEqual(len(b.baseline_epochs), n_epochs + 1)


# ═════════════════════════════════════════════════════════════════════════
class TestRecovery(unittest.TestCase):  # BH-2320-08
    def _run(self, hard):
        cfg = bh.BatteryHealthConfig()
        eng = bh.BatteryHealthEngine(cfg)
        packs = [_pack(27, 25)] * 3
        t = 0.0
        for i in range(30):                       # 30 min discharge, 100 -> 85
            eng.update(_sample(t, soc=100 - 0.5 * i, power=-600, chg=1000.0,
                               dis=0.115 * i, packs=packs))
            t += 60
        eng.mark_gap()                            # one failed read
        eng.mark_recovery("coordinator recovered", now=t, hard=hard)
        for i in range(30, 60):                   # settling 5 min, then on
            eng.update(_sample(t, soc=100 - 0.5 * i, power=-600, chg=1000.0,
                               dis=0.115 * i, packs=packs))
            t += 60
        eng.update(_sample(t, soc=70, power=CHARGE_W, chg=1000.0, dis=6.9, packs=packs))
        return eng

    def test_soft_recovery_keeps_the_night(self):
        eng = self._run(hard=False)
        self.assertEqual(len(eng.segments.segments), 1)
        self.assertAlmostEqual(eng.segments.segments[0].soc_start, 100.0)

    def test_hard_recovery_still_discards(self):
        eng = self._run(hard=True)
        self.assertTrue(all(s.soc_start < 100.0 for s in eng.segments.segments))

    def test_manager_uses_soft_recovery_for_coordinator_recovery(self):
        src = (_BASE / "battery_health_manager.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", "") == "mark_recovery"
                 and n.args and isinstance(n.args[0], ast.Constant)
                 and n.args[0].value == "coordinator recovered"]
        self.assertEqual(len(calls), 1)
        kws = {k.arg: k.value for k in calls[0].keywords}
        self.assertIn("hard", kws)
        self.assertIs(kws["hard"].value, False)

    def test_counter_reset_still_hard(self):
        src = (_BASE / "battery_health.py").read_text(encoding="utf-8")
        self.assertIn('self.mark_recovery("lifetime counter reset", now=s.timestamp)', src)


# ═════════════════════════════════════════════════════════════════════════
class TestCalibration(unittest.TestCase):  # BH-2320-10
    def test_latency_matches_const(self):
        tree = ast.parse((_BASE / "const.py").read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.Assign)
                    and getattr(n.targets[0], "id", "") == "SOH_CALIBRATION_MIN_TTL")
        call = node.value
        kw = {k.arg: k.value.value for k in call.keywords}
        seconds = kw.get("hours", 0) * 3600 + kw.get("minutes", 0) * 60 + kw.get("seconds", 0)
        self.assertEqual(seconds, bh.CALIBRATION_DETECTION_LATENCY_S)
        self.assertGreaterEqual(bh.BatteryHealthConfig().calibration_settle_s,
                                bh.CALIBRATION_DETECTION_LATENCY_S + 300)

    def test_start_edge_retro_excludes_last_hour_only(self):
        eng = bh.BatteryHealthEngine(bh.BatteryHealthConfig())
        now = 100 * DAY
        old = _seg(23.0, now - 2 * DAY)                    # ended long ago
        recent = _seg(23.0, now - 8 * HOUR - 1800)         # ended 30 min ago
        eng.segments.segments = [old, recent]
        pk_recent = _seg(7.8, now - 8 * HOUR - 1800)
        eng.pack_capacity.trackers[0].segments = [pk_recent]
        eng.efficiency._record_window(0.989, 1, now - 2 * DAY, None)
        eng.efficiency._record_window(0.989, 1, now - 600, None)
        eng.update(_sample(now, soc=100, power=0, calib=True))
        self.assertFalse(old.exclude_calibration)
        self.assertTrue(recent.exclude_calibration)
        self.assertTrue(pk_recent.exclude_calibration)
        self.assertEqual(list(eng.efficiency.window_end_ts), [now - 2 * DAY])

    def test_end_edge_settles_65_minutes(self):
        eng = bh.BatteryHealthEngine(bh.BatteryHealthConfig())
        eng.update(_sample(0, soc=100, power=0, calib=True))
        eng.update(_sample(60, soc=100, power=0, calib=False))
        self.assertEqual(eng._calib_settle_until, 60 + 3900)


# ═════════════════════════════════════════════════════════════════════════
class TestEffectiveAgeForecast(unittest.TestCase):  # BH-2320-03 / N3
    def _feed_constant(self, eng, t0, hours, cell, soc, power=0.0, step=300.0):
        packs = [_pack(cell, cell)] * 3
        t = t0
        while t <= t0 + hours * HOUR:
            eng.update(_sample(t, soc=soc, power=power, bms=cell + 9, packs=packs))
            t += step
        return t

    def test_constant_stress_equals_s_squared_times_age(self):
        cfg = bh.BatteryHealthConfig(battery_install_ts=0.0)
        eng = bh.BatteryHealthEngine(cfg)
        # cells 35 C -> S = 2.0 (Q10 2, SOC below knee); observe from day 100
        self._feed_constant(eng, 100 * DAY, 24 * 8, 35.0, 50.0)
        r = eng.report
        age_d = r.attributes["battery_age_days"]
        self.assertAlmostEqual(r.attributes["effective_age_days"], 4.0 * age_d, delta=0.5)
        self.assertTrue(r.attributes["effective_age_prior_frozen"])
        expected = 100 - 2.5 * math.sqrt(4.0 * age_d / 365.25)
        self.assertAlmostEqual(r.predicted_soh, round(expected, 1), delta=0.11)

    def test_forecast_never_rises_after_freeze(self):
        cfg = bh.BatteryHealthConfig(battery_install_ts=0.0)
        eng = bh.BatteryHealthEngine(cfg)
        t = self._feed_constant(eng, 200 * DAY, 24 * 8, 35.0, 95.0)   # hot summer
        last = eng.report.predicted_soh
        for week in range(6):                                          # cool winter
            t = self._feed_constant(eng, t, 24 * 7, 12.0, 40.0, step=900.0)
            now = eng.report.predicted_soh
            self.assertLessEqual(now, last)
            last = now

    def test_old_model_would_rise_in_winter(self):
        """Control: the pre-2.3.2.0 formula (90-day mean stress × √age)
        rises when the 90-day stress falls -- the defect being fixed."""
        age = 1.0
        summer = 100 - 2.5 * 3.0 * math.sqrt(age)
        winter = 100 - 2.5 * 0.8 * math.sqrt(age + 0.25)
        self.assertGreater(winter, summer)

    def test_cold_charge_hours(self):
        eng = bh.BatteryHealthEngine(bh.BatteryHealthConfig())
        self._feed_constant(eng, 0, 2, 8.0, 50.0, power=2000.0)       # cold + charging
        self._feed_constant(eng, 3 * HOUR, 2, 8.0, 50.0, power=-800.0)  # cold, discharging
        self._feed_constant(eng, 6 * HOUR, 2, 20.0, 50.0, power=2000.0)  # warm, charging
        self.assertAlmostEqual(eng.report.attributes["cold_charge_hours"], 2.0, delta=0.1)

    def test_stress_lifetime_persistence(self):
        eng = bh.BatteryHealthEngine(bh.BatteryHealthConfig())
        self._feed_constant(eng, 0, 10, 30.0, 90.0, power=2000.0)
        d = json.loads(json.dumps(eng.stress.to_dict()))
        st = bh.StressAccumulator(bh.BatteryHealthConfig())
        st.restore(d)
        for k in ("life_s2dt", "life_dt", "observation_start_ts", "cold_charge_s"):
            self.assertAlmostEqual(getattr(st, k), getattr(eng.stress, k))
        st.restore({"buckets": {}, "life_s2dt": "x", "life_dt": float("nan"),
                    "prior_mean_s2": -1})
        self.assertEqual((st.life_s2dt, st.life_dt, st.prior_mean_s2), (0.0, 0.0, None))


# ═════════════════════════════════════════════════════════════════════════
def _v3_store() -> dict:
    """A schema-3 store with the field store's SHAPE (synthetic values)."""
    def seg(i, imp, temp, dsoc=32.0):
        start = 1786700000.0 + i * 2 * DAY
        return {"start_ts": start, "end_ts": start + 9 * HOUR, "soc_start": 100.0,
                "soc_end": 100.0 - dsoc, "energy_kwh": imp * dsoc / 100,
                "implied_capacity_kwh": imp, "freshness": 1.0,
                "exclude_calibration": False, "gap_bridged": 20,
                "soc_midpoint": 100 - dsoc / 2, "charge_ceiling": 100.0,
                "avg_temp_c": temp}
    unit = [seg(i, 23.1 + 0.1 * math.sin(i), 36.5) for i in range(24)] + [
        seg(30, 24.6, 37.0, dsoc=11.0)]
    packs = [{"segments": [seg(i, 7.78 + 0.03 * math.cos(i + k), 26.0 + k) for i in range(24)],
              "reference_capacity": 7.84 + 0.08 * k, "reference_epochs": [
                  {"ts": 1.0, "value": 7.84 + 0.08 * k, "reason": "auto", "previous": None}]}
             for k in range(3)]
    return {
        "schema_version": 3, "first_seen_ts": 1786633440.0,
        "held_subscores": {"balance": [100.0, 1790837000.0],
                           "capacity": [99.8, 1790837000.0],
                           "efficiency": [98.1, 1790837000.0]},
        "learning_enabled": True, "settling_events": 3,
        "ceiling": {"value": 100.0, "rejected": 0, "debounced": 0},
        "segments": {"segments": unit, "reference_capacity": 32.81,
                     "reference_captured_ts": 1.0, "reference_epochs": [
                         {"ts": 1.0, "value": 32.81, "reason": "auto", "previous": None}],
                     "discarded": 14, "gap_bridged": 1320},
        "pack_capacity": {"trackers": packs, "slot_labels": ["u1p1", "u1p2", "u1p3"]},
        "efficiency": {"anchor": None, "windows": TestEfficiency.FIELD_WINDOWS,
                       "window_tiers": [1] * 21, "baseline": 0.98958,
                       "baseline_tier": 1, "baseline_pool": [1.0025, 0.9714, 0.9896],
                       "baseline_epochs": [{"ts": 1.0, "value": 0.98958, "tier": 1,
                                            "reason": "auto: first 3 windows"}],
                       "last_ceiling": 100.0},
        "balance": {"scores": [100.0] * 20, "raw_dv": [0.1] * 20, "raw_dt": [2.2] * 20,
                    "raw_dt_min": [2.1] * 20,
                    "thermal_rise": [[1786634240.0 + 90 * i, [3.6, 5.8, 4.7]] for i in range(20)],
                    "baseline_rise": None, "sample_soc": [100.0] * 20,
                    "included": [1, 2, 3], "excluded": [], "baseline_dv": 0.0,
                    "baseline_dt": 2.2, "baseline_captured_ts": 1786634240.0,
                    "baseline_epochs": [{"ts": 1.0, "dv": 0.0, "dt": 2.2, "reason": "auto"}],
                    "pool_dv": [], "pool_dt": [], "last_ceiling": 100.0},
        "stress": {"buckets": {"496287": [3600.0 * 3.3, 3600.0]}},
        "charge_counter": {"last": 2289.57, "offset": 0.0, "resets": 0},
        "discharge_counter": {"last": 2249.68, "offset": 0.0, "resets": 0},
    }


class TestMigration(unittest.TestCase):  # BH-2320-12
    def _restore(self, data):
        eng = bh.BatteryHealthEngine(bh.BatteryHealthConfig(), pack_count=3,
                                     pack_slot_labels=["u1p1", "u1p2", "u1p3"])
        eng.restore(copy.deepcopy(data))
        return eng

    def test_schema_and_registered_migration(self):
        self.assertEqual(bh.SCHEMA_VERSION, 4)
        self.assertIn(3, bh._SCHEMA_MIGRATIONS)

    def test_v3_store_upgraded_with_history_kept(self):
        eng = self._restore(_v3_store())
        self.assertIsNone(eng.last_schema_reset_ts)            # not a fresh start
        self.assertEqual(len(eng.segments.segments), 25)
        unit = eng.segments
        self.assertAlmostEqual(unit.reference_capacity_kwh, 23.1, delta=0.15)
        self.assertEqual(unit.reference_epochs[0]["value"], 32.81)        # kept
        self.assertTrue(unit.reference_epochs[-1]["reason"].startswith("2.3.2.0 upgrade"))
        for tr in eng.pack_capacity.trackers:
            self.assertAlmostEqual(tr.reference_capacity_kwh, 7.78, delta=0.05)
            self.assertEqual(len(tr.reference_epochs), 2)
        self.assertEqual(eng.stress._buckets, {})              # BMS-biased, dropped
        self.assertEqual(len(eng.balance.raw_dv), 0)           # per-tick buffers dropped
        self.assertIsNone(eng.balance.baseline_dv)
        self.assertEqual(len(eng.balance.baseline_epochs), 2)
        self.assertAlmostEqual(eng.efficiency.baseline, 0.98935, delta=1e-4)
        self.assertTrue(eng.dirty)
        r = eng._evaluate(1790837286.0)
        self.assertAlmostEqual(r.soh_capacity, 100.0, delta=0.05)
        self.assertEqual(r.soh_efficiency, 100.0)
        self.assertIn("balance", r.attributes["held_terms"])

    def test_upgrade_runs_once(self):
        eng = self._restore(_v3_store())
        saved = json.loads(json.dumps(eng.to_dict()))
        self.assertEqual(saved["schema_version"], 4)
        self.assertNotIn("upgrade_2320_pending", saved)
        eng2 = self._restore(saved)
        self.assertEqual(eng2.segments.reference_epochs, eng.segments.reference_epochs)
        self.assertEqual(eng2.efficiency.baseline_epochs, eng.efficiency.baseline_epochs)
        self.assertFalse(eng2.dirty)

    def test_upgrade_failure_keeps_restored_state(self):
        eng = bh.BatteryHealthEngine(bh.BatteryHealthConfig(), pack_count=3,
                                     pack_slot_labels=["u1p1", "u1p2", "u1p3"])

        def boom(now):
            raise RuntimeError("x")
        eng._apply_upgrade_2320 = boom
        eng.restore(copy.deepcopy(_v3_store()))
        self.assertEqual(len(eng.segments.segments), 25)
        self.assertEqual(eng.segments.reference_capacity_kwh, 32.81)
        self.assertTrue(eng.dirty)

    def test_thin_data_clears_reference_for_recapture(self):
        data = _v3_store()
        data["segments"]["segments"] = data["segments"]["segments"][:5]
        eng = self._restore(data)
        self.assertIsNone(eng.segments.reference_capacity_kwh)
        self.assertEqual(eng.segments.reference_epochs[-1]["previous"], 32.81)

    @unittest.skipUnless(os.environ.get("HUAWEI_BH_FIELD_STORE"),
                         "field store not provided (confidential, not shipped)")
    def test_field_store_replay(self):  # pragma: no cover - operator data
        raw = json.loads(pathlib.Path(os.environ["HUAWEI_BH_FIELD_STORE"]).read_text())
        eng = self._restore(raw["data"])
        r = eng._evaluate(raw["data"]["held_subscores"]["capacity"][1])
        self.assertAlmostEqual(r.attributes["estimated_capacity_kwh"], 23.1, delta=0.3)
        self.assertEqual(r.attributes["pack_capacity_soh_percent"], [100.0, 100.0, 100.0])


# ═════════════════════════════════════════════════════════════════════════
class TestWiring(unittest.TestCase):
    def test_entities_expose_new_attributes(self):
        src = (_BASE / "battery_health_entities.py").read_text(encoding="utf-8")
        for key in ("effective_age_days", "effective_age_prior_frozen",
                    "temperature_source", "segments_used", "balance_last_sample_ts",
                    "efficiency_loss_pct", "efficiency_deadband_pct",
                    "efficiency_rejected_outlier_windows"):
            self.assertIn(f'"{key}"', src, key)
        ast.parse(src)

    def test_options_default_depth_15(self):
        src = (_BASE / "config_flow.py").read_text(encoding="utf-8")
        self.assertIn("options.get(CONF_BH_MIN_SEGMENT_DELTA_SOC, 15.0)", src)
        self.assertNotIn("options.get(CONF_BH_MIN_SEGMENT_DELTA_SOC, 10.0)", src)

    def test_engine_module_stays_ha_free(self):
        tree = ast.parse((_BASE / "battery_health.py").read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                mod = getattr(n, "module", None) or n.names[0].name
                self.assertFalse(str(mod).startswith("homeassistant"), mod)

    def test_version(self):
        manifest = json.loads((_BASE / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], "2.3.2.0")


if __name__ == "__main__":
    unittest.main()
