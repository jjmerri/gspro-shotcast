"""The claim card's real window, on Windows (CI's Windows jobs, a booth PC): it goes up with its code,
the code is kept out of a screen grab, no code is drawn where Windows can't keep it out, and it
comes down when told. Elsewhere there's no booth: skipped.

What's behind the card is the test's own: a plain magenta window under where the card goes, so a
grab of the card's place is all magenta when the card is kept out of it, whatever else is on the
desktop."""
import gc
import sys
import threading
import time

import pytest

from commentator import cloud_cards

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the booth's window: Windows only")
_REAL_CHECK = cloud_cards._out_of_capture
EXCLUSION_SINCE = 19041   # Windows 10 2004: the first that can keep a window out of screen capture


class Backdrop:
    """A magenta window over the bottom right of the primary screen, in capture, till `close`."""

    def __init__(self):
        self.ready, self.stop = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        if not self.ready.wait(30):
            self.close()
            raise AssertionError("no desktop to draw on")

    def _run(self):
        import tkinter as tk

        from commentator.overlay import _dpi_aware

        _dpi_aware()   # (as the card's window is: both in the screen's own pixels)
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{sw * 6 // 10}x{sh * 8 // 10}+{sw - sw * 6 // 10}+{sh - sh * 8 // 10}")
        root.configure(bg="#ff00ff")

        def tick():
            if self.stop.is_set():
                root.quit()
            else:
                root.after(100, tick)

        root.after(300, self.ready.set)
        root.after(0, tick)
        try:
            root.mainloop()
        finally:
            root.destroy()
            del root, tick
            gc.collect()   # the Tk interpreter is freed here, on its own thread

    def close(self):
        self.stop.set()
        self.thread.join(15)
        assert not self.thread.is_alive()


def _shares(rect):
    """Of a screen rectangle (left, top, width, height) as a screen capture sees it: how much is
    (magenta, white, black), each 0 to 1."""
    import mss

    with mss.mss() as sct:
        px = sct.grab({"left": rect[0], "top": rect[1], "width": rect[2], "height": rect[3]}).rgb
    n = magenta = white = black = 0
    for i in range(0, len(px) - 2, 3 * 5):
        r, g, b = px[i], px[i + 1], px[i + 2]
        n += 1
        magenta += r > 235 and g < 20 and b > 235
        white += r > 235 and g > 235 and b > 235
        black += r < 20 and g < 20 and b < 20
    return magenta / n, white / n, black / n


def _show(monkeypatch, mode: str):
    """Put the card up; returns (what's shown, the window, its thread, what Windows said about
    keeping it out of capture). `mode`: "kept out" (as a booth runs it); "let in" (the exclusion
    taken off again, to see the card in a grab); "refused" (as on a Windows that can't keep it out:
    the app is told no, and the card is let into the grab so what it drew can be seen)."""
    said = []

    def checked(root):
        got = _REAL_CHECK(root)   # (the app's own, not an earlier case's stand-in for it)
        said.append(got)
        if mode != "kept out":
            import ctypes

            from commentator.overlay import _hwnd

            ctypes.windll.user32.SetWindowDisplayAffinity(_hwnd(root), 0)
        return False if mode == "refused" else got

    monkeypatch.setattr(cloud_cards, "_out_of_capture", checked)
    cards = [cloud_cards.Card(str(i), n, f"A1b2C3d4E5f6G7h8I9j0K{i}")
             for i, n in enumerate(cloud_cards.labels(["Mike", "Ann", "Mike", ""]))]
    shown = cloud_cards.Shown("u", cards, time.monotonic(),
                              cloud_cards.claim_link("https://shotcast.example", [(c.name, c.code) for c in cards]))
    window = cloud_cards.Window(shown)
    thread = threading.Thread(target=window._run, daemon=True)
    thread.start()
    end = time.monotonic() + 30
    while not said and thread.is_alive() and time.monotonic() < end:
        time.sleep(0.1)
    if not (said and thread.is_alive() and window.rect):
        shown.down.set()   # (whenever it does come up, it comes straight down)
        raise AssertionError("the window never came up")
    time.sleep(3.0)   # (drawn)
    return shown, window, thread, said


def _down(shown, thread):
    shown.down.set()
    thread.join(15)
    assert not thread.is_alive() and shown.closed.is_set()


@pytest.fixture
def backdrop():
    pytest.importorskip("mss")
    b = Backdrop()
    try:
        assert _shares((b_left := 0, 0, 1, 1)) is not None and b_left == 0   # (the screen can be grabbed at all)
        yield b
    finally:
        b.close()


def test_the_card_on_a_real_desktop(monkeypatch, backdrop):
    """One test for all of it, its cases in turn: they use the same corner of the one desktop, and
    tests run side by side (xdist) would be in each other's grabs."""
    _no_code_where_windows_cant_keep_it_out_of_capture(monkeypatch)
    if sys.getwindowsversion().build < EXCLUSION_SINCE:
        pytest.skip("this Windows can't keep a window out of screen capture: the card shows no code")
    _its_code_is_on_the_screen_and_not_in_a_grab(monkeypatch)


def _its_code_is_on_the_screen_and_not_in_a_grab(monkeypatch):
    shown, window, thread, said = _show(monkeypatch, "kept out")
    try:
        # (Refused on a Windows that can: every bay would show "ask the staff" in place of the code.)
        assert said[0], "Windows refused to keep the card out of screen capture"
        assert window.code_drawn
        hidden = _shares(window.rect)
        cloud_cards.apply(shown, [{"slot_id": "1", "claimed": True, "name": "Ann R."}])
        time.sleep(1.5)                      # (redrawn with a claimed player: the window grows a little)
        assert thread.is_alive() and not shown.closed.is_set()
        redrawn = _shares(window.rect)
    finally:
        _down(shown, thread)
    # Where the card is, a grab shows only what's behind it.
    assert hidden[0] > 0.97 and redrawn[0] > 0.97, (hidden, redrawn)

    # The control: the same card let into capture is in the grab, code and all. So the grab does
    # see that piece of the screen, the code was there, and it was the exclusion that hid it.
    shown, window, thread, said = _show(monkeypatch, "let in")
    try:
        assert window.code_drawn
        magenta, white, black = _shares(window.rect)
    finally:
        _down(shown, thread)
    assert magenta < 0.2 and white > 0.15 and black > 0.10, (magenta, white, black)


def _no_code_where_windows_cant_keep_it_out_of_capture(monkeypatch):
    shown, window, thread, said = _show(monkeypatch, "refused")
    try:
        cloud_cards.apply(shown, [{"slot_id": "1", "claimed": True, "name": "Ann R."}])
        time.sleep(1.5)                      # (a redraw doesn't bring the code back)
        assert not window.code_drawn
        magenta, white, black = _shares(window.rect)
    finally:
        _down(shown, thread)
    # The card is there (it says to ask the staff) and there's no code's white square on it.
    assert magenta < 0.2 and white < 0.05, (magenta, white, black)
