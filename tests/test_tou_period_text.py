"""Tests for v2.3.0.0 -- editable per-slot LUNA2000 time-of-use periods.

Covers:

* tou_periods.py (pure logic) against the REAL ``huawei-solar`` library:
  grammar, formatting, day mapping, schedule validation (including a
  seeded equivalence check against the library's own validator), and
  single-slot edit semantics.
* text.py (the new platform) against minimal Home Assistant stubs and a
  fake device/guard/coordinator: eligibility, display, and the full
  ICS write path (validate -> lock -> one guard hold covering fresh read
  + write -> bookkeeping), including every failure branch.
* HS-230-001: the length cap on the schema that is ACTUALLY registered
  for ``set_tou_periods``.
* Static contracts: platform registration, translation keys and their
  placeholders, manifest version.

Loading approach: integration modules are loaded under a private package
name (``hs_integ``) so their relative imports resolve to each other,
while ``huawei_solar`` keeps resolving to the real PyPI library. This
avoids the package-name collision described in test_date.py without
stubbing the library itself. Requires ``huawei-solar`` and
``voluptuous`` (both runtime requirements of the integration).

Run standalone:  cd tests && python3 -m pytest test_tou_period_text.py
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import importlib.util
import json
import pathlib
import random
import re
import sys
import types
import unittest
from datetime import timedelta

_ROOT = pathlib.Path(__file__).parent.parent
_PKG = "hs_integ"

# Real library -- imported before any stubbing.
from huawei_solar import register_names as rn  # noqa: E402
from huawei_solar import register_values as rv  # noqa: E402
from huawei_solar.exceptions import (  # noqa: E402
    HuaweiSolarException,
    TimeOfUsePeriodsException,
)
from huawei_solar.register_definitions import periods as lib_periods  # noqa: E402
from huawei_solar.register_definitions.periods import (  # noqa: E402
    ChargeFlag,
    HUAWEI_LUNA2000_TimeOfUsePeriod as Period,
)
from huawei_solar.registers import REGISTERS  # noqa: E402

TOU_REG = rn.STORAGE_HUAWEI_LUNA2000_TIME_OF_USE_CHARGING_AND_DISCHARGING_PERIODS
ALL_DAYS = (True,) * 7


# ═══════════════════════════════════════════════════════════════════════
# Home Assistant stubs (only what text.py / types.py actually use)
# ═══════════════════════════════════════════════════════════════════════


def _mod(name: str) -> types.ModuleType:
    m = sys.modules.get(name)
    if m is None:
        m = types.ModuleType(name)
        sys.modules[name] = m
    return m


def _install_ha_stubs() -> None:
    for name in (
        "homeassistant", "homeassistant.components", "homeassistant.helpers",
    ):
        _mod(name)

    core = _mod("homeassistant.core")
    core.HomeAssistant = type("HomeAssistant", (), {})
    core.callback = lambda f: f

    const = _mod("homeassistant.const")

    class EntityCategory:
        CONFIG = "config"
        DIAGNOSTIC = "diagnostic"

    const.EntityCategory = EntityCategory

    exc = _mod("homeassistant.exceptions")

    class HomeAssistantError(Exception):
        def __init__(self, *args, translation_domain=None, translation_key=None,
                     translation_placeholders=None):
            super().__init__(*args)
            self.translation_domain = translation_domain
            self.translation_key = translation_key
            self.translation_placeholders = translation_placeholders

    class ServiceValidationError(HomeAssistantError):
        pass

    exc.HomeAssistantError = HomeAssistantError
    exc.ServiceValidationError = ServiceValidationError

    ce = _mod("homeassistant.config_entries")

    class ConfigEntry:
        def __class_getitem__(cls, item):
            return cls

    ce.ConfigEntry = ConfigEntry

    _mod("homeassistant.helpers.device_registry").DeviceInfo = dict
    _mod("homeassistant.helpers.entity_platform").AddEntitiesCallback = object

    ent = _mod("homeassistant.helpers.entity")

    class Entity:
        """Mirrors real HA: available -> _attr_available (default True)."""
        entity_id = None
        _attr_available = True

        @property
        def available(self):
            return self._attr_available

        def async_write_ha_state(self):
            self.__dict__.setdefault("_state_writes", 0)
            self._state_writes += 1

    class EntityDescription:
        def __init__(self, key, **kwargs):
            self.key = key
            for k, v in kwargs.items():
                setattr(self, k, v)

    ent.Entity = Entity
    ent.EntityDescription = EntityDescription

    uc = _mod("homeassistant.helpers.update_coordinator")

    class CoordinatorEntity(Entity):
        """Mirrors real HA: available ONLY reflects last_update_success
        (confirmed against homeassistant/helpers/update_coordinator.py)."""

        def __class_getitem__(cls, item):
            return cls

        def __init__(self, coordinator, context=None):
            self.coordinator = coordinator
            self.coordinator_context = context

        @property
        def available(self):
            return self.coordinator.last_update_success

    uc.CoordinatorEntity = CoordinatorEntity

    text = _mod("homeassistant.components.text")

    class TextMode:
        TEXT = "text"
        PASSWORD = "password"

    class TextEntity(Entity):
        _attr_native_value = None

        @property
        def native_value(self):
            return self._attr_native_value

    text.TextEntity = TextEntity
    text.TextMode = TextMode


def _load(modname: str) -> types.ModuleType:
    src = _ROOT / f"{modname}.py"
    spec = importlib.util.spec_from_file_location(f"{_PKG}.{modname}", str(src))
    m = importlib.util.module_from_spec(spec)
    m.__package__ = _PKG
    sys.modules[f"{_PKG}.{modname}"] = m
    spec.loader.exec_module(m)
    return m


def _bootstrap():
    _install_ha_stubs()
    pkg = types.ModuleType(_PKG)
    pkg.__path__ = []  # mark as package
    sys.modules[_PKG] = pkg

    const = _load("const")
    tou = _load("tou_periods")

    # update_coordinator.py is far too HA-coupled to load here; text.py
    # and types.py only need the class names.
    ucm = types.ModuleType(f"{_PKG}.update_coordinator")
    ucm.HuaweiSolarUpdateCoordinator = type("HuaweiSolarUpdateCoordinator", (), {})
    ucm.HuaweiSolarOptimizerUpdateCoordinator = type(
        "HuaweiSolarOptimizerUpdateCoordinator", (), {}
    )
    sys.modules[f"{_PKG}.update_coordinator"] = ucm

    types_mod = _load("types")  # REAL guard/lock/deadline primitives
    text = _load("text")
    return const, tou, types_mod, text


CONST, TOU, TYPES, TEXT = _bootstrap()
HAExc = sys.modules["homeassistant.exceptions"]


def _run(coro):
    return asyncio.run(coro)


def P(start, end, days=ALL_DAYS, flag=ChargeFlag.CHARGE):
    return Period(start_time=start, end_time=end, charge_flag=flag, days_effective=days)


# ═══════════════════════════════════════════════════════════════════════
# Part A — tou_periods.py (pure)
# ═══════════════════════════════════════════════════════════════════════


class TestConstants(unittest.TestCase):
    def test_slot_count_matches_library(self):
        self.assertEqual(TOU.MAX_TOU_PERIODS, lib_periods.HUAWEI_LUNA2000_TOU_PERIODS)
        self.assertEqual(TOU.MAX_TOU_PERIODS, 14)

    def test_text_max_length_is_longest_valid_period(self):
        self.assertEqual(TOU.PERIOD_TEXT_MAX_LENGTH, len("00:00-06:00/1234567/+"))

    def test_default_visible_slots_in_range(self):
        self.assertTrue(1 <= TOU.DEFAULT_VISIBLE_TOU_SLOTS <= TOU.MAX_TOU_PERIODS)

    def test_raw_cap_above_entity_cap(self):
        self.assertGreater(TOU.RAW_INPUT_MAX_LENGTH, TOU.PERIOD_TEXT_MAX_LENGTH)

    def test_module_has_no_homeassistant_import(self):
        tree = ast.parse((_ROOT / "tou_periods.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertFalse((node.module or "").startswith("homeassistant"))
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(alias.name.startswith("homeassistant"))


class TestParseValid(unittest.TestCase):
    def test_basic_charge(self):
        p = TOU.parse_period_text("00:00-06:00/1234567/+")
        self.assertEqual(p, P(0, 360))

    def test_discharge_weekdays(self):
        p = TOU.parse_period_text("17:00-21:30/12345/-")
        self.assertEqual(p.start_time, 17 * 60)
        self.assertEqual(p.end_time, 21 * 60 + 30)
        self.assertEqual(p.charge_flag, ChargeFlag.DISCHARGE)
        # library index 0 = Sunday; Monday..Friday = 1..5
        self.assertEqual(p.days_effective, (False, True, True, True, True, True, False))

    def test_sunday_is_seven_and_maps_to_index_zero(self):
        p = TOU.parse_period_text("10:00-11:00/7/+")
        self.assertEqual(p.days_effective, (True, False, False, False, False, False, False))

    def test_latest_allowed_end(self):
        p = TOU.parse_period_text("00:00-23:59/1/+")
        self.assertEqual(p.end_time, 23 * 60 + 59)

    def test_surrounding_whitespace_tolerated(self):
        self.assertEqual(TOU.parse_period_text("  00:00-06:00/1/+ "),
                         TOU.parse_period_text("00:00-06:00/1/+"))

    def test_day_order_irrelevant_and_normalised_on_display(self):
        p = TOU.parse_period_text("08:00-09:00/7312/+")
        self.assertEqual(TOU.format_period(p), "08:00-09:00/1237/+")

    def test_empty_and_whitespace_mean_clear(self):
        self.assertIsNone(TOU.parse_period_text(""))
        self.assertIsNone(TOU.parse_period_text("   "))

    def test_round_trip_canonical(self):
        for text in ("00:00-06:00/1234567/+", "17:00-21:30/12345/-",
                     "23:00-23:59/67/-", "05:05-05:06/4/+"):
            self.assertEqual(TOU.format_period(TOU.parse_period_text(text)), text)

    def test_parsed_period_equals_library_decoded_period(self):
        """write-verification compares written vs read-back values with ==;
        this proves a parsed period survives the library's own
        encode/decode unchanged."""
        reg = REGISTERS[TOU_REG]
        written = [TOU.parse_period_text("00:00-06:00/1234567/+"),
                   TOU.parse_period_text("17:00-21:30/12345/-"),
                   TOU.parse_period_text("10:00-11:00/7/+")]
        decoded = reg.decode(reg.encode(written)).value
        self.assertEqual(decoded, written)


class TestParseInvalid(unittest.TestCase):
    def assertRejected(self, text, key):
        with self.assertRaises(TOU.TouPeriodError) as ctx:
            TOU.parse_period_text(text)
        self.assertEqual(ctx.exception.translation_key, key, text)
        self.assertIsInstance(ctx.exception, ValueError)

    def test_format_errors(self):
        for text in (
            "24:00-24:00/1/+", "00:00-24:00/1/+",  # 24:00 refused, like services.py
            "25:00-26:00/1/+", "12:60-13:00/1/+", "1:00-2:00/1/+",
            "00:00-06:00/1/", "00:00-06:00//+", "00:00-06:00/1234567",
            "00:00-06:00/0/+", "00:00-06:00/8/+", "00:00-06:00/1/*",
            "00:00-06:00/12345678/+",
            "00:00-06:00/1/+\n01:00-02:00/1/+",   # one period per slot
            "00:00-06:00/1/+;07:00-08:00/1/+",
            "+00:00-06:00/1/+", "00:00 - 06:00/1/+", "abc",
            "٠٠:٠٠-٠٦:٠٠/١/+",  # non-ASCII digits
            "00:00-06:00/1/+\x00",
        ):
            self.assertRejected(text, "tou_period_invalid_format")

    def test_duplicate_days(self):
        self.assertRejected("00:00-06:00/11/+", "tou_period_duplicate_days")
        self.assertRejected("00:00-06:00/1234561/+", "tou_period_duplicate_days")

    def test_start_not_before_end(self):
        self.assertRejected("06:00-06:00/1/+", "tou_period_start_not_before_end")
        self.assertRejected("22:00-02:00/1/+", "tou_period_start_not_before_end")

    def test_oversized_rejected_before_regex(self):
        with self.assertRaises(TOU.TouPeriodError) as ctx:
            TOU.parse_period_text("0" * 100000)
        self.assertEqual(ctx.exception.translation_key, "tou_period_too_long")
        self.assertEqual(ctx.exception.placeholders["max"],
                         str(TOU.RAW_INPUT_MAX_LENGTH))

    def test_non_string(self):
        for value in (None, 5, b"00:00-06:00/1/+"):
            self.assertRejected(value, "tou_period_invalid_format")

    def test_placeholders_are_strings(self):
        with self.assertRaises(TOU.TouPeriodError) as ctx:
            TOU.parse_period_text("06:00-05:00/1/+")
        self.assertTrue(all(isinstance(v, str) for v in ctx.exception.placeholders.values()))


class TestFormatDeviceValues(unittest.TestCase):
    def test_faithful_for_values_refused_on_input(self):
        self.assertEqual(TOU.format_period(P(0, 1440)), "00:00-24:00/1234567/+")
        self.assertEqual(TOU.format_period(P(60, 120, days=(False,) * 7,
                                             flag=ChargeFlag.DISCHARGE)),
                         "01:00-02:00//-")

    def test_matches_sensor_rendering(self):
        """Same text as sensor.py's read-only TOU sensor attributes."""
        src = (_ROOT / "sensor.py").read_text()
        self.assertIn("f\"/{'+' if period.charge_flag == ChargeFlag.CHARGE else '-'}\"", src)
        self.assertIn("if days[(i + 1) % 7]:", src)


