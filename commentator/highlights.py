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
from .live_stream import SHOT_SHOWS, TYPES, segment_durations

MAX_MOMENTS = 8
REEL_SECONDS = 180.0      # ...and no more than fit in this long a reel (each clip: PRE, its post, and
CLIP_SLACK = 8.0          # this, as its ends round out to the stream's 4 s segments)
MIN_LEVEL = 6             # a moment the booth rates under this (out of 10) isn't a big one...
OK_LEVEL = 5              # ...and a solid shot under it (OK_*) fills the reel's other places
MIN_FINAL = 4.5           # ...nor one whose score, less what's already picked, is this or less (a bad 5 is 4.75)
PRE = 3.0                 # seconds of the stream before a shot's time (it shows ~4 s after it), as reels were sized
LEAD_IN = 2.0             # ...but a clip starts this long before the swing shows (SHOT_SHOWS after its time)
WATCH_LEAD = 1.0          # ...and the moment's `at` a second before it
POST_PUTT, POST_SHOT, POST_BIG = 12.0, 16.0, 20.0   # ...and after: a putt, a full shot, a 9 or 10's call
# ...or, where the booth's lines were logged, till it's done talking about it (its call, reaction
# and flight lines), stopping before anything else it says (the next shot's setup, a segment,
# banter) or the next swing: each line logged as it goes to air, an exchange's back to back, at about
# this many words a second, heard on the stream TALK_LAG later (the stream's own delay, and the
# call's voicing)
TALK_WORDS_PER_SECOND, TALK_GAP, TALK_TAIL, TALK_LAG, TALK_MAX = 2.7, 0.6, 1.5, 6.0, 45.0
STREAM_LAG = 4.5          # where the booth logged when a line played (aired_at): heard this much later
STOP_MARGIN = 2.0         # a clip ends this far ahead of the next thing said (a line's start can be early)
MUST_GRACE = 2.5         # ...and drops a segment holding both it and only this much of the clip's own talk
MIN_AFTER = 8.0           # ...but not before this long after its last shot (the ball comes to rest)
REACTION_SECONDS = 30.0   # a reaction (logged with no shot) is to the last shot this recently before it
FLIGHT_SECONDS = 15.0     # ...and a flight line to the one this recently
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
    """(session -> every line's (started, done, the time of the shot it's about: its call, a reaction
    or flight line, or None); session -> its shots' times), in logged time. A line the booth noted
    the air time of is placed by that (and in a session with any, one without never aired: an
    interrupted exchange's rest); others by their words, after the exchange before."""
    try:
        rows = db.execute("SELECT session_id, shot_id, ts, kind, text, aired_at, aired_seconds FROM lines"
                          " ORDER BY ts, id").fetchall()
    except sqlite3.OperationalError:
        try:
            rows = [(*r, None, None) for r in db.execute(
                "SELECT session_id, shot_id, ts, kind, text FROM lines ORDER BY ts, id")]
        except sqlite3.OperationalError:
            return ()   # (a database from before the booth logged its lines)
    noted = {r[0] for r in rows if r[5] is not None}
    shift = STREAM_LAG - TALK_LAG   # (an aired time, as the logged time it's heard the same as)
    times: dict[int, list[float]] = {}
    for s in shots:
        times.setdefault(s["session_id"], []).append(s["ts"])
    ts_of = {s["id"]: s["ts"] for s in shots}
    batches: dict[tuple[int, float], list] = {}   # an exchange's lines share the time they're logged
    for session, shot_id, ts, kind, text, aired_at, aired_seconds in rows:
        if session in noted and aired_at is None:
            continue
        about = ts_of.get(shot_id) if kind in ("shot", "reaction") else None
        if about is None and kind in ("reaction", "flight"):
            i = bisect.bisect_right(times.get(session, []), ts) - 1
            if i >= 0 and ts - times[session][i] <= (REACTION_SECONDS if kind == "reaction" else FLIGHT_SECONDS):
                about = times[session][i]
        batches.setdefault((session, ts), []).append(
            ((aired_at + shift, aired_at + shift + (aired_seconds or 0.0)) if aired_at is not None
             else len((text or "").split()) / TALK_WORDS_PER_SECOND, about))
    spoken: dict[int, list[tuple[float, float, float | None]]] = {}
    busy: dict[int, float] = {}
    for (session, ts), lines in sorted(batches.items(), key=lambda kv: kv[0][1]):
        at = max(ts, busy.get(session, 0.0))   # (an exchange waits for the one before it)
        done = at
        for seconds, about in lines:
            if isinstance(seconds, tuple):
                spoken.setdefault(session, []).append((*seconds, about))
                done = at = max(done, seconds[1])
                at += TALK_GAP
                continue
            spoken.setdefault(session, []).append((at, at + seconds, about))
            done = at + seconds
            at = done + TALK_GAP
        busy[session] = done
    for v in spoken.values():
        v.sort(key=lambda x: x[:2])
    return spoken, times


