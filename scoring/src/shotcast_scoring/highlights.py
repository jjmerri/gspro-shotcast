"""A round's highlight moments: the parts of the app's highlight reels (commentator/highlights.py) that
need no database or bucket, shared with Shotcast Cloud so both pick a round's moments the same way.
The app keeps its own copy for now; the two are kept the same (its parity test).

A shot is a mapping with the app's shots columns (id, player, hole, shot_number, ts, is_putt,
lie_before, lie_after, to_pin_yards, finish_yards, total_yards, offline_yards, gimme_yards, outcome,
hazard_x, fmt, picked, mulligan, left_out, shared_ball, tags (JSON text), rest_ts, session_id); missing
ones take DEFAULTS. `player` is whatever names the player (the app's name; the cloud's slot), and goes
into a moment's words as it is.

Where a clip ends depends on the stream's timing (when a swing shows, when a line is heard), and a
reel's length budget is spent on its clips, so the timing (`Timing`; `APP`, the app's measured
numbers) is a parameter of the picking too.
"""
from __future__ import annotations

import bisect
import json
import re
from dataclasses import dataclass

from shotcast_scoring import excitement as I

SHOT_SHOWS = 5.0             # a shot's swing is on the stream this long after the booth logged it (measured 5.2-5.8 s)

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
MIN_AFTER = 8.0           # ...but not before this long after its last shot's swing shows (the ball comes to rest)
REST_TAIL = 3.0           # ...or, where GSPro's log had the ball stop (rest_ts), or a gimme was logged as it
REST_SECONDS = 15.0       # did (one this soon after the shot, before the next swing), this long after that shows
#                          (more than MUST_GRACE: the segment the ball stops in is never dropped)
REST_MAX = 60.0           # (a rest_ts further after its shot than this is none)
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
            "to_pin_yards": None, "lie_before": None, "lie_after": None, "tags": None, "is_putt": 0, "shot_number": None,
            "rest_ts": None}


@dataclass(frozen=True)
class Timing:
    """The stream's timing, in seconds (the constants above, by name in lower case)."""

    shot_shows: float = SHOT_SHOWS
    stream_lag: float = STREAM_LAG
    talk_lag: float = TALK_LAG
    talk_words_per_second: float = TALK_WORDS_PER_SECOND
    talk_gap: float = TALK_GAP
    talk_tail: float = TALK_TAIL
    talk_max: float = TALK_MAX
    stop_margin: float = STOP_MARGIN
    min_after: float = MIN_AFTER
    rest_tail: float = REST_TAIL
    rest_seconds: float = REST_SECONDS
    rest_max: float = REST_MAX
    reaction_seconds: float = REACTION_SECONDS
    flight_seconds: float = FLIGHT_SECONDS
    pre: float = PRE
    clip_slack: float = CLIP_SLACK


APP = Timing()   # the app's own stream, as measured


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


def talk(rows: list, shots: list[dict], ts_of: dict | None = None, timing: Timing = APP,
         unaired: str = "drop") -> tuple:
    """The booth's lines placed in time, for `pick`'s clips (the app's highlights._talk, from its rows):
    (session -> every line's (started, done, the time of the shot it's about: its call, a reaction or
    flight line, or None); session -> its swings' times; session -> its gimmes' times), in logged time.
    `rows`: (session_id, shot_id, ts, kind, text, aired_at, aired_seconds), ordered by (ts, id). A line
    the booth noted the air time of is placed by that; others by their words, after the exchange
    before. In a session with any air times, one without never aired (an interrupted exchange's rest)
    and is dropped (`unaired="drop"`, the app's), or placed by its words (`"estimate"`: a round
    uploaded before its last lines' air times were noted)."""
    noted = {r[0] for r in rows if r[5] is not None}
    shift = timing.stream_lag - timing.talk_lag   # (an aired time, as the logged time it's heard the same as)
    times: dict[int, list[float]] = {}
    rests: dict[int, list[float]] = {}
    for s in shots:   # (a gimme's never struck)
        (rests if s.get("outcome") == "gimme" else times).setdefault(s["session_id"], []).append(s["ts"])
    ts_of = {s["id"]: s["ts"] for s in shots} if ts_of is None else ts_of
    batches: dict[tuple[int, float], list] = {}   # an exchange's lines share the time they're logged
    for session, shot_id, ts, kind, text, aired_at, aired_seconds in rows:
        if session in noted and aired_at is None and unaired == "drop":
            continue
        about = ts_of.get(shot_id) if kind in ("shot", "reaction") else None
        if about is None and kind in ("reaction", "flight"):
            i = bisect.bisect_right(times.get(session, []), ts) - 1
            if i >= 0 and ts - times[session][i] <= (timing.reaction_seconds if kind == "reaction" else timing.flight_seconds):
                about = times[session][i]
        batches.setdefault((session, ts), []).append(
            ((aired_at + shift, aired_at + shift + (aired_seconds or 0.0)) if aired_at is not None
             else len((text or "").split()) / timing.talk_words_per_second, about))
    spoken: dict[int, list[tuple[float, float, float | None]]] = {}
    busy: dict[int, float] = {}
    for (session, ts), lines in sorted(batches.items(), key=lambda kv: kv[0][1]):
        at = max(ts, busy.get(session, 0.0))   # (an exchange waits for the one before it)
        done = at
        for seconds, about in lines:
            if isinstance(seconds, tuple):
                spoken.setdefault(session, []).append((*seconds, about))
                done = at = max(done, seconds[1])
                at += timing.talk_gap
                continue
            spoken.setdefault(session, []).append((at, at + seconds, about))
            done = at + seconds
            at = done + timing.talk_gap
        busy[session] = done
    for v in spoken.values():
        v.sort(key=lambda x: x[:2])
    return spoken, times, rests


