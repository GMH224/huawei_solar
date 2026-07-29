"""Tier-aware chunking, prio labelling and SLOW-tier TTL (v1.3.3).

FIELD BASIS (3,400 requests over 28.8 h, one shared bus):

  chunk of FAST/NORMAL only    : ~6 ms regardless of size (18 regs -> 6.2 ms)
  chunk containing SLOW/STATIC : ~2,900 ms + 377 ms/register

  99% of all service time was spent in the 20.7% of requests touching
  SLOW-tier content; `data_update_coordinator` alone was 52% of it.

The fixed ~2.9 s entry cost is what makes SPLITTING the expensive set a
pessimisation: 27 expensive registers cost ~13.1 s as one chunk but ~22.2 s as
four chunks of seven. These tests pin that reasoning so a future "just lower
BATCH_CHUNK_SIZE" change cannot quietly undo it.
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys
import types
import unittest

_ROOT = pathlib.Path(__file__).parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(f"tsep_{name}", str(_ROOT / f"{name}.py"))
    m = importlib.util.module_from_spec(spec)
    m.__package__ = "tsep"
    sys.modules[f"tsep_{name}"] = m
    spec.loader.exec_module(m)
    return m


if "tsep" not in sys.modules:
    p = types.ModuleType("tsep"); p.__path__ = []; sys.modules["tsep"] = p

for n in ("homeassistant", "homeassistant.core", "homeassistant.helpers",
          "homeassistant.helpers.storage"):
    if n not in sys.modules:
        sys.modules[n] = types.ModuleType(n)
if not hasattr(sys.modules["homeassistant.core"], "HomeAssistant"):
    sys.modules["homeassistant.core"].HomeAssistant = type("H", (), {})
    sys.modules["homeassistant.core"].callback = lambda f: f
if not hasattr(sys.modules["homeassistant.helpers.storage"], "Store"):
    sys.modules["homeassistant.helpers.storage"].Store = type("S", (), {})

hs = sys.modules.get("huawei_solar")
if hs is None:
    hs = types.ModuleType("huawei_solar"); hs.__path__ = []
    sys.modules["huawei_solar"] = hs
if not hasattr(hs, "RegisterName"):
    class RegisterName(str):
        pass
    hs.RegisterName = RegisterName
if not hasattr(hs, "Result"):
    hs.Result = type("Result", (), {})

RC = _load("register_cache")
Tier = RC.RegisterTier


class TestSlowTierTTL(unittest.TestCase):
    """(3) Only reducing FREQUENCY reduces total cost."""

    def test_slow_ttl_raised_from_300(self):
        self.assertGreaterEqual(RC._TIER_BASE_TTL[Tier.SLOW], 900.0)

    def test_fast_and_normal_unchanged(self):
        """The cheap tiers must NOT be slowed — they are not the problem."""
        self.assertEqual(RC._TIER_BASE_TTL[Tier.FAST], 0.0)
        self.assertEqual(RC._TIER_BASE_TTL[Tier.NORMAL], 30.0)

    def test_ttl_override_is_clamped(self):
        original = RC._TIER_BASE_TTL[Tier.SLOW]
        try:
            RC.set_slow_tier_ttl(10)          # absurdly low
            self.assertGreaterEqual(RC._TIER_BASE_TTL[Tier.SLOW], 300.0)
            RC.set_slow_tier_ttl(999999)      # absurdly high
            self.assertLessEqual(RC._TIER_BASE_TTL[Tier.SLOW], 3600.0)
            RC.set_slow_tier_ttl(1200)
            self.assertEqual(RC._TIER_BASE_TTL[Tier.SLOW], 1200.0)
        finally:
            RC._TIER_BASE_TTL[Tier.SLOW] = original

    def test_expensive_exchange_frequency_reduced(self):
        """900 s vs 300 s is ~3x fewer expensive exchanges per day."""
        before = 86400 / 300
        after = 86400 / RC._TIER_BASE_TTL[Tier.SLOW]
        self.assertLessEqual(after, before / 2.5)


class TestChunkSourceContract(unittest.TestCase):
    """(1) and (2), asserted structurally against the source.

    update_coordinator.py cannot be imported without a full HA runtime, so
    these are AST/source checks — but they pin the exact decisions that the
    field data drove, with the reasoning attached.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = (_ROOT / "update_coordinator.py").read_text()
        cls.tree = ast.parse(cls.src)

    def _fn(self, name):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        return None

    # ── (1) prio labelling ──────────────────────────────────────────────────
    def test_chunk_tier_reports_slowest_not_fastest(self):
        """REGRESSION: min() labelled a 26-SLOW chunk as 'FAST'.

        All 3,400 field records came back 'FAST', including a 51.5 s outlier,
        making the field useless for the correlation it existed for.
        """
        fn = self._fn("_chunk_tier")
        self.assertIsNotNone(fn)
        # Strip the docstring: it deliberately NAMES the old min() bug, so a
        # naive source search would match the explanation of the defect.
        executable = ast.Module(
            body=[n for n in fn.body if not (
                isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str))],
            type_ignores=[],
        )
        body = ast.unparse(executable)
        self.assertIn("max(tiers)", body)
        self.assertNotIn("min(tiers)", body)

    def test_chunk_tier_reports_composition(self):
        """A tier label alone cannot distinguish 1 SLOW from 26 SLOW."""
        fn = self._fn("_chunk_tier")
        self.assertIn("composition", ast.unparse(fn))

    # ── (2) tier separation ─────────────────────────────────────────────────
    def test_split_by_cost_exists_and_splits_at_slow(self):
        fn = self._fn("_split_by_cost")
        self.assertIsNotNone(fn, "_split_by_cost must exist")
        body = ast.unparse(fn)
        self.assertIn("RegisterTier.SLOW", body)
        self.assertIn("expensive", body)

    def test_chunking_separates_cheap_from_expensive(self):
        self.assertIn("cheap_names, expensive_names = _split_by_cost", self.src)
        self.assertIn("_chunk(cheap_names, BATCH_CHUNK_SIZE)", self.src)

    def test_expensive_set_is_not_fragmented(self):
        """The fixed ~2.9 s entry cost makes splitting a pessimisation.

        27 expensive registers: ~13.1 s as one chunk, ~22.2 s as four of seven.
        The expensive group must therefore use the FULL BATCH_CHUNK_SIZE, not
        a reduced cap.
        """
        self.assertIn("_chunk(\n            expensive_names, BATCH_CHUNK_SIZE\n        )", self.src)

    def test_batch_chunk_size_not_reduced(self):
        """Guard against a future 'just lower the chunk size' change."""
        const = (_ROOT / "const.py").read_text()
        for line in const.splitlines():
            if line.strip().startswith("BATCH_CHUNK_SIZE"):
                value = int(line.split("=")[1].split("#")[0].strip())
                self.assertGreaterEqual(
                    value, 20,
                    "lowering BATCH_CHUNK_SIZE fragments the expensive set and "
                    "multiplies the ~2.9 s per-request entry cost",
                )


