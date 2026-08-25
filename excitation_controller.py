"""Experimental excitation controller for huawei_solar v2.0.15.

Purpose
-------
NOT a production optimization. This module exists to answer a data-
availability problem, not a control-quality problem: across every capture
analyzed this project (2.0.12 through 2.0.14, ~90h combined field data),
POLL_INTERVAL sat at its cold-start value (60s) 75-98% of the time and GAP
was pinned at its maximum (500ms) 46-98% of the time, depending on device.
TIMEOUT already showed real, usable natural variation (15-60s, 37-46
distinct values) and is therefore NOT deliberately excited here -- see
Section 6.3 and 7 of `Controller_Redesign_Exact_GAP_TIMEOUT_POLL_Data_
Findings.md` for the field evidence this module is built from.

Without deliberate variation, the load-based adaptive design in
`adaptive_controller_design.md` (Section 8, "the one component this design
cannot validate from existing data") remains a hypothesis consistent with
observational data, not something causally tested. This module makes GAP
and POLL vary on a bounded, logged, reversible schedule so the NEXT capture
can support that test -- it does not itself decide what the final policy
should be.

Design principles (all traced to the three source documents this release
implements):
  - Single-input excitation first: only one of {GAP, POLL} is perturbed at
    a time. Combined excitation (varying both together) is explicitly out
    of scope for this release -- see `_ExcitationMode.COMPLETE` below.
  - Deterministic, not random: the schedule is a fixed, reproducible
    sequence, not noise injection. Two runs of the same schedule visit the
    same levels in the same order.
  - Bounded within the ALREADY-validated operating envelope
    (ADAPTIVE_GAP_MIN/MAX, ADAPTIVE_POLL_MIN/MAX from const.py) -- this
    release does not introduce any new hard limit, it samples more
    thoroughly within limits 2.0.9 through 2.0.14 already established as
    safe.
  - TIMEOUT is intentionally NOT excited by a schedule in this release.
    It already has real, non-degenerate variation, and simulation work in
    `adaptive_controller_design.md` Section 7.1 found that several
    register classes (INTRINSIC, SATURATION_STATE_MASTER) need timeout
    >=25-35s to avoid spurious timeouts. A naive excitation schedule
    pushing TIMEOUT toward its 15s floor would risk directly reproducing
    that already-found-and-fixed regression. The floor for this release's
    OWN safety envelope is therefore raised to 30s project-wide (see
    EXCITATION_TIMEOUT_FLOOR_S below) as a blanket protection, even though
    no schedule in this release commands TIMEOUT at all.
  - Go/no-go per level, not a blind calendar: each level requires BOTH a
    minimum dwell time AND a minimum transaction count before advancing,
    and is continuously monitored for a safety-threshold breach that
    reverts to NORMAL and HALTS the schedule -- it does not silently
    retry or skip ahead. A human decides whether/how to resume, matching
    the explicit "no rollercoaster deployment" requirement this release
    was commissioned under.
  - The existing transition-detection safety net is never overridden.
    If the underlying AdaptiveModbusController reports `in_transition`,
    excitation defers to NORMAL for that cycle regardless of schedule
    state -- an active fault condition is never used as an excitation
    opportunity.
  - Excitation state persists across restarts (same Store mechanism as
    the rest of the adaptive controller) so a restart during a multi-day
    experiment does not silently restart the whole schedule from level 1.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
import logging
import time
from typing import Any

from .adaptive_modbus import AdaptiveParams
from .const import (
    ADAPTIVE_GAP_MAX,
    ADAPTIVE_GAP_MIN,
    ADAPTIVE_POLL_MAX,
    ADAPTIVE_POLL_MIN,
)

_LOGGER = logging.getLogger(__name__)

#: This release's own, additional safety floor for TIMEOUT -- see module
#: docstring. Applied even though no schedule in this release commands
#: TIMEOUT directly, as a defense against a future schedule revision
#: accidentally reintroducing the already-fixed spurious-timeout problem.
EXCITATION_TIMEOUT_FLOOR_S = 30.0

#: Minimum time AND minimum transaction count a level must accumulate
#: before the schedule is allowed to advance -- both conditions must be
#: met, matching the acceptance-criteria structure in
#: `Adaptive_Modbus_Controller_Experimental_Identification_Release.md`
#: Section 35 ("each level has sufficient observations").
_MIN_LEVEL_DWELL = timedelta(hours=4)
_MIN_LEVEL_TRANSACTIONS = 200

#: Go/no-go thresholds, evaluated over a rolling window of the most recent
#: transactions within the current level (see _GoNoGoMonitor). Exceeding
#: EITHER threshold triggers an immediate, latching revert to NORMAL.
_GONOGO_WINDOW_TRANSACTIONS = 100
_GONOGO_MAX_ERROR_RATE = 0.05
_GONOGO_MAX_TIMEOUT_RATE = 0.03


class ExcitationMode(Enum):
    """Which single input, if any, is currently being deliberately varied.

    Ordering here is deliberate and matches the schedule's own sequencing
    (GAP first, then POLL) -- see _DEFAULT_SCHEDULE. TIMEOUT has no
    corresponding mode in this release (module docstring).
    """
    NORMAL = "NORMAL"
    EXCITE_GAP = "EXCITE_GAP"
    EXCITE_POLL = "EXCITE_POLL"
    #: Terminal state once every level of every mode has completed its
    #: full dwell+count requirement without a go/no-go revert. Behaves
    #: identically to NORMAL (no override applied) but is reported
    #: separately in telemetry so "the experiment finished cleanly" is
    #: distinguishable from "the experiment is still in NORMAL between
    #: levels" or "the experiment was manually halted before completion".
    COMPLETE = "COMPLETE"
    #: Latched by the go/no-go monitor (see _GoNoGoMonitor.evaluate).
    #: Behaves identically to NORMAL. Does NOT auto-resume -- clearing
    #: this state requires an explicit call to resume_after_halt(),
    #: which is a deliberate, logged, human-initiated action, not
    #: something this module does on its own on any timer or schedule.
    HALTED = "HALTED"


@dataclass(frozen=True)
class ExcitationLevel:
    """One commanded value within one mode's own schedule."""
    value: float  # ms for GAP, seconds for POLL
    label: str    # human-readable, for logging/telemetry only


