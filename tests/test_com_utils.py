from __future__ import annotations

import pytest

from sw_mcp.core.com_utils import as_list, call, call_with_out_ints, deg, mm, mm2, try_call, vec


class TypeInfoStyle:
    """pywin32 with type info: zero-arg members are methods."""

    def GetTitle(self):
        return "Part1"

    def Parameter(self, name):
        return f"dim:{name}"


class NoTypeInfoStyle:
    """pywin32 without type info: zero-arg members arrive already evaluated."""

    GetTitle = "Part1"


def test_call_handles_both_binding_styles():
    assert call(TypeInfoStyle(), "GetTitle") == "Part1"
    assert call(NoTypeInfoStyle(), "GetTitle") == "Part1"
    assert call(TypeInfoStyle(), "Parameter", "D1@Sketch1") == "dim:D1@Sketch1"


def test_call_rejects_args_on_property():
    with pytest.raises(TypeError):
        call(NoTypeInfoStyle(), "GetTitle", 1)


class FakeOle:
    def __init__(self):
        self.calls = []

    def GetIDsOfNames(self, name):
        return 77

    def Invoke(self, dispid, lcid, flags, want_result, *args):
        import pythoncom

        self.calls.append((dispid, flags, args))
        assert flags == pythoncom.DISPATCH_METHOD
        return ("point", *args)


class MethodSeenAsProperty:
    """pywin32 evaluated CreatePoint as a property (the value is not a function)."""

    CreatePoint = None

    def __init__(self):
        self._oleobj_ = FakeOle()


def test_call_falls_back_to_invoke_for_methods_with_args():
    obj = MethodSeenAsProperty()
    assert call(obj, "CreatePoint", 1.0, 2.0) == ("point", 1.0, 2.0)
    assert obj._oleobj_.calls[0][0] == 77


def test_try_call_defaults():
    assert try_call(None, "GetTitle", default="x") == "x"
    assert try_call(NoTypeInfoStyle(), "Missing", default=3) == 3


class OutParamsInPlace:
    def Save3(self, options, errors, warnings):
        errors.value = 4
        warnings.value = 1
        return False


class OutParamsAsTuple:
    def Save3(self, options, errors, warnings):
        return (True, 0, 2)


def test_out_params_both_styles():
    assert call_with_out_ints(OutParamsInPlace(), "Save3", 1) == (False, [4, 1])
    assert call_with_out_ints(OutParamsAsTuple(), "Save3", 1) == (True, [0, 2])


def test_units_and_rounding():
    assert mm(0.0123456789) == 12.346
    assert mm2(0.0008) == 800.0
    assert deg(3.141592653589793) == 180.0
    assert vec((0.0, 1.0, -0.0)) == [0.0, 1.0, 0.0]
    assert vec((1, 2)) is None
    assert as_list(None) == [] and as_list(5) == [5] and as_list((1, 2)) == [1, 2]
