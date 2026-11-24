"""Season analytics for the stats page: strokes gained, gapping, typical miss, recovery, trends,
course and hole difficulty, highlights, the shot log and broadcast transcript, and booth stats.

Everything reads the booth's season database (store.py's schema) through a read-only connection.
Shots are taken the way the booth's own stats take them: mulligans and unattributed strokes are left
out, and so are shared-ball rounds (scramble, alternate) wherever a stroke has to be one player's.
"""

import json
import re
import sqlite3
from fractions import Fraction
from collections import Counter, defaultdict
from typing import Any, Callable

from . import night_totals as N
from . import shot_shape
from .round_record import reuse
from .store import DRIVE_SQL

from shotcast_scoring.analytics import (  # noqa: F401 (the shared copy; this module keeps the app's names)
    APPROACH_MAX_YARDS,
    AROUND_GREEN_YARDS,
    BASELINE,
    DEEP_ROUGH_EXTRA,
    DISPERSION_CURVE,
    DISPERSION_MIN,
    ELLIPSE_95,
    FULL_SWING,
    GAPPING_CLEAN_LIES,
    GAPPING_MIN_CLEAN,
    SG_CATEGORIES,
    _category,
    _exact,
    _gimme_end,
    _hole_key,
    _hole_no,
    _par_of,
    add_recovery,
    add_sg,
    dispersion,
    expected,
    gapping_from,
    holes_of,
    lateral_yards,
    miss_label,
    recovery,
    recovery_from,
    recovery_totals,
    sg_from,
    sg_summary,
    sg_totals,
    strokes_gained,
)

SHARED_BALL = ("scramble", "alternate")

# An approach, as strokes gained has it: a full shot from beyond chipping range, not a par-4/5 tee
# shot (a par-3 tee shot is one); and not so far out it's a lay-up wood. A chip: off the green, close.
APPROACH_SQL = (f"is_putt = 0 AND to_pin_yards > {AROUND_GREEN_YARDS} AND to_pin_yards <= {APPROACH_MAX_YARDS}"
                " AND (COALESCE(lie_before, '') != 'tee' OR hole LIKE '%par 3%')")
CHIP_SQL = f"is_putt = 0 AND to_pin_yards <= {AROUND_GREEN_YARDS} AND COALESCE(lie_before, '') NOT IN ('green', 'tee')"


def _ctx(context: str | None, table: str = "") -> str:
    """A WHERE fragment for the round context (league / tournament / practice: neither), on `table`'s
    columns when given. A tournament round is never a league one."""
    t = f"{table}." if table else ""
    return {"league": f" AND {t}is_league = 1",
            "tournament": f" AND {t}is_league = 0 AND {t}is_tournament = 1",
            "practice": f" AND {t}is_league = 0 AND {t}is_tournament = 0"}.get(context or "", "")


# _ctx's filters, on a row
_IN_CONTEXT: dict[str, Callable[[Any], bool]] = {
    "league": lambda r: r["is_league"] == 1,
    "tournament": lambda r: r["is_league"] == 0 and r["is_tournament"] == 1,
    "practice": lambda r: r["is_league"] == 0 and r["is_tournament"] == 0,
}


def holes_played(db: sqlite3.Connection, context: str | None = None, player: str | None = None) -> list[dict]:
    """Each player's strokes grouped into holes, in order: a hole starts at stroke 1, when the stroke
    number goes back, or when the hole changes. Mulligans, unattributed strokes and shared-ball formats
    are left out. A stroke a player left out of their stats stays in its hole (it was still played, and
    it ends the stroke before it) but gains no strokes and counts toward no recovery or chip-in. A hole is `complete` when it ends holed, or in a conceded tap-in."""
    where, sql = HOLE_ROWS_WHERE, HOLE_ROWS_SQL
    if player:
        rows = db.execute(sql.format(where + _ctx(context) + " AND player = ?"), (player,)).fetchall()
    else:
        # every context's rows read once a build (inside round_record.frozen), and each one's picked out
        rows = reuse(db, ("holes_played_rows",), lambda: db.execute(sql.format(where)).fetchall())
        if context in _IN_CONTEXT:
            rows = [r for r in rows if _IN_CONTEXT[context](r)]
    return holes_of(rows)


