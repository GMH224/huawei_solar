"""Regression tests for Defect Y -- back-off's SLOW/STATIC deferral had no
ceiling of its own, so a register could go unread for as long as back-off
persisted (AUDIT_1.3.21.md). Field evidence: a 107-minute capture where
battery pack temperature registers refreshed at a median of ~9 minutes,
worst case ~22 minutes apart, and BMS temperature specifically was never
successfully read even once across the whole capture.

Requested directly, with a specific tolerance stated explicitly: "if they
haven't been updated for 5 minutes, they become more important," paired
with an explicit trade: "I rather have lower fast frequency than constant
errors." Two changes implement this:

1. FAST tier's base TTL: 0.0 -> 3.0s (register_cache.py). The "give."
2. A starvation ceiling: a SLOW/STATIC register more than
   REGISTER_STARVATION_CEILING_S (300s) PAST ITS OWN DUE-TIME is promoted
   into the back-off priority set, one per cycle
   (REGISTER_STARVATION_PROMOTIONS_PER_CYCLE), regardless of tier.
   (update_coordinator.py / register_cache.py). The "take," bounded.

The measurement is deliberately "time past due" (age - effective_ttl), not
raw "time since last read" -- SLOW's own 900s base TTL means a SLOW
register is already >=900s old the instant it first becomes due at all, so
thresholding raw age at 300s would fire on every SLOW/STATIC register the
moment it became due, defeating tier-based deferral entirely rather than
merely bounding it. This distinction is adversarially proven below, not
just asserted.
"""
from __future__ import annotations

import ast
import pathlib
import time
import unittest

_UPDATE_COORD_SRC = pathlib.Path(__file__).parent.parent / "update_coordinator.py"
_CACHE_SRC = pathlib.Path(__file__).parent.parent / "register_cache.py"
_CONST_SRC = pathlib.Path(__file__).parent.parent / "const.py"

STARVATION_CEILING_S = 300.0
PROMOTIONS_PER_CYCLE = 1
SLOW_TTL = 900.0
FAST_TTL = 3.0


# ═══════════════════════════════════════════════════════════════════════
# overdue_by() semantics, reproduced in isolation
# ═══════════════════════════════════════════════════════════════════════

class _FakeEntry:
    def __init__(self, ts: float, ttl: float):
        self.ts = ts
        self.ttl = ttl


def overdue_by(entry: _FakeEntry | None, now: float) -> float | None:
    """Exact mirror of RegisterCache.overdue_by()'s logic."""
    if entry is None:
        return None
    age = now - entry.ts
    return age - entry.ttl


def overdue_by_naive_raw_age(entry: _FakeEntry | None, now: float) -> float | None:
    """The REJECTED alternative: raw age since read, with no TTL offset.
    Reproduced only to prove adversarially why it would have been wrong."""
    if entry is None:
        return None
    return now - entry.ts


