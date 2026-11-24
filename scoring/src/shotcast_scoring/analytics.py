"""A player's season analytics with no database: strokes gained, gapping and dispersion, the typical
miss, recovery. Shared by the booth's stats page (commentator/season_analytics.py reads its season
file and hands the rows here) and Shotcast Cloud (which works them out from a round's stored body).

A stroke is a mapping with the booth's `shots` columns: id, player, session_id, course, hole (its label,
"Hole 3, par 4, 380 yards"), shot_number, lie_before, is_putt, to_pin_yards, outcome, gimme_yards,
finish_yards, left_out, is_league, date; a swing for gapping: id, club, carry, game, total_yards,
ball_speed, club_speed, launch_angle, total_spin, spin_axis, start_direction, handed, lie_before.

These trust their input as the booth's season file gives it. A caller with untrusted numbers bounds
them first: yardages and gimme radii to what a course has, shot numbers to a hole's, a swing's carry
positive and its speeds positive when given (gapping_from's quantiles need a swing left), and hole
labels short. handicap_index's caller does the same for cards and tees (see there).
"""

import bisect
import math
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from fractions import Fraction
from typing import Any

from . import shot_shape

# Expected strokes to hole out, PGA Tour baseline (approximate, after Mark Broadie's "Every Shot
# Counts" tables). Off the green: yards to the pin; on the green: feet.
BASELINE = {
    "tee": [(100, 2.92), (120, 2.99), (140, 2.97), (160, 2.99), (180, 3.05), (200, 3.12), (220, 3.17),
            (240, 3.25), (260, 3.45), (280, 3.65), (300, 3.71), (320, 3.79), (340, 3.86), (360, 3.92),
            (380, 3.96), (400, 3.99), (420, 4.02), (440, 4.08), (460, 4.17), (480, 4.28), (500, 4.41),
            (520, 4.54), (540, 4.65), (560, 4.74), (580, 4.79), (600, 4.82)],
    "fairway": [(0, 1.0), (5, 2.10), (10, 2.18), (20, 2.40), (40, 2.60), (60, 2.70), (80, 2.75), (100, 2.80),
                (120, 2.85), (140, 2.91), (160, 2.98), (180, 3.08), (200, 3.19), (220, 3.32), (240, 3.45),
                (260, 3.58), (280, 3.69), (300, 3.78), (350, 4.00), (400, 4.10), (450, 4.25), (500, 4.40),
                (550, 4.60), (600, 4.75)],
    "rough": [(0, 1.0), (5, 2.25), (10, 2.35), (20, 2.59), (40, 2.78), (60, 2.91), (80, 2.96), (100, 3.02),
              (120, 3.08), (140, 3.15), (160, 3.23), (180, 3.31), (200, 3.42), (220, 3.53), (240, 3.64),
              (260, 3.74), (280, 3.83), (300, 3.90), (350, 4.10), (400, 4.25), (500, 4.55), (600, 4.85)],
    "sand": [(0, 1.0), (5, 2.35), (10, 2.43), (20, 2.53), (40, 2.82), (60, 3.15), (80, 3.24), (100, 3.23),
             (120, 3.21), (140, 3.22), (160, 3.28), (180, 3.40), (200, 3.55), (220, 3.70), (240, 3.84),
             (260, 3.93), (280, 4.00), (300, 4.04), (400, 4.30), (600, 4.90)],
    "green": [(0, 1.0), (1, 1.0), (2, 1.01), (3, 1.04), (4, 1.13), (5, 1.23), (6, 1.34), (7, 1.42), (8, 1.50),
              (9, 1.56), (10, 1.61), (15, 1.78), (20, 1.87), (30, 1.98), (40, 2.06), (50, 2.14), (60, 2.21),
              (90, 2.40), (120, 2.55)],
}
DEEP_ROUGH_EXTRA = 0.15        # deep rough plays a touch harder than the rough table
AROUND_GREEN_YARDS = 50        # off the green and this close: "around the green", else an approach
APPROACH_MAX_YARDS = 250
SG_CATEGORIES = ("off_tee", "approach", "around_green", "putting")


def expected(lie: str | None, yards: float | None) -> float | None:
    """Expected strokes to hole out from a lie and distance (yards; feet are worked out for the green)."""
    if yards is None:
        return None
    lie = (lie or "fairway").lower()
    extra = DEEP_ROUGH_EXTRA if lie == "deep rough" else 0.0
    table = BASELINE.get({"deep rough": "rough", "fringe": "fairway"}.get(lie, lie), BASELINE["fairway"])
    x = yards * 3.0 if lie == "green" else yards
    xs = [p[0] for p in table]
    i = bisect.bisect_left(xs, x)
    if i == 0:
        return table[0][1] + extra
    if i >= len(table):
        (x0, y0), (x1, y1) = table[-2], table[-1]
    else:
        (x0, y0), (x1, y1) = table[i - 1], table[i]
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0) + extra


