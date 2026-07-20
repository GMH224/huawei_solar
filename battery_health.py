"""Battery Health Index (BHI) v2 engine — pure computation core.

Design summary (see BATTERY_HEALTH.md for the full rationale):

    BHI = w_cap·SOH_cap + w_eff·SOH_eff + w_bal·SOH_bal   (weights renormalized
                                                           over available terms)

  *Measured health only.*  Stress exposure and warranty bookkeeping are
  deliberately kept OUT of the composite and exposed as separate values:

    - SOH_cap  — capacity retention from harvested discharge segments
                 (ΔSOC²·freshness weighting, SOC-correction guard,
                 Huawei-SOH-calibration "golden" anchors)
    - SOH_eff  — round-trip efficiency drift between full-charge anchors
                 (replaces voltage-sag internal-resistance estimation, which
                 is invalid behind the LUNA2000-S1 Module+ per-module
                 optimizers)
    - SOH_bal  — pack voltage/temperature balance at rest near full SOC
    - stress   — Q10 × f(SOC) time-weighted exposure ratio (model input,
                 not a health measurement)
    - forecast — √t calendar + throughput cycle model → predicted SOH and
                 measured-vs-model divergence (the real early-warning signal)
    - EFC / warranty consumption — bookkeeping sensors

This module has **no Home Assistant imports** and **no Modbus writes**.  It is
a pure function of the samples fed into it, and every rolling window is
serializable via ``to_dict()`` / ``from_dict()`` for Store persistence.

Safety property (audit-relevant): this subsystem is strictly read-only with
respect to the inverter/BMS.  It observes registers already polled by the
energy-storage coordinator and never issues register writes.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any

_LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# ── Plausibility bounds (samples outside are DISCARDED, never clipped) ───────
SOC_MIN, SOC_MAX = 0.0, 100.0
PACK_VOLTAGE_MIN, PACK_VOLTAGE_MAX = 10.0, 800.0      # wide: covers LV & HV packs
TEMP_MIN_C, TEMP_MAX_C = -20.0, 60.0
POWER_LIMIT_W = 15_750.0                              # 1.5 × 10.5 kW hw limit
COUNTER_RESET_TOLERANCE_KWH = 1.0                     # decrease > this = reset

SECONDS_PER_DAY = 86_400.0


# ═════════════════════════════════════════════════════════════════════════════
# Configuration
# ═════════════════════════════════════════════════════════════════════════════
@dataclass
class BatteryHealthConfig:
    """All tunable constants.  Values are starting points, not fitted truths."""

    # System reference
    rated_capacity_kwh: float = 20.7          # LUNA2000-21-S1 usable (BOL)
    warranty_throughput_kwh: float = 28_840.0  # CH/EEA: 28.84 MWh to 60%

    # Composite weights (auto-normalized; measured health terms only)
    weight_capacity: float = 0.60
    weight_efficiency: float = 0.20
    weight_balance: float = 0.20

    # SOH_cap — segment harvesting
    capacity_window_days: float = 90.0
    min_segment_delta_soc: float = 10.0
    segment_rest_power_w: float = 50.0        # |power| below this = idle
    soc_backstep_tolerance: float = 0.11      # allowed upward SOC jitter (%)
    implied_capacity_min_kwh: float = 8.0     # consistency band — outside =
    implied_capacity_max_kwh: float = 35.0    #   SOC-correction / glitch guard
    full_charge_soc: float = 97.0             # "full" for freshness/anchors
    freshness_tau_kwh: float = 40.0           # coulomb-drift decay constant
    golden_weight_boost: float = 4.0          # Huawei SOH-calibration segments
    trim_fraction: float = 0.10               # weighted trimmed mean

    # SOH_eff — round-trip efficiency drift
    eff_min_window_charge_kwh: float = 30.0   # min charge between anchors
    eff_anchor_rest_power_w: float = 100.0
    eff_valid_min: float = 0.50               # plausibility band for η
    eff_valid_max: float = 1.05
    eff_baseline_windows: int = 3             # first N windows → baseline
    eff_rolling_windows: int = 6              # last N windows → current
    eff_pts_per_pct_loss: float = 8.0         # SOH_eff slope

    # SOH_bal — pack balance
    balance_min_soc: float = 95.0
    balance_rest_power_w: float = 50.0
    balance_dv_full_score: float = 0.05       # ΔV ≤ this → 100
    balance_dv_zero_score: float = 0.50       # ΔV ≥ this → 0
    balance_dt_full_score: float = 1.0        # ΔT ≤ this → 100
    balance_dt_zero_score: float = 8.0        # ΔT ≥ this → 0
    balance_sample_count: int = 20            # median over last N samples

    # Stress accumulator (model input — NOT part of BHI)
    q10: float = 2.0
    stress_ref_temp_c: float = 25.0
    stress_soc_knee: float = 80.0
    stress_soc_max_factor: float = 2.5
    stress_window_days: float = 90.0
    stress_max_gap_s: float = 900.0           # gaps > this excluded from Δt

    # Aging forecast (heuristic model — documented as such)
    forecast_calendar_pct_per_sqrt_year: float = 2.5   # at stress_ratio = 1.0
    forecast_cycle_pct_per_efc: float = 0.004          # ≈ 20% over 5000 EFC

    # Confidence
    confidence_min_segments: int = 5
    stale_after_days: float = 60.0

    def normalized_weights(self) -> tuple[float, float, float]:
        """Return (w_cap, w_eff, w_bal) normalized to sum to 1.0."""
        w = (self.weight_capacity, self.weight_efficiency, self.weight_balance)
        total = sum(w)
        if total <= 0:
            return (0.60, 0.20, 0.20)
        return (w[0] / total, w[1] / total, w[2] / total)


def clip(value: float, lo: float, hi: float) -> float:
    """Clamp *value* to [lo, hi]."""
    return max(lo, min(hi, value))


# ═════════════════════════════════════════════════════════════════════════════
# Input sample + validation
# ═════════════════════════════════════════════════════════════════════════════
@dataclass
class PackSample:
    """One battery pack's reading for a poll tick."""

    voltage: float | None = None
    temp_max: float | None = None
    temp_min: float | None = None
    online: bool = False


