"""ET240 Windows HID + WinUSB transport; no vendor software or extra driver needed."""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import os
import uuid

VID, PID = 0x1E4E, 0x720E
WINUSB_GUID = 'fce22465-21b7-4c3c-90c8-f9b25a24f5ab'
HID_GUID = '4d1e55b2-f16f-11cf-88cb-001111000030'


class ET240Error(RuntimeError):
    pass


class GUID(C.Structure):
    _fields_ = [('data', C.c_ubyte * 16)]

    def __init__(self, value):
        super().__init__((C.c_ubyte * 16).from_buffer_copy(uuid.UUID(value).bytes_le))


class InterfaceData(C.Structure):
    _fields_ = [('size', W.DWORD), ('guid', GUID), ('flags', W.DWORD), ('reserved', C.c_size_t)]


class DeviceInfo(C.Structure):
    _fields_ = [('size', W.DWORD), ('guid', GUID), ('devinst', W.DWORD), ('reserved', C.c_size_t)]


class Overlapped(C.Structure):
    _fields_ = [('internal', C.c_size_t), ('internal_high', C.c_size_t),
                ('offset', W.DWORD), ('offset_high', W.DWORD), ('event', W.HANDLE)]


class HidCaps(C.Structure):
    _fields_ = [('usage', W.WORD), ('usage_page', W.WORD), ('input_length', W.WORD),
                ('output_length', W.WORD), ('feature_length', W.WORD),
                ('reserved', W.WORD * 17), ('rest', W.WORD * 10)]


class InterfaceDescriptor(C.Structure):
    _pack_ = 1
    _fields_ = [(n, C.c_ubyte) for n in ('length', 'type', 'number', 'alt', 'endpoints',
                                        'class_code', 'subclass', 'protocol', 'string')]


class PipeInfo(C.Structure):
    _fields_ = [('type', C.c_int), ('id', C.c_ubyte), ('max_packet_size', W.USHORT),
                ('interval', C.c_ubyte)]