def _post(post: float, at: dict, last: dict, talk: tuple | None) -> dict:
    """The stretch of stream after `at` (seconds): `post`, or on till the booth's done talking about
    the shots from `at` to `last`; and where the booth logged its lines, `stop` before whatever it says
    next or the next swing, though never before `must` (its own lines' end; None: no stop)."""
    if not talk:
        return {"post": post, "stop": None, "must": 0.0}
    spoken, swings = talk
    session = at.get("session_id")
    lo, hi = at["ts"], last["ts"]
    end = at["ts"] + post - TALK_LAG   # (when what's heard at the cut was logged)
    own = [done for _, done, about in spoken.get(session, []) if about is not None and lo <= about <= hi]
    must = max(hi + MIN_AFTER - TALK_LAG, *(d + TALK_TAIL for d in own)) if own else hi + MIN_AFTER - TALK_LAG
    end = max(end, must)
    times = swings.get(session, [])
    i = bisect.bisect_right(times, hi)
    stop = times[i] + STREAM_LAG - TALK_LAG if i < len(times) else None   # (the next swing, as heard)
    for start, _, about in spoken.get(session, []):
        if start > hi and not (about is not None and lo <= about <= hi):
            stop = start if stop is None else min(stop, start)
            break
    if stop is not None:
        stop -= STOP_MARGIN
        end = min(end, max(stop, must))
    # (at most TALK_MAX after the last shot of it: a three-putt's run of putts comes first)
    cap = TALK_MAX + hi - TALK_LAG   # (as logged)
    rel = lambda t: round(min(cap, t) + TALK_LAG - at["ts"], 1)
    return {"post": rel(end), "stop": None if stop is None else rel(max(stop, must)), "must": rel(must)}


