"""Random broadband excitation controller for huawei_solar v2.0.15.4.

Purpose
-------
NOT a production controller, and NOT an extension of excitation_
controller.py's own ExcitationController (2.0.15/2.0.15b/2.0.15.3) --
a deliberately separate, much simpler mechanism answering a different
question.

ExcitationController answers "what does the system look like at each
GAP/POLL level, once settled?" -- long dwell times (4h minimum per
level) are specifically designed to let transients die out before a
measurement counts. That design has a structural blind spot: it can
never observe what happens in the minutes immediately after a parameter
change, while the channel still carries residual state from whatever
came before -- and it only ever varies one lever at a time, so it can
never observe joint interaction between GAP, TIMEOUT, and POLL.

This module exists specifically to close that blind spot: it assumes
the channel is NOT memoryless (a real, evidenced finding from this
project's own earlier analysis -- the EWMA load model's own fitted time
constant is tau=~45s) and deliberately re-randomizes all three levers
together, every 10 minutes, for as long as it runs. A 10-minute window
is roughly 13x that time constant -- long enough that most of each
window reflects a genuinely new, distinct operating point, while the
first minute or two of every window directly captures the transient
this project has never otherwise measured.

Design principles, all confirmed directly with the person who
commissioned this release before writing any code:
  - Independent, uncoupled draws for all three levers (GAP, TIMEOUT,
    POLL) -- confirmed directly: no code-level relationship between
    poll_interval and request_timeout exists anywhere in this
    integration (checked directly in adaptive_modbus.py,
    update_coordinator.py, modbus_guard.py before this was written),
    so there is no real coupling to preserve by drawing them together.
  - Deliberately NO go/no-go safety monitor, unlike ExcitationController.
    This is READ-ONLY telemetry collection (Modbus register reads),
    not a write/control action -- an unstable channel under this mode
    degrades data collection, not physical inverter safety, and the
    whole point of this release is to observe exactly the edge-
    transition instability a production controller (built later, on
    2.0.14) should be designed to avoid. Halting on exactly the
    conditions this release exists to capture would silently discard
    the most valuable data it could produce.
  - The existing in_transition safety net is NOT touched or bypassed --
    see apply() below. That protection is orthogonal to the stability
    question above: it exists so a genuine day/night mode transition is
    never treated as an excitation opportunity, which remains correct
    and desired here exactly as it always has been.
  - Deliberately NO persistence across restarts, unlike ExcitationController.
    There is no "progress" to preserve: every 10-minute window is
    independent and equally valid regardless of when it starts, so a
    restart simply begins a new random draw immediately. Keeping this
    release genuinely simple (per explicit instruction: "this is
    experimental throwaway code... keep it simple") was judged to
    matter more here than a persistence mechanism this design does not
    actually need.
  - Shared per bus endpoint, reusing the exact registry pattern
    ExcitationController and ModbusGuard already established and this
    project already found necessary the hard way: GAP is combined
    across every device on a bus via ModbusGuard's own max(), so two
    devices drawing independently would silently produce an effective
    GAP governed by whichever device happened to draw the larger value
    -- not a controlled observation of either draw. A shared instance
    makes both devices draw and apply the identical values at the
    identical moment, structurally, not just usually.
  - max_queue_depth is deliberately untouched -- not part of this
    release's own randomization scope.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
import logging
import random
import time
from typing import Any

from .adaptive_modbus import AdaptiveParams

_LOGGER = logging.getLogger(__name__)

#: Redraw cadence. ~13x this project's own fitted channel memory time
#: constant (tau=~45s, EWMA load model) -- long enough that most of each
#: window reflects a genuinely new, settled-ish operating point, while
#: still frequent enough to accumulate many transitions over a 3-day run
#: (~432 redraws total).
REDRAW_INTERVAL = timedelta(minutes=10)

#: Random draw bounds. GAP and POLL confirmed to sit within this
#: project's own already-validated hard bounds (const.py's own
#: ADAPTIVE_GAP_MIN/MAX, ADAPTIVE_POLL_MIN/MAX) before this file was
#: written -- this release samples more thoroughly within an envelope
#: already established as safe, the same principle excitation_
#: controller.py's own schedule was built on.
#:
#: TIMEOUT's own floor is deliberately 20s, NOT the 30s floor originally
#: used here and agreed for the sequential excitation releases. That
#: floor existed to avoid reproducing an already-fixed regression
#: (several register classes need timeout >=25-35s to avoid spurious
#: timeouts) -- appropriate when the goal was clean, uncontaminated
#: envelope data. This release's own goal is different: it exists
#: specifically to generate evidence for designing the real filter/
#: coordinator, which needs to correctly handle exactly the edge
#: conditions production should avoid, not just data that stays clear
#: of them. Confirmed directly this is the right trade-off for this
#: release's own purpose: spurious timeouts here mean an abandoned,
#: retried READ (this is read-only telemetry collection, no physical
#: safety implication), and the resulting data directly characterizes
#: the real transition each register class hits, rather than only
#: inheriting simulation-derived estimates that were never confirmed
#: against genuinely independent, randomly-sampled conditions.
#:
#: GAP's own floor is NOT lowered the same way, deliberately -- see
#: MIN_INTER_REQUEST_GAP's own docstring in modbus_guard.py: 150ms is a
#: documented HARDWARE constraint (SUN2000 Modbus FSM reset time
#: ~100ms), already tested lower (30ms) and found to cause "pervasive
#: 0x06 SLAVE_DEVICE_BUSY responses on all SUN2000 hardware" -- a
#: known, uniform failure mode already characterized once, not an
#: open boundary worth re-discovering with fresh random sampling the
#: way TIMEOUT's own boundary is. The guard clamps to this floor
#: unconditionally regardless of what is drawn here, so setting GAP_
#: MIN_MS below 150 would also be silently ineffective on the real
#: wire, not just unadvisable.
GAP_MIN_MS = 150.0
GAP_MAX_MS = 500.0
TIMEOUT_MIN_S = 20.0
TIMEOUT_MAX_S = 60.0
POLL_MIN_S = 30.0
POLL_MAX_S = 90.0


@dataclass(frozen=True)
class _RandomDraw:
    gap_ms: float
    timeout_s: float
    poll_s: float


def _new_draw() -> _RandomDraw:
    return _RandomDraw(
        gap_ms=random.uniform(GAP_MIN_MS, GAP_MAX_MS),
        timeout_s=random.uniform(TIMEOUT_MIN_S, TIMEOUT_MAX_S),
        poll_s=random.uniform(POLL_MIN_S, POLL_MAX_S),
    )


class RandomExcitationController:
    """Shared (per bus endpoint) broadband random excitation.

    See module docstring for the full design and its reasoning. Public
    surface deliberately mirrors ExcitationController's own enable/
    disable/apply/record_outcome/telemetry_snapshot shape (even where a
    method here is a no-op, e.g. record_outcome) so AdaptiveModbusController's
    own existing integration points (get_params(), record_request(),
    enable_excitation(), disable_excitation()) do not need their own
    call sites rewritten -- only which class they instantiate.
    """

    _registry: dict[str, "RandomExcitationController"] = {}

    @classmethod
    def get_or_create(cls, bus_endpoint: str) -> "RandomExcitationController":
        if bus_endpoint not in cls._registry:
            cls._registry[bus_endpoint] = cls()
        return cls._registry[bus_endpoint]

    @classmethod
    def clear_registry(cls) -> None:
        cls._registry.clear()

    def __init__(self) -> None:
        self._draw = _new_draw()
        self._draw_start_mono = time.monotonic()
        self._last_applied_gap_ms: float | None = None
        self._last_applied_timeout_s: float | None = None
        self._last_applied_poll_s: float | None = None

    def maybe_advance(self) -> None:
        """Call once per poll cycle -- named to match ExcitationController's
        own maybe_advance() exactly, so AdaptiveModbusController.get_params()'s
        own existing call site needs no changes between releases (only
        which class enable_excitation() instantiates differs). "Advance"
        here means "redraw if the 10-minute window has elapsed", not
        "move to the next level" -- this class has no levels at all, see
        module docstring. A no-op until REDRAW_INTERVAL has genuinely
        elapsed; safe to call as often as get_params() itself is called
        (multiple times per real device poll is expected and harmless --
        see enable_excitation()'s own docstring in adaptive_modbus.py for
        why get_params() can be called more often than one might assume).
        """
        elapsed = time.monotonic() - self._draw_start_mono
        if elapsed < REDRAW_INTERVAL.total_seconds():
            return
        self._draw = _new_draw()
        self._draw_start_mono = time.monotonic()
        _LOGGER.info(
            "RandomExcitationController: new draw -- gap=%.1fms timeout=%.1fs poll=%.1fs",
            self._draw.gap_ms, self._draw.timeout_s, self._draw.poll_s,
        )

    def record_outcome(self, success: bool, was_timeout: bool) -> None:
        """No-op -- deliberately no go/no-go monitor. See module
        docstring for why. Present only so AdaptiveModbusController.
        record_request()'s own existing call site (shared with
        ExcitationController) does not need special-casing for which
        excitation class is currently active.
        """
        return

    def apply(self, base_params: AdaptiveParams, in_transition: bool) -> AdaptiveParams:
        """Override GAP, TIMEOUT, and POLL with the current random draw
        -- unless an active transition is in progress, in which case
        base_params is returned completely unchanged. This safety net
        is not touched or weakened by this release; see module
        docstring.
        """
        if in_transition:
            self._last_applied_gap_ms = None
            self._last_applied_timeout_s = None
            self._last_applied_poll_s = None
            return base_params
        d = self._draw
        self._last_applied_gap_ms = d.gap_ms
        self._last_applied_timeout_s = d.timeout_s
        self._last_applied_poll_s = d.poll_s
        return replace(
            base_params,
            request_gap=timedelta(milliseconds=d.gap_ms),
            request_timeout=timedelta(seconds=d.timeout_s),
            poll_interval=timedelta(seconds=d.poll_s),
        )

    def telemetry_snapshot(self) -> dict[str, Any]:
        return {
            "random_excitation_mode": "ACTIVE",
            "random_excitation_draw_gap_ms": self._draw.gap_ms,
            "random_excitation_draw_timeout_s": self._draw.timeout_s,
            "random_excitation_draw_poll_s": self._draw.poll_s,
            "random_excitation_applied_gap_ms": self._last_applied_gap_ms,
            "random_excitation_applied_timeout_s": self._last_applied_timeout_s,
            "random_excitation_applied_poll_s": self._last_applied_poll_s,
            "random_excitation_draw_elapsed_s": round(
                time.monotonic() - self._draw_start_mono, 1
            ),
        }
