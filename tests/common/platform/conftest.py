"""Native-boundary doubles: exercise ctypes dispatch without a USB device."""

from types import SimpleNamespace

import pytest

from physiclaw.common.platform import uvc


class UsbBus:
    """One USB camera behind fake IOKit functions and real COM vtables."""

    def __init__(self, mocker):
        self.number_error = 0
        self.iterator_error = 0
        self.plugin_error = 0
        self.query_error = 0
        self.registry_error = 0
        self.open_error = 0
        self.exposure_answers = True
        self.device_present = True
        self.interface_present = True
        self.calls = []
        self.requests = []
        self.released_services = []
        self.destroyed_plugins = []
        self.created_plugins = []
        self._iterators = {}
        self._plugins = []  # Own the vtables/callbacks until discovery finishes.

        device = uvc._DeviceVtbl()
        device.Release = uvc._RELEASE(lambda _: self._record("device.Release"))
        device.CreateInterfaceIterator = uvc._ITER(self._interface_iterator)
        self.device = uvc.C.pointer(uvc.C.pointer(device))
        interface = uvc._IntfVtbl()
        interface.Release = uvc._RELEASE(lambda _: self._record("interface.Release"))
        interface.GetInterfaceNumber = uvc._GET_U8(self._interface_number)
        interface.USBInterfaceOpen = uvc._NOARG(
            lambda _: self._record("open") or self.open_error
        )
        interface.USBInterfaceClose = uvc._NOARG(lambda _: self._record("close"))
        interface.ControlRequest = uvc._CTRL(self._control)
        self.interface = uvc.C.pointer(uvc.C.pointer(interface))
        self.iokit = SimpleNamespace(
            IOServiceMatching=mocker.Mock(side_effect=lambda cls: cls),
            IOServiceGetMatchingServices=mocker.Mock(
                side_effect=self._matching_services
            ),
            IOIteratorNext=mocker.Mock(side_effect=self._next),
            IOObjectRelease=mocker.Mock(side_effect=self.released_services.append),
            IOCreatePlugInInterfaceForService=mocker.Mock(
                side_effect=self._create_plugin
            ),
            IODestroyPlugInInterface=mocker.Mock(side_effect=self._destroy_plugin),
        )
        self.cf = SimpleNamespace(
            CFUUIDGetConstantUUIDWithBytes=mocker.Mock(return_value=1)
        )

    def _record(self, name):
        self.calls.append(name)
        return 0

    def _matching_services(self, port, matching, out):
        uvc.C.cast(out, uvc.C.POINTER(uvc.C.c_uint32))[0] = 10
        self._iterators[10] = iter([100] if self.device_present else [])
        return self.registry_error

    def _interface_iterator(self, device, request, out):
        self._record("interfaces")
        out[0] = 20
        self._iterators[20] = iter([200] if self.interface_present else [])
        return self.iterator_error

    def _next(self, iterator):
        return next(self._iterators[iterator], 0)

    def _interface_number(self, interface, out):
        self._record("number")
        out[0] = 3
        return self.number_error

    def _create_plugin(self, service, uc_uuid, iid, out, score):
        if self.plugin_error:
            return self.plugin_error
        target = self.device if service == 100 else self.interface

        def query(plugin, interface_id, result):
            if self.query_error:
                return self.query_error
            result[0] = uvc.C.cast(target, uvc.C.c_void_p).value
            return 0

        table = uvc._DeviceVtbl()
        table.QueryInterface = uvc._QI(query)
        pointer = uvc.C.pointer(uvc.C.pointer(table))
        self._plugins.append(pointer)
        address = uvc.C.cast(pointer, uvc.C.c_void_p).value
        self.created_plugins.append(address)
        uvc.C.cast(out, uvc.C.POINTER(uvc.C.c_void_p))[0] = address
        return 0

    def _destroy_plugin(self, plugin):
        self.destroyed_plugins.append(uvc.C.cast(plugin, uvc.C.c_void_p).value)

    def _control(self, interface, pipe, request):
        req = request.contents
        self.requests.append(
            (req.bmRequestType, req.bRequest, req.wValue, req.wIndex, req.wLength)
        )
        if not self.exposure_answers:
            return 1
        req.wLenDone = req.wLength
        return 0


@pytest.fixture
def usb_bus(mocker):
    bus = UsbBus(mocker)
    mocker.patch.object(uvc, "_iokit", None)
    mocker.patch.object(uvc, "_cf", None)
    mocker.patch.object(uvc.C, "CDLL", side_effect=[bus.iokit, bus.cf])
    return bus
