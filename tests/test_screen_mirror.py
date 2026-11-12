from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageOps, ImageStat

from commentator.config import DEFAULT_REGIONS, ScreenConfig
from commentator.hud import OcrItem
from commentator.screen import Screen, mirrored_region

FRAMES = Path(__file__).parent / "fixtures" / "mirror"


def _frame(mirrored: bool = False, bar: bool = True, text: bool = True) -> Image.Image:
    """A 1600x1000 course with GSPro's teal slope bar along the bottom right (or left, mirrored); on the
    green the bar is there with no slope text in it."""
    img = Image.new("RGB", (1600, 1000), (70, 120, 60))
    if bar:
        d = ImageDraw.Draw(img)
        d.rectangle((1320, 940, 1600, 1000), fill=(2, 23, 22))
        for x in range(1330, 1600, 14) if text else ():
            d.rectangle((x, 955, x + 2, 985), fill=(235, 235, 235))
    return ImageOps.mirror(img) if mirrored else img


def test_mirrored_region_flips_left_and_right():
    r = mirrored_region(DEFAULT_REGIONS["minimap"])
    assert round(r.left + r.width, 4) == round(1 - DEFAULT_REGIONS["minimap"].left, 4)
    assert (r.top, r.width, r.height) == (DEFAULT_REGIONS["minimap"].top, DEFAULT_REGIONS["minimap"].width,
                                          DEFAULT_REGIONS["minimap"].height)


def test_the_screen_follows_the_minimap_to_either_side():
    s = Screen(ScreenConfig())
    assert s.detect_mirror(_frame()) is False
    assert s.region("club") == DEFAULT_REGIONS["club"]
    assert s.detect_mirror(_frame(mirrored=True)) is True
    assert s.region("club").left > 0.85
    assert s.region("minimap").left < 0.05
    assert s.region("player_box") == DEFAULT_REGIONS["player_box"]
    assert s.detect_mirror(_frame()) is False


def test_an_empty_slope_bar_on_the_green_tells_the_side_on_a_second_read():
    s = Screen(ScreenConfig())
    assert s.detect_mirror(_frame(mirrored=True, text=False)) is False   # alike to a flat panel: once isn't enough
    assert s.detect_mirror(_frame(mirrored=True, text=False)) is True
    assert s.detect_mirror(_frame(text=False)) is True
    assert s.detect_mirror(_frame(text=False)) is False


@pytest.mark.parametrize("flip", [False, True])
def test_gspro_359_frames_from_a_round(flip):
    """Real 3.59 frames (scaled down): the navy slope bar with its text says standard at once, an empty
    one on the green on a second read; the between-holes scorecard and the game menu, both with flat
    dark panels in the corners, never move it. The scorecard flipped a 2v2 round to mirrored at every
    hole, so its lie, club and minimap boxes were cut from the course."""
    def frame(name):
        img = Image.open(FRAMES / f"359-{name}.jpg").convert("RGB")
        return ImageOps.mirror(img) if flip else img

    s = Screen(ScreenConfig())
    s.mirrored = not flip
    assert s.detect_mirror(frame("course")) is flip
    s.mirrored = not flip
    for name in ("scorecard", "menu", "scorecard"):
        assert s.detect_mirror(frame(name)) is not flip
    assert s.detect_mirror(frame("green")) is not flip
    assert s.detect_mirror(frame("menu")) is not flip
    assert s.detect_mirror(frame("green")) is not flip   # the menu between: not two reads in a row
    assert s.detect_mirror(frame("green")) is flip


