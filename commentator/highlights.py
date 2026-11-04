"""Each round's highlight reel: its best moments (and at most one bad shot per player) back to back,
with the booth's calls, cut out of the round's stream. Picked from the shots the way the booth rates
them live (interest.py's excitement levels), so a reel's moments are the ones the booth got loudest
about. Run after each publish (stats_site.publish), with the rounds that build picked:

    live/reels.json                  {"reels": {"<round>": {"label", "date", "league", "ended", "ids",
                                     "duration", "moments": [{"at", "id", "player", "hole", "what", "bad"}]}}}
    live/reels/<round>/reel.m3u8     the reel's playlist: each moment's segments, copied from its stream
    live/reels/<round>/<session>/... (live/s/<session>/), a discontinuity between moments

`<round>` is the round's lowest rounds row id (stats_web.round_groups). A league round's reel stays
for the season (publish.season_start; with none set, for as long as the round is on the page), any
other round's for as long as its stream would have (recordings.json's keep_days). A round whose stream
is gone gets no reel; one whose stream hasn't reached its last moment yet gets it on the next publish.
"""
from __future__ import annotations

import bisect
import json
import re
import sqlite3
import time
from typing import Any

from . import interest as I
from .live_stream import TYPES, segment_durations

MAX_MOMENTS = 8
MIN_LEVEL = 6             # a moment the booth rates under this (out of 10) isn't a big one...
OK_LEVEL = 5              # ...and a solid shot under it (OK_*) fills the reel's other places
MIN_FINAL = 4.5           # ...nor one whose score, less what's already picked, is this or less (a bad 5 is 4.75)
PRE = 3.0                 # seconds of the stream before a shot's time (it shows ~4 s after it)
POST_PUTT, POST_SHOT, POST_BIG = 12.0, 16.0, 20.0   # ...and after: a putt, a full shot, a 9 or 10's call
# ...or, where the booth's lines were logged, till it's done talking about it, and never mid-line:
# each line logged as it goes to air, an exchange's back to back, at about this many words a second,
# heard on the stream TALK_LAG later (the stream's own delay, and the call's voicing); and as a cut
# lands on the segment after (TALK_LOOKAHEAD), a line starting that soon finishes too, up to the
# next swing
TALK_WORDS_PER_SECOND, TALK_GAP, TALK_TAIL, TALK_LAG, TALK_MAX = 2.7, 0.6, 1.5, 6.0, 45.0
TALK_LOOKAHEAD = 4.0
REACTION_SECONDS = 30.0   # a reaction (logged with no shot) is to the last shot this recently before it
CLOSE_FROM_YARDS, CLOSE_FEET = 100.0, 15.0          # the booth's close_approach_* defaults
BIG_DRIVE_YARDS = 300.0   # a round's longest drive from here is a moment
SHORT_MISS_FEET = 4.0     # a putt missed from inside this is a bad moment
OFFLINE_MISS_YARDS = 45.0  # ...and a full shot this far off line
OK_PUTT_FEET = 6.0        # solid shots: a putt holed from beyond this for par or better,
OK_GREEN_FEET = 25.0      # an approach (CLOSE_FROM_YARDS and up) to inside this,
OK_CHIP_FEET = 6.0        # a chip or pitch to inside this,
OK_DRIVE_YARDS = 280.0    # a drive this long in the fairway (a notch under the others)
PB_LEVEL = 7              # a player's longest drive or holed putt yet...
PB_HISTORY = 10           # ...once they've this many of them before the round to beat
NEAR_PB = {"drive": 0.95, "putt": 0.85}   # this share of it: close, a solid shot a notch up
NEAR_PB_LEVEL = 5.5
NOT_PUTT_COUNTED = ("scramble", "alternate")       # one ball between partners: nobody three-putts alone
# A shot's columns an older database may not have (stats_web.connect's views fill in only some).
DEFAULTS = {"picked": None, "hazard_x": None, "shared_ball": 0, "gimme_yards": None, "mulligan": 0, "left_out": 0,
            "fmt": None, "offline_yards": None, "outcome": None, "finish_yards": None, "total_yards": None,
            "to_pin_yards": None, "lie_before": None, "lie_after": None, "tags": None, "is_putt": 0, "shot_number": None}


