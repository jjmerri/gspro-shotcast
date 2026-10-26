"""Hole geometry from GSPro's course file, for rounds whose layout GSPro never logged.

GSPro logs each hole's tee and pin (hole_layouts) only when a round is complete; a round ended early
logs none, and its hole replays had no tee or pin to draw the minimap against. The course file
(`<course>.GKD`, JSON) has the same world positions: every tee box and the pin for each of four days.
Which ones the round used is worked out from its shots: the pin is the day whose spot matches the
shots' logged distances to it, and the tee is the box whose distance to that pin matches the tee
shot's. From the tee GSPro measures along the hole's aim points (a dogleg's yardage, not the straight
line), so that distance is too. A group tees off one box all round: the box most holes match is used
where a hole's own match is unclear.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from collections import Counter
from pathlib import Path

from .sources.gspro_log import M_TO_YD

log = logging.getLogger(__name__)

TEE_BOXES = ("Black", "White", "Green", "Blue", "Yellow", "Red", "Junior", "Par3")
PIN_MATCH_M = 2.0        # a day's pin fits a hole when the shots' distances to it miss by at most this
TEE_MATCH_M = 3.0        # a box fits a hole when its distance to the pin is within this of the tee shot's
_HOLE = re.compile(r"Hole (\d+)")


def _xyz(p) -> tuple[float, float, float] | None:
    if not isinstance(p, dict) or p.get("x") is None or p.get("z") is None:
        return None
    return float(p["x"]), float(p.get("y") or 0.0), float(p["z"])


def load(folder: Path) -> dict[int, dict] | None:
    """Each hole's par, tee boxes, aim points and pins from the course folder's GKD file, or None."""
    files = [f for f in folder.glob("*") if f.suffix.lower() == ".gkd" and "par3" not in f.stem.lower()]
    if not files:
        return None
    try:
        data = json.loads(min(files, key=lambda f: len(f.name)).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        log.info("Couldn't read the course file in %s (%s)", folder, e)
        return None
    out = {}
    for h in data.get("Holes") or []:
        if not isinstance(h, dict) or not h.get("HoleNumber"):
            continue
        tees = {t.get("TeeType"): p for t in h.get("Tees") or []
                if t.get("TeeType") in TEE_BOXES and (p := _xyz(t.get("Position")))}
        aims = [p for t in h.get("Tees") or [] if re.fullmatch(r"AimPoint\d+", t.get("TeeType") or "")
                and t.get("Enabled") and (p := _xyz(t.get("Position")))]
        pins = {p.get("Day"): q for p in h.get("Pins") or [] if (q := _xyz(p.get("Position")))}
        out[int(h["HoleNumber"])] = {"par": h.get("Par"), "tees": tees, "aims": aims, "pins": pins}
    return out or None


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[2] - b[2])


def _yardage(tee, aims: list, pin) -> float:
    """The hole's length from this tee as GSPro shows it on the tee: through its aim points to the pin."""
    pts = [tee, *aims, pin]
    return sum(_dist(a, b) for a, b in zip(pts, pts[1:]))