def test_an_empty_lie_and_club_with_the_course_in_the_minimaps_corner_are_read_on_the_other_side():
    """The minimap's side couldn't be told (no slope bar showing), and the layout was stale: the lie
    and club boxes read nothing there, so they're read on the other side and that side is kept."""
    img = _frame(bar=False)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 450, 260, 1000), fill=(200, 30, 30))     # the HUD boxes, on the left: mirrored
    d.rectangle((1445, 840, 1600, 1000), fill=(30, 30, 200))
    s = Screen(ScreenConfig())

    def ocr(crop):
        r, g, b = ImageStat.Stat(crop).mean
        text = "Fairway" if r > 150 else "7 Iron RH" if b > 150 else ""
        return [OcrItem(text, 10, 10, 10)] if text else []

    s.ocr_items = ocr
    hud = s.read_hud(["lie", "club"], image=img)
    assert (hud.lie, hud.club, hud.handed, s.mirrored) == ("fairway", "7I", "RH", True)
    assert s.read_hud(["lie", "club"], image=img).lie == "fairway"     # and stays there

    s = Screen(ScreenConfig())    # an at-once read: the other side as it was with the rest
    s.ocr_items = ocr
    s._hud_base = lambda: img
    crops = s.grab_regions(["lie", "club"])
    s._hud_base = lambda: _frame(bar=False)    # the boxes gone by the time they're read
    hud = s.read_hud(["lie", "club"], crops=crops)
    assert (hud.lie, hud.club, s.mirrored) == ("fairway", "7I", True)

    s = Screen(ScreenConfig())
    s.ocr_items = lambda crop: []
    hud = s.read_hud(["lie", "club"], image=img)
    assert (hud.lie, s.mirrored) == (None, False)    # nothing there either: the layout stays

    s = Screen(ScreenConfig())
    s.ocr_items = ocr
    hud = s.read_hud(["lie", "club"], image=_frame(text=False))
    assert (hud.lie, s.mirrored) == (None, False)    # the bar is in the minimap's corner: no second read


def test_a_dark_grey_corner_is_not_the_slope_bar():
    img = _frame(bar=False)
    ImageDraw.Draw(img).rectangle((0, 940, 280, 1000), fill=(40, 40, 42))
    s = Screen(ScreenConfig())
    s.mirrored = True
    assert s.detect_mirror(img) is True


def test_a_frame_without_the_slope_bar_keeps_the_last_layout():
    s = Screen(ScreenConfig())
    s.detect_mirror(_frame(mirrored=True))
    assert s.detect_mirror(_frame(bar=False)) is True
    assert s.detect_mirror(Image.new("RGB", (1600, 1000), (0, 0, 0))) is True


MONITORS = [{"left": -1920, "top": 0, "width": 5760, "height": 2160},      # 0: the whole desktop
            {"left": 0, "top": 0, "width": 3840, "height": 2160},
            {"left": -1920, "top": 0, "width": 1920, "height": 1080}]


def test_monitor_of_finds_the_monitor_holding_the_windows_centre():
    from commentator.screen import monitor_of

    assert monitor_of((-1920, 0, 0, 1080), MONITORS) == 2
    assert monitor_of((0, 0, 3840, 2160), MONITORS) == 1
    assert monitor_of((9000, 0, 9100, 100), MONITORS) is None


def test_the_screen_follows_gspro_to_its_monitor(monkeypatch):
    from commentator import screen as screen_mod, winshot

    window = {"title": "GSPro", "rect": (-1920, 0, 0, 1080), "minimized": False}
    monkeypatch.setattr(winshot, "window_info", lambda title: window)
    clock = [100.0]
    monkeypatch.setattr(screen_mod.time, "monotonic", lambda: clock[0])
    s = Screen(ScreenConfig())
    assert s.monitor_index(MONITORS) == 2
    window["minimized"] = True       # minimized: stays where GSPro was
    clock[0] += 10
    assert s.monitor_index(MONITORS) == 2
    assert s.monitor_index(MONITORS[:2]) == 1   # its monitor unplugged while minimized
    assert s.monitor_index(MONITORS) == 1       # (and the fallback sticks until it's looked up again)
    window = None                    # GSPro closed: back to screen.monitor
    clock[0] += 10
    assert s.monitor_index(MONITORS) == 1
    assert Screen(ScreenConfig(follow_window=False, monitor=1)).monitor_index(MONITORS) == 1
    assert Screen(ScreenConfig(monitor=2)).monitor_index(MONITORS[:2]) == 1   # screen.monitor unplugged


