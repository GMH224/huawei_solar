"""Tests for v2.3.1.0 (bus-side fixes, "stage 2").

HS-2310-001a  Slow-region isolation: no physical read spans a known slow
              address region together with registers outside it.
HS-2310-001b  Cadence floors: SOH-calibration status at most hourly,
              configuration coordinator registers at most every 30 min.
HS-2310-002   Single retry layer: the vendor transport sends every request
              once (retry only after a lost connection); 20 s per attempt;
              outer bounds longer than one attempt.
HS-2310-003   Power-flow chunks on the guard's priority lane; the busy
              transition starts only on a PERSISTENT busy, for 5 min.
HS-2310-004   Dawn peer wake: one device waking on its own evidence wakes
              the other coordinators on the same bus (with a hold).
HS-2310-005   External audit F-04 (reconfigure restore), F-05 (awaited
              keep-alive stop), F-06 (TModbusError per chunk).

Approach: every pure piece runs for real -- against the REAL installed
huawei-solar register map and the REAL tmodbus/tenacity transport objects
where the change touches them. update_coordinator.py / config_flow.py /
adaptive_modbus.py need a live Home Assistant to import, so their pure
functions and methods are extracted from the real source and executed; the
wiring is checked structurally (ast).

Run standalone:  cd tests && python3 -m pytest test_ics_2310_fixes.py
"""

from __future__ import annotations

import ast
import asyncio
from datetime import timedelta
import importlib
import json
import logging
import pathlib
import random
import sys
import time as _time
import types
import unittest
import weakref

_ROOT = pathlib.Path(__file__).parent.parent


# ── real huawei_solar (see test_tier_separation.py for why this dance) ───────
def _ensure_real_huawei_solar():
    """Make the REAL huawei_solar and tmodbus packages importable.

    In a single-process run other files have replaced parts of both with
    stubs (modules without a __file__). Stubs are dropped and the real
    packages imported; real modules already loaded are reused (the real
    huawei_solar package must never be imported twice in one process). If
    that is impossible, the module is skipped with an explicit reason --
    the same convention as test_tier_separation.py -- and runs in full when
    this file is run on its own (the per-file regression mode).
    """
    cached = sys.modules.get("huawei_solar")
    if cached is not None and getattr(cached, "__file__", None) is not None:
        return
    for name in list(sys.modules):
        if name.split(".")[0] in ("huawei_solar", "tmodbus"):
            if getattr(sys.modules[name], "__file__", None) is None:
                sys.modules.pop(name)
    importlib.invalidate_caches()
    try:
        importlib.import_module("tmodbus.exceptions")
        importlib.import_module("huawei_solar")
    except Exception as err:  # noqa: BLE001
        raise unittest.SkipTest(
            "real huawei_solar/tmodbus not importable in this process "
            f"({type(err).__name__}: {err}); run this file on its own"
        ) from err


# Everything that needs the REAL library is loaded in setUpModule(), not at
# collection time: other files in this suite install lightweight
# huawei_solar stubs when they are COLLECTED, and the real package cannot be
# imported twice in one process (tmodbus PDU registry). Loading at test time
# means a single-process `pytest .` sees the same stubs during collection as
# before, and the real-library tests that run later (test_tier_separation,
# test_modbus_keepalive_registername) find the real package already loaded
# and reuse it instead of re-importing it.
REGISTERS = create_tcp_client = None
ModbusConnectionError = ServerDeviceBusyError = TModbusError = None
CONST = BUS_POLICY = RC = NIGHT = GUARD = KEEPALIVE = None
UC: dict = {}
_address_group = None
SLOW: tuple = ()
FakeCoord = None
_Busy = None
_PKG = "hs2310pkg"


def setUpModule():  # noqa: N802 -- unittest hook name
    global REGISTERS, create_tcp_client, ModbusConnectionError, ServerDeviceBusyError
    global TModbusError, CONST, BUS_POLICY, RC, NIGHT, GUARD, KEEPALIVE, UC
    global _address_group, SLOW, FakeCoord, _Busy
    _ensure_real_huawei_solar()
    from huawei_solar.registers import REGISTERS as _regs
    from huawei_solar.modbus_client import create_tcp_client as _ctc
    import tmodbus.exceptions as _tx
    REGISTERS, create_tcp_client = _regs, _ctc
    ModbusConnectionError = _tx.ModbusConnectionError
    ServerDeviceBusyError = _tx.ServerDeviceBusyError
    TModbusError = _tx.TModbusError

    for name in [n for n in sys.modules if n == _PKG or n.startswith(_PKG + ".")]:
        sys.modules.pop(name)
    pkg = types.ModuleType(_PKG)
    pkg.__path__ = [str(_ROOT)]  # type: ignore[attr-defined]
    sys.modules[_PKG] = pkg
    CONST = importlib.import_module(f"{_PKG}.const")
    BUS_POLICY = importlib.import_module(f"{_PKG}.bus_policy")
    RC = importlib.import_module(f"{_PKG}.register_cache")
    NIGHT = importlib.import_module(f"{_PKG}.night_mode")
    GUARD = importlib.import_module(f"{_PKG}.modbus_guard")
    KEEPALIVE = importlib.import_module(f"{_PKG}.modbus_keepalive")

    UC = _extract_uc_address_functions()
    _address_group = UC["_address_group"]
    SLOW = UC["_SLOW_ADDRESS_RANGES"]

    class _BusyErr(ServerDeviceBusyError):
        def __init__(self):  # noqa: D107 -- construct without a PDU
            Exception.__init__(self, "busy")
    _Busy = _BusyErr
    FakeCoord = _build_fake_coordinator_class()

_UC_SRC = (_ROOT / "update_coordinator.py").read_text()
_UC_TREE = ast.parse(_UC_SRC)
_INIT_SRC = (_ROOT / "__init__.py").read_text()
_FLOW_SRC = (_ROOT / "config_flow.py").read_text()
_ADAPT_SRC = (_ROOT / "adaptive_modbus.py").read_text()