def _par(label: str | None) -> int | None:
    m = re.search(r"par (\d)", label or "")
    return int(m.group(1)) if m else None


def _hole(label: str | None) -> int | None:
    m = re.search(r"Hole (\d+)", label or "")
    return int(m.group(1)) if m else None


def _ordinal_score(to_par: int) -> str:
    return {-3: "albatross", -2: "eagle", -1: "birdie", 0: "par"}.get(to_par, "")


def _names(text: str | None) -> list[str]:
    try:
        v = json.loads(text or "null")
    except (TypeError, ValueError):
        return []
    return [n for n in v if isinstance(n, str)] if isinstance(v, list) else []


def _penalties(hole: list[dict]) -> set[int]:
    """The ids of a player's strokes on a hole that went into a penalty area: an entry logged and the
    next stroke a number on (the penalty), or the ball seen in the water. GSPro can log an entry for a
    ball that ends up dry (hole_replay's rule too)."""
    out = set()
    for i, s in enumerate(hole):
        nxt = hole[i + 1] if i + 1 < len(hole) else None
        try:
            wet = "in_water" in json.loads(s["tags"] or "[]")
        except (TypeError, ValueError):
            wet = False
        if wet or (s["hazard_x"] is not None and nxt is not None and s["shot_number"] and nxt["shot_number"]
                   and nxt["shot_number"] > s["shot_number"] + 1):
            out.add(s["id"])
    return out


def _holes(shots: list[dict]) -> list[list[dict]]:
    """A round's strokes per player per hole, in order: a hole starts at stroke 1, when the stroke
    number goes back, or when the hole changes."""
    out: list[list[dict]] = []
    cur: dict[str, list[dict]] = {}
    for s in shots:
        h = cur.get(s["player"])
        if h is None or _hole(h[-1]["hole"]) != _hole(s["hole"]) or s["shot_number"] == 1 or \
                (s["shot_number"] or 0) <= (h[-1]["shot_number"] or 0):
            h = cur[s["player"]] = []
            out.append(h)
        h.append(s)
    return out


def _good(s: dict, par: int | None, hole: int | None, longest: dict | None) -> tuple[int, str, float] | None:
    """(excitement level, what happened, seconds of stream after it) for a shot worth a highlight."""
    who, on = s["player"], f" on {hole}" if hole else ""
    start, finish, lie = s["to_pin_yards"], s["finish_yards"], (s["lie_before"] or "").lower()
    if s["outcome"] == "holed":
        to_par = s["shot_number"] - par if s["shot_number"] and par else None
        name = _ordinal_score(to_par) if to_par is not None else ""
        if s["is_putt"]:
            feet = start * 3 if start else None
            level = I.holed_putt_level(feet, to_par)
            what = f"{who} holes {f'a {round(feet)} ft putt' if feet else 'the putt'}{f' for {name}' if name else ''}{on}"
            if not level and feet and feet > OK_PUTT_FEET and to_par is not None and to_par <= 0:
                level = OK_LEVEL
            return (level, what, POST_PUTT) if level else None
        if s["shot_number"] == 1:
            return 10, f"Hole in one! {who}{on}", POST_BIG
        level = I.holed_out_level(start, s["shot_number"], par, CLOSE_FROM_YARDS)
        return level, f"{who} holes out from {round(start) if start else '?'} yd{f' for {name}' if name else ''}{on}", POST_BIG
    split = (lie == "tee" and (par or 0) >= 4 and (s["total_yards"] or 0) >= OK_DRIVE_YARDS
             and (s.get("lie_after") or "").lower() == "fairway")
    if s["is_putt"] or lie == "green" or finish is None:
        if longest is s:
            yd = s["total_yards"]
            return (7 if yd >= BIG_DRIVE_YARDS + 30 else 6), f"{who} bombs a {round(yd)} yd drive{on}", POST_SHOT
        if split:
            return OK_LEVEL - 0.2, f"{who} splits the fairway, {round(s['total_yards'])} yd{on}", POST_SHOT
        return None
    feet = finish * 3
    if start and start >= CLOSE_FROM_YARDS and feet <= CLOSE_FEET:
        level = I.stuffed_level(feet)
        if s["outcome"] == "conceded" or (s["gimme_yards"] and finish <= s["gimme_yards"]):
            level = max(level, I.approach_gimme_level(start, feet))
        return level, f"{who} stuffs it to {max(1, round(feet))} ft from {round(start)} yd{on}", POST_SHOT
    if start and (s["outcome"] == "conceded" or (s["gimme_yards"] and finish <= s["gimme_yards"])):
        level = I.short_game_gimme_level(start, lie, feet)
        if level:
            return level, f"{who} chips to {max(1, round(feet))} ft from {round(start)} yd{on}", POST_SHOT
    if longest is s:
        yd = s["total_yards"]
        return (7 if yd >= BIG_DRIVE_YARDS + 30 else 6), f"{who} bombs a {round(yd)} yd drive{on}", POST_SHOT
    if start and start >= CLOSE_FROM_YARDS and feet <= OK_GREEN_FEET and (s.get("lie_after") or "").lower() == "green":
        return OK_LEVEL, f"{who} finds the green, {round(feet)} ft from {round(start)} yd{on}", POST_SHOT
    if start and start < CLOSE_FROM_YARDS and feet <= OK_CHIP_FEET:
        return OK_LEVEL, f"{who} chips to {max(1, round(feet))} ft from {round(start)} yd{on}", POST_SHOT
    if split:
        return OK_LEVEL - 0.2, f"{who} splits the fairway, {round(s['total_yards'])} yd{on}", POST_SHOT
    return None


