"""Hole replays: each hole of a round drawn from where GSPro had the ball, shot by shot.

GSPro's log gives every shot's starting position in the course's world metres (shots.start_*), the
player's minimap aim click (shots.aim_map_*), and, when a round is completed, each hole's tee and pin
(hole_layouts). A shot runs from its start to where the player's next stroke was played from, and a
hole's last stroke into the pin; a ball lost to a penalty area (water, OB) runs to where it went in
(shots.hazard_*), and the drop is the next stroke's start. Positions are top-down (x, z): Unity's y is height, and +x is to the
right of a player facing +z, so the plain (x, z) plane is the map as seen from above.

A stroke GSPro's round file recorded (shot_tracks, GSPro 3.59+) is drawn as the ball went: "path", its
flight and roll from where it was played to where it stopped, each point [x, z, height, seconds], with
"land" the index of the touchdown and "descent" the angle it came down at (the page's shot view). Otherwise a full shot is drawn bowed by its estimated curve (shot_shape.bend_yards, as
"bend": metres to the right of the straight line at its middle), and a putt straight.

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
import bisect
import io
import json
import sqlite3
from collections import Counter
from typing import Any, Callable, Iterable

from shotcast_scoring.replay import (  # (the parts with no database: shared with Shotcast Cloud)
    ROUND_HOURS,
    _HOLE,
    _fill_gaps,
    _replay,
    _scramble_teams,
    track,
    _layout_for,  # noqa: F401 (tests read these from here)
    estimate_pin,  # noqa: F401
    _simplify,  # noqa: F401
)

from . import round_record, stats_web

_COLS = ("id, session_id, ts, player, course, hole, shot_number, club, is_putt, lie_before, lie_after,"
         " to_pin_yards, total_yards, offline_yards,"
         " outcome, mulligan, shared_ball, fmt, tags, start_x, start_z, aim_map_x, aim_map_y, hazard_x, hazard_z,"
         " start_direction, spin_axis, handed, picked, COALESCE(carry_game_yards, carry_raw_tile_yards, carry_raw) AS carry")


def _tracks(db: sqlite3.Connection, ids: set[int] | None = None) -> dict[int, dict[str, Any]]:
    """Each tracked shot's path (GSPro's round file, shot_tracks): {"path": [[x, z, height, seconds], ...],
    the height in metres above where it was played and the seconds since the strike; "land": the
    touchdown's index (GSPro's touchdown point; absent for a putt that never flew); "descent": the angle in degrees it came down
    at; "end": (x, z)}. Flight and roll points carry their own times (a flight stored before they did
    was recorded a fixed step apart, so its points are spread evenly over its seconds). A ball GSPro
    never brought to rest (out of bounds, lost) runs to its touchdown, "lost": True. A shot tracked
    with nothing to draw gets {}: its start is still GSPro's. `ids`: those shots' only."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'shot_tracks'").fetchone():
        return {}
    sql = ("SELECT t.shot_id, t.flight, t.flight_s, t.roll, t.land_x, t.land_y, t.land_z, t.end_x, t.end_y, t.end_z,"
           " s.start_x, s.start_y, s.start_z FROM shot_tracks t JOIN shots s ON s.id = t.shot_id")
    if ids is None:
        rows = db.execute(sql).fetchall()
    else:
        some = sorted(ids)
        rows = [r for i in range(0, len(some), 500) for r in db.execute(
            f"{sql} WHERE t.shot_id IN ({','.join('?' * len(some[i:i + 500]))})", some[i:i + 500])]
    return {r["shot_id"]: track({**dict(r), "flight": json.loads(r["flight"] or "[]"),
                                 "roll": json.loads(r["roll"] or "[]")}) for r in rows}


def _layouts(db: sqlite3.Connection) -> list[sqlite3.Row]:
    try:
        return db.execute("SELECT * FROM hole_layouts ORDER BY id").fetchall()
    except sqlite3.OperationalError:
        return []


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


