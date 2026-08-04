"""Address-aware Modbus chunking, SLOW-tier TTL, and cache tests (v1.3.5).

HISTORY — this file replaces ``test_tier_separation.py``.

v1.3.3 found that requests touching SLOW/STATIC-tier registers cost far more
than FAST/NORMAL ones, and chunked by tier accordingly. v1.3.4 pushed that
further: coalesce a coordinator's whole SLOW/STATIC cohort into one request.
Enabled in the field, coalescing caused every battery entity to go
unavailable within hours — see AUDIT_1.3.5.md for the incident record.

A 29,000-request capture taken during recovery found the TIER correlation was
a CONFOUND, not the cause. The real driver, confirmed against the actual
Huawei register address map (huawei_solar 3.0.5):

    huawei_solar.device.base.batch_update() groups the registers it is given
    into physical Modbus exchanges using MAX_BATCHED_REGISTERS_GAP=16 and
    MAX_BATCHED_REGISTERS_COUNT=64 (address gap / span). A register set that
    fits in ONE physical exchange costs ~7-60 ms REGARDLESS OF TIER. A set
    forced into two or more costs roughly one further ~2,900-3,000 ms fixed
    toll per exchange — again regardless of tier.

    A representative main-inverter register set (input_power ..
    internal_temperature, real addresses 32064-32087) forms a single
    9-register contiguous block, followed by a register 18 addresses further
    on (accumulated_yield_energy) -- corroborating, not exactly reproducing,
    the field's own directly-measured regs=7-vs-8 threshold (the two differ
    by one register because the exact real coordinator entity list is not
    statically enumerable; see the module docstring in update_coordinator.py).

v1.3.5 retires coalescing and night-deferral outright (not merely disables
them — see const.py) and replaces tier-based chunking with _address_group(),
which reproduces the vendor library's OWN grouping rule in our own code, so
each group can be issued as a separately-paced request instead of being
split invisibly (and unpaced) inside the library.

SLOW-tier TTL (300 -> 900 s, v1.3.3) is KEPT: it is a caching decision (how
often slow-changing data needs refreshing at all), independent of the
per-request cost model that has now been corrected.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
import unittest
from datetime import timedelta

_ROOT = pathlib.Path(__file__).parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(f"tsep_{name}", str(_ROOT / f"{name}.py"))
    m = importlib.util.module_from_spec(spec)
    m.__package__ = "tsep"
    sys.modules[f"tsep_{name}"] = m
    spec.loader.exec_module(m)
    return m


if "tsep" not in sys.modules:
    p = types.ModuleType("tsep")
    p.__path__ = []
    sys.modules["tsep"] = p

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
    hs = types.ModuleType("huawei_solar")
    hs.__path__ = []
    sys.modules["huawei_solar"] = hs
if not hasattr(hs, "RegisterName"):
    class RegisterName(str):
        pass
    hs.RegisterName = RegisterName
if not hasattr(hs, "Result"):
    hs.Result = type("Result", (), {})

RC = _load("register_cache")
Tier = RC.RegisterTier


# ── Pure-function extraction of the address-grouping algorithm ──────────────
#
# update_coordinator.py pulls in Home Assistant's DataUpdateCoordinator and a
# large surface of the huawei_solar library, which is heavy and fragile to
# stub in isolation. _modbus_span/_address_group are pure functions with no
# such dependency (the library import inside _modbus_span is lazy and
# exception-guarded), so they are extracted by source slice and exec'd in a
# minimal namespace — the same technique test_module_imports.py's structural
# checks are built on, applied here to get fast, dependency-free execution
# rather than just AST inspection.
def _extract_address_functions():
    src = (_ROOT / "update_coordinator.py").read_text()
    modspan_start = src.index("@lru_cache(maxsize=512)\ndef _modbus_span")
    modspan_end = src.index("def _modbus_address(name")
    modspan_src = src[modspan_start:modspan_end]
    group_start = src.index("_ADDRESS_GROUP_MAX_GAP = 16")
    group_end = src.index("class HuaweiSolarUpdateCoordinator")
    group_src = src[group_start:group_end]

    ns: dict = {
        "RegisterName": hs.RegisterName,
    }
    header = "from functools import lru_cache\n"
    exec(header + modspan_src + group_src, ns)
    return ns


_NS = _extract_address_functions()
_modbus_span = _NS["_modbus_span"]
_address_group = _NS["_address_group"]
MAX_GAP = _NS["_ADDRESS_GROUP_MAX_GAP"]
MAX_SPAN = _NS["_ADDRESS_GROUP_MAX_SPAN"]


def _fake_table(monkeypatch_ns, table: dict[str, tuple[int, int]]):
    """Point _modbus_span at a synthetic (name -> (start, end)) table.

    Deterministic and independent of any installed library version or of
    another test module's `huawei_solar` stub — the exact fragility that has
    caused cross-test collisions in this suite before.
    """
    def fake_span(name):
        return table.get(str(name), (0, 0))
    monkeypatch_ns["_modbus_span"] = fake_span


class TestAddressGroupAlgorithm(unittest.TestCase):
    """The grouping rule itself, against a synthetic address table.

    Mirrors huawei_solar.device.base.batch_update()'s own rule EXACTLY
    (gap < 16, span <= 64), verified against the real library's constants in
    TestRealRegisterMap below.
    """

    def _group(self, table: dict[str, tuple[int, int]], names: list[str]):
        saved = _NS["_modbus_span"]
        try:
            _fake_table(_NS, table)
            return _NS["_address_group"]([hs.RegisterName(n) for n in names])
        finally:
            _NS["_modbus_span"] = saved

    def test_empty_input(self):
        self.assertEqual(self._group({}, []), [])

    def test_single_register_is_one_group(self):
        table = {"a": (100, 100)}
        groups = self._group(table, ["a"])
        self.assertEqual(len(groups), 1)

    def test_tightly_packed_registers_form_one_group(self):
        """The exact shape of the real input_power..internal_temperature
        block: contiguous, 8 registers, span 24 -- must be ONE group."""
        table = {chr(97 + i): (100 + i, 100 + i) for i in range(8)}
        groups = self._group(table, list(table))
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 8)

    def test_gap_at_the_boundary_splits(self):
        """gap == MAX_GAP must still split (the rule is gap < 16, not <=)."""
        table = {"a": (100, 100), "b": (100 + 1 + MAX_GAP, 100 + 1 + MAX_GAP)}
        groups = self._group(table, ["a", "b"])
        self.assertEqual(len(groups), 2)

    def test_gap_just_under_boundary_stays_together(self):
        table = {"a": (100, 100), "b": (100 + MAX_GAP, 100 + MAX_GAP)}
        groups = self._group(table, ["a", "b"])
        self.assertEqual(len(groups), 1)

    def test_span_over_limit_splits_even_with_no_gap(self):
        """A dense run of registers wider than MAX_SPAN must still split,
        even though every individual gap is 0 (the real library enforces
        BOTH constraints independently)."""
        table = {str(i): (100 + i, 100 + i) for i in range(MAX_SPAN + 5)}
        groups = self._group(table, list(table))
        self.assertGreaterEqual(len(groups), 2)
        total = sum(len(g) for g in groups)
        self.assertEqual(total, MAX_SPAN + 5)   # no register lost

    def test_reproduces_the_field_incident_shape(self):
        """The exact address layout that caused the outage: a coordinator's
        SLOW/STATIC cohort (v1.3.4 coalescing) scattered across many
        unrelated functional blocks, each far from the others."""
        table = {
            "alarm_1": (32008, 32008),
            "state_1": (32000, 32000),   # 8 from alarm_1: SAME group as it
            "daily_yield": (32114, 32115),   # 105 from alarm_1: new group
            "device_status": (35000, 35001),  # far from all: new group
            "counter_a": (40000, 40001),      # far from all: new group
        }
        groups = self._group(table, list(table))
        # state_1/alarm_1 are close enough (8 apart) to share a group; the
        # other three are each far enough to force their own — FOUR groups
        # for what coalescing would have crammed into ONE. That is precisely
        # what caused the outage: 4 exchanges worth of fixed toll (~12 s)
        # instead of one ~7-60 ms exchange for whichever subset was actually
        # tightly packed.
        self.assertEqual(len(groups), 4)

    def test_no_register_is_ever_lost_or_duplicated(self):
        table = {f"r{i}": (100 + i * 20, 100 + i * 20) for i in range(30)}
        names = list(table)
        groups = self._group(table, names)
        flat = [str(n) for g in groups for n in g]
        self.assertEqual(sorted(flat), sorted(names))
        self.assertEqual(len(flat), len(names))


class TestRealRegisterMap(unittest.TestCase):
    """Validates _address_group against the ACTUAL huawei_solar 3.0.5 register
    table — the direct evidentiary link between the incident and the fix.

    Every other test file in this suite installs its OWN incomplete
    `huawei_solar` stub into sys.modules, and this file (sorting late
    alphabetically) is collected after most of them — so a fake is very
    likely already cached under that name by the time this class runs.
    setUpClass therefore force-purges every ``huawei_solar*`` entry, imports
    the GENUINE site-packages install fresh, and tearDownClass restores
    exactly what was there before, so no other test file is affected.

    Skipped (not failed) if the real package cannot be found at all: it is a
    runtime dependency of the integration, not of the test suite.
    """

    _saved_modules: dict = {}

    @classmethod
    def setUpClass(cls):
        import importlib
        cls._saved_modules = {}
        for name in list(sys.modules):
            if name == "huawei_solar" or name.startswith("huawei_solar."):
                cls._saved_modules[name] = sys.modules.pop(name)
        try:
            importlib.invalidate_caches()
            from huawei_solar.registers import REGISTERS
            import huawei_solar.register_names as rn
        except ImportError:
            cls._restore()
            raise unittest.SkipTest(
                "huawei_solar library not installed in this environment; "
                "this test validates against the real vendor register map "
                "when available (pip install huawei-solar==3.0.5)"
            )
        cls.REGISTERS = REGISTERS
        cls.rn = rn

    @classmethod
    def tearDownClass(cls):
        cls._restore()

    @classmethod
    def _restore(cls):
        for name in list(sys.modules):
            if name == "huawei_solar" or name.startswith("huawei_solar."):
                del sys.modules[name]
        for name, mod in cls._saved_modules.items():
            sys.modules[name] = mod
        cls._saved_modules = {}

    def _real_names(self, candidates):
        out = []
        for name in candidates:
            try:
                self.REGISTERS[self.rn.RegisterName(name)]
                out.append(self.rn.RegisterName(name))
            except KeyError:
                pass
        return out

    def test_main_inverter_block_matches_field_evidence(self):
        """A representative register set validated during the incident
        analysis: a large contiguous power/temperature block, PV strings,
        and a scattered yield pair -- corroborates the field's directly
        measured regs=7-vs-8 threshold (see module docstring for why this
        set's block is 9 wide rather than exactly 8: it is an approximation
        of the real coordinator's entity list, not a static enumeration)."""
        candidates = [
            "input_power", "active_power", "day_active_power_peak",
            "efficiency", "internal_temperature", "daily_yield_energy",
            "accumulated_yield_energy", "pv_01_voltage", "pv_01_current",
            "pv_02_voltage", "pv_02_current", "grid_voltage",
            "reactive_power", "power_factor", "grid_frequency",
        ]
        names = self._real_names(candidates)
        names.sort(key=lambda n: _modbus_span(n)[0])
        groups = _address_group(names)
        sizes = sorted(len(g) for g in groups)
        # PV pair (4), the wide contiguous power/temp block, and the
        # yield-energy pair -- three groups, matching the real address map.
        self.assertEqual(len(groups), 3)
        self.assertEqual(sizes, [2, 4, 9])
        self.assertGreaterEqual(
            max(sizes), 8,
            "the large contiguous power/temperature block must stay whole "
            "and must be at or above the field's measured regs=8 threshold",
        )

    def test_rated_power_is_far_enough_to_split(self):
        """rated_power sits ~1,925 registers from the main block in the real
        map -- must never be grouped with it."""
        names = self._real_names(["rated_power", "input_power", "active_power"])
        names.sort(key=lambda n: _modbus_span(n)[0])
        groups = _address_group(names)
        self.assertEqual(len(groups), 2)

    def test_modbus_span_resolves_real_addresses(self):
        start, end = _modbus_span(self.rn.RegisterName("active_power"))
        self.assertEqual((start, end), (32080, 32081))


class TestModbusSpanRobustness(unittest.TestCase):
    """_modbus_span must never raise, with or without the real library."""

    def test_unknown_register_degrades_to_zero_width(self):
        result = _modbus_span(hs.RegisterName("definitely_not_a_real_register"))
        self.assertEqual(result, (0, 0))

    def test_never_raises_on_garbage_input(self):
        for bad in (hs.RegisterName(""), hs.RegisterName("🔥"), hs.RegisterName("a" * 500)):
            with self.subTest(value=bad):
                self.assertEqual(_modbus_span(bad), (0, 0))


class TestSlowTierTTL(unittest.TestCase):
    """(v1.3.3, kept) Slow-changing data needs refreshing less often.

    This is a CACHING decision, independent of the v1.3.5 per-request cost
    model correction: how often we bother reading slow-changing registers at
    all is a separate question from how expensive any one read is.
    """

    def test_slow_ttl_raised_from_300(self):
        self.assertGreaterEqual(RC._TIER_BASE_TTL[Tier.SLOW], 900.0)

    def test_fast_and_normal_unchanged(self):
        self.assertEqual(RC._TIER_BASE_TTL[Tier.FAST], 0.0)
        self.assertEqual(RC._TIER_BASE_TTL[Tier.NORMAL], 30.0)

    def test_ttl_override_is_clamped(self):
        original = RC._TIER_BASE_TTL[Tier.SLOW]
        try:
            RC.set_slow_tier_ttl(10)
            self.assertGreaterEqual(RC._TIER_BASE_TTL[Tier.SLOW], 300.0)
            RC.set_slow_tier_ttl(999999)
            self.assertLessEqual(RC._TIER_BASE_TTL[Tier.SLOW], 3600.0)
            RC.set_slow_tier_ttl(1200)
            self.assertEqual(RC._TIER_BASE_TTL[Tier.SLOW], 1200.0)
        finally:
            RC._TIER_BASE_TTL[Tier.SLOW] = original


class TestCoalescingAndNightDeferralAreGone(unittest.TestCase):
    """v1.3.5 retires these outright — pinned so they cannot silently return.

    Coalescing caused a real outage. Re-adding it (or night-deferral, which
    shared its now-disproven rationale) must be a deliberate, documented
    decision with fresh evidence, not an accidental reintroduction via a
    merge or a copy-pasted option block.
    """

    def test_register_cache_has_no_coalescing_state_or_methods(self):
        cache = RC.RegisterCache()
        for attr in ("_coalesce_slow_tier", "coalesced_registers",
                     "coalesce_events", "set_coalesce_slow_tier",
                     "coalescing_stats"):
            self.assertFalse(
                hasattr(cache, attr),
                f"RegisterCache.{attr} should not exist — coalescing was "
                f"retired in v1.3.5 after causing a production outage",
            )

    def test_register_cache_has_no_night_deferral_state_or_methods(self):
        cache = RC.RegisterCache()
        for attr in ("_prefer_night_for_slow", "deferred_expensive",
                     "set_prefer_night_for_slow"):
            self.assertFalse(hasattr(cache, attr))

    def test_filter_stale_ignores_extra_kwargs_gracefully(self):
        """Sanity: the simplified filter_stale signature still works."""
        cache = RC.RegisterCache()
        stale = cache.filter_stale(
            [hs.RegisterName("some_register")], timedelta(seconds=30)
        )
        self.assertEqual(stale, [hs.RegisterName("some_register")])

    def test_const_no_longer_defines_removed_options(self):
        const_src = (_ROOT / "const.py").read_text()
        for token in ("CONF_COALESCE_SLOW_TIER", "CONF_PREFER_NIGHT_FOR_SLOW",
                      "DEFAULT_COALESCE_SLOW_TIER", "DEFAULT_PREFER_NIGHT_FOR_SLOW"):
            self.assertNotIn(token, const_src)

    def test_update_coordinator_no_longer_calls_split_by_cost(self):
        src = (_ROOT / "update_coordinator.py").read_text()
        self.assertNotIn("_split_by_cost", src)
        self.assertIn("_address_group", src)


if __name__ == "__main__":
    unittest.main()
