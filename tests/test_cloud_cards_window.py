"""The claim card's real window, on Windows (CI's Windows jobs, a booth PC): it goes up, its code is
kept out of a screen grab, and it comes down when told. Elsewhere there's no booth: skipped."""
import sys
import threading
import time

import pytest

from commentator import cloud_cards

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the booth's window: Windows only")


def _white_in_the_cards_corner() -> float:
    """How much of the primary screen's bottom-right corner is pure white, in a screen grab: the
    card's code is a white square there."""
    import mss

    with mss.mss() as sct:
        img = sct.grab(sct.monitors[1])
    w, h = img.size
    px = img.rgb
    white = total = 0
    for y in range(h // 2, h, 4):
        for x in range(int(w * 0.6), w, 4):
            i = (y * w + x) * 3
            total += 1
            white += px[i] > 250 and px[i + 1] > 250 and px[i + 2] > 250
    return white / total


def _show(monkeypatch, back_in_capture: bool):
    """Put the card up; returns (what's shown, its thread, what Windows said about keeping it out of
    capture). `back_in_capture`: the control, with the exclusion taken off again."""
    said = []
    real = cloud_cards._out_of_capture

    def checked(root):
        got = real(root)
        said.append(got)
        if back_in_capture and got:
            import ctypes

            from commentator.overlay import _hwnd

            assert ctypes.windll.user32.SetWindowDisplayAffinity(_hwnd(root), 0)
        return got

    monkeypatch.setattr(cloud_cards, "_out_of_capture", checked)
    cards = [cloud_cards.Card(str(i), n, f"A1b2C3d4E5f6G7h8I9j0K{i}")
             for i, n in enumerate(cloud_cards.labels(["Mike", "Ann", "Mike", ""]))]
    shown = cloud_cards.Shown("u", cards, time.monotonic(),
                              cloud_cards.claim_link("https://shotcast.example", [(c.name, c.code) for c in cards]))
    thread = threading.Thread(target=cloud_cards.Window(shown)._run, daemon=True)
    thread.start()
    end = time.monotonic() + 20
    while not said and thread.is_alive() and time.monotonic() < end:
        time.sleep(0.1)
    time.sleep(1.5)   # (drawn)
    return shown, thread, said


def test_the_cards_code_is_on_the_screen_but_not_in_a_screen_grab_and_it_comes_down_when_told(monkeypatch):
    before = _white_in_the_cards_corner()
    shown, thread, said = _show(monkeypatch, back_in_capture=False)
    try:
        assert said and thread.is_alive(), "the window never came up"
        hidden = _white_in_the_cards_corner()
        cloud_cards.apply(shown, [{"slot_id": "1", "claimed": True, "name": "Ann R."}])
        time.sleep(1.2)                      # (a redraw with a claimed player)
        assert thread.is_alive() and not shown.closed.is_set()
    finally:
        shown.down.set()
        thread.join(10)
    assert not thread.is_alive() and shown.closed.is_set()
    # Out of capture, or (where this Windows can't do that) no code drawn at all: nothing new in a grab.
    assert hidden <= before + 0.01, (before, hidden, said)
    if not said[0]:
        pytest.skip("this Windows can't keep a window out of screen capture: the card showed no code")

    # The control: the same window put back into capture is in the grab, so the grab does see that
    # corner and it was the exclusion that hid the code.
    shown, thread, said = _show(monkeypatch, back_in_capture=True)
    try:
        seen = _white_in_the_cards_corner()
    finally:
        shown.down.set()
        thread.join(10)
    assert seen > before + 0.03, (before, seen)
    assert not thread.is_alive()