@dataclass
class HealthSample:
    """One validated poll tick fed to the engine.

    ``power_w`` follows the Huawei register convention:
    positive = charging, negative = discharging.
    """

    timestamp: float
    soc: float | None = None
    power_w: float | None = None
    battery_temp_c: float | None = None
    lifetime_charge_kwh: float | None = None
    lifetime_discharge_kwh: float | None = None
    packs: list[PackSample] = field(default_factory=list)
    soh_calibration_active: bool = False


def _valid_or_none(
    value: Any, lo: float, hi: float, name: str
) -> float | None:
    """Return float(value) if within [lo, hi], else None (discard, don't clip)."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or v < lo or v > hi:
        _LOGGER.debug("battery_health: discarding implausible %s=%r", name, value)
        return None
    return v


def validate_sample(raw: HealthSample) -> HealthSample:
    """Apply plausibility bounds field-by-field.  Bad fields become None;
    the rest of the sample stays usable (per-field discard, per spec §9)."""
    out = HealthSample(timestamp=raw.timestamp)
    out.soc = _valid_or_none(raw.soc, SOC_MIN, SOC_MAX, "soc")
    out.power_w = _valid_or_none(raw.power_w, -POWER_LIMIT_W, POWER_LIMIT_W, "power")
    out.battery_temp_c = _valid_or_none(
        raw.battery_temp_c, TEMP_MIN_C, TEMP_MAX_C, "battery_temp"
    )
    # Lifetime counters: only lower-bounded here; reset detection is separate.
    out.lifetime_charge_kwh = _valid_or_none(
        raw.lifetime_charge_kwh, 0.0, 1e9, "lifetime_charge"
    )
    out.lifetime_discharge_kwh = _valid_or_none(
        raw.lifetime_discharge_kwh, 0.0, 1e9, "lifetime_discharge"
    )
    out.soh_calibration_active = bool(raw.soh_calibration_active)
    for pack in raw.packs:
        out.packs.append(
            PackSample(
                voltage=_valid_or_none(
                    pack.voltage, PACK_VOLTAGE_MIN, PACK_VOLTAGE_MAX, "pack_v"
                ),
                temp_max=_valid_or_none(pack.temp_max, TEMP_MIN_C, TEMP_MAX_C, "pack_tmax"),
                temp_min=_valid_or_none(pack.temp_min, TEMP_MIN_C, TEMP_MAX_C, "pack_tmin"),
                online=bool(pack.online),
            )
        )
    return out


# ═════════════════════════════════════════════════════════════════════════════
# Lifetime counter reset detection
# ═════════════════════════════════════════════════════════════════════════════
class CounterMonitor:
    """Track a monotonically increasing lifetime counter.

    Detects resets (firmware update, BMS replacement, rollover): a decrease
    larger than COUNTER_RESET_TOLERANCE_KWH is logged as a reset event and a
    new offset is established so deltas stay correct and never go negative.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._last: float | None = None
        self._offset = 0.0
        self.reset_count = 0

    @property
    def last_raw(self) -> float | None:
        return self._last

    @property
    def value(self) -> float | None:
        """Current continuous (offset-corrected) value without feeding."""
        return None if self._last is None else self._last + self._offset

    def feed(self, raw: float | None) -> float | None:
        """Return the continuous (offset-corrected) counter value, or None."""
        if raw is None:
            return None if self._last is None else self._last + self._offset
        if self._last is not None and raw < self._last - COUNTER_RESET_TOLERANCE_KWH:
            # Counter reset — carry forward the old total as an offset.
            self._offset += self._last
            self.reset_count += 1
            _LOGGER.warning(
                "battery_health: %s counter reset detected (%.2f → %.2f kWh); "
                "treating as reset event #%d, not negative energy",
                self._name, self._last, raw, self.reset_count,
            )
        self._last = raw
        return raw + self._offset

    def to_dict(self) -> dict[str, Any]:
        return {"last": self._last, "offset": self._offset, "resets": self.reset_count}

    def restore(self, data: dict[str, Any]) -> None:
        self._last = data.get("last")
        self._offset = float(data.get("offset", 0.0))
        self.reset_count = int(data.get("resets", 0))


