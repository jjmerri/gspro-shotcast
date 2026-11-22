"""What the store's parts share: the schema, constants and small helpers (the top of the old store.py,
as it was)."""

import logging
import sqlite3

from ..models import Shot

log = logging.getLogger("commentator.store")   # (one logger for the whole package)

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
    total_from_log INTEGER NOT NULL DEFAULT 0,   -- 1 = total_yards came from GSPro's logged positions, not the
                                                 -- HUD; 2 = from GSPro's round file (shot_tracks)
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
    is_tournament INTEGER NOT NULL DEFAULT 0,    -- 1 = a tournament round (Simulator Golf Tour), never a league one
    mulligan INTEGER NOT NULL DEFAULT 0,         -- 1 = GSPro took the stroke back; excluded from every stat
    left_out INTEGER NOT NULL DEFAULT 0,         -- 1 = left out of the stats by hand (a misread, a practice
                                                 -- swing); still a stroke on the card, unlike a mulligan
    excluded INTEGER NOT NULL DEFAULT 0,         -- its round's: 1 = excluded from the stats, 2 = deleted (stats_web)
    shared_ball INTEGER NOT NULL DEFAULT 0,      -- 1 = a team format where GSPro copies balls between
                                                 -- teammates, so the next stroke may not be from this ball
    -- Club delivery, in degrees (face impact in mm from centre), only when the monitor reported club
    -- data for the shot (ContainsClubData); NULL otherwise, never a placeholder zero.
    club_path REAL, angle_of_attack REAL, face_to_target REAL, dynamic_loft REAL,
    face_impact_h REAL, face_impact_v REAL,
    -- From GSPro's log: where the ball sat for this shot, in the course's world metres (y is height;
    -- NULL for a tee shot, which GSPro sets up without logging it, unless its round file gave it: use
    -- hole_layouts' tee), and the player's last minimap aim click before it, in minimap units (NULL
    -- when the aim wasn't moved).
    start_x REAL, start_y REAL, start_z REAL, aim_map_x REAL, aim_map_y REAL,
    -- Where the ball went into a penalty area (water, OB), world metres, from GSPro's log; else NULL.
    hazard_x REAL, hazard_y REAL, hazard_z REAL,
    handed TEXT                                  -- 'RH' / 'LH' as GSPro had the player set; NULL if unknown
);
CREATE TABLE IF NOT EXISTS shot_tracks (             -- GSPro's own record of a stroke (currentRound.dat, 3.59+; see
    shot_id INTEGER PRIMARY KEY REFERENCES shots(id),   -- round_file.py), applied over the booth's reads
    gspro_shot_id TEXT NOT NULL UNIQUE,
    land_x REAL, land_y REAL, land_z REAL,       -- the touchdown (world metres), for a shot that flew
    end_x REAL, end_y REAL, end_z REAL,          -- where it stopped; NULL when a penalty followed
    flight TEXT,                                 -- JSON [[x, y, z], ...] from the strike to the touchdown
    flight_s REAL,                               -- the flight's seconds
    roll TEXT,                                   -- JSON [[x, y, z, seconds], ...] from the touchdown (a putt's run)
    carry_yards REAL, total_yards REAL, finish_yards REAL, lie_after TEXT,  -- what it set on the shot
    was TEXT                                     -- JSON of the shot's columns it replaced, put back if the
);                                               -- stroke turns out to be another shot's
CREATE TABLE IF NOT EXISTS hole_layouts (            -- each hole's geometry, logged by GSPro when a round is complete
    id INTEGER PRIMARY KEY,
    session_id INTEGER REFERENCES sessions(id),  -- NULL when backfilled from a log no session matched
    ts REAL,
    course TEXT NOT NULL,
    hole INTEGER NOT NULL,                       -- the course's hole number
    par INTEGER,
    tee_x REAL, tee_y REAL, tee_z REAL,          -- world metres, as shots.start_*
    pin_x REAL, pin_y REAL, pin_z REAL,          -- this round's pin
    wp_x REAL, wp_y REAL, wp_z REAL,             -- the course designer's aim waypoint (not the player's aim)
    tee_box TEXT                                 -- the course file's box at the tee (Black, Blue, ...); NULL
                                                 -- until matched (course_layout.name_tee_boxes)
);
CREATE TABLE IF NOT EXISTS logged_pins (             -- pins GSPro 3.59+ logged setting a hole up, as seen: which
    id INTEGER PRIMARY KEY,                      -- hole is worked out from the shots (course_layout.fill_missing)
    session_id INTEGER REFERENCES sessions(id),
    ts REAL NOT NULL,
    x REAL NOT NULL, y REAL NOT NULL, z REAL NOT NULL   -- world metres, as hole_layouts.pin_*
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
CREATE TABLE IF NOT EXISTS round_folders (          -- the GSPro course folder a round was surely played from
    record_uuid TEXT PRIMARY KEY,                -- its record (shots.record_uuid)
    folder TEXT NOT NULL                         -- e.g. balsams_gsp: GSPro's round file's CourseKey
);
CREATE TABLE IF NOT EXISTS tee_lengths (           -- each tee box's whole-course length, from GSPro's course files
    course TEXT NOT NULL,                        -- as the season names the course (shots.course)
    tee TEXT NOT NULL,                           -- the course file's tee box: Black, Blue, ..., Par3; '' when
                                                 -- its file was read and no box had every hole (not to read again)
    yards INTEGER NOT NULL,                      -- summed over its holes
    holes INTEGER NOT NULL,
    PRIMARY KEY (course, tee)
);
CREATE TABLE IF NOT EXISTS lines (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    shot_id INTEGER REFERENCES shots(id),
    ts REAL NOT NULL,
    kind TEXT NOT NULL,                          -- preshot / flight / shot / segment
    speaker TEXT NOT NULL,
    text TEXT NOT NULL,
    excluded INTEGER NOT NULL DEFAULT 0,         -- its round's: 1 = excluded from the stats, 2 = deleted (stats_web)
    aired_at REAL,                               -- when it started playing (NULL: not aired, or logged before this was)
    aired_seconds REAL                           -- ...and how long it played
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
    is_tournament INTEGER NOT NULL DEFAULT 0,    -- 1 = a tournament round (Simulator Golf Tour), never a league one
    complete INTEGER NOT NULL DEFAULT 1,         -- 0 = quit early in GSPro (holes_played < holes)
    holes_played INTEGER,                        -- holes actually scored (NULL on older rows: use holes)
    nine_to_par INTEGER,                         -- gross to par over the first nine played, set only on a
    nine_net_to_par INTEGER,                     -- part-played round; the season ranks per nine, and
                                                 -- prorating eleven holes onto nine invents a score
    players TEXT,                                -- JSON: the side's players (NULL on older rows)
    excluded INTEGER NOT NULL DEFAULT 0,         -- 1 = excluded from the stats on the local page (stats_web)
    -- GSPro's physics settings as its log writes them (4.0+, NULL before): raw values, scale unconfirmed.
    green_firmness INTEGER, fairway_firmness INTEGER, course_type INTEGER,
    launch_monitor TEXT                          -- GSPro's "LM Type" for the round, e.g. UNEEKOR EYEXO2
);
CREATE TABLE IF NOT EXISTS week_results (       -- a league team's finished week, by the sim's count
    id INTEGER PRIMARY KEY,
    ended_at REAL NOT NULL,
    week INTEGER NOT NULL,
    end_date TEXT,                               -- the week's deadline: week numbers repeat each season
    team TEXT NOT NULL,
    net INTEGER NOT NULL,                        -- team net to par, scored the way Squabbit does
    round_id INTEGER,                            -- one of the team's rounds rows: it counts while that does
    holes TEXT                                   -- JSON {hole number: team net to par on it}, for the recap's story
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
    is_tournament INTEGER NOT NULL DEFAULT 0,    -- 1 = a tournament round (Simulator Golf Tour), never a league one
    complete INTEGER NOT NULL DEFAULT 1,         -- 0 = quit early in GSPro
    holes_played INTEGER,                        -- holes this player actually scored
    nine_to_par INTEGER,                         -- gross to par over the first nine played, on a
                                                 -- part-played round only (see the rounds table)
    excluded INTEGER NOT NULL DEFAULT 0          -- its round's: 1 = excluded from the stats, 2 = deleted (stats_web)
);
CREATE INDEX IF NOT EXISTS shots_player ON shots(player);
CREATE INDEX IF NOT EXISTS shots_lie ON shots(lie_before);
"""

# The carry model learned from the shots before `before_ts` (Store.carry_model), so a start doesn't go
# over every shot of every season again. Any write to a shot it learned from, or to a session's shots it
# covers (a stroke's next one says where it came to rest), deletes it, from any connection.
CARRY_SNAPSHOT_VERSION = 1   # bump when carry_pairs or CarryModel.observe learn differently
# What carry_pairs reads, so a write to any other column (a round's record, the map, a putt's outcome)
# leaves the snapshot be.
_CARRY_COLUMNS = ("id, session_id, player, hole, shot_number, is_putt, mulligan, fmt, lie_before, carry_raw, units, "
                  "carry_raw_tile_yards, carry_game_yards, elevation_yards, to_pin, course, launch_angle, "
                  "finish_yards, ts, attributed, shared_ball, left_out, tree_hits, excluded")


def _carry_covered(ts: str, sid: str) -> str:
    t = "(SELECT before_ts FROM carry_snapshot)"
    return f"({ts} < {t} OR EXISTS (SELECT 1 FROM shots c WHERE c.session_id = {sid} AND c.ts < {t}))"


_ANY_SNAPSHOT = "EXISTS (SELECT 1 FROM carry_snapshot)"
_DROP_SNAPSHOT = "BEGIN DELETE FROM carry_snapshot; END;"
CARRY_SNAPSHOT = f"""
CREATE TABLE IF NOT EXISTS carry_snapshot (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    key TEXT NOT NULL,                           -- CARRY_SNAPSHOT_VERSION and the configured altitudes
    before_ts REAL NOT NULL,
    state TEXT NOT NULL                          -- CarryModel.state(), JSON
);
CREATE TRIGGER IF NOT EXISTS shots_carry_insert AFTER INSERT ON shots
WHEN {_ANY_SNAPSHOT} AND {_carry_covered("NEW.ts", "NEW.session_id")} {_DROP_SNAPSHOT}
CREATE TRIGGER IF NOT EXISTS shots_carry_delete AFTER DELETE ON shots
WHEN {_ANY_SNAPSHOT} AND {_carry_covered("OLD.ts", "OLD.session_id")} {_DROP_SNAPSHOT}
CREATE TRIGGER IF NOT EXISTS shots_carry_update AFTER UPDATE OF {_CARRY_COLUMNS} ON shots
WHEN {_ANY_SNAPSHOT} AND ({_carry_covered("OLD.ts", "OLD.session_id")} OR {_carry_covered("NEW.ts", "NEW.session_id")})
{_DROP_SNAPSHOT}
CREATE TRIGGER IF NOT EXISTS shots_carry_renumber AFTER UPDATE ON shots   -- (SET rowid = isn't an UPDATE OF id)
WHEN {_ANY_SNAPSHOT} AND NEW.id IS NOT OLD.id
     AND ({_carry_covered("OLD.ts", "OLD.session_id")} OR {_carry_covered("NEW.ts", "NEW.session_id")})
{_DROP_SNAPSHOT}
CREATE TRIGGER IF NOT EXISTS shots_carry_replace BEFORE INSERT ON shots
WHEN {_ANY_SNAPSHOT} AND EXISTS (SELECT 1 FROM shots s WHERE (s.id = NEW.id OR s.uid = NEW.uid)
                                 AND {_carry_covered("s.ts", "s.session_id")}) {_DROP_SNAPSHOT}
CREATE TRIGGER IF NOT EXISTS shots_carry_replace_by BEFORE UPDATE ON shots
WHEN {_ANY_SNAPSHOT} AND (NEW.id IS NOT OLD.id OR NEW.uid IS NOT OLD.uid)
     AND EXISTS (SELECT 1 FROM shots s WHERE ((NEW.id IS NOT OLD.id AND s.id = NEW.id) OR (NEW.uid IS NOT OLD.uid AND s.uid = NEW.uid))
                 AND {_carry_covered("s.ts", "s.session_id")}) {_DROP_SNAPSHOT}
"""

# Made by _migrate once their columns are sure to exist: name -> table(columns).
INDEXES = {
    "shots_session_ts": "shots(session_id, ts)",
    "shots_ts": "shots(ts)",
    "lines_session": "lines(session_id)",
    "lines_shot": "lines(shot_id)",
    "rounds_session": "rounds(session_id)",
    "player_rounds_session": "player_rounds(session_id)",
    "shots_course": "shots(course COLLATE NOCASE)",   # course_name: as each round is saved, and in play
}


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
# A tee shot whose club was never read (a HUD that hides the club box before the impact read) counts
# too when the pin was DRIVE_MIN_TO_PIN+ away, so not a par 3: a 320-yard drive was missing from the
# stats of a booth that read no club all season. Its tee shot must be known (shot 1, or the tee lie).
DRIVE_MIN_TO_PIN = 250
_TEE_SHOT = "(shot_number = 1 OR lie_before = 'tee' OR (shot_number IS NULL AND lie_before IS NULL))"
UNKNOWN_CLUB_DRIVE_SQL = (f"(club IS NULL AND (shot_number = 1 OR lie_before = 'tee')"
                          f" AND to_pin_yards >= {DRIVE_MIN_TO_PIN})")
DRIVE_SQL = f"(is_putt = 0 AND ((club = 'DR' AND {_TEE_SHOT}) OR {UNKNOWN_CLUB_DRIVE_SQL}))"


def is_drive(club: str | None, is_putt: bool, shot_number: int | None, lie: str | None,
             to_pin_yards: float | None) -> bool:
    """DRIVE_SQL for a shot in hand."""
    if is_putt:
        return False
    lie = (lie or "").lower()
    if (club or "").upper() == "DR":
        return shot_number == 1 or lie == "tee" or (shot_number is None and not lie)
    return (not club and (shot_number == 1 or lie == "tee")
            and to_pin_yards is not None and to_pin_yards >= DRIVE_MIN_TO_PIN)


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


# The booth's closing words on a round come once it's recorded: the lines on no shot this long after a
# round's end are still its (rounds in a session are far more apart), excluded or deleted with it.
CLOSER_SECONDS = 180


def _ctx_sql(context: str | None) -> str:
    """A WHERE-clause fragment slicing a stat by round context: None = lifetime (league + practice),
    'league' = league rounds only (the 'under pressure' slice), 'practice' = every other round (a
    tournament one included: the booth's split is league play or not; the stats site splits three ways).
    Appended to a query whose table carries is_league (shots / rounds / player_rounds), which is 1/0
    with legacy rows defaulted to 1, so no NULL handling is needed. No bind params (literal filter).
    Every context leaves out the rounds excluded on the stats page (and their shots)."""
    if context == "league":
        return " AND excluded = 0 AND is_league = 1"
    if context == "practice":
        return " AND excluded = 0 AND is_league = 0"
    return " AND excluded = 0"


def _median(xs: list[float]) -> float | None:
    xs = sorted(xs)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def planner_counts_stale(db: sqlite3.Connection, grown: float = 2.0, least: int = 1000) -> bool:
    """Whether a table with indexes has `grown` times the rows ANALYZE last counted (or has `least`
    rows and was never counted). SQLite plans by those counts: kept from a small season, a lookup of
    a few hundred shots' lines reads every line instead of using the index (a 6-year season's build
    went from seconds to minutes). ANALYZE again then: a few times in a file's life (a 6-year
    season's takes 0.3 s)."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_stat1'").fetchone():
        counted: dict[str, int] = {}
    else:
        counted = {t: n for t, n in db.execute(
            "SELECT tbl, MAX(CAST(stat AS INTEGER)) FROM sqlite_stat1 GROUP BY tbl")}
    tables = [r[0] for r in db.execute("SELECT DISTINCT tbl_name FROM sqlite_master WHERE type = 'index'"
                                       " AND sql IS NOT NULL AND tbl_name NOT LIKE 'sqlite_%'")]
    for t in tables:
        rows = db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        if rows >= least and rows > grown * counted.get(t, 0):
            return True
    return False


# The season file's format (PRAGMA user_version) once upgraded: 6 is round_record.migrate's, 7 the
# stranded openers' repair (_repair_stranded_openers), 8 the picked-up holes' (_repair_picked_up_holes).
# Raise it with any change to the file an older release would trip over: a file older than this is copied
# before it's upgraded (backup.before_upgrade).
FORMAT = 8


class SeasonCopyFailed(RuntimeError):
    """A season file in an older format couldn't be copied, so it wasn't opened (or upgraded)."""