def test_a_windowed_gspro_is_read_from_its_game_view_not_the_monitor(monkeypatch):
    import sys
    from types import SimpleNamespace

    from commentator import screen as screen_mod, winshot

    desktop = Image.new("RGB", (3840, 2160), (0, 0, 0))
    view = (300, 200, 300 + 1600, 200 + 1000)    # a 1600x1000 client area under a title bar
    l, t, w, h = Screen(ScreenConfig()).region("player_box").to_pixels(1600, 1000)
    desktop.paste((200, 10, 10), (view[0] + l, view[1] + t, view[0] + l + w, view[1] + t + h))

    class Sct:
        monitors = [None, {"left": 0, "top": 0, "width": 3840, "height": 2160}]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def grab(self, box):
            img = desktop.crop((box["left"], box["top"], box["left"] + box["width"], box["top"] + box["height"]))
            return SimpleNamespace(size=img.size, bgra=img.convert("RGBX").tobytes("raw", "BGRX"))

    monkeypatch.setitem(sys.modules, "mss", SimpleNamespace(mss=Sct))
    window = {"title": "GSPro", "rect": (292, 169, 1908, 1208), "client": view, "minimized": False, "game": True}
    monkeypatch.setattr(winshot, "window_info", lambda title: window)
    clock = [100.0]
    monkeypatch.setattr(screen_mod.time, "monotonic", lambda: clock[0])
    s = Screen(ScreenConfig())
    box = s.grab(s.region("player_box"))
    assert box.size == (w, h) and box.getcolors() == [(w * h, (200, 10, 10))]
    assert s.monitor_size() == (1600, 1000) and s.monitor_rect() == (0, 0, 3840, 2160)
    window["minimized"] = True       # kept while minimized
    clock[0] += 10
    assert s.monitor_size() == (1600, 1000)
    window = None                    # GSPro closed: the whole monitor
    clock[0] += 10
    assert s.monitor_size() == (3840, 2160)
    window = {"title": "GSPro", "rect": (0, 0, 800, 600), "client": (0, 0, 800, 600), "minimized": False,
              "game": False}         # a browser tab titled GSPro isn't its view
    clock[0] += 10
    assert s.monitor_size() == (3840, 2160)
    assert Screen(ScreenConfig(follow_window=False)).gspro_view() is None


def test_the_on_air_badge_only_shows_while_gspro_is_up(monkeypatch):
    from commentator import winshot
    from commentator.config import OverlayConfig
    from commentator.overlay import OnAirOverlay

    monkeypatch.setattr("sys.platform", "win32")
    window = {"title": "GSPro", "rect": (0, 0, 3840, 2160), "minimized": False, "game": True}
    monkeypatch.setattr(winshot, "window_info", lambda title: window)
    s = Screen(ScreenConfig())
    assert s.gspro_showing()
    window["game"] = False           # only a browser tab titled GSPro
    assert not s.gspro_showing()
    window["game"] = True
    window["minimized"] = True
    assert not s.gspro_showing()
    window = None
    assert not s.gspro_showing()

    def broken(title):
        raise OSError("no user32")

    monkeypatch.setattr(winshot, "window_info", broken)
    assert s.gspro_showing()            # a failed lookup never hides the badge

    def badge(showing):
        return OnAirOverlay(OverlayConfig(), is_on=lambda: True, toggle=lambda: None, showing=showing)

    assert not badge(lambda: False)._gspro_up()
    assert badge(None)._gspro_up()
    assert badge(lambda: 1 / 0)._gspro_up()


class ScreenShotError(Exception):
    """Named as mss's: Windows refusing the grab (the PC locked)."""