def _par_of(label: str | None) -> int | None:
    m = re.search(r"par (\d)", label or "")
    return int(m.group(1)) if m else None


def _hole_no(label: str | None) -> int | None:
    m = re.search(r"Hole (\d+)", label or "")
    return int(m.group(1)) if m else None


def _hole_key(label: str | None) -> str:
    """'Hole 10, par 4, 257 yards' and 'par 4, 257 yards' are the same hole (the number is read late)."""
    return re.sub(r"^Hole \d+,\s*", "", label or "")


def holes_of(rows) -> list[dict]:
    """holes_played's holes from its strokes, in its order."""
    holes: list[dict] = []
    cur: dict | None = None
    for r in rows:
        key = (r["player"], r["session_id"], r["course"], _hole_key(r["hole"]))
        sn = r["shot_number"]
        new = cur is None or key != cur["key"] or sn == 1 or (sn is not None and cur["last_sn"] is not None
                                                               and sn <= cur["last_sn"])
        if new:
            cur = {"key": key, "player": r["player"], "session_id": r["session_id"], "date": r["date"],
                   "course": r["course"], "label": r["hole"], "par": _par_of(r["hole"]), "hole": _hole_no(r["hole"]),
                   "strokes": [], "last_sn": None, "league": bool(r["is_league"])}
            holes.append(cur)
        cur["strokes"].append(r)
        cur["last_sn"] = sn if sn is not None else cur["last_sn"]
        cur["hole"] = cur["hole"] or _hole_no(r["hole"])
        cur["label"] = r["hole"] if _hole_no(r["hole"]) else cur["label"]
    for h in holes:
        last = h["strokes"][-1]
        h["complete"] = last["outcome"] in ("holed", "gimme")
        h["score"] = (h["last_sn"] or len(h["strokes"])) if h["complete"] else None
        del h["key"]
    return holes


def _category(stroke, par: int | None) -> str:
    lie = (stroke["lie_before"] or "").lower()
    if lie == "green" or (stroke["is_putt"] and not lie):
        return "putting"
    if lie == "tee" and (par or 0) >= 4:
        return "off_tee"
    return "around_green" if (stroke["to_pin_yards"] or 999) <= AROUND_GREEN_YARDS else "approach"


def _gimme_end(radius_yards: float | None, usual_ft: float | None) -> float:
    """Expected strokes left by a stroke that stopped in the gimme circle: 1.0 (the conceded tap-in) in
    the usual circle. A bigger circle concedes longer putts, so its gimme is worth what a ball spread
    over it would take to hole (the average spot is 2/3 of the radius out) beyond the usual one's."""
    if radius_yards is None or usual_ft is None:
        return 1.0
    return 1.0 + expected("green", radius_yards * 2 / 3) - expected("green", usual_ft / 3.0 * 2 / 3)


def strokes_gained(holes: list[dict], usual_gimme_ft: float | None = None) -> list[dict]:
    """Strokes gained on every stroke whose start and finish are known: expected strokes before, minus
    expected after, minus one (and minus any penalty strokes, seen as a jump in the stroke number). A
    conceded tap-in is a stroke from a known spot (expected 1.0 in the usual circle, `usual_gimme_ft`;
    see _gimme_end), so it's 0 gained and only ends the stroke before it. Only finished holes count, so
    the strokes and the holes they're divided by match (a hole left unfinished is often the messy one,
    and its stroke numbers can skip).
    `gimme_r`: the radius (yards) of the gimme circle a stroke stopped in, when known; so the cloud can
    keep a round's strokes gained before a league's usual circle is known (usual_gain)."""
    out = []
    # A gimme's radius is copied from the stroke that left it, which can be the one without (a session's
    # opening tee shot): then the radius the rest of that session's strokes logged.
    logged: dict[int, Counter] = defaultdict(Counter)
    for h in holes:
        logged[h["session_id"]].update(st["gimme_yards"] for st in h["strokes"] if st["gimme_yards"] is not None)
    session_radius = {sid: c.most_common(1)[0][0] for sid, c in logged.items() if c}
    for h in holes:
        if not h["complete"]:
            continue
        s = h["strokes"]
        for i, st in enumerate(s):
            if st["outcome"] == "gimme" or st["left_out"]:
                continue
            start = expected(st["lie_before"] or ("green" if st["is_putt"] else None), st["to_pin_yards"])
            if start is None:
                continue
            gimme_r = None
            if i + 1 < len(s):
                nxt = s[i + 1]
                radius = nxt["gimme_yards"] if nxt["gimme_yards"] is not None else session_radius.get(h["session_id"])
                if nxt["outcome"] == "gimme":
                    gimme_r = radius
                end = _gimme_end(radius, usual_gimme_ft) if nxt["outcome"] == "gimme" else expected(
                    nxt["lie_before"] or ("green" if nxt["is_putt"] else None), nxt["to_pin_yards"])
                if end is None:
                    continue
                penalty = max(0, (nxt["shot_number"] or 0) - (st["shot_number"] or 0) - 1) \
                    if nxt["shot_number"] and st["shot_number"] else 0
            else:
                end, penalty = 0.0, 0
            out.append({"player": h["player"], "session_id": h["session_id"], "date": h["date"],
                        "category": _category(st, h["par"]), "sg": start - end - 1 - penalty, "shot_id": st["id"],
                        "gimme_r": gimme_r})
    return out


