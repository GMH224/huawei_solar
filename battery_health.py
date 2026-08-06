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
import time as time_module
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
    rated_capacity_kwh: float = 20.7          # nameplate fallback only
    #: v1.2.0 - SOH_cap is anchored to a MEASURED beginning-of-life capacity,
    #: not the nameplate.  Field evidence: a LUNA2000-21-S1 rated 20.7 kWh
    #: measured a consistent ~22.8 kWh across 162 segments spanning 6 months
    #: (spread 0.31 kWh).  Anchoring to the nameplate pinned SOH_cap at the
    #: 100% clip, hiding the first ~10% of any real degradation.
    capacity_reference_kwh: float | None = None      # learned, persisted
    capacity_reference_min_segments: int = 20
    #: The reference must also SPAN time.  Implied capacity depends ~2% on
    #: where in the SOC range a segment sat (Finding J), and usage is
    #: seasonal, so anchoring to the first N segments alone captures whatever
    #: conditions happened to come first.  On the field dataset that biased
    #: the reference to 21.9 kWh against a true ~22.75 kWh and left SOH
    #: capacity reading 103.8% indefinitely.
    capacity_reference_min_span_days: float = 45.0
    soh_capacity_clip_max: float = 110.0      # do not hide headroom at 100
    battery_install_ts: float | None = None   # for calendar-age forecast
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
    segment_max_idle_s: float = 21600.0       # 6 h at rest ends a segment
    golden_weight_boost: float = 4.0          # Huawei SOH-calibration segments
    trim_fraction: float = 0.10               # weighted trimmed mean

    # SOH_eff — round-trip efficiency drift
    #: v1.2.0 - anchors must sit at EQUAL stored energy, not merely "high
    #: SOC". Relaxing to SOC>=97 admitted up to 3 SOC points of mismatch,
    #: worth ~4.5% of a 15 kWh window. Field data (187 days, 23 windows):
    #: eta stdev 0.0101 at SOC>=97 vs 0.0018 at SOC>=100 - 5.6x quieter with
    #: zero windows lost.
    eff_min_window_charge_kwh: float = 15.0   # was 30: 2x faster AND quieter
    eff_anchor_rest_power_w: float = 100.0
    eff_anchor_tier1_soc: float = 99.0        # at/near a BMS recalibration point
    eff_anchor_soc_match: float = 0.5         # tier 2: anchors matched to +-this
    eff_anchor_ceiling_margin: float = 0.5    # tier 2 gate: ceiling - this
    eff_anchor_min_ceiling: float = 60.0      # below this, do not anchor at all
    eff_tier2_max_window_days: float = 21.0   # bound coulomb drift in tier 2
    eff_valid_min: float = 0.50               # plausibility band for η
    eff_valid_max: float = 1.05
    eff_baseline_windows: int = 3             # first N windows → baseline
    eff_rolling_windows: int = 6              # last N windows → current
    eff_pts_per_pct_loss: float = 8.0         # SOH_eff slope

    # SOH_bal — pack balance (v1.2.0: baseline-relative)
    #: Absolute thresholds proved unusable on real hardware: a rock-stable
    #: 2.4 C inter-pack offset (present at idle AND under load, so NOT
    #: battery-generated heat) scored ~81/100 on a healthy pack set, and the
    #: 0.1 V voltage register resolution made one LSB worth 11 score points.
    #: The score is now deviation from a learned per-installation baseline.
    balance_use_baseline: bool = True
    balance_baseline_min_samples: int = 20
    balance_dv_dev_full_score: float = 0.15   # deviation >= 1 LSB tolerated
    balance_dv_dev_zero_score: float = 0.40
    balance_dt_dev_full_score: float = 1.0
    balance_dt_dev_zero_score: float = 6.0
    #: Sampling is gated relative to the prevailing charge ceiling: a
    #: configured cap or winter PV may keep the pack below 95% for months
    #: (field: 78 consecutive days).  The floor still applies because LFP's
    #: flat mid-range OCV makes dV uninformative low down.
    balance_ceiling_margin: float = 10.0
    balance_min_soc_floor: float = 60.0
    balance_min_soc: float = 95.0             # fallback when ceiling unknown
    balance_rest_power_w: float = 50.0
    balance_dv_full_score: float = 0.05       # legacy absolute mode
    balance_dv_zero_score: float = 0.50
    balance_dt_full_score: float = 1.0
    balance_dt_zero_score: float = 8.0
    balance_sample_count: int = 20            # median over last N samples

    # Stress accumulator (model input — NOT part of BHI)
    q10: float = 2.0
    stress_ref_temp_c: float = 25.0
    stress_soc_knee: float = 80.0
    stress_soc_max_factor: float = 2.5
    stress_window_days: float = 90.0
    stress_max_gap_s: float = 900.0           # gaps > this excluded from Δt
    #: v1.1.8 — a data gap no longer destroys an in-progress discharge
    #: segment.  SOC is an absolute state reading and storage_total_discharge
    #: is a cumulative counter, so ΔSOC and Δenergy across a gap remain valid
    #: without the samples in between; the implied-capacity plausibility band
    #: already rejects any interval where something unobserved happened.
    #: Gaps longer than this are still treated as untrustworthy.
    max_gap_bridge_s: float = 3600.0          # 1 h

    # Aging forecast (heuristic model — documented as such)
    forecast_calendar_pct_per_sqrt_year: float = 2.5   # at stress_ratio = 1.0
    forecast_cycle_pct_per_efc: float = 0.004          # ≈ 20% over 5000 EFC

    # Confidence
    confidence_min_segments: int = 5
    stale_after_days: float = 60.0
    #: v1.2.1 - after ANY recovery (HA restart, coordinator returning from an
    #: outage, counter reset) registers may briefly report stale or default
    #: values. Measurement resumes immediately, but IRREVERSIBLE operations
    #: (baseline capture, epoch changes) wait for this settling period.
    settling_period_s: float = 300.0          # 5 minutes
    #: A firmware update or reboot can leave register 47081 returning a
    #: default value for the duration of the cycle. Accepting it as a real
    #: setting change would fire a ceiling epoch and destroy the efficiency
    #: and balance baselines - four times a year, on a residential system
    #: whose reboot behaviour the vendor does not document.
    ceiling_min_plausible: float = 20.0
    ceiling_debounce_samples: int = 3
    #: Pack cooling runs at roughly -0.4 C/hour (measured over 48 undisturbed
    #: rest windows), so thermal rise carries HOURS of load history. Twenty
    #: consecutive samples from one afternoon are not a norm; the baseline
    #: must span days to average across diurnal cycles.
    thermal_rise_baseline_min_span_days: float = 3.0

    #: v1.2.0 - term availability is seasonal (balance/efficiency need a high
    #: SOC that winter may not reach).  Hold the last good sub-score rather
    #: than dropping it, so the renormalised composite does not step at the
    #: seasonal boundary with no underlying health change.
    subscore_hold_days: float = 90.0

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
    #: Configured end-of-charge SOC (register 47081). "Full" is defined
    #: relative to this, not to an absolute 100% - a user running a 93%
    #: summer cap still reaches their ceiling daily (v1.2.0).
    charge_ceiling_soc: float | None = None
    #: Optional ambient temperature of the battery room. Field evidence shows
    #: every pack sits a characteristic amount above ambient (max sensors
    #: +2.6/+5.2/+3.9 C, min sensors +0.5/+3.3/+2.1 C). That RISE tracks heat
    #: generation, so it can reveal all packs ageing together - something
    #: inter-pack spread is blind to by construction.
    ambient_temp_c: float | None = None


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
    out.charge_ceiling_soc = _valid_or_none(
        raw.charge_ceiling_soc, 0.0, 100.0, "charge_ceiling")
    out.ambient_temp_c = _valid_or_none(
        raw.ambient_temp_c, TEMP_MIN_C, TEMP_MAX_C, "ambient_temp")
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
        #: True when the most recent feed() had no fresh reading and the
        #: previous value was carried forward.  Segment endpoints must not be
        #: taken from a carried-forward value (Finding C, v1.2.0).
        self.is_stale = False

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
            self.is_stale = True
            return None if self._last is None else self._last + self._offset
        self.is_stale = False
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