def _bad(s: dict, hole: list[dict], team_ball: bool, penalised: bool) -> tuple[int, str, float] | None:
    """(how bad, what happened, seconds after) for a shot that's a highlight for the wrong reasons."""
    who, on = s["player"], f" on {_hole(s['hole'])}" if _hole(s["hole"]) else ""
    if penalised:
        return 7, f"{who} finds a penalty area{on}", POST_SHOT
    if s["is_putt"]:
        feet = (s["to_pin_yards"] or 0) * 3
        if s["outcome"] in ("missed", "conceded") and 0 < feet <= SHORT_MISS_FEET:
            return 6, f"{who} misses from {max(1, round(feet))} ft{on}", POST_PUTT
        if team_ball:
            return None   # one ball between partners: nobody three-putts alone
        putts = sum(1 for x in hole if x["is_putt"])   # the conceded tap-in counts
        # (pick() plays it from the first putt)
        if putts >= 3 and s is [x for x in hole if x["is_putt"] and x["outcome"] != "gimme"][-1]:
            return (6 if putts >= 4 else 5), f"{who} {'four' if putts >= 4 else 'three'}-putts{on}", POST_PUTT
        return None
    if (s["offline_yards"] is not None and abs(s["offline_yards"]) >= OFFLINE_MISS_YARDS
            and (s["lie_before"] or "").lower() not in ("green", "")):
        return 5, f"{who} sprays one {round(abs(s['offline_yards']))} yd off line{on}", POST_SHOT
    return None


def _result(hole: list[dict], par: int | None) -> int | None:
    """The hole's score against par (its last stroke's number less par); None unless it was finished."""
    last = hole[-1]
    if not par or last["outcome"] not in ("holed", "gimme") or not last["shot_number"]:
        return None
    return last["shot_number"] - par


def _is_drive(s: dict) -> bool:
    return (s["lie_before"] or "").lower() == "tee" and not s["is_putt"] and (_par(s["hole"]) or 0) >= 4 \
        and bool(s["total_yards"]) and s["picked"] != 0


def _is_holed_putt(s: dict) -> bool:
    return bool(s["is_putt"]) and s["outcome"] == "holed" and bool(s["to_pin_yards"])


def _bests(shots: list[dict], before: dict[str, dict]) -> tuple[set[int], dict[int, float]]:
    """The round's personal bests: each player's longest drive and longest holed putt of the round,
    when it beats their every one before it (`before`: player -> {"drive": [most, count], "putt": ...},
    in yards) and they'd PB_HISTORY of them to beat; and the ones that came close (NEAR_PB), with how
    far short (yards)."""
    out, near = set(), {}
    for kind, test, val in (("drive", _is_drive, lambda s: s["total_yards"]),
                            ("putt", _is_holed_putt, lambda s: s["to_pin_yards"])):
        top: dict[str, dict] = {}
        for s in shots:
            if test(s) and (s["player"] not in top or val(s) > val(top[s["player"]])):
                top[s["player"]] = s
        for who, s in top.items():
            most, count = (before.get(who) or {}).get(kind, (0.0, 0))
            if count < PB_HISTORY or (kind == "putt" and val(s) * 3 <= OK_PUTT_FEET):
                continue
            if val(s) > most:
                out.add(s["id"])
            elif val(s) >= NEAR_PB[kind] * most:
                near[s["id"]] = most - val(s)
    return out, near


