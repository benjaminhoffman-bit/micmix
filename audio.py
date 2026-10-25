"""MicMix's sound: the mic (through a noise gate), the music (one app, or everything on an output device) and the
soundboard, mixed into the output that feeds the virtual cable. The window is micmix.pyw.

Standard library only, all through ctypes: winmm (MME) for the mic and outputs, WASAPI loopback for the music, Media
Foundation to decode sound files, and Windows' default-device setting (for the installer to put back what VB-CABLE's
setup changed).
"""
import contextlib
import ctypes
import math
import os
import queue
import threading
import time
import uuid
from array import array
from ctypes import wintypes as w
from types import SimpleNamespace

RATE, CHANNELS = 48000, 2  # Discord and VB-Cable run at 48 kHz; Windows converts for any other device
BLOCK = RATE // 50 * CHANNELS * 2  # bytes in 20 ms of 16-bit stereo
SILENCE = bytes(BLOCK)
MIC_BUFS, OUT_BUFS, PAD = 8, 6, 2  # 160 ms of mic buffers; at most 120 ms queued per output; 40 ms cushion after a gap
WAVE_MAPPER, CALLBACK_EVENT, WHDR_DONE = 0xFFFFFFFF, 0x50000, 1
ENDPOINT_ID, ENDPOINT_ID_SIZE, PREFERRED_GET = 0x811, 0x812, 0x2015  # waveOutMessage: DRV_QUERYFUNCTIONINSTANCEID...
LOOPBACK_FLAGS, BUFFER_SILENT, CLSCTX_ALL = 0x88020000, 2, 23  # LOOPBACK | AUTOCONVERTPCM | SRC_DEFAULT_QUALITY
VOICE_FLAGS, SPEECH = 0x88040000, 9  # EVENTCALLBACK | AUTOCONVERTPCM | SRC_DEFAULT_QUALITY; AudioCategory_Speech
VT_BLOB, PROCESS_LOOPBACK, E_NOINTERFACE = 65, 1, -2147467262
FIRST_AUDIO, ALL_STREAMS, END_OF_STREAM = 0xFFFFFFFD, 0xFFFFFFFE, 2  # Media Foundation source reader
GATE_HOLD, GATE_FALL = 10, 0.2  # the gate stays open 200 ms (10 blocks) after the voice drops, then fades in 100 ms
STOP = "stop"  # on the soundboard queue: silence every sound
LONGEST_SOUND, TOO_LONG = 10 * 60 * RATE * CHANNELS * 2, "it's over 10 minutes long, too long for a sound"
DEFAULT, OFF = "Windows default", "Off"

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


class CLIENT_PROPERTIES(ctypes.Structure):  # AudioClientProperties
    _fields_ = [("cbSize", w.UINT), ("bIsOffload", w.BOOL), ("eCategory", ctypes.c_int), ("Options", ctypes.c_int)]


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
kernel32.OpenProcess.argtypes, kernel32.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
kernel32.CreateToolhelp32Snapshot.restype = w.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
kernel32.Process32FirstW.argtypes = kernel32.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, w.DWORD]
ole32.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p, w.DWORD, ctypes.c_void_p, PTR]
ole32.CoCreateInstance.restype = ctypes.HRESULT
ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
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
POLICYCONFIG, IPOLICYCONFIG = guid("870AF99C-171D-4F9E-AF0D-E63DF40C2BC9"), guid("F8679F50-850A-41CF-9C72-430F290290C8")
IAUDIOCLIENT2 = guid("726778CD-F60A-4EDA-82DE-E47610CD78AA")
HANDLER_IIDS = {uuid.UUID(i).bytes_le for i in ("00000000-0000-0000-C000-000000000046",  # IUnknown
                                                 "41D949AB-9862-444A-80F6-C261334DA5EB",  # the completion handler
                                                 "94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90")}  # IAgileObject: required