def _extract_uc_address_functions() -> dict:
    """The real module-level grouping/slow-region/priority helpers of
    update_coordinator.py, exec'd against the real register map."""
    start = _UC_SRC.index("@lru_cache(maxsize=512)\ndef _modbus_span")
    end = _UC_SRC.index("def _modbus_address(name")
    group_start = _UC_SRC.index("_ADDRESS_GROUP_MAX_GAP = 16")
    group_end = _UC_SRC.index("class HuaweiSolarUpdateCoordinator")
    ns: dict = {
        "RegisterName": str,
        "SOH_CALIBRATION_MIN_TTL": CONST.SOH_CALIBRATION_MIN_TTL,
        "weakref": weakref,
    }
    exec(  # noqa: S102 -- executing this repository's own source
        "from __future__ import annotations\nfrom functools import lru_cache\n"
        + _UC_SRC[start:end] + _UC_SRC[group_start:group_end],
        ns,
    )
    # Bind the span lookup to the real register map captured here (the
    # production version imports it lazily, which in a single-process run
    # could meet another file's stub later on).
    regs = REGISTERS

    def _span_real(name):
        try:
            r = regs[name]
            return r.register, r.register + r.length - 1
        except Exception:  # noqa: BLE001
            return 0, 0
    ns["_modbus_span"] = _span_real
    return ns




def _span(name):
    r = REGISTERS[name]
    return r.register, r.register + r.length - 1


def _sorted(names):
    return sorted(names, key=lambda n: _span(n)[0])


def _read_range(group):
    lo = min(_span(n)[0] for n in group)
    hi = max(_span(n)[1] for n in group)
    return lo, hi


def _touches(lo, hi, ranges):
    return any(a <= hi and b >= lo for a, b in ranges)


def _legacy_group(names):
    """Independent re-implementation of the pre-2.3.1.0 rule (gap<16,
    span<=64), used to prove the option-off path is unchanged."""
    groups, cur = [], [names[0]]
    gs, ce = _span(names[0])
    for n in names[1:]:
        s, e = _span(n)
        if s - ce - 1 < 16 and e - gs <= 64:
            cur.append(n)
            ce = max(ce, e)
        else:
            groups.append(cur)
            cur, gs, ce = [n], s, e
    groups.append(cur)
    return groups


PACK = "storage_unit_1_battery_pack_{p}_{s}"
PACK_BATTERY_SET = [
    PACK.format(p=p, s=s)
    for p in (1, 2, 3)
    for s in ("working_status", "state_of_capacity", "charge_discharge_power",
              "voltage", "current", "total_charge", "total_discharge")
]
MAIN_SET = ["input_power", "active_power", "efficiency", "internal_temperature",
            "day_active_power_peak", "device_status", "startup_time", "shutdown_time"]


# ═════════════════════════════════════════════════════════════════════════════
# HS-2310-001a  slow-region isolation
# ═════════════════════════════════════════════════════════════════════════════

class TestSlowRegions(unittest.TestCase):
    def test_fixed_regions_present(self):
        for r in ((32000, 32015), (32088, 32105), (37920, 37926)):
            self.assertIn(r, SLOW)

    def test_pack_gaps_derived_from_real_register_map(self):
        self.assertIn((38230, 38232), SLOW)  # measured in the field
        self.assertIn((38272, 38274), SLOW)
        self.assertIn((38314, 38316), SLOW)

    def test_pack_gaps_contain_no_register_at_all(self):
        """Nothing we could ever need lives in a pack gap -- avoiding it
        costs nothing."""
        used = {a for r in REGISTERS.values() for a in range(r.register, r.register + r.length)}
        for lo, hi in SLOW:
            if lo >= 38200:
                self.assertFalse(any(a in used for a in range(lo, hi + 1)), (lo, hi))

    def test_pack_gap_sits_between_soc_and_charge_discharge_power(self):
        for p in (1, 2, 3):
            soc_end = _span(PACK.format(p=p, s="state_of_capacity"))[1]
            cdp = _span(PACK.format(p=p, s="charge_discharge_power"))[0]
            self.assertIn((soc_end + 1, cdp - 1), SLOW)

    def test_regions_sorted_and_disjoint(self):
        self.assertEqual(list(SLOW), sorted(SLOW))
        for (a1, b1), (a2, b2) in zip(SLOW, SLOW[1:]):
            self.assertLess(b1, a2)


