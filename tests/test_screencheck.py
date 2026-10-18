from pathlib import Path

import pytest
import yaml
from PIL import Image

from commentator import screencheck as sc
from commentator.config import DEFAULT_REGIONS, ScreenConfig, load_config
from commentator.hud import OcrItem


def _items(kind, place, w, h):
    """OCR lines for each of a screen's anchors, put where `place` maps their reference point."""
    out = []
    texts = {r"shot \d+": "Shot 2", r"par \d": "PAR 4", r"\d{2,3} yds": "392 YDS", r"club": "CLUB", r"hand": "HAND",
             r".*\b(up|down)": "0.0° UP", r".*\b(right|left)": "0.0° RIGHT", r"play\b.+": "PLAY RIVIERA!",
             r"invite players.*": "INVITE PLAYERS"}
    tiles = iter(["CARRY (game)", "CARRY (raw)", "TOTAL LENGTH", "BALL SPEED", "TOTAL SPIN", "CLUB SPEED"])
    for g in sc.GROUPS[kind]:
        if g.name == "tiles_left":
            continue
        for a in g.anchors:
            text = next(tiles) if a.pattern == sc._TILE else texts.get(a.pattern, a.pattern.upper())
            x, y = place(a.x, a.y, g.pin)
            s = place(1, 1, g.pin)[0] - place(0, 0, g.pin)[0]
            out.append(OcrItem(text=text, x=x, y=y, h=a.h * s, left=x))
    return out


def _boxes_match(proposed, w, h, want=lambda n: DEFAULT_REGIONS[n].to_pixels):
    return {n: round(sc.overlap(r.to_pixels(w, h), want(n)(w, h)), 2) for n, r in proposed.items()}


def test_screens_are_told_apart_by_their_labels():
    lines = lambda *t: [OcrItem(text=x, x=0, y=0, h=10) for x in t]  # noqa: E731
    assert sc.detect_kind(lines("ROUND SETTINGS", "PLAYERS", "MATCH SETTINGS", "HOLES", "GAME MODE")) == "setup_match"
    assert sc.detect_kind(lines("ROUND SETTINGS", "MATCH SETTINGS", "HOLES", "FRONT 9")) == "setup_holes"
    assert sc.detect_kind(lines("ROUND SETTINGS", "PLAYERS", "MATCH SETTINGS", "HOLES")) == "setup_players"
    assert sc.detect_kind(lines("SCORECARD", "HOLE", "INDEX", "CLUB", "HAND")) == "scorecard"
    assert sc.detect_kind(lines("Shot 1", "PAR 4", "CARRY (game)")) == "shot"
    assert sc.detect_kind(lines("Brightness & color")) is None


@pytest.mark.parametrize("w,h", [(1920, 1080), (2560, 1600), (3440, 1440)])
def test_boxes_follow_gspro_scaling_with_the_width(w, h):
    """GSPro scales with the width and pins each panel to its edge, as the defaults assume: the boxes
    placed from the labels land on the defaults, on 16:9, 16:10 and ultrawide alike."""
    s = w / sc.REF_W

    def place(x, y, pin):
        return x * s, (y * s if pin == "top" else h - (sc.REF_H - y) * s if pin == "bottom"
                       else h / 2 + (y - sc.REF_H / 2) * s)

    for kind in ("shot", "setup_players"):
        fits = sc.fit_groups(kind, _items(kind, place, w, h), w, h)
        got = _boxes_match(sc.proposed_regions(fits, w, h), w, h)
        assert got and min(got.values()) > 0.9, got


def test_boxes_follow_a_gspro_window_that_doesnt_fill_the_screen():
    w, h, s, ox, oy = 3450, 2160, 0.7, 400, 250

    def place(x, y, pin):
        return ox + x * s, oy + y * s

    fits = sc.fit_groups("shot", _items("shot", place, w, h), w, h)
    proposed = sc.proposed_regions(fits, w, h)

    def window(n):
        def px(_w, _h):
            left, top, bw, bh = DEFAULT_REGIONS[n].to_pixels(sc.REF_W, sc.REF_H)
            return ox + left * s, oy + top * s, bw * s, bh * s
        return px

    got = _boxes_match(proposed, w, h, window)
    assert set(got) >= {"player_box", "hole_box", "tiles", "club", "slope", "lie"} and min(got.values()) > 0.9, got


