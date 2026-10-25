"""MicMix: your microphone plus one app's sound (or everything on an output), a soundboard and a noise gate, in one
virtual mic for Discord, with a slider between voice and music and keybinds that work from any app.

Mic + music + sounds go to "Send to". Make that a virtual cable (VB-Audio's free VB-CABLE: "CABLE Input") and pick
the cable's other end ("CABLE Output") as the microphone in Discord. The slider fades voice <-> music; both are full at
the middle. "Music from" takes one app (Spotify, a browser, your MP3 player) as OBS's Application Audio Capture does,
so game sounds and Discord stay out; or everything playing on one output device, as OBS's Desktop Audio does.

This file is the window, the keybinds and the installer's helpers; the sound is audio.py. Standard library only.
The app needs no admin and changes no Windows setting. Picks, keybinds and the sliders are kept in micmix.json next to
this file, sounds in sounds\\.
"""
import contextlib
import ctypes
import json
import math
import os
import queue
import shutil
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from ctypes import wintypes as w
from pathlib import Path
from tkinter import filedialog, ttk

import audio
from audio import DEFAULT, OFF

HERE = Path(__file__).resolve().parent
SETTINGS, SOUNDS = HERE / "micmix.json", HERE / "sounds"
SOUND_TYPES = {".mp3", ".wav", ".m4a", ".aac", ".wma", ".flac", ".ogg", ".opus"}  # what Windows may decode
ACTIONS = {"mute": "Mute or unmute your mic", "music": "Music on or off", "up": "Music up", "down": "Music down",
           "stop": "Stop sounds"}
GATE_OFF = -72  # the noise gate slider's far left (dBFS): off. The mic meter under it uses the same scale
MODIFIERS = ((0x11, 2, "Ctrl"), (0x12, 1, "Alt"), (0x10, 4, "Shift"), (0x5B, 8, "Win"), (0x5C, 8, "Win"))
WM_HOTKEY, WM_QUIT, WM_CLOSE, MOD_NOREPEAT = 0x312, 0x12, 0x10, 0x4000
APP_ID, TRAY_CLASS, WM_TRAY, WM_SHOW = "SilvaDes.MicMix", "MicMixTray", 0x8001, 0x8002  # WM_APP + 1, + 2
WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONUP = 0x202, 0x203, 0x205
MF_CHECKED, MF_SEPARATOR, TPM_RETURNCMD = 0x8, 0x800, 0x100
NIM_ADD, NIM_MODIFY, NIM_DELETE, NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0, 1, 2, 1, 2, 4, 0x10
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT), ("uFlags", w.UINT), ("uCallbackMessage", w.UINT),
                ("hIcon", w.HICON), ("szTip", w.WCHAR * 128), ("dwState", w.DWORD), ("dwStateMask", w.DWORD),
                ("szInfo", w.WCHAR * 256), ("uVersion", w.UINT), ("szInfoTitle", w.WCHAR * 64), ("dwInfoFlags", w.DWORD),
                ("guidItem", ctypes.c_ubyte * 16), ("hBalloonIcon", w.HICON)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", w.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", w.HINSTANCE), ("hIcon", w.HICON), ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH),
                ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT), ("rcWork", w.RECT), ("dwFlags", w.DWORD)]


user32, kernel32, shell32 = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32"), ctypes.WinDLL("shell32")
user32.RegisterHotKey.argtypes = [w.HWND, ctypes.c_int, w.UINT, w.UINT]
user32.UnregisterHotKey.argtypes = [w.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
user32.TranslateMessage.argtypes = user32.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
user32.PostThreadMessageW.argtypes = [w.DWORD, w.UINT, w.WPARAM, w.LPARAM]
user32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.GetAsyncKeyState.argtypes, user32.GetAsyncKeyState.restype = [ctypes.c_int], ctypes.c_short
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, ctypes.c_void_p]
user32.CreateWindowExW.restype = w.HWND
user32.DefWindowProcW.argtypes, user32.DefWindowProcW.restype = [w.HWND, w.UINT, w.WPARAM, w.LPARAM], ctypes.c_ssize_t
user32.DestroyWindow.argtypes = [w.HWND]
user32.FindWindowW.argtypes, user32.FindWindowW.restype = [w.LPCWSTR, w.LPCWSTR], w.HWND
user32.RegisterWindowMessageW.argtypes = [w.LPCWSTR]
user32.CreatePopupMenu.restype = w.HMENU
user32.AppendMenuW.argtypes = [w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR]
user32.TrackPopupMenu.argtypes = [w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, ctypes.c_void_p]
user32.DestroyMenu.argtypes = [w.HMENU]
user32.SetForegroundWindow.argtypes = [w.HWND]
user32.GetCursorPos.argtypes = [ctypes.POINTER(w.POINT)]
user32.LoadImageW.argtypes = [w.HINSTANCE, w.LPCWSTR, w.UINT, ctypes.c_int, ctypes.c_int, w.UINT]
user32.LoadImageW.restype = w.HANDLE
user32.MonitorFromPoint.argtypes, user32.MonitorFromPoint.restype = [w.POINT, w.DWORD], w.HANDLE
user32.GetMonitorInfoW.argtypes = [w.HANDLE, ctypes.POINTER(MONITORINFO)]
user32.GetParent.argtypes, user32.GetParent.restype = [w.HWND], w.HWND
user32.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
user32.DestroyIcon.argtypes = [w.HICON]
kernel32.GetModuleHandleW.argtypes, kernel32.GetModuleHandleW.restype = [w.LPCWSTR], w.HMODULE
shell32.Shell_NotifyIconW.argtypes = [w.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]


class Hotkeys(threading.Thread):
    """System-wide keybinds (RegisterHotKey), so they work while a game or Discord has the focus. Windows delivers them
    to this thread's own message queue; each press goes on `pressed` for the window to act on."""

    def __init__(self, bindings: dict[str, list], pressed: queue.Queue):
        super().__init__(daemon=True)
        self.bindings, self.pressed, self.taken, self.tid = bindings, pressed, set(), 0
        self.ready = threading.Event()
        self.start()
        self.ready.wait(2)

    def run(self) -> None:
        self.tid = kernel32.GetCurrentThreadId()
        actions = list(self.bindings)
        for i, action in enumerate(actions, 1):
            mods, vk = self.bindings[action][:2]
            if not user32.RegisterHotKey(None, i, mods | MOD_NOREPEAT, vk):
                self.taken.add(action)  # another app holds that key
        self.ready.set()
        msg = w.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and 0 < msg.wParam <= len(actions):
                self.pressed.put(actions[msg.wParam - 1])
        for i in range(1, len(actions) + 1):
            user32.UnregisterHotKey(None, i)

    def stop(self) -> None:
        user32.PostThreadMessageW(self.tid, WM_QUIT, 0, 0)
        self.join(1)