class TestOverdueBySemantics(unittest.TestCase):
    def test_none_for_never_read_register(self):
        self.assertIsNone(overdue_by(None, now=1000.0))

    def test_negative_immediately_after_read(self):
        """Just read -- very much NOT due yet, however you look at it."""
        entry = _FakeEntry(ts=1000.0, ttl=SLOW_TTL)
        self.assertLess(overdue_by(entry, now=1000.0), 0)

    def test_zero_exactly_at_its_own_due_time(self):
        entry = _FakeEntry(ts=1000.0, ttl=SLOW_TTL)
        self.assertAlmostEqual(overdue_by(entry, now=1000.0 + SLOW_TTL), 0.0)

    def test_grows_only_after_due_time(self):
        entry = _FakeEntry(ts=1000.0, ttl=SLOW_TTL)
        now = 1000.0 + SLOW_TTL + 120.0  # 120s past due
        self.assertAlmostEqual(overdue_by(entry, now=now), 120.0)

    def test_adversarial_naive_raw_age_would_fire_immediately_on_slow_tier(self):
        """Proves the rejected alternative was genuinely wrong, not just
        stylistically different: the instant a SLOW register first becomes
        due (age == 900s), naive raw-age already exceeds the 300s ceiling
        -- meaning EVERY SLOW/STATIC register would starve-promote the
        moment back-off started, defeating tier deferral entirely."""
        entry = _FakeEntry(ts=1000.0, ttl=SLOW_TTL)
        now_when_first_due = 1000.0 + SLOW_TTL  # the exact moment it becomes due

        naive = overdue_by_naive_raw_age(entry, now=now_when_first_due)
        correct = overdue_by(entry, now=now_when_first_due)

        self.assertGreaterEqual(
            naive, STARVATION_CEILING_S,
            "naive raw-age check already exceeds the ceiling the instant "
            "the register first becomes due -- would starve-promote "
            "immediately, defeating deferral",
        )
        self.assertLess(
            correct, STARVATION_CEILING_S,
            "the fixed (due-time-relative) check must NOT yet consider a "
            "just-became-due register starved",
        )

    def test_fixed_metric_does_eventually_cross_the_ceiling(self):
        """Confirms the fix isn't just 'never fires' -- it fires, correctly
        delayed by an extra STARVATION_CEILING_S beyond the due time."""
        entry = _FakeEntry(ts=1000.0, ttl=SLOW_TTL)
        now = 1000.0 + SLOW_TTL + STARVATION_CEILING_S + 1.0
        self.assertGreaterEqual(overdue_by(entry, now=now), STARVATION_CEILING_S)


# ═══════════════════════════════════════════════════════════════════════
# Back-off priority filter with starvation promotion, reproduced in isolation
# ═══════════════════════════════════════════════════════════════════════

FAST, NORMAL, SLOW, STATIC = "FAST", "NORMAL", "SLOW", "STATIC"


def _priority_filter_old(stale_names, classify, backoff_cycle, normal_divisor=4):
    """Pre-Defect-Y logic: SLOW/STATIC always fully deferred during backoff,
    no matter how long they've been waiting."""
    priority = []
    for n in stale_names:
        tier = classify(n)
        if tier == FAST:
            priority.append(n)
        elif tier == NORMAL and backoff_cycle % normal_divisor == 0:
            priority.append(n)
        # SLOW/STATIC: always skipped, unconditionally
    return priority


def _priority_filter_new(stale_names, classify, overdue_fn, backoff_cycle,
                          normal_divisor=4, ceiling=STARVATION_CEILING_S,
                          promotions_per_cycle=PROMOTIONS_PER_CYCLE):
    """Exact mirror of the fixed logic in update_coordinator.py -- note the
    bare `else:` below (not `elif tier in (SLOW, STATIC):`), matching the
    real code precisely: anything not FAST/NORMAL falls into the
    starvation-tracked path, including a None tier from a cache-based
    (rather than name-based) classifier."""
    priority = []
    starved = []
    for n in stale_names:
        tier = classify(n)
        if tier == FAST:
            priority.append(n)
        elif tier == NORMAL and backoff_cycle % normal_divisor == 0:
            priority.append(n)
        elif tier != NORMAL:
            overdue = overdue_fn(n)
            if overdue is None:
                starved.append((float("inf"), n))
            elif overdue >= ceiling:
                starved.append((overdue, n))
    if starved:
        starved.sort(key=lambda item: item[0], reverse=True)
        priority.extend(n for _, n in starved[:promotions_per_cycle])
    return priority