# holes_played's strokes (WHERE fragment, and the query it goes into)
HOLE_ROWS_WHERE = "attributed = 1 AND mulligan = 0 AND player IS NOT NULL AND player != 'unknown'" \
                  " AND (fmt IS NULL OR fmt NOT IN ('scramble', 'alternate'))"
HOLE_ROWS_SQL = "SELECT s.*, sess.date FROM shots s JOIN sessions sess ON sess.id = s.session_id WHERE {}" \
                " ORDER BY s.player, s.session_id, s.ts, s.id"


def gapping(db: sqlite3.Connection, player: str, context: str | None = None,
            finish: dict[int, float] | None = None, since: str | None = None) -> list[dict]:
    """Per club, carry and launch numbers from the launch monitor, over full swings only: a chip or a
    knock-down (ball speed well under the club's best) would drag a wedge's carry down. Every real swing
    counts, a mulligan's too. Medians and the middle half, so one mishit doesn't move the numbers.
    Only swings from the tee or fairway count (see GAPPING_CLEAN_LIES); a club with too few of those
    uses every lie, and says so (`any_lie`).
    A swing with no raw carry (a launch monitor that sends none, and no CARRY (raw) tile on screen) uses
    GSPro's game carry, slope and weather in it: `game` counts those. `since` (YYYY-MM-DD): the nights
    from then on only.
    `finish`: hole_replay.finish_laterals, worked out here when not given."""
    if finish is None:
        from .hole_replay import finish_laterals   # hole_replay imports stats_web, which imports this
        finish = finish_laterals(db, since)
    raw = "COALESCE(NULLIF(carry_raw_tile_yards, 0), NULLIF(carry_raw, 0))"
    rows = db.execute(
        f"SELECT id, club, COALESCE({raw}, carry_game_yards) AS carry, {raw} IS NULL AS game, total_yards, ball_speed,"
        " club_speed, launch_angle, total_spin, spin_axis, start_direction, handed, lie_before FROM shots WHERE player = ?"
        " AND attributed = 1 AND is_putt = 0 AND left_out = 0 AND club IS NOT NULL AND club != 'PT'"
        f" AND COALESCE({raw}, carry_game_yards) > 0" + _ctx(context)
        + ("" if since is None else " AND session_id IN (SELECT id FROM sessions WHERE date >= ?)"),
        (player,) if since is None else (player, since)).fetchall()
    return gapping_from(rows, finish)