def _talk(db: sqlite3.Connection, shots: list[dict]) -> tuple:
    """(shot id -> when the booth was done talking about it: its call and any reaction to it;
    session -> every line's (started, done); session -> its shots' times), in logged time."""
    try:
        rows = db.execute("SELECT session_id, shot_id, ts, kind, text FROM lines ORDER BY ts, id").fetchall()
    except sqlite3.OperationalError:
        return ()   # (a database from before the booth logged its lines)
    by_session: dict[int, list[dict]] = {}
    for s in shots:
        by_session.setdefault(s["session_id"], []).append(s)
    times = {k: [s["ts"] for s in v] for k, v in by_session.items()}
    batches: dict[tuple[int, float], list] = {}   # an exchange's lines share the time they're logged
    for session, shot_id, ts, kind, text in rows:
        b = batches.setdefault((session, ts), [[], set()])
        b[0].append(len((text or "").split()) / TALK_WORDS_PER_SECOND)
        if shot_id is None and kind == "reaction":
            i = bisect.bisect_right(times.get(session, []), ts) - 1
            if i >= 0 and ts - times[session][i] <= REACTION_SECONDS:
                shot_id = by_session[session][i]["id"]
        if shot_id is not None and kind in ("shot", "reaction"):
            b[1].add(shot_id)
    per_shot: dict[int, float] = {}
    spoken: dict[int, list[tuple[float, float]]] = {}
    busy: dict[int, float] = {}
    for (session, ts), (lines, about) in sorted(batches.items(), key=lambda kv: kv[0][1]):
        at = max(ts, busy.get(session, 0.0))   # (an exchange waits for the one before it)
        for seconds in lines:
            spoken.setdefault(session, []).append((at, at + seconds))
            at += seconds + TALK_GAP
        done = busy[session] = at - TALK_GAP
        for shot_id in about:
            per_shot[shot_id] = max(per_shot.get(shot_id, 0.0), done)
    return per_shot, spoken, times


def _post(post: float, at: dict, last: dict, talk: tuple | None) -> float:
    """Seconds of stream after `at`: `post`, or on till the booth's done talking about `last`; and
    never stopping mid-line (one under way at the cut finishes, though not whatever follows it)."""
    if not talk:
        return post
    per_shot, spoken, swings = talk
    end = at["ts"] + post - TALK_LAG   # (when what's heard at the cut was logged)
    if (done := per_shot.get(last["id"])) is not None:
        end = max(end, done + TALK_TAIL)
    times = swings.get(at.get("session_id"), [])
    for start, done in spoken.get(at.get("session_id"), []):
        if done <= end - TALK_TAIL:
            continue
        i = bisect.bisect_right(times, max(end, last["ts"]))
        swing = times[i] if i < len(times) else float("inf")   # the next shot not already in it
        if start >= end + TALK_LOOKAHEAD or (start >= end and start >= swing - 1):
            break
        end = done + TALK_TAIL
    # (at most TALK_MAX after the last shot of it: a three-putt's run of putts comes first)
    return round(max(post, min(TALK_MAX + last["ts"] - at["ts"], end + TALK_LAG - at["ts"])), 1)