@dataclass(frozen=True)
class ExcitationScheduleEntry:
    mode: ExcitationMode
    levels: tuple[ExcitationLevel, ...]


def _build_default_schedule() -> tuple[ExcitationScheduleEntry, ...]:
    """The actual 2.0.15 excitation schedule.

    GAP levels span the full existing ADAPTIVE_GAP_MIN..MAX envelope
    (150-500ms) -- this range is already narrow and already validated, so
    no further restriction is applied.

    POLL levels are deliberately NOT the full ADAPTIVE_POLL_MIN..MAX
    envelope (20-180s). They are bounded to 30-120s, matching the
    already-agreed-safe subset from IMM_V2_Controller_V1_spec.md's own
    hard bounds (POLL 30..120s) rather than introducing a wider range
    this release has no prior safety basis for. 180s in particular is
    left untested here deliberately -- widening toward it is a decision
    for a LATER release, informed by what this one finds, not something
    to gamble on in a data-generation run.
    """
    gap_levels = (
        ExcitationLevel(ADAPTIVE_GAP_MIN.total_seconds() * 1000, "GAP_LOW"),
        ExcitationLevel(325.0, "GAP_MID"),
        ExcitationLevel(ADAPTIVE_GAP_MAX.total_seconds() * 1000, "GAP_HIGH"),
    )
    poll_levels = (
        ExcitationLevel(30.0, "POLL_LOW"),
        ExcitationLevel(60.0, "POLL_MID"),
        ExcitationLevel(90.0, "POLL_HIGH"),
        ExcitationLevel(120.0, "POLL_MAX"),
    )
    return (
        ExcitationScheduleEntry(ExcitationMode.EXCITE_GAP, gap_levels),
        ExcitationScheduleEntry(ExcitationMode.EXCITE_POLL, poll_levels),
    )


