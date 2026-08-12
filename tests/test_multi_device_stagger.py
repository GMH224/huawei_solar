"""Regression test for Defect I -- coordinator first-poll stagger offsets
were device-blind, causing same-type coordinators on different devices
sharing one ModbusGuard endpoint (daisy-chained inverters) to collide on
their first poll after every boot/reload (AUDIT_1.3.10.md).

Confirmed directly from a debug capture of a real two-inverter reload:
ModbusGuard's adaptively-learned queue depth (1, from 71 days of real
history) is correct for steady-state traffic, but a same-type first-poll
collision between two devices forces shedding + a 10s coordinator retry,
observed costing ~20s per colliding coordinator type.

Two things are tested:

1. BEHAVIOURAL: `_staggered_start_delay()`'s actual arithmetic (reproduced
   here, per this project's established trade-off for __init__.py -- see
   test_learning_gate_unsub.py's HISTORY docstring -- since __init__.py's
   import graph is too heavy for this fast suite) never returns the same
   delay for the same coordinator type on two different device indices,
   and device_index=0 reproduces today's existing, unchanged values exactly
   (no behaviour change for single-device installations).
2. STATIC (AST): __init__.py's four start_delay= call sites must go
   through _staggered_start_delay(kind, device_index), not the raw
   _COORDINATOR_START_DELAYS[kind] dict lookup directly -- the exact shape
   of the original defect.
"""
from __future__ import annotations

import ast
import pathlib
import unittest
from datetime import timedelta

_INIT_SRC = pathlib.Path(__file__).parent.parent / "__init__.py"


# ── 1. Behavioural: the staggering arithmetic itself ───────────────────────

_COORDINATOR_START_DELAYS = {
    "main":          timedelta(seconds=0),
    "power_meter":   timedelta(seconds=7),
    "energy_storage": timedelta(seconds=14),
    "configuration": timedelta(seconds=10),
    # v2.0.0b (MOD-03, external ICS audit)
    "sync_power":    timedelta(seconds=16),
}
# v2.0.3 (ICS-04, external ICS audit -- confirmed): was 5s, smaller than
# the 16s span _COORDINATOR_START_DELAYS' own five offsets already
# occupy -- see __init__.py's own comment on this same constant for the
# full reasoning. Reproduced here per this file's own established
# convention (see module docstring); kept in sync with the real value,
# and test_stride_exceeds_max_offset_in_the_real_source below checks the
# REAL source directly, not just this reproduction, so the two cannot
# silently drift apart again.
_MULTI_DEVICE_STAGGER_STRIDE = timedelta(seconds=20)


def _staggered_start_delay(kind: str, device_index: int) -> timedelta:
    return _COORDINATOR_START_DELAYS[kind] + device_index * _MULTI_DEVICE_STAGGER_STRIDE


