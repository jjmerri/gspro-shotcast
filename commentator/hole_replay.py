"""Hole replays: each hole of a round drawn from where GSPro had the ball, shot by shot.

GSPro's log gives every shot's starting position in the course's world metres (shots.start_*), the
player's minimap aim click (shots.aim_map_*), and, when a round is completed, each hole's tee and pin
(hole_layouts). A shot runs from its start to where the player's next stroke was played from, and a
hole's last stroke into the pin; a ball lost to a penalty area (water, OB) runs to where it went in
(shots.hazard_*), and the drop is the next stroke's start. Positions are top-down (x, z): Unity's y is height, and +x is to the
right of a player facing +z, so the plain (x, z) plane is the map as seen from above.

A full shot is drawn bowed by its estimated curve (shot_shape.bend_yards, as "bend": metres to
the right of the straight line at its middle); a putt is straight.

A tee shot has no logged start (GSPro sets it up without a setup block), so it comes from the hole's
tee in hole_layouts, from any round on the course. The pin moves round to round, so it's taken only
from this round's layout; a round quit before the end logged none, and the pin is then estimated from
the distances to it logged at each shot (estimate_pin).

The background is GSPro's minimap of the hole off the tee (hole_images, one per course and hole), drawn
so its ball lands on the tee and its pin on the pin. For the round it was taken in it's exact; for
another round there a different tee box or pin leaves it slightly off ("approx"), and its white pin dot
and yardages are that round's.
"""
from __future__ import annotations

import base64
import io
import json
import math
import re
import sqlite3
from typing import Any

from . import shot_shape, stats_web
from .sources.gspro_log import M_TO_YD, aim_offset

ROUND_HOURS = 8 * 3600          # a round's layout is logged at its end, within this of its shots
PIN_FIT_MAX_RMS_M = 3.0         # an estimated pin whose distances miss by more than this is dropped
_HOLE = re.compile(r"Hole (\d+)")
_PAR = re.compile(r"par (\d)")
_COLS = ("id, session_id, ts, player, course, hole, shot_number, club, is_putt, lie_before, lie_after,"
         " to_pin_yards, total_yards, offline_yards,"
         " outcome, mulligan, shared_ball, fmt, tags, start_x, start_z, aim_map_x, aim_map_y, hazard_x, hazard_z,"
         " start_direction, spin_axis, handed, picked, COALESCE(carry_game_yards, carry_raw_tile_yards, carry_raw) AS carry")


def _xz(x: float | None, z: float | None) -> tuple[float, float] | None:
    return (x, z) if x is not None and z is not None else None


def _unit(dx: float, dz: float) -> tuple[float, float] | None:
    n = math.hypot(dx, dz)
    return (dx / n, dz / n) if n > 1e-6 else None


def estimate_pin(obs: list[tuple[tuple[float, float], float]]) -> tuple[float, float] | None:
    """The pin from shots' positions and their logged distances to it (metres): the point whose
    distances best match, by Gauss-Newton. Shots mostly run along one line, which leaves the side of
    that line ambiguous, so the search starts past the closest ball on the line from the farthest, and
    settles on the nearer solution. None when there's too little to go on or the fit is poor."""
    obs = [(p, d) for p, d in obs if d is not None and d >= 0]
    if not obs:
        return None
    near = min(obs, key=lambda o: o[1])
    if len(obs) == 1:
        return near[0] if near[1] <= 2.0 else None
    far = max(obs, key=lambda o: o[1])
    u = _unit(near[0][0] - far[0][0], near[0][1] - far[0][1]) or (0.0, 0.0)
    px, pz = near[0][0] + u[0] * near[1], near[0][1] + u[1] * near[1]
    for _ in range(30):
        # Normal equations of the 2x2 least squares: sum J^T J and J^T r over the observations.
        a = b = c = gx = gz = 0.0
        for (x, z), d in obs:
            dist = math.hypot(px - x, pz - z)
            if dist < 1e-6:
                continue
            jx, jz, r = (px - x) / dist, (pz - z) / dist, dist - d
            a, b, c = a + jx * jx, b + jx * jz, c + jz * jz
            gx, gz = gx + jx * r, gz + jz * r
        det = a * c - b * b
        if abs(det) < 1e-9:   # one direction unconstrained (all balls on a line): step along the other
            if a + c < 1e-9:
                break
            step = (gx * (a / (a + c)) + gz * (b / (a + c)), gx * (b / (a + c)) + gz * (c / (a + c)))
            sx, sz = step[0] / max(a + c, 1e-9), step[1] / max(a + c, 1e-9)
        else:
            sx, sz = (c * gx - b * gz) / det, (a * gz - b * gx) / det
        px, pz = px - sx, pz - sz
        if math.hypot(sx, sz) < 1e-3:
            break
    rms = math.sqrt(sum((math.hypot(px - x, pz - z) - d) ** 2 for (x, z), d in obs) / len(obs))
    return (px, pz) if rms <= PIN_FIT_MAX_RMS_M else None