def test_a_refused_screen_grab_logs_once_until_capture_works_again(caplog, monkeypatch):
    from commentator import screen as screen_mod

    s = Screen(ScreenConfig())
    monkeypatch.setattr(s, "crop", lambda *a: (_ for _ in ()).throw(ScreenShotError("BitBlt")))
    caplog.set_level("DEBUG", logger="commentator.screen")
    for _ in range(3):
        s.read_hud(["player_box", "hole_box", "lie"], image=_frame())
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1 and "BitBlt" in warnings[0].getMessage()
    assert not [r for r in caplog.records if r.levelname == "ERROR"]

    screen_mod._capture_ok()
    assert "works again" in caplog.records[-1].getMessage()

    caplog.clear()
    monkeypatch.setattr(s, "crop", lambda *a: (_ for _ in ()).throw(ValueError("bad crop")))
    s.read_hud(["player_box"], image=_frame())
    assert [r.levelname for r in caplog.records if r.levelname in ("WARNING", "ERROR")] == ["ERROR"]


def test_a_grab_blacks_out_whatever_isnt_gspro(monkeypatch):
    import types

    from commentator import winshot

    class Sct:   # the desktop: all white
        def grab(self, box):
            return types.SimpleNamespace(size=(box["width"], box["height"]),
                                         bgra=b"\xff" * (box["width"] * box["height"] * 4))
    names = {7: "chrome.exe", 8: "obs64.exe"}
    monkeypatch.setattr(winshot, "exe_name", names.get)
    # GSPro windowed at (0, 0)-(80, 100); Chrome over its top left, OBS over its bottom left.
    monkeypatch.setattr(winshot, "coverage", lambda _t: ((0, 0, 80, 100), [((-50, -50, 20, 30), 7),
                                                                            ((0, 90, 10, 100), 8)]))
    s = Screen(ScreenConfig(show_through=["OBS64.exe"]))
    img = s._sct_grab(Sct(), {"left": 10, "top": 10, "width": 90, "height": 80})
    assert img.getpixel((9, 19)) == img.getpixel((75, 50)) == (0, 0, 0)   # Chrome; beside the window
    assert img.getpixel((10, 20)) == img.getpixel((69, 50)) == img.getpixel((0, 79)) == (255, 255, 255)
    monkeypatch.setattr(winshot, "coverage", lambda _t: (None, []))   # GSPro isn't up
    assert s._sct_grab(Sct(), {"left": 0, "top": 0, "width": 4, "height": 4}).getextrema() == ((0, 0),) * 3
    s.cfg.mask_other_windows = False
    assert s._sct_grab(Sct(), {"left": 0, "top": 0, "width": 4, "height": 4}).getextrema() == ((255, 255),) * 3


def test_a_window_that_moves_during_a_grab_blacks_it_all_out(monkeypatch):
    import types

    from commentator import winshot

    covers = iter([((0, 0, 10, 10), [((0, 0, 5, 10), 7)]), ((0, 0, 10, 10), [])])   # before, after the grab
    monkeypatch.setattr(winshot, "coverage", lambda _t: next(covers))
    monkeypatch.setattr(winshot, "exe_name", lambda _p: "chrome.exe")
    sct = types.SimpleNamespace(grab=lambda box: types.SimpleNamespace(size=(10, 10), bgra=b"\xff" * 400))
    img = Screen(ScreenConfig())._sct_grab(sct, {"left": 0, "top": 0, "width": 10, "height": 10})
    assert img.getextrema() == ((0, 0),) * 3   # where it was as the grab was taken isn't known
    # Not knowing what's in front at all reads nothing.
    monkeypatch.setattr(winshot, "coverage", lambda _t: 1 / 0)
    img = Screen(ScreenConfig())._sct_grab(sct, {"left": 0, "top": 0, "width": 10, "height": 10})
    assert img.getextrema() == ((0, 0),) * 3
