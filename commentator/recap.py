"""A league week's recap on the stats page: once a week is final, the booth's two voices run through
how it went (each team's result, the standings) over a few of each team's moments from its round's
highlight reel (highlights.py).

The video is the reels' own segments, copied under live/recaps/<key>/ and played muted; the
narration is a clip per line, which the page plays over it at the times in its timeline. A build's
playlist and timeline are named for it, and the index (put last) names the build, so a page never
gets one build's video with another's lines:

    live/recaps.json                    the index: key -> {week, course, end_date, built, ...}
    live/recaps/<key>/recap-<built>.json  the timeline: intro and outro lines over cards, each clip's lines
    live/recaps/<key>/recap-<built>.m3u8  the clips, a discontinuity between each
    live/recaps/<key>/<session>/<seg>   the segments, copied from the reels
    live/recaps/<key>/a/<sha1>.wav      each line's audio, named for its content (cached for good)

The engine builds it (Engine._build_recap): this module picks the moments, writes the request
for the lines, times them and publishes.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import tempfile
import time
import wave
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .board import Board, place, place_points
from .highlights import _get, playlist
from .live_stream import TYPES

log = logging.getLogger(__name__)

RECAP_SECONDS = 240.0   # the whole recap, cards and clips
INTRO_SECONDS = 22.0    # what the intro and outro take, for picking how many clips fit
OUTRO_SECONDS = 22.0
CLIP_SECONDS = 14.0     # what a clip takes, for picking how many fit
PER_TEAM = 3            # moments a leading team (or a big mover) can get; any other, two
LEAD = 1.0              # a clip starts this long before the moment's mark in its reel (`at`, a second before the shot)
LINE_START = 1.5        # its first line this long after that, as the swing comes
LINE_GAP = 0.3
TAIL = 1.5              # the clip runs this long past its last line
CLIP_MIN = 10.0         # long enough to see where the ball went
CLIP_MAX = 25.0
CLIP_WORDS = 26         # a clip's lines, together
CATCH_UP_WORDS = 16     # more for a clip that first catches up on turns no clip showed
CARD_WORDS = 35         # an intro or outro line
BOARD_ROWS = 5          # the TV-style net leaderboard over a clip: the top this many, and the clip's team
BUILD = 5               # how a recap's made: a change to it builds every recap again
CARD_LEAD = 0.8         # a card's first line this long into it
RETIRE_SECONDS = 3600.0  # a rebuild's leftovers stay this long (a player open on the last build still plays them)


def _net(n: int) -> str:
    return "E" if n == 0 else f"{n:+d}"


def week_facts(board: Board, week: int, today: str) -> dict[str, Any]:
    """The week's result and the standings it leaves, by the league file's team names (`team`) with
    their display names: rows best first {team, name, players, net, place, tied, points}, standings
    {team, name, points, place, was} (`was`: the place before this week; None for a first week)."""
    lg = board.league
    entry = lg.week_entry(week) or {}
    posted = board.posted(week)
    nets = [e.net for e in posted]
    rows = []
    for e in posted:
        pos, tied = place(e.net, nets)
        pts = place_points(pos, nets.count(e.net), board.points_table) if board.points_table else None
        rows.append({"team": e.team, "name": lg.team_name(e.team), "players": lg.team_players.get(e.team, []),
                     "net": e.net, "place": pos, "tied": tied, "points": None if pts is None else round(pts, 2)})
    table, _ = board.standings(today)
    before, _ = Board({w: v for w, v in board.weeks.items() if w != week}, lg, board.points_table).standings(today)
    was = {t: pl for t, _, pl in before}
    standings = [{"team": t, "name": lg.team_name(t), "points": round(p, 2), "place": pl, "was": was.get(t)}
                 for t, p, pl in table]
    return {"week": week, "course": entry.get("course"), "end_date": entry.get("end_date"), "rows": rows,
            "standings": standings, "missed": [lg.team_name(e.team) for e in board.weeks.get(week, []) if e.net is None]}


def key_of(facts: dict) -> str:
    return f"{facts.get('end_date') or 'week'}-w{facts['week']}"


def footage(reels: dict[str, dict], team_reel: dict[str, str], players: dict[str, list[str]]) -> dict[str, dict]:
    """Each team's moments from its round's reel (`reels`: reels.json's; `team_reel`: team -> reel
    key): team -> {key, moments, ats}. A reel two teams share (one round, a side each) gives each the
    shots its players hit; one of a single team's, all of them (a name the league file spells
    differently still counts) but another team's players' (a round shared that the other team has
    since played again). `ats`: where every moment of the reel starts, for where a clip has to end.
    `finish`: the round's last shot, cut on its own (highlights._finish), when it's the team's."""
    shared = {k for k in team_reel.values() if list(team_reel.values()).count(k) > 1}
    out: dict[str, dict] = {}
    for team, key in team_reel.items():
        reel = reels.get(key)
        if not isinstance(reel, dict) or not reel.get("moments"):
            continue
        names = {p.casefold() for p in players.get(team, [])}
        others = {p.casefold() for t, ps in players.items() if t != team for p in ps} - names
        moments = [m for m in reel["moments"] if isinstance(m, dict) and isinstance(m.get("at"), (int, float))
                   and (who := str(m.get("player") or "").casefold()) not in others
                   and (key not in shared or who in names)]
        f = reel.get("finish")
        if not (isinstance(f, dict) and isinstance(f.get("at"), (int, float))
                and (who := str(f.get("player") or "").casefold()) not in others and (key not in shared or who in names)):
            f = None
        if moments or f:
            out[team] = {"key": key, "moments": moments, "cuts": reel.get("cuts"), "ats": sorted(m["at"] for m in reel["moments"]
                                                                       if isinstance(m.get("at"), (int, float))),
                         **({"finish": f} if f else {})}
    return out