class TestCostModel(unittest.TestCase):
    """The arithmetic that drove the design decision, pinned."""

    FIXED_MS = 2924.0
    PER_REG_MS = 377.0

    def cost(self, regs, chunks=1):
        per = regs / chunks
        return chunks * (self.FIXED_MS + self.PER_REG_MS * per)

    def test_splitting_expensive_registers_is_worse(self):
        one = self.cost(27, 1)
        four = self.cost(27, 4)
        self.assertGreater(four, one)
        self.assertGreater(four - one, 8000)      # ~9 s worse

    def test_separating_cheap_registers_is_free(self):
        """Cheap chunks cost ~6 ms, so an extra request is negligible."""
        self.assertLess(6.0, self.FIXED_MS / 100)


if __name__ == "__main__":
    unittest.main()


# ══════════════════════════════════════════════════════════════════════════════
# v1.3.4 — SLOW-tier coalescing, night deferral, wait instrumentation
# ══════════════════════════════════════════════════════════════════════════════
from datetime import timedelta


def _cache():
    c = RC.RegisterCache()
    return c


def _seed(cache, name, tier, age_s, now):
    """Put an entry in the cache with a controlled age."""
    e = RC._CacheEntry(value=1, raw=1, ts=now - age_s, tier=tier)
    cache._store[name] = e
    return e


