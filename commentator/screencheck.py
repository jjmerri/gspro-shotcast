"""`screen-check`: which GSPro screen is up, what the booth reads from it, and where its boxes belong.

The booth reads GSPro through fixed boxes (fractions of the screen) measured on a 16:10 screen and
adjusted to other shapes. When a screen, a GSPro update or a HUD setting moves a panel, a box misses
and the read comes back empty. This finds the panels by the labels GSPro always draws on them ("PAR 4",
"CARRY (game)", "CLUB", "MATCH SETTINGS", ...): one OCR pass over the whole screen, then each group of
boxes is placed by fitting the labels' reference positions (scale and offset) to where they are. The
boxes are read both ways, so a fix is only offered when it reads better, and `--fix` writes it to
config.yaml's screen.regions.
"""

import re
import shutil
import statistics
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from PIL import Image, ImageDraw, ImageFont

from .config import DEFAULT_REGIONS, Region
from .hud import OcrItem

# The screen the reference positions below were measured on (GSPro 3.1.6, 16:10).
REF_W, REF_H = 3450, 2160


@dataclass(frozen=True)
class Anchor:
    pattern: str            # full-match regex, case-insensitive, on one OCR line
    x: float                # reference point (REF_W x REF_H pixels): the text's centre, or its left
    y: float                # edge for left-aligned text that changes length
    h: float                # reference text height, for the scale when a group can't fit its own
    point: str = "center"   # "center" or "left"


@dataclass(frozen=True)
class Group:
    """Boxes that move together, pinned to one edge of the screen the way GSPro lays them out."""
    name: str
    pin: str                # "top", "bottom" or "middle": GSPro scales its UI with the screen's width
    anchors: tuple[Anchor, ...]
    regions: tuple[str, ...]


_TILE = r"(carry|total|ball|club|spin|launch|side|smash|descent|apex|height|roll|offline|back|face|path|attack|peak|speed|angle|distance|dynamic|loft|lie|axis)\b.*"
_TILE_Y = (281, 418, 555, 691, 828, 965)

GROUPS = {
    "shot": (
        Group("player", "top", (Anchor(r"shot \d+", 73, 146, 43, "left"),), ("player_box", "player_name", "others")),
        Group("hole", "top", (Anchor(r"par \d", 2925, 147, 38), Anchor(r"\d{2,3} yds", 3130, 146, 36)), ("hole_box",)),
        Group("club", "bottom", (Anchor(r"club", 157, 1954, 38), Anchor(r"hand", 156, 2099, 38)), ("club",)),
        Group("slope", "bottom", (Anchor(r".*\b(up|down)", 3050, 2105, 52), Anchor(r".*\b(right|left)", 3280, 2104, 54)),
              ("slope", "lie", "minimap")),
        # After the club box, whose CLUB label would otherwise pass for a tile's.
        Group("tiles", "top", tuple(Anchor(_TILE, 3290, y, 38) for y in _TILE_Y), ("tiles",)),
        Group("tiles_left", "top", tuple(Anchor(_TILE, REF_W - 3290, y, 38) for y in _TILE_Y), ("tiles_left",)),
    ),
    "scorecard": (
        Group("scorecard", "middle", (Anchor(r"scorecard", 1682, 534, 57), Anchor(r"hole", 450, 666, 48),
                                      Anchor(r"par", 438, 762, 52), Anchor(r"index", 459, 852, 48),
                                      Anchor(r"distance", 492, 943, 42), Anchor(r"out", 1833, 667, 50),
                                      Anchor(r"in", 2910, 667, 43)), ("scorecard_title", "scorecard")),
    ),
}
_SETUP_TOP = Group("setup", "top", (
    Anchor(r"round settings", 323, 273, 62), Anchor(r"players", 1276, 275, 55),
    Anchor(r"match settings", 1725, 274, 47), Anchor(r"holes", 2175, 276, 57), Anchor(r"back", 185, 89, 50)),
    ("setup_players_tab", "setup_match_tab", "setup_holes_tab", "setup_game_mode", "setup_front9_box",
     "setup_back9_box", *(f"setup_slot_{i}" for i in range(1, 9))))