def pick(shots: list[dict], most: int = MAX_MOMENTS, sides: dict[str, int] | None = None,
         before: dict[str, dict] | None = None, talk: tuple | None = None) -> list[dict]:
    """A round's highlight moments, in the order they happened. Picked best first (the booth's
    excitement, a notch more for a shot that set up a birdie or better; solid shots, OK_*, fill in), a
    player's next pick past the others' and a hole's next counting for a little less, so one hot hand
    or a scramble's chips on one hole don't fill it: at most `most`, two a hole, one bad one a player,
    and no more bad ones than good. `shots` are the round's, in
    order (dicts with the shots table's columns); mulligans and strokes left out of the stats are skipped.
    `before`: each player's bests before the round (_bests), for a personal best to count as a big one.
    `sides`: each player's side, when the round had more than one (a team's one ball is holed by
    whoever holed it; with a player's side unknown, nobody else's result counts for theirs)."""
    shots = [{**DEFAULTS, **s} for s in shots]
    shots = [s for s in shots if not s["mulligan"] and not s["left_out"] and s["player"] and s["player"] != "unknown"]
    holes = _holes(shots)
    wet = {i for h in holes for i in _penalties(h)}
    drives = [s for s in shots if s["lie_before"] == "tee" and not s["is_putt"] and (_par(s["hole"]) or 0) >= 4
              and s["id"] not in wet and s["picked"] != 0
              and s["total_yards"] and s["total_yards"] >= BIG_DRIVE_YARDS]
    longest = max(drives, key=lambda s: s["total_yards"], default=None)
    pbs, near = _bests([s for s in shots if s["id"] not in wet], before or {})
    side = (lambda p: 0) if sides is None else sides.get
    team_done = {(side(h[0]["player"]), _hole(h[-1]["hole"])): h[-1]["shot_number"] for h in holes
                 if (h[0]["fmt"] or "") in NOT_PUTT_COUNTED and h[-1]["outcome"] in ("holed", "gimme")
                 and side(h[0]["player"]) is not None}
    pool = []
    for i, hole in enumerate(holes):
        par, number = _par(hole[0]["hole"]), _hole(hole[-1]["hole"])
        team_ball = (hole[0]["fmt"] or "") in NOT_PUTT_COUNTED or bool(hole[0]["shared_ball"])
        result = _result(hole, par)
        done = team_done.get((side(hole[0]["player"]), number))
        if team_ball and result is None and par and done and side(hole[0]["player"]) is not None:
            result = done - par
        for s in hole:
            if s["outcome"] == "gimme":
                continue   # the conceded tap-in: never struck, nothing to watch
            # (a penalty's never a good one, however close it ends up)
            g = _good(s, par, number, longest) if s["picked"] != 0 and s["id"] not in wet else None
            if s["id"] in pbs:
                if s["is_putt"]:
                    g = (max(g[0] if g else 0, PB_LEVEL), (g[1] if g else
                         f"{s['player']} holes a {round(s['to_pin_yards'] * 3)} ft putt{f' on {number}' if number else ''}")
                         + ", a personal best", POST_PUTT)
                elif g and g[0] > PB_LEVEL:   # (a hole in one, say: keep it, and its clip)
                    g = (g[0], f"{g[1]}, {s['player']}'s longest drive yet", g[2])
                else:
                    g = (PB_LEVEL, f"{s['player']}'s longest drive yet: {round(s['total_yards'])} yd"
                         f"{f' on {number}' if number else ''}", POST_SHOT)
            elif s["id"] in near and (not g or g[0] < NEAR_PB_LEVEL):
                short = round(near[s["id"]] * (3 if s["is_putt"] else 1))
                unit = "ft" if s["is_putt"] else "yd"
                if s["is_putt"]:
                    base = g[1] if g else f"{s['player']} holes a {round(s['to_pin_yards'] * 3)} ft putt{f' on {number}' if number else ''}"
                else:
                    base = f"{s['player']} hits a {round(s['total_yards'])} yd drive{f' on {number}' if number else ''}"
                g = (NEAR_PB_LEVEL, f"{base}, {max(1, short)} {unit} off a personal best",
                     POST_PUTT if s["is_putt"] else POST_SHOT)
            if g and g[0] >= OK_LEVEL - 0.2:
                what, score = g[1], g[0]
                if s["outcome"] != "holed" and s is not longest and s["id"] not in pbs and s["id"] not in near and result is not None and result <= 0:
                    what += {-3: " to set up albatross", -2: " to set up eagle", -1: " to set up birdie"}.get(
                        result, " to save par" if (s["lie_before"] or "").lower() != "fairway" or result == 0 else "")
                    score += result < 0
                pool.append({"shot": s, "level": g[0], "score": score, "what": what, "post": _post(g[2], s, s, talk), "bad": False,
                             "hole": number if number is not None else -i})
            elif (b := _bad(s, hole, team_ball, s["id"] in wet)) and b[0] >= MIN_LEVEL - 1:
                at, post = s, b[2]
                if "-putts" in b[1]:   # the whole run of putts, from the first
                    at = next(x for x in hole if x["is_putt"])
                    post += max(0.0, s["ts"] - at["ts"])
                pool.append({"shot": at, "level": b[0], "score": b[0] - 0.25, "what": b[1], "post": _post(post, at, s, talk), "bad": True,
                             "hole": number if number is not None else -i})
    picked: list[dict] = []
    per_player = dict.fromkeys({m["shot"]["player"] for m in pool}, 0)   # (one with nothing to show: no say)
    per_hole: dict[int, list[dict]] = {}
    bad_by: set[str] = set()
    while len(picked) < most:
        best, best_score = None, MIN_FINAL
        for m in pool:
            who, on = m["shot"]["player"], per_hole.get(m["hole"], [])
            # (a bad one only behind as many good ones: the reel's never mostly mistakes)
            if m in picked or len(on) >= 2 or (m["bad"] and (who in bad_by or len(bad_by) >= len(picked) - len(bad_by))) \
                    or any(x["shot"] is m["shot"] for x in on):
                continue
            # (against the player with fewest: a solo round isn't held back)
            score = m["score"] - 0.75 * (per_player[who] - min(per_player.values())) - 1.0 * len(on)
            if score > best_score:
                best, best_score = m, score
        if best is None:
            break
        picked.append(best)
        who = best["shot"]["player"]
        per_player[who] += 1
        per_hole.setdefault(best["hole"], []).append(best)
        if best["bad"]:
            bad_by.add(who)
    return sorted(picked, key=lambda m: (m["shot"]["ts"], m["shot"]["id"]))


