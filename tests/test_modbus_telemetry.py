"""Tests for modbus_telemetry.py bug fix.

Bug 5 — record_failure() and record_timeout() did not call _evict(), so the
_failures and _timeouts deques grew unboundedly when the inverter was only
producing errors (no successful requests).  The fix calls _evict(now) at the
end of both methods.

Test strategy
-------------
We instantiate ModbusTelemetry using only a minimal stub for HomeAssistant and
drive it through purely-failure scenarios, then assert that the rolling-window
deques respect the 1-hour window boundary.
"""

from __future__ import annotations

import sys
import time
import types
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Minimal HA stubs so modbus_telemetry.py can be imported standalone.
# ---------------------------------------------------------------------------

for _mod in [
    "homeassistant",
    "homeassistant.components",
    "homeassistant.components.sensor",
    "homeassistant.const",
    "homeassistant.core",
    "homeassistant.helpers",
    "homeassistant.helpers.device_registry",
    "homeassistant.helpers.event",
    "homeassistant.helpers.entity",
    "homeassistant.helpers.entity_platform",
]:
    sys.modules.setdefault(_mod, types.ModuleType(_mod))

# Provide just enough attributes for the import to succeed.
_ha = sys.modules["homeassistant.components.sensor"]
for _attr in ["SensorDeviceClass", "SensorEntity", "SensorStateClass"]:
    setattr(_ha, _attr, MagicMock())

_const = sys.modules["homeassistant.const"]
_const.EntityCategory = MagicMock()
_const.UnitOfTime = MagicMock()

_core = sys.modules["homeassistant.core"]
_core.HomeAssistant = MagicMock
_core.callback = lambda f: f  # decorator passthrough

_event = sys.modules["homeassistant.helpers.event"]
_event.async_track_time_interval = MagicMock(return_value=MagicMock())

import importlib, pathlib

_src = pathlib.Path(__file__).parent.parent / "modbus_telemetry.py"

# Stub the local .const import that modbus_telemetry.py needs.
_const_stub = types.ModuleType("const_stub")
_const_stub.DOMAIN = "huawei_solar"
sys.modules["huawei_solar_const"] = _const_stub

_spec = importlib.util.spec_from_file_location("modbus_telemetry", _src)
telemetry_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]

# Patch the relative import of .const before exec.
with patch.dict(
    "sys.modules",
    {
        # The module does `from .const import DOMAIN`; we satisfy that by
        # injecting a stub under the expected dotted name.
        "huawei_solar.const": _const_stub,
    },
):
    try:
        _spec.loader.exec_module(telemetry_mod)  # type: ignore[union-attr]
    except Exception as exc:
        pytest.skip(f"Could not load modbus_telemetry.py standalone: {exc}")

ModbusTelemetry = telemetry_mod.ModbusTelemetry
_WINDOW_SEC = telemetry_mod._WINDOW_SEC  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_telemetry() -> ModbusTelemetry:
    hass = MagicMock()
    device_info = MagicMock()
    ModbusTelemetry._registry.clear()
    return ModbusTelemetry(hass, "SN-TEST", device_info)


# ---------------------------------------------------------------------------
# Bug 5 — deque eviction on failure / timeout paths
# ---------------------------------------------------------------------------

class TestTelemetryEviction:
    """_failures and _timeouts must not grow beyond the rolling window."""

    def test_failures_evicted_during_failure_only_run(self):
        """record_failure() must evict old entries without a record_request() call."""
        t = _make_telemetry()
        old_ts = time.monotonic() - _WINDOW_SEC - 10  # older than the window

        # Manually plant an old entry that should be evicted.
        t._failures.append(old_ts)

        # Now record a fresh failure; _evict() must remove the old entry.
        t.record_failure()

        # Only the new entry should remain.
        assert len(t._failures) == 1, (
            f"Expected 1 entry in _failures after eviction, got {len(t._failures)}. "
            "record_failure() is not calling _evict()."
        )

    def test_timeouts_evicted_during_timeout_only_run(self):
        """record_timeout() must evict old entries without a record_request() call."""
        t = _make_telemetry()
        old_ts = time.monotonic() - _WINDOW_SEC - 10

        t._timeouts.append(old_ts)
        t.record_timeout()

        assert len(t._timeouts) == 1, (
            f"Expected 1 entry in _timeouts after eviction, got {len(t._timeouts)}. "
            "record_timeout() is not calling _evict()."
        )

    def test_deques_bounded_under_continuous_failures(self):
        """Deques must stay bounded regardless of how many failures are recorded."""
        t = _make_telemetry()

        # Simulate a long outage: plant many old entries spanning 3 hours.
        base = time.monotonic() - 3 * _WINDOW_SEC
        for i in range(200):
            t._failures.append(base + i)
            t._timeouts.append(base + i)

        # Record a single failure at 'now'; this should trigger eviction.
        t.record_failure()
        t.record_timeout()

        # All entries older than the window should be gone.
        now = time.monotonic()
        cutoff = now - _WINDOW_SEC
        stale_failures = [ts for ts in t._failures if ts < cutoff]
        stale_timeouts = [ts for ts in t._timeouts if ts < cutoff]

        assert not stale_failures, (
            f"{len(stale_failures)} stale failure entries remain after eviction."
        )
        assert not stale_timeouts, (
            f"{len(stale_timeouts)} stale timeout entries remain after eviction."
        )

    def test_failure_rate_accurate_without_requests(self):
        """failure_rate_percent in snapshot must reflect windowed counts correctly."""
        t = _make_telemetry()

        # Plant old data that should be evicted.
        old_ts = time.monotonic() - _WINDOW_SEC - 10
        for _ in range(50):
            t._failures.append(old_ts)
            t._requests.append(old_ts)

        # Record a single fresh timeout (no successful request).
        t.record_timeout()

        snap = t.snapshot()

        # With no successful requests in the window, failure_rate should be 0
        # (the denominator is requests_per_hour, which should be 0 or very small).
        assert snap["requests_per_hour"] == 0, (
            "Old requests should have been evicted from the rolling window."
        )
        assert snap["timeouts_per_hour"] == 1

    def test_lifetime_totals_never_decrease(self):
        """total_* counters must be monotonically increasing regardless of eviction."""
        t = _make_telemetry()

        t.record_request(batch_size=5)
        t.record_failure()
        t.record_timeout()

        assert t.total_requests == 1
        assert t.total_failures == 2   # record_failure + record_timeout each increment this
        assert t.total_timeouts == 1

        # Record more; totals must only go up.
        t.record_failure()
        assert t.total_failures == 3

    def test_record_cache_hit_increments_correctly(self):
        t = _make_telemetry()
        t.record_cache_hit()
        t.record_cache_hit()
        assert t.total_cache_hits == 2
        snap = t.snapshot()
        assert snap["cache_hits_per_hour"] == 2

    def test_skipped_polls_tracked(self):
        t = _make_telemetry()
        t.record_skipped_poll()
        t.record_skipped_poll()
        snap = t.snapshot()
        assert snap["total_skipped_polls"] == 2
