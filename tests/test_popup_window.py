"""The on-screen question's real window, on Windows (CI's Windows jobs, a booth PC), beside another Tk
loop as busy as the ON AIR badge's: taking the box down ends its own loop only. Elsewhere: skipped."""
import gc
import logging
import sys
import threading

import pytest

from commentator import popup
from commentator.overlay import _end_loop

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the booth's window: Windows only")


def _badge(stop: threading.Event, up: threading.Event) -> None:
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()

    def frame():
        if stop.is_set():
            _end_loop(root)
        else:
            root.after(1, frame)

    root.after(0, frame)
    root.after(0, up.set)
    root.mainloop()
    _end_loop(root)
    del root, frame
    gc.collect()   # the Tk interpreter is freed here, on its own thread


class _Shown(popup.Question):
    """Says when its loop is running (an `after` fires only from inside it)."""

    def __init__(self) -> None:
        super().__init__("Test question", "Taken down by the test", "Yes", "No", on_answer=lambda yes: None)
        self.up = threading.Event()

    def _field(self, tk, root, frame, unit):
        root.after(0, self.up.set)
        return None


def test_taking_a_question_down_leaves_the_badges_loop_running(monkeypatch, caplog):
    monkeypatch.setattr(popup, "TOPMOST_EVERY_MS", 20)
    stop, up = threading.Event(), threading.Event()
    badge = threading.Thread(target=_badge, args=(stop, up), daemon=True)
    badge.start()
    try:
        assert up.wait(30)
        for _ in range(10):
            q = _Shown()
            box = threading.Thread(target=q._run, daemon=True)
            box.start()
            assert q.up.wait(10)        # the box is up, in its loop
            q.close()
            box.join(10)
            assert not box.is_alive()   # the box came down
            assert badge.is_alive()     # and the badge's loop is still going
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    finally:
        stop.set()
        badge.join(10)
