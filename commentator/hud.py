"""Parses OCR of the GSPro HUD (release 3.1.x layout) into fields.

Layout, as seen on GSPro 3.1.6.20:
- player box (top left): name, "Shot N", distance to pin, elevation, and a red badge
  with the shot ordinal and score to par, e.g. "4th (-1)".
- hole box (top right): hole number square, course, "PAR 4", hole length.
- data tiles (right edge): value above label, one or two columns, chosen by the player.
- minimap header (lower right): the ball's current lie ("Fairway", "Rough", "Green"...).
"""

import re
from dataclasses import dataclass, field

# Documented on the GSPro HUD: Tee, Fairway, Rough, Deep Rough, Sand, Green. Pine straw and
# concrete exist too (exact HUD spelling unconfirmed); the rest are surfaces courses may have.
# Listing them gives each its own penalty instead of being read as nothing. Multi-word names
# come before their parts.
LIES = ("deep rough", "semi rough", "pine straw", "cart path", "waste area", "fairway", "rough", "green",
        "fringe", "bunker", "sand", "tee", "water", "hazard", "out of bounds", "fescue", "native", "waste",
        "concrete", "trees")

DISTANCE = re.compile(r"(\d+(?:\.\d+)?)\s*(m|yds?|ft)\b(?:\s*(\d+))?", re.IGNORECASE)
SCORE = re.compile(r"\((E|[+-]\d+)\)")
SHOT = re.compile(r"\bshot\s*(\d+)", re.IGNORECASE)
PAR = re.compile(r"\bpar\s*(\d)", re.IGNORECASE)
NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass
class OcrItem:
    text: str
    x: float  # box centre
    y: float
    h: float  # box height
    left: float = 0.0  # box left edge


@dataclass
class Hud:
    player_text: str | None = None
    shot_number: int | None = None
    to_pin: str | None = None
    score_to_par: str | None = None
    course: str | None = None
    par: int | None = None
    hole_length: str | None = None
    hole_number: int | None = None
    lie: str | None = None
    tiles: dict[str, str] = field(default_factory=dict)
    # Pin height relative to the ball, + uphill. None when the arrow's direction couldn't be read.
    elevation_yards: float | None = None
    # Minimap footer: slope under the ball. + uphill / - downhill, and the side it falls toward.
    slope_updown: float | None = None
    slope_side: str | None = None
    slope_side_degrees: float | None = None
    # Bottom-left boxes: the club GSPro has selected, and RH/LH.
    club: str | None = None
    handed: str | None = None
    # The rows under the player box, one per other player in the match.
    others: list["OtherPlayer"] = field(default_factory=list)
    scorecard_open: bool = False
    scorecard: "Scorecard | None" = None
    # GSPro's centre-screen result banner on hole completion ("Birdie", "Double Bogey"...). Present
    # only when the hole just ended (ball holed or conceded in the gimme), so a reliable made signal.
    hole_result: str | None = None
    # Pixel signature of the top row's name cell (who's up), from the `player_name` region, no OCR.
    # None when not read; NAME_DARK when the cell is blank (the screen is fading between players).
    name_sig: "tuple[bool, ...] | None" = None
    # Where each tile's value sat in the `tiles` crop (left, top, right, bottom pixels), from the OCR.
    # Following a ball to rest compares just Total Length's number: the rest of the region is the
    # live 3D scene beside the tile panel, which never holds still.
    tile_boxes: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)
    tile_crop_size: tuple[int, int] | None = None   # the `tiles` crop those boxes are in


NAME_DARK: tuple = ()


def name_sig_changed(a: "tuple | None", b: "tuple | None", threshold: float = 0.015) -> bool:
    """True when two name-cell signatures show a different player (or one is dark: the screen is
    switching). Measured on real HUD frames: the same name differs by <=0.1% of pixels even across a
    brightness change, different names by >=3.6%. False when either wasn't read."""
    if a is None or b is None:
        return False
    if a == NAME_DARK or b == NAME_DARK:
        return a != b
    return sum(x != y for x, y in zip(a, b)) / max(len(a), 1) > threshold


@dataclass
class OtherPlayer:
    name: str
    to_pin: str | None = None
    score_to_par: str | None = None


@dataclass
class Scorecard:
    """GSPro's scorecard: hole numbers in column order, par and stroke index, strokes per row."""

    holes: list[int]
    par: dict[int, int] = field(default_factory=dict)
    index: dict[int, int] = field(default_factory=dict)
    # Row name as GSPro shows it (often upper case) -> hole -> strokes. Unplayed holes are absent.
    scores: dict[str, dict[int, int]] = field(default_factory=dict)


