"""Recording a shot, and each hole's layout and image.

Part of Store (commentator/store/core.py), which is these parts together."""
import io
import json
import math
import re
import sqlite3
import time

from .common import CLUB_COLS, _club_values, _position_values, log
from ..hud import Hud
from ..models import Shot
from ..sources.gspro_log import M_TO_YD


class ShotsMixin:
    """Recording a shot, and each hole's layout and image."""

    def shot_struck_before(self, at: float, within: float) -> int | None:
        """This session's last shot struck at or before `at` (and no more than `within` seconds
        before), not taken back nor a gimme the booth added: its id."""
        row = self.db.execute("SELECT id FROM shots WHERE session_id = ? AND ts <= ? AND ts >= ? AND mulligan = 0 "
                              "AND outcome IS NOT 'gimme' ORDER BY ts DESC LIMIT 1", (self.session_id, at, at - within)).fetchone()
        return row["id"] if row else None

    def shot_struck_after(self, at: float, within: float) -> int | None:
        """This session's first shot struck after `at` (and within `within` seconds of it), taken back
        or not, but not a gimme the booth added: its id."""
        row = self.db.execute("SELECT id FROM shots WHERE session_id = ? AND ts > ? AND ts <= ? "
                              "AND outcome IS NOT 'gimme' ORDER BY ts LIMIT 1", (self.session_id, at, at + within)).fetchone()
        return row["id"] if row else None

    def shot_brief(self, shot_id: int) -> tuple[sqlite3.Row | None, list[str]]:
        """A shot's row and what the booth said about it (its call), for a word on it later."""
        row = self.db.execute("SELECT ts, player, attributed, hole, shot_number, club, is_putt, lie_before, lie_after, outcome, mulligan "
                              "FROM shots WHERE id = ?", (shot_id,)).fetchone()
        said = [r["text"] for r in self.db.execute("SELECT text FROM lines WHERE shot_id = ? AND kind = 'shot' "
                                                   "ORDER BY id LIMIT 2", (shot_id,))]
        return row, said

    def record_shot(
        self, *, player: str | None, hole: str | None, shot: Shot, before: Hud, after: Hud, tags: list[str],
        carry_game_yards: float | None, total_yards: float | None, offline_yards: float | None,
        course: str | None = None, carry_raw_tile_yards: float | None = None,
        to_pin_yards: float | None = None, finish_yards: float | None = None,
        gimme_yards: float | None = None, outcome: str | None = None, fmt: str | None = None,
        attributed: bool = True, is_league: bool = True, shared_ball: bool = False,
        is_tournament: bool = False, start: tuple | None = None, aim: tuple | None = None, handed: str | None = None,
        tree_hits: int | None = None, record: str | None = None,
    ) -> int:
        record, slot = self.records.place(self.session_id, player, shot.timestamp, course, record)
        cur = self.db.execute(
            """INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, ball_speed,
                   launch_angle, start_direction, total_spin, spin_axis, club_speed, carry_raw, units,
                   lie_before, slope_updown, slope_side, slope_side_degrees, elevation_yards, to_pin,
                   score_to_par, lie_after, carry_raw_tile_yards, carry_game_yards, total_yards, offline_yards,
                   tags, raw, to_pin_yards, finish_yards, gimme_yards, outcome, fmt, attributed, is_league,
                   shared_ball, club_path, angle_of_attack, face_to_target, dynamic_loft, face_impact_h,
                   face_impact_v, start_x, start_y, start_z, aim_map_x, aim_map_y, handed, tree_hits, is_tournament,
                   record_uuid, slot_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                       ?,?,?,?,?,?,?,?,?,?)""",
            (self.session_id, shot.timestamp, player, course, hole, before.shot_number or after.shot_number, shot.club,
             int(shot.is_putt), shot.ball_speed, shot.vla, shot.hla, shot.total_spin, shot.spin_axis,
             shot.club_speed, shot.carry, shot.units, before.lie, before.slope_updown, before.slope_side,
             before.slope_side_degrees, before.elevation_yards, before.to_pin or after.to_pin,
             before.score_to_par or after.score_to_par, after.lie, carry_raw_tile_yards, carry_game_yards,
             total_yards, offline_yards,
             json.dumps(tags), json.dumps(shot.raw),
             to_pin_yards, finish_yards, gimme_yards, outcome, fmt, int(attributed), int(is_league),
             int(shared_ball), *_club_values(shot), *_position_values(start, aim),
             (handed or before.handed or shot.handed or "").strip().upper() or None, tree_hits, int(is_tournament),
             record, slot),
        )
        self._unhole_if_played_on(cur.lastrowid, later=False)
        self._fill_previous_finish(cur.lastrowid)
        self.db.commit()
        return cur.lastrowid

    def club_fields_seen(self, player: str, club: str) -> set[str]:
        """The club columns (club_speed and CLUB_COLS) ever recorded non-zero for this player with this
        club: the fields their monitor measures with it (see club_fields.py). Only their own shots: not
        a shared ball's, which a partner may have hit."""
        cols = ("club_speed", *CLUB_COLS)
        row = self.db.execute(f"SELECT {', '.join(f'MAX({c} != 0)' for c in cols)} FROM shots"
                              " WHERE player = ? AND club = ? AND attributed = 1", (player, club)).fetchone()
        return {c for c, v in zip(cols, row) if v}

    def record_hole_layout(self, course: str | None, holes: list[dict], *, session_id: int | None = -1,
                           ts: float | None = None) -> int:
        """Store the geometry of each played hole in a completed round's layout (the log's hole list,
        see gspro_log._layout_holes; hole numbers are list positions + 1). GSPro logs the list several
        times over, so a repeat of a row this round already stored is skipped (see below). Returns the
        rows added. session_id -1 means this booth's session."""
        if not course:
            return 0
        live = session_id == -1
        sid = self.session_id if live else session_id
        ts = time.time() if live and ts is None else ts
        added = 0
        for n, h in enumerate(holes, 1):
            pin, tee, wp = h.get("pin"), h.get("tee"), h.get("waypoint")
            if not (h.get("played") and pin):
                continue
            # GSPro logs the list several times as a round completes: skip a row already stored for the
            # same round, the same geometry with no stroke on the hole between the two. The same pins
            # and tees played again later in the session are that later round's own layout.
            same = self.db.execute("SELECT ts FROM hole_layouts WHERE session_id IS ? AND course = ? AND hole = ?"
                                   " AND pin_x = ? AND pin_z = ? AND tee_x IS ? AND tee_z IS ?",
                                   (sid, course, n, pin[0], pin[2], *((tee[0], tee[2]) if tee else (None, None))))
            if any(t is None or ts is None or sid is None or not self.db.execute(
                    "SELECT 1 FROM shots WHERE session_id = ? AND course = ? COLLATE NOCASE AND hole LIKE ?"
                    " AND ts > ? AND ts < ? LIMIT 1", (sid, course, f"Hole {n},%", min(t, ts), max(t, ts))).fetchone()
                   for (t,) in same.fetchall()):
                continue
            none3 = (None, None, None)
            self.db.execute("INSERT INTO hole_layouts (session_id, ts, course, hole, par, tee_x, tee_y, tee_z,"
                            " pin_x, pin_y, pin_z, wp_x, wp_y, wp_z) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (sid, ts, course, n, h.get("par"),
                             *(tee or none3), *pin, *(wp or none3)))
            added += 1
        self.db.commit()
        return added

    def record_logged_pin(self, pos: tuple[float, float, float], ts: float | None = None) -> None:
        """Keep a pin GSPro logged as it set a hole up (gspro_log._SCM_PIN), for this session."""
        self.db.execute("INSERT INTO logged_pins (session_id, ts, x, y, z) VALUES (?,?,?,?,?)",
                        (self.session_id, time.time() if ts is None else ts, *pos))
        self.db.commit()

    def fill_log_totals(self, session_id: int | None = -1) -> int:
        """Fill in a full shot's total from GSPro's logged positions when its TOTAL LENGTH tile was never
        read (hidden for the shot by GSPro's "hide UI on shot"): the flat distance from where it was played
        to where the same player's next stroke on the hole was played from. A tee shot starts on the tee
        of this round's layout (logged when the round is complete; an earlier round's tee box may differ).
        Not for a stroke followed by a penalty (the next stroke number is skipped), a ball teammates share,
        a putt, or a pairing whose distance doesn't fit the carry. Totals filled this way are worked out
        again each time (a mulligan or a corrected position can change the pairing); the HUD's never are.
        Returns the shots whose total changed."""
        sid = self.session_id if session_id == -1 else session_id
        rows = self.db.execute(
            "SELECT id, ts, player, course, hole, shot_number, is_putt, mulligan, shared_ball, attributed, fmt,"
            " start_x, start_z, total_yards, total_from_log, COALESCE(carry_game_yards, carry_raw_tile_yards, carry_raw) AS carry"
            " FROM shots WHERE session_id IS ? ORDER BY ts, id", (sid,)).fetchall()
        ends = [e for (e,) in self.db.execute("SELECT ended_at FROM rounds WHERE session_id IS ? AND ended_at IS NOT NULL",
                                               (sid,))]
        mine: dict[tuple, list] = {}
        for r in rows:
            if r["player"] and r["player"] != "unknown" and not r["mulligan"]:
                mine.setdefault((r["player"], r["course"], r["hole"]), []).append(r)
        want: dict[int, float] = {}
        for (_, course, hole), strokes in mine.items():
            for r, nxt in zip(strokes, strokes[1:]):
                if ((r["total_yards"] is not None and r["total_from_log"] != 1) or r["is_putt"] or r["shared_ball"] or not r["attributed"]
                        or r["fmt"] in ("scramble", "alternate") or not r["carry"] or nxt["start_x"] is None
                        or not r["shot_number"] or nxt["shot_number"] != r["shot_number"] + 1
                        or nxt["ts"] - r["ts"] > 1800 or any(r["ts"] < e < nxt["ts"] for e in ends)):
                    continue   # (the hole played again in a later round isn't this ball)
                start = (r["start_x"], r["start_z"]) if r["start_x"] is not None and r["shot_number"] > 1 else None
                if start is None and r["shot_number"] == 1 and (m := re.match(r"Hole (\d+)", hole or "")):
                    tee = self.db.execute(
                        "SELECT tee_x, tee_z FROM hole_layouts WHERE session_id IS ? AND course = ? COLLATE NOCASE"
                        " AND hole = ? AND ts >= ? AND tee_x IS NOT NULL ORDER BY ts LIMIT 1",
                        (sid, course, int(m.group(1)), nxt["ts"])).fetchone()
                    if tee is not None and self.db.execute(   # the hole teed off again first: another round's
                            "SELECT 1 FROM shots WHERE session_id IS ? AND course = ? COLLATE NOCASE AND (hole = ? OR hole LIKE ?)"
                            " AND shot_number = 1 AND ts > ? AND ts < (SELECT MIN(ts) FROM hole_layouts WHERE"
                            " session_id IS ? AND course = ? COLLATE NOCASE AND hole = ? AND ts >= ? AND tee_x IS NOT NULL)"
                            " LIMIT 1", (sid, course, f"Hole {m.group(1)}", f"Hole {m.group(1)},%", nxt["ts"], sid, course, int(m.group(1)), nxt["ts"])).fetchone():
                        tee = None
                    start = (tee["tee_x"], tee["tee_z"]) if tee else None
                if start is None:
                    continue
                total = math.hypot(nxt["start_x"] - start[0], nxt["start_z"] - start[1]) * M_TO_YD
                if not 0.6 * r["carry"] <= total <= 1.6 * r["carry"] + 40:
                    continue   # not this ball's rest (a mulligan the log didn't flag, a misattributed stroke)
                want[r["id"]] = round(total, 1)
        filled = 0
        for r in rows:
            if (r["total_yards"] is None or r["total_from_log"] == 1) and want.get(r["id"]) != r["total_yards"]:
                self.db.execute("UPDATE shots SET total_yards = ?, total_from_log = ? WHERE id = ?"
                                " AND (total_yards IS NULL OR total_from_log = 1)",
                                (want.get(r["id"]), int(r["id"] in want), r["id"]))
                filled += 1
        if filled:
            self.db.commit()
        return filled

    def has_hole_image(self, course: str | None, hole: int | None) -> bool:
        """Whether the hole's minimap is kept already, with its ball and pin found."""
        return self.db.execute("SELECT 1 FROM hole_images WHERE course = ? COLLATE NOCASE AND hole = ?"
                               " AND ball_x IS NOT NULL AND pin_x IS NOT NULL", (course, hole)).fetchone() is not None

    def record_hole_image(self, course: str | None, hole: int | None, image: bytes,
                          ball: tuple[float, float] | None, pin: tuple[float, float] | None, *,
                          session_id: int | None = -1, ts: float | None = None, replace: bool = False) -> int | None:
        """Store a hole's minimap snapshot (minimap.snapshot). One per course and hole, for every round
        there: a later one replaces it only when it found the ball and pin and the stored one didn't, or
        with `replace`. session_id -1 means this booth's session. Returns the row id, or None when
        nothing was stored."""
        if not course or not hole:
            return None
        sid = self.session_id if session_id == -1 else session_id
        ts = time.time() if ts is None else ts
        old = self.db.execute("SELECT id, ball_x, pin_x FROM hole_images WHERE course = ? COLLATE NOCASE"
                              " AND hole = ?", (course, hole)).fetchone()
        found = ball is not None and pin is not None
        if old is not None and not replace and (old["ball_x"] is not None and old["pin_x"] is not None or not found):
            return None
        self.db.execute("DELETE FROM hole_images WHERE course = ? COLLATE NOCASE AND hole = ?", (course, hole))
        b, p = (ball, pin) if found else ((None, None), (None, None))
        cur = self.db.execute("INSERT INTO hole_images (session_id, ts, course, hole, image, ball_x, ball_y,"
                              " pin_x, pin_y) VALUES (?,?,?,?,?,?,?,?,?)", (sid, ts, course, hole, image, *b, *p))
        self.db.commit()
        return cur.lastrowid

    def _locate_hole_images(self) -> None:
        """Look again for the ball and pin on stored minimaps that have none: the search once knew only a
        bright red ball, and a map without them backs no replay. Only touches rows that have none."""
        rows = self.db.execute("SELECT id, image FROM hole_images WHERE ball_x IS NULL OR pin_x IS NULL").fetchall()
        if not rows:
            return
        from PIL import Image

        from ..minimap import locate

        found = 0
        for r in rows:
            try:
                spots = locate(Image.open(io.BytesIO(r["image"])))
            except Exception:   # a damaged image stays as it is
                log.debug("Couldn't read hole image %s", r["id"], exc_info=True)
                continue
            if spots:
                found += 1
                self.db.execute("UPDATE hole_images SET ball_x = ?, ball_y = ?, pin_x = ?, pin_y = ? WHERE id = ?",
                                (*spots[0], *spots[1], r["id"]))
        if found:
            self.db.commit()
            log.info("Found the ball and pin on %d stored hole minimaps", found)
