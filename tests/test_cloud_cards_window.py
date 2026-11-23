"""The claim card's real window, on Windows (CI's Windows jobs, a booth PC): it goes up, its code is
kept out of a screen grab, and it comes down when told. Elsewhere there's no booth: skipped."""
import sys
import threading
import time

import pytest

from commentator import cloud_cards

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the booth's window: Windows only")
EXCLUSION_SINCE = 19041   # Windows 10 2004: the first that can keep a window out of screen capture


def _grab(rect):
    """The pixels of a screen rectangle (left, top, width, height), as a screen capture sees them."""
    import mss

    with mss.mss() as sct:
        return sct.grab({"left": rect[0], "top": rect[1], "width": rect[2], "height": rect[3]}).rgb


def _differ(a: bytes, b: bytes) -> float:
    """How much of two grabs of the same rectangle differs (sampled pixels, 0 to 1)."""
    assert len(a) == len(b) and a
    step = 3 * 7
    n = range(0, len(a) - 2, step)
    return sum(a[i:i + 3] != b[i:i + 3] for i in n) / len(n)


def _show(monkeypatch, back_in_capture: bool):
    """Put the card up; returns (what's shown, its thread, what Windows said about keeping it out of
    capture, where the window is). `back_in_capture`: the control, with the exclusion taken off again."""
    said, rect = [], []
    real = cloud_cards._out_of_capture

    def checked(root):
        got = real(root)
        if back_in_capture and got:
            import ctypes

            from commentator.overlay import _hwnd

            assert ctypes.windll.user32.SetWindowDisplayAffinity(_hwnd(root), 0)
        root.update_idletasks()
        rect.append((root.winfo_rootx(), root.winfo_rooty(), root.winfo_width(), root.winfo_height()))
        said.append(got)
        return got

    monkeypatch.setattr(cloud_cards, "_out_of_capture", checked)
    cards = [cloud_cards.Card(str(i), n, f"A1b2C3d4E5f6G7h8I9j0K{i}")
             for i, n in enumerate(cloud_cards.labels(["Mike", "Ann", "Mike", ""]))]
    shown = cloud_cards.Shown("u", cards, time.monotonic(),
                              cloud_cards.claim_link("https://shotcast.example", [(c.name, c.code) for c in cards]))
    thread = threading.Thread(target=cloud_cards.Window(shown)._run, daemon=True)
    thread.start()
    end = time.monotonic() + 30
    while not said and thread.is_alive() and time.monotonic() < end:
        time.sleep(0.1)
    return shown, thread, said, rect


def _down(shown, thread):
    shown.down.set()
    thread.join(15)
    assert not thread.is_alive() and shown.closed.is_set()
    time.sleep(1.0)   # (gone from the screen)


def test_the_cards_code_is_on_the_screen_but_not_in_a_screen_grab_and_it_comes_down_when_told(monkeypatch):
    pytest.importorskip("mss")
    shown, thread, said, rect = _show(monkeypatch, back_in_capture=False)
    try:
        assert said and thread.is_alive(), "the window never came up"
        if not said[0] and sys.getwindowsversion().build < EXCLUSION_SINCE:
            pytest.skip("this Windows can't keep a window out of screen capture: the card shows no code")
        # (Refused on a Windows that can: every bay would show "ask the staff" in place of the code.)
        assert said[0], "Windows refused to keep the card out of screen capture"
        time.sleep(3.0)                      # (drawn, code and all)
        hidden = _grab(rect[0])
        cloud_cards.apply(shown, [{"slot_id": "1", "claimed": True, "name": "Ann R."}])
        time.sleep(1.5)                      # (redrawn with a claimed player)
        assert thread.is_alive() and not shown.closed.is_set()
        redrawn = _grab(rect[0])
    finally:
        _down(shown, thread)
    behind = _grab(rect[0])                  # the same piece of the screen with no card at all
    # Up or not, a grab of where the card is shows only what's behind it.
    assert _differ(hidden, behind) < 0.02 and _differ(redrawn, behind) < 0.02

    # The control: the same window put back into capture is in the grab, so the grab does see that
    # piece of the screen and it was the exclusion that hid the card.
    shown, thread, said, again = _show(monkeypatch, back_in_capture=True)
    try:
        assert again and again[0] == rect[0]
        end = time.monotonic() + 15
        seen = 0.0
        while seen < 0.3 and time.monotonic() < end:
            time.sleep(0.5)
            seen = _differ(_grab(rect[0]), behind)
    finally:
        _down(shown, thread)
    assert seen >= 0.3, seen
