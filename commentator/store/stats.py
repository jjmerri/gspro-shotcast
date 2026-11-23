"""Reads for the booth's facts: drives, closest to the pin, proximity, putting and scoring.

Part of Store (commentator/store/core.py), which is these parts together."""
import json
import sqlite3
from typing import Any, Callable

from .common import DRIVE_SQL, UNKNOWN_CLUB_DRIVE_SQL, _DRIVE, _ctx_sql


class StatsMixin:
    """Reads for the booth's facts: drives, closest to the pin, proximity, putting and scoring."""

    def season_stats(self, player: str, context: str | None = None) -> dict[str, Any]:
        """Season numbers for one player, in yards, excluding tonight so 'season best' means before tonight:
        shots, longest_drive, avg_drive, nights (booth_history)."""
        return self.history.season_stats(player, context)

    def today_longest_drive(self, player: str) -> float | None:
        """The player's longest drive already recorded today, so a second big drive tonight isn't
        another 'personal best' when an earlier one tonight went further."""
        r = self.db.execute(
            f"""SELECT MAX({_DRIVE}) AS longest FROM shots
               WHERE player = ? AND {DRIVE_SQL}
                     AND session_id IN (SELECT id FROM sessions WHERE date = (SELECT date FROM sessions WHERE id = ?))
                     AND mulligan = 0 AND left_out = 0 AND excluded = 0""",
            (player, self.session_id),
        ).fetchone()
        return r["longest"] if r else None

    def today_longest_league_drive(self) -> tuple[str, float] | None:
        """The longest league drive already recorded today, anyone's: (player, yards). The season record
        (season_longest_drive) is before today, so without it Derek's 255 was "the league's longest" an
        hour after his 280 (Bethpage, 11-16)."""
        r = self.db.execute(
            f"""SELECT player, {_DRIVE} AS d FROM shots
               WHERE {DRIVE_SQL} AND player IS NOT NULL AND player != 'unknown'
                     AND session_id IN (SELECT id FROM sessions WHERE date = (SELECT date FROM sessions WHERE id = ?))
                     AND mulligan = 0 AND left_out = 0{_ctx_sql('league')} AND {_DRIVE} IS NOT NULL
               ORDER BY d DESC LIMIT 1""",
            (self.session_id,),
        ).fetchone()
        return (r["player"], r["d"]) if r else None

    def rename_shots(self, old: str, new: str, after_id: int, record: str | None = None) -> int:
        """A player's shots in a round (`record`'s; or every one since `after_id`) go to their right name,
        and so does their slot. Returns how many."""
        where, arg = self._scope(after_id, record)
        olds = [old]
        if self.records.known(record):
            self.records.rename(record, old, new)
            # (and in another case, as a restart or a misread had it: the same slot now)
            olds = [p for (p,) in self.db.execute(f"SELECT DISTINCT player FROM shots WHERE {where}", arg)
                    if p and p != new and p.casefold() in (old.casefold(), new.casefold())]
        n = sum(self.db.execute(f"UPDATE shots SET player = ? WHERE player = ? AND {where}", (new, o, *arg)).rowcount
                for o in olds)
        self.db.commit()
        return n

    def last_shot_id(self) -> int:
        return self.db.execute("SELECT COALESCE(MAX(id), 0) FROM shots").fetchone()[0]

    def week_ctp(self, courses: list[str], holes: list[int], since: str, before_id: int | None = None,
                 team_of: Callable[[str], str | None] | None = None) -> dict[int, tuple[str, float]]:
        """The closest league tee shot on each closest-to-the-pin hole of this course since `since`
        (YYYY-MM-DD): hole number -> (player, yards). Only a tee shot that finished on the green (or
        in the hole) counts. Every format: a scramble records a finish only for the ball the team
        picked (its best, so the one that counts), and a shot whose hitter isn't known (one GSPro
        profile for an alternate shot or scramble team) goes to the team, `team_of(player)`, or is
        left out without it. `before_id` leaves out shots recorded after it (the round being played)."""
        from shotcast_scoring import ctp

        from ..league import course_match_score

        rows = self.db.execute(
            """SELECT sh.player, sh.course, sh.hole, sh.finish_yards, sh.attributed, sh.outcome, sh.shot_number,
                      sh.mulligan, sh.is_putt, sh.lie_after FROM shots sh
               JOIN sessions se ON se.id = sh.session_id
               WHERE sh.is_league = 1 AND sh.excluded = 0 AND sh.left_out = 0 AND sh.shot_number = 1
                     AND sh.player IS NOT NULL AND sh.player != 'unknown' AND se.date >= ?
                     AND (? IS NULL OR sh.id <= ?) ORDER BY sh.id""",
            (since, before_id, before_id),
        ).fetchall()
        marks = []
        for r in rows:
            hole, yards = ctp.hole_number(r["hole"]), ctp.mark(dict(r))
            if hole is None or yards is None:
                continue
            if not any(c and (c.lower() == (r["course"] or "").lower() or course_match_score(c, r["course"] or "") >= 0.6)
                       for c in courses):
                continue
            who = r["player"] if r["attributed"] else (team_of(r["player"]) if team_of else None)
            if who:
                marks.append((hole, who, yards))
        return ctp.closest(marks, holes)

    def players_after(self, after_id: int, record: str | None = None) -> list[str]:
        """The named players on a round's shots (`record`'s; or those recorded after `after_id`)."""
        where, arg = self._scope(after_id, record)
        return [r[0] for r in self.db.execute(
            f"SELECT DISTINCT player FROM shots WHERE {where} AND player IS NOT NULL AND player != 'unknown'", arg)]

    def restamp_shots(self, after_id: int, player: str, *, fmt: str | None, attributed: bool,
                      shared_ball: bool, record: str | None = None) -> None:
        """A player's shots in a round (`record`'s; or after `after_id`) re-read under its format (see
        Engine._league_after_quit)."""
        where, arg = self._scope(after_id, record)
        self.db.execute(f"UPDATE shots SET fmt = ?, attributed = ?, shared_ball = ? WHERE {where} AND player = ?",
                        (fmt, int(attributed), int(shared_ball), *arg, player))
        self.db.commit()

    def season_longest_drive(self, context: str | None = None) -> tuple[str, float] | None:
        """The longest drive before today: (player, yards) (booth_history)."""
        return self.history.season_longest_drive(context)

    def hole_scoring(self, course: str | None, hole: int | None, context: str | None = None) -> list[int]:
        """Every gross score recorded on this hole of this course across all rounds (including
        tonight), for field-scoring facts like 'most players birdied this hole this season'. The
        caller compares to par. rounds.scores is JSON keyed by hole number."""
        if not course or hole is None:
            return []
        out: list[int] = []
        for row in self.db.execute(
                "SELECT scores FROM rounds WHERE course = ? COLLATE NOCASE AND scores IS NOT NULL" + _ctx_sql(context), (course,)):
            try:
                scores = json.loads(row["scores"])
            except (TypeError, ValueError):
                continue
            if isinstance(scores, dict) and isinstance(v := scores.get(str(hole)), int):
                out.append(v)
        return out

    def leaderboard(self) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT player, COUNT(*) AS shots, COUNT(DISTINCT session_id) AS nights,
                      ROUND(MAX(CASE WHEN {DRIVE_SQL} THEN {_DRIVE} END)) AS longest_drive,
                      ROUND(AVG(CASE WHEN {DRIVE_SQL} THEN {_DRIVE} END)) AS avg_drive,
                      ROUND(100.0 * SUM(CASE WHEN (club = 'DR' OR {UNKNOWN}) AND lie_after = 'fairway' THEN 1 ELSE 0 END)
                            / NULLIF(SUM(CASE WHEN (club = 'DR' OR {UNKNOWN}) AND lie_after IS NOT NULL THEN 1 ELSE 0 END), 0)) AS fairway_pct
               FROM shots WHERE player IS NOT NULL AND player != 'unknown' AND mulligan = 0 AND left_out = 0 AND excluded = 0
               GROUP BY player ORDER BY longest_drive DESC""".format(_DRIVE=_DRIVE, DRIVE_SQL=DRIVE_SQL, UNKNOWN=UNKNOWN_CLUB_DRIVE_SQL)
        ).fetchall()

    def player_profile(self, player: str, context: str | None = None) -> dict[str, Any]:
        """Season colour for a player: how much they've played, their most-used club, and their
        start-line tendency (the classic 'typical miss'). start_direction > 0 is right of the aim
        (booth_history)."""
        return self.history.player_profile(player, context)

    def approach_proximity(self, player: str, yards: float, band: float = 15.0,
                           context: str | None = None) -> dict[str, Any]:
        """How close this player's full shots from about `yards` finish to the pin, this season (booth_history)."""
        return self.history.approach_proximity(player, yards, band, context)

    def putting_make_rate(self, player: str, feet: float, band_feet: float = 2.5,
                          context: str | None = None) -> dict[str, Any]:
        """Holed rate on putts from about `feet` this season. An attempt is a putt whose result was
        resolved (holed / conceded / missed); made = holed. Unresolved putts (no result signal) are
        excluded so they don't read as misses, and so are the conceded tap-ins (outcome 'gimme'), which
        nobody hit."""
        lo, hi = max(0.0, feet - band_feet) / 3.0, (feet + band_feet) / 3.0   # feet -> yards
        return self.history.putting_make_rate(player, lo, hi, context)

    def three_putt_rate(self, player: str, feet: float, band_feet: float = 10.0,
                        context: str | None = None) -> dict[str, Any]:
        """How often this player three-putts (or worse) when the FIRST putt is about `feet` feet. A
        hole's putts are grouped by session + course + hole; the first putt (by time) buckets the hole
        by its length, and three or more putts on it is a three-putt (a conceded tap-in's 'gimme' row
        counts, as it does on the card; as on the PGA Tour, any stroke from the green is a putt and one
        from off it isn't). Wider
        band than the make rate:
        lag distance varies more and three-putts are rarer, so a tight band starves the sample.

        Ordered by `ts` (always set), not shot_number (nullable, and SQLite sorts NULLs first, which
        would make a tap-in the 'first' putt). `hole IS NOT NULL` drops pre-first-hole putts, whose
        NULL hole SQLite would otherwise lump into one giant 'hole'. One residual: a player who plays
        the same course+hole twice in a single session (two rounds a night) has them merged - rare, and
        the min-sample gate on the caller blunts it."""
        lo, hi = max(0.0, feet - band_feet) / 3.0, (feet + band_feet) / 3.0   # feet -> yards
        return self.history.three_putt_rate(player, lo, hi, context)

    _BEFORE_TODAY = "session_id NOT IN (SELECT id FROM sessions WHERE date = (SELECT date FROM sessions WHERE id = ?))"

    def approach_leave(self, player: str, lo_yards: float, hi_yards: float | None = None) -> dict[str, Any]:
        """Season baseline for the round sheet, before today: how far from the hole this player's full
        shots from `lo_yards` up to `hi_yards` finish when they reach the green (holed = 0)."""
        return self.history.approach_leave(player, lo_yards, hi_yards if hi_yards is not None else 1e9)

    def putt_band_rate(self, player: str, lo_feet: float, hi_feet: float | None = None) -> dict[str, Any]:
        """Season baseline for the round sheet, before today: putts from over `lo_feet` up to `hi_feet`.
        Conceded misses count as misses; the never-struck tap-ins ('gimme') don't count."""
        return self.history.putt_band_rate(player, lo_feet / 3.0, (hi_feet if hi_feet is not None else 1e9) / 3.0)

    def length_norm(self, player: str, club: str | None = None) -> dict[str, Any]:
        """How far this player usually hits it (median total length, as GSPro measured it): drives (from
        the tee with 250+ yards to the pin), or one club's full swings. Which swings count as full never
        depends on how far that swing went (that would drop the good ones and keep the mishits): first
        the club's typical length over everything but chips, then only shots whose pin was well beyond
        that, so the player wasn't hitting a partial. A club's swings count only off the tee or fairway:
        rough, sand and pine straw take distance off (a 3-wood out of the rough made "his reliable
        110-yard option") (booth_history)."""
        return self.history.length_norm(player, club)

    def shot_lengths(self, shot_ids: list[int]) -> dict[int, float]:
        """GSPro's total length for these shots, where the HUD showed it (none left out of the stats)."""
        ids = [i for i in shot_ids if i is not None]
        if not ids:
            return {}
        rows = self.db.execute(
            f"SELECT id, total_yards FROM shots WHERE total_yards IS NOT NULL AND left_out = 0 AND id IN ({','.join('?' * len(ids))})", ids)
        return {r["id"]: r["total_yards"] for r in rows}

    def fairways_hit(self, player: str, context: str | None = None) -> dict[str, Any]:
        """Driving accuracy: of this player's driver tee shots this season, how many found the fairway.
        A per-player SHOT stat, so it works in a TWO-ball scramble too; it's empty only for the shared-
        ball formats (alternate, and a one-ball scramble), which are not attributed to a single player
        (booth_history)."""
        return self.history.fairways_hit(player, context)

    def recovery_proximity(self, player: str, lies: tuple[str, ...], max_to_pin: float | None = None,
                           context: str | None = None) -> dict[str, Any]:
        """How close this player's shots from a given lie type (greenside sand, rough) finish to the pin,
        in yards. `max_to_pin` limits to greenside (a bunker within ~30 yd, not a fairway bunker). A
        per-player SHOT stat; empty for the shared-ball formats, which aren't attributed to a player
        (booth_history)."""
        return self.history.recovery_proximity(player, tuple(lies), max_to_pin, context)