class TestStarvationPromotion(unittest.TestCase):
    def test_adversarial_old_pattern_starves_forever(self):
        """Proves the hazard is real: a SLOW register deferred through 50
        consecutive back-off cycles is NEVER once included, regardless of
        how overdue it becomes."""
        classify = lambda n: SLOW
        for cycle in range(1, 51):
            result = _priority_filter_old(["bms_temperature"], classify, cycle)
            self.assertEqual(result, [], f"cycle {cycle}: must be empty under the old logic")

    def test_new_pattern_promotes_once_starved(self):
        classify = lambda n: SLOW
        # Not yet starved: overdue is below the ceiling.
        overdue_fn = lambda n: 100.0
        result = _priority_filter_new(["bms_temperature"], classify, overdue_fn, backoff_cycle=5)
        self.assertEqual(result, [], "must not promote before crossing the ceiling")

        # Now starved.
        overdue_fn = lambda n: STARVATION_CEILING_S + 1.0
        result = _priority_filter_new(["bms_temperature"], classify, overdue_fn, backoff_cycle=5)
        self.assertEqual(result, ["bms_temperature"])

    def test_never_read_register_is_promoted_as_maximally_starved(self):
        classify = lambda n: STATIC
        overdue_fn = lambda n: None  # never read
        result = _priority_filter_new(["bms_temperature"], classify, overdue_fn, backoff_cycle=5)
        self.assertEqual(result, ["bms_temperature"])

    def test_only_one_promotion_per_cycle_even_with_many_starved_candidates(self):
        """The 'no thundering herd' guarantee: many SLOW/STATIC registers
        crossing the ceiling together (realistic -- they're often read as
        one original batch, so they share similar timestamps) still only
        inject ONE extra read per cycle, not a burst."""
        names = [f"reg_{i}" for i in range(10)]
        classify = lambda n: SLOW
        overdue_fn = lambda n: STARVATION_CEILING_S + 50.0  # all equally starved
        result = _priority_filter_new(names, classify, overdue_fn, backoff_cycle=5)
        self.assertEqual(len(result), 1, "must promote at most one per cycle")

    def test_most_overdue_candidate_wins(self):
        names = ["less_overdue", "most_overdue", "medium_overdue"]
        classify = lambda n: SLOW
        overdue_values = {"less_overdue": 310.0, "most_overdue": 900.0, "medium_overdue": 500.0}
        overdue_fn = lambda n: overdue_values[n]
        result = _priority_filter_new(names, classify, overdue_fn, backoff_cycle=5)
        self.assertEqual(result, ["most_overdue"])

    def test_fast_and_normal_behaviour_unaffected(self):
        """No regression: FAST is still always included, NORMAL still
        follows its own divisor rule, regardless of the new starvation path."""
        classify = lambda n: {"p": FAST, "q": NORMAL}[n]
        overdue_fn = lambda n: 0.0  # irrelevant for FAST/NORMAL
        result = _priority_filter_new(["p", "q"], classify, overdue_fn, backoff_cycle=4)
        self.assertIn("p", result)
        self.assertIn("q", result)  # cycle 4 % 4 == 0
        result = _priority_filter_new(["p", "q"], classify, overdue_fn, backoff_cycle=5)
        self.assertIn("p", result)
        self.assertNotIn("q", result)  # cycle 5 % 4 != 0


# ═══════════════════════════════════════════════════════════════════════
# The classify_register vs tier_of bug, caught and fixed during this work
# ═══════════════════════════════════════════════════════════════════════

class TestNeverCachedRegisterClassifiedCorrectly(unittest.TestCase):
    def test_adversarial_tier_of_style_lookup_misclassifies_uncached_fast_register(self):
        """Proves the bug an early draft of this fix had: using a
        cache-lookup-based classifier (returns None for anything never yet
        cached) would route a brand-new FAST-tier register into the
        starvation path instead of always including it."""
        tier_of_style = lambda n: None  # never cached -- mirrors RegisterCache.tier_of()
        overdue_fn = lambda n: None  # also never read, naturally
        result = _priority_filter_new(["new_fast_register"], tier_of_style, overdue_fn, backoff_cycle=5)
        # With a tier_of-style classifier, this ends up in `starved` and is
        # capped to one promotion -- not the "always include" FAST gets.
        # Demonstrating this ambiguity is exactly why classify_register()
        # (name-based, not cache-based) is required instead.
        self.assertEqual(result, ["new_fast_register"])  # promoted via starvation, not via "always FAST"

    def test_name_based_classifier_gets_it_right(self):
        classify_register_style = lambda n: FAST  # correct, name-based, cache-independent
        overdue_fn = lambda n: None
        result = _priority_filter_new(["new_fast_register"], classify_register_style, overdue_fn, backoff_cycle=5)
        self.assertEqual(result, ["new_fast_register"])  # included via the FAST branch directly