def rounds(db: sqlite3.Connection) -> dict[int, dict]:
    """Every round's reel: round -> {label, date, league, ended, moments: [{id, ts, player, hole, what,
    bad, post}]}, for rounds with two moments or more (`db`: stats_web.connect's)."""
    from .stats_web import round_groups, shot_rounds

    groups, of_row = round_groups(db)
    of_shot = shot_rounds(db)
    by_round: dict[int, list[dict]] = {}
    every = [dict(r) for r in db.execute("SELECT * FROM shots ORDER BY ts, id")]
    for r in every:
        if (g := of_shot.get(r["id"])) is not None:
            by_round.setdefault(g, []).append(r)
    talk = _talk(db, every)
    league, rows = set(), {}
    for r in db.execute("SELECT * FROM rounds"):
        league |= {of_row[r["id"]]} if "is_league" in r.keys() and r["is_league"] else set()
        rows.setdefault(of_row[r["id"]], []).append(r)
    # Each round's players' bests before it: every shot of theirs before its first, any round or none.
    before_round: dict[int, dict] = {}
    running: dict[str, dict] = {}
    for r in db.execute("SELECT * FROM shots ORDER BY ts, id"):
        s = {**DEFAULTS, **dict(r)}
        g = of_shot.get(s["id"])
        if g is not None and g not in before_round:
            before_round[g] = {p: {k: tuple(v) for k, v in b.items()} for p, b in running.items()}
        if s["mulligan"] or s["left_out"] or not s["player"]:
            continue
        for kind, test, val in (("drive", _is_drive, "total_yards"), ("putt", _is_holed_putt, "to_pin_yards")):
            if test(s):
                b = running.setdefault(s["player"], {}).setdefault(kind, [0.0, 0])
                b[0], b[1] = max(b[0], s[val]), b[1] + 1
    out: dict[int, dict] = {}
    for g in groups:
        shots = by_round.get(g["round"], [])
        sides = None
        if len(rows.get(g["round"], [])) > 1:   # sides by the players each named (an older row: none)
            sides = {n: i for i, r in enumerate(rows[g["round"]])
                     for n in (_names(r["players"]) if "players" in r.keys() else None) or []}
        moments = pick(shots, sides=sides, before=before_round.get(g["round"], {}), talk=talk)
        if len(moments) < 2:
            continue
        players = list(dict.fromkeys(s["player"] for s in shots if s["player"] and s["player"] != "unknown"))
        out[g["round"]] = {
            "label": " · ".join(x for x in (g["course"], " & ".join(players)) if x),
            "date": time.strftime("%Y-%m-%d", time.localtime(g["end"])), "ended": g["end"],
            "league": g["round"] in league,
            "moments": [{"id": m["shot"]["id"], "ts": m["shot"]["ts"], "player": m["shot"]["player"],
                         "hole": _hole(m["shot"]["hole"]), "what": m["what"], "bad": m["bad"], "post": m["post"]}
                        for m in moments]}
    return out


