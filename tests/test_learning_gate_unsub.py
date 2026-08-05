"""Regression test for §2.3 / Defect F -- "Unable to remove unknown job
listener" on reload.

ROOT CAUSE: hass.bus.async_listen_once() self-unsubscribes the instant its
event fires. _async_register_learning_gates() (in __init__.py) handed that
unsub callable directly to entry.async_on_unload(), so the SAME unsub ran a
second time whenever the entry unloaded after the event had already fired
(the common case for EVENT_HOMEASSISTANT_STOP, re-armed on every setup and
almost never still pending by the time of a later reload). The second
removal hits an already-empty listener slot.

Two things are tested:

1. BEHAVIOURAL: a minimal fake event bus that reproduces HA's real
   self-unsubscribe-on-fire semantics (calling remove() a second time after
   the event fired raises, exactly like the field error). The v1.3.7 guard
   pattern (_guarded_once, mirrored here) must survive an unload that
   happens after the event fired, AND must still cleanly cancel a listener
   that never fired.

2. STATIC (AST): __init__.py's _async_register_learning_gates must not hand
   hass.bus.async_listen_once(...) directly to entry.async_on_unload(...) --
   it must go through a wrapping call instead. This is deliberately a cheap,
   fast, dependency-free backstop (see project rule: "source-string/AST
   assertions are not full coverage, but are a legitimate way to catch a
   specific defect class") for exactly the pattern that caused this bug, so
   it cannot be silently reintroduced.

ADVERSARIAL CHECK (run manually, not part of the suite): pointing
_INIT_SRC at the pre-v1.3.7 __init__.py reproduces the failure for test 2,
and the behavioural fake-bus test in test 1 demonstrates the exact
exception the field log shows when exercised against the OLD (unguarded)
pattern instead of _guarded_once.
"""
from __future__ import annotations

import ast
import pathlib
import unittest

_INIT_SRC = pathlib.Path(__file__).parent.parent / "__init__.py"


# ── 1. Behavioural: fake bus reproducing HA's real self-unsub-on-fire ──────

class _FakeBus:
    """Reproduces the exact hazard: listen_once removes its own listener the
    moment the event fires; calling the returned unsub again afterwards
    raises, matching "Unable to remove unknown job listener"."""

    def __init__(self):
        self._listeners = {}
        self._callbacks = {}

    def async_listen_once(self, event_type, callback_fn):
        token = object()
        self._listeners[event_type] = token
        self._callbacks[event_type] = callback_fn

        def _unsub():
            current = self._listeners.get(event_type)
            if current is not token:
                raise KeyError(
                    f"Unable to remove unknown job listener for {event_type!r}"
                )
            del self._listeners[event_type]

        self._unsub_for_test = _unsub
        return _unsub

    def fire(self, event_type):
        # HA fires the callback then self-removes the one-shot listener.
        callback_fn = self._callbacks[event_type]
        del self._listeners[event_type]
        callback_fn(None)


def _guarded_once(bus, event_type, on_fire, unload_callbacks):
    """Mirrors the v1.3.7 fix in __init__.py._async_register_learning_gates."""
    fired = False

    def _wrapped(event):
        nonlocal fired
        fired = True
        on_fire(event)

    unsub = bus.async_listen_once(event_type, _wrapped)

    def _remove():
        if not fired:
            unsub()

    unload_callbacks.append(_remove)


def _unguarded_once(bus, event_type, on_fire, unload_callbacks):
    """The OLD (broken) pattern, for the adversarial comparison."""
    unsub = bus.async_listen_once(event_type, on_fire)
    unload_callbacks.append(unsub)


class TestGuardedOnceSurvivesLateUnload(unittest.TestCase):
    def test_guarded_pattern_survives_unload_after_fire(self):
        bus = _FakeBus()
        unload_callbacks = []
        fired_reasons = []

        _guarded_once(bus, "started", lambda e: fired_reasons.append("settled"), unload_callbacks)

        bus.fire("started")  # event already fired before the entry unloads
        self.assertEqual(fired_reasons, ["settled"])

        # Entry unloads later (a reload) -- must NOT raise.
        for cb in unload_callbacks:
            cb()

    def test_guarded_pattern_still_cancels_pending_listener(self):
        bus = _FakeBus()
        unload_callbacks = []
        fired_reasons = []

        _guarded_once(bus, "started", lambda e: fired_reasons.append("settled"), unload_callbacks)

        # Entry unloads BEFORE the event ever fired -- must still remove it.
        for cb in unload_callbacks:
            cb()
        self.assertEqual(fired_reasons, [])
        self.assertNotIn("started", bus._listeners)

    def test_unguarded_pattern_reproduces_the_field_bug(self):
        """Adversarial: proves the fake bus reproduces the real defect when
        the OLD pattern is used, so test 1's pass is meaningful."""
        bus = _FakeBus()
        unload_callbacks = []

        _unguarded_once(bus, "started", lambda e: None, unload_callbacks)
        bus.fire("started")

        with self.assertRaises(KeyError):
            for cb in unload_callbacks:
                cb()


# ── 2. Static: the fixed pattern must be present in __init__.py ───────────

class TestNoDirectUnsubForwarding(unittest.TestCase):
    def _get_function(self, tree, name):
        func = next(
            (
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == name
            ),
            None,
        )
        assert func is not None, f"{name} not found in __init__.py"
        return func

    def test_async_listen_once_not_passed_directly_to_async_on_unload(self):
        source = _INIT_SRC.read_text()
        tree = ast.parse(source)
        func = self._get_function(tree, "_async_register_learning_gates")

        violations = []
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "async_on_unload"
            ):
                for arg in node.args:
                    if (
                        isinstance(arg, ast.Call)
                        and isinstance(arg.func, ast.Attribute)
                        and arg.func.attr == "async_listen_once"
                    ):
                        violations.append(node.lineno)

        assert not violations, (
            f"entry.async_on_unload() is passed hass.bus.async_listen_once(...) "
            f"directly at line(s) {violations}. This reintroduces the double-"
            "unsub bug (§2.3): wrap the listen_once call so its unsub is only "
            "invoked if the event has not already fired."
        )


if __name__ == "__main__":
    unittest.main()
