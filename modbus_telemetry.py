"""Modbus traffic telemetry for Huawei Solar.

Tracks per-device Modbus statistics (requests, failures, timeouts, cache hits)
over a rolling 1-hour window and exposes them as HA sensor entities.

Architecture
------------
ModbusTelemetry  – singleton-per-serial-number, thread-safe via asyncio.
                   Stored in hass.data[DOMAIN]["telemetry"][serial_number].

HuaweiSolarModbusTelemetrySensorEntity
                 – standard HA SensorEntity that pulls values from the
                   singleton on each HA poll (no coordinator needed).

Integration wiring
------------------
1. ModbusTelemetry.get_or_create(hass, serial_number) in __init__.py after
   each coordinator is built — returns the singleton.
2. Coordinators call record_request() / record_failure() / record_timeout() /
   record_cache_hit() on the singleton.
3. sensor.py calls create_telemetry_entities() to register the HA sensors.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta
import logging
import time
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Rolling window for rate calculations
_WINDOW = timedelta(hours=1)
_WINDOW_SEC = _WINDOW.total_seconds()

# How often the HA sensors are pushed an update (independent of poll cycle)
_TELEMETRY_PUSH_INTERVAL = timedelta(seconds=60)


# ──────────────────────────────────────────────────────────────────────────────
# Core telemetry object
# ──────────────────────────────────────────────────────────────────────────────

class ModbusTelemetry:
    """Per-device rolling Modbus traffic statistics.

    Uses deques of timestamps for O(1) append and O(k) windowed-count, where k
    is typically very small (≤ 120 events/hour at 30 s poll intervals).
    """

    _registry: dict[str, "ModbusTelemetry"] = {}

    # ── class-level helpers ───────────────────────────────────────────────────

    @classmethod
    def get_or_create(
        cls, hass: HomeAssistant, serial_number: str, device_info: DeviceInfo
    ) -> "ModbusTelemetry":
        """Return existing singleton or create a new one."""
        if serial_number not in cls._registry:
            cls._registry[serial_number] = cls(hass, serial_number, device_info)
        return cls._registry[serial_number]

    @classmethod
    def get(cls, serial_number: str) -> "ModbusTelemetry | None":
        """Return existing singleton or None."""
        return cls._registry.get(serial_number)

    @classmethod
    def clear_registry(cls) -> None:
        """Remove all singletons (called on integration unload)."""
        cls._registry.clear()

    # ── instance ──────────────────────────────────────────────────────────────

    def __init__(
        self, hass: HomeAssistant, serial_number: str, device_info: DeviceInfo
    ) -> None:
        self.hass = hass
        self.serial_number = serial_number
        self.device_info = device_info

        # Rolling timestamp deques — one entry per event
        self._requests: deque[float] = deque()
        self._failures: deque[float] = deque()
        self._timeouts: deque[float] = deque()
        self._cache_hits: deque[float] = deque()
        self._batch_sizes: deque[int] = deque()

        # Lifetime totals (never reset)
        self.total_requests: int = 0
        self.total_failures: int = 0
        self.total_timeouts: int = 0
        self.total_cache_hits: int = 0
        self.total_skipped_polls: int = 0
        self._night_mode: bool = False
        self._guard_gap_ms: float = 150.0
        self._mppt_sweeps_detected: int = 0

        # Derived metrics updated on each call to snapshot()
        self._last_snapshot: dict[str, Any] = {}

        # HA entity update callbacks registered via add_listener()
        self._listeners: list[callback] = []

        # Schedule periodic push to HA entities
        self._unsub = async_track_time_interval(
            hass, self._push_to_listeners, _TELEMETRY_PUSH_INTERVAL
        )

    # ── event recording (called from coordinators) ────────────────────────────

    def record_request(self, batch_size: int = 1) -> None:
        """Record a Modbus request."""
        now = time.monotonic()
        self._requests.append(now)
        self._batch_sizes.append(batch_size)
        self.total_requests += 1
        self._evict(now)

    def record_failure(self) -> None:
        """Record a non-timeout failure."""
        now = time.monotonic()
        self._failures.append(now)
        self.total_failures += 1

    def record_timeout(self) -> None:
        """Record a timeout."""
        now = time.monotonic()
        self._timeouts.append(now)
        self.total_timeouts += 1
        self.total_failures += 1

    def record_cache_hit(self) -> None:
        """Record a register cache hit (request skipped)."""
        now = time.monotonic()
        self._cache_hits.append(now)
        self.total_cache_hits += 1

    def record_skipped_poll(self) -> None:
        """Record a poll that was entirely skipped (back-off / dedup)."""
        self.total_skipped_polls += 1

    def record_night_mode(self, active: bool) -> None:
        """Record the current night-mode state (for snapshot reporting)."""
        self._night_mode = active

    def record_guard_gap(self, gap_ms: float) -> None:
        """Record the current dynamic guard gap for reporting."""
        self._guard_gap_ms = gap_ms

    def record_mppt_sweep(self) -> None:
        """Record a detected MPPT sweep event."""
        self._mppt_sweeps_detected += 1

    # ── derived metric helpers ────────────────────────────────────────────────

    def _evict(self, now: float) -> None:
        """Remove entries older than the rolling window from all deques."""
        cutoff = now - _WINDOW_SEC
        for dq in (
            self._requests,
            self._failures,
            self._timeouts,
            self._cache_hits,
        ):
            while dq and dq[0] < cutoff:
                dq.popleft()
        # batch_sizes can grow independently — trim to same length as requests
        while len(self._batch_sizes) > len(self._requests):
            self._batch_sizes.popleft()

    def snapshot(self) -> dict[str, Any]:
        """Return a point-in-time snapshot of all metrics."""
        now = time.monotonic()
        self._evict(now)

        req_ph = len(self._requests)
        fail_ph = len(self._failures)
        to_ph = len(self._timeouts)
        cache_ph = len(self._cache_hits)

        avg_batch = (
            round(sum(self._batch_sizes) / len(self._batch_sizes), 1)
            if self._batch_sizes
            else 0.0
        )
        failure_rate = (
            round(fail_ph / req_ph * 100, 1) if req_ph else 0.0
        )

        snap = {
            "requests_per_hour": req_ph,
            "failures_per_hour": fail_ph,
            "timeouts_per_hour": to_ph,
            "cache_hits_per_hour": cache_ph,
            "failure_rate_percent": failure_rate,
            "avg_batch_size": avg_batch,
            "total_requests": self.total_requests,
            "total_failures": self.total_failures,
            "total_timeouts": self.total_timeouts,
            "total_cache_hits": self.total_cache_hits,
            "total_skipped_polls": self.total_skipped_polls,
            "night_mode_active": self._night_mode,
            "guard_gap_ms": round(self._guard_gap_ms, 1),
            "mppt_sweeps_detected": self._mppt_sweeps_detected,
        }
        self._last_snapshot = snap
        return snap

    # ── HA listener plumbing ──────────────────────────────────────────────────

    def add_listener(self, cb: Any) -> None:
        """Register a callback that is called when telemetry is pushed."""
        self._listeners.append(cb)

    def remove_listener(self, cb: Any) -> None:
        """Unregister a previously registered callback."""
        try:
            self._listeners.remove(cb)
        except ValueError:
            pass

    @callback
    def _push_to_listeners(self, _now: datetime) -> None:
        """Push updated telemetry to all registered HA entities.

        Each listener is called in a try/except so that a failing callback
        does not prevent the remaining listeners from receiving the update.
        """
        snap = self.snapshot()
        for cb_fn in list(self._listeners):  # iterate a copy — listener may remove itself
            try:
                cb_fn(snap)
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "Telemetry[%s]: listener %r raised, skipping",
                    self.serial_number,
                    cb_fn,
                    exc_info=True,
                )

    def stop(self) -> None:
        """Cancel the periodic push timer."""
        if self._unsub:
            self._unsub()
            self._unsub = None


# ──────────────────────────────────────────────────────────────────────────────
# HA Sensor entities
# ──────────────────────────────────────────────────────────────────────────────

# Sensor definitions: (attr_key, name, unit, icon, extra_kwargs)
_TELEMETRY_SENSORS: list[tuple[str, str, str | None, str, dict]] = [
    (
        "requests_per_hour",
        "Modbus requests / hour",
        None,
        "mdi:counter",
        {"state_class": SensorStateClass.MEASUREMENT},
    ),
    (
        "failures_per_hour",
        "Modbus failures / hour",
        None,
        "mdi:alert-circle-outline",
        {"state_class": SensorStateClass.MEASUREMENT},
    ),
    (
        "timeouts_per_hour",
        "Modbus timeouts / hour",
        None,
        "mdi:timer-off-outline",
        {"state_class": SensorStateClass.MEASUREMENT},
    ),
    (
        "cache_hits_per_hour",
        "Modbus cache hits / hour",
        None,
        "mdi:database-check-outline",
        {"state_class": SensorStateClass.MEASUREMENT},
    ),
    (
        "failure_rate_percent",
        "Modbus failure rate",
        "%",
        "mdi:percent",
        {"state_class": SensorStateClass.MEASUREMENT},
    ),
    (
        "avg_batch_size",
        "Avg Modbus batch size",
        None,
        "mdi:package-variant",
        {"state_class": SensorStateClass.MEASUREMENT},
    ),
    (
        "total_requests",
        "Modbus total requests",
        None,
        "mdi:counter",
        {
            "state_class": SensorStateClass.TOTAL_INCREASING,
            "entity_registry_enabled_default": False,
        },
    ),
    (
        "total_failures",
        "Modbus total failures",
        None,
        "mdi:alert-circle",
        {
            "state_class": SensorStateClass.TOTAL_INCREASING,
            "entity_registry_enabled_default": False,
        },
    ),
    (
        "total_cache_hits",
        "Modbus total cache hits",
        None,
        "mdi:database-check",
        {
            "state_class": SensorStateClass.TOTAL_INCREASING,
            "entity_registry_enabled_default": False,
        },
    ),
    (
        "total_skipped_polls",
        "Modbus skipped polls",
        None,
        "mdi:skip-next-circle-outline",
        {
            "state_class": SensorStateClass.TOTAL_INCREASING,
            "entity_registry_enabled_default": False,
        },
    ),
    (
        "night_mode_active",
        "Inverter night mode",
        None,
        "mdi:weather-night",
        {},   # no state_class — this is a boolean-ish string sensor
    ),
    (
        "guard_gap_ms",
        "Modbus guard gap",
        "ms",
        "mdi:timer-sand",
        {"state_class": SensorStateClass.MEASUREMENT},
    ),
    (
        "mppt_sweeps_detected",
        "MPPT sweeps detected",
        None,
        "mdi:sine-wave",
        {
            "state_class": SensorStateClass.TOTAL_INCREASING,
            "entity_registry_enabled_default": False,
        },
    ),
]


def create_telemetry_entities(
    telemetry: ModbusTelemetry,
) -> list["HuaweiSolarModbusTelemetrySensorEntity"]:
    """Create all HA sensor entities for a ModbusTelemetry instance."""
    return [
        HuaweiSolarModbusTelemetrySensorEntity(telemetry, attr_key, name, unit, icon, extra)
        for attr_key, name, unit, icon, extra in _TELEMETRY_SENSORS
    ]


class HuaweiSolarModbusTelemetrySensorEntity(SensorEntity):
    """HA Sensor backed by ModbusTelemetry — no coordinator needed."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(
        self,
        telemetry: ModbusTelemetry,
        attr_key: str,
        name: str,
        unit: str | None,
        icon: str,
        extra: dict,
    ) -> None:
        self._telemetry = telemetry
        self._attr_key = attr_key
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_device_info = telemetry.device_info
        self._attr_unique_id = (
            f"{telemetry.serial_number}_modbus_telemetry_{attr_key}"
        )
        self._attr_native_value: Any = 0

        for k, v in extra.items():
            setattr(self, f"_attr_{k}", v)

        self._cb = self._on_telemetry_update

    async def async_added_to_hass(self) -> None:
        """Register callback with the telemetry singleton."""
        self._telemetry.add_listener(self._cb)
        # Populate immediately
        snap = self._telemetry.snapshot()
        self._attr_native_value = snap.get(self._attr_key, 0)

    async def async_will_remove_from_hass(self) -> None:
        """Deregister callback."""
        self._telemetry.remove_listener(self._cb)

    @callback
    def _on_telemetry_update(self, snap: dict[str, Any]) -> None:
        """Receive a fresh snapshot and push to HA."""
        self._attr_native_value = snap.get(self._attr_key, 0)
        self.async_write_ha_state()