_DEFAULT_SCHEDULE = _build_default_schedule()


# ── Go/no-go safety monitor ──────────────────────────────────────────────────

@dataclass
class _GoNoGoMonitor:
    """Rolling-window safety check for the CURRENT level only.

    Resets its own window whenever the level changes (see
    ExcitationController._advance_if_ready) -- a level that was safe does
    not get to "spend down" a bad window carried over from the level
    before it, and a level that breaches its own threshold cannot be
    rescued by outcomes that happened before it started.
    """
    outcomes: list[bool] = field(default_factory=list)   # True = success
    timeouts: list[bool] = field(default_factory=list)    # True = was a timeout

    def record(self, success: bool, was_timeout: bool) -> None:
        self.outcomes.append(success)
        self.timeouts.append(was_timeout)
        if len(self.outcomes) > _GONOGO_WINDOW_TRANSACTIONS:
            self.outcomes.pop(0)
            self.timeouts.pop(0)

    def reset(self) -> None:
        self.outcomes.clear()
        self.timeouts.clear()

    def breach_reason(self) -> str | None:
        """None if safe to continue; otherwise a human-readable reason."""
        n = len(self.outcomes)
        if n < _GONOGO_WINDOW_TRANSACTIONS:
            return None  # not enough data in this level yet to judge
        error_rate = sum(1 for ok in self.outcomes if not ok) / n
        timeout_rate = sum(self.timeouts) / n
        if error_rate > _GONOGO_MAX_ERROR_RATE:
            return (
                f"error_rate {error_rate:.3f} exceeded "
                f"{_GONOGO_MAX_ERROR_RATE:.3f} over last {n} transactions"
            )
        if timeout_rate > _GONOGO_MAX_TIMEOUT_RATE:
            return (
                f"timeout_rate {timeout_rate:.3f} exceeded "
                f"{_GONOGO_MAX_TIMEOUT_RATE:.3f} over last {n} transactions"
            )
        return None


# ── Main orchestrator ────────────────────────────────────────────────────────

