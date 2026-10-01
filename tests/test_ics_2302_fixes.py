"""Tests for v2.3.0.2.

HS-2302-001  A cached value is no longer withheld (entity `unknown`) on AGE
             ALONE. Expiry needs age > ceiling AND >= 2 consecutive missed
             refreshes, with a hard limit of 3 x ceiling of continuous
             uncertainty as defence in depth.
HS-2302-002  The coordinator success path no longer calls
             cache.invalidate_all() after a poll that followed a timeout,
             busy, shed or admission timeout. Keep-alive link loss still does.
HS-2302-003  Options label for sync_power_dedicated_reads; `{skipped_notice}`
             placeholder restored in 14 translations.
HS-2302-004  A register invalidated by our own write stays BAD/WRITE_PENDING
             through failed re-reads and keep-alive link loss (design §6).

Approach:
  * register_cache.py is imported and executed for real (stub huawei_solar
    package, same pattern as test_register_cache.py) with a fake monotonic
    clock, so every assertion runs the production code.
  * The five `unknown` episodes from the 2026-09-26/27 field capture are
    replayed through the real cache as anonymised event sequences (offsets
    in seconds from the register's last good read; no serial numbers, no
    addresses). The OLD behaviour is reproduced as a reference predicate
    plus the old success-path invalidate_all(); the NEW behaviour is the
    production code. The old arm must reproduce the durations actually
    observed in Home Assistant's history; that is what makes the new arm's
    result meaningful.
  * update_coordinator.py needs a live Home Assistant to import, so its
    changes are checked structurally with `ast`.

Run standalone:  cd tests && python3 -m pytest test_ics_2302_fixes.py
"""

from __future__ import annotations

import ast
import importlib.util
import json
import pathlib
import re
import sys
import types
import unittest
from datetime import timedelta

_ROOT = pathlib.Path(__file__).parent.parent

# ── real const.py ─────────────────────────────────────────────────────────────
_cspec = importlib.util.spec_from_file_location("hs2302_const", _ROOT / "const.py")
CONST = importlib.util.module_from_spec(_cspec)
_cspec.loader.exec_module(CONST)

# ── real register_cache.py, stubbed huawei_solar ──────────────────────────────
_hs = types.ModuleType("huawei_solar")
_hs.RegisterName = str  # type: ignore[attr-defined]


class _Result:
    def __init__(self, v):
        self.value = v


_hs.Result = _Result  # type: ignore[attr-defined]
sys.modules.setdefault("huawei_solar", _hs)

_rspec = importlib.util.spec_from_file_location(
    "register_cache_hs2302", str(_ROOT / "register_cache.py")
)
RC = importlib.util.module_from_spec(_rspec)
RC.__package__ = "huawei_solar"
_rspec.loader.exec_module(RC)

Quality, Reason, RegisterTier = RC.Quality, RC.Reason, RC.RegisterTier


class _Clock:
    """Stands in for the `time` module inside register_cache (monotonic only)."""

    def __init__(self, t: float = 10_000.0) -> None:
        self.t = t

    def monotonic(self) -> float:
        return self.t


def _cache(clock: _Clock, **kw) -> "RC.RegisterCache":
    RC.time = clock  # module-level `import time` -> fake clock for this module copy
    kw.setdefault("starvation_ceiling_s", CONST.REGISTER_STARVATION_CEILING_S)
    kw.setdefault("energy_availability_ceiling_s", CONST.ENERGY_AVAILABILITY_CEILING_S)
    kw.setdefault("min_missed_refreshes", CONST.MIN_MISSED_REFRESHES_BEFORE_EXPIRY)
    kw.setdefault("uncertain_hard_limit_factor", CONST.UNCERTAIN_HARD_LIMIT_FACTOR)
    return RC.RegisterCache(**kw)


def _r(v):
    return _Result(v)


FAST = "active_power"                 # FAST tier, not an energy counter
ENERGY = "accumulated_yield_energy"   # energy counter (600 s ceiling)
STATIC = "model_name"                 # STATIC tier