# ---- the story: the week as one leaderboard, hole by hole ------------------------------------

def _num(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def race(facts: dict) -> dict | None:
    """The week told as one tournament: every team's running net to par after each hole, as if
    they'd gone out together (they play on their own schedules), and the turns in it. From
    facts["holes"] (team -> {hole number: that hole's team net to par}). Every team in the results
    needs one, adding up to its result, all on the same holes: one left out could have led or tied,
    making the story's leads untrue. None otherwise, or with fewer than two teams.
    {holes, teams (best first), after: [{team: running net}] per hole, events: [{hole, teams, text}]
    (a lead changing hands or tied, also `gained`/`lost`: the teams that took it, and that lost it),
    key: team -> the holes its story turned on}."""
    nets = {r["team"]: r["net"] for r in facts["rows"]}
    cards: dict[str, dict[int, int]] = {}
    for t, hs in (facts.get("holes") or {}).items():
        card = {h: v for h, v in ((_num(h), _num(v)) for h, v in (hs or {}).items()) if h is not None and v is not None}
        if t in nets and card and sum(card.values()) == nets[t]:
            cards[t] = card
    if len(nets) < 2 or set(cards) != set(nets) or len({frozenset(c) for c in cards.values()}) != 1:
        return None
    teams = [r["team"] for r in facts["rows"]]
    holes = sorted(next(iter(cards.values())))
    name = {r["team"]: r["name"] for r in facts["rows"]}
    run = {t: 0 for t in teams}
    after = []
    for h in holes:
        for t in teams:
            run[t] += cards[t][h]
        after.append(dict(run))
    events: list[dict] = []
    key: dict[str, set[int]] = {t: set() for t in teams}

    def note(hole: int, who: list[str], text: str, gained: list[str] | None = None, lost_: list[str] | None = None) -> None:
        events.append({"hole": hole, "teams": who, "text": text,
                       **({"gained": gained, "lost": lost_ or []} if gained is not None else {})})
        for t in who:
            key[t].add(hole)

    def names(ts) -> str:   # team names have "and" in them: a list is by commas
        return ", ".join(name[t] for t in ts)

    prev: set[str] = set()
    for i, h in enumerate(holes):
        best = min(after[i].values())
        leaders = {t for t in teams if after[i][t] == best}
        lost = [t for t in teams if t in prev - leaders]
        if len(leaders) == 1 and leaders != prev:
            (t,) = leaders
            verb = "lead" if i == 0 else "pull clear alone" if t in prev else "take the lead"
            note(h, [t] + lost, f"{name[t]} {verb} after hole {h} at {_net(best)}"
                 + (f" (falling back: {names(lost)})" if lost else ""), [t] if i else None, lost)
        elif len(leaders) > 1 and leaders - prev:
            caught = [t for t in teams if t in prev] if len(prev) == 1 else []   # a lone lead gone
            joined = [t for t in teams if t in leaders - prev]
            note(h, joined + caught + lost,
                 f"Tied for the lead after hole {h} at {_net(best)}: {names([t for t in teams if t in leaders])}"
                 + (f" (falling back: {names(lost)})" if lost else ""), joined if i else None, caught + lost)
        prev = leaders

    def margin(i: int) -> tuple[str | None, int]:
        order = sorted(teams, key=lambda t: after[i][t])
        gap = after[i][order[1]] - after[i][order[0]]
        return (order[0], gap) if gap > 0 else (None, 0)

    half = (len(holes) + 1) // 2 - 1
    if len(holes) >= 4:
        top, gap = margin(half)
        if top:
            note(holes[half], [top], f"Halfway, after hole {holes[half]}: {name[top]} lead by {gap} at {_net(after[half][top])}")
    widest = max(range(len(holes)), key=lambda i: (margin(i)[1], -i))
    top, gap = margin(widest)
    if top and gap >= 2 and not (len(holes) >= 4 and widest == half):
        note(holes[widest], [top], f"{name[top]}'s biggest lead: {gap} after hole {holes[widest]}")

    winners = [r["team"] for r in facts["rows"] if r["place"] == 1 and r["team"] in run]
    if len(winners) == 1:
        w = winners[0]
        alone = [i for i in range(len(holes)) if margin(i)[0] == w]
        since = len(holes) - 1
        while since > 0 and since - 1 in alone:
            since -= 1
        if alone and alone[-1] == len(holes) - 1:
            if since == 0:
                note(holes[0], [w], f"{name[w]} led alone after every hole: wire to wire")
            elif since == len(holes) - 1:
                note(holes[since], [w], f"{name[w]} won it on the last hole")
            else:
                note(holes[since], [w], f"{name[w]} led alone from hole {holes[since]} to the finish")
        behind = max(range(len(holes)), key=lambda i: (after[i][w] - min(after[i].values()), -i))
        down = after[behind][w] - min(after[behind].values())
        if down >= 2:
            note(holes[behind], [w], f"{name[w]} were {down} back after hole {holes[behind]} and came back to win")

    swings = []
    for i, h in enumerate(holes):
        for t in teams:
            v = cards[t][h]
            if v <= -2 or v >= 3:
                was = 1 + sum(1 for o in teams if (after[i - 1][o] if i else 0) < (after[i - 1][t] if i else 0))
                now = 1 + sum(1 for o in teams if after[i][o] < after[i][t])
                swings.append((abs(v), h, t, v, was, now))
    for _, h, t, v, was, now in sorted(swings, key=lambda x: (-x[0], x[1]))[:6]:
        move = f", {_ordinal(was)} to {_ordinal(now)}" if was != now else ""
        note(h, [t], f"{name[t]}: {_hole_net(v)} as a team on hole {h}, net{move}")
    events.sort(key=lambda e: holes.index(e["hole"]))
    return {"holes": holes, "teams": teams, "after": after, "events": events, "key": key}


def turn_moments(story: dict, find: Callable[[str, int, str], dict | None]) -> dict[str, list[dict]]:
    """The shots each lead change or tie turned on, cut from the rounds' streams for the clips that show
    it (the reels' picks are a round's best shots, not the race's): the miss of a team that lost the
    lead going over par on the hole; the long putt or hole-out of one that took it (not its tap-in);
    and with neither, the shot that set up a team's birdie that took it. `find(team, hole, want)`:
    highlights.turn_shot of that team's round. Returns team -> moments, `turn`: True."""
    out: dict[str, list[dict]] = {}

    def add(team: str, m: dict | None) -> bool:
        if m is None:
            return False
        have = out.setdefault(team, [])
        if all(x["id"] != m["id"] for x in have):
            have.append({**m, "turn": True})
        return True

    for e in story["events"]:
        if "gained" not in e:
            continue
        i = story["holes"].index(e["hole"])
        v = {t: story["after"][i][t] - (story["after"][i - 1][t] if i else 0) for t in story["teams"]}
        shown = False
        for t in e["lost"]:
            if v[t] > 0:
                shown |= add(t, find(t, e["hole"], "miss"))
        for t in e["gained"]:
            shown |= add(t, find(t, e["hole"], "made"))
        if not shown:
            for t in e["gained"]:
                if v[t] < 0:
                    add(t, find(t, e["hole"], "setup"))
    return out


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _hole_net(v: int) -> str:
    # Not "birdie": an aggregate team's -2 can be two players' birdies.
    return "even" if v == 0 else f"{abs(v)} {'under' if v < 0 else 'over'}"


def standing(story: dict, team: str, i: int) -> str:
    """Where `team` stood after the race's i-th hole (i -1: before the first): its running net and
    place, "leading by 2" / "tied for the lead" / "3rd, 2 back"."""
    run = story["after"][i] if i >= 0 else {t: 0 for t in story["teams"]}
    mine = run[team]
    best_other = min(v for t, v in run.items() if t != team)
    if mine < best_other:
        where = f"leading by {best_other - mine}"
    elif mine == best_other:
        where = "tied for the lead"
    else:
        pos = 1 + sum(1 for v in run.values() if v < mine)
        where = f"{_ordinal(pos)}, {mine - min(run.values())} back"
    return f"{_net(mine)}, {where}"


def board(story: dict, i: int, team: str, rows: dict[str, dict]) -> dict:
    """The net leaderboard after the race's i-th hole, as a broadcast puts it up going to the next:
    {thru, rows: [{place, tied, name, net, on}]}, the top BOARD_ROWS and `team` (`on`) wherever it is."""
    run = story["after"][i]
    nets = list(run.values())
    out = []
    for n, t in enumerate(sorted(run, key=lambda t: (run[t], story["teams"].index(t)))):
        if n < BOARD_ROWS or t == team:
            pos, tied = place(run[t], nets)
            out.append({"place": pos, "tied": tied, "name": rows.get(t, {}).get("name", t), "net": run[t],
                        **({"on": True} if t == team else {})})
    return {"thru": story["holes"][i], "rows": out}


def _ranked(moments: list[dict], key: set[int] | None = None) -> list[dict]:
    """A team's moments, the best first. With the holes its story turned on (`key`): those first, a
    rough one among them too (it may be how a lead went), then the rest. Otherwise the good by how
    big (a reel from before levels counts each the same), then the bad."""
    key = key or set()
    return sorted(moments, key=lambda m: (_num(m.get("hole")) not in key, not m.get("turn"), bool(m.get("bad")),
                                          -(m.get("level") or 1), m["at"]))


def choose(facts: dict, footage: dict[str, dict], budget: float = RECAP_SECONDS) -> list[dict]:
    """The recap's clips in the order they play. Every team with footage gets its best moment (a bad
    one only when it has nothing good) while they fit; then, a round at a time, another good one, the
    leaders and big movers first, up to PER_TEAM for them and two for the rest. Each: {team, key, **moment}.

    With the week's story (race()), a broadcast of it: the shots on the holes it turned on come first
    (a rough one there too), the teams in it get the most, a shot off the week's holes isn't one, and
    the clips run in hole order across the field. Without, the teams from last place to the winner,
    each team's in the order they were hit. Either way, a winner alone with its round's finish ends on
    that, its room kept first (`finish`: True). A story's clip after holes no clip showed carries the
    turns on them (`since`: their texts), for the booth to catch up on before its shot."""
    story = race(facts)
    holes = story["holes"] if story else []
    if story:
        footage = {t: {**f, "moments": [m for m in f["moments"] if t not in story["teams"]
                                        or _num(m.get("hole")) in holes]} for t, f in footage.items()}
    order = [r["team"] for r in facts["rows"] if footage.get(r["team"], {}).get("moments")]
    if not order and not any(f.get("finish") for f in footage.values()):
        return []
    place_of = {r["team"]: r["place"] for r in facts["rows"]}
    moved = {s["team"]: (s["was"] - s["place"]) if s.get("was") else 0 for s in facts.get("standings") or []}
    lead = {s["team"] for s in facts.get("standings") or [] if s["place"] <= 2}
    key = story["key"] if story else {}
    told = {t for t, hs in key.items() if hs}
    cap = {t: PER_TEAM if (t in lead or place_of[t] == 1 or moved.get(t, 0) >= 2 or t in told) else 2 for t in order}
    ranked = {t: _ranked(footage[t]["moments"], key.get(t)) for t in order}
    room = budget - INTRO_SECONDS - OUTRO_SECONDS
    winners = [r["team"] for r in facts["rows"] if r["place"] == 1]
    last = None
    if len(winners) == 1 and (f := (footage.get(winners[0]) or {}).get("finish")) and room >= CLIP_SECONDS:
        w = winners[0]
        last = {"team": w, "key": footage[w]["key"], **f, "finish": True}
        room -= CLIP_SECONDS
        if w in ranked:   # (the same shot in its reel isn't shown twice)
            ranked[w] = [m for m in ranked[w] if m.get("id") is None or m.get("id") != f.get("id")]
            if not ranked[w]:
                order.remove(w)
    picked: dict[str, list[dict]] = {t: [] for t in order}
    for t in order:
        if room >= CLIP_SECONDS:
            picked[t].append(ranked[t][0])
            room -= CLIP_SECONDS
    priority = sorted(order, key=lambda t: (-len(key.get(t) or ()), place_of[t] - 2 * max(0, moved.get(t, 0))))
    while room >= CLIP_SECONDS:
        added = False
        for t in priority:
            more = [m for m in ranked[t] if m not in picked[t]
                    and (not m.get("bad") or _num(m.get("hole")) in (key.get(t) or ()))]
            if more and len(picked[t]) < cap[t] and room >= CLIP_SECONDS:
                picked[t].append(more[0])
                room -= CLIP_SECONDS
                added = True
        if not added:
            break
    if story:
        def when(c: dict) -> tuple:
            h = _num(c.get("hole"))
            return (holes.index(h) if h in holes else len(holes), -place_of[c["team"]], c["at"])
        out = sorted(({"team": t, "key": footage[t]["key"], **m} for t in order for m in picked[t]), key=when)
    else:
        out = [{"team": t, "key": footage[t]["key"], **m}
               for t in reversed(order) for m in sorted(picked[t], key=lambda m: m["at"])]
    out += [last] if last else []
    if story:
        shown = -1   # the index of the last hole a clip was on
        for c in out:
            h = _num(c.get("hole"))
            if h not in holes:
                continue
            n = holes.index(h)
            if since := [e["text"] for e in story["events"] if shown < holes.index(e["hole"]) < n]:
                c["since"] = since
            shown = max(shown, n)
    return out


def digest(facts: dict, footage: dict[str, dict], turns: dict[str, list[dict]] | None = None) -> str:
    """What a recap is built from: the week's numbers and the moments it could show (where each is
    in its reel, and what it is; `turns`: turn_moments'). A change to any (Squabbit's net replacing
    the sim's, a team's reel cut late or cut again) rebuilds it, as does a change to how it's made or drawn."""
    from . import burn

    body = {"v": [BUILD, burn.LOOK, burn.CARD_LOOK], "rows": [[r["team"], r["net"]] for r in facts["rows"]],
            "holes": {t: {str(h): v for h, v in hs.items()} for t, hs in sorted((facts.get("holes") or {}).items())},
            "standings": [[s["team"], s["points"]] for s in facts["standings"]],
            "footage": {t: [f["key"], f.get("cuts"), [[m.get(k) for k in ("id", "at", "what", "level", "bad", "player", "hole")]
                                                      for m in f["moments"]],
                            [(f.get("finish") or {}).get(k) for k in ("id", "at", "what", "player", "hole", "cut")]]
                        for t, f in sorted(footage.items())},
            "turns": {t: [[m["id"], m["what"]] for m in ms] for t, ms in sorted((turns or {}).items())}}
    return hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


# ---- the lines -----------------------------------------------------------------------------

def schema(speakers: list[str]) -> dict[str, Any]:
    line = {"type": "object", "properties": {"speaker": {"type": "string", "enum": speakers}, "text": {"type": "string"}},
            "required": ["speaker", "text"], "additionalProperties": False}
    lines = {"type": "array", "items": line}
    return {"type": "object", "properties": {
        "intro": lines,
        "clips": {"type": "array", "items": {"type": "object", "properties": {"clip": {"type": "integer"}, "lines": lines},
                                             "required": ["clip", "lines"], "additionalProperties": False}},
        "outro": lines}, "required": ["intro", "clips", "outro"], "additionalProperties": False}


def prompt(facts: dict, clips: list[dict], names: dict[str, str]) -> tuple[str, str]:
    """The request for the recap's lines, and its facts (what the lines are checked against).
    `names`: speaker key -> commentator's name."""
    out = [f"League week {facts['week']}" + (f" at {facts['course']}" if facts.get("course") else "") + " is final."]
    out.append("The week's results, best first (team net to par" + (", points earned" if any(
        r["points"] is not None for r in facts["rows"]) else "") + "):")
    for r in facts["rows"]:
        pts = f", {r['points']:g} points" if r["points"] is not None else ""
        out.append(f"- {'T' if r['tied'] else ''}{r['place']}. {r['name']} ({' & '.join(r['players'])}): "
                   f"{_net(r['net'])}{pts}")
    if facts.get("missed"):
        out.append("No score posted: " + ", ".join(facts["missed"]) + ".")
    if facts.get("standings"):
        out.append("The season standings after it:")
        for s in facts["standings"]:
            move = ""
            if s.get("was"):
                d = s["was"] - s["place"]
                move = f" (up {d} from {s['was']})" if d > 0 else f" (down {-d} from {s['was']})" if d < 0 else " (same place)"
            out.append(f"- {s['place']}. {s['name']}: {s['points']:g} points{move}")
    by_team = {r["team"]: r["name"] for r in facts["rows"]}
    story = race(facts)
    if story:
        out.append("How the week played out hole by hole: each team's running net to par after every hole, "
                   "the leader first. The teams played at different times, so this is the leaderboard as if "
                   "they'd gone out together" + (" (left off: " + ", ".join(
                       by_team[r["team"]] for r in facts["rows"] if r["team"] not in story["teams"]) + ", "
                       "no hole by hole card)" if len(story["teams"]) < len(facts["rows"]) else "") + ":")
        for h, run in zip(story["holes"], story["after"]):
            out.append(f"- after hole {h}: " + ", ".join(f"{by_team[t]} {_net(v)}" for t, v in
                                                          sorted(run.items(), key=lambda tv: tv[1])))
        out.append("The turns in it:")
        out += [f"- {e['text']}" for e in story["events"]]
    out.append("The clips, in the order they play:")
    for i, c in enumerate(clips):
        line = (f"- clip {i}: {by_team.get(c['team'], c['team'])}, {c.get('player') or 'a player'}"
                + (f" on hole {c['hole']}" if c.get("hole") else "") + f": {c.get('what') or 'a shot'}"
                + (" (a rough moment)" if c.get("bad") else ""))
        h = _num(c.get("hole"))
        if story and c["team"] in story["teams"] and h in story["holes"]:
            n = story["holes"].index(h)
            v = story["after"][n][c["team"]] - (story["after"][n - 1][c["team"]] if n else 0)
            line += (f". Going to the hole: {standing(story, c['team'], n - 1)}. "
                     + ("The hole, finished with this shot: " if c.get("finish") else
                        "How the hole ends (still to come as this shot's hit): ")
                     + f"{_hole_net(v)} as a team, net, leaving them {standing(story, c['team'], n)}")
        if c.get("since"):
            line += ". Before it, off camera (no clip showed these): " + "; ".join(c["since"])
        if c.get("finish"):
            line += ". THE WINNING FINISH: the shot that ended the winners' week"
        out.append(line)
    facts_text = "\n".join(out)
    who = " and ".join(names.values()) or "the booth"
    ask = (
        f"\n\nWrite the week's recap package, voiced by {who}, as they would on a highlights show the day "
        "after. It plays as a short video: an intro over the results, then the clips, then an outro over "
        "the standings.\n"
        + (f"- intro: two or three lines, at most {CARD_WORDS} words each: the week, the course, who won "
           "it, and the story of how (a wire to wire win, a comeback, a lead that changed hands late), told "
           "as the hook that the clips will play out.\n"
           f"- clips: for every clip listed, by its number, one or two lines, at most {CLIP_WORDS} words "
           "between them. The clips run in hole order, the way a tournament broadcast follows the "
           "leaderboard through the round: call the shot the way the clip says it went, and put it in the "
           "race (where the team stood going to the hole and what it did to the board). Carry the story "
           "from clip to clip: who led early, where the lead changed, who pulled away or fell back, and "
           "how it was decided. Don't call a clip in isolation. Where a clip has turns off camera before it, "
           "catch the viewer up on them first (\"Meanwhile on seven...\"): every lead change and tie gets "
           f"told; such a clip can take {CATCH_UP_WORDS} more words.\n"
           if story else
           f"- intro: two or three lines, at most {CARD_WORDS} words each: the week, the course, who won it "
           "and by what, and how the field finished.\n"
           f"- clips: for every clip listed, by its number, one or two lines, at most {CLIP_WORDS} words "
           "between them. They play as the shot is hit, so call it the way the clip says it went, and tie it "
           "to that team's week. Go through the teams as the clips run, last place to the winner.\n")
        + f"- outro: two or three lines, at most {CARD_WORDS} words each, on the season standings now: who "
        "leads, who moved, what's ahead.\n"
        + ("- The last clip is the winners' finish: call the win there, over that shot, and nowhere "
           "before it. On a clip whose shot didn't finish its hole, the hole's result is still to come: "
           "don't call it as done.\n" if any(c.get("finish") for c in clips) else
           "- On a clip whose shot didn't finish its hole, the hole's result is still to come: don't call it as done.\n")
        + "Trade lines between the commentators, as they do live. Use only the facts here: no other scores, "
        "points or shots. Say nets the way golf does (\"four under\", \"even par\").")
    return facts_text + ask, facts_text


def script(raw: str, clips: list[dict], speakers: list[str]) -> tuple[dict, list[str]]:
    """The model's lines, {intro, clips: {n: lines}, outro} as [(speaker, text)], and what's wrong with
    them: no intro or outro, a clip with no lines, or one with too many words."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}

    def lines(items) -> list[tuple[str, str]]:
        return [(str(i["speaker"]), " ".join(str(i.get("text") or "").split())) for i in items or []
                if isinstance(i, dict) and i.get("speaker") in speakers and str(i.get("text") or "").strip()]

    out = {"intro": lines(data.get("intro")), "outro": lines(data.get("outro")), "clips": {}}
    for c in data.get("clips") or []:
        if isinstance(c, dict) and isinstance(c.get("clip"), int) and 0 <= c["clip"] < len(clips):
            out["clips"][c["clip"]] = lines(c.get("lines"))[:2]
    problems = []
    if not out["intro"]:
        problems.append("write the intro")
    if not out["outro"]:
        problems.append("write the outro")
    if missing := [str(i) for i in range(len(clips)) if not out["clips"].get(i)]:
        problems.append("write lines for clip " + ", ".join(missing))
    def room(i: int) -> int:
        return CLIP_WORDS + (CATCH_UP_WORDS if clips[i].get("since") else 0)
    if long := [str(i) for i, ls in out["clips"].items() if sum(len(t.split()) for _, t in ls) > room(i) + 4]:
        problems.append(f"keep each clip's lines to {CLIP_WORDS} words between them, {CLIP_WORDS + CATCH_UP_WORDS} "
                        f"catching up off camera (clip {', '.join(long)})")
    if long := [t[:30] for _, t in out["intro"] + out["outro"] if len(t.split()) > CARD_WORDS + 5]:
        problems.append(f"keep each intro and outro line to {CARD_WORDS} words")
    return out, problems


# ---- the video and its timeline --------------------------------------------------------------

def parts(m3u8: str) -> list[list[tuple[str, float]]]:
    """A reel playlist's runs of segments, (uri, seconds), split at each discontinuity."""
    out: list[list[tuple[str, float]]] = [[]]
    dur = None
    for raw in m3u8.splitlines():
        s = raw.strip()
        if s.startswith("#EXTINF:"):
            try:
                dur = float(s[8:].split(",", 1)[0])
            except ValueError:
                dur = None
        elif s == "#EXT-X-DISCONTINUITY":
            if out[-1]:
                out.append([])
        elif s and not s.startswith("#") and dur is not None:
            out[-1].append((s, dur))
            dur = None
    return [p for p in out if p]


def cut(clip: dict, reel: list[list[tuple[str, float]]], ats: list[float], spoken: float) -> tuple[list[tuple[str, float]], float] | None:
    """A clip's segments of its reel, and how far into the first its moment's mark (`at` less LEAD)
    is: from just before the moment for long enough to hear its `spoken` seconds of lines, but not
    into the reel's next moment or past the end of the moment's run. None if the reel doesn't reach it."""
    start, run = 0.0, None
    for p in reel:
        length = sum(d for _, d in p)
        if start <= clip["at"] < start + length:
            run = (start, p)
            break
        start += length
    if run is None:
        return None
    first, segs = run
    want = max(first, clip["at"] - LEAD)
    nxt = min([a for a in ats if a > clip["at"] + 0.5], default=float("inf"))
    end = min(first + sum(d for _, d in segs), nxt - LEAD, want + CLIP_MAX)
    end = max(min(end, want + max(CLIP_MIN, LINE_START + spoken + TAIL)), want + 0.1)
    out, t, skew = [], first, None
    for uri, d in segs:
        if t + d > want and t < end:
            skew = want - t if skew is None else skew
            out.append((uri, d))
        t += d
    return (out, skew) if out else None


def timeline(facts: dict, clips: list[dict], lines: dict, voices: dict[tuple, tuple[float, str]],
             cuts: list[tuple[list[tuple[str, float]], float] | None]) -> tuple[dict, list[list[tuple[str, float]]], list[str]]:
    """recap.json's body (bar its key), the playlist's parts and where each part's segments are (under
    live/: a reel's folder, or a clip's own `src`, s for one cut from its stream). `lines`: script()'s;
    `voices`: each voiced line's (seconds, audio file) by ("intro", n), ("clip", i, n), ("outro", n);
    `cuts`: each clip's cut() (a clip that's None, or has no voiced lines, is left out)."""
    def voiced(where: tuple, ls) -> list[dict]:
        return [{"speaker": s, "text": t, "seconds": round(voices[(*where, n)][0], 2), "audio": voices[(*where, n)][1]}
                for n, (s, t) in enumerate(ls) if (*where, n) in voices]

    by_team = {r["team"]: r for r in facts["rows"]}
    story = race(facts)
    out_clips, out_parts, sources, offset = [], [], [], 0.0
    for i, (c, cu) in enumerate(zip(clips, cuts)):
        said = voiced(("clip", i), lines["clips"].get(i, []))
        if cu is None or not said:
            continue
        segs, skew = cu
        at = offset + skew + LINE_START
        for line in said:
            line["at"] = round(at, 2)
            at += line["seconds"] + LINE_GAP
        r = by_team.get(c["team"], {})
        out_clips.append({"at": round(offset, 2), "team": r.get("name", c["team"]), "player": c.get("player"),
                          "hole": c.get("hole"), "what": c.get("what"), "bad": bool(c.get("bad")),
                          "lines": said, "src": [c["key"], c.get("id")]})
        h = _num(c.get("hole"))
        if story and h in story["holes"] and story["holes"].index(h) > 0:   # (up all through, as on TV)
            out_clips[-1]["board"] = board(story, story["holes"].index(h) - 1, c["team"], by_team)
        out_parts.append(segs)
        sources.append(c.get("src") or f"reels/{c['key']}")
        offset += sum(d for _, d in segs)
    body = {"week": facts["week"], "course": facts.get("course"), "end_date": facts.get("end_date"),
            "rows": [{k: r[k] for k in ("name", "players", "net", "place", "tied", "points")} for r in facts["rows"]],
            "missed": facts.get("missed") or [],
            "standings": [{k: s[k] for k in ("name", "points", "place", "was")} for s in facts["standings"]],
            "intro": voiced(("intro",), lines["intro"]), "clips": out_clips,
            "outro": voiced(("outro",), lines["outro"]), "duration": round(offset, 1)}
    return body, out_parts, sources


def wav(samples: np.ndarray, rate: int) -> bytes:
    pcm = (np.clip(np.asarray(samples, dtype="float32"), -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


# ---- S3 ------------------------------------------------------------------------------------

def _root(prefix: str) -> str:
    return (prefix.strip("/") + "/" if prefix.strip("/") else "") + "live/"


def load(client: Any, bucket: str, prefix: str) -> tuple[dict[str, dict], dict[str, dict]]:
    """reels.json's reels and recaps.json's recaps, as they are up there."""
    root = _root(prefix)
    out = []
    for name, field in (("reels.json", "reels"), ("recaps.json", "recaps")):
        raw = _get(client, bucket, root + name)
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        got = body.get(field) if isinstance(body, dict) else None
        out.append({k: v for k, v in (got or {}).items() if isinstance(v, dict)})
    return out[0], out[1]


def reel_playlist(client: Any, bucket: str, prefix: str, key: str, finish: bool = False) -> list[list[tuple[str, float]]]:
    """A reel's playlist parts, or (`finish`) its round's finish's, its uris from the reel's folder."""
    name = "finish/finish.m3u8" if finish else "reel.m3u8"
    raw = _get(client, bucket, f"{_root(prefix)}reels/{key}/{name}")
    got = parts(raw.decode("utf-8", "replace")) if raw else []
    return [[(f"finish/{u}", d) for u, d in p] for p in got] if finish else got


def _keys(client: Any, bucket: str, prefix: str) -> list[str]:
    return [o["Key"] for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
            for o in page.get("Contents", [])]


def _delete(client: Any, bucket: str, keys: list[str]) -> None:
    for i in range(0, len(keys), 1000):
        client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]]})


