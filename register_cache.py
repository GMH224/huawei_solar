"""Register value cache for Huawei Solar.

v2.12.2 additions
-----------------
- filter_stale() now includes registers within one poll interval of TTL expiry
  (speculative pre-fetch) so registers don't all expire simultaneously and
  create large bursty batches.
- sort_by_address() helper: sorts a register name list by Modbus address so
  batch_update() can merge contiguous reads into fewer frames.
- Cross-coordinator LiveRegisterBus: a per-inverter singleton that lets
  coordinators "publish" freshly read non-STATIC values and "subscribe" to
  them, so a register read by one coordinator is served to others for one
  poll cycle without a second Modbus request.

Volatility tiers
----------------
STATIC   – Hardware constants. Base TTL: 60 min. Shared via StaticRegisterCache.
SLOW     – Changes at most once per event. Base TTL: 5 min, cap 30 min.
NORMAL   – SOC, voltage, current. Base TTL: 30 s, cap 5 min.
FAST     – Real-time power. Always read, no adaptive stretching.
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
    FAST   = auto()
    NORMAL = auto()
    SLOW   = auto()
    STATIC = auto()


_TIER_BASE_TTL: dict[RegisterTier, float] = {
    RegisterTier.FAST:   0.0,
    RegisterTier.NORMAL: 30.0,
    RegisterTier.SLOW:   300.0,
    RegisterTier.STATIC: 3600.0,
}

_TIER_CAP_TTL: dict[RegisterTier, float] = {
    RegisterTier.FAST:   60.0,
    RegisterTier.NORMAL: 300.0,
    RegisterTier.SLOW:   1800.0,
    RegisterTier.STATIC: 86400.0,
}

ADAPTIVE_FACTOR: float = 2.0
NIGHT_TTL_MULTIPLIER: float = 10.0
OPTIMISTIC_TTL: float = 8.0

# Speculative pre-fetch: include registers whose TTL will expire within this
# fraction of the poll interval on the *next* cycle, so we fetch them now
# while we already have the bus, instead of triggering a solo round-trip later.
PREFETCH_LEAD_FRACTION: float = 0.5   # fetch if < 50 % of poll interval remains


# ── Register classification ───────────────────────────────────────────────────

_STATIC_SUBSTRINGS: tuple[str, ...] = (
    "serial_number", "firmware_version", "software_version",
    "model_name", "model_id", "rated_power", "rated_capacity",
    "p_max", "manufacturer", "inverter_rated_power",
    "storage_rated_capacity", "storage_maximum_charge_power",
    "storage_maximum_discharge_power",
    "storage_maximum_power_of_charge_from_grid", "charger_rated_power",
)

_SLOW_SUBSTRINGS: tuple[str, ...] = (
    "daily_", "current_day_", "total_", "accumulated_", "yearly_",
    "total_charge", "total_discharge", "total_energy", "total_active",
    "total_negative", "total_positive", "total_feed_in", "total_supply",
    "total_pv_energy", "grid_accumulated",
    "device_status", "running_status", "working_mode", "alarm",
    "temperature", "soh_calibration", "remaining_charge_dis",
    "storage_unit_1_working_mode", "storage_unit_2_working_mode",
    "phase_a_active_power_built_in", "phase_b_active_power_built_in",
    "phase_c_active_power_built_in", "phase_a_active_power_external",
    "phase_b_active_power_external", "phase_c_active_power_external",
    "active_power_built_in", "active_power_external",
)

_FAST_SUBSTRINGS: tuple[str, ...] = (
    "power_meter_active_power", "power_meter_reactive_power",
    "storage_charge_discharge_power",
    "storage_unit_1_charge_discharge", "storage_unit_2_charge_discharge",
    "battery_pack_1_charge_discharge", "battery_pack_2_charge_discharge",
    "battery_pack_3_charge_discharge",
    "input_power", "total_dc_input_power",
    "inverter_active_power", "inverter_reactive_power",
    "sdongle_total_active", "sdongle_total_input", "sdongle_total_battery",
    "smartlogger_active_power", "smartlogger_input_power",
    "smartlogger_external_meter_active", "smartlogger_external_meter_reactive",
)


def _classify(name: RegisterName) -> RegisterTier:
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


def sort_by_address(names: list[RegisterName]) -> list[RegisterName]:
    """Sort register names by their Modbus address (ascending).

    Presenting registers in address order to batch_update() gives the
    underlying library the best chance of merging contiguous reads into a
    single Modbus frame, reducing total frame count.
    """
    def _addr(n: RegisterName) -> int:
        return getattr(n, "register", getattr(n, "address", 0)) or 0
    return sorted(names, key=_addr)


# ── Optimistic result wrapper ─────────────────────────────────────────────────

class _OptimisticResult:
    __slots__ = ("value",)
    def __init__(self, value: Any) -> None:
        self.value = value


# ── Cache entry ───────────────────────────────────────────────────────────────

class _CacheEntry:
    __slots__ = ("value", "raw", "ts", "dirty", "tier", "effective_ttl")

    def __init__(self, value: Any, raw: Any, ts: float, tier: RegisterTier) -> None:
        self.value = value
        self.raw = raw
        self.ts = ts
        self.dirty = False
        self.tier = tier
        self.effective_ttl: float = _TIER_BASE_TTL[tier]


def _raw(result: "Result[Any]") -> Any:
    try:
        return result.value
    except Exception:
        return result


# ── Main cache class ──────────────────────────────────────────────────────────

class RegisterCache:
    """Adaptive, tier-aware register value cache."""

    def __init__(self, telemetry: "ModbusTelemetry | None" = None) -> None:
        self._store: dict[RegisterName, _CacheEntry] = {}
        self._telemetry = telemetry
        self._night_mode: bool = False

    def set_night_mode(self, active: bool) -> None:
        if active != self._night_mode:
            _LOGGER.debug("Register cache: night mode %s", "ON" if active else "OFF")
            self._night_mode = active
            if not active:
                for entry in self._store.values():
                    if entry.tier in (RegisterTier.NORMAL, RegisterTier.FAST):
                        entry.effective_ttl = _TIER_BASE_TTL[entry.tier]

    @property
    def night_mode(self) -> bool:
        return self._night_mode

    def _effective_ttl(self, entry: _CacheEntry) -> float:
        ttl = entry.effective_ttl
        if self._night_mode and entry.tier != RegisterTier.STATIC:
            ttl = min(ttl * NIGHT_TTL_MULTIPLIER, _TIER_CAP_TTL[entry.tier])
        return ttl

    def filter_stale(
        self,
        names: list[RegisterName],
        default_ttl: timedelta,
    ) -> list[RegisterName]:
        """Return register names that need a fresh read.

        v2.12.2: also includes registers approaching TTL expiry within
        PREFETCH_LEAD_FRACTION × poll_interval so they're fetched during the
        current batch rather than triggering a solo round-trip next cycle.
        """
        now = time.monotonic()
        poll_s = default_ttl.total_seconds()
        lead = poll_s * PREFETCH_LEAD_FRACTION

        stale: list[RegisterName] = []
        cache_hits = 0

        for name in names:
            entry = self._store.get(name)
            if entry is None or entry.dirty:
                stale.append(name)
                continue

            ttl = self._effective_ttl(entry)
            if entry.tier == RegisterTier.NORMAL:
                ttl = max(ttl, poll_s)

            age = now - entry.ts
            remaining = ttl - age

            if remaining <= 0:
                stale.append(name)
            elif entry.tier not in (RegisterTier.STATIC, RegisterTier.FAST) and remaining <= lead:
                # Speculative pre-fetch: will expire before next poll anyway
                stale.append(name)
                _LOGGER.debug("Cache pre-fetch: %s (expires in %.1f s)", name, remaining)
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
        now = time.monotonic()
        for name, result in results.items():
            raw_new = _raw(result)
            existing = self._store.get(name)
            tier = existing.tier if existing else _classify(name)

            if existing is not None and not existing.dirty:
                if raw_new == existing.raw:
                    existing.effective_ttl = min(
                        existing.effective_ttl * ADAPTIVE_FACTOR,
                        _TIER_CAP_TTL[tier],
                    )
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

    def write_optimistic(self, name: RegisterName, value: Any, ttl: float = OPTIMISTIC_TTL) -> None:
        now = time.monotonic()
        tier = _classify(name)
        entry = _CacheEntry(_OptimisticResult(value), value, now, tier)
        entry.effective_ttl = ttl
        entry.dirty = False
        self._store[name] = entry
        _LOGGER.debug("Register cache: optimistic write %s = %r (TTL %.0f s)", name, value, ttl)

    def merge(
        self,
        fresh: dict[RegisterName, "Result[Any]"],
        requested: list[RegisterName],
    ) -> dict[RegisterName, "Result[Any]"]:
        merged: dict[RegisterName, "Result[Any]"] = {}
        for name in requested:
            if name in fresh:
                merged[name] = fresh[name]
            elif name in self._store and not self._store[name].dirty:
                merged[name] = self._store[name].value
        return merged

    def invalidate(self, name: RegisterName) -> None:
        if name in self._store:
            self._store[name].dirty = True

    def invalidate_all(self) -> None:
        for entry in self._store.values():
            entry.dirty = True

    def get(self, name: RegisterName) -> "Result[Any] | None":
        entry = self._store.get(name)
        if entry and not entry.dirty:
            return entry.value
        return None

    def tier_of(self, name: RegisterName) -> "RegisterTier | None":
        entry = self._store.get(name)
        return entry.tier if entry else None

    def effective_ttl_of(self, name: RegisterName) -> float:
        entry = self._store.get(name)
        return self._effective_ttl(entry) if entry else 0.0

    @property
    def size(self) -> int:
        return len(self._store)

    def clear(self) -> None:
        self._store.clear()


# ── Shared STATIC cache ───────────────────────────────────────────────────────

class StaticRegisterCache:
    """Inverter-scoped singleton for STATIC-tier registers shared across coordinators."""

    _registry: dict[str, "StaticRegisterCache"] = {}

    @classmethod
    def get_or_create(cls, serial_number: str) -> "StaticRegisterCache":
        if serial_number not in cls._registry:
            cls._registry[serial_number] = cls(serial_number)
        return cls._registry[serial_number]

    @classmethod
    def clear_registry(cls) -> None:
        cls._registry.clear()

    def __init__(self, serial_number: str) -> None:
        self._serial = serial_number
        self._store: dict[RegisterName, Any] = {}

    def filter_stale(self, names: list[RegisterName]) -> list[RegisterName]:
        return [n for n in names if n not in self._store]

    def update(self, results: dict[RegisterName, Any]) -> None:
        self._store.update(results)

    def get_all(self, names: list[RegisterName]) -> dict[RegisterName, Any]:
        return {n: self._store[n] for n in names if n in self._store}

    def invalidate_all(self) -> None:
        self._store.clear()

    @property
    def size(self) -> int:
        return len(self._store)


# ── Live register bus ─────────────────────────────────────────────────────────

class LiveRegisterBus:
    """Per-inverter singleton for cross-coordinator register sharing.

    A coordinator that reads a non-STATIC register publishes the result here.
    Other coordinators that need the same register in the same poll window
    receive the cached value without issuing a second Modbus request.

    Entries expire after one poll interval (TTL = publish_ttl_seconds) so
    stale values never persist across poll cycles.

    Usage
    -----
        bus = LiveRegisterBus.get_or_create(serial_number)

        # After a successful batch_update():
        bus.publish(fresh_results, ttl=poll_interval.total_seconds())

        # Before issuing a request, check if some names are already known:
        already_known = bus.query(stale_names)
        still_needed = [n for n in stale_names if n not in already_known]
    """

    _registry: dict[str, "LiveRegisterBus"] = {}

    @classmethod
    def get_or_create(cls, serial_number: str) -> "LiveRegisterBus":
        if serial_number not in cls._registry:
            cls._registry[serial_number] = cls(serial_number)
        return cls._registry[serial_number]

    @classmethod
    def clear_registry(cls) -> None:
        cls._registry.clear()

    def __init__(self, serial_number: str) -> None:
        self._serial = serial_number
        self._store: dict[RegisterName, tuple[Any, float]] = {}  # name → (result, expires_at)

    def publish(
        self,
        results: dict[RegisterName, Any],
        ttl: float,
    ) -> None:
        """Publish freshly read results so other coordinators can consume them."""
        expires = time.monotonic() + ttl
        for name, result in results.items():
            self._store[name] = (result, expires)

    def query(
        self,
        names: list[RegisterName],
    ) -> dict[RegisterName, Any]:
        """Return results for names that are available and not yet expired."""
        now = time.monotonic()
        found: dict[RegisterName, Any] = {}
        expired = []
        for name in names:
            entry = self._store.get(name)
            if entry is None:
                continue
            result, expires_at = entry
            if now < expires_at:
                found[name] = result
            else:
                expired.append(name)
        for name in expired:
            del self._store[name]
        return found

    def evict_expired(self) -> None:
        """Remove expired entries (called periodically to keep memory bounded)."""
        now = time.monotonic()
        expired = [n for n, (_, exp) in self._store.items() if now >= exp]
        for name in expired:
            del self._store[name]

    @property
    def size(self) -> int:
        return len(self._store)
