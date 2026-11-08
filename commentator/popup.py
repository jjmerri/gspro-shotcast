"""A question on the sim's screen: a box over GSPro, on its monitor, that stays on top until someone
answers. Like the ON AIR badge it never takes focus, so GSPro keeps the keyboard and mouse look;
clicking a button still works. A box with a text field (`Entry`) takes the keyboard only once its
field is clicked, and hands it back to GSPro when answered. Tk runs on the box's own thread for its
whole life (made, shown and torn down there); the answer is handed to the caller's callback on that
thread.
"""

import gc
import logging
import sys
import threading
from collections.abc import Callable

from .overlay import ON_TEXT, PANEL, PANEL_EDGE, TOPMOST_EVERY_MS, _dpi_aware, _no_activate, _topmost

log = logging.getLogger(__name__)

YES_GREEN = "#2e8b57"
NO_GREY = "#3a4b52"
MUTED = "#b8c4c9"


class Question:
    def __init__(self, title: str, detail: str, yes: str, no: str, on_answer: Callable[[bool], None],
                 where: Callable[[], tuple[int, int, int, int]] | None = None) -> None:
        self.title, self.detail, self.yes, self.no = title, detail, yes, no
        self.on_answer = on_answer      # called once, on the box's thread; must hand off itself
        self.where = where              # (left, top, width, height) of GSPro's monitor; None: the primary
        self._closing = threading.Event()

    def show(self) -> "Question":
        threading.Thread(target=self._run, name="question", daemon=True).start()
        return self

    def close(self) -> None:
        """Takes the box down unanswered (the round ended first)."""
        self._closing.set()

    def _run(self) -> None:
        try:
            self._loop()
        except Exception:
            log.exception("Couldn't show the question on screen: %s", self.title)
        gc.collect()   # the Tk interpreter is freed here, on its own thread

    def _loop(self) -> None:
        import tkinter as tk

        if sys.platform == "win32":
            _dpi_aware()
        mon = None
        if self.where is not None:
            try:
                mon = self.where()
            except Exception:
                log.debug("Couldn't find GSPro's monitor", exc_info=True)
        root = tk.Tk()
        root.withdraw()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        left, top, sw, sh = mon or (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())
        unit = max(14, round(sh * 0.022))
        frame = tk.Frame(root, bg=PANEL, highlightthickness=2, highlightbackground=PANEL_EDGE,
                         padx=unit * 2, pady=unit * 3 // 2)
        frame.pack()
        tk.Label(frame, text=self.title, bg=PANEL, fg=ON_TEXT, font=("Segoe UI", -round(unit * 1.4), "bold"),
                 wraplength=round(sw * 0.4), justify="center").pack()
        tk.Label(frame, text=self.detail, bg=PANEL, fg=MUTED, font=("Segoe UI", -unit),
                 wraplength=round(sw * 0.4), justify="center").pack(pady=(unit // 2, unit))
        field = self._field(tk, root, frame, unit)
        buttons = tk.Frame(frame, bg=PANEL)
        buttons.pack()
        done = {"answered": False}

        def answer(yes: bool) -> None:
            if done["answered"]:
                return
            if (got := self._result(yes, field)) is _INVALID:
                return
            done["answered"] = True
            root.quit()
            self._give_back()
            try:
                self.on_answer(got)
            except Exception:
                log.exception("Question answer handler failed")

        if field is not None:
            field.bind("<Return>", lambda _e: answer(True))

        for text, color, yes in ((self.yes, YES_GREEN, True), (self.no, NO_GREY, False)):
            tk.Button(buttons, text=text, bg=color, fg=ON_TEXT, activebackground=color, activeforeground=ON_TEXT,
                      relief="flat", bd=0, padx=unit * 2, pady=unit // 2, cursor="hand2",
                      font=("Segoe UI", -round(unit * 1.1), "bold"),
                      command=lambda y=yes: answer(y)).pack(side="left", padx=unit // 2)
        root.update_idletasks()
        w, h = root.winfo_reqwidth(), root.winfo_reqheight()
        root.geometry(f"+{left + (sw - w) // 2}+{top + round(sh * 0.3) - h // 2}")
        root.deiconify()
        if sys.platform == "win32":
            root.update_idletasks()
            _no_activate(root)

        def tick() -> None:
            if self._closing.is_set():
                done["answered"] = True
                root.quit()
                return
            # GSPro taking focus (or going fullscreen) can push it above a plain topmost window.
            if sys.platform == "win32":
                _topmost(root)
            else:
                root.attributes("-topmost", True)
                root.lift()
            root.after(TOPMOST_EVERY_MS, tick)

        tick()
        log.info("Asking on screen: %s", self.title)
        root.mainloop()
        root.destroy()

    # A plain question has no field: its answer is yes or no.
    def _field(self, tk, root, frame, unit):
        return None

    def _result(self, yes: bool, field):
        return yes

    def _give_back(self) -> None:
        pass


_INVALID = object()


class Entry(Question):
    """A question with a text field, filled in with the booth's guess: yes answers the field's text
    (once `valid` accepts it; until then the field turns red), no answers None."""

    def __init__(self, title: str, detail: str, value: str, yes: str, no: str,
                 on_answer: Callable[[str | None], None], valid: Callable[[str], bool],
                 where: Callable[[], tuple[int, int, int, int]] | None = None) -> None:
        super().__init__(title, detail, yes, no, on_answer, where)
        self.value, self.valid = value, valid
        self._gspro = None   # the window that had the keyboard before the field took it

    def _field(self, tk, root, frame, unit):
        field = tk.Entry(frame, justify="center", font=("Segoe UI", -round(unit * 1.4), "bold"), width=12,
                         bg="#ffffff", fg="#111111", relief="flat", insertbackground="#111111")
        field.insert(0, self.value)
        field.pack(pady=(0, unit))

        def take_keyboard(_e=None) -> None:
            if sys.platform == "win32" and self._gspro is None:
                self._gspro = _take_focus(root)
            field.focus_force()

        field.bind("<Button-1>", take_keyboard, add="+")
        field.bind("<Key>", lambda _e: field.configure(bg="#ffffff"), add="+")
        return field

    def _result(self, yes: bool, field):
        if not yes:
            return None
        text = field.get().strip()
        if not self.valid(text):
            field.configure(bg="#f4b4b4")
            return _INVALID
        return text

    def _give_back(self) -> None:
        if self._gspro:
            import ctypes

            ctypes.windll.user32.SetForegroundWindow(self._gspro)


def _take_focus(root) -> int | None:
    """Lets the box take the keyboard (it was made never to): the click on it was the last input, so
    Windows allows it. Returns the window that had it, to hand back."""
    import ctypes

    from .overlay import _hwnd

    GWL_EXSTYLE, WS_EX_NOACTIVATE = -20, 0x08000000
    u = ctypes.windll.user32
    before = u.GetForegroundWindow()
    hwnd = _hwnd(root)
    u.SetWindowLongW(hwnd, GWL_EXSTYLE, u.GetWindowLongW(hwnd, GWL_EXSTYLE) & ~WS_EX_NOACTIVATE)
    u.SetForegroundWindow(hwnd)
    return before if before and before != hwnd else None
