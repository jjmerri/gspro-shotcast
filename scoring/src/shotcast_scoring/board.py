"""A league's week results and season standings: places, points and flights.

Each week every team has a net to par once it's posted (None till then). A week's places come from
those nets (ties share a place), and the season's points from the league's points table: a tie splits
the points of the places it covers, so two teams tied for 1st each get the average of the 1st and 2nd
place points.

A week counts toward the standings once it's final: every team has posted, or its deadline (its
schedule entry's end_date) has passed.

In a league with flights a team places, scores points and stands among its own flight's teams that
week: the flight it played the week in, else the one it's in now. An Entry's `flight` is that flight's
id (None: none, or no flights).

Usage: build standings the way the app does, `Board(weeks, league, points_table).with_flights(played)`
(or `.with_sim(results, played)`). A Board doesn't resolve flights when it's built: its entries are
in the flights they're given, and with_flights/with_sim set each team's week to the flight `played`
says it played in, else the one the league has it in now. How the server handles past flight
assignments (a team's flight for a week it played before it moved) is a phase 2 design question.

The league itself is a LeagueView: the app's League, or LeagueData for anyone without one.

Callers must pass clean data; the package does not validate. These are the app's rules as they've
always run, taking what the app has always passed and failing as they always have (the Shotcast Cloud
server validates its input before calling them). A None net is a team yet to post: it waits, places
nowhere and scores nothing.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol


@dataclass
class Entry:
    team: str            # the league's team name
    net: int | None      # net to par; None until the team posts
    sim: bool = False    # the net is the sim's count of the team's round, not yet posted officially
    flight: str | None = None   # the flight id the team played the week in (None: none, or no flights)


# Every flight: a board's whole field (flight None is the teams in none).
ALL = ...


def flight_label(name: str | None) -> str:
    """A flight as the facts say it: "the A flight" ("Gold Flight" as it is; None: the teams in none)."""
    if name is None:
        return "the teams without a flight"
    return name if "flight" in name.lower().split() else f"the {name} flight"


def place(net: int, nets: list[int]) -> tuple[int, bool]:
    """This net's place among `nets` (which include it): 1 + how many are better, and whether it's tied."""
    return 1 + sum(1 for n in nets if n < net), sum(1 for n in nets if n == net) > 1


def place_points(position: int, tied: int, table: list[float]) -> float:
    """Points for a place shared by `tied` teams: the average of the places they cover."""
    covered = [table[i] if i < len(table) else 0.0 for i in range(position - 1, position - 1 + tied)]
    return sum(covered) / len(covered)


class LeagueView(Protocol):
    """What a Board reads of its league."""

    # team -> its players' names
    team_players: Mapping[str, Sequence[str]]
    # The teams' flights now: team -> (flight id, name), for the teams in one (empty: no flights).
    flights: Mapping[str, tuple[str, str]]

    @property
    def schedule(self) -> Sequence[Mapping[str, Any]]:
        """The season's weeks: each {"week": n, "end_date": "YYYY-MM-DD", "course": name, ...}."""
        ...

    def team_name(self, team: str) -> str: ...

    def flight(self, team: str | None) -> tuple[str, str] | None: ...

    def week_entry(self, week: int) -> Mapping[str, Any] | None: ...


@dataclass(frozen=True)
class LeagueData:
    """A LeagueView as plain data."""

    team_players: Mapping[str, Sequence[str]]
    schedule: Sequence[Mapping[str, Any]] = ()
    team_names: Mapping[str, str] = field(default_factory=dict)   # team -> display name (none: its own)
    flights: Mapping[str, tuple[str, str]] = field(default_factory=dict)

    def team_name(self, team: str) -> str:
        return self.team_names.get(team) or team

    def flight(self, team: str | None) -> tuple[str, str] | None:
        return self.flights.get(team) if team else None

    def week_entry(self, week: int) -> Mapping[str, Any] | None:
        return next((e for e in self.schedule if e.get("week") == week), None)