class ExcitationController:
    """Owns the 2.0.15 excitation schedule's own state and safety gating.

    v2.0.15.3 FIX (real 3-day field run, this release): ONE INSTANCE PER
    BUS ENDPOINT, not per device -- a genuine architectural correction,
    not a stylistic one. The original per-device design (2.0.15/2.0.15b)
    reasoned that GAP and POLL are per-device adaptive quantities and two
    independent schedules would therefore be fine. That reasoning missed
    a fact this project already knew and had even fixed telemetry for:
    ModbusGuard.update_gap() combines every device's own requested GAP
    on a shared bus via max() -- the SLOWEST device's own request always
    wins on the real wire, regardless of what any other device requests.

    Confirmed directly against a real 3-day capture: with two
    independent, per-device schedules, one device's excitation halted
    within ~15 minutes (a real go/no-go breach) and never resumed (no
    auto-resume existed yet either -- see the cooldown/retry logic
    below, added in the same release this docstring was rewritten for).
    That device's own un-excited baseline request (500ms) then dominated
    the shared bus's own max() for the ENTIRE remaining ~44 hours. The
    OTHER device's own schedule ran genuinely, correctly, through all
    three GAP levels -- and every one of those correct, well-executed
    requests was silently discarded by the guard's own max(), because
    nothing coordinated the two independent schedules to request the
    same level at the same time. The real, physical bus gap was 500ms
    for 100% of that 44-hour run. Two independent schedules cannot
    reliably produce real GAP variation on a shared bus; only a
    genuinely shared schedule can.

    Sharing the whole controller (not just its GAP-mode state) is
    deliberate, not merely convenient: POLL is NOT combined across
    devices at the guard level (confirmed directly: no update_poll()/
    update_timeout() method exists on ModbusGuard, unlike update_gap()/
    update_max_queue_depth()), so a shared POLL schedule was not
    strictly required to fix the defect above -- but it is not harmful
    either. Each device's own get_params() still calls apply() and
    receives the schedule's current level independently; sharing one
    instance means both devices receive the SAME level at the SAME
    time, which produces more consistent, better-populated POLL data
    than one device exciting POLL alone, not less.

    A genuine, related benefit of sharing rather than a side effect
    reasoned about after the fact: the go/no-go monitor now aggregates
    outcomes from every device sharing the bus, not just one -- a
    problem that shows up as elevated errors/timeouts on one device but
    not the other (plausible, given this project's own earlier finding
    that the two devices share some common physical constraint but are
    not identical) is now visible to the one schedule governing both,
    rather than silently invisible to a schedule that only ever saw
    half the picture.
    """

    #: v2.0.15.3 FIX (real 3-day field run, this release): the missing
    #: piece that let a single early halt silently waste an entire
    #: unattended run. A halt now auto-resumes after a cooldown, up to a
    #: bounded number of attempts for the CURRENT mode -- if it keeps
    #: halting after that many genuine attempts, auto-resume stops and
    #: the existing manual button (button.py) remains the only path
    #: forward, exactly as it already was for every halt before this
    #: release. This is not "never require a human" -- it is "don't let
    #: one bad 15-minute window near the very start of a multi-day
    #: unattended run cost the whole run before a human ever gets a
    #: chance to look at it."
    _HALT_COOLDOWN = timedelta(minutes=30)
    _MAX_AUTO_RESUME_ATTEMPTS_PER_MODE = 3

    #: Registry keyed by bus endpoint (matching ModbusGuard's own
    #: get_or_create() pattern exactly) -- NOT by device serial number.
    _registry: dict[str, "ExcitationController"] = {}

    @classmethod
    def get_or_create(
        cls, bus_endpoint: str, schedule: tuple[ExcitationScheduleEntry, ...] = _DEFAULT_SCHEDULE
    ) -> "ExcitationController":
        if bus_endpoint not in cls._registry:
            cls._registry[bus_endpoint] = cls(schedule)
        return cls._registry[bus_endpoint]

    @classmethod
    def get_or_restore(
        cls,
        bus_endpoint: str,
        persisted_data: dict[str, Any],
        schedule: tuple[ExcitationScheduleEntry, ...] = _DEFAULT_SCHEDULE,
    ) -> "ExcitationController":
        """v2.0.15.3 FIX (real 3-day field run, this release): the
        registry-aware counterpart to from_persisted_dict(), which by
        itself always constructs a brand-new instance and was never
        registry-aware at all -- correct on its own for the pre-2.0.15.3
        per-device design, but a real, silent bug once ExcitationController
        became shared per bus endpoint (see this class's own docstring):
        each device's own AdaptiveModbusController._deserialize() calls
        this restoration path independently, once per device, from its
        own separately-stored persisted data. Without this method, the
        SECOND device to restore would silently create and register its
        own, separate ExcitationController for the same bus_endpoint --
        overwriting the first device's own, likely more current, shared
        instance in the registry with a second, disconnected one, right
        at the exact moment (a restart) this whole fix exists to protect.

        Deliberately "first restoration wins, every later one reuses it
        unchanged" rather than trying to reconcile two devices' own
        persisted snapshots of what was, before the restart, the SAME
        shared object -- both should hold near-identical data (they were
        sharing one instance right up until shutdown), so any difference
        is a save-timing artifact, not a real conflict worth resolving
        carefully for code this explicitly experimental and throwaway.
        """
        if bus_endpoint in cls._registry:
            return cls._registry[bus_endpoint]
        restored = cls.from_persisted_dict(persisted_data, schedule)
        cls._registry[bus_endpoint] = restored
        return restored

    @classmethod
    def clear_registry(cls) -> None:
        cls._registry.clear()

    def __init__(self, schedule: tuple[ExcitationScheduleEntry, ...] = _DEFAULT_SCHEDULE):
        self._schedule = schedule
        self._entry_idx = 0
        self._level_idx = 0
        self._state = schedule[0].mode if schedule else ExcitationMode.COMPLETE
        self._level_start_mono = time.monotonic()
        self._level_transaction_count = 0
        self._gonogo = _GoNoGoMonitor()
        self._halt_reason: str | None = None
        self._last_applied_value: float | None = None
        # v2.0.15.3 FIX: auto-resume bookkeeping. _halt_mono is None
        # whenever not currently halted (used both to detect "how long
        # have we been halted" and, via is-None, whether the cooldown
        # check in maybe_advance() applies at all). _auto_resume_count_
        # this_mode resets to 0 on every genuine mode advance (see
        # _advance_if_ready below) -- a mode that has never halted
        # before always gets its own full attempt budget, not one
        # inherited from whatever mode ran before it.
        self._halt_mono: float | None = None
        self._auto_resume_count_this_mode = 0

    # ── Outcome recording (call once per completed transaction) ────────────

    def record_outcome(self, success: bool, was_timeout: bool) -> None:
        if self._state in (ExcitationMode.NORMAL, ExcitationMode.COMPLETE, ExcitationMode.HALTED):
            return  # go/no-go only meaningful while actively exciting something
        self._level_transaction_count += 1
        self._gonogo.record(success, was_timeout)
        reason = self._gonogo.breach_reason()
        if reason is not None:
            self._halt("go/no-go breach: " + reason)

    def _halt(self, reason: str) -> None:
        _LOGGER.error(
            "ExcitationController: HALTING excitation schedule (%s). "
            "Reverting to NORMAL. Will auto-resume after a %s cooldown, "
            "up to %d attempt(s) for the current mode -- or call "
            "resume_after_halt() explicitly to resume sooner once the "
            "cause has been reviewed.",
            reason, self._HALT_COOLDOWN, self._MAX_AUTO_RESUME_ATTEMPTS_PER_MODE,
        )
        self._state = ExcitationMode.HALTED
        self._halt_reason = reason
        self._halt_mono = time.monotonic()
        self._gonogo.reset()

    def resume_after_halt(self, *, is_automatic: bool = False) -> None:
        """Recovery from a go/no-go halt -- either human-initiated (the
        button in button.py) or automatic (see maybe_advance()'s own
        cooldown/retry-limit check below).

        Deliberately does NOT resume the level that triggered the halt --
        resumes at the START of that same mode's level sequence (level 0),
        since a level that just breached safety thresholds should not be
        immediately re-attempted with no change. Advancing past it silently
        would also hide the breach from anyone reviewing the schedule's own
        history. This is unchanged by the addition of auto-resume: an
        automatic resume follows exactly the same rule a manual one always
        has, not a lighter-touch version of it.
        """
        if self._state != ExcitationMode.HALTED:
            return
        _LOGGER.warning(
            "ExcitationController: %s resume after halt (was: %s), "
            "restarting current mode from its first level.",
            "AUTOMATIC" if is_automatic else "manual",
            self._halt_reason,
        )
        self._level_idx = 0
        self._level_start_mono = time.monotonic()
        self._level_transaction_count = 0
        self._gonogo.reset()
        self._halt_reason = None
        self._halt_mono = None
        self._state = self._schedule[self._entry_idx].mode

    # ── Schedule progression ────────────────────────────────────────────────

    def maybe_advance(self) -> None:
        """Call once per poll cycle. Advances to the next level/mode if the
        current level has met BOTH its dwell-time and transaction-count
        requirement. While halted, checks the auto-resume cooldown/retry
        logic instead (v2.0.15.3 FIX) -- no-op only once that budget is
        exhausted, or if complete.
        """
        if self._state == ExcitationMode.COMPLETE:
            return
        if self._state == ExcitationMode.HALTED:
            self._maybe_auto_resume()
            return
        if self._state == ExcitationMode.NORMAL:
            # NORMAL between/before modes is not itself timed -- the
            # schedule enters EXCITE_GAP immediately on first construction
            # (see __init__), so reaching NORMAL mid-schedule should not
            # happen in this release's own linear schedule. Defensive only.
            return

        dwell_ok = (time.monotonic() - self._level_start_mono) >= _MIN_LEVEL_DWELL.total_seconds()
        count_ok = self._level_transaction_count >= _MIN_LEVEL_TRANSACTIONS
        if not (dwell_ok and count_ok):
            return

        entry = self._schedule[self._entry_idx]
        if self._level_idx + 1 < len(entry.levels):
            self._level_idx += 1
        elif self._entry_idx + 1 < len(self._schedule):
            self._entry_idx += 1
            self._level_idx = 0
            self._state = self._schedule[self._entry_idx].mode
            # v2.0.15.3 FIX: a genuinely new mode gets its own full
            # auto-resume attempt budget -- a mode that never halted
            # before should never inherit a used-up budget from a
            # different mode that struggled earlier in the schedule.
            self._auto_resume_count_this_mode = 0
        else:
            _LOGGER.info("ExcitationController: schedule COMPLETE.")
            self._state = ExcitationMode.COMPLETE
            self._level_start_mono = time.monotonic()
            self._level_transaction_count = 0
            self._gonogo.reset()
            return

        self._level_start_mono = time.monotonic()
        self._level_transaction_count = 0
        self._gonogo.reset()
        _LOGGER.info(
            "ExcitationController: advanced to %s level %s",
            self._state.value,
            self._current_level().label if self._current_level() else "n/a",
        )

    def _maybe_auto_resume(self) -> None:
        """v2.0.15.3 FIX (real 3-day field run, this release): the
        specific gap that let one early, unattended halt silently waste
        an entire 44-hour run -- see this class's own docstring for the
        full incident.
        """
        assert self._halt_mono is not None, "HALTED state must always set _halt_mono"
        elapsed = time.monotonic() - self._halt_mono
        if elapsed < self._HALT_COOLDOWN.total_seconds():
            return
        if self._auto_resume_count_this_mode >= self._MAX_AUTO_RESUME_ATTEMPTS_PER_MODE:
            # Budget exhausted for this mode -- stay halted. This is the
            # deliberate hand-off point back to a human: repeated,
            # genuine breaches after several real attempts are treated
            # as a persistent problem, not transient bad luck, and the
            # existing manual button (button.py) remains the only path
            # forward from here, exactly as it always has been.
            return
        self._auto_resume_count_this_mode += 1
        _LOGGER.warning(
            "ExcitationController: auto-resume attempt %d/%d for this "
            "mode after %s cooldown.",
            self._auto_resume_count_this_mode, self._MAX_AUTO_RESUME_ATTEMPTS_PER_MODE,
            self._HALT_COOLDOWN,
        )
        self.resume_after_halt(is_automatic=True)

    def _current_level(self) -> ExcitationLevel | None:
        if self._state not in (ExcitationMode.EXCITE_GAP, ExcitationMode.EXCITE_POLL):
            return None
        return self._schedule[self._entry_idx].levels[self._level_idx]

    # ── Applying excitation to a poll cycle's own params ────────────────────

    def apply(self, base_params: AdaptiveParams, in_transition: bool) -> AdaptiveParams:
        """Return base_params, or base_params with ONE field overridden.

        in_transition is passed explicitly (rather than read off
        base_params) so this stays correct even if AdaptiveParams' own
        field name changes -- the safety property that matters is "an
        active transition always wins", not any particular attribute path.
        """
        if in_transition:
            self._last_applied_value = None
            return base_params  # existing safety net always wins, no exceptions
        level = self._current_level()
        if level is None:
            self._last_applied_value = None
            return base_params

        self._last_applied_value = level.value
        if self._state == ExcitationMode.EXCITE_GAP:
            return dataclasses.replace(
                base_params, request_gap=timedelta(milliseconds=level.value)
            )
        if self._state == ExcitationMode.EXCITE_POLL:
            return dataclasses.replace(
                base_params, poll_interval=timedelta(seconds=level.value)
            )
        return base_params  # unreachable given _current_level()'s own guard

    # ── Observability ────────────────────────────────────────────────────────

    def telemetry_snapshot(self) -> dict[str, Any]:
        level = self._current_level()
        return {
            "excitation_mode": self._state.value,
            "excitation_level_label": level.label if level else None,
            "excitation_commanded_value": level.value if level else None,
            "excitation_applied_value": self._last_applied_value,
            "excitation_level_elapsed_s": round(time.monotonic() - self._level_start_mono, 1),
            "excitation_level_transaction_count": self._level_transaction_count,
            "excitation_halt_reason": self._halt_reason,
            # v2.0.15.3 FIX (real 3-day field run, this release): the
            # original release built this whole method but never wired
            # it into anything -- see adaptive_modbus.py's own snapshot()
            # for where that gap is finally closed. Included here from
            # the start of THIS release's own visibility fix, not added
            # after the fact: seconds since halt (None while not halted)
            # and the current auto-resume attempt count let a telemetry
            # reader distinguish "just halted, cooldown pending" from
            # "auto-resumed once already, will try again" from "budget
            # exhausted, now needs a human" without cross-referencing
            # log timestamps by hand.
            "excitation_halted_for_s": (
                round(time.monotonic() - self._halt_mono, 1)
                if self._halt_mono is not None else None
            ),
            "excitation_auto_resume_count_this_mode": self._auto_resume_count_this_mode,
        }

    # ── Persistence (mirrors AdaptiveModbusController's own Store pattern) ──

    def to_persisted_dict(self) -> dict[str, Any]:
        return {
            "entry_idx": self._entry_idx,
            "level_idx": self._level_idx,
            "state": self._state.value,
            "halt_reason": self._halt_reason,
            # Deliberately NOT persisted: level_start_mono (monotonic clock
            # is meaningless across a restart), transaction_count and the
            # go/no-go window (a restart should not let an in-progress
            # level's own count silently satisfy the requirement using
            # pre-restart transactions it never actually observed
            # end-to-end under this run's own process).
        }

    @classmethod
    def from_persisted_dict(
        cls, data: dict[str, Any], schedule: tuple[ExcitationScheduleEntry, ...] = _DEFAULT_SCHEDULE
    ) -> "ExcitationController":
        ctrl = cls(schedule)
        try:
            ctrl._entry_idx = min(int(data.get("entry_idx", 0)), max(0, len(schedule) - 1))
            ctrl._level_idx = int(data.get("level_idx", 0))
            state_str = data.get("state", ExcitationMode.NORMAL.value)
            ctrl._state = ExcitationMode(state_str)
            ctrl._halt_reason = data.get("halt_reason")
            if ctrl._state not in (ExcitationMode.HALTED, ExcitationMode.COMPLETE, ExcitationMode.NORMAL):
                # restored mid-mode -- clamp level_idx to the restored
                # entry's own valid range rather than trusting stale data
                entry = schedule[ctrl._entry_idx]
                ctrl._level_idx = min(ctrl._level_idx, len(entry.levels) - 1)
            if ctrl._state == ExcitationMode.HALTED:
                # v2.0.15.3 FIX: _halt_mono is a monotonic timestamp, not
                # persisted (same reasoning as level_start_mono above --
                # meaningless across a restart). A restored HALTED state
                # with _halt_mono left at None would make _maybe_auto_
                # resume()'s own assertion fail the next time maybe_
                # advance() runs. Treat the restart itself as the start
                # of a fresh cooldown window -- safe in both directions:
                # it never resumes sooner than a real 30-minute wait from
                # NOW, and it never assumes a cooldown that started before
                # the restart has already silently elapsed.
                ctrl._halt_mono = time.monotonic()
        except (ValueError, KeyError, IndexError) as exc:
            _LOGGER.warning(
                "ExcitationController: failed to restore persisted state (%s), "
                "starting fresh from the beginning of the schedule.", exc,
            )
            return cls(schedule)
        return ctrl

