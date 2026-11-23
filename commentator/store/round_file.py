"""Applying GSPro's round file to the recorded shots.

Part of Store (commentator/store/core.py), which is these parts together."""
import bisect
import json
import math
import re
import sqlite3

from .common import CLUB_COLS, log
from .. import round_file
from ..models import PUTTER_CODES


class RoundFileMixin:
    """Applying GSPro's round file to the recorded shots."""

    def apply_round_file(self, tracks: list, *, now: float, window_s: float = 12 * 3600,
                         same_player=None, rewrite: bool = True, gone: set | frozenset = frozenset(),
                         name_of=None, pinned: dict | None = None, rounds: dict | None = None,
                         folders: dict | None = None) -> tuple[int, list]:
        """Apply GSPro's round file (round_file.tracks) to the shots it records: each stroke is matched to
        the row with its hole, stroke number and ball speed and a player of its name (`same_player`, a
        (gspro_name, player) test, for names that differ more than "Josh" / "Josh Merritt"), recorded
        within `window_s` of `now`. GSPro's start, carry, total, lies and finish replace the booth's; a lie
        the HUD read finer than GSPro's codes stays. Idempotent. Returns (shots written, tracks not
        matched yet: a row is recorded only once the booth has the shot). `now` is when the file was
        written: a stroke's row is from before then, or just after (the booth can record it late).
        `rewrite` False leaves a stroke already applied to a shot it still fits as it is (the same file
        applied again, for shots recorded since). `gone`: the file's ShotIDs that are no longer strokes
        (round_file.untracked: a mulligan took one back), whose shots get back what they had.
        `name_of` (GSPro's name -> the player to store) also names the strokes the booth recorded blind
        (no player read off the screen): see _fill_blind. `pinned` (ShotID -> shot id): strokes the live
        follow already tied to their shot, which is a candidate whatever its stroke number (the booth's
        count can carry a penalty GSPro's leaves out). `rounds` (a round record's uuid -> GSPro's RoundIDs
        of the strokes the live follow tied to its shots): a stroke of another GSPro round isn't its.
        `folders` (a round record's uuid -> its GSPro course folder, when the booth is sure of it): a shot
        of such a round is a stroke's only when the stroke's course key is that folder, whatever other
        course the key's name is like. They're kept (round_folders), so a file read again before the
        round is (a restart) still goes by them; a folder of None forgets the round's (GSPro found another
        course's files in it: Wentworth by the Sea's in balsams_gsp, keyed by Wentworth's own name)."""
        courses = self.courses_played()
        rounds = rounds or {}

        def round_ok(t, row) -> bool:
            ids = rounds.get(row["record_uuid"])
            return not ids or t.round_id is None or t.round_id in ids
        ends: list[float] = []   # each play's end: its sides' rounds end within 120 s of each other
        # (only those from the window on: a stroke's row is in it, and its play ends after it)
        for (e,) in self.db.execute("SELECT DISTINCT ended_at FROM rounds WHERE ended_at >= ? ORDER BY ended_at",
                                    (now - window_s,)):
            if ends and e - ends[-1] <= 120:
                ends[-1] = e
            else:
                ends.append(e)
        known: dict = {}
        if folders:
            self.db.executemany("INSERT OR REPLACE INTO round_folders VALUES (?, ?)",
                                [(u, f) for u, f in folders.items() if f])
            self.db.executemany("DELETE FROM round_folders WHERE record_uuid = ?",
                                [(u,) for u, f in folders.items() if not f])
            self.db.commit()
        folders = {**dict(self.db.execute("SELECT record_uuid, folder FROM round_folders").fetchall()),
                   **{u: f for u, f in (folders or {}).items() if f}}

        def folder_of(row) -> str | None:
            return folders.get(row["record_uuid"]) if row["record_uuid"] else None

        def course_ok(key, row) -> bool:
            # The shot's round's folder, when sure of it; else the shot's course, when GSPro's key names
            # one we have; else (a key not like any course name) a shot from this session, the round in play.
            if key and (folder := folder_of(row)):
                return round_file.folder_is(key, folder)
            if key not in known:
                known[key] = any(round_file.course_fits(key, c) for c in courses)
            if known[key]:
                return round_file.course_named(key, row["course"], courses)
            return row["session_id"] == self.session_id

        def course_wrong(key, row) -> bool:   # GSPro's key names a course we have, not the shot's
            if folder := folder_of(row):
                return bool(key) and not round_file.folder_is(key, folder)
            return bool(row["course"]) and not course_ok(key, row) and known[key]

        def round_of(row) -> float:   # the end of the play the shot was in (inf: still in play)
            i = bisect.bisect_left(ends, row["ts"])
            return ends[i] if i < len(ends) else math.inf

        pinned = pinned or {}
        claimed = dict(self.db.execute("SELECT gspro_shot_id, shot_id FROM shot_tracks").fetchall())

        def candidates(t) -> list:
            # Its stroke number on its hole; or the shot the follow pinned it to, or it was already
            # matched to (a pin from before a restart), whatever the booth numbered it.
            rows = self.db.execute(
                "SELECT * FROM shots WHERE ts >= ? AND ts <= ?"
                " AND ((shot_number = ? AND (hole = ? OR hole LIKE ?)) OR id = ? OR id = ?)"
                " AND ball_speed IS NOT NULL AND mulligan = 0 AND outcome IS NOT 'gimme'",
                (now - window_s, now + 600, t.shot_number, f"Hole {t.hole}", f"Hole {t.hole},%",
                 pinned.get(t.shot_id, -1), claimed.get(t.shot_id, -1))).fetchall()
            # (its course was checked when tied: but one known since to be another's isn't)
            tied = (pinned.get(t.shot_id), claimed.get(t.shot_id))
            return [r for r in rows if (r["id"] in tied and not course_wrong(t.course_key, r)
                                        or course_ok(t.course_key, r)) and round_ok(t, r)
                    and abs(r["ball_speed"] - t.ball_speed) <= max(0.6, 0.01 * t.ball_speed)
                    and (r["id"] == pinned.get(t.shot_id) or round_file.names_fit(t.player, r["player"])
                         or (same_player is not None and same_player(t.player, r["player"])))]

        # The file is one round's: the round of the shots the live follow pinned to it, else the latest
        # round with shots it could be (a round GSPro has started over since can't be). A stroke is
        # matched only within it.
        cands = {t.shot_id: candidates(t) for t in tracks}
        rows_by_id = {r["id"]: r for rs in cands.values() for r in rs}
        # A pin is the live follow's own match: it wins over one by stroke number and speed.
        pin_of = {sid: gid for gid, sid in pinned.items()
                  if gid in cands and any(r["id"] == sid for r in cands[gid])}
        latest = max((rows_by_id[sid] for sid in pin_of), key=lambda r: r["ts"], default=None) \
            or max(rows_by_id.values(), key=lambda r: r["ts"], default=None)
        this_round = None if latest is None else round_of(latest)
        changed = False
        for gid in gone & claimed.keys():
            self._release_track(claimed.pop(gid))
            changed = True
        for t in tracks:
            sid = claimed.get(t.shot_id)
            if sid is not None and (sid not in rows_by_id or round_of(rows_by_id[sid]) != this_round
                                    or all(r["id"] != sid for r in cands[t.shot_id])
                                    or pin_of.get(sid, t.shot_id) != t.shot_id
                                    or (t.shot_id in pinned and pin_of.get(pinned[t.shot_id]) == t.shot_id
                                        and pinned[t.shot_id] != sid)):
                self._release_track(sid)
                del claimed[t.shot_id]
                changed = True
        # A pinned shot another file's stroke was matched to (an earlier round's, GSPro started over on the
        # same course): the live follow's pin is the stroke it is.
        here = {t.shot_id for t in tracks}
        held = {sid: gid for gid, sid in claimed.items()}
        for sid, gid in pin_of.items():
            if (other := held.get(sid)) is not None and other != gid and other not in here:
                self._release_track(sid)
                del claimed[other]
                changed = True
        taken = set(self.db.execute("SELECT shot_id FROM shot_tracks").fetchall())
        taken = {r[0] for r in taken}
        written, pending = 0, []
        matched: dict[int, str] = {}   # shot id -> the file's stroke it was matched to
        for t in tracks:
            row = rows_by_id.get(claimed[t.shot_id]) if t.shot_id in claimed else None
            if row is not None and not rewrite:
                taken.add(row["id"])
                matched[row["id"]] = t.shot_id
                continue
            if row is None:
                fits = [r for r in cands[t.shot_id] if r["id"] not in taken and round_of(r) == this_round
                        and pin_of.get(r["id"], t.shot_id) == t.shot_id]
                pin = [r for r in fits if pin_of.get(r["id"]) == t.shot_id]
                row = min(pin or fits, key=lambda r: (abs(r["ball_speed"] - t.ball_speed), -r["ts"]), default=None)
            if row is None:
                pending.append(t)
                continue
            taken.add(row["id"])
            matched[row["id"]] = t.shot_id
            if changed:   # a release above may have put its columns back since it was read
                row = self.db.execute("SELECT * FROM shots WHERE id = ?", (row["id"],)).fetchone()
            # A pinned shot the booth recorded blind takes its player from the file (as _fill_blind).
            blind = pin_of.get(row["id"]) == t.shot_id and name_of is not None \
                and (row["player"] is None or row["player"] == "unknown")
            self._apply_track(row, t, self._blind_named(row, t, name_of, self._screen_club(row)) if blind else None)
            written += 1
        if name_of is not None and pending:
            filled = self._fill_blind(tracks, now=now, window_s=window_s, taken=taken, matched=matched,
                                      name_of=name_of, course_ok=course_ok, round_ok=round_ok, folder_of=folder_of,
                                      in_round=lambda r: this_round is None or round_of(r) == this_round,
                                      round_of=lambda r: (round_of(r), r["record_uuid"]))
            if filled:
                pending = [t for t in pending if t.shot_id not in filled]
                written += len(filled)
        if written or changed:
            self.db.commit()
        return written, pending

    _TRACKED = ("start_x", "start_y", "start_z", "lie_before", "lie_after", "carry_game_yards", "total_yards",
                "total_from_log", "finish_yards", "club")
    _NAMED = ("player", "hole", "shot_number", "is_putt", "attributed")   # what _fill_blind sets too
    BLIND_RUN = 3   # strokes in a row whose ball speeds line up before blind shots are named from the file

    def _fill_blind(self, tracks: list, *, now: float, window_s: float, taken: set, matched: dict,
                    name_of, course_ok, in_round=lambda r: True, round_of=lambda r: 0,
                    round_ok=lambda t, r: True, folder_of=lambda r: None) -> set[str]:
        """Name the shots the booth recorded blind (no player read: the screen covered, the HUD unread)
        from GSPro's round file, which has every stroke's player. With no name or stroke number to go
        on, a stroke is lined up with the shots by order: the file's strokes and the booth's shots, back
        from the latest, must agree on ball speed, BLIND_RUN or more in a row; a shot already matched to
        a stroke must be that stroke. The run stops at the first that doesn't fit (a shot the booth
        missed), so nothing before it is guessed. Returns the ShotIDs of the strokes it named."""
        rows = self.db.execute(
            "SELECT * FROM shots WHERE ts >= ? AND ts <= ? AND ball_speed IS NOT NULL AND mulligan = 0"
            " AND outcome IS NOT 'gimme' ORDER BY ts, id", (now - window_s, now + 120)).fetchall()
        rows = [r for r in rows if in_round(r)]   # (the file's round only)

        def blind(row) -> bool:
            return row["player"] is None or row["player"] == "unknown"
        if not tracks or not any(blind(r) and r["id"] not in taken for r in rows):
            return set()
        row_of = {g: i for i, g in matched.items()}

        def fits(t, row) -> bool:
            if abs(row["ball_speed"] - t.ball_speed) > max(0.6, 0.01 * t.ball_speed):
                return False
            if row["id"] in taken or t.shot_id in row_of:
                return matched.get(row["id"]) == t.shot_id
            # (a shot of a round whose folder is known is checked by it, course read or not)
            return (blind(row) and ((row["course"] is None and not folder_of(row)) or course_ok(t.course_key, row))
                    and round_ok(t, row))

        def run(i: int, j: int) -> list:
            out = []
            play = round_of(rows[j]) if j >= 0 else None   # a run never spans a round's end
            while i >= 0 and j >= 0 and round_of(rows[j]) == play and fits(tracks[i], rows[j]):
                out.append((tracks[i], rows[j]))
                i, j = i - 1, j - 1
            return out
        # The latest strokes may not be recorded yet, nor the latest shots in the file yet.
        best = max((run(len(tracks) - 1 - a, len(rows) - 1 - b) for a in range(3) for b in range(3)), key=len)
        if len(best) < self.BLIND_RUN:
            return set()
        filled = set()
        for t, row in reversed(best):   # (oldest first, as recorded: _unmeasured learns from each)
            if row["id"] in taken:
                continue
            self._apply_track(row, t, self._blind_named(row, t, name_of, self._screen_club(row)))
            taken.add(row["id"])
            filled.add(t.shot_id)
        if filled:
            log.info("GSPro's round file named %d shots the booth recorded without a player", len(filled))
        return filled

    def screen_putt(self, shot_id: int | None) -> bool | None:
        """Putt or not as the shot is once the club GSPro's round file filled (club_fix) is taken back
        (GSPro's call at impact, else as it was before); None when it hasn't filled one."""
        row = None if shot_id is None else self.db.execute("SELECT * FROM shots WHERE id = ?", (shot_id,)).fetchone()
        if row is None or row["club"] == self._screen_club(row):
            return None
        tr = self._track(shot_id)
        was = json.loads(tr["was"]) if tr is not None and tr["was"] else {}
        return bool(was.get("ended_putt", was.get("is_putt", row["is_putt"])))

    def _unmeasured(self, row: sqlite3.Row, club: str, player: str | None) -> list[str]:
        """The club columns a shot recorded without a club has that `player`'s monitor doesn't measure
        with `club`, as club_fields.apply has them (which a shot with no club skips): zeros it wrote for
        them, not measurements. None with no player (a shot not attributed: a shared ball whose name
        isn't who hit it)."""
        if not player or player == "unknown":
            return []
        # (a shot without club data has no delivery fields: Shot.club_measured)
        fields = ("club_speed", *CLUB_COLS) if any(row[c] is not None for c in CLUB_COLS) else ("club_speed",)
        known = self.club_fields_seen(player, club)
        if known and not any(row[c] for c in known.intersection(fields)):
            return [c for c in fields if row[c] is not None]   # every field known for the club read 0: a misread
        known |= {c for c in fields if row[c]}
        return [c for c in fields if c not in known and row[c] is not None]

    def _screen_club(self, row: sqlite3.Row) -> str | None:
        """The club the booth read off the screen for a shot: not one an earlier read of GSPro's round file
        set (club_fix), which the snapshot of what the file replaced keeps."""
        tr = self._track(row["id"])
        was = json.loads(tr["was"]) if tr is not None and tr["was"] else {}
        return was["club"] if "club" in was else row["club"]

    @staticmethod
    def _blind_named(row: sqlite3.Row, t, name_of, screen_club: str | None = None) -> dict:
        """What a stroke in GSPro's round file names on a shot the booth recorded blind (its own label for
        the hole kept, when it's that hole: stats group a hole's shots by it). `screen_club`: the club the
        booth read for it (_screen_club)."""
        same = row["hole"] and re.match(rf"Hole {t.hole}\b", row["hole"])
        # (the club, where the screen read none, is GSPro's, which says whether it's a putt: club_fix)
        club = round_file.club_fix(t, screen_club)
        return {"player": name_of(t.player) or t.player,
                "hole": row["hole"] if same else f"Hole {t.hole}" + (f", par {t.par}" if t.par else ""),
                "shot_number": t.shot_number,
                "is_putt": int(club in PUTTER_CODES) if club else int(row["is_putt"]) if t.putt is None else int(t.putt),
                "attributed": 1}

    def _release_track(self, shot_id: int) -> None:
        """A stroke matched to the wrong shot: what it set goes back to what the shot had."""
        tr = self._track(shot_id)
        if tr is not None and tr["was"]:
            was = json.loads(tr["was"])
            cols = [c for c in self._TRACKED + self._NAMED + ("club_speed", *CLUB_COLS) if c in was]
            self.db.execute(f"UPDATE shots SET {', '.join(f'{c} = ?' for c in cols)} WHERE id = ?",
                            (*(was[c] for c in cols), shot_id))
            if "outcome" in was and was["outcome"] is None:   # (a concession the file's putt call gave it)
                self.db.execute("UPDATE shots SET outcome = NULL WHERE id = ? AND outcome = 'conceded'", (shot_id,))
            elif "outcome" in was:   # (a miss or concession the file's full-shot call took off)
                self.db.execute("UPDATE shots SET outcome = ? WHERE id = ? AND outcome IS NULL",
                                (was["outcome"], shot_id))
            if "is_putt" in was and not was["is_putt"]:   # (a putt only by the file's say: its miss goes too)
                self.db.execute("UPDATE shots SET outcome = NULL WHERE id = ? AND outcome = 'missed'", (shot_id,))
            self.db.execute("UPDATE shots SET finish_yards = 0 WHERE id = ? AND outcome = 'holed' AND is_putt = 0",
                            (shot_id,))   # (a chip-in still holed finished in the cup, whatever it had before)
            if any(c in was for c in self._NAMED):
                self._sync_gimme(shot_id)
            # (a concession graded meanwhile follows the putt or not put back - a full shot's is its gimme's -
            # over a miss the snapshot had)
            p = self.db.execute("SELECT uid, is_putt, outcome FROM shots WHERE id = ?", (shot_id,)).fetchone()
            if p is not None and not p["is_putt"] and p["outcome"] == "conceded":
                self.db.execute("UPDATE shots SET outcome = NULL WHERE id = ?", (shot_id,))
            elif p is not None and p["is_putt"] and p["outcome"] in (None, "missed") and p["uid"] is not None \
                    and self.db.execute("SELECT 1 FROM shots WHERE derived_from = ? AND outcome = 'gimme'",
                                        (p["uid"],)).fetchone():
                self.db.execute("UPDATE shots SET outcome = 'conceded' WHERE id = ?", (shot_id,))
        self.db.execute("DELETE FROM shot_tracks WHERE shot_id = ?", (shot_id,))

    def _sync_gimme(self, shot_id: int) -> None:
        """The conceded putt a shot left (record_gimme) follows it when the round file names it or takes
        the name back: same player, hole and attribution, the stroke after it. And both shots' slots."""
        p = self.db.execute("SELECT uid, player, hole, shot_number, attributed FROM shots WHERE id = ?",
                            (shot_id,)).fetchone()
        if p is not None and p["uid"] is not None:
            self.db.execute("UPDATE shots SET player = ?, hole = ?, shot_number = ?, attributed = ?"
                            " WHERE derived_from = ? AND outcome = 'gimme'",
                            (p["player"], p["hole"], None if p["shot_number"] is None else p["shot_number"] + 1,
                             p["attributed"], p["uid"]))
        # Each goes to its player's slot in its round's record (none, with the name taken back): the
        # record and the cloud upload say whose a shot is by its slot alone.
        self.records.reslot([shot_id] + ([r[0] for r in self.db.execute(
            "SELECT id FROM shots WHERE derived_from = ? AND outcome = 'gimme'", (p["uid"],))] if p and p["uid"] else []))

    def _apply_track(self, row: sqlite3.Row, t, named: dict | None = None) -> None:
        def lie(old, new):   # GSPro's lie
            return new or old
        carry, total = t.carry_yards, t.total_yards
        # A scramble keeps a finish only for the ball the team picked (the booth's): see set_format.
        shared = row["fmt"] == "scramble" or bool(row["shared_ball"])
        old = self._track(row["id"])
        was = old["was"] if old is not None else json.dumps({c: row[c] for c in self._TRACKED + tuple(named or ())})
        # A putt is GSPro's putter (or not) when its club fills one the screen read none for (club_fix),
        # unless the file's naming the shot said. One an earlier read set goes back to the screen's once
        # the file can't say (the hole ended on the stroke since), or never could (a club the booth's
        # earlier versions took from the file over the screen's), with the putt or not it changed.
        before = json.loads(was) if was else {}
        screen = self._screen_club(row)
        club = round_file.club_fix(t, screen)
        unfill = club is None and row["club"] != screen
        club = None if club == row["club"] else club
        fix_putt = club is not None and "is_putt" not in (named or {}) and int(club in PUTTER_CODES) != row["is_putt"]
        unfill_putt = unfill and "is_putt" not in (named or {})
        # GSPro's putt or not at impact decides a shot with no club, read or filled (the screen's guess
        # from speed and launch kept in the snapshot to go back to).
        call = not named and club is None and not unfill and not row["club"] and t.putt is not None \
            and int(t.putt) != row["is_putt"]
        # (a snapshot without the putt it changed, from an earlier version: GSPro's own call at impact,
        # or the screen's putter)
        putt = (named or {}).get("is_putt", int(club in PUTTER_CODES) if fix_putt
                                 else int(t.putt) if unfill_putt and not screen and t.putt is not None
                                 else before.get("is_putt", int(bool(t.putt) or screen in PUTTER_CODES))
                                 if unfill_putt else int(t.putt) if call else row["is_putt"])
        finish = t.finish_yards if not (putt or shared) else None
        if finish is None and t.picked_up and old is not None and not (putt or shared) and old["finish_yards"]:
            finish = old["finish_yards"]   # (a pick-up's own DistanceToPin is stale: an earlier read's stands,
                                           # not the 0 an older version took it as holed with)
        lie_after = lie(row["lie_after"], t.lie_after)
        # What an earlier read of the file set and this one takes back (a penalty since followed the
        # stroke): where it stopped isn't where play went on.
        if t.end is None and row["total_yards"] is not None:   # a penalty followed: no resting total, whoever read one
            self.db.execute("UPDATE shots SET total_yards = NULL, total_from_log = 0 WHERE id = ?", (row["id"],))
        if old is not None:
            if old["total_yards"] is not None and total is None:
                self.db.execute("UPDATE shots SET total_yards = NULL, total_from_log = 0 WHERE id = ?"
                                " AND total_from_log = 2", (row["id"],))
            if old["finish_yards"] is not None and finish is None:
                self.db.execute("UPDATE shots SET finish_yards = NULL WHERE id = ? AND finish_yards IS ?",
                                (row["id"], old["finish_yards"]))
            if old["lie_after"] is not None and t.lie_after is None and row["lie_after"] == old["lie_after"]:
                lie_after = None
        self.db.execute(
            "UPDATE shots SET start_x = ?, start_y = ?, start_z = ?, lie_before = ?, lie_after = ?,"
            " carry_game_yards = COALESCE(?, carry_game_yards), total_yards = COALESCE(?, total_yards),"
            " total_from_log = CASE WHEN ? IS NULL THEN total_from_log ELSE 2 END,"
            " finish_yards = CASE WHEN outcome = 'holed' AND ? = 0 THEN 0 ELSE COALESCE(?, finish_yards) END"
            " WHERE id = ?",   # (a chip-in's is in the cup, whatever the file's rest read: shot_tracks keeps that)
            (*t.start, lie(row["lie_before"], t.lie_before), lie_after, carry, total, total, int(bool(putt)), finish,
             row["id"]))
        if named:
            self.db.execute(f"UPDATE shots SET {', '.join(f'{c} = ?' for c in named)} WHERE id = ?",
                            (*named.values(), row["id"]))
            self._sync_gimme(row["id"])
        if club is not None:
            # What it replaces goes in the snapshot (one from before clubs were kept has no club either).
            putter = int(club in PUTTER_CODES)
            w = json.loads(was)
            # (whose swing it was: an attributed shot's player, or the one the file names a blind shot for,
            # unless the ball was shared some other way than a scramble's, where each swing is its player's)
            own = row["attributed"] or (named and not (row["shared_ball"] and row["fmt"] != "scramble"))
            who = (named or {}).get("player", row["player"]) if own else None
            zeros = self._unmeasured(row, club, who) if not row["club"] else []
            # (putt or not once the fill is taken back, as the unfill has it: screen_putt)
            ended = int(t.putt) if t.putt is not None else w.get("is_putt", row["is_putt"])
            was = json.dumps({"club": row["club"], "is_putt": row["is_putt"], **{c: row[c] for c in zeros},
                              "ended_putt": ended, **w})
            log.info("Shot %d club %s -> %s from GSPro's round file", row["id"], row["club"] or "unread", club)
            self.db.execute("UPDATE shots SET club = ?, is_putt = CASE WHEN ? THEN ? ELSE is_putt END"
                            f"{''.join(f', {c} = NULL' for c in zeros)} WHERE id = ?",
                            (club, fix_putt, putter, row["id"]))
        elif call:
            log.info("Shot %d %s from GSPro's round file", row["id"], "a putt" if t.putt else "a full shot")
            was = json.dumps({"is_putt": row["is_putt"], **json.loads(was)})
            self.db.execute("UPDATE shots SET is_putt = ? WHERE id = ?", (int(t.putt), row["id"]))
        elif unfill:
            log.info("Shot %d club %s -> %s: back to the screen's", row["id"], row["club"], screen or "unread")
            back = [c for c in ("club_speed", *CLUB_COLS) if c in before]   # (the zeros the fill took off)
            self.db.execute(f"UPDATE shots SET club = ?, is_putt = ?{''.join(f', {c} = ?' for c in back)} WHERE id = ?",
                            (screen, putt, *(before[c] for c in back), row["id"]))
        if row["is_putt"] and not putt and row["outcome"] in ("missed", "conceded"):
            # (a miss or concession graded on it as a putt goes with the putt - a full shot's concession is
            # its gimme's - kept to go back to: _release_track)
            was = json.dumps({"outcome": row["outcome"], **json.loads(was)})
            self.db.execute("UPDATE shots SET outcome = NULL WHERE id = ?", (row["id"],))
        elif not row["is_putt"] and putt and row["outcome"] is None and row["uid"] is not None \
                and self.db.execute("SELECT 1 FROM shots WHERE derived_from = ? AND outcome = 'gimme'",
                                    (row["uid"],)).fetchone():
            # (a full shot that left a gimme, now a putt: a conceded putt's)
            was = json.dumps({"outcome": None, **json.loads(was)})
            self.db.execute("UPDATE shots SET outcome = 'conceded' WHERE id = ?", (row["id"],))
        self.db.execute(
            "INSERT OR REPLACE INTO shot_tracks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row["id"], t.shot_id, *(t.land or (None,) * 3), *(t.end or (None,) * 3),
             json.dumps([[round(c, 2) for c in p] for p in t.flight]) if t.flight else None,
             t.flight_s, json.dumps([[round(c, 2) for c in p] for p in t.roll]) if t.roll else None,
             carry, total, finish, t.lie_after, was))