# ═════════════════════════════════════════════════════════════════════════════
# SOH_cap — discharge segment harvesting
# ═════════════════════════════════════════════════════════════════════════════
@dataclass
class DischargeSegment:
    """One completed, qualifying discharge segment."""

    start_ts: float
    end_ts: float
    soc_start: float
    soc_end: float
    energy_kwh: float
    implied_capacity_kwh: float
    freshness: float          # exp(-throughput_since_full/τ) at segment start
    golden: bool              # Huawei SOH calibration ran during this segment

    @property
    def delta_soc(self) -> float:
        return self.soc_start - self.soc_end

    def weight(self, cfg: BatteryHealthConfig) -> float:
        w = self.delta_soc ** 2 * self.freshness
        if self.golden:
            w *= cfg.golden_weight_boost
        return w

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DischargeSegment:
        return cls(**{k: d[k] for k in (
            "start_ts", "end_ts", "soc_start", "soc_end", "energy_kwh",
            "implied_capacity_kwh", "freshness", "golden",
        )})


class SegmentTracker:
    """Continuous discharge-segment detection with v2 guards.

    v2 improvements over the original spec:
      * SOC-correction guard — implied capacity outside a plausibility band
        means the BMS snapped its SOC mid-segment → discard (§Finding 3).
      * Freshness weighting — segments starting shortly after a 100% charge
        (fresh coulomb-count anchor) weigh more (§Finding 3).
      * Golden segments — a Huawei SOH calibration cycle is the BMS's own
        controlled full-cycle measurement → boosted weight (§Finding 1).
      * Robust aggregation — weighted trimmed mean + spread, not a plain
        weighted mean a single outlier can drag.
    """

    def __init__(self, cfg: BatteryHealthConfig) -> None:
        self._cfg = cfg
        self.segments: list[DischargeSegment] = []
        # Active segment state
        self._active = False
        self._start_ts = 0.0
        self._start_soc = 0.0
        self._start_discharge_kwh = 0.0
        self._last_soc = 0.0
        self._seg_calibration_seen = False
        self._seg_freshness = 1.0
        # Freshness bookkeeping
        self._throughput_since_full_kwh = 0.0
        self._last_discharge_kwh: float | None = None
        self.last_segment_ts: float | None = None
        self.discarded_segments = 0
        # v1.1.6: aggregation cache — soh_capacity() is O(n log n); recompute
        # only when the segment set changes, not on every 30 s tick.
        self._agg_cache: tuple[float | None, dict[str, Any]] | None = None

    # ── feed ────────────────────────────────────────────────────────────────
    def feed(self, s: HealthSample) -> DischargeSegment | None:
        """Process one sample; return a completed segment if one just closed."""
        cfg = self._cfg
        soc, power = s.soc, s.power_w
        discharge = s.lifetime_discharge_kwh

        # Freshness bookkeeping (independent of segment state)
        if discharge is not None:
            if self._last_discharge_kwh is not None:
                delta = discharge - self._last_discharge_kwh
                if delta > 0:
                    self._throughput_since_full_kwh += delta
            self._last_discharge_kwh = discharge
        if soc is not None and soc >= cfg.full_charge_soc:
            self._throughput_since_full_kwh = 0.0

        if soc is None or power is None or discharge is None:
            # Missing critical field: per spec §9 do not guess — discard any
            # in-progress segment and skip this tick for segment detection.
            if self._active:
                self._discard("missing field / read failure mid-segment")
            return None

        discharging = power < -cfg.segment_rest_power_w

        if not self._active:
            if discharging:
                self._begin(s, soc, discharge)
            return None

        # Active segment ------------------------------------------------------
        if s.soh_calibration_active:
            self._seg_calibration_seen = True

        if soc > self._last_soc + cfg.soc_backstep_tolerance:
            # SOC rose: charging blip or SOC correction → close at last point.
            return self._close(self._last_soc, discharge, s.timestamp)
        self._last_soc = min(self._last_soc, soc)

        if not discharging:
            return self._close(soc, discharge, s.timestamp)
        return None

    def mark_gap(self) -> None:
        """A data gap occurred (coordinator failure): discard active segment."""
        if self._active:
            self._discard("data gap")

    # ── internals ───────────────────────────────────────────────────────────
    def _begin(self, s: HealthSample, soc: float, discharge: float) -> None:
        self._active = True
        self._start_ts = s.timestamp
        self._start_soc = soc
        self._last_soc = soc
        self._start_discharge_kwh = discharge
        self._seg_calibration_seen = bool(s.soh_calibration_active)
        self._seg_freshness = math.exp(
            -self._throughput_since_full_kwh / self._cfg.freshness_tau_kwh
        )

    def _discard(self, reason: str) -> None:
        _LOGGER.debug("battery_health: discarding segment (%s)", reason)
        self._active = False
        self.discarded_segments += 1
        self._agg_cache = None      # discard counter appears in attributes

    def _close(
        self, end_soc: float, end_discharge_kwh: float, end_ts: float
    ) -> DischargeSegment | None:
        cfg = self._cfg
        self._active = False
        delta_soc = self._start_soc - end_soc
        energy = end_discharge_kwh - self._start_discharge_kwh

        if delta_soc < cfg.min_segment_delta_soc:
            return None  # too shallow — noise, silently drop
        if energy <= 0:
            self._discard("non-positive energy")
            return None

        implied = energy / (delta_soc / 100.0)
        if not (cfg.implied_capacity_min_kwh <= implied <= cfg.implied_capacity_max_kwh):
            # SOC-correction event or counter glitch mid-segment.
            self._discard(
                f"implied capacity {implied:.1f} kWh outside plausibility band "
                "(likely BMS SOC correction)"
            )
            return None

        seg = DischargeSegment(
            start_ts=self._start_ts,
            end_ts=end_ts,
            soc_start=self._start_soc,
            soc_end=end_soc,
            energy_kwh=energy,
            implied_capacity_kwh=implied,
            freshness=self._seg_freshness,
            golden=self._seg_calibration_seen,
        )
        self.segments.append(seg)
        self.last_segment_ts = self._start_ts
        self._agg_cache = None
        return seg

    def prune(self, now: float) -> None:
        cutoff = now - self._cfg.capacity_window_days * SECONDS_PER_DAY
        # Fast path (v1.1.6): segments is start_ts-ordered (append-only), so
        # the first element is the oldest — no rebuild unless it expired.
        if not self.segments or self.segments[0].start_ts >= cutoff:
            return
        self.segments = [s for s in self.segments if s.start_ts >= cutoff]
        self._agg_cache = None

    # ── aggregation ─────────────────────────────────────────────────────────
    def soh_capacity(self) -> tuple[float | None, dict[str, Any]]:
        """Weighted trimmed-mean SOH_cap plus diagnostic attributes.

        Cached (v1.1.6): recomputed only when the segment set or discard
        counter changed since the last call — the aggregation is O(n log n)
        and this runs on every coordinator tick.
        """
        if self._agg_cache is not None:
            soh, attrs = self._agg_cache
            return soh, dict(attrs)
        cfg = self._cfg
        segs = self.segments
        attrs: dict[str, Any] = {
            "segment_count": len(segs),
            "golden_segment_count": sum(1 for s in segs if s.golden),
            "discarded_segment_count": self.discarded_segments,
        }
        if not segs:
            self._agg_cache = (None, dict(attrs))
            return None, attrs

        weighted = sorted(
            ((s.implied_capacity_kwh, s.weight(cfg)) for s in segs),
            key=lambda t: t[0],
        )
        total_w = sum(w for _, w in weighted)
        if total_w <= 0:
            self._agg_cache = (None, dict(attrs))
            return None, attrs

        # Trim `trim_fraction` of total weight from each tail (only when we
        # have enough segments that trimming can't erase everything).
        if len(weighted) >= 5:
            trim = total_w * cfg.trim_fraction
            kept: list[tuple[float, float]] = []
            low_budget, high_budget = trim, trim
            for cap, w in weighted:
                if low_budget > 0:
                    cut = min(w, low_budget)
                    low_budget -= cut
                    w -= cut
                if w > 0:
                    kept.append((cap, w))
            kept_rev: list[tuple[float, float]] = []
            for cap, w in reversed(kept):
                if high_budget > 0:
                    cut = min(w, high_budget)
                    high_budget -= cut
                    w -= cut
                if w > 0:
                    kept_rev.append((cap, w))
            weighted = list(reversed(kept_rev)) or weighted
            total_w = sum(w for _, w in weighted)

        mean_cap = sum(c * w for c, w in weighted) / total_w
        var = sum(w * (c - mean_cap) ** 2 for c, w in weighted) / total_w
        attrs["estimated_capacity_kwh"] = round(mean_cap, 2)
        attrs["capacity_spread_kwh"] = round(math.sqrt(var), 2)
        soh = clip(mean_cap / cfg.rated_capacity_kwh * 100.0, 0.0, 100.0)
        self._agg_cache = (soh, dict(attrs))
        return soh, attrs

    # ── persistence ─────────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return {
            "segments": [s.to_dict() for s in self.segments],
            "throughput_since_full": self._throughput_since_full_kwh,
            "last_discharge": self._last_discharge_kwh,
            "last_segment_ts": self.last_segment_ts,
            "discarded": self.discarded_segments,
        }

    def restore(self, data: dict[str, Any]) -> None:
        self.segments = [
            DischargeSegment.from_dict(d) for d in data.get("segments", [])
        ]
        self._throughput_since_full_kwh = float(data.get("throughput_since_full", 0.0))
        self._last_discharge_kwh = data.get("last_discharge")
        self.last_segment_ts = data.get("last_segment_ts")
        self.discarded_segments = int(data.get("discarded", 0))
        self._agg_cache = None
        # Never resume a half-open segment across a restart (spec §8).
        self._active = False


