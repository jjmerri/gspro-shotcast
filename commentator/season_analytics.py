"""Season analytics for the stats page: strokes gained, gapping, typical miss, recovery, trends,
course and hole difficulty, highlights, the shot log and broadcast transcript, and booth stats.

Everything reads the booth's season database (store.py's schema) through a read-only connection.
Shots are taken the way the booth's own stats take them: mulligans and unattributed strokes are left
out, and so are shared-ball rounds (scramble, alternate) wherever a stroke has to be one player's.
"""

import bisect
import json
import re
import sqlite3
import statistics
from collections import Counter, defaultdict
from typing import Any

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


def _ctx(context: str | None) -> str:
    return {"league": " AND is_league = 1", "practice": " AND is_league = 0"}.get(context or "", "")


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
    are left out. A hole is `complete` when it ends holed, or in a conceded tap-in."""
    where = "attributed = 1 AND mulligan = 0 AND player IS NOT NULL AND player != 'unknown'" \
            " AND (fmt IS NULL OR fmt NOT IN ('scramble', 'alternate'))" + _ctx(context)
    args: tuple = ()
    if player:
        where += " AND player = ?"
        args = (player,)
    rows = db.execute(f"SELECT s.*, sess.date FROM shots s JOIN sessions sess ON sess.id = s.session_id"
                      f" WHERE {where} ORDER BY s.player, s.session_id, s.ts, s.id", args).fetchall()
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


def strokes_gained(holes: list[dict]) -> list[dict]:
    """Strokes gained on every stroke whose start and finish are known: expected strokes before, minus
    expected after, minus one (and minus any penalty strokes, seen as a jump in the stroke number). A
    conceded tap-in is a stroke from a known spot (expected 1.0), so it's 0 gained and only ends the
    stroke before it. Only finished holes count, so the strokes and the holes they're divided by
    match (a hole left unfinished is often the messy one, and its stroke numbers can skip)."""
    out = []
    for h in holes:
        if not h["complete"]:
            continue
        s = h["strokes"]
        for i, st in enumerate(s):
            if st["outcome"] == "gimme":
                continue
            start = expected(st["lie_before"] or ("green" if st["is_putt"] else None), st["to_pin_yards"])
            if start is None:
                continue
            if i + 1 < len(s):
                nxt = s[i + 1]
                end = 1.0 if nxt["outcome"] == "gimme" else expected(
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


def sg_summary(holes: list[dict]) -> dict[str, dict]:
    """Per player: strokes gained per category and in total, per nine holes played, against the tour."""
    sg = strokes_gained(holes)
    nines: dict[str, float] = defaultdict(float)
    for h in holes:
        if h["complete"]:
            nines[h["player"]] += 1 / 9
    totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    counts: dict[str, Counter] = defaultdict(Counter)
    for s in sg:
        totals[s["player"]][s["category"]] += s["sg"]
        counts[s["player"]][s["category"]] += 1
    out = {}
    for player, cats in totals.items():
        n = nines.get(player) or 0
        if not n:
            continue
        per = {c: round(cats.get(c, 0.0) / n, 2) for c in SG_CATEGORIES}
        per["total"] = round(sum(per.values()), 2)
        out[player] = {"per_nine": per, "strokes": dict(counts[player]), "holes": round(n * 9)}
    return out


FULL_SWING = 0.8   # a swing counts toward gapping when its ball speed is this share of the club's top speed


def gapping(db: sqlite3.Connection, player: str, context: str | None = None) -> list[dict]:
    """Per club, carry and launch numbers from the launch monitor, over full swings only: a chip or a
    knock-down (ball speed well under the club's best) would drag a wedge's carry down. Every real swing
    counts, a mulligan's too. Medians and the middle half, so one mishit doesn't move the numbers."""
    rows = db.execute(
        "SELECT club, COALESCE(carry_raw_tile_yards, carry_raw) AS carry, total_yards, ball_speed, club_speed,"
        " launch_angle, total_spin, spin_axis, start_direction FROM shots WHERE player = ? AND attributed = 1"
        " AND is_putt = 0 AND club IS NOT NULL AND club != 'PT' AND COALESCE(carry_raw_tile_yards, carry_raw) > 0"
        + _ctx(context), (player,)).fetchall()
    by_club: dict[str, list] = defaultdict(list)
    for r in rows:
        by_club[r["club"]].append(r)
    out = []
    for club, every in by_club.items():
        speeds = sorted(r["ball_speed"] for r in every if r["ball_speed"])
        top = speeds[int(0.9 * (len(speeds) - 1))] if speeds else None
        rs = [r for r in every if top is None or (r["ball_speed"] or 0) >= FULL_SWING * top]

        def med(col):
            vals = [r[col] for r in rs if r[col] is not None]
            return round(statistics.median(vals), 1) if vals else None
        carries = sorted(r["carry"] for r in rs)
        q = statistics.quantiles(carries, n=4) if len(carries) >= 2 else [carries[0]] * 3
        smash = [r["ball_speed"] / r["club_speed"] for r in rs if r["ball_speed"] and r["club_speed"]]
        out.append({"club": club, "n": len(rs), "swings": len(every), "carry": med("carry"), "carry_lo": round(q[0]), "carry_hi": round(q[2]),
                    "carry_max": round(carries[-1]), "total": med("total_yards"), "ball_speed": med("ball_speed"),
                    "club_speed": med("club_speed"), "smash": round(statistics.median(smash), 2) if smash else None,
                    "launch": med("launch_angle"), "spin": med("total_spin"), "spin_axis": med("spin_axis"),
                    "start_dir": med("start_direction"), "miss": miss_label(med("start_direction"), med("spin_axis"))})
    out.sort(key=lambda c: -(c["carry"] or 0))
    for c in out:
        c["gap_to_next"] = None
    for a, b in zip(out, out[1:]):
        a["gap_to_next"] = round(a["carry"] - b["carry"]) if a["carry"] is not None and b["carry"] is not None else None
    return out


def miss_label(start: float | None, axis: float | None) -> str | None:
    """A right-hander's typical ball flight from start direction and spin axis (positive = right)."""
    if start is None or axis is None:
        return None
    side = "straight" if abs(start) < 1.5 else f"{abs(start):.0f}° {'right' if start > 0 else 'left'}"
    curve = ("slice" if axis >= 9 else "fade" if axis >= 3 else "hook" if axis <= -9 else "draw" if axis <= -3
             else "no curve")
    return f"starts {side}, {curve}"


def recovery(holes: list[dict]) -> dict[str, list[dict]]:
    """Per player and trouble lie (rough, deep rough, sand): strokes played from it, how close they
    finished, and how often they found the green (or holed out). Greenside (≤50 yd) and longer apart."""
    acc: dict[tuple, list] = defaultdict(list)
    for h in holes:
        s = h["strokes"]
        for i, st in enumerate(s):
            lie = (st["lie_before"] or "").lower()
            if lie not in ("rough", "deep rough", "sand") or st["is_putt"] or st["to_pin_yards"] is None:
                continue
            nxt = s[i + 1] if i + 1 < len(s) else None
            holed = nxt is None and h["complete"]
            green = holed or (nxt is not None and (nxt["lie_before"] == "green" or nxt["outcome"] == "gimme"))
            if nxt is None and not holed:
                continue
            band = "greenside" if st["to_pin_yards"] <= AROUND_GREEN_YARDS else "long"
            acc[(h["player"], lie, band)].append((st["to_pin_yards"], st["finish_yards"], green, holed))
    out: dict[str, list[dict]] = defaultdict(list)
    for (player, lie, band), xs in sorted(acc.items()):
        fin = [f for _, f, _, _ in xs if f is not None]
        out[player].append({"lie": lie, "band": band, "n": len(xs),
                            "avg_from": round(statistics.mean(d for d, _, _, _ in xs)),
                            "avg_left": round(statistics.mean(fin), 1) if fin else None,
                            "green_pct": round(100.0 * sum(g for _, _, g, _ in xs) / len(xs), 1),
                            "holed": sum(h for _, _, _, h in xs)})
    return out


def trends(db: sqlite3.Connection, holes: list[dict], context: str | None = None) -> dict[str, list[dict]]:
    """Per player, one point per night: score to par per nine, putts per hole, fairways and greens hit,
    and strokes gained per nine. From individual-ball rounds."""
    nights: dict[tuple, dict] = {}
    for r in db.execute("SELECT pr.*, s.date FROM player_rounds pr JOIN sessions s ON s.id = pr.session_id"
                        " WHERE (fmt IS NULL OR fmt NOT IN ('scramble', 'alternate'))" + _ctx(context).replace(
                            "is_league", "pr.is_league") + " ORDER BY pr.ended_at"):
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
    for s in strokes_gained(holes):
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
                        + _ctx(context).replace("is_league", "pr.is_league") + who + " ORDER BY pr.ended_at",
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
        if len(played) >= 9 or r["complete"]:
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


def highlights(db: sqlite3.Connection, holes: list[dict], context: str | None = None,
               player: str | None = None) -> dict[str, Any]:
    """The season's moments: aces, eagles, chip-ins, birdie streaks, bounce-backs and blow-ups from the
    cards; and top-five lists of drives, approaches, chips and putts holed from the shots. With
    `player`, that player's own."""
    moments: list[dict] = []
    streaks: list[dict] = []
    bounce: Counter = Counter()
    blowups: list[dict] = []
    args = (player,) if player else ()
    if player:
        holes = [h for h in holes if h["player"] == player]
    for r in db.execute("SELECT pr.*, s.date FROM player_rounds pr JOIN sessions s ON s.id = pr.session_id"
                        " WHERE (fmt IS NULL OR fmt NOT IN ('scramble', 'alternate'))"
                        + _ctx(context).replace("is_league", "pr.is_league")
                        + (" AND pr.player = ?" if player else "") + " ORDER BY pr.ended_at", args):
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
                moments.append({**base, "hole": h, "kind": "ace", "text": f"Hole-in-one on the par-{pars[h]} {h}th"})
            elif d <= -2:
                moments.append({**base, "hole": h, "kind": "eagle",
                                "text": f"{'Albatross' if d <= -3 else 'Eagle'} {scores[h]} on the par-{pars[h]} {h}th"})
            if d >= 3:
                blowups.append({**base, "hole": h, "score": scores[h], "par": pars[h], "over": d})
            if i and played[i - 1] == h - 1 and scores[h - 1] - pars[h - 1] >= 1 and d <= -1:
                bounce[r["player"]] += 1
            if d <= -1 and (not run or run[-1] == h - 1):
                run.append(h)
            elif d <= -1:
                run = [h]
            else:
                if len(run) >= 2:
                    streaks.append({**base, "holes": run[:], "length": len(run)})
                run = []
        if len(run) >= 2:
            streaks.append({**base, "holes": run[:], "length": len(run)})
    # Chip-ins: a full shot from off the green (not the tee) that was the hole's last stroke.
    for h in holes:
        last = h["strokes"][-1]
        if h["complete"] and last["outcome"] == "holed" and not last["is_putt"] and last["lie_before"] not in ("tee", "green"):
            yd = last["to_pin_yards"]
            moments.append({"player": h["player"], "date": h["date"], "course": h["course"], "hole": h["hole"],
                            "kind": "holed out", "text": f"Holed out from {round(yd) if yd else '?'} yd"
                            f" ({last['lie_before'] or 'off the green'}, {last['club'] or '?'})"})
    moments.sort(key=lambda m: m["date"] or "", reverse=True)
    base = (" AND player IS NOT NULL AND player != 'unknown' AND attributed = 1 AND mulligan = 0" + _ctx(context)
            + (" AND player = ?" if player else ""))
    drive = "COALESCE(total_yards, carry_game_yards, carry_raw)"

    def top(sql: str) -> list[dict]:
        return [dict(r) for r in db.execute(sql, args)]

    j = " FROM shots JOIN sessions ON sessions.id = shots.session_id"
    return {
        "moments": moments,
        "streaks": sorted(streaks, key=lambda s: (-s["length"], s["date"] or ""))[:10],
        "bounce_backs": [{"player": p, "count": n} for p, n in bounce.most_common()],
        "blowups": sorted(blowups, key=lambda b: (-b["over"], b["date"] or ""))[:5],
        "drives": top(f"SELECT player, ROUND({drive}) AS v, course, hole, date{j} WHERE club = 'DR' AND is_putt = 0"
                      f" AND (shot_number = 1 OR lie_before = 'tee') AND {drive} IS NOT NULL{base} ORDER BY v DESC LIMIT 5"),
        "approaches": top(f"SELECT player, ROUND(finish_yards * 3) AS v, outcome, club, ROUND(to_pin_yards) AS from_yd, course,"
                          f" hole, date{j} WHERE {APPROACH_SQL} AND finish_yards IS NOT NULL{base} ORDER BY finish_yards LIMIT 5"),
        "chips": top(f"SELECT player, ROUND(finish_yards * 3) AS v, outcome, club, ROUND(to_pin_yards) AS from_yd, lie_before AS lie,"
                     f" course, hole, date{j} WHERE {CHIP_SQL} AND finish_yards IS NOT NULL{base} ORDER BY finish_yards LIMIT 5"),
        "putts": top(f"SELECT player, ROUND(to_pin_yards * 3) AS v, course, hole, date{j} WHERE is_putt = 1"
                     f" AND outcome = 'holed' AND to_pin_yards IS NOT NULL{base} ORDER BY to_pin_yards DESC LIMIT 5"),
    }


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
        for g in zip(words, words[1:], words[2:]):
            if (sum(w not in _STOP for w in g) >= 2 and all(w.isalpha() or "'" in w for w in g)
                    and not any(w in on_air for w in g)):
                grams[r["speaker"]][" ".join(g)] += 1
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
             lie: str | None = None, limit: int = 500) -> list[dict]:
    """Shots, newest first, each with what the booth said about it."""
    where, args = ["1=1"], []
    for col, val in (("player", player), ("date", date), ("course", course), ("club", club), ("lie_before", lie)):
        if val:
            where.append(f"{'sessions.' if col == 'date' else 'shots.'}{col} = ?")
            args.append(val)
    rows = db.execute(
        "SELECT shots.id, sessions.date, shots.session_id, shots.player, shots.course, shots.hole, shot_number, club,"
        " is_putt, lie_before, lie_after, ROUND(to_pin_yards, 1) AS to_pin, ROUND(finish_yards, 1) AS finish,"
        " ROUND(COALESCE(carry_raw_tile_yards, carry_raw)) AS carry, ROUND(total_yards) AS total, ball_speed,"
        " club_speed, launch_angle, total_spin, spin_axis, start_direction, outcome, mulligan, fmt"
        " FROM shots JOIN sessions ON sessions.id = shots.session_id WHERE " + " AND ".join(where)
        + _ctx(context).replace("is_league", "shots.is_league") + " ORDER BY shots.ts DESC, shots.id DESC LIMIT ?",
        (*args, limit)).fetchall()
    out = [dict(r) for r in rows]
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
    out = []
    for r in db.execute("SELECT s.id, s.date, COUNT(l.id) AS lines FROM sessions s JOIN lines l ON l.session_id = s.id"
                        " WHERE EXISTS (SELECT 1 FROM shots WHERE shots.session_id = s.id)"
                        " GROUP BY s.id ORDER BY s.started_at DESC"):
        courses = [c[0] for c in db.execute("SELECT DISTINCT course FROM shots WHERE session_id = ? AND course IS NOT NULL",
                                            (r["id"],))]
        players = [p[0] for p in db.execute("SELECT DISTINCT player FROM shots WHERE session_id = ? AND player IS NOT NULL"
                                            " AND player != 'unknown'", (r["id"],))]
        out.append({"id": r["id"], "date": r["date"], "lines": r["lines"], "courses": courses, "players": players})
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
