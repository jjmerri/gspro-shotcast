"""The league's week results and season standings, live from Squabbit's leaderboards.

Squabbit scores the league: each week's leaderboard has every team's net to par once they've posted.
A week's places come from those nets (ties share a place), and the season's points from the league's
points table (`points_table` in the history file): a tie splits the points of the places it covers, so
two teams tied for 1st each get the average of the 1st and 2nd place points. Checked against the 2025
season's spreadsheet: the same points on 106 of 108 team-weeks (the other two were a tie the
spreadsheet recorded that Squabbit's nets don't show).

A week counts toward the standings once it's final: every team has posted, or its deadline has passed.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from typing import Any

from .league import League


@dataclass
class Entry:
    team: str            # the league file's team name (Squabbit's when it can't be matched)
    net: int | None      # net to par; None until the team posts
    sim: bool = False    # the net is the sim's count of the team's round, not yet posted on Squabbit


def place(net: int, nets: list[int]) -> tuple[int, bool]:
    """This net's place among `nets` (which include it): 1 + how many are better, and whether it's tied."""
    return 1 + sum(1 for n in nets if n < net), sum(1 for n in nets if n == net) > 1


def place_points(position: int, tied: int, table: list[float]) -> float:
    """Points for a place shared by `tied` teams: the average of the places they cover."""
    covered = [table[i] if i < len(table) else 0.0 for i in range(position - 1, position - 1 + tied)]
    return sum(covered) / len(covered)


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


class Board:
    def __init__(self, weeks: dict[int, list[Entry]], league: League, points_table: list[float] | None) -> None:
        self.weeks = weeks
        self.league = league
        self.points_table = points_table or []

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
                entries.append(Entry(team=cls._team(league, teams.get(e.get("entityId")) or {}, e.get("entityName")),
                                     net=None if total is None or e.get("dnf") else int(total)))
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

    def with_sim(self, results: dict[int, dict[str, int]]) -> "Board":
        """This board with the sim's finished rounds (Store.week_results) standing in for teams that
        haven't posted on Squabbit yet. Squabbit's number wins once it's there."""
        weeks: dict[int, list[Entry]] = {}
        for w in sorted(set(self.weeks) | set(results)):
            sim = results.get(w, {})
            entries = self.weeks.get(w) or [Entry(t, None) for t in self.league.team_players]
            known = {e.team for e in entries}
            weeks[w] = [Entry(e.team, sim[e.team], sim=True) if e.net is None and e.team in sim else e
                        for e in entries] + [Entry(t, n, sim=True) for t, n in sim.items()
                                             if t not in known and t in self.league.team_players]
        return Board(weeks, self.league, self.points_table)

    def posted(self, week: int, exclude: str | None = None) -> list[Entry]:
        """The week's posted results, best first."""
        return sorted((e for e in self.weeks.get(week, []) if e.net is not None and e.team != exclude),
                      key=lambda e: e.net)

    def team_count(self, week: int) -> int:
        return len(self.weeks.get(week, []))

    def final(self, week: int, today: str) -> bool:
        entries = self.weeks.get(week, [])
        if not any(e.net is not None for e in entries):
            return False
        if all(e.net is not None for e in entries):
            return True
        end = (self.league.week_entry(week) or {}).get("end_date")
        return bool(end) and str(end) < today

    def current_week(self, today: str) -> int | None:
        """The week to show as this week's: the earliest one teams have started posting that isn't final,
        else the latest final one (its result stands till the next week's first post)."""
        live = [w for w in sorted(self.weeks) if self.posted(w) and not self.final(w, today)]
        if live:
            return live[0]
        done = [w for w in sorted(self.weeks) if self.final(w, today)]
        return done[-1] if done else None

    def page(self, today: str) -> dict[str, Any]:
        """The stats page's Home tab league cards (league.json): this week's results and who's yet to
        play, and the season standings (a sim-counted net is marked "sim", and standings that use one
        "provisional"). Teams by their display names, each with its players, so the
        page can pick out a viewer's own team."""
        def team(t: str) -> dict[str, Any]:
            return {"team": self.league.team_name(t), "players": self.league.team_players.get(t, [])}
        out: dict[str, Any] = {"week": None, "standings": None}
        if (w := self.current_week(today)) is not None:
            entry = self.league.week_entry(w) or {}
            out["week"] = {"week": w, "course": entry.get("course"), "deadline": entry.get("end_date"),
                           "final": self.final(w, today),
                           "rows": [{**team(e.team), "net": e.net, **({"sim": True} if e.sim else {})}
                                    for e in self.posted(w)],
                           "waiting": [self.league.team_name(e.team) for e in self.weeks[w] if e.net is None]}
        table, weeks = self.standings(today)
        if weeks:
            counted = [w for w in self.weeks if self.final(w, today)]
            out["standings"] = {"weeks": weeks,
                                "rows": [{**team(t), "points": round(p, 2), "place": pl} for t, p, pl in table],
                                "provisional": any(e.sim for w in counted for e in self.weeks[w])}
        return out

    def standings(self, today: str | None = None) -> tuple[list[tuple[str, float, int]], int]:
        """[(team, points, place)] best first over the final weeks, and how many weeks that is."""
        today = today or datetime.date.today().isoformat()
        if not self.points_table:
            return [], 0
        totals: dict[str, float] = {}
        weeks = [w for w in sorted(self.weeks) if self.final(w, today)]
        for w in weeks:
            for e in self.weeks[w]:
                totals.setdefault(e.team, 0.0)
            nets = [e.net for e in self.weeks[w] if e.net is not None]
            for e in self.weeks[w]:
                if e.net is not None:
                    pos, _ = place(e.net, nets)
                    totals[e.team] += place_points(pos, nets.count(e.net), self.points_table)
        if not weeks:
            return [], 0
        order = sorted(totals.items(), key=lambda kv: -kv[1])
        return [(t, p, 1 + sum(1 for _, q in order if q > p)) for t, p in order], len(weeks)
