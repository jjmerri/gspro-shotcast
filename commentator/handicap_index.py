"""Our own handicap index, from the rounds on record, the way the World Handicap System works it out.

Each individual-ball card of nine holes or more is a score differential: the adjusted gross (every hole
capped at net double bogey: par + 2 + the handicap strokes it gets; par + 5 before there's an index)
less the course rating, times 113 over the slope. A nine-hole card (or a round quit before 18) counts
its first nine against half the rating (by par) and is made an 18-hole differential by adding what the
index expects of the other nine (0.52 x index + 1.2), or doubled before there's an index. The index is
the WHS average of the lowest differentials among the latest 20 (the lowest 8 of 20, fewer when
there are fewer, with its adjustments), once there are three.

The rating and slope are the tee's in GSPro's course files (store.course_tees). The tee played isn't
logged, so it's the tee whose card yardages best match the round's logged tee-to-pin distances
(hole_layouts); with no yardages, the rated tee whose rating best fits the length played. A course
with no rated tee plays to its par on a 113 slope.

The league's handicaps (its file, refreshed from Squabbit) come first wherever the booth uses one;
this index stands in for a player who has none there, and is what the stats page shows.
"""
from __future__ import annotations

import json
import math
import sqlite3
import statistics
from typing import Any

from .handicap import allocate
from .sources.gspro_log import M_TO_YD

SHARED_BALL = ("scramble", "alternate")
NEUTRAL_SLOPE = 113
MAX_INDEX = 54.0
LATEST = 20
# Of the latest differentials on record: how many lowest to average, and the adjustment, per count.
WHS_TABLE = {3: (1, -2.0), 4: (1, -1.0), 5: (1, 0.0), 6: (2, -1.0), 7: (2, 0.0), 8: (2, 0.0),
             9: (3, 0.0), 10: (3, 0.0), 11: (3, 0.0), 12: (4, 0.0), 13: (4, 0.0), 14: (4, 0.0),
             15: (5, 0.0), 16: (5, 0.0), 17: (6, 0.0), 18: (6, 0.0), 19: (7, 0.0), 20: (8, 0.0)}
JUNIOR_TEES = ("Junior", "Par 3")   # GSPro's placeholder-rated forward tees: only by yardage


def index_from(diffs: list[float]) -> float | None:
    """The WHS index from score differentials, oldest first; None with fewer than three."""
    latest = diffs[-LATEST:]
    if len(latest) < 3:
        return None
    take, adjust = WHS_TABLE[len(latest)]
    value = statistics.mean(sorted(latest)[:take]) + adjust
    return min(MAX_INDEX, math.floor(value * 10 + 0.5) / 10)


def _json(text: str | None) -> dict[int, Any]:
    try:
        return {int(k): v for k, v in json.loads(text or "{}").items()}
    except (TypeError, ValueError, AttributeError):
        return {}


def _tees(db: sqlite3.Connection) -> dict[str, list[dict]]:
    try:
        rows = db.execute("SELECT course, tee, rating, slope, yards, par, hole_index FROM course_tees").fetchall()
    except sqlite3.OperationalError:
        return {}
    out: dict[str, list[dict]] = {}
    for course, tee, rating, slope, yards, par, index in rows:
        out.setdefault((course or "").casefold(), []).append(
            {"tee": tee, "rating": rating, "slope": slope, "yards": {h: y for h, y in _json(yards).items() if y},
             "par": _json(par), "index": _json(index)})
    return out


def _distances(db: sqlite3.Connection) -> dict[tuple, dict[int, list[float]]]:
    """Logged tee-to-pin distances, yards: per (course, session) and per course (session None)."""
    out: dict[tuple, dict[int, list[float]]] = {}
    try:
        rows = db.execute("SELECT course, session_id, hole, tee_x, tee_z, pin_x, pin_z FROM hole_layouts"
                          " WHERE tee_x IS NOT NULL AND pin_x IS NOT NULL").fetchall()
    except sqlite3.OperationalError:
        return out
    for course, sid, hole, tx, tz, px, pz in rows:
        d = math.hypot(px - tx, pz - tz) * M_TO_YD
        for key in ((course.casefold(), sid), (course.casefold(), None)):
            out.setdefault(key, {}).setdefault(hole, []).append(d)
    return out


