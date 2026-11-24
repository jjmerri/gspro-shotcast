"""A handicap index from a player's cards, the way the World Handicap System works it out, with no
database: see commentator/handicap_index.py for the rules and how the booth reads the cards (shared
with Shotcast Cloud, which reads them from rounds' stored bodies).

A tee is a mapping {tee, rating, slope, yards: {hole: yards}, par: {hole: par}, index: {hole: stroke
index}}; a card is (player, course, date, scores {hole: strokes}, pars {hole: par}, tee or None,
mulligans).

These trust their input as the booth gives it. A caller with untrusted cards checks them first:
`_differential` hands out the course handicap one stroke at a time (handicap.allocate), so a wild
rating, slope, score or index makes it loop for as long as the number is large. Hole numbers as ints
1-18, 9 or more holes in both scores and pars, scores and pars in a golf range, a tee's rating and
slope in the WHS ranges (slope 55-155), and no empty distance list for pick_tee.
"""

from __future__ import annotations

import math
import statistics
from typing import Any

from .handicap import allocate, round_half_up

NEUTRAL_SLOPE = 113
MAX_INDEX = 54.0
LATEST = 20
FIRST_HOLES = 54     # holes to play before the first index
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
    holes = sorted(h for h in scores if h in pars)[:18]
    par = sum(pars[h] for h in holes)
    par18 = sum((tee or {}).get("par", {}).values()) or (par * 18 / len(holes))
    rating = tee["rating"] * par / par18 if tee else float(par)
    slope = tee["slope"] if tee else NEUTRAL_SLOPE
    share = len(holes) / 18
    if index is not None:
        if len(holes) > 9:
            # 10 to 17 holes: the strokes fall as they would over the full 18, on the holes played.
            full = sorted(set(holes) | set((tee or {}).get("par", {})) | set(pars))[:18]
            full_par = sum(pars.get(h) or (tee or {}).get("par", {}).get(h) or 0 for h in full)
            full_rating = tee["rating"] if tee else float(full_par)
            ch = round_half_up(index * slope / NEUTRAL_SLOPE + (full_rating - full_par))
        else:
            full = holes
            ch = round_half_up(index * share * slope / NEUTRAL_SLOPE + (rating - par))
        hard = sorted(full, key=lambda h: ((tee or {}).get("index", {}).get(h) or 99, h))
        strokes = allocate(ch, hard)
        cap = {h: pars[h] + 2 + strokes.get(h, 0) for h in holes}
    else:
        cap = {h: pars[h] + 5 for h in holes}
    adjusted = sum(min(scores[h], cap[h]) for h in holes)
    diff = (adjusted - rating) * NEUTRAL_SLOPE / slope
    if len(holes) < 18:
        missing = 18 - len(holes)
        diff = diff + (0.52 * index + 1.2) * missing / 9 if index is not None else diff * 18 / len(holes)
    return round(diff, 1), len(holes)


def _index_of(cards: list[tuple]) -> dict[str, dict[str, Any]]:
    diffs: dict[str, list[float]] = {}
    played_holes: dict[str, int] = {}
    out: dict[str, dict[str, Any]] = {}
    known: dict[str, float | None] = {}   # each player's index until their next card

    def current(player: str) -> float | None:
        if player not in known:
            known[player] = index_from(diffs.get(player, [])) if played_holes.get(player, 0) >= FIRST_HOLES else None
        return known[player]

    for player, course, date, sc, pa, tee, mulls in cards:
        diff, counted = _differential(sc, pa, tee, current(player))
        diffs.setdefault(player, []).append(diff)
        played_holes[player] = played_holes.get(player, 0) + counted
        known.pop(player, None)
        p = out.setdefault(player, {"index": None, "rounds": 0, "history": []})
        p["index"], p["rounds"] = current(player), len(diffs[player])
        p["history"].append({"date": date, "course": course, "holes": counted, "tee": tee and tee["tee"],
                             "differential": diff, "index": p["index"], "mulligans": mulls})
    return out