class TestPreconditions(unittest.TestCase):
    """The names used below classify the way the tests assume."""

    def test_register_classification(self):
        self.assertEqual(RC.classify_register(FAST), RegisterTier.FAST)
        self.assertFalse(RC.is_energy_counter(FAST))
        self.assertTrue(RC.is_energy_counter(ENERGY))
        self.assertEqual(RC.classify_register(STATIC), RegisterTier.STATIC)

    def test_constants(self):
        self.assertEqual(CONST.MIN_MISSED_REFRESHES_BEFORE_EXPIRY, 2)
        self.assertEqual(CONST.UNCERTAIN_HARD_LIMIT_FACTOR, 3.0)
        # unchanged ceilings -- this release does not move them
        self.assertEqual(CONST.REGISTER_STARVATION_CEILING_S, 300.0)
        self.assertEqual(CONST.ENERGY_AVAILABILITY_CEILING_S, 600.0)

    def test_constructor_defaults_match_const(self):
        """register_cache.py does not import const.py (dependency-light by
        design), so its defaults are duplicated; pin them together."""
        c = RC.RegisterCache()
        self.assertEqual(c._min_missed_refreshes, CONST.MIN_MISSED_REFRESHES_BEFORE_EXPIRY)
        self.assertEqual(c._uncertain_hard_limit_factor, CONST.UNCERTAIN_HARD_LIMIT_FACTOR)
        self.assertEqual(c._starvation_ceiling_s, CONST.REGISTER_STARVATION_CEILING_S)
        self.assertEqual(c._energy_availability_ceiling_s, CONST.ENERGY_AVAILABILITY_CEILING_S)


# ── HS-2302-001: the rule itself ─────────────────────────────────────────────