def _fmt_distance(m: re.Match) -> str:
    value, unit, inches = m.group(1), m.group(2).lower(), m.group(3)
    unit = {"yd": "yards", "yds": "yards", "m": "metres", "ft": "feet"}[unit]
    return f"{value} {unit}" + (f" {inches} inches" if inches and unit == "feet" else "")


def parse_player_box(items: list[str]) -> dict:
    text = " ".join(items)
    out: dict = {"player_text": text}
    if m := SHOT.search(text):
        out["shot_number"] = int(m.group(1))
    if m := SCORE.search(text):
        out["score_to_par"] = m.group(1)
    else:
        # On the tee GSPro shows the score bare ("+4", "E"), not in brackets after a stroke ordinal.
        head = text[:m.start()] if (m := SHOT.search(text)) else text
        if (words := head.split()) and SCORE_BADGE.match(words[-1]):
            out["score_to_par"] = words[-1]
        elif any(t.strip() == "E" for t in items):
            out["score_to_par"] = "E"
    # The first distance after "Shot N" is to the pin; the second is elevation.
    after_shot = text[m.end():] if (m := SHOT.search(text)) else text
    if d := DISTANCE.search(after_shot):
        out["to_pin"] = _fmt_distance(d)
    return out


ELEVATION = re.compile(r"([▲▼]?)\s*(\d+(?:\.\d+)?)\s*(yds?|ft|in|'|m)?\s*(\d+)?", re.IGNORECASE)


def parse_elevation(text: str, metric: bool, arrow: str | None) -> float | None:
    """`text` is the OCR of the elevation readout; `arrow` is "up"/"down" from the pixels.

    OCR sometimes keeps the arrow glyph, which beats the pixel guess. Metric layouts show a bare
    number: whole numbers look like centimetres ("50" beside a "50cm" marker), decimals like metres.
    """
    m = ELEVATION.search(text)
    if not m:
        return None
    glyph, value, unit, inches = m.group(1), float(m.group(2)), (m.group(3) or "").lower(), m.group(4)
    direction = {"▲": "up", "▼": "down"}.get(glyph) or arrow
    if direction is None:
        return None
    if unit.startswith("y"):
        yards = value
    elif unit in ("ft", "'"):
        yards = (value + (int(inches) / 12 if inches else 0)) / 3
    elif unit == "in":
        yards = value / 36
    elif unit == "m" or (metric and "." in m.group(2)):
        yards = value * 1.0936
    elif metric:
        yards = value / 100 * 1.0936
    else:
        return None
    return yards if direction == "up" else -yards


def arrow_direction(region) -> str | None:
    """Up or down from the elevation arrow's pixels (a green triangle a few pixels tall).

    A triangle's mass sits toward its base. On compressed video the call is often unclear
    (on recorded 720p frames it was right only when well clear of the middle), so anything
    near the middle returns None rather than a guess.
    """
    import numpy as np

    a = np.asarray(region, dtype=float)
    if a.size == 0:
        return None
    w = np.clip((a[..., 1] - np.maximum(a[..., 0], a[..., 2])) / 255.0, 0, None)
    if w.sum() < 1:
        return None
    rows = w.sum(axis=1)
    nz = np.nonzero(rows > rows.max() * 0.15)[0]
    top, bot = nz.min(), nz.max()
    cy = (w * np.arange(a.shape[0])[:, None]).sum() / w.sum()
    rel = (cy - top) / max(bot - top, 1)
    return "up" if rel > 0.6 else "down" if rel < 0.4 else None


SLOPE_UPDOWN = re.compile(r"(\d+(?:\.\d+)?)\s*[°º]?\s*(up|down)", re.IGNORECASE)
SLOPE_SIDE = re.compile(r"(\d+(?:\.\d+)?)\s*[°º]?\s*(left|right)", re.IGNORECASE)


def parse_slope(items: list[str]) -> dict:
    text = " ".join(items)
    out: dict = {}
    if m := SLOPE_UPDOWN.search(text):
        out["slope_updown"] = float(m.group(1)) * (1 if m.group(2).lower() == "up" else -1)
    if m := SLOPE_SIDE.search(text):
        out["slope_side"] = m.group(2).lower()
        out["slope_side_degrees"] = float(m.group(1))
    return out


def parse_hole_box(items: list[str]) -> dict:
    out: dict = {}
    text = " ".join(items)
    if m := PAR.search(text):
        out["par"] = int(m.group(1))
    if d := DISTANCE.search(text):
        out["hole_length"] = _fmt_distance(d)
    for t in items:
        t = t.strip()
        if t.isdigit() and 1 <= int(t) <= 18 and not out.get("hole_number"):
            out["hole_number"] = int(t)
        elif re.search(r"[A-Za-z]{3}", t) and not PAR.search(t) and not DISTANCE.search(t):
            out.setdefault("course", t)
    return out


