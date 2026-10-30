"""The ON AIR badge: a small always-on-top window over GSPro with a pulsing red dot. Clicking it takes
the booth off air (and back on). Tk runs on its own thread; the click is handed to the booth's event
loop, and the badge reads the booth's actual state so it never shows something the booth isn't doing.
It hides while GSPro's window is closed or minimized.

While the booth's health checks have an issue open (commentator/health.py), a "!" chip shows to the
right of the badge: amber for a warning, red for a failure. Hover it for the list; right-click it to
dismiss it until the issues change.
"""

import logging
import math
import sys
import threading
import time
from collections.abc import Callable

from .config import HUD_ASPECT, OverlayConfig

log = logging.getLogger(__name__)

KEY = "#ff00fe"          # painted where the window should be see-through
PANEL = "#14232a"        # GSPro's dark panel colour
PANEL_EDGE = "#2b3e46"
RED = (230, 32, 40)
RED_DIM = (120, 18, 22)
OFF_GREY = "#7d8a90"
AMBER = "#f0a020"
FAIL_RED = "#e02028"
CHIP_EVERY_S = 0.5
ON_TEXT = "#ffffff"
FRAME_MS = 40
PULSE_SECONDS = 1.6
TOPMOST_EVERY_MS = 1000
MONITOR_EVERY_S = 3.0    # how often to check GSPro hasn't moved to another monitor


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> str:
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def pulse(now: float) -> float:
    """0..1..0 over one pulse, eased so the dot lingers bright."""
    return 0.5 - 0.5 * math.cos(2 * math.pi * (now % PULSE_SECONDS) / PULSE_SECONDS)


def badge_geometry(cfg: OverlayConfig, sw: int, sh: int) -> tuple[int, int, int, int]:
    """(x, y, w, h) of the badge. GSPro sizes its HUD to the screen width, so the badge does too: the
    config fractions are for a 16:10 screen, and on another shape the badge keeps its place in the
    HUD's top strip (same horizontal fraction, height and top offset scaled with the width)."""
    r = (sw / sh) / HUD_ASPECT if sh else 1.0
    h = max(22, round(sh * 0.036 * r * cfg.scale))
    return round(sw * cfg.left), round(sh * cfg.top * r), round(h * 3.7), h