class API:
    def __init__(self):
        if os.name != 'nt':
            raise ET240Error('Direct ET240 access requires Windows.')
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        self.setup = C.WinDLL('setupapi', use_last_error=True)
        self.cfg = C.WinDLL('cfgmgr32', use_last_error=True)
        self.hid = C.WinDLL('hid', use_last_error=True)
        self.usb = C.WinDLL('winusb', use_last_error=True)
        P = C.POINTER
        signatures = [
            (self.setup, 'SetupDiGetClassDevsW', W.HANDLE, [P(GUID), W.LPCWSTR, W.HWND, W.DWORD]),
            (self.setup, 'SetupDiEnumDeviceInterfaces', W.BOOL, [W.HANDLE, C.c_void_p, P(GUID), W.DWORD, P(InterfaceData)]),
            (self.setup, 'SetupDiGetDeviceInterfaceDetailW', W.BOOL, [W.HANDLE, P(InterfaceData), C.c_void_p, W.DWORD, P(W.DWORD), P(DeviceInfo)]),
            (self.setup, 'SetupDiDestroyDeviceInfoList', W.BOOL, [W.HANDLE]),
            (self.cfg, 'CM_Get_Parent', W.DWORD, [P(W.DWORD), W.DWORD, W.DWORD]),
            (self.cfg, 'CM_Get_Device_IDW', W.DWORD, [W.DWORD, W.LPWSTR, W.ULONG, W.ULONG]),
            (self.kernel, 'CreateFileW', W.HANDLE, [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE]),
            (self.kernel, 'CloseHandle', W.BOOL, [W.HANDLE]),
            (self.kernel, 'CreateEventW', W.HANDLE, [C.c_void_p, W.BOOL, W.BOOL, W.LPCWSTR]),
            (self.kernel, 'WaitForSingleObject', W.DWORD, [W.HANDLE, W.DWORD]),
            (self.kernel, 'ReadFile', W.BOOL, [W.HANDLE, C.c_void_p, W.DWORD, P(W.DWORD), P(Overlapped)]),
            (self.kernel, 'WriteFile', W.BOOL, [W.HANDLE, C.c_void_p, W.DWORD, P(W.DWORD), P(Overlapped)]),
            (self.kernel, 'GetOverlappedResult', W.BOOL, [W.HANDLE, P(Overlapped), P(W.DWORD), W.BOOL]),
            (self.kernel, 'CancelIoEx', W.BOOL, [W.HANDLE, P(Overlapped)]),
            (self.hid, 'HidD_GetPreparsedData', W.BOOL, [W.HANDLE, P(C.c_void_p)]),
            (self.hid, 'HidD_FreePreparsedData', W.BOOL, [C.c_void_p]),
            (self.hid, 'HidP_GetCaps', C.c_long, [C.c_void_p, P(HidCaps)]),
            (self.usb, 'WinUsb_Initialize', W.BOOL, [W.HANDLE, P(W.HANDLE)]),
            (self.usb, 'WinUsb_Free', W.BOOL, [W.HANDLE]),
            (self.usb, 'WinUsb_QueryInterfaceSettings', W.BOOL, [W.HANDLE, C.c_ubyte, P(InterfaceDescriptor)]),
            (self.usb, 'WinUsb_QueryPipe', W.BOOL, [W.HANDLE, C.c_ubyte, C.c_ubyte, P(PipeInfo)]),
            (self.usb, 'WinUsb_SetPipePolicy', W.BOOL, [W.HANDLE, C.c_ubyte, W.ULONG, W.ULONG, C.c_void_p]),
            (self.usb, 'WinUsb_ReadPipe', W.BOOL, [W.HANDLE, C.c_ubyte, C.c_void_p, W.ULONG, P(W.ULONG), C.c_void_p]),
        ]
        for dll, name, restype, argtypes in signatures:
            f = getattr(dll, name)
            f.restype, f.argtypes = restype, argtypes

    @staticmethod
    def check(ok, operation):
        if not ok:
            code = C.get_last_error()
            raise ET240Error(f'{operation}: {C.FormatError(code).strip()} (Windows error {code})')

    def ancestors(self, devinst):
        result = []
        for _ in range(8):
            buf = C.create_unicode_buffer(1024)
            if self.cfg.CM_Get_Device_IDW(devinst, buf, len(buf), 0):
                break
            result.append(buf.value)
            parent = W.DWORD()
            if self.cfg.CM_Get_Parent(C.byref(parent), devinst, 0):
                break
            devinst = parent.value
        return result

    def enumerate(self, value):
        guid = GUID(value)
        handle = self.setup.SetupDiGetClassDevsW(C.byref(guid), None, None, 0x12)
        self.check(handle != W.HANDLE(-1).value, 'Enumerate device interfaces')
        rows = []
        try:
            index = 0
            while True:
                item = InterfaceData()
                item.size = C.sizeof(item)
                if not self.setup.SetupDiEnumDeviceInterfaces(handle, None, C.byref(guid), index, C.byref(item)):
                    if C.get_last_error() == 259:
                        break
                    self.check(False, 'Enumerate interface')
                index += 1
                needed = W.DWORD()
                self.setup.SetupDiGetDeviceInterfaceDetailW(handle, C.byref(item), None, 0, C.byref(needed), None)
                detail = C.create_string_buffer(needed.value)
                C.cast(detail, C.POINTER(W.DWORD))[0] = 8 if C.sizeof(C.c_void_p) == 8 else 6
                info = DeviceInfo()
                info.size = C.sizeof(info)
                self.check(self.setup.SetupDiGetDeviceInterfaceDetailW(handle, C.byref(item), detail, len(detail), None, C.byref(info)), 'Get interface path')
                path = C.wstring_at(C.addressof(detail) + 4)
                if 'vid_1e4e&pid_720e' not in path.lower():
                    continue
                ancestors = self.ancestors(info.devinst)
                parent = next((s for s in ancestors if s.upper().startswith('USB\\VID_1E4E&PID_720E\\')), '')
                rows.append({'path': path, 'parent': parent, 'ancestors': ancestors})
        finally:
            self.setup.SetupDiDestroyDeviceInfoList(handle)
        return rows

    def open_file(self, path):
        handle = self.kernel.CreateFileW(path, 0xC0000000, 3, None, 3, 0x40000000, None)
        self.check(handle != W.HANDLE(-1).value, 'Open ET240; close other analyzer programs')
        return handle


def enumerate_devices():
    api = API()
    usb, hid = api.enumerate(WINUSB_GUID), api.enumerate(HID_GUID)
    result = []
    for row in usb:
        matches = [h for h in hid if h['parent'] and h['parent'] == row['parent']]
        result.append({**row, 'hid_path': matches[0]['path'] if len(matches) == 1 else None,
                       'serial': row['parent'].rsplit('\\', 1)[-1], 'vid': VID, 'pid': PID})
    return result


def build_config(*, sample_rate=3, bmc=True, aux=False, usb4=False, vconn=True):
    """48-byte SET_CONFIG body from DeviceAp.SetConfig (v1.08.26.0809)."""
    if sample_rate not in range(5):
        raise ValueError('sample_rate must be 0..4')
    body = bytearray(48)
    body[0], body[1], body[2] = 3 if bmc else 0, 32 if aux else 0, 0
    body[8:11] = bytes((sample_rate, 128 if usb4 else 0, 128 if vconn else 0))
    body[16:30] = bytes.fromhex('43 27 02 00 43 27 08 00 00 00 FB 69 10 00')
    return bytes(body)