class TestIsolationGrouping(unittest.TestCase):
    def _groups(self, names, isolate):
        return _address_group(_sorted(names), SLOW if isolate else ())

    def test_old_grouping_spans_the_pack_gap(self):
        """Adversarial control: the pre-2.3.1.0 grouping DOES read the gap
        (the ~2 s reads in both captures)."""
        spanning = [g for g in self._groups(PACK_BATTERY_SET, False)
                    if _touches(*_read_range(g), [r for r in SLOW if r[0] >= 38200])]
        self.assertEqual(len(spanning), 3)

    def test_isolated_grouping_never_spans_a_pack_gap(self):
        for g in self._groups(PACK_BATTERY_SET, True):
            self.assertFalse(_touches(*_read_range(g), [r for r in SLOW if r[0] >= 38200]), g)

    def test_soc_and_status_still_read_just_separately(self):
        groups = self._groups(PACK_BATTERY_SET, True)
        flat = [n for g in groups for n in g]
        self.assertEqual(sorted(flat), sorted(PACK_BATTERY_SET))
        for p in (1, 2, 3):
            g = next(g for g in groups if PACK.format(p=p, s="state_of_capacity") in g)
            self.assertIn(PACK.format(p=p, s="working_status"), g)
            self.assertNotIn(PACK.format(p=p, s="charge_discharge_power"), g)

    def test_power_block_separated_from_status_block(self):
        groups = self._groups(MAIN_SET, True)
        power = next(g for g in groups if "input_power" in g)
        self.assertIn("active_power", power)
        self.assertFalse(_touches(*_read_range(power), [(32088, 32105)]))
        status = next(g for g in groups if "device_status" in g)
        self.assertNotIn("input_power", status)
        # control: the old rule merges them (the 2.4 s power reads)
        old = self._groups(MAIN_SET, False)
        self.assertTrue(any("input_power" in g and "device_status" in g for g in old))

    def test_state_alarm_block_isolated_from_pv_values(self):
        names = ["state_1", "state_2", "state_3", "alarm_1", "alarm_2", "alarm_3",
                 "pv_01_voltage", "pv_01_current"]
        groups = self._groups(names, True)
        pv = next(g for g in groups if "pv_01_voltage" in g)
        self.assertFalse(any(n.startswith(("state_", "alarm_")) for n in pv))

    def test_option_off_is_exactly_the_legacy_rule(self):
        rng = random.Random(2310)
        pool = [n for n in REGISTERS if 30000 <= REGISTERS[n].register <= 48100]
        for _ in range(300):
            names = _sorted(rng.sample(pool, rng.randint(1, 40)))
            self.assertEqual(_address_group(names, ()), _legacy_group(names))

    def test_isolation_never_loses_or_duplicates_and_keeps_limits(self):
        rng = random.Random(1)
        pool = [n for n in REGISTERS if 30000 <= REGISTERS[n].register <= 48100]
        for _ in range(300):
            names = _sorted(rng.sample(pool, rng.randint(1, 40)))
            groups = _address_group(names, SLOW)
            self.assertEqual([n for g in groups for n in g], names)
            for g in groups:
                lo, hi = _read_range(g)
                self.assertLessEqual(_span(g[-1])[1] - _span(g[0])[0], 64)
                zones = {UC["_slow_zone"](_span(n)[0], SLOW) for n in g}
                self.assertEqual(len(zones), 1, g)
                if zones == {None}:
                    for a, b in zip(g, g[1:]):
                        self.assertFalse(
                            UC["_gap_touches_slow_region"](_span(a)[1] + 1, _span(b)[0] - 1, SLOW), g)

    def test_isolation_only_ever_splits_never_merges(self):
        """Every isolated group is a sub-list of one legacy group."""
        rng = random.Random(7)
        pool = [n for n in REGISTERS if 30000 <= REGISTERS[n].register <= 48100]
        for _ in range(200):
            names = _sorted(rng.sample(pool, rng.randint(1, 40)))
            legacy = _legacy_group(names)
            for g in _address_group(names, SLOW):
                self.assertTrue(any(set(g) <= set(lg) for lg in legacy))


class TestFieldCostModel(unittest.TestCase):
    """Deterministic consequence for the plant's own battery register set,
    with the costs measured in the two captures (AUDIT §2): a read touching
    a pack gap ~2.1 s p50, otherwise ~5 ms."""

    SLOW_MS, FAST_MS = 2100.0, 5.0

    def _cost(self, groups):
        pack_gaps = [r for r in SLOW if r[0] >= 38200]
        return sum(self.SLOW_MS if _touches(*_read_range(g), pack_gaps) else self.FAST_MS
                   for g in groups)

    def test_one_full_pack_round_cost(self):
        old = self._cost(_address_group(_sorted(PACK_BATTERY_SET), ()))
        new = self._cost(_address_group(_sorted(PACK_BATTERY_SET), SLOW))
        self.assertGreaterEqual(old, 3 * self.SLOW_MS)
        self.assertLess(new, 100.0)


# ═════════════════════════════════════════════════════════════════════════════
# HS-2310-003  priority eligibility
# ═════════════════════════════════════════════════════════════════════════════

class TestPowerFlowPriority(unittest.TestCase):
    def f(self, chunk):
        return UC["_is_power_flow_chunk"](chunk)

    def test_power_chunks(self):
        self.assertTrue(self.f(["input_power", "active_power", "efficiency"]))
        self.assertTrue(self.f(["power_meter_active_power"]))
        self.assertTrue(self.f(["storage_charge_discharge_power", "storage_total_discharge"]))

    def test_not_power(self):
        self.assertFalse(self.f(["efficiency", "internal_temperature"]))
        self.assertFalse(self.f([PACK.format(p=1, s="charge_discharge_power")]),
                         "pack power must stay throttleable")

    def test_slow_read_never_priority(self):
        self.assertFalse(self.f(["input_power", "active_power", "device_status"]))
        self.assertFalse(self.f(["active_power", "state_1"]))

    def test_register_set_is_exact(self):
        self.assertEqual(UC["_POWER_FLOW_REGISTERS"], frozenset({
            "input_power", "active_power", "power_meter_active_power",
            "storage_charge_discharge_power"}))
        for n in UC["_POWER_FLOW_REGISTERS"]:
            self.assertIn(n, REGISTERS)


# ═════════════════════════════════════════════════════════════════════════════
# HS-2310-001b  cadence floors (real register_cache.py)
# ═════════════════════════════════════════════════════════════════════════════

class _Clock:
    def __init__(self):
        self.t = 50_000.0

    def monotonic(self):
        return self.t


class _R:
    def __init__(self, v):
        self.value = v


SOH = "storage_unit_1_battery_pack_1_soh_calibration_status"