def burn_boards(client: Any, bucket: str, prefix: str, key: str, body: dict,
                video: list[list[tuple[str, float]]], sources: list[str]) -> tuple[list[list[tuple[str, float]]], dict[str, bytes]]:
    """Each clip with a board (timeline's) drawn into its video, so it's there full screen: its part's
    segments made again under b/ (named for what's in them: one already up isn't made again), and the
    clip marked `burned` (the page draws no board over it). Returns the playlist's parts and the
    segments made, by uri. Without ffmpeg, or for a clip it fails on, the page draws the board."""
    from . import burn

    ff = burn.ffmpeg()
    if not ff:
        if any(c.get("board") for c in body["clips"]):
            log.warning("League recap: no ffmpeg, so its leaderboard isn't in the video (pip install imageio-ffmpeg)")
        return video, {}
    root, base = _root(prefix), f"{_root(prefix)}recaps/{key}/"
    there = set(_keys(client, bucket, base + "b/"))
    out, made = [], {}
    for c, part, src in zip(body["clips"], video, sources):
        if not c.get("board"):
            out.append(part)
            continue
        tag = hashlib.sha1(json.dumps([burn.LOOK, src.removeprefix("reels/"), [u for u, _ in part], c["board"]], sort_keys=True).encode()).hexdigest()[:16]
        names = [f"b/{tag}-{n}.ts" for n in range(len(part))]
        if not all(base + n in there for n in names):
            try:
                segs = [client.get_object(Bucket=bucket, Key=f"{root}{src}/{u}")["Body"].read() for u, _ in part]
                made.update(zip(names, burn.clip(ff, segs, c["board"])))
            except Exception as e:
                log.warning("League recap: couldn't draw the leaderboard into the clip at %ss (%s)", c["at"], e)
                out.append(part)
                continue
        c["burned"] = True
        out.append([(n, d) for n, (_, d) in zip(names, part)])
    return out, made