class CeilingMonitor:
    """Validate and debounce the configured end-of-charge SOC (register 47081).

    A ceiling change is a *destructive* signal here: it restarts the efficiency
    and balance baseline epochs, because the ceiling shifts eta and the SOC
    operating band systematically. That makes a spurious change expensive, and
    firmware-update / reboot windows are exactly when a register is most likely
    to report a default or stale value.

    Two guards, deliberately simple:
      * a plausibility floor - nobody configures a ceiling below ~20%, so a
        lower reading is a glitch by definition, not a setting;
      * a debounce - a genuine setting change persists across consecutive
        polls; a transient does not.
    """

    def __init__(self, cfg: BatteryHealthConfig) -> None:
        self._cfg = cfg
        self.value: float | None = None
        self._candidate: float | None = None
        self._streak = 0
        self.rejected_count = 0
        self.debounced_count = 0

    def feed(self, raw: float | None) -> float | None:
        """Return the accepted ceiling, ignoring implausible/transient values."""
        cfg = self._cfg
        if raw is None:
            return self.value
        if raw < cfg.ceiling_min_plausible:
            self.rejected_count += 1
            _LOGGER.warning(
                "battery_health: implausible end-of-charge SOC %.0f%% ignored "
                "(below the %.0f%% floor) - likely a reboot/firmware artefact; "
                "keeping %s",
                raw, cfg.ceiling_min_plausible,
                "unset" if self.value is None else f"{self.value:.0f}%",
            )
            return self.value
        if self.value is None:
            self.value = raw
            return self.value
        if abs(raw - self.value) < 1.0:
            self._candidate = None
            self._streak = 0
            return self.value
        if self._candidate is not None and abs(raw - self._candidate) < 1.0:
            self._streak += 1
        else:
            self._candidate = raw
            self._streak = 1
        if self._streak >= cfg.ceiling_debounce_samples:
            _LOGGER.info(
                "battery_health: end-of-charge SOC change confirmed %.0f%% -> "
                "%.0f%% after %d consistent readings",
                self.value, self._candidate, self._streak)
            self.value = self._candidate
            self._candidate = None
            self._streak = 0
            self.debounced_count += 1
        return self.value

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "rejected": self.rejected_count,
                "debounced": self.debounced_count}

    def restore(self, data: dict[str, Any]) -> None:
        self.value = data.get("value")
        self.rejected_count = int(data.get("rejected", 0))
        self.debounced_count = int(data.get("debounced", 0))


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
    gap_bridged: int = 0      # v1.1.8: data gaps spanned by this segment
    #: v1.2.0 (Finding J): implied capacity depends ~2% on where in the SOC
    #: range the segment sat (field: 22.98 kWh at midpoint 85-100% vs
    #: 23.49 kWh at 50-65%).  Seasonal usage shifts therefore look like
    #: capacity change unless the operating band is recorded alongside it.
    soc_midpoint: float = 0.0
    charge_ceiling: float | None = None

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
        kwargs = {k: d[k] for k in (
            "start_ts", "end_ts", "soc_start", "soc_end", "energy_kwh",
            "implied_capacity_kwh", "freshness", "golden",
        )}
        # Tolerate pre-1.1.8 persisted segments (field added in 1.1.8).
        kwargs["gap_bridged"] = int(d.get("gap_bridged", 0))
        # Pre-1.2.0 segments predate these fields.
        kwargs["soc_midpoint"] = float(
            d.get("soc_midpoint", (d["soc_start"] + d["soc_end"]) / 2.0))
        kwargs["charge_ceiling"] = d.get("charge_ceiling")
        return cls(**kwargs)


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
        # v1.1.8 gap bridging
        self._gap_pending = False
        self._idle_since: float | None = None
        self._last_good_ts: float | None = None
        self._bridges_in_segment = 0
        self.gap_bridged_count = 0
        self._seg_ceiling: float | None = None
        #: Measured beginning-of-life capacity (Finding H). Captured once from
        #: the first N segments, persisted, and re-anchorable via a button.
        self.reference_capacity_kwh: float | None = None
        self.reference_captured_ts: float | None = None
        self.reference_epochs: list[dict[str, Any]] = []
        self.stale_endpoint_skips = 0

    # ── feed ────────────────────────────────────────────────────────────────
    def feed(
        self, s: HealthSample, counter_stale: bool = False
    ) -> DischargeSegment | None:
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
            # v1.1.8: a read failure NO LONGER destroys an in-progress segment.
            #
            # Rationale (corrects a v1.1.5 design error): the previous rule
            # discarded the segment "because we cannot know what happened during
            # the outage".  But nothing needs to be known: SOC is an absolute
            # state reading and lifetime discharge is a cumulative counter, so
            # ΔSOC and Δenergy across the gap are both still exact without the
            # intervening samples.  If something unobserved DID occur, the
            # implied-capacity plausibility band rejects the segment on close —
            # that guard exists precisely for this.
            #
            # The old rule made measurement structurally impossible on a link
            # with intermittent Modbus timeouts: register_cache correctly
            # refuses to serve energy counters from a stale cache after a
            # timeout, so every timeout produced a None here and killed the
            # segment long before it could reach the minimum ΔSOC.
            if self._active:
                self._gap_pending = True
            return None

        prev_good_ts = self._last_good_ts
        self._last_good_ts = s.timestamp

        discharging = power < -cfg.segment_rest_power_w

        if not self._active:
            # Finding C (v1.2.0): never open a segment on a carried-forward
            # counter value - the start endpoint would be stale by however
            # long the read had been failing.
            if discharging and not counter_stale:
                self._begin(s, soc, discharge)
            elif discharging:
                self.stale_endpoint_skips += 1
            return None

        # Active segment ------------------------------------------------------
        # Resolve a pending gap first: bridge it if short enough, otherwise
        # give up on this segment (bounded trust — v1.1.8).
        if self._gap_pending:
            elapsed = (
                s.timestamp - prev_good_ts if prev_good_ts is not None else 0.0
            )
            if elapsed > cfg.max_gap_bridge_s:
                self._discard(
                    f"data gap of {elapsed / 60.0:.0f} min exceeds the "
                    f"{cfg.max_gap_bridge_s / 60.0:.0f} min bridge limit"
                )
                if discharging:
                    self._begin(s, soc, discharge)
                return None
            self._gap_pending = False
            self._bridges_in_segment += 1
            self.gap_bridged_count += 1
            self._agg_cache = None
            _LOGGER.debug(
                "battery_health: bridged a %.0f s data gap mid-segment", elapsed
            )

        if s.soh_calibration_active:
            self._seg_calibration_seen = True

        if soc > self._last_soc + cfg.soc_backstep_tolerance:
            # SOC rose: charging blip or SOC correction → close at last point.
            return self._close(self._last_soc, discharge, s.timestamp)
        self._last_soc = min(self._last_soc, soc)

        # Finding F (v1.2.0): resting does NOT end a segment.
        #
        # Capacity arithmetic is dkWh / dSOC - a ratio unaffected by the
        # battery pausing partway through.  The previous rule closed on any
        # tick above the rest threshold, so a single near-zero power reading
        # (field: 15 such blips in 8 days, median duration 130 s) split a
        # 10-hour overnight discharge into two ~5 h halves, each marginal
        # against the minimum dSOC.  Only genuine CHARGING ends a segment;
        # prolonged rest still does, to bound SOC drift.
        charging = power > cfg.segment_rest_power_w
        if charging:
            return self._close(soc, discharge, s.timestamp)
        if not discharging:
            if self._idle_since is None:
                self._idle_since = s.timestamp
            elif s.timestamp - self._idle_since > cfg.segment_max_idle_s:
                return self._close(soc, discharge, s.timestamp)
            return None
        self._idle_since = None
        return None

    def mark_gap(self) -> None:
        """A data gap occurred (coordinator read failure).

        v1.1.8: this marks the gap *pending* rather than destroying the
        segment.  It is bridged on the next good sample if it falls within
        ``max_gap_bridge_s``, and discarded otherwise.
        """
        if self._active:
            self._gap_pending = True

    def discard_active(self, reason: str) -> None:
        """Destroy the in-progress segment outright.

        Reserved for events that genuinely invalidate the interval arithmetic
        — currently only a lifetime-counter reset, where energy of unknown
        magnitude may have flowed before the counter restarted.  A plain data
        gap is NOT such an event (see feed()).
        """
        if self._active:
            self._discard(reason)

    # ── internals ───────────────────────────────────────────────────────────
    def _begin(self, s: HealthSample, soc: float, discharge: float) -> None:
        self._active = True
        self._start_ts = s.timestamp
        self._start_soc = soc
        self._last_soc = soc
        self._start_discharge_kwh = discharge
        self._gap_pending = False
        self._idle_since = None
        self._bridges_in_segment = 0
        self._seg_ceiling = s.charge_ceiling_soc
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
            gap_bridged=self._bridges_in_segment,
            soc_midpoint=(self._start_soc + end_soc) / 2.0,
            charge_ceiling=self._seg_ceiling,
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
            "gap_bridged_count": self.gap_bridged_count,
            "stale_endpoint_skips": self.stale_endpoint_skips,
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
        # Finding J: record the SOC band these segments came from, so a
        # seasonal shift in operating range is not mistaken for capacity fade.
        mids = [s.soc_midpoint for s in segs]
        attrs["segment_soc_midpoint_mean"] = round(sum(mids) / len(mids), 1)
        ceils = [s.charge_ceiling for s in segs if s.charge_ceiling is not None]
        if ceils:
            attrs["segment_charge_ceiling_mean"] = round(sum(ceils) / len(ceils), 1)

        # Finding H: capture a measured beginning-of-life reference once.
        if (
            self.reference_capacity_kwh is None
            and len(segs) >= cfg.capacity_reference_min_segments
        ):
            span_days = (
                max(s.end_ts for s in segs) - min(s.start_ts for s in segs)
            ) / SECONDS_PER_DAY
            if span_days >= cfg.capacity_reference_min_span_days:
                self.set_reference(
                    _median(sorted(s.implied_capacity_kwh for s in segs)),
                    reason="auto: %d segments spanning %.0f days"
                           % (len(segs), span_days),
                    ts=max(s.end_ts for s in segs),
                )

        reference = self.reference_capacity_kwh or cfg.rated_capacity_kwh
        attrs["capacity_reference_kwh"] = round(reference, 2)
        attrs["capacity_reference_is_measured"] = self.reference_capacity_kwh is not None
        attrs["capacity_reference_captured"] = self.reference_captured_ts
        attrs["capacity_reference_epochs"] = len(self.reference_epochs)
        soh = clip(mean_cap / reference * 100.0, 0.0, cfg.soh_capacity_clip_max)
        self._agg_cache = (soh, dict(attrs))
        return soh, attrs

    def set_reference(
        self, value: float, reason: str, ts: float | None = None
    ) -> None:
        """Anchor SOH_cap to a measured capacity, appending a new epoch.

        Epochs are appended, never overwritten: the raw estimate and every
        prior reference remain reconstructible, so re-anchoring can never
        destroy the long-term record (only re-zero a derived view).
        """
        prev = self.reference_capacity_kwh
        self.reference_epochs.append(
            {"ts": ts, "value": round(value, 3), "reason": reason,
             "previous": None if prev is None else round(prev, 3)}
        )
        self.reference_capacity_kwh = value
        self.reference_captured_ts = ts
        self._agg_cache = None
        _LOGGER.warning(
            "battery_health: capacity reference set to %.2f kWh (was %s) - %s. "
            "SOH capacity is measured against this value from now on.",
            value, "unset" if prev is None else f"{prev:.2f} kWh", reason,
        )

    # ── persistence ─────────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return {
            "segments": [s.to_dict() for s in self.segments],
            "throughput_since_full": self._throughput_since_full_kwh,
            "last_discharge": self._last_discharge_kwh,
            "last_segment_ts": self.last_segment_ts,
            "discarded": self.discarded_segments,
            "gap_bridged": self.gap_bridged_count,
            "reference_capacity": self.reference_capacity_kwh,
            "reference_captured_ts": self.reference_captured_ts,
            "reference_epochs": self.reference_epochs,
            "stale_endpoint_skips": self.stale_endpoint_skips,
        }

    def restore(self, data: dict[str, Any]) -> None:
        self.segments = [
            DischargeSegment.from_dict(d) for d in data.get("segments", [])
        ]
        self._throughput_since_full_kwh = float(data.get("throughput_since_full", 0.0))
        self._last_discharge_kwh = data.get("last_discharge")
        self.last_segment_ts = data.get("last_segment_ts")
        self.discarded_segments = int(data.get("discarded", 0))
        self.gap_bridged_count = int(data.get("gap_bridged", 0))
        self.reference_capacity_kwh = data.get("reference_capacity")
        self.reference_captured_ts = data.get("reference_captured_ts")
        self.reference_epochs = list(data.get("reference_epochs", []))
        self.stale_endpoint_skips = int(data.get("stale_endpoint_skips", 0))
        self._gap_pending = False
        self._idle_since = None
        self._last_good_ts = None
        self._bridges_in_segment = 0
        self._agg_cache = None
        # Never resume a half-open segment across a restart (spec §8).
        self._active = False