def trends(db: sqlite3.Connection, holes: list[dict], context: str | None = None,
           usual_gimme_ft: float | None = None, sg: list[dict] | None = None, where: str = "") -> dict[str, list[dict]]:
    """Per player, one point per night: score to par per nine, putts per hole, fairways and greens hit,
    and strokes gained per nine. From individual-ball rounds. `sg`: strokes_gained(holes,
    usual_gimme_ft), when already worked out. `where`: a fragment limiting the cards (their sessions,
    to go with `holes`')."""
    nights: dict[tuple, dict] = {}
    for r in db.execute("SELECT pr.*, s.date FROM player_rounds pr JOIN sessions s ON s.id = pr.session_id"
                        " WHERE (fmt IS NULL OR fmt NOT IN ('scramble', 'alternate'))" + _ctx(context, "pr") + where
                        + " ORDER BY pr.ended_at"):
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
    for s in strokes_gained(holes, usual_gimme_ft) if sg is None else sg:
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
    cards, so the hardest and easiest holes stand out. With `player`, from that player's cards only.
    League play adds the team formats' cards (one per team, named for it): every league team plays the
    week's format, so they compare like for like. Practice and all play don't, where a scramble's nine
    isn't anyone's own."""
    by_course: dict[str, dict] = {}
    for (course, who), m in _courses_merged(db, context).items():
        if player and who != player and player not in m["members"]:
            continue
        c = by_course.setdefault(course, _new_course())
        c["players"].extend(p for p in (m["members"] or [who]) if p not in c["players"])
        _add_course(c, m)
    out = []
    for course, c in by_course.items():
        holes = []
        for h, (_, par, scores) in sorted(c["holes"].items()):
            n = sum(scores.values())
            f = Fraction(sum(s * k for s, k in scores.items()), n)
            mean = int(f) if f.denominator == 1 else float(f)   # as statistics.mean gives it
            holes.append({"hole": h, "par": par, "n": n, "avg": round(mean, 2), "to_par": round(mean - par, 2),
                          "birdie_pct": round(100.0 * sum(k for s, k in scores.items() if s < par) / n),
                          "bogey_plus_pct": round(100.0 * sum(k for s, k in scores.items() if s > par) / n),
                          "best": min(scores), "worst": max(scores)})
        ranked = sorted((h for h in holes if h["n"] >= 1), key=lambda h: h["to_par"])
        out.append({"course": course, "rounds": c["rounds"], "players": sorted(c["players"]),
                    "last_played": c["last_played"], "best": c["best"][1] if c["best"] else None, "holes": holes,
                    "easiest": ranked[0]["hole"] if ranked else None, "hardest": ranked[-1]["hole"] if ranked else None})
    out.sort(key=lambda c: (-c["rounds"], c["course"]))
    return out


def _new_course() -> dict[str, Any]:
    """A course's counts over some cards (_collect_courses): rounds of a nine or more, the last date
    played, the best nine as [rank key, best], per hole [first card's key, its par, {score: holes}], and a
    team card's players (`members`; none on an individual's)."""
    return {"rounds": 0, "players": [], "last_played": None, "best": None, "holes": {}, "members": []}


def _add_course(into: dict[str, Any], part: dict[str, Any]) -> None:
    into["rounds"] += part["rounds"]
    into.setdefault("members", []).extend(p for p in part.get("members", ()) if p not in into["members"])
    if part["last_played"] is not None:
        into["last_played"] = max(into["last_played"] or "", part["last_played"])
    if part["best"] is not None and (into["best"] is None or list(part["best"][0]) < list(into["best"][0])):
        into["best"] = part["best"]
    for h, (key, par, scores) in part["holes"].items():
        e = into["holes"].get(h)
        if e is None:
            into["holes"][h] = [key, par, dict(scores)]
            continue
        if list(key) < list(e[0]):   # the par is the first card's
            e[0], e[1] = key, par
        for s, k in scores.items():
            e[2][s] = e[2].get(s, 0) + k


def _courses_merged(db: sqlite3.Connection, context: str | None) -> dict[tuple[str, str], dict[str, Any]]:
    """Each (course, player)'s counts over every night in `context`. Every context's at once, from one
    read of the nights' partials, which aren't kept after."""
    def run() -> dict[str | None, dict[tuple[str, str], dict[str, Any]]]:
        nights = N.per_night(db, "courses", lambda ns: _collect_courses(db, ns), memo=False)
        return {c: _courses_merge(nights, c) for c in (None, *N.CONTEXTS)}
    return reuse(db, ("courses_merged",), run)[context if context in N.CONTEXTS else None]


# A night's league team-format cards (_collect_courses), kept apart: only league play counts them.
LEAGUE_TEAM = "league_team"


def _courses_merge(nights: dict[str, Any], context: str | None) -> dict[tuple[str, str], dict[str, Any]]:
    keep = N.wanted(context)
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for night in nights.values():
        for ctx, cards in night.items():
            if (context == "league") if ctx == LEAGUE_TEAM else keep(ctx):
                for course, players in cards.items():
                    for who, part in players.items():
                        _add_course(out.setdefault((course, who), _new_course()), part)
    return out


def _collect_courses(db: sqlite3.Connection, ns: "N.Nights") -> dict[str, dict[str, dict[str, dict[str, Any]]]]:
    """Per night, per context, per course and player (or league team, under LEAGUE_TEAM): the counts
    behind courses()."""
    out: dict[str, dict[str, dict[str, dict[str, Any]]]] = {d: {} for d in ns.dates}

    def card(r: sqlite3.Row, ctx: str, who: str, members: list[str]) -> None:
        try:
            scores = {int(k): v for k, v in json.loads(r["scores"] or "{}").items() if isinstance(v, int)}
            pars = {int(k): v for k, v in json.loads(r["hole_pars"] or "{}").items() if isinstance(v, int)}
        except (TypeError, ValueError):
            return
        c = out[ns.date[r["session_id"]]].setdefault(ctx, {}).setdefault(r["course"], {}).setdefault(
            who, _new_course())
        c["members"] = members
        key = [r["ended_at"], r["id"]]   # (cards in the order played)
        c["last_played"] = max(c["last_played"] or "", r["date"])
        played = [h for h in sorted(scores) if h in pars]
        _add_course(c, {"rounds": 0, "last_played": None, "best": None,
                        "holes": {h: [key, pars[h], {scores[h]: 1}] for h in played}})
        if len(played) >= 9:   # a round of a nine or more (a one-hole 19th's birdie isn't a -9 nine)
            c["rounds"] += 1
            to_par = sum(scores[h] - pars[h] for h in played)
            # A round quit part-way through is ranked by the nine it finished; prorating the odd
            # holes onto a nine would make the best a score nobody shot. Older rows have no stored nine.
            stored = r["nine_to_par"] if "nine_to_par" in r.keys() else None
            per9 = stored if stored is not None else (to_par * 9 / len(played) if played else None)
            if per9 is not None:
                _add_course(c, {"rounds": 0, "last_played": None, "holes": {}, "best": [
                    [per9, *key], {"player": who, "date": r["date"], "to_par": to_par, "holes": len(played),
                                   "to_par_9": round(per9, 1)}]})

    for r in db.execute("SELECT pr.*, s.date FROM player_rounds pr JOIN sessions s ON s.id = pr.session_id"
                        " WHERE course IS NOT NULL AND (fmt IS NULL OR fmt NOT IN ('scramble', 'alternate'))"
                        + ns.where):
        card(r, N.context_of(r["is_league"], r["is_tournament"]), r["player"], [])
    # A league team format's card is the team's, one per team (player_rounds has a copy for each partner).
    for r in db.execute("SELECT * FROM rounds WHERE course IS NOT NULL AND is_league = 1"
                        " AND format IN ('scramble', 'alternate')" + ns.where):
        try:
            members = [p for p in json.loads(r["players"] or "[]") if isinstance(p, str)]
        except (TypeError, ValueError):
            members = []
        card(r, LEAGUE_TEAM, r["side"], members)
    return out


def _to_par_name(d: int) -> str:
    return {-3: "albatross", -2: "eagle", -1: "birdie", 1: "bogey", 2: "double"}.get(d, f"+{d}" if d > 0 else f"{d}")


def firsts(rows, starts: dict[str, str | None], k: int, date: str = "date") -> dict[tuple, list[dict]]:
    """The first `k` of `rows` (in their order) in each of the date ranges `starts` (key: first night
    YYYY-MM-DD, None for all time), for everyone (None, key) and for each player (player, key)."""
    out: dict[tuple, list[dict]] = {}
    for r in rows:
        r = dict(r)
        d = r.pop("_date") if date == "_date" else r[date]
        for key, since in starts.items():
            if since is not None and (d or "") < since:
                continue
            for who in (None, r["player"]):
                got = out.setdefault((who, key), [])
                if len(got) < k:
                    got.append(r)
    return out


def highlights(db: sqlite3.Connection, holes: list[dict], context: str | None = None,
               player: str | None = None, since: str | None = None) -> dict[str, Any]:
    """The season's moments: aces, eagles, chip-ins, birdie streaks, bounce-backs and blow-ups from the
    cards; and top-five lists of drives, approaches, chips and putts holed from the shots. With
    `player`, that player's own; with `since` (YYYY-MM-DD), only nights from that date on."""
    return highlights_table(db, holes, context, {"": since})(player, "")