class TestValidatePeriods(unittest.TestCase):
    def assertRejected(self, periods, key):
        with self.assertRaises(TOU.TouPeriodError) as ctx:
            TOU.validate_periods(periods)
        self.assertEqual(ctx.exception.translation_key, key)
        return ctx.exception

    def test_empty_ok(self):
        TOU.validate_periods([])

    def test_full_schedule_ok(self):
        TOU.validate_periods([P(i * 60, i * 60 + 30) for i in range(14)])

    def test_too_many(self):
        err = self.assertRejected([P(i * 60, i * 60 + 30) for i in range(15)],
                                  "tou_period_too_many")
        self.assertEqual(err.placeholders, {"count": "15", "max": "14"})

    def test_overlap_names_slots_and_day(self):
        mon = (False, True, False, False, False, False, False)
        err = self.assertRejected(
            [P(0, 60), P(600, 700, days=mon), P(650, 800, days=mon)],
            "tou_period_overlap",
        )
        self.assertEqual(err.placeholders, {"slot_a": "2", "slot_b": "3", "day": "Monday"})

    def test_overlap_sunday_label(self):
        sun = (True, False, False, False, False, False, False)
        err = self.assertRejected([P(0, 100, days=sun), P(50, 60, days=sun)],
                                  "tou_period_overlap")
        self.assertEqual(err.placeholders["day"], "Sunday")

    def test_contained_period_is_overlap(self):
        self.assertRejected([P(0, 600), P(60, 120)], "tou_period_overlap")

    def test_same_time_different_days_ok(self):
        mon = (False, True, False, False, False, False, False)
        tue = (False, False, True, False, False, False, False)
        TOU.validate_periods([P(0, 600, days=mon), P(0, 600, days=tue)])

    def test_touching_periods_ok(self):
        TOU.validate_periods([P(0, 360), P(360, 720)])

    def test_bad_device_style_values(self):
        self.assertRejected([P(600, 600)], "tou_period_start_not_before_end")
        self.assertRejected([P(0, 1441)], "tou_period_start_not_before_end")
        self.assertRejected([P(-1, 10)], "tou_period_start_not_before_end")

    def test_end_2400_accepted_like_library(self):
        TOU.validate_periods([P(0, 1440)])

    def test_wrong_type(self):
        self.assertRejected([P(0, 10), "00:00-06:00/1/+"], "tou_period_invalid_format")

    def test_equivalent_to_library_validator(self):
        """Seeded randomized check: validate_periods() accepts a schedule
        iff the library's own encode() does. Ours runs first; the
        library remains the independent second gate."""
        reg = REGISTERS[TOU_REG]
        rng = random.Random(230)
        checked = accepted = 0
        for _ in range(3000):
            n = rng.randint(0, 16)
            sched = []
            for _ in range(n):
                s = rng.randrange(0, 1441, 60)
                e = rng.randrange(0, 1441, 60)
                days = tuple(rng.random() < 0.3 for _ in range(7))
                flag = rng.choice([ChargeFlag.CHARGE, ChargeFlag.DISCHARGE])
                sched.append(P(s, e, days=days, flag=flag))
            try:
                TOU.validate_periods(sched)
                ours = True
            except TOU.TouPeriodError:
                ours = False
            try:
                reg.encode(sched)
                lib = True
            except TimeOfUsePeriodsException:
                lib = False
            self.assertEqual(ours, lib, sched)
            checked += 1
            accepted += ours
        # make sure both outcomes were actually exercised
        self.assertGreater(accepted, 50)
        self.assertLess(accepted, checked - 50)


