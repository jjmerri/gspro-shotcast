"""Season analytics for the stats page: strokes gained, gapping, typical miss, recovery, trends,
course and hole difficulty, highlights, the shot log and broadcast transcript, and booth stats.

Everything reads the booth's season database (store.py's schema) through a read-only connection.
Shots are taken the way the booth's own stats take them: mulligans and unattributed strokes are left
out, and so are shared-ball rounds (scramble, alternate) wherever a stroke has to be one player's.
"""

import bisect
import json
import math
import re
import sqlite3
import statistics
from fractions import Fraction
from collections import Counter, defaultdict
from typing import Any, Callable

from . import night_totals as N
from . import shot_shape
from .round_record import reuse
from .store import DRIVE_SQL

SHARED_BALL = ("scramble", "alternate")

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
# An approach, as strokes gained has it: a full shot from beyond chipping range, not a par-4/5 tee
# shot (a par-3 tee shot is one); and not so far out it's a lay-up wood. A chip: off the green, close.
APPROACH_SQL = (f"is_putt = 0 AND to_pin_yards > {AROUND_GREEN_YARDS} AND to_pin_yards <= {APPROACH_MAX_YARDS}"
                " AND (COALESCE(lie_before, '') != 'tee' OR hole LIKE '%par 3%')")
CHIP_SQL = f"is_putt = 0 AND to_pin_yards <= {AROUND_GREEN_YARDS} AND COALESCE(lie_before, '') NOT IN ('green', 'tee')"
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


def _ctx(context: str | None, table: str = "") -> str:
    """A WHERE fragment for the round context (league / tournament / practice: neither), on `table`'s
    columns when given. A tournament round is never a league one."""
    t = f"{table}." if table else ""
    return {"league": f" AND {t}is_league = 1",
            "tournament": f" AND {t}is_league = 0 AND {t}is_tournament = 1",
            "practice": f" AND {t}is_league = 0 AND {t}is_tournament = 0"}.get(context or "", "")


# _ctx's filters, on a row
_IN_CONTEXT: dict[str, Callable[[Any], bool]] = {
    "league": lambda r: r["is_league"] == 1,
    "tournament": lambda r: r["is_league"] == 0 and r["is_tournament"] == 1,
    "practice": lambda r: r["is_league"] == 0 and r["is_tournament"] == 0,
}


def _par_of(label: str | None) -> int | None:
    m = re.search(r"par (\d)", label or "")
    return int(m.group(1)) if m else None


def _hole_no(label: str | None) -> int | None:
    m = re.search(r"Hole (\d+)", label or "")
    return int(m.group(1)) if m else None


def _hole_key(label: str | None) -> str:
    """'Hole 10, par 4, 257 yards' and 'par 4, 257 yards' are the same hole (the number is read late)."""
    return re.sub(r"^Hole \d+,\s*", "", label or "")


def holes_played(db: sqlite3.Connection, context: str | None = None, player: str | None = None) -> list[dict]:
    """Each player's strokes grouped into holes, in order: a hole starts at stroke 1, when the stroke
    number goes back, or when the hole changes. Mulligans, unattributed strokes and shared-ball formats
    are left out. A stroke a player left out of their stats stays in its hole (it was still played, and
    it ends the stroke before it) but gains no strokes and counts toward no recovery or chip-in. A hole is `complete` when it ends holed, or in a conceded tap-in."""
    where, sql = HOLE_ROWS_WHERE, HOLE_ROWS_SQL
    if player:
        rows = db.execute(sql.format(where + _ctx(context) + " AND player = ?"), (player,)).fetchall()
    else:
        # every context's rows read once a build (inside round_record.frozen), and each one's picked out
        rows = reuse(db, ("holes_played_rows",), lambda: db.execute(sql.format(where)).fetchall())
        if context in _IN_CONTEXT:
            rows = [r for r in rows if _IN_CONTEXT[context](r)]
    return holes_of(rows)


# holes_played's strokes (WHERE fragment, and the query it goes into)
HOLE_ROWS_WHERE = "attributed = 1 AND mulligan = 0 AND player IS NOT NULL AND player != 'unknown'" \
                  " AND (fmt IS NULL OR fmt NOT IN ('scramble', 'alternate'))"