query_interface, release = com(0, ctypes.c_void_p, PTR), ctypes.WINFUNCTYPE(ctypes.c_ulong)(2, "Release")  # IUnknown
enum_endpoints, get_default = com(3, w.UINT, w.DWORD, PTR), com(4, w.UINT, w.UINT, PTR)  # IMMDeviceEnumerator
get_device = com(5, w.LPCWSTR, PTR)
device_count, device_item = com(3, ctypes.POINTER(w.UINT)), com(4, w.UINT, PTR)  # IMMDeviceCollection
activate, get_id = com(3, ctypes.c_void_p, w.DWORD, ctypes.c_void_p, PTR), com(5, PTR)  # IMMDevice
session_list = com(5, PTR)  # IAudioSessionManager2
session_count, get_session = com(3, ctypes.POINTER(ctypes.c_int)), com(4, ctypes.c_int, PTR)  # IAudioSessionEnumerator
session_pid = com(14, ctypes.POINTER(w.DWORD))  # IAudioSessionControl2
activate_result = com(3, ctypes.POINTER(w.LONG), PTR)  # IActivateAudioInterfaceAsyncOperation
initialize = com(3, ctypes.c_int, w.DWORD, ctypes.c_longlong, ctypes.c_longlong, ctypes.c_void_p, ctypes.c_void_p)
start, stop, get_service = com(10), com(11), com(14, ctypes.c_void_p, PTR)  # IAudioClient
set_event_handle, set_client_properties = com(13, w.HANDLE), com(16, ctypes.c_void_p)  # IAudioClient, IAudioClient2
get_buffer = com(3, PTR, ctypes.POINTER(w.UINT), ctypes.POINTER(w.DWORD), ctypes.c_void_p, ctypes.c_void_p)
release_buffer, next_packet = com(4, w.UINT), com(5, ctypes.POINTER(w.UINT))  # IAudioCaptureClient
set_default_endpoint = com(13, w.LPCWSTR, w.UINT)  # IPolicyConfig: undocumented, unchanged since Windows 7
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


def is_cable(name: str) -> bool:
    """An end of VB-CABLE, under either of the names Windows gives it ("CABLE Input (VB-Audio Virtual Cable)" or
    "Speakers (VB-Audio Virtual Cable)", cut to 31 characters by MME)."""
    return name.upper().startswith("CABLE") or "VB-AUDIO VIRTUAL" in name.upper()


def is_app(name: str) -> bool:
    """A Music from pick is an app (its exe name, in any case) rather than a whole device."""
    return name.lower().endswith(".exe")


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


def app_pid(exe: str) -> int | None:
    """The top process of exe's tree: the tree includes the helper processes that play the sound for Chrome, Spotify
    and the like. Of several separate trees, the one with sound open wins. Never MicMix itself, whose output would
    loop back in."""
    procs, exe = processes(), exe.lower()
    named = lambda pid: procs.get(pid, ("",))[0].lower() == exe  # noqa: E731
    if not any(map(named, procs)):  # not running: skip the slow look at sound sessions (the engine asks every 2 s)
        return None

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


def defaults() -> dict[str, str]:
    """Windows' default devices as {"flow role": endpoint id}: flow 0 output, 1 input; role 0 console, 1 multimedia,
    2 communications. For the installer: VB-CABLE's setup makes the cable the default, and this puts yours back."""
    ole32.CoInitializeEx(None, 0)
    enum, found = ctypes.c_void_p(), {}
    ole32.CoCreateInstance(ENUMERATOR, None, CLSCTX_ALL, IMMDEVICEENUMERATOR, ctypes.byref(enum))
    try:
        for flow in (0, 1):
            for role in (0, 1, 2):
                device, text = ctypes.c_void_p(), ctypes.c_void_p()
                with contextlib.suppress(OSError):  # no device of that kind at all
                    get_default(enum, flow, role, ctypes.byref(device))
                    get_id(device, ctypes.byref(text))
                    found[f"{flow} {role}"] = ctypes.wstring_at(text.value)
                    ole32.CoTaskMemFree(text)
                if device:
                    release(device)
    finally:
        release(enum)
    return found


