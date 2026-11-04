"""Handicap strokes the way the league's Squabbit computes them, and the season check that proves it.

Squabbit is the source of truth for net scores. These rules reproduce every 2025 team result on its
leaderboards (`python -m commentator squabbit --validate`):

- Each format's handicap settings say whether to use a USGA course handicap. Aggregate does:
  index x slope/113 + (rating - par), scaled to the holes played (a 9-hole round uses half the
  index and half the rating, against the par of the nine). The others use the bare index scaled to
  the holes played. The index is capped at the format's max handicap (54 for aggregate).
- Then the format's percentage: one per player (best ball 75%, aggregate 100%), or for one-ball
  formats a percentage per teammate, lowest handicap first (25% + 20%), summed before rounding.
- Strokes go to the played holes in stroke-index order (the tee's own index), the hardest first.
  A plus handicap gives strokes back on the easiest holes.
"""

from __future__ import annotations

import math
import re
from typing import Any

TEAM_BALL_TYPES = ("Scramble", "AlternateShot")
FORMAT_KEYS = {"Scramble": "scramble", "AlternateShot": "alternate", "BestBall": "best_ball", "TeamSum": "aggregate"}


def round_half_up(x: float) -> int:
    """Handicaps round .5 away from zero (Python's round() would take 2.5 to 2)."""
    return math.floor(x + 0.5) if x >= 0 else -math.floor(-x + 0.5)


def course_handicap(index: float, holes: int, *, use_course: bool = False, rating: float | None = None,
                    slope: float | None = None, par: int | None = None, max_index: float | None = None) -> float:
    """Unrounded handicap for `holes` holes. `par` is the par of just the holes played."""
    if max_index is not None:
        index = min(index, max_index)
    share = holes / 18
    if use_course and rating is not None and slope and par is not None:
        return index * share * slope / 113 + (rating * share - par)
    return index * share


def side_strokes(handicaps: list[float], allowances: list[float], team_ball: bool) -> int:
    """A side's strokes from its players' unrounded handicaps. One ball: lowest handicap takes the
    first allowance, the next the second, summed then rounded. Otherwise the side is one player."""
    if team_ball:
        return round_half_up(sum(h * a for h, a in zip(sorted(handicaps), allowances)))
    return round_half_up(handicaps[0] * allowances[0])


def allocate(strokes: int, order: list[int]) -> dict[int, int]:
    """Strokes per hole, `order` listing the holes hardest first. A plus handicap (negative strokes)
    gives them back from the easiest hole up."""
    out = {h: 0 for h in order}
    if not order:
        return out
    for i in range(abs(strokes)):
        if strokes > 0:
            out[order[i % len(order)]] += 1
        else:
            out[order[-1 - i % len(order)]] -= 1
    return out


def allowances(settings: dict[str, Any] | None) -> list[float] | None:
    """A Squabbit format's percentages as fractions, or None when it doesn't set any."""
    s = settings or {}
    if s.get("useHandicaps") is False:
        return [0.0]
    per = [float(p) / 100 for p in s.get("handicapPercentagesPerPlayer") or []]
    if per:
        return per
    if s.get("handicapPercentageForAll") is not None:
        return [float(s["handicapPercentageForAll"]) / 100]
    return None


def _format(tournament: dict[str, Any]) -> dict[str, Any]:
    return next(iter((tournament.get("formatsV2") or {}).values()), {})


def handicap_settings(fmt: dict[str, Any]) -> dict[str, Any]:
    """Where Squabbit keeps a format's handicap settings: the main game, or (aggregate) per round."""
    main = fmt.get("mainGameTemplate") or {}
    if main.get("handicapSettings"):
        return main["handicapSettings"]
    per_round = next(iter((fmt.get("tournamentRoundsGameTemplate") or {}).values()), {})
    return per_round.get("handicapSettings") or {}


def _by_tee(values: dict[str, Any] | None, tee: dict[str, Any], fallback: Any) -> Any:
    values = values or {}
    return values.get(tee.get("id")) or values.get(tee.get("name")) or fallback


def week_card(tournament: dict[str, Any], tournament_round: dict[str, Any]) -> dict[str, Any] | None:
    """One league week's card from Squabbit: course, tee (rating, slope), par, stroke index and yards
    per hole for that tee, the holes played and the format's handicap settings."""
    courses = tournament_round.get("courses") or {}
    if not courses:
        return None
    course_id, course = next(iter(courses.items()))
    wanted = ((tournament_round.get("teeIds") or {}).get(course_id) or [None])[0]
    tees = course.get("tees") or []
    want = str(wanted or "").lower()
    tee = next((t for t in tees if want in (str(t.get("id")).lower(), str(t.get("name")).lower())),
               tees[0] if tees else {})
    holes = course.get("holes") or []
    fmt = _format(tournament)
    settings = handicap_settings(fmt)
    name = tournament.get("name") or ""
    week = re.search(r"week\s*(\d+)", name, re.I)
    start = tournament_round.get("startingHole")
    start = start.get("holeNumber") if isinstance(start, dict) else start
    return {
        "week": int(week.group(1)) if week else None,
        "name": name,
        "date": tournament.get("date"),
        "format": FORMAT_KEYS.get(fmt.get("formatType") or "", fmt.get("formatType")),
        "course": course.get("name"),
        "holes": tournament_round.get("numHoles"),
        "start_hole": start,
        "tee": {"name": tee.get("name"), "rating": tee.get("rating"), "slope": tee.get("slope")},
        "par": {str(i): _by_tee(h.get("pars"), tee, h.get("par")) for i, h in enumerate(holes, 1)},
        "index": {str(i): _by_tee(h.get("handicaps"), tee, h.get("handicap")) for i, h in enumerate(holes, 1)},
        "yards": {str(i): _by_tee(h.get("yardages"), tee, None) for i, h in enumerate(holes, 1)},
        "handicap": {
            "use_course_handicap": bool(settings.get("useCourseHandicaps")),
            "allowances": allowances(settings),
            "max_index": fmt.get("maxHandicap"),
            "max_hole_score": fmt.get("maxHoleScore"),
        },
    }


