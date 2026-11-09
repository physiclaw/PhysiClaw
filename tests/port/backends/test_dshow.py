"""DirectShow metadata and COM lifetime tests; no physical camera."""

import ctypes as C
from types import SimpleNamespace

import pytest

from physiclaw.port.backends import dshow, windows


@pytest.fixture
def native(mocker):
    state = SimpleNamespace(
        init=0,
        fail=None,
        bounds=(0, 1023, 1, 512, 3),
        released=[],
        selected=[],
        calls=[],
    )
    ole = mocker.Mock()
    ole.CoInitializeEx.side_effect = lambda *args: state.init

    def create(*args):
        args[-1]._obj.value = 10
        return -1 if state.fail == "create" else 0

    def invoke(obj, slot, types, *args):
        state.calls.append((obj.value, slot))
        if slot == 2:
            state.released.append(obj.value)
            return 0
        if obj.value == 10:
            if state.fail == "enumerate":
                return 1
            args[1]._obj.value = 20
        elif obj.value == 20:
            index = len(state.selected)
            state.selected.append(index)
            if state.fail == "next":
                return 1
            args[1]._obj.value = 30 + index
            args[2]._obj.value = 1
        elif slot == 8:
            if state.fail == "bind":
                return -1
            args[-1]._obj.value = 40
        elif obj.value == 40:
            assert slot == 3 and args[0] == 6
            if state.fail == "range":
                return -1
            for target, value in zip(args[1:], state.bounds, strict=True):
                target._obj.value = value
        else:
            raise AssertionError((obj, slot))
        return 0

    ole.CoCreateInstance.side_effect = create
    mocker.patch.object(dshow, "_ole32", return_value=ole)
    mocker.patch.object(dshow, "_invoke", side_effect=invoke)
    state.ole = ole
    return state


def test_range_uses_selected_camera_and_releases_every_interface(native):
    assert dshow.focus_range(2) == (0, 1023, 1)
    assert native.selected == [0, 1, 2]
    assert (32, 8) in native.calls
    assert native.released == [40, 32, 31, 30, 20, 10]
    native.ole.CoUninitialize.assert_called_once_with()


@pytest.mark.parametrize("hr", [0, 1, dshow._RPC_E_CHANGED_MODE])
def test_com_initialization_balanced_only_when_owned(native, hr):
    native.init = hr
    assert dshow.focus_range(0) == (0, 1023, 1)
    assert native.ole.CoUninitialize.call_count == int(hr in (0, 1))


def test_failed_com_initialization_opens_nothing(native):
    native.init = -1
    assert dshow.focus_range(0) is None
    native.ole.CoCreateInstance.assert_not_called()
    native.ole.CoUninitialize.assert_not_called()


@pytest.mark.parametrize(
    "stage,released",
    [
        ("create", []),
        ("enumerate", [10]),
        ("next", [20, 10]),
        ("bind", [30, 20, 10]),
        ("range", [40, 30, 20, 10]),
    ],
)
def test_partial_probe_failure_releases_owned_interfaces(native, stage, released):
    native.fail = stage
    assert dshow.focus_range(0) is None
    assert native.released == released
    native.ole.CoUninitialize.assert_called_once_with()


@pytest.mark.parametrize(
    "bounds",
    [
        (0, 1023, 1, 512, 1),
        (-1, 1023, 1, 512, 3),
        (100, 10, 1, 100, 3),
        (0, 1023, 0, 512, 3),
    ],
)
def test_unusable_or_auto_only_range_is_unavailable(native, bounds):
    native.bounds = bounds
    assert dshow.focus_range(0) is None
    assert native.released == [40, 30, 20, 10]


def test_non_default_bounds_and_single_position_are_preserved(native):
    for bounds in [(10, 110, 5, 40, 2), (0, 0, 1, 0, 3)]:
        native.bounds = bounds
        assert dshow.focus_range(0) == bounds[:3]


def test_negative_index_does_not_probe_native_camera(native):
    assert dshow.focus_range(-1) is None
    native.ole.CoInitializeEx.assert_not_called()


def test_native_query_unavailable_is_not_a_calibration_failure(mocker):
    mocker.patch.object(dshow, "_ole32", side_effect=OSError("not available"))
    assert dshow.focus_range(0) is None


def test_windows_routes_range_query_to_directshow(native):
    assert windows.camera_focus_search_range(1) == (0, 1023, 1)
    assert native.selected == [0, 1]


def test_hresult_and_pointer_widths_match_windows_abi():
    assert C.sizeof(dshow._LONG) == C.sizeof(dshow._DWORD) == 4
    assert C.sizeof(dshow._GUID) == 16
    assert C.sizeof(dshow._P) == C.sizeof(C.c_void_p)