class Tray(threading.Thread):
    """MicMix's icon in the notification area: click it for the window, right-click for quick actions. Like Hotkeys,
    it runs its own Windows message loop (behind a hidden window) and hands each command over through `pressed`."""

    def __init__(self, pressed: queue.Queue, shared, color: str):
        super().__init__(daemon=True)
        self.pressed, self.shared, self.color, self.added = pressed, shared, color, False
        self.ready = threading.Event()
        self.start()
        self.ready.wait(3)

    def run(self) -> None:
        self.proc = WNDPROC(self.handle)  # kept here: Windows calls it as long as the window lives
        self.explorer_restarted = user32.RegisterWindowMessageW("TaskbarCreated")  # before the window: it gets messages
        module = kernel32.GetModuleHandleW(None)                                    # while it is being created
        user32.RegisterClassW(ctypes.byref(WNDCLASSW(lpfnWndProc=self.proc, hInstance=module,
                                                     lpszClassName=TRAY_CLASS)))
        self.hwnd = user32.CreateWindowExW(0, TRAY_CLASS, "MicMix", 0, 0, 0, 0, 0, None, None, module, None)
        size = user32.GetSystemMetrics(49)  # SM_CXSMICON, already scaled for this display
        self.icon = user32.LoadImageW(None, str(ASSETS / "micmix.ico"), 1, size, size, 0x10)  # the SD mark
        self.data = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=self.hwnd, uID=1,
                                    uFlags=NIF_MESSAGE | NIF_ICON | NIF_TIP, uCallbackMessage=WM_TRAY,
                                    hIcon=self.icon, szTip="MicMix")
        self.added = bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self.data)))
        self.ready.set()
        msg = w.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.data))
        user32.DestroyWindow(self.hwnd)
        user32.DestroyIcon(self.icon)

    def handle(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg == WM_TRAY and lparam == WM_LBUTTONUP:  # a click: the pop-up panel by the clock
            user32.SetForegroundWindow(self.hwnd)  # MicMix may take the focus now, so the panel can close on click-away
            self.pressed.put("flyout")
        elif msg == WM_TRAY and lparam == WM_LBUTTONDBLCLK:  # the full window (its button-up then finds the panel
            self.pressed.put("show")                         # just closed, so it doesn't open again)
        elif msg == WM_SHOW:  # MicMix started a second time: the full window
            self.pressed.put("show")
        elif msg == WM_CLOSE:  # the installer, before it updates or removes MicMix: quit for real, saving settings
            self.pressed.put("quit")
        elif msg == WM_TRAY and lparam == WM_RBUTTONUP:
            self.menu()
        elif msg == self.explorer_restarted:  # Explorer restarted and lost its icons: add ours back
            self.data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            self.added = bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self.data))) or self.added
        else:
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        return 0

    def menu(self) -> None:
        menu = user32.CreatePopupMenu()
        for cmd, text in ((1, "Open MicMix"), (2, "Mute mic"), (3, "Stop sounds"), (0, None), (4, "Quit MicMix")):
            user32.AppendMenuW(menu, MF_SEPARATOR if not cmd else MF_CHECKED if cmd == 2 and self.shared.muted else 0,
                               cmd, text)
        at = w.POINT()
        user32.GetCursorPos(ctypes.byref(at))
        user32.SetForegroundWindow(self.hwnd)  # or the menu wouldn't close when you click elsewhere
        cmd = user32.TrackPopupMenu(menu, TPM_RETURNCMD, at.x, at.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, 0, 0, 0)  # WM_NULL, as Windows asks after a tray menu
        user32.DestroyMenu(menu)
        if cmd:
            self.pressed.put({1: "show", 2: "mute", 3: "stop", 4: "quit"}[cmd])

    def notify(self, title: str, text: str) -> None:
        """A one-off notification from the icon."""
        self.data.uFlags, self.data.szInfoTitle, self.data.szInfo, self.data.dwInfoFlags = NIF_INFO, title, text, 1
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.data))

    def stop(self) -> None:
        user32.PostMessageW(self.hwnd, WM_QUIT, 0, 0)
        self.join(1)


def key_name(event: tk.Event) -> str:
    name = event.keysym.replace("KP_", "Num ").replace("Prior", "PageUp").replace("Next", "PageDown")
    return name[:1].upper() + name[1:]


def load() -> dict:
    try:
        data = json.loads(SETTINGS.read_text(encoding="utf-8-sig"))  # -sig: a BOM from Notepad or PowerShell is fine
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        pass
    with contextlib.suppress(OSError):  # unreadable: kept as micmix.json.bad, not overwritten with the defaults
        SETTINGS.replace(SETTINGS.with_suffix(".json.bad"))
    return {}


def work_area(x: int, y: int) -> w.RECT:
    """The screen at (x, y), minus its taskbar."""
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    user32.GetMonitorInfoW(user32.MonitorFromPoint(w.POINT(x, y), 2), ctypes.byref(info))  # 2: the nearest screen
    return info.rcWork


# SILVADES: neon #39FF14 on near-black #050505, Oxanium 700 headings, IBM Plex Mono labels (assets\fonts, OFL)
WINDOW, CARD, STROKE, FIELD, HOVER, RAIL = "#050505", "#121212", "#222222", "#1a1a1a", "#262626", "#333333"
TEXT, TEXT2, TEXT3, NEON = "#e0e0e0", "#9a9a9a", "#808080", "#39ff14"
ASSETS = HERE / "assets"
ICONS = {"Microphone": "", "Music from": "", "Send to": ""}  # Segoe Fluent Icons / MDL2 Assets
K = 1.0  # screen pixels per 96-DPI pixel, set by theme(); fonts are in points and scale by themselves


def px(*n: float):
    r = [round(x * K) for x in n]
    return r[0] if len(r) == 1 else tuple(r)


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