def highlights_table(db: sqlite3.Connection, holes: list[dict], context: str | None,
                     starts: dict[str, str | None]) -> Callable[[str | None, str], dict[str, Any]]:
    """highlights() for every player (and everyone, None) and each of the date ranges `starts` (key:
    first night, None for all time), from one pass over the shots: returns pick(player, key)."""
    return highlights_pick(*highlight_rows(db, holes, context), starts)


TOPS = ("drives", "approaches", "chips", "putts")


def highlight_rows(db: sqlite3.Connection, holes: list[dict], context: str | None,
                   where: str = "") -> tuple[list[dict], list[dict], list[dict], list[dict], dict[str, list[dict]]]:
    """highlights_table's moments, streaks, bounce-backs and blow-ups (in the order they happened), and
    each top list's shots (best first, each with its sort key, `_k`). `where`: a fragment limiting them
    to some sessions (`holes` being theirs)."""
    moments: list[dict] = []
    streaks: list[dict] = []
    bounce: list[dict] = []
    blowups: list[dict] = []
    # A card moment's stroke (its id and time, for the page's links and video): the player's hole in
    # that session, the last one played before the round ended.
    by_hole: dict[tuple, list[dict]] = {}
    for h in holes:
        by_hole.setdefault((h["player"], h["session_id"], h["course"], h["hole"]), []).append(h)

    def at(r, hole: int, last: bool = False) -> dict:
        hs = [h for h in by_hole.get((r["player"], r["session_id"], r["course"], hole), [])
              if h["strokes"][0]["ts"] <= r["ended_at"] + 60]
        if not hs:
            return {}
        st = hs[-1]["strokes"][-1 if last else 0]
        return {"id": st["id"], "ts": st["ts"]}

    for r in db.execute("SELECT pr.*, s.date FROM player_rounds pr JOIN sessions s ON s.id = pr.session_id"
                        " WHERE (fmt IS NULL OR fmt NOT IN ('scramble', 'alternate'))"
                        + _ctx(context, "pr") + where + " ORDER BY pr.ended_at, pr.id"):
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
                moments.append({**base, **at(r, h, True), "hole": h, "kind": "ace", "text": f"Hole-in-one on the par-{pars[h]} {h}th"})
            elif d <= -2:
                moments.append({**base, **at(r, h, True), "hole": h, "kind": "eagle",
                                "text": f"{'Albatross' if d <= -3 else 'Eagle'} {scores[h]} on the par-{pars[h]} {h}th"})
            if d >= 3:
                blowups.append({**base, **at(r, h), "hole": h, "score": scores[h], "par": pars[h], "over": d})
            if i and played[i - 1] == h - 1 and (prev := scores[h - 1] - pars[h - 1]) >= 1 and d <= -1:
                bounce.append({**base, **at(r, h), "holes": [h - 1, h], "from": _to_par_name(prev), "to": _to_par_name(d)})
            if d <= -1 and (not run or run[-1] == h - 1):
                run.append(h)
            elif d <= -1:
                run = [h]
            else:
                if len(run) >= 2:
                    streaks.append({**base, **at(r, run[0]), "holes": run[:], "length": len(run)})
                run = []
        if len(run) >= 2:
            streaks.append({**base, **at(r, run[0]), "holes": run[:], "length": len(run)})

    def holed_out(st, hole: int | None) -> dict:
        yd = st["to_pin_yards"]
        return {"player": st["player"], "date": st["date"], "course": st["course"], "hole": hole,
                "id": st["id"], "ts": st["ts"], "kind": "holed out", "text": f"Holed out from {round(yd) if yd else '?'} yd"
                f" ({st['lie_before'] or 'off the green'}, {st['club'] or '?'})"}

    # Chip-ins: a full shot from off the green (not the tee) that was the hole's last stroke.
    for h in holes:
        last = h["strokes"][-1]
        if h["complete"] and last["outcome"] == "holed" and not last["is_putt"] and not last["left_out"] and last["lie_before"] not in ("tee", "green"):
            moments.append(holed_out(last, h["hole"]))
    base = " AND player IS NOT NULL AND player != 'unknown' AND attributed = 1 AND mulligan = 0 AND left_out = 0" \
           + _ctx(context) + where
    drive = "COALESCE(total_yards, carry_game_yards, carry_raw)"
    j = " FROM shots JOIN sessions ON sessions.id = shots.session_id"
    # A shared ball's (a scramble's, an alternate shot's) chip-ins, which `holes` leaves out: its holed
    # full shot from off the green is still the player's who hit it. Not one the player's next stroke
    # played on from, on that hole (the older rows' stale hole-out; as Store._same_hole_next has it).
    nxt = "(SELECT {} FROM shots n WHERE n.session_id = shots.session_id AND n.player = shots.player" \
          " AND n.attributed = 1 AND n.outcome IS NOT 'gimme' AND n.id > shots.id ORDER BY n.id LIMIT 1)"
    for r in db.execute(f"SELECT shots.id, ts, player, course, hole, shot_number, club, lie_before, to_pin_yards, date,"
                        f" {nxt.format('n.hole')} AS next_hole, {nxt.format('n.shot_number')} AS next_n{j}"
                        f" WHERE fmt IN ('scramble', 'alternate') AND outcome = 'holed' AND is_putt = 0"
                        f" AND COALESCE(lie_before, '') NOT IN ('tee', 'green'){base} ORDER BY ts, shots.id"):
        a, b = r["shot_number"], r["next_n"]
        if not (_hole_key(r["next_hole"]) == _hole_key(r["hole"]) and a is not None and b is not None and b > a):
            moments.append(holed_out(r, _hole_no(r["hole"])))
    tops = {}
    for name, (sql, k) in {
        "drives": (f"SELECT shots.id, ts, player, ROUND({drive}) AS v, course, hole, date{j} WHERE {DRIVE_SQL}"
                   f" AND {drive} IS NOT NULL{base} ORDER BY v DESC, ts, shots.id", lambda r: (-r["v"], r["ts"], r["id"])),
        "approaches": (f"SELECT shots.id, ts, player, ROUND(finish_yards * 3) AS v, outcome, club, ROUND(to_pin_yards) AS from_yd,"
                       f" course, hole, date, finish_yards AS _f{j} WHERE {APPROACH_SQL} AND finish_yards IS NOT NULL{base}"
                       f" ORDER BY finish_yards, ts, shots.id", lambda r: (r["_f"], r["ts"], r["id"])),
        "chips": (f"SELECT shots.id, ts, player, ROUND(finish_yards * 3) AS v, outcome, club, ROUND(to_pin_yards) AS from_yd,"
                  f" lie_before AS lie, course, hole, date, finish_yards AS _f{j} WHERE {CHIP_SQL} AND finish_yards IS NOT NULL{base}"
                  f" ORDER BY finish_yards, ts, shots.id", lambda r: (r["_f"], r["ts"], r["id"])),
        "putts": (f"SELECT shots.id, ts, player, ROUND(to_pin_yards * 3) AS v, course, hole, date, to_pin_yards AS _f{j}"
                  f" WHERE is_putt = 1 AND outcome = 'holed' AND to_pin_yards IS NOT NULL{base}"
                  f" ORDER BY to_pin_yards DESC, ts, shots.id", lambda r: (-r["_f"], r["ts"], r["id"])),
    }.items():
        tops[name] = []
        for r in db.execute(sql):
            row = {c: r[c] for c in r.keys() if c != "_f"}
            row["_k"] = k(r)
            tops[name].append(row)
    return moments, streaks, bounce, blowups, tops