class Board:
    def __init__(self, weeks: dict[int, list[Entry]], league: LeagueView, points_table: list[float] | None,
                 flights: dict[str, str] | None = None) -> None:
        self.weeks = weeks
        self.league = league
        self.points_table = points_table or []
        self.flights = flights or {}   # the flights its entries are in: id -> name (none: no flights)

    @staticmethod
    def _flight(league: LeagueView, team: str) -> str | None:
        return (f := league.flight(team)) and f[0]

    def with_flights(self, flights: dict[int, dict[str, tuple[str, str] | None]] | None = None,
                     league: LeagueView | None = None) -> "Board":
        """This board with each team's week in its flight (`league`'s, the active one: its teams'
        flights now; default this board's): the one its sim round of the week was played in
        (Store.week_flights), else the one it's in now. The nets stay Squabbit's."""
        league = league or self.league
        names = {f[0]: f[1] for f in league.flights.values()}
        names |= {f[0]: f[1] for teams in (flights or {}).values() for f in teams.values() if f}

        def flight(w: int, t: str) -> str | None:
            played = (flights or {}).get(w, {})
            return (played[t] or (None,))[0] if t in played else self._flight(league, t)

        return type(self)({w: [replace(e, flight=flight(w, e.team)) for e in es] for w, es in self.weeks.items()},
                          league, self.points_table, names)

    def with_sim(self, results: dict[int, dict[str, int]],
                 flights: dict[int, dict[str, tuple[str, str] | None]] | None = None,
                 league: LeagueView | None = None) -> "Board":
        """This board with the sim's finished rounds (Store.week_results) standing in for teams that
        haven't posted on Squabbit yet. Squabbit's number wins once it's there. `flights`, `league`:
        with_flights'."""
        league = league or self.league
        weeks: dict[int, list[Entry]] = {}
        for w in sorted(set(self.weeks) | set(results)):
            sim = results.get(w, {})
            entries = self.weeks.get(w) or [Entry(t, None) for t in league.team_players]
            known = {e.team for e in entries}
            weeks[w] = [replace(e, net=sim[e.team], sim=True) if e.team in sim and e.net is None else e
                        for e in entries] + [Entry(t, n, sim=True) for t, n in sim.items()
                                             if t not in known and t in league.team_players]
        return type(self)(weeks, league, self.points_table).with_flights(flights)

    def entries(self, week: int, flight=ALL) -> list[Entry]:
        """The week's teams, of `flight` (an id; None: the teams in none) or (ALL) every one."""
        return [e for e in self.weeks.get(week, []) if flight is ALL or e.flight == flight]

    def posted(self, week: int, exclude: str | None = None, flight=ALL) -> list[Entry]:
        """The week's posted results (of `flight`: entries), best first."""
        return sorted((e for e in self.entries(week, flight) if e.net is not None and e.team != exclude),
                      key=lambda e: e.net)

    def team_count(self, week: int, flight=ALL) -> int:
        return len(self.entries(week, flight))

    def week_places(self, week: int) -> dict[str, tuple[int, bool, float]]:
        """Each posted team's (place, tied, points) for the week, among the teams of the flight it played
        the week in (no flights: the whole field). Points 0 without a points table."""
        out = {}
        for e in self.posted(week):
            nets = [o.net for o in self.posted(week, flight=e.flight)]
            pos, tied = place(e.net, nets)
            out[e.team] = pos, tied, place_points(pos, nets.count(e.net), self.points_table) if self.points_table else 0.0
        return out

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

    def by_date(self, today: str) -> tuple[list[tuple[str | None, list[int]]], int | None]:
        """The league's weeks grouped by due date ([(end_date, weeks)] in schedule order, the weeks due the
        same day together), only dates with a week teams have posted in; and which to show first: the
        earliest with a week still open, else the latest. A week without a due date stands alone."""
        groups: dict[str, tuple[str | None, list[int]]] = {}
        schedule = {int(e["week"]) for e in self.league.schedule or [] if e.get("week") is not None}
        for w in sorted(set(self.weeks) | schedule):
            end = (self.league.week_entry(w) or {}).get("end_date")
            groups.setdefault(str(end) if end else f"week {w}", (str(end) if end else None, []))[1].append(w)
        shown = [g for g in groups.values() if any(self.posted(w) for w in g[1])]
        shown.sort(key=lambda g: min(g[1]))
        if not shown:
            return [], None
        live = [i for i, (_, ws) in enumerate(shown) if any(self.posted(w) and not self.final(w, today) for w in ws)]
        return shown, live[0] if live else len(shown) - 1

    def _week_card(self, w: int, today: str) -> dict[str, Any]:
        entry = self.league.week_entry(w) or {}
        entries = self.weeks.get(w) or [Entry(t, None) for t in self.league.team_players]
        end = entry.get("end_date")
        # A week nobody posted in doesn't count toward the standings, but past its deadline it's over.
        return {"week": w, "course": entry.get("course"), "deadline": end,
                "final": self.final(w, today) or bool(end) and str(end) < today,
                "rows": [{**self._row(e.team), "net": e.net, **({"sim": True} if e.sim else {}),
                          **self.flight_row(e.flight)} for e in self.posted(w)],
                "waiting": [self.league.team_name(e.team) for e in entries if e.net is None]}

    def _row(self, t: str) -> dict[str, Any]:
        return {"team": self.league.team_name(t), "players": self.league.team_players.get(t, [])}

    def flight_row(self, flight: str | None) -> dict[str, Any]:
        """A week card row's flight, for the page to place it within: {"flight": its name, None for the
        teams in none} on a board with flights, else nothing (the card as it always was)."""
        return {"flight": self.flights.get(flight)} if self.flights else {}

    def flight_key(self, flight: str | None) -> tuple[bool, str]:
        """Flights in the order they're shown: by name, the teams in none last."""
        return flight is None, self.flights.get(flight) or ""

    def flight_now(self, team: str) -> str | None:
        """The flight `team` is in now (its id; None: none)."""
        return self._flight(self.league, team)

    def page(self, today: str) -> dict[str, Any]:
        """The stats page's Home tab league cards (league.json): every due date's weeks (`dates`, each
        week's results and who's yet to play), which date to open on (`current`, see by_date), and the
        season standings (a sim-counted net is marked "sim", and standings that use one "provisional").
        `week` is the one week older pages show (current_week). Teams by their display names, each with
        its players, so the page can pick out a viewer's own team."""
        team = self._row
        out: dict[str, Any] = {"week": None, "dates": [], "current": None, "standings": None}
        groups, current = self.by_date(today)
        cards = {w: self._week_card(w, today) for _, ws in groups for w in ws}
        out["dates"] = [{"date": d, "weeks": [cards[w] for w in ws]} for d, ws in groups]
        out["current"] = current
        if (w := self.current_week(today)) is not None:
            out["week"] = cards.get(w) or self._week_card(w, today)
        tables, weeks = self.season(today)
        if weeks:
            counted = [w for w in self.weeks if self.final(w, today)]
            out["standings"] = {"weeks": weeks,
                                "rows": [{**team(t), "points": round(p, 2), "place": pl, **self.flight_row(f)}
                                         for f, table in tables for t, p, pl in table],
                                "provisional": any(e.sim for w in counted for e in self.weeks[w])}
        return out

    def standings(self, today: str | None = None, flight=ALL) -> tuple[list[tuple[str, float, int]], int]:
        """[(team, points, place)] best first over the final weeks, and how many weeks that is: each
        week's points by place among the team's flight that week, the table of `flight`'s weeks (ALL:
        every team's)."""
        today = today or datetime.date.today().isoformat()
        if not self.points_table:
            return [], 0
        totals: dict[str, float] = {}
        weeks = [w for w in sorted(self.weeks) if self.final(w, today)]
        for w in weeks:
            places = self.week_places(w)
            for e in self.entries(w, flight):
                totals[e.team] = totals.get(e.team, 0.0) + (places[e.team][2] if e.team in places else 0.0)
        if not weeks:
            return [], 0
        order = sorted(totals.items(), key=lambda kv: -kv[1])
        return [(t, p, 1 + sum(1 for _, q in order if q > p)) for t, p in order], len(weeks)

    def season(self, today: str | None = None) -> tuple[list[tuple[Any, list[tuple[str, float, int]]]], int]:
        """The season standings as they're shown: [(flight, its standings)] (flight_key's order; each
        flight's teams those that played a final week in it), or without flights the one table (ALL).
        And how many final weeks that is."""
        table, weeks = self.standings(today)
        if not self.flights or not weeks:
            return [(ALL, table)], weeks
        final = [w for w in self.weeks if self.final(w, today or datetime.date.today().isoformat())]
        flights = sorted({e.flight for w in final for e in self.entries(w)}, key=self.flight_key)
        return [(f, t) for f in flights if (t := self.standings(today, f)[0])], weeks