def usual_gain(gimme_r: float | None, usual_ft: float | None) -> float:
    """What a stroke into a gimme circle of `gimme_r` yards gains once the usual circle is known: the
    stroke's strokes gained with `usual_ft` is its strokes gained with none (strokes_gained(holes)),
    plus this. 0 when either is unknown (see _gimme_end)."""
    return 1.0 - _gimme_end(gimme_r, usual_ft)


def sg_summary(holes: list[dict], usual_gimme_ft: float | None = None,
               sg: list[dict] | None = None) -> dict[str, dict]:
    """Per player: strokes gained per category and in total, per nine holes played, against the tour.
    `sg`: strokes_gained(holes, usual_gimme_ft), when already worked out."""
    return sg_from(sg_totals(holes, strokes_gained(holes, usual_gimme_ft) if sg is None else sg))


def sg_totals(holes: list[dict], sg: list[dict]) -> dict[str, dict[str, Any]]:
    """What sg_summary is worked out from, per player: strokes gained per category (its sum, kept
    exactly, and its strokes) and finished holes. Totals of some holes add up (add_sg) to theirs together."""
    out: dict[str, dict[str, Any]] = {}
    for s in sg:
        c = out.setdefault(s["player"], {"cats": {}, "holes": 0})["cats"].setdefault(s["category"], [[], 0])
        exact_add(c[0], s["sg"])
        c[1] += 1
    for h in holes:
        if h["complete"]:
            out.setdefault(h["player"], {"cats": {}, "holes": 0})["holes"] += 1
    return out


def add_sg(into: dict[str, dict[str, Any]], part: dict[str, dict[str, Any]]) -> None:
    for player, got in part.items():
        m = into.setdefault(player, {"cats": {}, "holes": 0})
        m["holes"] += got["holes"]
        for cat, (partials, n) in got["cats"].items():
            c = m["cats"].setdefault(cat, [[], 0])
            exact_merge(c[0], partials)
            c[1] += n


def sg_from(totals: dict[str, dict[str, Any]]) -> dict[str, dict]:
    out = {}
    for player, m in totals.items():
        if not m["cats"] or not m["holes"]:
            continue
        per = {c: round(math.fsum(m["cats"][c][0]) * 9 / m["holes"], 2) if c in m["cats"] else 0.0
               for c in SG_CATEGORIES}
        per["total"] = round(sum(per.values()), 2)
        out[player] = {"per_nine": per, "strokes": {c: n for c, (_, n) in m["cats"].items()}, "holes": m["holes"]}
    return out


FULL_SWING = 0.8   # a swing counts toward gapping when its ball speed is this share of the club's top speed
# GSPro picks the club by the distance and ignores the lie, so from the rough the player often swings
# more club than it says (a "9-iron" from the rough carried 131 yards to 113 from the fairway). Gapping
# reads only tee and fairway swings, unless a club has fewer than this many of them.
GAPPING_CLEAN_LIES = ("tee", "fairway")
GAPPING_MIN_CLEAN = 5


