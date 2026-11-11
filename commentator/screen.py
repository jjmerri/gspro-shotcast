import asyncio
import io
import logging
import re
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageDraw

from .config import HUD_ASPECT, NEW_UI_REGIONS, Region, ScreenConfig
from .hud import (
    DISTANCE, NAME_DARK, Hud, OcrItem, arrow_direction, is_lie_cell, parse_club, parse_elevation, parse_hole_box, parse_lie,
    parse_pick_cards,
    parse_others, parse_player_box, parse_score_banner, parse_scorecard, parse_slope, parse_tiles, tile_value_items,
)

log = logging.getLogger(__name__)

_outage_lock = threading.Lock()
_outage: dict[str, Any] = {"since": None, "failures": 0}


def _capture_error(exc: BaseException | None) -> bool:
    """Windows refusing a screen grab (mss's ScreenShotError: the PC locked, the display asleep, a
    UAC prompt up), not a bad read."""
    return exc is not None and type(exc).__name__ == "ScreenShotError"


def log_failure(msg: str, *args: Any, logger: logging.Logger | None = None, level: int = logging.ERROR) -> None:
    """In an except block: log msg at `level`, with the traceback, for a read that failed. A refused screen grab logs one
    warning instead, and every read failing on it after that only at DEBUG, until a grab works again
    (_capture_ok): the booth polls several times a second, so a locked PC used to log hundreds of
    tracebacks a minute."""
    import sys

    logger = logger or log
    exc = sys.exc_info()[1]
    if not _capture_error(exc):
        logger.log(level, msg, *args, exc_info=True)
        return
    with _outage_lock:
        first = _outage["since"] is None
        if first:
            _outage["since"] = time.monotonic()
        _outage["failures"] += 1
    if first:
        logger.warning("Screen capture is failing (%s): the PC locked or the display asleep? "
                       "Not logged again until it works", exc)
    else:
        logger.debug(msg, *args, exc_info=True)


def _capture_ok() -> None:
    """A screen grab worked: ends an outage log_failure is holding quiet, saying how long it was."""
    if _outage["since"] is None:
        return
    with _outage_lock:
        since, failures = _outage["since"], _outage["failures"]
        _outage["since"], _outage["failures"] = None, 0
    if since is not None:
        log.info("Screen capture works again after %.0f s (%d reads failed)", time.monotonic() - since, failures)


@dataclass(frozen=True)
class Tone:
    """A region's grey levels (0-255): mean, median, and the share of near-white (>= 200) pixels;
    and the share of strongly blue pixels (GSPro 3.59 marks the selected setup tab with a blue
    underline) and of saturated ones (its name pills are a dark team colour, not grey), with their
    median RGB (`tint`, None with too few of them): the same team colour reads the same tint."""

    mean: float
    median: float
    light: float
    blue: float = 0.0
    color: float = 0.0
    tint: tuple[int, int, int] | None = None


def tone_of(img: Image.Image) -> Tone:
    """The Tone of a whole image (a region's crop)."""
    import numpy as np

    hist = img.convert("L").histogram()
    n = sum(hist) or 1
    mean = sum(v * c for v, c in enumerate(hist)) / n
    acc, median = 0, 0
    for v, c in enumerate(hist):
        acc += c
        if acc * 2 >= n:
            median = v
            break
    a = np.asarray(img.convert("RGB"), dtype=int)
    tint = None
    if a.size:
        r, g, b = a[..., 0], a[..., 1], a[..., 2]
        blue = float(((b > 150) & (b - r > 80) & (b - g > 30)).mean())
        saturated = a.max(axis=2) - a.min(axis=2) > 60
        color = float(saturated.mean())
        if color >= 0.1:
            tint = tuple(int(v) for v in np.median(a[saturated], axis=0))
    else:
        blue = color = 0.0
    return Tone(mean=mean, median=median, light=sum(hist[200:]) / n, blue=blue, color=color, tint=tint)


def _longest_run(ok) -> "tuple[int, int] | None":
    best, start = None, None
    for i, v in enumerate([*ok, False]):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if best is None or i - start > best[1] - best[0]:
                best = (start, i)
            start = None
    return best