class TestMissedRefreshRule(unittest.TestCase):
    def setUp(self):
        self.clk = _Clock()
        self.c = _cache(self.clk)
        self.c.update({FAST: _r(100), ENERGY: _r(5000.0)})

    def test_one_miss_past_ceiling_is_still_served(self):
        """The field case: value one poll old, one shed -> must not blank."""
        self.clk.t += 301
        self.c.record_attempt([FAST], Quality.UNCERTAIN, Reason.SHED)
        q, reason, age = self.c.quality_of(FAST)
        self.assertEqual((q, reason), (Quality.UNCERTAIN, Reason.SHED))
        self.assertAlmostEqual(age, 301)
        self.assertIsNotNone(self.c.get(FAST))
        self.assertIn(FAST, self.c.merge({}, [FAST]))

    def test_old_rule_would_have_withheld_it(self):
        """Adversarial control for the test above: the reproduced OLD
        predicate (UNCERTAIN and age > ceiling) says BAD at this state."""
        self.clk.t += 301
        self.c.record_attempt([FAST], Quality.UNCERTAIN, Reason.SHED)
        e = self.c._store[FAST]
        self.assertTrue(e.quality == Quality.UNCERTAIN and self.clk.t - e.ts > 300.0)

    def test_two_misses_past_ceiling_expire(self):
        self.clk.t += 150
        self.c.record_attempt([FAST], Quality.UNCERTAIN, Reason.SHED)
        self.clk.t += 151
        self.c.record_attempt([FAST], Quality.UNCERTAIN, Reason.TIMEOUT)
        self.assertEqual(self.c.quality_of(FAST)[:2], (Quality.BAD, Reason.EXPIRED))
        self.assertIsNone(self.c.get(FAST))
        self.assertNotIn(FAST, self.c.merge({}, [FAST]))

    def test_two_misses_within_ceiling_are_served(self):
        """Day-time case: two quick misses but a young value -> served."""
        self.clk.t += 30
        self.c.record_attempt([FAST], Quality.UNCERTAIN, Reason.DEVICE_BUSY)
        self.clk.t += 30
        self.c.record_attempt([FAST], Quality.UNCERTAIN, Reason.SHED)
        self.assertEqual(self.c.quality_of(FAST)[0], Quality.UNCERTAIN)
        self.assertIsNotNone(self.c.get(FAST))

    def test_every_non_good_reason_counts_as_a_miss(self):
        for reason in (Reason.SHED, Reason.ADMISSION_TIMEOUT, Reason.BACKOFF_DEFERRED,
                       Reason.TIMEOUT, Reason.LINK_DOWN, Reason.DEVICE_BUSY):
            with self.subTest(reason=reason.name):
                clk = _Clock()
                c = _cache(clk)
                c.update({FAST: _r(1)})
                clk.t += 400
                c.record_attempt([FAST], Quality.UNCERTAIN, reason)
                self.assertEqual(c.quality_of(FAST)[0], Quality.UNCERTAIN)
                c.record_attempt([FAST], Quality.UNCERTAIN, reason)
                self.assertEqual(c.quality_of(FAST)[:2], (Quality.BAD, Reason.EXPIRED))

    def test_successful_read_resets_the_streak(self):
        self.clk.t += 301
        self.c.record_attempt([FAST], Quality.UNCERTAIN, Reason.SHED)
        self.c.update({FAST: _r(101)})
        e = self.c._store[FAST]
        self.assertEqual((e.missed, e.uncertain_since), (0, None))
        # a new single miss after an old one must not add up to two
        self.clk.t += 301
        self.c.record_attempt([FAST], Quality.UNCERTAIN, Reason.SHED)
        self.assertEqual(self.c.quality_of(FAST)[0], Quality.UNCERTAIN)

    def test_reset_also_on_unchanged_value(self):
        """update() has two branches (value changed / unchanged); both reset."""
        self.clk.t += 10
        self.c.record_attempt([FAST], Quality.UNCERTAIN, Reason.SHED)
        self.c.update({FAST: _r(100)})  # same raw value -> TTL-stretch branch
        e = self.c._store[FAST]
        self.assertEqual((e.missed, e.uncertain_since, e.quality), (0, None, Quality.GOOD))

    def test_energy_counter_uses_its_longer_ceiling(self):
        self.clk.t += 450
        self.c.record_attempt([ENERGY], Quality.UNCERTAIN, Reason.SHED)
        self.c.record_attempt([ENERGY], Quality.UNCERTAIN, Reason.SHED)
        self.assertEqual(self.c.quality_of(ENERGY)[0], Quality.UNCERTAIN,
                         "2 misses but 450 s < 600 s energy ceiling -> served")
        self.clk.t += 151
        self.assertEqual(self.c.quality_of(ENERGY)[:2], (Quality.BAD, Reason.EXPIRED))

    def test_static_still_exempt(self):
        self.c.update({STATIC: _r("SUN2000")})
        self.clk.t += 100_000
        for _ in range(5):
            self.c.record_attempt([STATIC], Quality.UNCERTAIN, Reason.TIMEOUT)
        self.assertEqual(self.c.quality_of(STATIC)[0], Quality.UNCERTAIN)
        self.assertIsNotNone(self.c.get(STATIC))

    def test_write_pending_still_bad_immediately(self):
        self.c.invalidate(FAST)
        self.assertEqual(self.c.quality_of(FAST)[:2], (Quality.BAD, Reason.WRITE_PENDING))
        self.assertIsNone(self.c.get(FAST))

    def test_absent_entry_is_untouched(self):
        self.c.record_attempt(["never_seen"], Quality.UNCERTAIN, Reason.SHED)
        self.assertNotIn("never_seen", self.c._store)
        self.assertEqual(self.c.quality_of("never_seen")[:2], (Quality.BAD, Reason.NEVER_READ))

    def test_good_record_attempt_clears_counters(self):
        self.c.record_attempt([FAST], Quality.UNCERTAIN, Reason.SHED)
        self.c.record_attempt([FAST], Quality.GOOD, None)
        e = self.c._store[FAST]
        self.assertEqual((e.missed, e.uncertain_since), (0, None))

    def test_served_uncertain_value_is_still_due_for_a_read(self):
        """Serving longer must NOT mean reading less: an UNCERTAIN entry is
        still returned by filter_stale(), so every poll keeps trying."""
        self.clk.t += 301
        self.c.record_attempt([FAST], Quality.UNCERTAIN, Reason.SHED)
        self.assertEqual(self.c.filter_stale([FAST], timedelta(seconds=30)), [FAST])


