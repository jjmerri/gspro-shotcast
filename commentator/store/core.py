"""The Store: opening the season file, its sessions and round records, and migrations. Its work is in
the parts beside this file, one per job, which it is put together from."""
import json
import sqlite3
import time
from pathlib import Path
from typing import Callable

from .common import (
    CARRY_SNAPSHOT, CLUB_COLS, FORMAT, HAZARD_COLS, INDEXES, POSITION_COLS, SCHEMA, SeasonCopyFailed, _club_values,
    log, planner_counts_stale,
)
from ..models import Shot
from ..sources.gspro_log import M_TO_YD
from .shots import ShotsMixin
from .finishes import FinishesMixin
from .shot_edits import ShotEditsMixin
from .round_file import RoundFileMixin
from .session import SessionMixin
from .stats import StatsMixin
from .repairs import RepairsMixin
from .memory import MemoryMixin
from .rounds import RoundsMixin


class Store(ShotsMixin,
            FinishesMixin,
            ShotEditsMixin,
            RoundFileMixin,
            SessionMixin,
            StatsMixin,
            RepairsMixin,
            MemoryMixin,
            RoundsMixin):
    def __init__(self, path: str | None, new_session: bool = True, backups: str | Path | None = None,
                 on_copy: Callable[[], None] | None = None, check_later: bool = False) -> None:
        """Open (creating or upgrading) the season file. new_session=False opens it for maintenance, such
        as a backfill, without starting a booth session in it. `backups`: where a file in an older format
        is copied first (default: a backups folder beside it, where the booth's own copies go by default);
        `on_copy` is called before that copy. If it can't be copied, SeasonCopyFailed and the file is left
        as it was. `check_later`: the copy is checked and compressed in the background (the booth, so it
        starts in seconds rather than a minute on a big file)."""
        if path:
            from .. import backup

            where = backups or Path(path).parent / "backups"
            try:
                made = backup.before_upgrade(path, where, FORMAT, on_copy=on_copy, later=check_later)
            except Exception as e:
                log.exception("Season file not copied before upgrading it: not opening it")
                raise SeasonCopyFailed(f"the season file ({path}) couldn't be copied to {where} before "
                                       f"upgrading it, so it wasn't opened: {e}") from e
            if made:
                log.info("Season file copied before upgrading it: %s", made)
        # Long enough to outwait anything else holding the file (a backup's copy, a page edit), so the booth
        # doesn't stop over a busy moment.
        self.db = sqlite3.connect(path or ":memory:", timeout=30)
        if path:
            # Write-ahead logging: the stats page reads a snapshot of the file while the booth writes
            # (stats_web.connect). Kept by the file once set. A file where it can't be (a network
            # share) stays as it was, and the page reads a copy instead.
            try:
                if self.db.execute("PRAGMA journal_mode = WAL").fetchone()[0] != "wal":
                    log.warning("season file not in WAL mode: the stats page reads a copy of it")
            except sqlite3.OperationalError as e:
                log.warning("season file not switched to WAL mode: %s", e)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.executescript(CARRY_SNAPSHOT)   # (after _migrate: it reads shots.uid)
        self._backfill_finishes()
        self._locate_hole_images()
        from ..round_record import Live
        self.records = Live(self.db)   # which round or practice stretch each new row is in
        from ..booth_history import History
        self.history = History(self.db, lambda: self.session_id)   # the booth's history reads, kept
        if not new_session:
            self.session_id = None
            self.db.commit()
            return
        self.records.start(time.time())
        self.session_id = self.db.execute(
            "INSERT INTO sessions (started_at, date) VALUES (?, ?)", (time.time(), time.strftime("%Y-%m-%d"))
        ).lastrowid
        self.db.commit()

    def begin_round(self, uuid: str, *, course: str | None, players: dict[str, str | None],
                    seats: list[str] | None = None, since_id: int | None = None) -> str:
        """A round starting: its record (see round_record.Live.begin). Returns its UUID: a round a
        stopped booth left in progress, continued, keeps its own."""
        u = self.records.begin(uuid, session_id=self.session_id, course=self.course_name(course), players=players,
                               seats=seats, since_id=since_id, now=time.time())
        self.db.commit()
        return u

    def round_start_and_speeds(self, uuid: str) -> tuple[float | None, list[float]]:
        """When a round's record started (a continued round's first start) and the ball speeds of its
        strokes, mulligans aside: what tells GSPro's round file for it from another round's."""
        row = self.db.execute("SELECT started_at FROM round_records WHERE uuid = ?", (uuid,)).fetchone()
        speeds = [x for (x,) in self.db.execute("SELECT ball_speed FROM shots WHERE record_uuid = ? AND "
                                                "ball_speed IS NOT NULL AND mulligan = 0", (uuid,))]
        return (row["started_at"] if row else None), speeds

    def round_folder(self, uuid: str) -> str | None:
        """The GSPro course folder kept for a round record (apply_round_file's `folders`), if any."""
        row = self.db.execute("SELECT folder FROM round_folders WHERE record_uuid = ?", (uuid,)).fetchone()
        return row[0] if row else None

    def round_gspro_shots(self, uuid: str) -> list[str]:
        """GSPro's ShotIDs the booth tied a round's shots to (shot_tracks)."""
        return [x for (x,) in self.db.execute("SELECT t.gspro_shot_id FROM shot_tracks t JOIN shots s ON"
                                               " s.id = t.shot_id WHERE s.record_uuid = ?", (uuid,))]

    def round_players(self, uuid: str, players: dict[str, str | None], seats: list[str] | None = None) -> None:
        """A round's players changed mid-round: its slots follow (see round_record.Live.sync)."""
        self.records.sync(uuid, players, seats)
        self.db.commit()

    def end_round(self, uuid: str, complete: bool = True) -> None:
        self.records.end(uuid, complete, time.time())
        self.db.commit()

    def _scope(self, after_id: int, record: str | None) -> tuple[str, tuple]:
        """A round's shots: its record's, plus any since its floor written with no record (a path that
        stamps none); a round with no record (an older caller): every one since its floor."""
        if self.records.known(record):
            return "(record_uuid = ? OR (record_uuid IS NULL AND id > ?))", (record, after_id)
        return "id > ?", (after_id,)

    def roll_session_if_idle(self, idle_hours: float = 6.0, now: float | None = None) -> bool:
        """Start a new session when this one has sat idle for hours: a booth left running overnight
        otherwise filed the next morning's rounds under the night before."""
        if self.session_id is None:
            return False
        now = time.time() if now is None else now
        last = self.db.execute(
            "SELECT MAX(COALESCE((SELECT MAX(ts) FROM shots WHERE session_id = s.id), 0), s.started_at) "
            "FROM sessions s WHERE s.id = ?", (self.session_id,)).fetchone()[0]
        if last is None or now - last < idle_hours * 3600:
            return False
        self.session_id = self.db.execute(
            "INSERT INTO sessions (started_at, date) VALUES (?, ?)",
            (now, time.strftime("%Y-%m-%d", time.localtime(now)))).lastrowid
        self.db.commit()
        return True

    def _migrate(self) -> None:
        """Add columns a pre-existing season.db is missing, so an older file keeps working."""
        from .. import round_record   # (it reads this module's constants)

        # hole_images was once kept per session: keep each hole's first map with its ball and pin found
        # (else its first), as record_hole_image does now.
        self.db.execute("""DELETE FROM hole_images WHERE id NOT IN (
                               SELECT (SELECT i.id FROM hole_images i
                                       WHERE i.course = h.course COLLATE NOCASE AND i.hole = h.hole
                                       ORDER BY i.ball_x IS NULL OR i.pin_x IS NULL, i.id LIMIT 1)
                               FROM hole_images h)""")
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(shots)")}
        added = {"to_pin_yards": "REAL", "finish_yards": "REAL", "gimme_yards": "REAL",
                 "outcome": "TEXT", "fmt": "TEXT", "attributed": "INTEGER NOT NULL DEFAULT 1",
                 "is_league": "INTEGER NOT NULL DEFAULT 1", "is_tournament": "INTEGER NOT NULL DEFAULT 0",
                 "mulligan": "INTEGER NOT NULL DEFAULT 0",
                 "shared_ball": "INTEGER NOT NULL DEFAULT 0", "left_out": "INTEGER NOT NULL DEFAULT 0", "handed": "TEXT",
                 "excluded": "INTEGER NOT NULL DEFAULT 0",
                 "picked": "INTEGER", "tree_hits": "INTEGER", "total_from_log": "INTEGER NOT NULL DEFAULT 0",
                 "rest_ts": "REAL",   # when GSPro's log had the ball stop (wall clock), for highlight clips
                 **{col: "REAL" for col in CLUB_COLS}, **{col: "REAL" for col in POSITION_COLS},
                 **{col: "REAL" for col in HAZARD_COLS}}
        for col, decl in added.items():
            if col not in have:
                self.db.execute(f"ALTER TABLE shots ADD COLUMN {col} {decl}")
        if not set(CLUB_COLS) <= have:
            self._backfill_club_data()
        have_rounds = {r["name"] for r in self.db.execute("PRAGMA table_info(rounds)")}
        rounds_added = {"scramble_hit": "INTEGER", "scramble_tries": "INTEGER", "sand_hit": "INTEGER",
                        "sand_tries": "INTEGER", "hole_pars": "TEXT", "is_league": "INTEGER NOT NULL DEFAULT 1",
                        "is_tournament": "INTEGER NOT NULL DEFAULT 0",
                        "complete": "INTEGER NOT NULL DEFAULT 1", "holes_played": "INTEGER",
                        "nine_to_par": "INTEGER", "nine_net_to_par": "INTEGER", "players": "TEXT",
                        "excluded": "INTEGER NOT NULL DEFAULT 0", "green_firmness": "INTEGER",
                        "fairway_firmness": "INTEGER", "course_type": "INTEGER", "launch_monitor": "TEXT"}
        for col, decl in rounds_added.items():
            if col not in have_rounds:
                self.db.execute(f"ALTER TABLE rounds ADD COLUMN {col} {decl}")
        # player_rounds gained per-hole scores/pars and putts after its first version (SCHEMA creates
        # the table; an older file has it without these columns).
        have_pr = {r["name"] for r in self.db.execute("PRAGMA table_info(player_rounds)")}
        pr_added = {"putts": "INTEGER", "putt_holes": "INTEGER", "scores": "TEXT", "hole_pars": "TEXT",
                    "is_league": "INTEGER NOT NULL DEFAULT 1", "is_tournament": "INTEGER NOT NULL DEFAULT 0",
                    "complete": "INTEGER NOT NULL DEFAULT 1", "holes_played": "INTEGER", "nine_to_par": "INTEGER", "excluded": "INTEGER NOT NULL DEFAULT 0"}
        for col, decl in pr_added.items():
            if col not in have_pr:
                self.db.execute(f"ALTER TABLE player_rounds ADD COLUMN {col} {decl}")
        have_lines = {r["name"] for r in self.db.execute("PRAGMA table_info(lines)")}
        for col, decl in {"excluded": "INTEGER NOT NULL DEFAULT 0", "aired_at": "REAL", "aired_seconds": "REAL"}.items():
            if col not in have_lines:
                self.db.execute(f"ALTER TABLE lines ADD COLUMN {col} {decl}")
        if "holes" not in {r["name"] for r in self.db.execute("PRAGMA table_info(week_results)")}:
            self.db.execute("ALTER TABLE week_results ADD COLUMN holes TEXT")
        if "tee_box" not in {r["name"] for r in self.db.execute("PRAGMA table_info(hole_layouts)")}:
            self.db.execute("ALTER TABLE hole_layouts ADD COLUMN tee_box TEXT")
        # Per-session and per-shot lookups (the booth's, during play, and the stats build's) would
        # otherwise scan every shot ever recorded. Made here, after the columns exist; the first time,
        # ANALYZE so the planner knows how the indexes compare (and again below as the season grows).
        have_ix = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        for name, on in INDEXES.items():
            self.db.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {on}")
        round_record.install(self.db)
        if not set(INDEXES) <= have_ix:
            self.db.execute("ANALYZE")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 1:
            # Tee yardages were kept as the course CSV gives them, in metres, and from its old-format
            # file where a course has both: make them yards, and have the courses read again (see
            # courses_without_tees) for the current file's par, index and yardages.
            for course, tee, yards in self.db.execute("SELECT course, tee, yards FROM course_tees").fetchall():
                metres = json.loads(yards or "{}")
                self.db.execute("UPDATE course_tees SET yards = ? WHERE course = ? AND tee = ?",
                                (json.dumps({h: round(m * M_TO_YD) for h, m in metres.items()}), course, tee))
            self.db.execute("DELETE FROM tee_lengths")
            self.db.execute("PRAGMA user_version = 1")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 2:
            # Newer GSPro showed the driver for any long shot, so off the tee it was a 3-wood (hud.deck_club).
            self.db.execute("""UPDATE shots SET club = '3W' WHERE club = 'DR' AND is_putt = 0
                               AND NOT (lower(COALESCE(lie_before, '')) LIKE 'tee%' OR COALESCE(shot_number, 0) = 1
                                        OR (COALESCE(lie_before, '') = '' AND shot_number IS NULL))""")
            self.db.execute("PRAGMA user_version = 2")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 3:
            self._repair_alternate_finishes()
            self.db.execute("PRAGMA user_version = 3")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 4:
            self._repair_misread_first_strokes()
            self.db.execute("PRAGMA user_version = 4")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 5:
            self._repair_tee_finishes()
            self.db.execute("PRAGMA user_version = 5")
        # A round's course read off the scorecard was stored in capitals: file it under the shots' name.
        for table in ("rounds", "player_rounds"):
            for (course,) in self.db.execute(f"SELECT DISTINCT course FROM {table} WHERE course IS NOT NULL").fetchall():
                if (name := self.course_name(course)) != course:
                    self.db.execute(f"UPDATE {table} SET course = ? WHERE course = ?", (name, course))
        round_record.migrate(self.db)
        if self.db.execute("PRAGMA user_version").fetchone()[0] == 6:
            self._repair_stranded_openers()
            self.db.execute("PRAGMA user_version = 7")
        if self.db.execute("PRAGMA user_version").fetchone()[0] == 7:
            self._repair_picked_up_holes()
            self.db.execute("PRAGMA user_version = 8")
        if planner_counts_stale(self.db):   # (last: after the rows the migrations add)
            self.db.execute("ANALYZE")
        self.db.commit()

    def course_name(self, course: str | None) -> str | None:
        """The course as the season already names it, ignoring case: GSPro's own name off its shots
        (the scorecard and setup screen read it in capitals), else a round's that isn't in capitals."""
        if not course:
            return course
        for table in ("shots", "rounds"):
            row = self.db.execute(f"SELECT course FROM {table} WHERE course = ? COLLATE NOCASE AND course != upper(course)"
                                  " ORDER BY id DESC LIMIT 1", (course,)).fetchone()
            if row:
                return row[0]
        return course

    def _backfill_club_data(self) -> None:
        """Fill CLUB_COLS on older rows from the Open Connect message each one kept in `raw`."""
        updates = []
        for row in self.db.execute("SELECT id, raw FROM shots WHERE raw IS NOT NULL"):
            try:
                msg = json.loads(row["raw"])
            except (TypeError, ValueError):
                continue
            if isinstance(msg, dict) and (vals := _club_values(Shot.from_gspro(msg))) != (None,) * len(CLUB_COLS):
                updates.append((*vals, row["id"]))
        sets = ", ".join(f"{c} = ?" for c in CLUB_COLS)
        self.db.executemany(f"UPDATE shots SET {sets} WHERE id = ?", updates)
        if updates:
            log.info("Backfilled club data on %d shots", len(updates))

    def close(self) -> None:
        self.db.close()