def gapping_from(rows: list[Any], finish: dict[int, float]) -> list[dict]:
    """gapping's clubs from its swings (see commentator/season_analytics.gapping) and where GSPro had
    each one finish sideways, by the swing's id (hole_replay.finish_laterals)."""
    by_club: dict[str, list] = defaultdict(list)
    for r in rows:
        by_club[r["club"]].append(r)
    hands = Counter(r["handed"] for r in rows if r["handed"])
    handed = hands.most_common(1)[0][0] if hands else None
    out = []
    for club, every in by_club.items():
        swings = len(every)
        clean = [r for r in every if r["lie_before"] in GAPPING_CLEAN_LIES]
        any_lie = len(clean) < GAPPING_MIN_CLEAN
        if not any_lie:
            every = clean
        speeds = sorted(r["ball_speed"] for r in every if r["ball_speed"])
        top = speeds[int(0.9 * (len(speeds) - 1))] if speeds else None
        rs = [r for r in every if top is None or (r["ball_speed"] or 0) >= FULL_SWING * top]

        def med(col):
            vals = [r[col] for r in rs if r[col] is not None]
            return round(statistics.median(vals), 1) if vals else None
        carries = sorted(r["carry"] for r in rs)
        q = statistics.quantiles(carries, n=4) if len(carries) >= 2 else [carries[0]] * 3
        smash = [r["ball_speed"] / r["club_speed"] for r in rs if r["ball_speed"] and r["club_speed"]]
        side = [(finish.get(r["id"]), r["carry"]) for r in rs]
        side = [(x if x is not None else lateral_yards(r["start_direction"], r["spin_axis"], r["carry"]), y, x is not None)
                for (x, y), r in zip(side, rs)]
        out.append({"club": club, "n": len(rs), "swings": swings, "game": sum(r["game"] for r in rs),
                    **dispersion([(x, y) for x, y, _ in side if x is not None]),
                    "side_logged": sum(known for _, _, known in side),
                    "any_lie": any_lie, "carry": med("carry"), "carry_lo": round(q[0]), "carry_hi": round(q[2]),
                    "carry_max": round(carries[-1]), "total": med("total_yards"), "ball_speed": med("ball_speed"),
                    "club_speed": med("club_speed"), "smash": round(statistics.median(smash), 2) if smash else None,
                    "launch": med("launch_angle"), "spin": med("total_spin"), "spin_axis": med("spin_axis"),
                    "start_dir": med("start_direction"), "miss": miss_label(med("start_direction"), med("spin_axis"), handed)})
    out.sort(key=lambda c: -(c["carry"] or 0))
    for c in out:
        c["gap_to_next"] = None
    for a, b in zip(out, out[1:]):
        a["gap_to_next"] = round(a["carry"] - b["carry"]) if a["carry"] is not None and b["carry"] is not None else None
    return out


DISPERSION_MIN = 5       # swings a club needs before its spread is worked out
# Of the launch monitor's curve, the share that shows in where a swing finishes sideways: fitted to the
# swings GSPro logged positions for, which the sim's own flight curves less than shot_shape draws it.
DISPERSION_CURVE = 0.45
ELLIPSE_95 = math.sqrt(5.991)   # 2-D normal: 95% of shots inside this many SDs (chi-square, 2 dof)


def lateral_yards(start: float | None, axis: float | None, carry: float | None) -> float | None:
    """Where a full swing finished sideways of the target line, estimated from the launch monitor,
    yards (positive = right, as seen): the start line carried out plus its curve. For a swing GSPro
    logged no positions for (hole_replay.finish_laterals has the rest)."""
    if start is None or axis is None or not carry:
        return None
    return carry * math.sin(math.radians(start)) + (shot_shape.curve_yards(axis, carry, DISPERSION_CURVE) or 0.0)


def dispersion(pts: list[tuple[float, float]]) -> dict[str, Any]:
    """A club's landing spots (lateral, carry) and their spread: `side` is the median miss sideways,
    `width` how wide 95% of swings land, and `ellipse` the 95% ellipse (centre, semi-axes, and the
    tilt of its long axis from the carry line, degrees, positive leaning right with distance).
    Spread needs DISPERSION_MIN swings."""
    out: dict[str, Any] = {"landing": [[round(x, 1), round(y, 1)] for x, y in pts], "side": None,
                           "width": None, "ellipse": None}
    if not pts:
        return out
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    out["side"] = round(statistics.median(xs), 1)
    if len(pts) < DISPERSION_MIN:
        return out
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    n = len(pts) - 1
    sxx = sum((x - mx) ** 2 for x in xs) / n
    syy = sum((y - my) ** 2 for y in ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in pts) / n
    out["width"] = round(2 * 1.96 * math.sqrt(sxx))
    # Eigen-decomposition of the 2x2 covariance: the ellipse's axes.
    mid, diff = (sxx + syy) / 2, math.hypot((syy - sxx) / 2, sxy)
    long_, short = mid + diff, max(0.0, mid - diff)
    tilt = math.degrees(0.5 * math.atan2(2 * sxy, syy - sxx))
    out["ellipse"] = {"x": round(mx, 1), "y": round(my, 1), "long": round(ELLIPSE_95 * math.sqrt(long_), 1),
                      "short": round(ELLIPSE_95 * math.sqrt(short), 1), "tilt": round(tilt, 1)}
    return out