class TestCadenceFloors(unittest.TestCase):
    def setUp(self):
        self.clk = _Clock()
        RC.time = self.clk
        self.addCleanup(setattr, RC, "time", _time)

    def test_soh_floor_names(self):
        floors = UC["_soh_calibration_ttl_floors"]()
        self.assertEqual(len(floors), 7)
        self.assertIn("storage_unit_soh_calibration_status", floors)
        self.assertIn("storage_unit_2_battery_pack_3_soh_calibration_status", floors)
        self.assertTrue(all(v == 3600.0 for v in floors.values()))
        for name in floors:
            self.assertIn(name, REGISTERS, name)

    def test_per_register_floor(self):
        c = RC.RegisterCache(ttl_floors_s={SOH: 3600.0})
        c.update({SOH: _R(1), "internal_temperature": _R(40)})
        self.clk.t += 1000  # SLOW base TTL is 900 s
        stale = c.filter_stale([SOH, "internal_temperature"], timedelta(seconds=30))
        self.assertEqual(stale, ["internal_temperature"])
        self.clk.t += 2601
        self.assertEqual(c.filter_stale([SOH], timedelta(seconds=30)), [SOH])

    def test_without_floor_it_would_be_due(self):
        c = RC.RegisterCache()
        c.update({SOH: _R(1)})
        self.clk.t += 1000
        self.assertEqual(c.filter_stale([SOH], timedelta(seconds=30)), [SOH])

    def test_coordinator_wide_floor(self):
        c = RC.RegisterCache(min_ttl_s=1800.0)
        c.update({"storage_maximum_charging_power": _R(5000), "model_name": _R("x")})
        self.clk.t += 1000
        self.assertEqual(c.filter_stale(["storage_maximum_charging_power"], timedelta(seconds=30)), [])
        self.clk.t += 801
        self.assertEqual(c.filter_stale(["storage_maximum_charging_power"], timedelta(seconds=30)),
                         ["storage_maximum_charging_power"])

    def test_floor_never_delays_a_written_register(self):
        c = RC.RegisterCache(min_ttl_s=1800.0)
        c.update({"storage_maximum_charging_power": _R(5000)})
        c.invalidate("storage_maximum_charging_power")  # our own write
        self.assertEqual(c.filter_stale(["storage_maximum_charging_power"], timedelta(seconds=30)),
                         ["storage_maximum_charging_power"])

    def test_floor_never_delays_an_uncertain_register(self):
        c = RC.RegisterCache(ttl_floors_s={SOH: 3600.0})
        c.update({SOH: _R(1)})
        c.record_attempt([SOH], RC.Quality.UNCERTAIN, RC.Reason.TIMEOUT)
        self.assertEqual(c.filter_stale([SOH], timedelta(seconds=30)), [SOH])

    def test_static_ignores_floors(self):
        c = RC.RegisterCache(min_ttl_s=99999.0)
        c.update({"model_name": _R("x")})
        e = c._store["model_name"]
        self.assertEqual(c._effective_ttl(e, "model_name"), e.effective_ttl)

    def test_negative_values_clamped(self):
        c = RC.RegisterCache(min_ttl_s=-5, ttl_floors_s={SOH: -1})
        self.assertEqual(c._min_ttl_s, 0.0)
        self.assertEqual(c._ttl_floors_s[SOH], 0.0)

    def test_defaults_are_no_floor(self):
        c = RC.RegisterCache()
        self.assertEqual((c._min_ttl_s, c._ttl_floors_s), (0.0, {}))


# ═════════════════════════════════════════════════════════════════════════════
# Options (bus_policy.BusPolicy)
# ═════════════════════════════════════════════════════════════════════════════

class TestBusPolicy(unittest.TestCase):
    KEYS = ("slow_path_isolation", "slow_register_cadence", "single_retry_layer",
            "protect_power_reads", "dawn_peer_wake")

    def test_defaults_all_on(self):
        p = BUS_POLICY.BusPolicy.from_options(None)
        self.assertTrue(all(getattr(p, k) for k in self.KEYS))
        self.assertEqual(p, BUS_POLICY.BusPolicy.from_options({}))

    def test_each_switch_maps_to_its_key(self):
        for k in self.KEYS:
            p = BUS_POLICY.BusPolicy.from_options({k: False})
            self.assertFalse(getattr(p, k))
            self.assertTrue(all(getattr(p, o) for o in self.KEYS if o != k))

    def test_non_bool_falls_back_to_default(self):
        for junk in ("false", 0, "", None, 1, "yes"):
            p = BUS_POLICY.BusPolicy.from_options({k: junk for k in self.KEYS})
            self.assertTrue(all(getattr(p, k) for k in self.KEYS), junk)

    def test_keys_match_const(self):
        for k in self.KEYS:
            self.assertEqual(getattr(CONST, f"CONF_{k.upper()}"), k)
            self.assertIs(getattr(CONST, f"DEFAULT_{k.upper()}"), True)


# ═════════════════════════════════════════════════════════════════════════════
# HS-2310-002  single retry layer (REAL tmodbus transport objects)
# ═════════════════════════════════════════════════════════════════════════════

def _client():
    # Never connects: only the transport objects are exercised.
    return create_tcp_client(host="127.0.0.1", port=1, unit_id=1)


def _instrument(client, outcomes):
    """Replace the base transport's wire call with a scripted fake and the
    retry sleeps with no-ops. Returns the call list."""
    transport = client.transport
    calls = []
    script = list(outcomes)

    async def fake_send(unit_id, pdu):
        calls.append(unit_id)
        item = script.pop(0) if script else "ok"
        if isinstance(item, BaseException) or (isinstance(item, type) and issubclass(item, BaseException)):
            raise item() if isinstance(item, type) else item
        return item

    async def no_sleep(_s):
        return None

    transport.base_transport.send_and_receive = fake_send
    transport.base_transport.is_open = lambda: True

    async def fake_reconnect():
        return None

    transport._do_auto_reconnect = fake_reconnect
    transport.response_retry_strategy = transport.response_retry_strategy.copy(sleep=no_sleep)
    return calls


def _send(client):
    return asyncio.run(client.transport.send_and_receive(1, object()))