def _name_bar(img: Image.Image) -> Image.Image:
    """The name cell with everything but its name bar (the rows and columns of the bar's fill colour
    and the white text over it) painted over in the fill. GSPro 4's cell also takes in the course
    above the bar (7 of 56 rows at 3450x2160), and sky, sand or a dark tree line passing there
    mid-flight read as a new player. The cell keeps its size so a bar edge found a row off (dark
    scenery next to a dark bar) doesn't shift the text. The cell as is when no bar is found (a fade,
    a menu)."""
    import numpy as np

    a = np.asarray(img.convert("RGB"), dtype=int)
    if a.ndim != 3 or a.shape[0] < 3 or a.shape[1] < 3:
        return img
    keys = (a[..., 0] // 8) * 1024 + (a[..., 1] // 8) * 32 + a[..., 2] // 8
    values, counts = np.unique(keys, return_counts=True)
    fill = np.median(a[keys == values[counts.argmax()]], axis=0)
    span = 255 - fill
    lit = span > 24
    if not lit.any():
        return img
    # Text is white anti-aliased into the fill: every pixel of the bar lies between the two.
    t = ((a[..., lit] - fill[lit]) / span[lit]).mean(axis=2)
    resid = np.abs(a - (fill + t[..., None] * span)).max(axis=2)
    on_bar = (resid < 40) & (t > -0.15) & (t < 1.15)
    is_fill = np.abs(a - fill).max(axis=2) < 24
    rows = _longest_run((on_bar.mean(axis=1) >= 0.9) & (is_fill.mean(axis=1) >= 0.1))
    if rows is None or rows[1] - rows[0] < 3:
        return img
    cols = _longest_run(on_bar[rows[0]:rows[1]].mean(axis=0) >= 0.9)
    if cols is None or cols[1] - cols[0] < 3:
        return img
    keep = np.zeros(on_bar.shape, dtype=bool)
    keep[rows[0]:rows[1], cols[0]:cols[1]] = True
    keep &= on_bar
    if keep.all():
        return img
    out = np.where(keep[..., None], a, np.round(fill).astype(int))
    return Image.fromarray(out.astype(np.uint8), "RGB")


def name_signature(img: Image.Image) -> tuple:
    """The name cell's bar (see _name_bar) as a 96x12 text mask (thresholded midway between its darkest and
    brightest pixel, so a brightness shift doesn't change it), or NAME_DARK when there's no text
    contrast (the screen is fading or black between players)."""
    px = list(_name_bar(img).convert("L").resize((96, 12), Image.BILINEAR).tobytes())
    lo, hi = min(px), max(px)
    if hi - lo < 60:
        return NAME_DARK
    mid = (lo + hi) / 2
    return tuple(p > mid for p in px)

def _tile_boxes(items: list[OcrItem], scale: float, width: int, height: int) -> dict[str, tuple[int, int, int, int]]:
    """Each tile value's box in the unscaled crop, padded so a number that grows a digit ("99.8" to
    "100.2") still falls inside it. `width`/`height` are the OCR'd (scaled) image's size."""
    out = {}
    for label, v in tile_value_items(items).items():
        pad_x, pad_y = 0.35 * v.h, 0.3 * v.h
        right = 2 * v.x - v.left
        box = (max(0.0, v.left - pad_x), max(0.0, v.y - v.h / 2 - pad_y),
               min(float(width), right + pad_x), min(float(height), v.y + v.h / 2 + pad_y))
        out[label] = tuple(round(c / scale) for c in box)
    return out


def _group_lines(items: list[OcrItem]) -> list[str]:
    """OCR items grouped into text lines by vertical position: items on roughly the same row join
    left-to-right into one line, and a clear vertical gap starts a new line. Lets a caller tell one
    name (first + last on a single row) from several stacked names (a dropdown)."""
    its = sorted((i for i in items if i.text.strip()), key=lambda i: i.y)
    if not its:
        return []
    lines: list[list[OcrItem]] = [[its[0]]]
    for it in its[1:]:
        ref = lines[-1][0]   # the line's FIRST item, so a row can't creep by chaining off each new one
        if it.y - ref.y <= max(it.h, ref.h) * 0.7:   # same row (within ~0.7 of the text height)
            lines[-1].append(it)
        else:
            lines.append([it])
    return [" ".join(i.text for i in sorted(ln, key=lambda i: i.left)).strip() for ln in lines]


# GSPro's lefty layout (a per-player setting, not every lefty's) swaps the bottom corners: the minimap
# with its lie header and slope bar goes bottom-left, the club and hand boxes bottom-right.
MIRRORED = ("lie", "club", "minimap", "slope")
# Just inside the slope bar, past where the club boxes end: a dark grey panel with white text on the
# minimap's side, the course on the other. Sampled on both sides to say which side the minimap is on.
_MIRROR_PROBE = Region(left=0.10, top=0.955, width=0.04, height=0.03, anchor="bottom", aspect=HUD_ASPECT)


def mirrored_region(region: Region) -> Region:
    """The same box on the other side of the screen (fractional regions only)."""
    if max(region.left, region.top, region.width, region.height) > 1:
        return region
    return region.model_copy(update={"left": round(1 - region.left - region.width, 4)})


def _corner_look(img: Image.Image) -> str | None:
    """What a mirror probe shows: "text" (the slope bar with the slope's white text in it), "flat" (the
    bar empty, as on the green, or any flat dark panel), "course" (grass, trees, sand: far less blue
    than green), else None. The bar is a dark panel whose blue is at least near its green: 3.1's teal
    (2,23,22) and 3.59's semi-transparent navy (41,47,56 with text, 12,20,32 empty), which an "even
    blue and green" test missed. A scorecard's or menu's flat panel looks like an empty bar, so a side
    is only told by the bar on one side and the course on the other (detect_mirror)."""
    from PIL import ImageStat

    st = ImageStat.Stat(img.convert("RGB"))
    (r, g, b), spread = st.mean, sum(st.stddev) / 3
    if r < 70 and g - r >= 4 and b - r >= 8 and b >= g - 6 and (spread > 35 or spread < 6):
        return "text" if spread > 35 else "flat"
    return "course" if b < g - 8 else None


OTHER_SIDE = "@other_side"   # grab_regions' key for the other side's boxes (Screen._other_side)

FOLLOW_SECONDS = 3.0   # how often to look again for the monitor GSPro's window is on


def monitor_of(rect: tuple[int, int, int, int], monitors: list[dict]) -> int | None:
    """The index into mss's monitors (1 up; 0 is the whole desktop) of the one holding the centre of
    `rect` (left, top, right, bottom), or None when it's on none of them."""
    cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
    return next((i for i, m in enumerate(monitors) if i and m["left"] <= cx < m["left"] + m["width"]
                 and m["top"] <= cy < m["top"] + m["height"]), None)


def _box_lie_cell(items: list[OcrItem]) -> OcrItem | None:
    """The player box's lie (3.59/4.0): a cell of lie words only ("Deep" for deep rough) right of the
    distance, so a player called "Alex Green" doesn't count. None on 3.1, whose box has no lie."""
    dist = next((i for i in items if DISTANCE.search(i.text)), None)
    if dist is None:
        return None
    return min((i for i in items if i.left > dist.left and is_lie_cell(i.text)), key=lambda i: i.left, default=None)


def _strip_neighbour(items: list[OcrItem], row: OcrItem, start: int, end: int) -> int | None:
    """What 3.59's shot box (columns start..end) should hold, from the plain strip digits beside it
    on the distance row: one less than the digit right of it, else one more than the digit left."""
    after, before = None, None
    for i in items:
        if abs(i.y - row.y) > row.h or not re.fullmatch(r"[\d ]+", i.text.strip() or "x"):
            continue
        digits = i.text.split()
        if i.left >= end - 2 and (after is None or i.left < after[0]):
            after = (i.left, _strip_number(digits[0], first=True))
        elif 2 * i.x - i.left <= start + 2 and (before is None or i.left > before[0]):
            before = (i.left, _strip_number(digits[-1], first=False))
    if after and after[1] is not None:
        guess = after[1] - 1
    elif before and before[1] is not None:
        guess = before[1] + 1
    else:
        guess = None
    return guess if guess and 0 < guess < 20 else None


def _strip_left_of_box(items: list[OcrItem], row: OcrItem, start: int) -> bool:
    """Whether plain strip digits sit left of the shot box (past the lie word, so not the elevation):
    then it isn't the first stroke. On 3.59 the PC's OCR read Derek's third, "1 2 [3] 4", as
    "１２ 3", and the box alone as "1"."""
    on_row = [i for i in items if abs(i.y - row.y) <= row.h and i is not row]
    words = [r for i in on_row if re.search(r"[A-Za-z]", i.text) and (r := 2 * i.x - i.left) <= start + 2]
    floor = max(words, default=2 * row.x - row.left)
    return any(re.fullmatch(r"[\d ]+", i.text.strip() or "x") and i.left > floor and 2 * i.x - i.left <= start + 2
               for i in on_row)


def _strip_number(token: str, first: bool) -> int | None:
    """The strip number at the box's end of an OCR token: "7", "10", or digits run together
    ("234", its first or last). None when it could be either ("12": twelve, or 1 and 2)."""
    run = len(token) > 1 and all(int(b) == int(a) + 1 for a, b in zip(token, token[1:]))
    if run:
        return None if int(token) < 20 else int(token[0] if first else token[-1])
    return int(token) if int(token) < 20 else None


# How the text detector sizes each region's crop with screen.ocr_fast (benchmarked on 4K, 1080p,
# 1280x800 and 1280x720 frames against the default). RapidOCR's default enlarges every crop until its
# short side is 736 px, which is most of an OCR's time. ("min", n): enlarge to a short side of at least
# n px. ("ref", h): scale the crop to the size it would be on a screen h px tall (shrinking it on a
# bigger screen). ("shrink", h): the same, but only where that shrinks it (the default otherwise). Regions not listed keep the default: the scorecard (reads worse smaller), the score banner (not
# benchmarked) and the setup player slots (names at 4K only come back with the big upscale). The other
# players' board drops scores (and reads a result banner's word as a player) at ref 1080; shrunk to
# 1620 it reads as well as the default on 4K in half the time, but enlarged to it (1080p, 720p) misreads
# names the default gets.
FAST_DETECT: dict[str, tuple[str, int]] = {
    "player_box": ("min", 320), "hole_box": ("min", 320),
    **{n: ("ref", 1080) for n in ("lie", "club", "slope", "tiles", "tiles_left", "scorecard_title",
                                  "setup_course", "load_course", "course_select", "setup_game_mode",
                                  "pick_panel")},
    "others": ("shrink", 1620),
}


class Screen:
    def __init__(self, cfg: ScreenConfig) -> None:
        self.cfg = cfg
        self._ocr: Any = None
        # One OCR at a time: the detector's input size is set on the shared engine before each call.
        self._ocr_lock = threading.Lock()
        self._det_default: tuple[str, int] | None = None
        self._ocr_how = threading.local()   # the detector size / rec-only for this thread's next ocr_items
        self._frame_h = 0   # the height of the screen the last region was cut from (FAST_DETECT's "ref")
        self.last_read_seconds = 0.0
        self.last_capture_seconds = 0.0
        self.mirrored = False   # the current player's HUD is in GSPro's lefty layout (see MIRRORED)
        self._mirror_pending: bool | None = None   # one empty-bar read for the other side (detect_mirror)
        # Which GSPro menus are on screen: "classic" (3.1) or "new" (3.59's redesign, NEW_UI_REGIONS).
        # The engine sets it from the GSPro version in the log.
        self.ui = "classic"
        self.monitor = cfg.monitor   # the monitor read last (follow_window moves it to GSPro's)
        self._monitor_at = -1e9
        self._monitor_lock = threading.Lock()
        self._masked_logged: dict[str, float] = {}   # who blanked a grab, and when that was last logged

    def _covered(self, box: tuple[int, int, int, int]) -> list[tuple[int, int, int, int]]:
        """The parts of a grab of `box` (left, top, right, bottom on the desktop) GSPro isn't showing,
        in the grab's own pixels (see ScreenConfig.mask_other_windows)."""
        if not self.cfg.mask_other_windows:
            return []
        left, top, right, bottom = box
        try:
            from . import winshot

            cover = winshot.coverage(self.cfg.window_title)
        except Exception:
            # Unsure what's in front: read nothing rather than risk someone's browser.
            log.debug("Couldn't tell which windows cover GSPro", exc_info=True)
            self._log_masked("couldn't tell which windows are in front of GSPro")
            return [(0, 0, right - left, bottom - top)]
        if cover is None:
            return []
        gspro, front = cover
        if gspro is None:
            self._log_masked("GSPro's window isn't up", 600)
            return [(0, 0, right - left, bottom - top)]
        gl, gt, gr, gb = gspro
        out = [(left, top, right, gt), (left, gb, right, bottom), (left, max(top, gt), gl, min(bottom, gb)),
               (gr, max(top, gt), right, min(bottom, gb))]
        skip = {n.casefold() for n in self.cfg.show_through}
        for r, pid in front:
            name = winshot.exe_name(pid)
            if name.casefold() not in skip:
                hit = (max(left, r[0]), max(top, r[1]), min(right, r[2]), min(bottom, r[3]))
                if hit[0] < hit[2] and hit[1] < hit[3]:
                    self._log_masked(f"{name} is in front of GSPro")
                    out.append(hit)
        out = [(max(l, left), max(t, top), min(r, right), min(b, bottom)) for l, t, r, b in out]
        return [(l - left, t - top, r - left, b - top) for l, t, r, b in out if l < r and t < b]

    def _log_masked(self, why: str, every: float = 60) -> None:
        now = time.monotonic()
        if now - self._masked_logged.get(why, -1e9) > every:
            self._masked_logged[why] = now
            log.info("Screen read blanked where GSPro isn't showing: %s", why)

    def _sct_grab(self, sct, box: dict) -> Image.Image:
        """One mss grab of `box`, with every part GSPro isn't showing blacked out. The windows are
        looked at just before the grab and just after: when they moved meanwhile (a window dragged or
        closed), where one was at the moment of the grab isn't known, so it's all blacked out."""
        desk = (box["left"], box["top"], box["left"] + box["width"], box["top"] + box["height"])
        before = self._covered(desk)
        raw = sct.grab(box)
        _capture_ok()
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        after = self._covered(desk)
        if holes := before if before == after else [(0, 0, img.width, img.height)]:
            draw = ImageDraw.Draw(img)
            for l, t, r, b in holes:
                draw.rectangle((l, t, r - 1, b - 1), fill=(0, 0, 0))
        return img

    def monitor_index(self, monitors: list[dict]) -> int:
        """The mss monitor to read: the one GSPro's window is on (looked up every few seconds), else
        screen.monitor. Called from worker threads and the overlay's."""
        if not self.cfg.follow_window:
            return self.cfg.monitor
        now = time.monotonic()
        with self._monitor_lock:
            if now - self._monitor_at < FOLLOW_SECONDS and self.monitor < len(monitors):
                return self.monitor
            self._monitor_at = now
        found, why = None, "screen.monitor: GSPro's window isn't up"
        try:
            from . import winshot

            info = winshot.window_info(self.cfg.window_title)
            if info and info["minimized"] and self.monitor < len(monitors):
                return self.monitor   # it comes back where it was
            if info:
                found, why = monitor_of(info["rect"], monitors), "GSPro's window is on it"
        except Exception:
            log.debug("Couldn't look for GSPro's window", exc_info=True)
        if found is None or found >= len(monitors):
            found = self.cfg.monitor if self.cfg.monitor < len(monitors) else 1
        with self._monitor_lock:
            if found != self.monitor:
                log.info("Reading monitor %s (%s)", found, why)
            self.monitor = found
        return found

    def gspro_showing(self) -> bool:
        """GSPro's own (Unity) window is open and not minimized; a browser tab or folder with GSPro in
        its title doesn't count. True off Windows or when the check fails, so a broken lookup never
        hides the ON AIR badge."""
        import sys

        if sys.platform != "win32":
            return True
        try:
            from . import winshot

            info = winshot.window_info(self.cfg.window_title)
        except Exception:
            log.debug("Couldn't look for GSPro's window", exc_info=True)
            return True
        return bool(info) and info.get("game", False) and not info["minimized"]

    def _mon(self, sct) -> dict:
        return sct.monitors[self.monitor_index(sct.monitors)]

    def monitor_rect(self) -> tuple[int, int, int, int]:
        """(left, top, width, height) of the monitor being read, in desktop pixels."""
        import mss

        with mss.mss() as sct:
            m = self._mon(sct)
            return m["left"], m["top"], m["width"], m["height"]

    def detect_mirror(self, base: "Image.Image | None" = None) -> bool:
        """Check which side the minimap is on, from `base` (a full frame) or two tiny grabs. Runs on
        every read, since a player's layout can change between shots. A side counts only with the
        slope bar on it and the course on the other: a scorecard, menu or loading screen (a dark panel
        on both sides, or on neither) keeps the last answer. The bar with its slope text switches at
        once; an empty bar (on the green, and alike to a flat panel) only on a second read in a row."""
        right = mirrored_region(_MIRROR_PROBE)
        try:
            left_img, right_img = ((self.crop(base, _MIRROR_PROBE), self.crop(base, right)) if base is not None
                                   else (self.grab(_MIRROR_PROBE), self.grab(right)))
        except Exception:
            log_failure("Mirrored-layout probe failed")
            self._mirror_pending = None
            return self.mirrored
        left, right_look = _corner_look(left_img), _corner_look(right_img)
        if left in ("text", "flat") and right_look == "course":
            side, look = True, left
        elif right_look in ("text", "flat") and left == "course":
            side, look = False, right_look
        else:
            self._mirror_pending = None   # only reads in a row confirm an empty bar
            return self.mirrored
        if side == self.mirrored:
            self._mirror_pending = None
        elif look == "text" or self._mirror_pending == side:
            self._set_mirrored(side, "slope bar")
        else:
            self._mirror_pending = side
        return self.mirrored

    def _set_mirrored(self, side: bool, why: str) -> None:
        self.mirrored, self._mirror_pending = side, None
        log.info("GSPro HUD layout: %s (%s)", "mirrored (minimap bottom-left, club bottom-right)"
                 if side else "standard (minimap bottom-right, club bottom-left)", why)

    def base_region(self, name: str) -> Region | None:
        """A region as configured for this GSPro, before any mirroring. On GSPro's new menus (self.ui)
        it comes from the config's regions_new, else NEW_UI_REGIONS for a box they moved."""
        if self.ui == "new":
            if name in self.cfg.regions_new:
                return self.cfg.regions_new[name]
            if name in NEW_UI_REGIONS:
                return NEW_UI_REGIONS[name]
        return self.cfg.regions.get(name)

    def region(self, name: str) -> Region | None:
        """A HUD region (see base_region), moved to the other side when the layout is mirrored."""
        region = self.base_region(name)
        if region is not None and self.mirrored and name in MIRRORED:
            return mirrored_region(region)
        return region

    def _padded(self, name: str, img: Image.Image) -> Image.Image:
        """3.59's name pills fill their crop edge to edge in a saturated team colour, and the OCR's
        detector misses a short name there ("Josh" on red) unless the pill gets a margin of its own
        colour."""
        if self.ui != "new" or not name.startswith("setup_slot_"):
            return img
        from PIL import ImageOps

        return ImageOps.expand(img, border=max(8, img.height // 2), fill=img.getpixel((1, 1)))

    def _setup_frame(self) -> Image.Image:
        """Full frame for the pre-round setup read."""
        return self._grab_full()

    def _hud_base(self) -> "Image.Image | None":
        """Base frame for a HUD read: GSPro's window (capture: window), else None so read_hud grabs
        each region off the monitor."""
        return self._window_frame()

    def _window_frame(self) -> Image.Image | None:
        """GSPro's own window via PrintWindow (occlusion-proof), or None to fall back to the monitor."""
        if self.cfg.capture != "window":
            return None
        from . import winshot

        img = winshot.capture_window(self.cfg.window_title)
        if img is None:
            log.warning("Couldn't capture the %r window; using the monitor grab", self.cfg.window_title)
        else:
            _capture_ok()
        return img

    def _grab_full(self) -> Image.Image:
        img = self._window_frame()
        if img is not None:
            return img
        import mss

        # mss handles aren't thread-safe, and these calls run in worker threads.
        with mss.mss() as sct:
            return self._sct_grab(sct, self._mon(sct))

    def monitor_size(self) -> tuple[int, int]:
        import mss

        with mss.mss() as sct:
            mon = self._mon(sct)
            return mon["width"], mon["height"]

    def grab(self, region: Region | None = None) -> Image.Image:
        # Monitor mode grabs just the small region box; grabbing the whole 4K screen per read (then
        # cropping) moves far more pixels and stutters the game.
        if region is not None and self.cfg.capture != "window":
            import mss

            with mss.mss() as sct:
                mon = self._mon(sct)
                self._frame_h = mon["height"]
                left, top, width, height = region.to_pixels(mon["width"], mon["height"])
                box = {"left": mon["left"] + left, "top": mon["top"] + top, "width": width, "height": height}
                return self._sct_grab(sct, box)
        img = self._grab_full()
        return self.crop(img, region) if region else img

    def burst(self, region: Region, frames: int, interval: float) -> list[Image.Image]:
        """`frames` quick grabs of a small region, `interval` seconds apart (one mss handle, so the
        frames come tens of milliseconds apart, not the full-screen grab's hundreds)."""
        import mss

        out = []
        with mss.mss() as sct:
            mon = self._mon(sct)
            left, top, width, height = region.to_pixels(mon["width"], mon["height"])
            box = {"left": mon["left"] + left, "top": mon["top"] + top, "width": width, "height": height}
            for i in range(frames):
                if i:
                    time.sleep(interval)
                out.append(self._sct_grab(sct, box))
        return out

    def to_jpeg(self, img: Image.Image) -> bytes:
        if img.width > self.cfg.image_max_width:
            h = round(img.height * self.cfg.image_max_width / img.width)
            img = img.resize((self.cfg.image_max_width, h))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return buf.getvalue()

    async def snapshot_jpeg(self) -> bytes:
        def grab_jpeg() -> bytes:
            return self.to_jpeg(self.grab())
        return await asyncio.to_thread(grab_jpeg)

    def _ocr_engine(self) -> Any:
        if self._ocr is None:
            from rapidocr import RapidOCR

            params = ({"EngineConfig.onnxruntime.intra_op_num_threads": self.cfg.ocr_threads}
                      if self.cfg.ocr_threads else None)
            try:
                self._ocr = RapidOCR(params=params) if params else RapidOCR()
            except Exception:
                log.exception("OCR thread cap rejected; using defaults")
                self._ocr = RapidOCR()
            # RapidOCR logs a WARNING on every region that holds no text ("text detection result is
            # empty"); the setup watcher reads many empty regions. Quiet it AFTER building the engine
            # (its constructor resets the level) and drop that one message outright with a filter.
            rlog = logging.getLogger("RapidOCR")
            rlog.setLevel(logging.ERROR)
            rlog.addFilter(lambda rec: "text detection result is empty" not in rec.getMessage())
        return self._ocr

    def ocr_items(self, img: Image.Image) -> list[OcrItem]:
        import numpy as np

        size, rec_only = getattr(self._ocr_how, "size", None), getattr(self._ocr_how, "rec_only", False)
        arr = np.array(img)
        with self._ocr_lock:
            engine = self._ocr_engine()
            # RapidOCR keeps each call's use_* flags for the next, so every call sets them all.
            if rec_only:
                res = engine(arr, use_det=False, use_cls=False, use_rec=True)
            else:
                self._size_detector(engine, size)
                res = engine(arr, use_det=True, use_cls=True, use_rec=True)
        if not res.txts:
            return []
        if rec_only:   # no detection, so no boxes: the text fills the image
            w, h = img.size
            return [OcrItem(text=t, x=w / 2, y=h / 2, h=float(h), left=0.0) for t in res.txts if t]
        items = []
        for text, box in zip(res.txts, res.boxes):
            xs, ys = box[:, 0], box[:, 1]
            items.append(OcrItem(text=text, x=float(xs.mean()), y=float(ys.mean()), h=float(ys.max() - ys.min()),
                                 left=float(xs.min())))
        return items

    def _size_detector(self, engine: Any, size: tuple[str, int] | None) -> None:
        """Set the detector's input size for the next call: `size`, else its configured default."""
        det = getattr(engine, "text_det", None) or (engine._load_det_model() if hasattr(engine, "_load_det_model") else None)
        if det is None or not hasattr(det, "limit_side_len"):
            return
        if self._det_default is None:
            self._det_default = (det.limit_type, det.limit_side_len)
        det.limit_type, det.limit_side_len = size or self._det_default

    def _det_size(self, name: str | None, img: Image.Image, scale: float = 1.0) -> tuple[str, int] | None:
        """The detector's (limit_type, limit_side_len) for region `name`'s crop `img` (already resized
        by `scale`) under FAST_DETECT, or None for the default."""
        rule = FAST_DETECT.get(name or "") if self.cfg.ocr_fast else None
        if rule is None:
            return None
        kind, n = rule
        if kind == "min":
            return "min", n
        if not self._frame_h:
            return None
        r = n / self._frame_h / (scale or 1.0)
        short = min(img.size)
        if kind == "shrink" and r >= 1:
            return None
        if r >= 1:
            return "min", max(short, round(short * r))
        return "max", max(32, round(max(img.size) * r))

    def ocr_region(self, img: Image.Image, name: str | None, scale: float = 1.0,
                   rec_only: bool = False) -> list[OcrItem]:
        """ocr_items on region `name`'s crop, sized for it (_det_size). `rec_only`: read `img` as one
        piece of text, with no detection (a lone digit already cut out)."""
        self._ocr_how.size = None if rec_only else self._det_size(name, img, scale)
        self._ocr_how.rec_only = rec_only
        try:
            return self.ocr_items(img)
        finally:
            self._ocr_how.size, self._ocr_how.rec_only = None, False

    def ocr(self, img: Image.Image) -> str:
        return " ".join(i.text for i in self.ocr_items(img))

    def crop(self, img: Image.Image, region: Region) -> Image.Image:
        self._frame_h = img.height
        left, top, width, height = region.to_pixels(img.width, img.height)
        return img.crop((left, top, left + width, top + height))

    def region_tones(self, names: list[str], image: Image.Image) -> dict[str, "Tone"]:
        """Grey-level summary of each named region in `image`: a cheap pixel probe (~2 ms for nine
        regions), no OCR. Tells the setup screen's states apart: the selected PLAYERS tab, a filled
        name pill, an empty "+" slot, and an open player dropdown (a near-white list)."""
        out: dict[str, Tone] = {}
        for name in names:
            region = self.region(name)
            if region is None:
                continue
            out[name] = tone_of(self.crop(image, region))
        return out

    def setup_frame(self) -> Image.Image:
        """One full frame for the pre-round setup watcher (no OCR)."""
        return self._setup_frame()

    def grab_regions(self, names: list[str]) -> dict[str, Any]:
        """The named HUD regions as they are right now, no OCR: a few ms, so they're near-simultaneous
        (read_hud grabs each region as it gets to it, over a second apart with OCR in between). With
        the lie or club, the other side's boxes too, under OTHER_SIDE (read_hud's _other_side)."""
        base = self._hud_base()
        if any(n in MIRRORED for n in names):
            self.detect_mirror(base)   # where the mirrored boxes are, before they're cut out
        out: dict[str, Any] = {}
        for name in names:
            if (region := self.region(name)) is not None:
                out[name] = self.crop(base, region) if base is not None else self.grab(region)
        if "lie" in names or "club" in names:
            try:
                out[OTHER_SIDE] = self._other_side_crops(names, base)
            except Exception:
                log_failure("Grabbing the other side's HUD boxes failed")
        return out

    def grab_boxes(self, names: tuple[str, ...] | list[str]) -> dict[str, Image.Image]:
        """The named HUD regions off the monitor through one mss handle, as the layout stands (no
        mirror probe: HudBuffer's thread mustn't move the layout under a read). Empty with window
        capture, whose PrintWindow grabs stutter the game when polled."""
        if self.cfg.capture != "monitor":
            return {}
        import mss

        out: dict[str, Image.Image] = {}
        grabbed: list[tuple[tuple[int, int, int, int], Image.Image]] = []
        with mss.mss() as sct:
            mon = self._mon(sct)
            self._frame_h = mon["height"]
            for name in names:
                if (region := self.region(name)) is None:
                    continue
                left, top, width, height = region.to_pixels(mon["width"], mon["height"])
                # Each grab waits on the compositor (~20 ms on the sim PC, whatever its size): a box
                # inside one already grabbed (the name in the player box) is cut from that.
                if (inside := next(((b, img) for b, img in grabbed if b[0] <= left and b[1] <= top
                                    and left + width <= b[0] + b[2] and top + height <= b[1] + b[3]), None)):
                    (bl, bt, _, _), img = inside
                    out[name] = img.crop((left - bl, top - bt, left - bl + width, top - bt + height))
                    continue
                out[name] = self._sct_grab(sct, {"left": mon["left"] + left, "top": mon["top"] + top,
                                                 "width": width, "height": height})
                grabbed.append(((left, top, width, height), out[name]))
        return out

    def read_buffered(self, frames: list[dict[str, Image.Image]], accept=None, tries: int = 4,
                      want_club: bool = False) -> Hud | None:
        """The newest of `frames` (HudBuffer.before, newest first) whose player box reads a player and
        a shot number (and passes `accept`, e.g. a known player), with its club and lie read too. A frame
        whose name cell is blank (the box hidden: GSPro's flight view) is skipped by a pixel check, no
        OCR, so the whole buffer is looked through; at most `tries` frames are OCR'd. `want_club`: a
        frame whose club box reads nothing is passed over for an older one that does (GSPro can hide the
        club box at the swing while the player box still shows), falling back to the newest."""
        shown = [c for c in frames if "player_box" in c
                 and ("player_name" not in c or name_signature(c["player_name"]) != NAME_DARK)]
        first = None
        for crops in shown[:tries]:
            hud = self.read_hud(["player_box"], crops=crops, other_side=False)
            if not hud.player_text or hud.shot_number is None or (accept is not None and not accept(hud)):
                continue
            rest = self.read_hud([n for n in ("player_name", "club", "lie") if n in crops], crops=crops,
                                 other_side=False)
            hud.name_sig, hud.club, hud.lie = rest.name_sig, rest.club, hud.box_lie or rest.lie
            hud.handed = hud.handed or rest.handed
            hud.crops = crops
            if want_club and not hud.club:
                first = first or hud
                continue
            return hud
        return first

    def _other_side_crops(self, names: list[str], base: "Image.Image | None") -> dict[str, Any]:
        """The minimap-side probe and the named mirrored boxes on the side the layout doesn't say, with
        the layout they were cut against."""
        mirrored = self.mirrored
        probe = _MIRROR_PROBE if mirrored else mirrored_region(_MIRROR_PROBE)
        out: dict[str, Any] = {"mirrored": mirrored,
                               "probe": self.crop(base, probe) if base is not None else self.grab(probe)}
        for name in names:
            if name in MIRRORED and name != "minimap" and (region := self.base_region(name)) is not None:
                region = region if mirrored else mirrored_region(region)
                out[name] = self.crop(base, region) if base is not None else self.grab(region)
        return out

    def read_hud(self, parts: list[str], image: Image.Image | None = None,
                 crops: dict[str, Image.Image] | None = None, other_side: bool = True) -> Hud:
        """OCR the named HUD regions, from `crops` (grab_regions), `image` (a saved frame) or the live
        screen. `other_side`: an empty lie or club box may be read again on the other side (_other_side)."""
        hud = Hud(crops=crops)
        # With a full frame (a saved image, or GSPro's window via WGC) crop each region from it;
        # otherwise (monitor mode, WGC off/stale) grab each small region box on its own, since grabbing
        # the whole 4K screen per read stutters the game. WGC's frame is captured on a background thread,
        # so cropping it here is free and occlusion-proof.
        _c = time.perf_counter()
        base = image if image is not None or crops is not None else self._hud_base()
        self.last_capture_seconds = time.perf_counter() - _c
        if crops is None and any(n in MIRRORED for n in parts):
            self.detect_mirror(base)
        for name in parts:
            region = self.region(name)
            if region is None:
                continue
            try:
                if crops is not None and name in crops:
                    img = crops[name]
                else:
                    img = self.crop(base, region) if base is not None else self.grab(region)
                if name == "player_name":
                    hud.name_sig = name_signature(img)
                    continue
                scale = self.cfg.ocr_scale
                crop_size = img.size
                if scale and scale != 1.0:
                    img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))))
                items = self.ocr_region(img, name, scale)
            except Exception:
                log_failure("OCR failed for region %s", name)
                continue
            texts = [i.text for i in items]
            if name == "player_box":
                for k, v in parse_player_box(texts).items():
                    setattr(hud, k, v)
                if hud.shot_number is None:
                    hud.shot_number = self._strip_shot(img, items)
                hud.elevation_yards = self._elevation(img, items, metric=(hud.to_pin or "").endswith("metres"))
                if (cell := _box_lie_cell(items)) is not None:
                    hud.box_lie = parse_lie([cell.text])
            elif name == "hole_box":
                for k, v in parse_hole_box(texts).items():
                    setattr(hud, k, v)
            elif name == "lie":
                hud.lie = parse_lie(texts)
            elif name == "pick_panel":
                hud.pick_cards = parse_pick_cards(items)
            elif name in ("tiles", "tiles_left"):
                hud.tiles = {**hud.tiles, **parse_tiles(items)}
                if name == "tiles":
                    hud.tile_boxes = _tile_boxes(items, scale or 1.0, img.width, img.height)
                    hud.tile_crop_size = crop_size
            elif name == "club":
                for k, v in parse_club(texts).items():
                    setattr(hud, k, v)
            elif name == "slope":
                for k, v in parse_slope(texts).items():
                    setattr(hud, k, v)
            elif name == "others":
                hud.others = parse_others(items)
            elif name == "score_banner":
                banner = parse_score_banner(texts, hud.par)
                hud.hole_result = banner[0] if banner else None
            elif name == "scorecard_title":
                # 3.59's card has no title: the GS PRO logo, with the hole row's "Out" under it.
                title = " ".join(texts).upper()
                hud.scorecard_open = "SCORECARD" in title or (
                    "GSPRO" in title.replace(" ", "") and re.search(r"\bOUT\b", title) is not None)
            elif name == "scorecard":
                hud.scorecard = parse_scorecard(items)
        if other_side:
            self._other_side(parts, hud, base, crops)
        # The player box's lie is the ball's own, in plain words; the minimap's header is the backup.
        hud.lie = hud.box_lie or hud.lie
        return hud

    def _other_side(self, parts: list[str], hud: Hud, base: "Image.Image | None",
                    crops: dict[str, Any] | None = None) -> None:
        """A lie or club box that read empty, with the course rather than the slope bar in the
        minimap's corner: the layout is stale (detect_mirror couldn't tell), so those boxes are read
        on the other side, and that side kept when they read there. From grab_regions' crops the other
        side is the one grabbed with them (one moment), never a later live grab."""
        if not (("lie" in parts and not hud.lie) or ("club" in parts and not hud.club)):
            return
        try:
            other_crops = crops.get(OTHER_SIDE) if crops is not None else self._other_side_crops(parts, base)
            if not other_crops or _corner_look(other_crops["probe"]) != "course":
                return
            mirrored = other_crops["mirrored"]
            names = [n for n in parts if n in other_crops and n not in ("mirrored", "probe")]
            other = self.read_hud(names, crops={n: other_crops[n] for n in names}, other_side=False)
        except Exception:
            log_failure("Reading the other side's HUD boxes failed")
            return
        if not (other.lie or other.club) or self.mirrored != mirrored:
            return
        self._set_mirrored(not mirrored, "lie/club read on the other side")
        hud.lie = hud.lie or other.lie
        hud.club, hud.handed = hud.club or other.club, hud.handed or other.handed
        if "slope" in names:
            hud.slope_updown, hud.slope_side = other.slope_updown, other.slope_side
            hud.slope_side_degrees = other.slope_side_degrees

    @staticmethod
    def _elevation(img: Image.Image, items: list[OcrItem], metric: bool) -> float | None:
        """The elevation readout is the last number in the player box, with its arrow just left of it.

        On 3.59 the lie and the shot-number strip follow it, so only items left of the lie count; and
        OCR sometimes joins it to the distance ("5ft 5  0ft 0"), so then it's the text after that."""
        numeric = [i for i in items if re.search(r"\d", i.text) and not re.search(r"shot|\(", i.text, re.I)]
        if (lie := _box_lie_cell(items)) is not None:
            numeric = [i for i in numeric if i.left < lie.left]
        if not numeric:
            return None
        if len(numeric) >= 2:  # the first number is the distance to the pin
            item, text, left = numeric[-1], numeric[-1].text, numeric[-1].left
        else:
            item = numeric[0]
            d = DISTANCE.search(item.text)
            text = item.text[d.end():] if d else ""
            if not re.search(r"\d", text):
                return None
            width = 2 * (item.x - item.left)
            left = item.left + width * (len(item.text) - len(text.lstrip())) / max(len(item.text), 1)
        h = max(item.h, 4)
        crop = img.crop((max(0, int(left - 1.6 * h)), int(item.y - h / 2), int(left + 0.9 * h), int(item.y + h / 2)))
        return parse_elevation(text, metric, arrow_direction(crop))

    def _strip_shot(self, img: Image.Image, items: list[OcrItem]) -> int | None:
        """3.59's shot number: the digit in the white box on the strip of shot numbers (1..par, more
        once past it) right of the distance to the pin. The strip's plain digits OCR as one run
        ("1234"), so the box is found by its pixels and its digit read on its own."""
        import numpy as np
        from PIL import ImageOps

        row = next((i for i in items if DISTANCE.search(i.text)), None)
        if row is None:
            return None
        # The box is about twice the text's height, so its columns stay mostly light through the digit
        # while the gap to the "FOR PAR" pill that can follow it is dark top to bottom.
        h = max(row.h, 4)
        top = max(0, int(row.y - h))
        band = np.asarray(img.convert("RGB"), dtype=int)[top:int(row.y + h)]
        if band.size == 0:
            return None
        light = (band.min(axis=2) > 130) & (np.ptp(band, axis=2) < 50)
        cols = np.append(light.mean(axis=0) > 0.3, False)
        start = None
        for x, on in enumerate(cols):
            if on and start is None:
                start = x
            elif not on and start is not None:
                if start > row.x and 0.6 * h <= x - start <= 1.6 * h:
                    # Just the white: the box has a team-colour line above and a grey band below,
                    # and with them a lone digit often doesn't OCR.
                    white = band[:, start:x].min(axis=2) > 170
                    rows = np.nonzero(white.mean(axis=1) > 0.5)[0]
                    if rows.size == 0:
                        return None
                    tight = img.crop((start + 1, top + rows.min() + 1, x - 1, top + rows.max()))
                    loose = img.crop((start, top, x, top + band.shape[0]))
                    # A lone digit is the detector's weak spot; no one framing reads every digit (at
                    # 3450 px a "1" read "7" as often as "1", and a neighbour creeps into "31" or
                    # "14"). The strip counts up, so the plain digits beside the box say what it
                    # should hold: a read that agrees wins, else the most common read.
                    expect = _strip_neighbour(items, row, start, x)
                    not_first = _strip_left_of_box(items, row, start)
                    if not_first and expect == 1:
                        expect = None
                    reads = []
                    for crop, scale, pad in ((tight, 1, 10), (tight, 2, 20), (loose, 2, x - start),
                                             (tight, 3, 30)):
                        crop = crop.resize((crop.width * scale, crop.height * scale))
                        crop = ImageOps.expand(crop, border=pad, fill=(255, 255, 255))
                        text = re.sub(r"[^0-9]", "", unicodedata.normalize("NFKC", "".join(
                            i.text for i in self.ocr_region(crop, None, rec_only=self.cfg.ocr_fast))))
                        if text and 0 < int(text) < 20 and not (not_first and int(text) == 1):
                            if int(text) == expect:
                                return expect
                            reads.append(int(text))
                    return max(reads, key=reads.count) if reads else expect if not_first else None
                start = None
        return None

    async def read_hud_async(self, parts: list[str], at_once: bool = False) -> Hud:
        """`at_once`: grab every region first, then OCR them, so all of them show one moment. Grabbed
        as it's read, the club box (last of an impact read) came ~3 s after the swing on a slow read,
        gone by then, on 70 shots of one round."""
        started = time.perf_counter()
        hud = await asyncio.to_thread(
            lambda: self.read_hud(parts, crops=self.grab_regions(parts)) if at_once else self.read_hud(parts))
        self.last_read_seconds = time.perf_counter() - started
        return hud

    def read_region_text(self, names: list[str], image: Image.Image | None = None) -> dict[str, str]:
        """OCR each named region to a plain joined string. Used for screens without a HUD layout
        (the pre-round setup screen); a missing region or a failed read comes back as "".

        Grabs the whole monitor once and crops, rather than each region as its own mss sub-rectangle:
        the per-region sub-grab came back black on this setup (so the live read was empty though an
        offline screenshot of the same screen OCR'd fine). Monitor grab, not PrintWindow window
        capture: polling PrintWindow continuously hangs the booth during GSPro's menu->game transition.
        GSPro is the front window while a round is being set up, so the monitor grab is clean; a
        transient popup can't corrupt the roster either, since a slot only counts when it resolves to
        a league player (which a notification's text won't) and the roster is kept across polls."""
        base = image if image is not None else self._grab_full()
        out: dict[str, str] = {}
        for name in names:
            region = self.region(name)
            if region is None:
                out[name] = ""
                continue
            try:
                img = self.crop(base, region)
                scale = self.cfg.ocr_scale
                if scale and scale != 1.0:
                    img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))))
                out[name] = " ".join(i.text for i in self.ocr_region(img, name, scale)).strip()
            except Exception:
                log_failure("OCR failed for region %s", name)
                out[name] = ""
        return out

    def read_region_lines(self, names: list[str], image: Image.Image | None = None) -> dict[str, list[str]]:
        """Like read_region_text, but returns each region's OCR grouped into text lines by vertical
        position, not one flat string. For the setup slots: a name pill holds a single line (first and
        last name share that line), but clicking the pill opens a dropdown that stacks several players
        vertically, so the line count separates a real one-name slot from an opened picker."""
        base = image if image is not None else self._grab_full()
        out: dict[str, list[str]] = {}
        for name in names:
            region = self.region(name)
            if region is None:
                out[name] = []
                continue
            try:
                img = self._padded(name, self.crop(base, region))
                scale = self.cfg.ocr_scale
                if scale and scale != 1.0:
                    img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))))
                out[name] = _group_lines(self.ocr_region(img, name, scale))
            except Exception:
                log_failure("OCR failed for region %s", name)
                out[name] = []
        return out

    def read_region_lines_cached(
        self, names: list[str], image: "Image.Image", cache: dict
    ) -> tuple[dict[str, list[str]], dict]:
        """Like read_region_lines, but skips any region whose cropped pixels are unchanged since the
        last call (`cache` is {name: (digest, lines)}), reusing its cached lines. Returns
        (lines_by_name, new_cache). The setup slots are static apart from the player name (proven: the
        static UI is pixel-identical across captures), so once the roster is read most polls OCR
        nothing and a single changed slot reads on its own - catching a quick swap in one slot's time
        rather than all eight. A digest collision would only ever cost an extra OCR, never a miss."""
        import hashlib

        out: dict[str, list[str]] = {}
        new_cache: dict[str, tuple] = {}
        for name in names:
            region = self.region(name)
            if region is None:
                out[name] = []
                continue
            try:
                img = self.crop(image, region)
            except Exception:
                log.exception("Crop failed for region %s", name)
                out[name] = []
                continue
            digest = hashlib.blake2b(img.convert("L").resize((80, 32)).tobytes(), digest_size=8).digest()
            prev = cache.get(name)
            if prev is not None and prev[0] == digest:
                lines = prev[1]
            else:
                scale = self.cfg.ocr_scale
                ocr_img = self._padded(name, img)
                ocr_img = (ocr_img.resize((max(1, round(ocr_img.width * scale)), max(1, round(ocr_img.height * scale))))
                           if scale and scale != 1.0 else ocr_img)
                try:
                    lines = _group_lines(self.ocr_region(ocr_img, name, scale))
                except Exception:
                    log_failure("OCR failed for region %s", name)
                    lines = []
                # Visibility for debugging the hash: a region re-read means its pixels changed. INFO on a
                # real change (the hash fired - compare against the roster the watcher then logs), DEBUG
                # on the cold first read. A change that never logs here means the hash missed it; a slot
                # that logs every poll means the hash is false-triggering (jitter in that region).
                (log.info if prev is not None else log.debug)("Region %s changed; re-read %r", name, lines)
            out[name] = lines
            new_cache[name] = (digest, lines)
        return out, new_cache

    def grab_and_read(self, names: list[str]) -> tuple[Image.Image, dict[str, str]]:
        """One frame of GSPro's window (occlusion-proof WGC, else a monitor grab) plus OCR of `names`
        from it, returning the frame so the caller can OCR more regions from the same frame without
        re-capturing. Lets an idle poll read just a gate region and only read the rest when that gate
        matches, keeping fast polling cheap. Used by the pre-round setup watcher."""
        frame = self._setup_frame()
        return frame, self.read_region_text(names, image=frame)

    def save_jpeg(self, path, max_width: int = 1280) -> None:
        """A small copy of the whole screen, for reviewing what the booth saw."""
        img = self.grab()
        if img.width > max_width:
            img = img.resize((max_width, round(img.height * max_width / img.width)))
        img.save(path, format="JPEG", quality=75)

    def calibration_image(self, image: Image.Image | None = None) -> Image.Image:
        """Screenshot (or `image`) with a labelled 100px grid and the configured regions outlined."""
        img = image.copy() if image is not None else self.grab()
        draw = ImageDraw.Draw(img)
        for x in range(0, img.width, 100):
            draw.line([(x, 0), (x, img.height)], fill=(255, 0, 255), width=1)
            draw.text((x + 2, 2), str(x), fill=(255, 255, 0))
        for y in range(0, img.height, 100):
            draw.line([(0, y), (img.width, y)], fill=(255, 0, 255), width=1)
            draw.text((2, y + 2), str(y), fill=(255, 255, 0))
        for name in self.cfg.regions:
            if (r := self.region(name)) is None:
                continue
            left, top, width, height = r.to_pixels(img.width, img.height)
            draw.rectangle([left, top, left + width, top + height], outline=(0, 255, 0), width=3)
            draw.text((left + 4, top + 4), name, fill=(0, 255, 0))
        return img