class TestStaggeredStartDelay(unittest.TestCase):
    def test_device_zero_matches_existing_unchanged_offsets(self):
        """Single-device installations (device_index=0, the common case)
        must see byte-identical behaviour to before this fix."""
        for kind, delay in _COORDINATOR_START_DELAYS.items():
            self.assertEqual(_staggered_start_delay(kind, 0), delay)

    def test_second_device_does_not_collide_with_first(self):
        """The actual defect: same-type coordinators on two devices sharing
        one bus must not wake for their first poll at the same offset."""
        for kind in _COORDINATOR_START_DELAYS:
            delay_device0 = _staggered_start_delay(kind, 0)
            delay_device1 = _staggered_start_delay(kind, 1)
            self.assertNotEqual(
                delay_device0, delay_device1,
                f"device 0 and device 1 both get {delay_device0} for "
                f"'{kind}' -- this is the exact collision Defect I fixes.",
            )

    def test_third_device_also_gets_its_own_window(self):
        """Confirms this scales past two devices, not just a hardcoded pair."""
        offsets = {
            _staggered_start_delay("main", i) for i in range(4)
        }
        self.assertEqual(len(offsets), 4, "four devices must get four distinct offsets")

    def test_stride_is_large_enough_to_clear_a_healthy_first_poll(self):
        """A healthy first-poll exchange was observed well under 1s in the
        field capture; the stride must comfortably exceed that with margin,
        or the fix wouldn't actually separate the bursts in practice."""
        self.assertGreaterEqual(_MULTI_DEVICE_STAGGER_STRIDE.total_seconds(), 2.0)

    def test_no_cross_kind_collision_across_three_devices(self):
        """ICS-03, external ICS audit -- confirmed: the ORIGINAL 5s stride
        was large enough to clear the same-type collision the tests above
        already check, but too small to prevent a DIFFERENT coordinator
        TYPE on a later device from landing inside an earlier device's
        own window -- e.g. device 2's "main" (0 + 2*5 = 10s) landing
        exactly on device 0's own "configuration" (10s) with the old 5s
        stride. Checked directly: every (device, kind) pair across four
        devices must get a genuinely unique offset, not just "unique
        among same-kind pairs"."""
        seen: dict[float, tuple[int, str]] = {}
        for device_index in range(4):
            for kind in _COORDINATOR_START_DELAYS:
                delay = _staggered_start_delay(kind, device_index).total_seconds()
                if delay in seen:
                    other_device, other_kind = seen[delay]
                    self.fail(
                        f"collision at {delay}s: device {device_index}'s "
                        f"'{kind}' and device {other_device}'s "
                        f"'{other_kind}' both land here -- the exact cross-"
                        f"kind collision ICS-04 identified"
                    )
                seen[delay] = (device_index, kind)

    def test_stride_exceeds_max_offset_in_the_real_source(self):
        """The actual guarantee (stride > max base offset) checked
        against the REAL __init__.py source directly, not just this
        file's own reproduction above -- confirms the two cannot
        silently drift apart and reopen ICS-04 without this test
        catching it."""
        source = _INIT_SRC.read_text()
        stride_idx = source.find("_MULTI_DEVICE_STAGGER_STRIDE = timedelta(seconds=")
        assert stride_idx > -1, "_MULTI_DEVICE_STAGGER_STRIDE not found in __init__.py"
        stride_line = source[stride_idx: source.find(")", stride_idx) + 1]
        real_stride = float(
            stride_line.split("seconds=")[1].rstrip(")")
        )
        idx = source.find("_COORDINATOR_START_DELAYS = {")
        end = source.find("\n}", idx)
        table_src = source[idx:end]
        offsets = [
            float(m) for m in __import__("re").findall(
                r"timedelta\(seconds=(\d+(?:\.\d+)?)\)", table_src,
            )
        ]
        assert offsets, "could not parse any offsets from _COORDINATOR_START_DELAYS"
        self.assertGreater(
            real_stride, max(offsets),
            f"stride ({real_stride}s) must exceed the maximum base offset "
            f"({max(offsets)}s) in the REAL source, or a cross-kind "
            f"collision (ICS-04) becomes possible again for some device "
            f"count",
        )

    def test_sync_power_slot_comes_after_every_other_coordinator(self):
        """v2.0.0b (MOD-03, external ICS audit -- confirmed): SyncPower
        must be positioned AFTER the other four coordinators' slots, not
        merely present -- the whole point is giving their first polls a
        real chance to populate the regular caches before SyncPower's own
        (now cache-first, MOD-01) reads run."""
        other_kinds = [k for k in _COORDINATOR_START_DELAYS if k != "sync_power"]
        sync_power_delay = _staggered_start_delay("sync_power", 0)
        for kind in other_kinds:
            self.assertGreaterEqual(
                sync_power_delay, _staggered_start_delay(kind, 0),
                f"sync_power's stagger slot must not be earlier than "
                f"'{kind}''s",
            )


# ── 3. MOD-03: SyncPower's first refresh actually sleeps before firing ─────

class TestSyncPowerFirstRefreshIsStaggered(unittest.TestCase):
    """v2.0.0b (MOD-03, external ICS audit -- confirmed): _sync_first_
    refresh() used to fire immediately, with no stagger delay of its own
    -- exactly when the regular per-device caches are coldest."""

    def test_sync_first_refresh_sleeps_before_the_actual_refresh(self):
        source = _INIT_SRC.read_text()
        idx = source.find("async def _sync_first_refresh(")
        assert idx > -1, "_sync_first_refresh not found in __init__.py"
        end = source.find("\n                try:", idx)
        body = source[idx: end if end > -1 else idx + 2200]
        sleep_idx = body.find("await asyncio.sleep(")
        # v2.0.3 (F-02, external ICS audit -- confirmed): the actual call
        # changed from async_config_entry_first_refresh() (setup-only,
        # confirmed via a real production ConfigEntryError to always fail
        # here, since this coroutine only ever runs after the entry has
        # already left SETUP_IN_PROGRESS) to async_request_refresh() (the
        # ordinary, valid-post-setup refresh mechanism).
        refresh_idx = body.find("await coord.async_request_refresh()")
        self.assertGreater(sleep_idx, -1, "no asyncio.sleep() found -- MOD-03 has regressed")
        self.assertGreater(
            refresh_idx, -1,
            "async_request_refresh() not found -- F-02 has regressed back "
            "to the setup-only API that is guaranteed to fail here",
        )
        self.assertNotIn(
            "await coord.async_config_entry_first_refresh()", body,
            "the setup-only API must not be called from this background "
            "task at all -- see F-02's own fix reasoning. (Checked as the "
            "exact call pattern, not the bare method name -- this "
            "function's own comment mentions that name too, describing "
            "the old, now-fixed behaviour for context.)",
        )
        self.assertLess(
            sleep_idx, refresh_idx,
            "the stagger sleep must happen BEFORE the actual first refresh",
        )
        self.assertIn(
            '_staggered_start_delay("sync_power", 0)', body[sleep_idx: refresh_idx],
            "must sleep for the staggered sync_power delay specifically, "
            "not an arbitrary or hardcoded duration",
        )


