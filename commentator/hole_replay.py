"""Hole replays: each hole of a round drawn from where GSPro had the ball, shot by shot.

GSPro's log gives every shot's starting position in the course's world metres (shots.start_*), the
player's minimap aim click (shots.aim_map_*), and, when a round is completed, each hole's tee and pin
(hole_layouts). A shot runs from its start to where the player's next stroke was played from, and a
hole's last stroke into the pin. Positions are top-down (x, z): Unity's y is height, and +x is to the
right of a player facing +z, so the plain (x, z) plane is the map as seen from above.

A tee shot has no logged start (GSPro sets it up without a setup block), so it comes from the hole's
tee in hole_layouts, from any round on the course. The pin moves round to round, so it's taken only
from this round's layout; a round quit before the end logged none, and the pin is then estimated from
the distances to it logged at each shot (estimate_pin).

The background is GSPro's minimap of the hole off the tee (hole_images), drawn so its ball lands on the
tee and its pin on the pin. One from the same session is exact; failing that, one from another round
on the course is used, where a different tee box or pin leaves it slightly off ("approx").
"""
from __future__ import annotations

import base64
import io
import json
import math
import re
import sqlite3
from typing import Any

from . import stats_web
from .sources.gspro_log import M_TO_YD, aim_offset

ROUND_HOURS = 8 * 3600          # a round's layout is logged at its end, within this of its shots
PIN_FIT_MAX_RMS_M = 3.0         # an estimated pin whose distances miss by more than this is dropped
_HOLE = re.compile(r"Hole (\d+)")
_PAR = re.compile(r"par (\d)")
_COLS = ("id, session_id, ts, player, course, hole, shot_number, club, is_putt, lie_before, to_pin_yards,"
         " outcome, mulligan, shared_ball, tags, start_x, start_z, aim_map_x, aim_map_y")


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


def _layout_for(layouts: list[sqlite3.Row], course: str, hole: int, session: int, first_ts: float,
                last_ts: float) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
    """(this round's layout, for the pin; any layout of the hole, for the tee), preferring the latest."""
    key = (course or "").casefold()
    same = [lo for lo in layouts if (lo["course"] or "").casefold() == key and lo["hole"] == hole]
    this_round = [lo for lo in same if lo["session_id"] == session and lo["ts"] is not None
                  and last_ts - 60 <= lo["ts"] <= first_ts + ROUND_HOURS]
    with_tee = [lo for lo in same if lo["tee_x"] is not None]
    return (this_round[0] if this_round else None), (with_tee[-1] if with_tee else None)


def _images(db: sqlite3.Connection) -> list[sqlite3.Row]:
    try:
        return db.execute("SELECT * FROM hole_images WHERE ball_x IS NOT NULL AND pin_x IS NOT NULL"
                          " ORDER BY id").fetchall()
    except sqlite3.OperationalError:
        return []


def _map_for(images: list[sqlite3.Row], course: str, hole: int, session: int) -> dict[str, Any] | None:
    """The hole's minimap for the replay background: this session's, else the latest from any round."""
    from PIL import Image

    key = (course or "").casefold()
    same = [im for im in images if (im["course"] or "").casefold() == key and im["hole"] == hole]
    if not same:
        return None
    mine = [im for im in same if im["session_id"] == session]
    im = (mine or same)[-1]
    w, h = Image.open(io.BytesIO(im["image"])).size
    return {"src": "data:image/jpeg;base64," + base64.b64encode(im["image"]).decode(), "w": w, "h": h,
            "ball": [round(im["ball_x"] * w, 1), round(im["ball_y"] * h, 1)],
            "pin": [round(im["pin_x"] * w, 1), round(im["pin_y"] * h, 1)], "approx": not mine}


def _r(p: tuple[float, float] | None) -> list[float] | None:
    return [round(p[0], 2), round(p[1], 2)] if p else None


def hole_replays(db: sqlite3.Connection) -> dict[tuple[int, int], dict[str, Any]]:
    """Every replayable hole, keyed by (round, hole number): the round as stats_web.shot_rounds numbers
    it. A hole is replayable when at least one of its shots has a position."""
    have = {r[1] for r in db.execute("PRAGMA table_info(shots)")}
    if "start_x" not in have:
        return {}
    round_of = stats_web.shot_rounds(db)
    groups: dict[tuple[int, int], list[sqlite3.Row]] = {}
    for row in db.execute(f"SELECT {_COLS} FROM shots ORDER BY id"):
        m = _HOLE.match(row["hole"] or "")
        if row["id"] in round_of and m:
            groups.setdefault((round_of[row["id"]], int(m.group(1))), []).append(row)
    layouts, images = _layouts(db), _images(db)
    out = {}
    for key, rows in groups.items():
        if not any(r["start_x"] is not None for r in rows):
            continue
        if (rp := _replay(rows, key[1], layouts)) is not None:
            if rp["tee"] and rp["pin"] and (m := _map_for(images, rp["course"], key[1], rows[0]["session_id"])):
                rp["map"] = m
            out[key] = rp
    return out


def _replay(rows: list[sqlite3.Row], hole: int, layouts: list[sqlite3.Row]) -> dict[str, Any] | None:
    first = rows[0]
    this_round, any_round = _layout_for(layouts, first["course"], hole, first["session_id"], first["ts"], rows[-1]["ts"])
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
    players = []
    for name in dict.fromkeys(r["player"] or "unknown" for r in rows):
        mine = [r for r in played if (r["player"] or "unknown") == name]
        finished = any((r["player"] or "unknown") == name and r["outcome"] in ("holed", "gimme", "conceded")
                       for r in rows)
        shots = []
        for i, r in enumerate(mine):
            start = _xz(r["start_x"], r["start_z"]) or (tee if (r["shot_number"] or 0) <= 1 else None)
            nxt = mine[i + 1] if i + 1 < len(mine) else None
            tags = json.loads(r["tags"] or "[]")
            end = None
            if r["shared_ball"] or "in_water" in tags:
                end = None   # a teammate's ball may be played on; a penalty's next stroke is the drop
            elif nxt is not None:
                if (nxt["shot_number"] or 0) == (r["shot_number"] or 0) + 1:
                    end = _xz(nxt["start_x"], nxt["start_z"])
            elif finished:
                end = pin
            aim = None
            if (r["aim_map_x"] is not None and start and pin and not r["is_putt"]
                    and r["to_pin_yards"] and (f := _unit(pin[0] - start[0], pin[1] - start[1]))):
                along, right = aim_offset(r["aim_map_x"], r["aim_map_y"], r["to_pin_yards"] / M_TO_YD)
                aim = (start[0] + f[0] * along + f[1] * right, start[1] + f[1] * along - f[0] * right)
            shots.append({"n": r["shot_number"], "club": r["club"], "putt": bool(r["is_putt"]),
                          "lie": r["lie_before"], "to_pin": r["to_pin_yards"] and round(r["to_pin_yards"], 1),
                          "from": _r(start), "to": _r(end), "aim": _r(aim)})
        if any(s["from"] or s["to"] for s in shots):
            players.append({"name": name, "shots": shots})
    if not players:
        return None
    return {"course": first["course"], "hole": hole, "par": par, "tee": _r(tee), "pin": _r(pin),
            "pin_estimated": pin_est, "players": players}
