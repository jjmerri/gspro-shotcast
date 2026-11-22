"""Repairs to rows an older version or a missed read left wrong, and finishes filled later.

Part of Store (commentator/store/core.py), which is these parts together."""
import itertools
import json
import math
import re
import sqlite3

from .common import log
from ..sources.gspro_log import M_TO_YD


class RepairsMixin:
    """Repairs to rows an older version or a missed read left wrong, and finishes filled later."""

    HOLE_VISIT_SECONDS = 1800   # a hole's strokes are played well inside this; a later round's aren't

    def _repair_stranded_openers(self) -> None:
        """An opener aired over the load before a round that started a new session (the booth idle for
        hours) stayed in the old session's practice stretch, a record of its own with no shot, so the
        round showed none. Its opener lines from the last PRACTICE_IDLE_SECONDS before the round are the
        round's (round_record.Live.begin now moves them as it starts), and the stretch left empty goes.
        Only openers: other talk with no shot before a round can't be told from practice's."""
        from ..round_record import PRACTICE_IDLE_SECONDS
        for p, sid in self.db.execute(
                "SELECT uuid, session_id FROM round_records p WHERE kind = 'practice' AND state != 'in_progress'"
                " AND NOT EXISTS (SELECT 1 FROM shots WHERE record_uuid = p.uuid)").fetchall():
            last = self.db.execute("SELECT MAX(ts) FROM lines WHERE record_uuid = ? AND kind = 'opener'",
                                   (p,)).fetchone()[0]
            if last is None:
                continue
            r = self.db.execute(
                "SELECT uuid, session_id, started_at FROM round_records WHERE kind = 'round' AND started_at >= ?"
                " AND started_at - ? <= ? AND session_id IS NOT ? ORDER BY started_at LIMIT 1",
                (last, last, PRACTICE_IDLE_SECONDS, sid)).fetchone()
            if not r:
                continue
            n = self.db.execute("UPDATE lines SET record_uuid = ?, session_id = COALESCE(?, session_id)"
                                " WHERE record_uuid = ? AND kind = 'opener' AND shot_id IS NULL AND ts > ?",
                                (r[0], r[1], p, r[2] - PRACTICE_IDLE_SECONDS)).rowcount
            log.info("Moved %d opener line(s) from practice stretch %s into round %s", n, p, r[0])
            if not self.db.execute("SELECT 1 FROM lines WHERE record_uuid = ? LIMIT 1", (p,)).fetchone():
                self.db.execute("DELETE FROM round_slots WHERE round_uuid = ?", (p,))
                self.db.execute("DELETE FROM round_records WHERE uuid = ?", (p,))

    PICKED_UP_OFF_PIN_M = 1.0   # a holed ball's end this far from its round's pin was never in (a logged
                                # pin can sit a few tenths off the cup GSPro drops the ball into)
    PIN_AFTER_ROUND_S = 300     # how long after its round's end the hole's pin may be logged

    def _repair_picked_up_holes(self) -> None:
        """A hole GSPro auto-conceded at the maximum score marks the last stroke dropped in its round file,
        where the ball lay, so it was stored holed with no distance left (Evan's 6-iron from 172 yards at
        Dinosaur Mountain, 48 m short). Its ball stopped where the round file has it: holed no more (a
        putt missed), and a full shot left that far from the hole's pin that round. The pin is the one
        GSPro logged for the hole after the stroke, within its round (not the next one's): its layout, or
        one course_layout.fill_missing stored (stamped with a shot's time) from a pin GSPro logged setting
        the hole up that round, before the stroke. None (a round quit early, a course file's pin) and the shot is left be."""
        rows = self.db.execute(
            "SELECT s.id, s.session_id, s.course, s.hole, s.ts, s.is_putt, r.started_at, r.ended_at,"
            " t.end_x, t.end_y, t.end_z, (SELECT MIN(n.started_at) FROM round_records n WHERE"
            " n.session_id = r.session_id AND n.started_at > r.started_at) AS next_at FROM shots s"
            " JOIN shot_tracks t ON t.shot_id = s.id JOIN round_records r ON r.uuid = s.record_uuid"
            " WHERE s.outcome = 'holed' AND t.end_x IS NOT NULL AND r.ended_at IS NOT NULL").fetchall()
        for r in rows:
            if not (m := re.match(r"Hole (\d+)\b", r["hole"] or "")):
                continue
            until = r["ended_at"] + self.PIN_AFTER_ROUND_S
            if r["next_at"] is not None:
                until = min(until, r["next_at"])
            pin = self.db.execute(
                "SELECT pin_x, pin_y, pin_z FROM hole_layouts h WHERE session_id = ? AND course = ? COLLATE NOCASE"
                " AND hole = ? AND pin_x IS NOT NULL AND ts >= ? AND ts <= ? AND (NOT EXISTS (SELECT 1 FROM shots x"
                " WHERE x.session_id = h.session_id AND x.ts = h.ts) OR EXISTS (SELECT 1 FROM logged_pins p WHERE"
                " p.session_id = h.session_id AND abs(p.x - h.pin_x) < 0.01 AND abs(p.y - h.pin_y) < 0.01"
                " AND abs(p.z - h.pin_z) < 0.01 AND p.ts >= ? AND p.ts <= ?)) ORDER BY ts LIMIT 1",
                (r["session_id"], r["course"], int(m[1]), max(r["ts"] - 60, r["started_at"] or 0), until,
                 (r["started_at"] or 0) - 60, r["ts"])).fetchone()
            if pin is None:
                continue
            off = math.dist((r["end_x"], r["end_y"], r["end_z"]), tuple(pin))
            if off <= self.PICKED_UP_OFF_PIN_M:
                continue
            if r["is_putt"]:   # (a putt keeps no distance left: its miss is what counts)
                self.db.execute("UPDATE shots SET outcome = 'missed', finish_yards = NULL WHERE id = ?", (r["id"],))
                self.db.execute("UPDATE shot_tracks SET finish_yards = NULL WHERE shot_id = ?", (r["id"],))
                log.info("Shot %s: a picked-up hole's last putt, missed", r["id"])
                continue
            left = round(off * M_TO_YD, 1)
            self.db.execute("UPDATE shots SET outcome = NULL, finish_yards = ? WHERE id = ?", (left, r["id"]))
            self.db.execute("UPDATE shot_tracks SET finish_yards = ? WHERE shot_id = ?", (left, r["id"]))
            log.info("Shot %s: a picked-up hole's last stroke, not holed: %.1f yd from the pin", r["id"], left)

    def _repair_tee_finishes(self) -> None:
        """A full shot saved as finishing on the tee kept the lie it was hit from (Corey's opening drive at
        Bethpage, 275 yards). Its real lie is where the next stroke on the hole was played from: the same
        player's, or in a scramble, when this was the ball picked, everyone's next (left alone unless they
        all agree, the player's own among them: another team's may come first). Not mulligans (hit again) or whiffs."""
        rows = self.db.execute(
            "SELECT id, session_id, player, course, hole, shot_number, fmt, picked, ts FROM shots "
            "WHERE lower(lie_after) = 'tee' AND is_putt = 0 AND COALESCE(mulligan, 0) = 0 "
            "AND total_yards >= 10 AND shot_number IS NOT NULL").fetchall()
        for r in rows:
            # The hole visit: until the player starts the hole over (a restart: their count goes back).
            reset = self.db.execute(
                "SELECT MIN(ts) FROM shots WHERE session_id = ? AND course = ? AND hole = ? AND player = ? "
                "AND ts > ? AND shot_number <= ? AND COALESCE(mulligan, 0) = 0",
                (r["session_id"], r["course"], r["hole"], r["player"], r["ts"], r["shot_number"])).fetchone()[0]
            until = min(r["ts"] + self.HOLE_VISIT_SECONDS, reset if reset is not None else float("inf"))
            nxt = self.db.execute(
                "SELECT player, fmt, lie_before FROM shots WHERE session_id = ? AND course = ? AND hole = ? "
                "AND shot_number = ? AND ts > ? AND ts < ? AND COALESCE(mulligan, 0) = 0 ORDER BY ts, id",
                (r["session_id"], r["course"], r["hole"], r["shot_number"] + 1, r["ts"], until)).fetchall()
            if r["fmt"] == "scramble":
                nxt = [n for n in nxt if n["fmt"] == "scramble"]
                lies = {(n["lie_before"] or "").lower() for n in nxt}
                # The picker plays on from their own ball too: without their next stroke (a penalty, a
                # missed shot), another team's could be all there is.
                mine = any(n["player"] == r["player"] for n in nxt)
                nxt = nxt[:1] if r["picked"] and mine and len(lies) == 1 else []
            else:
                nxt = [n for n in nxt if n["player"] == r["player"]][:1]
            if nxt and nxt[0]["lie_before"] and not nxt[0]["lie_before"].lower().startswith("tee"):
                self.db.execute("UPDATE shots SET lie_after = ? WHERE id = ?", (nxt[0]["lie_before"], r["id"]))

    def _repair_misread_first_strokes(self) -> None:
        """A mid-hole stroke whose HUD strip was read "shot 1" was saved off the tee (Dan's lob wedge from
        19.6 yards at Sand Valley's 2nd, the third): the stroke after the player's last on that visit to
        the hole, from where that left them, and the round's phantom penalty (a gap in the strokes) gone.
        Only well short of the hole's length, and only where the next stroke comes after it; after a stroke
        that may have cost a penalty, only numbered from the next stroke. Not in alternate shot, where the
        partner's strokes come between a player's own."""
        rows = self.db.execute("SELECT id, session_id, player, course, hole, shot_number, lie_after, mulligan, "
                               "is_putt, to_pin_yards, ts, fmt FROM shots ORDER BY session_id, player, ts, id").fetchall()

        def visit(seq):
            """The strokes on the same visit to the hole, back to (or on to) a stroke 1: a restart."""
            for o in seq:
                if o["hole"] != r["hole"] or o["course"] != r["course"]:
                    return
                if o["mulligan"] or not o["shot_number"]:
                    continue
                yield o
                if o["shot_number"] == 1:
                    return

        fixes = {}
        for _, group in itertools.groupby(rows, key=lambda x: (x["session_id"], x["player"])):
            group = list(group)
            for i, r in enumerate(group):
                if r["shot_number"] != 1 or r["mulligan"] or r["is_putt"] or r["to_pin_yards"] is None \
                        or r["fmt"] == "alternate":
                    continue
                m = re.search(r"(\d+) yards", r["hole"] or "")
                if not m or r["to_pin_yards"] > 0.75 * int(m.group(1)):
                    continue
                before = list(visit(reversed(group[:i])))
                if not before or (n := max(o["shot_number"] for o in before)) < 2:
                    continue
                after = next(visit(group[i + 1:]), None)
                if after is not None and after["shot_number"] != 1 and after["shot_number"] <= n + 1:
                    continue
                last, num = max(before, key=lambda o: o["ts"]), n + 1
                if last["lie_after"] in (None, "water"):
                    # A penalty may come between (wet, or no finish saved: out of bounds): only the next stroke
                    # tells, while the stroke itself took none. In a scramble too: the drop may be the team's.
                    if after is None or after["shot_number"] == 1 or r["lie_after"] in (None, "water"):
                        continue
                    num = after["shot_number"] - 1
                restart = next((o["ts"] for o in group[i + 1:] if o["hole"] == r["hole"] and o["course"] == r["course"]
                                and not o["mulligan"] and o["shot_number"] == 1), None)
                fixes[r["id"]] = (r, num, last, min(o["ts"] for o in before), restart)
        if not fixes:
            return
        # Every number as saved, before any is changed: two partners' misreads on one hole share a gap.
        holes, updates = {}, []
        for r, n, last, began, restart in fixes.values():
            rnd = self._round_of(r)
            if r["fmt"] == "scramble":
                # The ball played is the team's pick: a teammate's saved lie for the same stroke on this visit
                # to the hole (after the player's stroke 1, before their restart), else unknown (the player's
                # own last finish may not be the ball picked).
                team = rnd[1] if rnd else []
                marks = ",".join("?" * len(team))
                until = min(x for x in (restart, rnd[4] if rnd else None, r["ts"] + 300) if x is not None)
                mates = self.db.execute(
                    f"SELECT DISTINCT lie_before FROM shots WHERE session_id = ? AND course = ? AND hole = ? "
                    f"AND player != ? AND player IN ({marks}) AND shot_number = ? AND mulligan = 0 "
                    f"AND ts > ? AND ts > ? - 300 AND ts < ? "
                    f"AND lie_before IS NOT NULL AND lie_before != 'tee' AND id NOT IN ({','.join('?' * len(fixes))})",
                    (r["session_id"], r["course"], r["hole"], r["player"], *team, n, began, r["ts"], until,
                     *fixes)).fetchall() if team else []
                lie = mates[0][0] if len(mates) == 1 else None
            else:
                lie = last["lie_after"] if last["lie_after"] not in (None, "water") else None
            updates.append((n, lie, r["id"]))
            if rnd:
                holes.setdefault((rnd[0], r["hole"]), (r, rnd))
        self.db.executemany("UPDATE shots SET shot_number = ?, lie_before = CASE WHEN lie_before = 'tee' "
                            "THEN ? ELSE lie_before END WHERE id = ?", updates)
        left: dict[int, int] = {}
        for (rid, _), (r, (_, team, penalties, start, ended)) in holes.items():
            marks = ",".join("?" * len(team))
            got = self.db.execute(
                f"SELECT id, shot_number FROM shots WHERE session_id = ? AND course = ? AND hole = ? AND mulligan = 0 "
                f"AND shot_number IS NOT NULL AND ts > ? AND ts <= ? AND player IN ({marks})",
                (r["session_id"], r["course"], r["hole"], start, ended, *team)).fetchall()
            nums = sorted({x["shot_number"] for x in got})
            was = sorted({1 if x["id"] in fixes else x["shot_number"] for x in got})

            def gaps(seq):
                return sum(b - a - 1 for a, b in zip(seq, seq[1:]) if b - a > 1)
            if (delta := gaps(was) - gaps(nums)) > 0 and (have := left.get(rid, penalties)):
                left[rid] = max(0, have - delta)
        for rid, penalties in left.items():
            self.db.execute("UPDATE rounds SET penalties = ? WHERE id = ?", (penalties, rid))

    def _round_of(self, shot: sqlite3.Row) -> tuple | None:
        """The saved round a shot was played in (id, its side's players, penalties, the window of its
        shots): the session's first round on that course to end after it whose side has the player."""
        for rid, players, side, penalties, ended in self.db.execute(
                "SELECT id, players, side, penalties, ended_at FROM rounds WHERE session_id = ? AND ended_at >= ? "
                "AND lower(course) = lower(?) ORDER BY ended_at", (shot["session_id"], shot["ts"], shot["course"])):
            try:
                team = [p for p in json.loads(players or "[]") if isinstance(p, str)] or [side]
            except ValueError:
                team = [side]
            if shot["player"] not in team:
                continue
            start = self.db.execute("SELECT MAX(ended_at) FROM rounds WHERE session_id = ? AND ended_at < ? - 120",
                                    (shot["session_id"], ended)).fetchone()[0]
            return rid, team, penalties, start if start is not None else float("-inf"), ended
        return None

    def _repair_alternate_finishes(self) -> None:
        """Alternate shot's full shots were given the hole's end (the gimme radius, or 0 for the partner's
        holed putt) as their finish: the player's own next stroke, on the next hole. The team's next
        stroke is where it finished. Shots don't name their team or round, so each saved alternate
        round's own shots are taken: its session, after the round before it ended, its players."""
        rounds = self.db.execute("SELECT session_id, ended_at, players FROM rounds WHERE format = 'alternate' "
                                 "AND players IS NOT NULL").fetchall()
        for session_id, ended_at, players in rounds:
            try:
                team = [p for p in json.loads(players) if isinstance(p, str)]
            except ValueError:
                continue
            if not team:
                continue
            # Each side of a round is saved a moment apart: the round before ended minutes earlier.
            start = self.db.execute("SELECT MAX(ended_at) FROM rounds WHERE session_id = ? AND ended_at < ?",
                                    (session_id, ended_at - 120)).fetchone()[0]
            marks = ",".join("?" * len(team))
            shots = self.db.execute(
                f"SELECT id, hole, shot_number, is_putt, to_pin_yards, finish_yards, outcome FROM shots "
                f"WHERE session_id = ? AND ts > ? AND ts <= ? AND player IN ({marks}) AND fmt = 'alternate' "
                f"AND mulligan = 0 ORDER BY id",
                (session_id, start if start is not None else float("-inf"), ended_at, *team)).fetchall()
            for n, (sid, hole, num, putt, _, finish, outcome) in enumerate(shots):
                if putt or finish is None or num is None or outcome == "holed":
                    continue
                for _, nhole, nnum, _, npin, _, nout in shots[n + 1:]:
                    if nhole != hole:
                        break
                    if nnum == num + 1 and nout != "gimme" and npin is not None:
                        self.db.execute("UPDATE shots SET finish_yards = ? WHERE id = ?", (npin, sid))
                        break

    def drop_finish(self, shot_id: int, yards: float | None, lie: str | None = None,
                    was: str | None = None) -> None:
        """Take back the finish fill_finish wrote (`yards`), and the lie written with it (`lie`, over `was`),
        unless another has replaced them since."""
        if yards is not None:
            self.db.execute("UPDATE shots SET finish_yards = NULL WHERE id = ? AND finish_yards = ?", (shot_id, yards))
        if lie is not None:
            self.db.execute("UPDATE shots SET lie_after = ? WHERE id = ? AND lie_after = ?", (was, shot_id, lie))
        self.db.commit()

    def fill_finish(self, shot_id: int | None, yards: float, replace: bool = False) -> bool:
        """A shared ball's finish from where the team's next stroke started, unless one's already known
        (or over it, with `replace`). Whether it was written."""
        if shot_id is None or self._tracked_finish(shot_id):
            return False
        only_unknown = "" if replace else " AND finish_yards IS NULL"
        cur = self.db.execute(f"UPDATE shots SET finish_yards = ? WHERE id = ?{only_unknown}", (yards, shot_id))
        self.db.commit()
        return cur.rowcount > 0

    def tee_finishes(self, shot_ids: list[int]) -> dict[int, float]:
        """Shot id -> how far from the hole it finished on the green (0 in the hole), for those known."""
        if not shot_ids:
            return {}
        rows = self.db.execute(
            f"""SELECT id, finish_yards, outcome FROM shots WHERE id IN ({",".join("?" * len(shot_ids))})
                AND mulligan = 0 AND ((finish_yards IS NOT NULL AND lie_after = 'green') OR outcome = 'holed')""",
            shot_ids).fetchall()
        return {r["id"]: 0.0 if r["outcome"] == "holed" else r["finish_yards"] for r in rows}

    def fill_tee_finish(self, shot_id: int | None, yards: float) -> None:
        """A tee shot's finish on the green from the HUD at the next stroke, when GSPro's log couldn't
        give one (alternate shot's shared ball has no per-player log index)."""
        if shot_id is None or self._tracked_finish(shot_id):
            return
        self.db.execute("UPDATE shots SET finish_yards = COALESCE(finish_yards, ?), lie_after = 'green' WHERE id = ?",
                        (yards, shot_id))
        self.db.commit()
