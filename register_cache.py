"""Register value cache for Huawei Solar.

The SUN2000 inverter reacts badly to excessive Modbus traffic.  Many registers
never change (rated power, serial number, battery capacity) and others change
slowly (configuration registers polled every 15 minutes).  This module provides
a time-aware cache that:

  • Stores the last seen value for every register name.
  • Returns cached values for registers that haven't changed recently.
  • Allows callers to declare a TTL (time-to-live) per register group.
  • Tracks a "dirty" flag per register so that writes invalidate the cache.
  • Reports hit/miss statistics to ModbusTelemetry.

Usage
-----
    cache = RegisterCache(telemetry)
    fresh_names = cache.filter_stale(all_names, ttl=timedelta(seconds=30))
    # ... fetch only fresh_names from the inverter ...
    cache.update(new_results)
    full_results = cache.merge(new_results)     # fill in cached values
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any, TYPE_CHECKING

from huawei_solar import RegisterName, Result

if TYPE_CHECKING:
    from .modbus_telemetry import ModbusTelemetry

_LOGGER = logging.getLogger(__name__)

# Registers that almost never change — long TTL to avoid querying them every poll.
# Using a 5-minute TTL means they are refreshed ~12 times per hour instead of ~120.
_STATIC_REGISTER_TTL = timedelta(minutes=5)

# Prefixes of register names that are considered "static" (configuration/rated values).
_STATIC_PREFIXES: tuple[str, ...] = (
    "rated_power",
    "storage_maximum_charge_power",
    "storage_maximum_discharge_power",
    "storage_rated_capacity",
    "p_max",
    "inverter_rated_power",
    "storage_maximum_power_of_charge_from_grid",
)


def _ttl_for_register(name: str) -> float:
    """Return the effective TTL in seconds for a given register name."""
    lname = str(name).lower()
    for prefix in _STATIC_PREFIXES:
        if lname.startswith(prefix):
            return _STATIC_REGISTER_TTL.total_seconds()
    return 0.0  # Default: always considered stale (callers supply their own TTL)


class _CacheEntry:
    __slots__ = ("value", "ts", "dirty")

    def __init__(self, value: Any, ts: float) -> None:
        self.value = value
        self.ts = ts
        self.dirty = False


class RegisterCache:
    """Time-aware register value cache.

    Parameters
    ----------
    telemetry:
        Optional ModbusTelemetry instance.  When provided, cache hits are
        reported so they show up in the telemetry sensors.
    """

    def __init__(self, telemetry: "ModbusTelemetry | None" = None) -> None:
        self._store: dict[RegisterName, _CacheEntry] = {}
        self._telemetry = telemetry

    # ── public API ────────────────────────────────────────────────────────────

    def filter_stale(
        self,
        names: list[RegisterName],
        default_ttl: timedelta,
    ) -> list[RegisterName]:
        """Return only those names whose cached value is stale or absent.

        Parameters
        ----------
        names:
            The full list of registers that *would* be fetched.
        default_ttl:
            TTL applied to registers that don't have a static TTL override.
            Typically the poll interval of the coordinator.

        Returns
        -------
        list[RegisterName]
            Subset of *names* that need a fresh read from the inverter.
        """
        now = time.monotonic()
        stale: list[RegisterName] = []
        for name in names:
            entry = self._store.get(name)
            if entry is None or entry.dirty:
                stale.append(name)
                continue
            # Choose the longer of static TTL and the caller-supplied default
            ttl = max(_ttl_for_register(name), default_ttl.total_seconds())
            age = now - entry.ts
            if age >= ttl:
                stale.append(name)
            else:
                if self._telemetry:
                    self._telemetry.record_cache_hit()

        hits = len(names) - len(stale)
        if hits:
            _LOGGER.debug(
                "Register cache: %d hit(s), %d miss(es) / %d total",
                hits,
                len(stale),
                len(names),
            )
        return stale

    def update(self, results: dict[RegisterName, "Result[Any]"]) -> None:
        """Store fresh results and clear dirty flags."""
        now = time.monotonic()
        for name, result in results.items():
            self._store[name] = _CacheEntry(result, now)

    def merge(
        self,
        fresh: dict[RegisterName, "Result[Any]"],
        requested: list[RegisterName],
    ) -> dict[RegisterName, "Result[Any]"]:
        """Return fresh results supplemented with cached values for any
        registers in *requested* that are missing from *fresh*.

        This is the final merge step: the coordinator calls
        ``merge(fresh_results, all_requested_names)`` and gets back a
        complete dict with no gaps.
        """
        merged: dict[RegisterName, "Result[Any]"] = {}
        for name in requested:
            if name in fresh:
                merged[name] = fresh[name]
            elif name in self._store and not self._store[name].dirty:
                merged[name] = self._store[name].value
            # If neither fresh nor cached, simply omit — sensor will show unavailable.
        return merged

    def invalidate(self, name: RegisterName) -> None:
        """Mark a register as dirty (e.g. after a write operation)."""
        if name in self._store:
            self._store[name].dirty = True
            _LOGGER.debug("Cache invalidated: %s", name)

    def invalidate_all(self) -> None:
        """Invalidate every cached register (e.g. after a reconnect)."""
        for entry in self._store.values():
            entry.dirty = True

    def get(self, name: RegisterName) -> "Result[Any] | None":
        """Return a cached value or None."""
        entry = self._store.get(name)
        if entry and not entry.dirty:
            return entry.value
        return None

    @property
    def size(self) -> int:
        """Number of registers in the cache."""
        return len(self._store)

    def clear(self) -> None:
        """Evict all cached values."""
        self._store.clear()