def _next_after(times: dict[int, list], after: dict[int, tuple[set, float]]) -> dict[int, float | None]:
    """For each round in `after` (its sessions, a time): the earliest of `times` (round -> [sessions, a
    time]) later than that time among rounds sharing one of its sessions, else None. One sorted list per
    session, so it's not every round against every round."""
    by_session: dict[Any, list[float]] = {}
    for ses, at in times.values():
        for s in ses:
            by_session.setdefault(s, []).append(at)
    for v in by_session.values():
        v.sort()
    out: dict[int, float | None] = {}
    for rnd, (ses, at) in after.items():
        found = [v[i] for s in ses if (v := by_session.get(s)) and (i := bisect.bisect_right(v, at)) < len(v)]
        out[rnd] = min(found, default=None)
    return out


def round_tees(db: sqlite3.Connection, round_of: dict[int, int] | None = None) -> dict[int, str]:
    """Each round's tee box (Black, Blue, ...), keyed as stats_web.shot_rounds numbers rounds: the box
    most of its holes' own layouts were named (hole_layouts.tee_box). Rounds with none named are left out."""
    by_hole: dict[tuple[str, int, Any], list[sqlite3.Row]] = {}
    named = False
    for lo in _layouts(db):
        if "tee_box" in lo.keys():
            by_hole.setdefault(((lo["course"] or "").casefold(), lo["hole"], lo["session_id"]), []).append(lo)
            named = named or bool(lo["tee_box"])
    if not named:
        return {}
    holes: dict[tuple[int, int], list] = {}   # (round, hole) -> [course, first ts, last ts]
    span: dict[int, list] = {}                # round -> [sessions, first ts, last ts]
    ends: dict[int, list] = {}                # every recorded round -> [sessions, last ts]
    shot_ts: dict[float, set] = {}            # a shot's ts -> the rounds of shots then
    if round_of is None and round_record.recorded(db):
        # From each round's digest of its shots (round_record.record_shots), and the shots at a layout's time.
        for rnd, d in round_record.round_shots(db).items():
            for n, (course, lo, hi, _, _) in d["holes"].items():
                holes[(rnd, n)] = [course, lo, hi]
            if d["holes"]:
                span[rnd] = [set(d["hole_sessions"]), min(h[1] for h in d["holes"].values()),
                             max(h[2] for h in d["holes"].values())]
        for rnd, d in round_record.round_shots(db, "main.shots").items():
            if d["ids"]:
                ends[rnd] = [set(d["sessions"]), d["last"]]
        round_of, every_round = stats_web.shot_rounds(db), stats_web.shot_rounds(db, every=True)
        for sid, ts in db.execute("SELECT id, ts FROM main.shots WHERE ts IN"
                                  " (SELECT ts FROM hole_layouts WHERE ts IS NOT NULL)"):
            shot_ts.setdefault(ts, set()).add(round_of.get(sid, every_round.get(sid)))
    else:
        round_of = stats_web.shot_rounds(db) if round_of is None else round_of
        every_round = stats_web.shot_rounds(db, every=True)
        for sid, ses, ts, course, hole in db.execute("SELECT id, session_id, ts, course, hole FROM shots ORDER BY id"):
            m = _HOLE.match(hole or "")
            if (rnd := round_of.get(sid)) is None or not m:
                continue
            h = holes.setdefault((rnd, int(m.group(1))), [course, ts, ts])
            h[1], h[2] = min(h[1], ts), max(h[2], ts)
            sp = span.setdefault(rnd, [set(), ts, ts])
            sp[0].add(ses)
            sp[1], sp[2] = min(sp[1], ts), max(sp[2], ts)
        for sid, ses, ts in db.execute("SELECT id, session_id, ts FROM main.shots"):
            rnd = every_round.get(sid)
            shot_ts.setdefault(ts, set()).add(round_of.get(sid, rnd))
            if rnd is not None:
                e = ends.setdefault(rnd, [set(), ts])
                e[0].add(ses)
                e[1] = max(e[1], ts)
    # GSPro logs a round's layouts as it completes, so a round's own are its holes' earliest after it was
    # played (and the round started: not the round before's, logged a moment before) and before the next recorded round in its sessions ended (one left out of the stats too:
    # a stray shot after the round can't move an end, as it can a start). Layouts worked out from a
    # round's shots (course_layout.fill_missing) are stamped with one of its shots: another round's
    # aren't this one's.
    votes: dict[int, Counter] = {}
    next_end = _next_after(ends, {rnd: (ses, end) for rnd, (ses, _, end) in span.items()})
    for (rnd, hole), (course, first, last) in holes.items():
        ses, start, end = span[rnd]
        until = next_end[rnd]
        top = min(first + ROUND_HOURS, until) if until is not None else first + ROUND_HOURS
        key = (course or "").casefold()
        own = [lo for s in ses for lo in by_hole.get((key, hole, s), [])
               if lo["ts"] is not None and max(last - 60, start) <= lo["ts"] < top
               and shot_ts.get(lo["ts"], {rnd}) <= {rnd}]
        lo = min(own, key=lambda lo: (lo["ts"], lo["id"]), default=None)
        if lo is not None and lo["tee_box"]:
            votes.setdefault(rnd, Counter())[lo["tee_box"]] += 1
    return {rnd: c.most_common(1)[0][0] for rnd, c in votes.items()}