def test_a_shifted_hud_is_found_and_read_again():
    """The 16:9 tee fixture shrunk into a window: the default boxes miss most panels, and the moved
    ones read what the fixture did."""
    pytest.importorskip("rapidocr")
    from commentator import fixtures
    from commentator.screen import Screen

    frame, data = fixtures.load_screen(fixtures.ROOT / "screens" / "3.1.6.20-1920x1080-tee")
    canvas = Image.new("RGB", frame.size, (20, 60, 110))
    canvas.paste(frame.resize((1536, 864), Image.LANCZOS), (250, 150))
    rep = sc.run(Screen(ScreenConfig()), canvas)
    assert rep.kind == "shot" and sc.passed(rep.current) < sc.passed(rep.fixed)
    assert {"player_box", "hole_box", "club", "lie"} <= set(rep.moves)
    fixed = {c.label: c for c in rep.fixed}
    for label in ("player", "distance to pin", "course", "par / length", "lie", "club / hand"):
        assert fixed[label].ok, (label, fixed[label])
    assert "FIX" in sc.format_report(rep)


def test_fix_writes_regions_and_keeps_the_rest_of_the_config(tmp_path):
    cfg = tmp_path / "config.yaml"
    example = Path("config.example.yaml").read_text(encoding="utf-8")
    cfg.write_text(example, encoding="utf-8")
    lie = sc.Region(left=0.8, top=0.4, width=0.15, height=0.5)
    backup = sc.write_regions(cfg, {"lie": lie})
    assert backup and backup.read_text(encoding="utf-8") == example
    text = cfg.read_text(encoding="utf-8")
    assert "# regions:" in text and "commentary:" in text   # comments and later sections kept
    loaded = load_config(cfg)
    assert loaded.screen.regions["lie"] == lie and loaded.screen.regions["club"] == DEFAULT_REGIONS["club"]
    assert yaml.safe_load(text)["commentary"] == yaml.safe_load(example)["commentary"]

    club = sc.Region(left=0.01, top=0.8, width=0.1, height=0.15)
    lie2 = sc.Region(left=0.81, top=0.41, width=0.15, height=0.5)
    sc.write_regions(cfg, {"club": club, "lie": lie2})
    regions = load_config(cfg).screen.regions
    assert regions["lie"] == lie2 and regions["club"] == club
    assert cfg.read_text(encoding="utf-8").count("lie:") == text.count("lie:")   # replaced, not added


def test_fix_adds_a_screen_section_when_there_is_none(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("log_dir: logs\n", encoding="utf-8")
    r = sc.Region(left=0.1, top=0.2, width=0.3, height=0.1)
    sc.write_regions(cfg, {"hole_box": r})
    assert load_config(cfg).screen.regions["hole_box"] == r and load_config(cfg).log_dir == "logs"


def test_fix_keeps_inline_regions(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("screen:\n  ocr: true\n  regions: {club: {left: 0.1, top: 0.1, width: 0.1, height: 0.1}}\n"
                   "other: 1\n", encoding="utf-8")
    lie = sc.Region(left=0.8, top=0.4, width=0.15, height=0.5)
    sc.write_regions(cfg, {"lie": lie})
    loaded = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert loaded["screen"]["regions"]["club"] == {"left": 0.1, "top": 0.1, "width": 0.1, "height": 0.1}
    assert sc.Region(**loaded["screen"]["regions"]["lie"]) == lie
    assert loaded["screen"]["ocr"] is True and loaded["other"] == 1


def test_fix_replaces_a_multiline_region_and_follows_four_space_indent(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("screen:\n    ocr: true\n    regions:\n        lie:\n            left: 0.84\n"
                   "            top: 0.45\n            width: 0.16\n            height: 0.53\n"
                   "        club: {left: 0.1, top: 0.1, width: 0.1, height: 0.1}\n"
                   "commentary:\n    enabled: true\n", encoding="utf-8")
    lie = sc.Region(left=0.8, top=0.4, width=0.15, height=0.5)
    hole = sc.Region(left=0.5, top=0.0, width=0.2, height=0.1)
    sc.write_regions(cfg, {"lie": lie, "hole_box": hole})
    loaded = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    regions = loaded["screen"]["regions"]
    assert sc.Region(**regions["lie"]) == lie and sc.Region(**regions["hole_box"]) == hole
    assert regions["club"]["left"] == 0.1 and loaded["commentary"] == {"enabled": True}

    cfg.write_text("screen:\n    ocr: true\ncommentary:\n    enabled: true\n", encoding="utf-8")
    sc.write_regions(cfg, {"lie": lie})
    loaded = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert sc.Region(**loaded["screen"]["regions"]["lie"]) == lie and loaded["screen"]["ocr"] is True


def test_fix_that_cant_be_written_cleanly_changes_nothing(tmp_path):
    cfg = tmp_path / "config.yaml"
    original = "screen:\n  regions: {club: {left: 0.1,\n    top: 0.1, width: 0.1, height: 0.1}}\n"
    cfg.write_text(original, encoding="utf-8")
    with pytest.raises(RuntimeError, match="nothing changed"):
        sc.write_regions(cfg, {"lie": sc.Region(left=0.8, top=0.4, width=0.15, height=0.5)})
    assert cfg.read_text(encoding="utf-8") == original
    assert not (tmp_path / "config.yaml.bak").exists()