def _get(client: Any, bucket: str, key: str) -> bytes | None:
    try:
        return client.get_object(Bucket=bucket, Key=key)["Body"].read()
    except client.exceptions.NoSuchKey:
        return None


def playlist(parts: list[list[tuple[str, float]]]) -> bytes:
    """A closed VOD playlist of `parts` (each a run of (uri, seconds)), a discontinuity between them."""
    target = max([6] + [int(d + 0.999) for p in parts for _, d in p])
    body = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-PLAYLIST-TYPE:VOD", f"#EXT-X-TARGETDURATION:{target}",
            "#EXT-X-MEDIA-SEQUENCE:0"]
    for i, part in enumerate(parts):
        if i:
            body.append("#EXT-X-DISCONTINUITY")
        body += [x for uri, d in part for x in (f"#EXTINF:{d:.3f},", uri)]
    body.append("#EXT-X-ENDLIST")
    return ("\n".join(body) + "\n").encode()


def keep(reel: dict, now: float, keep_days: float, season_from: str | None) -> bool:
    """Whether a round's reel stays: a league one for the season, any other while streams are kept."""
    if reel.get("league"):
        return not season_from or reel.get("date", "") >= season_from
    return now - reel.get("ended", 0) <= keep_days * 86400


def sync(client: Any, bucket: str, prefix: str, reels: dict[int, dict], season_from: str | None = None,
         clock=time.time) -> dict[str, int]:
    """Cut a reel for each of `reels` (rounds.py's) that has none yet, or whose moments changed while
    its stream is still kept, and delete the reels no longer wanted (a round gone off the page, a
    practice round's past its streams' keep, a league one from a past season)."""
    root = (prefix.strip("/") + "/" if prefix.strip("/") else "") + "live/"
    raw = _get(client, bucket, root + "recordings.json")
    recs_body = json.loads(raw) if raw else {}
    recordings = [r for r in recs_body.get("recordings") or [] if isinstance(r, dict)]
    keep_days = recs_body.get("keep_days") if isinstance(recs_body.get("keep_days"), (int, float)) else 7
    saved = _get(client, bucket, root + "reels.json")
    index = {k: v for k, v in ((json.loads(saved).get("reels") or {}) if saved else {}).items() if isinstance(v, dict)}
    now = clock()
    wanted = {str(k): v for k, v in reels.items() if keep(v, now, keep_days, season_from)}
    playlists: dict[str, tuple[list[tuple[str, float]], bool]] = {}
    added = changed = 0
    for key, reel in wanted.items():
        ids = [m["id"] for m in reel["moments"]]
        cuts = [[m["id"], m["post"]] for m in reel["moments"]]   # what the playlist is cut from
        old_entry = index.get(key)
        if old_entry and old_entry.get("cuts") == cuts:
            # The same stretches of stream: only what's said about them can have changed.
            fresh = {**old_entry, **{k: reel[k] for k in ("label", "date", "league", "ended")},
                     "moments": [{**o, **{k: m[k] for k in ("player", "hole", "what", "bad")}}
                                 for o, m in zip(old_entry.get("moments") or [], reel["moments"])], "cuts": cuts}
            if fresh != old_entry:
                index[key], changed = fresh, changed + 1
            continue
        cut = _cut(client, bucket, root, recordings, playlists, reel, now)
        if cut is None:
            if old_entry:   # the old reel stands, as the round is now (a league round's keep goes by it)
                fresh = {**old_entry, **{k: reel[k] for k in ("label", "date", "league", "ended")}}
                if fresh != old_entry:
                    index[key], changed = fresh, changed + 1
            continue   # not streamed (yet, or any more): the next publish
        parts, moments = cut
        old = [o["Key"] for page in client.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=f"{root}reels/{key}/") for o in page.get("Contents", [])]
        new = set()
        for part in parts:
            for session, name, _ in part:
                k = f"{root}reels/{key}/{session}/{name}"
                new.add(k)
                if k not in old:
                    client.copy_object(Bucket=bucket, Key=k, CopySource={"Bucket": bucket, "Key": f"{root}s/{session}/{name}"},
                                       MetadataDirective="REPLACE", CacheControl="public, max-age=31536000, immutable",
                                       ContentType=TYPES.get("." + name.rsplit(".", 1)[-1], "application/octet-stream"))
        client.put_object(Bucket=bucket, Key=f"{root}reels/{key}/reel.m3u8",
                          Body=playlist([[(f"{s}/{n}", d) for s, n, d in p] for p in parts]),
                          CacheControl="no-cache", ContentType=TYPES[".m3u8"])
        new.add(f"{root}reels/{key}/reel.m3u8")
        stale = [k for k in old if k not in new]
        for i in range(0, len(stale), 1000):
            client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in stale[i:i + 1000]]})
        index[key] = {**{k: reel[k] for k in ("label", "date", "league", "ended")}, "ids": ids, "cuts": cuts,
                      "duration": round(sum(d for p in parts for *_, d in p), 1), "moments": moments}
        added += 1
    gone = [k for k in index if k not in wanted]
    for k in gone:
        del index[k]
    stale = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=root + "reels/"):
        stale += [o["Key"] for o in page.get("Contents", []) if o["Key"][len(root) + 6:].split("/", 1)[0] not in index]
    if added or changed or gone or saved is None:
        client.put_object(Bucket=bucket, Key=root + "reels.json", Body=json.dumps({"reels": index}).encode(),
                          CacheControl="no-cache", ContentType=TYPES[".json"])
    for i in range(0, len(stale), 1000):
        client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in stale[i:i + 1000]]})
    return {"kept": len(index), "added": added, "removed": len(gone)}


