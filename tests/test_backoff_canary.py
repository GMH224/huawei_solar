"""Regression test for Defect T -- HuaweiSolarUpdateCoordinator's back-off
state machine could become permanently wedged (AUDIT_1.3.17.md).

Root cause, confirmed directly from a field debug capture: two early-return
branches in `_async_update_data()` -- "everything is still within its cache
TTL" and "nothing FAST-tier is due this particular back-off cycle" -- both
returned a cached snapshot without raising, which Home Assistant logs as
`success: True`, but WITHOUT ever reaching the real success path further
down that resets `_consecutive_timeouts` / `_consecutive_failures` /
`_backoff_cycle` to zero. A coordinator that entered back-off could
therefore remain there indefinitely: every cycle "succeeds" from Home
Assistant's point of view without the coordinator ever actually attempting
real communication, so genuine recovery is never observed. This was
confirmed directly in the field: a back-off cycle counter climbing past 10
with zero interleaved failures, and every "successful" cycle's total
duration matching its own back-off sleep almost exactly (meaning no time
at all was spent actually talking to the device after waking).

Following this project's established trade-off for files too heavy to
import directly in this fast suite (update_coordinator.py pulls in a large
part of Home Assistant and the huawei_solar device layer -- see
test_deferred_first_poll.py's precedent): the exact branching logic is
reproduced here in an isolated mini-coordinator, both in its old (broken)
and new (fixed) form, so the adversarial comparison is direct and
mechanical rather than inferred. Static (AST) checks then confirm the
real source actually contains the fix.
"""
from __future__ import annotations

import ast
import pathlib
import unittest

_COORD_SRC = pathlib.Path(__file__).parent.parent / "update_coordinator.py"

MAX_CONSECUTIVE_TIMEOUTS = 3
BACKOFF_NORMAL_DIVISOR = 4

FAST, NORMAL, SLOW, STATIC = "FAST", "NORMAL", "SLOW", "STATIC"


class _FakeCache:
    """A minimal stand-in for RegisterCache, just enough to drive the
    branching logic under test."""

    def __init__(self, stale: set[str], tiers: dict[str, str]):
        self.stale = set(stale)
        self.tiers = tiers

    def filter_stale(self, all_names, _day_interval):
        return [n for n in all_names if n in self.stale]

    def tier_of(self, name):
        return self.tiers.get(name, NORMAL)


def _pick_backoff_canary(candidates, cache):
    """Exact mirror of the real helper added for this fix."""
    for n in candidates:
        if cache.tier_of(n) == FAST:
            return n
    return candidates[0] if candidates else None


class _MiniCoordinator:
    """Reproduces _async_update_data's steps 3-5 exactly, with a `fixed`
    flag selecting the old (broken) or new (patched) branching."""

    def __init__(self, all_names, cache, fixed: bool, start_in_backoff: bool = True):
        self.all_names = all_names
        self.cache = cache
        self.fixed = fixed
        self._consecutive_timeouts = MAX_CONSECUTIVE_TIMEOUTS if start_in_backoff else 0
        self._backoff_cycle = 0
        self.real_attempts: list[list[str]] = []

    def run_cycle(self, outcome: str = "success") -> str:
        """One call ~= one _async_update_data() invocation. `outcome`
        controls what a forced real attempt (if any) resolves to."""
        stale_names = self.cache.filter_stale(self.all_names, None)
        in_backoff = self._consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS

        if not stale_names:
            if self.fixed and in_backoff:
                canary = _pick_backoff_canary(self.all_names, self.cache)
                if canary is not None:
                    stale_names = [canary]
            if not stale_names:
                return "returned_cached_no_test"

        if in_backoff:
            self._backoff_cycle += 1
            priority_names = []
            for n in stale_names:
                tier = self.cache.tier_of(n)
                if tier == FAST:
                    priority_names.append(n)
                elif tier == NORMAL and self._backoff_cycle % BACKOFF_NORMAL_DIVISOR == 0:
                    priority_names.append(n)
            if self.fixed and not priority_names:
                canary = _pick_backoff_canary(self.all_names, self.cache)
                if canary is not None:
                    priority_names = [canary]
            stale_names = priority_names
        else:
            self._backoff_cycle = 0

        if not stale_names:
            return "returned_cached_no_test"

        # A real attempt is made.
        self.real_attempts.append(list(stale_names))
        if outcome == "success":
            self._consecutive_timeouts = 0
            self._backoff_cycle = 0
            return "real_success"
        self._consecutive_timeouts += 1
        return "real_failure"