def miss_label(start: float | None, axis: float | None, handed: str | None = None) -> str | None:
    """A typical ball flight from start direction and spin axis (positive = right). The direction is
    as seen; the curve is named for the player's hand (a lefty's ball curving right is a draw)."""
    if start is None or axis is None:
        return None
    side = "straight" if abs(start) < 1.5 else f"{abs(start):.0f}° {'right' if start > 0 else 'left'}"
    if shot_shape.lefty(handed):
        axis = -axis
    curve = ("slice" if axis >= 9 else "fade" if axis >= 3 else "hook" if axis <= -9 else "draw" if axis <= -3
             else "no curve")
    return f"starts {side}, {curve}"


def recovery(holes: list[dict]) -> dict[str, list[dict]]:
    """Per player and trouble lie (rough, deep rough, sand): strokes played from it, how close they
    finished, and how often they found the green (or holed out). Greenside (≤50 yd) and longer apart."""
    return recovery_from(recovery_totals(holes))


def recovery_totals(holes: list[dict]) -> dict[tuple, list]:
    """recovery's counts per (player, lie, band): strokes, the sum of their distances (exact, as a
    numerator and denominator), strokes with a finish and the sum of those, greens found, holed."""
    acc: dict[tuple, list] = defaultdict(list)
    for h in holes:
        s = h["strokes"]
        for i, st in enumerate(s):
            lie = (st["lie_before"] or "").lower()
            if st["left_out"] or lie not in ("rough", "deep rough", "sand") or st["is_putt"] or st["to_pin_yards"] is None:
                continue
            nxt = s[i + 1] if i + 1 < len(s) else None
            holed = nxt is None and h["complete"]
            green = holed or (nxt is not None and (nxt["lie_before"] == "green" or nxt["outcome"] == "gimme"))
            if nxt is None and not holed:
                continue
            band = "greenside" if st["to_pin_yards"] <= AROUND_GREEN_YARDS else "long"
            acc[(h["player"], lie, band)].append((st["to_pin_yards"], st["finish_yards"], green, holed))
    out = {}
    for key, xs in acc.items():
        fin = [f for _, f, _, _ in xs if f is not None]
        out[key] = [len(xs), _exact(sum(Fraction(d) for d, _, _, _ in xs)), len(fin), _exact(sum(map(Fraction, fin))),
                    sum(g for _, _, g, _ in xs), sum(h for _, _, _, h in xs)]
    return out


def add_recovery(into: dict[tuple, list], part: dict[tuple, list]) -> None:
    """Add recovery_totals' counts into another's."""
    for key, (n, frm, n_fin, fin, green, holed) in part.items():
        got = into.setdefault(key, [0, (0, 1), 0, (0, 1), 0, 0])
        got[0] += n
        got[1] = _exact(Fraction(*got[1]) + Fraction(*frm))
        got[2] += n_fin
        got[3] = _exact(Fraction(*got[3]) + Fraction(*fin))
        got[4] += green
        got[5] += holed


def _exact(f: Fraction) -> tuple[int, int]:
    return f.numerator, f.denominator


def recovery_from(totals: dict[tuple, list]) -> dict[str, list[dict]]:
    """recovery() from recovery_totals' counts: a mean is its exact sum over its count, as statistics.mean."""
    out: dict[str, list[dict]] = defaultdict(list)
    for (player, lie, band), (n, frm, n_fin, fin, green, holed) in sorted(totals.items()):
        out[player].append({"lie": lie, "band": band, "n": n,
                            "avg_from": round(float(Fraction(*frm) / n)),
                            "avg_left": round(float(Fraction(*fin) / n_fin), 1) if n_fin else None,
                            "green_pct": round(100.0 * green / n, 1),
                            "holed": holed})
    return out



def exact_add(partials: list[float], x: float) -> None:
    """Add `x` to a sum kept exactly, as floats that don't overlap (as math.fsum keeps it): however
    the sum is split up and put back together (exact_merge), math.fsum(partials) is the same, the
    true sum correctly rounded."""
    i = 0
    for y in partials:
        if abs(x) < abs(y):
            x, y = y, x
        hi = x + y
        lo = y - (hi - x)
        if lo:
            partials[i] = lo
            i += 1
        x = hi
    partials[i:] = [x]


def exact_merge(into: list[float], part: Iterable[float]) -> None:
    for x in part:
        exact_add(into, x)
