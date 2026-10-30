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


def window_info(substr: str) -> dict | None:
    """The title, screen rectangle (left, top, right, bottom), minimized state and whether it's a Unity
    game window ("game") of the window find_window picks, or None when there isn't one (or not on
    Windows)."""
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
    return {"title": buf.value, "rect": (r.left, r.top, r.right, r.bottom),
            "minimized": bool(_user32.IsIconic(hwnd)), "game": cls.value == UNITY_CLASS}


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
        return Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
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