class TestInvalidateAllAndHardLimit(unittest.TestCase):
    def setUp(self):
        self.clk = _Clock()
        self.c = _cache(self.clk)
        self.c.update({FAST: _r(100), ENERGY: _r(5000.0)})

    def test_invalidate_all_is_not_a_miss(self):
        self.clk.t += 1000  # old value (e.g. SLOW tier, night)
        self.c.invalidate_all()
        e = self.c._store[FAST]
        self.assertEqual(e.missed, 0)
        self.assertEqual(e.uncertain_since, self.clk.t)
        self.assertEqual(self.c.quality_of(FAST)[:2], (Quality.UNCERTAIN, Reason.LINK_DOWN),
                         "old rule: BAD at once (age 1000 > 300); new: served until misses")

    def test_after_link_loss_two_failed_polls_expire(self):
        self.clk.t += 1000
        self.c.invalidate_all()
        self.clk.t += 300
        self.c.record_attempt([FAST], Quality.UNCERTAIN, Reason.TIMEOUT)
        self.assertEqual(self.c.quality_of(FAST)[0], Quality.UNCERTAIN)
        self.clk.t += 300
        self.c.record_attempt([FAST], Quality.UNCERTAIN, Reason.TIMEOUT)
        self.assertEqual(self.c.quality_of(FAST)[:2], (Quality.BAD, Reason.EXPIRED))

    def test_hard_limit_without_any_recorded_miss(self):
        """Defence in depth: a value degraded by a path that never records a
        miss still expires after 3 x ceiling of continuous uncertainty."""
        self.c.invalidate_all()
        self.clk.t += 900
        self.assertEqual(self.c.quality_of(FAST)[0], Quality.UNCERTAIN, "exactly 900 s: not yet")
        self.clk.t += 1
        self.assertEqual(self.c.quality_of(FAST)[:2], (Quality.BAD, Reason.EXPIRED))
        self.assertEqual(self.c.quality_of(ENERGY)[0], Quality.UNCERTAIN,
                         "energy hard limit is 3 x 600 = 1800 s")
        self.clk.t += 900
        self.assertEqual(self.c.quality_of(ENERGY)[:2], (Quality.BAD, Reason.EXPIRED))

    def test_hard_limit_with_one_miss(self):
        self.clk.t += 10
        self.c.record_attempt([FAST], Quality.UNCERTAIN, Reason.SHED)
        self.clk.t += 901
        self.assertEqual(self.c.quality_of(FAST)[:2], (Quality.BAD, Reason.EXPIRED))

    def test_hard_limit_measured_from_degradation_not_read_time(self):
        self.clk.t += 1500  # value GOOD but old (SLOW register at night)
        self.c.invalidate_all()
        self.assertEqual(self.c.quality_of(FAST)[0], Quality.UNCERTAIN)

    def test_uncertainty_clock_not_restarted_by_repeated_degradation(self):
        self.c.invalidate_all()
        start = self.clk.t
        self.clk.t += 100
        self.c.invalidate_all()
        self.c.record_attempt([FAST], Quality.UNCERTAIN, Reason.SHED)
        self.assertEqual(self.c._store[FAST].uncertain_since, start)

    def test_parameters_are_clamped_never_disabling_expiry(self):
        c = _cache(self.clk, min_missed_refreshes=0, uncertain_hard_limit_factor=0.1)
        self.assertEqual(c._min_missed_refreshes, 1)
        self.assertEqual(c._uncertain_hard_limit_factor, 1.0)


# ── HS-2302-004: a written register stays BAD until re-read ──────────────────

class TestWritePendingIsSticky(unittest.TestCase):
    """V2_ARCHITECTURE_DESIGN.md §6: after our own write the old value is
    KNOWN wrong and must not be shown. Found while reviewing HS-2302-001:
    record_attempt()/invalidate_all() used to overwrite BAD/WRITE_PENDING
    with UNCERTAIN, so one failed re-read brought the pre-write value back.
    The new miss rule would have widened that window for old (SLOW/config)
    registers, which the age-only rule had hidden by accident."""

    def setUp(self):
        self.clk = _Clock()
        self.c = _cache(self.clk)
        self.c.update({FAST: _r("pre-write")})
        self.clk.t += 1000        # an old config value (age > ceiling)
        self.c.invalidate(FAST)   # our own write

    def test_failed_reread_keeps_it_bad(self):
        for reason in (Reason.TIMEOUT, Reason.SHED, Reason.DEVICE_BUSY,
                       Reason.BACKOFF_DEFERRED):
            self.c.record_attempt([FAST], Quality.UNCERTAIN, reason)
            self.assertEqual(self.c.quality_of(FAST)[:2], (Quality.BAD, Reason.WRITE_PENDING))
            self.assertIsNone(self.c.get(FAST))
            self.assertNotIn(FAST, self.c.merge({}, [FAST]))
        self.assertEqual(self.c._store[FAST].missed, 4, "misses still counted")

    def test_link_loss_keeps_it_bad(self):
        self.c.invalidate_all()
        self.assertEqual(self.c.quality_of(FAST)[:2], (Quality.BAD, Reason.WRITE_PENDING))

    def test_still_due_for_reread(self):
        self.c.record_attempt([FAST], Quality.UNCERTAIN, Reason.TIMEOUT)
        self.assertEqual(self.c.filter_stale([FAST], timedelta(seconds=30)), [FAST])

    def test_successful_reread_clears_it(self):
        self.c.record_attempt([FAST], Quality.UNCERTAIN, Reason.TIMEOUT)
        self.c.update({FAST: _r("post-write")})
        self.assertEqual(self.c.quality_of(FAST)[0], Quality.GOOD)
        self.assertEqual(self.c.get(FAST).value, "post-write")
        self.assertEqual(self.c._store[FAST].missed, 0)


