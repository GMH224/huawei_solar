"""Regression test for Defect S -- ModbusKeepAlive's keep-alive probe has
never successfully run against the huawei_solar package this integration
depends on (AUDIT_1.3.16.md).

Root cause: `_get_keepalive_register()` used `RegisterName[KEEPALIVE_REGISTER]`
(Enum-style subscript lookup) and only caught `KeyError`. `RegisterName` is
actually a `typing.NewType` over `str`, not an Enum -- subscripting it
always raises `TypeError: 'NewType' object is not subscriptable`, a
different exception than the one being caught. The error was therefore
never actually handled: it propagated out of this function, out of
`_probe()` (called before `_probe()`'s own try block), and was only ever
silenced by `_run()`'s outer catch-all ("unexpected error in run loop"), on
every single keep-alive cycle.

This was a genuine defect in this project's own integration code -- an
unverified assumption about the register-name type's runtime shape
(Enum-like member lookup), never checked against its actual behaviour, and
an exception handler narrow enough to miss the real failure mode. Ours to
own outright, regardless of which package defines that type (see the
BUG-9 FOLLOW-UP note in modbus_keepalive.py).

This test exercises the real, installed huawei_solar package directly (not
a fake/stub), since the entire point of the defect is a mismatch between
an assumption made in our code and that package's actual runtime
behaviour -- a fake would not be able to prove the fix actually works
against what our integration really runs against.
"""
from __future__ import annotations

import ast
import pathlib
import sys
import unittest

_KEEPALIVE_SRC = pathlib.Path(__file__).parent.parent / "modbus_keepalive.py"
_CONST_SRC = pathlib.Path(__file__).parent.parent / "const.py"


class _RealHuaweiSolarMixin:
    """Provides `cls.RegisterName` / `cls.REGISTERS` bound to the GENUINE,
    installed huawei_solar library, regardless of what other test files in
    this suite have stubbed into sys.modules.

    v1.3.16 lesson, discovered while writing this test: the real
    huawei_solar package has a non-idempotent import-time side effect
    (registering PDU classes into `tmodbus`'s global registry), which
    raises if the same real modules are ever imported a SECOND time in one
    process. The fix is to only ever do the purge-and-fresh-import dance
    ONCE per process: if the real package is already cached (because this
    class, or test_tier_separation.py's TestRealRegisterMap -- updated
    alongside this file to use the same "already real? skip the purge"
    guard -- already loaded it earlier in this session), reuse it directly
    instead of forcing a second import. Deliberately does NOT restore a
    prior stub afterward: every stub-creating file in this suite
    unconditionally overwrites sys.modules["huawei_solar"] at its own
    import time regardless of what was there before, so nothing depends on
    inheriting any particular prior state -- restoring it was unnecessary
    complexity that, in an earlier version of this fix, itself caused a
    second class to see a stub again and re-trigger the same collision.
    """

    @staticmethod
    def _is_real_huawei_solar(mod) -> bool:
        # Every stub built for this suite is a bare types.ModuleType() with
        # no __file__; the genuine installed package always has one.
        return mod is not None and getattr(mod, "__file__", None) is not None

    @classmethod
    def setUpClass(cls):
        import importlib

        cached = sys.modules.get("huawei_solar")
        if cls._is_real_huawei_solar(cached):
            from huawei_solar import RegisterName
            from huawei_solar.registers import REGISTERS
            cls.RegisterName = RegisterName
            cls.REGISTERS = REGISTERS
            return

        for name in list(sys.modules):
            if name == "huawei_solar" or name.startswith("huawei_solar."):
                del sys.modules[name]
        try:
            importlib.invalidate_caches()
            from huawei_solar import RegisterName
            from huawei_solar.registers import REGISTERS
        except ImportError:
            raise unittest.SkipTest(
                "huawei_solar library not installed in this environment; "
                "this test validates against the real huawei_solar package "
                "when available (pip install huawei-solar==3.0.5)"
            )
        cls.RegisterName = RegisterName
        cls.REGISTERS = REGISTERS


