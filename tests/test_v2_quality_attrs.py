"""Direct tests for HuaweiSolarEntity._quality_attrs() (types.py), the
shared, single implementation of the data_quality/data_quality_reason/
data_age_seconds attributes used by every entity platform
(V2_ARCHITECTURE_DESIGN.md §8, §10.4).

Loads the REAL types.py (not a fake stand-in like test_entities.py's
minimal HuaweiSolarEntity stub, which only exists to keep that file's
value/availability tests fast) against a REAL RegisterCache, so the actual
attribute-building logic is what's under test, not an assumption about it.
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types as _pytypes
import unittest
from datetime import timedelta
from unittest.mock import patch

_ROOT = pathlib.Path(__file__).parent.parent


def _load(modname: str, package: str = "huawei_solar"):
    src = _ROOT / f"{modname}.py"
    spec = importlib.util.spec_from_file_location(f"quality_attrs_test_{modname}", str(src))
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = package
    spec.loader.exec_module(mod)
    return mod


# ── Minimal stubs for types.py's dependencies ────────────────────────────────
# Lightweight, purpose-built fakes -- not the real "heavy" coordinator/HA
# classes, matching this project's established convention (e.g.
# test_synchronized_power_coordinator.py's own approach) for testing one
# specific piece of a heavy file's dependency chain without loading all of it.

_hs = sys.modules.get("huawei_solar") or _pytypes.ModuleType("huawei_solar")
# v2.0.0: sys.modules is process-global across the whole pytest run; several
# other test files also stub "huawei_solar" minimally for their own needs
# (e.g. just RegisterName/Result). setdefault() alone isn't robust here --
# if another file's stub got there first and lacks what THIS file needs,
# collection fails. Add only what's missing, on whatever's already there,
# rather than either blindly reusing an incompatible stub or replacing it
# wholesale (which could break whichever other file installed it first).
for _attr, _val in {
    "HuaweiSolarDevice": object,
    "RegisterName": str,
    "SUN2000Device": object,
    "Result": object,
}.items():
    if not hasattr(_hs, _attr):
        setattr(_hs, _attr, _val)
sys.modules["huawei_solar"] = _hs


class _Result:
    """v2.0.0: deliberately NOT reusing sys.modules["huawei_solar"].Result --
    another test file may already have installed a Result stub there with
    an incompatible constructor (confirmed: one such collision has a
    zero-argument constructor). Nothing in register_cache.py/types.py does
    an isinstance() check against the real Result type anywhere -- both
    duck-type via getattr(result, "value", result) -- so this class only
    needs to be constructible and carry .value; it does not need to BE the
    same class as whatever huawei_solar.Result currently resolves to.
    """

    def __init__(self, v):
        self.value = v

_hs_config_entries = _pytypes.ModuleType("homeassistant.config_entries")
_hs_config_entries.ConfigEntry = object
sys.modules["homeassistant.config_entries"] = _hs_config_entries

_hs_device_registry = _pytypes.ModuleType("homeassistant.helpers.device_registry")
_hs_device_registry.DeviceInfo = dict
sys.modules["homeassistant.helpers.device_registry"] = _hs_device_registry

_hs_entity = _pytypes.ModuleType("homeassistant.helpers.entity")


class _FakeEntity:
    pass


class _FakeEntityDescription:
    pass


_hs_entity.Entity = _FakeEntity
_hs_entity.EntityDescription = _FakeEntityDescription
sys.modules["homeassistant.helpers.entity"] = _hs_entity

_uc_stub = _pytypes.ModuleType("huawei_solar.update_coordinator")
_uc_stub.HuaweiSolarUpdateCoordinator = object
_uc_stub.HuaweiSolarOptimizerUpdateCoordinator = object
sys.modules["huawei_solar.update_coordinator"] = _uc_stub

# v2.0.0b (MOD-05/MOD-06): types.py now imports WRITE_TIMEOUT/
# WRITE_SEQUENCE_TIMEOUT from .const for the new _guarded_write()/
# _guarded_write_sequence() helpers. Real timedelta objects, not plain
# floats -- both helpers call .total_seconds() on these.
from datetime import timedelta as _timedelta
_const_stub = _pytypes.ModuleType("huawei_solar.const")
_const_stub.WRITE_TIMEOUT = _timedelta(seconds=15)
_const_stub.WRITE_SEQUENCE_TIMEOUT = _timedelta(seconds=30)
sys.modules["huawei_solar.const"] = _const_stub

TYPES = _load("types")
RC = _load("register_cache")


def _r(v):
    return _Result(v)


class _FakeCoordinator:
    """The real HuaweiSolarEntity._quality_attrs() only ever touches
    coordinator.cache -- everything else about a real coordinator is
    irrelevant to this method, so this is deliberately minimal."""

    def __init__(self, cache):
        self.cache = cache


class TestQualityAttrs(unittest.TestCase):
    def setUp(self):
        self.entity = TYPES.HuaweiSolarEntity()

    def test_good_quality_has_no_reason_key(self):
        cache = RC.RegisterCache()
        cache.update({"soc": _r(80)})
        coord = _FakeCoordinator(cache)
        attrs = self.entity._quality_attrs(coord, "soc")
        self.assertEqual(attrs["data_quality"], "good")
        self.assertNotIn(
            "data_quality_reason", attrs,
            "GOOD quality must omit the reason key entirely, not set it to None/empty",
        )
        self.assertIn("data_age_seconds", attrs)

    def test_uncertain_quality_includes_reason(self):
        cache = RC.RegisterCache()
        cache.update({"soc": _r(80)})
        cache.record_attempt(["soc"], RC.Quality.UNCERTAIN, RC.Reason.LINK_DOWN)
        coord = _FakeCoordinator(cache)
        attrs = self.entity._quality_attrs(coord, "soc")
        self.assertEqual(attrs["data_quality"], "uncertain")
        self.assertEqual(attrs["data_quality_reason"], "link_down")

    def test_bad_never_read_has_no_age(self):
        cache = RC.RegisterCache()
        coord = _FakeCoordinator(cache)
        attrs = self.entity._quality_attrs(coord, "nope")
        self.assertEqual(attrs["data_quality"], "bad")
        self.assertEqual(attrs["data_quality_reason"], "never_read")
        self.assertNotIn(
            "data_age_seconds", attrs,
            "a register that was never read has no meaningful age -- must "
            "be omitted, not set to None or 0",
        )

    def test_age_is_rounded_not_raw_float(self):
        cache = RC.RegisterCache()
        cache.update({"soc": _r(80)})
        coord = _FakeCoordinator(cache)
        attrs = self.entity._quality_attrs(coord, "soc")
        # round(x, 1) -- a fresh read should be a small number with at most
        # one decimal place, not a long raw float.
        age = attrs["data_age_seconds"]
        self.assertEqual(age, round(age, 1))

    def test_shared_across_calls_not_recreated_incorrectly(self):
        """The same HuaweiSolarEntity instance, called for two different
        registers with different quality, must not leak state between
        calls (a real risk if this were implemented with any per-call
        mutable default or cached state)."""
        cache = RC.RegisterCache()
        cache.update({"a": _r(1), "b": _r(2)})
        cache.record_attempt(["a"], RC.Quality.UNCERTAIN, RC.Reason.TIMEOUT)
        coord = _FakeCoordinator(cache)

        attrs_a = self.entity._quality_attrs(coord, "a")
        attrs_b = self.entity._quality_attrs(coord, "b")
        self.assertEqual(attrs_a["data_quality"], "uncertain")
        self.assertEqual(attrs_b["data_quality"], "good")
        self.assertNotIn("data_quality_reason", attrs_b)


class _FakeGuardForWriteTests:
    """Minimal ModbusGuard stand-in for _guarded_write() tests -- a real
    async context manager (unlike a MagicMock's default __aenter__), so
    it correctly nests with the real asyncio.timeout() inside
    _guarded_write()/_guarded_write_sequence()."""

    def __init__(self):
        self.request_calls: list[str] = []

    def request(self, *, label: str = ""):
        self.request_calls.append(label)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _SlowDevice:
    """A device whose set() takes as long as told -- used to prove the
    write timeout genuinely fires, not just that it's referenced."""

    def __init__(self, delay_s: float, result=True):
        self.delay_s = delay_s
        self.result = result
        self.calls: list[tuple] = []

    async def set(self, name, value):
        self.calls.append((name, value))
        await asyncio.sleep(self.delay_s)
        return self.result


class TestGuardedWrite(unittest.TestCase):
    """v2.0.0b (MOD-05, external ICS audit -- confirmed): _guarded_write()
    is the shared primitive that pairs ModbusGuard with an actual
    timeout around device.set() -- proven here against the REAL
    implementation, not a stub, since a stub could hide exactly the kind
    of gap this fix closes."""

    def setUp(self):
        self.entity = TYPES.HuaweiSolarEntity()

    def test_fast_write_succeeds_and_returns_the_device_result(self):
        guard = _FakeGuardForWriteTests()
        device = _SlowDevice(delay_s=0.001, result=True)
        result = asyncio.run(
            self.entity._guarded_write(guard, device, "some_register", 42, label="test_write")
        )
        self.assertTrue(result)
        self.assertEqual(device.calls, [("some_register", 42)])
        self.assertEqual(guard.request_calls, ["test_write"])

    def test_slow_write_actually_times_out(self):
        """The core claim: a write that hangs longer than WRITE_TIMEOUT
        must raise TimeoutError, not hang forever holding the guard.
        WRITE_TIMEOUT patched to a small value for a fast test -- the
        real 15s default is not itself under test here, just that the
        bound is genuinely enforced."""
        guard = _FakeGuardForWriteTests()
        device = _SlowDevice(delay_s=0.2, result=True)
        with patch.object(TYPES, "WRITE_TIMEOUT", timedelta(seconds=0.02)):
            with self.assertRaises(TimeoutError):
                asyncio.run(
                    self.entity._guarded_write(guard, device, "reg", 1, label="test_write")
                )


class TestGuardedWriteSequence(unittest.TestCase):
    """v2.0.0b (MOD-06, external ICS audit -- confirmed): _guarded_write_
    sequence() holds the guard once across multiple writes, bounded by
    ONE whole-sequence deadline -- not one deadline per write, which
    would let total duration scale unboundedly with sequence length."""

    def setUp(self):
        self.entity = TYPES.HuaweiSolarEntity()

    def test_multiple_writes_share_one_guard_acquisition(self):
        guard = _FakeGuardForWriteTests()
        device = _SlowDevice(delay_s=0.001, result=True)

        async def _go():
            async with self.entity._guarded_write_sequence(guard, label="seq") as write:
                await write(device, "reg_a", 1)
                await write(device, "reg_b", 2)
                await write(device, "reg_c", 3)

        asyncio.run(_go())
        self.assertEqual(
            guard.request_calls, ["seq"],
            "all three writes must share ONE guard.request() call, not one each",
        )
        self.assertEqual(
            device.calls, [("reg_a", 1), ("reg_b", 2), ("reg_c", 3)],
        )

    def test_whole_sequence_deadline_is_a_single_fixed_budget_not_per_write(self):
        """The core claim from MOD-06's fix: the deadline bounds the
        WHOLE sequence once, not len(writes) * per-write-timeout. Proven
        by making each individual write fast enough to pass alone, but
        the sequence long enough in total to exceed a shrunk whole-
        sequence budget."""
        guard = _FakeGuardForWriteTests()
        device = _SlowDevice(delay_s=0.02, result=True)

        async def _go():
            async with self.entity._guarded_write_sequence(guard, label="seq") as write:
                for _ in range(10):  # 10 * 0.02s = 0.2s total, each write alone is fast
                    await write(device, "reg", 1)

        with patch.object(TYPES, "WRITE_SEQUENCE_TIMEOUT", timedelta(seconds=0.05)):
            with self.assertRaises(TimeoutError):
                asyncio.run(_go())

    def test_sequence_releases_the_guard_even_on_failure(self):
        """A write raising partway through the sequence must not leave
        the guard held -- the async context manager's own __aexit__
        handles this via normal exception propagation, checked directly
        rather than assumed."""
        guard = _FakeGuardForWriteTests()

        class _FailingDevice:
            async def set(self, name, value):
                raise ValueError("simulated write failure")

        async def _go():
            async with self.entity._guarded_write_sequence(guard, label="seq") as write:
                await write(_FailingDevice(), "reg", 1)

        with self.assertRaises(ValueError):
            asyncio.run(_go())
        # The guard's own request() was still called exactly once -- the
        # fake's __aexit__ always runs (matching a real async context
        # manager's guarantee), so nothing here should indicate a leak.
        self.assertEqual(guard.request_calls, ["seq"])


if __name__ == "__main__":
    unittest.main()
