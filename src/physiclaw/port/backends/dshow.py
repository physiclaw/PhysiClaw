"""Read-only DirectShow focus metadata, using the capture-device index."""

import ctypes as C
import logging
import uuid
from contextlib import contextmanager

log = logging.getLogger(__name__)
_P = C.c_void_p
_LONG = C.c_int32
_DWORD = C.c_uint32
_GUID = C.c_ubyte * 16
_RPC_E_CHANGED_MODE = -2147417850


def _guid(value):
    return _GUID.from_buffer_copy(uuid.UUID(value).bytes_le)


def _ole32():
    ole = getattr(C, "WinDLL")("ole32")
    ole.CoInitializeEx.argtypes = [_P, _DWORD]
    ole.CoInitializeEx.restype = _LONG
    ole.CoCreateInstance.argtypes = [_P, _P, _DWORD, _P, _P]
    ole.CoCreateInstance.restype = _LONG
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    return ole


def _invoke(obj, slot, argtypes, *args):
    table = C.cast(obj, C.POINTER(C.POINTER(_P))).contents
    method = getattr(C, "WINFUNCTYPE")(_LONG, _P, *argtypes)(table[slot])
    return method(obj, *args)


def _check(hr):
    if hr != 0:
        raise OSError(f"DirectShow HRESULT 0x{hr & 0xFFFFFFFF:08x}")


@contextmanager
def _camera_control(index):
    ole = _ole32()
    initialized = False
    refs = []
    try:
        hr = ole.CoInitializeEx(None, 0)
        if hr in (0, 1):
            initialized = True
        elif hr != _RPC_E_CHANGED_MODE:
            _check(hr)

        devices = _P()
        _check(
            ole.CoCreateInstance(
                C.byref(_guid("62be5d10-60eb-11d0-bd3b-00a0c911ce86")),
                None,
                1,
                C.byref(_guid("29840822-5b84-11d0-bd3b-00a0c911ce86")),
                C.byref(devices),
            )
        )
        refs.append(devices)
        if not devices:
            raise OSError("DirectShow device enumerator unavailable")
        monikers = _P()
        _check(
            _invoke(
                devices,
                3,
                [_P, _P, _DWORD],
                C.byref(_guid("860bb310-5d01-11d0-bd3b-00a0c911ce86")),
                C.byref(monikers),
                0,
            )
        )
        refs.append(monikers)
        if not monikers:
            raise OSError("No DirectShow cameras")
        for _ in range(index + 1):
            moniker, fetched = _P(), _DWORD()
            _check(
                _invoke(
                    monikers,
                    3,
                    [_DWORD, _P, _P],
                    1,
                    C.byref(moniker),
                    C.byref(fetched),
                )
            )
            if fetched.value != 1 or not moniker:
                raise OSError("DirectShow camera index unavailable")
            refs.append(moniker)

        control = _P()
        _check(
            _invoke(
                moniker,
                8,
                [_P, _P, _P, _P],
                None,
                None,
                C.byref(_guid("c6e13370-30ac-11d0-a18c-00a0c9118956")),
                C.byref(control),
            )
        )
        refs.append(control)
        if not control:
            raise OSError("DirectShow focus controls unavailable")
        yield control
    finally:
        for ref in reversed(refs):
            if ref:
                _invoke(ref, 2, [])
        if initialized:
            ole.CoUninitialize()


def focus_range(index: int) -> tuple[int, int, int] | None:
    """Query IAMCameraControl::GetRange without starting another stream."""
    if index < 0:
        return None
    try:
        with _camera_control(index) as control:
            low, high, step, default, caps = (_LONG() for _ in range(5))
            _check(
                _invoke(
                    control,
                    3,
                    [_LONG, _P, _P, _P, _P, _P],
                    6,
                    C.byref(low),
                    C.byref(high),
                    C.byref(step),
                    C.byref(default),
                    C.byref(caps),
                )
            )
            if (
                not caps.value & 2
                or low.value < 0
                or high.value < low.value
                or step.value <= 0
            ):
                return None
            return low.value, high.value, step.value
    except (OSError, AttributeError):
        log.debug("DirectShow focus range unavailable", exc_info=True)
        return None