class TestSingleRetryLayer(unittest.TestCase):
    def _applied(self):
        c = _client()
        self.assertTrue(BUS_POLICY.apply_single_retry_layer(c, 20.0))
        return c

    def test_timeout_is_set_on_the_base_transport(self):
        c = self._applied()
        self.assertEqual(c.transport.base_transport.timeout, 20.0)

    def test_tcp_transport_takes_timeout_at_connect(self):
        """The per-attempt timeout is read by tmodbus when the protocol is
        created (connect/reconnect) -- setting it before connect is enough."""
        import inspect
        from tmodbus.transport.async_tcp import AsyncTcpTransport
        self.assertIn("timeout=self.timeout", inspect.getsource(AsyncTcpTransport.open))

    def test_library_default_resends_on_timeout(self):
        """Adversarial control: WITHOUT the change the transport re-sends."""
        c = _client()
        calls = _instrument(c, [TimeoutError, TimeoutError, TimeoutError])
        with self.assertRaises(TimeoutError):
            _send(c)
        self.assertEqual(len(calls), 3)

    def test_library_default_resends_on_busy(self):
        c = _client()
        calls = _instrument(c, [_Busy(), "ok"])
        self.assertEqual(_send(c), "ok")
        self.assertEqual(len(calls), 2)

    def test_timeout_sent_once_and_surfaces(self):
        c = self._applied()
        calls = _instrument(c, [TimeoutError, "ok"])
        with self.assertRaises(TimeoutError):
            _send(c)
        self.assertEqual(len(calls), 1)

    def test_busy_sent_once_and_surfaces(self):
        c = self._applied()
        calls = _instrument(c, [_Busy(), "ok"])
        with self.assertRaises(ServerDeviceBusyError):
            _send(c)
        self.assertEqual(len(calls), 1)

    def test_lost_connection_is_retried_once(self):
        c = self._applied()
        calls = _instrument(c, [ModbusConnectionError("gone"), "ok"])
        self.assertEqual(_send(c), "ok")
        self.assertEqual(len(calls), 2)

    def test_lost_connection_twice_gives_up(self):
        c = self._applied()
        calls = _instrument(c, [ModbusConnectionError("a"), ModbusConnectionError("b"), "ok"])
        with self.assertRaises(ModbusConnectionError):
            _send(c)
        self.assertEqual(len(calls), 2)

    def test_success_unchanged(self):
        c = self._applied()
        calls = _instrument(c, ["ok"])
        self.assertEqual(_send(c), "ok")
        self.assertEqual(len(calls), 1)

    def test_fail_safe_on_unexpected_object(self):
        obj = types.SimpleNamespace(transport=types.SimpleNamespace())
        self.assertFalse(BUS_POLICY.apply_single_retry_layer(obj, 20.0))
        self.assertFalse(BUS_POLICY.apply_single_retry_layer(object(), 20.0))

    def test_fail_safe_on_bad_timeout_changes_nothing(self):
        c = _client()
        before = (c.transport.response_retry_strategy, c.transport.base_transport.timeout)
        self.assertFalse(BUS_POLICY.apply_single_retry_layer(c, 0))
        self.assertEqual((c.transport.response_retry_strategy, c.transport.base_transport.timeout), before)


class TestTimeoutConsistency(unittest.TestCase):
    """Every bound that wraps ONE transport attempt on a steady-state path is
    longer than the attempt, so the transport ends a slow attempt."""

    def test_values(self):
        self.assertEqual(CONST.MODBUS_RESPONSE_TIMEOUT, timedelta(seconds=20))
        self.assertEqual(CONST.RESPONSE_TIMEOUT_MARGIN, timedelta(seconds=3))
        need = CONST.MODBUS_RESPONSE_TIMEOUT + CONST.RESPONSE_TIMEOUT_MARGIN
        self.assertGreaterEqual(CONST.WRITE_TIMEOUT, need)
        self.assertGreaterEqual(KEEPALIVE._KEEPALIVE_TIMEOUT, need)
        # guard admission wait is NOT an attempt bound; unchanged
        self.assertEqual(GUARD.QUEUE_WAIT_TIMEOUT, timedelta(seconds=10))

    def test_chunk_timeout_raised_when_option_on(self):
        body = ast.unparse(_method("HuaweiSolarUpdateCoordinator", "_execute_batch"))
        self.assertIn("if self._bus_policy.single_retry_layer:", body)
        self.assertIn("MODBUS_RESPONSE_TIMEOUT + RESPONSE_TIMEOUT_MARGIN", body)
        self.assertIn("async with asyncio.timeout(chunk_timeout_s):", body)
        self.assertNotIn("asyncio.timeout(effective_timeout.total_seconds())", body)

    def test_setup_applies_policy_before_first_exchange(self):
        func = _func(_INIT_SRC, "async_setup_entry")
        src = ast.get_source_segment(_INIT_SRC, func)
        a = src.index("apply_single_retry_layer(client, MODBUS_RESPONSE_TIMEOUT.total_seconds())")
        b = src.index("create_device_instance(client)")
        self.assertLess(a, b)
        self.assertIn("if BusPolicy.from_options(entry.options).single_retry_layer:", src)


# ═════════════════════════════════════════════════════════════════════════════
# HS-2310-003  transition trigger/duration (real notify_transition source)
# ═════════════════════════════════════════════════════════════════════════════

def _func(src, name):
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(name)


def _method(cls, name, src=_UC_SRC):
    for node in ast.parse(src).body:
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name:
                    return item
    raise AssertionError(f"{cls}.{name}")


def _exec_method(src, cls, name, extra_ns):
    node = _method(cls, name, src)
    node.decorator_list = []
    code = ast.unparse(node)
    ns = dict(extra_ns)
    exec("from __future__ import annotations\n" + code, ns)  # noqa: S102
    return ns[name]


