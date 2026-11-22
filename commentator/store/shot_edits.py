"""Changing a recorded shot: its result, player, round class, hole, position and track.

Part of Store (commentator/store/core.py), which is these parts together."""
import json
import sqlite3
from typing import Any, Iterable


class ShotEditsMixin:
    """Changing a recorded shot: its result, player, round class, hole, position and track."""

    def shot_is_putt(self, shot_id: int | None, tracked: bool = False) -> bool | None:
        """Whether the stored shot is a putt now (None: no such shot; `tracked`: or no track from GSPro's
        round file applied to it)."""
        row = None if shot_id is None else self.db.execute("SELECT is_putt FROM shots WHERE id = ?",
                                                          (shot_id,)).fetchone()
        if row is None or (tracked and self._track(shot_id) is None):
            return None
        return bool(row[0])

    def clear_miss(self, shot_id: int | None, full: bool = False) -> None:
        """Take back a missed putt's outcome from a stroke the hole has since ended on, now a full shot (a
        putter GSPro's round file filled, taken back; `full`: to be taken back)."""
        if shot_id is not None:
            self.db.execute("UPDATE shots SET outcome = NULL WHERE id = ? AND outcome = 'missed' AND"
                            " (is_putt = 0 OR ?)", (shot_id, full))
            self.db.commit()

    def set_shot_result(self, shot_id: int | None, *, finish_yards: float | None = None,
                        outcome: str | None = None) -> None:
        """Fill in a shot's finishing distance and holed/conceded outcome, known only once the ball
        settles (for a putt, after the reaction path resolves it)."""
        if shot_id is None:
            return
        if finish_yards is not None and not self._tracked_finish(shot_id):
            self.db.execute("UPDATE shots SET finish_yards = ? WHERE id = ?", (finish_yards, shot_id))
        if outcome is not None:
            self.db.execute("UPDATE shots SET outcome = ? WHERE id = ?", (outcome, shot_id))
            if outcome == "holed":
                self._unhole_if_played_on(shot_id, later=True)
        self.db.commit()

    def set_shot_player(self, shot_id: int | None, player: str, *, attributed: bool, fmt: str | None,
                        shared_ball: bool) -> None:
        """Who hit a shot recorded with no name (GSPro's log said whose it was afterwards), and the
        format they were playing."""
        if shot_id is not None:
            self.db.execute("UPDATE shots SET player = ?, attributed = ?, fmt = ?, shared_ball = ? WHERE id = ?",
                            (player, int(attributed), fmt, int(shared_ball), shot_id))
            self.records.reslot([shot_id])
            self.db.commit()

    def set_shot_round_class(self, shot_id: int | None, *, is_league: bool, fmt: str | None, attributed: bool,
                             shared_ball: bool, hole: str | None = None, course: str | None = None,
                             is_tournament: bool = False, record: str | None = None) -> None:
        """A shot recorded before its round started, now taken into it: the round's league/practice
        context and format, whether the stats can credit its player, and (`hole`, `course`) where it
        was when it was labelled with the last round's. Into the round's `record` too, once it has one
        (before, its start takes in its opening shots)."""
        if shot_id is not None:
            self.db.execute("UPDATE shots SET is_league = ?, fmt = ?, attributed = ?, shared_ball = ?,"
                            " hole = coalesce(?, hole), course = coalesce(?, course), is_tournament = ? WHERE id = ?",
                            (int(is_league), fmt, int(attributed), int(shared_ball), hole, course,
                             int(is_tournament), shot_id))
            self.records.take(shot_id, record)
            self.db.commit()

    def mark_tournament(self, after_id: int, record: str | None = None) -> int:
        """A round found to be a tournament one after it started (GSPro's first SGT sync follows its
        first stroke): its shots (`record`'s; or every one since `after_id`, its shot floor) are marked."""
        where, arg = self._scope(after_id, record)
        cur = self.db.execute(f"UPDATE shots SET is_tournament = 1 WHERE {where} AND is_tournament = 0", arg)
        self.db.commit()
        return cur.rowcount

    def mark_league(self, after_id: int, record: str | None = None) -> int:
        """A round found to be a league one only at its end (see Round.make_league): its shots
        (`record`'s; or every one since `after_id`, its shot floor) are league shots."""
        where, arg = self._scope(after_id, record)
        cur = self.db.execute(f"UPDATE shots SET is_league = 1, is_tournament = 0 WHERE {where}", arg)
        self.db.commit()
        return cur.rowcount

    def mark_practice(self, after_id: int, record: str | None = None) -> int:
        """A league round the players said is only practice (Engine._league_answer): its shots so far
        (`record`'s; or every one since `after_id`, its shot floor) are practice shots."""
        where, arg = self._scope(after_id, record)
        cur = self.db.execute(f"UPDATE shots SET is_league = 0 WHERE {where} AND is_league = 1", arg)
        self.db.commit()
        return cur.rowcount

    def finish_of(self, shot_id: int) -> tuple[str | None, float | None]:
        """A shot's stored (lie_after, finish_yards)."""
        row = self.db.execute("SELECT lie_after, finish_yards FROM shots WHERE id = ?", (shot_id,)).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def move_shot_player(self, shot_id: int, player: str, *, fmt: str | None, shared_ball: bool,
                         gimme_id: int | None) -> None:
        """A named shot was someone else's: it, and the conceded putt row it added (`gimme_id`), go to
        `player`."""
        if gimme_id is not None:
            self.db.execute("UPDATE shots SET player = ?, fmt = ? WHERE id = ? AND outcome = 'gimme'",
                            (player, fmt, gimme_id))
        self.db.execute("UPDATE shots SET player = ?, fmt = ?, shared_ball = ? WHERE id = ?",
                        (player, fmt, int(shared_ball), shot_id))
        self.records.reslot([shot_id] + ([gimme_id] if gimme_id is not None else []))
        self.db.commit()

    def set_attributed(self, shot_ids: list[int], attributed: bool) -> None:
        """Whether these shots count in their player's own stats (a shared profile's don't)."""
        if shot_ids:
            self.db.execute(f"UPDATE shots SET attributed = ? WHERE id IN ({', '.join('?' * len(shot_ids))})",
                            (int(attributed), *shot_ids))
            self.db.commit()

    def clear_shot_result(self, shot_id: int, outcome: str, finish: tuple | None = None,
                          gimme_id: int | None = None) -> None:
        """Take back a shot's `outcome`, and the conceded putt row it added (`gimme_id`, when known; it
        was graded against the wrong player's line on GSPro's log). Its finish goes back to `finish`:
        (lie_after, finish_yards)."""
        row = self.db.execute("SELECT player, hole, shot_number FROM shots WHERE id = ?", (shot_id,)).fetchone()
        if row is None:
            return
        if outcome == "conceded" and gimme_id is not None:
            self.db.execute("DELETE FROM shots WHERE id = ? AND outcome = 'gimme'", (gimme_id,))
        elif outcome == "conceded":
            self.db.execute(
                """DELETE FROM shots WHERE id = (SELECT MIN(id) FROM shots WHERE id > ? AND outcome = 'gimme'
                       AND player IS ? AND hole IS ? AND shot_number IS ?)""",
                (shot_id, row[0], row[1], None if row[2] is None else row[2] + 1))
        self.db.execute("UPDATE shots SET outcome = NULL WHERE id = ?", (shot_id,))
        if finish is not None:
            tr = self._track(shot_id)
            if tr is None or tr["lie_after"] is None:
                self.db.execute("UPDATE shots SET lie_after = ? WHERE id = ?", (finish[0], shot_id))
            if tr is None or tr["finish_yards"] is None:
                self.db.execute("UPDATE shots SET finish_yards = ? WHERE id = ?", (finish[1], shot_id))
        self.db.commit()

    def set_mulligan(self, shot_id: int | None) -> None:
        """GSPro took this stroke back (a mulligan): it stays in the table, flagged, and drops out of
        every score and stat query. The club and launch numbers are still a real swing."""
        if shot_id is not None:
            self.db.execute("UPDATE shots SET mulligan = 1 WHERE id = ?", (shot_id,))
            self.db.commit()

    def set_left_out(self, shot_id: int, left_out: bool) -> bool:
        """Leave a shot out of the stats (or put it back), by hand. It stays a stroke on the card and in
        the replay. False when there's no such shot."""
        cur = self.db.execute("UPDATE shots SET left_out = ? WHERE id = ?", (int(left_out), shot_id))
        self.db.commit()
        return cur.rowcount > 0

    def record_course_tees(self, course: str, card: Any) -> None:
        """A course's tees (course_card.CourseCard): rating, slope, yards and par, for our own handicap
        index. Replaces what was kept for the course, so a corrected course file wins."""
        if not course or not (card.yards or card.ratings or getattr(card, "lengths", None)):
            return
        course = self.course_name(course)
        self.db.execute("DELETE FROM tee_lengths WHERE course = ? COLLATE NOCASE", (course,))
        lengths = getattr(card, "lengths", None) or {"": (0, 0)}
        self.db.executemany("INSERT INTO tee_lengths VALUES (?, ?, ?, ?)",
                            [(course, tee, y, n) for tee, (y, n) in lengths.items()])
        par = json.dumps({str(h): p for h, p in card.par.items()})
        index = json.dumps({str(h): i for h, i in card.index.items()})
        rows = []
        for tee in dict.fromkeys([*card.yards, *card.ratings]):
            rating, slope = card.ratings.get(tee, (None, None))
            yards = card.yards.get(tee) or {}
            if rating is None and not any(yards.values()):
                continue
            rows.append((course, tee, rating, slope, json.dumps({str(h): y for h, y in yards.items()}), par, index))
        self.db.execute("DELETE FROM course_tees WHERE course = ? COLLATE NOCASE", (course,))
        self.db.executemany("INSERT INTO course_tees VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        self.db.commit()

    def courses_without_tees(self) -> list[str]:
        """Courses with a round on record but no tees (or tee lengths) kept yet, to fill from GSPro's
        course files."""
        return [r[0] for r in self.db.execute(
            "SELECT DISTINCT course FROM rounds WHERE course IS NOT NULL AND (NOT EXISTS"
            " (SELECT 1 FROM course_tees t WHERE t.course = rounds.course COLLATE NOCASE) OR NOT EXISTS"
            " (SELECT 1 FROM tee_lengths t WHERE t.course = rounds.course COLLATE NOCASE))")]

    def set_shot_position(self, shot_id: int | None, start: tuple) -> None:
        """Where GSPro's log had the ball for this shot, once its score line arrived after it was recorded."""
        if shot_id is not None:
            self.db.execute("UPDATE shots SET start_x = ?, start_y = ?, start_z = ? WHERE id = ? AND start_x IS NULL",
                            (*start, shot_id))
            self.db.commit()

    def set_shot_rest(self, shot_id: int | None, at: float) -> None:
        """When GSPro's log had this shot's ball stop (wall clock), for its highlight clip to end on."""
        if shot_id is not None:
            self.db.execute("UPDATE shots SET rest_ts = ? WHERE id = ? AND rest_ts IS NULL", (at, shot_id))
            self.db.commit()

    def set_picked(self, shot_id: int | None, picked: bool) -> None:
        """Whether the team played on from this shared-ball shot, as GSPro's log said: its pick, or the
        putt that holed. NULL when the log never said (a round before this was read, or no log)."""
        if shot_id is not None:
            self.db.execute("UPDATE shots SET picked = ? WHERE id = ?", (int(picked), shot_id))
            self.db.commit()

    def set_shot_hazard(self, shot_id: int | None, entry: tuple) -> None:
        """Where GSPro's log had this shot's ball go into a penalty area (logged as it rolls or flies in)."""
        if shot_id is not None:
            self.db.execute("UPDATE shots SET hazard_x = ?, hazard_y = ?, hazard_z = ? WHERE id = ? AND hazard_x IS NULL",
                            (*entry, shot_id))
            self.db.commit()

    def set_shot_start(self, shot_id: int | None, shot_number: int | None, lie_before: str | None,
                       replaced: int | None = None) -> None:
        """A re-hit after a mulligan is the same stroke from the same spot as the one it replaced (shot
        id `replaced`). GSPro's log can put the re-hit where the taken-back ball stopped, so the
        replaced shot's position is copied over when both were set up the same distance out (a
        scramble's new pick in between moves the ball, and the distance with it)."""
        if shot_id is None:
            return
        self.db.execute("UPDATE shots SET shot_number = ?, lie_before = ? WHERE id = ?", (shot_number, lie_before, shot_id))
        if replaced is not None:
            self.db.execute(
                "UPDATE shots SET (start_x, start_y, start_z) = (SELECT start_x, start_y, start_z FROM shots"
                " WHERE id = :old) WHERE id = :new AND EXISTS (SELECT 1 FROM shots o WHERE o.id = :old"
                " AND o.start_x IS NOT NULL AND abs(o.to_pin_yards - shots.to_pin_yards) < 0.5)"
                " AND NOT EXISTS (SELECT 1 FROM shot_tracks t WHERE t.shot_id = :new)",
                {"old": replaced, "new": shot_id})
        self.db.commit()

    def relabel_hole(self, old: str, new: str, since_id: int, keep: Iterable[int] = ()) -> None:
        """This session's shots from `since_id` on that were recorded on hole `old` move to `new`, but
        for those in `keep` (played on `old` after all)."""
        keep = list(keep)
        self.db.execute(f"UPDATE shots SET hole = ? WHERE session_id = ? AND hole = ? AND id >= ?"
                        f" AND id NOT IN ({', '.join('?' * len(keep))})",
                        (new, self.session_id, old, since_id, *keep))
        self.db.commit()

    def set_shot_hole(self, shot_ids: Iterable[int], hole: str, shot_number: int | None = None) -> None:
        """Shots moved to the hole (and, given one, the stroke number) GSPro's round file has them on."""
        ids = list(shot_ids)
        if not ids:
            return
        marks = ", ".join("?" * len(ids))
        self.db.execute(f"UPDATE shots SET hole = ? WHERE id IN ({marks})", (hole, *ids))
        if shot_number is not None:
            self.db.execute(f"UPDATE shots SET shot_number = ? WHERE id IN ({marks})", (shot_number, *ids))
        self.db.commit()

    def shot_holes(self, shot_ids: Iterable[int]) -> dict[int, tuple]:
        """Each shot's stored hole and stroke number (for put_shot_holes to put back)."""
        ids = list(shot_ids)
        if not ids:
            return {}
        return {r[0]: (r[1], r[2]) for r in self.db.execute(
            f"SELECT id, hole, shot_number FROM shots WHERE id IN ({', '.join('?' * len(ids))})", ids)}

    def put_shot_holes(self, rows: dict[int, tuple]) -> None:
        """Shots back on the hole and stroke number (none, too) shot_holes had."""
        self.db.executemany("UPDATE shots SET hole = ?, shot_number = ? WHERE id = ?",
                            [(h, n, i) for i, (h, n) in rows.items()])
        self.db.commit()

    def tracked_shots(self, gspro_shot_ids: Iterable[str]) -> dict[str, int]:
        """GSPro's ShotIDs already applied to a shot (shot_tracks): ShotID -> the shot's id."""
        ids = list(gspro_shot_ids)
        if not ids:
            return {}
        return {r[0]: r[1] for r in self.db.execute(
            f"SELECT gspro_shot_id, shot_id FROM shot_tracks WHERE gspro_shot_id IN ({', '.join('?' * len(ids))})", ids)}

    def set_shot_tiles(self, shot_id: int, *, carry_game_yards: float | None, carry_raw_tile_yards: float | None,
                       total_yards: float | None, offline_yards: float | None) -> None:
        """A shot's tiles read after it was recorded, at the next player's setup (GSPro hid them while the
        ball flew). They take the place of the follow's unconfirmed reads and of a total worked out from
        the log; a tile not read leaves its column as it was. GSPro's round file (shot_tracks) wins over
        both: a carry or total it set stays."""
        tr = self._track(shot_id)
        if tr is not None and tr["carry_yards"] is not None:
            carry_game_yards = None
        if tr is not None and (tr["total_yards"] is not None or tr["end_x"] is None):
            total_yards = None   # (no end: a penalty followed, so the ball has no resting total)
        cols = {"carry_game_yards": carry_game_yards, "carry_raw_tile_yards": carry_raw_tile_yards,
                "offline_yards": offline_yards, "total_yards": total_yards}
        sets = [f"{col} = ?" for col, v in cols.items() if v is not None]
        if total_yards is not None:
            sets.append("total_from_log = 0")
        if sets:
            self.db.execute(f"UPDATE shots SET {', '.join(sets)} WHERE id = ?",
                            (*(v for v in cols.values() if v is not None), shot_id))
            self.db.commit()

    def set_lie_after(self, shot_id: int | None, lie: str | None) -> str | None:
        """Where the ball came to rest, corrected once the next shot says where it was played from. A lie
        GSPro's round file set stays. The lie the shot has now."""
        if shot_id is None or not lie:
            return None
        tr = self._track(shot_id)
        if tr is not None and tr["lie_after"]:
            lie = tr["lie_after"]
        self.db.execute("UPDATE shots SET lie_after = ? WHERE id = ?", (lie, shot_id))
        self.db.commit()
        return lie

    def _tracked_finish(self, shot_id: int | None) -> bool:
        """GSPro's round file set the shot's finish, so the booth's own reading of it stands aside."""
        tr = self._track(shot_id)
        return tr is not None and tr["finish_yards"] is not None

    def _track(self, shot_id: int | None) -> sqlite3.Row | None:
        """What GSPro's round file set on the shot (shot_tracks), if it's been applied."""
        if shot_id is None:
            return None
        return self.db.execute("SELECT * FROM shot_tracks WHERE shot_id = ?", (shot_id,)).fetchone()

    def shot_records(self, shot_ids: Iterable[int]) -> dict[int, str | None]:
        """The round record each shot is stored in."""
        ids = list(shot_ids)
        if not ids:
            return {}
        return {r[0]: r[1] for r in self.db.execute(
            f"SELECT id, record_uuid FROM shots WHERE id IN ({', '.join('?' * len(ids))})", ids)}

    def shot_courses(self, shot_ids: Iterable[int]) -> dict[int, str | None]:
        """The course each shot was stored on."""
        ids = list(shot_ids)
        if not ids:
            return {}
        return {r[0]: r[1] for r in self.db.execute(
            f"SELECT id, course FROM shots WHERE id IN ({', '.join('?' * len(ids))})", ids)}

    def courses_played(self) -> list[str]:
        """Every course with a shot stored: one index step each (course_fits ignores case), not a scan
        of every shot."""
        return [r[0] for r in self.db.execute(
            "WITH RECURSIVE c(x) AS (SELECT (SELECT min(course COLLATE NOCASE) FROM shots) UNION ALL"
            " SELECT (SELECT min(course COLLATE NOCASE) FROM shots WHERE course COLLATE NOCASE > c.x)"
            " FROM c WHERE c.x IS NOT NULL) SELECT x FROM c WHERE x IS NOT NULL")]