# ── 4. MOD-16: setup-failure device.stop() calls are now bounded ──────────

class TestSetupFailureStopIsBounded(unittest.TestCase):
    """v2.0.0b (MOD-16, external ICS audit -- confirmed): five setup-
    failure exception handlers called `await primary_device.stop()`
    directly, with no timeout -- unlike the normal unload path, which was
    already correctly bounded (Defect U/Finding 3). A wedged transport
    during a failed setup could hang cleanup indefinitely, delaying Home
    Assistant's own retry."""

    def test_bounded_helper_exists_and_uses_disconnect_timeout(self):
        source = _INIT_SRC.read_text()
        idx = source.find("async def _bounded_device_stop(")
        self.assertGreater(idx, -1, "_bounded_device_stop() not found in __init__.py")
        end = source.find("\nasync def ", idx + 10)
        body = source[idx: end if end > -1 else idx + 1500]
        self.assertIn("asyncio.wait_for(", body)
        self.assertIn("DISCONNECT_TIMEOUT.total_seconds()", body)
        self.assertIn(
            "except Exception", body,
            "a failed/timed-out stop() must be caught and logged, not "
            "left to propagate and block the rest of setup-failure cleanup",
        )

    def test_zero_bare_primary_device_stop_calls_remain(self):
        """The core check: every one of the five original call sites must
        now go through the bounded helper, not a bare device.stop()."""
        source = _INIT_SRC.read_text()
        tree = ast.parse(source)
        bare_calls = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "stop"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "primary_device"
            ):
                bare_calls.append(node.lineno)
        self.assertEqual(
            bare_calls, [],
            f"bare primary_device.stop() (not routed through "
            f"_bounded_device_stop()) found at line(s) {bare_calls} -- "
            f"MOD-16 has regressed for at least one call site",
        )

    def test_five_call_sites_use_the_bounded_helper(self):
        source = _INIT_SRC.read_text()
        count = source.count("await _bounded_device_stop(primary_device)")
        self.assertEqual(
            count, 5,
            f"expected exactly 5 call sites routed through "
            f"_bounded_device_stop() (matching the audit's own citation), "
            f"found {count}",
        )


# ── 2. Static: the real call sites must use the staggered helper ──────────

class TestInitPyUsesStaggeredHelper(unittest.TestCase):
    def test_no_raw_coordinator_start_delays_lookup_outside_helper(self):
        source = _INIT_SRC.read_text()
        tree = ast.parse(source)

        helper = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_staggered_start_delay"
            ),
            None,
        )
        assert helper is not None, (
            "_staggered_start_delay() not found in __init__.py -- the "
            "Defect I device-aware stagger helper is missing entirely."
        )

        violations = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id == "_COORDINATOR_START_DELAYS"
            ):
                # The lookup inside the helper itself is fine and expected;
                # anything else is the original, device-blind pattern.
                if not (helper.lineno <= node.lineno <= (helper.end_lineno or helper.lineno)):
                    violations.append(node.lineno)

        assert not violations, (
            f"_COORDINATOR_START_DELAYS[...] is looked up directly, outside "
            f"_staggered_start_delay(), at line(s) {violations} -- this "
            "reintroduces Defect I (device-blind first-poll stagger, "
            "colliding across daisy-chained devices on reload). Route "
            "through _staggered_start_delay(kind, device_index) instead."
        )

    def test_setup_inverter_device_data_accepts_device_index(self):
        tree = ast.parse(_INIT_SRC.read_text())
        func = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef)
                and n.name == "_setup_inverter_device_data"
            ),
            None,
        )
        assert func is not None, "_setup_inverter_device_data not found"
        arg_names = [a.arg for a in func.args.args] + [a.arg for a in func.args.kwonlyargs]
        assert "device_index" in arg_names, (
            "_setup_inverter_device_data no longer accepts device_index -- "
            "without it, per-device staggering cannot be computed."
        )