def pick(shots: list[dict], most: int = MAX_MOMENTS, sides: dict[str, int] | None = None,
         seconds: float = REEL_SECONDS,
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
                pool.append({"shot": s, "level": g[0], "score": score, "what": what, **_post(g[2], s, s, talk), "bad": False,
                             "hole": number if number is not None else -i})
            elif (b := _bad(s, hole, team_ball, s["id"] in wet)) and b[0] >= MIN_LEVEL - 1:
                at, post = s, b[2]
                if "-putts" in b[1]:   # the whole run of putts, from the first
                    at = next(x for x in hole if x["is_putt"])
                    post += max(0.0, s["ts"] - at["ts"])
                pool.append({"shot": at, "level": b[0], "score": b[0] - 0.25, "what": b[1], **_post(post, at, s, talk), "bad": True,
                             "hole": number if number is not None else -i})
    picked: list[dict] = []
    per_player = dict.fromkeys({m["shot"]["player"] for m in pool}, 0)   # (one with nothing to show: no say)
    per_hole: dict[int, list[dict]] = {}
    bad_by: set[str] = set()
    used = 0.0
    clip = lambda m: PRE + m["post"] + CLIP_SLACK
    while len(picked) < most:
        best, best_score = None, MIN_FINAL
        for m in pool:
            who, on = m["shot"]["player"], per_hole.get(m["hole"], [])
            # (a bad one only behind as many good ones: the reel's never mostly mistakes)
            if m in picked or len(on) >= 2 or (m["bad"] and (who in bad_by or len(bad_by) >= len(picked) - len(bad_by))) \
                    or any(x["shot"] is m["shot"] for x in on) or used + clip(m) > seconds:
                continue
            # (against the player with fewest: a solo round isn't held back)
            score = m["score"] - 0.75 * (per_player[who] - min(per_player.values())) - 1.0 * len(on)
            if score > best_score:
                best, best_score = m, score
        if best is None:
            break
        picked.append(best)
        used += clip(best)
        who = best["shot"]["player"]
        per_player[who] += 1
        per_hole.setdefault(best["hole"], []).append(best)
        if best["bad"]:
            bad_by.add(who)
    return sorted(picked, key=lambda m: (m["shot"]["ts"], m["shot"]["id"]))


def finish(shots: list[dict], talk: tuple | None = None) -> dict | None:
    """The shot that finished a round, for a league recap to close on (not one of its reel's moments):
    the last struck on the round's last hole that holed out, or left a gimme. None if it wasn't finished."""
    shots = [{**DEFAULTS, **s} for s in shots]
    shots = [s for s in shots if not s["mulligan"] and not s["left_out"] and s["player"] and s["player"] != "unknown"]
    number = _hole(shots[-1]["hole"]) if shots else None
    if number is None:
        return None
    ends = []
    for h in _holes(shots):
        struck = [s for s in h if s["outcome"] != "gimme"]
        if _hole(h[-1]["hole"]) == number and h[-1]["outcome"] in ("holed", "gimme") and struck:
            ends.append((h, struck[-1]))
    if not ends:
        return None
    hole, s = max(ends, key=lambda e: (e[1]["ts"], e[1]["id"]))
    result = _result(hole, _par(hole[0]["hole"]))
    score = (_ordinal_score(result) or {1: "bogey", 2: "double bogey"}.get(result, "")) if result is not None else ""
    end = (f" for {score}" if score else "") + " to finish"
    if s["outcome"] == "holed" and s["is_putt"] and s["to_pin_yards"]:
        what = f"{s['player']} holes a {round(s['to_pin_yards'] * 3)} ft putt on {number}{end}"
    elif s["outcome"] == "holed":
        what = f"{s['player']} holes out on {number}{end}"
    else:
        what = f"{s['player']} rolls it up for a gimme on {number}{end}"
    return {"shot": s, "level": OK_LEVEL, "what": what, "bad": False, "hole": number,
            **_post(POST_PUTT if s["is_putt"] else POST_SHOT, s, s, talk)}


LONG_PUTT_FEET = 10.0     # a putt holed from this far is worth showing where a lead turned (a tap-in isn't)
TURN_POST = 20.0          # seconds of stream after a turn's shot, for the booth to catch up over


def turn_shot(shots: list[dict], hole: int, want: str) -> dict | None:
    """A team's shot on `hole` that a league week's lead turned on, for its recap (`shots`: its round's,
    in order, only its players'): `miss`, the one that cost it (a penalty, else the last putt missed,
    else the last shot short of holing; any player's, as every one's might have counted); `made`, the
    longest putt holed or a hole-out (None for a tap-in, so a teammate's after doesn't hide it); `setup`,
    a full shot its player then one-putted, the closest (None unless inside OK_GREEN_FEET). Shaped as
    rounds()'s moments, cut on its own (cut_alone)."""
    shots = [{**DEFAULTS, **s} for s in shots]
    on = [s for s in shots if not s["mulligan"] and not s["left_out"] and s["player"] and s["player"] != "unknown"
          and _hole(s["hole"]) == hole]
    struck = [s for s in on if s["outcome"] != "gimme" and s["picked"] != 0]   # (not a scramble ball left)
    if not struck:
        return None
    where = f" on {hole}"
    shot, level, bad = None, 6, False
    if want == "miss":
        wet = {i for h in _holes(on) for i in _penalties(h)}
        misses = [s for s in struck if s["outcome"] != "holed"]
        putts = [s for s in misses if s["is_putt"]]
        if pen := [s for s in misses if s["id"] in wet]:
            shot, level, what = pen[-1], 7, f"{pen[-1]['player']} finds a penalty area{where}"
        elif putts:
            shot = putts[-1]
            what = f"{shot['player']} misses from {max(1, round((shot['to_pin_yards'] or 0) * 3))} ft{where}"
        elif misses:
            shot = misses[-1]
            start, left = shot["to_pin_yards"], shot["finish_yards"]
            what = (f"{shot['player']} sprays one {round(abs(shot['offline_yards']))} yd off line{where}"
                    if (shot["offline_yards"] is not None and abs(shot["offline_yards"]) >= OFFLINE_MISS_YARDS) else
                    f"{shot['player']} leaves it {round(left * 3)} ft away from {round(start)} yd{where}" if start and left
                    else f"{shot['player']}'s shot{where}")
        bad = True
    elif want == "made":
        holed = sorted((s for s in struck if s["outcome"] == "holed"),   # a hole-out, else the longest putt, last
                       key=lambda s: (not s["is_putt"], (s["to_pin_yards"] or 0) if s["is_putt"] else 0))
        if holed and holed[-1]["is_putt"] and (holed[-1]["to_pin_yards"] or 0) * 3 >= LONG_PUTT_FEET:
            shot, level = holed[-1], 7
            what = f"{shot['player']} holes a {round(shot['to_pin_yards'] * 3)} ft putt{where}"
        elif holed and not holed[-1]["is_putt"]:
            shot, level = holed[-1], 9
            what = (f"Hole in one! {shot['player']}{where}" if shot["shot_number"] == 1 else
                    f"{shot['player']} holes out from {round(shot['to_pin_yards'] or 0)} yd{where}")
    elif want == "setup":
        # A full shot its player's next stroke holed (or was given): what left the birdie putt.
        team_ball = (on[0]["fmt"] or "") in NOT_PUTT_COUNTED or bool(on[0]["shared_ball"])

        def one_putt(a: dict) -> bool:
            after = [b for b in on if b["shot_number"] == (a["shot_number"] or 0) + 1 and b["picked"] != 0
                     and (team_ball or b["player"] == a["player"])]
            return any(b["outcome"] in ("holed", "gimme") for b in after)
        full = [s for s in struck if not s["is_putt"] and s["finish_yards"] is not None and one_putt(s)]
        close = min(full, key=lambda s: s["finish_yards"]) if full else None
        if close is not None and close["finish_yards"] * 3 <= OK_GREEN_FEET:
            shot = close
            what = f"{shot['player']} hits it to {max(1, round(shot['finish_yards'] * 3))} ft"
            what += f" from {round(shot['to_pin_yards'])} yd{where}" if shot["to_pin_yards"] else where
    if shot is None:
        return None
    return {"id": shot["id"], "ts": shot["ts"], "player": shot["player"], "hole": hole, "what": what, "bad": bad,
            "level": level, **_post(TURN_POST, shot, shot, None)}


def cut_alone(client: Any, bucket: str, prefix: str, moments: list[dict],
              clock=time.time) -> dict[int, tuple[list[list[tuple[str, float]]], float]]:
    """Each of `moments` (turn_shot()'s) cut on its own from its stream, where that's still kept: id ->
    (its playlist's parts, (uri under live/s/, seconds); where the shot is in it)."""
    root = (prefix.strip("/") + "/" if prefix.strip("/") else "") + "live/"
    raw = _get(client, bucket, root + "recordings.json")
    try:
        recordings = [r for r in (json.loads(raw) if raw else {}).get("recordings") or [] if isinstance(r, dict)]
    except ValueError:
        recordings = []
    playlists: dict = {}
    out = {}
    for m in moments:
        got = _cut(client, bucket, root, recordings, playlists, {"moments": [m]}, clock())
        if got is not None:
            parts, (cut,) = got
            out[m["id"]] = ([[(f"{s}/{n}", d) for s, n, d in p] for p in parts], cut["at"])
    return out


def _entry(m: dict) -> dict:
    return {"id": m["shot"]["id"], "ts": m["shot"]["ts"], "player": m["shot"]["player"], "hole": _hole(m["shot"]["hole"]),
            "what": m["what"], "bad": m["bad"], "level": m["level"], "post": m["post"], "stop": m["stop"], "must": m["must"]}


def rounds(db: sqlite3.Connection) -> dict[int, dict]:
    """Every round's reel: round -> {label, date, league, ended, moments: [{id, ts, player, hole, what,
    bad, level, post, stop, must}]}, for rounds with two moments or more (`db`: stats_web.connect's);
    a league round's also `finish` (finish()'s, the same shape), cut on its own for the week's recap."""
    from .stats_web import _late_gimmes, round_groups, shot_rounds

    groups, of_row = round_groups(db)
    of_shot = shot_rounds(db)
    by_round: dict[int, list[dict]] = {}
    every = [dict(r) for r in db.execute("SELECT * FROM shots ORDER BY ts, id")]
    for r in every:
        if (g := of_shot.get(r["id"])) is not None:
            by_round.setdefault(g, []).append(r)
    # (a putt conceded after its round was recorded is that round's finish, though not in its reel)
    late = {sid: g for sid, g in _late_gimmes(db, groups, {}).items() if sid not in of_shot or of_shot[sid] != g}
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
            "moments": [_entry(m) for m in moments]}
        ends = sorted(shots + [r for r in every if late.get(r["id"]) == g["round"]], key=lambda r: (r["ts"], r["id"]))
        if g["round"] in league and (f := finish(ends, talk)):
            out[g["round"]]["finish"] = _entry(f)
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
    shifted = sum(_shift(v) for v in index.values())
    now = clock()
    wanted = {str(k): v for k, v in reels.items() if keep(v, now, keep_days, season_from)}
    playlists: dict[str, tuple[list[tuple[str, float]], bool]] = {}
    added = changed = 0
    for key, reel in wanted.items():
        ids = [m["id"] for m in reel["moments"]]
        cuts = [[m["id"], m["post"], m.get("stop"), m.get("must")] for m in reel["moments"]]   # what the playlist is cut from
        meta = {k: reel[k] for k in ("label", "date", "league", "ended")}
        old_entry = entry = index.get(key)
        if old_entry and old_entry.get("cuts") == cuts:
            # The same stretches of stream: only what's said about them can have changed.
            entry = {**old_entry, **meta,
                     "moments": [{**o, **{k: m[k] for k in ("player", "hole", "what", "bad", "level") if k in m}}
                                 for o, m in zip(old_entry.get("moments") or [], reel["moments"])], "cuts": cuts}
        elif (cut := _cut(client, bucket, root, recordings, playlists, reel, now)) is not None:
            parts, moments = cut
            base = f"{root}reels/{key}/"
            _put(client, bucket, root, base, "reel.m3u8", parts, skip=base + "finish/")
            entry = {**meta, "shows": SHOT_SHOWS, "ids": ids, "cuts": cuts, "duration": round(sum(d for p in parts for *_, d in p), 1),
                     "moments": moments, **({"finish": old_entry["finish"]} if old_entry and "finish" in old_entry else {})}
            added += 1
        elif old_entry:   # not streamed (any more): the old reel stands, as the round is now (a league round's keep goes by it)
            entry = {**old_entry, **meta}
        if entry is None:
            continue   # not streamed (yet): the next publish
        entry = _finish(client, bucket, root, recordings, playlists, key, reel.get("finish"), entry, now)
        if entry != old_entry:
            index[key], changed = entry, changed + 1
    gone = [k for k in index if k not in wanted]
    for k in gone:
        del index[k]
    stale = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=root + "reels/"):
        stale += [o["Key"] for o in page.get("Contents", []) if o["Key"][len(root) + 6:].split("/", 1)[0] not in index]
    if added or changed or gone or shifted or saved is None:
        client.put_object(Bucket=bucket, Key=root + "reels.json", Body=json.dumps({"reels": index}).encode(),
                          CacheControl="no-cache", ContentType=TYPES[".json"])
    for i in range(0, len(stale), 1000):
        client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in stale[i:i + 1000]]})
    return {"kept": len(index), "added": added, "removed": len(gone)}


