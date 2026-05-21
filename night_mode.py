"""Night-mode / low-power-mode detector for Huawei Solar.

The SUN2000 enters a low-power sleep state at night when PV input drops to
zero.  During this period the inverter still responds to Modbus but most
registers are frozen — polling at 30 s intervals generates unnecessary traffic
that keeps the communication bus busy and can trigger error responses.

This module provides ``NightModeDetector``, a lightweight state-machine that:

  1. Watches the inverter's INPUT_POWER (PV string power) reported in the
     most recent coordinator result.
  2. Transitions to NIGHT mode when power has been at or below
     ``NIGHT_POWER_THRESHOLD_W`` for ``NIGHT_ENTRY_HOLD`` consecutive polls.
  3. Returns to DAY mode immediately when power rises above
     ``DAY_POWER_THRESHOLD_W``.
  4. Also respects DEVICE_STATUS — if the inverter reports "Standby" or
     "Shutdown" the detector forces NIGHT mode immediately.
  5. Notifies the coordinator via ``on_mode_change`` callback so it can
     adjust its poll interval and instruct the cache to stretch TTLs.

Integration
-----------
``NightModeDetector`` is instantiated once per coordinator in
``HuaweiSolarUpdateCoordinator.__init__`` and called at the end of every
successful ``_async_update_data`` with the fresh result dict.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta
from enum import Enum, auto
from typing import Any, TYPE_CHECKING

from huawei_solar import RegisterName

try:
    from huawei_solar import register_names as rn
    _HAS_RN = True
except ImportError:
    _HAS_RN = False

_LOGGER = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────

# W — inverter is considered "off" below this PV input power
NIGHT_POWER_THRESHOLD_W: float = 50.0

# W — inverter is considered "on" above this (hysteresis prevents flapping)
DAY_POWER_THRESHOLD_W: float = 100.0

# Number of consecutive polls below threshold before entering NIGHT mode
NIGHT_ENTRY_HOLD: int = 3

# Device status strings that force NIGHT mode regardless of power
_NIGHT_STATUS_SUBSTRINGS: tuple[str, ...] = (
    "standby",
    "shutdown",
    "sleeping",
    "off",
    "fault",      # during fault, also reduce traffic
)


class InverterMode(Enum):
    DAY = auto()
    NIGHT = auto()


class NightModeDetector:
    """State-machine that detects inverter sleep/wake transitions.

    Parameters
    ----------
    on_mode_change:
        Callback invoked whenever the mode transitions.
        Signature: ``callback(new_mode: InverterMode) -> None``.
    poll_interval_day:
        The poll interval the coordinator uses in DAY mode.
    poll_interval_night:
        The poll interval the coordinator should use in NIGHT mode.
    """

    def __init__(
        self,
        on_mode_change: Callable[[InverterMode], None],
        poll_interval_day: timedelta,
        poll_interval_night: timedelta,
    ) -> None:
        self._on_mode_change = on_mode_change
        self.poll_interval_day = poll_interval_day
        self.poll_interval_night = poll_interval_night
        self._mode: InverterMode = InverterMode.DAY
        self._below_threshold_count: int = 0

    # ── public API ────────────────────────────────────────────────────────────

    @property
    def mode(self) -> InverterMode:
        return self._mode

    @property
    def is_night(self) -> bool:
        return self._mode == InverterMode.NIGHT

    def current_poll_interval(self) -> timedelta:
        """Return the poll interval appropriate for the current mode."""
        return (
            self.poll_interval_night
            if self._mode == InverterMode.NIGHT
            else self.poll_interval_day
        )

    def evaluate(self, result: dict[RegisterName, Any]) -> None:
        """Inspect a fresh poll result and update the mode if needed.

        This must be called after every successful poll, passing the merged
        result dict from the coordinator.
        """
        if not result:
            return

        # ── Check device status first (instant transition) ────────────────
        status_val = self._get_value(result, "device_status")
        if status_val is not None:
            status_str = str(status_val).lower()
            if any(sub in status_str for sub in _NIGHT_STATUS_SUBSTRINGS):
                self._below_threshold_count = NIGHT_ENTRY_HOLD  # force entry
                self._transition(InverterMode.NIGHT, reason=f"device_status={status_str!r}")
                return

        # ── Check PV input power ──────────────────────────────────────────
        pv_power = self._get_power(result)

        if pv_power is None:
            # Can't determine — stay in current mode
            return

        if self._mode == InverterMode.DAY:
            if pv_power <= NIGHT_POWER_THRESHOLD_W:
                self._below_threshold_count += 1
                _LOGGER.debug(
                    "NightMode: PV power %.1f W ≤ threshold %.1f W "
                    "(%d/%d polls below threshold)",
                    pv_power,
                    NIGHT_POWER_THRESHOLD_W,
                    self._below_threshold_count,
                    NIGHT_ENTRY_HOLD,
                )
                if self._below_threshold_count >= NIGHT_ENTRY_HOLD:
                    self._transition(
                        InverterMode.NIGHT,
                        reason=f"PV power {pv_power:.1f} W for {self._below_threshold_count} polls",
                    )
            else:
                self._below_threshold_count = 0

        else:  # NIGHT mode
            if pv_power >= DAY_POWER_THRESHOLD_W:
                self._below_threshold_count = 0
                self._transition(
                    InverterMode.DAY,
                    reason=f"PV power {pv_power:.1f} W ≥ wake threshold {DAY_POWER_THRESHOLD_W:.1f} W",
                )

    # ── private helpers ───────────────────────────────────────────────────────

    def _transition(self, new_mode: InverterMode, reason: str) -> None:
        if new_mode == self._mode:
            return
        old = self._mode
        self._mode = new_mode
        _LOGGER.info(
            "NightMode: %s → %s  (%s)",
            old.name,
            new_mode.name,
            reason,
        )
        self._on_mode_change(new_mode)

    def _get_value(self, result: dict, key_substr: str) -> Any:
        """Find a result value by substring match on the register name."""
        for rname, res in result.items():
            if key_substr in str(rname).lower():
                try:
                    return res.value
                except Exception:
                    return res
        return None

    def _get_power(self, result: dict) -> float | None:
        """Extract the best available PV power reading from the result."""
        # Preference order: INPUT_POWER > TOTAL_DC_INPUT_POWER > ACTIVE_POWER
        for candidate in ("input_power", "total_dc_input_power", "active_power"):
            val = self._get_value(result, candidate)
            if val is not None:
                try:
                    f = float(val)
                    # ACTIVE_POWER can be negative (import from grid) — ignore
                    if candidate == "active_power" and f < 0:
                        continue
                    return f
                except (TypeError, ValueError):
                    pass
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Idea 8: MPPT sweep detector
# ──────────────────────────────────────────────────────────────────────────────

# A voltage dip of this percentage (relative to last reading) signals an MPPT sweep
MPPT_VOLTAGE_DIP_THRESHOLD: float = 0.02   # 2 % drop triggers detection

# Number of consecutive string-voltage readings we track for dip detection
_MPPT_HISTORY: int = 3


class MpptSweepDetector:
    """Detects MPPT sweep events from PV string voltage readings.

    The SUN2000 performs a maximum-power-point tracking sweep every few
    minutes.  During the ~50 ms sweep the inverter cannot respond reliably
    to Modbus requests, which causes spurious timeouts.

    This detector watches the PV string voltage (or total DC input power)
    across consecutive poll results.  When a sudden dip followed by a recovery
    is observed it sets ``sweep_detected = True`` for one cycle so the
    coordinator can insert a brief pause (MPPT_SWEEP_PAUSE) before the next
    Modbus request.

    Usage
    -----
    In ``_async_update_data``, after updating the cache but before returning:

        if self._mppt_detector.evaluate(merged_result):
            await asyncio.sleep(MPPT_SWEEP_PAUSE.total_seconds())
    """

    def __init__(self) -> None:
        self._voltage_history: list[float] = []
        self._sweep_detected: bool = False

    @property
    def sweep_detected(self) -> bool:
        """True if the most recent evaluate() call saw a sweep signature."""
        return self._sweep_detected

    def evaluate(self, result: dict) -> bool:
        """Inspect a fresh result dict for an MPPT sweep voltage dip.

        Returns True if a sweep was just detected (caller should pause).
        """
        voltage = self._get_voltage(result)
        if voltage is None or voltage <= 0.0:
            self._sweep_detected = False
            return False

        self._voltage_history.append(voltage)
        if len(self._voltage_history) > _MPPT_HISTORY:
            self._voltage_history.pop(0)

        if len(self._voltage_history) < 2:
            self._sweep_detected = False
            return False

        # Dip: latest voltage significantly below the previous reading
        prev = self._voltage_history[-2]
        if prev > 0.0:
            dip = (prev - voltage) / prev
            if dip >= MPPT_VOLTAGE_DIP_THRESHOLD:
                _LOGGER.debug(
                    "MpptSweepDetector: voltage dip %.1f %% detected "
                    "(%.1f V → %.1f V) — inserting post-sweep pause",
                    dip * 100,
                    prev,
                    voltage,
                )
                self._sweep_detected = True
                return True

        self._sweep_detected = False
        return False

    def _get_voltage(self, result: dict) -> float | None:
        """Extract the best available PV voltage reading."""
        # Try PV string 1 voltage first, fall back to any voltage reading
        for key_substr in ("pv1_voltage", "pv_1_voltage", "pv01_voltage",
                           "total_dc_input", "input_power"):
            for rname, res in result.items():
                if key_substr in str(rname).lower():
                    try:
                        v = float(res.value if hasattr(res, "value") else res)
                        if v > 0:
                            return v
                    except (TypeError, ValueError):
                        pass
        return None
