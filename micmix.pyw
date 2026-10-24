"""MicMix: your microphone plus one app's sound (or everything on an output) in one virtual mic, with a slider.

Mic + music go to "Send to". Make that a virtual cable (VB-Audio's free VB-Cable: "CABLE Input") and pick the cable's
other end ("CABLE Output") as the microphone in Discord. The slider fades voice <-> music; both are full at the middle.
"Music from" takes one app (Spotify, a browser, your MP3 player) as OBS's Application Audio Capture does, so game
sounds and Discord stay out; or everything playing on one output device, as OBS's Desktop Audio does.

Standard library only, all through ctypes: winmm (MME) for the mic and Send to, WASAPI loopback for the music.
Needs no admin and changes no Windows setting. The picks and the slider are kept in micmix.json next to this file.
"""
import contextlib
import ctypes
import json
import math
import os
import threading
import tkinter as tk
import uuid
from array import array
from ctypes import wintypes as w
from pathlib import Path
from tkinter import ttk
from types import SimpleNamespace

RATE, CHANNELS = 48000, 2  # Discord and VB-Cable run at 48 kHz; Windows converts for any other device
BLOCK = RATE // 50 * CHANNELS * 2  # bytes in 20 ms of 16-bit stereo
SILENCE = bytes(BLOCK)
MIC_BUFS, OUT_BUFS, PAD = 8, 6, 2  # 160 ms of mic buffers; at most 120 ms queued to Send to; 40 ms cushion after a gap
WAVE_MAPPER, CALLBACK_EVENT, WHDR_DONE = 0xFFFFFFFF, 0x50000, 1
ENDPOINT_ID, ENDPOINT_ID_SIZE, PREFERRED_GET = 0x811, 0x812, 0x2015  # waveOutMessage: DRV_QUERYFUNCTIONINSTANCEID...
LOOPBACK_FLAGS, BUFFER_SILENT, CLSCTX_ALL = 0x88020000, 2, 23  # LOOPBACK | AUTOCONVERTPCM | SRC_DEFAULT_QUALITY
VT_BLOB, PROCESS_LOOPBACK, E_NOINTERFACE = 65, 1, -2147467262
DEFAULT, OFF = "Windows default", "Off"
SETTINGS = Path(__file__).with_suffix(".json")

winmm, kernel32, ole32 = ctypes.WinDLL("winmm"), ctypes.WinDLL("kernel32"), ctypes.WinDLL("ole32")
mmdevapi = ctypes.WinDLL("mmdevapi")


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [("wFormatTag", w.WORD), ("nChannels", w.WORD), ("nSamplesPerSec", w.DWORD),
                ("nAvgBytesPerSec", w.DWORD), ("nBlockAlign", w.WORD), ("wBitsPerSample", w.WORD), ("cbSize", w.WORD)]


class WAVEHDR(ctypes.Structure):
    _fields_ = [("lpData", ctypes.c_void_p), ("dwBufferLength", w.DWORD), ("dwBytesRecorded", w.DWORD),
                ("dwUser", ctypes.c_size_t), ("dwFlags", w.DWORD), ("dwLoops", w.DWORD),
                ("lpNext", ctypes.c_void_p), ("reserved", ctypes.c_size_t)]


class CAPS(ctypes.Structure):  # WAVEOUTCAPSW; WAVEINCAPSW is the same without dwSupport
    _fields_ = [("wMid", w.WORD), ("wPid", w.WORD), ("vDriverVersion", w.UINT), ("szPname", w.WCHAR * 32),
                ("dwFormats", w.DWORD), ("wChannels", w.WORD), ("wReserved1", w.WORD), ("dwSupport", w.DWORD)]


class PROPVARIANT(ctypes.Structure):  # only its VT_BLOB form
    _fields_ = [("vt", w.WORD), ("reserved", w.WORD * 3), ("cbSize", w.ULONG), ("pBlobData", ctypes.c_void_p)]


class LOOPBACK_PARAMS(ctypes.Structure):  # AUDIOCLIENT_ACTIVATION_PARAMS, in its process loopback form
    _fields_ = [("ActivationType", ctypes.c_int), ("TargetProcessId", w.DWORD), ("ProcessLoopbackMode", ctypes.c_int)]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
                ("th32ParentProcessID", w.DWORD), ("pcPriClassBase", w.LONG), ("dwFlags", w.DWORD),
                ("szExeFile", w.WCHAR * 260)]