def hole_replays(db: sqlite3.Connection, rounds: Iterable[int] | None = None,
                 finish: dict[int, float] | None = None) -> dict[tuple[int, int], dict[str, Any]]:
    """Every replayable hole, keyed by (round, hole number): the round as stats_web.shot_rounds numbers
    it. A hole is replayable when at least one of its shots has a position, or it took one drawn stroke
    (an ace, or a tee shot into the gimme circle), which runs from the tee to the pin. Kept per round
    between builds (stats_cache). `rounds`: only theirs (one year's data file's), so a build holds one
    year's replays at a time. `finish`, when given, is filled with each swing's sideways finish in
    them (see finish_laterals)."""
    from . import stats_cache

    holes, plan, compute = _plan(db)
    sigs = {rnd: sig for rnd, (sig, _) in plan.items()}
    want = sigs if rounds is None else {r: sigs[r] for r in rounds if r in sigs}
    if stats_cache.of(db) is None:
        per_round = round_record.reuse(db, ("hole_replays",), lambda: compute(list(sigs)))
    else:
        per_round = stats_cache.per_key(db, "hole_replays", want, compute, keep=set(sigs))
    if finish is not None:
        for rnd in want:
            finish.update(per_round.get(rnd, {}).get("finish", {}))
    return {(rnd, hole): per_round[rnd][hole] for rnd, hole in holes
            if rnd in want and rnd in per_round and hole in per_round[rnd]}


def replay_rounds(db: sqlite3.Connection) -> dict[int, tuple[str, list]]:
    """Each round that may have a hole replay: (the signature its replays are kept under, "" with no cache
    open; its holes' sessions). Without reading any replay."""
    return _plan(db)[1]


def _plan(db: sqlite3.Connection) -> tuple[dict[tuple[int, int], list], dict[int, tuple[str, list]],
                                         Callable[[list], dict[int, dict[int, dict]]]]:
    """What hole_replays works from, once per build (round_record.reuse): each hole that may have a
    replay, each round's (signature, sessions), and what works out rounds' replays."""
    return round_record.reuse(db, ("hole_replay_plan",), lambda: _make_plan(db))