class ET240:
    def __init__(self, *, device=None, timeout_ms=1000):
        self.api = API()
        self.selector, self.timeout_ms = device, timeout_ms
        self.hid_handle = self.usb_file = self.usb_handle = None
        self.info = None
        self.endpoints = []

    def open(self):
        rows = enumerate_devices()
        if self.selector is not None:
            rows = [r for r in rows if r['serial'] == self.selector or r['path'] == self.selector]
        if len(rows) != 1:
            raise ET240Error(f'Expected one ET240, found {len(rows)}. Use --device SERIAL if multiple devices are connected.')
        self.info = rows[0]
        if not self.info['hid_path']:
            raise ET240Error('The matching ET240 HID interface was not found.')
        try:
            self.hid_handle = self.api.open_file(self.info['hid_path'])
            pp, caps = C.c_void_p(), HidCaps()
            self.api.check(self.api.hid.HidD_GetPreparsedData(self.hid_handle, C.byref(pp)), 'HID descriptor')
            try:
                if self.api.hid.HidP_GetCaps(pp, C.byref(caps)) < 0:
                    raise ET240Error('Could not read ET240 HID report lengths.')
            finally:
                self.api.hid.HidD_FreePreparsedData(pp)
            self.input_length, self.output_length = caps.input_length, caps.output_length
            if min(self.input_length, self.output_length) < 50:
                raise ET240Error('Unexpected ET240 HID report size.')
            return self
        except BaseException:
            self.close()
            raise

    def _hid_io(self, payload=None):
        size = self.input_length if payload is None else self.output_length
        buffer = C.create_string_buffer(size)
        if payload is not None:
            if len(payload) > size:
                raise ET240Error('HID command exceeds report length.')
            buffer[:len(payload)] = payload
        count, ov = W.DWORD(), Overlapped()
        ov.event = self.api.kernel.CreateEventW(None, True, False, None)
        self.api.check(ov.event, 'Create HID I/O event')
        pending = False
        try:
            call = self.api.kernel.ReadFile if payload is None else self.api.kernel.WriteFile
            if not call(self.hid_handle, buffer, size, C.byref(count), C.byref(ov)):
                self.api.check(C.get_last_error() == 997, 'HID transfer')
                pending = True
                if self.api.kernel.WaitForSingleObject(ov.event, self.timeout_ms) != 0:
                    raise ET240Error('ET240 HID transfer timed out.')
                self.api.check(self.api.kernel.GetOverlappedResult(self.hid_handle, C.byref(ov), C.byref(count), False), 'Complete HID transfer')
                pending = False
            if count.value != size:
                raise ET240Error(f'Short HID transfer: {count.value}/{size}')
            return bytes(buffer.raw[:count.value])
        finally:
            if pending:
                self.api.kernel.CancelIoEx(self.hid_handle, C.byref(ov))
                self.api.kernel.GetOverlappedResult(self.hid_handle, C.byref(ov), C.byref(count), True)
            self.api.kernel.CloseHandle(ov.event)

    def get_info(self):
        self._hid_io(b'\x00\x02')
        raw = self._hid_io()
        return {'device_id': raw[1:13].hex(' ').upper(), 'edition': raw[17],
                'edition_name': 'SN3005' if raw[17] == 5 else f'edition-{raw[17]}',
                'firmware': raw[19:25].hex(' ').upper(), 'raw': raw[:25].hex(' '),
                'serial': self.info['serial']}

    def configure(self, **kwargs):
        self._hid_io(b'\x00\x10' + build_config(**kwargs))

    def start_capture(self, **kwargs):
        self.configure(**kwargs)
        self.usb_file = self.api.open_file(self.info['path'])
        handle = W.HANDLE()
        self.api.check(self.api.usb.WinUsb_Initialize(self.usb_file, C.byref(handle)), 'Initialize WinUSB')
        self.usb_handle = handle.value
        desc = InterfaceDescriptor()
        self.api.check(self.api.usb.WinUsb_QueryInterfaceSettings(self.usb_handle, 0, C.byref(desc)), 'WinUSB interface')
        self.endpoints = []
        for index in range(desc.endpoints):
            pipe = PipeInfo()
            self.api.check(self.api.usb.WinUsb_QueryPipe(self.usb_handle, 0, index, C.byref(pipe)), 'WinUSB endpoint')
            self.endpoints.append({'address': pipe.id, 'type': pipe.type, 'max_packet_size': pipe.max_packet_size})
        inputs = [e for e in self.endpoints if e['type'] == 2 and e['address'] & 0x80]
        if len(inputs) != 1:
            raise ET240Error(f'Unexpected ET240 bulk IN endpoints: {inputs}')
        self.input_pipe = inputs[0]['address']
        timeout = W.ULONG(self.timeout_ms)
        self.api.check(self.api.usb.WinUsb_SetPipePolicy(self.usb_handle, self.input_pipe, 3, C.sizeof(timeout), C.byref(timeout)), 'Set USB timeout')

    def read(self):
        buffer, count = C.create_string_buffer(512), W.ULONG()
        ok = self.api.usb.WinUsb_ReadPipe(self.usb_handle, self.input_pipe, buffer, len(buffer), C.byref(count), None)
        if not ok and C.get_last_error() == 121:
            return b''
        self.api.check(ok, 'Read ET240 capture')
        return buffer.raw[:count.value]

    def stop_capture(self):
        # The official application stops by freeing WinUSB and closing its file.
        if self.usb_handle:
            self.api.usb.WinUsb_Free(self.usb_handle)
            self.usb_handle = None
        if self.usb_file:
            self.api.kernel.CloseHandle(self.usb_file)
            self.usb_file = None

    def close(self):
        self.stop_capture()
        if self.hid_handle:
            self.api.kernel.CloseHandle(self.hid_handle)
            self.hid_handle = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *_):
        self.close()
