"""Handicap strokes the way the league's Squabbit computes them, and the season check that proves it.

The rules themselves (strokes, their allocation, the max hole score, a team's net per hole) are
shotcast_scoring.handicap's, shared with Shotcast Cloud (scoring/; re-exported here). This is the
Squabbit side: its week card (week_card), its format settings (allowances), its players' rounds scored
the way its leaderboard does (team_holes, team_net) and the season check (validate_season:
`python -m commentator squabbit --validate`, which these rules pass for every 2025 team result).
"""

from __future__ import annotations

import re
from typing import Any

from shotcast_scoring import handicap as rules
from shotcast_scoring.handicap import allocate, course_handicap, hole_cap, round_half_up, side_strokes, strokes_for

__all__ = ["FORMAT_KEYS", "TEAM_BALL_TYPES", "allocate", "allowances", "course_handicap", "handicap_settings",
           "hole_cap", "round_half_up", "side_strokes", "strokes_for", "team_holes", "team_net", "validate_season",
           "week_card"]

TEAM_BALL_TYPES = ("Scramble", "AlternateShot")
FORMAT_KEYS = {"Scramble": "scramble", "AlternateShot": "alternate", "BestBall": "best_ball", "TeamSum": "aggregate"}


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
    """team_net hole by hole: each hole's team net to par, by the course's hole number.

    Reads Squabbit's documents as this always has (each lookup, int() and arithmetic in the same order,
    so a malformed one fails the same way), capping and netting each posted score, then counts them by
    the shared format rule (shotcast_scoring.handicap.team_holes) over the players who've posted each
    hole. A Squabbit format it doesn't know scores as a sum."""
    first = members[0]
    holes = sorted(int(h) for h in first["holeScoresV2"])
    pars = {h: int(card["par"][str(h)]) for h in holes}
    order = sorted(holes, key=lambda h: (int(card["index"][str(h)]), h))
    gross, given = [], []

    def side(m: dict[str, Any], strokes: dict[int, int], scored: list[int]) -> None:
        capped = {}
        for h in scored:
            capped[h] = _capped(card, m["holeScoresV2"][str(h)], pars[h], strokes[h])
            capped[h] - strokes[h]   # a score that isn't a number fails here, where it always has
        gross.append(capped)
        given.append(strokes)

    if format_type in TEAM_BALL_TYPES:
        fmt = FORMAT_KEYS[format_type]
        side(first, allocate(strokes_for(card, [m["userHandicap"] for m in members], holes, True, first.get("tee")),
                             order), holes)
    else:
        fmt = "best_ball" if format_type == "BestBall" else "aggregate"
        for m in members:
            strokes = allocate(strokes_for(card, [m["userHandicap"]], holes, False, m.get("tee")), order)
            side(m, strokes, [h for h in holes if str(h) in m["holeScoresV2"]])
    nets = rules.team_holes(fmt, gross, given, pars, None, holes, complete_sides=False)
    if len(nets) == len(set(holes)):
        return nets
    # A hole none of the players has posted (a hole key Squabbit wrote another way): 0, as it always was,
    # but best ball has no best of nothing.
    if fmt == "best_ball":
        raise ValueError("min() iterable argument is empty")
    return {h: nets.get(h, 0) for h in holes}


def _capped(card: dict[str, Any], gross: int, par: int, strokes: int) -> int:
    cap = hole_cap(card, par, strokes)
    return gross if cap is None else min(gross, cap)
