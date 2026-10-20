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