def _make_plan(db: sqlite3.Connection) -> tuple[dict[tuple[int, int], list], dict[int, tuple[str, list]],
                                                Callable[[list], dict[int, dict[int, dict]]]]:
    have = {r[1] for r in db.execute("PRAGMA table_info(shots)")}
    if "start_x" not in have:
        return {}, {}, lambda rnds: {}
    cols = _COLS if "hazard_x" in have else _COLS.replace("hazard_x, hazard_z", "NULL AS hazard_x, NULL AS hazard_z")
    if "picked" not in have:
        cols = cols.replace(" picked,", " NULL AS picked,")
    round_of = stats_web.shot_rounds(db)
    # Each hole's first shot, and its first and last shot's times (by id, as its rows come), and each
    # round's sessions and courses: enough to order the holes and end each round's layout window at
    # the next round's start without reading every shot's every column.
    holes: dict[tuple[int, int], list] = {}
    span: dict[int, list] = {}
    if round_record.recorded(db):
        for rnd, d in round_record.round_shots(db).items():
            for n, (_, _, _, a, b) in d["holes"].items():
                holes[(rnd, n)] = [a, b]
            if d["holes"]:
                span[rnd] = [set(d["hole_sessions"]), set(d["courses"])]
    else:
        for sid, ses, ts, course, hole in db.execute("SELECT id, session_id, ts, course, hole FROM shots ORDER BY id"):
            m = _HOLE.match(hole or "")
            if sid in round_of and m:
                rnd = round_of[sid]
                h = holes.setdefault((rnd, int(m.group(1))), [ts, ts])
                h[1] = ts
                sp = span.setdefault(rnd, [set(), set()])
                sp[0].add(ses)
                sp[1].add((course or "").casefold())
    first: dict[int, float] = {}
    last: dict[int, float] = {}
    for (rnd, _), (a, b) in holes.items():
        first[rnd] = min(first.get(rnd, a), a)
        last[rnd] = max(last.get(rnd, b), b)
    times = {rnd: [sp[0], first[rnd], last[rnd]] for rnd, sp in span.items()}
    next_start = _next_after({rnd: (ses, a) for rnd, (ses, a, _) in times.items()},
                             {rnd: (ses, b) for rnd, (ses, _, b) in times.items()})
    layouts, images = _layouts(db), _images(db)

    from . import stats_cache

    records: dict[int, str] = {}
    sigs = {rnd: "" for rnd in span}
    if stats_cache.of(db):
        # Each course's layouts and minimaps, which any round there may draw on.
        def per_course(rows: list[sqlite3.Row]) -> dict[str, str]:
            out: dict[str, list] = {}
            for r in rows:
                out.setdefault((r["course"] or "").casefold(), []).append(tuple(r))
            return {c: stats_cache.sig(v) for c, v in out.items()}
        lay_of, img_of = per_course(layouts), per_course(images)
        records, gens = stats_cache.row_records(db), stats_cache.record_sigs(db)
        sigs = {rnd: stats_cache.sig(rnd, records.get(rnd), gens.get(records.get(rnd)), sorted(ses, key=str),
                                     next_start[rnd], [(c, lay_of.get(c), img_of.get(c)) for c in sorted(courses)])
                for rnd, (ses, courses) in span.items()}

    def compute(rnds: list) -> dict[int, dict[int, dict]]:
        want = set(rnds)
        if len(want) == len(span):
            rows_ = db.execute(f"SELECT {cols} FROM shots ORDER BY id").fetchall()
        else:
            uuids = sorted({records[r] for r in want})
            rows_ = []
            for i in range(0, len(uuids), 500):
                chunk = uuids[i:i + 500]
                rows_ += db.execute(f"SELECT {cols} FROM shots WHERE record_uuid IN ({','.join('?' * len(chunk))})",
                                    chunk).fetchall()
            rows_.sort(key=lambda r: r["id"])
        groups: dict[tuple[int, int], list[sqlite3.Row]] = {}
        for row in rows_:
            m = _HOLE.match(row["hole"] or "")
            if (rnd := round_of.get(row["id"])) in want and m:
                groups.setdefault((rnd, int(m.group(1))), []).append(row)
        by_round: dict[int, list[sqlite3.Row]] = {}
        for (rnd, _), rows in groups.items():
            by_round.setdefault(rnd, []).extend(rows)
        teams = {rnd: _scramble_teams(rows) for rnd, rows in by_round.items()}
        sides = _sides(db)
        cards = _cards(db)
        player_cards = _player_cards(db)
        tracks = _tracks(db, None if len(want) == len(span) else {r["id"] for r in rows_})
        out: dict[int, dict[int | str, dict]] = {}
        for key, rows in groups.items():
            rows = _fill_gaps(rows)
            drawn = [r for r in rows if not r["mulligan"] and r["outcome"] != "gimme"]
            one_stroke = (drawn and all(r["shot_number"] == 1 for r in drawn)
                          and any(r["outcome"] in ("holed", "gimme", "conceded") for r in rows))
            if not any(r["start_x"] is not None for r in rows) and not one_stroke:
                continue
            ses = times[key[0]][0]
            until = next_start[key[0]]
            lat: dict[int, float] = {}
            scored = {name: card[str(key[1])] for name, card in player_cards.get(key[0], {}).items()
                      if isinstance(card.get(str(key[1])), int)}
            if (rp := _replay(rows, key[1], layouts, ses, until, teams[key[0]], sides.get(key[0]), tracks,
                              lat, scored)) is not None:
                if rp["tee"] and rp["pin"] and (m := _map_for(images, rp["course"], key[1], ses)):
                    rp["map"] = m
                rp["ts"] = min(r["ts"] for r in rows)   # the hole's first shot, for the page's video link
                for p in rp["players"]:
                    card = cards.get((key[0], p["name"].casefold()), {}).get(str(key[1]))
                    if "used" not in p and isinstance(card, int) and p["strokes"] and card < p["strokes"]:
                        # GSPro capped the hole at its maximum score: the card has fewer strokes than were hit.
                        p["strokes"], p["capped"] = card, True
                rnd = out.setdefault(key[0], {})
                rnd[key[1]] = rp
                rnd.setdefault("finish", {}).update(lat)   # (finish_laterals', kept with the round's)
        return out

    return holes, {rnd: (sigs[rnd], sorted(span[rnd][0], key=str)) for rnd in span}, compute


