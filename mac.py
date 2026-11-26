"""MicMix on a Mac: everything macOS does differently from Windows, so audio.py and micmix.pyw stay one app.

Sound (audio.py takes these in place of its Windows ones): AudioQueue for the mic and the outputs, a Core Audio tap
for the music (one app's sound, or every app's but Discord's: macOS 14.2 or later), the voice-processing unit for
voice isolation (macOS's Mic Mode) and ExtAudioFile to decode sounds. For micmix.pyw: the menu bar icon, keybinds
that work in any app (Carbon hot keys: no permission needed), the screen's free area, the Dock icon and the brand
fonts. Standard library only: ctypes into Apple's frameworks and the Objective-C runtime.

Import audio, not this file, first: audio.py imports the sound half of this file at its end.
"""
import collections
import contextlib
import ctypes
import os
import queue
import struct
import threading
from ctypes import POINTER, byref, c_bool, c_char_p, c_double, c_int, c_int32, c_long, c_uint32, c_ulong, c_void_p
from types import SimpleNamespace

from audio import BLOCK, CHANNELS, LONGEST_SOUND, MIC_BUFS, OUT_BUFS, PAD, RATE, SILENCE, TOO_LONG, WAVE_MAPPER

DEFAULT, ALL_APPS = "Mac default", "All apps (not Discord)"
ME = "com.silvades.micmix"  # MicMix.app's bundle id; also the start of its own devices' ids, which no list shows


def load(name: str) -> ctypes.CDLL:
    return ctypes.CDLL(f"/System/Library/Frameworks/{name}.framework/{name}")


objc, libc = ctypes.CDLL("/usr/lib/libobjc.A.dylib"), ctypes.CDLL(None)
cf, ca, at = load("CoreFoundation"), load("CoreAudio"), load("AudioToolbox")
for framework in ("Foundation", "AppKit", "AVFoundation"):  # their classes, for the Objective-C runtime
    load(framework)
carbon, coretext = load("Carbon"), load("CoreText")


def code(text: str) -> int:
    """A four-letter code ('dev#') as the number Core Audio uses."""
    return struct.unpack(">I", text.encode())[0]


def says(status: int) -> str:
    """An OSStatus in words where it's a four-letter code, else its number."""
    raw = struct.pack(">i", status)
    return f"'{raw.decode()}'" if all(32 <= b < 127 for b in raw) else f"error {status}"


# --- Objective-C, through its C runtime ---
for name, res, args in (("objc_getClass", c_void_p, [c_char_p]), ("sel_registerName", c_void_p, [c_char_p]),
                        ("objc_allocateClassPair", c_void_p, [c_void_p, c_char_p, ctypes.c_size_t]),
                        ("class_addMethod", c_bool, [c_void_p, c_void_p, c_void_p, c_char_p]),
                        ("objc_registerClassPair", None, [c_void_p]), ("objc_autoreleasePoolPush", c_void_p, []),
                        ("objc_autoreleasePoolPop", None, [c_void_p])):
    getattr(objc, name).restype, getattr(objc, name).argtypes = res, args
SENDS: dict = {}


def send(obj, method: str, *args, ret=c_void_p, types: tuple = ()):
    """[obj method...]. Arguments are ids unless `types` says otherwise (ints, doubles, BOOLs, structs by value)."""
    types = types or (c_void_p,) * len(args)
    if (ret, types) not in SENDS:  # objc_msgSend called through the method's own C type, as Apple asks
        stret = ret is NSRect and os.uname().machine == "x86_64"  # Intel returns big structs through memory
        SENDS[ret, types] = ctypes.CFUNCTYPE(ret, c_void_p, c_void_p, *types)(
            ("objc_msgSend_stret" if stret else "objc_msgSend", objc))
    return SENDS[ret, types](obj, objc.sel_registerName(method.encode()), *args)


def cls(name: str) -> int:
    return objc.objc_getClass(name.encode())


def ns(text: str) -> int:
    return send(cls("NSString"), "stringWithUTF8String:", text.encode(), types=(c_char_p,))


def text(obj) -> str:
    raw = send(obj, "UTF8String", ret=c_char_p) if obj else None
    return raw.decode() if raw else ""


def number(n: int) -> int:
    return send(cls("NSNumber"), "numberWithLongLong:", n, types=(ctypes.c_longlong,))


def array(items: list) -> int:
    return send(cls("NSArray"), "arrayWithObjects:count:", (c_void_p * len(items))(*items), len(items),
                types=(c_void_p, c_ulong))


def dictionary(pairs: dict) -> int:
    keys, values = [ns(k) for k in pairs], list(pairs.values())
    return send(cls("NSDictionary"), "dictionaryWithObjects:forKeys:count:", (c_void_p * len(values))(*values),
                (c_void_p * len(keys))(*keys), len(keys), types=(c_void_p, c_void_p, c_ulong))


@contextlib.contextmanager
def pool():
    """Frees what Objective-C made along the way: off the main thread nothing else would."""
    mark = objc.objc_autoreleasePoolPush()
    try:
        yield
    finally:
        objc.objc_autoreleasePoolPop(mark)


class NSRect(ctypes.Structure):
    _fields_ = [("x", c_double), ("y", c_double), ("w", c_double), ("h", c_double)]


def app() -> int:
    """NSApp: Tk's own application object."""
    return send(cls("NSApplication"), "sharedApplication")


# --- Core Audio: devices and processes ---
class Address(ctypes.Structure):  # AudioObjectPropertyAddress
    _fields_ = [("selector", c_uint32), ("scope", c_uint32), ("element", c_uint32)]


SYSTEM, GLOBAL, INPUT, OUTPUT = 1, code("glob"), code("inpt"), code("outp")
ca.AudioObjectGetPropertyDataSize.argtypes = [c_uint32, POINTER(Address), c_uint32, c_void_p, POINTER(c_uint32)]
ca.AudioObjectGetPropertyData.argtypes = [c_uint32, POINTER(Address), c_uint32, c_void_p, POINTER(c_uint32), c_void_p]
cf.CFStringCreateWithCString.restype, cf.CFStringCreateWithCString.argtypes = c_void_p, [c_void_p, c_char_p, c_uint32]
cf.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_long, c_uint32]
cf.CFRelease.argtypes = [c_void_p]
cf.CFURLCreateFromFileSystemRepresentation.restype = c_void_p
cf.CFURLCreateFromFileSystemRepresentation.argtypes = [c_void_p, c_char_p, c_long, c_bool]
UTF8 = 0x08000100