CLUB_WORDS = [
    (r"\bdriver\b", "DR"), (r"\bputter\b", "PT"), (r"\blob\s*wedge\b", "LW"), (r"\bsand\s*wedge\b", "SW"),
    (r"\bgap\s*wedge\b", "GW"), (r"\bapproach\s*wedge\b", "AW"), (r"\bpitching\s*wedge\b", "PW"),
    # GSPro's own short names: "P Wedge", "G Wedge", "S Wedge", "L Wedge".
    (r"\b([pgsla])\s*-?\s*wedge\b", "{}W"),
    (r"\b(\d)\s*wood\b", "{}W"), (r"\b(\d)\s*hybrid\b", "{}H"), (r"\b(\d)\s*iron\b", "{}I"),
    (r"\b(\d)\s*([whi])\b", "{}{}"), (r"\b(lw|sw|gw|aw|pw)\b", "{}"),
]


def parse_club(items: list[str]) -> dict:
    """GSPro's club box ("Driver", "7 Iron", ...) and handedness box ("RH"/"LH")."""
    text = " ".join(items).lower()
    out: dict = {}
    for pattern, code in CLUB_WORDS:
        if m := re.search(pattern, text):
            out["club"] = code.format(*(g.upper() for g in m.groups())) if m.groups() else code
            break
    if m := re.search(r"\b(rh|lh)\b", text):
        out["handed"] = m.group(1).upper()
    return out


def parse_lie(items: list[str]) -> str | None:
    text = " ".join(items).lower()
    for lie in LIES:
        if re.search(rf"\b{lie}\b", text):
            return lie
    return None


# GSPro's result-banner wording (abbreviated: "DBL BOGEY", not "Double Bogey"), longest/worst first
# so "DBL BOGEY" matches before the plain "BOGEY" it contains. Label is for commentary, the int is
# strokes relative to par (None for an ace, where it depends on the hole's par).
SCORE_BANNERS: list[tuple[tuple[str, ...], str, int | None]] = [
    (("HOLE IN ONE", "ACE"), "Ace", None),
    (("ALBATROSS", "DOUBLE EAGLE", "DBL EAGLE"), "Albatross", -3),
    (("EAGLE",), "Eagle", -2),
    (("BIRDIE",), "Birdie", -1),
    (("QUAD BOGEY", "QUADRUPLE"), "Quadruple Bogey", 4),
    (("TRIPLE BOGEY", "TRP BOGEY", "TRIPLE"), "Triple Bogey", 3),
    (("DOUBLE BOGEY", "DBL BOGEY", "DBLBOGEY"), "Double Bogey", 2),
    (("BOGEY",), "Bogey", 1),
    (("PAR",), "Par", 0),
]


def parse_score_banner(items: list[str]) -> tuple[str, int | None] | None:
    """The centre-screen hole-result banner: (label, strokes-relative-to-par), or None if no banner
    is up. OCR of a short all-caps word, so match on the joined text rather than word boundaries.

    GSPro's "For Birdie" banner shows "FOR BIRDIE"/"FOR BOGEY" before the shot and flashes the bare
    result ("BIRDIE", "DBL BOGEY") only once the hole is done, so a "FOR" means it hasn't finished."""
    text = " ".join(items).upper()
    if not text.strip() or re.search(r"\bFOR\b", text):
        return None
    for needles, label, to_par in SCORE_BANNERS:
        if any(n in text for n in needles):
            return label, to_par
    return None


PLACEHOLDER = re.compile(r"^\s*-+\s*[.,]?\s*-+\s*$")


def parse_tiles(items: list[OcrItem]) -> dict[str, str]:
    """Pairs each label with the value directly above it (tiles are value-over-label).

    Total Length shows "--.--" until the ball is moving; it counts as a value here, or the label
    gets paired with the number from the tile above it.
    """
    return {label: v.text.strip() for label, v in tile_value_items(items).items()}


def tile_value_items(items: list[OcrItem]) -> dict[str, OcrItem]:
    """Each tile label (lowercase) with the OCR item of its value; see parse_tiles."""
    labels = [i for i in items if re.search(r"[A-Za-z]{3}", i.text)]
    values = [i for i in items if (NUMBER.search(i.text) or PLACEHOLDER.match(i.text))
              and not re.search(r"[A-Za-z]{3}", i.text)]
    out: dict[str, OcrItem] = {}
    for label in labels:
        above = [v for v in values if 0 < label.y - v.y < 3 * max(label.h, v.h) and abs(v.x - label.x) < 3 * label.h]
        if above:
            out[label.text.strip().lower()] = min(above, key=lambda v: (label.y - v.y) + abs(v.x - label.x))
    return out