class TestNotifyTransition(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.fn = _exec_method(_ADAPT_SRC, "AdaptiveModbusController", "notify_transition", {
            "timedelta": timedelta,
            "time": self.clock,
            "ADAPTIVE_TRANSITION_DURATION_MINUTES": 10,
            "_LOGGER": logging.getLogger("t"),
        })
        self.me = types.SimpleNamespace(
            _in_transition=False, _transition_expires=0.0, _last_transition_reason=None,
            _last_transition_ts=0.0, serial_number="x", _push_to_listeners=lambda _: None)

    def test_default_is_ten_minutes(self):
        self.fn(self.me, "day→night")
        self.assertEqual(self.me._transition_expires - self.clock.t, 600.0)

    def test_busy_duration_five_minutes(self):
        self.fn(self.me, "busy", duration=CONST.BUSY_TRANSITION_DURATION)
        self.assertEqual(self.me._transition_expires - self.clock.t, 300.0)

    def test_never_shortens_a_running_transition(self):
        self.fn(self.me, "night→day")
        self.clock.t += 60
        self.fn(self.me, "busy", duration=timedelta(minutes=5))
        self.assertEqual(self.me._transition_expires, 50_000.0 + 600.0)

    def test_extends_when_later(self):
        self.fn(self.me, "busy", duration=timedelta(minutes=5))
        self.clock.t += 240
        self.fn(self.me, "busy", duration=timedelta(minutes=5))
        self.assertEqual(self.me._transition_expires, self.clock.t + 300.0)

    def test_zero_duration_means_default(self):
        self.fn(self.me, "x", duration=timedelta(0))
        self.assertEqual(self.me._transition_expires - self.clock.t, 600.0)

    def test_constant(self):
        self.assertEqual(CONST.BUSY_TRANSITION_DURATION, timedelta(minutes=5))


class TestChunkLoopWiring(unittest.TestCase):
    body = ast.unparse(_method("HuaweiSolarUpdateCoordinator", "_execute_batch"))

    def test_priority_lane_for_power_chunks(self):
        self.assertIn("use_priority = self._bus_policy.protect_power_reads and _is_power_flow_chunk(chunk)",
                      self.body)
        self.assertIn("self.guard.request(priority=use_priority, label=self.name)", self.body)

    def test_first_busy_trigger_only_when_option_off(self):
        self.assertIn(
            "if busy_retries == 1 and self._adaptive and (not self._bus_policy.protect_power_reads):",
            self.body)

    def test_persistent_busy_trigger(self):
        self.assertIn("self._adaptive.notify_transition('0x06 SLAVE_DEVICE_BUSY (persistent)', "
                      "duration=BUSY_TRANSITION_DURATION)", self.body)
        i = self.body.index("0x06 SLAVE_DEVICE_BUSY (persistent)")
        j = self.body.index("continue")
        self.assertGreater(i, j, "persistent trigger must be after the retry branch")

    def test_isolation_switch(self):
        self.assertIn("slow_ranges = _SLOW_ADDRESS_RANGES if self._bus_policy.slow_path_isolation else ()",
                      self.body)
        self.assertIn("_address_group(protected_run, slow_ranges)", self.body)

    def test_f06_tmodbus_error_per_chunk(self):
        self.assertIn("except (TimeoutError, ReadException, ConnectionInterruptedException, "
                      "HuaweiSolarException, TModbusError) as exc:", self.body)

    def test_f06_classification_of_raw_transport_error(self):
        """A raw TModbusError lands in the generic UNCERTAIN branch, i.e. it
        is recorded as a miss, not raised past the chunk loop."""
        src = ast.unparse(_method("HuaweiSolarUpdateCoordinator", "_classify_failure"))
        self.assertTrue(src.rstrip().endswith("return (Quality.UNCERTAIN, Reason.LINK_DOWN)"))
        self.assertTrue(issubclass(ServerDeviceBusyError, TModbusError))


class TestGuardPriorityLane(unittest.TestCase):
    def test_depth(self):
        self.assertEqual(GUARD.MAX_PRIORITY_QUEUE_DEPTH, 4)
        self.assertEqual(GUARD.PRIORITY_AIRTIME_BUDGET_FRACTION, 0.20)

    def test_priority_not_shed_when_normal_lane_full(self):
        g = GUARD.ModbusGuard("t2310:priority")
        g._max_queue_depth = 1
        g._queue_depth = 1  # normal lane full

        async def run():
            with self.assertRaises(GUARD.ModbusQueueShed):
                async with g.request(label="normal"):
                    pass
            async with g.request(priority=True, label="power"):
                return True

        self.assertTrue(asyncio.run(run()))


# ═════════════════════════════════════════════════════════════════════════════
# HS-2310-004  dawn peer wake
# ═════════════════════════════════════════════════════════════════════════════

class TestNightDetectorHold(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        NIGHT.time = self.clock
        self.addCleanup(setattr, NIGHT, "time", _time)
        self.modes = []
        self.d = NIGHT.NightModeDetector(self.modes.append, timedelta(seconds=30), timedelta(minutes=5))
        self.d._mode = NIGHT.InverterMode.NIGHT

    def _pv(self, w, status=None):
        r = {"input_power": _R(w)}
        if status is not None:
            r["device_status"] = _R(status)
        return r

    def test_force_day_and_hold_against_pv(self):
        self.d.force_day("peer", hold_s=3600)
        self.assertEqual(self.modes, [NIGHT.InverterMode.DAY])
        for _ in range(5):
            self.d.evaluate(self._pv(10))
        self.assertFalse(self.d.is_night)

    def test_hold_against_standby_status(self):
        self.d.force_day("peer", hold_s=3600)
        self.d.evaluate(self._pv(10, "Standby: no irradiation"))
        self.assertFalse(self.d.is_night)

    def test_after_hold_normal_rules(self):
        self.d.force_day("peer", hold_s=3600)
        self.clock.t += 3601
        for _ in range(NIGHT.NIGHT_ENTRY_HOLD):
            self.d.evaluate(self._pv(10))
        self.assertTrue(self.d.is_night)

    def test_force_day_noop_in_day(self):
        self.d._mode = NIGHT.InverterMode.DAY
        self.d.force_day("peer", hold_s=3600)
        self.assertEqual(self.modes, [])
        self.assertFalse(self.d.night_entry_suppressed())

    def test_without_hold_pv_puts_it_back_to_night(self):
        """Control: without the hold the woken device drops back at once."""
        self.d.force_day("peer", hold_s=0)
        for _ in range(NIGHT.NIGHT_ENTRY_HOLD):
            self.d.evaluate(self._pv(10))
        self.assertTrue(self.d.is_night)


class _FakeCoord:
    """Carries the REAL _on_mode_change/_wake_bus_peers/_peer_wake methods."""


def _build_fake_coordinator_class():
    ns = {
        "_bus_peers": UC["_bus_peers"],
        "InverterMode": NIGHT.InverterMode,
        "NIGHT_POLL_INTERVAL": timedelta(minutes=5),
        "PEER_WAKE_HOLD": CONST.PEER_WAKE_HOLD,
        "_LOGGER": logging.getLogger("t2310"),
    }
    for name in ("_on_mode_change", "_wake_bus_peers", "_peer_wake"):
        setattr(_FakeCoord, name, _exec_method(_UC_SRC, "HuaweiSolarUpdateCoordinator", name, ns))
    return _FakeCoord


def _coord(bus, name, night=True, peer_wake=True):
    c = FakeCoord()
    c.name = name
    c._bus_key = bus
    c._bus_policy = BUS_POLICY.BusPolicy(dawn_peer_wake=peer_wake)
    c._waking_from_peer = False
    c._shutdown = False
    c.cache = types.SimpleNamespace(set_night_mode=lambda v: None)
    c.telemetry = None
    c._adaptive = None
    c._adaptive_poll_interval = lambda: timedelta(seconds=30)
    c.update_interval = None
    c._night_detector = NIGHT.NightModeDetector(c._on_mode_change, timedelta(seconds=30),
                                                timedelta(minutes=5))
    if night:
        c._night_detector._mode = NIGHT.InverterMode.NIGHT
    UC["_register_bus_member"](bus, c)
    return c


class TestPeerWake(unittest.TestCase):
    def setUp(self):
        UC["_BUS_MEMBERS"].clear()

    def test_own_wake_wakes_peers_on_same_bus_only(self):
        inv2 = _coord("bus", "inv2")
        inv1 = _coord("bus", "inv1")
        batt = _coord("bus", "inv1_battery")
        other = _coord("other-bus", "x")
        inv2._night_detector.evaluate({"input_power": _R(500)})  # own evidence
        self.assertFalse(inv1._night_detector.is_night)
        self.assertFalse(batt._night_detector.is_night)
        self.assertTrue(other._night_detector.is_night)
        self.assertTrue(inv1._night_detector.night_entry_suppressed())

    def test_no_cascade_from_a_peer_wake(self):
        calls = []
        inv2 = _coord("bus", "inv2")
        inv1 = _coord("bus", "inv1")
        orig = FakeCoord._wake_bus_peers

        def spy(self):
            calls.append(self.name)
            orig(self)
        FakeCoord._wake_bus_peers = spy
        try:
            inv2._night_detector.evaluate({"input_power": _R(500)})
        finally:
            FakeCoord._wake_bus_peers = orig
        self.assertEqual(calls, ["inv2"])
        self.assertFalse(inv1._night_detector.is_night)

    def test_option_off(self):
        inv2 = _coord("bus", "inv2", peer_wake=False)
        inv1 = _coord("bus", "inv1")
        inv2._night_detector.evaluate({"input_power": _R(500)})
        self.assertTrue(inv1._night_detector.is_night)

    def test_night_entry_is_not_propagated(self):
        inv2 = _coord("bus", "inv2", night=False)
        inv1 = _coord("bus", "inv1", night=False)
        for _ in range(NIGHT.NIGHT_ENTRY_HOLD):
            inv2._night_detector.evaluate({"input_power": _R(0)})
        self.assertTrue(inv2._night_detector.is_night)
        self.assertFalse(inv1._night_detector.is_night)

    def test_shutdown_peer_not_woken(self):
        inv2 = _coord("bus", "inv2")
        inv1 = _coord("bus", "inv1")
        inv1._shutdown = True
        inv2._night_detector.evaluate({"input_power": _R(500)})
        self.assertTrue(inv1._night_detector.is_night)

    def test_registry_unregister(self):
        a = _coord("bus", "a")
        UC["_unregister_bus_member"]("bus", a)
        self.assertNotIn("bus", UC["_BUS_MEMBERS"])

    def test_wiring(self):
        unload = ast.unparse(_method("HuaweiSolarUpdateCoordinator", "_on_entry_unload"))
        self.assertIn("_unregister_bus_member(self._bus_key, self)", unload)
        init = ast.unparse(_method("HuaweiSolarUpdateCoordinator", "__init__"))
        self.assertIn("_register_bus_member(endpoint, self)", init)
        self.assertEqual(CONST.PEER_WAKE_HOLD, timedelta(minutes=60))


# ═════════════════════════════════════════════════════════════════════════════
# HS-2310-001b wiring
# ═════════════════════════════════════════════════════════════════════════════

class TestCadenceWiring(unittest.TestCase):
    def test_cache_floors_gated_by_option(self):
        init = ast.unparse(_method("HuaweiSolarUpdateCoordinator", "__init__"))
        self.assertIn("self._bus_policy.slow_register_cadence", init)
        self.assertIn("ttl_floors_s=_soh_calibration_ttl_floors() if self._bus_policy.slow_register_cadence else None",
                      init)
        self.assertIn(
            "min_ttl_s=min_register_ttl.total_seconds() if min_register_ttl is not None "
            "and self._bus_policy.slow_register_cadence else 0.0", init)

    def test_config_coordinators_get_the_floor(self):
        self.assertEqual(_INIT_SRC.count("min_register_ttl=CONFIGURATION_MIN_REGISTER_TTL"), 2)
        self.assertEqual(CONST.CONFIGURATION_MIN_REGISTER_TTL, timedelta(minutes=30))
        self.assertEqual(CONST.SOH_CALIBRATION_MIN_TTL, timedelta(hours=1))

    def test_policy_created_before_cache(self):
        init = ast.unparse(_method("HuaweiSolarUpdateCoordinator", "__init__"))
        self.assertLess(init.index("self._bus_policy: BusPolicy = BusPolicy.from_options("),
                        init.index("self.cache = RegisterCache("))


# ═════════════════════════════════════════════════════════════════════════════
# HS-2310-005  F-05 awaited keep-alive stop (real ModbusKeepAlive.async_stop)
# ═════════════════════════════════════════════════════════════════════════════

class TestKeepAliveAsyncStop(unittest.TestCase):
    def _ka(self):
        ka = KEEPALIVE.ModbusKeepAlive.__new__(KEEPALIVE.ModbusKeepAlive)
        ka.serial_number = "t"
        ka._task = None
        return ka

    def test_cancels_and_waits_until_exited(self):
        events = []

        async def run():
            ka = self._ka()

            async def loop():
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    await asyncio.sleep(0.05)  # unwinding an in-flight probe
                    events.append("exited")
                    raise
            ka._task = asyncio.get_running_loop().create_task(loop())
            await asyncio.sleep(0)
            ok = await ka.async_stop(2.0)
            events.append("returned")
            return ok, ka._task

        ok, task = asyncio.run(run())
        self.assertTrue(ok)
        self.assertIsNone(task)
        self.assertEqual(events, ["exited", "returned"])

    def test_bounded_when_task_ignores_cancel(self):
        async def run():
            ka = self._ka()
            stop = asyncio.Event()

            async def stubborn():
                while not stop.is_set():
                    try:
                        await asyncio.sleep(0.01)
                    except asyncio.CancelledError:
                        continue
            ka._task = asyncio.get_running_loop().create_task(stubborn())
            await asyncio.sleep(0)
            t0 = _time.monotonic()
            ok = await ka.async_stop(0.2)
            elapsed = _time.monotonic() - t0
            stop.set()
            return ok, elapsed

        ok, elapsed = asyncio.run(run())
        self.assertFalse(ok)
        self.assertLess(elapsed, 1.0)

    def test_no_task(self):
        self.assertTrue(asyncio.run(self._ka().async_stop(1.0)))

    def test_unload_awaits_before_disconnect(self):
        func = _func(_INIT_SRC, "async_unload_entry")
        src = ast.get_source_segment(_INIT_SRC, func)
        a = src.index("await keepalive.async_stop(KEEPALIVE_STOP_TIMEOUT.total_seconds())")
        b = src.index("primary_device.client.disconnect(),\n")
        self.assertLess(a, b)

    def test_setup_rollback_uses_awaited_stop(self):
        self.assertIn("functools.partial(keepalive.async_stop, KEEPALIVE_STOP_TIMEOUT.total_seconds())",
                      _INIT_SRC)


# ═════════════════════════════════════════════════════════════════════════════
# HS-2310-005  F-04 reconfigure restore (real ConfigFlow.async_remove)
# ═════════════════════════════════════════════════════════════════════════════

class TestReconfigureRestore(unittest.TestCase):
    def setUp(self):
        self.remove = _exec_method(_FLOW_SRC, "ConfigFlow", "async_remove",
                                   {"_LOGGER": logging.getLogger("t")})
        self.scheduled = []
        hass = types.SimpleNamespace(
            async_create_task=lambda coro, name=None: self.scheduled.append((coro, name)),
            config_entries=types.SimpleNamespace(async_reload=lambda eid: ("reload", eid)),
        )
        self.flow = types.SimpleNamespace(hass=hass, _reconfigure_unloaded_entry_id=None,
                                          _reconfigure_committed=False)

    def test_abandoned_reconfigure_reloads(self):
        self.flow._reconfigure_unloaded_entry_id = "E1"
        self.remove(self.flow)
        self.assertEqual(len(self.scheduled), 1)
        self.assertEqual(self.scheduled[0][0], ("reload", "E1"))

    def test_committed_does_not_reload_twice(self):
        self.flow._reconfigure_unloaded_entry_id = "E1"
        self.flow._reconfigure_committed = True
        self.remove(self.flow)
        self.assertEqual(self.scheduled, [])

    def test_other_flows_untouched(self):
        self.remove(self.flow)
        self.assertEqual(self.scheduled, [])

    def test_only_once(self):
        self.flow._reconfigure_unloaded_entry_id = "E1"
        self.remove(self.flow)
        self.remove(self.flow)
        self.assertEqual(len(self.scheduled), 1)

    def test_never_raises(self):
        self.flow._reconfigure_unloaded_entry_id = "E1"

        def boom(coro, name=None):
            raise RuntimeError("x")
        self.flow.hass.async_create_task = boom
        self.remove(self.flow)  # must not raise

    def test_wiring(self):
        step = ast.unparse(_method("ConfigFlow", "async_step_reconfigure", _FLOW_SRC))
        self.assertIn("unloaded = await self.hass.config_entries.async_unload(self.context['entry_id'])",
                      step)
        self.assertIn("if unloaded:", step)
        self.assertIn("self._reconfigure_unloaded_entry_id = self.context['entry_id']", step)
        src = _FLOW_SRC
        i = src.index("self._reconfigure_committed = True")
        j = src.index("await self.hass.config_entries.async_reload(\n                self._reconfigure_entry.entry_id")
        self.assertLess(i, j)
        self.assertIn("@callback\n    def async_remove(self) -> None:", src)


# ═════════════════════════════════════════════════════════════════════════════
# Options UI, strings, version
# ═════════════════════════════════════════════════════════════════════════════

class TestOptionsAndStrings(unittest.TestCase):
    KEYS = TestBusPolicy.KEYS

    def test_options_flow_offers_all_five(self):
        for k in self.KEYS:
            self.assertIn(f"CONF_{k.upper()},\n                    default=options.get(CONF_{k.upper()}, "
                          f"DEFAULT_{k.upper()}),", _FLOW_SRC)

    def test_labels(self):
        for f in ("strings.json", "translations/en.json"):
            d = json.loads((_ROOT / f).read_text(encoding="utf-8"))
            data = d["options"]["step"]["init"]["data"]
            for k in self.KEYS:
                self.assertTrue(data.get(k), (f, k))

    def test_version(self):
        manifest = json.loads((_ROOT / "manifest.json").read_text())
        # v2.3.2.0: moved from "2.3.1.0" -- pins the CURRENT release.
        self.assertEqual(manifest["version"], "2.3.2.0")


if __name__ == "__main__":
    unittest.main()