# ═════════════════════════════════════════════════════════════════════════════
# SOH_eff — round-trip efficiency drift between full-charge anchors
# ═════════════════════════════════════════════════════════════════════════════
class EfficiencyTracker:
    """Round-trip efficiency between successive full-charge anchor states.

    η = Δ(lifetime discharge) / Δ(lifetime charge) between two ticks at which
    the battery is full and at rest.  Rising I²R losses (growing internal
    resistance) show up as declining η — the physics SOH_res tried to reach,
    measured through counters the Module+ optimizers cannot distort.
    """

    def __init__(self, cfg: BatteryHealthConfig) -> None:
        self._cfg = cfg
        self._anchor: tuple[float, float, float] | None = None  # ts, chg, dis
        self.windows: deque[float] = deque(maxlen=64)           # η history
        self.baseline: float | None = None
        self._baseline_pool: list[float] = []

    def feed(self, s: HealthSample) -> None:
        cfg = self._cfg
        if (
            s.soc is None
            or s.power_w is None
            or s.lifetime_charge_kwh is None
            or s.lifetime_discharge_kwh is None
        ):
            return
        if s.soc < cfg.full_charge_soc or abs(s.power_w) > cfg.eff_anchor_rest_power_w:
            return

        # Battery is full & at rest → candidate anchor.
        if self._anchor is None:
            self._anchor = (s.timestamp, s.lifetime_charge_kwh, s.lifetime_discharge_kwh)
            return

        _, chg0, dis0 = self._anchor
        d_charge = s.lifetime_charge_kwh - chg0
        d_discharge = s.lifetime_discharge_kwh - dis0
        if d_charge < cfg.eff_min_window_charge_kwh:
            # Same full-dwell period (or too little throughput): slide anchor.
            self._anchor = (s.timestamp, chg0, dis0)
            return

        eta = d_discharge / d_charge
        self._anchor = (s.timestamp, s.lifetime_charge_kwh, s.lifetime_discharge_kwh)
        if not (cfg.eff_valid_min <= eta <= cfg.eff_valid_max):
            _LOGGER.debug("battery_health: discarding implausible η=%.3f", eta)
            return
        self.windows.append(eta)
        if self.baseline is None:
            self._baseline_pool.append(eta)
            if len(self._baseline_pool) >= cfg.eff_baseline_windows:
                self.baseline = _median(self._baseline_pool)
                _LOGGER.info(
                    "battery_health: efficiency baseline captured: η=%.3f "
                    "(median of %d windows)", self.baseline, len(self._baseline_pool),
                )

    def invalidate_anchor(self) -> None:
        """Counter reset / data gap: current window can't be trusted."""
        self._anchor = None

    def reset_baseline(self) -> None:
        """Manual baseline re-capture (button/service)."""
        self.baseline = None
        self._baseline_pool.clear()

    def soh_efficiency(self) -> tuple[float | None, dict[str, Any]]:
        cfg = self._cfg
        attrs: dict[str, Any] = {
            "efficiency_baseline": self.baseline,
            "efficiency_window_count": len(self.windows),
        }
        if self.baseline is None or not self.windows:
            return None, attrs
        recent = list(self.windows)[-cfg.eff_rolling_windows:]
        current = _median(recent)
        attrs["efficiency_current"] = round(current, 4)
        loss_pct_points = max(0.0, (self.baseline - current) * 100.0)
        soh = clip(100.0 - loss_pct_points * cfg.eff_pts_per_pct_loss, 0.0, 100.0)
        return soh, attrs

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchor": list(self._anchor) if self._anchor else None,
            "windows": list(self.windows),
            "baseline": self.baseline,
            "baseline_pool": list(self._baseline_pool),
        }

    def restore(self, data: dict[str, Any]) -> None:
        anchor = data.get("anchor")
        self._anchor = tuple(anchor) if anchor else None
        self.windows = deque(data.get("windows", []), maxlen=64)
        self.baseline = data.get("baseline")
        self._baseline_pool = list(data.get("baseline_pool", []))