def _cards(db: sqlite3.Connection) -> dict[tuple[int, str], dict[str, Any]]:
    """Each recorded side's card by (round, side name folded): hole number -> strokes."""
    if "scores" not in {r[1] for r in db.execute("PRAGMA table_info(rounds)")}:
        return {}
    _, of_row = stats_web.round_groups(db)
    out = {}
    for rid, side, scores in db.execute("SELECT id, side, scores FROM rounds WHERE scores IS NOT NULL"):
        try:
            card = json.loads(scores)
        except (TypeError, ValueError):
            continue
        if isinstance(card, dict) and rid in of_row and side:
            out[(of_row[rid], side.casefold())] = card
    return out


def _player_cards(db: sqlite3.Connection) -> dict[int, dict[str, dict[str, Any]]]:
    """Each recorded side's card under each of its players' names (folded), by round as hole_replays
    numbers it: a team's card is its partners'."""
    cols = {r[1] for r in db.execute("PRAGMA table_info(rounds)")}
    if not {"scores", "players"} <= cols:
        return {}
    _, of_row = stats_web.round_groups(db)
    out: dict[int, dict[str, dict[str, Any]]] = {}
    for rid, players, scores in db.execute("SELECT id, players, scores FROM rounds WHERE scores IS NOT NULL"):
        try:
            card, names = json.loads(scores), json.loads(players or "[]")
        except (TypeError, ValueError):
            continue
        if isinstance(card, dict) and isinstance(names, list) and rid in of_row:
            for name in names:
                if isinstance(name, str):
                    out.setdefault(of_row[rid], {})[name.casefold()] = card
    return out


def _sides(db: sqlite3.Connection) -> dict[int, dict[str, int]]:
    """Each round's players' sides, by round as hole_replays numbers it: the rounds row each side's
    players are recorded on (its id stands for the side)."""
    if "players" not in {r[1] for r in db.execute("PRAGMA table_info(rounds)")}:
        return {}
    _, of_row = stats_web.round_groups(db)
    out: dict[int, dict[str, int]] = {}
    for rid, players in db.execute("SELECT id, players FROM rounds"):
        try:
            names = json.loads(players or "[]")
        except ValueError:
            continue
        for name in names if isinstance(names, list) else []:
            if isinstance(name, str):
                out.setdefault(of_row.get(rid, rid), {})[name.casefold()] = rid
    return out



def finish_laterals(db: sqlite3.Connection, since: str | None = None) -> dict[int, float]:
    """Where each swing GSPro logged positions for finished sideways of the line it was aimed down,
    yards (positive = right, as seen), by shot id: from where it started to where the next stroke
    started, after the roll (or where it went into a penalty area). The aim is the one set on the
    minimap; with none set, the pin, or off the tee of a par 4 or 5 the course's own aim point. A swing
    with no aim to go by (a long shot from the fairway with no aim set, a tee shot on a course with no
    aim point logged) is left out, as is a mulligan, which GSPro puts back. `since` (YYYY-MM-DD): the
    rounds with a night from then on only (their replays alone are read)."""
    out: dict[int, float] = {}
    rounds = None
    if since is not None:
        recent = {sid for (sid,) in db.execute("SELECT id FROM sessions WHERE date >= ?", (since,))}
        rounds = [rnd for rnd, (_, sessions) in replay_rounds(db).items() if recent.intersection(sessions)]
    hole_replays(db, rounds, finish=out)
    return out