def highlights_pick(moments: list[dict], streaks: list[dict], bounce: list[dict], blowups: list[dict],
                    tops: dict[str, list[dict]],
                    starts: dict[str, str | None]) -> Callable[[str | None, str], dict[str, Any]]:
    """highlights_table's pick(player, key), from highlight_rows' lists (tops' rows in any order)."""
    tops = {name: firsts(({c: v for c, v in r.items() if c != "_k"} for r in sorted(rows, key=lambda r: r["_k"])),
                         starts, 5)
            for name, rows in tops.items()}

    def pick(player: str | None, key: str) -> dict[str, Any]:
        since = starts[key]

        def mine(xs: list[dict]) -> list[dict]:
            return [dict(x) for x in xs if (player is None or x["player"] == player)
                    and (since is None or (x["date"] or "") >= since)]
        return {
            "moments": sorted(mine(moments), key=lambda m: m["date"] or "", reverse=True),
            "streaks": sorted(mine(streaks), key=lambda s: (-s["length"], s["date"] or ""))[:10],
            "bounce_backs": sorted(mine(bounce), key=lambda b: b["date"] or "", reverse=True)[:10],
            "blowups": sorted(mine(blowups), key=lambda b: (-b["over"], b["date"] or ""))[:5],
            **{name: [dict(r) for r in got.get((player, key), [])] for name, got in tops.items()},
        }
    return pick


