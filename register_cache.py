"""Register value cache for Huawei Solar.

The SUN2000 inverter reacts badly to excessive Modbus traffic.  Many registers
never change (rated power, serial number, battery capacity) and others change
slowly.  This module provides a time-aware, adaptive cache that:

  • Assigns TTLs per-register based on observed volatility (adaptive TTL).
  • Groups registers into STATIC / SLOW / NORMAL / FAST tiers.
  • Doubles a register's effective TTL every time its value is unchanged
    (up to a per-tier cap), and resets the TTL as soon as the value changes.
  • Tracks dirty flags so writes immediately invalidate the cache.
  • Reports hit/miss statistics to ModbusTelemetry.

Volatility tiers
----------------
STATIC   – Hardware constants that never change in normal operation:
           serial numbers, firmware versions, model names, rated power,
           battery pack capacities, manufacturer strings.
           Base TTL: 60 min.  Cap: never re-read after first successful read
           (effectively ∞ during a session; invalidated on reconnect).

SLOW     – Values that change at most once per day or once per event:
           daily/total energy counters, working mode, alarm status,
           temperature (changes slowly), SOH calibration status.
           Base TTL: 5 min.  Adaptive cap: 30 min.

NORMAL   – Typical sensor values: SOC, power, voltage, current.
           Base TTL: 30 s (== poll interval).  Adaptive cap: 5 min.

FAST     – High-priority real-time values: grid import/export power,
           battery charge/discharge power, PV input power.
           Base TTL: 0 (always read).  No adaptive stretching.

Adaptive TTL algorithm
----------------------
After each successful poll, for every register in SLOW or NORMAL tier:
  - If value UNCHANGED → new_ttl = min(current_ttl * ADAPTIVE_FACTOR, tier_cap)
  - If value CHANGED   → new_ttl = tier_base_ttl   (reset to minimum)

This means a stable reading (e.g. battery idle at 80 % SOC at night) will
organically slow its own polling from 30 s → 60 s → 120 s → … → 300 s,
while a changing reading stays at 30 s.

Night-mode interaction
----------------------
When the coordinator sets ``night_mode=True`` on the cache, the effective TTL
for NORMAL registers is stretched by NIGHT_TTL_MULTIPLIER (default 10×),
turning a 30 s poll into 300 s.  FAST registers are also stretched to 60 s
so the inverter is not completely silent.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from enum import IntEnum, auto
from typing import Any, TYPE_CHECKING

from huawei_solar import RegisterName, Result

if TYPE_CHECKING:
    from .modbus_telemetry import ModbusTelemetry

_LOGGER = logging.getLogger(__name__)


# ── Tier definitions ──────────────────────────────────────────────────────────

class RegisterTier(IntEnum):
    FAST   = auto()   # always polled — real-time power/grid values
    NORMAL = auto()   # standard 30 s poll
    SLOW   = auto()   # 5 min base, adaptive up to 30 min
    STATIC = auto()   # read once per session


# Base TTLs (seconds)
_TIER_BASE_TTL: dict[RegisterTier, float] = {
    RegisterTier.FAST:   0.0,
    RegisterTier.NORMAL: 30.0,
    RegisterTier.SLOW:   300.0,
    RegisterTier.STATIC: 3600.0,
}

# Adaptive cap TTLs (seconds) — TTL will not grow beyond this
_TIER_CAP_TTL: dict[RegisterTier, float] = {
    RegisterTier.FAST:   60.0,    # even FAST stretches to 60 s in night mode
    RegisterTier.NORMAL: 300.0,   # 5 min cap
    RegisterTier.SLOW:   1800.0,  # 30 min cap
    RegisterTier.STATIC: 86400.0, # effectively "read once"
}

# Multiplier applied to TTL each poll cycle the value is unchanged
ADAPTIVE_FACTOR: float = 2.0

# Multiplier applied to all non-FAST TTLs during inverter night/sleep mode
NIGHT_TTL_MULTIPLIER: float = 10.0


# ── Register classification ───────────────────────────────────────────────────
#
# Rules applied in order; first match wins.
# Patterns are tested against the lowercase string form of the RegisterName.

_STATIC_SUBSTRINGS: tuple[str, ...] = (
    "serial_number",
    "firmware_version",
    "software_version",
    "model_name",
    "model_id",
    "rated_power",
    "rated_capacity",
    "p_max",
    "manufacturer",
    "inverter_rated_power",
    "storage_rated_capacity",
    "storage_maximum_charge_power",    # hardware-rated max (not the soft limit)
    "storage_maximum_discharge_power", # hardware-rated max
    "storage_maximum_power_of_charge_from_grid",
    "charger_rated_power",
)

_SLOW_SUBSTRINGS: tuple[str, ...] = (
    "daily_",
    "current_day_",
    "total_",
    "accumulated_",
    "yearly_",
    "total_charge",
    "total_discharge",
    "total_energy",
    "total_active",
    "total_negative",
    "total_positive",
    "total_feed_in",
    "total_supply",
    "total_pv_energy",
    "grid_accumulated",
    "device_status",
    "running_status",
    "working_mode",
    "alarm",
    "temperature",          # changes slowly
    "soh_calibration",
    "remaining_charge_dis",
    "storage_unit_1_working_mode",
    "storage_unit_2_working_mode",
    "phase_a_active_power_built_in",
    "phase_b_active_power_built_in",
    "phase_c_active_power_built_in",
    "phase_a_active_power_external",
    "phase_b_active_power_external",
    "phase_c_active_power_external",
    "active_power_built_in",
    "active_power_external",
)

_FAST_SUBSTRINGS: tuple[str, ...] = (
    "power_meter_active_power",
    "power_meter_reactive_power",
    "storage_charge_discharge_power",   # battery charge/discharge — real-time
    "storage_unit_1_charge_discharge",
    "storage_unit_2_charge_discharge",
    "battery_pack_1_charge_discharge",
    "battery_pack_2_charge_discharge",
    "battery_pack_3_charge_discharge",
    "input_power",                      # PV input
    "total_dc_input_power",
    "active_power",                     # AC output
    "reactive_power",
    "sdongle_total_active",
    "sdongle_total_input",
    "sdongle_total_battery",
    "smartlogger_active_power",
    "smartlogger_input_power",
    "smartlogger_external_meter_active",
    "smartlogger_external_meter_reactive",
    "inverter_active_power",
)


def _classify(name: RegisterName) -> RegisterTier:
    """Return the volatility tier for a register name."""
    s = str(name).lower()
    for sub in _STATIC_SUBSTRINGS:
        if sub in s:
            return RegisterTier.STATIC
    for sub in _FAST_SUBSTRINGS:
        if sub in s:
            return RegisterTier.FAST
    for sub in _SLOW_SUBSTRINGS:
        if sub in s:
            return RegisterTier.SLOW
    return RegisterTier.NORMAL


# ── Cache entry ───────────────────────────────────────────────────────────────

class _CacheEntry:
    __slots__ = ("value", "raw", "ts", "dirty", "tier", "effective_ttl")

    def __init__(self, value: Any, raw: Any, ts: float, tier: RegisterTier) -> None:
        self.value = value                          # full Result object
        self.raw = raw                              # comparable raw value for change detection
        self.ts = ts
        self.dirty = False
        self.tier = tier
        self.effective_ttl: float = _TIER_BASE_TTL[tier]


def _raw(result: "Result[Any]") -> Any:
    """Extract a comparable value from a Result for change detection."""
    try:
        return result.value
    except Exception:
        return result


# ── Main cache class ──────────────────────────────────────────────────────────

class RegisterCache:
    """Adaptive, tier-aware register value cache.

    Parameters
    ----------
    telemetry:
        Optional ModbusTelemetry instance.  When provided, cache hits are
        reported so they appear in the Modbus diagnostic sensors.
    night_mode:
        When True, non-FAST TTLs are multiplied by NIGHT_TTL_MULTIPLIER.
        Set via set_night_mode(); controlled by the coordinator.
    """

    def __init__(self, telemetry: "ModbusTelemetry | None" = None) -> None:
        self._store: dict[RegisterName, _CacheEntry] = {}
        self._telemetry = telemetry
        self._night_mode: bool = False

    # ── night-mode control ────────────────────────────────────────────────────

    def set_night_mode(self, active: bool) -> None:
        """Enable or disable night-mode TTL stretching."""
        if active != self._night_mode:
            _LOGGER.debug("Register cache: night mode %s", "ON" if active else "OFF")
            self._night_mode = active
            # On wakeup, reset all NORMAL adaptive TTLs so we get fresh data immediately
            if not active:
                for entry in self._store.values():
                    if entry.tier in (RegisterTier.NORMAL, RegisterTier.FAST):
                        entry.effective_ttl = _TIER_BASE_TTL[entry.tier]

    @property
    def night_mode(self) -> bool:
        return self._night_mode

    # ── effective TTL helper ──────────────────────────────────────────────────

    def _effective_ttl(self, entry: _CacheEntry) -> float:
        """Return the actual TTL to use for a cache entry, respecting night mode."""
        ttl = entry.effective_ttl
        if self._night_mode and entry.tier != RegisterTier.STATIC:
            ttl = min(ttl * NIGHT_TTL_MULTIPLIER, _TIER_CAP_TTL[entry.tier])
        return ttl

    # ── public API ────────────────────────────────────────────────────────────

    def filter_stale(
        self,
        names: list[RegisterName],
        default_ttl: timedelta,
    ) -> list[RegisterName]:
        """Return only those register names that need a fresh read.

        Parameters
        ----------
        names:
            All register names requested by active HA entities.
        default_ttl:
            Fallback TTL for NORMAL-tier registers (should equal the
            coordinator's poll interval).  Ignored for other tiers.
        """
        now = time.monotonic()
        stale: list[RegisterName] = []
        cache_hits = 0

        for name in names:
            entry = self._store.get(name)
            if entry is None or entry.dirty:
                stale.append(name)
                continue

            ttl = self._effective_ttl(entry)

            # For NORMAL tier, never use a TTL shorter than default_ttl so that
            # the coordinator's own interval is always respected as a minimum.
            if entry.tier == RegisterTier.NORMAL:
                ttl = max(ttl, default_ttl.total_seconds())

            age = now - entry.ts
            if age >= ttl:
                stale.append(name)
            else:
                cache_hits += 1
                if self._telemetry:
                    self._telemetry.record_cache_hit()

        if cache_hits:
            _LOGGER.debug(
                "Register cache: %d hit(s) / %d miss(es) / %d total  [night=%s]",
                cache_hits, len(stale), len(names), self._night_mode,
            )
        return stale

    def update(self, results: dict[RegisterName, "Result[Any]"]) -> None:
        """Store fresh results, update adaptive TTLs, and clear dirty flags."""
        now = time.monotonic()
        for name, result in results.items():
            raw_new = _raw(result)
            existing = self._store.get(name)
            tier = existing.tier if existing else _classify(name)

            if existing is not None and not existing.dirty:
                # Adaptive TTL: stretch if value unchanged, reset if changed
                if raw_new == existing.raw:
                    new_ttl = min(
                        existing.effective_ttl * ADAPTIVE_FACTOR,
                        _TIER_CAP_TTL[tier],
                    )
                    existing.effective_ttl = new_ttl
                    existing.value = result
                    existing.ts = now
                else:
                    existing.effective_ttl = _TIER_BASE_TTL[tier]
                    existing.raw = raw_new
                    existing.value = result
                    existing.ts = now
                existing.dirty = False
            else:
                self._store[name] = _CacheEntry(result, raw_new, now, tier)

    def merge(
        self,
        fresh: dict[RegisterName, "Result[Any]"],
        requested: list[RegisterName],
    ) -> dict[RegisterName, "Result[Any]"]:
        """Merge fresh results with cached values to produce a complete response."""
        merged: dict[RegisterName, "Result[Any]"] = {}
        for name in requested:
            if name in fresh:
                merged[name] = fresh[name]
            elif name in self._store and not self._store[name].dirty:
                merged[name] = self._store[name].value
        return merged

    def invalidate(self, name: RegisterName) -> None:
        """Mark a single register dirty (after a write)."""
        if name in self._store:
            self._store[name].dirty = True
            _LOGGER.debug("Cache invalidated: %s", name)

    def invalidate_all(self) -> None:
        """Mark every cached register dirty (after reconnect / outage recovery)."""
        for entry in self._store.values():
            entry.dirty = True

    def get(self, name: RegisterName) -> "Result[Any] | None":
        """Return a cached value or None."""
        entry = self._store.get(name)
        if entry and not entry.dirty:
            return entry.value
        return None

    def tier_of(self, name: RegisterName) -> "RegisterTier | None":
        """Return the tier of a cached register, or None if not cached."""
        entry = self._store.get(name)
        return entry.tier if entry else None

    def effective_ttl_of(self, name: RegisterName) -> float:
        """Return the current effective TTL of a cached register in seconds."""
        entry = self._store.get(name)
        return self._effective_ttl(entry) if entry else 0.0

    def any_cached_name(self) -> "RegisterName | None":
        """Return any non-dirty cached register name, or None if cache is empty.

        Used by the keepalive ping to perform a minimal 1-register read without
        accessing private internals.  Prefers STATIC-tier registers (cheapest
        to read and never expected to change).
        """
        # Prefer STATIC tier first (cheapest + most stable)
        for name, entry in self._store.items():
            if not entry.dirty and entry.tier == RegisterTier.STATIC:
                return name
        # Fall back to any non-dirty entry
        for name, entry in self._store.items():
            if not entry.dirty:
                return name
        return None

    @property
    def size(self) -> int:
        return len(self._store)

    def clear(self) -> None:
        self._store.clear()