FORMAT = WAVEFORMATEX(1, CHANNELS, RATE, RATE * CHANNELS * 2, CHANNELS * 2, 16, 0)  # 1: plain PCM
HDR, PTR = ctypes.POINTER(WAVEHDR), ctypes.POINTER(ctypes.c_void_p)
for io in ("waveIn", "waveOut"):  # both APIs share these signatures; declared, so 64-bit handles pass whole
    for name, args in {"Open": [ctypes.POINTER(w.HANDLE), w.UINT, ctypes.POINTER(WAVEFORMATEX), w.HANDLE,
                                ctypes.c_size_t, w.DWORD],
                       "PrepareHeader": [w.HANDLE, HDR, w.UINT], "UnprepareHeader": [w.HANDLE, HDR, w.UINT],
                       "Reset": [w.HANDLE], "Close": [w.HANDLE], "GetNumDevs": [],
                       "GetDevCapsW": [ctypes.c_size_t, ctypes.POINTER(CAPS), w.UINT],
                       "GetErrorTextW": [w.UINT, w.LPWSTR, w.UINT],
                       "Message": [w.HANDLE, w.UINT, ctypes.c_size_t, ctypes.c_size_t]}.items():
        getattr(winmm, io + name).argtypes = args
winmm.waveInAddBuffer.argtypes = winmm.waveOutWrite.argtypes = [w.HANDLE, HDR, w.UINT]
winmm.waveInStart.argtypes = [w.HANDLE]
kernel32.CreateEventW.restype = w.HANDLE
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR]
kernel32.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
kernel32.CloseHandle.argtypes = [w.HANDLE]
kernel32.CreateToolhelp32Snapshot.restype = w.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
kernel32.Process32FirstW.argtypes = kernel32.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, w.DWORD]
ole32.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p, w.DWORD, ctypes.c_void_p, PTR]
ole32.CoCreateInstance.restype = ctypes.HRESULT
mmdevapi.ActivateAudioInterfaceAsync.argtypes = [w.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, PTR]
mmdevapi.ActivateAudioInterfaceAsync.restype = ctypes.HRESULT


def guid(text: str) -> ctypes.Array:
    return (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(text).bytes_le)


def com(index: int, *argtypes):
    """COM method number `index` (vtable order in the Windows SDK headers); call it with the object first.
    HRESULT failures raise OSError."""
    return ctypes.WINFUNCTYPE(ctypes.HRESULT, *argtypes)(index, f"method{index}")


ENUMERATOR, IMMDEVICEENUMERATOR = guid("BCDE0395-E52F-467C-8E3D-C4579291692E"), guid("A95664D2-9614-4F35-A746-DE8DB63617E6")
IAUDIOCLIENT, IAUDIOCAPTURECLIENT = guid("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2"), guid("C8ADBD64-E71E-48A0-A4DE-185C395CD317")
IAUDIOSESSIONMANAGER2 = guid("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F")
IAUDIOSESSIONCONTROL2 = guid("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D")
HANDLER_IIDS = {uuid.UUID(i).bytes_le for i in ("00000000-0000-0000-C000-000000000046",  # IUnknown
                                                 "41D949AB-9862-444A-80F6-C261334DA5EB",  # the completion handler
                                                 "94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90")}  # IAgileObject: required
query_interface, release = com(0, ctypes.c_void_p, PTR), ctypes.WINFUNCTYPE(ctypes.c_ulong)(2, "Release")  # IUnknown
enum_endpoints, get_device = com(3, w.UINT, w.DWORD, PTR), com(5, w.LPCWSTR, PTR)  # IMMDeviceEnumerator
device_count, device_item = com(3, ctypes.POINTER(w.UINT)), com(4, w.UINT, PTR)  # IMMDeviceCollection
activate = com(3, ctypes.c_void_p, w.DWORD, ctypes.c_void_p, PTR)  # IMMDevice
session_list = com(5, PTR)  # IAudioSessionManager2
session_count, get_session = com(3, ctypes.POINTER(ctypes.c_int)), com(4, ctypes.c_int, PTR)  # IAudioSessionEnumerator
session_pid = com(14, ctypes.POINTER(w.DWORD))  # IAudioSessionControl2
activate_result = com(3, ctypes.POINTER(w.LONG), PTR)  # IActivateAudioInterfaceAsyncOperation
initialize = com(3, ctypes.c_int, w.DWORD, ctypes.c_longlong, ctypes.c_longlong, ctypes.c_void_p, ctypes.c_void_p)
start, stop, get_service = com(10), com(11), com(14, ctypes.c_void_p, PTR)  # IAudioClient
get_buffer = com(3, PTR, ctypes.POINTER(w.UINT), ctypes.POINTER(w.DWORD), ctypes.c_void_p, ctypes.c_void_p)
release_buffer, next_packet = com(4, w.UINT), com(5, ctypes.POINTER(w.UINT))  # IAudioCaptureClient
QUERY = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, PTR)  # our one COM object's methods
COUNT = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
COMPLETED = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)
# ponytail: activation handlers are never freed, so a late call from Windows can't land in freed memory;
# a few hundred bytes each time you pick an app
HANDLERS = []


def devices(io: str) -> list[str]:
    """Names of the "waveIn" or "waveOut" devices; a name's index is its device id. MME cuts names at 31 characters."""
    caps, names = CAPS(), []
    for i in range(getattr(winmm, io + "GetNumDevs")()):
        failed = getattr(winmm, io + "GetDevCapsW")(i, ctypes.byref(caps), ctypes.sizeof(caps))
        names.append(f"Device {i}" if failed else caps.szPname)
    return names