_STOP = set("a an and the of to in on at for is it's its it that this with he his him you your be was are as"
            " but so just from off up out there what all i we our by or not no have has into than".split())


def booth(db: sqlite3.Connection, names: dict[str, str] | None = None, phrases_since: str | None = None) -> dict[str, Any]:
    """The commentators: lines and words by kind, a few signature phrases (from the nights since
    `phrases_since`, a date; None: all of them), and putt-call accuracy by night."""
    names = names or {}
    on_air = tuple(sorted({n.lower() for n in names.values()} | {"nigel", "duke"}))
    per: dict[str, dict] = {}
    rounds_by_date: Counter = Counter()
    lines_by_date: dict[str, int] = {}
    for d, night in N.per_night(db, "booth", lambda ns: _collect_booth(db, ns), memo=False).items():
        for speaker, (lines, words, kinds) in night["speakers"].items():
            p = per.setdefault(speaker, {"key": speaker, "name": names.get(speaker, speaker),
                                         "lines": 0, "words": 0, "kinds": Counter()})
            p["lines"] += lines
            p["words"] += words
            p["kinds"].update(kinds)
            lines_by_date[d] = lines_by_date.get(d, 0) + lines
        rounds_by_date.update(night["rounds"])
    grams: dict[str, dict[str, list]] = defaultdict(dict)
    for night in N.per_night(db, "phrases", lambda ns: _collect_phrases(db, ns, set(on_air)), extra=(on_air,),
                             since=phrases_since, memo=False).values():
        for speaker, got in night.items():
            mine = grams[speaker]
            for g, (n, first) in got.items():
                have = mine.get(g)
                mine[g] = [n, tuple(first)] if have is None else [have[0] + n, min(have[1], tuple(first))]
    calls = [{"date": r["date"], "who": names.get(r["commentator"], r["commentator"]), "calls": r["n"], "right": r["hits"] or 0}
             for r in db.execute("SELECT s.date, p.commentator, COUNT(*) n, SUM(correct) hits FROM predictions p"
                                 " JOIN sessions s ON s.id = p.session_id WHERE correct IS NOT NULL"
                                 " GROUP BY s.date, p.commentator ORDER BY s.date")]
    return {
        "commentators": [{**{k: v for k, v in p.items() if k != "kinds"}, "kinds": dict(p["kinds"]),
                          "words_per_line": round(p["words"] / p["lines"], 1) if p["lines"] else None,
                          "phrases": [g for g, n in _most_common(grams[k], 8) if n >= 2]}
                         for k, p in sorted(per.items())],
        "nights": [{"date": d, "lines": n, "rounds": rounds_by_date.get(d, 0)}
                   for d, n in sorted((d, n) for d, n in lines_by_date.items() if d)],
        "calls": calls,
    }


