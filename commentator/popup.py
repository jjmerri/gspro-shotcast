"""A yes/no question on the sim's screen: a box over GSPro, on its monitor, that stays on top until
someone answers. Like the ON AIR badge it never takes focus, so GSPro keeps the keyboard and mouse
look; clicking a button still works. Tk runs on the box's own thread for its whole life (made,
shown and torn down there); the answer is handed to the caller's callback on that thread.
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
        buttons = tk.Frame(frame, bg=PANEL)
        buttons.pack()
        done = {"answered": False}

        def answer(yes: bool) -> None:
            if done["answered"]:
                return
            done["answered"] = True
            root.quit()
            try:
                self.on_answer(yes)
            except Exception:
                log.exception("Question answer handler failed")

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