def device_id(io: str, name: str) -> int | None:
    """A picked name's device id: WAVE_MAPPER for the Windows default, None for Off or a device that is gone."""
    if name == DEFAULT:
        return WAVE_MAPPER
    names = devices(io)
    return names.index(name) if name in names else None


def default_device(io: str) -> int:
    """The MME id of the Windows default "waveIn" or "waveOut" device."""
    dev, flags = w.DWORD(), w.DWORD()
    getattr(winmm, io + "Message")(WAVE_MAPPER, PREFERRED_GET, ctypes.addressof(dev), ctypes.addressof(flags))
    return dev.value


def processes() -> dict[int, tuple[str, int]]:
    """pid -> (exe name, parent pid) for every running process."""
    snap, entry, found = kernel32.CreateToolhelp32Snapshot(2, 0), PROCESSENTRY32W(), {}  # 2: TH32CS_SNAPPROCESS
    entry.dwSize = ctypes.sizeof(entry)
    more = kernel32.Process32FirstW(snap, ctypes.byref(entry))
    while more:
        found[entry.th32ProcessID] = entry.szExeFile, entry.th32ParentProcessID
        more = kernel32.Process32NextW(snap, ctypes.byref(entry))
    kernel32.CloseHandle(snap)
    return found


def is_app(name: str) -> bool:
    """A Music from pick is an app (its exe name, in any case) rather than a whole device."""
    return name.lower().endswith(".exe")


def app_pid(exe: str) -> int | None:
    """The top process of exe's tree: the tree includes the helper processes that play the sound for Chrome, Spotify
    and the like. Of several separate trees, the one with sound open wins. Never MicMix itself, whose output would
    loop back in."""
    procs, exe = processes(), exe.lower()
    named = lambda pid: procs.get(pid, ("",))[0].lower() == exe  # noqa: E731

    def top(pid: int) -> int:
        for _ in range(64):  # bounded: a reused pid could make a loop
            if not named(procs[pid][1]):
                break
            pid = procs[pid][1]
        return pid

    tops = [top(p) for p in session_pids() if named(p)] + [p for p, (_, parent) in procs.items()
                                                           if named(p) and not named(parent)]
    return next((p for p in tops if p != os.getpid()), None)


def playing_apps() -> list[str]:
    """Apps with sound open on any output device, as Windows' volume mixer lists them; never Discord or MicMix."""
    procs = processes()
    names = {procs[p][0] for p in session_pids() - {0, os.getpid()} if p in procs}
    return sorted((name for name in names if "discord" not in name.lower()), key=str.lower)


def session_pids() -> set[int]:
    """The processes with a sound session on any output device."""
    ole32.CoInitializeEx(None, 2)  # apartment COM for the window's thread; a harmless no-op where COM is on
    objects, pids, n = [], set(), w.UINT()

    def new() -> ctypes.c_void_p:  # every COM object made here, released at the end
        objects.append(ctypes.c_void_p())
        return objects[-1]

    try:
        with contextlib.suppress(OSError):  # whatever fails just lists less
            enum, outputs = new(), new()
            ole32.CoCreateInstance(ENUMERATOR, None, CLSCTX_ALL, IMMDEVICEENUMERATOR, ctypes.byref(enum))
            enum_endpoints(enum, 0, 1, ctypes.byref(outputs))  # outputs (0) that are on (1)
            device_count(outputs, ctypes.byref(n))
            for i in range(n.value):
                device, manager, sessions, count = new(), new(), new(), ctypes.c_int()
                device_item(outputs, i, ctypes.byref(device))
                activate(device, IAUDIOSESSIONMANAGER2, CLSCTX_ALL, None, ctypes.byref(manager))
                session_list(manager, ctypes.byref(sessions))
                session_count(sessions, ctypes.byref(count))
                for j in range(count.value):
                    control, control2, pid = new(), new(), w.DWORD()
                    get_session(sessions, j, ctypes.byref(control))
                    query_interface(control, IAUDIOSESSIONCONTROL2, ctypes.byref(control2))
                    session_pid(control2, ctypes.byref(pid))
                    pids.add(pid.value)
    finally:
        for obj in reversed(objects):
            if obj:
                release(obj)
    return pids


def gains(fade: float) -> tuple[float, float]:
    """Slider 0 (voice only) .. 1 (music only) -> (voice, music) gains. Both are full at the middle; squared, so the
    quiet end of each fade gets most of the slider's travel (music under your voice sits around 0.1-0.2)."""
    return min(1.0, 2 - 2 * fade) ** 2, min(1.0, 2 * fade) ** 2


def mix(voice: bytes, music: bytes, vg: float, mg: float) -> bytes:
    """voice*vg + music*mg, clipped to 16 bits."""
    v, m = round(vg * 32768), round(mg * 32768)  # gains in 1/32768ths: integer math, exact at 0 and 1
    return array("h", [min(32767, max(-32768, (x * v + y * m) >> 15))
                       for x, y in zip(array("h", voice), array("h", music))]).tobytes()