class TestApplySlotEdit(unittest.TestCase):
    A, B, C, X = P(0, 60), P(120, 180), P(240, 300), P(600, 660)

    def test_replace(self):
        self.assertEqual(TOU.apply_slot_edit([self.A, self.B], 2, self.X), [self.A, self.X])

    def test_set_past_end_appends(self):
        self.assertEqual(TOU.apply_slot_edit([self.A, self.B], 9, self.X),
                         [self.A, self.B, self.X])
        self.assertEqual(TOU.apply_slot_edit([], 1, self.X), [self.X])

    def test_clear_shifts_later_periods_up(self):
        self.assertEqual(TOU.apply_slot_edit([self.A, self.B, self.C], 1, None),
                         [self.B, self.C])

    def test_clear_empty_slot_is_noop(self):
        self.assertEqual(TOU.apply_slot_edit([self.A], 5, None), [self.A])

    def test_does_not_mutate_input(self):
        current = [self.A, self.B]
        TOU.apply_slot_edit(current, 1, None)
        TOU.apply_slot_edit(current, 1, self.X)
        TOU.apply_slot_edit(current, 3, self.X)
        self.assertEqual(current, [self.A, self.B])

    def test_invalid_slots(self):
        for slot in (0, -1, 15, True, "1", 1.0, None):
            with self.assertRaises(TOU.TouPeriodError) as ctx:
                TOU.apply_slot_edit([self.A], slot, self.X)
            self.assertEqual(ctx.exception.translation_key, "tou_period_invalid_slot")

    def test_full_schedule_edits_never_grow_past_limit(self):
        """With 14 periods present, every slot (1..14) exists, so a set
        is always a replacement -- the entity cannot produce a 15th."""
        full = [P(i * 60, i * 60 + 30) for i in range(14)]
        for slot in range(1, 15):
            self.assertEqual(len(TOU.apply_slot_edit(full, slot, self.X)), 14)