# ── HS-2302-002: effect on reads (bus) ───────────────────────────────────────

class TestNoFullRereadAfterRecovery(unittest.TestCase):
    """What the removed success-path invalidate_all() did to the NEXT poll."""

    NAMES = [f"reg_{i}" for i in range(10)]  # NORMAL tier by default

    def _scenario(self, old_behaviour: bool) -> list[str]:
        clk = _Clock()
        c = _cache(clk)
        c.update({n: _r(i) for i, n in enumerate(self.NAMES)})
        clk.t += 5
        failed = self.NAMES[:3]
        c.record_attempt(failed, Quality.UNCERTAIN, Reason.SHED)   # poll N fails
        clk.t += 5
        c.update({n: _r(99) for n in failed})                      # poll N+1 succeeds
        if old_behaviour:
            c.invalidate_all()                                     # removed in 2.3.0.2
        clk.t += 5
        return c.filter_stale(self.NAMES, timedelta(seconds=30))   # poll N+2

    def test_old_behaviour_forced_everything_to_be_reread(self):
        self.assertEqual(sorted(self._scenario(True)), sorted(self.NAMES))

    def test_new_behaviour_reads_only_what_is_due(self):
        self.assertEqual(self._scenario(False), [])


# ── Replay of the five field episodes ────────────────────────────────────────
#
# Offsets in seconds from the register's last good read (t = 0). Events:
#   ("shed", t)  refresh attempt for this register was shed (recorded miss)
#   ("hit",  t)  poll ran but this register was not due (nothing recorded)
#   ("ok",   t)  register read successfully
# Every event is a coordinator notification, i.e. a moment at which Home
# Assistant re-evaluates the entity. Sources (capture 2026-09-26/27, local
# time): bus record timestamps for reads; Home Assistant history for the
# moment each entity went `unknown`; missing request ids for shed polls.
# Shed polls leave no bus record -- where their exact time is not pinned by
# a history transition it is interpolated and marked (i); the results below
# do not depend on those values.

EPISODES = {
    # 02:07 INV1 active power: 02:02:19 read, poll 02:07:20 shed, 02:12:26 ok
    "night_0207_inv1_power": dict(
        name=FAST, observed_s=306,
        events=[("shed", 301), ("ok", 607)], expect_new_s=0),
    # 07:39 INV1 active power: 07:34:51 read, 07:39:52 + 07:44:52 shed, 07:49:53 ok
    "dawn_0739_inv1_power": dict(
        name=FAST, observed_s=602,
        events=[("shed", 301), ("shed", 601), ("ok", 902)], expect_new_s=301),
    # 07:44 INV1 total yield: 07:34:51 read; at the 07:39:5x shed poll it was
    # not yet due (NORMAL tier, night TTL 300 s, age ~299 s -- see
    # TestTimingSensitivity), 07:44:52 shed, 07:49:53 ok
    "dawn_0744_inv1_yield": dict(
        name=ENERGY, observed_s=302,
        events=[("hit", 299), ("shed", 601), ("ok", 902)], expect_new_s=0),
    # 08:00 INV1 active power: 07:55:06 read, 08:00:07 shed, 08:05:15 ok
    "morning_0800_inv1_power": dict(
        name=FAST, observed_s=308,
        events=[("shed", 301), ("ok", 609)], expect_new_s=0),
    # 12:32 INV2 active power: 12:26:07 read; 12:28:13 partial poll (its chunk
    # shed); two more shed polls (i); 12:32:30 shed; 12:35:01 ok
    "noon_1232_inv2_power": dict(
        name=FAST, observed_s=150,
        events=[("shed", 126), ("shed", 190), ("shed", 255), ("shed", 383), ("ok", 534)],
        expect_new_s=151),
}