class TestSlowTierCoalescing(unittest.TestCase):
    """(1) The dominant remaining cost, and why TTL alone does not fix it.

    Measured: `data_update_coordinator` performed 37.9 expensive reads/hour —
    one every 1.6 min — against a 300 s TTL that should allow ~12/h. TTLs are
    per register, so ~26 SLOW registers expire at ~26 different moments and
    nearly every poll drags one in, paying the full ~2.9 s entry cost.

    Raising the TTL cuts each register's rate but NOT the number of distinct
    expiry moments: 26 registers at 900 s still expire ~1.7x/minute.
    Coalescing collapses them onto a shared expiry.
    """

    def setUp(self):
        self.now = RC.time.monotonic()
        self.ttl = timedelta(seconds=30)

    def test_due_expensive_register_pulls_in_fresh_siblings(self):
        c = _cache()
        names = ["slow_a", "slow_b", "slow_c"]
        _seed(c, "slow_a", Tier.SLOW, 99999, self.now)     # overdue
        _seed(c, "slow_b", Tier.SLOW, 1, self.now)         # fresh
        _seed(c, "slow_c", Tier.SLOW, 1, self.now)         # fresh
        stale = c.filter_stale(names, self.ttl)
        self.assertEqual(set(stale), set(names),
                         "one due expensive register must refresh the cohort")
        self.assertEqual(c.coalesce_events, 1)
        self.assertEqual(c.coalesced_registers, 2)

    def test_cheap_registers_are_not_pulled_in(self):
        """Coalescing must not inflate a chunk with FAST/NORMAL registers.

        Those cost ~6 ms and have their own cadence; dragging them along would
        defeat the tier separation shipped in v1.3.3.
        """
        c = _cache()
        _seed(c, "slow_a", Tier.SLOW, 99999, self.now)
        _seed(c, "fast_b", Tier.FAST, 0.0, self.now)
        _seed(c, "norm_c", Tier.NORMAL, 0.0, self.now)
        stale = c.filter_stale(["slow_a", "fast_b", "norm_c"], self.ttl)
        self.assertIn("slow_a", stale)
        self.assertNotIn("norm_c", stale)

    def test_no_coalescing_when_nothing_expensive_is_due(self):
        c = _cache()
        _seed(c, "slow_a", Tier.SLOW, 1, self.now)
        _seed(c, "slow_b", Tier.SLOW, 1, self.now)
        stale = c.filter_stale(["slow_a", "slow_b"], self.ttl)
        self.assertEqual(stale, [])
        self.assertEqual(c.coalesce_events, 0)

    def test_coalescing_can_be_disabled(self):
        c = _cache()
        c.set_coalesce_slow_tier(False)
        _seed(c, "slow_a", Tier.SLOW, 99999, self.now)
        _seed(c, "slow_b", Tier.SLOW, 1, self.now)
        stale = c.filter_stale(["slow_a", "slow_b"], self.ttl)
        self.assertEqual(stale, ["slow_a"])
        self.assertEqual(c.coalesce_events, 0)

    def test_expected_cost_reduction(self):
        """The arithmetic that justifies the change."""
        fixed, per_reg = 2924.0, 377.0
        today = 37.9 * (fixed + 13 * per_reg) / 1000
        coalesced = 4.0 * (fixed + 26 * per_reg) / 1000
        self.assertGreater(today / coalesced, 4.0)


class TestNightDeferral(unittest.TestCase):
    """(3) Deliberately OFF by default — no night data exists yet."""

    def setUp(self):
        self.now = RC.time.monotonic()
        self.ttl = timedelta(seconds=30)

    def test_disabled_by_default(self):
        self.assertFalse(_cache()._prefer_night_for_slow)

    def test_defers_expensive_refresh_in_daylight_when_enabled(self):
        c = _cache()
        c.set_prefer_night_for_slow(True)
        c.set_night_mode(False)
        _seed(c, "slow_a", Tier.SLOW, RC._TIER_BASE_TTL[Tier.SLOW] * 1.2, self.now)
        stale = c.filter_stale(["slow_a"], self.ttl)
        self.assertEqual(stale, [])
        self.assertEqual(c.deferred_expensive, 1)

    def test_deferral_cannot_starve(self):
        """Past the bound, an overdue register is refreshed regardless."""
        c = _cache()
        c.set_prefer_night_for_slow(True)
        c.set_night_mode(False)
        age = RC._TIER_BASE_TTL[Tier.SLOW] * (RC.SLOW_DEFER_MAX_TTL_MULTIPLE + 1)
        _seed(c, "slow_a", Tier.SLOW, age, self.now)
        self.assertEqual(c.filter_stale(["slow_a"], self.ttl), ["slow_a"])

    def test_night_mode_allows_the_refresh(self):
        c = _cache()
        c.set_prefer_night_for_slow(True)
        c.set_night_mode(True)
        _seed(c, "slow_a", Tier.SLOW, RC._TIER_BASE_TTL[Tier.SLOW] * 1.2, self.now)
        self.assertEqual(c.filter_stale(["slow_a"], self.ttl), ["slow_a"])
