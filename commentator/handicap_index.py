"""Our own handicap index, from the rounds on record, the way the World Handicap System works it out.

Each individual-ball card of nine holes or more is a score differential: the adjusted gross (every hole
capped at net double bogey: par + 2 + the handicap strokes it gets; par + 5 before there's an index)
less the course rating, times 113 over the slope. A card of 9 to 17 holes counts the holes played
against their share of the rating (by par) and is made an 18-hole differential by adding what the
index expects of each hole not played ((0.52 x index + 1.2) / 9 a hole), or scaled up to 18 holes
before there's an index. The index is the WHS average of the lowest differentials among the latest
20 (the lowest 8 of 20, fewer when there are fewer, with its adjustments), once there are three and
54 holes have been played.

The rating and slope are the tee's in GSPro's course files (store.course_tees). The tee played isn't
logged, so it's the tee whose card yardages best match the round's logged tee-to-pin distances
(hole_layouts, from the batch GSPro logged as that round ended); with no yardages, the rated tee
whose rating best fits the length played. A course with no rated tee plays to its par on a 113 slope.

Every such round counts, mulligans or not; `without_mulligans` is the index without the rounds with
a mulligan (a stroke not taken), for the page's toggle. A putt GSPro read under MISREAD_PUTT_MPH
never holds a round out: that's the launch monitor catching the ball being set, not a putt (a
deliberate putt that slow would roll under two feet, inside every gimme).

The league's handicaps (its file, refreshed from Squabbit) come first wherever the booth uses one;
this index stands in for a player who has none there, and is what the stats page shows.
"""
from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

from shotcast_scoring.handicap_index import (  # noqa: F401 (the shared copy; this module keeps the app's names)
    FIRST_HOLES,
    JUNIOR_TEES,
    LATEST,
    MAX_INDEX,
    NEUTRAL_SLOPE,
    WHS_TABLE,
    _differential,
    _index_of,
    index_from,
    pick_tee,
)
from .sources.gspro_log import M_TO_YD

SHARED_BALL = ("scramble", "alternate")
LAYOUT_MATCH_S = 3600  # a round's layouts are logged as it ends
MISREAD_PUTT_MPH = 2.0
# A mulligan that counts against a round: any but a misread putt (see the module docstring).
COUNTED_MULLIGAN = (f"mulligan = 1 AND NOT (COALESCE(is_putt, 0) = 1"
                    f" AND COALESCE(ball_speed, {MISREAD_PUTT_MPH}) < {MISREAD_PUTT_MPH})")
MULLIGAN_SPAN_S = 8 * 3600   # a mulligan this long before the round's end was in a round never recorded


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
    """Logged tee-to-pin distances, yards: per (course, session, batch ts), per (course, session) and per
    course (session None)."""
    out: dict[tuple, dict[int, list[float]]] = {}
    try:
        rows = db.execute("SELECT course, session_id, ts, hole, tee_x, tee_z, pin_x, pin_z FROM hole_layouts"
                          " WHERE tee_x IS NOT NULL AND pin_x IS NOT NULL").fetchall()
    except sqlite3.OperationalError:
        return out
    for course, sid, ts, hole, tx, tz, px, pz in rows:
        d = math.hypot(px - tx, pz - tz) * M_TO_YD
        c = course.casefold()
        for key in ((c, sid, ts), (c, sid), (c, None)):
            out.setdefault(key, {}).setdefault(hole, []).append(d)
    return out


def _batches(layouts: dict[tuple, dict[int, list[float]]]) -> dict[tuple, list[tuple]]:
    """The batch keys of `layouts` per (course, session), in their order there."""
    out: dict[tuple, list[tuple]] = {}
    for k in layouts:
        if len(k) == 3 and k[2] is not None:
            out.setdefault(k[:2], []).append(k)
    return out


def _round_layout(layouts: dict[tuple, dict[int, list[float]]], batches: dict[tuple, list[tuple]],
                  course: str, sid: int, ended: float | None):
    """The tee-to-pin distances logged for this round: the session's batch logged nearest its end, else
    the session's, else the course's."""
    batches = batches.get((course, sid), [])
    if ended is not None and batches:
        near = min(batches, key=lambda k: abs(k[2] - ended))
        if abs(near[2] - ended) <= LAYOUT_MATCH_S:
            return layouts[near]
    return layouts.get((course, sid)) or layouts.get((course, None))


def mulligans_by_round(db: sqlite3.Connection) -> dict[int, int]:
    """Counted mulligans (COUNTED_MULLIGAN) per player_rounds row: each one in the player's first round
    of that session on that course ending after it."""
    ends: dict[tuple, list[tuple[float, int]]] = {}
    for rid, sid, player, course, ended in db.execute(
            "SELECT id, session_id, player, course, ended_at FROM player_rounds ORDER BY ended_at, id"):
        ends.setdefault((sid, player, (course or "").casefold()), []).append((ended, rid))
    out: dict[int, int] = {}
    for sid, player, course, ts in db.execute(
            f"SELECT session_id, player, course, ts FROM shots WHERE {COUNTED_MULLIGAN} AND player IS NOT NULL"):
        for ended, rid in ends.get((sid, player, (course or "").casefold()), []):
            if ended >= ts:
                if ended - ts <= MULLIGAN_SPAN_S:
                    out[rid] = out.get(rid, 0) + 1
                break
    return out


def indexes(db: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Per player: the current index (None until three differentials), how many differentials, and the
    history, one point per counted round: {date, course, holes, tee, differential, index, mulligans},
    and the same without the rounds with a counted mulligan, under `without_mulligans`."""
    tees, layouts = _tees(db), _distances(db)
    batches = _batches(layouts)
    rows = db.execute("SELECT pr.id, pr.player, pr.session_id, pr.ended_at, pr.course, pr.fmt, pr.scores, pr.hole_pars, s.date"
                      " FROM player_rounds pr JOIN sessions s ON s.id = pr.session_id"
                      " WHERE pr.excluded = 0 ORDER BY pr.ended_at, pr.id").fetchall()
    mulligans = mulligans_by_round(db)
    cards = []
    for rid, player, sid, ended, course, fmt, scores, pars, date in rows:
        if (fmt or "") in SHARED_BALL or not player:
            continue
        sc = {h: v for h, v in _json(scores).items() if isinstance(v, int)}
        pa = {h: v for h, v in _json(pars).items() if isinstance(v, int)}
        holes = sorted(h for h in sc if h in pa)
        if len(holes) < 9:
            continue
        key = (course or "").casefold()
        tee = pick_tee(tees.get(key, []), _round_layout(layouts, batches, key, sid, ended), holes[:18])
        cards.append((player, course, date, sc, pa, tee, mulligans.get(rid, 0)))
    every = _index_of(cards)
    clean = _index_of([c for c in cards if not c[6]])
    empty = {"index": None, "rounds": 0, "history": []}
    return {p: {**every[p], "without_mulligans": clean.get(p, empty)} for p in every}