def _layouts(db: sqlite3.Connection) -> list[sqlite3.Row]:
    try:
        return db.execute("SELECT * FROM hole_layouts ORDER BY id").fetchall()
    except sqlite3.OperationalError:
        return []


def _layout_for(layouts: list[sqlite3.Row], course: str, hole: int, sessions: set, first_ts: float,
                last_ts: float, until: float | None = None) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
    """(this round's layout, for the pin; the layout for the tee: this round's, else the hole's latest).
    GSPro logs a layout when a round completes, so this round's is stored in one of its sessions (a
    booth restarted mid-round starts another) after the hole was played and before `until`, those
    sessions' next round's first shot (a round quit early has none)."""
    key = (course or "").casefold()
    same = [lo for lo in layouts if (lo["course"] or "").casefold() == key and lo["hole"] == hole]
    end = min(first_ts + ROUND_HOURS, until) if until is not None else first_ts + ROUND_HOURS
    this_round = [lo for lo in same if lo["session_id"] in sessions and lo["ts"] is not None
                  and last_ts - 60 <= lo["ts"] <= end]
    with_tee = [lo for lo in this_round[:1] + same[::-1] if lo["tee_x"] is not None]
    return (this_round[0] if this_round else None), (with_tee[0] if with_tee else None)


def _images(db: sqlite3.Connection) -> list[sqlite3.Row]:
    try:
        return db.execute("SELECT * FROM hole_images WHERE ball_x IS NOT NULL AND pin_x IS NOT NULL"
                          " ORDER BY id").fetchall()
    except sqlite3.OperationalError:
        return []


def _map_for(images: list[sqlite3.Row], course: str, hole: int, sessions: set) -> dict[str, Any] | None:
    """The hole's minimap for the replay background, by id (published once: hole_image_sources)."""
    from PIL import Image

    key = (course or "").casefold()
    same = [im for im in images if (im["course"] or "").casefold() == key and im["hole"] == hole]
    if not same:
        return None
    im = same[-1]
    w, h = Image.open(io.BytesIO(im["image"])).size
    return {"id": im["id"], "w": w, "h": h,
            "ball": [round(im["ball_x"] * w, 1), round(im["ball_y"] * h, 1)],
            "pin": [round(im["pin_x"] * w, 1), round(im["pin_y"] * h, 1)], "approx": im["session_id"] not in sessions}


def hole_image_sources(db: sqlite3.Connection, ids: set[int]) -> dict[int, str]:
    """The kept minimaps with these ids, as data: URLs for the page."""
    return {im["id"]: "data:image/jpeg;base64," + base64.b64encode(im["image"]).decode()
            for im in _images(db) if im["id"] in ids}


def _r(p: tuple[float, float] | None) -> list[float] | None:
    return [round(p[0], 2), round(p[1], 2)] if p else None


