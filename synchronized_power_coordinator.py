"""SynchronizedPowerCoordinator — contiguous multi-inverter power snapshot.

Problem solved
--------------
With two inverters on the same Modbus bus the standard per-device coordinators
fire at different times.  ModbusGuard serialises them correctly but the resulting
wall-clock spread between the first and last reading can reach 3–4 seconds.  When
HA renders the Energy dashboard power-flow card it samples all entity states at a
single moment, so values measured 3 s apart can produce wildly wrong arithmetic —
especially during ramp events (cloud passing, EV charger switching on).

Solution
--------
This coordinator reads exactly the four registers needed for power-flow in one
*contiguous* block, serialised behind the primary inverter's ModbusGuard so no
other coordinator can interleave.  All four sensor entities update from the same
HA coordinator tick — their ``last_updated`` timestamps are identical.

Reads performed per poll (in order)
-------------------------------------
1. INV1  — ``INPUT_POWER``             (PV string DC power)
2. Meter — ``POWER_METER_ACTIVE_POWER``  (grid import/export; signed W)
3. Battery — ``STORAGE_CHARGE_DISCHARGE_POWER`` (signed W; + = charging)
4. INV2  — ``INPUT_POWER``             (PV string DC power, standalone inverter)

Derived values exposed as HA sensor entities
---------------------------------------------
• ``pv_power_total``     = INV1_pv + INV2_pv                 [W]
• ``grid_power``         = raw meter reading (signed)         [W]  + = import
• ``battery_power``      = raw battery reading (signed)       [W]  + = charging
• ``home_consumption``   = pv_total + grid_power − battery_power [W]

Sign convention for home_consumption
--------------------------------------
Energy conservation: PV + grid_import = home + grid_export + battery_charge
Rearranging:         home = PV + grid_power − battery_power
  • grid_power   > 0 → importing → adds to home              ✓
  • grid_power   < 0 → exporting → reduces home              ✓
  • battery_power> 0 → charging  → battery consumes, reduces home ✓
  • battery_power< 0 → discharging → battery feeds home      ✓
  • Result is clamped to ≥ 0 W; small negative values indicate
    transient measurement noise.

Architecture
------------
The coordinator acquires the primary inverter guard for the entire poll
sequence.  Because both inverters share the same physical SmartLogger / SDongle
connection, holding the primary guard for sequential reads prevents any other
coordinator from interleaving on the physical bus.  The secondary inverter's
guard is acquired *after* the primary guard releases, making the total spread
the sum of four back-to-back Modbus reads plus three inter-request gaps
(≈ 4 × 300 ms + 3 × 150 ms ≈ 1.7 s worst case, vs 3–4 s with independent
coordinators).

Error handling
--------------
If any individual read fails the coordinator logs a warning at DEBUG level and
continues with ``None`` for that slot.  Derived values that depend on a missing
reading are marked as unavailable (``None``) so HA shows "unavailable" rather
than a silently wrong number.  This is preferable to raising ``UpdateFailed``
for a partial outage.

If ALL reads fail the coordinator raises ``UpdateFailed`` so HA marks all four
entities unavailable and the normal back-off logic takes over.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from huawei_solar import (
    ConnectionInterruptedException,
    HuaweiSolarException,
    register_names as rn,
)
from huawei_solar.device.base import HuaweiSolarDevice

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import SYNC_POWER_UPDATE_INTERVAL, UPDATE_TIMEOUT
from .modbus_guard import ModbusGuard
from .modbus_telemetry import ModbusTelemetry

_LOGGER = logging.getLogger(__name__)


# ── Result dataclass ───────────────────────────────────────────────────────────

@dataclass(slots=True)
class SynchronizedPowerData:
    """Snapshot of all power-flow values from a single coordinator tick.

    All fields share the same ``last_updated`` timestamp because they are
    populated from the same ``DataUpdateCoordinator`` cycle.

    ``None`` indicates that the reading failed (device unavailable / not
    installed).  Sensors must treat ``None`` as unavailable.
    """

    #: DC power from inverter 1's PV strings (W).  Always present.
    inv1_pv_power: float | None

    #: DC power from inverter 2's PV strings (W).  ``None`` if not installed.
    inv2_pv_power: float | None

    #: Grid power, signed W.  Positive = importing, negative = exporting.
    #: ``None`` if no meter is connected to INV1.
    grid_power: float | None

    #: Battery charge/discharge power, signed W.
    #: Positive = charging (battery consuming power from system).
    #: Negative = discharging (battery supplying power to system).
    #: ``None`` if no battery is connected.
    battery_power: float | None

    #: Topology flags — distinguish "not installed" (a missing input legitimately
    #: contributes 0) from "installed but this tick's read failed" (a missing
    #: input means the derived value is unknown and must be reported as
    #: unavailable, not silently computed with a wrong term).  Default False so
    #: that a single-inverter / no-battery / no-meter system is unaffected.
    has_inv2: bool = False
    has_meter: bool = False
    has_battery: bool = False

    # ── Derived properties ────────────────────────────────────────────────────

    @property
    def pv_power_total(self) -> float | None:
        """Sum of all PV string power across both inverters.

        Returns ``None`` if INV1 is unavailable, or if a second inverter is
        installed but its reading failed this tick (otherwise the total would
        silently omit INV2's contribution and report a wrong number).
        """
        if self.inv1_pv_power is None:
            return None
        if self.has_inv2 and self.inv2_pv_power is None:
            return None
        return self.inv1_pv_power + (self.inv2_pv_power or 0)

    @property
    def home_consumption(self) -> float | None:
        """Estimated home consumption derived from the power balance equation.

        Returns ``None`` if any required reading is unavailable — including a
        battery that is installed but failed to read this tick (substituting 0
        would over/under-count home by the actual battery power).
        A small negative result (measurement noise) is clamped to 0.
        """
        pv = self.pv_power_total
        grid = self.grid_power
        batt = self.battery_power
        if pv is None or grid is None:
            return None
        if self.has_battery and batt is None:
            return None
        # Battery contribution: discharging (negative) feeds home so we subtract
        # battery_power (positive = charging reduces available power).
        raw = pv + grid - (batt or 0)
        return max(0.0, raw)


# ── Coordinator ────────────────────────────────────────────────────────────────

class SynchronizedPowerCoordinator(DataUpdateCoordinator[SynchronizedPowerData]):
    """DataUpdateCoordinator that reads all power-flow registers in one block.

    Parameters
    ----------
    inv1_device:
        The primary SUN2000 inverter device object (has meter + battery).
    inv2_device:
        The secondary SUN2000 inverter device (standalone).
        Pass ``None`` if there is only one inverter.
    has_meter:
        Whether a power meter is connected to INV1.
    has_battery:
        Whether a LUNA2000 battery is connected to INV1.
    update_interval:
        How often to poll.  Default is ``SYNC_POWER_UPDATE_INTERVAL`` (10 s).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        inv1_device: HuaweiSolarDevice,
        inv2_device: HuaweiSolarDevice | None,
        *,
        has_meter: bool,
        has_battery: bool,
        update_interval: timedelta = SYNC_POWER_UPDATE_INTERVAL,
        update_timeout: timedelta = UPDATE_TIMEOUT,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="huawei_solar_synchronized_power",
            update_interval=update_interval,
        )
        self._inv1 = inv1_device
        self._inv2 = inv2_device
        self._has_meter = has_meter
        self._has_battery = has_battery
        self._update_timeout = update_timeout
        self._telemetry: ModbusTelemetry | None = None

        # Primary guard: serialises all reads for this coordinator.
        # Because both inverters are on the same SmartLogger/SDongle TCP
        # connection, holding the primary guard prevents interleaving on the
        # shared physical bus.
        self._primary_guard = ModbusGuard.get_or_create(inv1_device.serial_number)

        # Secondary guard: acquired separately after primary releases, for the
        # INV2 read.  If INV2 has its own serial number it has its own guard;
        # if somehow the same serial is reused (shouldn't happen) this is the
        # same object — no deadlock risk because we never hold both simultaneously.
        self._secondary_guard: ModbusGuard | None = (
            ModbusGuard.get_or_create(inv2_device.serial_number)
            if inv2_device is not None
            else None
        )

        self._consecutive_failures = 0

    # ── wiring ─────────────────────────────────────────────────────────────────

    def attach_telemetry(self, telemetry: ModbusTelemetry) -> None:
        """Wire in a ModbusTelemetry instance (called from __init__.py)."""
        self._telemetry = telemetry

    # ── poll ───────────────────────────────────────────────────────────────────

    async def _async_update_data(self) -> SynchronizedPowerData:
        """Read all power-flow registers in one contiguous Modbus sequence.

        Each read is a minimal single-register ``batch_update`` call guarded by
        the primary guard so the block is uninterrupted.  Partial failures are
        tolerated and reported as ``None`` in the result.
        """
        inv1_pv: float | None = None
        grid: float | None = None
        battery: float | None = None
        inv2_pv: float | None = None
        any_success = False

        timeout = self._update_timeout.total_seconds()

        # ── reads 1–3: all through the primary guard ──────────────────────────

        # Read 1: INV1 PV power
        try:
            async with self._primary_guard.request():
                async with asyncio.timeout(timeout):
                    result = await self._inv1.batch_update([rn.INPUT_POWER])
                    inv1_pv = _extract_w(result, rn.INPUT_POWER)
                    any_success = True
                    if self._telemetry:
                        self._telemetry.record_request(1)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug(
                "SyncPower: failed to read INV1 INPUT_POWER: %s", exc
            )
            if self._telemetry:
                _record_failure(self._telemetry, exc)

        # Read 2: grid meter (only if meter is present and INV1 read succeeded
        # enough that the guard released cleanly)
        if self._has_meter:
            try:
                async with self._primary_guard.request():
                    async with asyncio.timeout(timeout):
                        result = await self._inv1.batch_update(
                            [rn.POWER_METER_ACTIVE_POWER]
                        )
                        grid = _extract_w(result, rn.POWER_METER_ACTIVE_POWER)
                        any_success = True
                        if self._telemetry:
                            self._telemetry.record_request(1)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug(
                    "SyncPower: failed to read POWER_METER_ACTIVE_POWER: %s", exc
                )
                if self._telemetry:
                    _record_failure(self._telemetry, exc)

        # Read 3: battery (only if battery is present)
        if self._has_battery:
            try:
                async with self._primary_guard.request():
                    async with asyncio.timeout(timeout):
                        result = await self._inv1.batch_update(
                            [rn.STORAGE_CHARGE_DISCHARGE_POWER]
                        )
                        battery = _extract_w(result, rn.STORAGE_CHARGE_DISCHARGE_POWER)
                        any_success = True
                        if self._telemetry:
                            self._telemetry.record_request(1)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug(
                    "SyncPower: failed to read STORAGE_CHARGE_DISCHARGE_POWER: %s",
                    exc,
                )
                if self._telemetry:
                    _record_failure(self._telemetry, exc)

        # ── read 4: INV2 PV power (secondary guard) ───────────────────────────
        if self._inv2 is not None and self._secondary_guard is not None:
            try:
                async with self._secondary_guard.request():
                    async with asyncio.timeout(timeout):
                        result = await self._inv2.batch_update([rn.INPUT_POWER])
                        inv2_pv = _extract_w(result, rn.INPUT_POWER)
                        any_success = True
                        if self._telemetry:
                            self._telemetry.record_request(1)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug(
                    "SyncPower: failed to read INV2 INPUT_POWER: %s", exc
                )
                if self._telemetry:
                    _record_failure(self._telemetry, exc)

        # ── failure handling ──────────────────────────────────────────────────
        if not any_success:
            self._consecutive_failures += 1
            raise UpdateFailed(
                f"SynchronizedPowerCoordinator: all reads failed "
                f"(consecutive: {self._consecutive_failures})"
            )

        if self._consecutive_failures > 0:
            _LOGGER.info(
                "SyncPower: communication restored after %d consecutive failure(s)",
                self._consecutive_failures,
            )
        self._consecutive_failures = 0

        return SynchronizedPowerData(
            inv1_pv_power=inv1_pv,
            inv2_pv_power=inv2_pv,
            grid_power=grid,
            battery_power=battery,
            has_inv2=self._inv2 is not None,
            has_meter=self._has_meter,
            has_battery=self._has_battery,
        )


# ── helpers ────────────────────────────────────────────────────────────────────

def _extract_w(result: dict[Any, Any], key: Any) -> float | None:
    """Pull the numeric watt value from a batch_update result dict."""
    try:
        val = result[key].value
        return float(val) if val is not None else None
    except (KeyError, AttributeError, TypeError, ValueError):
        return None


def _record_failure(telemetry: ModbusTelemetry, exc: Exception) -> None:
    """Route the exception to the appropriate telemetry counter."""
    if isinstance(exc, TimeoutError):
        telemetry.record_timeout()
    else:
        telemetry.record_failure()
