"""Rounds, week results, and each player's and team's season.

Part of Store (commentator/store/core.py), which is these parts together."""
import json
import time
from typing import Any, Sequence

from .common import _COUNTS_AS_ROUND, _GROSS_PER_NINE, _NET_PER_NINE, _ctx_sql


class RoundsMixin:
    """Rounds, week results, and each player's and team's season."""

    def record_round(self, *, course, fmt, side, team, holes, stats, strokes, net_to_par, hole_number,
                     hole_pars=None, is_league: bool = True, complete: bool = True, players: list[str] | None = None,
                     is_tournament: bool = False, first_nine: tuple[int, int | None] | None = None,
                     physics: dict | None = None, record: str | None = None) -> int:
        """One side's round. A round quit early is recorded too (complete=0, holes_played short): its
        per-hole and per-try stats count, but it only counts as a round in the season totals once a
        full nine was played (see _COUNTS_AS_ROUND), and those totals are scored per nine holes.
        `first_nine` is (gross to par, net to par) over the first nine holes played, stored only for a
        part-played round so the season ranks the nine it finished rather than a prorated fraction.
        `physics`: GSPro's settings for the round from its log (green_firmness, fairway_firmness,
        course_type, launch_monitor), whichever were logged."""
        # Key per-hole maps by real hole number for cross-round per-hole stats; drop holes whose number
        # is unknown rather than falling back to the round sequence (which would collide with hole 1).
        scores_json = json.dumps({str(h): v for k, v in stats.scores.items() if (h := hole_number(k)) is not None})
        # None (not "{}") when no hole numbers resolved - an unconfirmed start hole makes hole_number()
        # return None for every seq - so team_hole_stats' `hole_pars IS NOT NULL` skips the empty row.
        pars = {str(h): p for k, p in (hole_pars or {}).items() if (h := hole_number(k)) is not None and p}
        pars_json = json.dumps(pars) if pars else None
        # Only a part-played round needs its own nine: a finished one is already a whole number of
        # nines, and halving an 18 is a fair rate, where prorating an 11 is not.
        nine, nine_net = first_nine if first_nine and not complete else (None, None)
        cur = self.db.execute(
            """INSERT INTO rounds (session_id, ended_at, date, course, format, side, team, holes, gross, par, strokes,
                   net_to_par, fir_hit, fir_tries, gir_hit, gir_tries, scramble_hit, scramble_tries, sand_hit,
                   sand_tries, putts, putt_holes, penalties, longest_drive, scores, hole_pars, source, is_league,
                   complete, holes_played, nine_to_par, nine_net_to_par, players, is_tournament, green_firmness,
                   fairway_firmness, course_type, launch_monitor, round_uuid)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.session_id, time.time(), time.strftime("%Y-%m-%d"), self.course_name(course), fmt, side, team, holes,
             stats.gross if stats.holes_scored else None, stats.par or None, strokes, net_to_par,
             *stats.fir, *stats.gir, *stats.scramble, *stats.sand,
             stats.putts if stats.putt_holes else None, stats.putt_holes, stats.penalties, stats.longest_drive,
             scores_json, pars_json, stats.source, int(is_league), int(complete), stats.holes_scored,
             nine, nine_net, json.dumps(players) if players else None, int(is_tournament),
             *((physics or {}).get(k) for k in ("green_firmness", "fairway_firmness", "course_type", "launch_monitor")),
             record if self.records.known(record) else None),
        )
        if self.records.known(record):   # (its players' slots learn their side)
            for name in players or []:
                if slot := self.records.slot(record, name):
                    self.db.execute("UPDATE round_slots SET side = ? WHERE slot_id = ?", (side, slot))
        self.db.commit()
        return cur.lastrowid

    def record_week_result(self, week: int, end_date: str | None, team: str, net: int, round_id: int,
                           holes: dict[int, int] | None = None) -> None:
        self.db.execute("INSERT INTO week_results (ended_at, week, end_date, team, net, round_id, holes) VALUES (?,?,?,?,?,?,?)",
                        (time.time(), week, end_date, team, net, round_id,
                         json.dumps({str(h): v for h, v in holes.items()}) if holes else None))
        self.db.commit()

    def week_results(self, schedule: list[dict]) -> dict[int, dict[str, int]]:
        """This season's finished league weeks by the sim's count: week -> team -> net to par, the
        team's latest round of the week. A week matches the schedule's by number and deadline. A round
        since deleted, excluded or made a practice one on the stats page doesn't count."""
        return self._week_rows(schedule, "net")

    def week_rounds(self, schedule: list[dict]) -> dict[int, dict[str, int]]:
        """week_results' rounds: week -> team -> the rounds row its net is from."""
        return self._week_rows(schedule, "round_id")

    def week_holes(self, schedule: list[dict]) -> dict[int, dict[str, dict[int, int]]]:
        """week_results' hole by hole nets: week -> team -> {hole number: net to par}. A team whose
        latest round of the week has none (recorded before they were kept) is left out."""
        out: dict[int, dict[str, dict[int, int]]] = {}
        for week, teams in self._week_rows(schedule, "holes").items():
            for team, raw in teams.items():
                try:
                    holes = {int(h): int(v) for h, v in json.loads(raw).items()} if raw else None
                except (TypeError, ValueError, AttributeError):
                    holes = None
                if holes:
                    out.setdefault(week, {})[team] = holes
        return out

    def _week_rows(self, schedule: list[dict], field: str) -> dict[int, dict[str, int]]:
        ends = {e["week"]: e.get("end_date") for e in schedule if e.get("week")}
        out: dict[int, dict[str, int]] = {}
        for week, end, team, value in self.db.execute(
                f"SELECT w.week, w.end_date, w.team, w.{field} FROM week_results w JOIN rounds r ON r.id = w.round_id"
                " WHERE r.excluded = 0 AND r.is_league = 1 ORDER BY w.ended_at, w.id"):
            if week in ends and ends[week] == end:
                out.setdefault(week, {})[team] = value
        return out

    def league_round_courses(self, team: str) -> list[str]:
        """The course of every full league round this team has posted (one row per round, not per side)."""
        rows = self.db.execute(
            "SELECT course, ended_at FROM rounds WHERE team = ? AND is_league = 1 AND complete = 1 AND excluded = 0 "
            "AND course IS NOT NULL ORDER BY ended_at", (team,)).fetchall()
        out, last = [], None
        for course, ended in rows:
            if last is None or course != last[0] or ended - last[1] > 60:   # a round's sides land together
                out.append(course)
            last = (course, ended)
        return out

    def record_player_round(self, *, player: str, course: str | None, fmt: str | None, stats,
                            hole_number, hole_pars=None, is_league: bool = True, complete: bool = True,
                            nine_to_par: int | None = None, is_tournament: bool = False,
                            record: str | None = None) -> None:
        """One individual player's round for per-player season stats: FIR/GIR/scrambling/sand-save
        counts, putts, and the per-hole scores and pars (keyed by real hole number, like record_round)
        for the scores-vs-par stats. Scrambling/sand and the hole-score stats are only meaningful in an
        individual-ball format; they're stored as computed and gated out for the shared-ball formats at
        query time by `fmt`. `nine_to_par` is gross to par over the first nine holes played, stored
        only for a part-played round (see record_round)."""
        scores_json = json.dumps({str(h): v for k, v in stats.scores.items() if (h := hole_number(k)) is not None})
        pars = {str(h): p for k, p in (hole_pars or {}).items() if (h := hole_number(k)) is not None and p}
        pars_json = json.dumps(pars) if pars else None
        self.db.execute(
            """INSERT INTO player_rounds (session_id, ended_at, player, course, fmt, fir_hit, fir_tries,
                   gir_hit, gir_tries, scramble_hit, scramble_tries, sand_hit, sand_tries,
                   putts, putt_holes, scores, hole_pars, is_league, complete, holes_played, nine_to_par, is_tournament,
                   round_uuid, slot_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.session_id, time.time(), player, self.course_name(course), fmt,
             *stats.fir, *stats.gir, *stats.scramble, *stats.sand,
             stats.putts if stats.putt_holes else None, stats.putt_holes, scores_json, pars_json, int(is_league),
             int(complete), stats.holes_scored, None if complete else nine_to_par, int(is_tournament),
             *((record, self.records.slot(record, player)) if self.records.known(record) else (None, None))),
        )
        self.db.commit()

    def player_broadcast_stats(self, player: str, context: str | None = None) -> dict[str, Any]:
        """Season rate stats for one individual player (before tonight). FIR is genuine per-player in any
        format (one's own tee shot). GIR, scrambling and sand are summed over individual-ball rounds
        only: in a shared-ball format (scramble/alternate) the partners play one selected ball, so the
        green reached and the hole score aren't a single player's, and those rates would be meaningless."""
        indiv = "(fmt IS NULL OR fmt NOT IN ('scramble', 'alternate'))"
        r = self.db.execute(
            f"""SELECT SUM(fir_hit) AS fir_hit, SUM(fir_tries) AS fir_tries,
                       SUM(CASE WHEN {indiv} THEN gir_hit ELSE 0 END) AS gir_hit,
                       SUM(CASE WHEN {indiv} THEN gir_tries ELSE 0 END) AS gir_tries,
                       SUM(CASE WHEN {indiv} THEN scramble_hit ELSE 0 END) AS scr_hit,
                       SUM(CASE WHEN {indiv} THEN scramble_tries ELSE 0 END) AS scr_tries,
                       SUM(CASE WHEN {indiv} THEN sand_hit ELSE 0 END) AS sand_hit,
                       SUM(CASE WHEN {indiv} THEN sand_tries ELSE 0 END) AS sand_tries,
                       SUM(CASE WHEN {indiv} THEN putts ELSE 0 END) AS putts,
                       SUM(CASE WHEN {indiv} THEN putt_holes ELSE 0 END) AS putt_holes
                FROM player_rounds WHERE player = ? AND session_id NOT IN (SELECT id FROM sessions WHERE date = (SELECT date FROM sessions WHERE id = ?))""" + _ctx_sql(context),
            (player, self.session_id),
        ).fetchone()
        return {k: r[k] for k in r.keys()}

    def player_hole_stats(self, player: str, context: str | None = None) -> dict[str, Any]:
        """Scores-vs-par season stats for one individual player, computed from their stored per-hole
        scores and pars: birdie-or-better rate, average score to par by hole type (par 3/4/5),
        bounce-back rate (birdie-or-better right after a bogey-or-worse), and scoring over the last
        three holes. The per-team twin is team_hole_stats; this reads player_rounds and, like the other
        hole-score stats, uses individual-ball rounds only (a scramble score isn't one player's).
        Returns raw counts/sums so the caller gates on sample size."""
        rows = self.db.execute(
            """SELECT scores, hole_pars FROM player_rounds
               WHERE player = ? AND session_id NOT IN (SELECT id FROM sessions WHERE date = (SELECT date FROM sessions WHERE id = ?)) AND hole_pars IS NOT NULL
                     AND (fmt IS NULL OR fmt NOT IN ('scramble', 'alternate'))""" + _ctx_sql(context),
            (player, self.session_id),
        ).fetchall()
        holes_scored = birdies = 0
        by_par: dict[int, list[int]] = {3: [0, 0], 4: [0, 0], 5: [0, 0]}  # par -> [sum to_par, holes]
        bounce_tries = bounce_hit = 0
        closing_to_par = closing_holes = 0
        for row in rows:
            try:
                scores = {int(h): v for h, v in json.loads(row["scores"] or "{}").items()}
                pars = {int(h): p for h, p in json.loads(row["hole_pars"] or "{}").items()}
            except (ValueError, TypeError):
                continue
            played = sorted(h for h in scores if h in pars)
            for h in played:
                to_par = scores[h] - pars[h]
                holes_scored += 1
                birdies += to_par <= -1
                if pars[h] in by_par:
                    by_par[pars[h]][0] += to_par
                    by_par[pars[h]][1] += 1
            # Bounce-back: a bogey-or-worse hole answered by a birdie-or-better on the very next hole by
            # number. A gap in the card (a hole not recorded) breaks the pair rather than reach across it.
            for prev, nxt in zip(played, played[1:]):
                if nxt == prev + 1 and scores[prev] - pars[prev] >= 1:
                    bounce_tries += 1
                    bounce_hit += scores[nxt] - pars[nxt] <= -1
            # Closing stretch: the last three holes of the round that were actually scored.
            for h in played[-3:]:
                closing_to_par += scores[h] - pars[h]
                closing_holes += 1
        return {
            "holes": holes_scored, "birdies": birdies,
            "par3": tuple(by_par[3]), "par4": tuple(by_par[4]), "par5": tuple(by_par[5]),
            "bounce_tries": bounce_tries, "bounce_hit": bounce_hit,
            "closing_to_par": closing_to_par, "closing_holes": closing_holes,
        }

    def season_rounds(self, side: str, context: str | None = None,
                      skip_rows: Sequence[int] | None = None) -> dict[str, Any]:
        """Earlier rounds for a player or team, before tonight: complete ones, plus a full nine or more
        from one quit early, sliced by `context` (see _ctx_sql). `rounds` counts every scored round;
        `net_rounds` only those with a net score (a handicap known), which the net average and best
        come from - counted off the same expression they average, or a part-played round whose stored
        nine has a net score the round as a whole doesn't would be averaged in but never counted.
        Scores to par are per nine holes, so a 9 and an 18 compare. `skip_rows` (a just-recorded
        round's own rows) counts earlier today's rounds too, leaving out only those rows."""
        if skip_rows is not None:
            earlier = f"id NOT IN ({','.join('?' * len(skip_rows))})" if skip_rows else "1"
            args: tuple = (side, *skip_rows)
        else:
            earlier = "session_id NOT IN (SELECT id FROM sessions WHERE date = (SELECT date FROM sessions WHERE id = ?))"
            args = (side, self.session_id)
        r = self.db.execute(
            f"""SELECT COUNT(*) AS rounds, COUNT({_NET_PER_NINE}) AS net_rounds,
                      AVG({_NET_PER_NINE}) AS avg_net_to_par, MIN({_NET_PER_NINE}) AS best_net_to_par,
                      COUNT({_GROSS_PER_NINE}) AS gross_rounds, AVG({_GROSS_PER_NINE}) AS avg_gross_to_par, MIN({_GROSS_PER_NINE}) AS best_gross_to_par,
                      AVG(1.0 * putts / NULLIF(putt_holes, 0)) AS putts_per_hole,
                      AVG(1.0 * fir_hit / NULLIF(fir_tries, 0)) AS fir_rate,
                      AVG(1.0 * gir_hit / NULLIF(gir_tries, 0)) AS gir_rate
               FROM rounds WHERE side = ? AND {earlier} AND gross IS NOT NULL
                     AND {_COUNTS_AS_ROUND}""" + _ctx_sql(context),
            args,
        ).fetchone()
        return {k: r[k] for k in r.keys()}

    def team_broadcast_stats(self, side: str, context: str | None = None) -> dict[str, Any]:
        """Season rate stats for a side from the aggregate round columns: GIR/FIR rates, scrambling
        (up-and-down) and sand-save percentages, each with the counts behind them so the caller can
        gate on sample size. Excludes tonight's session."""
        r = self.db.execute(
            f"""SELECT COALESCE(SUM(CASE WHEN {_COUNTS_AS_ROUND} THEN 1 ELSE 0 END), 0) AS rounds,
                      SUM(gir_hit) AS gir_hit, SUM(gir_tries) AS gir_tries,
                      SUM(fir_hit) AS fir_hit, SUM(fir_tries) AS fir_tries,
                      SUM(scramble_hit) AS scr_hit, SUM(scramble_tries) AS scr_tries,
                      SUM(sand_hit) AS sand_hit, SUM(sand_tries) AS sand_tries
               FROM rounds WHERE side = ? AND session_id NOT IN (SELECT id FROM sessions WHERE date = (SELECT date FROM sessions WHERE id = ?)) AND gross IS NOT NULL""" + _ctx_sql(context),
            (side, self.session_id),
        ).fetchone()
        return {k: r[k] for k in r.keys()}

    def side_rounds(self, side: str) -> list[dict[str, Any]]:
        """A side's earlier rounds that count (a full nine or more), newest first, earlier today's
        included: the booth's memory of when and where they last played and how. `to_par9` is the gross
        score to par per nine holes (None when a par is missing), so a 9 and an 18 compare."""
        rows = self.db.execute(
            f"""SELECT id, ended_at, date, course, holes, COALESCE(holes_played, holes) AS played, complete,
                      gross, par, putts, putt_holes, fir_hit, fir_tries, gir_hit, gir_tries, penalties,
                      scores, hole_pars, is_league, {_GROSS_PER_NINE} AS to_par9
               FROM rounds WHERE side = ? AND gross IS NOT NULL AND {_COUNTS_AS_ROUND}""" + _ctx_sql(None)
            + " ORDER BY ended_at DESC, id DESC", (side,)).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]

    def player_team_rounds(self, player: str) -> list[dict[str, Any]]:
        """The rounds `player` played as part of a team (a side of more than them), newest first, with the
        same counting rule as side_rounds: so a solo player's record on a course knows they've been there."""
        rows = self.db.execute(
            f"""SELECT id, ended_at, date, course, format, side FROM rounds
               WHERE side != ? AND players IS NOT NULL AND gross IS NOT NULL AND {_COUNTS_AS_ROUND}
                     AND EXISTS (SELECT 1 FROM json_each(rounds.players) WHERE value = ?)"""
            + _ctx_sql(None) + " ORDER BY ended_at DESC, id DESC", (player, player)).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]

    def team_course_history(self, side: str, course: str | None, context: str | None = None) -> dict[str, Any]:
        """A side's record on one course this season (before tonight): rounds played, average and best
        net to par per nine holes. For the 'they love this track' line. None course -> no history."""
        if not course:
            return {"rounds": 0}
        r = self.db.execute(
            f"""SELECT COUNT(*) AS rounds, AVG({_NET_PER_NINE}) AS avg_net_to_par, MIN({_NET_PER_NINE}) AS best_net_to_par
               FROM rounds WHERE side = ? AND course = ? COLLATE NOCASE AND session_id NOT IN (SELECT id FROM sessions WHERE date = (SELECT date FROM sessions WHERE id = ?)) AND {_NET_PER_NINE} IS NOT NULL
                     AND {_COUNTS_AS_ROUND}"""
            + _ctx_sql(context),
            (side, course, self.session_id),
        ).fetchone()
        return {k: r[k] for k in r.keys()}

    def team_hole_stats(self, side: str, context: str | None = None) -> dict[str, Any]:
        """Scores-vs-par stats a broadcast leans on, computed in Python from each round's stored per-hole
        scores and pars (keyed by real hole number): birdie-or-better rate, average score to par by hole
        type (par 3/4/5), bounce-back rate (birdie-or-better right after a bogey-or-worse), and scoring
        to par over the last three holes. Returns raw counts/sums so the caller gates on sample size."""
        rows = self.db.execute(
            """SELECT holes, scores, hole_pars FROM rounds
               WHERE side = ? AND session_id NOT IN (SELECT id FROM sessions WHERE date = (SELECT date FROM sessions WHERE id = ?)) AND hole_pars IS NOT NULL AND gross IS NOT NULL"""
            + _ctx_sql(context),
            (side, self.session_id),
        ).fetchall()
        holes_scored = birdies = 0
        by_par: dict[int, list[int]] = {3: [0, 0], 4: [0, 0], 5: [0, 0]}  # par -> [sum to_par, holes]
        bounce_tries = bounce_hit = 0
        closing_to_par = closing_holes = 0
        for row in rows:
            try:
                scores = {int(h): v for h, v in json.loads(row["scores"] or "{}").items()}
                pars = {int(h): p for h, p in json.loads(row["hole_pars"] or "{}").items()}
            except (ValueError, TypeError):
                continue
            played = sorted(h for h in scores if h in pars)
            for h in played:
                to_par = scores[h] - pars[h]
                holes_scored += 1
                birdies += to_par <= -1
                if pars[h] in by_par:
                    by_par[pars[h]][0] += to_par
                    by_par[pars[h]][1] += 1
            # Bounce-back: a bogey-or-worse hole answered by a birdie-or-better on the very next hole by
            # number. A gap in the card (a hole not recorded) breaks the pair rather than reach across it.
            for prev, nxt in zip(played, played[1:]):
                if nxt == prev + 1 and scores[prev] - pars[prev] >= 1:
                    bounce_tries += 1
                    bounce_hit += scores[nxt] - pars[nxt] <= -1
            # Closing stretch: the last three holes of the round that were actually scored.
            for h in played[-3:]:
                closing_to_par += scores[h] - pars[h]
                closing_holes += 1
        return {
            "holes": holes_scored, "birdies": birdies,
            "par3": tuple(by_par[3]), "par4": tuple(by_par[4]), "par5": tuple(by_par[5]),
            "bounce_tries": bounce_tries, "bounce_hit": bounce_hit,
            "closing_to_par": closing_to_par, "closing_holes": closing_holes,
        }
