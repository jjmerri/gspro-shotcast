"""Capture a window's own rendered content via the Win32 PrintWindow API, so a window in front
of it (a popup, a notification, the booth's own console) can't corrupt the screenshot.

Windows only; on any other platform, or if the window isn't found, the caller falls back to a
plain monitor grab. Verified to return real frames for GSPro (a Unity app) even when occluded.
"""

import ctypes
import logging
from ctypes import wintypes

from PIL import Image

log = logging.getLogger(__name__)

PW_RENDERFULLCONTENT = 2

try:
    _user32 = ctypes.windll.user32
    _gdi32 = ctypes.windll.gdi32
    try:   # per-monitor, as the overlay asks: whichever sets it first wins, so they must agree
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        _user32.SetProcessDPIAware()
    _AVAILABLE = True
except (AttributeError, OSError):
    _AVAILABLE = False


UNITY_CLASS = "UnityWndClass"   # GSPro's own window, not a folder or browser tab with GSPro in its title


def find_window(substr: str) -> int | None:
    """The handle of the visible window whose title contains `substr`: a Unity game window first, then
    the largest. None if there isn't one."""
    if not _AVAILABLE:
        return None
    matches: list[tuple[int, tuple[bool, int]]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        if _user32.IsWindowVisible(hwnd):
            n = _user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                _user32.GetWindowTextW(hwnd, buf, n + 1)
                if substr.lower() in buf.value.lower():
                    r = wintypes.RECT()
                    _user32.GetWindowRect(hwnd, ctypes.byref(r))
                    cls = ctypes.create_unicode_buffer(64)
                    _user32.GetClassNameW(hwnd, cls, 64)
                    matches.append((hwnd, (cls.value == UNITY_CLASS, (r.right - r.left) * (r.bottom - r.top))))
        return True

    _user32.EnumWindows(visit, 0)
    return max(matches, key=lambda m: m[1])[0] if matches else None


def _client(hwnd) -> tuple[int, int, int, int] | None:
    """The window's client area (inside its title bar and border) on the desktop, or None."""
    r, origin = wintypes.RECT(), wintypes.POINT(0, 0)
    if not (_user32.GetClientRect(hwnd, ctypes.byref(r)) and _user32.ClientToScreen(hwnd, ctypes.byref(origin))):
        return None
    return origin.x, origin.y, origin.x + r.right, origin.y + r.bottom


def window_info(substr: str) -> dict | None:
    """The title, screen rectangle (left, top, right, bottom), client area ("client", the same on the
    desktop: the game's view, without a windowed GSPro's title bar and border), minimized state and
    whether it's a Unity game window ("game") of the window find_window picks, or None when there isn't
    one (or not on Windows)."""
    hwnd = find_window(substr)
    if not hwnd:
        return None
    r = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(r))
    n = _user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    _user32.GetWindowTextW(hwnd, buf, n + 1)
    cls = ctypes.create_unicode_buffer(64)
    _user32.GetClassNameW(hwnd, cls, 64)
    return {"title": buf.value, "rect": (r.left, r.top, r.right, r.bottom), "client": _client(hwnd),
            "minimized": bool(_user32.IsIconic(hwnd)), "game": cls.value == UNITY_CLASS}


# Coverage lookups run on every screen grab, so they use their own typed handles (64-bit HWNDs) and
# remember GSPro's window between grabs.
WDA_EXCLUDEFROMCAPTURE = 0x11
_FIND_SECONDS = 2.0
if _AVAILABLE:
    _u = ctypes.WinDLL("user32")
    _dwm = ctypes.WinDLL("dwmapi")
    _k32 = ctypes.WinDLL("kernel32")
    for _name, _args, _res in (("GetTopWindow", [wintypes.HWND], wintypes.HWND),
                               ("GetWindow", [wintypes.HWND, wintypes.UINT], wintypes.HWND),
                               ("IsWindow", [wintypes.HWND], wintypes.BOOL),
                               ("IsWindowVisible", [wintypes.HWND], wintypes.BOOL),
                               ("IsIconic", [wintypes.HWND], wintypes.BOOL),
                               ("GetWindowRect", [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
                               ("GetWindowDisplayAffinity", [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)],
                                wintypes.BOOL),
                               ("GetWindowThreadProcessId", [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)],
                                wintypes.DWORD)):
        _f = getattr(_u, _name)
        _f.argtypes, _f.restype = _args, _res
    _dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    _dwm.DwmGetWindowAttribute.restype = ctypes.c_long
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
_gspro: list = [None, 0.0]   # GSPro's window handle and when it was looked up


def _frame(hwnd) -> tuple[int, int, int, int] | None:
    """The window's visible bounds (without the invisible resize border), else its rectangle."""
    r = wintypes.RECT()
    if _dwm.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:   # extended frame bounds
        if not _u.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
    return (r.left, r.top, r.right, r.bottom) if r.right >= r.left and r.bottom >= r.top else None