# ═════════════════════════════════════════════════════════════════════════════
# SOH_eff — round-trip efficiency drift between full-charge anchors
# ═════════════════════════════════════════════════════════════════════════════
class EfficiencyTracker:
    """Round-trip efficiency between successive equal-energy anchor states.

    v1.2.0 - the anchor rule is the whole ballgame.  eta = dDischarge/dCharge
    is only meaningful between two states holding the SAME stored energy.
    "SOC >= 97" was a poor proxy: it admitted up to 3 SOC points of mismatch,
    worth ~4.5% on a 15 kWh window.  Field data (187 days, 23 windows):

        anchor rule            eta stdev   jitter at slope 8
        SOC >= 97 (v1.1.x)      0.0101         +-8.1 pts
        SOC >= 100              0.0018         +-1.5 pts   <- 5.6x quieter

    ...with ZERO windows lost.  But an absolute 100% gate is unusable in
    winter, when a configured charge ceiling (or weak PV) may keep the pack
    below it for months - field: 122 consecutive days below 100%.  So anchors
    are defined RELATIVE to the prevailing ceiling, in two tiers:

      tier 1  SOC >= eff_anchor_tier1_soc, i.e. at a BMS recalibration point.
              Highest quality; SOC is freshly re-referenced.
      tier 2  Matched pairs (+- eff_anchor_soc_match) at the prevailing
              ceiling.  Usable year-round, flagged lower confidence, and
              time-capped because coulomb drift grows between recalibrations.

    Changing the configured ceiling shifts eta systematically (field: 0.9801
    at a 93% cap vs 0.9883 at 100% - 6.5 SOH points, comparable to a whole
    lifetime of real degradation), so a ceiling change starts a new baseline
    epoch rather than contaminating the existing one.
    """

    def __init__(self, cfg: BatteryHealthConfig) -> None:
        self._cfg = cfg
        self._anchor: tuple[float, float, float, float, int] | None = None
        self.windows: deque[float] = deque(maxlen=64)
        self.window_tiers: deque[int] = deque(maxlen=64)
        self.baseline: float | None = None
        self.baseline_tier: int | None = None
        self._baseline_pool: list[float] = []
        self.baseline_epochs: list[dict[str, Any]] = []
        self.last_ceiling: float | None = None

    # ── anchor qualification ────────────────────────────────────────────────
    def _anchor_tier(self, s: HealthSample) -> int:
        """Return 1 or 2 for a qualifying anchor, else 0."""
        cfg = self._cfg
        if s.soc is None or s.power_w is None:
            return 0
        if abs(s.power_w) > cfg.eff_anchor_rest_power_w:
            return 0
        if s.soc >= cfg.eff_anchor_tier1_soc:
            return 1
        ceiling = s.charge_ceiling_soc
        if ceiling is None or ceiling < cfg.eff_anchor_min_ceiling:
            return 0
        if s.soc >= ceiling - cfg.eff_anchor_ceiling_margin:
            return 2
        return 0

    def feed(self, s: HealthSample, learn: bool = True) -> None:
        cfg = self._cfg
        # Finding O: a ceiling change invalidates cross-epoch comparison, and
        # must be detected even on ticks where the counters failed to read.
        # Suppressed while learning is paused: a reboot-time register artefact
        # must never be able to destroy a baseline (v1.2.1).
        ceiling = s.charge_ceiling_soc
        if ceiling is not None:
            if (
                learn
                and self.last_ceiling is not None
                and abs(ceiling - self.last_ceiling) >= 1.0
            ):
                self.new_epoch(
                    f"charge ceiling changed {self.last_ceiling:.0f}% -> "
                    f"{ceiling:.0f}%", ts=s.timestamp)
            if learn or self.last_ceiling is None:
                self.last_ceiling = ceiling
        if not learn:
            return
        if s.lifetime_charge_kwh is None or s.lifetime_discharge_kwh is None:
            return

        tier = self._anchor_tier(s)
        if tier == 0:
            return

        if self._anchor is None:
            self._anchor = (s.timestamp, s.lifetime_charge_kwh,
                            s.lifetime_discharge_kwh, s.soc, tier)
            return

        t0, chg0, dis0, soc0, tier0 = self._anchor
        d_charge = s.lifetime_charge_kwh - chg0
        if d_charge < cfg.eff_min_window_charge_kwh:
            # Same dwell (or too little throughput): slide the anchor forward.
            self._anchor = (s.timestamp, chg0, dis0, soc0, tier0)
            return

        window_tier = max(tier, tier0)
        # Tier 2 requires the two anchors to sit at the same SOC, and bounds
        # how long the window may span (coulomb drift between recalibrations).
        if window_tier == 2:
            if abs(s.soc - soc0) > cfg.eff_anchor_soc_match:
                self._anchor = (s.timestamp, s.lifetime_charge_kwh,
                                s.lifetime_discharge_kwh, s.soc, tier)
                return
            if (s.timestamp - t0) > cfg.eff_tier2_max_window_days * SECONDS_PER_DAY:
                self._anchor = (s.timestamp, s.lifetime_charge_kwh,
                                s.lifetime_discharge_kwh, s.soc, tier)
                return

        eta = (s.lifetime_discharge_kwh - dis0) / d_charge
        self._anchor = (s.timestamp, s.lifetime_charge_kwh,
                        s.lifetime_discharge_kwh, s.soc, tier)
        if not (cfg.eff_valid_min <= eta <= cfg.eff_valid_max):
            _LOGGER.debug("battery_health: discarding implausible eta=%.3f", eta)
            return
        self.windows.append(eta)
        self.window_tiers.append(window_tier)
        if self.baseline is None:
            self._baseline_pool.append(eta)
            if len(self._baseline_pool) >= cfg.eff_baseline_windows:
                self.baseline = _median(self._baseline_pool)
                self.baseline_tier = window_tier
                self.baseline_epochs.append(
                    {"ts": s.timestamp, "value": round(self.baseline, 5),
                     "tier": window_tier, "reason": "auto: first %d windows"
                     % len(self._baseline_pool)})
                _LOGGER.info(
                    "battery_health: efficiency baseline captured: eta=%.4f "
                    "(median of %d tier-%d windows)",
                    self.baseline, len(self._baseline_pool), window_tier)

    def invalidate_anchor(self) -> None:
        """Discard the open window. Called ONLY on a lifetime-counter reset.

        A plain data gap does not invalidate it: both endpoints are cumulative
        counter readings, so eta over the window survives missing samples
        in between (v1.1.8).
        """
        self._anchor = None

    def new_epoch(self, reason: str, ts: float | None = None) -> None:
        """Start a fresh baseline epoch, retaining the previous one."""
        prev = self.baseline
        self.baseline_epochs.append(
            {"ts": ts, "value": None, "reason": reason,
             "previous": None if prev is None else round(prev, 5)})
        self.baseline = None
        self.baseline_tier = None
        self._baseline_pool.clear()
        self._anchor = None
        self.windows.clear()
        self.window_tiers.clear()
        _LOGGER.warning(
            "battery_health: efficiency baseline epoch restarted (%s). "
            "Previous baseline %s retained in history.",
            reason, "unset" if prev is None else f"eta={prev:.4f}")

    def reset_baseline(self) -> None:
        self.new_epoch("manual reset")

    def soh_efficiency(self) -> tuple[float | None, dict[str, Any]]:
        cfg = self._cfg
        attrs: dict[str, Any] = {
            "efficiency_baseline": self.baseline,
            "efficiency_window_count": len(self.windows),
            "efficiency_baseline_tier": self.baseline_tier,
            "efficiency_baseline_epochs": len(self.baseline_epochs),
            "efficiency_charge_ceiling": self.last_ceiling,
        }
        if self.baseline is None or not self.windows:
            return None, attrs
        recent = list(self.windows)[-cfg.eff_rolling_windows:]
        current = _median(recent)
        attrs["efficiency_current"] = round(current, 4)
        tiers = list(self.window_tiers)[-cfg.eff_rolling_windows:]
        attrs["efficiency_current_tier"] = max(tiers) if tiers else None
        loss_pct_points = max(0.0, (self.baseline - current) * 100.0)
        soh = clip(100.0 - loss_pct_points * cfg.eff_pts_per_pct_loss, 0.0, 100.0)
        return soh, attrs

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchor": list(self._anchor) if self._anchor else None,
            "windows": list(self.windows),
            "window_tiers": list(self.window_tiers),
            "baseline": self.baseline,
            "baseline_tier": self.baseline_tier,
            "baseline_pool": list(self._baseline_pool),
            "baseline_epochs": self.baseline_epochs,
            "last_ceiling": self.last_ceiling,
        }

    def restore(self, data: dict[str, Any]) -> None:
        anchor = data.get("anchor")
        self._anchor = tuple(anchor) if anchor else None
        self.windows = deque(data.get("windows", []), maxlen=64)
        self.window_tiers = deque(data.get("window_tiers", []), maxlen=64)
        self.baseline = data.get("baseline")
        self.baseline_tier = data.get("baseline_tier")
        self._baseline_pool = list(data.get("baseline_pool", []))
        self.baseline_epochs = list(data.get("baseline_epochs", []))
        self.last_ceiling = data.get("last_ceiling")