def _median(values: list[float]) -> float:
    v = sorted(values)
    n = len(v)
    mid = n // 2
    return v[mid] if n % 2 else (v[mid - 1] + v[mid]) / 2.0


# ═════════════════════════════════════════════════════════════════════════════
# SOH_bal — pack balance at rest near full SOC
# ═════════════════════════════════════════════════════════════════════════════
class BalanceTracker:
    """ΔV / ΔT spread across online packs, sampled at rest & high SOC."""

    def __init__(self, cfg: BatteryHealthConfig) -> None:
        self._cfg = cfg
        self.scores: deque[float] = deque(maxlen=cfg.balance_sample_count)
        self.last_included: list[int] = []
        self.last_excluded: list[int] = []
        self._median_cache: float | None = None     # v1.1.6

    def feed(self, s: HealthSample) -> None:
        cfg = self._cfg
        if s.soc is None or s.power_w is None:
            return
        if s.soc < cfg.balance_min_soc or abs(s.power_w) > cfg.balance_rest_power_w:
            return

        included, excluded = [], []
        volts, temps = [], []
        for idx, pack in enumerate(s.packs, start=1):
            if pack.online and pack.voltage is not None and pack.temp_max is not None:
                included.append(idx)
                volts.append(pack.voltage)
                temps.append(pack.temp_max)
            else:
                excluded.append(idx)
        # A pack offline mid-poll is excluded rather than compared against a
        # stale/zero reading (spec §9).
        if len(included) < 2:
            return
        self.last_included, self.last_excluded = included, excluded

        dv = max(volts) - min(volts)
        dt = max(temps) - min(temps)
        score_v = 100.0 - clip(
            (dv - cfg.balance_dv_full_score)
            / (cfg.balance_dv_zero_score - cfg.balance_dv_full_score) * 100.0,
            0.0, 100.0,
        )
        score_t = 100.0 - clip(
            (dt - cfg.balance_dt_full_score)
            / (cfg.balance_dt_zero_score - cfg.balance_dt_full_score) * 100.0,
            0.0, 100.0,
        )
        self.scores.append((score_v + score_t) / 2.0)
        self._median_cache = None

    def soh_balance(self) -> tuple[float | None, dict[str, Any]]:
        attrs = {
            "balance_sample_count": len(self.scores),
            "packs_included": self.last_included,
            "packs_excluded": self.last_excluded,
        }
        if not self.scores:
            return None, attrs
        if self._median_cache is None:
            self._median_cache = _median(list(self.scores))
        return self._median_cache, attrs

    def to_dict(self) -> dict[str, Any]:
        return {
            "scores": list(self.scores),
            "included": self.last_included,
            "excluded": self.last_excluded,
        }

    def restore(self, data: dict[str, Any]) -> None:
        self.scores = deque(
            data.get("scores", []), maxlen=self._cfg.balance_sample_count
        )
        self._median_cache = None
        self.last_included = list(data.get("included", []))
        self.last_excluded = list(data.get("excluded", []))