# ═══════════════════════════════════════════════════════════════════════
# Part B — text.py (platform)
# ═══════════════════════════════════════════════════════════════════════


class FakeSun2000:
    def __init__(self, serial="INV001", battery_type=rv.StorageProductModel.HUAWEI_LUNA2000):
        self.serial_number = serial
        self.battery_type = battery_type
        self.stored = [P(0, 360)]
        self.events: list[tuple] = []
        self.guard = None
        self.set_result = True
        self.get_exc: BaseException | None = None
        self.set_exc: BaseException | None = None
        self.get_delay = 0.0
        self.get_value_override = "unset"

    async def get(self, name):
        self.events.append(("get", name, self.guard.active))
        if self.get_delay:
            await asyncio.sleep(self.get_delay)
        if self.get_exc:
            raise self.get_exc
        value = list(self.stored) if self.get_value_override == "unset" else self.get_value_override
        return types.SimpleNamespace(value=value)

    async def set(self, name, value):
        self.events.append(("set", name, self.guard.active, list(value)))
        if self.set_exc:
            raise self.set_exc
        if self.set_result:
            # the real library validates at encode time, before any write
            REGISTERS[name].encode(value)
            self.stored = list(value)
        return self.set_result


class FakeEmma:
    serial_number = "EMMA01"


class FakeGuard:
    def __init__(self):
        self.active = 0
        self.labels: list[str] = []

    @contextlib.asynccontextmanager
    async def request(self, label=None, **_):
        self.labels.append(label)
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1


class FakeQuality:
    name = "GOOD"


class FakeCoordinator:
    def __init__(self, device, data_periods=None):
        self.guard = FakeGuard()
        device.guard = self.guard
        self.last_update_success = True
        self.data = (
            {TOU_REG: types.SimpleNamespace(value=data_periods)}
            if data_periods is not None else {}
        )
        self.cache = types.SimpleNamespace(quality_of=lambda k: (FakeQuality(), None, 1.23))
        self.invalidated: list = []
        self.verifications: list = []
        self.refreshes = 0

    def invalidate_cache(self, name):
        self.invalidated.append(name)

    def schedule_verify_write(self, name, value):
        self.verifications.append((name, list(value)))

    async def async_request_refresh(self):
        self.refreshes += 1


# Swap device classes for eligibility checks (text.py uses isinstance).
TEXT.SUN2000Device = FakeSun2000
TEXT.EMMADevice = FakeEmma


def _inverter_data(device, coordinator, *, storage=True, config=True):
    return TYPES.HuaweiSolarInverterData(
        device=device,
        device_info={"name": "inv"},
        update_coordinator=coordinator,
        configuration_update_coordinator=coordinator if config else None,
        power_meter=None,
        connected_energy_storage={"name": "battery"} if storage else None,
        battery_1=None,
        battery_2=None,
        optimizer_device_infos=None,
        power_meter_update_coordinator=None,
        energy_storage_update_coordinator=None,
        optimizer_update_coordinator=None,
    )


def _entry(device_datas, param_config=True):
    return types.SimpleNamespace(
        data={CONST.CONF_ENABLE_PARAMETER_CONFIGURATION: param_config},
        runtime_data={CONST.DATA_DEVICE_DATAS: device_datas},
    )


def _setup(device_datas, param_config=True):
    added: list = []
    _run(TEXT.async_setup_entry(None, _entry(device_datas, param_config), added.extend))
    return added


def _entity(slot=1, stored=None, data=None):
    dev = FakeSun2000()
    if stored is not None:
        dev.stored = list(stored)
    coord = FakeCoordinator(dev, data if data is not None else list(dev.stored))
    ent = TEXT.HuaweiSolarTOUPeriodTextEntity(coord, dev, {"name": "battery"}, slot)
    return ent, dev, coord