def set_defaults(saved: dict[str, str]) -> list[str]:
    """Make these devices Windows' defaults again (see defaults()). Each role on its own, so a device that's gone
    doesn't keep the rest on the cable; returns the roles Windows refused."""
    ole32.CoInitializeEx(None, 0)
    policy, refused = ctypes.c_void_p(), []
    ole32.CoCreateInstance(POLICYCONFIG, None, CLSCTX_ALL, IPOLICYCONFIG, ctypes.byref(policy))
    try:
        for key, device in saved.items():
            try:
                set_default_endpoint(policy, device, int(key.split()[1]))
            except OSError:
                refused.append(key)
    finally:
        release(policy)
    return refused


def db(peak: int) -> float:
    return 20 * math.log10(max(peak, 1) / 32768)


def peak(block: bytes) -> int:
    a = array("h", block)
    return max(max(a), -min(a)) if a else 0


def gains(fade: float) -> tuple[float, float]:
    """Slider 0 (voice only) .. 1 (music only) -> (voice, music) gains. Both are full at the middle; squared, so the
    quiet end of each fade gets most of the slider's travel (music under your voice sits around 0.1-0.2)."""
    return min(1.0, 2 - 2 * fade) ** 2, min(1.0, 2 * fade) ** 2


def mix(*parts: tuple[bytes, float, float]) -> bytes:
    """Sum 16-bit blocks of the same size, each at a gain that slides from its first to its second value across the
    block (so fades don't click), clipped to 16 bits."""
    total = None
    for block, g0, g1 in parts:
        if g0 == g1 == 0:
            continue
        a = array("h", block)
        if g0 == g1:  # gains in 1/32768ths: integer math, exact at 0 and 1
            v = round(g0 * 32768)
            scaled = [s * v >> 15 for s in a]
        else:
            v0, dv = g0 * 32768, (g1 - g0) * 32768 * CHANNELS / len(a)
            scaled = [s * round(v0 + dv * (i // CHANNELS)) >> 15 for i, s in enumerate(a)]
        total = scaled if total is None else [t + s for t, s in zip(total, scaled)]
    if total is None:
        return bytes(len(parts[0][0]))
    return array("h", [min(32767, max(-32768, t)) for t in total]).tobytes()


class Gate:
    """Mutes the mic between words: opens at once when a block peaks above the threshold, stays open 200 ms after the
    voice drops, then fades out over 100 ms, so word endings aren't chopped."""

    def __init__(self):
        self.gain, self.hold = 1.0, 0

    def step(self, level: int, threshold: float | None) -> tuple[float, float]:
        """The gain at the start and at the end of this block. threshold is in dBFS; None means the gate is off."""
        start = self.gain
        if threshold is None or db(level) >= threshold:
            self.gain, self.hold = 1.0, GATE_HOLD
            return 1.0, 1.0
        if self.hold:
            self.hold -= 1
        else:
            self.gain = max(0.0, round(self.gain - GATE_FALL, 6))  # rounded: five falls land on 0, not 5e-17
        return start, self.gain


MF_MAJOR_TYPE, MF_SUBTYPE = guid("48EBA18E-F8C9-4687-BF11-0A74C9F96A8F"), guid("F7E34C9A-42E8-4714-B74B-CB29D72C35E5")
MF_AUDIO, MF_PCM = guid("73647561-0000-0010-8000-00AA00389B71"), guid("00000001-0000-0010-8000-00AA00389B71")
MF_CHANNELS, MF_RATE = guid("37E48BF5-645E-4C5B-89DE-ADA9E29B696A"), guid("5FAEEAE7-0290-4C31-9E8A-C534F68D9DBA")
MF_BITS = guid("F2DEB57F-40FA-4764-AA33-ED4F2D1FF669")
set_uint32, set_guid = com(21, ctypes.c_void_p, w.UINT), com(24, ctypes.c_void_p, ctypes.c_void_p)  # IMFAttributes
select_stream, set_media_type = com(4, w.DWORD, w.BOOL), com(7, w.DWORD, ctypes.c_void_p, ctypes.c_void_p)
read_sample = com(9, w.DWORD, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.POINTER(w.DWORD),  # IMFSourceReader
                  ctypes.POINTER(ctypes.c_longlong), PTR)
contiguous = com(41, PTR)  # IMFSample::ConvertToContiguousBuffer
lock, unlock = com(3, PTR, ctypes.POINTER(w.DWORD), ctypes.POINTER(w.DWORD)), com(4)  # IMFMediaBuffer


def decode(path: str) -> bytes:
    """A sound file as 48 kHz 16-bit stereo, decoded and converted by Windows (Media Foundation: MP3, WAV, M4A/AAC,
    WMA, FLAC and whatever else Windows plays). Raises OSError with a plain reason."""
    try:
        mfplat, mfread = ctypes.WinDLL("mfplat"), ctypes.WinDLL("mfreadwrite")
    except OSError as e:  # Windows "N" editions ship without Media Foundation
        raise OSError("this Windows can't decode sounds: install Microsoft's Media Feature Pack") from e
    mfplat.MFCreateMediaType.argtypes, mfplat.MFCreateMediaType.restype = [PTR], ctypes.HRESULT
    mfread.MFCreateSourceReaderFromURL.argtypes = [w.LPCWSTR, ctypes.c_void_p, PTR]
    mfread.MFCreateSourceReaderFromURL.restype = ctypes.HRESULT
    ole32.CoInitializeEx(None, 0)
    mfplat.MFStartup(0x20070, 0)  # MF_VERSION, full start; counted, so every call is fine
    reader, kind, pcm = ctypes.c_void_p(), ctypes.c_void_p(), bytearray()
    try:
        mfread.MFCreateSourceReaderFromURL(path, None, ctypes.byref(reader))
        select_stream(reader, ALL_STREAMS, False)  # cover art and the like stay undecoded
        select_stream(reader, FIRST_AUDIO, True)
        mfplat.MFCreateMediaType(ctypes.byref(kind))
        set_guid(kind, MF_MAJOR_TYPE, MF_AUDIO)
        set_guid(kind, MF_SUBTYPE, MF_PCM)
        for key, value in ((MF_BITS, 16), (MF_CHANNELS, CHANNELS), (MF_RATE, RATE)):
            set_uint32(kind, key, value)
        set_media_type(reader, FIRST_AUDIO, None, kind)  # the reader decodes, resamples and remixes to this
        while True:
            index, flags, when, sample = w.DWORD(), w.DWORD(), ctypes.c_longlong(), ctypes.c_void_p()
            read_sample(reader, FIRST_AUDIO, 0, ctypes.byref(index), ctypes.byref(flags), ctypes.byref(when),
                        ctypes.byref(sample))
            if sample:
                buf, data, size = ctypes.c_void_p(), ctypes.c_void_p(), w.DWORD()
                try:
                    contiguous(sample, ctypes.byref(buf))
                    lock(buf, ctypes.byref(data), None, ctypes.byref(size))
                    pcm += ctypes.string_at(data, size.value)
                    unlock(buf)
                finally:
                    for obj in (buf, sample):
                        if obj:
                            release(obj)
            if flags.value & END_OF_STREAM:
                break
            if len(pcm) > LONGEST_SOUND:  # it all stays in memory: 11 MB a minute
                raise OSError(TOO_LONG)
    except OSError as e:
        if str(e) == TOO_LONG:
            raise
        raise OSError(f"Windows can't play this file ({e.strerror or e})") from e
    finally:
        for obj in (kind, reader):
            if obj:
                release(obj)
    del pcm[len(pcm) // 4 * 4:]  # whole stereo frames; trimmed in place, not copied
    return bytes(pcm)


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

    def __init__(self, dev: int, label: str = "Send to"):
        super().__init__(dev, OUT_BUFS, label)
        for h in self.hdrs:
            h.dwFlags |= WHDR_DONE  # done = free to fill

    def write(self, data: bytes) -> None:
        free = [h for h in self.hdrs if h.dwFlags & WHDR_DONE]
        # ponytail: the mic's clock paces the outputs, so drift between the clocks costs a 20 ms gap or skip every
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


def endpoint(io: str, dev: int) -> str:
    """The WASAPI endpoint id behind MME device dev ("waveIn" or "waveOut"): MME reports it for each of its devices."""
    size = ctypes.c_size_t()
    getattr(winmm, io + "Message")(dev, ENDPOINT_ID_SIZE, ctypes.addressof(size), 0)
    text = ctypes.create_unicode_buffer(size.value // 2 + 1)
    getattr(winmm, io + "Message")(dev, ENDPOINT_ID, ctypes.addressof(text), size.value)
    return text.value


def device_client(dev: int, io: str = "waveOut") -> ctypes.c_void_p:
    """An IAudioClient on the WASAPI endpoint behind MME device dev."""
    enum, device, client = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    try:
        ole32.CoCreateInstance(ENUMERATOR, None, CLSCTX_ALL, IMMDEVICEENUMERATOR, ctypes.byref(enum))
        get_device(enum, endpoint(io, dev), ctypes.byref(device))
        activate(device, IAUDIOCLIENT, CLSCTX_ALL, None, ctypes.byref(client))
    finally:
        for obj in (device, enum):
            if obj:
                release(obj)
    return client


class VoiceMic:
    """The mic through Windows' own voice processing, 20 ms at a time: Mic's stand-in when voice isolation is on.
    It opens the mic as a speech app does (the Speech category), which brings in Voice Clarity's noise removal on
    Windows 11, or the mic driver's own. (The Communications category does the same here, but counts as a call, and
    then Windows lowers other sounds, the music included, by 80%.) Lives on the sound thread: COM is started for that
    thread here."""

    def __init__(self, dev: int):
        ole32.CoInitializeEx(None, 0)
        self.client, self.capture, self.buf = ctypes.c_void_p(), ctypes.c_void_p(), bytearray()
        self.event = kernel32.CreateEventW(None, False, False, None)
        client2 = ctypes.c_void_p()
        try:
            try:
                self.client = device_client(dev, "waveIn")
                query_interface(self.client, IAUDIOCLIENT2, ctypes.byref(client2))
                set_client_properties(client2, ctypes.byref(CLIENT_PROPERTIES(ctypes.sizeof(CLIENT_PROPERTIES), 0,
                                                                              SPEECH, 0)))
                initialize(self.client, 0, VOICE_FLAGS, 2_000_000, 0, ctypes.byref(FORMAT), None)  # 200 ms buffer
                set_event_handle(self.client, self.event)
                get_service(self.client, IAUDIOCAPTURECLIENT, ctypes.byref(self.capture))
                start(self.client)
            finally:  # released before close(), which ends COM on this thread
                if client2:
                    release(client2)
        except OSError as e:
            self.close()
            raise OSError(f"this mic can't do it ({e})") from e

    def read(self) -> bytes | None:
        """The next 20 ms, or None after half a second without it, as Mic.read (the engine counts those)."""
        deadline, n, data, frames, flags = time.monotonic() + 0.5, w.UINT(), ctypes.c_void_p(), w.UINT(), w.DWORD()
        while len(self.buf) < BLOCK:  # Windows hands over its own period (often 10 ms), not ours
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            kernel32.WaitForSingleObject(self.event, int(left * 1000) + 1)
            try:
                next_packet(self.capture, ctypes.byref(n))
                while n.value:
                    get_buffer(self.capture, ctypes.byref(data), ctypes.byref(frames), ctypes.byref(flags), None,
                               None)
                    size = frames.value * CHANNELS * 2
                    self.buf += bytes(size) if flags.value & BUFFER_SILENT else ctypes.string_at(data, size)
                    release_buffer(self.capture, frames.value)
                    next_packet(self.capture, ctypes.byref(n))
            except OSError:  # the mic went away: the engine's stall check says so in plain words
                return None
        chunk = bytes(self.buf[:BLOCK])
        del self.buf[:BLOCK]
        return chunk

    def close(self) -> None:
        if self.client:
            with contextlib.suppress(OSError):
                stop(self.client)
        for obj in (self.capture, self.client):
            if obj:
                release(obj)
        kernel32.CloseHandle(self.event)
        ole32.CoUninitialize()


class Loopback:
    """The music, 20 ms at a time: one app's sound (source = its exe name, as OBS's Application Audio Capture) or
    everything on an output device (source = its MME id, as OBS's Desktop Audio).
    Lives on the sound thread: COM is started for that thread here."""

    def __init__(self, source: int | str, pid: int | None = None):
        ole32.CoInitializeEx(None, 0)  # multithreaded
        self.client, self.capture, self.app = ctypes.c_void_p(), ctypes.c_void_p(), None
        self.buf, self.flowing = bytearray(), False
        try:
            if isinstance(source, str):
                pid = pid or app_pid(source)
                if pid is None:
                    raise OSError(f"{source} isn't running")
                self.app = kernel32.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE: loopback never says it closed
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
        if self.app and kernel32.WaitForSingleObject(self.app, 0) == 0:  # signalled: the app closed
            raise OSError("the app closed")
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
        if self.app:
            kernel32.CloseHandle(self.app)
        ole32.CoUninitialize()


def shared_state() -> SimpleNamespace:
    """What the window sets and the sound thread reads (and back). Every field is swapped whole, so no locks."""
    return SimpleNamespace(gain=(1.0, 0.0), muted=False, gate=None, board=0.5, peak=0, mic_peak=0, gate_open=True,
                           clips=queue.Queue())


class Engine(threading.Thread):
    """Mic in (through the gate), music and soundboard on top, out to Send to; the sounds also to monitor, your own
    headphones, when there is one. The mic's clock paces it all."""

    def __init__(self, shared: SimpleNamespace, mic: int | None, out: int, music: int | str | None,
                 monitor: int | None, isolate: bool = False):
        super().__init__(daemon=True)
        self.shared, self.devs, self.running, self.error, self.warning = shared, (mic, out, music, monitor), True, "", ""
        self.isolate, self.found = isolate, None
        self.start()

    def note(self, text: str) -> None:
        """Add to what the status line says: one fallback doesn't hide another."""
        self.warning = " ".join(filter(None, (self.warning, text)))

    def unnote(self, text: str) -> None:
        """Take a note back once it no longer holds."""
        self.warning = " ".join(self.warning.replace(text, "").split())

    def look(self, exe: str) -> None:
        """Whether the Music from app runs now. On its own thread: a look at every process takes ~40 ms with a thousand
        of them, and the sound has only ~40 ms queued."""
        self.found = app_pid(exe)

    def run(self) -> None:
        mic = out = music = monitor = None
        mic_dev, out_dev, music_src, monitor_dev = self.devs
        s = self.shared
        with contextlib.suppress(queue.Empty):  # pad presses made while nothing ran must not all play at once now
            while True:
                s.clips.get_nowait()
        lost = ""  # what the status line says while the music is missing
        try:
            if music_src == out_dev:  # it would loop into itself: no music, but the voice goes on
                music_src = None
                self.note("Live: your mic only. Music from is the device you send to, so the mix would loop into "
                          "itself. If the cable became your Windows default output, set your headphones back as the "
                          "default.")
            names = devices("waveIn")
            if all(map(is_cable, names)):  # none at all, or only the cable
                raise OSError("No microphone found. Plug one in, then pick it under Microphone.")
            if mic_dev is None or mic_dev >= len(names):
                raise OSError("Your microphone is gone (unplugged?). Plug it back in, then pick it under Microphone.")
            if is_cable(names[mic_dev]):
                raise OSError("Microphone is the cable, so the mix would loop into itself. If the cable became your "
                              "Windows default mic, set your real mic back as the default, or pick it here.")
            if self.isolate:
                try:
                    mic = VoiceMic(mic_dev)
                except OSError as e:  # some mics refuse speech mode: the plain mic still works
                    self.note(f"Live, but without voice isolation: {e}.")
            mic = mic or Mic(mic_dev)
            out = Speaker(out_dev)
            try:
                music = None if music_src is None else Loopback(music_src)
            except OSError as e:  # no music is no reason to cut the voice
                lost = f"Live: your mic only. {e}." + (" It comes in once it runs." if isinstance(music_src, str) else "")
                self.note(lost)
            if music and music_src == monitor_dev:  # music takes your headphones whole: it would carry the sounds
                self.note("Live. Hear them too is off while Music from is your headphones.")  # into the mix twice
            elif monitor_dev is not None and monitor_dev != out_dev:
                with contextlib.suppress(OSError):  # hearing your own sounds is a nicety, never a reason to stop
                    monitor = Speaker(monitor_dev, "Your headphones")
            gate, playing, stalls, ticks = Gate(), [], 0, 0
            last = (0.0 if s.muted else s.gain[0]), s.gain[1], s.board  # gains fade from block to block: no clicks
            while self.running:
                voice = mic.read()
                if voice is None:
                    stalls += 1
                    if stalls == 4:  # 2 s of nothing from a mic that sends every 20 ms
                        raise OSError("The microphone stopped sending sound (unplugged?). Plug it back in, then "
                                      "pick it again.")
                    continue
                stalls, ticks = 0, ticks + 1
                if not music and isinstance(music_src, str):  # the app isn't running: look for it every 2 s
                    if self.found:  # a look found it
                        with contextlib.suppress(OSError):
                            music = Loopback(music_src, self.found)
                            self.unnote(lost)
                        self.found = None
                    elif ticks % 100 == 0:
                        threading.Thread(target=self.look, args=(music_src,), daemon=True).start()
                try:
                    tune = music.block() if music else SILENCE
                except OSError:
                    music.close()
                    music, tune = None, SILENCE
                    lost = (f"Live: your mic only. {music_src} closed. Its music comes back when it runs again."
                            if isinstance(music_src, str) else
                            "Live: your mic only. The music stopped (device unplugged?): pick it again under Music from.")
                    self.note(lost)
                level = peak(voice)
                g0, g1 = gate.step(level, s.gate)
                now = (0.0 if s.muted else s.gain[0]), s.gain[1], s.board
                sounds = self.sounds(playing, s, last[2], now[2])
                parts = [(voice, last[0] * g0, now[0] * g1), (tune, last[1], now[1])]
                if sounds:
                    parts.append((sounds, 1.0, 1.0))
                last = now
                sent = mix(*parts)
                out.write(sent)
                if monitor:
                    try:
                        monitor.write(sounds or SILENCE)
                    except OSError:  # headphones unplugged: the voice to Discord carries on
                        monitor.close()
                        monitor = None
                        self.note("Live. Your headphones went away, so you no longer hear your sounds.")
                s.peak, s.mic_peak, s.gate_open = max(s.peak, peak(sent)), max(s.mic_peak, level), g1 > 0
        except Exception as e:  # a thread's error would vanish under pythonw: the window shows it instead
            self.error = str(e) or type(e).__name__
        finally:
            for dev in (monitor, music, out, mic):
                if dev:
                    dev.close()

    @staticmethod
    def sounds(playing: list, s: SimpleNamespace, b0: float | None = None, b1: float | None = None) -> bytes | None:
        """The soundboard's next 20 ms, at a volume going from b0 to b1 (default: the board's), or None when nothing
        plays. playing: [pcm, position] pairs."""
        fading = []  # stopped mid-play: they fade out over this block, so Stop doesn't click
        with contextlib.suppress(queue.Empty):
            while True:
                clip = s.clips.get_nowait()
                if clip == STOP:
                    fading += [p for p in playing if p[1]]  # one that never started just goes
                    playing.clear()
                else:
                    playing.append([clip, 0])
        if not playing and not fading:
            return None
        b0, b1 = s.board if b0 is None else b0, s.board if b1 is None else b1
        parts = [(pcm[at:at + BLOCK].ljust(BLOCK, b"\0"), b0, b1) for pcm, at in playing]
        parts += [(pcm[at:at + BLOCK].ljust(BLOCK, b"\0"), b0, 0.0) for pcm, at in fading]
        for p in playing:
            p[1] += BLOCK
        playing[:] = [p for p in playing if p[1] < len(p[0])]
        return mix(*parts)