def hole_replays(db: sqlite3.Connection) -> dict[tuple[int, int], dict[str, Any]]:
    """Every replayable hole, keyed by (round, hole number): the round as stats_web.shot_rounds numbers
    it. A hole is replayable when at least one of its shots has a position, or it took one drawn stroke
    (an ace, or a tee shot into the gimme circle), which runs from the tee to the pin."""
    have = {r[1] for r in db.execute("PRAGMA table_info(shots)")}
    if "start_x" not in have:
        return {}
    cols = _COLS if "hazard_x" in have else _COLS.replace("hazard_x, hazard_z", "NULL AS hazard_x, NULL AS hazard_z")
    if "picked" not in have:
        cols = cols.replace(" picked,", " NULL AS picked,")
    round_of = stats_web.shot_rounds(db)
    groups: dict[tuple[int, int], list[sqlite3.Row]] = {}
    for row in db.execute(f"SELECT {cols} FROM shots ORDER BY id"):
        m = _HOLE.match(row["hole"] or "")
        if row["id"] in round_of and m:
            groups.setdefault((round_of[row["id"]], int(m.group(1))), []).append(row)
    layouts, images = _layouts(db), _images(db)
    # Each round's sessions and first and last shot, to end its layout window at the next round's start.
    span: dict[int, list] = {}
    for (rnd, _), rows in groups.items():
        sp = span.setdefault(rnd, [set(), rows[0]["ts"], rows[-1]["ts"]])
        sp[0].update(r["session_id"] for r in rows)
        sp[1], sp[2] = min(sp[1], rows[0]["ts"]), max(sp[2], rows[-1]["ts"])
    by_round: dict[int, list[sqlite3.Row]] = {}
    for (rnd, _), rows in groups.items():
        by_round.setdefault(rnd, []).extend(rows)
    teams = {rnd: _scramble_teams(rows) for rnd, rows in by_round.items()}
    out = {}
    for key, rows in groups.items():
        drawn = [r for r in rows if not r["mulligan"] and r["outcome"] != "gimme"]
        one_stroke = (drawn and all(r["shot_number"] == 1 for r in drawn)
                      and any(r["outcome"] in ("holed", "gimme", "conceded") for r in rows))
        if not any(r["start_x"] is not None for r in rows) and not one_stroke:
            continue
        ses, _, last = span[key[0]]
        until = min((f for s, f, _ in span.values() if s & ses and f > last), default=None)
        if (rp := _replay(rows, key[1], layouts, ses, until, teams[key[0]])) is not None:
            if rp["tee"] and rp["pin"] and (m := _map_for(images, rp["course"], key[1], ses)):
                rp["map"] = m
            rp["ts"] = min(r["ts"] for r in rows)   # the hole's first shot, for the page's video link
            out[key] = rp
    return out


def _travel_m(r: sqlite3.Row) -> float | None:
    """How far the shot went, in metres: the total and offline yardage, else the carry."""
    if r["total_yards"]:
        return math.hypot(r["total_yards"], r["offline_yards"] or 0) / M_TO_YD
    return r["carry"] / M_TO_YD if r["carry"] else None


def _shared(r: sqlite3.Row) -> bool:
    """A team format where GSPro copies balls between teammates (a scramble's opening tee shots
    can predate the log revealing it, and so lack the flag)."""
    return r["fmt"] == "scramble" or bool(r["shared_ball"])


def _scramble_teams(rows: list[sqlite3.Row]) -> dict[str, str]:
    """Each shared-ball player's team, as one partner's name, from a whole round's shots: GSPro
    puts partners' balls on the very same spot for every stroke after the tee shot (all teams tee
    off from the same one). Two players are partners when they shared a spot on most of the strokes
    they both played, so an opponent who once putts from beside them isn't. Read over the round, not
    a hole, so a hole one partner finished alone still knows the team."""
    spots: dict[tuple, list[tuple]] = {}
    for r in rows:
        if (_shared(r) and r["player"] and not r["mulligan"] and (r["shot_number"] or 0) > 1
                and r["start_x"] is not None and (m := _HOLE.match(r["hole"] or ""))):
            spots.setdefault((m.group(1), r["shot_number"]), []).append((r["player"], (r["start_x"], r["start_z"])))
    both: dict[tuple, list[int]] = {}
    for shots in spots.values():
        for i, (a, pa) in enumerate(shots):
            for b, pb in shots[i + 1:]:
                if a != b:
                    n = both.setdefault(tuple(sorted((a, b))), [0, 0])
                    n[0] += math.dist(pa, pb) <= 0.05
                    n[1] += 1
    team: dict[str, str] = {}

    def root(p):
        while team.setdefault(p, p) != p:
            p = team[p]
        return p
    for (a, b), (same, n) in both.items():
        if same * 2 > n:
            team[root(a)] = root(b)
    return {p: root(p) for p in list(team)}