class TestSetupEligibility(unittest.TestCase):
    def _dd(self, **kw):
        dev = FakeSun2000(battery_type=kw.pop("battery_type", rv.StorageProductModel.HUAWEI_LUNA2000))
        return _inverter_data(dev, FakeCoordinator(dev, []), **kw)

    def test_eligible_creates_fourteen_slots(self):
        ents = _setup([self._dd()])
        self.assertEqual([e.slot for e in ents], list(range(1, 15)))
        self.assertEqual(len({e._attr_unique_id for e in ents}), 14)
        self.assertEqual(ents[0]._attr_unique_id, f"INV001_{TOU_REG}_slot_1")

    def test_visibility_defaults(self):
        ents = _setup([self._dd()])
        visible = [e.slot for e in ents if e._attr_entity_registry_visible_default]
        self.assertEqual(visible, [1, 2, 3, 4])

    def test_entity_presentation(self):
        e = _setup([self._dd()])[6]
        self.assertEqual(e._attr_entity_category, "config")
        self.assertEqual(e._attr_translation_key, "tou_period")
        self.assertEqual(e._attr_translation_placeholders, {"slot": "7"})
        self.assertEqual(e._attr_native_max, 21)
        self.assertEqual(e._attr_native_min, 0)
        self.assertEqual(e._attr_device_info, {"name": "battery"})
        self.assertFalse(hasattr(e, "_attr_pattern"))

    def test_not_gated_on_working_mode(self):
        src = (_ROOT / "text.py").read_text()
        code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
        self.assertNotIn("STORAGE_WORKING_MODE", code)

    def test_parameter_configuration_disabled(self):
        self.assertEqual(_setup([self._dd()], param_config=False), [])

    def test_emma_anywhere_in_entry_disables(self):
        emma = types.SimpleNamespace(device=FakeEmma())
        self.assertEqual(_setup([self._dd(), emma]), [])

    def test_lg_resu_excluded(self):
        self.assertEqual(_setup([self._dd(battery_type=rv.StorageProductModel.LG_RESU)]), [])

    def test_no_battery_type_excluded(self):
        self.assertEqual(_setup([self._dd(battery_type=rv.StorageProductModel.NONE)]), [])

    def test_no_connected_storage_excluded(self):
        self.assertEqual(_setup([self._dd(storage=False)]), [])

    def test_no_configuration_coordinator_excluded(self):
        self.assertEqual(_setup([self._dd(config=False)]), [])

    def test_non_inverter_device_data_excluded(self):
        plain = types.SimpleNamespace(device=FakeSun2000())
        self.assertEqual(_setup([plain]), [])

    def test_two_inverters_each_get_slots(self):
        d1 = self._dd()
        dev2 = FakeSun2000(serial="INV002")
        d2 = _inverter_data(dev2, FakeCoordinator(dev2, []))
        ents = _setup([d1, d2])
        self.assertEqual(len(ents), 28)
        self.assertEqual(len({e._attr_unique_id for e in ents}), 28)

    def test_constructor_rejects_bad_slot(self):
        dev = FakeSun2000()
        for slot in (0, 15):
            with self.assertRaises(ValueError):
                TEXT.HuaweiSolarTOUPeriodTextEntity(FakeCoordinator(dev), dev, {}, slot)


class TestDisplay(unittest.TestCase):
    def test_slots_render_from_coordinator(self):
        periods = [P(0, 360), P(17 * 60, 21 * 60, days=(False, True, True, True, True, True, False),
                                flag=ChargeFlag.DISCHARGE)]
        texts = []
        for slot in (1, 2, 3):
            ent, _, _ = _entity(slot, data=periods)
            ent._handle_coordinator_update()
            self.assertTrue(ent.available)
            texts.append(ent.native_value)
        self.assertEqual(texts, ["00:00-06:00/1234567/+", "17:00-21:00/12345/-", ""])

    def test_attributes(self):
        ent, _, _ = _entity(2, data=[P(0, 60)])
        ent._handle_coordinator_update()
        attrs = ent._attr_extra_state_attributes
        self.assertEqual(attrs["slot"], 2)
        self.assertEqual(attrs["configured_periods"], 1)
        self.assertEqual(attrs["data_quality"], "good")

    def test_device_value_refused_on_input_is_still_shown(self):
        ent, _, _ = _entity(1, data=[P(0, 1440)])
        ent._handle_coordinator_update()
        self.assertEqual(ent.native_value, "00:00-24:00/1234567/+")

    def test_undisplayable_value_becomes_unknown(self):
        ent, _, _ = _entity(1, data=[P(0, 100000)])
        ent._handle_coordinator_update()
        self.assertIsNone(ent.native_value)
        self.assertTrue(ent.available)

    def test_garbage_entry_becomes_unknown(self):
        ent, _, _ = _entity(1, data=["garbage"])
        ent._handle_coordinator_update()
        self.assertIsNone(ent.native_value)

    def test_register_missing_is_unavailable(self):
        ent, _, coord = _entity(1)
        coord.data = {}
        ent._handle_coordinator_update()
        self.assertFalse(ent.available)
        self.assertIsNone(ent.native_value)

    def test_non_list_value_is_unavailable(self):
        ent, _, coord = _entity(1)
        coord.data = {TOU_REG: types.SimpleNamespace(value=None)}
        ent._handle_coordinator_update()
        self.assertFalse(ent.available)

    def test_coordinator_failure_is_unavailable_even_with_data(self):
        ent, _, coord = _entity(1, data=[P(0, 60)])
        ent._handle_coordinator_update()
        coord.last_update_success = False
        self.assertFalse(ent.available)

    def test_unavailable_before_first_update(self):
        ent, _, _ = _entity(1)
        self.assertFalse(ent.available)

    def test_state_written_on_update(self):
        ent, _, _ = _entity(1)
        ent._handle_coordinator_update()
        self.assertEqual(ent._state_writes, 1)