def _most_common(counts: dict[str, list], k: int) -> list[tuple[str, int]]:
    """The k commonest, a tie going to the phrase said first (as Counter.most_common over the lines
    in order): counts are kept as [n, (line id, word) first said]."""
    return [(g, n) for g, (n, _) in sorted(counts.items(), key=lambda kv: (-kv[1][0], kv[1][1]))[:k]]


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower().replace("\u2019", "'"))


def _collect_booth(db: sqlite3.Connection, ns: "N.Nights") -> dict[str, dict[str, Any]]:
    """Per night: each speaker's lines, words and lines by kind; and the rounds by their date (as many
    as courses played in each session)."""
    out: dict[str, dict[str, Any]] = {d: {"speakers": {}, "rounds": {}} for d in ns.dates}
    for r in db.execute("SELECT session_id, speaker, kind, text FROM lines WHERE 1=1" + ns.where):
        p = out[ns.date[r["session_id"]]]["speakers"].setdefault(r["speaker"], [0, 0, {}])
        p[0] += 1
        p[1] += len(_words(r["text"]))
        p[2][r["kind"]] = p[2].get(r["kind"], 0) + 1
    for sid, d, _ in db.execute("SELECT DISTINCT session_id, date, course FROM rounds WHERE 1=1" + ns.where):
        got = out[ns.date[sid]]["rounds"]
        got[d] = got.get(d, 0) + 1
    return out


def _collect_phrases(db: sqlite3.Connection, ns: "N.Nights", on_air: set[str]) -> dict[str, dict[str, dict]]:
    """Per night, each speaker's phrases: three words, two of them not stop words, none a number or a
    commentator's name; each with [times said, (line id, word) first said]."""
    out: dict[str, dict[str, dict]] = {d: {} for d in ns.dates}
    for r in db.execute("SELECT id, session_id, speaker, text FROM lines WHERE 1=1" + ns.where):
        d = ns.date[r["session_id"]]
        words = _words(r["text"])
        usable = [(w.isalpha() or "'" in w) and w not in on_air for w in words]
        stop = [w in _STOP for w in words]
        mine = out[d].setdefault(r["speaker"], {})
        for i in range(len(words) - 2):
            if usable[i] and usable[i + 1] and usable[i + 2] and stop[i] + stop[i + 1] + stop[i + 2] <= 1:
                g = f"{words[i]} {words[i + 1]} {words[i + 2]}"
                got = mine.get(g)
                if got is None:
                    mine[g] = [1, (r["id"], i)]
                else:
                    got[0] += 1
                    got[1] = min(got[1], (r["id"], i))
    return out