def _pid(hwnd) -> int:
    pid = wintypes.DWORD()
    _u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def exe_name(pid: int) -> str:
    """The program a process runs (e.g. chrome.exe), for the log and show_through: never a window's
    title. Looked up each time, as a process id can be reused by another program."""
    name = f"pid {pid}"
    h = _k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        try:
            buf, n = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
            if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                name = buf.value.replace("/", "\\").rsplit("\\", 1)[-1]
        finally:
            _k32.CloseHandle(h)
    return name


def _shows(hwnd) -> bool:
    """A window that puts pixels on the screen a grab would see: visible, not minimized or cloaked
    (another virtual desktop, a suspended app) and not kept out of captures (the booth's own badge and
    popups). A click-through overlay counts: it can still draw a chat over the game."""
    if not _u.IsWindowVisible(hwnd) or _u.IsIconic(hwnd):
        return False
    cloaked = wintypes.DWORD()
    if _dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0 and cloaked.value:
        return False
    aff = wintypes.DWORD()
    return not (_u.GetWindowDisplayAffinity(hwnd, ctypes.byref(aff)) and aff.value == WDA_EXCLUDEFROMCAPTURE)


def coverage(substr: str) -> tuple[tuple[int, int, int, int] | None, list[tuple[tuple[int, int, int, int], int]]] | None:
    """What a screen grab would show of GSPro: its window's bounds on the desktop (None when it isn't
    up or is minimized) and each other program's window in front of it, as (bounds, pid). None when
    it can't be told (not on Windows)."""
    if not _AVAILABLE:
        return None
    import time

    hwnd, at = _gspro
    now = time.monotonic()
    if not (hwnd and _u.IsWindow(hwnd) and _u.IsWindowVisible(hwnd)) or now - at > _FIND_SECONDS:
        hwnd = find_window(substr)
        cls = ctypes.create_unicode_buffer(64)
        if hwnd and not (_user32.GetClassNameW(hwnd, cls, 64) and cls.value == UNITY_CLASS):
            hwnd = None   # a browser tab or folder with GSPro in its title isn't GSPro
        _gspro[:] = [hwnd, now]
    if not hwnd or not _shows(hwnd) or (bounds := _frame(hwnd)) is None:
        return None, []   # not up, minimized, or on another virtual desktop: it shows nothing
    cloaked = wintypes.DWORD()
    if _dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) != 0:
        raise OSError("couldn't tell whether GSPro's window is on this desktop")   # (a window in front is masked anyway)
    own = _pid(hwnd)
    front: list[tuple[tuple[int, int, int, int], int]] = []
    h = _u.GetTopWindow(None)
    for _ in range(5000):   # the z-order, front to back, down to GSPro
        if not h:
            raise OSError("the window order ran out before GSPro's window")
        if h == hwnd:
            return bounds, front
        if _shows(h) and (pid := _pid(h)) != own:
            if (r := _frame(h)) is None:
                raise OSError("couldn't tell where a window in front of GSPro is")
            front.append((r, pid))
        h = _u.GetWindow(h, 2)   # GW_HWNDNEXT
    raise OSError("the window order never reached GSPro's window")


class _BMI(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("w", wintypes.LONG), ("h", wintypes.LONG),
                ("planes", wintypes.WORD), ("bpp", wintypes.WORD), ("comp", wintypes.DWORD),
                ("imgsize", wintypes.DWORD), ("xppm", wintypes.LONG), ("yppm", wintypes.LONG),
                ("clrused", wintypes.DWORD), ("clrimp", wintypes.DWORD)]


def _capture(hwnd: int) -> Image.Image | None:
    r = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        return None
    hdc = _user32.GetWindowDC(hwnd)
    memdc = _gdi32.CreateCompatibleDC(hdc)
    bmp = _gdi32.CreateCompatibleBitmap(hdc, w, h)
    try:
        _gdi32.SelectObject(memdc, bmp)
        if not _user32.PrintWindow(hwnd, memdc, PW_RENDERFULLCONTENT):
            return None
        bmi = _BMI()
        bmi.size, bmi.w, bmi.h, bmi.planes, bmi.bpp = ctypes.sizeof(_BMI), w, -h, 1, 32  # top-down
        buf = ctypes.create_string_buffer(w * h * 4)
        _gdi32.GetDIBits(memdc, bmp, 0, h, buf, ctypes.byref(bmi), 0)
        img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
        # Just the game's view: a windowed GSPro's title bar and border would shift every region.
        if (c := _client(hwnd)) and c[2] > c[0] and c[3] > c[1]:
            img = img.crop((c[0] - r.left, c[1] - r.top, c[2] - r.left, c[3] - r.top))
        return img
    finally:
        _gdi32.DeleteObject(bmp)
        _gdi32.DeleteDC(memdc)
        _user32.ReleaseDC(hwnd, hdc)


def capture_window(substr: str) -> Image.Image | None:
    """The window's rendered content, occlusion and all ignored, or None if it can't be captured."""
    if not _AVAILABLE:
        return None
    hwnd = find_window(substr)
    if not hwnd:
        return None
    try:
        return _capture(hwnd)
    except Exception:
        log.exception("PrintWindow capture of %r failed", substr)
        return None