def pick_tee(tees: list[dict], played: dict[int, list[float]] | None, holes: list[int]) -> dict | None:
    """The rated tee this round was most likely played from (see the module docstring), or None."""
    rated = [t for t in tees if t["rating"] and t["slope"]]
    if not rated:
        return None
    played = {h: ds for h, ds in (played or {}).items() if h in holes}
    by_yards = [t for t in rated if all(t["yards"].get(h) for h in played)] if played else []
    if by_yards:
        def miss(t):
            return statistics.median(abs(d - t["yards"][h]) for h, ds in played.items() for d in ds)
        return min(by_yards, key=miss)
    grown = [t for t in rated if t["tee"] not in JUNIOR_TEES] or rated
    if played:
        # A rule of thumb for a men's rating from length: yards / 220 + 40.9, over 18 holes.
        length = statistics.mean(statistics.median(ds) for ds in played.values()) * 18
        return min(grown, key=lambda t: abs(t["rating"] - (length / 220 + 40.9)))
    return sorted(grown, key=lambda t: t["rating"])[len(grown) // 2]


def _differential(scores: dict[int, int], pars: dict[int, int], tee: dict | None, index: float | None) -> tuple[float, int]:
    """(18-hole differential, holes counted) for a card of 9 or more holes with pars."""
    holes = sorted(h for h in scores if h in pars)
    holes = holes[:18] if len(holes) >= 18 else holes[:9]
    par = sum(pars[h] for h in holes)
    par18 = sum((tee or {}).get("par", {}).values()) or (par * 18 / len(holes))
    rating = tee["rating"] * par / par18 if tee else float(par)
    slope = tee["slope"] if tee else NEUTRAL_SLOPE
    share = len(holes) / 18
    if index is not None:
        ch = round(index * share * slope / NEUTRAL_SLOPE + (rating - par))
        hard = sorted(holes, key=lambda h: ((tee or {}).get("index", {}).get(h) or 99, h))
        strokes = allocate(ch, hard)
        cap = {h: pars[h] + 2 + strokes.get(h, 0) for h in holes}
    else:
        cap = {h: pars[h] + 5 for h in holes}
    adjusted = sum(min(scores[h], cap[h]) for h in holes)
    diff = (adjusted - rating) * NEUTRAL_SLOPE / slope
    if len(holes) < 18:
        diff = diff + (0.52 * index + 1.2) if index is not None else diff * 2
    return round(diff, 1), len(holes)


def indexes(db: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Per player: the current index (None until three differentials), how many differentials, and the
    history, one point per counted round: {date, course, holes, tee, differential, index}."""
    tees, layouts = _tees(db), _distances(db)
    rows = db.execute("SELECT pr.player, pr.session_id, pr.course, pr.fmt, pr.scores, pr.hole_pars, s.date"
                      " FROM player_rounds pr JOIN sessions s ON s.id = pr.session_id"
                      " ORDER BY pr.ended_at, pr.id").fetchall()
    diffs: dict[str, list[float]] = {}
    out: dict[str, dict[str, Any]] = {}
    for player, sid, course, fmt, scores, pars, date in rows:
        if (fmt or "") in SHARED_BALL or not player:
            continue
        sc = {h: v for h, v in _json(scores).items() if isinstance(v, int)}
        pa = {h: v for h, v in _json(pars).items() if isinstance(v, int)}
        holes = sorted(h for h in sc if h in pa)
        if len(holes) < 9:
            continue
        key = (course or "").casefold()
        played = layouts.get((key, sid)) or layouts.get((key, None))
        tee = pick_tee(tees.get(key, []), played, holes[:18] if len(holes) >= 18 else holes[:9])
        mine = diffs.setdefault(player, [])
        diff, counted = _differential(sc, pa, tee, index_from(mine))
        mine.append(diff)
        p = out.setdefault(player, {"index": None, "rounds": 0, "history": []})
        p["index"], p["rounds"] = index_from(mine), len(mine)
        p["history"].append({"date": date, "course": course, "holes": counted, "tee": tee and tee["tee"],
                             "differential": diff, "index": p["index"]})
    return out
