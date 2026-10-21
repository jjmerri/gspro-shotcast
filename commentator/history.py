"""League history from earlier seasons (and this season's standings), as facts for the booth.

Built by `python -m commentator import-league`; everything here is counted in code so the
model only phrases it.
"""

import json
import re
from pathlib import Path
from typing import Any

from .league_import import ordinal
from .round import FORMAT_NAMES


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _place(n: int | None, tied: bool = False) -> str:
    return ("tied " if tied else "") + ordinal(n) if n else "unknown"


def ctp_wins(season: dict[str, Any], player: str) -> int:
    """Closest-to-the-pin prizes a player won in a season. The Results sheet holds money, one row
    per player per week, so a $40 week at $20 a pin is two. The prize is the season's "CTP - $N"
    payout, else (an early season with no payout line) its smallest CTP amount."""
    awards = [a for a in season.get("awards", []) if a["kind"] == "ctp" and (a.get("amount") or 0) > 0]
    prize = next((float(m.group(1)) for line in season.get("payouts", [])
                  if (m := re.match(r"ctp\s*-\s*\$(\d+(?:\.\d+)?)", line, re.I))), None)
    prize = prize or min((a["amount"] for a in awards), default=None)
    return sum(max(1, round(a["amount"] / prize)) for a in awards if a["player"] == player)


class History:
    def __init__(self, data: dict[str, Any]) -> None:
        self.current_year = data.get("current_year")
        self.seasons = data.get("seasons", [])

    @classmethod
    def load(cls, path: str | Path | None) -> "History":
        if path and Path(path).exists():
            return cls(json.loads(Path(path).read_text(encoding="utf-8-sig")))
        return cls({})

    def past(self) -> list[dict]:
        return [s for s in self.seasons if s.get("year") != self.current_year]

    def current(self) -> dict | None:
        return next((s for s in self.seasons if s.get("year") == self.current_year), None)

    @staticmethod
    def _entry(season: dict, players: set[str]) -> dict | None:
        return next((t for t in season.get("teams", []) if set(t.get("players", [])) == players), None)

    def team_facts(self, label: str, players: list[str], fmt: str | None = None, course: str | None = None,
                   same_course=None) -> list[str]:
        """`same_course(name)` says whether a past week's course is this round's; without it, by name."""
        pair = set(players)
        same_course = same_course or (lambda name: _same_course(name, course))
        facts = []
        titles = [s["year"] for s in self.past() if s.get("complete") and (t := self._entry(s, pair)) and t.get("rank") == 1]
        if titles:
            years = _join([str(y) for y in sorted(titles)])
            facts.append(f"{label} won the league in {years}" + (", so they're the defending champions." if
                         max(titles) == max(s["year"] for s in self.past()) else "."))
        seasons = [(s, t) for s in self.past() if (t := self._entry(s, pair))]
        if seasons:
            s, t = seasons[0]
            facts.append(f"In {s['year']} {label} finished {_place(t.get('rank'), t.get('rank_tied'))} of "
                         f"{s.get('team_count')} teams.")
            wins = sum(1 for s, t in seasons for w in t["weeks"] if w["place"] == 1)
            if wins:
                facts.append(f"{label} have won or tied for first in {wins} league weeks over "
                             f"{len(seasons)} season{'s' if len(seasons) > 1 else ''}.")
            if fmt:
                weeks = [(s["year"], w) for s, t in seasons for w in t["weeks"] if w["format"] == fmt and w["place"]]
                if weeks:
                    avg = sum(w["place"] for _, w in weeks) / len(weeks)
                    year, best = min(weeks, key=lambda yw: yw[1]["place"])
                    facts.append(f"In {len(weeks)} earlier {FORMAT_NAMES.get(fmt, fmt)} weeks {label} averaged about "
                                 f"{ordinal(round(avg))} place; best: {_place(best['place'], best['tied'])} at "
                                 f"{best['course']} in {year}.")
        else:
            facts.append(f"{label} are a new pairing this season.")
            for p in players:
                past = [(s, t) for s in self.past() for t in s.get("teams", []) if p in t.get("players", [])]
                if past:
                    s, t = past[0]
                    partner = [x for x in t["players"] if x != p]
                    facts.append(f"{p} finished {_place(t.get('rank'), t.get('rank_tied'))} in {s['year']}"
                                 + (f" playing with {partner[0]}." if partner else "."))
                else:
                    facts.append(f"{p} is new to the league this season.")
        if course:
            for s in self.past():
                for t in s.get("teams", []):
                    if pair & set(t.get("players", [])):
                        for w in t["weeks"]:
                            if w.get("course") and same_course(w["course"]) and w["place"]:
                                facts.append(f"{t['label'].replace('/', ' and ')} played {w['course']} in {s['year']} (week {w['week']}, "
                                             f"{FORMAT_NAMES.get(w['format'], w['format'])}) and placed "
                                             f"{_place(w['place'], w['tied'])}.")
        for p in players:
            by_year = [(s.get("year"), n) for s in self.seasons if (n := ctp_wins(s, p))]
            total = sum(n for _, n in by_year)
            if total:
                years = _join([f"{n} in {y}" for y, n in sorted(by_year, key=lambda yn: yn[0] or 0)])
                facts.append(f"{p} has won {total} closest-to-the-pin prize{'s' if total > 1 else ''} "
                             f"in league play ({years}).")
        return facts

    def standings_facts(self, label: str, players: list[str]) -> list[str]:
        season = self.current()
        if not season:
            return []
        teams = [t for t in season.get("teams", []) if t["weeks"]]
        mine = self._entry(season, set(players))
        if not teams:
            return [f"No {season['year']} league weeks have been recorded yet."]
        if not mine or not mine["weeks"]:
            return [f"{label} haven't posted a {season['year']} week yet in the standings."]
        leader = min(teams, key=lambda t: t["rank"])
        facts = [f"{season['year']} standings: {label} are {_place(mine['rank'], mine.get('rank_tied'))} of "
                 f"{season.get('team_count')} with {mine['total']:g} points after {len(mine['weeks'])} weeks."]
        if leader is not mine:
            facts.append(f"The leaders, {leader['label']}, have {leader['total']:g} points.")
        last = mine["weeks"][-1]
        facts.append(f"Last week out ({last['course']}) they placed {_place(last['place'], last['tied'])}.")
        return facts


def _same_course(a: str, b: str) -> bool:
    from .league import course_match_score

    return course_match_score(a, b) >= 0.6