def _median(values: list[float]) -> float:
    v = sorted(values)
    n = len(v)
    mid = n // 2
    return v[mid] if n % 2 else (v[mid - 1] + v[mid]) / 2.0


# ═════════════════════════════════════════════════════════════════════════════
# SOH_bal — pack balance at rest near full SOC
# ═════════════════════════════════════════════════════════════════════════════
class BalanceTracker:
    """Pack balance from dV / dT spread at rest, scored against a baseline.

    v1.2.0 - absolute thresholds proved unusable on real hardware:

    * A pack-temperature spread of 2.40 C (stdev 0.19 over 1056 samples) was
      present at idle (2.33 C) just as much as under >1 kW charge (2.52 C).
      Battery-generated heat would collapse at rest; this does not.  It is a
      fixed sensor/positional offset, not degradation - yet it scored ~81/100.
    * Pack voltage has 0.1 V resolution.  Against the old 0.05-0.50 V band one
      least-significant bit moved the score 11 points, so the metric mostly
      reported quantisation noise (observed plateaus at 90.5 / 84.9 / 79.4).

    So the score is now deviation from a learned per-installation baseline:
    fixed offsets cancel, and only a pack drifting away from its own
    established norm registers.  Raw dV/dT are always exposed and never
    re-zeroed, so re-baselining can only reset a derived view - the underlying
    record survives.

    Sampling is gated relative to the prevailing charge ceiling rather than an
    absolute SOC, because a configured cap (or winter PV) may keep the pack
    below 95% for months - field: 78 consecutive days.  LFP's flat mid-range
    OCV still makes dV uninformative low down, hence the hard floor.
    """

    def __init__(self, cfg: BatteryHealthConfig) -> None:
        self._cfg = cfg
        self.scores: deque[float] = deque(maxlen=cfg.balance_sample_count)
        self.raw_dv: deque[float] = deque(maxlen=cfg.balance_sample_count)
        self.raw_dt: deque[float] = deque(maxlen=cfg.balance_sample_count)
        #: Spread across the pack MIN-temperature sensors - a physically
        #: independent channel from the max sensors.  Field data shows the two
        #: agree closely (2.61 C vs 2.73 C) with identical pack ordering, which
        #: is what confirmed the inter-pack offset is a real thermal gradient
        #: rather than sensor miscalibration.  Divergence between the two
        #: channels therefore indicates a SENSOR fault, not a thermal one.
        self.raw_dt_min: deque[float] = deque(maxlen=cfg.balance_sample_count)
        #: Per-pack rise above ambient (max sensors), when an ambient sensor
        #: is configured. Empty otherwise - the feature degrades silently.
        #: (timestamp, [rise per pack]) - timestamps retained so the baseline
        #: can require a multi-day span (pack cooling constant is ~hours).
        self.thermal_rise: deque[tuple[float, list[float]]] = deque(
            maxlen=cfg.balance_sample_count)
        self.baseline_rise: list[float] | None = None
        self.sample_soc: deque[float] = deque(maxlen=cfg.balance_sample_count)
        self.last_included: list[int] = []
        self.last_excluded: list[int] = []
        self.baseline_dv: float | None = None
        self.baseline_dt: float | None = None
        self.baseline_captured_ts: float | None = None
        self.baseline_epochs: list[dict[str, Any]] = []
        self._pool_dv: list[float] = []
        self._pool_dt: list[float] = []
        self._median_cache: float | None = None
        self.last_ceiling: float | None = None

    def _gate_soc(self, s: HealthSample) -> float:
        """Minimum SOC for a balance sample, relative to the charge ceiling."""
        cfg = self._cfg
        ceiling = s.charge_ceiling_soc
        if ceiling is None:
            return cfg.balance_min_soc
        return max(cfg.balance_min_soc_floor, ceiling - cfg.balance_ceiling_margin)

    def feed(self, s: HealthSample, learn: bool = True) -> None:
        """Process a sample.

        When *learn* is False (learning switch off, or still settling after a
        recovery) raw dV/dT are still recorded so the sensors keep displaying
        live values, but no score is accumulated, no baseline is captured and
        no epoch is started - nothing irreversible happens on data that may be
        untrustworthy.
        """
        cfg = self._cfg
        if s.soc is None or s.power_w is None:
            return

        ceiling = s.charge_ceiling_soc
        if ceiling is not None:
            if (
                learn
                and self.last_ceiling is not None
                and abs(ceiling - self.last_ceiling) >= 1.0
            ):
                self.new_epoch(
                    f"charge ceiling changed {self.last_ceiling:.0f}% -> "
                    f"{ceiling:.0f}%", ts=s.timestamp)
            if learn or self.last_ceiling is None:
                self.last_ceiling = ceiling

        if s.soc < self._gate_soc(s) or abs(s.power_w) > cfg.balance_rest_power_w:
            return

        included, excluded, volts, temps = [], [], [], []
        temps_min: list[float] = []
        for idx, pack in enumerate(s.packs, start=1):
            if pack.online and pack.voltage is not None and pack.temp_max is not None:
                included.append(idx)
                volts.append(pack.voltage)
                temps.append(pack.temp_max)
                if pack.temp_min is not None:
                    temps_min.append(pack.temp_min)
            else:
                excluded.append(idx)
        if len(included) < 2:
            return
        self.last_included, self.last_excluded = included, excluded

        dv = max(volts) - min(volts)
        dt = max(temps) - min(temps)
        self.raw_dv.append(dv)
        self.raw_dt.append(dt)
        if len(temps_min) >= 2:
            self.raw_dt_min.append(max(temps_min) - min(temps_min))
        if s.ambient_temp_c is not None:
            self.thermal_rise.append(
                (s.timestamp, [t - s.ambient_temp_c for t in temps]))
        self.sample_soc.append(s.soc)

        if not learn:
            return          # raw values recorded above; nothing irreversible

        if cfg.balance_use_baseline and self.baseline_dv is None:
            self._pool_dv.append(dv)
            self._pool_dt.append(dt)
            if len(self._pool_dv) >= cfg.balance_baseline_min_samples:
                self.set_baseline(
                    _median(self._pool_dv), _median(self._pool_dt),
                    reason="auto: first %d samples" % len(self._pool_dv),
                    ts=s.timestamp)
            return   # no score until a baseline exists

        base_dv = self.baseline_dv if cfg.balance_use_baseline else 0.0
        base_dt = self.baseline_dt if cfg.balance_use_baseline else 0.0
        if base_dv is None or base_dt is None:
            return
        dev_v = max(0.0, dv - base_dv)
        dev_t = max(0.0, dt - base_dt)
        score_v = 100.0 - clip(
            (dev_v - cfg.balance_dv_dev_full_score)
            / (cfg.balance_dv_dev_zero_score - cfg.balance_dv_dev_full_score) * 100.0,
            0.0, 100.0)
        score_t = 100.0 - clip(
            (dev_t - cfg.balance_dt_dev_full_score)
            / (cfg.balance_dt_dev_zero_score - cfg.balance_dt_dev_full_score) * 100.0,
            0.0, 100.0)
        self.scores.append((score_v + score_t) / 2.0)
        self._median_cache = None

    def set_baseline(self, dv: float, dt: float, reason: str,
                     ts: float | None = None) -> None:
        """Anchor balance scoring to a measured resting spread (new epoch)."""
        prev = (self.baseline_dv, self.baseline_dt)
        self.baseline_epochs.append(
            {"ts": ts, "dv": round(dv, 3), "dt": round(dt, 2), "reason": reason,
             "previous_dv": prev[0], "previous_dt": prev[1]})
        self.baseline_dv, self.baseline_dt = dv, dt
        # Only anchor thermal rise once the samples SPAN days. Pack cooling
        # runs ~-0.4 C/h, so consecutive samples from a single afternoon carry
        # that afternoon's load history, not the installation's norm.
        if self.thermal_rise:
            span_days = (
                self.thermal_rise[-1][0] - self.thermal_rise[0][0]
            ) / SECONDS_PER_DAY
            if span_days >= self._cfg.thermal_rise_baseline_min_span_days:
                n = len(self.thermal_rise[-1][1])
                self.baseline_rise = [
                    _median([r[1][i] for r in self.thermal_rise if len(r[1]) > i])
                    for i in range(n)
                ]
            else:
                _LOGGER.debug(
                    "battery_health: thermal-rise baseline deferred - samples "
                    "span %.1f of %.1f required days",
                    span_days, self._cfg.thermal_rise_baseline_min_span_days)
        self.baseline_captured_ts = ts
        self._pool_dv.clear()
        self._pool_dt.clear()
        self._median_cache = None
        _LOGGER.warning(
            "battery_health: pack-balance baseline set to dV=%.3f V dT=%.2f C "
            "- %s. Raw dV/dT remain exposed and are unaffected.", dv, dt, reason)

    def new_epoch(self, reason: str, ts: float | None = None) -> None:
        prev = (self.baseline_dv, self.baseline_dt)
        self.baseline_epochs.append(
            {"ts": ts, "dv": None, "dt": None, "reason": reason,
             "previous_dv": prev[0], "previous_dt": prev[1]})
        self.baseline_dv = self.baseline_dt = None
        self.baseline_rise = None
        self.baseline_captured_ts = None
        self._pool_dv.clear()
        self._pool_dt.clear()
        self.scores.clear()
        self._median_cache = None
        _LOGGER.warning("battery_health: pack-balance baseline epoch restarted (%s)", reason)

    def reset_baseline(self) -> None:
        self.new_epoch("manual reset")

    def soh_balance(self) -> tuple[float | None, dict[str, Any]]:
        attrs: dict[str, Any] = {
            "balance_sample_count": len(self.scores),
            "packs_included": self.last_included,
            "packs_excluded": self.last_excluded,
            # Ground truth - never re-zeroed by any recalibration.
            "balance_raw_dv": round(self.raw_dv[-1], 3) if self.raw_dv else None,
            "balance_raw_dt": round(self.raw_dt[-1], 2) if self.raw_dt else None,
            "balance_raw_dt_min_sensors": (
                round(self.raw_dt_min[-1], 2) if self.raw_dt_min else None),
            # Deviation from the learned norm, in physical units - more
            # interpretable than the 0-100 score it feeds.
            "balance_dv_deviation": (
                round(self.raw_dv[-1] - self.baseline_dv, 3)
                if self.raw_dv and self.baseline_dv is not None else None),
            "balance_dt_deviation": (
                round(self.raw_dt[-1] - self.baseline_dt, 2)
                if self.raw_dt and self.baseline_dt is not None else None),
            "thermal_rise_above_ambient": (
                [round(v, 2) for v in self.thermal_rise[-1][1]]
                if self.thermal_rise else None),
            "thermal_rise_max": (
                round(max(self.thermal_rise[-1][1]), 2)
                if self.thermal_rise else None),
            "thermal_rise_baseline_max": (
                round(max(self.baseline_rise), 2) if self.baseline_rise else None),
            "thermal_rise_deviation": (
                round(max(self.thermal_rise[-1][1]) - max(self.baseline_rise), 2)
                if self.thermal_rise and self.baseline_rise else None),
            "balance_channel_disagreement": (
                round(abs(self.raw_dt[-1] - self.raw_dt_min[-1]), 2)
                if self.raw_dt and self.raw_dt_min else None),
            "balance_baseline_dv": self.baseline_dv,
            "balance_baseline_dt": self.baseline_dt,
            "balance_baseline_captured": self.baseline_captured_ts,
            "balance_baseline_epochs": len(self.baseline_epochs),
            "balance_sample_soc_mean": (
                round(sum(self.sample_soc) / len(self.sample_soc), 1)
                if self.sample_soc else None),
        }
        if not self.scores:
            return None, attrs
        if self._median_cache is None:
            self._median_cache = _median(list(self.scores))
        return self._median_cache, attrs

    def to_dict(self) -> dict[str, Any]:
        return {
            "scores": list(self.scores),
            "raw_dv": list(self.raw_dv),
            "raw_dt": list(self.raw_dt),
            "raw_dt_min": list(self.raw_dt_min),
            "thermal_rise": [[r[0], list(r[1])] for r in self.thermal_rise],
            "baseline_rise": self.baseline_rise,
            "sample_soc": list(self.sample_soc),
            "included": self.last_included,
            "excluded": self.last_excluded,
            "baseline_dv": self.baseline_dv,
            "baseline_dt": self.baseline_dt,
            "baseline_captured_ts": self.baseline_captured_ts,
            "baseline_epochs": self.baseline_epochs,
            "pool_dv": self._pool_dv,
            "pool_dt": self._pool_dt,
            "last_ceiling": self.last_ceiling,
        }

    def restore(self, data: dict[str, Any]) -> None:
        n = self._cfg.balance_sample_count
        self.scores = deque(data.get("scores", []), maxlen=n)
        self.raw_dv = deque(data.get("raw_dv", []), maxlen=n)
        self.raw_dt = deque(data.get("raw_dt", []), maxlen=n)
        self.raw_dt_min = deque(data.get("raw_dt_min", []), maxlen=n)
        self.thermal_rise = deque(
            [(float(r[0]), list(r[1])) for r in data.get("thermal_rise", [])
             if isinstance(r, (list, tuple)) and len(r) == 2],
            maxlen=n)
        self.baseline_rise = data.get("baseline_rise")
        self.sample_soc = deque(data.get("sample_soc", []), maxlen=n)
        self.last_included = list(data.get("included", []))
        self.last_excluded = list(data.get("excluded", []))
        self.baseline_dv = data.get("baseline_dv")
        self.baseline_dt = data.get("baseline_dt")
        self.baseline_captured_ts = data.get("baseline_captured_ts")
        self.baseline_epochs = list(data.get("baseline_epochs", []))
        self._pool_dv = list(data.get("pool_dv", []))
        self._pool_dt = list(data.get("pool_dt", []))
        self.last_ceiling = data.get("last_ceiling")
        self._median_cache = None


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
            self.attributes.get("learning_enabled"),
            self.attributes.get("learning_active"),
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
        #: Finding N: last good value of each sub-score, with its timestamp.
        #: Held (not dropped) while a term is seasonally unavailable, so the
        #: renormalised composite does not step when a term disappears.
        self._held: dict[str, tuple[float, float]] = {}
        self.ceiling = CeilingMonitor(self.cfg)
        #: v1.2.1 maintenance inhibit. Disable before planned work (firmware
        #: updates), re-enable once the system is stable, so a maintenance
        #: window cannot poison the learned baselines.
        self.learning_enabled = True
        #: Automatic counterpart: unplanned reboots and coordinator recoveries
        #: cannot be prepared for, so learning is suspended for a settling
        #: period after any of them.
        self._settling_until: float | None = None
        self.settling_events = 0
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
            # A counter reset genuinely invalidates interval arithmetic:
            # energy of unknown magnitude may have flowed before the counter
            # restarted. This is a hard discard, unlike a data gap (v1.1.8).
            self.segments.discard_active("lifetime counter reset")
            self.efficiency.invalidate_anchor()
            # A counter reset means the system restarted underneath us.
            self.mark_recovery("lifetime counter reset", now=s.timestamp)
            self.dirty = True

        # v1.2.1: sanity-check and debounce the configured ceiling BEFORE any
        # tracker sees it, so a reboot artefact cannot fire a baseline epoch.
        s.charge_ceiling_soc = self.ceiling.feed(s.charge_ceiling_soc)

        learning = self.learning_active(s.timestamp)
        counter_stale = self._discharge_counter.is_stale
        seg_before = (len(self.segments.segments), self.segments.discarded_segments,
                      self.segments.gap_bridged_count)
        if learning:
            closed = self.segments.feed(s, counter_stale=counter_stale)
            if closed is not None:
                self.dirty = True
        else:
            closed = None
            self.segments.mark_gap()
        eff_base_before = self.efficiency.baseline
        bal_base_before = self.balance.baseline_dv
        bal_n_before = len(self.balance.scores)
        self.efficiency.feed(s, learn=learning)
        self.balance.feed(s, learn=learning)
        # Finding E: persist on every material state change, not only on a
        # closed segment.  The efficiency baseline in particular is a
        # once-in-a-lifetime reference that was previously lost on an
        # unclean restart.
        if (
            eff_base_before is None and self.efficiency.baseline is not None
        ) or (
            bal_base_before is None and self.balance.baseline_dv is not None
        ) or len(self.balance.scores) != bal_n_before or seg_before != (
            len(self.segments.segments), self.segments.discarded_segments,
            self.segments.gap_bridged_count
        ):
            self.dirty = True
        self.stress.feed(s)
        self.segments.prune(s.timestamp)
        self.stress.prune(s.timestamp)

        self._last_report = self._evaluate(s.timestamp)
        return self._last_report

    def set_learning_enabled(self, enabled: bool) -> None:
        """Maintenance inhibit (v1.2.1).

        Measurement and display continue; only irreversible learning stops -
        segment recording, baseline capture and epoch changes.
        """
        if enabled == self.learning_enabled:
            return
        self.learning_enabled = enabled
        self.dirty = True
        if enabled:
            # Treat the resumption as a recovery: settle before trusting data.
            self.mark_recovery("learning re-enabled")
        else:
            self.segments.mark_gap()
            _LOGGER.warning(
                "battery_health: learning DISABLED - baselines and segments "
                "are frozen. Sensors continue to display. Re-enable once the "
                "system is stable.")

    def mark_recovery(self, reason: str, now: float | None = None) -> None:
        """Suspend learning for the settling period after a recovery."""
        base = now if now is not None else time_module.time()
        self._settling_until = base + self.cfg.settling_period_s
        self.settling_events += 1
        self.segments.mark_gap()
        _LOGGER.info(
            "battery_health: settling for %.0f s after %s - measurement "
            "continues, learning paused",
            self.cfg.settling_period_s, reason)

    def learning_active(self, now: float) -> bool:
        """True when it is safe to perform irreversible learning."""
        if not self.learning_enabled:
            return False
        if self._settling_until is not None:
            if now < self._settling_until:
                return False
            self._settling_until = None
        return True

    def mark_gap(self) -> None:
        """Coordinator update failed.

        v1.1.8: the segment tracker marks the gap pending (bridged on resume
        if short enough) and the efficiency anchor is left intact — both
        measurements are built from absolute/cumulative readings and survive
        missing samples. Only the stress accumulator must exclude the gap,
        since it integrates over *time* and an outage is not a calm period.
        """
        self.segments.mark_gap()
        self.stress.mark_gap()

    @property
    def report(self) -> HealthReport:
        return self._last_report

    def reset_efficiency_baseline(self) -> None:
        self.efficiency.reset_baseline()
        self.dirty = True

    def reset_balance_baseline(self) -> None:
        """Re-anchor pack-balance scoring (raw dV/dT are unaffected)."""
        self.balance.reset_baseline()
        self.dirty = True

    def reanchor_capacity_reference(self) -> bool:
        """Re-anchor SOH capacity to the current measured estimate.

        Refuses when there is not enough data to anchor on, so a reference
        cannot be captured from noise. Returns True if applied.
        """
        segs = self.segments.segments
        if len(segs) < self.cfg.capacity_reference_min_segments:
            _LOGGER.warning(
                "battery_health: refusing to re-anchor capacity reference - "
                "%d of %d required segments available",
                len(segs), self.cfg.capacity_reference_min_segments)
            return False
        span_days = (
            max(s.end_ts for s in segs) - min(s.start_ts for s in segs)
        ) / SECONDS_PER_DAY
        if span_days < self.cfg.capacity_reference_min_span_days:
            _LOGGER.warning(
                "battery_health: refusing to re-anchor capacity reference - "
                "segments span only %.0f of the %.0f days required to average "
                "out seasonal operating-range effects",
                span_days, self.cfg.capacity_reference_min_span_days)
            return False
        self.segments.set_reference(
            _median(sorted(s.implied_capacity_kwh for s in segs)),
            reason="manual re-anchor", ts=max(s.end_ts for s in segs))
        self.dirty = True
        return True

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

        # Finding N: a sub-score that is seasonally unavailable is HELD at its
        # last good value (up to subscore_hold_days) rather than dropped.
        # Otherwise the renormalised composite steps at the seasonal boundary
        # purely because a term appeared or vanished - a change with no
        # underlying health meaning.
        live = {"capacity": r.soh_capacity, "efficiency": r.soh_efficiency,
                "balance": r.soh_balance}
        held_terms: list[str] = []
        for name, value in live.items():
            if value is not None:
                self._held[name] = (value, now)
            else:
                prev = self._held.get(name)
                if prev is not None and (now - prev[1]) <= (
                    cfg.subscore_hold_days * SECONDS_PER_DAY
                ):
                    live[name] = prev[0]
                    held_terms.append(name)
                elif prev is not None:
                    self._held.pop(name, None)

        terms = [
            ("capacity", live["capacity"], w_cap),
            ("efficiency", live["efficiency"], w_eff),
            ("balance", live["balance"], w_bal),
        ]
        available = [(n, v, w) for n, v, w in terms if v is not None]
        r.attributes["contributing_terms"] = [n for n, _, _ in available]
        r.attributes["held_terms"] = held_terms
        # v1.3.20 FIX (Defect X1, independent ICS audit): `available` being
        # non-empty does not guarantee total_w > 0 -- a term is included
        # whenever its VALUE is not None, regardless of its WEIGHT. The
        # options flow lets weight_capacity/weight_efficiency/weight_balance
        # each independently go to 0.0 with no cross-field validation
        # (config_flow.py's vol.Range(min=0.0, max=1.0) on all three), so a
        # user setting all three to 0 is a real, reachable configuration,
        # not just a theoretical one. This guard is deliberately in addition
        # to, not instead of, the exception isolation added in
        # battery_health_manager.py's _handle_coordinator_update for the
        # same defect -- the correct fix lives here, at the actual point of
        # potential division; the isolation there is a second, independent
        # line of defence should some other unforeseen path reach this
        # method with a similarly degenerate input.
        if available:
            total_w = sum(w for _, _, w in available)
            if total_w > 0:
                r.bhi = round(sum(v * w for _, v, w in available) / total_w, 1)
            else:
                _LOGGER.warning(
                    "battery_health: all configured weights are 0 -- BHI "
                    "cannot be computed. Check weight_capacity/"
                    "weight_efficiency/weight_balance in the integration's "
                    "options."
                )

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
        # Finding D: prefer the true battery install date; first_seen_ts only
        # records when this integration started observing, which understates
        # calendar aging for an already-installed battery.
        age_origin = (
            cfg.battery_install_ts
            if cfg.battery_install_ts is not None
            else self.first_seen_ts
        )
        if age_origin is not None:
            age_years = max(0.0, (now - age_origin) / (365.25 * SECONDS_PER_DAY))
            r.attributes["battery_age_days"] = round((now - age_origin) / SECONDS_PER_DAY)
            r.attributes["battery_age_source"] = (
                "install_date" if cfg.battery_install_ts is not None
                else "first_seen")
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

        r.attributes["learning_enabled"] = self.learning_enabled
        r.attributes["learning_active"] = self.learning_active(now)
        r.attributes["settling_events"] = self.settling_events
        r.attributes["ceiling_rejected_readings"] = self.ceiling.rejected_count
        r.attributes["ceiling_confirmed_changes"] = self.ceiling.debounced_count
        r.attributes["counter_resets"] = (
            self._charge_counter.reset_count + self._discharge_counter.reset_count
        )
        return r

    # ── persistence ─────────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "first_seen_ts": self.first_seen_ts,
            "held_subscores": {k: list(v) for k, v in self._held.items()},
            "learning_enabled": self.learning_enabled,
            "settling_events": self.settling_events,
            "ceiling": self.ceiling.to_dict(),
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
        self._held = {
            k: (float(v[0]), float(v[1]))
            for k, v in (data.get("held_subscores") or {}).items()
            if isinstance(v, (list, tuple)) and len(v) == 2
        }
        self.learning_enabled = bool(data.get("learning_enabled", True))
        self.settling_events = int(data.get("settling_events", 0))
        self.ceiling.restore(data.get("ceiling", {}))
        self.segments.restore(data.get("segments", {}))
        self.efficiency.restore(data.get("efficiency", {}))
        self.balance.restore(data.get("balance", {}))
        self.stress.restore(data.get("stress", {}))
        self._charge_counter.restore(data.get("charge_counter", {}))
        self._discharge_counter.restore(data.get("discharge_counter", {}))
        self.dirty = False