class OnAirOverlay:
    def __init__(self, cfg: OverlayConfig, is_on: Callable[[], bool], toggle: Callable[[], None],
                 health_status: Callable[[], tuple[str | None, list[str]]] | None = None,
                 dismiss: Callable[[], None] | None = None,
                 where: Callable[[], tuple[int, int, int, int]] | None = None,
                 showing: Callable[[], bool] | None = None) -> None:
        self.cfg = cfg
        self.where = where        # (left, top, width, height) of GSPro's monitor; None: the primary
        self.showing = showing    # GSPro's window is up; the badge hides while it isn't. None: always shown
        self._visible = True
        self.is_on = is_on        # read on the Tk thread every frame; a bool read is safe
        self.toggle = toggle      # called on the Tk thread; must hand off to the booth's loop itself
        self.health_status = health_status   # thread-safe: (worst level or None, one line per issue)
        self.dismiss = dismiss
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if sys.platform == "win32":
            _dpi_aware()
        self._thread = threading.Thread(target=self._run, name="on-air-overlay", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self._build_and_loop()
        except Exception:
            log.exception("ON AIR overlay failed; the booth carries on without it")

    def _monitor(self) -> tuple[int, int, int, int] | None:
        if self.where is None:
            return None
        try:
            return self.where()
        except Exception:
            log.debug("Couldn't find GSPro's monitor", exc_info=True)
            return None

    def _gspro_up(self) -> bool:
        if self.showing is None:
            return True
        try:
            return bool(self.showing())
        except Exception:
            log.debug("Couldn't tell whether GSPro is open", exc_info=True)
            return True

    def _build_and_loop(self) -> None:
        import tkinter as tk

        # One root for the thread's life, never shown: the badge is a window under it, rebuilt on the
        # new monitor when GSPro moves (it may be another size). Replacing the root itself would leave
        # its interpreter to be freed on another thread, which aborts the process.
        root = tk.Tk()
        root.withdraw()
        self._visible = self._gspro_up()
        if not self._visible:
            log.info("ON AIR badge hidden until GSPro's window is up")
        self._show(tk, root, self._monitor())
        root.mainloop()

    def _show(self, tk, root, mon: tuple[int, int, int, int] | None) -> None:
        """Show the badge on `mon` (None: the primary screen); it moves itself when GSPro does."""
        win = tk.Toplevel(root)
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        if sys.platform == "win32":
            win.attributes("-transparentcolor", KEY)
        left, top, sw, sh = mon or (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())
        x, y, w, h = badge_geometry(self.cfg, sw, sh)
        x, y = x + left, y + top
        r = h * 0.26                              # dot radius
        font = ("Segoe UI", -round(h * 0.5), "bold")
        cw = round(h * 1.15) if self.health_status else 0     # room for the health chip, see-through when clear
        win.geometry(f"{w + cw}x{h}+{x}+{y}")
        c = tk.Canvas(win, width=w + cw, height=h, bg=KEY, highlightthickness=0, cursor="hand2")
        c.pack()
        _pill(c, 1, 1, w - 2, h - 2, fill=PANEL, outline=PANEL_EDGE)
        cx, cy = h * 0.55, h / 2
        glow = c.create_oval(0, 0, 0, 0, outline="", fill=PANEL)
        dot = c.create_oval(cx - r, cy - r, cx + r, cy + r, outline="", fill=_mix(RED_DIM, RED, 1))
        label = c.create_text(h * 1.05, cy, anchor="w", text="ON AIR", font=font, fill=ON_TEXT)

        def click(e=None) -> None:
            if e is not None and e.x >= w:
                return            # the health chip, not the badge
            try:
                self.toggle()
            except Exception:
                log.exception("ON AIR toggle failed")

        c.bind("<Button-1>", click)
        chip, hide_tip = self._health_chip(tk, win, c, x, y, w, h) if cw else (None, None)

        def reveal() -> None:
            win.deiconify()
            if sys.platform == "win32":
                win.update_idletasks()
                _no_activate(win)

        if self._visible:
            reveal()

        state = {"alive": True, "checked": time.monotonic()}

        def frame() -> None:
            if not state["alive"]:
                return
            now = time.monotonic()
            if (self.where is not None or self.showing is not None) and now - state["checked"] >= MONITOR_EVERY_S:
                state["checked"] = now
                up = self._gspro_up()
                if up != self._visible:
                    self._visible = up
                    log.info("ON AIR badge %s", "shown: GSPro's window is up" if up
                             else "hidden: GSPro's window is closed or minimized")
                    if up:
                        reveal()
                    else:
                        if hide_tip:
                            hide_tip()
                        win.withdraw()
                m = self._monitor() if self.where is not None else None
                if m is not None and m != mon:
                    state["alive"] = False   # this badge's callbacks stop; the new one has its own
                    win.destroy()
                    self._show(tk, root, m)
                    return
            if self.is_on():
                t = pulse(time.monotonic())
                c.itemconfigure(dot, fill=_mix(RED_DIM, RED, 0.35 + 0.65 * t))
                g = r * (1.15 + 0.55 * t)
                c.coords(glow, cx - g, cy - g, cx + g, cy + g)
                c.itemconfigure(glow, fill=_mix(_rgb(PANEL), RED_DIM, 0.6 * (1 - t) + 0.2))
                c.itemconfigure(label, text="ON AIR", fill=ON_TEXT)
            else:
                c.itemconfigure(dot, fill=OFF_GREY)
                c.coords(glow, 0, 0, 0, 0)
                c.itemconfigure(label, text="OFF AIR", fill=OFF_GREY)
            if chip:
                chip()
            root.after(FRAME_MS, frame)

        def stay_on_top() -> None:
            if not state["alive"]:
                return
            # GSPro taking focus (or going fullscreen) can push it above a plain topmost window; re-assert.
            # Not while hidden: SWP_SHOWWINDOW would bring it back.
            if self._visible:
                if sys.platform == "win32":
                    _topmost(win)
                else:
                    win.attributes("-topmost", True)
                    win.lift()
            root.after(TOPMOST_EVERY_MS, stay_on_top)

        frame()
        stay_on_top()
        log.info("ON AIR overlay shown at %s,%s (%sx%s) on a %sx%s screen", x, y, w, h, sw, sh)

    def _health_chip(self, tk, root, c, x: int, y: int, w: int, h: int
                     ) -> tuple[Callable[[], None], Callable[[], None]]:
        """Draw the (hidden) chip and its hover list; returns the per-frame updater and a hide for the list."""
        cx, cy, r = w + h * 0.6, h / 2, h * 0.42
        disc = c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=AMBER, outline=PANEL_EDGE, state="hidden", tags="chip")
        mark = c.create_text(cx, cy, text="!", fill=PANEL, font=("Segoe UI", -round(h * 0.62), "bold"),
                             state="hidden", tags="chip")
        tip = tk.Toplevel(root)
        tip.withdraw()
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        text = tk.Label(tip, justify="left", anchor="w", bg=PANEL, fg=ON_TEXT, padx=10, pady=6,
                        font=("Segoe UI", -max(12, round(h * 0.4))), wraplength=max(400, w * 4),
                        highlightthickness=1, highlightbackground=PANEL_EDGE)
        text.pack()
        state = {"level": None, "lines": [], "at": 0.0, "hover": False}

        def show(_e=None) -> None:
            state["hover"] = True
            if not state["lines"]:
                return
            text.configure(text="Booth health (right-click the ! to dismiss)\n\n" + "\n".join(state["lines"]))
            tip.geometry(f"+{x}+{y + h + 6}")
            tip.deiconify()
            if sys.platform == "win32":
                tip.update_idletasks()
                _no_activate(tip)

        def hide(_e=None) -> None:
            state["hover"] = False
            tip.withdraw()

        def dismiss(_e=None) -> None:
            if self.dismiss:
                try:
                    self.dismiss()
                except Exception:
                    log.exception("Health dismiss failed")
            state["at"] = 0.0
            hide()

        c.tag_bind("chip", "<Enter>", show)
        c.tag_bind("chip", "<Leave>", hide)
        c.tag_bind("chip", "<Button-3>", dismiss)

        def update() -> None:
            now = time.monotonic()
            if now - state["at"] < CHIP_EVERY_S:
                return
            state["at"] = now
            try:
                level, lines = self.health_status()
            except Exception:
                log.exception("Health status failed")
                return
            if (level, lines) == (state["level"], state["lines"]):
                return
            state["level"], state["lines"] = level, lines
            vis = "normal" if level else "hidden"
            c.itemconfigure(disc, state=vis, fill=FAIL_RED if level == "fail" else AMBER)
            c.itemconfigure(mark, state=vis, fill=ON_TEXT if level == "fail" else PANEL)
            if not level:
                hide()
            elif state["hover"]:
                show()

        return update, hide


