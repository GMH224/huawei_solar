"""Modbus traffic telemetry for Huawei Solar.

Tracks per-device Modbus statistics over a rolling 1-hour window and exposes
them as HA sensor entities.

v2.12.2 additions
-----------------
- RTT tracking: record_rtt() + median/p95 RTT in snapshot().
- Dynamic gap tracking: record_gap() + current_gap_ms in snapshot().
- Adaptive poll interval feedback: record_poll_interval_change() logs
  auto-tuning decisions visible in HA diagnostic sensors.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta
import logging
import statistics
import time
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_WINDOW = timedelta(hours=1)
_WINDOW_SEC = _WINDOW.total_seconds()
_TELEMETRY_PUSH_INTERVAL = timedelta(seconds=60)

# Rolling window size for RTT percentile calculations
_RTT_WINDOW = 120   # last 120 requests ≈ 1 h at 30 s poll


class ModbusTelemetry:
    """Per-device rolling Modbus traffic statistics."""

    _registry: dict[str, "ModbusTelemetry"] = {}

    @classmethod
    def get_or_create(
        cls, hass: HomeAssistant, serial_number: str, device_info: DeviceInfo
    ) -> "ModbusTelemetry":
        if serial_number not in cls._registry:
            cls._registry[serial_number] = cls(hass, serial_number, device_info)
        return cls._registry[serial_number]

    @classmethod
    def get(cls, serial_number: str) -> "ModbusTelemetry | None":
        return cls._registry.get(serial_number)

    @classmethod
    def clear_registry(cls) -> None:
        cls._registry.clear()

    def __init__(
        self, hass: HomeAssistant, serial_number: str, device_info: DeviceInfo
    ) -> None:
        self.hass = hass
        self.serial_number = serial_number
        self.device_info = device_info

        # Rolling timestamp deques
        self._requests: deque[float] = deque()
        self._failures: deque[float] = deque()
        self._timeouts: deque[float] = deque()
        self._cache_hits: deque[float] = deque()
        self._batch_sizes: deque[int] = deque()

        # RTT samples (fixed-size rolling window)
        self._rtt_samples: deque[float] = deque(maxlen=_RTT_WINDOW)

        # Lifetime totals
        self.total_requests: int = 0
        self.total_failures: int = 0
        self.total_timeouts: int = 0
        self.total_cache_hits: int = 0
        self.total_skipped_polls: int = 0

        # Current dynamic values (updated externally)
        self._current_gap_ms: float = 150.0
        self._night_mode: bool = False
        self._poll_interval_s: float = 30.0
        self._auto_tune_events: int = 0

        self._last_snapshot: dict[str, Any] = {}
        self._listeners: list[callback] = []

        self._unsub = async_track_time_interval(
            hass, self._push_to_listeners, _TELEMETRY_PUSH_INTERVAL
        )

    # ── event recording ───────────────────────────────────────────────────────

    def record_request(self, batch_size: int = 1) -> None:
        now = time.monotonic()
        self._requests.append(now)
        self._batch_sizes.append(batch_size)
        self.total_requests += 1
        self._evict(now)

    def record_failure(self) -> None:
        now = time.monotonic()
        self._failures.append(now)
        self.total_failures += 1
        self._evict(now)

    def record_timeout(self) -> None:
        now = time.monotonic()
        self._timeouts.append(now)
        self.total_timeouts += 1
        self.total_failures += 1
        self._evict(now)

    def record_cache_hit(self) -> None:
        now = time.monotonic()
        self._cache_hits.append(now)
        self.total_cache_hits += 1
        self._evict(now)

    def record_skipped_poll(self) -> None:
        self.total_skipped_polls += 1

    def record_night_mode(self, active: bool) -> None:
        self._night_mode = active

    def record_rtt(self, rtt_seconds: float) -> None:
        """Record a Modbus round-trip time sample."""
        if rtt_seconds > 0:
            self._rtt_samples.append(rtt_seconds)

    def record_gap(self, gap_ms: float) -> None:
        """Record the current dynamic inter-request gap."""
        self._current_gap_ms = gap_ms

    def record_poll_interval(self, interval_seconds: float) -> None:
        """Record the current effective poll interval (for auto-tune tracking)."""
        if interval_seconds != self._poll_interval_s:
            self._poll_interval_s = interval_seconds
            self._auto_tune_events += 1

    # ── derived metrics ───────────────────────────────────────────────────────

    def _evict(self, now: float) -> None:
        cutoff = now - _WINDOW_SEC
        for dq in (self._requests, self._failures, self._timeouts, self._cache_hits):
            while dq and dq[0] < cutoff:
                dq.popleft()
        while len(self._batch_sizes) > len(self._requests):
            self._batch_sizes.popleft()

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        self._evict(now)

        req_ph = len(self._requests)
        fail_ph = len(self._failures)
        to_ph = len(self._timeouts)
        cache_ph = len(self._cache_hits)

        avg_batch = (
            round(sum(self._batch_sizes) / len(self._batch_sizes), 1)
            if self._batch_sizes else 0.0
        )
        failure_rate = round(fail_ph / req_ph * 100, 1) if req_ph else 0.0

        # RTT statistics
        if len(self._rtt_samples) >= 3:
            sorted_rtts = sorted(self._rtt_samples)
            median_rtt_ms = round(statistics.median(sorted_rtts) * 1000, 1)
            p95_idx = int(len(sorted_rtts) * 0.95)
            p95_rtt_ms = round(sorted_rtts[min(p95_idx, len(sorted_rtts) - 1)] * 1000, 1)
        else:
            median_rtt_ms = None
            p95_rtt_ms = None

        snap = {
            "requests_per_hour": req_ph,
            "failures_per_hour": fail_ph,
            "timeouts_per_hour": to_ph,
            "cache_hits_per_hour": cache_ph,
            "failure_rate_percent": failure_rate,
            "avg_batch_size": avg_batch,
            "median_rtt_ms": median_rtt_ms,
            "p95_rtt_ms": p95_rtt_ms,
            "current_gap_ms": round(self._current_gap_ms, 1),
            "poll_interval_s": self._poll_interval_s,
            "auto_tune_events": self._auto_tune_events,
            "total_requests": self.total_requests,
            "total_failures": self.total_failures,
            "total_timeouts": self.total_timeouts,
            "total_cache_hits": self.total_cache_hits,
            "total_skipped_polls": self.total_skipped_polls,
            "night_mode_active": self._night_mode,
        }
        self._last_snapshot = snap
        return snap

    # ── HA listener plumbing ──────────────────────────────────────────────────

    def add_listener(self, cb: Any) -> None:
        self._listeners.append(cb)

    def remove_listener(self, cb: Any) -> None:
        try:
            self._listeners.remove(cb)
        except ValueError:
            pass

    @callback
    def _push_to_listeners(self, _now: datetime) -> None:
        snap = self.snapshot()
        for cb_fn in self._listeners:
            cb_fn(snap)

    def stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None


# ── HA Sensor entities ────────────────────────────────────────────────────────

_TELEMETRY_SENSORS: list[tuple[str, str, str | None, str, dict]] = [
    ("requests_per_hour",   "Modbus requests / hour",   None,  "mdi:counter",                {"state_class": SensorStateClass.MEASUREMENT}),
    ("failures_per_hour",   "Modbus failures / hour",   None,  "mdi:alert-circle-outline",   {"state_class": SensorStateClass.MEASUREMENT}),
    ("timeouts_per_hour",   "Modbus timeouts / hour",   None,  "mdi:timer-off-outline",      {"state_class": SensorStateClass.MEASUREMENT}),
    ("cache_hits_per_hour", "Modbus cache hits / hour", None,  "mdi:database-check-outline", {"state_class": SensorStateClass.MEASUREMENT}),
    ("failure_rate_percent","Modbus failure rate",       "%",   "mdi:percent",                {"state_class": SensorStateClass.MEASUREMENT}),
    ("avg_batch_size",      "Avg Modbus batch size",    None,  "mdi:package-variant",        {"state_class": SensorStateClass.MEASUREMENT}),
    ("median_rtt_ms",       "Modbus median RTT",        "ms",  "mdi:timer-outline",          {"state_class": SensorStateClass.MEASUREMENT}),
    ("p95_rtt_ms",          "Modbus P95 RTT",           "ms",  "mdi:timer-alert-outline",    {"state_class": SensorStateClass.MEASUREMENT}),
    ("current_gap_ms",      "Modbus inter-request gap", "ms",  "mdi:timer-sand",             {"state_class": SensorStateClass.MEASUREMENT}),
    ("poll_interval_s",     "Modbus poll interval",     "s",   "mdi:refresh",                {"state_class": SensorStateClass.MEASUREMENT}),
    ("auto_tune_events",    "Modbus auto-tune events",  None,  "mdi:tune",                   {"state_class": SensorStateClass.TOTAL_INCREASING}),
    ("night_mode_active",   "Inverter night mode",      None,  "mdi:weather-night",          {}),
    ("total_requests",      "Modbus total requests",    None,  "mdi:counter",                {"state_class": SensorStateClass.TOTAL_INCREASING, "entity_registry_enabled_default": False}),
    ("total_failures",      "Modbus total failures",    None,  "mdi:alert-circle",           {"state_class": SensorStateClass.TOTAL_INCREASING, "entity_registry_enabled_default": False}),
    ("total_cache_hits",    "Modbus total cache hits",  None,  "mdi:database-check",         {"state_class": SensorStateClass.TOTAL_INCREASING, "entity_registry_enabled_default": False}),
    ("total_skipped_polls", "Modbus skipped polls",     None,  "mdi:skip-next-circle-outline",{"state_class": SensorStateClass.TOTAL_INCREASING, "entity_registry_enabled_default": False}),
]


def create_telemetry_entities(
    telemetry: ModbusTelemetry,
) -> list["HuaweiSolarModbusTelemetrySensorEntity"]:
    return [
        HuaweiSolarModbusTelemetrySensorEntity(telemetry, attr_key, name, unit, icon, extra)
        for attr_key, name, unit, icon, extra in _TELEMETRY_SENSORS
    ]


class HuaweiSolarModbusTelemetrySensorEntity(SensorEntity):
    """HA Sensor backed by ModbusTelemetry."""

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
        self._attr_unique_id = f"{telemetry.serial_number}_modbus_telemetry_{attr_key}"
        self._attr_native_value: Any = None
        for k, v in extra.items():
            setattr(self, f"_attr_{k}", v)
        self._cb = self._on_telemetry_update

    async def async_added_to_hass(self) -> None:
        self._telemetry.add_listener(self._cb)
        snap = self._telemetry.snapshot()
        self._attr_native_value = snap.get(self._attr_key)

    async def async_will_remove_from_hass(self) -> None:
        self._telemetry.remove_listener(self._cb)

    @callback
    def _on_telemetry_update(self, snap: dict[str, Any]) -> None:
        self._attr_native_value = snap.get(self._attr_key)
        self.async_write_ha_state()
