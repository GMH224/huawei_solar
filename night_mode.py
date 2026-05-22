"""Night-mode / low-power-mode detector for Huawei Solar.

The SUN2000 enters a low-power sleep state at night when PV input drops to
zero.  During this period the inverter still responds to Modbus but most
registers are frozen — polling at 30 s intervals generates unnecessary traffic
that keeps the communication bus busy and can trigger error responses.

This module provides ``NightModeDetector``, a per-inverter singleton that:

  1. Watches the inverter's INPUT_POWER (PV string power) reported in the
     most recent coordinator result.
  2. Transitions to NIGHT mode when power has been at or below
     ``NIGHT_POWER_THRESHOLD_W`` for ``NIGHT_ENTRY_HOLD`` consecutive polls.
  3. Returns to DAY mode immediately when power rises above
     ``DAY_POWER_THRESHOLD_W``.
  4. Respects DEVICE_STATUS — if the inverter reports "Standby", "Shutdown",
     or "Sleeping" it forces NIGHT mode immediately.
     NOTE: "Fault" is intentionally excluded so a faulted inverter keeps
     30-second polling for timely alarm updates.
  5. Broadcasts mode changes to all registered coordinator callbacks via
     ``register_callback()`` so the poll interval is adjusted for every
     coordinator sharing the same inverter, not just the one that detected
     the transition.
  6. Persists the last-known mode to HA storage so a restart at night does
     not trigger three unnecessary 30-second polls before entering NIGHT mode.

v2.12.1 changes
---------------
- Promoted to per-inverter singleton (``get_or_create`` / ``clear_registry``).
- Added ``register_callback`` / ``unregister_callback`` for broadcast.
- Removed "fault" from ``_NIGHT_STATUS_SUBSTRINGS``.
- Added async_restore() / _async_save() for HA Store persistence.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import timedelta
from enum import Enum, auto
from typing import Any, TYPE_CHECKING

from huawei_solar import RegisterName

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

if TYPE_CHECKING:
    pass

_LOGGER = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────

# W — inverter is considered "off" below this PV input power
NIGHT_POWER_THRESHOLD_W: float = 50.0

# W — inverter is considered "on" above this (hysteresis prevents flapping)
DAY_POWER_THRESHOLD_W: float = 100.0

# Number of consecutive polls below threshold before entering NIGHT mode
NIGHT_ENTRY_HOLD: int = 3

# Device status strings that force NIGHT mode regardless of power.
# "fault" is deliberately absent — a faulted inverter needs frequent polling
# so alarm registers are refreshed promptly.
_NIGHT_STATUS_SUBSTRINGS: tuple[str, ...] = (
    "standby",
    "shutdown",
    "sleeping",
    "off",
)

# HA storage constants
_STORAGE_VERSION = 1
_STORAGE_KEY_PREFIX = "huawei_solar_night_mode_"


class InverterMode(Enum):
    DAY = auto()
    NIGHT = auto()


class NightModeDetector:
    """Per-inverter singleton that detects inverter sleep/wake transitions.

    Multiple coordinators sharing an inverter all register callbacks here.
    Only the coordinator with PV-power data in its result dict needs to call
    evaluate(); the others are notified automatically via their callbacks.
    """

    _registry: dict[str, "NightModeDetector"] = {}

    # ── class-level helpers ───────────────────────────────────────────────────

    @classmethod
    def get_or_create(cls, serial_number: str) -> "NightModeDetector":
        """Return existing singleton or create a new one."""
        if serial_number not in cls._registry:
            cls._registry[serial_number] = cls(serial_number)
        return cls._registry[serial_number]

    @classmethod
    def clear_registry(cls) -> None:
        """Remove all singletons (called on integration unload)."""
        cls._registry.clear()

    # ── instance ──────────────────────────────────────────────────────────────

    def __init__(self, serial_number: str) -> None:
        self._serial = serial_number
        self._mode: InverterMode = InverterMode.DAY
        self._below_threshold_count: int = 0
        self._callbacks: list[Callable[[InverterMode], None]] = []
        self._hass: HomeAssistant | None = None
        self._store: Store | None = None

    # ── callback registration ─────────────────────────────────────────────────

    def register_callback(
        self, cb: Callable[[InverterMode], None]
    ) -> None:
        """Register a callback invoked on every DAY ↔ NIGHT transition."""
        if cb not in self._callbacks:
            self._callbacks.append(cb)

    def unregister_callback(
        self, cb: Callable[[InverterMode], None]
    ) -> None:
        """Unregister a previously registered callback."""
        try:
            self._callbacks.remove(cb)
        except ValueError:
            pass

    # ── persistence ───────────────────────────────────────────────────────────

    def attach_hass(self, hass: HomeAssistant) -> None:
        """Attach HA instance for storage.  Called once from __init__.py."""
        if self._hass is None:
            self._hass = hass
            self._store = Store(
                hass, _STORAGE_VERSION, f"{_STORAGE_KEY_PREFIX}{self._serial}"
            )

    async def async_restore(self) -> None:
        """Restore persisted mode from HA storage (call on integration setup)."""
        if self._store is None:
            return
        try:
            data = await self._store.async_load()
        except Exception as err:
            _LOGGER.warning(
                "NightMode[%s]: could not load persisted state: %s", self._serial, err
            )
            return

        if not data or "mode" not in data:
            return

        try:
            restored = InverterMode[data["mode"]]
        except KeyError:
            return

        self._mode = restored
        if restored == InverterMode.NIGHT:
            # Pre-fill the hold counter so a single sunny poll wakes up
            # immediately instead of needing 3 polls below threshold first.
            self._below_threshold_count = NIGHT_ENTRY_HOLD
        _LOGGER.info(
            "NightMode[%s]: restored %s from storage", self._serial, restored.name
        )

    async def _async_save(self) -> None:
        """Persist the current mode to HA storage."""
        if self._store is None:
            return
        try:
            await self._store.async_save({"mode": self._mode.name})
        except Exception as err:
            _LOGGER.debug(
                "NightMode[%s]: could not save state: %s", self._serial, err
            )

    # ── public API ────────────────────────────────────────────────────────────

    @property
    def mode(self) -> InverterMode:
        return self._mode

    @property
    def is_night(self) -> bool:
        return self._mode == InverterMode.NIGHT

    def evaluate(self, result: dict[RegisterName, Any]) -> None:
        """Inspect a fresh poll result and update the mode if needed.

        Call after every successful poll on the coordinator that has PV-power
        data.  Safe to call from any coordinator — if the result dict does not
        contain a power register the method returns without changing state.
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
                    "NightMode[%s]: PV power %.1f W ≤ threshold %.1f W "
                    "(%d/%d polls below threshold)",
                    self._serial,
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
            "NightMode[%s]: %s → %s  (%s)",
            self._serial,
            old.name,
            new_mode.name,
            reason,
        )
        for cb in self._callbacks:
            cb(new_mode)
        # Persist asynchronously without blocking the coordinator
        asyncio.ensure_future(self._async_save())

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
        for candidate in ("input_power", "total_dc_input_power", "active_power"):
            val = self._get_value(result, candidate)
            if val is not None:
                try:
                    f = float(val)
                    if candidate == "active_power" and f < 0:
                        continue
                    return f
                except (TypeError, ValueError):
                    pass
        return None