_SETUP_BOTTOM = Group("play_button", "bottom", (
    Anchor(r"invite players.*", 278, 2073, 40), Anchor(r"play\b.+", 3047, 2069, 40)), ("setup_course",))
for _kind in ("setup_players", "setup_match", "setup_holes"):
    GROUPS[_kind] = (_SETUP_TOP, _SETUP_BOTTOM)

KIND_NAMES = {"shot": "a hole (the HUD)", "scorecard": "the scorecard", "setup_players": "setup, Players tab",
              "setup_match": "setup, Match Settings tab", "setup_holes": "setup, Holes tab"}


def detect_kind(items: list[OcrItem]) -> str | None:
    """Which GSPro screen the OCR lines are from, or None."""
    texts = {i.text.strip().upper() for i in items}
    has = lambda pat: any(re.fullmatch(pat, t, re.I) for t in texts)  # noqa: E731
    if has("MATCH SETTINGS") and has("HOLES") and has("ROUND SETTINGS"):
        if has("GAME MODE"):
            return "setup_match"
        if has(r"(FRONT|BACK) 9") or sum(bool(re.fullmatch(r"\d{2,3} YARDS", t)) for t in texts) >= 3:
            return "setup_holes"
        return "setup_players"
    if has("SCORECARD") and has("INDEX"):
        return "scorecard"
    if (has("CLUB") and has("HAND")) or has(r"PAR \d") or sum(bool(re.fullmatch(_TILE, t, re.I)) for t in texts) >= 2:
        return "shot"
    return None


def _predict(a: Anchor, pin: str, w: int, h: int) -> tuple[float, float]:
    """Where an anchor lands on a w x h screen if GSPro only rescaled (with the width) and re-pinned."""
    s = w / REF_W
    if pin == "top":
        return a.x * s, a.y * s
    if pin == "bottom":
        return a.x * s, h - (REF_H - a.y) * s
    return a.x * s, h / 2 + (a.y - REF_H / 2) * s


@dataclass
class Fit:
    group: Group
    matches: list[tuple[Anchor, OcrItem]]
    scale: float | None = None
    dx: float = 0.0
    dy: float = 0.0


def _point(a: Anchor, item: OcrItem) -> tuple[float, float]:
    return (item.left if a.point == "left" else item.x), item.y


def match_group(group: Group, items: list[OcrItem], w: int, h: int, used: set[int]) -> list[tuple[Anchor, OcrItem]]:
    """Each anchor's OCR line: the nearest one that matches, within a fifth of the screen of where it'd be."""
    out = []
    for a in group.anchors:
        px, py = _predict(a, group.pin, w, h)
        best, dist = None, 0.2 * w
        for i in items:
            if id(i) in used or not re.fullmatch(a.pattern, i.text.strip(), re.I):
                continue
            x, y = _point(a, i)
            d = ((x - px) ** 2 + (y - py) ** 2) ** 0.5
            if d < dist:
                best, dist = i, d
        if best is not None:
            used.add(id(best))
            out.append((a, best))
    return out


def _own_scale(matches: list[tuple[Anchor, OcrItem]]) -> float | None:
    """Least-squares scale from the spread of a group's anchors, if they're spread enough to say."""
    if len(matches) < 2:
        return None
    refs = [(a.x, a.y) for a, _ in matches]
    pts = [_point(a, i) for a, i in matches]
    rx, ry = statistics.fmean(p[0] for p in refs), statistics.fmean(p[1] for p in refs)
    qx, qy = statistics.fmean(p[0] for p in pts), statistics.fmean(p[1] for p in pts)
    num = sum((r[0] - rx) * (q[0] - qx) + (r[1] - ry) * (q[1] - qy) for r, q in zip(refs, pts))
    den = sum((r[0] - rx) ** 2 + (r[1] - ry) ** 2 for r in refs)
    if den < 500 ** 2:  # anchors too close together for a steady scale
        return None
    return num / den