class Wave:
    """One open MME device and its ring of 20 ms buffers, which Windows fills (Mic) or plays (Speaker)."""
    io = ""

    def __init__(self, dev: int, count: int, label: str):
        self.label, self.handle = label, w.HANDLE()
        self.event = kernel32.CreateEventW(None, False, False, None)
        self.call("Open", ctypes.byref(self.handle), dev, ctypes.byref(FORMAT), self.event, 0, CALLBACK_EVENT)
        self.bufs = [ctypes.create_string_buffer(BLOCK) for _ in range(count)]
        self.hdrs = [WAVEHDR(ctypes.addressof(b), BLOCK) for b in self.bufs]
        for h in self.hdrs:
            self.call("PrepareHeader", self.handle, h, ctypes.sizeof(h))

    def call(self, name: str, *args) -> None:
        if err := getattr(winmm, self.io + name)(*args):
            text = ctypes.create_unicode_buffer(256)
            getattr(winmm, self.io + "GetErrorTextW")(err, text, 256)
            raise OSError(f"{self.label}: {text.value or f'error {err}'}")

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.call("Reset", self.handle)  # hands every buffer back
            for h in self.hdrs:
                self.call("UnprepareHeader", self.handle, h, ctypes.sizeof(h))
            self.call("Close", self.handle)
        kernel32.CloseHandle(self.event)


class Mic(Wave):
    io = "waveIn"

    def __init__(self, dev: int):
        super().__init__(dev, MIC_BUFS, "Microphone")
        for h in self.hdrs:
            self.call("AddBuffer", self.handle, h, ctypes.sizeof(h))
        self.call("Start", self.handle)
        self.next = 0

    def read(self) -> bytes | None:
        """The next 20 ms from the mic, or None when it isn't there yet (just ask again)."""
        h = self.hdrs[self.next]
        if not h.dwFlags & WHDR_DONE:
            kernel32.WaitForSingleObject(self.event, 500)
            if not h.dwFlags & WHDR_DONE:
                return None
        data = self.bufs[self.next].raw
        h.dwFlags &= ~WHDR_DONE
        self.call("AddBuffer", self.handle, h, ctypes.sizeof(h))
        self.next = (self.next + 1) % MIC_BUFS
        return data


class Speaker(Wave):
    io = "waveOut"

    def __init__(self, dev: int):
        super().__init__(dev, OUT_BUFS, "Send to")
        for h in self.hdrs:
            h.dwFlags |= WHDR_DONE  # done = free to fill

    def write(self, data: bytes) -> None:
        free = [h for h in self.hdrs if h.dwFlags & WHDR_DONE]
        # ponytail: the mic's clock paces the output, so drift between the two clocks costs a 20 ms gap or skip every
        # few minutes; resample against the queue depth if that ever becomes audible
        if len(free) == OUT_BUFS:  # ran dry (just opened, or a stall): cushion it so the next hiccup isn't a click
            for _ in range(PAD):
                self.send(free.pop(), SILENCE)
        if free:
            self.send(free.pop(), data)

    def send(self, h: WAVEHDR, data: bytes) -> None:
        ctypes.memmove(h.lpData, data, BLOCK)
        h.dwFlags &= ~WHDR_DONE
        self.call("Write", self.handle, h, ctypes.sizeof(h))


class Activated:
    """The callback object ActivateAudioInterfaceAsync needs (IUnknown, the completion handler and IAgileObject),
    built from ctypes callbacks. All it does is say "done"."""

    def __init__(self):
        self.done = threading.Event()

        def query(this, iid, out):
            known = ctypes.string_at(iid, 16) in HANDLER_IIDS
            out[0] = this if known else None
            return 0 if known else E_NOINTERFACE

        def completed(this, operation):
            self.done.set()
            return 0

        self.methods = [QUERY(query), COUNT(lambda this: 1), COUNT(lambda this: 1), COMPLETED(completed)]
        self.vtable = (ctypes.c_void_p * 4)(*(ctypes.cast(m, ctypes.c_void_p) for m in self.methods))
        self.object = ctypes.c_void_p(ctypes.addressof(self.vtable))  # a COM object starts with its vtable's address
        HANDLERS.append(self)


def app_client(pid: int) -> ctypes.c_void_p:
    """An IAudioClient that hears only process pid and its children (process loopback: Windows 10 2004 and later)."""
    params = LOOPBACK_PARAMS(PROCESS_LOOPBACK, pid, 0)  # mode 0: include the process tree
    blob = PROPVARIANT(VT_BLOB, (0, 0, 0), ctypes.sizeof(params), ctypes.addressof(params))
    handler, op, unknown, client, result = Activated(), ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p(), w.LONG()
    try:
        mmdevapi.ActivateAudioInterfaceAsync("VAD\\Process_Loopback", IAUDIOCLIENT, ctypes.byref(blob),
                                              ctypes.byref(handler.object), ctypes.byref(op))
        if not handler.done.wait(5):
            raise OSError("Windows did not open the app's sound")
        activate_result(op, ctypes.byref(result), ctypes.byref(unknown))
        if result.value < 0:
            raise ctypes.WinError(result.value)
        query_interface(unknown, IAUDIOCLIENT, ctypes.byref(client))
    finally:
        for obj in (unknown, op):
            if obj:
                release(obj)
    return client