def _team_ends(played: list[sqlite3.Row], starts: dict[int, tuple | None], rows: list[sqlite3.Row],
               pin: tuple | None, teams: dict[str, str]) -> tuple[dict[int, tuple], set[int]]:
    """Where the picked ball of each shared-ball stroke went, by shot id, and the ids of the picked
    shots: in a scramble each partner hits from the team's ball, the team plays on from one of them,
    and the others' are lifted, so only the picked shot is drawn on. GSPro's log names the pick
    (shots.picked); a stroke recorded without it is worked out: the same partners' next stroke starts
    where it finished, so the picked one is the shot whose length fits that (the one whose resting lie
    is that stroke's, on a tie). The stroke that holed or was conceded runs to the pin, and so does
    the one that ended the hole when the team's last stroke was a gimme."""
    shared = [r for r in played if _shared(r) and r["shot_number"]]
    finished = {r["player"] for r in rows if r["outcome"] in ("holed", "gimme", "conceded") and not r["mulligan"]}
    def root(p):
        return teams.get(p, p)
    ends: dict[int, tuple] = {}
    # The log's pick stands whether or not its ball has a stored spot.
    logged_by = {(root(r["player"]), r["shot_number"]): r for r in shared if r["picked"] and r["player"]}
    used: set[int] = {r["id"] for r in logged_by.values()}
    done: set[int] = set()
    for r in shared:
        if r["id"] in done or starts.get(r["id"]) is None:
            continue
        st = starts[r["id"]]
        group = [o for o in shared if o["shot_number"] == r["shot_number"] and starts.get(o["id"]) is not None
                 and root(o["player"]) == root(r["player"]) and math.dist(starts[o["id"]], st) <= 0.05]
        done.update(o["id"] for o in group)
        names = {o["player"] for o in group}
        last = max(o["shot_number"] for o in shared if root(o["player"]) == root(r["player"]))
        holed = [o for o in group if o["outcome"] in ("holed", "conceded")]
        logged = logged_by.get((root(r["player"]), r["shot_number"]))
        if holed:
            if logged is not None and logged["id"] != holed[0]["id"]:
                used.discard(logged["id"])
            used.add(holed[0]["id"])
            if pin:
                ends[holed[0]["id"]] = pin
            continue
        nxt = [o for o in shared if o["shot_number"] == r["shot_number"] + 1 and o["player"] in names
               and o["start_x"] is not None]
        if nxt:
            to, lie = (nxt[0]["start_x"], nxt[0]["start_z"]), nxt[0]["lie_before"]
        elif pin and names & finished and r["shot_number"] == last:
            to, lie = pin, None
        else:
            continue
        if logged is not None:
            if any(o["id"] == logged["id"] for o in group):
                ends[logged["id"]] = to
            continue
        group = [o for o in group if o["picked"] is None]   # a ball the log says was lifted isn't the pick
        if not group:
            continue

        def err(o, to=to, lie=lie):
            t = _travel_m(o)
            return (abs(math.dist(st, to) - t) if t is not None else math.inf,
                    lie is not None and o["lie_after"] != lie)
        best = min(group, key=err)
        if len(group) == 1 or err(best)[0] < math.inf:
            ends[best["id"]] = to
            used.add(best["id"])
    return ends, used