def scroller(parent: tk.Misc, most: int) -> tuple[tk.Frame, tk.Frame]:
    """(outer, inner): place outer, fill inner. inner scrolls (wheel or bar) once it is taller than `most` pixels;
    outer.most can be lowered later (then call outer.fit)."""
    outer, spin = tk.Frame(parent, bg=CARD), 0
    view = tk.Canvas(outer, bg=CARD, highlightthickness=0, width=1, height=1)
    bar = ttk.Scrollbar(outer, command=view.yview)
    view.configure(yscrollcommand=bar.set)
    view.pack(side="left", fill="both", expand=True)
    inner = tk.Frame(view, bg=CARD)
    item = view.create_window(0, 0, window=inner, anchor="nw")

    def fit(_=None) -> None:
        tall = inner.winfo_reqheight()
        view.configure(width=inner.winfo_reqwidth(), height=min(tall, outer.most), scrollregion=(0, 0, 0, tall))
        if tall > outer.most:
            bar.pack(side="right", fill="y")
        else:
            bar.pack_forget()
        view.itemconfigure(item, width=view.winfo_width())

    def wheel(delta: str) -> None:  # 120 a notch; a touchpad sends smaller steps, which add up
        nonlocal spin
        spin -= int(delta)
        view.yview_scroll(steps := int(spin / 120), "units")
        spin -= steps * 120
    outer.most, outer.fit = most, fit
    inner.bind("<Configure>", fit)
    view.bind("<Configure>", fit)
    scroll = outer.register(wheel) + " %D"  # one Tcl command, freed with the scroller (not one per <Enter>)
    outer.bind("<Enter>", lambda e: outer.bind_all("<MouseWheel>", scroll))
    outer.bind("<Leave>", lambda e: outer.unbind_all("<MouseWheel>"))
    return outer, inner


def dark_titlebar(win: tk.Misc) -> None:
    win.update_idletasks()
    with contextlib.suppress(AttributeError, OSError):  # this window only
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4)