class TestWritePath(unittest.TestCase):
    def test_replace_slot(self):
        ent, dev, coord = _entity(1, stored=[P(0, 360), P(600, 660)])
        _run(ent.async_set_value("01:00-05:00/12345/-"))
        new = [TOU.parse_period_text("01:00-05:00/12345/-"), P(600, 660)]
        self.assertEqual(dev.stored, new)
        self.assertEqual(coord.invalidated, [TOU_REG])
        self.assertEqual(coord.verifications, [(TOU_REG, new)])
        self.assertEqual(coord.refreshes, 1)
        self.assertEqual(ent.native_value, "01:00-05:00/12345/-")

    def test_read_and_write_share_one_guard_hold(self):
        ent, dev, coord = _entity(1)
        _run(ent.async_set_value("01:00-05:00/1/+"))
        self.assertEqual(coord.guard.labels, ["tou_period_write"])
        kinds = [(e[0], e[2]) for e in dev.events]
        self.assertEqual(kinds, [("get", 1), ("set", 1)])

    def test_uses_fresh_device_read_not_stale_coordinator_data(self):
        """Lost-update protection: coordinator still shows [A] but the
        device now holds [A, B] (e.g. edited in FusionSolar)."""
        a, b = P(0, 60), P(600, 660)
        ent, dev, _ = _entity(3, stored=[a, b], data=[a])
        _run(ent.async_set_value("20:00-21:00/1/+"))
        self.assertEqual(dev.stored, [a, b, TOU.parse_period_text("20:00-21:00/1/+")])

    def test_append_past_end(self):
        ent, dev, _ = _entity(10, stored=[P(0, 60)])
        _run(ent.async_set_value("02:00-03:00/1/+"))
        self.assertEqual(len(dev.stored), 2)
        # this entity's slot (10) is still empty after an append
        self.assertEqual(ent.native_value, "")

    def test_clear_slot(self):
        a, b = P(0, 60), P(600, 660)
        ent, dev, coord = _entity(1, stored=[a, b])
        _run(ent.async_set_value(""))
        self.assertEqual(dev.stored, [b])
        self.assertEqual(ent.native_value, "10:00-11:00/1234567/+")
        self.assertEqual(coord.refreshes, 1)

    def test_clear_all_writes_empty_schedule(self):
        ent, dev, _ = _entity(1, stored=[P(0, 60)])
        _run(ent.async_set_value("  "))
        self.assertEqual(dev.stored, [])
        self.assertEqual(dev.events[-1][0], "set")

    def test_unchanged_value_issues_no_write(self):
        ent, dev, coord = _entity(1, stored=[P(0, 360)])
        _run(ent.async_set_value("00:00-06:00/1234567/+"))
        self.assertEqual([e[0] for e in dev.events], ["get"])
        self.assertEqual((coord.invalidated, coord.verifications, coord.refreshes), ([], [], 0))

    def test_clear_empty_slot_issues_no_write(self):
        ent, dev, coord = _entity(5, stored=[P(0, 360)])
        _run(ent.async_set_value(""))
        self.assertEqual([e[0] for e in dev.events], ["get"])
        self.assertEqual(coord.refreshes, 0)

    def test_invalid_text_rejected_before_any_bus_traffic(self):
        ent, dev, coord = _entity(1)
        with self.assertRaises(HAExc.ServiceValidationError) as ctx:
            _run(ent.async_set_value("06:00-05:00/1/+"))
        self.assertEqual(ctx.exception.translation_key, "tou_period_start_not_before_end")
        self.assertEqual(ctx.exception.translation_domain, "huawei_solar")
        self.assertEqual(dev.events, [])
        self.assertEqual(coord.guard.labels, [])

    def test_invalid_text_does_not_wait_for_write_lock(self):
        async def scenario():
            ent, dev, _ = _entity(1)
            lock = TYPES.get_device_write_lock(dev.serial_number)
            async with lock:
                with self.assertRaises(HAExc.ServiceValidationError):
                    await asyncio.wait_for(ent.async_set_value("bad"), 1)
        _run(scenario())

    def test_overlap_rejected_nothing_written(self):
        ent, dev, coord = _entity(2, stored=[P(0, 360)])
        with self.assertRaises(HAExc.ServiceValidationError) as ctx:
            _run(ent.async_set_value("05:00-07:00/1/+"))
        self.assertEqual(ctx.exception.translation_key, "tou_period_overlap")
        self.assertEqual(ctx.exception.translation_placeholders["slot_a"], "1")
        self.assertEqual([e[0] for e in dev.events], ["get"])
        self.assertEqual(dev.stored, [P(0, 360)])
        self.assertEqual((coord.invalidated, coord.refreshes), ([], 0))

    def test_full_schedule_slot_replace(self):
        full = [P(i * 60, i * 60 + 30) for i in range(14)]
        ent, dev, _ = _entity(14, stored=full)
        _run(ent.async_set_value("23:00-23:30/1/+"))
        self.assertEqual(len(dev.stored), 14)
        self.assertEqual(dev.stored[13], TOU.parse_period_text("23:00-23:30/1/+"))

    def test_write_returning_false(self):
        ent, dev, coord = _entity(1)
        dev.set_result = False
        with self.assertRaises(HAExc.HomeAssistantError) as ctx:
            _run(ent.async_set_value("01:00-02:00/1/+"))
        self.assertNotIsInstance(ctx.exception, HAExc.ServiceValidationError)
        self.assertEqual(ctx.exception.translation_key, "tou_period_write_rejected")
        self.assertEqual((coord.invalidated, coord.verifications, coord.refreshes), ([], [], 0))

    def test_read_returning_non_list(self):
        ent, dev, coord = _entity(1)
        dev.get_value_override = None
        with self.assertRaises(HAExc.HomeAssistantError) as ctx:
            _run(ent.async_set_value("01:00-02:00/1/+"))
        self.assertEqual(ctx.exception.translation_key, "tou_period_read_failed")
        self.assertEqual([e[0] for e in dev.events], ["get"])

    def test_read_library_error(self):
        ent, dev, _ = _entity(1)
        dev.get_exc = HuaweiSolarException("boom")
        with self.assertRaises(HAExc.HomeAssistantError) as ctx:
            _run(ent.async_set_value("01:00-02:00/1/+"))
        self.assertEqual(ctx.exception.translation_key, "tou_period_device_error")
        self.assertEqual([e[0] for e in dev.events], ["get"])

    def test_write_library_error(self):
        ent, dev, coord = _entity(1)
        dev.set_exc = HuaweiSolarException("boom")
        with self.assertRaises(HAExc.HomeAssistantError) as ctx:
            _run(ent.async_set_value("01:00-02:00/1/+"))
        self.assertEqual(ctx.exception.translation_key, "tou_period_device_error")
        self.assertEqual(ctx.exception.translation_placeholders["error"], "HuaweiSolarException")
        self.assertEqual(coord.refreshes, 0)

    def test_library_validation_is_second_gate(self):
        """Even if local validation were bypassed, the library's own
        encode() rejects an invalid schedule before anything is stored."""
        ent, dev, coord = _entity(2, stored=[P(0, 360)])
        original = TEXT.validate_periods
        TEXT.validate_periods = lambda periods: None
        try:
            with self.assertRaises(HAExc.HomeAssistantError) as ctx:
                _run(ent.async_set_value("05:00-07:00/1/+"))
        finally:
            TEXT.validate_periods = original
        self.assertEqual(ctx.exception.translation_key, "tou_period_device_error")
        self.assertEqual(ctx.exception.translation_placeholders["error"],
                         "TimeOfUsePeriodsException")
        self.assertEqual(dev.stored, [P(0, 360)])
        self.assertEqual(coord.refreshes, 0)

    def test_timeout_bounds_whole_sequence(self):
        ent, dev, coord = _entity(1)
        dev.get_delay = 5
        original = TYPES.WRITE_SEQUENCE_TIMEOUT
        TYPES.WRITE_SEQUENCE_TIMEOUT = timedelta(seconds=0.05)
        try:
            with self.assertRaises(HAExc.HomeAssistantError) as ctx:
                _run(ent.async_set_value("01:00-02:00/1/+"))
        finally:
            TYPES.WRITE_SEQUENCE_TIMEOUT = original
        self.assertEqual(ctx.exception.translation_key, "tou_period_timeout")
        self.assertEqual([e[0] for e in dev.events], ["get"])
        self.assertEqual(coord.guard.active, 0)  # guard released

    def test_guard_admission_timeout_mapped(self):
        ent, dev, coord = _entity(1)

        @contextlib.asynccontextmanager
        async def refusing(label=None, **_):
            raise asyncio.TimeoutError("queue shed")
            yield  # pragma: no cover

        coord.guard.request = refusing
        with self.assertRaises(HAExc.HomeAssistantError) as ctx:
            _run(ent.async_set_value("01:00-02:00/1/+"))
        self.assertEqual(ctx.exception.translation_key, "tou_period_timeout")
        self.assertEqual(dev.events, [])

    def test_shared_write_lock_serialises_with_services(self):
        """The entity must wait for the SAME per-serial lock services.py
        uses (services._get_device_write_lock delegates to it)."""
        async def scenario():
            ent, dev, _ = _entity(1)
            lock = TYPES.get_device_write_lock(dev.serial_number)
            await lock.acquire()
            task = asyncio.ensure_future(ent.async_set_value("01:00-02:00/1/+"))
            await asyncio.sleep(0.05)
            self.assertEqual(dev.events, [])  # blocked on the lock
            lock.release()
            await asyncio.wait_for(task, 1)
            self.assertEqual([e[0] for e in dev.events], ["get", "set"])
        _run(scenario())
        src = (_ROOT / "services.py").read_text()
        self.assertIn("return get_device_write_lock(serial_number)", src)

    def test_concurrent_slot_edits_do_not_lose_updates(self):
        async def scenario():
            dev = FakeSun2000(serial="INVCONC")
            dev.stored = []
            coord = FakeCoordinator(dev, [])
            ents = [TEXT.HuaweiSolarTOUPeriodTextEntity(coord, dev, {}, s) for s in (1, 2, 3)]
            await asyncio.gather(
                ents[0].async_set_value("01:00-02:00/1/+"),
                ents[1].async_set_value("03:00-04:00/1/+"),
                ents[2].async_set_value("05:00-06:00/1/+"),
            )
            return dev
        dev = _run(scenario())
        self.assertEqual(len(dev.stored), 3)
        self.assertEqual(sorted(p.start_time for p in dev.stored), [60, 180, 300])

    def test_write_lock_released_after_failure(self):
        async def scenario():
            ent, dev, _ = _entity(1)
            dev.set_result = False
            with self.assertRaises(HAExc.HomeAssistantError):
                await ent.async_set_value("01:00-02:00/1/+")
            self.assertFalse(TYPES.get_device_write_lock(dev.serial_number).locked())
        _run(scenario())

    def test_source_ordering_validate_before_lock(self):
        src = (_ROOT / "text.py").read_text()
        body = src[src.index("async def async_set_value"):]
        self.assertLess(body.index("parse_period_text(value)"),
                        body.index("get_device_write_lock(serial)"))
        self.assertLess(body.index("self.device.get(TOU_REGISTER)"),
                        body.index("validate_periods(updated)"))
        self.assertLess(body.index("validate_periods(updated)"),
                        body.index("await write(self.device, TOU_REGISTER, updated)"))
        # never a raw, unguarded set() call
        self.assertNotIn("self.device.set(", src)