def _cut(client: Any, bucket: str, root: str, recordings: list[dict], playlists: dict, reel: dict,
         now: float) -> tuple[list[list[tuple[str, str, float]]], list[dict]] | None:
    """The reel's parts (each a run of (session, segment, seconds)) and its moments with where each
    starts in it; None unless every moment is in a kept stream that's streamed past it. Moments
    whose stretches of stream overlap play as one part."""
    spans: list[tuple[str, int, int, list[dict]]] = []   # (session, first segment, last segment, moments)
    for m in reel["moments"]:
        rec = next((r for r in recordings if r.get("session") and r.get("since") is not None
                    and r["since"] <= m["ts"] <= (r.get("ended") or now) + 1), None)
        if rec is None:
            return None
        session = rec["session"]
        if session not in playlists:
            text = _get(client, bucket, f"{root}s/{session}/full.m3u8")
            playlists[session] = (list(segment_durations(text.decode("utf-8", "replace")).items()), "#EXT-X-ENDLIST" in text.decode("utf-8", "replace")) if text else ([], False)
        track, ended = playlists[session]
        t, start, idx = m["ts"] - rec["since"], 0.0, []
        for i, (_, dur) in enumerate(track):
            if start + dur > t - PRE and start < t + m["post"]:
                idx.append(i)
            start += dur
        if not idx or (start < t + m["post"] and not ended):
            return None
        if spans and spans[-1][0] == session and idx[0] <= spans[-1][2] + 1:
            s = spans[-1]
            spans[-1] = (session, s[1], max(s[2], idx[-1]), s[3] + [{**m, "_t": t}])
        else:
            spans.append((session, idx[0], idx[-1], [{**m, "_t": t}]))
    parts, moments, offset = [], [], 0.0
    for session, a, b, ms in spans:
        track = playlists[session][0]
        first = sum(d for _, d in track[:a])
        for m in ms:
            moments.append({"at": round(offset + max(0.0, m["_t"] - first - 1.0), 1),
                            **{k: m[k] for k in ("id", "player", "hole", "what", "bad")}})
        part = [(session, name, d) for name, d in track[a:b + 1]]
        parts.append(part)
        offset += sum(d for *_, d in part)
    return parts, moments