def _rows(items: list[OcrItem]) -> list[list[OcrItem]]:
    """Groups OCR boxes into text rows, top to bottom, each sorted left to right."""
    rows: list[list[OcrItem]] = []
    for item in sorted(items, key=lambda i: i.y):
        if rows and abs(item.y - rows[-1][0].y) < 0.6 * max(item.h, rows[-1][0].h, 1):
            rows[-1].append(item)
        else:
            rows.append([item])
    return [sorted(r, key=lambda i: i.x) for r in rows]


SCORE_BADGE = re.compile(r"^(E|[+-]\d+)$")


def parse_others(items: list[OcrItem]) -> list[OtherPlayer]:
    """The other players listed under the active one: name, distance to the pin, score badge.

    A row only counts when it has a distance with units, which keeps data tiles that share
    this side of the screen (bare numbers over labels) out of the list.
    """
    out = []
    for row in _rows(items):
        texts = [i.text.strip() for i in row]
        distance = DISTANCE.search(" ".join(texts))
        name = next((t for t in texts if re.search(r"[A-Za-z]{2}", t) and not DISTANCE.search(t)), None)
        if not (distance and name):
            continue
        score = next((t for t in texts if SCORE_BADGE.match(t)), None)
        out.append(OtherPlayer(name=name, to_pin=_fmt_distance(distance), score_to_par=score))
    return out


SCORECARD_LABELS = {"hole", "par", "index", "hcp", "handicap", "distance", "yards", "out", "in", "total", "scorecard",
                    "statistics"}


def parse_scorecard(items: list[OcrItem]) -> Scorecard | None:
    """Reads the scorecard table by column: each value belongs to the hole whose number sits above it.

    Checked on 3.1.x scorecards at 720p and 1080p: a HOLE row of 1-18 with OUT/IN between,
    then PAR, INDEX and DISTANCE rows, then one row per player with "--" for unplayed holes.
    """
    rows = _rows(items)
    header = next((r for r in rows if r and r[0].text.strip().upper() == "HOLE"), None)
    if header is None:
        return None
    columns = [(int(i.text), i.x) for i in header[1:] if i.text.strip().isdigit() and 1 <= int(i.text) <= 18]
    if len(columns) < 3:
        return None
    spacing = sorted(b[1] - a[1] for a, b in zip(columns, columns[1:]))[len(columns) // 2 - 1]
    first_x = min(x for _, x in columns)
    card = Scorecard(holes=[h for h, _ in columns])

    def values(row: list[OcrItem]) -> dict[int, int]:
        out = {}
        for item in row:
            if not item.text.strip().isdigit():
                continue
            hole, x = min(columns, key=lambda c: abs(c[1] - item.x))
            if abs(x - item.x) < spacing * 0.45:
                out[hole] = int(item.text)
        return out

    for row in rows[rows.index(header) + 1:]:
        label = row[0]
        name = label.text.strip()
        if label.x > first_x - spacing * 0.5 or ":" in name or not re.search(r"[A-Za-z]", name):
            continue
        key = name.lower()
        if key == "par":
            card.par = {h: v for h, v in values(row).items() if 3 <= v <= 6}
        elif key in ("index", "hcp", "handicap"):
            card.index = values(row)
        elif key not in SCORECARD_LABELS:
            card.scores[name] = {h: v for h, v in values(row).items() if 1 <= v <= 15}
    return card


def hole_key(hud: Hud) -> str | None:
    """Identity of the current hole. Par + length read reliably; the big hole number often doesn't."""
    if hud.par and hud.hole_length:
        return f"par {hud.par} {hud.hole_length}"
    return None


def describe(hud: Hud, units_hint: str = "") -> list[str]:
    """Plain-language facts from the HUD for the prompt."""
    facts = []
    if hud.lie:
        facts.append(f"The ball is now in the {hud.lie}." if hud.lie not in ("green", "tee") else f"The ball is now on the {hud.lie}.")
    tiles = hud.tiles
    # Worded as full sentences: a bare "Total distance: 18.6" got read as distance left to the hole.
    for key, template in (
        ("carry (game)", "It carried {} in the air."),
        ("total length", "It travelled {} in total, including roll."),
        ("offline (raw)", "It finished {} off the target line."),
    ):
        if key in tiles and (m := NUMBER.search(tiles[key])):
            facts.append(template.format(f"{m.group(0).lstrip('-')}{units_hint}"))
    if hud.score_to_par:
        score = "even par" if hud.score_to_par == "E" else f"{hud.score_to_par} to par"
        facts.append(f"Player's score on the round so far: {score}.")
    return facts