def _replay(name: str, events: list, *, old: bool) -> float:
    """Total seconds the entity would have shown `unknown`."""
    clk = _Clock()
    c = _cache(clk)
    t0 = clk.t
    c.update({name: _r(1)})
    ceiling = 600.0 if RC.is_energy_counter(name) else 300.0
    unknown_since = None
    total = 0.0
    poll_failed_before = False
    for kind, t in events:
        clk.t = t0 + t
        if kind == "shed":
            c.record_attempt([name], Quality.UNCERTAIN, Reason.SHED, clk.t)
            poll_failed_before = True
        elif kind == "ok":
            c.update({name: _r(1)})
            if old and poll_failed_before:
                c.invalidate_all()  # the removed success-path call
            poll_failed_before = False
        if old:
            e = c._store[name]
            withheld = e.quality == Quality.BAD or (
                e.quality == Quality.UNCERTAIN and clk.t - e.ts > ceiling)
        else:
            withheld = name not in c.merge({}, [name])
        if withheld and unknown_since is None:
            unknown_since = clk.t
        elif not withheld and unknown_since is not None:
            total += clk.t - unknown_since
            unknown_since = None
    assert unknown_since is None, "episode must end with the value served"
    return total


class TestFieldEpisodeReplay(unittest.TestCase):
    def test_old_behaviour_reproduces_what_home_assistant_recorded(self):
        for key, ep in EPISODES.items():
            with self.subTest(episode=key):
                got = _replay(ep["name"], ep["events"], old=True)
                self.assertLessEqual(abs(got - ep["observed_s"]), 2,
                                     f"old model {got:.0f}s vs observed {ep['observed_s']}s")

    def test_new_behaviour(self):
        for key, ep in EPISODES.items():
            with self.subTest(episode=key):
                self.assertEqual(_replay(ep["name"], ep["events"], old=False), ep["expect_new_s"])

    def test_summary(self):
        before = sum(e["observed_s"] for e in EPISODES.values())
        after = sum(_replay(e["name"], e["events"], old=False) for e in EPISODES.values())
        eliminated = sum(1 for e in EPISODES.values()
                         if _replay(e["name"], e["events"], old=False) == 0)
        self.assertEqual(before, 1668)
        self.assertEqual(after, 452)
        self.assertEqual(eliminated, 3)


class TestTimingSensitivity(unittest.TestCase):
    """Honest boundary: the 07:44 yield episode is removed only because the
    register was not yet due at the 07:39 shed poll. Had it been due (and
    shed), it would be withheld exactly as before."""

    def test_if_due_at_first_shed_it_would_still_blank(self):
        got = _replay(ENERGY, [("shed", 301), ("shed", 601), ("ok", 902)], old=False)
        self.assertEqual(got, 301)

    def test_noon_case_needs_stage_2(self):
        """12:32 (daytime, four shed polls in a row) is unchanged: the fix is
        interpretation-only and cannot help when the bus sheds repeatedly."""
        ep = EPISODES["noon_1232_inv2_power"]
        self.assertEqual(_replay(ep["name"], ep["events"], old=False),
                         _replay(ep["name"], ep["events"], old=True))


# ── update_coordinator.py, structural ────────────────────────────────────────

_UC_SRC = (_ROOT / "update_coordinator.py").read_text()
_UC_TREE = ast.parse(_UC_SRC)


def _method(cls: str, meth: str) -> ast.AST:
    for node in _UC_TREE.body:
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == meth:
                    return item
    raise AssertionError(f"{cls}.{meth} not found")


def _calls(node: ast.AST, attr: str) -> list[ast.Call]:
    return [n for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == attr]