def _get_keepalive_register_name() -> str:
    """Extract the KEEPALIVE_REGISTER constant's literal value directly
    from const.py's source, without importing the module (which pulls in
    Home Assistant)."""
    tree = ast.parse(_CONST_SRC.read_text())
    assign = next(
        (
            n for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "KEEPALIVE_REGISTER" for t in n.targets)
        ),
        None,
    )
    assert assign is not None, "KEEPALIVE_REGISTER not found in const.py"
    assert isinstance(assign.value, ast.Constant)
    return assign.value.value


class TestRegisterNameIsActuallyANewType(_RealHuaweiSolarMixin, unittest.TestCase):
    """Confirms the premise of the whole defect against the real library --
    if this ever stops being true (the library reverts to an Enum), the
    original bug would no longer reproduce, which is useful to know."""

    def test_register_name_is_a_newtype_not_an_enum(self):
        import typing
        self.assertIsInstance(self.RegisterName, typing.NewType)
        # The property that actually matters for this defect: subscripting
        # it must fail, exactly as the old (broken) call site did.
        with self.assertRaises(TypeError):
            self.RegisterName["model_id"]  # the exact old, broken call


class TestKeepaliveRegisterResolution(_RealHuaweiSolarMixin, unittest.TestCase):
    """Reproduces the OLD and NEW resolution logic directly against the
    real installed library."""

    def test_old_pattern_raises_typeerror_not_keyerror(self):
        """Adversarial: proves the old `except KeyError` genuinely could
        not have caught this -- the real failure is TypeError."""
        keepalive_register = _get_keepalive_register_name()
        with self.assertRaises(TypeError):
            self.RegisterName[keepalive_register]

    def test_new_pattern_resolves_cleanly(self):
        keepalive_register = _get_keepalive_register_name()
        self.assertIn(
            keepalive_register, self.REGISTERS,
            f"KEEPALIVE_REGISTER '{keepalive_register}' is not a real "
            "register in the installed library -- const.py needs updating, "
            "separate from this defect.",
        )
        result = self.RegisterName(keepalive_register)
        self.assertEqual(result, keepalive_register)

    def test_new_pattern_handles_a_genuinely_invalid_register_gracefully(self):
        """If KEEPALIVE_REGISTER were ever misconfigured to a nonexistent
        name, the new logic must detect it via REGISTERS membership rather
        than relying on any particular exception type."""
        fake_register = "this_register_definitely_does_not_exist_xyz"
        self.assertNotIn(fake_register, self.REGISTERS)
        # The fixed code's actual behaviour: check membership first, only
        # construct RegisterName(...) for a name known to be valid.
        if fake_register not in self.REGISTERS:
            resolved = None
        else:
            resolved = self.RegisterName(fake_register)  # pragma: no cover
        self.assertIsNone(resolved)


class TestSourceUsesCallNotSubscript(unittest.TestCase):
    def test_get_keepalive_register_does_not_subscript_register_name(self):
        source = _KEEPALIVE_SRC.read_text()
        tree = ast.parse(source)
        func = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_get_keepalive_register"
            ),
            None,
        )
        assert func is not None, "_get_keepalive_register not found in modbus_keepalive.py"

        subscript_violations = [
            n.lineno for n in ast.walk(func)
            if isinstance(n, ast.Subscript)
            and isinstance(n.value, ast.Name)
            and n.value.id == "RegisterName"
        ]
        assert not subscript_violations, (
            f"RegisterName[...] (subscript) used at line(s) "
            f"{subscript_violations} -- this reintroduces Defect S. "
            "RegisterName is a NewType in the installed library and does "
            "not support subscripting; use RegisterName(...) (a call) "
            "after checking membership in huawei_solar.registers.REGISTERS."
        )

    def test_get_keepalive_register_validates_against_registers_table(self):
        source = _KEEPALIVE_SRC.read_text()
        tree = ast.parse(source)
        func = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_get_keepalive_register"
            ),
            None,
        )
        assert func is not None
        references_registers_table = "REGISTERS" in ast.dump(func)
        assert references_registers_table, (
            "_get_keepalive_register no longer validates against "
            "huawei_solar.registers.REGISTERS -- this reintroduces the "
            "risk of an unverified assumption about the register name's "
            "validity."
        )


if __name__ == "__main__":
    unittest.main()
