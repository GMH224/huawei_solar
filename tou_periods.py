"""Pure helpers for editing Huawei LUNA2000 time-of-use (TOU) periods
one slot at a time.

v2.3.0.0 (new module). Backs text.py's per-slot "TOU period N" entities.
Deliberately has NO Home Assistant imports and NO bus access: everything
here is a pure function of its arguments, so the whole grammar /
validation / slot-edit contract is unit-testable without any HA runtime
or device, and the only code that ever touches the inverter stays in
text.py, behind the same ModbusGuard + write-lock + deadline discipline
every other write path in this integration uses.

Text format of ONE period (same grammar as one line of the existing
``set_tou_periods`` service, so users only ever learn one format):

    HH:MM-HH:MM/DAYS/FLAG      e.g.  00:00-06:00/1234567/+

* ``HH:MM`` -- 00:00 to 23:59, zero-padded, ASCII digits only.
  ``24:00`` is rejected on input, matching services.py's own ``_TIME``
  pattern (a deliberate earlier project decision); use ``23:59``.
* ``DAYS``  -- one to seven distinct digits, 1 = Monday ... 7 = Sunday,
  any order. Displayed back in ascending order.
* ``FLAG``  -- ``+`` charge, ``-`` discharge.
* An empty string (after trimming surrounding whitespace) means
  "remove the period in this slot".

Why per-slot, not one text entity holding the whole schedule: Home
Assistant hard-caps every entity state at 255 characters
(``homeassistant.const.MAX_LENGTH_STATE_STATE``, confirmed against the
real ``homeassistant.components.text`` source), while a full LUNA2000
schedule (14 periods x 21 characters + separators) needs up to ~307. A
single entity could not faithfully display a legitimately full
schedule; fourteen short entities always can.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from huawei_solar.register_definitions.periods import (
    ChargeFlag,
    HUAWEI_LUNA2000_TimeOfUsePeriod,
)

#: Number of TOU period slots exposed. Fixed (not read from the library)
#: so entity unique_ids stay stable even if a future library version
#: changes its own constant; tests assert the two agree, and the
#: library's own encode() independently rejects more periods than it
#: supports, so a mismatch can never produce an oversized write.
MAX_TOU_PERIODS = 14

#: Slots shown on the device page by default. The rest are created
#: enabled but hidden (Settings -> Entities can unhide them), which keeps
#: the battery device page readable for the common 1-4 period schedule
#: without making slots 5-14 unreachable.
DEFAULT_VISIBLE_TOU_SLOTS = 4

#: Exact length of the longest valid period text:
#: len("00:00-06:00/1234567/+") == 21.
PERIOD_TEXT_MAX_LENGTH = 21

#: Hard ceiling on raw input length, checked BEFORE any regex work
#: (same "reject oversized input before parsing" rule as HS-ICS-006).
#: Home Assistant's text platform already enforces the entity's
#: native_max (21) before our setter runs; this is the defence-in-depth
#: copy at the command sink, sized to tolerate surrounding whitespace.
RAW_INPUT_MAX_LENGTH = 64

_MINUTES_PER_DAY = 24 * 60

# Explicit [0-9] rather than \d: Python's \d also matches non-ASCII
# Unicode digits (e.g. Arabic-Indic), which int() would happily accept.
_TIME = r"(?:[01][0-9]|2[0-3]):[0-5][0-9]"
_PERIOD_RE = re.compile(
    rf"(?P<start>{_TIME})-(?P<end>{_TIME})/(?P<days>[1-7]{{1,7}})/(?P<flag>[+-])"
)

_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
              "Saturday", "Sunday")


class TouPeriodError(ValueError):
    """A TOU period (or resulting schedule) failed validation.

    ``translation_key`` / ``placeholders`` map 1:1 onto an entry in the
    ``exceptions`` section of strings.json, so text.py can surface the
    exact reason to the user as a ServiceValidationError without any
    string parsing.
    """

    def __init__(self, translation_key: str, **placeholders: Any) -> None:
        self.translation_key = translation_key
        self.placeholders = {k: str(v) for k, v in placeholders.items()}
        super().__init__(f"{translation_key}: {self.placeholders}")


# ── day-index conversion ─────────────────────────────────────────────────────
#
# The library's days_effective tuple is indexed Sunday=0 ... Saturday=6
# (see huawei_solar.register_definitions.periods._days_effective_parser,
# bit 0 = Sunday). The user-facing digits are ISO-style Monday=1 ...
# Sunday=7. `digit % 7` maps 7 -> 0 (Sunday) and 1..6 -> 1..6, exactly
# what services.py's own _parse_days_effective() and sensor.py's own
# _days_effective_to_str() already do -- kept identical on purpose.


def _days_to_tuple(days_text: str) -> tuple[bool, bool, bool, bool, bool, bool, bool]:
    days = [False] * 7
    for ch in days_text:
        days[int(ch) % 7] = True
    return tuple(days)  # type: ignore[return-value]


def _days_to_text(days: Sequence[bool]) -> str:
    return "".join(
        str(iso_day) for iso_day in range(1, 8) if days[iso_day % 7]
    )


def _minutes_to_text(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _text_to_minutes(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


# ── single period ────────────────────────────────────────────────────────────


def parse_period_text(text: str) -> HUAWEI_LUNA2000_TimeOfUsePeriod | None:
    """Parse one period. Returns None for "clear this slot".

    Raises TouPeriodError on anything that is not exactly one valid
    period. Never touches the device.
    """
    if not isinstance(text, str):
        raise TouPeriodError("tou_period_invalid_format", value=type(text).__name__)
    if len(text) > RAW_INPUT_MAX_LENGTH:
        raise TouPeriodError(
            "tou_period_too_long", length=len(text), max=RAW_INPUT_MAX_LENGTH
        )

    stripped = text.strip()
    if stripped == "":
        return None

    match = _PERIOD_RE.fullmatch(stripped)
    if match is None:
        raise TouPeriodError("tou_period_invalid_format", value=stripped)

    days_text = match.group("days")
    if len(set(days_text)) != len(days_text):
        raise TouPeriodError("tou_period_duplicate_days", value=stripped)

    start = _text_to_minutes(match.group("start"))
    end = _text_to_minutes(match.group("end"))
    if start >= end:
        raise TouPeriodError(
            "tou_period_start_not_before_end",
            start=match.group("start"), end=match.group("end"),
        )

    return HUAWEI_LUNA2000_TimeOfUsePeriod(
        start_time=start,
        end_time=end,
        charge_flag=(
            ChargeFlag.CHARGE if match.group("flag") == "+" else ChargeFlag.DISCHARGE
        ),
        days_effective=_days_to_tuple(days_text),
    )


def format_period(period: HUAWEI_LUNA2000_TimeOfUsePeriod) -> str:
    """Render a period read FROM the device.

    Faithful rather than normalising: a value the device holds but this
    module would refuse as input (e.g. an end time of 24:00 set from the
    FusionSolar app, or an empty day set) is still shown as-is, so the
    user sees what is actually configured. The charge flag rendering
    matches sensor.py's HuaweiSolarTOUSensorEntity exactly.
    """
    return (
        f"{_minutes_to_text(period.start_time)}-{_minutes_to_text(period.end_time)}"
        f"/{_days_to_text(period.days_effective)}"
        f"/{'+' if period.charge_flag == ChargeFlag.CHARGE else '-'}"
    )


# ── whole schedule ───────────────────────────────────────────────────────────


def validate_periods(periods: Sequence[HUAWEI_LUNA2000_TimeOfUsePeriod]) -> None:
    """Validate a complete schedule before it is written.

    Mirrors (and reports more precisely than) the library's own
    HUAWEI_LUNA2000_TimeOfUseRegisters._validate()/encode() checks,
    which still run afterwards as a second, independent gate inside
    device.set(). Checking here first means an invalid schedule is
    rejected with a slot-specific message and without any bus traffic
    beyond the read that produced ``periods``.
    """
    if len(periods) > MAX_TOU_PERIODS:
        raise TouPeriodError(
            "tou_period_too_many", count=len(periods), max=MAX_TOU_PERIODS
        )

    for idx, period in enumerate(periods, start=1):
        if not isinstance(period, HUAWEI_LUNA2000_TimeOfUsePeriod):
            raise TouPeriodError("tou_period_invalid_format", value=f"slot {idx}")
        if not (0 <= period.start_time < period.end_time <= _MINUTES_PER_DAY):
            raise TouPeriodError(
                "tou_period_start_not_before_end",
                start=_minutes_to_text(period.start_time),
                end=_minutes_to_text(period.end_time),
            )

    # Pairwise overlap per weekday. n <= 14, so O(n^2) is trivial, and a
    # pairwise check lets the error name BOTH offending slots.
    for day_idx in range(7):
        for i in range(len(periods)):
            a = periods[i]
            if not a.days_effective[day_idx]:
                continue
            for j in range(i + 1, len(periods)):
                b = periods[j]
                if not b.days_effective[day_idx]:
                    continue
                if a.start_time < b.end_time and b.start_time < a.end_time:
                    raise TouPeriodError(
                        "tou_period_overlap",
                        slot_a=i + 1,
                        slot_b=j + 1,
                        day=_DAY_NAMES[(day_idx - 1) % 7],
                    )


def apply_slot_edit(
    current: Sequence[HUAWEI_LUNA2000_TimeOfUsePeriod],
    slot: int,
    new_period: HUAWEI_LUNA2000_TimeOfUsePeriod | None,
) -> list[HUAWEI_LUNA2000_TimeOfUsePeriod]:
    """Return the schedule that results from editing one slot.

    ``slot`` is 1-based. The device stores a packed list (a count plus
    that many periods), so slots are positions in that list:

    * set an existing slot   -> replaced in place
    * set a slot past the end -> appended (becomes slot len+1)
    * clear an existing slot -> removed; later periods move up one slot
    * clear an empty slot    -> unchanged (no-op)

    Never mutates ``current``.
    """
    if isinstance(slot, bool) or not isinstance(slot, int) or not (
        1 <= slot <= MAX_TOU_PERIODS
    ):
        raise TouPeriodError("tou_period_invalid_slot", slot=slot, max=MAX_TOU_PERIODS)

    result = list(current)
    index = slot - 1

    if new_period is None:
        if index < len(result):
            del result[index]
        return result

    if index < len(result):
        result[index] = new_period
    else:
        result.append(new_period)
    return result