# ═════════════════════════════════════════════════════════════════════════════
# Stress accumulator (exposure model — separate from BHI)
# ═════════════════════════════════════════════════════════════════════════════
class StressAccumulator:
    """Q10 × f(SOC) time-weighted rolling exposure, bucketed hourly so the
    90-day window persists compactly (≤ ~2160 buckets, not 130k raw ticks)."""

    def __init__(self, cfg: BatteryHealthConfig) -> None:
        self._cfg = cfg
        # buckets: hour_epoch → [Σ stress·Δt, Σ Δt]
        self._buckets: dict[int, list[float]] = {}
        self._last_ts: float | None = None
        # v1.1.6: running totals — stress_ratio() must be O(1), not a sweep
        # of ~2 160 buckets on every 30 s tick.
        self._total_sdt = 0.0
        self._total_dt = 0.0
        self._oldest_bucket: int | None = None

    def feed(self, s: HealthSample) -> None:
        cfg = self._cfg
        if s.battery_temp_c is None or s.soc is None:
            # Read failure: skip tick; do NOT treat the gap as zero stress.
            self._last_ts = None if self._last_ts is None else self._last_ts
            return
        if self._last_ts is None:
            self._last_ts = s.timestamp
            return
        dt = s.timestamp - self._last_ts
        self._last_ts = s.timestamp
        if dt <= 0 or dt > cfg.stress_max_gap_s:
            # Long outage: exclude the gap's Δt from the denominator entirely
            # (otherwise outages silently inflate the score — spec §9).
            return

        soc_factor = 1.0
        if s.soc >= cfg.stress_soc_knee:
            span = 100.0 - cfg.stress_soc_knee
            soc_factor = 1.0 + (s.soc - cfg.stress_soc_knee) / span * (
                cfg.stress_soc_max_factor - 1.0
            )
        stress = (
            cfg.q10 ** ((s.battery_temp_c - cfg.stress_ref_temp_c) / 10.0) * soc_factor
        )
        bucket = int(s.timestamp // 3600)
        acc = self._buckets.setdefault(bucket, [0.0, 0.0])
        acc[0] += stress * dt
        acc[1] += dt
        self._total_sdt += stress * dt
        self._total_dt += dt
        if self._oldest_bucket is None or bucket < self._oldest_bucket:
            self._oldest_bucket = bucket

    def mark_gap(self) -> None:
        self._last_ts = None

    def prune(self, now: float) -> None:
        cutoff = int((now - self._cfg.stress_window_days * SECONDS_PER_DAY) // 3600)
        # Fast path: nothing to prune unless the oldest bucket expired.
        if self._oldest_bucket is None or self._oldest_bucket >= cutoff:
            return
        kept: dict[int, list[float]] = {}
        for k, v in self._buckets.items():
            if k >= cutoff:
                kept[k] = v
            else:
                self._total_sdt -= v[0]
                self._total_dt -= v[1]
        self._buckets = kept
        self._oldest_bucket = min(kept) if kept else None
        if not kept:
            # Avoid float drift accumulating in an empty window.
            self._total_sdt = 0.0
            self._total_dt = 0.0

    def stress_ratio(self) -> float | None:
        if self._total_dt <= 0:
            return None
        return self._total_sdt / self._total_dt

    def to_dict(self) -> dict[str, Any]:
        return {"buckets": {str(k): v for k, v in self._buckets.items()}}

    def restore(self, data: dict[str, Any]) -> None:
        self._buckets = {
            int(k): [float(v[0]), float(v[1])]
            for k, v in data.get("buckets", {}).items()
        }
        self._total_sdt = sum(v[0] for v in self._buckets.values())
        self._total_dt = sum(v[1] for v in self._buckets.values())
        self._oldest_bucket = min(self._buckets) if self._buckets else None
        self._last_ts = None


# ═════════════════════════════════════════════════════════════════════════════
# Engine
# ═════════════════════════════════════════════════════════════════════════════
@dataclass
class HealthReport:
    """Complete output of one engine evaluation."""

    bhi: float | None = None
    confidence: str = "low"                    # low / normal / stale
    soh_capacity: float | None = None
    soh_efficiency: float | None = None
    soh_balance: float | None = None
    stress_index: float | None = None          # 100/stress_ratio, informational
    stress_ratio: float | None = None
    predicted_soh: float | None = None
    health_divergence: float | None = None     # measured − predicted
    efc: float | None = None
    warranty_consumed_pct: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def signature(self) -> tuple:
        """Hashable digest of every sensor-facing value (v1.1.6).

        The manager notifies entities only when this changes, so ten sensors
        stop re-writing identical states into the HA recorder every 30 s.
        All float fields are already rounded to 1 decimal at assignment, so
        sub-display jitter does not defeat the comparison.
        """
        return (
            self.bhi,
            self.confidence,
            self.soh_capacity if self.soh_capacity is None else round(self.soh_capacity, 1),
            self.soh_efficiency if self.soh_efficiency is None else round(self.soh_efficiency, 1),
            self.soh_balance if self.soh_balance is None else round(self.soh_balance, 1),
            # Integer step: the rolling-window mixture makes the stress index
            # creep by ~0.01–0.1 per tick; sub-integer motion is not a
            # reportable change for an informational exposure metric.
            self.stress_index if self.stress_index is None else round(self.stress_index),
            self.predicted_soh,
            self.health_divergence,
            self.efc,
            self.warranty_consumed_pct,
            self.attributes.get("segment_count"),
            self.attributes.get("golden_segment_count"),
            self.attributes.get("discarded_segment_count"),
            self.attributes.get("counter_resets"),
            tuple(self.attributes.get("contributing_terms", ())),
        )


class BatteryHealthEngine:
    """Orchestrates all trackers; one instance per battery-equipped inverter."""

    def __init__(self, cfg: BatteryHealthConfig | None = None) -> None:
        self.cfg = cfg or BatteryHealthConfig()
        self.segments = SegmentTracker(self.cfg)
        self.efficiency = EfficiencyTracker(self.cfg)
        self.balance = BalanceTracker(self.cfg)
        self.stress = StressAccumulator(self.cfg)
        self._charge_counter = CounterMonitor("lifetime_charge")
        self._discharge_counter = CounterMonitor("lifetime_discharge")
        self.first_seen_ts: float | None = None
        self.dirty = False                     # persistence hint for manager
        self._last_report = HealthReport()

    # ── main entry point ────────────────────────────────────────────────────
    def update(self, raw: HealthSample) -> HealthReport:
        s = validate_sample(raw)
        if self.first_seen_ts is None:
            self.first_seen_ts = s.timestamp
            self.dirty = True

        # Counter reset handling first — a reset invalidates open windows.
        pre_resets = (
            self._charge_counter.reset_count + self._discharge_counter.reset_count
        )
        s.lifetime_charge_kwh = self._charge_counter.feed(s.lifetime_charge_kwh)
        s.lifetime_discharge_kwh = self._discharge_counter.feed(
            s.lifetime_discharge_kwh
        )
        post_resets = (
            self._charge_counter.reset_count + self._discharge_counter.reset_count
        )
        if post_resets != pre_resets:
            self.segments.mark_gap()
            self.efficiency.invalidate_anchor()
            self.dirty = True

        closed = self.segments.feed(s)
        if closed is not None:
            self.dirty = True
        self.efficiency.feed(s)
        self.balance.feed(s)
        self.stress.feed(s)
        self.segments.prune(s.timestamp)
        self.stress.prune(s.timestamp)

        self._last_report = self._evaluate(s.timestamp)
        return self._last_report

    def mark_gap(self) -> None:
        """Coordinator update failed: propagate per spec §9."""
        self.segments.mark_gap()
        self.stress.mark_gap()
        self.efficiency.invalidate_anchor()

    @property
    def report(self) -> HealthReport:
        return self._last_report

    def reset_efficiency_baseline(self) -> None:
        self.efficiency.reset_baseline()
        self.dirty = True

    # ── evaluation ──────────────────────────────────────────────────────────
    def _evaluate(self, now: float) -> HealthReport:
        cfg = self.cfg
        r = HealthReport()

        r.soh_capacity, cap_attrs = self.segments.soh_capacity()
        r.soh_efficiency, eff_attrs = self.efficiency.soh_efficiency()
        r.soh_balance, bal_attrs = self.balance.soh_balance()
        r.attributes.update(cap_attrs)
        r.attributes.update(eff_attrs)
        r.attributes.update(bal_attrs)

        # Composite over available measured terms only (renormalized weights;
        # a missing term must never crater the composite as an implicit 0).
        w_cap, w_eff, w_bal = cfg.normalized_weights()
        terms = [
            ("capacity", r.soh_capacity, w_cap),
            ("efficiency", r.soh_efficiency, w_eff),
            ("balance", r.soh_balance, w_bal),
        ]
        available = [(n, v, w) for n, v, w in terms if v is not None]
        r.attributes["contributing_terms"] = [n for n, _, _ in available]
        if available:
            total_w = sum(w for _, _, w in available)
            r.bhi = round(sum(v * w for _, v, w in available) / total_w, 1)

        # Stress + forecast (informational, never in BHI)
        r.stress_ratio = self.stress.stress_ratio()
        if r.stress_ratio is not None and r.stress_ratio > 0:
            r.stress_index = round(clip(100.0 / r.stress_ratio, 0.0, 100.0), 1)

        # EFC / warranty
        discharge_total = self._discharge_counter.value
        if discharge_total is not None:
            r.efc = round(discharge_total / cfg.rated_capacity_kwh, 1)
            r.warranty_consumed_pct = round(
                clip(discharge_total / cfg.warranty_throughput_kwh * 100.0, 0.0, 100.0),
                1,
            )

        # Aging forecast: predicted SOH = 100 − A·stress·√years − B·EFC.
        # Heuristic model for divergence detection, not a lab prediction.
        if self.first_seen_ts is not None:
            age_years = max(0.0, (now - self.first_seen_ts) / (365.25 * SECONDS_PER_DAY))
            stress = r.stress_ratio if r.stress_ratio is not None else 1.0
            calendar_loss = (
                cfg.forecast_calendar_pct_per_sqrt_year * stress * math.sqrt(age_years)
            )
            cycle_loss = cfg.forecast_cycle_pct_per_efc * (r.efc or 0.0)
            r.predicted_soh = round(clip(100.0 - calendar_loss - cycle_loss, 0.0, 100.0), 1)
            if r.soh_capacity is not None:
                r.health_divergence = round(r.soh_capacity - r.predicted_soh, 1)

        # Confidence
        seg_count = len(self.segments.segments)
        last_seg = self.segments.last_segment_ts
        if last_seg is not None and (now - last_seg) > cfg.stale_after_days * SECONDS_PER_DAY:
            r.confidence = "stale"
        elif (
            seg_count < cfg.confidence_min_segments
            or self.efficiency.baseline is None
        ):
            r.confidence = "low"
        else:
            r.confidence = "normal"

        r.attributes["counter_resets"] = (
            self._charge_counter.reset_count + self._discharge_counter.reset_count
        )
        return r

    # ── persistence ─────────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "first_seen_ts": self.first_seen_ts,
            "segments": self.segments.to_dict(),
            "efficiency": self.efficiency.to_dict(),
            "balance": self.balance.to_dict(),
            "stress": self.stress.to_dict(),
            "charge_counter": self._charge_counter.to_dict(),
            "discharge_counter": self._discharge_counter.to_dict(),
        }

    def restore(self, data: dict[str, Any] | None) -> None:
        if not data:
            return
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            _LOGGER.warning(
                "battery_health: unknown storage schema %s — starting fresh", version
            )
            return
        self.first_seen_ts = data.get("first_seen_ts")
        self.segments.restore(data.get("segments", {}))
        self.efficiency.restore(data.get("efficiency", {}))
        self.balance.restore(data.get("balance", {}))
        self.stress.restore(data.get("stress", {}))
        self._charge_counter.restore(data.get("charge_counter", {}))
        self._discharge_counter.restore(data.get("discharge_counter", {}))
        self.dirty = False