# ── v2.0.1: H-02 -- keepalive connection-loss propagates to ALL coordinators ─

class TestKeepaliveConnectionLossPropagation(unittest.TestCase):
    """H-02, ICS re-audit -- confirmed: keepalive's on_connection_lost/
    on_connection_restored were wired only to the main coordinator at
    creation time -- power_meter/energy_storage/configuration's own
    separate RegisterCache instances never learned about a keepalive-
    detected outage, so they could keep serving stale, pre-outage values
    as Quality.GOOD indefinitely (which MOD-01's SyncPower fallback would
    then actively reuse)."""

    def _init_source(self) -> str:
        return _INIT_SRC.read_text()

    def test_rewiring_helper_functions_exist(self):
        source = self._init_source()
        self.assertIn("def _on_connection_lost_all() -> None:", source)
        self.assertIn("def _on_connection_restored_all() -> None:", source)

    def test_coordinators_list_includes_all_four_and_filters_none(self):
        source = self._init_source()
        idx = source.find("_coordinators_for_keepalive = [")
        self.assertGreater(idx, -1)
        window = source[idx: idx + 400]
        for coordinator_var in (
            "update_coordinator", "power_meter_update_coordinator",
            "energy_storage_update_coordinator", "configuration_update_coordinator",
        ):
            self.assertIn(coordinator_var, window)
        self.assertIn(
            "if c is not None", window,
            "the list must filter out coordinators that don't exist for "
            "this device (e.g. no battery configured), not call "
            "on_connection_lost() on None",
        )

    def test_optimizer_coordinator_is_deliberately_excluded(self):
        """optimizer_update_coordinator is a different class with no
        on_connection_lost/on_connection_restored method -- including it
        would crash the callback with AttributeError the first time a
        keepalive outage was actually detected."""
        source = self._init_source()
        idx = source.find("_coordinators_for_keepalive = [")
        end = source.find("]", idx)
        window = source[idx:end]
        self.assertNotIn("optimizer_update_coordinator", window)

    def test_each_coordinators_callback_is_individually_exception_guarded(self):
        """One coordinator's own on_connection_lost() raising must not
        prevent the OTHER coordinators in the list from still being
        notified -- checked directly, not just assumed from a try/except
        existing somewhere in the function."""
        source = self._init_source()
        for fn_name in ("_on_connection_lost_all", "_on_connection_restored_all"):
            idx = source.find(f"def {fn_name}() -> None:")
            self.assertGreater(idx, -1)
            end = source.find("\n    def ", idx + 10)
            if end == -1:
                end = source.find("\n    keepalive._on_connection_lost", idx)
            body = source[idx: end if end > idx else idx + 500]
            self.assertIn("for c in _coordinators_for_keepalive:", body)
            self.assertIn("try:", body)
            self.assertIn("except Exception", body)

    def test_rewiring_happens_after_all_four_coordinators_are_created(self):
        """The re-wiring must be positioned AFTER every coordinator
        creation block, not interleaved with or before them -- otherwise
        a later-created coordinator would be missing from the list."""
        source = self._init_source()
        rewire_idx = source.find("_coordinators_for_keepalive = [")
        self.assertGreater(rewire_idx, -1)
        for creation_marker in (
            'name=f"{device.serial_number}_power_meter_data_update_coordinator"',
            'name=f"{device.serial_number}_battery_data_update_coordinator"',
            'name=f"{device.serial_number}_config_data_update_coordinator"',
        ):
            marker_idx = source.find(creation_marker)
            self.assertGreater(marker_idx, -1, f"{creation_marker} not found")
            self.assertLess(
                marker_idx, rewire_idx,
                f"coordinator creation ({creation_marker}) must happen "
                f"before the keepalive re-wiring, not after",
            )

    def test_keepalive_callbacks_are_reassigned_not_left_as_the_original_binding(self):
        source = self._init_source()
        self.assertIn("keepalive._on_connection_lost = _on_connection_lost_all", source)
        self.assertIn(
            "keepalive._on_connection_restored = _on_connection_restored_all", source,
        )


if __name__ == "__main__":
    unittest.main()