def device_client(dev: int) -> ctypes.c_void_p:
    """An IAudioClient on the WASAPI endpoint behind MME output dev (MME reports each of its devices' endpoint id)."""
    size = ctypes.c_size_t()
    winmm.waveOutMessage(dev, ENDPOINT_ID_SIZE, ctypes.addressof(size), 0)
    endpoint = ctypes.create_unicode_buffer(size.value // 2 + 1)
    winmm.waveOutMessage(dev, ENDPOINT_ID, ctypes.addressof(endpoint), size.value)
    enum, device, client = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    try:
        ole32.CoCreateInstance(ENUMERATOR, None, CLSCTX_ALL, IMMDEVICEENUMERATOR, ctypes.byref(enum))
        get_device(enum, endpoint.value, ctypes.byref(device))
        activate(device, IAUDIOCLIENT, CLSCTX_ALL, None, ctypes.byref(client))
    finally:
        for obj in (device, enum):
            if obj:
                release(obj)
    return client


class Loopback:
    """The music, 20 ms at a time: one app's sound (source = its exe name, as OBS's Application Audio Capture) or
    everything on an output device (source = its MME id, as OBS's Desktop Audio).
    Lives on the sound thread: COM is started for that thread here."""

    def __init__(self, source: int | str):
        ole32.CoInitializeEx(None, 0)  # multithreaded
        self.client, self.capture = ctypes.c_void_p(), ctypes.c_void_p()
        self.buf, self.flowing = bytearray(), False
        try:
            if isinstance(source, str):
                pid = app_pid(source)
                if pid is None:
                    raise OSError(f"{source} isn't running. Start it, then pick it again")
                self.client = app_client(pid)
            else:
                self.client = device_client(source)
            initialize(self.client, 0, LOOPBACK_FLAGS, 2_000_000, 0, ctypes.byref(FORMAT), None)  # 200 ms buffer
            get_service(self.client, IAUDIOCAPTURECLIENT, ctypes.byref(self.capture))
            start(self.client)
        except OSError as e:
            self.close()
            raise OSError(f"Music from: {e}") from e

    def block(self) -> bytes:
        """The next 20 ms. Silence while nothing plays: loopback then delivers nothing at all."""
        n, data, frames, flags = w.UINT(), ctypes.c_void_p(), w.UINT(), w.DWORD()
        next_packet(self.capture, ctypes.byref(n))
        while n.value:
            get_buffer(self.capture, ctypes.byref(data), ctypes.byref(frames), ctypes.byref(flags), None, None)
            size = frames.value * CHANNELS * 2
            self.buf += bytes(size) if flags.value & BUFFER_SILENT else ctypes.string_at(data, size)
            release_buffer(self.capture, frames.value)
            next_packet(self.capture, ctypes.byref(n))
        # ponytail: the music runs on its device's clock, not the mic's. A 40 ms cushion soaks up jitter and drift
        # gets trimmed (a 20-80 ms skip now and then); resample if that ever shows
        if len(self.buf) > 6 * BLOCK:
            del self.buf[:-2 * BLOCK]
        if len(self.buf) < (BLOCK if self.flowing else 2 * BLOCK):
            self.flowing = False
            return SILENCE
        self.flowing, chunk = True, bytes(self.buf[:BLOCK])
        del self.buf[:BLOCK]
        return chunk

    def close(self) -> None:
        if self.client:
            with contextlib.suppress(OSError):
                stop(self.client)
        for obj in (self.capture, self.client):
            if obj:
                release(obj)
        ole32.CoUninitialize()


class Engine(threading.Thread):
    """Mic in, music on top, out to Send to. The mic's clock paces it all.
    `shared` is what the window sets (gain) and reads back (peak); each field is swapped whole."""

    def __init__(self, shared: SimpleNamespace, mic: int, out: int, music: int | str | None):
        super().__init__(daemon=True)
        self.shared, self.devs, self.running, self.error, self.warning = shared, (mic, out, music), True, "", ""
        self.start()

    def run(self) -> None:
        mic = out = music = None
        try:
            if self.devs[2] == self.devs[1]:
                raise OSError("Music from is the device you send to, so the mix would loop into itself. If the "
                              "cable became your Windows default output, set your headphones back as the default.")
            if devices("waveIn")[self.devs[0]].upper().startswith("CABLE"):
                raise OSError("Microphone is the cable, so the mix would loop into itself. If the cable became your "
                              "Windows default mic, set your real mic back as the default, or pick it here.")
            mic = Mic(self.devs[0])
            out = Speaker(self.devs[1])
            try:
                music = None if self.devs[2] is None else Loopback(self.devs[2])
            except OSError as e:  # no music is no reason to cut the voice
                self.warning = f"Live: your mic only. {e}."
            stalls = 0
            while self.running:
                voice = mic.read()
                if voice is None:
                    stalls += 1
                    if stalls == 4:  # 2 s of nothing from a mic that sends every 20 ms
                        raise OSError("The microphone stopped sending sound (unplugged?). Plug it back in, then "
                                      "pick it again.")
                    continue
                stalls = 0
                try:
                    tune = music.block() if music else SILENCE
                except OSError:
                    music.close()
                    music, tune = None, SILENCE
                    self.warning = ("Live: your mic only. The music stopped (app closed or device unplugged): pick "
                                    "it again under Music from.")
                sent = mix(voice, tune, *self.shared.gain)
                out.write(sent)
                level = array("h", sent)
                self.shared.peak = max(self.shared.peak, max(level), -min(level))
        except Exception as e:  # a thread's error would vanish under pythonw: the window shows it instead
            self.error = str(e) or type(e).__name__
        finally:
            for dev in (music, out, mic):
                if dev:
                    dev.close()


def load() -> dict:
    with contextlib.suppress(OSError, ValueError):
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    return {}


import tkinter.font as tkfont  # noqa: E402  (the window's look from here down; the sound above needs neither)
import winreg  # noqa: E402

# Windows 11 dark (Fluent): layered greys and one accent, the user's Windows accent color
WINDOW, CARD, STROKE, FIELD, HOVER, RAIL = "#202020", "#2b2b2b", "#3a3a3a", "#373737", "#3f3f3f", "#5c5c5c"
TEXT, TEXT2, TEXT3 = "#ffffff", "#cfcfcf", "#9d9d9d"
ICONS = {"Microphone": "", "Music from": "", "Send to": ""}  # Segoe Fluent Icons / MDL2 Assets
K = 1.0  # screen pixels per 96-DPI pixel, set by theme(); fonts are in points and scale by themselves


def px(*n: float):
    r = [round(x * K) for x in n]
    return r[0] if len(r) == 1 else tuple(r)


def accent() -> str:
    """The Windows accent color (Settings > Personalization > Colors), which DWM keeps as 0xAABBGGRR."""
    with contextlib.suppress(OSError, TypeError):
        abgr = winreg.QueryValueEx(winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM"),
                                   "AccentColor")[0]
        return f"#{abgr & 255:02x}{abgr >> 8 & 255:02x}{abgr >> 16 & 255:02x}"
    return "#4cc2ff"


def disc(size: int, rings: list[tuple[float, str]], bg: str = CARD) -> tk.PhotoImage:
    """Concentric anti-aliased circles (radius in pixels, color), outermost first, on bg."""
    rgb, c = lambda h: [int(h[i:i + 2], 16) for i in (1, 3, 5)], size / 2

    def pixel(x: int, y: int) -> str:
        color, d = rgb(bg), math.hypot(x + 0.5 - c, y + 0.5 - c)
        for radius, ring in rings:
            a = min(1.0, max(0.0, radius - d + 0.5))
            color = [p + (q - p) * a for p, q in zip(color, rgb(ring))]
        return "#%02x%02x%02x" % tuple(round(v) for v in color)
    img = tk.PhotoImage(width=size, height=size)
    img.put(" ".join("{" + " ".join(pixel(x, y) for x in range(size)) + "}" for y in range(size)))
    return img


def card(parent: tk.Misc, pad: int = 0) -> tk.Frame:
    """A rounded card with a 1px edge, as in Windows Settings; fill the frame it returns."""
    back, r = tk.Canvas(parent, bg=WINDOW, highlightthickness=0, width=1, height=1), px(8)
    back.pack(fill="x", pady=(0, px(6)))
    body = tk.Frame(back, bg=CARD, padx=pad)
    item, edge = back.create_window(1, r, window=body, anchor="nw"), back.create_polygon(0, 0, 0, 0, smooth=True,
                                                                                         fill=CARD, outline=STROKE)

    def draw(_=None) -> None:
        w, h, c = back.winfo_width() - 1, body.winfo_reqheight() + 2 * r, 2 * r  # c: a spline corner, about r round
        back.configure(width=body.winfo_reqwidth() + 2, height=h + 1)
        back.itemconfigure(item, width=w - 1)
        back.coords(edge, c, 0, w - c, 0, w, 0, w, c, w, h - c, w, h, w - c, h, c, h, 0, h, 0, h - c, 0, c, 0, 0)
    body.bind("<Configure>", draw)
    back.bind("<Configure>", draw)
    return body


def theme(root: tk.Tk) -> str:
    """Windows 11 dark, in clam (the native "vista" theme ignores colors). Returns the accent color."""
    global K
    K = root.winfo_fpixels("1i") / 96
    have, lit = set(tkfont.families(root)), accent()
    pick = lambda *names: next((n for n in names if n in have), names[-1])  # noqa: E731
    text, strong = pick("Segoe UI Variable Text", "Segoe UI"), pick("Segoe UI Variable Text Semibold", "Segoe UI Semibold")
    icons = pick("Segoe Fluent Icons", "Segoe MDL2 Assets")
    if icons not in have:
        ICONS.clear()
    root.configure(bg=WINDOW)
    for k, v in (("background", "#2c2c2c"), ("foreground", TEXT), ("selectBackground", HOVER), ("font", (text, 10))):
        root.option_add(f"*TCombobox*Listbox.{k}", v)  # the drop-down list is a classic widget
    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", background=CARD, foreground=TEXT, bordercolor=STROKE, selectforeground=TEXT, font=(text, 10))
    s.configure("Title.TLabel", background=WINDOW, font=(pick("Segoe UI Variable Display Semib", "Segoe UI Semibold"), 15))
    s.configure("Note.TLabel", background=WINDOW, foreground=TEXT3, font=(text, 9))
    s.configure("Icon.TLabel", foreground=TEXT2, font=(icons, 13))
    s.configure("Dim.TLabel", foreground=TEXT2)
    s.configure("Value.TLabel", font=(strong, 10))
    s.configure("TCombobox", arrowcolor=TEXT2, arrowsize=px(9), padding=px(9, 5, 4, 5))
    s.map("TCombobox", bordercolor=[("focus", lit)], **dict.fromkeys(
        ("fieldbackground", "background", "lightcolor", "darkcolor", "selectbackground"), [("hover", HOVER), ("", FIELD)]))
    size, rail = px(20), tk.PhotoImage(width=2, height=px(20))  # Fluent's slider: a grey rail, a ringed accent thumb
    rail.put(CARD, to=(0, 0, 2, size))
    rail.put(RAIL, to=(0, (size - px(4)) // 2, 2, (size + px(4)) // 2))
    root.images = [rail, *(disc(size, [(size / 2, "#4e4e4e"), (size / 2 - 1, "#454545"), (px(r), lit)]) for r in (6, 7, 5)),
                   disc(px(32), [(px(16), lit), (px(11), WINDOW), (px(6), lit)], WINDOW)]  # the last: the window's icon
    s.element_create("Fluent.trough", "image", rail, sticky="ew")
    s.element_create("Fluent.slider", "image", root.images[1], ("pressed", root.images[3]), ("active", root.images[2]))
    s.layout("Fluent.Horizontal.TScale", [("Fluent.trough", {"sticky": "ew", "children": [
        ("Fluent.slider", {"side": "left", "sticky": ""})]})])
    root.iconphoto(True, root.images[-1])
    root.update_idletasks()
    with contextlib.suppress(AttributeError, OSError):  # a dark title bar, this window only
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4)
    return lit


class App(tk.Tk):
    def __init__(self):
        with contextlib.suppress(AttributeError, OSError):
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on scaled displays
        super().__init__()
        self.title("MicMix")
        lit = theme(self)
        self.configure(padx=px(20), pady=px(16))
        self.minsize(px(420), 0)
        saved = load()
        self.shared = SimpleNamespace(gain=(1.0, 0.0), peak=0)
        self.engine = None

        ttk.Label(self, text="MicMix", style="Title.TLabel").pack(anchor="w")
        ttk.Label(self, text="Your mic and your music, together in one mic", style="Note.TLabel").pack(
            anchor="w", pady=(0, px(14)))
        grid = card(self)
        grid.columnconfigure(2, weight=1)
        cable = next((d for d in devices("waveOut") if d.upper().startswith("CABLE") and "16CH" not in d.upper()), "")
        mics = lambda: [DEFAULT, *(d for d in devices("waveIn") if not d.upper().startswith("CABLE"))]  # no loops
        sources = lambda: [OFF, *playing_apps(), DEFAULT, *devices("waveOut")]  # one app, or all of a device
        self.mic = self.picker(grid, 0, "Microphone", mics, saved.get("mic"), DEFAULT)
        self.music = self.picker(grid, 1, "Music from", sources, saved.get("music"), OFF)
        self.out = self.picker(grid, 2, "Send to", lambda: devices("waveOut"), saved.get("out"), cable)

        fader = card(self, px(16))
        fader.columnconfigure(2, weight=1)
        for row, col, name in ((0, 0, "Voice"), (0, 3, "Music"), (2, 0, "Level")):
            ttk.Label(fader, text=name, style="Dim.TLabel").grid(row=row, column=col, sticky="w", padx=(0, px(6)),
                                                                 pady=px(4))
        self.levels = [ttk.Label(fader, style="Value.TLabel", width=4) for _ in range(2)]  # voice %, music %
        self.levels[0].grid(row=0, column=1, sticky="w")
        self.levels[1].grid(row=0, column=4, sticky="w")
        self.fade = tk.DoubleVar(value=float(saved.get("fade", 0.25)))
        ttk.Scale(fader, from_=0, to=1, variable=self.fade, command=self.set_fade,
                  style="Fluent.Horizontal.TScale").grid(row=1, column=0, columnspan=5, sticky="ew", pady=px(4, 6))
        self.meter = tk.Canvas(fader, width=1, height=px(4), bg=CARD, highlightthickness=0)
        self.meter.grid(row=2, column=1, columnspan=4, sticky="ew")
        for tag, color in (("rail", STROKE), ("level", lit)):
            self.meter.create_line(0, 0, 0, 0, width=px(4), capstyle="round", fill=color, tags=tag)
        self.status = ttk.Label(self, style="Note.TLabel", foreground=TEXT2)
        self.status.pack(fill="x", padx=px(2), pady=(px(6), 0))
        self.status.bind("<Configure>", lambda e: self.status.configure(wraplength=e.width))  # wrap at window width

        self.protocol("WM_DELETE_WINDOW", self.close)
        self.set_fade()
        self.restart()
        self.poll()

    def picker(self, grid: tk.Frame, row: int, label: str, names, saved: str | None, fallback: str) -> tk.StringVar:
        """A dropdown. Its list is read again each time it opens, so a device plugged in or an app started later shows
        up. A picked app stays picked while it isn't playing."""
        var = tk.StringVar(value=saved if saved in names() or is_app(str(saved)) else fallback)
        if row:  # a hairline between rows, edge to edge
            tk.Frame(grid, height=1, bg=WINDOW).grid(row=2 * row - 1, column=0, columnspan=3, sticky="ew")
        ttk.Label(grid, text=ICONS.get(label, ""), style="Icon.TLabel").grid(row=2 * row, column=0, padx=(px(16), 0))
        ttk.Label(grid, text=label).grid(row=2 * row, column=1, sticky="w", padx=px(12, 16))
        box = ttk.Combobox(grid, textvariable=var, state="readonly", width=30)
        box.configure(postcommand=lambda: box.configure(values=names()))
        box.grid(row=2 * row, column=2, sticky="ew", padx=(0, px(14)), pady=px(9))
        box.bind("<<ComboboxSelected>>", self.restart)
        return var

    def restart(self, *_) -> None:
        """Open the picked devices (after closing the old ones). Without a Send to there is nothing to run."""
        self.stop()
        mic = device_id("waveIn", self.mic.get())
        if mic in (None, WAVE_MAPPER):  # default, or gone: use a real id, so a cable that became the default is caught
            mic = default_device("waveIn")
        out = device_id("waveOut", self.out.get())
        music = self.music.get()
        if not is_app(music):  # a whole device, or Off
            music = device_id("waveOut", music)
            if music == WAVE_MAPPER:
                music = default_device("waveOut")  # loopback needs a real device, also checked against Send to
        self.engine = None if out is None else Engine(self.shared, mic, out, music)
        self.save()

    def stop(self) -> None:
        if self.engine:
            self.engine.running = False
            self.engine.join(1)

    def set_fade(self, *_) -> None:
        voice, music = gains(self.fade.get())
        self.shared.gain = voice, music
        for label, gain in zip(self.levels, (voice, music)):
            label.configure(text=f"{gain:.0%}")

    def poll(self) -> None:
        level = max(0, 100 + 100 / 3 * math.log10(max(self.shared.peak, 1) / 32768))  # the top 60 dB
        r, w = self.meter.winfo_height() / 2, self.meter.winfo_width()  # a pill: round caps, r from each end
        self.meter.coords("rail", r, r, w - r, r)
        self.meter.coords("level", r, r, r + (w - 2 * r) * level / 100, r)
        self.shared.peak = 0
        music = self.music.get()
        if self.engine and self.engine.error:
            text = self.engine.error
        elif not self.engine:
            text = "Pick Send to. For a virtual mic, install VB-Cable first (see README.md)."
        elif self.engine.warning:
            text = self.engine.warning
        elif music == OFF:
            text = "Live: your mic only. Pick the app your music plays in under Music from."
        elif is_app(music):
            text = f"Live: your mic + {music}, nothing else."
        else:
            text = f"Live: your mic + everything playing on {music}."
        self.status.configure(text=text)
        self.after(100, self.poll)

    def save(self) -> None:
        data = {"mic": self.mic.get(), "music": self.music.get(), "out": self.out.get(),
                "fade": round(self.fade.get(), 3)}
        with contextlib.suppress(OSError):
            SETTINGS.write_text(json.dumps(data, indent=1), encoding="utf-8")

    def close(self) -> None:
        self.save()
        self.stop()
        self.destroy()


if __name__ == "__main__":
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = w.HANDLE
    running = k32.CreateMutexW(None, False, "Local\\MicMix")  # held until exit
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS: a second copy would send everything twice
        ctypes.windll.user32.MessageBoxW(None, "MicMix is already open. Look for it on the taskbar.", "MicMix", 0x40)
    else:
        App().mainloop()