def _shift(entry: dict) -> bool:
    """A reel cut when its moments' `at` didn't allow for the stream's delay (a second before the shot's
    time, SHOT_SHOWS ahead of the swing) moved, once, to a second before the swing."""
    if entry.get("shows") == SHOT_SHOWS:
        return False
    for m in (entry.get("moments") or []) + ([entry["finish"]] if isinstance(entry.get("finish"), dict) else []):
        if isinstance(m, dict) and isinstance(m.get("at"), (int, float)):
            m["at"] = round(m["at"] + SHOT_SHOWS, 1)
    entry["shows"] = SHOT_SHOWS
    return True


def _put(client: Any, bucket: str, root: str, base: str, name: str, parts: list[list[tuple[str, str, float]]],
         skip: str | None = None) -> None:
    """`parts`' segments copied from their streams (`root`s/) under `base` (as base/session/segment), its playlist
    `name` there, and what was there before and isn't now (bar under `skip`) deleted."""
    old = [o["Key"] for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=base)
           for o in page.get("Contents", []) if not (skip and o["Key"].startswith(skip))]
    new = {base + name}
    for part in parts:
        for session, seg, _ in part:
            k = f"{base}{session}/{seg}"
            new.add(k)
            if k not in old:
                client.copy_object(Bucket=bucket, Key=k, CopySource={"Bucket": bucket, "Key": f"{root}s/{session}/{seg}"},
                                   MetadataDirective="REPLACE", CacheControl="public, max-age=31536000, immutable",
                                   ContentType=TYPES.get("." + seg.rsplit(".", 1)[-1], "application/octet-stream"))
    client.put_object(Bucket=bucket, Key=base + name, Body=playlist([[(f"{s}/{n}", d) for s, n, d in p] for p in parts]),
                      CacheControl="no-cache", ContentType=TYPES[".m3u8"])
    stale = [k for k in old if k not in new]
    for i in range(0, len(stale), 1000):
        client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in stale[i:i + 1000]]})