def _replay(rows: list[sqlite3.Row], hole: int, layouts: list[sqlite3.Row], sessions: set,
            until: float | None = None, teams: dict[str, str] | None = None) -> dict[str, Any] | None:
    first = rows[0]
    this_round, any_round = _layout_for(layouts, first["course"], hole, sessions, first["ts"],
                                        rows[-1]["ts"], until)
    tee = _xz(any_round["tee_x"], any_round["tee_z"]) if any_round else None
    pin, pin_est = (_xz(this_round["pin_x"], this_round["pin_z"]) if this_round else None), False
    played = [r for r in rows if not r["mulligan"] and r["outcome"] != "gimme"]
    if pin is None:
        pin = estimate_pin([((r["start_x"], r["start_z"]), r["to_pin_yards"] / M_TO_YD) for r in played
                            if r["start_x"] is not None and r["to_pin_yards"] is not None])
        pin_est = pin is not None
    lo = this_round or any_round
    par = lo["par"] if lo is not None else None
    if par is None and (m := _PAR.search(first["hole"] or "")):
        par = int(m.group(1))
    # A tee shot starts on the tee whatever was stored: a group round recorded before positions
    # followed the shooter can have the previous player's ball there.
    starts = {r["id"]: ((tee if r["shot_number"] == 1 else None) or _xz(r["start_x"], r["start_z"])
                        or (tee if (r["shot_number"] or 0) <= 1 else None)) for r in played}
    team_ends, team_used = _team_ends(played, starts, rows, pin, teams or {})
    players = []
    for name in dict.fromkeys(r["player"] or "unknown" for r in rows):
        mine = [r for r in played if (r["player"] or "unknown") == name]
        finished = any((r["player"] or "unknown") == name and r["outcome"] in ("holed", "gimme", "conceded")
                       for r in rows)
        shots = []
        for i, r in enumerate(mine):
            start = starts[r["id"]]
            nxt = mine[i + 1] if i + 1 < len(mine) else None
            tags = json.loads(r["tags"] or "[]")
            end, hazard = None, _xz(r["hazard_x"], r["hazard_z"])
            follows = nxt is not None and (nxt["shot_number"] or 0) == (r["shot_number"] or 0) + 1
            if _shared(r):
                end = team_ends.get(r["id"])   # drawn only for the ball the team played on
            elif hazard and ((nxt is not None and not follows) or "in_water" in tags):
                # Into a penalty area: the next stroke, after the penalty, is the drop. Only with a
                # penalty to back it: GSPro can log an entry point for a ball that ends up dry.
                end = hazard
            elif "in_water" in tags:
                end = None
            elif follows:
                end = _xz(nxt["start_x"], nxt["start_z"])
            elif nxt is None and finished:
                end = pin
            aim = None
            if (r["aim_map_x"] is not None and start and pin and not r["is_putt"]
                    and r["to_pin_yards"] and (f := _unit(pin[0] - start[0], pin[1] - start[1]))):
                along, right = aim_offset(r["aim_map_x"], r["aim_map_y"], r["to_pin_yards"] / M_TO_YD)
                aim = (start[0] + f[0] * along + f[1] * right, start[1] + f[1] * along - f[0] * right)
            shape = shot_shape.shape(r["start_direction"], r["spin_axis"], r["carry"], bool(r["is_putt"]), r["handed"])
            bend = None if r["is_putt"] else shot_shape.bend_yards(r["spin_axis"], r["carry"])
            shots.append({"n": r["shot_number"], "club": r["club"], "putt": bool(r["is_putt"]),
                          "lie": r["lie_before"], "to_pin": r["to_pin_yards"] and round(r["to_pin_yards"], 1),
                          "from": _r(start), "to": _r(end), "aim": _r(aim),
                          **({"shape": shape} if shape else {}),
                          **({"bend": round(bend / M_TO_YD, 2)} if bend else {}),
                          **({"penalty": True} if end is not None and end == hazard else {})})
        if any(s["from"] or s["to"] for s in shots):
            # The hole's score counts strokes that are never drawn: a gimme or conceded putt, a penalty.
            mine_all = [r for r in rows if (r["player"] or "unknown") == name and not r["mulligan"]]
            strokes = max((r["shot_number"] or 0 for r in mine_all), default=0) or None
            gimmes = sum(r["outcome"] == "gimme" for r in mine_all)
            entry = {"name": name, "shots": shots, "strokes": strokes, "gimmes": gimmes}
            if name in (teams or {}) or any(r["fmt"] == "scramble" for r in mine_all):
                # A scramble partner: the strokes the team played on from this player's ball, plus the
                # strokes that ball cost without a swing, so the partners' counts add up to the hole's
                # score: the gimme (only the picked partner's row carries it) and any penalty, the gap
                # in the team's stroke numbers after a picked shot.
                if name in (teams or {}):
                    team = [r for r in rows if (r["player"] or "unknown") in teams
                            and teams[r["player"]] == teams[name] and not r["mulligan"]]
                else:
                    team = [r for r in rows if r["fmt"] == "scramble" and not r["mulligan"]]
                numbers = sorted({r["shot_number"] for r in team if r["shot_number"]})
                used = [r for r in mine if r["id"] in team_used]
                entry["used"] = len(used)
                entry["used_gimmes"] = gimmes
                entry["used_pens"] = sum(max(0, nxt - r["shot_number"] - 1) for r in used if r["shot_number"]
                                         if (nxt := next((n for n in numbers if n > r["shot_number"]), None)))
            players.append(entry)
    if not players:
        return None
    return {"course": first["course"], "hole": hole, "par": par, "tee": _r(tee), "pin": _r(pin),
            "pin_estimated": pin_est, "players": players}