def hole_cap(card: dict[str, Any], par: int, strokes: int) -> int | None:
    """The most a hole can count for, gross, under the format's max hole score (e.g. net par + 4)."""
    rule = (card.get("handicap") or {}).get("max_hole_score") or {}
    if rule.get("doublePar"):
        cap = 2 * par
    elif rule.get("amountOverPar") is not None:
        cap = par + int(rule["amountOverPar"])
    else:
        return None
    return cap if rule.get("isGross") else cap + strokes


def strokes_for(card: dict[str, Any], indexes: list[float], holes: list[int], team_ball: bool,
                tee: dict[str, Any] | None = None) -> int:
    """A side's strokes over `holes` from its players' indexes, by the week card's settings."""
    hc = card["handicap"]
    tee = tee or card["tee"]
    par = sum(int(card["par"][str(h)]) for h in holes)
    cap = hc.get("max_index")
    per = [course_handicap(i, len(holes), use_course=hc.get("use_course_handicap", False), rating=tee.get("rating"),
                           slope=tee.get("slope"), par=par, max_index=cap) for i in indexes]
    allow = hc.get("allowances") or [1.0]
    if not team_ball and not hc.get("use_course_handicap"):
        return side_strokes(per, allow, False)
    if not team_ball:   # a USGA course handicap is a whole number before any percentage
        return side_strokes([round_half_up(per[0])], allow, False)
    return side_strokes(per, allow, True)


def validate_season(tournaments: list[dict[str, Any]], rounds: list[dict[str, Any]]) -> dict[str, list]:
    """Recompute every team's net to par on each week's leaderboard from the players' rounds (scores,
    the handicap index stored on the round, its tee) and compare. `tournaments` items carry the
    tournament doc ("t"), its rounds ("rounds") and its leaderboard ("lb"). Returns format ->
    [(week name, team, ours, Squabbit's)]."""
    out: dict[str, list] = {}
    for item in tournaments:
        t, lb = item["t"], item.get("lb") or {}
        if not item.get("rounds") or not lb.get("leaderboards"):
            continue
        card = week_card(t, item["rounds"][0])
        if not card:
            continue
        fmt = _format(t)
        teams = fmt.get("teams") or {}
        tid = t.get("id") or t.get("_id")
        by_user = {r["userId"]: r for r in rounds if r.get("tournamentId") == tid}
        board = next(iter(lb["leaderboards"].values()))
        for entry in board.get("teamEntries") or []:
            team = teams.get(entry.get("entityId")) or {}
            members = [by_user[u] for u in team.get("userIds", []) if u in by_user]
            if entry.get("total") is None or not members:
                continue
            ours = team_net(card, members, fmt.get("formatType"))
            out.setdefault(card["format"], []).append((card["name"], entry.get("entityName"), ours, int(entry["total"])))
    return out


def team_net(card: dict[str, Any], members: list[dict[str, Any]], format_type: str | None) -> int:
    """A team's net to par from its players' Squabbit rounds, under the week card's rules."""
    return sum(team_holes(card, members, format_type).values())


def team_holes(card: dict[str, Any], members: list[dict[str, Any]], format_type: str | None) -> dict[int, int]:
    """team_net hole by hole: each hole's team net to par, by the course's hole number."""
    first = members[0]
    holes = sorted(int(h) for h in first["holeScoresV2"])
    pars = {h: int(card["par"][str(h)]) for h in holes}
    order = sorted(holes, key=lambda h: (int(card["index"][str(h)]), h))
    if format_type in TEAM_BALL_TYPES:
        strokes = strokes_for(card, [m["userHandicap"] for m in members], holes, True, first.get("tee"))
        given = allocate(strokes, order)
        return {h: _capped(card, first["holeScoresV2"][str(h)], pars[h], given[h]) - given[h] - pars[h] for h in holes}
    nets = []
    for m in members:
        s = strokes_for(card, [m["userHandicap"]], holes, False, m.get("tee"))
        given = allocate(s, order)
        nets.append({h: _capped(card, m["holeScoresV2"][str(h)], pars[h], given[h]) - given[h]
                     for h in holes if str(h) in m["holeScoresV2"]})
    if format_type == "BestBall":
        return {h: min(n[h] for n in nets if h in n) - pars[h] for h in holes}
    return {h: sum(n[h] - pars[h] for n in nets if h in n) for h in holes}


def _capped(card: dict[str, Any], gross: int, par: int, strokes: int) -> int:
    cap = hole_cap(card, par, strokes)
    return gross if cap is None else min(gross, cap)