class TestCoordinatorStructure(unittest.TestCase):
    def test_success_path_no_longer_invalidates(self):
        body = _method("HuaweiSolarUpdateCoordinator", "_async_update_data")
        self.assertEqual(_calls(body, "invalidate_all"), [])

    def test_only_keepalive_link_loss_invalidates(self):
        sites = _calls(_UC_TREE, "invalidate_all")
        self.assertEqual(len(sites), 1)
        self.assertEqual(len(_calls(_method("HuaweiSolarUpdateCoordinator",
                                            "on_connection_lost"), "invalidate_all")), 1)

    def test_recovery_still_logged_and_counters_reset(self):
        body = ast.unparse(_method("HuaweiSolarUpdateCoordinator", "_async_update_data"))
        self.assertIn("communication restored", body)
        for name in ("self._consecutive_timeouts = 0", "self._consecutive_failures = 0",
                     "self._backoff_cycle = 0"):
            self.assertIn(name, body)

    def test_cache_constructed_with_new_parameters(self):
        init = _method("HuaweiSolarUpdateCoordinator", "__init__")
        ctor = [n for n in ast.walk(init) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name) and n.func.id == "RegisterCache"]
        self.assertEqual(len(ctor), 1)
        kws = {k.arg: ast.unparse(k.value) for k in ctor[0].keywords}
        self.assertEqual(kws.get("min_missed_refreshes"), "MIN_MISSED_REFRESHES_BEFORE_EXPIRY")
        self.assertEqual(kws.get("uncertain_hard_limit_factor"), "UNCERTAIN_HARD_LIMIT_FACTOR")

    def test_no_bus_side_change(self):
        """Bus-side mechanisms named in the plan are untouched: busy retry
        still triggers the transition, shed accounting unchanged."""
        self.assertIn('notify_transition("0x06 SLAVE_DEVICE_BUSY")', _UC_SRC)
        shed = ast.unparse(_method("HuaweiSolarUpdateCoordinator", "_record_shed"))
        self.assertIn("self._consecutive_timeouts += 1", shed)


# ── HS-2302-003: strings ──────────────────────────────────────────────────────

def _walk(o, p=()):
    if isinstance(o, dict):
        for k, v in o.items():
            yield from _walk(v, p + (k,))
    elif isinstance(o, str):
        yield p, o


_PH = re.compile(r"\{(\w+)\}")


class TestStrings(unittest.TestCase):
    def test_sync_power_option_has_a_label(self):
        for f in ("strings.json", "translations/en.json"):
            with self.subTest(file=f):
                d = json.loads((_ROOT / f).read_text(encoding="utf-8"))
                label = d["options"]["step"]["init"]["data"].get("sync_power_dedicated_reads")
                self.assertTrue(label)

    def test_every_option_field_has_a_label(self):
        flow = (_ROOT / "config_flow.py").read_text()
        d = json.loads((_ROOT / "strings.json").read_text(encoding="utf-8"))
        labels = d["options"]["step"]["init"]["data"]
        for const_name in ("CONF_SYNC_POWER_DEDICATED_READS", "CONF_WRITE_PERMISSION_PROBE",
                           "CONF_SLOW_TIER_TTL_S", "CONF_BH_ENABLED"):
            self.assertIn(const_name, flow)
            self.assertIn(getattr(CONST, const_name), labels)

    def test_no_translation_drops_or_invents_placeholders(self):
        en = dict(_walk(json.loads((_ROOT / "translations/en.json").read_text(encoding="utf-8"))))
        for f in sorted((_ROOT / "translations").glob("*.json")):
            with self.subTest(file=f.name):
                t = dict(_walk(json.loads(f.read_text(encoding="utf-8"))))
                bad = [".".join(k) for k, v in t.items()
                       if k in en and set(_PH.findall(v)) != set(_PH.findall(en[k]))]
                self.assertEqual(bad, [])

    def test_strings_and_en_agree_on_placeholders(self):
        st = dict(_walk(json.loads((_ROOT / "strings.json").read_text(encoding="utf-8"))))
        en = dict(_walk(json.loads((_ROOT / "translations/en.json").read_text(encoding="utf-8"))))
        bad = [k for k in st if k in en and set(_PH.findall(st[k])) != set(_PH.findall(en[k]))]
        self.assertEqual(bad, [])


class TestVersion(unittest.TestCase):
    def test_manifest_version(self):
        manifest = json.loads((_ROOT / "manifest.json").read_text())
        # v2.3.2.0: moved from "2.3.1.0" (was "2.3.0.2") -- pins the CURRENT release.
        self.assertEqual(manifest["version"], "2.3.2.0")


if __name__ == "__main__":
    unittest.main()