def _rgb(hexcolor: str) -> tuple[int, int, int]:
    return tuple(int(hexcolor[i:i + 2], 16) for i in (1, 3, 5))


def _pill(c, x0: float, y0: float, x1: float, y1: float, **kw) -> None:
    rad = (y1 - y0) / 2
    pts = []
    for i in range(0, 181, 15):   # left cap
        a = math.radians(90 + i)
        pts += [x0 + rad + rad * math.cos(a), y0 + rad - rad * math.sin(a)]
    for i in range(0, 181, 15):   # right cap
        a = math.radians(270 + i)
        pts += [x1 - rad + rad * math.cos(a), y0 + rad - rad * math.sin(a)]
    c.create_polygon(pts, smooth=False, **kw)


def _hwnd(root) -> int:
    import ctypes

    return ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()


def _dpi_aware() -> None:
    """Work in real pixels. Under display scaling (e.g. 150%) a DPI-unaware Tk reports the scaled-down
    screen size while screen capture later makes the process DPI-aware and the window is placed in real
    pixels, landing the badge well left of where it was aimed. Setting it once up front keeps the two
    consistent; it is a no-op if something already set it."""
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _no_activate(root) -> None:
    """A tool window that never takes focus: clicking the badge leaves GSPro focused, and it stays off
    the taskbar and Alt-Tab."""
    import ctypes

    GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE, WS_EX_TOPMOST = -20, 0x80, 0x08000000, 0x8
    u = ctypes.windll.user32
    hwnd = _hwnd(root)
    u.SetWindowLongW(hwnd, GWL_EXSTYLE, u.GetWindowLongW(hwnd, GWL_EXSTYLE) | WS_EX_TOOLWINDOW
                     | WS_EX_NOACTIVATE | WS_EX_TOPMOST)
    _topmost(root)


def _topmost(root) -> None:
    import ctypes

    HWND_TOPMOST, SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_SHOWWINDOW = -1, 0x1, 0x2, 0x10, 0x40
    ctypes.windll.user32.SetWindowPos(_hwnd(root), HWND_TOPMOST, 0, 0, 0, 0,
                                      SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
