"""What the booth notes as it goes: the format, gimmes, the lines spoken, and the carry model.

Part of Store (commentator/store/core.py), which is these parts together."""
import json
import sqlite3
import time

from .common import CARRY_SNAPSHOT_VERSION, CLOSER_SECONDS, _CARRY_COLUMNS, log
from ..models import Line


class SessionMixin:
    """What the booth notes as it goes: the format, gimmes, the lines spoken, and the carry model."""

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
                        self.db.execute("UPDATE shots SET finish_yards = ? WHERE id = ?", (was.get("finish_yards"), i))
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

    def carry_pairs(self) -> list[tuple]:
        """Past full shots for the carry model, oldest first: (lie, base carry yd, game carry yd
        corrected for how high the ball came down, course, launch angle, base). Base is GSPro's CARRY
        (raw) where it was read, else the launch monitor's carry. Shots whose landing height isn't
        known are left out (see correct_for_elevation)."""
        return [pair for _, pair in self._carry_pairs()]

    def carry_model(self, defaults: dict[str, float] | None = None, course_altitude_ft: dict[str, float] | None = None,
                    now: float | None = None):
        """CarryModel(defaults, carry_pairs(), course_altitude_ft), learning from the shots since the
        carry_snapshot only: it keeps what the model had learned before today, saved here again each day."""
        from ..carry_model import CarryModel
        model = CarryModel(defaults, None, course_altitude_ft)
        key = json.dumps([CARRY_SNAPSHOT_VERSION, course_altitude_ft or {}], sort_keys=True)
        t = time.localtime(time.time() if now is None else now)
        midnight = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
        own = not self.db.in_transaction   # (inside the caller's transaction: learn, don't save)
        if own:
            self.db.execute("BEGIN")   # a snapshot saved is of what was read: if another write came since, it isn't
        try:
            snap = self.db.execute("SELECT key, before_ts, state FROM carry_snapshot").fetchone()
            since = None
            if snap is not None and snap["key"] == key:
                model.restore(json.loads(snap["state"]))
                since = snap["before_ts"]
            save, state = own and (since is None or since < midnight), None
            for ts, pair in self._carry_pairs(since):
                if save and state is None and ts >= midnight:
                    state = model.state()
                model.observe(*pair)
            if save:
                try:
                    self.db.execute("DELETE FROM carry_snapshot")
                    self.db.execute("INSERT INTO carry_snapshot (id, key, before_ts, state) VALUES (1, ?, ?, ?)",
                                    (key, midnight, json.dumps(model.state() if state is None else state)))
                except sqlite3.OperationalError as e:   # another connection wrote meanwhile: saved next start
                    log.info("carry model snapshot not saved: %s", e)
                    save = False
        except BaseException:
            if own:
                self.db.rollback()
            raise
        if own:
            (self.db.commit if save else self.db.rollback)()
        return model

    def _carry_pairs(self, since: float | None = None) -> list[tuple[float, tuple]]:
        """carry_pairs, each with its shot's time, from the shots at `since` or later."""
        from ..carry_model import correct_for_elevation
        from ..facts import to_yards

        rows = self.db.execute(
            f"SELECT {_CARRY_COLUMNS} FROM shots WHERE excluded = 0"
            + ("" if since is None else " AND ts >= ?") + " ORDER BY id",
            () if since is None else (since,)).fetchall()
        # Each player's next stroke (by impact time: rows are written as shot handlers finish, which
        # can be out of order; a mulligan was taken back): if it's the very next
        # stroke on the same hole, its pin elevation, less this one's, is where this ball came to rest
        # (the pin doesn't move), if it was played from this ball.
        next_of: dict[int, sqlite3.Row] = {}
        last: dict[tuple, sqlite3.Row] = {}
        rows.sort(key=lambda r: (r["ts"] or 0, r["id"]))
        for r in rows:
            if r["mulligan"] or not r["player"] or r["player"] == "unknown":
                continue
            who = (r["session_id"], r["player"])
            if who in last:
                next_of[last[who]["id"]] = r
            last[who] = r
        def shared(r) -> bool:   # a team format where GSPro copies balls between teammates
            return r["fmt"] == "scramble" or bool(r["shared_ball"])

        out = []
        for r in rows:
            # (a left-out shot still marks where the one before it came to rest, above)
            # (a ball knocked down by a tree carried short of what the lie and elevation explain)
            if r["is_putt"] or r["left_out"] or r["tree_hits"] or not r["lie_before"] \
                    or not (r["carry_game_yards"] or 0) > 0:
                continue
            if r["carry_raw_tile_yards"]:
                base, carry = "gspro", r["carry_raw_tile_yards"]
            elif r["carry_raw"]:
                base = "lm"
                carry = r["carry_raw"] * (1 if (r["units"] or "y").lower().startswith("y") else 1.0936)
            else:
                continue
            elev, rest_rise = r["elevation_yards"], None
            # Either stroke may be the first to show a team format. There, the next stroke is from this
            # ball only if the team picked it: in a scramble the booth records a finish only for the
            # picked ball (the store never fills one in), so a finish says so. Alternate shot: no.
            nxt = next_of.get(r["id"])
            own_ball = nxt is not None and r["attributed"] and nxt["attributed"] and (
                not shared(r) and not shared(nxt) or r["fmt"] == "scramble" and r["finish_yards"] is not None)
            if elev is not None and not r["mulligan"]:
                if own_ball and self._same_hole_next(r, nxt) and nxt["shot_number"] == r["shot_number"] + 1:
                    if nxt["elevation_yards"] is not None:
                        rest_rise = elev - nxt["elevation_yards"]
                elif r["finish_yards"] == 0:
                    rest_rise = elev   # holed: it rested at the pin
            game = correct_for_elevation(r["carry_game_yards"], elev, to_yards(r["to_pin"]), carry, rest_rise)
            if game is not None:
                out.append((r["ts"], (r["lie_before"], carry, game, r["course"], r["launch_angle"], base)))
        return out
