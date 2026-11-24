"""What the booth notes as it goes: the format, gimmes, the lines spoken.

Part of Store (commentator/store/core.py), which is these parts together."""
import json
import time

from .common import CLOSER_SECONDS
from ..models import Line


class SessionMixin:
    """What the booth notes as it goes: the format, gimmes, the lines spoken."""

    def set_format(self, shot_ids: list[int], fmt: str | None) -> None:
        """Relabel a round's shots once its real format is known (a scramble GSPro's log revealed, or
        none for a player it left on a team of one)."""
        if shot_ids:
            # A scramble's balls are shared, including the tee shots recorded before it was known; a lone
            # player's are their own.
            self.db.executemany("UPDATE shots SET fmt = ?, shared_ball = ? WHERE id = ?",
                                [(fmt, int(fmt == "scramble"), i) for i in shot_ids])
            if fmt == "scramble":
                # A finish GSPro's round file set before the scramble was known goes back to the booth's:
                # a scramble's finish is only for the ball picked (carry_pairs, week_ctp).
                for i in shot_ids:
                    tr = self._track(i)
                    if tr is not None and tr["finish_yards"] is not None:
                        was = json.loads(tr["was"] or "{}")
                        self.db.execute("UPDATE shots SET finish_yards = CASE WHEN outcome = 'holed' AND is_putt = 0"
                                        " THEN 0 ELSE ? END WHERE id = ?", (was.get("finish_yards"), i))   # (a chip-in's stays 0)
                        self.db.execute("UPDATE shot_tracks SET finish_yards = NULL WHERE shot_id = ?", (i,))
            # The conceded putt a stroke left (record_gimme's row, which copied that stroke's format).
            self.db.executemany(
                """UPDATE shots SET fmt = ? WHERE outcome = 'gimme' AND id > ? AND EXISTS (
                       SELECT 1 FROM shots s WHERE s.id = ? AND s.session_id IS shots.session_id
                       AND s.player IS shots.player AND s.hole IS shots.hole
                       AND s.shot_number + 1 IS shots.shot_number)""",
                [(fmt, i, i) for i in shot_ids])
            self.db.commit()

    def record_gimme(self, shot_id: int, left_yards: float | None = None) -> int | None:
        """The conceded putt after `shot_id` ended inside the gimme circle: GSPro counts the stroke but
        nobody hits it, so it's stored as a putt with outcome 'gimme' and no launch data, from the green,
        `left_yards` from the hole (GSPro's tdist, when logged; else just within the gimme radius). Same
        player, hole and flags as the stroke that left it."""
        cur = self.db.execute(
            """INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, units,
                   lie_before, to_pin, to_pin_yards, score_to_par, tags, raw, gimme_yards, outcome, fmt, attributed,
                   is_league, is_tournament, excluded, record_uuid, slot_id, derived_from)
               SELECT session_id, ?, player, course, hole, shot_number + 1, NULL, 1, units, 'green', NULL, ?,
                      score_to_par, '["gimme"]', '{}', gimme_yards, 'gimme', fmt, attributed, is_league,
                      is_tournament, excluded, record_uuid, slot_id, uid
               FROM shots WHERE id = ?""",
            (time.time(), left_yards, shot_id),
        )
        if cur.rowcount:
            self._fill_previous_finish(cur.lastrowid)
        self.db.commit()
        return cur.lastrowid if cur.rowcount else None

    def record_lines(self, kind: str, lines: list[Line], shot_id: int | None = None) -> None:
        # Said about a round already excluded or deleted on the stats page (a closer): so is the line
        # (worked out in the INSERT, so a delete can't land between).
        # Each line keeps its rows (`_line_ids`), for when it airs (mark_aired); one that's already
        # aired gets its air time now.
        now = time.time()
        # Its record: its shot's, else the round being played or just ended (a closer), else practice.
        on = self.db.execute("SELECT record_uuid FROM shots WHERE id = ?", (shot_id,)).fetchone() if shot_id else None
        record = on[0] if on else self.records.place_line(self.session_id, now) if lines else None
        for l in lines:
            aired = getattr(l, "_aired", None) or (None, None)
            cur = self.db.execute(
                "INSERT INTO lines (session_id, shot_id, ts, kind, speaker, text, excluded, aired_at, aired_seconds,"
                " record_uuid)"
                " VALUES (?1,?2,?3,?4,?5,?6,"
                " COALESCE(CASE WHEN ?2 IS NOT NULL THEN (SELECT excluded FROM shots WHERE id = ?2)"
                " ELSE (SELECT MAX(excluded) FROM rounds WHERE session_id = ?1 AND ended_at > ?3 - ?7 AND ended_at <= ?3) END, 0),"
                " ?8, ?9, ?10)",
                (self.session_id, shot_id, now, kind, l.speaker, l.text, CLOSER_SECONDS, *aired, record))
            try:
                l._line_ids = [*getattr(l, "_line_ids", []), cur.lastrowid]
            except AttributeError:
                pass   # (a line that can't carry them: its air time goes unlogged)
        self.db.commit()

    def mark_aired(self, line: Line, began: float, seconds: float) -> None:
        """Note when a logged line started playing and for how long (the highlight reels' cuts)."""
        ids = getattr(line, "_line_ids", None)
        if not ids:
            return
        self.db.executemany("UPDATE lines SET aired_at = ?, aired_seconds = ? WHERE id = ?",
                            [(began, round(seconds, 2), i) for i in ids])
        self.db.commit()

    def spoken_lines(self, since: float | None = None) -> list[str]:
        """The booth's lines (since a time, when given)."""
        if since is None:
            return [r[0] for r in self.db.execute("SELECT text FROM lines")]
        return [r[0] for r in self.db.execute("SELECT text FROM lines WHERE ts >= ?", (since,))]