def fit_groups(kind: str, items: list[OcrItem], w: int, h: int) -> list[Fit]:
    """Scale and offset for each of the screen's groups that has an anchor in view."""
    used: set[int] = set()
    fits = [Fit(g, m) for g in GROUPS[kind] if (m := match_group(g, items, w, h, used))
            and (len(m) >= 2 or not g.name.startswith("tiles"))]   # one stray word isn't a tile panel
    for f in fits:
        f.scale = _own_scale(f.matches)
    spread = [f.scale for f in fits if f.scale]
    heights = [i.h / a.h for f in fits for a, i in f.matches if i.h > 0]
    common = (statistics.median(spread) if spread else statistics.median(heights) if heights else w / REF_W)
    for f in fits:
        s = f.scale = f.scale or common
        f.dx = statistics.fmean(_point(a, i)[0] - s * a.x for a, i in f.matches)
        f.dy = statistics.fmean(_point(a, i)[1] - s * a.y for a, i in f.matches)
    return fits


def proposed_regions(fits: list[Fit], w: int, h: int) -> dict[str, Region]:
    """Each fitted group's boxes moved onto this screen, as plain fractions of it."""
    out = {}
    for f in fits:
        for name in f.group.regions:
            left, top, bw, bh = DEFAULT_REGIONS[name].to_pixels(REF_W, REF_H)
            x0, y0 = f.scale * left + f.dx, f.scale * top + f.dy
            x1, y1 = x0 + f.scale * bw, y0 + f.scale * bh
            x0, y0, x1, y1 = max(0.0, x0), max(0.0, y0), min(float(w), x1), min(float(h), y1)
            if x1 - x0 < 4 or y1 - y0 < 4:
                continue
            out[name] = Region(left=round(x0 / w, 4), top=round(y0 / h, 4),
                               width=round((x1 - x0) / w, 4), height=round((y1 - y0) / h, 4))
    return out


def overlap(a: tuple, b: tuple) -> float:
    """Intersection over union of two (left, top, width, height) boxes."""
    ix = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union else 0.0


# ---- what's read -------------------------------------------------------------------------------

@dataclass
class Check:
    label: str
    ok: bool
    value: str
    note: str = ""


def checks(kind: str, read: dict) -> list[Check]:
    """The read of one screen as pass/fail lines a person can act on."""
    v = lambda x: "" if x in (None, "", [], {}) else str(x)  # noqa: E731
    if kind == "shot":
        tiles = read.get("tiles") or {}
        return [
            Check("player", bool(read.get("player_text")) and read.get("shot_number") is not None,
                  v(read.get("player_text"))),
            Check("distance to pin", bool(read.get("to_pin")), v(read.get("to_pin"))),
            Check("score", bool(read.get("score_to_par")), v(read.get("score_to_par"))),
            Check("course", bool(read.get("course")), v(read.get("course"))),
            Check("par / length", bool(read.get("par")) and bool(read.get("hole_length")),
                  f"par {read.get('par')}, {read.get('hole_length')}" if read.get("par") else ""),
            Check("lie", bool(read.get("lie")), v(read.get("lie"))),
            Check("club / hand", bool(read.get("club")) and bool(read.get("handed")),
                  f"{read.get('club')} {read.get('handed')}" if read.get("club") else ""),
            Check("data tiles", bool(tiles), ", ".join(f"{k} {x}" for k, x in tiles.items()),
                  "" if tiles else "blank until a shot is hit on this hole"),
        ]
    if kind == "scorecard":
        return [Check("scorecard open", bool(read.get("open")), "yes" if read.get("open") else ""),
                Check("holes", bool(read.get("holes")), v(read.get("holes"))),
                Check("pars", bool(read.get("par")), v(read.get("par")))]
    out = [Check("course (Play button)", bool(read.get("course")), v(read.get("course")))]
    if kind == "setup_players":
        names = [s for s in read.get("slots") or [] if s]
        out += [Check("players tab", bool(read.get("players_tab")), "selected" if read.get("players_tab") else ""),
                Check("players", bool(names), ", ".join(names), "" if names else "add a player to a slot to check")]
    elif kind == "setup_match":
        out += [Check("match settings tab", bool(read.get("match_tab")), "selected" if read.get("match_tab") else ""),
                Check("game mode", bool(read.get("mode_read")), "read" if read.get("mode_read") else "")]
    elif kind == "setup_holes":
        out += [Check("holes tab", bool(read.get("holes_tab")), "selected" if read.get("holes_tab") else ""),
                Check("front / back 9", read.get("picked") is not None, v(read.get("picked")))]
    return out