def infer(geometry: dict[int, dict], shots: dict[int, list[dict]]) -> dict[int, dict]:
    """The tee and pin each hole was played with, from its shots (dicts with shot_number, start_x/z,
    to_pin_yards, mulligan). Holes it can't tell are left out."""
    played = {h: rows for h, rows in shots.items() if any(not r["mulligan"] for r in rows)}
    fitting: dict[int, dict[str, float]] = {}
    for hole, rows in played.items():
        g = geometry.get(hole)
        # Not the tee shot's: its stored start can be where a mulligan's ball stopped, not the tee.
        obs = [((r["start_x"], 0.0, r["start_z"]), r["to_pin_yards"] / M_TO_YD) for r in rows
               if not r["mulligan"] and (r["shot_number"] or 0) > 1 and r["start_x"] is not None
               and r["to_pin_yards"] is not None]
        if g and g["pins"] and obs:
            miss = {day: sum(abs(_dist(p, o) - d) for o, d in obs) / len(obs) for day, p in g["pins"].items()}
            fitting[hole] = {day: m for day, m in miss.items() if m <= PIN_MATCH_M}
    # GSPro sets one pin day for the round: the day most holes fit, where a hole's shots fit it too (two
    # days' pins can be close enough that both fit) or can't say (no shot positioned).
    days = Counter(day for f in fitting.values() for day in f)
    usual = days.most_common(1)[0][0] if days else None
    pins: dict[int, str] = {}
    for hole in played:
        f = fitting.get(hole)
        if f and usual not in f:
            pins[hole] = min(f, key=f.get)
        elif (f or hole not in fitting) and usual in (geometry.get(hole) or {}).get("pins", {}):
            pins[hole] = usual
    fits: dict[int, dict[str, float]] = {}
    for hole, day in pins.items():
        g = geometry[hole]
        ds = [r["to_pin_yards"] / M_TO_YD for r in shots[hole]
              if r["shot_number"] == 1 and not r["mulligan"] and r["to_pin_yards"]]
        if ds:
            miss = {b: abs(_yardage(t, g["aims"], g["pins"][day]) - ds[0]) for b, t in g["tees"].items()}
            fits[hole] = {b: m for b, m in miss.items() if m <= TEE_MATCH_M}
    votes = Counter(b for f in fits.values() for b in f)
    box = votes.most_common(1)[0][0] if votes else None
    out = {}
    for hole, day in pins.items():
        g, f = geometry[hole], fits.get(hole) or {}
        b = box if box in f or not f else min(f, key=f.get)
        out[hole] = {"par": g["par"], "pin": g["pins"][day], "tee": g["tees"].get(b) if b else None,
                     "day": day, "box": b}
    return out


def fill_missing(db: sqlite3.Connection, courses_dirs, since: float | None = None) -> int:
    """Store a layout from the course file for each hole of a recorded round that has no layout from
    GSPro's log (a round quit early), for the hole replays. Rounds ending before `since` are skipped.
    Returns the holes filled."""
    from . import course_card, hole_replay, stats_web

    round_of = stats_web.shot_rounds(db)
    rounds: dict[int, list[sqlite3.Row]] = {}
    for r in db.execute("SELECT id, session_id, ts, course, hole, shot_number, start_x, start_z, to_pin_yards,"
                        " mulligan FROM shots ORDER BY id"):
        if r["id"] in round_of and _HOLE.match(r["hole"] or ""):
            rounds.setdefault(round_of[r["id"]], []).append(r)
    spans = {n: ({r["session_id"] for r in rows}, rows[0]["ts"], rows[-1]["ts"]) for n, rows in rounds.items()}
    layouts = hole_replay._layouts(db)
    geometry: dict[str, dict | None] = {}
    filled = 0
    for n, rows in rounds.items():
        ses, first, last = spans[n]
        if since is not None and last < since:
            continue
        course = rows[0]["course"]
        until = min((f for s, f, _ in spans.values() if s & ses and f > last), default=None)
        by_hole: dict[int, list] = {}
        for r in rows:
            by_hole.setdefault(int(_HOLE.match(r["hole"]).group(1)), []).append(r)
        missing = {h: hr for h, hr in by_hole.items()
                   if hole_replay._layout_for(layouts, course, h, ses, hr[0]["ts"], hr[-1]["ts"], until)[0] is None}
        if not missing:
            continue
        key = (course or "").casefold()
        if key not in geometry:
            folder = course_card.find(courses_dirs, course)
            geometry[key] = load(folder) if folder else None
        if not geometry[key]:
            continue
        found = infer(geometry[key], {h: [dict(r) for r in hr] for h, hr in missing.items()})
        none3 = (None, None, None)
        for h, lo in found.items():
            last_row = missing[h][-1]
            db.execute("INSERT INTO hole_layouts (session_id, ts, course, hole, par, tee_x, tee_y, tee_z,"
                       " pin_x, pin_y, pin_z) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (last_row["session_id"], last_row["ts"], course, h, lo["par"],
                        *(lo["tee"] or none3), *lo["pin"]))
            filled += 1
        if found:
            log.info("Round %s at %s had no layout from GSPro: took %d holes' tee and pin from the course file"
                     " (%s tees, pins %s)", n, course, len(found), next(iter(found.values()))["box"] or "unknown",
                     "/".join(sorted({lo["day"] for lo in found.values()})))
    if filled:
        db.commit()
    return filled