def burn_cards(client: Any, bucket: str, prefix: str, key: str, body: dict, video: list[list[tuple[str, float]]],
               sources: list[str]) -> tuple[list[list[tuple[str, float]]], list[str], dict[str, bytes]]:
    """The intro's results and the outro's standings made video (burn.card) at the recap's start and
    end, so it's one video, seekable through and full screen: each a clip of the body's (`card`:
    "intro"/"outro", its lines at their times, the clips after it moved on), the body's intro and outro
    left empty (`cards`: True). Under b/, named for what's in them (one already up isn't made again).
    Returns the playlist's parts, their sources and the segments made, by uri. Without ffmpeg, or if
    it fails, all as it was: the page shows the cards over the player."""
    from . import burn

    ff = burn.ffmpeg()
    if not ff or not video or not (body["intro"] or body["outro"]):
        return video, sources, {}
    root, base = _root(prefix), f"{_root(prefix)}recaps/{key}/"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp, "first.ts")
            first.write_bytes(client.get_object(Bucket=bucket, Key=f"{root}{sources[0]}/{video[0][0][0]}")["Body"].read())
            like = burn.probe(ff, first)
        there = set(_keys(client, bucket, base + "b/"))
        made, cards = {}, {}
        for kind in ("intro", "outro"):
            lines = body[kind]
            if not lines:
                continue
            at, timed = CARD_LEAD, []
            for line in lines:
                timed.append({**line, "at": round(at, 2)})
                at += line["seconds"] + LINE_GAP
            seconds = round(at - LINE_GAP + TAIL, 2)
            what = {"week": body["week"], "course": body.get("course"), "rows": body["rows"], "standings": body["standings"]}
            tag = hashlib.sha1(json.dumps([burn.CARD_LOOK, kind, what, seconds, like], sort_keys=True).encode()).hexdigest()[:16]
            plan = burn.card_plan(seconds)
            names = [f"b/{kind}-{tag}-{n}.ts" for n in range(len(plan))]
            if not all(base + n in there for n in names):
                segs = burn.card(ff, burn.card_png(kind, body, like["width"], like["height"]), seconds, like)
                made.update(zip(names, (b for b, _ in segs)))
            cards[kind] = ({"at": 0.0, "card": kind, "team": None, "player": None, "hole": None, "what": None,
                            "bad": False, "lines": timed}, list(zip(names, plan)))
    except Exception as e:
        log.warning("League recap: couldn't make its opening and closing cards into video (%s); the page shows them", e)
        return video, sources, {}
    clips = [{**c, "lines": [dict(ln) for ln in c["lines"]]} for c in body["clips"]]
    video, sources = list(video), list(sources)
    if "intro" in cards:
        shift = sum(d for _, d in cards["intro"][1])
        for c in clips:
            c["at"] = round(c["at"] + shift, 2)
            for line in c["lines"]:
                line["at"] = round(line["at"] + shift, 2)
        clips.insert(0, cards["intro"][0])
        video.insert(0, cards["intro"][1])
        sources.insert(0, "card")
    if "outro" in cards:
        end = sum(d for p in video for _, d in p)
        c = cards["outro"][0]
        c["at"] = round(end, 2)
        for line in c["lines"]:
            line["at"] = round(line["at"] + end, 2)
        clips.append(c)
        video.append(cards["outro"][1])
        sources.append("card")
    body.update(clips=clips, cards=True, duration=round(sum(d for p in video for _, d in p), 1),
                **{k: [] for k in cards})
    return video, sources, made