def theme(root: tk.Tk) -> str:
    """The SILVADES look, in clam (the native "vista" theme ignores colors). Returns the accent color."""
    global K
    K = root.winfo_fpixels("1i") / 96
    for ttf in (ASSETS / "fonts").glob("*.ttf"):  # the brand fonts, for this process only (nothing installed)
        with contextlib.suppress(AttributeError, OSError):
            ctypes.windll.gdi32.AddFontResourceExW(str(ttf), 0x10, 0)  # FR_PRIVATE
    have, lit = set(tkfont.families(root)), NEON
    pick = lambda *names: next((n for n in names if n in have), names[-1])  # noqa: E731
    text, display = pick("IBM Plex Mono", "Cascadia Mono", "Consolas"), pick("Oxanium", "Segoe UI Semibold")
    icons = pick("Segoe Fluent Icons", "Segoe MDL2 Assets")
    if icons not in have:
        ICONS.clear()
    root.configure(bg=WINDOW)
    for k, v in (("background", FIELD), ("foreground", TEXT), ("selectBackground", NEON), ("selectForeground", WINDOW),
                 ("font", (text, 10))):
        root.option_add(f"*TCombobox*Listbox.{k}", v)  # the drop-down list is a classic widget
    root.option_add("*TCombobox.font", (text, 10))  # the field in the list's font
    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", background=CARD, foreground=TEXT, bordercolor=STROKE, selectforeground=TEXT, font=(text, 10))
    s.configure("Title.TLabel", background=WINDOW, foreground=NEON, font=(display, 20, "bold"))
    s.configure("Brand.TLabel", background=WINDOW, foreground=TEXT3, font=(text, 9, "bold"))
    s.configure("Note.TLabel", background=WINDOW, foreground=TEXT3, font=(text, 9))
    s.configure("Icon.TLabel", foreground=NEON, font=(icons, 13))
    s.configure("Dim.TLabel", foreground=TEXT2)
    s.configure("Value.TLabel", font=(text, 10, "bold"))
    s.configure("Pad.TButton", padding=px(6, 6), font=(text, 9, "bold"), wraplength=px(80), justify="center")
    strong = (text, 10, "bold")
    s.configure("TCombobox", arrowcolor=TEXT2, arrowsize=px(9), padding=px(9, 5, 4, 5))
    s.map("TCombobox", bordercolor=[("focus", lit)], **dict.fromkeys(
        ("fieldbackground", "background", "lightcolor", "darkcolor", "selectbackground"), [("hover", HOVER), ("", FIELD)]))
    s.configure("TButton", background=FIELD, foreground=TEXT, bordercolor=STROKE, lightcolor=FIELD, darkcolor=FIELD,
                focuscolor=FIELD, padding=px(12, 6), font=(text, 10))
    s.map("TButton", background=[("pressed", RAIL), ("active", HOVER)], lightcolor=[("active", HOVER)],
          darkcolor=[("active", HOVER)], bordercolor=[("focus", lit)])
    s.configure("Sound.TButton", padding=px(10, 10), font=strong, wraplength=px(120), justify="center")
    s.configure("TCheckbutton", indicatorbackground=FIELD, indicatorforeground=lit, upperbordercolor="#6a6a6a",
                lowerbordercolor="#6a6a6a", focuscolor=CARD, indicatorsize=px(15), indicatormargin=px(0, 0, 8, 0))
    s.map("TCheckbutton", background=[("active", CARD)], indicatorbackground=[("selected", lit), ("active", HOVER)],
          indicatorforeground=[("selected", WINDOW)], upperbordercolor=[("focus", TEXT)],
          lowerbordercolor=[("focus", TEXT)])  # ticked: a neon box; a light edge while the keyboard is on it
    s.configure("TScrollbar", troughcolor=CARD, background=RAIL, bordercolor=CARD, lightcolor=RAIL, darkcolor=RAIL,
                arrowcolor=TEXT2, gripcount=0)
    s.map("TScrollbar", **dict.fromkeys(("background", "lightcolor", "darkcolor"), [("pressed", TEXT3),
                                                                                   ("active", "#4a4a4a")]))
    size, wide = px(20), root.winfo_screenwidth()  # the slider: a grey rail, a ringed neon thumb
    rail = tk.PhotoImage(width=wide, height=size)  # screen-wide, drawn in one go (a 2 px one is tiled: ~1 s a redraw)
    rail.put(CARD, to=(0, 0, wide, size))
    rail.put(RAIL, to=(0, (size - px(4)) // 2, wide, (size + px(4)) // 2))
    thumbs = [disc(size, [(size / 2, edge), (size / 2 - px(ring), "#2a2a2a"), (px(r), lit)])  # normal, hover, held,
              for r, edge, ring in ((6, "#3a3a3a", 1), (7, "#3a3a3a", 1), (5, "#3a3a3a", 1), (6, TEXT, 1.5))]  # focused
    mark = 56 if K > 1.6 else 40 if K > 1.2 else 28  # the SD mark, near its drawn size
    root.images, root.mark = [rail, *thumbs], None
    with contextlib.suppress(tk.TclError):  # no assets folder: only the logo is lost
        root.mark = tk.PhotoImage(file=str(ASSETS / f"sd-{mark}.png"))
    s.element_create("Fluent.trough", "image", rail, sticky="ew", width=2)  # width: ask for 2 px, not the screen's
    s.element_create("Fluent.slider", "image", thumbs[0], ("pressed", thumbs[2]), ("active", thumbs[1]),
                     ("focus", thumbs[3]))
    s.layout("Fluent.Horizontal.TScale", [("Fluent.trough", {"sticky": "ew", "children": [
        ("Fluent.slider", {"side": "left", "sticky": ""})]})])
    # Tk's sliders step by 1 a click on the rail (held: again and again) and a key press: on a 0-1 slider, all the
    # way. A click now puts the thumb where you click (as Windows' sliders do) and drags on from there; keys step 1%.
    root.bind_class("TScale", "<Button-1>", "ttk::scale::Jump %W %x %y")
    root.eval("proc ttk::scale::Increment {w delta} {if {[winfo exists $w]} {"
              "$w set [expr {[$w get] + $delta * ([$w cget -to] - [$w cget -from]) / 100.0}]}}")
    with contextlib.suppress(tk.TclError):
        root.iconbitmap(default=str(ASSETS / "micmix.ico"))
    dark_titlebar(root)
    return lit


def brand(parent: tk.Misc, subtitle: str) -> None:
    """The header: SD mark, MICMIX, by SILVADES, and a line under it."""
    row = tk.Frame(parent, bg=WINDOW)
    row.pack(anchor="w", fill="x")
    if mark := parent.nametowidget(".").mark:
        tk.Label(row, image=mark, bg=WINDOW).pack(side="left", padx=(0, px(10)))
    ttk.Label(row, text="MICMIX", style="Title.TLabel").pack(side="left")
    ttk.Label(row, text="by SILVADES", style="Brand.TLabel").pack(side="left", padx=px(10, 0), pady=px(8, 0))
    if subtitle:
        ttk.Label(parent, text=subtitle, style="Note.TLabel").pack(anchor="w", pady=(px(2), px(10)))


class App(tk.Tk):
    def __init__(self):
        with contextlib.suppress(AttributeError, OSError):
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on scaled displays
        super().__init__()
        self.withdraw()  # shown once it's built, not as an empty box first
        self.title("MicMix")
        self.lit = theme(self)
        self.configure(padx=px(20), pady=px(12))
        self.minsize(px(420), 0)
        saved = load()
        self.shared, self.engine, self.hotkeys, self.dialog = audio.shared_state(), None, None, None
        self.keys: dict[str, list] = saved.get("keys", {})
        self.pressed, self.cache, self.key_labels, self.note = queue.Queue(), {}, {}, ("", 0.0)
        self.last_fade, self.tray_tip, self.closed, self.ticks = 0.25, bool(saved.get("tray_tip")), False, 0
        self.pop, self.pop_closed = None, 0.0  # the tray's pop-up panel, and when it last closed
        self.tray = Tray(self.pressed, self.shared, self.lit)

        brand(self, "Your mic, your music and your sounds, together in one mic")
        grid = card(self)
        grid.columnconfigure(2, weight=1)
        cable = next((d for d in audio.devices("waveOut") if audio.is_cable(d) and "16CH" not in d.upper()), "")
        mics = lambda: [DEFAULT, *(d for d in audio.devices("waveIn") if not audio.is_cable(d))]  # no loops
        sources = lambda: [OFF, *audio.playing_apps(), DEFAULT, *audio.devices("waveOut")]  # one app, or a device
        self.mic = self.picker(grid, 0, "Microphone", mics, saved.get("mic"), DEFAULT)
        self.music = self.picker(grid, 1, "Music from", sources, saved.get("music"), OFF)
        self.out = self.picker(grid, 2, "Send to", lambda: audio.devices("waveOut"), saved.get("out"), cable)

        fader = card(self, px(16))
        fader.columnconfigure(2, weight=1)
        for row, col, name in ((0, 0, "Voice"), (0, 3, "Music"), (2, 0, "Level"), (3, 0, "Noise gate")):
            ttk.Label(fader, text=name, style="Dim.TLabel").grid(row=row, column=col, sticky="w", padx=(0, px(6)),
                                                                 pady=px(4))
        self.levels = [ttk.Label(fader, style="Value.TLabel", width=4) for _ in range(2)]  # voice %, music %
        self.levels[0].grid(row=0, column=1, sticky="w")
        self.levels[1].grid(row=0, column=4, sticky="w")
        self.fade = tk.DoubleVar(value=float(saved.get("fade", 0.25)))
        ttk.Scale(fader, from_=0, to=1, variable=self.fade, command=self.set_fade,
                  style="Fluent.Horizontal.TScale").grid(row=1, column=0, columnspan=5, sticky="ew", pady=px(4, 6))
        self.meter = self.bar(fader, row=2, column=1, columnspan=4)
        self.gate_label = ttk.Label(fader, style="Value.TLabel", width=6)
        self.gate_label.grid(row=3, column=1, sticky="w")
        switches = tk.Frame(fader, bg=CARD)
        switches.grid(row=7, column=0, columnspan=5, sticky="w", pady=px(4, 10))
        self.isolate, self.muted = tk.BooleanVar(value=bool(saved.get("isolate"))), tk.BooleanVar(value=False)
        ttk.Checkbutton(switches, text="Voice isolation (Windows' noise removal, where your PC has it)",
                        variable=self.isolate, command=self.restart).pack(side="left", padx=(0, px(20)))
        ttk.Checkbutton(switches, text="Mute mic", variable=self.muted, command=self.set_mute).pack(side="left")
        self.gate = tk.DoubleVar(value=float(saved.get("gate", GATE_OFF)))
        ttk.Scale(fader, from_=GATE_OFF, to=0, variable=self.gate, command=self.set_gate,
                  style="Fluent.Horizontal.TScale").grid(row=4, column=0, columnspan=5, sticky="ew", pady=px(2, 0))
        self.mic_meter = self.bar(fader, row=5, column=0, columnspan=5, inset=px(10))  # under the gate's thumb
        ttk.Label(fader, text="Drag the gate just above your mic's level when you're quiet.", style="Dim.TLabel").grid(
            row=6, column=0, columnspan=5, sticky="w", pady=px(4, 2))

        board = card(self, px(16))
        board.columnconfigure(1, weight=1)
        ttk.Label(board, text="Soundboard", style="Value.TLabel").grid(row=0, column=0, sticky="w", pady=px(6))
        self.volume = tk.DoubleVar(value=float(saved.get("board", 0.7)))
        ttk.Scale(board, from_=0, to=1, variable=self.volume, command=self.set_volume,
                  style="Fluent.Horizontal.TScale").grid(row=0, column=1, sticky="ew", padx=px(12))
        self.volume_label = ttk.Label(board, style="Value.TLabel", width=4)
        self.volume_label.grid(row=0, column=2, sticky="w")
        self.hear = tk.BooleanVar(value=bool(saved.get("hear", True)))
        ttk.Checkbutton(board, text="Hear them too", variable=self.hear, command=self.restart).grid(
            row=0, column=3, sticky="e", padx=(px(12), 0))
        self.pad_box, self.pads = scroller(board, px(270))  # about four rows of pads, then it scrolls
        self.pad_box.grid(row=1, column=0, columnspan=4, sticky="ew", pady=px(4))
        tools = tk.Frame(board, bg=CARD)
        tools.grid(row=2, column=0, columnspan=4, sticky="ew", pady=px(6, 10))
        for text, command in (("Add sounds…", self.add_sounds), ("Open folder", self.open_folder),
                              ("Stop sounds", lambda: self.shared.clips.put(audio.STOP)), ("Keybinds…", self.keybinds)):
            ttk.Button(tools, text=text, command=command).pack(side="left", padx=(0, px(6)))

        self.status = ttk.Label(self, style="Note.TLabel", foreground=TEXT2)
        self.status.pack(fill="x", padx=px(2), pady=(px(6), 0))
        self.status.bind("<Configure>", lambda e: self.status.configure(wraplength=e.width))  # wrap at window width

        self.shown_sounds: list[str] = []
        self.bind("<Activate>", lambda e: e.widget is self and self.draw_pads())  # files dropped in the folder
        self.bind("<Configure>", lambda e: e.widget is self and self.fit_screen(e.height))
        self.protocol("WM_DELETE_WINDOW", self.hide)
        self.protocol("WM_SAVE_YOURSELF", self.save)  # Tk's word for Windows signing out or shutting down
        self.draw_pads()
        self.set_fade()
        self.set_gate()
        self.set_volume()
        self.rebind()
        self.restart()
        self.poll()
        self.update_idletasks()
        self.deiconify()

    def fit_screen(self, height: int) -> None:
        """Keep the whole window above the taskbar as it grows (at start, and as sounds are added): fewer rows of pads
        before they scroll, then up the screen as far as it needs. A move alone (a drag) is left alone."""
        if height == getattr(self, "fitted", None) or self.state() != "normal":  # not minimized or maximized
            return
        self.fitted, frame = height, w.RECT()
        user32.GetWindowRect(user32.GetParent(self.winfo_id()), ctypes.byref(frame))  # with its title bar
        area, tall = work_area((frame.left + frame.right) // 2, frame.top), frame.bottom - frame.top
        room = area.bottom - area.top
        if tall > room and self.pad_box.most > px(60):  # taller than the screen: fewer pad rows
            self.pad_box.most = max(px(60), min(self.pad_box.most, self.pads.winfo_reqheight()) - (tall - room))
            self.pad_box.fit()
        if (top := max(area.top, min(frame.top, area.bottom - tall))) != frame.top:
            self.geometry(f"+{frame.left}+{top}")

    def bar(self, parent: tk.Frame, inset: int = 0, **grid) -> tk.Canvas:
        """A slim pill meter (rail + level), drawn in poll(). inset keeps its ends under a slider thumb's travel."""
        meter = tk.Canvas(parent, width=1, height=px(4), bg=CARD, highlightthickness=0)
        meter.grid(sticky="ew", **grid)
        meter.inset = inset
        for tag, color in (("rail", STROKE), ("level", self.lit)):
            meter.create_line(0, 0, 0, 0, width=px(4), capstyle="round", fill=color, tags=tag)
        return meter

    @staticmethod
    def show(meter: tk.Canvas, fraction: float, color: str) -> None:
        r, w = meter.winfo_height() / 2 + meter.inset, meter.winfo_width()  # a pill: round caps, r from each end
        meter.coords("rail", r, r - meter.inset, w - r, r - meter.inset)
        meter.coords("level", r, r - meter.inset, r + (w - 2 * r) * max(0.0, min(1.0, fraction)), r - meter.inset)
        meter.itemconfigure("level", fill=color)

    def picker(self, grid: tk.Frame, row: int, label: str, names, saved: str | None, fallback: str) -> tk.StringVar:
        """A dropdown. Its list is read again each time it opens, so a device plugged in or an app started later shows
        up. A pick stays picked while its app isn't playing or its device is unplugged."""
        var = tk.StringVar(value=saved if isinstance(saved, str) and saved else fallback)
        if row:  # a hairline between rows, edge to edge
            tk.Frame(grid, height=1, bg=WINDOW).grid(row=2 * row - 1, column=0, columnspan=3, sticky="ew")
        ttk.Label(grid, text=ICONS.get(label, ""), style="Icon.TLabel").grid(row=2 * row, column=0, padx=(px(16), 0))
        ttk.Label(grid, text=label).grid(row=2 * row, column=1, sticky="w", padx=px(12, 16))
        box = ttk.Combobox(grid, textvariable=var, state="readonly", width=30)
        box.configure(postcommand=lambda: box.configure(values=names()))
        box.grid(row=2 * row, column=2, sticky="ew", padx=(0, px(14)), pady=px(7))
        box.bind("<<ComboboxSelected>>", self.restart)
        box.bind("<MouseWheel>", lambda e: "break")  # a stray scroll over it mustn't switch the device
        return var

    def watched(self) -> tuple:
        """What the engine was opened against: the devices there are and Windows' defaults. When it changes (a headset
        plugged in or out, the default switched in Windows), the engine opens again and follows."""
        return (audio.devices("waveIn"), audio.devices("waveOut"), audio.default_device("waveIn"),
                audio.default_device("waveOut"))

    def restart(self, *_) -> None:
        """Open the picked devices (after closing the old ones). Without a Send to there is nothing to run."""
        self.stop()
        self.watch = self.watched()
        mic = audio.device_id("waveIn", self.mic.get())
        if mic == audio.WAVE_MAPPER:  # use a real id, so a cable that became the default is caught (None: gone)
            mic = audio.default_device("waveIn")
        out = audio.device_id("waveOut", self.out.get())
        music = self.music.get()
        if not audio.is_app(music):  # a whole device, or Off
            music = audio.device_id("waveOut", music)
            if music == audio.WAVE_MAPPER:
                music = audio.default_device("waveOut")  # loopback needs a real device, also checked against Send to
        monitor = audio.default_device("waveOut") if self.hear.get() else None
        self.engine = None if out is None else audio.Engine(self.shared, mic, out, music, monitor, self.isolate.get())
        self.save()

    def stop(self) -> None:
        if self.engine:
            self.engine.running = False
            self.engine.join(1)

    def set_fade(self, *_) -> None:
        voice, music = audio.gains(self.fade.get())
        self.shared.gain = voice, music
        for label, gain in zip(self.levels + (self.pop_levels if self.pop else []), (voice, music) * 2):
            label.configure(text=f"{gain:.0%}")

    def flyout(self) -> None:
        """The pop-up by the clock, as Twinkle Tray's: the slider, the level, mute, voice isolation and the first
        sounds. A click anywhere else, or on the icon again, closes it. Its controls share the window's variables."""
        if self.pop:
            self.close_flyout()
            return
        pop = self.pop = tk.Toplevel(self, bg=NEON)  # the 1px edge
        pop.overrideredirect(True)
        pop.protocol("WM_DELETE_WINDOW", self.close_flyout)  # Alt+F4: closed our way, so self.pop isn't left dead
        pop.bind("<Escape>", lambda e: self.close_flyout())
        pop.attributes("-topmost", True)
        body = tk.Frame(pop, bg=WINDOW, padx=px(16), pady=px(12))
        body.pack(padx=1, pady=1)
        brand(body, "")
        tk.Frame(body, bg=WINDOW, height=px(10)).pack()
        mixer = card(body, px(14))
        mixer.columnconfigure(1, weight=1)
        self.pop_levels = []
        for col, name in ((0, "Voice"), (2, "Music")):
            ttk.Label(mixer, text=name, style="Dim.TLabel").grid(row=0, column=col, sticky="w", pady=px(8, 0))
            self.pop_levels.append(ttk.Label(mixer, style="Value.TLabel", width=5))
            self.pop_levels[-1].grid(row=0, column=col + 1, sticky="w", padx=(px(8), 0), pady=px(8, 0))
        ttk.Scale(mixer, from_=0, to=1, variable=self.fade, command=self.set_fade, length=px(300),
                  style="Fluent.Horizontal.TScale").grid(row=1, column=0, columnspan=4, sticky="ew", pady=px(4))
        self.pop_meter = self.bar(mixer, row=2, column=0, columnspan=4)
        switches = tk.Frame(mixer, bg=CARD)
        switches.grid(row=3, column=0, columnspan=4, sticky="w", pady=px(8, 10))
        ttk.Checkbutton(switches, text="Mute mic", variable=self.muted, command=self.set_mute).pack(side="left",
                                                                                                  padx=(0, px(16)))
        ttk.Checkbutton(switches, text="Voice isolation", variable=self.isolate, command=self.restart).pack(side="left")
        if names := self.sound_names()[:8]:
            pads = card(body, px(10))
            for i, name in enumerate(names):
                ttk.Button(pads, text=Path(name).stem, style="Pad.TButton", command=lambda n=name: self.play(n)).grid(
                    row=i // 4, column=i % 4, sticky="nsew", padx=px(0, 4), pady=px(8, 0) if i < 4 else px(4, 0))
            for col in range(4):
                pads.columnconfigure(col, weight=1, uniform="pad")
            tk.Frame(pads, bg=CARD, height=px(6)).grid(row=2, column=0)
        self.pop_status = ttk.Label(body, style="Note.TLabel", wraplength=px(330))
        self.pop_status.pack(fill="x", pady=px(2, 10))
        tools = tk.Frame(body, bg=WINDOW)
        tools.pack(fill="x")
        for text, command in (("Open MicMix", lambda: self.act("show")), ("Stop sounds", lambda: self.act("stop")),
                              ("Quit", self.close)):
            ttk.Button(tools, text=text, command=command).pack(side="left", padx=(0, px(6)))
        self.set_fade()
        at, gap = w.POINT(), px(12)
        user32.GetCursorPos(ctypes.byref(at))
        area = work_area(at.x, at.y)  # the screen the icon is on, minus its taskbar

        def place(_=None) -> None:  # again whenever its size settles (the cards size themselves a moment later)
            width = pop.winfo_reqwidth()
            x = min(max(at.x - width // 2, area.left + gap), area.right - width - gap)  # under the pointer, on screen
            y = (f"+{area.top + gap}" if at.y < area.top else  # by the taskbar, wherever it is. "-N": its bottom edge
                 f"-{pop.winfo_screenheight() - area.bottom + gap}")  # stays put, so growing pushes it up, not under
            if (x, y) != getattr(pop, "at", None):  # moving it fires <Configure> again: only move when it changed
                pop.at = x, y
                pop.geometry(f"+{x}{y}")
        place()
        pop.bind("<Configure>", lambda e: e.widget is pop and place())
        pop.focus_force()
        pop.bind("<FocusOut>", lambda e: self.after(150, self.flyout_focus))

    def flyout_focus(self) -> None:
        """Close the panel once the focus has gone elsewhere (a click outside it)."""
        if not self.pop:
            return
        try:
            inside = str(self.focus_get() or "").startswith(str(self.pop))
        except (KeyError, tk.TclError):
            inside = False
        if not inside:
            self.close_flyout()

    def close_flyout(self) -> None:
        if self.pop:
            self.pop.destroy()
            self.pop, self.pop_closed = None, time.monotonic()

    def set_gate(self, *_) -> None:
        on = self.gate.get() > GATE_OFF + 0.5
        self.shared.gate = self.gate.get() if on else None
        self.gate_label.configure(text=f"{self.gate.get():.0f} dB" if on else "Off")

    def set_volume(self, *_) -> None:
        self.shared.board = self.volume.get() ** 2  # squared, as the music: even steps to the ear
        self.volume_label.configure(text=f"{self.volume.get():.0%}")

    def set_mute(self) -> None:
        self.shared.muted = self.muted.get()

    def sound_names(self) -> list[str]:
        with contextlib.suppress(OSError):
            SOUNDS.mkdir(exist_ok=True)  # deleted while MicMix runs: back, so Add sounds and Open folder still work
            return sorted((p.name for p in SOUNDS.iterdir() if p.suffix.lower() in SOUND_TYPES), key=str.lower)
        return []

    def draw_pads(self, force: bool = False) -> None:
        names = self.sound_names()
        if names == self.shown_sounds and not force and self.pads.winfo_children():
            return
        self.shown_sounds = names
        for gone in set(self.cache) - set(names):  # deleted or renamed: free its memory
            del self.cache[gone]
        for child in self.pads.winfo_children():
            child.destroy()
        if not names:
            ttk.Label(self.pads, text="Add MP3, WAV or M4A files to play them into your mic.", style="Dim.TLabel").grid(
                row=0, column=0, columnspan=4, sticky="w", pady=px(4))  # across the columns, not widening one
        for i, name in enumerate(names):
            key = self.keys.get(f"sound:{name}")
            pad = ttk.Button(self.pads, text=Path(name).stem + (f"\n{key[2]}" if key else ""), style="Sound.TButton",
                             command=lambda n=name: self.play(n))
            pad.grid(row=i // 4, column=i % 4, sticky="nsew", padx=px(0, 6), pady=px(0, 6))
        for col in range(4):
            self.pads.columnconfigure(col, weight=1, uniform="pad")

    def play(self, name: str) -> None:
        """Into the mix (and your headphones). A sound is decoded once, on its own thread, then kept in memory until
        its file changes."""
        try:
            info = (SOUNDS / name).stat()
        except OSError:
            self.note = (f"Can't play {Path(name).stem}: the file is gone.", time.monotonic() + 6)
            return
        stamp = info.st_mtime_ns, info.st_size  # a file replaced under the same name gets decoded again
        if self.cache.get(name, (None,))[0] == stamp:
            self.shared.clips.put(self.cache[name][1])
            return

        def load_and_play() -> None:
            try:
                pcm = audio.decode(str(SOUNDS / name))
            except (OSError, MemoryError) as e:
                self.note = (f"Can't play {Path(name).stem}: {e or 'not enough memory'}.", time.monotonic() + 6)
                return
            self.cache[name] = stamp, pcm
            self.shared.clips.put(pcm)
        threading.Thread(target=load_and_play, daemon=True).start()

    def add_sounds(self) -> None:
        types = [("Sounds", " ".join(f"*{t}" for t in sorted(SOUND_TYPES))), ("All files", "*.*")]
        for path in filedialog.askopenfilenames(parent=self, title="Add sounds", filetypes=types):
            with contextlib.suppress(OSError, shutil.SameFileError):
                shutil.copy2(path, SOUNDS / Path(path).name)
        self.draw_pads()

    def open_folder(self) -> None:
        os.startfile(SOUNDS)

    def keybinds(self) -> None:
        """A window listing every action and sound with its keys: click Set, then press the keys."""
        if self.dialog and self.dialog.winfo_exists():
            self.dialog.lift()
            return
        top = self.dialog = tk.Toplevel(self, bg=WINDOW, padx=px(20), pady=px(16))
        top.withdraw()  # shown once it's built
        top.title("MicMix keybinds")
        top.transient(self)
        ttk.Label(top, text="Keybinds", style="Title.TLabel").pack(anchor="w")
        ttk.Label(top, text="They work in any app, games included. Click Set, then press the keys (Esc cancels). Keys "
                            "that type need Ctrl or Alt; F1-F24 and the number pad work alone.",
                  style="Note.TLabel", wraplength=px(560)).pack(anchor="w", pady=(0, px(12)))
        box, body = scroller(card(top, px(16)), px(440))
        box.pack(fill="both", expand=True, pady=px(6))
        body.columnconfigure(1, weight=1)
        names = self.sound_names()
        gone = [a[6:] for a in self.keys if a.startswith("sound:") and a[6:] not in names]  # still bound: clearable
        rows = [*ACTIONS.items(), *((f"sound:{n}", f"Play {Path(n).stem}") for n in names),
                *((f"sound:{n}", f"Play {Path(n).stem} (file gone)") for n in gone)]
        top.bind("<Destroy>", lambda e: e.widget is top and not self.hotkeys and self.rebind())  # closed mid-capture
        self.key_labels = {}
        for r, (action, label) in enumerate(rows):
            ttk.Label(body, text=label).grid(row=r, column=0, sticky="w", pady=px(3), padx=(0, px(16)))
            shown = ttk.Label(body, style="Dim.TLabel", width=-24)  # -: at least that wide, wider for long text
            shown.grid(row=r, column=1, sticky="w")
            ttk.Button(body, text="Set", command=lambda a=action: self.capture(a)).grid(row=r, column=2, padx=px(6),
                                                                                       pady=px(3))
            ttk.Button(body, text="Clear", command=lambda a=action: self.bind_key(a, None)).grid(row=r, column=3)
            self.key_labels[action] = shown
        self.show_keys()
        dark_titlebar(top)
        top.deiconify()

    def capture(self, action: str) -> None:
        if self.hotkeys:  # let go of MicMix's own keys meanwhile, or pressing one would run it instead of moving it
            self.hotkeys.stop()
            self.hotkeys = None
        self.key_labels[action].configure(text="Press the keys…", foreground=self.lit)
        self.dialog.focus_force()

        def pressed(event: tk.Event) -> None:
            if event.keycode in (0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5):
                return  # a modifier on its own: wait for the key
            if event.keysym == "Escape":
                self.dialog.unbind("<KeyPress>")
                self.rebind()
                self.show_keys()
                return
            held = [m for m in MODIFIERS if user32.GetAsyncKeyState(m[0]) & 0x8000]
            mods = sum({m[1] for m in held})
            if not mods & 11 and not 0x60 <= event.keycode <= 0x87:  # 11: Ctrl, Alt or Win; 0x60-0x87: numpad, F-keys
                self.key_labels[action].configure(text="Add Ctrl or Alt (that key types)", foreground=self.lit)
                return  # a key that types would stop typing in every app: keep listening
            self.dialog.unbind("<KeyPress>")
            name = "+".join([*dict.fromkeys(m[2] for m in held), key_name(event)])
            self.bind_key(action, [mods, event.keycode, name])
        self.dialog.bind("<KeyPress>", pressed)

    def bind_key(self, action: str, binding: list | None) -> None:
        """One action per key: binding a key takes it from whatever had it."""
        self.dialog.unbind("<KeyPress>")  # Clear while a Set still listens: that Set is off
        for other, keys in list(self.keys.items()):
            if binding and keys[:2] == binding[:2]:
                del self.keys[other]
        self.keys.pop(action, None)
        if binding:
            self.keys[action] = binding
        self.rebind()
        self.save()
        self.draw_pads(force=True)  # a pad shows its key
        self.show_keys()

    def show_keys(self) -> None:
        taken = self.hotkeys.taken if self.hotkeys else set()
        for action, shown in self.key_labels.items():
            key = self.keys.get(action)
            text = (key[2] + ("  (another app has it)" if action in taken else "")) if key else "None"
            shown.configure(text=text, foreground=TEXT if key and action not in taken else TEXT3)

    def rebind(self) -> None:
        if self.hotkeys:
            self.hotkeys.stop()
        self.hotkeys = Hotkeys(dict(self.keys), self.pressed)

    def act(self, action: str) -> None:
        """What a keybind does."""
        if action == "mute":
            self.muted.set(not self.muted.get())
            self.set_mute()
        elif action == "music":  # off: voice only; on: back to where the slider was
            if self.fade.get() > 0.01:
                self.last_fade = self.fade.get()
            self.fade.set(0 if self.fade.get() > 0.01 else self.last_fade)
            self.set_fade()
        elif action in ("up", "down"):  # music is full from the middle on: "up" stops there, or it'd take your voice
            f = self.fade.get()
            self.fade.set(min(max(f, 0.5), f + 0.05) if action == "up" else max(0.0, f - 0.05))
            self.set_fade()
        elif action == "stop":
            self.shared.clips.put(audio.STOP)
        elif action.startswith("sound:"):
            self.play(action[6:])
        elif action == "flyout":  # a click on the tray icon; the click that just closed the panel doesn't reopen it
            if time.monotonic() - self.pop_closed > 0.4:
                self.flyout()
        elif action == "show":  # the full window: from the panel, the tray menu, or MicMix started again
            self.close_flyout()
            self.deiconify()
            self.lift()
            self.focus_force()
        elif action == "quit":
            self.close()

    def poll(self) -> None:
        """Every 50 ms. One error in it mustn't stop it: keybinds, the tray and Quit all come through here."""
        try:
            self.tick()
        finally:
            if not self.closed:  # "quit" already closed the window
                self.after(50, self.poll)

    def tick(self) -> None:
        with contextlib.suppress(queue.Empty):
            while not self.closed:
                self.act(self.pressed.get_nowait())
        if self.closed:
            return
        self.ticks += 1
        if self.ticks % 20 == 0 and self.watched() != self.watch:  # once a second
            self.restart()
        s, music = self.shared, self.music.get()
        peak = s.peak
        self.show(self.meter, 1 + audio.db(peak) / 60, self.lit)  # the top 60 dB
        self.show(self.mic_meter, 1 - audio.db(s.mic_peak) / GATE_OFF, self.lit if s.gate_open else TEXT3)
        s.peak = s.mic_peak = 0
        if self.engine and self.engine.error:
            text = self.engine.error
        elif not self.engine:
            text = "Pick Send to. For a virtual mic, install VB-CABLE first (see README.md)."
        elif time.monotonic() < self.note[1]:
            text = self.note[0]
        elif self.engine.warning:
            text = self.engine.warning
        elif music == OFF:
            text = "Live: your mic only. Pick the app your music plays in under Music from."
        elif audio.is_app(music):
            text = f"Live: your mic + {music}, nothing else."
        elif self.engine.devs[2] is None:
            text = f"Live: your mic only. {music} isn't plugged in."
        else:
            text = f"Live: your mic + everything playing on {music}."
        if s.muted and self.engine and not self.engine.error:
            text = "Mic muted. " + text
        self.status.configure(text=text)
        if self.pop:
            self.show(self.pop_meter, 1 + audio.db(peak) / 60, self.lit)
            self.pop_status.configure(text=text)

    def save(self) -> None:
        data = {"mic": self.mic.get(), "music": self.music.get(), "out": self.out.get(),
                "fade": round(self.fade.get(), 3), "gate": round(self.gate.get(), 1),
                "board": round(self.volume.get(), 3), "hear": self.hear.get(), "isolate": self.isolate.get(),
                "keys": self.keys, "tray_tip": self.tray_tip}
        with contextlib.suppress(OSError):  # written aside, then swapped in: a save cut short can't wipe it
            new = SETTINGS.with_suffix(".tmp")
            new.write_text(json.dumps(data, indent=1), encoding="utf-8")
            new.replace(SETTINGS)

    def hide(self) -> None:
        """The close button: MicMix keeps your mic going from the notification area (when it has an icon there)."""
        if not self.tray.added:
            self.close()
            return
        self.withdraw()
        if not self.tray_tip:  # said once (set before saving, so it stays said)
            self.tray_tip = True
            self.tray.notify("MicMix is still on", "Your mic keeps going. Click MicMix's icon by the clock (under ^ if "
                                                   "Windows hid it) to open it; right-click it to quit.")
        self.save()

    def close(self) -> None:
        self.closed = True
        self.save()
        self.stop()
        for thread in (self.hotkeys, self.tray):
            if thread:
                thread.stop()
        self.destroy()


def main(args: list[str]) -> None:
    """The window; or, for the installer: --save-defaults FILE / --restore-defaults FILE (Windows' default devices,
    which VB-CABLE's setup changes)."""
    if args[:1] == ["--save-defaults"]:
        Path(args[1]).write_text(json.dumps(audio.defaults(), indent=1), encoding="utf-8")
    elif args[:1] == ["--restore-defaults"]:  # exit code 1 if Windows refused any (a device that's gone)
        sys.exit(1 if audio.set_defaults(json.loads(Path(args[1]).read_text(encoding="utf-8"))) else 0)
    elif args[:1] and args[0].startswith("--"):  # an older installer's --icon: nothing to do, and no window
        return
    else:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = w.HANDLE
        running = k32.CreateMutexW(None, False, "Local\\MicMix")  # noqa: F841  (held until exit)
        if ctypes.get_last_error() in (183, 5):  # ALREADY_EXISTS; ACCESS_DENIED: one run as administrator holds it.
            tray = user32.FindWindowW(TRAY_CLASS, None)  # A second copy would send everything twice: bring the
            user32.AllowSetForegroundWindow(-1)  # running one's window up instead, handing it the right to come first
            if not (tray and user32.PostMessageW(tray, WM_SHOW, 0, 0)):  # an elevated one can't be messaged from here
                user32.MessageBoxW(None, "MicMix is already open. Look for it on the taskbar.", "MicMix", 0x40)
        else:
            shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)  # its own taskbar button, not Python's
            try:
                App().mainloop()
            except Exception:  # under pythonw a start-up error would vanish without a trace
                import traceback
                user32.MessageBoxW(None, traceback.format_exc()[-1500:], "MicMix couldn't start", 0x10)


if __name__ == "__main__":
    main(sys.argv[1:])
