"""A league week's recap: the parts of the app's recap (commentator/recap.py) that need no database,
bucket, model or voice, shared with Shotcast Cloud so both tell a week the same way. The app imports
them from here.

`week_facts` reads a week off a Board (its rows, the standings it leaves, who didn't post); with each
team's net to par per hole added as facts["holes"] (team -> {hole: net}), `race` tells the week as
one tournament. `choose` picks the clips from each team's moments (team -> {key, moments, finish?},
each moment {id, at, hole, what, level, bad, player}); `prompt` and `schema` ask a model for the
lines, and `script` reads what it wrote. `turn_moments` asks for the shots a lead turned on
(shotcast_scoring.highlights.turn_shot of a team's round).

Names (teams, players, the course) go into the prompt as they are: a caller with untrusted ones
cleans them first.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from shotcast_scoring.board import Board, place, place_points

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