def publish(client: Any, bucket: str, prefix: str, key: str, body: dict, video: list[list[tuple[str, float]]],
            sources: list[str], audio: list[tuple[str, bytes]], digest_: str, season_from: str | None = None,
            clock=time.time, made: dict[str, bytes] | None = None) -> dict:
    """Put a recap up: its clips' segments copied from their reels or streams (`sources`: timeline's;
    a reel's segments go when it's cut again, a stream's past its keep), its playlist, lines' audio, timeline and an entry in
    recaps.json; then the recaps of past seasons go. An earlier build's leftovers are retired (listed
    in the entry) and go at a build RETIRE_SECONDS on, a page that loaded it still playing them till
    then. `made`: segments made for it (burn_boards'), by uri, put up instead of copied. Returns the
    index entry."""
    root = _root(prefix)
    base = f"{root}recaps/{key}/"
    old = set(_keys(client, bucket, base))
    new = set()
    for part, src in zip(video, sources):
        for uri, _ in part:
            k = base + uri
            if k not in old and k not in new and made and uri in made:
                client.put_object(Bucket=bucket, Key=k, Body=made[uri], CacheControl="public, max-age=31536000, immutable",
                                  ContentType=TYPES[".ts"])
            elif k not in old and k not in new:
                client.copy_object(Bucket=bucket, Key=k, CopySource={"Bucket": bucket, "Key": f"{root}{src}/{uri}"},
                                   MetadataDirective="REPLACE", CacheControl="public, max-age=31536000, immutable",
                                   ContentType=TYPES.get("." + uri.rsplit(".", 1)[-1], "application/octet-stream"))
            new.add(k)
    for name, data in audio:
        client.put_object(Bucket=bucket, Key=base + name, Body=data, ContentType="audio/wav",
                          CacheControl="public, max-age=31536000, immutable")
        new.add(base + name)
    built = round(clock())
    name = f"{base}recap-{built}"
    client.put_object(Bucket=bucket, Key=name + ".m3u8", Body=playlist(video), CacheControl="no-cache",
                      ContentType=TYPES[".m3u8"])
    client.put_object(Bucket=bucket, Key=name + ".json", Body=json.dumps({**body, "key": key, "built": built}).encode(),
                      CacheControl="no-cache", ContentType=TYPES[".json"])
    new |= {name + ".m3u8", name + ".json"}
    _, index = load(client, bucket, prefix)
    retired = {}
    for k in old - new:
        retired[k[len(base):]] = ((index.get(key) or {}).get("retired") or {}).get(k[len(base):], built)
    # Every recap's leftovers retired long enough go now (a week not built again has no publish of its own).
    for k, v in index.items():
        if not isinstance(v, dict) or not isinstance(v.get("retired"), dict):
            continue
        here = f"{root}recaps/{k}/"
        gone_now = [here + n for n, when in v["retired"].items() if built - when >= RETIRE_SECONDS]
        if k == key:
            gone_now = [g for g in gone_now if g not in new]
            retired = {n: w for n, w in retired.items() if built - w < RETIRE_SECONDS}
        else:
            v["retired"] = {n: w for n, w in v["retired"].items() if built - w < RETIRE_SECONDS}
            if not v["retired"]:
                del v["retired"]
        _delete(client, bucket, sorted(gone_now))
    entry = {"week": body["week"], "course": body.get("course"), "end_date": body.get("end_date"), "built": built,
             "duration": body["duration"], "clips": sum(1 for c in body["clips"] if not c.get("card")), "hash": digest_,
             "winner": body["rows"][0]["name"] if body["rows"] else None}
    if retired:
        entry["retired"] = retired
    index[key] = entry
    gone = [k for k, v in index.items() if season_from and str(v.get("end_date") or "") < season_from]
    for k in gone:
        del index[k]
        _delete(client, bucket, _keys(client, bucket, f"{root}recaps/{k}/"))
    client.put_object(Bucket=bucket, Key=root + "recaps.json", Body=json.dumps({"recaps": index}).encode(),
                      CacheControl="no-cache", ContentType=TYPES[".json"])
    return entry
