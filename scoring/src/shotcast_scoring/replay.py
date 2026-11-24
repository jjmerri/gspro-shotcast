"""Hole replays: each hole of a round drawn from where GSPro had the ball, shot by shot. The parts that
need no database, shared by the app (commentator/hole_replay.py, which reads its own tables and says
in full what a replay is) and Shotcast Cloud, so both draw a hole the same way.

A shot is a mapping with the app's shots columns (id, player, course, hole, shot_number, club, is_putt,
lie_before, to_pin_yards, total_yards, offline_yards, outcome, mulligan, shared_ball, fmt, tags (JSON
text), start_x, start_z, aim_map_x, aim_map_y, hazard_x, hazard_z, start_direction, spin_axis, handed,
picked, carry, ts); a layout, hole_layouts' (course, hole, session_id, ts, tee_x, tee_z, pin_x, pin_z,
wp_x, wp_z, par); a track, `track`'s answer.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

from shotcast_scoring import shot_shape

M_TO_YD = 1.0936133       # GSPro's log distances are Unity metres; the booth works in yards
AIM_MAP_SPAN = 677.0    # minimap units from the ball (y ~ -338) to the pin (y ~ +339)
AIM_MAP_BALL_Y = -338.0


def aim_offset(map_x: float, map_y: float, to_pin_m: float) -> tuple[float, float]:
    """A minimap aim click as metres (along the ball-to-pin line, right of it), given the distance to
    the pin then. GSPro scales the map to fit ball and pin whatever the distance, so one map unit is
    to_pin / 677 metres. Measured on full shots; a long hole whose map stops short of the green, and the
    putting view, are unverified."""
    scale = to_pin_m / AIM_MAP_SPAN
    return (map_y - AIM_MAP_BALL_Y) * scale, map_x * scale


ROUND_HOURS = 8 * 3600          # a round's layout is logged at its end, within this of its shots
PIN_FIT_MAX_RMS_M = 3.0         # an estimated pin whose distances miss by more than this is dropped
_HOLE = re.compile(r"Hole (\d+)")
_PAR = re.compile(r"par (\d)")


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


PATH_TOLERANCE_M = 0.3          # a drawn path keeps the points it needs to stay within this of the ball's


def _simplify(pts: list[tuple[float, ...]], tol: float = PATH_TOLERANCE_M) -> list[int]:
    """The indices of the timed points ((..., seconds), positions in any number of dimensions) that a
    ball moving steadily from each kept point to the next needs to pass within `tol` of all the rest
    at their times (Douglas-Peucker on the time-synchronised distance), the first and last always.
    So a roll keeps the points that slow it down, not just its corners."""
    keep, stack = {0, len(pts) - 1}, [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        a, b = pts[i], pts[j]
        ab = [q - p for p, q in zip(a[:-1], b[:-1])]
        dt, n2 = b[-1] - a[-1], sum(c * c for c in ab)
        far, at = 0.0, None
        for k in range(i + 1, j):
            ap = [q - p for p, q in zip(a[:-1], pts[k][:-1])]
            if dt > 1e-9:
                u = max(0.0, min(1.0, (pts[k][-1] - a[-1]) / dt))
            else:
                # No time between them: the distance to the segment, so a ball that rolls back stays drawn.
                u = max(0.0, min(1.0, sum(x * y for x, y in zip(ap, ab)) / n2)) if n2 > 1e-18 else 0.0
            d = math.sqrt(sum((x - u * y) ** 2 for x, y in zip(ap, ab)))
            if d > far:
                far, at = d, k
        if at is not None and far > tol:
            keep.add(at)
            stack += [(i, at), (at, j)]
    return sorted(keep)

def track(r: Any) -> dict[str, Any]:
    """One shot's path from GSPro's round file (a shot_tracks row: its flight and roll as lists, the
    rest as stored, and the shot's start), as hole_replay._tracks describes it; {} when there's
    nothing to draw."""
    flight, roll = r["flight"] or [], r["roll"] or []
    # No rest: GSPro took the ball away (out of bounds, lost), so its flight is drawn to the touchdown.
    lost = r["end_x"] is None
    if r["start_x"] is None or (lost and not flight):
        return {}
    if lost:
        roll = []
    base = r["start_y"] if r["start_y"] is not None else next((p[1] for p in [*flight, *roll]), 0.0)
    secs = r["flight_s"] or 0.0
    # The flight's points run up to the touchdown; GSPro's touchdown point is usually a little past the last.
    td = [r["land_x"], r["land_y"], r["land_z"]] if flight and r["land_x"] is not None else None
    apart = td is not None and math.dist(td, flight[-1][:3]) > 0.01
    steps = len(flight) if apart else max(1, len(flight) - 1)
    # Points stored with their time keep it; older rows' are spread evenly over the flight.
    fly = [(p[0], p[2], p[1] - base, p[3] if len(p) > 3 else secs * i / steps)
           for i, p in enumerate([*flight, *([[*td, secs]] if apart else [])])]
    rolled = [(p[0], p[2], p[1] - base, max(p[3], secs)) for p in roll]
    last_t = max([secs, *(p[3] for p in rolled)])
    if lost:
        end_x, end_z, end_h = fly[-1][0], fly[-1][1], fly[-1][2]
    else:
        end_x, end_z = r["end_x"], r["end_z"]
        end_h = r["end_y"] - base if r["end_y"] is not None else (rolled or fly or [(0, 0, 0.0)])[-1][2]
    pts = [(r["start_x"], r["start_z"], 0.0, 0.0), *fly, *rolled, (end_x, end_z, end_h, last_t)]
    land = len(fly) if fly else None   # the touchdown: the flight's last point
    pts = [p for i, p in enumerate(pts)
           if i == 0 or i == land or math.dist(p[:3], pts[i - 1][:3]) > 0.01]
    if fly:
        land = next((i for i, p in enumerate(pts) if p == fly[-1]), None)
    idx = _simplify(pts)
    if land is not None and land not in idx:
        idx = sorted({*idx, land})
    trk: dict[str, Any] = {"path": [_r4(pts[i]) for i in idx], "end": (end_x, end_z), **({"lost": True} if lost else {})}
    if land is not None:
        trk["land"] = idx.index(land)
    if len(flight) >= 2:
        (ax, ay, az), (bx, by, bz) = flight[-2][:3], flight[-1][:3]
        if (run := math.hypot(bx - ax, bz - az)) > 1e-6:
            trk["descent"] = round(math.degrees(math.atan2(ay - by, run)))
    return trk


def _r4(p: tuple[float, float, float, float]) -> list[float]:
    """A tracked point for the page: x and z to the centimetre, its height to the decimetre, its time."""
    return [round(p[0], 2), round(p[1], 2), round(p[2], 1), round(p[3], 2)]


def _layout_for(layouts: list[Any], course: str, hole: int, sessions: set, first_ts: float,
                last_ts: float, until: float | None = None) -> tuple[Any | None, Any | None]:
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


def _r(p: tuple[float, float] | None) -> list[float] | None:
    return [round(p[0], 2), round(p[1], 2)] if p else None


_ENDS_HOLE = ("holed", "gimme")
ANON_MAX = 6                       # more shots than this with no player on a hole are left unattributed
FIT_MIN_M, FIT_SHARE = 5.0, 0.15   # a shot fits a gap when its length misses the next start by at most this


def _penalised(r: dict) -> bool:
    """A shot that may have cost a penalty stroke, so its next stroke's number can't be known."""
    return r["hazard_x"] is not None or "in_water" in json.loads(r["tags"] or "[]")


def _fit(a: dict, b: dict) -> bool | None:
    """Whether shot a ends where shot b starts (its length against the distance between their starts),
    or None when either position or a's length is unknown."""
    t = _travel_m(a)
    if t is None or a["start_x"] is None or b["start_x"] is None:
        return None
    return abs(math.dist((a["start_x"], a["start_z"]), (b["start_x"], b["start_z"])) - t) <= max(FIT_MIN_M, FIT_SHARE * t)


def _numbering(seq: list[dict], tail_open: bool = True) -> dict[int, int] | None:
    """Stroke numbers for one player's unnumbered shots on a hole (in the order played), by index,
    where the numbered strokes around them leave no doubt: a run between two numbers that leave exactly
    room for it, a run before the first number that counts up to it, and (unless tail_open is False: a
    stroke nobody could be given may be in it) a run after the last number when nothing there may have
    cost a penalty, the hole wasn't over and no shot's length contradicts where the next was played
    from. None when the numbers can't be right: they don't rise, or leave too little room for the
    unnumbered shots between or before them."""
    known = [(i, r["shot_number"]) for i, r in enumerate(seq) if r["shot_number"]]
    if any(b <= a for (_, a), (_, b) in zip(known, known[1:])):
        return None
    out: dict[int, int] = {}
    if not known:
        return out
    if known[0][0] > known[0][1] - 1 or any(j - i > b - a for (i, a), (j, b) in zip(known, known[1:])):
        return None
    for (i, a), (j, b) in zip(known, known[1:]):
        if b - a - 1 == j - i - 1:
            out.update({k: a + k - i for k in range(i + 1, j)})
    j, b = known[0]
    if j and b - 1 == j and all(_fit(x, y) is not False for x, y in zip(seq[:j], seq[1:j + 1])):
        out.update({k: k + 1 for k in range(j)})
    i, a = known[-1]
    tail = seq[i:]
    if (tail_open and len(tail) > 1 and not any(_penalised(r) for r in tail[:-1])
            and not any(r["outcome"] in _ENDS_HOLE for r in tail[:-1])
            and all(_fit(x, y) is not False for x, y in zip(tail, tail[1:]))):
        out.update({k: a + k - i for k in range(i + 1, len(seq))})
    return out


def _fill_gaps(rows: list) -> list:
    """A hole's shots with the stroke numbers and players a misread left out filled in where the rest
    of the hole leaves no doubt, so its replay still runs shot to shot. A shot with no player goes to the
    one player it fits: one whose numbering then accounts for it and, when the hole has more than one
    player, whose ball it was by position (the shot before ends where it starts, or it ends where the
    next one starts); a stray swing after the hole is over fits nobody. A player it could be along with
    other shots that have no player (two of them filling one gap) counts, so those stay unattributed. Shared-ball holes are left as
    they are: their partners' shots share numbers by design. Only the replay sees this; the shots keep
    what was recorded."""
    if not any(r["shot_number"] is None or r["player"] is None for r in rows) or any(_shared(r) for r in rows):
        return rows
    out = [dict(r) for r in rows]
    seq = [r for r in out if not r["mulligan"]]
    names = list(dict.fromkeys(r["player"] for r in seq if r["player"]))
    def takes(name, u, extra):
        mine = [r for r in seq if r["player"] == name or r is u or any(r is e for e in extra)]
        k = next(i for i, r in enumerate(mine) if r is u)
        num = _numbering(mine)
        if num is None or (u["shot_number"] is None and k not in num):
            return False
        if any(r["outcome"] in _ENDS_HOLE for r in mine[:k]):
            return False
        by_pos = [f for f in (_fit(mine[k - 1], u) if k else None,
                              _fit(u, mine[k + 1]) if k + 1 < len(mine) else None) if f is not None]
        return False not in by_pos and (len(names) == 1 or bool(by_pos))
    if len(anon := [r for r in seq if r["player"] is None]) <= ANON_MAX:
        for u in anon:
            others = [r for r in seq if r["player"] is None and r is not u]
            fits = [name for name in names
                    if any(takes(name, u, [o for b, o in enumerate(others) if m >> b & 1]) for m in range(1 << len(others)))]
            if len(fits) == 1:
                u["player"] = fits[0]
    anon = [i for i, r in enumerate(seq) if r["player"] is None]
    for name in names:
        mine = [r for r in seq if r["player"] == name]
        # A stroke left without a player could be one of this player's after their last number.
        last = max((i for i, r in enumerate(seq) if r["player"] == name and r["shot_number"]), default=None)
        end = next((i for i, r in enumerate(seq) if r["player"] == name and r["outcome"] in _ENDS_HOLE), len(seq))
        tail_open = last is None or not any(last < i < end for i in anon)
        for k, n in (_numbering(mine, tail_open) or {}).items():
            mine[k]["shot_number"] = n
    return out


def _travel_m(r: Any) -> float | None:
    """How far the shot went, in metres: the total and offline yardage, else the carry."""
    if r["total_yards"]:
        return math.hypot(r["total_yards"], r["offline_yards"] or 0) / M_TO_YD
    return r["carry"] / M_TO_YD if r["carry"] else None


def _shared(r: Any) -> bool:
    """A team format where GSPro copies balls between teammates (a scramble's opening tee shots
    can predate the log revealing it, and so lack the flag)."""
    return r["fmt"] == "scramble" or bool(r["shared_ball"])


def _alternate_ends(played: list[Any], starts: dict[int, tuple | None], rows: list[Any],
                    pin: tuple | None, sides: dict[str, int]) -> dict[int, tuple | None]:
    """Where each alternate-shot stroke went, by shot id: partners take turns on one ball, so a stroke
    ends where the team's next one starts (the one whose start its length fits, when the sides aren't
    known). The stroke that holed or was conceded, or left a gimme, runs to the pin; one
    into a penalty area with a penalty stroke after it, to where it went in."""
    def team(a, b):
        a, b = (a or "").casefold(), (b or "").casefold()
        return a == b or a not in sides or b not in sides or sides[a] == sides[b]
    alt = [r for r in rows if r["fmt"] == "alternate" and r["shot_number"] and not r["mulligan"]]
    ends: dict[int, tuple | None] = {}
    for r in played:
        if r["fmt"] != "alternate" or not r["shot_number"]:
            continue
        n, st, end = r["shot_number"], starts.get(r["id"]), None
        hazard, wet = _xz(r["hazard_x"], r["hazard_z"]), "in_water" in json.loads(r["tags"] or "[]")
        ours = [o for o in alt if o["id"] != r["id"] and team(o["player"], r["player"])]
        after = min((o["shot_number"] for o in ours if o["shot_number"] > n), default=None)
        if r["outcome"] in ("holed", "conceded"):
            end = pin
        elif hazard and ((after is not None and after > n + 1) or wet):
            end = hazard
        elif wet:
            end = None
        elif any(o["outcome"] == "gimme" and o["shot_number"] == n and o["player"] != r["player"] for o in ours):
            end = pin   # the partner's gimme, under this stroke's number
        elif nxt := [o for o in ours if o["shot_number"] == n + 1 and o["outcome"] != "gimme"
                     and starts.get(o["id"]) is not None]:
            t = _travel_m(r)
            best = min(nxt, key=lambda o: abs(math.dist(st, starts[o["id"]]) - t) if st and t is not None else 0)
            end = starts[best["id"]]
        elif any(o["outcome"] == "gimme" and o["shot_number"] == n + 1 for o in ours):
            end = pin
        ends[r["id"]] = end
    return ends


def _scramble_teams(rows: list[Any]) -> dict[str, str]:
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


def _team_ends(played: list[Any], starts: dict[int, tuple | None], rows: list[Any],
               pin: tuple | None, teams: dict[str, str],
               scored: dict[str, int] | None = None) -> tuple[dict[int, tuple], set[int], set[int]]:
    """Where the picked ball of each shared-ball stroke went, by shot id, and the ids of the picked
    shots: in a scramble each partner hits from the team's ball, the team plays on from one of them,
    and the others' are lifted, so only the picked shot is drawn on. GSPro's log names the pick
    (shots.picked); a stroke recorded without it is worked out: the same partners' next stroke starts
    where it finished, so the picked one is the shot whose length fits that (the one whose resting lie
    is that stroke's, on a tie). The stroke that holed or was conceded runs to the pin, and so does
    the one that ended the hole when the team's last stroke was a gimme. A picked-up hole's last stroke
    (none holed, and a partner's card, `scored`: player name folded -> the hole's strokes, ends the hole
    on it) counts as used but isn't drawn on; an unfinished hole's isn't. Also the ids of strokes the
    team took again from the same spot after a penalty with nowhere to drop (out of bounds, lost):
    no ball was played on, and each is drawn as GSPro tracked it."""
    shared = [r for r in played if _shared(r) and r["shot_number"]]
    finished = {r["player"] for r in rows if r["outcome"] in ("holed", "gimme", "conceded") and not r["mulligan"]}
    def root(p):
        return teams.get(p, p)
    ends: dict[int, tuple] = {}
    again: set[int] = set()
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
        mates = [o for o in shared if root(o["player"]) == root(r["player"])]   # the team, not just this group
        later = sorted((o for o in mates if o["shot_number"] > r["shot_number"] + 1), key=lambda o: o["shot_number"])
        wet = [o for o in group if o["hazard_x"] is not None and o["hazard_z"] is not None]
        skipped = not any(o["shot_number"] == r["shot_number"] + 1 for o in mates)
        if skipped and later and wet:
            # Into a penalty area: the team's next stroke, after the penalty, is from the drop, so the
            # picked ball runs to where it went in: the log's pick, else the wet ball nearest the drop.
            if logged is not None:
                pick = next((o for o in wet if o["id"] == logged["id"]), None)
            else:
                drop = next((_xz(o["start_x"], o["start_z"]) for o in later if o["start_x"] is not None), None)
                pick = (wet[0] if len(wet) == 1 else min(wet, key=lambda o: math.dist(
                    (o["hazard_x"], o["hazard_z"]), drop)) if drop else None)
            if pick is not None and (logged is not None or pick["picked"] is None):
                ends[pick["id"]] = (pick["hazard_x"], pick["hazard_z"])
                used.add(pick["id"])
            continue
        if skipped and any(starts.get(o["id"]) is not None and math.dist(starts[o["id"]], st) <= 1.0
                           for o in later if o["shot_number"] == later[0]["shot_number"]):
            again.update(o["id"] for o in group)   # stroke and distance: played again from here
            if logged is None:
                used.add(group[0]["id"])   # someone's ball carries the stroke and the penalty in the counts
            continue
        if nxt:
            to, lie = (nxt[0]["start_x"], nxt[0]["start_z"]), nxt[0]["lie_before"]
        elif pin and names & finished and r["shot_number"] == last:
            to, lie = pin, None
        elif (r["shot_number"] == last and not any(root(p) == root(r["player"]) for p in finished)
              and any((scored or {}).get((o["player"] or "").casefold()) == last for o in mates)):
            # A pick-up (GSPro capped the hole with no ball in): the team's last stroke is still one of
            # its strokes, so a ball carries it, though it went nowhere drawn: the log's pick, else the
            # one whose length best fits the pin.
            group = [o for o in group if o["picked"] is None]
            if logged is None and group:
                used.add(min(group, key=lambda o: (abs(math.dist(st, pin) - t) if pin and (t := _travel_m(o))
                                                   is not None else math.inf))["id"])
            continue
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
    return ends, used, again


FINISH_PIN_MAX = 250   # yards: further out, a swing with no aim set is as likely a lay-up as at the pin

def _target(r, start: tuple[float, float], aim: tuple[float, float] | None, pin: tuple[float, float] | None,
            par: int | None, layout) -> tuple[float, float] | None:
    """The point a swing was aimed at, for finish_laterals."""
    if aim:
        return aim
    if r["shot_number"] == 1 and (par or 0) >= 4:
        if layout is None or "wp_x" not in layout.keys():
            return None
        return _xz(layout["wp_x"], layout["wp_z"])
    if pin and r["to_pin_yards"] and r["to_pin_yards"] <= FINISH_PIN_MAX:
        return pin
    return None


def _replay(rows: list[Any], hole: int, layouts: list[Any], sessions: set,
            until: float | None = None, teams: dict[str, str] | None = None,
            sides: dict[str, int] | None = None, tracks: dict[int, dict] | None = None,
            finish: dict[int, float] | None = None,
            scored: dict[str, int] | None = None) -> dict[str, Any] | None:
    first = rows[0]
    tracks = tracks or {}
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
    # GSPro's round file has it exactly (shot_tracks), a tee shot's too.
    starts = {r["id"]: ((tee if r["shot_number"] == 1 and r["id"] not in tracks else None)
                        or _xz(r["start_x"], r["start_z"])
                        or (tee if (r["shot_number"] or 0) <= 1 else None)) for r in played}
    team_ends, team_used, team_again = _team_ends(played, starts, rows, pin, teams or {}, scored or {})
    alt_ends = _alternate_ends(played, starts, rows, pin, sides or {})
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
            if r["fmt"] == "alternate":
                end = alt_ends.get(r["id"])
            elif _shared(r):
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
            trk = tracks.get(r["id"])
            penalty = end is not None and end == hazard
            # A scramble's ball is drawn only where the team played on (or all of them, taken again from
            # the tee after going out of bounds); alternate shot's every stroke is the team's ball.
            if trk and not penalty and (end is not None or r["fmt"] == "alternate" or not _shared(r)
                                        or r["id"] in team_again):
                end = trk["end"]   # where GSPro had the ball stop (a drop after it is the next start)
                if r["id"] in team_again or (trk.get("lost") and nxt is not None and not follows):
                    penalty = True if "in_water" in tags else "ob"
            else:
                trk = None
            aim = None
            if (r["aim_map_x"] is not None and start and pin and not r["is_putt"]
                    and r["to_pin_yards"] and (f := _unit(pin[0] - start[0], pin[1] - start[1]))):
                along, right = aim_offset(r["aim_map_x"], r["aim_map_y"], r["to_pin_yards"] / M_TO_YD)
                aim = (start[0] + f[0] * along + f[1] * right, start[1] + f[1] * along - f[0] * right)
            if finish is not None and not r["is_putt"] and start and end and (
                    target := _target(r, start, aim, pin, par, lo)) and (f := _unit(target[0] - start[0],
                                                                                    target[1] - start[1])):
                finish[r["id"]] = ((end[0] - start[0]) * f[1] - (end[1] - start[1]) * f[0]) * M_TO_YD
            shape = shot_shape.shape(r["start_direction"], r["spin_axis"], r["carry"], bool(r["is_putt"]), r["handed"])
            bend = None if r["is_putt"] or trk else shot_shape.bend_yards(r["spin_axis"], r["carry"])
            shots.append({"n": r["shot_number"], "club": r["club"], "putt": bool(r["is_putt"]),
                          "lie": r["lie_before"], "to_pin": r["to_pin_yards"] and round(r["to_pin_yards"], 1),
                          "from": _r(start), "to": _r(end), "aim": _r(aim),
                          **({"shape": shape} if shape else {}),
                          **({"bend": round(bend / M_TO_YD, 2)} if bend else {}),
                          **({"path": trk["path"], **{k: trk[k] for k in ("land", "descent") if k in trk}} if trk else {}),
                          **({"penalty": penalty} if penalty else {})})
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
                entry["used_drives"] = sum(r["shot_number"] == 1 for r in used)
                entry["used_pens"] = sum(max(0, nxt - r["shot_number"] - 1) for r in used if r["shot_number"]
                                         if (nxt := next((n for n in numbers if n > r["shot_number"]), None)))
            players.append(entry)
    if not players:
        return None
    return {"course": first["course"], "hole": hole, "par": par, "tee": _r(tee), "pin": _r(pin),
            "pin_estimated": pin_est, "players": players}