def passed(cs: list[Check]) -> int:
    return sum(c.ok for c in cs)


@dataclass
class Report:
    kind: str | None
    size: tuple[int, int]
    current: list[Check] = field(default_factory=list)
    fixed: list[Check] = field(default_factory=list)
    moves: dict[str, Region] = field(default_factory=dict)        # regions worth writing
    proposed: dict[str, Region] = field(default_factory=dict)     # every region placed
    off: dict[str, float] = field(default_factory=dict)           # region -> overlap with where it belongs
    anchors: int = 0


def run(screen, img: Image.Image) -> Report:
    """Detect the screen, read it with the current boxes, place the boxes from GSPro's labels, and read
    again with them. `moves` holds the boxes worth changing: those well off where they belong, when
    the read with them is at least as good and fixes something."""
    from . import fixtures

    img = img.convert("RGB")
    w, h = img.size
    items = screen.ocr_items(img)
    kind = detect_kind(items)
    rep = Report(kind, (w, h))
    if kind is None:
        return rep
    rep.current = checks(kind, fixtures.read_kind(kind, screen, img))
    fits = fit_groups(kind, items, w, h)
    rep.anchors = sum(len(f.matches) for f in fits)
    rep.proposed = proposed_regions(fits, w, h)
    regions = screen.cfg.regions
    for name, r in rep.proposed.items():
        rep.off[name] = overlap(regions[name].to_pixels(w, h), r.to_pixels(w, h))
    moved = {n: r for n, r in rep.proposed.items() if rep.off[n] < 0.7}
    if not moved:
        return rep
    trial = type(screen)(screen.cfg.model_copy(update={"regions": {**regions, **moved}}))
    trial._ocr = screen._ocr
    rep.fixed = checks(kind, fixtures.read_kind(kind, trial, img))
    if passed(rep.fixed) > passed(rep.current):
        rep.moves = moved
    return rep


