"""The league's week results and season standings, live from Squabbit's leaderboards.

The rules (places, points, standings: Board) are shotcast_scoring.board's, shared with
Shotcast Cloud (scoring/); this is the Squabbit side: reading its leaderboards into a Board.

Squabbit scores the league: each week's leaderboard has every team's net to par once they've posted.
A week's places come from those nets (ties share a place), and the season's points from the league's
points table (`points_table` in the history file): a tie splits the points of the places it covers, so
two teams tied for 1st each get the average of the 1st and 2nd place points. Checked against the 2025
season's spreadsheet: the same points on 106 of 108 team-weeks (the other two were a tie the
spreadsheet recorded that Squabbit's nets don't show).

A week counts toward the standings once it's final: every team has posted, or its deadline has passed.
"""

from __future__ import annotations

import re
from typing import Any

from shotcast_scoring.board import ALL, Entry, flight_label, place, place_points
from shotcast_scoring.board import Board as _Board

from .league import League

__all__ = ["ALL", "Board", "Entry", "flight_label", "place", "place_points", "squabbit_holes", "week_of"]


def week_of(tournament: dict[str, Any]) -> int | None:
    m = re.search(r"week\s*(\d+)", tournament.get("name") or "", re.I)
    return int(m.group(1)) if m else None


def squabbit_holes(tournament: dict[str, Any], rounds: list[dict[str, Any]], card: dict[str, Any],
                   league: League) -> dict[str, dict[int, int]]:
    """Each team's net to par hole by hole in a league week, from its players' Squabbit rounds the way
    the leaderboard scores them (handicap.team_holes): league team -> {hole number: net}. `rounds`:
    the league's player rounds (any week's); `card`: the week's (handicap.week_card). A team whose
    rounds can't be scored is left out."""
    from .handicap import _format, team_holes

    fmt = _format(tournament)
    tid = tournament.get("id") or tournament.get("_id")
    by_user = {r["userId"]: r for r in rounds if r.get("tournamentId") == tid and r.get("userId")}
    out: dict[str, dict[int, int]] = {}
    for team in (fmt.get("teams") or {}).values():
        members = [by_user[u] for u in team.get("userIds") or [] if u in by_user]
        if not members:
            continue
        try:
            holes = team_holes(card, members, fmt.get("formatType"))
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if holes:
            out[Board._team(league, team, team.get("name"))] = holes
    return out


class Board(_Board):
    """shotcast_scoring's Board over the app's League, read from Squabbit's leaderboards."""

    @classmethod
    def from_squabbit(cls, items: list[tuple[dict, dict | None]], league: League,
                      points_table: list[float] | None) -> "Board":
        """`items`: each league tournament with its leaderboard document (or None)."""
        from .handicap import _format

        weeks: dict[int, list[Entry]] = {}
        for t, lb in items:
            week = week_of(t)
            boards = list(((lb or {}).get("leaderboards") or {}).values())
            if week is None or not boards:
                continue
            teams = _format(t).get("teams") or {}
            entries = []
            for e in boards[0].get("teamEntries") or []:
                total = e.get("total")
                team = cls._team(league, teams.get(e.get("entityId")) or {}, e.get("entityName"))
                entries.append(Entry(team=team, net=None if total is None or e.get("dnf") else int(total)))
            weeks[week] = entries
        return cls(weeks, league, points_table)

    @staticmethod
    def _team(league: League, squabbit_team: dict, name: str | None) -> str:
        """The league team behind a Squabbit team: by its players' Squabbit ids, else by its name."""
        teams = {league.player_team.get(p) for u in squabbit_team.get("userIds") or []
                 if (p := league.squabbit_ids.get(u))}
        if len(teams) == 1 and None not in teams:
            return teams.pop()
        return league.match_team(name) or name or "?"
