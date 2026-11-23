"""Where a shot finished: filled from the next shot, and backfilled.

Part of Store (commentator/store/core.py), which is these parts together."""
import re
import sqlite3

from .common import log


class FinishesMixin:
    """Where a shot finished: filled from the next shot, and backfilled."""

    _FINISH_COLS = "id, session_id, player, hole, shot_number, is_putt, outcome, mulligan, attributed, " \
                   "to_pin_yards, finish_yards, gimme_yards, fmt"

    def _finish_from_next(self, prev: sqlite3.Row, nxt: sqlite3.Row) -> float | None:
        """Where a full shot finished, read off the same player's next stroke: that stroke's distance to
        the pin, or the gimme radius when it's the conceded tap-in. Only for the very next stroke on the
        same hole (a penalty in between means the next was played from a drop, not where the ball lay)."""
        if prev["is_putt"] or prev["mulligan"] or prev["finish_yards"] is not None or prev["outcome"] == "gimme":
            return None
        # A scramble's next stroke may be from a partner's ball; its conceded putt is the picked ball's own.
        if prev["fmt"] == "scramble" and nxt["outcome"] != "gimme":
            return None
        if not self._same_hole_next(prev, nxt) or nxt["shot_number"] != prev["shot_number"] + 1:
            return None
        if nxt["outcome"] == "gimme":   # its logged length, else inside the radius (None with neither)
            return nxt["to_pin_yards"] or prev["gimme_yards"] or nxt["gimme_yards"]
        return nxt["to_pin_yards"]

    def _fill_previous_finish(self, shot_id: int) -> None:
        """A safety net for the booth's own finish tracking (which needs GSPro's log at the next shot):
        when a stroke is recorded, the same player's previous full shot on the hole finished where this
        one starts."""
        row = self.db.execute(f"SELECT {self._FINISH_COLS} FROM shots WHERE id = ?", (shot_id,)).fetchone()
        if not row or not row["player"] or row["player"] == "unknown" or not row["attributed"]:
            return
        prev = self.db.execute(
            f"SELECT {self._FINISH_COLS} FROM shots WHERE session_id = ? AND player = ? AND attributed = 1"
            " AND id < ? AND mulligan = 0 ORDER BY id DESC LIMIT 1", (row["session_id"], row["player"], shot_id)).fetchone()
        if prev and (finish := self._finish_from_next(prev, row)) is not None:
            self.db.execute("UPDATE shots SET finish_yards = ? WHERE id = ?", (finish, prev["id"]))

    def _backfill_finishes(self) -> None:
        """Fill finishing distances the booth missed on older rows: from the next stroke, or 0 for a
        full shot that holed out. Idempotent; only touches rows that have none. Reads only the sessions
        of the round records changed since it last did (a record's gen moves with any of its rows; it
        notes the one it saw in finishes_gen) and those a shot was deleted from (finish_recheck), not every
        shot each start: all of them the first time, or while any shot has no record."""
        if self.db.in_transaction:
            self.db.commit()
        self.db.execute("BEGIN IMMEDIATE")   # (nothing changes a record between its look and its note)
        try:
            self._backfill_finishes_in()
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def _backfill_finishes_in(self) -> None:
        where, args, stale = "", [], None
        if self.db.execute("SELECT 1 FROM shots WHERE record_uuid IS NULL LIMIT 1").fetchone() is None:
            stale = [u for (u,) in self.db.execute("SELECT uuid FROM round_records WHERE finishes_gen IS NOT gen")]
            deleted = {s for (s,) in self.db.execute("SELECT session_id FROM finish_recheck")}
            if not stale and not deleted:
                return
            if self.db.execute("SELECT 1 FROM round_records WHERE finishes_gen IS NOT NULL LIMIT 1").fetchone():
                sessions = set(deleted)
                for i in range(0, len(stale), 500):
                    chunk = stale[i:i + 500]
                    sessions.update(s for (s,) in self.db.execute(
                        f"SELECT DISTINCT session_id FROM shots WHERE record_uuid IN ({','.join('?' * len(chunk))})", chunk))
                where, args = f" AND session_id IN ({','.join('?' * len(sessions))})", sorted(sessions)
        rows = self.db.execute(f"SELECT {self._FINISH_COLS} FROM shots WHERE attributed = 1 AND player IS NOT NULL"
                               f" AND player != 'unknown'{where} ORDER BY session_id, player, id", args).fetchall()
        # An older row's stale hole-out (its player's next stroke, a gimme aside, played on): not in the cup.
        struck = [r for r in rows if r["outcome"] != "gimme"]
        played_on = {prev["id"] for prev, nxt in zip(struck, struck[1:])
                     if (prev["session_id"], prev["player"]) == (nxt["session_id"], nxt["player"])
                     and self._same_hole_next(prev, nxt)}
        updates = []
        for prev, nxt in zip(rows, rows[1:]):
            if prev["mulligan"] or (prev["session_id"], prev["player"]) != (nxt["session_id"], nxt["player"]):
                continue
            if nxt["mulligan"]:
                continue
            if (finish := self._finish_from_next(prev, nxt)) is not None:
                updates.append((finish, prev["id"]))
        updates += [(0.0, r["id"]) for r in rows if not r["is_putt"] and r["outcome"] == "holed"
                    and r["finish_yards"] is None and not r["mulligan"] and r["id"] not in played_on]
        if updates:
            self.db.executemany("UPDATE shots SET finish_yards = ? WHERE id = ?", updates)
            log.info("Filled %d missing shot finishing distances from the following strokes", len(updates))
        self.db.execute("DELETE FROM finish_recheck")
        if stale is not None:
            for i in range(0, len(stale), 500):
                chunk = stale[i:i + 500]
                self.db.execute(f"UPDATE round_records SET finishes_gen = gen WHERE uuid IN ({','.join('?' * len(chunk))})",
                                chunk)

    def _same_hole_next(self, earlier: sqlite3.Row, later: sqlite3.Row) -> bool:
        """`later` is the same player's next stroke on the same hole: same hole (the number may be read
        late, so 'par 4, 257 yards' matches 'Hole 10, par 4, 257 yards') and a higher stroke number. A
        mulligan's re-hit keeps the stroke number, so it isn't one; with a stroke number unread, it
        can't be told from a replayed hole, so it's left alone."""
        def key(label):
            return re.sub(r"^Hole \d+,\s*", "", label or "")
        if key(earlier["hole"]) != key(later["hole"]):
            return False
        a, b = earlier["shot_number"], later["shot_number"]
        return a is not None and b is not None and b > a

    def _unhole_if_played_on(self, shot_id: int | None, *, later: bool) -> None:
        """A stroke isn't holed if the same player hits again on the same hole after it (a stale
        hole-out read off GSPro's log at the next tee once marked two tee shots holed). Checked both
        ways: when the next stroke is recorded, and when a holed result arrives after it. A putt that
        was played on becomes 'missed'; a full shot loses its outcome."""
        if shot_id is None:
            return
        row = self.db.execute("SELECT id, session_id, player, hole, shot_number, attributed FROM shots WHERE id = ?",
                              (shot_id,)).fetchone()
        if not row or not row["player"] or row["player"] == "unknown" or not row["attributed"]:
            return
        op, order = (">", "ASC") if later else ("<", "DESC")
        other = self.db.execute(
            f"SELECT id, hole, shot_number, is_putt, outcome FROM shots WHERE session_id = ? AND player = ?"
            f" AND attributed = 1 AND outcome IS NOT 'gimme' AND id {op} ? ORDER BY id {order} LIMIT 1",
            (row["session_id"], row["player"], shot_id)).fetchone()
        if not other:
            return
        holed, nxt = (row, other) if later else (other, row)
        if not self._same_hole_next(holed, nxt):
            return
        h = self.db.execute("SELECT id, is_putt, outcome FROM shots WHERE id = ?", (holed["id"],)).fetchone()
        if h["outcome"] != "holed":
            return
        # (a full shot's 0 left was the hole-out's: where it stopped is GSPro's round file's, if it has it)
        tr = self.db.execute("SELECT finish_yards FROM shot_tracks WHERE shot_id = ?", (h["id"],)).fetchone()
        self.db.execute("UPDATE shots SET outcome = ?, finish_yards = CASE WHEN is_putt = 0 AND finish_yards = 0"
                        " THEN ? ELSE finish_yards END WHERE id = ?",
                        ("missed" if h["is_putt"] else None, tr[0] if tr is not None and tr[0] else None, h["id"]))
        log.warning("Shot %s was marked holed but %s hit again on the hole (shot %s): not holed",
                    h["id"], row["player"], nxt["id"])