class HudBuffer:
    """The last few seconds of the HUD boxes that name a shot, grabbed a couple of times a second while
    `active()` says so (a round is on). Some setups hide the player box (and the club box) the
    moment the ball is struck, before the impact read can grab it: the address frames still show
    who hit it, which stroke, with what club. Small crops only, no OCR until asked."""

    # Two grabs a tick: the name is cut from the player box. Not the lie (the minimap, a big box): the
    # setup read and GSPro's log have it.
    REGIONS = ("player_box", "player_name", "club")

    def __init__(self, screen: Screen, seconds: float = 8.0, interval: float = 0.5) -> None:
        self.screen, self.seconds, self.interval = screen, seconds, interval
        self._frames: deque[tuple[float, dict[str, Image.Image]]] = deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.grab_seconds = 0.0   # the last grab's cost

    def start(self, active) -> None:
        self._thread = threading.Thread(target=self._run, args=(active,), name="hud-buffer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self, active) -> None:
        failed = False
        while not self._stop.wait(self.interval):
            if not active():
                with self._lock:
                    self._frames.clear()
                continue
            started = time.perf_counter()
            try:
                crops = self.screen.grab_boxes(self.REGIONS)
            except Exception:
                if not failed:
                    log.exception("HUD buffer grab failed")
                failed = True
                continue
            failed = False
            self.grab_seconds = time.perf_counter() - started
            if crops:
                self.add(time.monotonic(), crops)

    def add(self, at: float, crops: dict[str, Image.Image]) -> None:
        with self._lock:
            self._frames.append((at, crops))
            while self._frames and self._frames[0][0] < at - self.seconds:
                self._frames.popleft()

    def before(self, at: float, since: float | None = None) -> list[dict[str, Image.Image]]:
        """The frames grabbed at or before `at` and after `since` (the last shot's impact), newest first."""
        with self._lock:
            return [c for t, c in reversed(self._frames) if t <= at and (since is None or t > since)]