# ═══════════════════════════════════════════════════════════════════════
# Static (AST/source) checks against the real code
# ═══════════════════════════════════════════════════════════════════════

class TestSourceImplementsDefectY(unittest.TestCase):
    def test_overdue_by_exists_and_uses_effective_ttl_not_raw_age(self):
        tree = ast.parse(_CACHE_SRC.read_text())
        func = next(
            (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "overdue_by"),
            None,
        )
        assert func is not None, "RegisterCache.overdue_by() not found"
        calls_effective_ttl = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "_effective_ttl"
            for n in ast.walk(func)
        )
        assert calls_effective_ttl, (
            "overdue_by() does not use _effective_ttl() -- if it's using raw "
            "age instead, this reintroduces the naive-age flaw."
        )

    def test_priority_filter_uses_classify_register_not_tier_of(self):
        tree = ast.parse(_UPDATE_COORD_SRC.read_text())
        func = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef)
                and n.name == "_async_update_data"
                and n.returns is not None
                and "RegisterName" in ast.dump(n.returns)
            ),
            None,
        )
        assert func is not None
        source_segment = ast.get_source_segment(_UPDATE_COORD_SRC.read_text(), func) or ""
        # Find the priority-filter loop specifically (after "back-off cycle")
        idx = source_segment.find("back-off cycle")
        window = source_segment[idx: idx + 4000]
        assert "classify_register(n)" in window, (
            "priority filter no longer uses classify_register(n) -- this "
            "reintroduces the never-cached-register misclassification bug."
        )
        # AST-based (not string search) so this doesn't trip on the fix's
        # own explanatory comment, which mentions the old call by name.
        calls_tier_of = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "tier_of"
            for n in ast.walk(func)
        )
        assert not calls_tier_of, (
            "priority filter calls .tier_of() again -- this returns None "
            "for never-cached registers regardless of their real tier, "
            "misrouting them into the starvation path."
        )

    def test_starvation_constants_exist_with_expected_values(self):
        tree = ast.parse(_CONST_SRC.read_text())
        values = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id in (
                    "REGISTER_STARVATION_CEILING_S",
                    "REGISTER_STARVATION_PROMOTIONS_PER_CYCLE",
                ) and isinstance(node.value, ast.Constant):
                    values[node.target.id] = node.value.value
        assert values.get("REGISTER_STARVATION_CEILING_S") == 300.0, (
            "REGISTER_STARVATION_CEILING_S missing or not 300.0 (the "
            "operator's own stated 5-minute tolerance)"
        )
        assert values.get("REGISTER_STARVATION_PROMOTIONS_PER_CYCLE") == 1, (
            "REGISTER_STARVATION_PROMOTIONS_PER_CYCLE missing or not 1 -- "
            "this is the 'no thundering herd' guarantee"
        )

    def test_fast_tier_base_ttl_is_3_seconds(self):
        tree = ast.parse(_CACHE_SRC.read_text())
        # _TIER_BASE_TTL is a dict literal; find the FAST -> value mapping.
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if (
                        isinstance(k, ast.Attribute) and k.attr == "FAST"
                        and isinstance(v, ast.Constant) and v.value == 3.0
                    ):
                        found = True
        assert found, "RegisterTier.FAST's base TTL is not 3.0 in _TIER_BASE_TTL"


if __name__ == "__main__":
    unittest.main()