def shot_log(db: sqlite3.Connection, context: str | None = None, *, player: str | None = None,
             date: str | None = None, course: str | None = None, club: str | None = None,
             lie: str | None = None, limit: int = 500, sessions: list[int] | None = None) -> list[dict]:
    """Shots, newest first, each with what the booth said about it. `sessions`: only theirs."""
    where, args = ["1=1"], []
    if sessions is not None:
        where.append("shots.session_id IN (SELECT value FROM json_each(?))")
        args.append(json.dumps(list(sessions)))
    for col, val in (("player", player), ("date", date), ("course", course), ("club", club), ("lie_before", lie)):
        if val:
            where.append(f"{'sessions.' if col == 'date' else 'shots.'}{col} = ?")
            args.append(val)
    rows = db.execute(
        "SELECT shots.id, shots.ts, sessions.date, shots.session_id, shots.player, shots.course, shots.hole, shot_number, club,"
        " is_putt, lie_before, lie_after, ROUND(to_pin_yards, 1) AS to_pin, ROUND(finish_yards, 1) AS finish,"
        " ROUND(COALESCE(carry_raw_tile_yards, carry_raw)) AS carry, ROUND(total_yards) AS total, ball_speed,"
        " club_speed, launch_angle, total_spin, spin_axis, start_direction, outcome, mulligan, left_out, fmt,"
        " handed, COALESCE(carry_game_yards, carry_raw_tile_yards, carry_raw) AS flight"
        " FROM shots JOIN sessions ON sessions.id = shots.session_id WHERE " + " AND ".join(where)
        + _ctx(context, "shots") + " ORDER BY shots.ts DESC, shots.id DESC LIMIT ?",
        (*args, limit)).fetchall()
    out = [dict(r) for r in rows]
    for r in out:
        r["shape"] = shot_shape.shape(r["start_direction"], r["spin_axis"], r.pop("flight"), bool(r["is_putt"]), r.pop("handed"))
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
    def run() -> list[dict]:
        every = [b for night in N.per_night(db, "broadcasts", lambda ns: _collect_broadcasts(db, ns), memo=False).values()
                 for b in night]
        # newest first (a session with no start last; a tie, the later session first); the start read now, as it's
        # not in a night's signature
        start = dict(db.execute("SELECT id, started_at FROM sessions"))
        every.sort(key=lambda b: (start.get(b["id"]) is None, -(start.get(b["id"]) or 0), -b["id"]))
        return every
    return [dict(b) for b in reuse(db, ("broadcasts",), run)]


def _collect_broadcasts(db: sqlite3.Connection, ns: "N.Nights") -> dict[str, list]:
    """Per night, its sessions with lines and shots: {id, date, lines, courses, players}, the courses and players in the order each first appears in the session."""
    courses: dict[int, list[str]] = {}
    players: dict[int, list[str]] = {}
    for sid, course in db.execute("SELECT session_id, course FROM shots WHERE course IS NOT NULL" + ns.where +
                                  " GROUP BY session_id, course ORDER BY session_id, MIN(id)"):
        courses.setdefault(sid, []).append(course)
    for sid, player in db.execute("SELECT session_id, player FROM shots WHERE player IS NOT NULL AND player != 'unknown'"
                                  + ns.where + " GROUP BY session_id, player ORDER BY session_id, MIN(id)"):
        players.setdefault(sid, []).append(player)
    out: dict[str, list] = {d: [] for d in ns.dates}
    for r in db.execute("SELECT s.id, s.date, COUNT(l.id) AS lines FROM sessions s"
                        " JOIN lines l ON l.session_id = s.id"
                        " WHERE EXISTS (SELECT 1 FROM shots WHERE shots.session_id = s.id)"
                        + ns.where.replace("session_id", "s.id") + " GROUP BY s.id"):
        out[ns.date[r["id"]]].append({"id": r["id"], "date": r["date"], "lines": r["lines"],
                                      "courses": courses.get(r["id"], []), "players": players.get(r["id"], [])})
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