def prop(obj: int, selector: str, kind=c_uint32, scope: int = GLOBAL, qualifier=None):
    """One property of a Core Audio object: a number, a list of object ids (kind=list) or a string (kind=str);
    None when it has none."""
    address, size = Address(code(selector), scope, 0), c_uint32()
    extra = (ctypes.sizeof(qualifier), byref(qualifier)) if qualifier is not None else (0, None)
    if ca.AudioObjectGetPropertyDataSize(obj, byref(address), *extra, byref(size)):
        return None
    value = (c_uint32 * (size.value // 4))() if kind is list else c_void_p() if kind is str else kind()
    if ca.AudioObjectGetPropertyData(obj, byref(address), *extra, byref(size), byref(value)):
        return None
    if kind is list:
        return list(value)
    if kind is str:
        return cfstring(value.value)
    return value.value


def prop_struct(obj: int, selector: str, kind, scope: int = GLOBAL):
    """A property that is a C struct (an AudioStreamBasicDescription, say), or None."""
    address, value = Address(code(selector), scope, 0), kind()
    size = c_uint32(ctypes.sizeof(value))
    return None if ca.AudioObjectGetPropertyData(obj, byref(address), 0, None, byref(size), byref(value)) else value


def cfstring(ref, release: bool = True) -> str:
    if not ref:
        return ""
    out = ctypes.create_string_buffer(1024)
    cf.CFStringGetCString(ref, out, 1024, UTF8)
    if release:
        cf.CFRelease(ref)
    return out.value.decode(errors="replace")


def all_devices(io: str) -> list[tuple[int, str, str]]:
    """(id, name, uid) of every input ("waveIn") or output ("waveOut") device, in Core Audio's order. Leaves out
    hidden devices and the ones MicMix and macOS's voice processing make for themselves, which come and go as the
    sound starts and stops (the window would take that for a device plugged in, and start the sound again)."""
    found, scope = [], INPUT if io == "waveIn" else OUTPUT
    for dev in prop(SYSTEM, "dev#", list) or []:
        name, uid = prop(dev, "lnam", str) or f"Device {dev}", prop(dev, "uid ", str) or ""
        if (prop(dev, "stm#", list, scope) and not prop(dev, "hidn") and not uid.startswith(ME)
                and not name.startswith("CADefaultDeviceAggregate")):
            found.append((dev, name, uid))
    return found


def devices(io: str) -> list[str]:
    """Names of the input ("waveIn") or output ("waveOut") devices; a name's index is its device id here."""
    return [name for _, name, _ in all_devices(io)]


def device_id(io: str, name: str) -> int | None:
    """A picked name's device id: WAVE_MAPPER for the Mac default, None for Off or a device that is gone."""
    if name == DEFAULT:
        return WAVE_MAPPER
    names = devices(io)
    return names.index(name) if name in names else None


def default_device(io: str) -> int | None:
    """The id of the Mac's default input ("waveIn") or output ("waveOut"), None when there's none."""
    dev = prop(SYSTEM, "dIn " if io == "waveIn" else "dOut")
    ids = [d for d, _, _ in all_devices(io)]
    return ids.index(dev) if dev in ids else None


def bluetooth(io: str, dev: int) -> bool:
    found = all_devices(io)
    return dev < len(found) and prop(found[dev][0], "tran") == code("blue")


libc.proc_pidpath.argtypes = [c_int, c_char_p, c_uint32]
libc.responsibility_get_pid_responsible_for_pid.argtypes = [c_int]
libc.responsibility_get_pid_responsible_for_pid.restype = c_int


def bundle(path: str) -> str:
    """The app a program belongs to, from its path: the outermost .app ('/Applications/Google Chrome.app' for the
    helper deep inside it), or '' for a program outside one (a macOS service)."""
    at_app = path.find(".app/")
    return path[:at_app + 4] if at_app >= 0 else ""


def program(pid: int) -> str:
    out = ctypes.create_string_buffer(4096)
    return out.value.decode(errors="replace") if libc.proc_pidpath(pid, out, 4096) > 0 else ""


def sound_processes() -> list[tuple[int, int, str]]:
    """(Core Audio process object, pid, app) for every process macOS has a sound connection for, playing or not,
    the way Windows' volume mixer lists apps. app is the path of the .app it belongs to: browsers and Electron apps
    play from helper processes, and Safari from a WebKit service, so it's the app responsible for the process."""
    found = []
    for obj in prop(SYSTEM, "prs#", list) or []:  # macOS 14.2 or later
        pid = prop(obj, "ppid", c_int)
        if pid is None:
            continue
        owner = libc.responsibility_get_pid_responsible_for_pid(pid)
        found.append((obj, pid, bundle(program(owner if owner > 0 else pid)) or bundle(program(pid))))
    return found


def app_name(path: str) -> str:
    return os.path.basename(path)


def listed(pid: int, path: str) -> bool:
    """An app Music from offers: a real app (not a part of macOS), never Discord or MicMix itself."""
    name = app_name(path).lower()
    return bool(path) and not path.startswith("/System/Library/") and pid != os.getpid() and "discord" not in name \
        and name != "micmix.app"


def playing_apps() -> list[str]:
    """Apps with a sound connection, as Windows' volume mixer lists them; never Discord or MicMix."""
    return sorted({app_name(path) for _, pid, path in sound_processes() if listed(pid, path)}, key=str.lower)


def is_app(name: str) -> bool:
    """A Music from pick is an app ("Spotify.app"), or every app's sound."""
    return name.lower().endswith(".app") or name == ALL_APPS


def app_pid(name: str) -> int | None:
    """A process of that app with a sound connection, None while there's none: the app isn't running, or hasn't
    opened any sound yet. Every app's sound is always there."""
    if name == ALL_APPS:
        return os.getpid()
    return next((pid for _, pid, path in sound_processes() if app_name(path) == name and pid != os.getpid()), None)


# --- permissions ---
AV_AUDIO = c_void_p.in_dll(load("AVFoundation"), "AVMediaTypeAudio")


def mic_access() -> int:
    """Whether macOS lets MicMix use the mic: 0 not asked yet, 1 restricted, 2 denied, 3 allowed."""
    return send(cls("AVCaptureDevice"), "authorizationStatusForMediaType:", AV_AUDIO, ret=c_long)


try:  # macOS's own check for "System Audio Recording", private but steady since macOS 10.14; without it, no check
    _tcc = ctypes.CDLL("/System/Library/PrivateFrameworks/TCC.framework/TCC")
    _tcc.TCCAccessPreflight.argtypes, _tcc.TCCAccessPreflight.restype = [c_void_p, c_void_p], c_int
except (OSError, AttributeError):
    _tcc = None


def capture_access() -> int | None:
    """Whether macOS lets MicMix record other apps: 0 allowed, 1 denied, anything else not asked yet or unknown."""
    if not _tcc:
        return None
    service = cf.CFStringCreateWithCString(None, b"kTCCServiceAudioCapture", UTF8)
    try:
        return _tcc.TCCAccessPreflight(service, None)
    finally:
        cf.CFRelease(service)


PAGES = {"mic": "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
         "capture": "x-apple.systempreferences:com.apple.preference.security?Privacy_AudioCapture",
         "blackhole": "https://existential.audio/blackhole/"}


def open_page(page: str) -> None:
    """System Settings at Privacy & Security's Microphone ("mic") or System Audio Recording ("capture") page, or
    BlackHole's own page."""
    os.spawnlp(os.P_NOWAIT, "open", "open", PAGES[page])


# --- sound: AudioQueue ---
class ASBD(ctypes.Structure):  # AudioStreamBasicDescription
    _fields_ = [("rate", c_double), ("format", c_uint32), ("flags", c_uint32), ("packet_bytes", c_uint32),
                ("packet_frames", c_uint32), ("frame_bytes", c_uint32), ("channels", c_uint32), ("bits", c_uint32),
                ("reserved", c_uint32)]


class AQBuffer(ctypes.Structure):  # AudioQueueBuffer
    _fields_ = [("capacity", c_uint32), ("data", c_void_p), ("size", c_uint32), ("user", c_void_p),
                ("descs_capacity", c_uint32), ("descs", c_void_p), ("descs_count", c_uint32)]


FORMAT = ASBD(RATE, code("lpcm"), 0xC, CHANNELS * 2, 1, CHANNELS * 2, CHANNELS, 16, 0)  # signed, packed 16-bit
INPUT_DONE = ctypes.CFUNCTYPE(None, c_void_p, c_void_p, POINTER(AQBuffer), c_void_p, c_uint32, c_void_p)
OUTPUT_DONE = ctypes.CFUNCTYPE(None, c_void_p, c_void_p, POINTER(AQBuffer))
for name, args in (("AudioQueueNewInput", [POINTER(ASBD), INPUT_DONE, c_void_p, c_void_p, c_void_p, c_uint32,
                                           POINTER(c_void_p)]),
                   ("AudioQueueNewOutput", [POINTER(ASBD), OUTPUT_DONE, c_void_p, c_void_p, c_void_p, c_uint32,
                                            POINTER(c_void_p)]),
                   ("AudioQueueSetProperty", [c_void_p, c_uint32, c_void_p, c_uint32]),
                   ("AudioQueueAllocateBuffer", [c_void_p, c_uint32, POINTER(POINTER(AQBuffer))]),
                   ("AudioQueueEnqueueBuffer", [c_void_p, POINTER(AQBuffer), c_uint32, c_void_p]),
                   ("AudioQueueStart", [c_void_p, c_void_p]), ("AudioQueueStop", [c_void_p, c_bool]),
                   ("AudioQueueDispose", [c_void_p, c_bool])):
    getattr(at, name).argtypes, getattr(at, name).restype = args, c_int32


class Queue:
    """One AudioQueue on one device, with its ring of 20 ms buffers, which macOS fills (Mic) or plays (Speaker) on a
    thread of its own; MicMix's format (48 kHz, 16-bit stereo) is converted to the device's by macOS."""
    count = 0

    def __init__(self, uid: str, label: str, output: bool):
        self.label, self.handle, self.on = label, c_void_p(), True
        if output:
            self.done = OUTPUT_DONE(self.played)  # kept here: macOS calls it as long as the queue lives
            self.call("AudioQueueNewOutput", byref(FORMAT), self.done, None, None, None, 0, byref(self.handle))
        else:
            self.done = INPUT_DONE(self.filled)
            self.call("AudioQueueNewInput", byref(FORMAT), self.done, None, None, None, 0, byref(self.handle))
        try:
            device = c_void_p(cf.CFStringCreateWithCString(None, uid.encode(), UTF8))
            try:
                self.call("AudioQueueSetProperty", self.handle, code("aqcd"), byref(device), ctypes.sizeof(device))
            finally:
                cf.CFRelease(device)
            self.bufs = []
            for _ in range(self.count):
                buf = POINTER(AQBuffer)()
                self.call("AudioQueueAllocateBuffer", self.handle, BLOCK, byref(buf))
                self.bufs.append(buf)
        except OSError:
            self.close()
            raise

    def call(self, name: str, *args) -> None:
        if err := getattr(at, name)(*args):
            raise OSError(f"{self.label}: macOS said {says(err)}" if err != -66680 else
                          f"{self.label}: the device is gone (unplugged?)")  # kAudioQueueErr_InvalidDevice

    def filled(self, *_) -> None:
        pass

    def played(self, *_) -> None:
        pass

    def close(self) -> None:
        self.on = False  # what macOS hands back while stopping isn't queued again
        if self.handle:
            at.AudioQueueStop(self.handle, True)
            at.AudioQueueDispose(self.handle, True)  # synchronous: no callback after this
            self.handle = c_void_p()


class Mic(Queue):
    """The mic, 20 ms at a time."""
    count = MIC_BUFS

    def __init__(self, dev: int, uid: str | None = None, label: str = "Microphone"):
        if uid is None:
            if mic_access() in (1, 2):
                raise OSError("Microphone: macOS keeps MicMix off your mic. Open System Settings, then Privacy & "
                              "Security, then Microphone, and turn on MicMix.")
            uid = all_devices("waveIn")[dev][2]
        self.ready, self.part = queue.Queue(), bytearray()
        super().__init__(uid, label, output=False)
        try:
            for buf in self.bufs:
                self.call("AudioQueueEnqueueBuffer", self.handle, buf, 0, None)
            self.call("AudioQueueStart", self.handle, None)  # the first time, this waits for macOS's question
        except OSError as e:
            self.close()
            if label == "Microphone" and mic_access() == 0:
                raise OSError("Microphone: macOS asked whether MicMix may use your mic and got no answer. Pick your "
                              "mic again under Microphone to be asked again.") from e
            raise

    def filled(self, _user, handle, buf, *_) -> None:
        data = buf.contents
        self.part += ctypes.string_at(data.data, data.size)
        while len(self.part) >= BLOCK:  # macOS may hand over a little more or less than asked
            self.ready.put(bytes(self.part[:BLOCK]))
            del self.part[:BLOCK]
        while self.ready.qsize() > MIC_BUFS:  # nobody reads (a stall): the oldest goes, as Windows' ring overwrites
            with contextlib.suppress(queue.Empty):
                self.ready.get_nowait()
        if self.on:
            at.AudioQueueEnqueueBuffer(handle, buf, 0, None)

    def read(self) -> bytes | None:
        """The next 20 ms, or None when it isn't there within half a second (just ask again)."""
        try:
            return self.ready.get(timeout=0.5)
        except queue.Empty:
            return None


class Speaker(Queue):
    """An output: 20 ms written at a time, at most 120 ms queued."""
    count = OUT_BUFS

    def __init__(self, dev: int, label: str = "Send to"):
        self.free = collections.deque()
        super().__init__(all_devices("waveOut")[dev][2], label, output=True)
        self.free.extend(self.bufs)
        try:
            self.call("AudioQueueStart", self.handle, None)
        except OSError:
            self.close()
            raise

    def played(self, _user, _handle, buf) -> None:
        self.free.append(buf)  # done = free to fill

    def write(self, data: bytes) -> None:
        # ponytail: the mic's clock paces the outputs, as on Windows: a 20 ms gap or skip every few minutes
        if len(self.free) == OUT_BUFS:  # ran dry (just opened, or a stall): cushion it so the next hiccup isn't a click
            for _ in range(PAD):
                self.send(self.free.popleft(), SILENCE)
        if self.free:
            self.send(self.free.popleft(), data)

    def send(self, buf, data: bytes) -> None:
        ctypes.memmove(buf.contents.data, data, BLOCK)
        buf.contents.size = BLOCK
        self.call("AudioQueueEnqueueBuffer", self.handle, buf, 0, None)


# --- the music: a Core Audio tap (macOS 14.2 or later) ---
ca.AudioHardwareCreateProcessTap.argtypes = [c_void_p, POINTER(c_uint32)]
ca.AudioHardwareCreateAggregateDevice.argtypes = [c_void_p, POINTER(c_uint32)]
ca.AudioHardwareDestroyProcessTap.argtypes = ca.AudioHardwareDestroyAggregateDevice.argtypes = [c_uint32]
IOPROC = ctypes.CFUNCTYPE(c_int32, c_uint32, c_void_p, c_void_p, c_void_p, c_void_p, c_void_p, c_void_p)
ca.AudioDeviceCreateIOProcID.argtypes = [c_uint32, IOPROC, c_void_p, POINTER(c_void_p)]
ca.AudioDeviceStart.argtypes = ca.AudioDeviceStop.argtypes = ca.AudioDeviceDestroyIOProcID.argtypes = [c_uint32, c_void_p]
FEED = ctypes.CFUNCTYPE(c_int32, c_void_p, POINTER(c_uint32), c_void_p, c_void_p, c_void_p)
at.AudioConverterNew.argtypes = [POINTER(ASBD), POINTER(ASBD), POINTER(c_void_p)]
at.AudioConverterFillComplexBuffer.argtypes = [c_void_p, FEED, c_void_p, POINTER(c_uint32), c_void_p, c_void_p]
at.AudioConverterDispose.argtypes = [c_void_p]
EMPTY = code("more")  # what our feed says when it has nothing more for now (any status but 0 will do)


class Converter:
    """macOS's AudioConverter, from a device's format (the tap's: 32-bit float at the output's rate) to MicMix's."""

    def __init__(self, source: ASBD):
        self.handle, self.source, self.pending, self.held = c_void_p(), source, bytearray(), None
        self.feed = FEED(self.supply)  # kept: the converter calls it during convert()
        if err := at.AudioConverterNew(byref(source), byref(FORMAT), byref(self.handle)):
            raise OSError(f"macOS can't convert the app's sound ({says(err)})")

    def supply(self, _conv, packets, data, _descs, _user) -> int:
        frames = len(self.pending) // self.source.frame_bytes
        if not frames:
            packets[0] = 0
            return EMPTY
        self.held = ctypes.create_string_buffer(bytes(self.pending[:frames * self.source.frame_bytes]))
        del self.pending[:frames * self.source.frame_bytes]
        given = ctypes.cast(data, POINTER(AudioBufferList)).contents
        given.buffers[0].channels, given.buffers[0].size = self.source.channels, len(self.held) - 1
        given.buffers[0].data = ctypes.addressof(self.held)
        packets[0] = frames
        return 0

    def convert(self, raw: bytes) -> bytes:
        self.pending += raw
        room = int(len(self.pending) // self.source.frame_bytes * RATE / self.source.rate) + 64
        out = ctypes.create_string_buffer(room * CHANNELS * 2)
        into = AudioBufferList(1, (AudioBuffer * 1)(AudioBuffer(CHANNELS, len(out), ctypes.addressof(out))))
        frames = c_uint32(room)
        err = at.AudioConverterFillComplexBuffer(self.handle, self.feed, None, byref(frames), byref(into), None)
        if err not in (0, EMPTY):
            raise OSError(f"macOS can't convert the app's sound ({says(err)})")
        return out.raw[:frames.value * CHANNELS * 2]

    def close(self) -> None:
        if self.handle:
            at.AudioConverterDispose(self.handle)
            self.handle = c_void_p()


class Tap:
    """One app's sound (the stereo mix of its processes), or every app's but the ones left out, through a private
    device that only MicMix sees; the app keeps playing as before. AudioQueue can't record such a device, so macOS
    hands its sound straight to MicMix, on its sound thread, which only keeps it: take() converts it on the engine's."""

    def __init__(self, processes: frozenset, everything: bool):
        self.tap, self.device, self.proc, self.converter = c_uint32(), c_uint32(), c_void_p(), None
        self.arrived, self.callback = collections.deque(), IOPROC(self.hear)
        try:
            with pool():
                ids = array([number(p) for p in sorted(processes)])
                about = send(cls("CATapDescription"), "alloc")
                about = send(about, "initStereoGlobalTapButExcludeProcesses:" if everything else
                             "initStereoMixdownOfProcesses:", ids)
                if not about:
                    raise OSError("this Mac can't take in an app's sound (it needs macOS 14.2 or later)")
                send(about, "setPrivate:", True, types=(c_bool,))  # only MicMix sees it
                send(about, "setName:", ns("MicMix music"))
                uid = text(send(send(about, "UUID"), "UUIDString"))
                err = ca.AudioHardwareCreateProcessTap(about, byref(self.tap))
                send(about, "release")
                if err:
                    raise OSError(f"macOS wouldn't open the app's sound ({says(err)})")
                one = number(1)
                err = ca.AudioHardwareCreateAggregateDevice(dictionary({
                    "uid": ns(f"{ME}.music.{uid}"), "name": ns("MicMix music"), "private": one, "stacked": number(0),
                    "tapautostart": one, "taps": array([dictionary({"uid": ns(uid), "drift": one})])}), byref(self.device))
                if err:
                    raise OSError(f"macOS wouldn't open the app's sound ({says(err)})")
            source = prop_struct(self.tap.value, "tfmt", ASBD)  # the tap's own format
            if source is None:
                raise OSError("macOS didn't say what the app's sound is like")
            self.converter = Converter(source)
            err = ca.AudioDeviceCreateIOProcID(self.device.value, self.callback, None, byref(self.proc))
            if err or (err := ca.AudioDeviceStart(self.device.value, self.proc)):  # the first time, macOS asks
                raise OSError(f"macOS wouldn't start the app's sound ({says(err)})")
        except OSError as e:
            self.close()
            if capture_access() == 2:  # not asked yet, or asked and nobody answered
                raise OSError("macOS asked whether MicMix may record other apps' sound and got no answer. Pick it "
                              "again under Music from to be asked again") from e
            raise

    def hear(self, _device, _now, data, *_) -> int:
        """On macOS's sound thread: keep what came, nothing more."""
        given = ctypes.cast(data, POINTER(AudioBufferList)).contents
        if given.count:
            self.arrived.append(ctypes.string_at(given.buffers[0].data, given.buffers[0].size))
        return 0

    def take(self) -> bytes:
        """Everything that came since the last take, as MicMix's format."""
        raw = bytearray()
        while self.arrived:
            raw += self.arrived.popleft()
        return self.converter.convert(bytes(raw)) if raw else b""

    def close(self) -> None:
        if self.proc:
            ca.AudioDeviceStop(self.device.value, self.proc)
            ca.AudioDeviceDestroyIOProcID(self.device.value, self.proc)
            self.proc = c_void_p()
        if self.device.value:
            ca.AudioHardwareDestroyAggregateDevice(self.device)
            self.device = c_uint32()
        if self.tap.value:
            ca.AudioHardwareDestroyProcessTap(self.tap)
            self.tap = c_uint32()
        if self.converter:
            self.converter.close()


class Loopback:
    """The music, 20 ms at a time: one app's sound (source = "Spotify.app"), as OBS's Application Audio Capture, or
    every app's but Discord's and MicMix's own (ALL_APPS). Its processes are looked at again every 2 s: a browser
    starts the helper that plays its sound only when a tab first plays, and Discord can start later (its sound must
    stay out, or your friends hear themselves). A change opens a new tap beside the old one, then swaps them."""

    def __init__(self, source: str, pid: int | None = None):
        self.source, self.everything = source, source == ALL_APPS
        self.buf, self.flowing, self.blocks, self.fresh, self.gone = bytearray(), False, 0, None, False
        if capture_access() == 1:
            raise OSError("Music from: macOS keeps MicMix from recording other apps. Open System Settings, then Privacy "
                          "& Security, then Screen & System Audio Recording, and turn on MicMix under System Audio "
                          "Recording Only")
        self.processes = self.wanted()
        if not self.processes and not self.everything:
            raise OSError(f"Music from: {source} isn't running")
        try:
            self.tap = Tap(self.processes, self.everything)
        except OSError as e:
            raise OSError(f"Music from: {e}") from e

    def wanted(self) -> frozenset:
        """The app's processes; or, for every app, the ones left out: Discord's and MicMix's."""
        if self.everything:
            return frozenset(obj for obj, pid, path in sound_processes()
                             if pid == os.getpid() or "discord" in app_name(path).lower())
        return frozenset(obj for obj, pid, path in sound_processes() if app_name(path) == self.source)

    def look(self) -> None:
        """On its own thread: a look at every process takes a few ms, a new tap tens of them."""
        now = self.wanted()
        if not now and not self.everything:
            self.gone = True
        elif now != self.processes:
            with contextlib.suppress(OSError):  # no new tap: the old one goes on, and the next look tries again
                self.fresh, self.processes = Tap(now, self.everything), now

    def block(self) -> bytes:
        """The next 20 ms. Silence while nothing plays (a tap sends silence then)."""
        if self.gone:
            raise OSError("the app closed")
        self.blocks += 1
        if self.blocks % 100 == 0:  # every 2 s
            threading.Thread(target=self.look, daemon=True).start()
        if self.fresh:
            old, self.tap, self.fresh = self.tap, self.fresh, None
            threading.Thread(target=old.close, daemon=True).start()
        self.buf += self.tap.take()
        # ponytail: the music runs on its device's clock, not the mic's: a 40 ms cushion soaks up jitter and drift
        # gets trimmed (a 20-80 ms skip now and then), as on Windows; resample if that ever shows
        if len(self.buf) > 6 * BLOCK:
            del self.buf[:-2 * BLOCK]
        if len(self.buf) < (BLOCK if self.flowing else 2 * BLOCK):
            self.flowing = False
            return SILENCE
        self.flowing, chunk = True, bytes(self.buf[:BLOCK])
        del self.buf[:BLOCK]
        return chunk

    def close(self) -> None:
        for tap in (self.tap, self.fresh):
            if tap:
                tap.close()


# --- voice isolation: macOS's voice processing, whose Mic Mode the user picks (Voice Isolation) ---
class AudioBuffer(ctypes.Structure):
    _fields_ = [("channels", c_uint32), ("size", c_uint32), ("data", c_void_p)]


class AudioBufferList(ctypes.Structure):
    _fields_ = [("count", c_uint32), ("buffers", AudioBuffer * 1)]


class Component(ctypes.Structure):  # AudioComponentDescription
    _fields_ = [("type", c_uint32), ("subtype", c_uint32), ("maker", c_uint32), ("flags", c_uint32),
                ("mask", c_uint32)]


RENDER = ctypes.CFUNCTYPE(c_int32, c_void_p, POINTER(c_uint32), c_void_p, c_uint32, c_uint32, c_void_p)


class Callback(ctypes.Structure):  # AURenderCallbackStruct
    _fields_ = [("proc", RENDER), ("data", c_void_p)]


at.AudioComponentFindNext.restype, at.AudioComponentFindNext.argtypes = c_void_p, [c_void_p, POINTER(Component)]
at.AudioComponentInstanceNew.argtypes = [c_void_p, POINTER(c_void_p)]
at.AudioUnitSetProperty.argtypes = [c_void_p, c_uint32, c_uint32, c_uint32, c_void_p, c_uint32]
at.AudioUnitRender.argtypes = [c_void_p, POINTER(c_uint32), c_void_p, c_uint32, c_uint32, POINTER(AudioBufferList)]
for name in ("AudioUnitInitialize", "AudioOutputUnitStart", "AudioOutputUnitStop", "AudioUnitUninitialize",
             "AudioComponentInstanceDispose"):
    getattr(at, name).argtypes = [c_void_p]


class VoiceMic:
    """The mic through macOS's voice processing, 20 ms at a time: Mic's stand-in when voice isolation is on. While it
    runs, macOS offers its Mic Mode for MicMix in Control Center, and Voice Isolation there does the noise removal.
    Its own gain control is off (it would move the noise gate's level about) and it lowers other apps' sound as
    little as macOS allows (voice processing counts as a call)."""

    def __init__(self, dev: int):
        found = all_devices("waveIn")
        if dev >= len(found):
            raise OSError("the mic is gone")
        self.unit, self.ready, self.part = c_void_p(), queue.Queue(), bytearray()
        self.room = ctypes.create_string_buffer(8192 * CHANNELS * 2)  # what one callback may ask for, at most
        self.list = AudioBufferList(1, (AudioBuffer * 1)(AudioBuffer(CHANNELS, 0, ctypes.addressof(self.room))))
        self.render = RENDER(self.arrived)
        part = at.AudioComponentFindNext(None, byref(Component(code("auou"), code("vpio"), code("appl"), 0, 0)))
        try:
            self.call(at.AudioComponentInstanceNew(part, byref(self.unit)) if part else -1)
            one, zero, mic = c_uint32(1), c_uint32(0), c_uint32(found[dev][0])
            for selector, scope, element, value in (
                    (2003, 1, 1, one), (2003, 2, 0, zero),  # EnableIO: input on, output off
                    (2000, 0, 1, mic),  # CurrentDevice: the picked mic
                    (2101, 0, 0, zero)):  # VoiceProcessingEnableAGC: off
                self.call(at.AudioUnitSetProperty(self.unit, selector, scope, element, byref(value),
                                                  ctypes.sizeof(value)))
            with contextlib.suppress(OSError):  # macOS 14 or later: duck other apps as little as it can
                ducking = struct.pack("<?3xI", True, 10)  # AUVoiceIOOtherAudioDuckingConfiguration: advanced, min
                self.call(at.AudioUnitSetProperty(self.unit, 2108, 0, 0, ducking, len(ducking)))
            for scope, element in ((2, 1), (1, 0)):  # StreamFormat: what it hands us, and (it insists they match)
                self.call(at.AudioUnitSetProperty(self.unit, 8, scope, element, byref(FORMAT),  # what it would play
                                                  ctypes.sizeof(FORMAT)))
            hook = Callback(self.render, None)
            self.call(at.AudioUnitSetProperty(self.unit, 2005, 0, 0, byref(hook), ctypes.sizeof(hook)))  # SetInputCallback
            self.call(at.AudioUnitInitialize(self.unit))
            self.call(at.AudioOutputUnitStart(self.unit))
        except OSError as e:
            self.close()
            raise OSError(f"this Mac can't do it with this mic ({e})") from e

    @staticmethod
    def call(err: int) -> None:
        if err:
            raise OSError(says(err))

    def arrived(self, _data, flags, stamp, bus, frames, _list) -> int:
        """On macOS's sound thread: take the voice in and hand it over, nothing more."""
        size = frames * CHANNELS * 2
        if size > len(self.room):
            return -50
        self.list.buffers[0].size = size
        if not at.AudioUnitRender(self.unit, flags, stamp, bus, frames, byref(self.list)):
            self.part += ctypes.string_at(self.room, size)
            while len(self.part) >= BLOCK:
                self.ready.put(bytes(self.part[:BLOCK]))
                del self.part[:BLOCK]
            while self.ready.qsize() > MIC_BUFS:
                with contextlib.suppress(queue.Empty):
                    self.ready.get_nowait()
        return 0

    read = Mic.read

    def close(self) -> None:
        if self.unit:
            at.AudioOutputUnitStop(self.unit)
            at.AudioUnitUninitialize(self.unit)
            at.AudioComponentInstanceDispose(self.unit)
            self.unit = c_void_p()


def mic_mode() -> int:
    """The Mic Mode picked for MicMix: 0 Standard, 1 Wide Spectrum, 2 Voice Isolation."""
    return send(cls("AVCaptureDevice"), "preferredMicrophoneMode", ret=c_long)


def show_mic_modes() -> None:
    """macOS's own Mic Mode picker (Control Center's), to choose Voice Isolation for MicMix."""
    send(cls("AVCaptureDevice"), "showSystemUserInterface:", 2, types=(c_long,))  # MicrophoneModes


# --- decoding sounds ---
for name, args in (("ExtAudioFileOpenURL", [c_void_p, POINTER(c_void_p)]),
                   ("ExtAudioFileSetProperty", [c_void_p, c_uint32, c_uint32, c_void_p]),
                   ("ExtAudioFileRead", [c_void_p, POINTER(c_uint32), POINTER(AudioBufferList)]),
                   ("ExtAudioFileDispose", [c_void_p])):
    getattr(at, name).argtypes, getattr(at, name).restype = args, c_int32


def decode(path: str) -> bytes:
    """A sound file as 48 kHz 16-bit stereo, decoded and converted by macOS (MP3, WAV, AIFF, M4A/AAC, ALAC, FLAC,
    CAF). Raises OSError with a plain reason."""
    raw = os.fsencode(path)
    url, handle = cf.CFURLCreateFromFileSystemRepresentation(None, raw, len(raw), False), c_void_p()
    err = at.ExtAudioFileOpenURL(url, byref(handle)) if url else -43
    if url:
        cf.CFRelease(url)
    if err:
        raise OSError(f"macOS can't play this file ({says(err)})")
    frames = 16384
    room, pcm = ctypes.create_string_buffer(frames * CHANNELS * 2), bytearray()
    files = AudioBufferList(1, (AudioBuffer * 1)(AudioBuffer(CHANNELS, 0, ctypes.addressof(room))))
    try:
        if err := at.ExtAudioFileSetProperty(handle, code("cfmt"), ctypes.sizeof(FORMAT), byref(FORMAT)):
            raise OSError(f"macOS can't play this file ({says(err)})")
        while True:
            got = c_uint32(frames)
            files.buffers[0].size = len(room)
            if err := at.ExtAudioFileRead(handle, byref(got), byref(files)):
                raise OSError(f"macOS can't play this file ({says(err)})")
            if not got.value:
                break
            pcm += room.raw[:got.value * CHANNELS * 2]
            if len(pcm) > LONGEST_SOUND:  # it all stays in memory: 11 MB a minute
                raise OSError(TOO_LONG)
    finally:
        at.ExtAudioFileDispose(handle)
    return bytes(pcm)


# --- the window's side: menu bar, keybinds, screen, Dock, fonts ---
IMP = ctypes.CFUNCTYPE(None, c_void_p, c_void_p, c_void_p)  # an Objective-C method: self, _cmd, sender


def target(methods: dict) -> int:
    """An Objective-C object whose methods (name -> function of the sender) run Python; for buttons and menus."""
    name = b"MicMixTarget%d" % len(TARGETS)
    made = objc.objc_allocateClassPair(cls("NSObject"), name, 0)
    for method, fn in methods.items():
        imp = IMP(lambda this, cmd, sender, fn=fn: fn(sender))
        TARGETS.append(imp)  # kept: Objective-C calls it for as long as MicMix runs
        objc.class_addMethod(made, objc.sel_registerName(method.encode()), ctypes.cast(imp, c_void_p), b"v@:@")
    objc.objc_registerClassPair(made)
    return send(send(made, "alloc"), "init")


TARGETS: list = []


class MenuBar:
    """MicMix's icon in the menu bar: click it for the panel, right-click (or Control-click) for the quick menu. It
    lives on the main thread with Tk, as macOS wants; commands go on `pressed` like the Windows tray's."""

    def __init__(self, pressed: queue.Queue, shared, color: str, icon: str):
        self.pressed, self.shared, self.added = pressed, shared, False
        self.target = target({"click:": self.click, "pick:": self.pick})
        self.item = send(send(send(cls("NSStatusBar"), "systemStatusBar"), "statusItemWithLength:", -1.0,
                              types=(c_double,)), "retain")  # the menu bar doesn't keep it by itself
        self.button = send(self.item, "button")
        image = send(send(cls("NSImage"), "alloc"), "initWithContentsOfFile:", ns(icon))
        if image:  # the SD mark as a template: macOS draws it black or white to suit the menu bar, like its own
            send(image, "setSize:", NSSize(29, 16), types=(NSSize,))
            send(image, "setTemplate:", True, types=(c_bool,))
            send(self.button, "setImage:", image)
        else:
            send(self.button, "setTitle:", ns("MicMix"))
        send(self.button, "setToolTip:", ns("MicMix"))
        send(self.button, "setTarget:", self.target)
        send(self.button, "setAction:", objc.sel_registerName(b"click:"))
        send(self.button, "sendActionOn:", 4 | 16, types=(c_ulong,))  # left and right mouse up
        self.added = True

    def click(self, _sender) -> None:
        event = send(app(), "currentEvent")
        right = send(event, "type", ret=c_ulong) == 4 or send(event, "modifierFlags", ret=c_ulong) & (1 << 18)
        if right:  # right mouse up, or Control held
            self.menu()
        else:
            self.pressed.put("flyout")

    def menu(self) -> None:
        menu = send(send(send(cls("NSMenu"), "alloc"), "initWithTitle:", ns("MicMix")), "autorelease")
        for tag, title in ((1, "Open MicMix"), (2, "Mute mic"), (3, "Stop sounds"), (0, None), (4, "Quit MicMix")):
            if not tag:
                send(menu, "addItem:", send(cls("NSMenuItem"), "separatorItem"))
                continue
            item = send(send(send(cls("NSMenuItem"), "alloc"), "initWithTitle:action:keyEquivalent:", ns(title),
                             objc.sel_registerName(b"pick:"), ns("")), "autorelease")
            send(item, "setTarget:", self.target)
            send(item, "setTag:", tag, types=(c_long,))
            if tag == 2 and self.shared.muted:
                send(item, "setState:", 1, types=(c_long,))  # a tick
            send(menu, "addItem:", item)
        send(self.item, "popUpStatusItemMenu:", menu)  # deprecated, still the one that sits under the icon

    def pick(self, sender) -> None:
        self.pressed.put({1: "show", 2: "mute", 3: "stop", 4: "quit"}[send(sender, "tag", ret=c_long)])

    def where(self) -> tuple[int, int] | None:
        """The icon's middle in Tk's coordinates (macOS counts from the main screen's bottom left)."""
        window = send(self.button, "window")
        if not window:
            return None
        icon = send(window, "frame", ret=NSRect)
        main = send(send(cls("NSScreen"), "screens"), "firstObject")
        tall = send(main, "frame", ret=NSRect).h
        return round(icon.x + icon.w / 2), round(tall - icon.y - icon.h / 2)

    def muted(self, on: bool) -> None:
        """A dimmed icon while the mic is muted, so it shows at a glance."""
        send(self.button, "setAppearsDisabled:", on, types=(c_bool,))

    def notify(self, title: str, text: str) -> None:
        """Nothing: on a Mac the panel drops down from the icon instead (see micmix.pyw), which shows where it is."""

    def stop(self) -> None:
        if self.added:
            send(send(cls("NSStatusBar"), "systemStatusBar"), "removeStatusItem:", self.item)
            self.added = False


class NSSize(ctypes.Structure):
    _fields_ = [("w", c_double), ("h", c_double)]


class EventType(ctypes.Structure):
    _fields_ = [("kind", c_uint32), ("what", c_uint32)]


class HotKeyID(ctypes.Structure):
    _fields_ = [("signature", c_uint32), ("id", c_uint32)]


HANDLER = ctypes.CFUNCTYPE(c_int32, c_void_p, c_void_p, c_void_p)
carbon.GetApplicationEventTarget.restype = c_void_p
carbon.InstallEventHandler.argtypes = [c_void_p, HANDLER, c_ulong, POINTER(EventType), c_void_p, POINTER(c_void_p)]
carbon.RegisterEventHotKey.argtypes = [c_uint32, c_uint32, HotKeyID, c_void_p, c_uint32, POINTER(c_void_p)]
carbon.UnregisterEventHotKey.argtypes = [c_void_p]
carbon.GetEventParameter.argtypes = [c_void_p, c_uint32, c_uint32, c_void_p, c_ulong, c_void_p, c_void_p]
# Tk's modifier bits -> Carbon's, with the Mac's own names in its order (Control, Option, Shift, Command)
MODIFIERS = ((4, 0x1000, "⌃"), (16, 0x800, "⌥"), (1, 0x200, "⇧"), (8, 0x100, "⌘"))
ALONE = {122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111, 105, 107, 113, 106, 64, 79, 80, 90,  # F1-F20
         65, 67, 69, 71, 75, 76, 78, 81, 82, 83, 84, 85, 86, 87, 88, 89, 91, 92}  # the number pad
KEYS = {**dict(enumerate("ASDFHGZXCV§BQWERYT123465=97-80]OU[IP")), **dict(zip(range(37, 48), "LJ'K;\\,/NM.")),
        36: "Return", 48: "Tab", 49: "Space", 50: "`", 51: "Delete", 53: "Esc"}  # what the keys say on US keyboards


def key_code(event) -> int:
    """The key's own number (macOS's virtual key code, kept by Tk 8.6.11 and later in the keycode's top byte)."""
    return (event.keycode >> 24) & 0xFF


class Hotkeys:
    """System-wide keybinds, so they work while a game or Discord has the focus: Carbon's hot keys, which need no
    permission. macOS hands each press to the main thread, inside Tk's event loop; it goes on `pressed`."""
    current = None

    def __init__(self, bindings: dict[str, list], pressed: queue.Queue):
        self.actions, self.pressed, self.taken, self.refs = list(bindings), pressed, set(), []
        if HOTKEY_HANDLER[1] is None:  # once: it hands every MicMix hot key to whichever Hotkeys is current
            HOTKEY_HANDLER[1] = c_void_p()
            carbon.InstallEventHandler(carbon.GetApplicationEventTarget(), HOTKEY_HANDLER[0], 1,
                                       byref(EventType(code("keyb"), 5)), None, byref(HOTKEY_HANDLER[1]))
        for i, action in enumerate(self.actions, 1):
            mods, key = bindings[action][:2]
            ref = c_void_p()
            if carbon.RegisterEventHotKey(key, mods, HotKeyID(code("MMix"), i), carbon.GetApplicationEventTarget(), 0,
                                          byref(ref)):
                self.taken.add(action)  # MicMix itself already holds that key
            else:
                self.refs.append(ref)
        Hotkeys.current = self

    @staticmethod
    def handle(_call, event, _data) -> int:
        hit = HotKeyID()
        if carbon.GetEventParameter(event, code("----"), code("hkid"), None, ctypes.sizeof(hit), None, byref(hit)):
            return -9874  # eventNotHandledErr
        keys = Hotkeys.current
        if keys and 0 < hit.id <= len(keys.actions):
            keys.pressed.put(keys.actions[hit.id - 1])
        return 0

    def stop(self) -> None:
        for ref in self.refs:
            carbon.UnregisterEventHotKey(ref)
        self.refs = []
        if Hotkeys.current is self:
            Hotkeys.current = None


HOTKEY_HANDLER = [HANDLER(Hotkeys.handle), None]


def work_area(x: int, y: int) -> SimpleNamespace:
    """The screen at (x, y), minus the menu bar and the Dock, in Tk's coordinates (points from the main screen's top
    left; macOS counts from its bottom left)."""
    screens = send(cls("NSScreen"), "screens")
    count = send(screens, "count", ret=c_ulong)
    frames = [(send(s, "frame", ret=NSRect), send(s, "visibleFrame", ret=NSRect))
              for s in (send(screens, "objectAtIndex:", i, types=(c_ulong,)) for i in range(count))]
    if not frames:
        return SimpleNamespace(left=0, top=0, right=1440, bottom=900)
    tall = frames[0][0].h  # the main screen's height turns one into the other

    def flip(r: NSRect) -> SimpleNamespace:
        return SimpleNamespace(left=round(r.x), top=round(tall - r.y - r.h), right=round(r.x + r.w),
                               bottom=round(tall - r.y))
    for whole, free in frames:
        box = flip(whole)
        if box.left <= x < box.right and box.top <= y < box.bottom:
            return flip(free)
    return flip(frames[0][1])


def dock(show: bool) -> None:
    """With the window: MicMix in the Dock and in front. Without it: only its menu bar icon, as menu bar apps do."""
    send(app(), "setActivationPolicy:", 0 if show else 1, types=(c_long,))  # Regular / Accessory
    if show:
        send(app(), "activateIgnoringOtherApps:", True, types=(c_bool,))


def activate() -> None:
    send(app(), "activateIgnoringOtherApps:", True, types=(c_bool,))


def active() -> bool:
    return bool(send(app(), "isActive", ret=c_bool))


def step_back() -> None:
    """Hand the focus back to the app that had it (MicMix has no window open)."""
    send(app(), "hide:", None)


def keep_awake() -> None:
    """No App Nap: macOS would slow a windowless MicMix down, and the sound with it. Idle sleep stays allowed."""
    reason = "MicMix is mixing your mic"
    KEPT.append(send(send(send(cls("NSProcessInfo"), "processInfo"), "beginActivityWithOptions:reason:",
                          0x00EFFFFF | 0xFF00000000, ns(reason), types=(ctypes.c_uint64, c_void_p)), "retain"))


KEPT: list = []
coretext.CTFontManagerRegisterFontsForURL.argtypes = [c_void_p, c_uint32, c_void_p]


def register_fonts(paths: list) -> None:
    """The brand fonts for this process only (nothing installed), as Windows' FR_PRIVATE."""
    for path in paths:
        raw = os.fsencode(path)
        url = cf.CFURLCreateFromFileSystemRepresentation(None, raw, len(raw), False)
        if url:
            coretext.CTFontManagerRegisterFontsForURL(url, 1, None)  # kCTFontManagerScopeProcess
            cf.CFRelease(url)
