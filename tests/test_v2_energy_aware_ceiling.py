"""Tests for v2.0.0's energy-aware _live_quality() ceiling
(V2_ARCHITECTURE_DESIGN.md §8.1) and record_attempt()/quality_of(), on top
of the base RegisterCache tests in test_register_cache.py.

Covers:
  - record_attempt() degrades quality/reason in place without touching
    value/ts, and is a safe no-op for a name with no existing entry.
  - Energy counters get a LONGER availability ceiling than everything
    else -- proven adversarially: the OLD single-ceiling design would have
    expired an energy counter well before ENERGY_AVAILABILITY_CEILING_S;
    the NEW design does not.
  - STATIC tier's exemption (§10.3) still takes precedence even for a
    (hypothetical) STATIC-classified energy counter -- exemption from
    EXPIRED entirely beats a merely-longer ceiling.
  - quality_of()'s full state matrix: NEVER_READ, GOOD, UNCERTAIN, EXPIRED.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
import unittest

# ── Stub huawei_solar (same minimal pattern as test_register_cache.py) ──────
_hs = types.ModuleType("huawei_solar")
_hs.RegisterName = str  # type: ignore[attr-defined]


class _Result:
    def __init__(self, v):
        self.value = v


_hs.Result = _Result  # type: ignore[attr-defined]
sys.modules.setdefault("huawei_solar", _hs)

_SRC = pathlib.Path(__file__).parent.parent / "register_cache.py"
_SPEC = importlib.util.spec_from_file_location("register_cache_energy_test", str(_SRC))
_MOD = importlib.util.module_from_spec(_SPEC)
_MOD.__package__ = "huawei_solar"
_SPEC.loader.exec_module(_MOD)

RegisterCache = _MOD.RegisterCache
RegisterTier = _MOD.RegisterTier
Quality = _MOD.Quality
Reason = _MOD.Reason


def _r(v):
    return _Result(v)


# A real SLOW-tier energy counter name (exact match in _ENERGY_COUNTER_NAMES).
_ENERGY_NAME = "daily_yield_energy"
# A real SLOW-tier, non-energy register name for comparison.
_NON_ENERGY_SLOW_NAME = "inverter_serial_number"  # STATIC actually -- see below
_NON_ENERGY_NAME = "some_slow_register_not_in_energy_set"


class TestRecordAttempt(unittest.TestCase):
    def test_degrades_existing_entry_in_place(self):
        c = RegisterCache()
        c.update({"soc": _r(80)})
        c.record_attempt(["soc"], Quality.UNCERTAIN, Reason.LINK_DOWN)
        quality, reason, age = c.quality_of("soc")
        self.assertEqual(quality, Quality.UNCERTAIN)
        self.assertEqual(reason, Reason.LINK_DOWN)
        self.assertIsNotNone(age)
        # value must survive untouched -- this is the whole point of the fix.
        self.assertEqual(c.get("soc").value, 80)

    def test_multiple_names_in_one_call(self):
        c = RegisterCache()
        c.update({"a": _r(1), "b": _r(2)})
        c.record_attempt(["a", "b"], Quality.UNCERTAIN, Reason.TIMEOUT)
        for name in ("a", "b"):
            quality, reason, _ = c.quality_of(name)
            self.assertEqual(quality, Quality.UNCERTAIN)
            self.assertEqual(reason, Reason.TIMEOUT)

    def test_no_op_for_name_with_no_existing_entry(self):
        c = RegisterCache()
        # Must not raise -- a register that was never cached and just failed
        # (e.g. BACKOFF_DEFERRED for something never read) has nothing to
        # degrade; quality_of() already reports NEVER_READ correctly.
        c.record_attempt(["never_seen"], Quality.UNCERTAIN, Reason.BACKOFF_DEFERRED)
        quality, reason, age = c.quality_of("never_seen")
        self.assertEqual(quality, Quality.BAD)
        self.assertEqual(reason, Reason.NEVER_READ)
        self.assertIsNone(age)

    def test_good_quality_path_via_record_attempt_not_used_for_success(self):
        # record_attempt() is documented as the non-success path; a fresh
        # success should go through update(), not record_attempt(). Confirm
        # record_attempt() doesn't itself refresh ts/raw the way update()
        # does, even if called with GOOD (defensive: it shouldn't be used
        # this way, but shouldn't corrupt state if it somehow is).
        c = RegisterCache()
        c.update({"soc": _r(80)})
        entry_ts_before = c._store["soc"].ts
        c.record_attempt(["soc"], Quality.GOOD, None)
        self.assertEqual(c._store["soc"].ts, entry_ts_before, "record_attempt must not touch ts")


class TestEnergyAwareCeiling(unittest.TestCase):
    def test_adversarial_old_single_ceiling_would_have_expired_this_energy_counter(self):
        """Proves the fix is real: reproduces what the OLD (pre-v2.0.0)
        single-ceiling design would have done, and shows it disagrees with
        the new design at a time past the generic ceiling but before the
        energy-specific one."""
        starvation_ceiling = 300.0
        energy_ceiling = 600.0
        c = RegisterCache(
            starvation_ceiling_s=starvation_ceiling,
            energy_availability_ceiling_s=energy_ceiling,
        )
        c.update({_ENERGY_NAME: _r(123.4)})
        c.record_attempt([_ENERGY_NAME], Quality.UNCERTAIN, Reason.LINK_DOWN)
        # Age it past the generic ceiling but well short of the energy one.
        c._store[_ENERGY_NAME].ts -= (starvation_ceiling + 30.0)

        # OLD design (reproduced): a single ceiling applied to everything.
        import time as _time
        age = _time.monotonic() - c._store[_ENERGY_NAME].ts
        old_design_result = Quality.BAD if age > starvation_ceiling else Quality.UNCERTAIN
        self.assertEqual(
            old_design_result, Quality.BAD,
            "sanity check: this age must exceed the generic ceiling for the "
            "adversarial comparison to mean anything",
        )

        # NEW design: still UNCERTAIN (servable) at this age, because this
        # register is a real energy counter and gets the longer ceiling.
        quality, reason, _ = c.quality_of(_ENERGY_NAME)
        self.assertEqual(
            quality, Quality.UNCERTAIN,
            "energy counter should still be servable at this age under the "
            "new, longer ceiling -- the old design would have wrongly "
            "expired it here",
        )
        self.assertIsNotNone(c.get(_ENERGY_NAME), "must still be served, not withheld")

    def test_energy_counter_does_expire_past_its_own_longer_ceiling(self):
        c = RegisterCache(starvation_ceiling_s=300.0, energy_availability_ceiling_s=600.0)
        c.update({_ENERGY_NAME: _r(1.0)})
        c.record_attempt([_ENERGY_NAME], Quality.UNCERTAIN, Reason.LINK_DOWN)
        c._store[_ENERGY_NAME].ts -= 700.0  # past even the longer ceiling
        quality, reason, _ = c.quality_of(_ENERGY_NAME)
        self.assertEqual(quality, Quality.BAD)
        self.assertEqual(reason, Reason.EXPIRED)
        self.assertIsNone(c.get(_ENERGY_NAME))

    def test_non_energy_register_uses_the_generic_shorter_ceiling(self):
        c = RegisterCache(starvation_ceiling_s=300.0, energy_availability_ceiling_s=600.0)
        c.update({_NON_ENERGY_NAME: _r(1.0)})
        c.record_attempt([_NON_ENERGY_NAME], Quality.UNCERTAIN, Reason.LINK_DOWN)
        c._store[_NON_ENERGY_NAME].ts -= 350.0  # past generic, well short of energy
        quality, reason, _ = c.quality_of(_NON_ENERGY_NAME)
        self.assertEqual(
            quality, Quality.BAD,
            "a non-energy register must use the SHORTER generic ceiling, "
            "not the longer energy one",
        )
        self.assertEqual(reason, Reason.EXPIRED)

    def test_static_exemption_still_wins_over_the_energy_ceiling(self):
        """§10.3's STATIC exemption (no EXPIRED at all, ever) must take
        precedence even in the hypothetical case of a STATIC-classified
        energy-counter-named register -- exemption beats "merely longer"."""
        c = RegisterCache(starvation_ceiling_s=300.0, energy_availability_ceiling_s=600.0)
        c.update({_ENERGY_NAME: _r(1.0)})
        c._store[_ENERGY_NAME].tier = RegisterTier.STATIC  # force for this test
        c.record_attempt([_ENERGY_NAME], Quality.UNCERTAIN, Reason.LINK_DOWN)
        c._store[_ENERGY_NAME].ts -= 10_000.0  # absurdly old
        quality, reason, _ = c.quality_of(_ENERGY_NAME)
        self.assertEqual(
            quality, Quality.UNCERTAIN,
            "STATIC tier must never become EXPIRED, regardless of how the "
            "register's name classifies for energy-counter purposes",
        )


class TestQualityOfStateMatrix(unittest.TestCase):
    def test_never_read(self):
        c = RegisterCache()
        quality, reason, age = c.quality_of("nope")
        self.assertEqual(quality, Quality.BAD)
        self.assertEqual(reason, Reason.NEVER_READ)
        self.assertIsNone(age)

    def test_good_right_after_update(self):
        c = RegisterCache()
        c.update({"soc": _r(80)})
        quality, reason, age = c.quality_of("soc")
        self.assertEqual(quality, Quality.GOOD)
        self.assertIsNone(reason)
        self.assertIsNotNone(age)
        self.assertLess(age, 1.0)

    def test_write_pending_is_bad_not_uncertain(self):
        # V2_ARCHITECTURE_DESIGN.md §6: WRITE_PENDING stays BAD, deliberately
        # different from the reconnect (UNCERTAIN) case -- we KNOW the old
        # value is wrong, not merely unverified.
        c = RegisterCache()
        c.update({"soc": _r(80)})
        c.invalidate("soc")
        quality, reason, _ = c.quality_of("soc")
        self.assertEqual(quality, Quality.BAD)
        self.assertEqual(reason, Reason.WRITE_PENDING)
        self.assertIsNone(c.get("soc"), "a WRITE_PENDING value must not be served")


if __name__ == "__main__":
    unittest.main()
