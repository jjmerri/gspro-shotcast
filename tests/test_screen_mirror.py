from PIL import Image, ImageDraw, ImageOps

from commentator.config import DEFAULT_REGIONS, ScreenConfig
from commentator.screen import Screen, mirrored_region


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


def test_an_empty_slope_bar_on_the_green_still_tells_the_side():
    s = Screen(ScreenConfig())
    assert s.detect_mirror(_frame(mirrored=True, text=False)) is True
    assert s.detect_mirror(_frame(text=False)) is False


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