HOLE_ROWS_SQL = "SELECT s.*, sess.date FROM shots s JOIN sessions sess ON sess.id = s.session_id WHERE {}" \
                " ORDER BY s.player, s.session_id, s.ts, s.id"


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
    and its stroke numbers can skip)."""
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
            if i + 1 < len(s):
                nxt = s[i + 1]
                radius = nxt["gimme_yards"] if nxt["gimme_yards"] is not None else session_radius.get(h["session_id"])
                end = _gimme_end(radius, usual_gimme_ft) if nxt["outcome"] == "gimme" else expected(
                    nxt["lie_before"] or ("green" if nxt["is_putt"] else None), nxt["to_pin_yards"])
                if end is None:
                    continue
                penalty = max(0, (nxt["shot_number"] or 0) - (st["shot_number"] or 0) - 1) \
                    if nxt["shot_number"] and st["shot_number"] else 0
            else:
                end, penalty = 0.0, 0
            out.append({"player": h["player"], "session_id": h["session_id"], "date": h["date"],
                        "category": _category(st, h["par"]), "sg": start - end - 1 - penalty, "shot_id": st["id"]})
    return out


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
        N.exact_add(c[0], s["sg"])
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
            N.exact_merge(c[0], partials)
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


def gapping(db: sqlite3.Connection, player: str, context: str | None = None,
            finish: dict[int, float] | None = None) -> list[dict]:
    """Per club, carry and launch numbers from the launch monitor, over full swings only: a chip or a
    knock-down (ball speed well under the club's best) would drag a wedge's carry down. Every real swing
    counts, a mulligan's too. Medians and the middle half, so one mishit doesn't move the numbers.
    Only swings from the tee or fairway count (see GAPPING_CLEAN_LIES); a club with too few of those
    uses every lie, and says so (`any_lie`).
    A swing with no raw carry (a launch monitor that sends none, and no CARRY (raw) tile on screen) uses
    GSPro's game carry, slope and weather in it: `game` counts those.
    `finish`: hole_replay.finish_laterals, worked out here when not given."""
    if finish is None:
        from .hole_replay import finish_laterals   # hole_replay imports stats_web, which imports this
        finish = finish_laterals(db)
    raw = "COALESCE(NULLIF(carry_raw_tile_yards, 0), NULLIF(carry_raw, 0))"
    rows = db.execute(
        f"SELECT id, club, COALESCE({raw}, carry_game_yards) AS carry, {raw} IS NULL AS game, total_yards, ball_speed,"
        " club_speed, launch_angle, total_spin, spin_axis, start_direction, handed, lie_before FROM shots WHERE player = ?"
        " AND attributed = 1 AND is_putt = 0 AND left_out = 0 AND club IS NOT NULL AND club != 'PT'"
        f" AND COALESCE({raw}, carry_game_yards) > 0" + _ctx(context), (player,)).fetchall()
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


def trends(db: sqlite3.Connection, holes: list[dict], context: str | None = None,
           usual_gimme_ft: float | None = None, sg: list[dict] | None = None, where: str = "") -> dict[str, list[dict]]:
    """Per player, one point per night: score to par per nine, putts per hole, fairways and greens hit,
    and strokes gained per nine. From individual-ball rounds. `sg`: strokes_gained(holes,
    usual_gimme_ft), when already worked out. `where`: a fragment limiting the cards (their sessions,
    to go with `holes`')."""
    nights: dict[tuple, dict] = {}
    for r in db.execute("SELECT pr.*, s.date FROM player_rounds pr JOIN sessions s ON s.id = pr.session_id"
                        " WHERE (fmt IS NULL OR fmt NOT IN ('scramble', 'alternate'))" + _ctx(context, "pr") + where
                        + " ORDER BY pr.ended_at"):
        try:
            scores = {int(k): v for k, v in json.loads(r["scores"] or "{}").items()}
            pars = {int(k): v for k, v in json.loads(r["hole_pars"] or "{}").items()}
        except (TypeError, ValueError):
            continue
        played = [h for h in scores if h in pars]
        n = nights.setdefault((r["player"], r["date"]), {"date": r["date"], "holes": 0, "to_par": 0, "putts": 0,
                                                         "putt_holes": 0, "fir": [0, 0], "gir": [0, 0]})
        n["holes"] += len(played)
        n["to_par"] += sum(scores[h] - pars[h] for h in played)
        n["putts"] += r["putts"] or 0
        n["putt_holes"] += r["putt_holes"] or 0
        n["fir"][0] += r["fir_hit"] or 0
        n["fir"][1] += r["fir_tries"] or 0
        n["gir"][0] += r["gir_hit"] or 0
        n["gir"][1] += r["gir_tries"] or 0
    sg_night: dict[tuple, float] = defaultdict(float)
    for s in strokes_gained(holes, usual_gimme_ft) if sg is None else sg:
        sg_night[(s["player"], s["date"])] += s["sg"]
    holes_night: Counter = Counter((h["player"], h["date"]) for h in holes if h["complete"])
    out: dict[str, list[dict]] = defaultdict(list)
    for (player, date), n in sorted(nights.items(), key=lambda kv: kv[0][1]):
        if not n["holes"]:
            continue
        hn = holes_night.get((player, date), 0)
        out[player].append({
            "date": date, "holes": n["holes"],
            "to_par_9": round(n["to_par"] * 9 / n["holes"], 1),
            "putts_per_hole": round(n["putts"] / n["putt_holes"], 2) if n["putt_holes"] else None,
            "fir_pct": round(100.0 * n["fir"][0] / n["fir"][1]) if n["fir"][1] else None,
            "gir_pct": round(100.0 * n["gir"][0] / n["gir"][1]) if n["gir"][1] else None,
            "sg_9": round(sg_night[(player, date)] * 9 / hn, 2) if hn else None,
        })
    return out


def courses(db: sqlite3.Connection, context: str | None = None, player: str | None = None) -> list[dict]:
    """Per course: rounds, players, the best nine, and every hole's scoring average from the individual
    cards, so the hardest and easiest holes stand out. With `player`, from that player's cards only."""
    by_course: dict[str, dict] = {}
    who = " AND pr.player = ?" if player else ""
    for r in db.execute("SELECT pr.*, s.date FROM player_rounds pr JOIN sessions s ON s.id = pr.session_id"
                        " WHERE course IS NOT NULL AND (fmt IS NULL OR fmt NOT IN ('scramble', 'alternate'))"
                        + _ctx(context, "pr") + who + " ORDER BY pr.ended_at",
                        (player,) if player else ()):
        try:
            scores = {int(k): v for k, v in json.loads(r["scores"] or "{}").items() if isinstance(v, int)}
            pars = {int(k): v for k, v in json.loads(r["hole_pars"] or "{}").items() if isinstance(v, int)}
        except (TypeError, ValueError):
            continue
        c = by_course.setdefault(r["course"], {"course": r["course"], "rounds": 0, "players": set(), "holes": {},
                                               "best": None, "last_played": None})
        c["players"].add(r["player"])
        c["last_played"] = max(c["last_played"] or "", r["date"])
        played = [h for h in sorted(scores) if h in pars]
        for h in played:
            e = c["holes"].setdefault(h, {"hole": h, "par": pars[h], "scores": []})
            e["scores"].append(scores[h])
        if len(played) >= 9:   # a round of a nine or more (a one-hole 19th's birdie isn't a -9 nine)
            c["rounds"] += 1
            to_par = sum(scores[h] - pars[h] for h in played)
            # A round quit part-way through is ranked by the nine it finished; prorating the odd
            # holes onto a nine would make the best a score nobody shot. Older rows have no stored nine.
            stored = r["nine_to_par"] if "nine_to_par" in r.keys() else None
            per9 = stored if stored is not None else (to_par * 9 / len(played) if played else None)
            if per9 is not None and (c["best"] is None or per9 < c["best"]["to_par_9"]):
                c["best"] = {"player": r["player"], "date": r["date"], "to_par": to_par, "holes": len(played),
                             "to_par_9": round(per9, 1)}
    out = []
    for c in by_course.values():
        holes = []
        for e in sorted(c["holes"].values(), key=lambda e: e["hole"]):
            s = e["scores"]
            holes.append({"hole": e["hole"], "par": e["par"], "n": len(s), "avg": round(statistics.mean(s), 2),
                          "to_par": round(statistics.mean(s) - e["par"], 2),
                          "birdie_pct": round(100.0 * sum(x < e["par"] for x in s) / len(s)),
                          "bogey_plus_pct": round(100.0 * sum(x > e["par"] for x in s) / len(s)),
                          "best": min(s), "worst": max(s)})
        ranked = sorted((h for h in holes if h["n"] >= 1), key=lambda h: h["to_par"])
        out.append({"course": c["course"], "rounds": c["rounds"], "players": sorted(c["players"]),
                    "last_played": c["last_played"], "best": c["best"], "holes": holes,
                    "easiest": ranked[0]["hole"] if ranked else None, "hardest": ranked[-1]["hole"] if ranked else None})
    out.sort(key=lambda c: (-c["rounds"], c["course"]))
    return out


def _to_par_name(d: int) -> str:
    return {-3: "albatross", -2: "eagle", -1: "birdie", 1: "bogey", 2: "double"}.get(d, f"+{d}" if d > 0 else f"{d}")


def firsts(rows, starts: dict[str, str | None], k: int, date: str = "date") -> dict[tuple, list[dict]]:
    """The first `k` of `rows` (in their order) in each of the date ranges `starts` (key: first night
    YYYY-MM-DD, None for all time), for everyone (None, key) and for each player (player, key)."""
    out: dict[tuple, list[dict]] = {}
    for r in rows:
        r = dict(r)
        d = r.pop("_date") if date == "_date" else r[date]
        for key, since in starts.items():
            if since is not None and (d or "") < since:
                continue
            for who in (None, r["player"]):
                got = out.setdefault((who, key), [])
                if len(got) < k:
                    got.append(r)
    return out


def highlights(db: sqlite3.Connection, holes: list[dict], context: str | None = None,
               player: str | None = None, since: str | None = None) -> dict[str, Any]:
    """The season's moments: aces, eagles, chip-ins, birdie streaks, bounce-backs and blow-ups from the
    cards; and top-five lists of drives, approaches, chips and putts holed from the shots. With
    `player`, that player's own; with `since` (YYYY-MM-DD), only nights from that date on."""
    return highlights_table(db, holes, context, {"": since})(player, "")


def highlights_table(db: sqlite3.Connection, holes: list[dict], context: str | None,
                     starts: dict[str, str | None]) -> Callable[[str | None, str], dict[str, Any]]:
    """highlights() for every player (and everyone, None) and each of the date ranges `starts` (key:
    first night, None for all time), from one pass over the shots: returns pick(player, key)."""
    return highlights_pick(*highlight_rows(db, holes, context), starts)


TOPS = ("drives", "approaches", "chips", "putts")


def highlight_rows(db: sqlite3.Connection, holes: list[dict], context: str | None,
                   where: str = "") -> tuple[list[dict], list[dict], list[dict], list[dict], dict[str, list[dict]]]:
    """highlights_table's moments, streaks, bounce-backs and blow-ups (in the order they happened), and
    each top list's shots (best first, each with its sort key, `_k`). `where`: a fragment limiting them
    to some sessions (`holes` being theirs)."""
    moments: list[dict] = []
    streaks: list[dict] = []
    bounce: list[dict] = []
    blowups: list[dict] = []
    # A card moment's stroke (its id and time, for the page's links and video): the player's hole in
    # that session, the last one played before the round ended.
    by_hole: dict[tuple, list[dict]] = {}
    for h in holes:
        by_hole.setdefault((h["player"], h["session_id"], h["course"], h["hole"]), []).append(h)

    def at(r, hole: int, last: bool = False) -> dict:
        hs = [h for h in by_hole.get((r["player"], r["session_id"], r["course"], hole), [])
              if h["strokes"][0]["ts"] <= r["ended_at"] + 60]
        if not hs:
            return {}
        st = hs[-1]["strokes"][-1 if last else 0]
        return {"id": st["id"], "ts": st["ts"]}

    for r in db.execute("SELECT pr.*, s.date FROM player_rounds pr JOIN sessions s ON s.id = pr.session_id"
                        " WHERE (fmt IS NULL OR fmt NOT IN ('scramble', 'alternate'))"
                        + _ctx(context, "pr") + where + " ORDER BY pr.ended_at, pr.id"):
        try:
            scores = {int(k): v for k, v in json.loads(r["scores"] or "{}").items() if isinstance(v, int)}
            pars = {int(k): v for k, v in json.loads(r["hole_pars"] or "{}").items() if isinstance(v, int)}
        except (TypeError, ValueError):
            continue
        played = [h for h in sorted(scores) if h in pars]
        base = {"player": r["player"], "date": r["date"], "course": r["course"]}
        run: list[int] = []
        for i, h in enumerate(played):
            d = scores[h] - pars[h]
            if scores[h] == 1:
                moments.append({**base, **at(r, h, True), "hole": h, "kind": "ace", "text": f"Hole-in-one on the par-{pars[h]} {h}th"})
            elif d <= -2:
                moments.append({**base, **at(r, h, True), "hole": h, "kind": "eagle",
                                "text": f"{'Albatross' if d <= -3 else 'Eagle'} {scores[h]} on the par-{pars[h]} {h}th"})
            if d >= 3:
                blowups.append({**base, **at(r, h), "hole": h, "score": scores[h], "par": pars[h], "over": d})
            if i and played[i - 1] == h - 1 and (prev := scores[h - 1] - pars[h - 1]) >= 1 and d <= -1:
                bounce.append({**base, **at(r, h), "holes": [h - 1, h], "from": _to_par_name(prev), "to": _to_par_name(d)})
            if d <= -1 and (not run or run[-1] == h - 1):
                run.append(h)
            elif d <= -1:
                run = [h]
            else:
                if len(run) >= 2:
                    streaks.append({**base, **at(r, run[0]), "holes": run[:], "length": len(run)})
                run = []
        if len(run) >= 2:
            streaks.append({**base, **at(r, run[0]), "holes": run[:], "length": len(run)})
    # Chip-ins: a full shot from off the green (not the tee) that was the hole's last stroke.
    for h in holes:
        last = h["strokes"][-1]
        if h["complete"] and last["outcome"] == "holed" and not last["is_putt"] and not last["left_out"] and last["lie_before"] not in ("tee", "green"):
            yd = last["to_pin_yards"]
            moments.append({"player": h["player"], "date": h["date"], "course": h["course"], "hole": h["hole"],
                            "id": last["id"], "ts": last["ts"], "kind": "holed out", "text": f"Holed out from {round(yd) if yd else '?'} yd"
                            f" ({last['lie_before'] or 'off the green'}, {last['club'] or '?'})"})
    base = " AND player IS NOT NULL AND player != 'unknown' AND attributed = 1 AND mulligan = 0 AND left_out = 0" \
           + _ctx(context) + where
    drive = "COALESCE(total_yards, carry_game_yards, carry_raw)"
    j = " FROM shots JOIN sessions ON sessions.id = shots.session_id"
    tops = {}
    for name, (sql, k) in {
        "drives": (f"SELECT shots.id, ts, player, ROUND({drive}) AS v, course, hole, date{j} WHERE {DRIVE_SQL}"
                   f" AND {drive} IS NOT NULL{base} ORDER BY v DESC, ts, shots.id", lambda r: (-r["v"], r["ts"], r["id"])),
        "approaches": (f"SELECT shots.id, ts, player, ROUND(finish_yards * 3) AS v, outcome, club, ROUND(to_pin_yards) AS from_yd,"
                       f" course, hole, date, finish_yards AS _f{j} WHERE {APPROACH_SQL} AND finish_yards IS NOT NULL{base}"
                       f" ORDER BY finish_yards, ts, shots.id", lambda r: (r["_f"], r["ts"], r["id"])),
        "chips": (f"SELECT shots.id, ts, player, ROUND(finish_yards * 3) AS v, outcome, club, ROUND(to_pin_yards) AS from_yd,"
                  f" lie_before AS lie, course, hole, date, finish_yards AS _f{j} WHERE {CHIP_SQL} AND finish_yards IS NOT NULL{base}"
                  f" ORDER BY finish_yards, ts, shots.id", lambda r: (r["_f"], r["ts"], r["id"])),
        "putts": (f"SELECT shots.id, ts, player, ROUND(to_pin_yards * 3) AS v, course, hole, date, to_pin_yards AS _f{j}"
                  f" WHERE is_putt = 1 AND outcome = 'holed' AND to_pin_yards IS NOT NULL{base}"
                  f" ORDER BY to_pin_yards DESC, ts, shots.id", lambda r: (-r["_f"], r["ts"], r["id"])),
    }.items():
        tops[name] = []
        for r in db.execute(sql):
            row = {c: r[c] for c in r.keys() if c != "_f"}
            row["_k"] = k(r)
            tops[name].append(row)
    return moments, streaks, bounce, blowups, tops


def highlights_pick(moments: list[dict], streaks: list[dict], bounce: list[dict], blowups: list[dict],
                    tops: dict[str, list[dict]],
                    starts: dict[str, str | None]) -> Callable[[str | None, str], dict[str, Any]]:
    """highlights_table's pick(player, key), from highlight_rows' lists (tops' rows in any order)."""
    tops = {name: firsts(({c: v for c, v in r.items() if c != "_k"} for r in sorted(rows, key=lambda r: r["_k"])),
                         starts, 5)
            for name, rows in tops.items()}

    def pick(player: str | None, key: str) -> dict[str, Any]:
        since = starts[key]

        def mine(xs: list[dict]) -> list[dict]:
            return [dict(x) for x in xs if (player is None or x["player"] == player)
                    and (since is None or (x["date"] or "") >= since)]
        return {
            "moments": sorted(mine(moments), key=lambda m: m["date"] or "", reverse=True),
            "streaks": sorted(mine(streaks), key=lambda s: (-s["length"], s["date"] or ""))[:10],
            "bounce_backs": sorted(mine(bounce), key=lambda b: b["date"] or "", reverse=True)[:10],
            "blowups": sorted(mine(blowups), key=lambda b: (-b["over"], b["date"] or ""))[:5],
            **{name: [dict(r) for r in got.get((player, key), [])] for name, got in tops.items()},
        }
    return pick


_STOP = set("a an and the of to in on at for is it's its it that this with he his him you your be was are as"
            " but so just from off up out there what all i we our by or not no have has into than".split())


def booth(db: sqlite3.Connection, names: dict[str, str] | None = None) -> dict[str, Any]:
    """The commentators: lines and words by kind, a few signature phrases, and putt-call accuracy by night."""
    names = names or {}
    on_air = {n.lower() for n in names.values()} | {"nigel", "duke"}
    per: dict[str, dict] = {}
    grams: dict[str, Counter] = defaultdict(Counter)
    for r in db.execute("SELECT speaker, kind, text FROM lines"):
        p = per.setdefault(r["speaker"], {"key": r["speaker"], "name": names.get(r["speaker"], r["speaker"]),
                                          "lines": 0, "words": 0, "kinds": Counter()})
        words = re.findall(r"[a-z0-9']+", r["text"].lower().replace("\u2019", "'"))
        p["lines"] += 1
        p["words"] += len(words)
        p["kinds"][r["kind"]] += 1
        # a phrase: three words, two of them not stop words, none a number or a commentator's name
        usable = [(w.isalpha() or "'" in w) and w not in on_air for w in words]
        stop = [w in _STOP for w in words]
        mine = grams[r["speaker"]]
        for i in range(len(words) - 2):
            if usable[i] and usable[i + 1] and usable[i + 2] and stop[i] + stop[i + 1] + stop[i + 2] <= 1:
                mine[f"{words[i]} {words[i + 1]} {words[i + 2]}"] += 1
    sessions = {r[0]: r[1] for r in db.execute("SELECT id, date FROM sessions")}
    rounds_by_date = Counter(r[0] for r in db.execute("SELECT DISTINCT date, session_id, course FROM rounds"))
    lines_by_date = Counter(sessions.get(r[0]) for r in db.execute("SELECT session_id FROM lines"))
    calls = [{"date": r["date"], "who": names.get(r["commentator"], r["commentator"]), "calls": r["n"], "right": r["hits"] or 0}
             for r in db.execute("SELECT s.date, p.commentator, COUNT(*) n, SUM(correct) hits FROM predictions p"
                                 " JOIN sessions s ON s.id = p.session_id WHERE correct IS NOT NULL"
                                 " GROUP BY s.date, p.commentator ORDER BY s.date")]
    return {
        "commentators": [{**{k: v for k, v in p.items() if k != "kinds"}, "kinds": dict(p["kinds"]),
                          "words_per_line": round(p["words"] / p["lines"], 1) if p["lines"] else None,
                          "phrases": [g for g, n in grams[k].most_common(8) if n >= 2]}
                         for k, p in sorted(per.items())],
        "nights": [{"date": d, "lines": n, "rounds": rounds_by_date.get(d, 0)}
                   for d, n in sorted((d, n) for d, n in lines_by_date.items() if d)],
        "calls": calls,
    }


def shot_log(db: sqlite3.Connection, context: str | None = None, *, player: str | None = None,
             date: str | None = None, course: str | None = None, club: str | None = None,
             lie: str | None = None, limit: int = 500, sessions: list[int] | None = None) -> list[dict]:
    """Shots, newest first, each with what the booth said about it. `sessions`: only theirs."""
    where, args = ["1=1"], []
    if sessions is not None:
        where.append("shots.session_id IN (SELECT value FROM json_each(?))")
        args.append(json.dumps(list(sessions)))
    for col, val in (("player", player), ("date", date), ("course", course), ("club", club), ("lie_before", lie)):
        if val:
            where.append(f"{'sessions.' if col == 'date' else 'shots.'}{col} = ?")
            args.append(val)
    rows = db.execute(
        "SELECT shots.id, shots.ts, sessions.date, shots.session_id, shots.player, shots.course, shots.hole, shot_number, club,"
        " is_putt, lie_before, lie_after, ROUND(to_pin_yards, 1) AS to_pin, ROUND(finish_yards, 1) AS finish,"
        " ROUND(COALESCE(carry_raw_tile_yards, carry_raw)) AS carry, ROUND(total_yards) AS total, ball_speed,"
        " club_speed, launch_angle, total_spin, spin_axis, start_direction, outcome, mulligan, left_out, fmt,"
        " handed, COALESCE(carry_game_yards, carry_raw_tile_yards, carry_raw) AS flight"
        " FROM shots JOIN sessions ON sessions.id = shots.session_id WHERE " + " AND ".join(where)
        + _ctx(context, "shots") + " ORDER BY shots.ts DESC, shots.id DESC LIMIT ?",
        (*args, limit)).fetchall()
    out = [dict(r) for r in rows]
    for r in out:
        r["shape"] = shot_shape.shape(r["start_direction"], r["spin_axis"], r.pop("flight"), bool(r["is_putt"]), r.pop("handed"))
    if out:
        ids = [r["id"] for r in out]
        said: dict[int, list] = defaultdict(list)
        for chunk in range(0, len(ids), 500):
            part = ids[chunk:chunk + 500]
            for ln in db.execute(f"SELECT shot_id, speaker, kind, text FROM lines WHERE shot_id IN ({','.join('?' * len(part))})"
                                 " ORDER BY ts, id", part):
                said[ln["shot_id"]].append({"speaker": ln["speaker"], "kind": ln["kind"], "text": ln["text"]})
        for r in out:
            r["lines"] = said.get(r["id"], [])
    return out


def broadcasts(db: sqlite3.Connection) -> list[dict]:
    """Booth sessions that said anything, newest first, with the courses and players in them."""
    courses: dict[int, list[str]] = {}
    players: dict[int, list[str]] = {}
    # (in the order each first appears in the session, as the per-session lookups gave them)
    for sid, course in db.execute("SELECT session_id, course FROM shots WHERE course IS NOT NULL"
                                  " GROUP BY session_id, course ORDER BY session_id, MIN(id)"):
        courses.setdefault(sid, []).append(course)
    for sid, player in db.execute("SELECT session_id, player FROM shots WHERE player IS NOT NULL AND player != 'unknown'"
                                  " GROUP BY session_id, player ORDER BY session_id, MIN(id)"):
        players.setdefault(sid, []).append(player)
    out = []
    for r in db.execute("SELECT s.id, s.date, COUNT(l.id) AS lines FROM sessions s JOIN lines l ON l.session_id = s.id"
                        " WHERE EXISTS (SELECT 1 FROM shots WHERE shots.session_id = s.id)"
                        " GROUP BY s.id ORDER BY s.started_at DESC"):
        out.append({"id": r["id"], "date": r["date"], "lines": r["lines"],
                    "courses": courses.get(r["id"], []), "players": players.get(r["id"], [])})
    return out


def transcript(db: sqlite3.Connection, session_id: int, names: dict[str, str] | None = None) -> list[dict]:
    """A session's broadcast in order: every line, with the shot it was about."""
    names = names or {}
    out = []
    for r in db.execute("SELECT l.ts, l.speaker, l.kind, l.text, s.player, s.hole, s.club, s.shot_number FROM lines l"
                        " LEFT JOIN shots s ON s.id = l.shot_id WHERE l.session_id = ? ORDER BY l.ts, l.id", (session_id,)):
        d = dict(r)
        d["name"] = names.get(r["speaker"], r["speaker"])
        out.append(d)
    return out
