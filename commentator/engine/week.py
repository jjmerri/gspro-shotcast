"""The league week: Squabbit's leaderboard and the sim's closest to the pin.

Part of Engine (commentator/engine/core.py), which is these parts together."""
import time

from ..board import place
from ..facts import ctp_text as _ctp_text, ordinal
from ..round import Round, to_par


class WeekMixin:
    """The league week: Squabbit's leaderboard and the sim's closest to the pin."""

    def _week_others(self, r: Round) -> list:
        """This week's results posted on Squabbit by teams not in this round, best first ([] when unknown)."""
        week = r.schedule.get("week")
        if not self.board or not r.is_league or not week:
            return []
        here = {t for t in r.teams if t}
        return [e for e in self.board.posted(week) if e.team not in here]

    def _record_week_results(self, r: Round, row_of: dict[str, int]) -> None:
        """Keep each league team's finished week net from this round (the stats page shows it until the
        team posts on Squabbit), and put the page's league cards up with it."""
        week = r.schedule.get("week") if r.is_league else None
        if not week:
            return
        done = False
        for t in dict.fromkeys(t for t in r.teams if t in self.league.team_players):
            if (tn := r.team_net(t)) and tn[1] == r.holes and t in row_of:
                holes = {r.start_hole + q - 1: v for q, v in (r.team_holes(t) or {}).items()}
                self.store.record_week_result(week, r.schedule.get("end_date"), t, tn[0], row_of[t], holes)
                done = True
        if done:
            self._publish_league_page()

    def _week_panel(self) -> dict | None:
        """The Live tab's week board while a league round is on (live/week.json): every team that's
        posted this week on Squabbit or finished it on the sim, the team(s) on the sim now with their
        running net, and who's yet to play. None outside a league round, or with no board for its week."""
        r = self.round
        board = self._league_board() if r is not None and not r.ended and r.is_league else None
        week = r.schedule.get("week") if board else None
        if not week or week not in board.weeks:
            return None
        here = [t for t in r.teams if t in self.league.team_players]
        name = self.league.team_name
        players = self.league.team_players.get
        rows = [{"team": name(e.team), "players": players(e.team, []), "net": e.net, **({"sim": True} if e.sim else {})}
                for e in board.posted(week) if e.team not in here]
        for t in here:
            tn = r.team_net(t)
            rows.append({"team": name(t), "players": players(t, []), "net": tn[0] if tn else None,
                         "thru": tn[1] if tn else 0, "on": True})
        return {"week": week, "course": r.schedule.get("course") or r.course, "deadline": r.schedule.get("end_date"),
                "holes": r.holes, "rows": rows,
                "waiting": [name(e.team) for e in board.weeks[week] if e.net is None and e.team not in here]}

    def _board_standings(self, r: Round) -> list[str] | None:
        """The season standings from Squabbit's final weeks. None when the board can't say (no fetch,
        no points table), so the history file's standings stand in."""
        b = self.board
        if not b or not b.points_table:
            return None
        table, weeks = b.standings(time.strftime("%Y-%m-%d"))
        if not weeks:
            return ["No league week is final yet this season, so there are no standings to talk about."]
        def where(pts, pl):
            return ("tied " if sum(1 for _, q, _ in table if q == pts) > 1 else "") + ordinal(pl)
        facts = [f"Season standings after {weeks} final week{'s' if weeks != 1 else ''} (from Squabbit): "
                 + ", ".join(f"{where(pts, pl)} {self.league.team_name(t)} with {pts:g} points"
                             for t, pts, pl in table[:3]) + f", of {len(table)} teams."]
        top = {t for t, _, _ in table[:3]}
        for t, pts, pl in table:
            if t in r.teams and t not in top:
                facts.append(f"{self.league.team_name(t)} are {where(pts, pl)} in the standings with {pts:g} points.")
        return facts

    def _week_board_facts(self, r: Round, closing: bool) -> list[str]:
        """Where this week stands on Squabbit: the other teams' posted nets (opener), and where this
        round's net would place among them (closer)."""
        week = r.schedule.get("week")
        if not self.board or not r.is_league or not week or week not in self.board.weeks:
            if closing and r.schedule and r.is_league:
                return ["Where this round places for the week isn't known yet: every team plays the same week "
                        "on its own schedule."]
            return []
        others = self._week_others(r)
        here = [t for t in r.teams if t]
        field = self.board.team_count(week) - len(here)
        names = " and ".join(self.league.team_name(t) for t in here) or "this group"
        if not closing:
            if not others:
                return [f"No other team has posted a week {week} score on Squabbit yet, so {names} can set the "
                        f"number to beat."]
            best = [e for e in others if e.net == others[0].net]
            lead = (" and ".join(self.league.team_name(e.team) for e in best)
                    + f" {'share' if len(best) > 1 else 'have'} the best at net {to_par(others[0].net)}")
            nxt = next((e for e in others if e.net != others[0].net), None)
            return [f"Week {week} so far on Squabbit: {len(others)} of the other {field} teams have posted. {lead}"
                    + (f", then {self.league.team_name(nxt.team)} at net {to_par(nxt.net)}" if nxt else "") + "."]
        facts = []
        for team in here:
            tn = r.team_net(team)
            if not tn or tn[1] < r.holes:
                continue
            label = self.league.team_name(team)
            if not others:
                facts.append(f"By the booth's count {label} finish week {week} at net {to_par(tn[0])}. No other team "
                             f"has posted week {week} on Squabbit yet, so that's the score to beat.")
                continue
            pos, tied = place(tn[0], [e.net for e in others] + [tn[0]])
            facts.append(f"By the booth's count {label} finish week {week} at net {to_par(tn[0])}. Against the "
                         f"{len(others)} other team{'s' if len(others) != 1 else ''} posted on Squabbit so far, that "
                         f"would be {'tied ' if tied else ''}{ordinal(pos)} for the week (Squabbit posts the "
                         f"official result).")
        return facts

    def _week_ctp(self, r: Round, before_id: int | None = None) -> dict[int, tuple[str, float]]:
        """The week's closest tee shot on the green per closest-to-the-pin hole, from every league round
        of this week's course on the sim (see Store.week_ctp)."""
        if not r.is_league or not r.ctp_holes:
            return {}
        return self._schedule_ctp(r.schedule, [r.course], r.ctp_holes, before_id)

    def _schedule_ctp(self, entry: dict, courses: list[str | None], holes: list[int],
                      before_id: int | None = None) -> dict[int, tuple[str, float]]:
        """A schedule week's closest tee shots on the green on its closest-to-the-pin holes, from the
        sim's league rounds on its course (see Store.week_ctp)."""
        ends = [e["end_date"] for e in self.league.data.get("schedule") or [] if e.get("end_date")]
        if not ends or not holes:
            return {}
        import datetime

        since = (datetime.date.fromisoformat(min(ends)) - datetime.timedelta(days=120)).isoformat()
        courses = [c for c in (*courses, entry.get("course"), entry.get("gspro_course")) if c]
        return self.store.week_ctp(courses, holes, since, before_id, team_of=self._team_label)

    def _team_label(self, name: str) -> str | None:
        """The league team a GSPro name plays for, by its display name (a shot with no known hitter)."""
        player = self.league.match_player(name)
        team = self.league.player_team.get(player) if player else self.league.match_team(name)
        return self.league.team_name(team) if team else None

    def _closer_week_ctp(self, r: Round) -> dict[int, tuple[str, str, float]] | None:
        """The week's best tee shot per CTP hole from rounds before this one, for the closer to measure
        tonight's against. None outside league weeks with a schedule (the week can't be counted)."""
        if not r.is_league or not r.ctp_holes \
                or not any(e.get("end_date") for e in self.league.data.get("schedule") or []):
            return None
        return {h: (who, _ctp_text(y), y) for h, (who, y) in self._week_ctp(r, before_id=r.shot_floor).items()}

    def _week_ctp_facts(self, r: Round) -> list[str]:
        if not r.is_league:
            return []
        best = self._week_ctp(r)
        facts = []
        for hole in r.ctp_holes:
            if hole in best:
                who, yards = best[hole]
                facts.append(f"Closest to the pin on hole {hole} this week so far: {who}, {_ctp_text(yards)} "
                             f"from the hole.")
            else:
                facts.append(f"Nobody has a tee shot on the green on hole {hole} this week yet.")
        return facts
