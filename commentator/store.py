"""Season history in SQLite: every shot and every line, kept across league nights.

One file, no server. Open it with any SQLite viewer (e.g. DB Browser for SQLite) to browse or
export. Replays and evals use an in-memory database so made-up shots never reach the season.
"""

import io
import json
import logging
import re
import sqlite3
import time
from typing import Any

from .hud import Hud
from .models import Line, Shot

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    started_at REAL NOT NULL,
    date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shots (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    ts REAL NOT NULL,
    player TEXT,
    course TEXT,
    hole TEXT,
    shot_number INTEGER,
    club TEXT,
    is_putt INTEGER NOT NULL,
    ball_speed REAL, launch_angle REAL, start_direction REAL, total_spin REAL, spin_axis REAL,
    club_speed REAL, carry_raw REAL,           -- launch monitor, in its own units
    units TEXT,
    lie_before TEXT, slope_updown REAL, slope_side TEXT, slope_side_degrees REAL,
    elevation_yards REAL, to_pin TEXT, score_to_par TEXT,
    lie_after TEXT,
    carry_raw_tile_yards REAL,                   -- GSPro CARRY (raw): flat-ground, no lie penalty (~launch monitor carry)
    carry_game_yards REAL, total_yards REAL, offline_yards REAL,   -- GSPro, after landing
    tags TEXT,                                   -- JSON list from the interest scorer
    raw TEXT,                                    -- the Open Connect message
    -- Per-player stats, mostly from GSPro's own log (see sources/gspro_log.py). to_pin_yards is the
    -- numeric distance before the shot; finish_yards is where the ball came to rest (how close it
    -- got), only when attributable; gimme_yards is the gimme radius then; outcome is a putt's result
    -- (holed / conceded / missed), or 'gimme' on the row for a conceded tap-in that was never struck.
    -- attributed is 0 on an alternate-shot round, where the log can't say which partner hit, so those shots are excluded from per-player stats.
    to_pin_yards REAL, finish_yards REAL, gimme_yards REAL, outcome TEXT, fmt TEXT,
    attributed INTEGER NOT NULL DEFAULT 1,
    is_league INTEGER NOT NULL DEFAULT 1,        -- 1 = league round, 0 = practice; for the stat context split
    mulligan INTEGER NOT NULL DEFAULT 0,         -- 1 = GSPro took the stroke back; excluded from every stat
    left_out INTEGER NOT NULL DEFAULT 0,         -- 1 = left out of the stats by hand (a misread, a practice
                                                 -- swing); still a stroke on the card, unlike a mulligan
    shared_ball INTEGER NOT NULL DEFAULT 0,      -- 1 = a team format where GSPro copies balls between
                                                 -- teammates, so the next stroke may not be from this ball
    -- Club delivery, in degrees (face impact in mm from centre), only when the monitor reported club
    -- data for the shot (ContainsClubData); NULL otherwise, never a placeholder zero.
    club_path REAL, angle_of_attack REAL, face_to_target REAL, dynamic_loft REAL,
    face_impact_h REAL, face_impact_v REAL,
    -- From GSPro's log: where the ball sat for this shot, in the course's world metres (y is height;
    -- NULL for a tee shot, which GSPro sets up without logging it: use hole_layouts' tee), and the
    -- player's last minimap aim click before it, in minimap units (NULL when the aim wasn't moved).
    start_x REAL, start_y REAL, start_z REAL, aim_map_x REAL, aim_map_y REAL,
    -- Where the ball went into a penalty area (water, OB), world metres, from GSPro's log; else NULL.
    hazard_x REAL, hazard_y REAL, hazard_z REAL,
    handed TEXT                                  -- 'RH' / 'LH' as GSPro had the player set; NULL if unknown
);
CREATE TABLE IF NOT EXISTS hole_layouts (            -- each hole's geometry, logged by GSPro when a round is complete
    id INTEGER PRIMARY KEY,
    session_id INTEGER REFERENCES sessions(id),  -- NULL when backfilled from a log no session matched
    ts REAL,
    course TEXT NOT NULL,
    hole INTEGER NOT NULL,                       -- the course's hole number
    par INTEGER,
    tee_x REAL, tee_y REAL, tee_z REAL,          -- world metres, as shots.start_*
    pin_x REAL, pin_y REAL, pin_z REAL,          -- this round's pin
    wp_x REAL, wp_y REAL, wp_z REAL              -- the course designer's aim waypoint (not the player's aim)
);
CREATE TABLE IF NOT EXISTS hole_images (             -- GSPro's minimap off the tee, the hole replay's background
    id INTEGER PRIMARY KEY,                      -- one per course and hole: the map is the same every round
    session_id INTEGER REFERENCES sessions(id),  -- the session it was taken in
    ts REAL,
    course TEXT NOT NULL,
    hole INTEGER NOT NULL,                       -- the course's hole number
    image BLOB NOT NULL,                         -- JPEG of the map area (title bar trimmed)
    ball_x REAL, ball_y REAL,                    -- the ball (on the tee) and the pin on it, as fractions of
    pin_x REAL, pin_y REAL                       -- the image from its top-left; NULL when not found
);
CREATE TABLE IF NOT EXISTS course_tees (            -- each tee's rating and slope, from GSPro's course files
    course TEXT NOT NULL,                        -- as the season names the course (shots.course)
    tee TEXT NOT NULL,                           -- GSPro's tee name: Black, White, Green, Blue, ...
    rating REAL, slope REAL,                     -- NULL when the course doesn't rate this tee
    yards TEXT,                                  -- JSON: hole number -> yards from this tee
    par TEXT,                                    -- JSON: hole number -> par
    hole_index TEXT,                             -- JSON: hole number -> stroke index
    PRIMARY KEY (course, tee)
);
CREATE TABLE IF NOT EXISTS lines (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    shot_id INTEGER REFERENCES shots(id),
    ts REAL NOT NULL,
    kind TEXT NOT NULL,                          -- preshot / flight / shot / segment
    speaker TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS form_mentions (           -- round-sheet angles the booth used, per player
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    ts REAL NOT NULL,
    player TEXT NOT NULL,
    topic TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS asides (                  -- course trivia and score check-ins that aired
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    topic TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS news_heard (              -- golf-news stories that aired, per player in the room
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    ts REAL NOT NULL,
    player TEXT NOT NULL,
    title TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rounds (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    ended_at REAL NOT NULL,
    date TEXT NOT NULL,
    course TEXT,
    format TEXT,
    side TEXT NOT NULL,                          -- the player, or the team for one-ball formats
    team TEXT,
    holes INTEGER NOT NULL,
    gross INTEGER, par INTEGER, strokes INTEGER, net_to_par INTEGER,
    fir_hit INTEGER, fir_tries INTEGER, gir_hit INTEGER, gir_tries INTEGER,
    scramble_hit INTEGER, scramble_tries INTEGER, sand_hit INTEGER, sand_tries INTEGER,
    putts INTEGER, putt_holes INTEGER, penalties INTEGER, longest_drive REAL,
    scores TEXT,                                 -- JSON: hole number (or hole of the round) -> strokes
    hole_pars TEXT,                              -- JSON: hole number -> par, for scores-vs-par season stats
    source TEXT,                                 -- scorecard / shots
    is_league INTEGER NOT NULL DEFAULT 1,        -- 1 = league round, 0 = practice; for the stat context split
    complete INTEGER NOT NULL DEFAULT 1,         -- 0 = quit early in GSPro (holes_played < holes)
    holes_played INTEGER,                        -- holes actually scored (NULL on older rows: use holes)
    nine_to_par INTEGER,                         -- gross to par over the first nine played, set only on a
    nine_net_to_par INTEGER                      -- part-played round; the season ranks per nine, and
                                                 -- prorating eleven holes onto nine invents a score
);
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    ts REAL NOT NULL,
    commentator TEXT NOT NULL,                   -- the persona key that made the call
    kind TEXT NOT NULL,                          -- what was predicted, e.g. 'putt'
    predicted TEXT NOT NULL,                     -- 'make' / 'miss'
    actual TEXT,                                 -- 'made' / 'missed'
    correct INTEGER                              -- 1/0 once resolved
);
CREATE TABLE IF NOT EXISTS player_rounds (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    ended_at REAL NOT NULL,
    player TEXT NOT NULL,                        -- the individual (roster name, as on the shot), not the side
    course TEXT,
    fmt TEXT,                                    -- scramble/best_ball/aggregate/... (alternate isn't recorded)
    fir_hit INTEGER, fir_tries INTEGER, gir_hit INTEGER, gir_tries INTEGER,
    scramble_hit INTEGER, scramble_tries INTEGER, sand_hit INTEGER, sand_tries INTEGER,
    putts INTEGER, putt_holes INTEGER,
    scores TEXT,                                 -- JSON: hole number -> this player's strokes
    hole_pars TEXT,                              -- JSON: hole number -> par, for scores-vs-par season stats
    is_league INTEGER NOT NULL DEFAULT 1,        -- 1 = league round, 0 = practice; for the stat context split
    complete INTEGER NOT NULL DEFAULT 1,         -- 0 = quit early in GSPro
    holes_played INTEGER,                        -- holes this player actually scored
    nine_to_par INTEGER                          -- gross to par over the first nine played, on a
                                                 -- part-played round only (see the rounds table)
);
CREATE INDEX IF NOT EXISTS shots_player ON shots(player);
CREATE INDEX IF NOT EXISTS shots_lie ON shots(lie_before);
"""


# A round row counts toward the season's round totals (rounds played, average/best net) when it was
# finished, or quit early after at least a full nine. Older rows have no holes_played: use holes.
COUNTS_AS_ROUND_HOLES = 9   # a quit this far in counts as a round, and earns a sign-off
# A round of nine or more holes, finished or quit after a full nine. A shorter game (a one-hole 19th)
# is left out: scored per nine, a birdie on its one hole would read as a -9 round.
_COUNTS_AS_ROUND = (f"((complete = 1 AND holes >= {COUNTS_AS_ROUND_HOLES})"
                    f" OR COALESCE(holes_played, holes) >= {COUNTS_AS_ROUND_HOLES})")
# Net to par per nine holes, so 9- and 18-hole rounds (and a nine inside an abandoned 18) compare.
# A round quit part-way through stores the nine it actually finished (nine_net_to_par), because
# prorating eleven holes onto a nine invents a score nobody shot. Older rows have no nine column.
_NET_PER_NINE = ("COALESCE(nine_net_to_par,"
                 " net_to_par * 9.0 / COALESCE(NULLIF(holes_played, 0), holes))")
# Gross to par per nine holes, only where par was known for every scored hole: `par` sums the known
# ones, so a missing par would read as strokes over par. NULL otherwise (left out of the averages).
_PARS_COMPLETE = ("(hole_pars IS NOT NULL AND scores IS NOT NULL AND holes_played > 0"
                  " AND (SELECT COUNT(*) FROM json_each(scores)) = holes_played"
                  " AND NOT EXISTS (SELECT 1 FROM json_each(scores) sc"
                  " WHERE json_extract(hole_pars, '$.\"' || sc.key || '\"') IS NULL))")
_GROSS_PER_NINE = (f"(CASE WHEN nine_to_par IS NOT NULL THEN nine_to_par"
                   f" WHEN {_PARS_COMPLETE} THEN (gross - par) * 9.0 / holes_played END)")


# A drive's length: the total distance (carry plus roll) where GSPro's tile was read, else the carry.
_DRIVE = "COALESCE(total_yards, carry_game_yards, carry_raw)"
# In the queries below, a drive is the driver off the tee (the hole's first shot, from the tee box,
# or neither read), never a putt: a late club read can tag a putt "DR", and one such row dragged a
# driving average to 208. And "earlier weeks" means earlier DAYS: every booth restart starts a new session,
# so excluding only the current session counted tonight's earlier holes as history.


POSITION_COLS = ("start_x", "start_y", "start_z", "aim_map_x", "aim_map_y")
HAZARD_COLS = ("hazard_x", "hazard_y", "hazard_z")


def _position_values(start: tuple | None, aim: tuple | None) -> tuple[float | None, ...]:
    return (*(start if start else (None, None, None)), *(aim if aim else (None, None)))


CLUB_COLS = ("club_path", "angle_of_attack", "face_to_target", "dynamic_loft", "face_impact_h", "face_impact_v")


def _club_values(shot: Shot) -> tuple[float | None, ...]:
    """The CLUB_COLS values for a shot: all NULL unless the monitor reported club data, since monitors
    without it send zeros that would read as a dead-square strike."""
    if not shot.club_measured:
        return (None,) * len(CLUB_COLS)
    return (shot.path, shot.angle_of_attack, shot.face_to_target, shot.loft,
            shot.horizontal_face_impact, shot.vertical_face_impact)


def _ctx_sql(context: str | None) -> str:
    """A WHERE-clause fragment slicing a stat by round context: None = lifetime (league + practice),
    'league' = league rounds only (the 'under pressure' slice), 'practice' = practice rounds only.
    Appended to a query whose table carries is_league (shots / rounds / player_rounds), which is 1/0
    with legacy rows defaulted to 1, so no NULL handling is needed. No bind params (literal filter)."""
    if context == "league":
        return " AND is_league = 1"
    if context == "practice":
        return " AND is_league = 0"
    return ""


def _median(xs: list[float]) -> float | None:
    xs = sorted(xs)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


class Store:
    def __init__(self, path: str | None, new_session: bool = True) -> None:
        """Open (creating or upgrading) the season file. new_session=False opens it for maintenance, such
        as a backfill, without starting a booth session in it."""
        self.db = sqlite3.connect(path or ":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self._backfill_finishes()
        self._locate_hole_images()
        if not new_session:
            self.session_id = None
            self.db.commit()
            return
        self.session_id = self.db.execute(
            "INSERT INTO sessions (started_at, date) VALUES (?, ?)", (time.time(), time.strftime("%Y-%m-%d"))
        ).lastrowid
        self.db.commit()

    def _migrate(self) -> None:
        """Add columns a pre-existing season.db is missing, so an older file keeps working."""
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
                 "is_league": "INTEGER NOT NULL DEFAULT 1", "mulligan": "INTEGER NOT NULL DEFAULT 0",
                 "shared_ball": "INTEGER NOT NULL DEFAULT 0", "left_out": "INTEGER NOT NULL DEFAULT 0", "handed": "TEXT",
                 "picked": "INTEGER",
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
                        "complete": "INTEGER NOT NULL DEFAULT 1", "holes_played": "INTEGER",
                        "nine_to_par": "INTEGER", "nine_net_to_par": "INTEGER"}
        for col, decl in rounds_added.items():
            if col not in have_rounds:
                self.db.execute(f"ALTER TABLE rounds ADD COLUMN {col} {decl}")
        # player_rounds gained per-hole scores/pars and putts after its first version (SCHEMA creates
        # the table; an older file has it without these columns).
        have_pr = {r["name"] for r in self.db.execute("PRAGMA table_info(player_rounds)")}
        pr_added = {"putts": "INTEGER", "putt_holes": "INTEGER", "scores": "TEXT", "hole_pars": "TEXT",
                    "is_league": "INTEGER NOT NULL DEFAULT 1", "complete": "INTEGER NOT NULL DEFAULT 1",
                    "holes_played": "INTEGER", "nine_to_par": "INTEGER"}
        for col, decl in pr_added.items():
            if col not in have_pr:
                self.db.execute(f"ALTER TABLE player_rounds ADD COLUMN {col} {decl}")
        # A round's course read off the scorecard was stored in capitals: file it under the shots' name.
        for table in ("rounds", "player_rounds"):
            for (course,) in self.db.execute(f"SELECT DISTINCT course FROM {table} WHERE course IS NOT NULL").fetchall():
                if (name := self.course_name(course)) != course:
                    self.db.execute(f"UPDATE {table} SET course = ? WHERE course = ?", (name, course))
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

    def record_shot(
        self, *, player: str | None, hole: str | None, shot: Shot, before: Hud, after: Hud, tags: list[str],
        carry_game_yards: float | None, total_yards: float | None, offline_yards: float | None,
        course: str | None = None, carry_raw_tile_yards: float | None = None,
        to_pin_yards: float | None = None, finish_yards: float | None = None,
        gimme_yards: float | None = None, outcome: str | None = None, fmt: str | None = None,
        attributed: bool = True, is_league: bool = True, shared_ball: bool = False,
        start: tuple | None = None, aim: tuple | None = None, handed: str | None = None,
    ) -> int:
        cur = self.db.execute(
            """INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, ball_speed,
                   launch_angle, start_direction, total_spin, spin_axis, club_speed, carry_raw, units,
                   lie_before, slope_updown, slope_side, slope_side_degrees, elevation_yards, to_pin,
                   score_to_par, lie_after, carry_raw_tile_yards, carry_game_yards, total_yards, offline_yards,
                   tags, raw, to_pin_yards, finish_yards, gimme_yards, outcome, fmt, attributed, is_league,
                   shared_ball, club_path, angle_of_attack, face_to_target, dynamic_loft, face_impact_h,
                   face_impact_v, start_x, start_y, start_z, aim_map_x, aim_map_y, handed)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                       ?,?,?,?,?,?)""",
            (self.session_id, shot.timestamp, player, course, hole, before.shot_number or after.shot_number, shot.club,
             int(shot.is_putt), shot.ball_speed, shot.vla, shot.hla, shot.total_spin, shot.spin_axis,
             shot.club_speed, shot.carry, shot.units, before.lie, before.slope_updown, before.slope_side,
             before.slope_side_degrees, before.elevation_yards, before.to_pin or after.to_pin,
             before.score_to_par or after.score_to_par, after.lie, carry_raw_tile_yards, carry_game_yards,
             total_yards, offline_yards,
             json.dumps(tags), json.dumps(shot.raw),
             to_pin_yards, finish_yards, gimme_yards, outcome, fmt, int(attributed), int(is_league),
             int(shared_ball), *_club_values(shot), *_position_values(start, aim),
             (handed or before.handed or shot.handed or "").strip().upper() or None),
        )
        self._unhole_if_played_on(cur.lastrowid, later=False)
        self._fill_previous_finish(cur.lastrowid)
        self.db.commit()
        return cur.lastrowid

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

        from .minimap import locate

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

    _FINISH_COLS = "id, session_id, player, hole, shot_number, is_putt, outcome, mulligan, attributed, " \
                   "to_pin_yards, finish_yards, gimme_yards, fmt"

    def _finish_from_next(self, prev: sqlite3.Row, nxt: sqlite3.Row) -> float | None:
        """Where a full shot finished, read off the same player's next stroke: that stroke's distance to
        the pin, or the gimme radius when it's the conceded tap-in. Only for the very next stroke on the
        same hole (a penalty in between means the next was played from a drop, not where the ball lay)."""
        if prev["is_putt"] or prev["mulligan"] or prev["finish_yards"] is not None or prev["outcome"] == "gimme":
            return None
        if prev["fmt"] == "scramble":   # the next stroke may be from a partner's ball
            return None
        if not self._same_hole_next(prev, nxt) or nxt["shot_number"] != prev["shot_number"] + 1:
            return None
        if nxt["outcome"] == "gimme":
            return prev["gimme_yards"] or nxt["gimme_yards"] or 1.3
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
        full shot that holed out. Idempotent; only touches rows that have none."""
        rows = self.db.execute(f"SELECT {self._FINISH_COLS} FROM shots WHERE attributed = 1 AND player IS NOT NULL"
                               " AND player != 'unknown' ORDER BY session_id, player, id").fetchall()
        updates = []
        for prev, nxt in zip(rows, rows[1:]):
            if prev["mulligan"] or (prev["session_id"], prev["player"]) != (nxt["session_id"], nxt["player"]):
                continue
            if nxt["mulligan"]:
                continue
            if (finish := self._finish_from_next(prev, nxt)) is not None:
                updates.append((finish, prev["id"]))
        updates += [(0.0, r["id"]) for r in rows if not r["is_putt"] and r["outcome"] == "holed"
                    and r["finish_yards"] is None and not r["mulligan"]]
        if updates:
            self.db.executemany("UPDATE shots SET finish_yards = ? WHERE id = ?", updates)
            self.db.commit()
            log.info("Filled %d missing shot finishing distances from the following strokes", len(updates))

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
        self.db.execute("UPDATE shots SET outcome = ? WHERE id = ?", ("missed" if h["is_putt"] else None, h["id"]))
        log.warning("Shot %s was marked holed but %s hit again on the hole (shot %s): not holed",
                    h["id"], row["player"], nxt["id"])

    def set_shot_result(self, shot_id: int | None, *, finish_yards: float | None = None,
                        outcome: str | None = None) -> None:
        """Fill in a shot's finishing distance and holed/conceded outcome, known only once the ball
        settles (for a putt, after the reaction path resolves it)."""
        if shot_id is None:
            return
        if finish_yards is not None:
            self.db.execute("UPDATE shots SET finish_yards = ? WHERE id = ?", (finish_yards, shot_id))
        if outcome is not None:
            self.db.execute("UPDATE shots SET outcome = ? WHERE id = ?", (outcome, shot_id))
            if outcome == "holed":
                self._unhole_if_played_on(shot_id, later=True)
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
        if not course or not (card.yards or card.ratings):
            return
        course = self.course_name(course)
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
        """Courses with a round on record but no tees kept yet, to fill from GSPro's course files."""
        return [r[0] for r in self.db.execute(
            "SELECT DISTINCT course FROM rounds WHERE course IS NOT NULL AND NOT EXISTS"
            " (SELECT 1 FROM course_tees t WHERE t.course = rounds.course COLLATE NOCASE)")]

    def set_shot_position(self, shot_id: int | None, start: tuple) -> None:
        """Where GSPro's log had the ball for this shot, once its score line arrived after it was recorded."""
        if shot_id is not None:
            self.db.execute("UPDATE shots SET start_x = ?, start_y = ?, start_z = ? WHERE id = ? AND start_x IS NULL",
                            (*start, shot_id))
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
                " AND o.start_x IS NOT NULL AND abs(o.to_pin_yards - shots.to_pin_yards) < 0.5)",
                {"old": replaced, "new": shot_id})
        self.db.commit()

    def relabel_hole(self, old: str, new: str, since_id: int) -> None:
        """This session's shots from `since_id` on that were recorded on hole `old` move to `new`."""
        self.db.execute("UPDATE shots SET hole = ? WHERE session_id = ? AND hole = ? AND id >= ?",
                        (new, self.session_id, old, since_id))
        self.db.commit()

    def set_lie_after(self, shot_id: int | None, lie: str | None) -> None:
        """Where the ball came to rest, corrected once the next shot says where it was played from."""
        if shot_id is not None and lie:
            self.db.execute("UPDATE shots SET lie_after = ? WHERE id = ?", (lie, shot_id))
            self.db.commit()

    def set_format(self, shot_ids: list[int], fmt: str) -> None:
        """Relabel a round's shots once its real format is known (a scramble GSPro's log revealed)."""
        if shot_ids:
            # A scramble's balls are shared, including the tee shots recorded before it was known.
            self.db.executemany("UPDATE shots SET fmt = ?, shared_ball = shared_ball OR ? WHERE id = ?",
                                [(fmt, fmt == "scramble", i) for i in shot_ids])
            self.db.commit()

    def record_gimme(self, shot_id: int) -> int | None:
        """The conceded putt after `shot_id` ended inside the gimme circle: GSPro counts the stroke but
        nobody hits it, so it's stored as a putt with outcome 'gimme' and no launch data, from the green,
        within the gimme radius (its exact length isn't logged). Same player, hole and flags as the
        stroke that left it."""
        cur = self.db.execute(
            """INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, units,
                   lie_before, to_pin, score_to_par, tags, raw, gimme_yards, outcome, fmt, attributed, is_league)
               SELECT session_id, ?, player, course, hole, shot_number + 1, NULL, 1, units, 'green', NULL,
                      score_to_par, '["gimme"]', '{}', gimme_yards, 'gimme', fmt, attributed, is_league
               FROM shots WHERE id = ?""",
            (time.time(), shot_id),
        )
        if cur.rowcount:
            self._fill_previous_finish(cur.lastrowid)
        self.db.commit()
        return cur.lastrowid if cur.rowcount else None

    def record_lines(self, kind: str, lines: list[Line], shot_id: int | None = None) -> None:
        self.db.executemany(
            "INSERT INTO lines (session_id, shot_id, ts, kind, speaker, text) VALUES (?,?,?,?,?,?)",
            [(self.session_id, shot_id, time.time(), kind, l.speaker, l.text) for l in lines],
        )
        self.db.commit()

    def spoken_lines(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT text FROM lines")]

    def carry_pairs(self) -> list[tuple]:
        """Past full shots for the carry model, oldest first: (lie, base carry yd, game carry yd
        corrected for how high the ball came down, course, launch angle, base). Base is GSPro's CARRY
        (raw) where it was read, else the launch monitor's carry. Shots whose landing height isn't
        known are left out (see correct_for_elevation)."""
        from .carry_model import correct_for_elevation
        from .facts import to_yards

        rows = self.db.execute(
            """SELECT id, session_id, player, hole, shot_number, is_putt, mulligan, fmt, lie_before, carry_raw,
                      units, carry_raw_tile_yards, carry_game_yards, elevation_yards, to_pin, course,
                      launch_angle, finish_yards, ts, attributed, shared_ball, left_out
               FROM shots ORDER BY id"""
        ).fetchall()
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
            if r["is_putt"] or r["left_out"] or not r["lie_before"] or not (r["carry_game_yards"] or 0) > 0:
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
                out.append((r["lie_before"], carry, game, r["course"], r["launch_angle"], base))
        return out

    def season_stats(self, player: str, context: str | None = None) -> dict[str, Any]:
        """Season numbers for one player, in yards, excluding tonight so 'season best' means before tonight."""
        r = self.db.execute(
            """SELECT COUNT(*) AS shots,
                      MAX(CASE WHEN club = 'DR' AND is_putt = 0 AND (shot_number = 1 OR lie_before = 'tee' OR (shot_number IS NULL AND lie_before IS NULL)) THEN {_DRIVE} END) AS longest_drive,
                      AVG(CASE WHEN club = 'DR' AND is_putt = 0 AND (shot_number = 1 OR lie_before = 'tee' OR (shot_number IS NULL AND lie_before IS NULL)) THEN {_DRIVE} END) AS avg_drive,
                      COUNT(DISTINCT session_id) AS nights
               FROM shots WHERE player = ? AND session_id NOT IN (SELECT id FROM sessions WHERE date = (SELECT date FROM sessions WHERE id = ?)) AND mulligan = 0 AND left_out = 0""".format(_DRIVE=_DRIVE) + _ctx_sql(context),
            (player, self.session_id),
        ).fetchone()
        return {k: r[k] for k in r.keys()}

    def last_shot_id(self) -> int:
        return self.db.execute("SELECT COALESCE(MAX(id), 0) FROM shots").fetchone()[0]

    def week_ctp(self, courses: list[str], holes: list[int], since: str,
                 before_id: int | None = None) -> dict[int, tuple[str, float]]:
        """The closest league tee shot on each closest-to-the-pin hole of this course since `since`
        (YYYY-MM-DD): hole number -> (player, yards). Only a tee shot that finished on the green (or
        in the hole) counts. Individual-ball rounds only (best ball, aggregate): a team-ball format's
        next stroke is from the chosen ball, so a player's own tee shot finish isn't known there.
        `before_id` leaves out shots recorded after it (the round being played)."""
        from .league import course_match_score

        rows = self.db.execute(
            """SELECT sh.player, sh.course, sh.hole, sh.finish_yards FROM shots sh
               JOIN sessions se ON se.id = sh.session_id
               WHERE sh.is_league = 1 AND sh.attributed = 1 AND sh.mulligan = 0 AND sh.left_out = 0 AND sh.is_putt = 0
                     AND sh.shot_number = 1 AND sh.fmt IN ('best_ball', 'aggregate')
                     AND sh.finish_yards IS NOT NULL AND (sh.lie_after = 'green' OR sh.outcome = 'holed')
                     AND sh.player IS NOT NULL AND sh.player != 'unknown' AND se.date >= ?
                     AND (? IS NULL OR sh.id <= ?)""",
            (since, before_id, before_id),
        ).fetchall()
        best: dict[int, tuple[str, float]] = {}
        for r in rows:
            m = re.match(r"Hole (\d+)\b", r["hole"] or "")
            if not m or int(m.group(1)) not in holes:
                continue
            if not any(c and (c.lower() == (r["course"] or "").lower() or course_match_score(c, r["course"] or "") >= 0.6)
                       for c in courses):
                continue
            hole = int(m.group(1))
            if hole not in best or r["finish_yards"] < best[hole][1]:
                best[hole] = (r["player"], r["finish_yards"])
        return best

    def season_longest_drive(self, context: str | None = None) -> tuple[str, float] | None:
        r = self.db.execute(
            f"""SELECT player, {_DRIVE} AS carry FROM shots
               WHERE club = 'DR' AND is_putt = 0 AND (shot_number = 1 OR lie_before = 'tee' OR (shot_number IS NULL AND lie_before IS NULL)) AND player IS NOT NULL AND player != 'unknown' AND session_id NOT IN (SELECT id FROM sessions WHERE date = (SELECT date FROM sessions WHERE id = ?))
                     AND mulligan = 0 AND left_out = 0"""
            + _ctx_sql(context) + " ORDER BY carry DESC LIMIT 1",
            (self.session_id,),
        ).fetchone()
        return (r["player"], r["carry"]) if r else None

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
                      ROUND(MAX(CASE WHEN club = 'DR' AND is_putt = 0 AND (shot_number = 1 OR lie_before = 'tee' OR (shot_number IS NULL AND lie_before IS NULL)) THEN {_DRIVE} END)) AS longest_drive,
                      ROUND(AVG(CASE WHEN club = 'DR' AND is_putt = 0 AND (shot_number = 1 OR lie_before = 'tee' OR (shot_number IS NULL AND lie_before IS NULL)) THEN {_DRIVE} END)) AS avg_drive,
                      ROUND(100.0 * SUM(CASE WHEN club = 'DR' AND lie_after = 'fairway' THEN 1 ELSE 0 END)
                            / NULLIF(SUM(CASE WHEN club = 'DR' AND lie_after IS NOT NULL THEN 1 ELSE 0 END), 0)) AS fairway_pct
               FROM shots WHERE player IS NOT NULL AND player != 'unknown' AND mulligan = 0 AND left_out = 0
               GROUP BY player ORDER BY longest_drive DESC""".format(_DRIVE=_DRIVE)
        ).fetchall()

    def player_profile(self, player: str, context: str | None = None) -> dict[str, Any]:
        """Season colour for a player: how much they've played, their most-used club, and their
        start-line tendency (the classic 'typical miss'). start_direction > 0 is right of the aim."""
        r = self.db.execute(
            """SELECT COUNT(*) AS shots, COUNT(DISTINCT session_id) AS nights,
                      AVG(CASE WHEN is_putt = 0 THEN start_direction END) AS avg_start_dir,
                      COUNT(CASE WHEN is_putt = 0 AND start_direction IS NOT NULL THEN 1 END) AS aimed
               FROM shots WHERE player = ? AND attributed = 1 AND mulligan = 0 AND left_out = 0""" + _ctx_sql(context),
            (player,),
        ).fetchone()
        fav = self.db.execute(
            """SELECT club, COUNT(*) AS n FROM shots
               WHERE player = ? AND attributed = 1 AND mulligan = 0 AND left_out = 0 AND club IS NOT NULL AND club NOT IN ('PT')"""
            + _ctx_sql(context) + " GROUP BY club ORDER BY n DESC LIMIT 1",
            (player,),
        ).fetchone()
        d = {k: r[k] for k in r.keys()}
        d["favorite_club"], d["favorite_club_n"] = (fav["club"], fav["n"]) if fav else (None, 0)
        return d

    def approach_proximity(self, player: str, yards: float, band: float = 15.0,
                           context: str | None = None) -> dict[str, Any]:
        """How close this player's full shots from about `yards` finish to the pin, this season."""
        r = self.db.execute(
            """SELECT COUNT(*) AS n, ROUND(AVG(finish_yards), 1) AS avg_finish_yards
               FROM shots WHERE player = ? AND attributed = 1 AND mulligan = 0 AND left_out = 0 AND is_putt = 0
                     AND finish_yards IS NOT NULL AND to_pin_yards BETWEEN ? AND ?""" + _ctx_sql(context),
            (player, yards - band, yards + band),
        ).fetchone()
        return {k: r[k] for k in r.keys()}

    def putting_make_rate(self, player: str, feet: float, band_feet: float = 2.5,
                          context: str | None = None) -> dict[str, Any]:
        """Holed rate on putts from about `feet` this season. An attempt is a putt whose result was
        resolved (holed / conceded / missed); made = holed. Unresolved putts (no result signal) are
        excluded so they don't read as misses, and so are the conceded tap-ins (outcome 'gimme'), which
        nobody hit."""
        lo, hi = max(0.0, feet - band_feet) / 3.0, (feet + band_feet) / 3.0   # feet -> yards
        r = self.db.execute(
            """SELECT COUNT(*) AS attempts, SUM(CASE WHEN outcome = 'holed' THEN 1 ELSE 0 END) AS made
               FROM shots WHERE player = ? AND attributed = 1 AND mulligan = 0 AND left_out = 0 AND is_putt = 1 AND outcome IS NOT NULL
                     AND outcome != 'gimme' AND to_pin_yards BETWEEN ? AND ?""" + _ctx_sql(context),
            (player, lo, hi),
        ).fetchone()
        return {k: r[k] for k in r.keys()}

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
        r = self.db.execute(
            """WITH hole_putts AS (
                   SELECT to_pin_yards,
                          ROW_NUMBER() OVER (PARTITION BY session_id, course, hole ORDER BY ts) AS putt_no,
                          COUNT(*) OVER (PARTITION BY session_id, course, hole) AS putts
                   FROM shots WHERE player = ? AND attributed = 1 AND mulligan = 0 AND left_out = 0 AND hole IS NOT NULL
                         AND (lie_before = 'green' OR (is_putt = 1 AND lie_before IS NULL))"""
            + _ctx_sql(context) + """
               )
               SELECT COUNT(*) AS holes, SUM(CASE WHEN putts >= 3 THEN 1 ELSE 0 END) AS three_putts
               FROM hole_putts WHERE putt_no = 1 AND to_pin_yards BETWEEN ? AND ?""",
            (player, lo, hi),
        ).fetchone()
        return {k: r[k] for k in r.keys()}

    _BEFORE_TODAY = "session_id NOT IN (SELECT id FROM sessions WHERE date = (SELECT date FROM sessions WHERE id = ?))"

    def approach_leave(self, player: str, lo_yards: float, hi_yards: float | None = None) -> dict[str, Any]:
        """Season baseline for the round sheet, before today: how far from the hole this player's full
        shots from `lo_yards` up to `hi_yards` finish when they reach the green (holed = 0)."""
        r = self.db.execute(
            f"""SELECT COUNT(*) AS n,
                       AVG(CASE WHEN outcome = 'holed' THEN 0 ELSE finish_yards END) AS avg_finish_yards
                FROM shots WHERE player = ? AND attributed = 1 AND mulligan = 0 AND left_out = 0 AND is_putt = 0
                      AND to_pin_yards >= ? AND to_pin_yards < ? AND finish_yards IS NOT NULL
                      AND (lie_after = 'green' OR outcome IN ('holed', 'conceded')) AND {self._BEFORE_TODAY}""",
            (player, lo_yards, hi_yards if hi_yards is not None else 1e9, self.session_id),
        ).fetchone()
        return {k: r[k] for k in r.keys()}

    def putt_band_rate(self, player: str, lo_feet: float, hi_feet: float | None = None) -> dict[str, Any]:
        """Season baseline for the round sheet, before today: putts from over `lo_feet` up to `hi_feet`.
        Conceded misses count as misses; the never-struck tap-ins ('gimme') don't count."""
        r = self.db.execute(
            f"""SELECT COUNT(*) AS attempts, SUM(CASE WHEN outcome = 'holed' THEN 1 ELSE 0 END) AS made
                FROM shots WHERE player = ? AND attributed = 1 AND mulligan = 0 AND left_out = 0 AND is_putt = 1
                      AND outcome IS NOT NULL AND outcome != 'gimme' AND to_pin_yards > ? AND to_pin_yards <= ?
                      AND {self._BEFORE_TODAY}""",
            (player, lo_feet / 3.0, (hi_feet if hi_feet is not None else 1e9) / 3.0, self.session_id),
        ).fetchone()
        return {k: r[k] for k in r.keys()}

    def length_norm(self, player: str, club: str | None = None) -> dict[str, Any]:
        """How far this player usually hits it (median total length, as GSPro measured it): drives (from
        the tee with 250+ yards to the pin), or one club's full swings. Which swings count as full never
        depends on how far that swing went (that would drop the good ones and keep the mishits): first
        the club's typical length over everything but chips, then only shots whose pin was well beyond
        that, so the player wasn't hitting a partial."""
        base = """SELECT total_yards, to_pin_yards FROM shots
                  WHERE player = ? AND attributed = 1 AND mulligan = 0 AND left_out = 0 AND is_putt = 0
                        AND total_yards IS NOT NULL AND total_yards > 0"""
        if club is None:
            rows = self.db.execute(base + " AND lie_before = 'tee' AND to_pin_yards >= 250", (player,)).fetchall()
            totals = [r["total_yards"] for r in rows]
        else:
            rows = self.db.execute(base + " AND club = ? AND to_pin_yards >= 40", (player, club)).fetchall()
            rough = _median([r["total_yards"] for r in rows])
            totals = [r["total_yards"] for r in rows if rough and r["to_pin_yards"] > rough * 1.15]
        return {"n": len(totals), "avg_total": _median(totals)}

    def shot_lengths(self, shot_ids: list[int]) -> dict[int, float]:
        """GSPro's total length for these shots, where the HUD showed it (none left out of the stats)."""
        ids = [i for i in shot_ids if i is not None]
        if not ids:
            return {}
        rows = self.db.execute(
            f"SELECT id, total_yards FROM shots WHERE total_yards IS NOT NULL AND left_out = 0 AND id IN ({','.join('?' * len(ids))})", ids)
        return {r["id"]: r["total_yards"] for r in rows}

    def record_form_mentions(self, players: list[str], topics: list[str]) -> None:
        """Round-sheet angles the booth used on these players, so later rounds can steer it elsewhere."""
        now = time.time()
        self.db.executemany("INSERT INTO form_mentions (session_id, ts, player, topic) VALUES (?, ?, ?, ?)",
                            [(self.session_id, now, p, t) for p in players for t in topics])
        self.db.commit()

    def record_aside(self, kind: str, topic: str, text: str) -> None:
        self.db.execute("INSERT INTO asides (session_id, ts, kind, topic, text) VALUES (?, ?, ?, ?, ?)",
                        (self.session_id, time.time(), kind, topic, text))
        self.db.commit()

    def record_news_heard(self, players: list[str], title: str) -> None:
        now = time.time()
        self.db.executemany("INSERT INTO news_heard (session_id, ts, player, title) VALUES (?, ?, ?, ?)",
                            [(self.session_id, now, p, title) for p in players])
        self.db.commit()

    def news_heard(self, players: list[str]) -> set[str]:
        """Titles of the stories any of these players has already heard from the booth."""
        if not players:
            return set()
        rows = self.db.execute(
            f"SELECT DISTINCT title FROM news_heard WHERE player IN ({','.join('?' * len(players))})", players)
        return {r["title"] for r in rows}

    def recent_asides(self, kind: str, topic: str, limit: int = 12) -> list[str]:
        """What the booth has already told on this topic (a course's trivia), most recent first."""
        rows = self.db.execute("SELECT text FROM asides WHERE kind = ? AND topic = ? ORDER BY ts DESC LIMIT ?",
                               (kind, topic, limit)).fetchall()
        return [r["text"] for r in rows]

    def recent_form_mentions(self, player: str, sessions: int = 3) -> list[str]:
        """Angles used on this player in their last few sessions before today, most used first."""
        rows = self.db.execute(
            f"""SELECT topic, COUNT(*) AS n FROM form_mentions
                WHERE player = ? AND {self._BEFORE_TODAY} AND session_id IN (
                    SELECT DISTINCT session_id FROM form_mentions WHERE player = ? AND {self._BEFORE_TODAY}
                    ORDER BY session_id DESC LIMIT ?)
                GROUP BY topic ORDER BY n DESC, topic""",
            (player, self.session_id, player, self.session_id, sessions),
        ).fetchall()
        return [r["topic"] for r in rows]

    def fairways_hit(self, player: str, context: str | None = None) -> dict[str, Any]:
        """Driving accuracy: of this player's driver tee shots this season, how many found the fairway.
        A per-player SHOT stat, so it works in a TWO-ball scramble too; it's empty only for the shared-
        ball formats (alternate, and a one-ball scramble), which are not attributed to a single player."""
        r = self.db.execute(
            """SELECT COUNT(*) AS tries, SUM(CASE WHEN lie_after = 'fairway' THEN 1 ELSE 0 END) AS hit
               FROM shots WHERE player = ? AND attributed = 1 AND mulligan = 0 AND left_out = 0 AND is_putt = 0 AND club = 'DR'
                     AND shot_number = 1 AND lie_after IS NOT NULL""" + _ctx_sql(context),
            (player,),
        ).fetchone()
        return {k: r[k] for k in r.keys()}

    def recovery_proximity(self, player: str, lies: tuple[str, ...], max_to_pin: float | None = None,
                           context: str | None = None) -> dict[str, Any]:
        """How close this player's shots from a given lie type (greenside sand, rough) finish to the pin,
        in yards. `max_to_pin` limits to greenside (a bunker within ~30 yd, not a fairway bunker). A
        per-player SHOT stat; empty for the shared-ball formats, which aren't attributed to a player."""
        placeholders = ",".join("?" for _ in lies)
        clause = " AND to_pin_yards <= ?" if max_to_pin is not None else ""
        params = [player, *lies] + ([max_to_pin] if max_to_pin is not None else [])
        r = self.db.execute(
            f"""SELECT COUNT(*) AS n, ROUND(AVG(finish_yards), 1) AS avg_finish_yards
                FROM shots WHERE player = ? AND attributed = 1 AND mulligan = 0 AND left_out = 0 AND is_putt = 0 AND finish_yards IS NOT NULL
                      AND lie_before IN ({placeholders}){clause}{_ctx_sql(context)}""",
            params,
        ).fetchone()
        return {k: r[k] for k in r.keys()}

    def record_round(self, *, course, fmt, side, team, holes, stats, strokes, net_to_par, hole_number,
                     hole_pars=None, is_league: bool = True, complete: bool = True,
                     first_nine: tuple[int, int | None] | None = None) -> None:
        """One side's round. A round quit early is recorded too (complete=0, holes_played short): its
        per-hole and per-try stats count, but it only counts as a round in the season totals once a
        full nine was played (see _COUNTS_AS_ROUND), and those totals are scored per nine holes.
        `first_nine` is (gross to par, net to par) over the first nine holes played, stored only for a
        part-played round so the season ranks the nine it finished rather than a prorated fraction."""
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
        self.db.execute(
            """INSERT INTO rounds (session_id, ended_at, date, course, format, side, team, holes, gross, par, strokes,
                   net_to_par, fir_hit, fir_tries, gir_hit, gir_tries, scramble_hit, scramble_tries, sand_hit,
                   sand_tries, putts, putt_holes, penalties, longest_drive, scores, hole_pars, source, is_league,
                   complete, holes_played, nine_to_par, nine_net_to_par)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.session_id, time.time(), time.strftime("%Y-%m-%d"), self.course_name(course), fmt, side, team, holes,
             stats.gross if stats.holes_scored else None, stats.par or None, strokes, net_to_par,
             *stats.fir, *stats.gir, *stats.scramble, *stats.sand,
             stats.putts if stats.putt_holes else None, stats.putt_holes, stats.penalties, stats.longest_drive,
             scores_json, pars_json, stats.source, int(is_league), int(complete), stats.holes_scored,
             nine, nine_net),
        )
        self.db.commit()

    def league_round_courses(self, team: str) -> list[str]:
        """The course of every full league round this team has posted (one row per round, not per side)."""
        rows = self.db.execute(
            "SELECT course, ended_at FROM rounds WHERE team = ? AND is_league = 1 AND complete = 1 "
            "AND course IS NOT NULL ORDER BY ended_at", (team,)).fetchall()
        out, last = [], None
        for course, ended in rows:
            if last is None or course != last[0] or ended - last[1] > 60:   # a round's sides land together
                out.append(course)
            last = (course, ended)
        return out

    def record_player_round(self, *, player: str, course: str | None, fmt: str | None, stats,
                            hole_number, hole_pars=None, is_league: bool = True, complete: bool = True,
                            nine_to_par: int | None = None) -> None:
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
                   putts, putt_holes, scores, hole_pars, is_league, complete, holes_played, nine_to_par)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.session_id, time.time(), player, self.course_name(course), fmt,
             *stats.fir, *stats.gir, *stats.scramble, *stats.sand,
             stats.putts if stats.putt_holes else None, stats.putt_holes, scores_json, pars_json, int(is_league),
             int(complete), stats.holes_scored, None if complete else nine_to_par),
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

    def season_rounds(self, side: str, context: str | None = None) -> dict[str, Any]:
        """Earlier rounds for a player or team, before tonight: complete ones, plus a full nine or more
        from one quit early, sliced by `context` (see _ctx_sql). `rounds` counts every scored round;
        `net_rounds` only those with a net score (a handicap known), which the net average and best
        come from - counted off the same expression they average, or a part-played round whose stored
        nine has a net score the round as a whole doesn't would be averaged in but never counted.
        Scores to par are per nine holes, so a 9 and an 18 compare."""
        r = self.db.execute(
            f"""SELECT COUNT(*) AS rounds, COUNT({_NET_PER_NINE}) AS net_rounds,
                      AVG({_NET_PER_NINE}) AS avg_net_to_par, MIN({_NET_PER_NINE}) AS best_net_to_par,
                      COUNT({_GROSS_PER_NINE}) AS gross_rounds, AVG({_GROSS_PER_NINE}) AS avg_gross_to_par, MIN({_GROSS_PER_NINE}) AS best_gross_to_par,
                      AVG(1.0 * putts / NULLIF(putt_holes, 0)) AS putts_per_hole,
                      AVG(1.0 * fir_hit / NULLIF(fir_tries, 0)) AS fir_rate,
                      AVG(1.0 * gir_hit / NULLIF(gir_tries, 0)) AS gir_rate
               FROM rounds WHERE side = ? AND session_id NOT IN (SELECT id FROM sessions WHERE date = (SELECT date FROM sessions WHERE id = ?)) AND gross IS NOT NULL
                     AND {_COUNTS_AS_ROUND}""" + _ctx_sql(context),
            (side, self.session_id),
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

    def record_prediction(self, commentator: str, kind: str, predicted: str, actual: str,
                          correct: bool) -> None:
        """A resolved commentator prediction, for the season call-accuracy stat."""
        self.db.execute(
            "INSERT INTO predictions (session_id, ts, commentator, kind, predicted, actual, correct) "
            "VALUES (?,?,?,?,?,?,?)",
            (self.session_id, time.time(), commentator, kind, predicted, actual, int(correct)),
        )
        self.db.commit()

    def prediction_record(self, commentator: str) -> dict[str, int]:
        """How many of a commentator's resolved calls were right, this season (across nights)."""
        r = self.db.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(correct), 0) AS hits FROM predictions "
            "WHERE commentator = ? AND correct IS NOT NULL",
            (commentator,),
        ).fetchone()
        return {"total": r["total"], "hits": r["hits"]}

    def close(self) -> None:
        self.db.close()

