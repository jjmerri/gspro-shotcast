import asyncio
import io
import logging
import time
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageDraw

from .config import HUD_ASPECT, Region, ScreenConfig
from .hud import (
    Hud, NAME_DARK, OcrItem, arrow_direction, parse_club, parse_elevation, parse_hole_box, parse_lie, parse_others,
    parse_player_box, parse_score_banner, parse_scorecard, parse_slope, parse_tiles, tile_value_items,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tone:
    """A region's grey levels (0-255): mean, median, and the share of near-white (>= 200) pixels."""

    mean: float
    median: float
    light: float


def name_signature(img: Image.Image) -> tuple:
    """The name cell as a 96x12 text mask (thresholded midway between its darkest and brightest
    pixel, so a brightness shift doesn't change it), or NAME_DARK when there's no text contrast (the
    screen is fading or black between players)."""
    px = list(img.convert("L").resize((96, 12), Image.BILINEAR).tobytes())
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


def _slope_bar(img: Image.Image) -> bool:
    """The slope bar's look: GSPro's dark teal panel (blue level with green, both above red), either
    with the slope's white text in it or, on the green where no slope is shown, empty and flat. Grass,
    sand and trees have far less blue than green; a black loading screen or a scorecard has no teal."""
    from PIL import ImageStat

    st = ImageStat.Stat(img.convert("RGB"))
    (r, g, b), spread = st.mean, sum(st.stddev) / 3
    teal = r < 60 and g - r >= 5 and b - r >= 4 and abs(g - b) <= 8
    return teal and (spread > 35 or spread < 6)


class Screen:
    def __init__(self, cfg: ScreenConfig) -> None:
        self.cfg = cfg
        self._ocr: Any = None
        self.last_read_seconds = 0.0
        self.last_capture_seconds = 0.0
        self.mirrored = False   # the current player's HUD is in GSPro's lefty layout (see MIRRORED)

    def detect_mirror(self, base: "Image.Image | None" = None) -> bool:
        """Check which side the minimap is on, from `base` (a full frame) or two tiny grabs. Runs on
        every read, since a player's layout can change between shots. A frame where neither side (or
        both) shows the slope bar (a loading screen, a scorecard) keeps the last answer."""
        right = mirrored_region(_MIRROR_PROBE)
        try:
            left_img, right_img = ((self.crop(base, _MIRROR_PROBE), self.crop(base, right)) if base is not None
                                   else (self.grab(_MIRROR_PROBE), self.grab(right)))
        except Exception:
            log.exception("Mirrored-layout probe failed")
            return self.mirrored
        on_left, on_right = _slope_bar(left_img), _slope_bar(right_img)
        if on_left != on_right and on_left != self.mirrored:
            self.mirrored = on_left
            log.info("GSPro HUD layout: %s", "mirrored (minimap bottom-left, club bottom-right)"
                     if on_left else "standard (minimap bottom-right, club bottom-left)")
        return self.mirrored

    def region(self, name: str) -> Region | None:
        """A HUD region, moved to the other side when the layout is mirrored."""
        region = self.cfg.regions.get(name)
        if region is not None and self.mirrored and name in MIRRORED:
            return mirrored_region(region)
        return region

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
        return img

    def _grab_full(self) -> Image.Image:
        img = self._window_frame()
        if img is not None:
            return img
        import mss

        # mss handles aren't thread-safe, and these calls run in worker threads.
        with mss.mss() as sct:
            raw = sct.grab(sct.monitors[self.cfg.monitor])
            return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

    def monitor_size(self) -> tuple[int, int]:
        import mss

        with mss.mss() as sct:
            mon = sct.monitors[self.cfg.monitor]
            return mon["width"], mon["height"]

    def grab(self, region: Region | None = None) -> Image.Image:
        # Monitor mode grabs just the small region box; grabbing the whole 4K screen per read (then
        # cropping) moves far more pixels and stutters the game.
        if region is not None and self.cfg.capture != "window":
            import mss

            with mss.mss() as sct:
                mon = sct.monitors[self.cfg.monitor]
                left, top, width, height = region.to_pixels(mon["width"], mon["height"])
                box = {"left": mon["left"] + left, "top": mon["top"] + top, "width": width, "height": height}
                raw = sct.grab(box)
                return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        img = self._grab_full()
        return self.crop(img, region) if region else img

    def burst(self, region: Region, frames: int, interval: float) -> list[Image.Image]:
        """`frames` quick grabs of a small region, `interval` seconds apart (one mss handle, so the
        frames come tens of milliseconds apart, not the full-screen grab's hundreds)."""
        import mss

        out = []
        with mss.mss() as sct:
            mon = sct.monitors[self.cfg.monitor]
            left, top, width, height = region.to_pixels(mon["width"], mon["height"])
            box = {"left": mon["left"] + left, "top": mon["top"] + top, "width": width, "height": height}
            for i in range(frames):
                if i:
                    time.sleep(interval)
                raw = sct.grab(box)
                out.append(Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX"))
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

        res = self._ocr_engine()(np.array(img))
        if not res.txts:
            return []
        items = []
        for text, box in zip(res.txts, res.boxes):
            xs, ys = box[:, 0], box[:, 1]
            items.append(OcrItem(text=text, x=float(xs.mean()), y=float(ys.mean()), h=float(ys.max() - ys.min()),
                                 left=float(xs.min())))
        return items

    def ocr(self, img: Image.Image) -> str:
        return " ".join(i.text for i in self.ocr_items(img))

    def crop(self, img: Image.Image, region: Region) -> Image.Image:
        left, top, width, height = region.to_pixels(img.width, img.height)
        return img.crop((left, top, left + width, top + height))

    def region_tones(self, names: list[str], image: Image.Image) -> dict[str, "Tone"]:
        """Grey-level summary of each named region in `image`: a cheap pixel probe (~2 ms for nine
        regions), no OCR. Tells the setup screen's states apart: the selected PLAYERS tab, a filled
        name pill, an empty "+" slot, and an open player dropdown (a near-white list)."""
        out: dict[str, Tone] = {}
        for name in names:
            region = self.cfg.regions.get(name)
            if region is None:
                continue
            hist = self.crop(image, region).convert("L").histogram()
            n = sum(hist) or 1
            mean = sum(v * c for v, c in enumerate(hist)) / n
            acc, median = 0, 0
            for v, c in enumerate(hist):
                acc += c
                if acc * 2 >= n:
                    median = v
                    break
            out[name] = Tone(mean=mean, median=median, light=sum(hist[200:]) / n)
        return out

    def setup_frame(self) -> Image.Image:
        """One full frame for the pre-round setup watcher (no OCR)."""
        return self._setup_frame()

    def grab_regions(self, names: list[str]) -> dict[str, Image.Image]:
        """The named HUD regions as they are right now, no OCR: a few ms, so they're near-simultaneous
        (read_hud grabs each region as it gets to it, over a second apart with OCR in between)."""
        base = self._hud_base()
        out = {}
        for name in names:
            if (region := self.region(name)) is not None:
                out[name] = self.crop(base, region) if base is not None else self.grab(region)
        return out

    def read_hud(self, parts: list[str], image: Image.Image | None = None,
                 crops: dict[str, Image.Image] | None = None) -> Hud:
        """OCR the named HUD regions, from `crops` (grab_regions), `image` (a saved frame) or the live screen."""
        hud = Hud()
        # With a full frame (a saved image, or GSPro's window via WGC) crop each region from it;
        # otherwise (monitor mode, WGC off/stale) grab each small region box on its own, since grabbing
        # the whole 4K screen per read stutters the game. WGC's frame is captured on a background thread,
        # so cropping it here is free and occlusion-proof.
        _c = time.perf_counter()
        base = image if image is not None or crops is not None else self._hud_base()
        self.last_capture_seconds = time.perf_counter() - _c
        if any(n in MIRRORED for n in parts):
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
                items = self.ocr_items(img)
            except Exception:
                log.exception("OCR failed for region %s", name)
                continue
            texts = [i.text for i in items]
            if name == "player_box":
                for k, v in parse_player_box(texts).items():
                    setattr(hud, k, v)
                hud.elevation_yards = self._elevation(img, items, metric=(hud.to_pin or "").endswith("metres"))
            elif name == "hole_box":
                for k, v in parse_hole_box(texts).items():
                    setattr(hud, k, v)
            elif name == "lie":
                hud.lie = parse_lie(texts)
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
                banner = parse_score_banner(texts)
                hud.hole_result = banner[0] if banner else None
            elif name == "scorecard_title":
                hud.scorecard_open = any("SCORECARD" in t.upper() for t in texts)
            elif name == "scorecard":
                hud.scorecard = parse_scorecard(items)
        return hud

    @staticmethod
    def _elevation(img: Image.Image, items: list[OcrItem], metric: bool) -> float | None:
        """The elevation readout is the last number in the player box, with its arrow just left of it."""
        import re

        numeric = [i for i in items if re.search(r"\d", i.text) and not re.search(r"shot|\(", i.text, re.I)]
        if len(numeric) < 2:  # the first number is the distance to the pin
            return None
        item = numeric[-1]
        h = max(item.h, 4)
        crop = img.crop((max(0, int(item.left - 1.6 * h)), int(item.y - h / 2), int(item.left + 0.9 * h), int(item.y + h / 2)))
        return parse_elevation(item.text, metric, arrow_direction(crop))

    async def read_hud_async(self, parts: list[str]) -> Hud:
        started = time.perf_counter()
        hud = await asyncio.to_thread(self.read_hud, parts)
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
            region = self.cfg.regions.get(name)
            if region is None:
                out[name] = ""
                continue
            try:
                img = self.crop(base, region)
                scale = self.cfg.ocr_scale
                if scale and scale != 1.0:
                    img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))))
                out[name] = " ".join(i.text for i in self.ocr_items(img)).strip()
            except Exception:
                log.exception("OCR failed for region %s", name)
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
            region = self.cfg.regions.get(name)
            if region is None:
                out[name] = []
                continue
            try:
                img = self.crop(base, region)
                scale = self.cfg.ocr_scale
                if scale and scale != 1.0:
                    img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))))
                out[name] = _group_lines(self.ocr_items(img))
            except Exception:
                log.exception("OCR failed for region %s", name)
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
            region = self.cfg.regions.get(name)
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
                ocr_img = (img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))))
                           if scale and scale != 1.0 else img)
                try:
                    lines = _group_lines(self.ocr_items(ocr_img))
                except Exception:
                    log.exception("OCR failed for region %s", name)
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
        for name, r in self.cfg.regions.items():
            left, top, width, height = r.to_pixels(img.width, img.height)
            draw.rectangle([left, top, left + width, top + height], outline=(0, 255, 0), width=3)
            draw.text((left + 4, top + 4), name, fill=(0, 255, 0))
        return img