# ═══════════════════════════════════════════════════════════════════════
# Part C — HS-230-001: registered set_tou_periods schema length cap
# ═══════════════════════════════════════════════════════════════════════


def _extract(source: str, names: list[str], extra_globals: dict) -> types.SimpleNamespace:
    tree = ast.parse(source)
    nodes = []
    for node in tree.body:
        name = None
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            name = node.name
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1
              and isinstance(node.targets[0], ast.Name)):
            name = node.targets[0].id
        if name in names:
            nodes.append(node)
    code = compile(ast.Module(body=nodes, type_ignores=[]), "<extracted>", "exec")
    ns = dict(extra_globals)
    exec(code, ns)  # noqa: S102 -- test-only, known source
    return types.SimpleNamespace(**{n: ns[n] for n in names})


class TestHS230001DispatchSchemaLength(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import voluptuous as vol

        def _cv_string(v):
            if not isinstance(v, str):
                raise vol.Invalid("not a string")
            return v

        cls.vol = vol
        cls.ns = _extract(
            (_ROOT / "services.py").read_text(),
            ["_TIME", "MAX_PERIODS_STRING_LENGTH", "HUAWEI_LUNA2000_TOU_PATTERN",
             "LG_RESU_TOU_PATTERN", "TOU_PERIODS_DISPATCH_SCHEMA"],
            {"vol": vol, "cv": types.SimpleNamespace(string=_cv_string),
             "DATA_DEVICE_ID": "device_id", "DATA_PERIODS": "periods"},
        )

    def test_dispatch_schema_is_the_registered_one(self):
        src = (_ROOT / "services.py").read_text()
        idx = src.index("SERVICE_SET_TOU_PERIODS,\n            set_tou_periods_dispatch,")
        self.assertIn("schema=TOU_PERIODS_DISPATCH_SCHEMA", src[idx: idx + 200])

    def test_giant_string_rejected_by_length(self):
        with self.assertRaises(self.vol.Invalid) as ctx:
            self.ns.TOU_PERIODS_DISPATCH_SCHEMA(
                {"device_id": "x", "periods": "00:00-23:59/1234567/+\n" * 10000}
            )
        self.assertIn("length", str(ctx.exception).lower())

    def test_normal_values_still_accepted(self):
        for periods in ("", "00:00-06:00/1234567/+\n17:00-21:00/12345/-",
                        "00:00-06:00/0.25\n"):
            out = self.ns.TOU_PERIODS_DISPATCH_SCHEMA({"device_id": "x", "periods": periods})
            self.assertEqual(out["periods"], periods)

    def test_length_checked_before_match(self):
        src = (_ROOT / "services.py").read_text()
        block = src[src.index("TOU_PERIODS_DISPATCH_SCHEMA = vol.Schema("):]
        block = block[: block.index("\n)\n")]
        self.assertLess(block.index("vol.Length(max=MAX_PERIODS_STRING_LENGTH)"),
                        block.index("vol.Match("))


# ═══════════════════════════════════════════════════════════════════════
# Part D — static contracts
# ═══════════════════════════════════════════════════════════════════════


def _used_translation_keys() -> dict[str, set[str]]:
    """translation key -> placeholder names, harvested from source."""
    keys: dict[str, set[str]] = {}
    for fname in ("text.py", "tou_periods.py"):
        tree = ast.parse((_ROOT / fname).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            fname_ = getattr(func, "id", getattr(func, "attr", ""))
            if fname_ == "TouPeriodError" and node.args and isinstance(node.args[0], ast.Constant):
                keys.setdefault(node.args[0].value, set()).update(
                    kw.arg for kw in node.keywords
                )
            for kw in node.keywords:
                if kw.arg == "translation_key" and isinstance(kw.value, ast.Constant):
                    ph = set()
                    for kw2 in node.keywords:
                        if kw2.arg == "translation_placeholders" and isinstance(kw2.value, ast.Dict):
                            ph = {k.value for k in kw2.value.keys}
                    keys.setdefault(kw.value.value, set()).update(ph)
    return keys


class TestStaticContracts(unittest.TestCase):
    def test_platform_registered(self):
        src = (_ROOT / "__init__.py").read_text()
        block = src[src.index("PLATFORMS: list[Platform] = ["):]
        block = block[: block.index("\n]")]
        self.assertIn("Platform.TEXT,", block)

    def test_manifest_version(self):
        manifest = json.loads((_ROOT / "manifest.json").read_text())
        self.assertEqual(manifest["version"], "2.3.0.1")

    def test_translation_files_contain_all_keys_and_placeholders(self):
        used = _used_translation_keys()
        used.pop("tou_period", None)  # entity name, checked separately
        self.assertGreaterEqual(len(used), 10)
        for name in ("strings.json", "translations/en.json"):
            data = json.loads((_ROOT / name).read_text(encoding="utf-8"))
            for key, placeholders in used.items():
                self.assertIn(key, data["exceptions"], f"{name}: {key}")
                message = data["exceptions"][key]["message"]
                in_message = set(re.findall(r"{(\w+)}", message))
                self.assertEqual(in_message, placeholders, f"{name}: {key}")

    def test_entity_name_translation(self):
        for name in ("strings.json", "translations/en.json"):
            data = json.loads((_ROOT / name).read_text(encoding="utf-8"))
            self.assertEqual(data["entity"]["text"]["tou_period"]["name"], "TOU period {slot}")

    def test_strings_and_en_identical_for_new_keys(self):
        s = json.loads((_ROOT / "strings.json").read_text(encoding="utf-8"))
        e = json.loads((_ROOT / "translations/en.json").read_text(encoding="utf-8"))
        self.assertEqual(s["entity"]["text"], e["entity"]["text"])
        for key in _used_translation_keys():
            if key in s["exceptions"]:
                self.assertEqual(s["exceptions"][key], e["exceptions"][key])


if __name__ == "__main__":
    unittest.main()