def _post(post: float, at: dict, last: dict, talk: tuple | None, timing: Timing = APP) -> dict:
    """The stretch of stream after `at` (seconds): `post`, or on till the booth's done talking about
    the shots from `at` to `last`; and where the booth logged its lines, `stop` before whatever it says
    next or the next swing, though never before `must` (its own lines' end, or the ball at rest; None:
    no stop). Where the ball's known to have stopped (its rest_ts, or a gimme), it's over at `must`."""
    if not talk:
        return {"post": post, "stop": None, "must": 0.0}
    spoken, swings, *more = talk
    session = at.get("session_id")
    lo, hi = at["ts"], last["ts"]
    end = at["ts"] + post - timing.talk_lag   # (when what's heard at the cut was logged)
    own = [done for _, done, about in spoken.get(session, []) if about is not None and lo <= about <= hi]
    times = swings.get(session, [])
    i = bisect.bisect_right(times, hi)
    gimmes = more[0].get(session, []) if more else []
    j = bisect.bisect_right(gimmes, hi)
    settled = last.get("rest_ts")
    if settled is None or not hi < settled <= hi + timing.rest_max:
        settled = gimmes[j] if j < len(gimmes) and gimmes[j] - hi <= timing.rest_seconds and (
            i == len(times) or gimmes[j] < times[i]) else None
    # (the ball at rest, as logged)
    rest = hi + timing.shot_shows + timing.min_after - timing.talk_lag if settled is None else settled + timing.shot_shows + timing.rest_tail - timing.talk_lag
    must = max(rest, *(d + timing.talk_tail for d in own)) if own else rest
    end = max(end, must) if settled is None else must
    stop = times[i] + timing.stream_lag - timing.talk_lag if i < len(times) else None   # (the next swing, as heard)
    for start, _, about in spoken.get(session, []):
        if start > hi and not (about is not None and lo <= about <= hi):
            stop = start if stop is None else min(stop, start)
            break
    if stop is not None:
        stop -= timing.stop_margin
        end = min(end, max(stop, must))
    # (at most timing.talk_max after the last shot of it: a three-putt's run of putts comes first)
    cap = timing.talk_max + hi - timing.talk_lag   # (as logged)
    if settled is not None:
        cap = max(cap, rest)   # (never before a long roll's known stop)
    rel = lambda t: round(min(cap, t) + timing.talk_lag - at["ts"], 1)
    return {"post": rel(end), "stop": None if stop is None else rel(max(stop, must)), "must": rel(must)}


def pick(shots: list[dict], most: int = MAX_MOMENTS, sides: dict[str, int] | None = None,
         seconds: float = REEL_SECONDS,
         before: dict[str, dict] | None = None, talk: tuple | None = None, timing: Timing = APP) -> list[dict]:
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
                pool.append({"shot": s, "level": g[0], "score": score, "what": what, **_post(g[2], s, s, talk, timing), "bad": False,
                             "hole": number if number is not None else -i})
            elif (b := _bad(s, hole, team_ball, s["id"] in wet)) and b[0] >= MIN_LEVEL - 1:
                at, post = s, b[2]
                if "-putts" in b[1]:   # the whole run of putts, from the first
                    at = next(x for x in hole if x["is_putt"])
                    post += max(0.0, s["ts"] - at["ts"])
                pool.append({"shot": at, "level": b[0], "score": b[0] - 0.25, "what": b[1], **_post(post, at, s, talk, timing), "bad": True,
                             "hole": number if number is not None else -i})
    picked: list[dict] = []
    per_player = dict.fromkeys({m["shot"]["player"] for m in pool}, 0)   # (one with nothing to show: no say)
    per_hole: dict[int, list[dict]] = {}
    bad_by: set[str] = set()
    used = 0.0
    clip = lambda m: timing.pre + m["post"] + timing.clip_slack
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


def finish(shots: list[dict], talk: tuple | None = None, timing: Timing = APP) -> dict | None:
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
            **_post(POST_PUTT if s["is_putt"] else POST_SHOT, s, s, talk, timing)}


def entry(m: dict) -> dict:
    return {"id": m["shot"]["id"], "ts": m["shot"]["ts"], "player": m["shot"]["player"], "hole": _hole(m["shot"]["hole"]),
            "what": m["what"], "bad": m["bad"], "level": m["level"], "post": m["post"], "stop": m["stop"], "must": m["must"]}