class TestBackoffCanNoLongerWedgePermanently(unittest.TestCase):
    def test_old_pattern_wedges_forever_when_everything_cached(self):
        """Adversarial: proves the hazard is real. Nothing is stale (cache
        TTLs haven't expired), the coordinator is already in back-off --
        the OLD pattern must never attempt a real read, no matter how many
        cycles pass."""
        cache = _FakeCache(stale=set(), tiers={"input_power": FAST})
        coord = _MiniCoordinator(["input_power", "efficiency"], cache, fixed=False)

        outcomes = [coord.run_cycle() for _ in range(20)]

        self.assertTrue(all(o == "returned_cached_no_test" for o in outcomes))
        self.assertEqual(coord.real_attempts, [], "old pattern must never test the connection")
        # Confirms this really is permanent, not just slow: 20 cycles is
        # already far more than any real device would need to recover.

    def test_new_pattern_forces_a_real_attempt_when_everything_cached(self):
        cache = _FakeCache(stale=set(), tiers={"input_power": FAST})
        coord = _MiniCoordinator(["input_power", "efficiency"], cache, fixed=True)

        outcome = coord.run_cycle(outcome="success")

        self.assertEqual(outcome, "real_success")
        self.assertEqual(coord.real_attempts, [["input_power"]], "must probe the FAST-tier register")

    def test_new_pattern_recovers_and_stays_recovered(self):
        cache = _FakeCache(stale=set(), tiers={"input_power": FAST})
        coord = _MiniCoordinator(["input_power", "efficiency"], cache, fixed=True)

        first = coord.run_cycle(outcome="success")
        self.assertEqual(first, "real_success")
        self.assertEqual(coord._consecutive_timeouts, 0)
        self.assertEqual(coord._backoff_cycle, 0)

        # Next cycle: no longer in backoff, so the normal (non-canary) path
        # applies -- and since nothing is stale, it correctly does nothing.
        second = coord.run_cycle()
        self.assertEqual(second, "returned_cached_no_test")
        self.assertEqual(coord._backoff_cycle, 0, "must not silently re-enter back-off after recovering")

    def test_new_pattern_still_backs_off_correctly_when_genuinely_failing(self):
        """No regression: if the device really is still unreachable, the
        fix must not pretend otherwise -- a failed canary keeps back-off
        active exactly as a real failure always did."""
        cache = _FakeCache(stale=set(), tiers={"input_power": FAST})
        coord = _MiniCoordinator(["input_power", "efficiency"], cache, fixed=True)

        outcomes = [coord.run_cycle(outcome="failure") for _ in range(3)]

        self.assertTrue(all(o == "real_failure" for o in outcomes))
        self.assertEqual(len(coord.real_attempts), 3, "must keep genuinely testing, not give up silently")
        self.assertGreaterEqual(coord._consecutive_timeouts, MAX_CONSECUTIVE_TIMEOUTS)

    def test_old_pattern_wedges_even_with_priority_filtered_empty_set(self):
        """The second hazard: stale_names is non-empty, but priority
        filtering (FAST always, NORMAL every Nth cycle) empties it out."""
        cache = _FakeCache(
            stale={"efficiency"},  # stale, but NORMAL tier and not an Nth cycle yet
            tiers={"input_power": FAST, "efficiency": NORMAL},
        )
        coord = _MiniCoordinator(["input_power", "efficiency"], cache, fixed=False)

        outcome = coord.run_cycle()  # backoff_cycle becomes 1; 1 % 4 != 0

        self.assertEqual(outcome, "returned_cached_no_test")
        self.assertEqual(coord.real_attempts, [])

    def test_new_pattern_forces_canary_when_priority_filtered_empty(self):
        cache = _FakeCache(
            stale={"efficiency"},
            tiers={"input_power": FAST, "efficiency": NORMAL},
        )
        coord = _MiniCoordinator(["input_power", "efficiency"], cache, fixed=True)

        outcome = coord.run_cycle(outcome="success")

        self.assertEqual(outcome, "real_success")
        self.assertEqual(coord.real_attempts, [["input_power"]])


class TestSourceHasCanaryForcing(unittest.TestCase):
    def test_pick_backoff_canary_exists(self):
        tree = ast.parse(_COORD_SRC.read_text())
        found = any(
            isinstance(n, ast.FunctionDef) and n.name == "_pick_backoff_canary"
            for n in ast.walk(tree)
        )
        assert found, (
            "_pick_backoff_canary() not found in update_coordinator.py -- "
            "the Defect T fix is missing entirely."
        )

    def test_async_update_data_calls_canary_at_least_twice(self):
        """Both early-return sites must call the canary picker -- once
        for 'everything cached', once for 'priority-filtered to nothing'."""
        tree = ast.parse(_COORD_SRC.read_text())
        # Two _async_update_data methods exist (main coordinator and the
        # sibling optimizer coordinator) -- disambiguate by return
        # annotation rather than relying on source order.
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
        assert func is not None, "HuaweiSolarUpdateCoordinator._async_update_data not found"
        call_count = sum(
            1 for n in ast.walk(func)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_pick_backoff_canary"
        )
        assert call_count >= 2, (
            f"_pick_backoff_canary is called {call_count} time(s) in "
            "_async_update_data, expected at least 2 (one per early-return "
            "site) -- this reintroduces part of Defect T."
        )


if __name__ == "__main__":
    unittest.main()