def _finish(client: Any, bucket: str, root: str, recordings: list[dict], playlists: dict, key: str,
            f: dict | None, entry: dict, now: float) -> dict:
    """The reel's entry with its round's finish (rounds()'s) cut on its own, under reels/<key>/finish/
    (finish.m3u8), not in the reel: {at, id, player, hole, what, bad, level, cut}. One not streamed
    (any more) keeps what was cut; a round with none any more has it deleted."""
    base, old = f"{root}reels/{key}/finish/", entry.get("finish")
    if not f:
        if old:
            stale = [o["Key"] for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=base)
                     for o in page.get("Contents", [])]
            for i in range(0, len(stale), 1000):
                client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in stale[i:i + 1000]]})
        return {k: v for k, v in entry.items() if k != "finish"}
    cut = [f["id"], f["post"], f.get("stop"), f.get("must")]
    if old and old.get("cut") == cut:
        return {**entry, "finish": {**old, **{k: f[k] for k in ("player", "hole", "what", "bad", "level") if k in f}}}
    got = _cut(client, bucket, root, recordings, playlists, {"moments": [f]}, now)
    if got is None:
        return entry
    parts, (m,) = got
    _put(client, bucket, root, base, "finish.m3u8", parts)
    return {**entry, "finish": {**m, "cut": cut, "duration": round(sum(d for p in parts for *_, d in p), 1)}}


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
            # (rounded out to whole segments, but not past the stop unless it's needed for `must`)
            if start + dur > t + SHOT_SHOWS - LEAD_IN and start < t + m["post"] and (
                    m.get("stop") is None or start + dur <= t + m["stop"]
                    or start < t + m.get("must", 0.0) - MUST_GRACE or not idx):
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
            moments.append({"at": round(offset + max(0.0, m["_t"] + SHOT_SHOWS - first - WATCH_LEAD), 1),
                            **{k: m[k] for k in ("id", "player", "hole", "what", "bad", "level") if k in m}})
        part = [(session, name, d) for name, d in track[a:b + 1]]
        parts.append(part)
        offset += sum(d for *_, d in part)
    return parts, moments