def annotate(img: Image.Image, screen, rep: Report) -> Image.Image:
    """The frame with the screen's boxes drawn: green where it read, red where it didn't, and yellow
    where `--fix` would move a box."""
    out = img.convert("RGB").copy()
    d = ImageDraw.Draw(out)
    w, h = out.size
    width = max(2, w // 800)
    font = ImageFont.load_default(size=max(12, w // 120))
    for name in rep.proposed:
        l, t, bw, bh = screen.cfg.regions[name].to_pixels(w, h)
        colour = (230, 40, 40) if name in rep.moves or rep.off.get(name, 1) < 0.7 else (40, 220, 40)
        d.rectangle([l, t, l + bw, t + bh], outline=colour, width=width)
        d.text((l + 4, t + 4), name, fill=colour, font=font)
    for name, r in rep.moves.items():
        l, t, bw, bh = r.to_pixels(w, h)
        d.rectangle([l, t, l + bw, t + bh], outline=(255, 220, 0), width=width)
    return out


def format_report(rep: Report) -> str:
    w, h = rep.size
    lines = [f"Screen {w}x{h}: {KIND_NAMES.get(rep.kind, 'not a GSPro screen the booth reads')}"]
    if rep.kind is None:
        lines.append("Bring up a hole (the HUD), the scorecard, or the setup screen's Players, Match Settings "
                     "or Holes tab and run it again.")
        return "\n".join(lines)
    fixed = {c.label: c for c in rep.fixed} if rep.moves else {}
    for c in rep.current:
        mark = "OK  " if c.ok else ("FIX " if fixed.get(c.label) and fixed[c.label].ok else "--  " if c.note else "MISS")
        text = c.value or "(nothing read)"
        if not c.ok and c.label in fixed and fixed[c.label].ok:
            text += f"  -> with moved boxes: {fixed[c.label].value}"
        elif c.note and not c.ok:
            text += f"  ({c.note})"
        lines.append(f"  {mark} {c.label:22} {text}")
    off = sorted(n for n, o in rep.off.items() if o < 0.7)
    if rep.moves:
        lines.append(f"Boxes off their panels: {', '.join(sorted(rep.moves))}. `screen-check --fix` writes "
                     "the moved ones to config.yaml (screen.regions).")
    elif off:
        lines.append(f"These boxes sit away from where GSPro's labels put them ({', '.join(off)}), but moving "
                     "them didn't read any better, so nothing to fix from this screen.")
    elif not rep.anchors:
        lines.append("Couldn't find GSPro's labels on this screen to place the boxes against.")
    elif all(c.ok or c.note for c in rep.current):
        lines.append("Every box is on its panel.")
    return "\n".join(lines)


# ---- config.yaml -------------------------------------------------------------------------------

def _region_line(name: str, r: Region, indent: str) -> str:
    return (f"{indent}{name}: {{left: {r.left:.4f}, top: {r.top:.4f}, width: {r.width:.4f}, "
            f"height: {r.height:.4f}}}  # screen-check")


def write_regions(path: Path, moves: dict[str, Region]) -> Path | None:
    """Put `moves` under screen.regions in the YAML config at `path`, keeping the rest of the file (and
    its comments) as it is. Regions already listed there are replaced line by line. Returns the backup
    of the old file (None if there was no file)."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    backup = None
    if path.exists():
        backup = path.with_name(path.name + ".bak")
        shutil.copyfile(path, backup)
    lines = text.splitlines()
    top = re.compile(r"^\S")
    s = next((i for i, ln in enumerate(lines) if re.match(r"^screen:\s*(#.*)?$", ln)), None)
    if s is None:
        lines += ["", "screen:", "  regions:"] + [_region_line(n, r, "    ") for n, r in moves.items()]
    else:
        end = next((i for i in range(s + 1, len(lines)) if top.match(lines[i])), len(lines))
        rg = next((i for i in range(s + 1, end) if re.match(r"^  regions:\s*(#.*)?$", lines[i])), None)
        if rg is None:
            at = end
            while at > s + 1 and not lines[at - 1].strip():
                at -= 1
            lines[at:at] = ["  regions:"] + [_region_line(n, r, "    ") for n, r in moves.items()]
        else:
            rend = rg + 1
            while rend < end and (not lines[rend].strip() or lines[rend].startswith("   ")):
                rend += 1
            block = lines[rg + 1:rend]
            tail = []
            while block and not block[-1].strip():
                tail.append(block.pop())
            for n, r in moves.items():
                idx = next((i for i, ln in enumerate(block) if re.match(rf"^\s+{re.escape(n)}:", ln)), None)
                if idx is None:
                    block.append(_region_line(n, r, "    "))
                else:
                    block[idx] = _region_line(n, r, re.match(r"^\s+", block[idx]).group(0))
            lines[rg + 1:rend] = block + tail
    new = "\n".join(lines) + "\n"
    loaded = (yaml.safe_load(new) or {}).get("screen", {}).get("regions", {})
    for n, r in moves.items():   # never leave a config the booth would read differently than intended
        if Region(**loaded.get(n, {})) != r:
            raise RuntimeError(f"Couldn't write {n} into {path} cleanly; nothing changed.")
    path.write_text(new, encoding="utf-8")
    return backup
