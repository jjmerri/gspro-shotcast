"""A league recap's net leaderboard drawn into its video (recap.burn_boards), and its opening results
and closing standings made video of their own (recap.burn_cards), so they're there full screen too (a
phone's player shows only the video itself, none of the page laid over it) and the recap's one video,
seekable from its start to its end.

Needs ffmpeg: imageio-ffmpeg's (the `publish` extra), or one on the PATH. Without, the recap goes up
as it was, the page drawing the board over the video."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from functools import lru_cache
from io import BytesIO
from pathlib import Path

log = logging.getLogger(__name__)

LOOK = 2                  # how a board's drawn: a change to it draws every recap's again
ACCENT = (95, 197, 138)   # the stats page's --accent
ROW = 0.028               # a row's text, as a share of the frame's height
BOTTOM = 0.12             # the board's bottom, up off the frame's (clear of a player's controls)
INSET = 0.04              # in from the picture's right edge, as a share of its width
CARD_LOOK = 2             # how a card's drawn and made: a change to it makes every recap's again
CARD_SEGMENT = 4.0        # a card's video, in segments this long
BG = (17, 19, 24)         # the stats page's background


@lru_cache(maxsize=1)
def ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def _par(n: int) -> str:
    return "E" if n == 0 else f"{n:+d}"


def board_png(board: dict, height: int) -> bytes:
    """The board (recap.board's) as a PNG for a frame `height` pixels tall: as the page draws it."""
    from PIL import Image, ImageDraw, ImageFont

    fs = max(10, round(height * ROW))
    font, head = ImageFont.load_default(size=fs), ImageFont.load_default(size=round(fs * 0.8))
    rows = [(f"{'T' if r.get('tied') else ''}{r['place']}", str(r["name"]), _par(int(r["net"])), bool(r.get("on")))
            for r in board["rows"]]
    title = f"NET · THRU {board['thru']}"
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    w = lambda s, f=font: probe.textlength(s, font=f)
    gap, pad, edge = fs * 0.6, fs * 0.7, max(2, round(fs * 0.2))
    cols = [max(w(r[i]) for r in rows) for i in range(3)]
    width = round(edge + 2 * pad + max(sum(cols) + 2 * gap, w(title, head)))
    line = round(fs * 1.35)
    top = pad + fs * 1.2
    img = Image.new("RGBA", (width, round(top + line * len(rows) + pad * 0.6)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, img.width - 1, img.height - 1), radius=max(2, fs // 4), fill=(0, 0, 0, 205))
    d.rectangle((0, 0, edge - 1, img.height - 1), fill=(*ACCENT, 255))
    x0 = edge + pad
    d.text((x0, pad * 0.8), title, font=head, fill=(*ACCENT, 255))
    for i, (place, name, net, on) in enumerate(rows):
        y, color = top + i * line, (*ACCENT, 255) if on else (255, 255, 255, 255)
        d.text((x0, y), place, font=font, fill=color)
        d.text((x0 + cols[0] + gap, y), name, font=font, fill=color)
        d.text((x0 + cols[0] + cols[1] + 2 * gap + cols[2] - w(net), y), net, font=font, fill=color)
    out = BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def _run(args: list[str], data: bytes | None = None) -> bytes:
    return subprocess.run(args, input=data, capture_output=True, check=True, timeout=180).stdout


def frame(ff: str, seg: Path) -> tuple[int, int, float]:
    """A segment's frame size, and the black bars each side of the picture in it (the sim's window
    narrower than the stream), as a share of its width: 0 when there are none or it can't tell."""
    import numpy as np

    probe = subprocess.run([ff, "-hide_banner", "-i", str(seg)], capture_output=True, text=True, timeout=60).stderr
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", probe)
    if not m:
        raise ValueError(f"no video in {seg.name}")
    W, H = 160, 90
    grab = ["-i", str(seg), "-frames:v", "1", "-vf", f"scale={W}:{H}", "-f", "rawvideo", "-pix_fmt", "rgb24"]
    raw = _run([ff, "-hide_banner", "-loglevel", "error", *grab, "-ss", "1", "-"])   # (past any fade in)
    if len(raw) < W * H * 3:
        raw = _run([ff, "-hide_banner", "-loglevel", "error", *grab, "-"])
    lit = np.frombuffer(raw[:W * H * 3], np.uint8).reshape(H, W, 3).astype(int).sum(axis=2) > 60
    cols = lit.any(axis=0)
    left, right = (int(np.argmax(cols)), int(np.argmax(cols[::-1]))) if cols.any() else (W, W)
    bars = min(left, right) / W if left < W / 4 and abs(left - right) <= 3 else 0.0
    return int(m.group(1)), int(m.group(2)), bars


def clip(ff: str, segs: list[bytes], board: dict) -> list[bytes]:
    """A clip's segments with its board drawn in at the picture's bottom right (off any bars in the
    frame), their timestamps and audio as they were."""
    from PIL import Image

    with tempfile.TemporaryDirectory() as tmp:
        src, img, out = Path(tmp, "in.ts"), Path(tmp, "board.png"), Path(tmp, "out.ts")
        src.write_bytes(segs[0])
        W, H, bars = frame(ff, src)
        png = board_png(board, H)
        img.write_bytes(png)
        bw, bh = Image.open(BytesIO(png)).size
        x = max(0, round(W - W * bars - W * (1 - 2 * bars) * INSET - bw))
        y = max(0, round(H - H * BOTTOM - bh))
        done = []
        for seg in segs:
            src.write_bytes(seg)
            _run([ff, "-hide_banner", "-loglevel", "error", "-y", "-copyts", "-i", str(src), "-i", str(img),
                  "-filter_complex", f"[0:v][1:v]overlay={x}:{y}:format=auto,format=yuv420p[v]",
                  "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                  "-c:a", "copy", "-muxdelay", "0", "-muxpreload", "0", "-f", "mpegts", str(out)])
            done.append(out.read_bytes())
        return done


def _arrow(d, x: float, y: float, size: float, up: bool, fill) -> None:
    h = size * 0.8
    pts = [(x, y + h), (x + size, y + h), (x + size / 2, y)] if up else [(x, y), (x + size, y), (x + size / 2, y + h)]
    d.polygon(pts, fill=fill)


def card_png(kind: str, body: dict, width: int, height: int) -> bytes:
    """The recap's opening card (`kind` "intro": the week's results) or closing one ("outro": the season
    standings), a frame `width` x `height`, as the page draws them."""
    from PIL import Image, ImageDraw, ImageFont

    fs = max(12, round(height * 0.042))
    font, big, small = (ImageFont.load_default(size=fs), ImageFont.load_default(size=round(fs * 1.5)),
                        ImageFont.load_default(size=round(fs * 0.75)))
    white, grey, accent = (255, 255, 255), (160, 166, 178), ACCENT
    if kind == "intro":
        title = f"Week {body.get('week')}"
        sub = " · ".join(str(x) for x in (body.get("course"), "final results") if x)
        rows = [(f"{'T' if r.get('tied') else ''}{r['place']}", str(r["name"]), _par(int(r["net"])),
                 f"{round(r['points'] * 10) / 10:g} pts" if r.get("points") is not None else "", None)
                for r in body.get("rows") or []]
    else:
        title, sub = "Standings", f"after week {body.get('week')}"
        rows, places = [], [r["place"] for r in body.get("standings") or []]
        for r in body.get("standings") or []:
            move = (r["was"] - r["place"]) if r.get("was") else 0
            tie = "T" if places.count(r["place"]) > 1 else ""
            rows.append((f"{tie}{r['place']}", str(r["name"]), f"{round(r['points'] * 10) / 10:g}", str(abs(move)) if move else "",
                         move > 0 if move else None))
    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)
    w = lambda s, f=font: d.textlength(s, font=f)
    line = round(fs * 1.5)
    most = max(1, int((height * 0.62) // line))
    rows = rows[:most]
    gap = fs * 1.2
    cols = [max([w(r[i]) for r in rows] + [0]) for i in range(4)]
    arrow = fs * 0.6 if any(r[4] is not None for r in rows) else 0
    table = sum(cols) + arrow + 3 * gap + (fs * 0.3 if arrow else 0)
    head = round(fs * 1.5) + round(fs * 1.2)
    total = head + fs + line * len(rows)
    x0, y = (width - max(table, w(title, big), w(sub, small))) / 2, (height - total) / 2
    d.rectangle((x0, y - fs * 0.5, x0 + fs * 2.5, y - fs * 0.5 + max(2, fs // 6)), fill=accent)
    d.text((x0, y), title, font=big, fill=white)
    y += round(fs * 1.5) + fs * 0.3
    d.text((x0, y), sub, font=small, fill=grey)
    y += round(fs * 1.2) + fs
    for place, name, num, extra, up in rows:
        x = x0
        d.text((x, y), place, font=font, fill=grey)
        x += cols[0] + gap
        d.text((x, y), name, font=font, fill=white)
        x += cols[1] + gap
        d.text((x + cols[2] - w(num), y), num, font=font, fill=accent if kind == "intro" else white)
        x += cols[2] + gap
        if up is not None:
            _arrow(d, x, y + fs * 0.25, arrow, up, accent if up else (224, 122, 107))
            x += arrow + fs * 0.3
        d.text((x, y + (fs * 0.2 if kind == "intro" else 0)), extra, font=small if kind == "intro" else font, fill=grey)
        y += line
    out = BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def probe(ff: str, seg: Path) -> dict:
    """A segment's frame size, frame rate and audio (rate, channel layout; None with none), for a
    card made to play on from it."""
    info = subprocess.run([ff, "-hide_banner", "-i", str(seg)], capture_output=True, text=True, timeout=60).stderr
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", info)
    if not m:
        raise ValueError(f"no video in {seg.name}")
    fps = re.search(r"([\d.]+) fps", info)
    a = re.search(r"Audio:.*?(\d{4,6}) Hz, ([a-z0-9.()]+)", info)
    return {"width": int(m.group(1)) // 2 * 2, "height": int(m.group(2)) // 2 * 2,
            "fps": min(60.0, float(fps.group(1))) if fps else 30.0,
            "audio": (int(a.group(1)), a.group(2).split("(")[0]) if a else None}


def card_plan(seconds: float) -> list[float]:
    """A card's segments' lengths: CARD_SEGMENT each, and what's left (at least half a second)."""
    import math

    n = max(1, math.ceil(seconds / CARD_SEGMENT - 0.05))
    return [round(CARD_SEGMENT if i < n - 1 else max(0.5, seconds - CARD_SEGMENT * i), 3) for i in range(n)]


def card(ff: str, png: bytes, seconds: float, like: dict) -> list[tuple[bytes, float]]:
    """`png` held still for `seconds`, as segments (bytes, seconds) of CARD_SEGMENT and what's left,
    made like the clips' (probe()'s `like`), with silence for sound if they have any."""
    with tempfile.TemporaryDirectory() as tmp:
        img, seg = Path(tmp, "card.png"), Path(tmp, "out.ts")
        img.write_bytes(png)
        fps, out = like["fps"], []
        for i, d in enumerate(card_plan(seconds)):
            args = [ff, "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-framerate", f"{fps:g}", "-i", str(img)]
            if like["audio"]:
                rate, layout = like["audio"]
                args += ["-f", "lavfi", "-i", f"anullsrc=r={rate}:cl={layout}", "-c:a", "aac", "-b:a", "64k"]
            args += ["-t", f"{d:.3f}", "-vf", f"scale={like['width']}:{like['height']},format=yuv420p",
                     "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", "-r", f"{fps:g}",
                     "-output_ts_offset", f"{CARD_SEGMENT * i:.3f}", "-muxdelay", "0", "-muxpreload", "0",
                     "-f", "mpegts", str(seg)]
            _run(args)
            out.append((seg.read_bytes(), d))
        return out
