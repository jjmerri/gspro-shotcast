"""Season history in SQLite: every shot and every line, kept across league nights.

One file, no server. Open it with any SQLite viewer (e.g. DB Browser for SQLite) to browse or
export. Replays and evals use an in-memory database so made-up shots never reach the season.
"""

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
    carry_raw_tile_yards REAL,                   -- GSPro CARRY (raw): flat-ground, incl. lie and altitude
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
    mulligan INTEGER NOT NULL DEFAULT 0          -- 1 = GSPro took the stroke back; excluded from every stat
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
    holes_played INTEGER                         -- holes actually scored (NULL on older rows: use holes)
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
    holes_played INTEGER                         -- holes this player actually scored
);
CREATE INDEX IF NOT EXISTS shots_player ON shots(player);
CREATE INDEX IF NOT EXISTS shots_lie ON shots(lie_before);
"""


# A round row counts toward the season's round totals (rounds played, average/best net) when it was
# finished, or quit early after at least a full nine. Older rows have no holes_played: use holes.
_COUNTS_AS_ROUND = "(complete = 1 OR COALESCE(holes_played, holes) >= 9)"
# Net to par per nine holes, so 9- and 18-hole rounds (and a nine inside an abandoned 18) compare.
_NET_PER_NINE = "(net_to_par * 9.0 / COALESCE(NULLIF(holes_played, 0), holes))"


# A drive's length: the total distance (carry plus roll) where GSPro's tile was read, else the carry.
_DRIVE = "COALESCE(total_yards, carry_game_yards, carry_raw)"
# In the queries below, a drive is the driver off the tee (the hole's first shot, from the tee box,
# or neither read), never a putt: a late club read can tag a putt "DR", and one such row dragged a
# driving average to 208. And "earlier weeks" means earlier DAYS: every booth restart starts a new session,
# so excluding only the current session counted tonight's earlier holes as history.


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
    def __init__(self, path: str | None) -> None:
        self.db = sqlite3.connect(path or ":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self._backfill_finishes()
        self.session_id = self.db.execute(
            "INSERT INTO sessions (started_at, date) VALUES (?, ?)", (time.time(), time.strftime("%Y-%m-%d"))
        ).lastrowid
        self.db.commit()

    def _migrate(self) -> None:
        """Add columns a pre-existing season.db is missing, so an older file keeps working."""
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(shots)")}
        added = {"to_pin_yards": "REAL", "finish_yards": "REAL", "gimme_yards": "REAL",
                 "outcome": "TEXT", "fmt": "TEXT", "attributed": "INTEGER NOT NULL DEFAULT 1",
                 "is_league": "INTEGER NOT NULL DEFAULT 1", "mulligan": "INTEGER NOT NULL DEFAULT 0"}
        for col, decl in added.items():
            if col not in have:
                self.db.execute(f"ALTER TABLE shots ADD COLUMN {col} {decl}")
        have_rounds = {r["name"] for r in self.db.execute("PRAGMA table_info(rounds)")}
        rounds_added = {"scramble_hit": "INTEGER", "scramble_tries": "INTEGER", "sand_hit": "INTEGER",
                        "sand_tries": "INTEGER", "hole_pars": "TEXT", "is_league": "INTEGER NOT NULL DEFAULT 1",
                        "complete": "INTEGER NOT NULL DEFAULT 1", "holes_played": "INTEGER"}
        for col, decl in rounds_added.items():
            if col not in have_rounds:
                self.db.execute(f"ALTER TABLE rounds ADD COLUMN {col} {decl}")
        # player_rounds gained per-hole scores/pars and putts after its first version (SCHEMA creates
        # the table; an older file has it without these columns).
        have_pr = {r["name"] for r in self.db.execute("PRAGMA table_info(player_rounds)")}
        pr_added = {"putts": "INTEGER", "putt_holes": "INTEGER", "scores": "TEXT", "hole_pars": "TEXT",
                    "is_league": "INTEGER NOT NULL DEFAULT 1", "complete": "INTEGER NOT NULL DEFAULT 1",
                    "holes_played": "INTEGER"}
        for col, decl in pr_added.items():
            if col not in have_pr:
                self.db.execute(f"ALTER TABLE player_rounds ADD COLUMN {col} {decl}")
        self.db.commit()

    def record_shot(
        self, *, player: str | None, hole: str | None, shot: Shot, before: Hud, after: Hud, tags: list[str],
        carry_game_yards: float | None, total_yards: float | None, offline_yards: float | None,
        course: str | None = None, carry_raw_tile_yards: float | None = None,
        to_pin_yards: float | None = None, finish_yards: float | None = None,
        gimme_yards: float | None = None, outcome: str | None = None, fmt: str | None = None,
        attributed: bool = True, is_league: bool = True,
    ) -> int:
        cur = self.db.execute(
            """INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, ball_speed,
                   launch_angle, start_direction, total_spin, spin_axis, club_speed, carry_raw, units,
                   lie_before, slope_updown, slope_side, slope_side_degrees, elevation_yards, to_pin,
                   score_to_par, lie_after, carry_raw_tile_yards, carry_game_yards, total_yards, offline_yards,
                   tags, raw, to_pin_yards, finish_yards, gimme_yards, outcome, fmt, attributed, is_league)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.session_id, shot.timestamp, player, course, hole, before.shot_number or after.shot_number, shot.club,
             int(shot.is_putt), shot.ball_speed, shot.vla, shot.hla, shot.total_spin, shot.spin_axis,
             shot.club_speed, shot.carry, shot.units, before.lie, before.slope_updown, before.slope_side,
             before.slope_side_degrees, before.elevation_yards, before.to_pin or after.to_pin,
             before.score_to_par or after.score_to_par, after.lie, carry_raw_tile_yards, carry_game_yards,
             total_yards, offline_yards,
             json.dumps(tags), json.dumps(shot.raw),
             to_pin_yards, finish_yards, gimme_yards, outcome, fmt, int(attributed), int(is_league)),
        )
        self._unhole_if_played_on(cur.lastrowid, later=False)
        self._fill_previous_finish(cur.lastrowid)
        self.db.commit()
        return cur.lastrowid

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

    def set_shot_start(self, shot_id: int | None, shot_number: int | None, lie_before: str | None) -> None:
        """A re-hit after a mulligan is the same stroke from the same spot as the one it replaced."""
        if shot_id is not None:
            self.db.execute("UPDATE shots SET shot_number = ?, lie_before = ? WHERE id = ?",
                            (shot_number, lie_before, shot_id))
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
            self.db.executemany("UPDATE shots SET fmt = ? WHERE id = ?", [(fmt, i) for i in shot_ids])
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
        corrected for pin elevation, course, launch angle, base). Base is GSPro's CARRY (raw) where
        it was read, else the launch monitor's carry."""
        from .carry_model import correct_for_elevation
        from .facts import to_yards

        rows = self.db.execute(
            """SELECT lie_before, carry_raw, units, carry_raw_tile_yards, carry_game_yards, elevation_yards,
                      to_pin, course, launch_angle FROM shots
               WHERE is_putt = 0 AND lie_before IS NOT NULL AND carry_game_yards > 0
               ORDER BY ts"""
        ).fetchall()
        out = []
        for r in rows:
            if r["carry_raw_tile_yards"]:
                base, carry = "gspro", r["carry_raw_tile_yards"]
            elif r["carry_raw"]:
                base = "lm"
                carry = r["carry_raw"] * (1 if (r["units"] or "y").lower().startswith("y") else 1.0936)
            else:
                continue
            game = correct_for_elevation(r["carry_game_yards"], r["elevation_yards"], to_yards(r["to_pin"]), carry)
            out.append((r["lie_before"], carry, game, r["course"], r["launch_angle"], base))
        return out

    def season_stats(self, player: str, context: str | None = None) -> dict[str, Any]:
        """Season numbers for one player, in yards, excluding tonight so 'season best' means before tonight."""
        r = self.db.execute(
            """SELECT COUNT(*) AS shots,
                      MAX(CASE WHEN club = 'DR' AND is_putt = 0 AND (shot_number = 1 OR lie_before = 'tee' OR (shot_number IS NULL AND lie_before IS NULL)) THEN {_DRIVE} END) AS longest_drive,
                      AVG(CASE WHEN club = 'DR' AND is_putt = 0 AND (shot_number = 1 OR lie_before = 'tee' OR (shot_number IS NULL AND lie_before IS NULL)) THEN {_DRIVE} END) AS avg_drive,
                      COUNT(DISTINCT session_id) AS nights
               FROM shots WHERE player = ? AND session_id NOT IN (SELECT id FROM sessions WHERE date = (SELECT date FROM sessions WHERE id = ?)) AND mulligan = 0""".format(_DRIVE=_DRIVE) + _ctx_sql(context),
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
               WHERE sh.is_league = 1 AND sh.attributed = 1 AND sh.mulligan = 0 AND sh.is_putt = 0
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
                     AND mulligan = 0"""
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
                "SELECT scores FROM rounds WHERE course = ? AND scores IS NOT NULL" + _ctx_sql(context), (course,)):
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
               FROM shots WHERE player IS NOT NULL AND player != 'unknown' AND mulligan = 0
               GROUP BY player ORDER BY longest_drive DESC""".format(_DRIVE=_DRIVE)
        ).fetchall()

    def player_profile(self, player: str, context: str | None = None) -> dict[str, Any]:
        """Season colour for a player: how much they've played, their most-used club, and their
        start-line tendency (the classic 'typical miss'). start_direction > 0 is right of the aim."""
        r = self.db.execute(
            """SELECT COUNT(*) AS shots, COUNT(DISTINCT session_id) AS nights,
                      AVG(CASE WHEN is_putt = 0 THEN start_direction END) AS avg_start_dir,
                      COUNT(CASE WHEN is_putt = 0 AND start_direction IS NOT NULL THEN 1 END) AS aimed
               FROM shots WHERE player = ? AND attributed = 1 AND mulligan = 0""" + _ctx_sql(context),
            (player,),
        ).fetchone()
        fav = self.db.execute(
            """SELECT club, COUNT(*) AS n FROM shots
               WHERE player = ? AND attributed = 1 AND mulligan = 0 AND club IS NOT NULL AND club NOT IN ('PT')"""
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
               FROM shots WHERE player = ? AND attributed = 1 AND mulligan = 0 AND is_putt = 0
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
               FROM shots WHERE player = ? AND attributed = 1 AND mulligan = 0 AND is_putt = 1 AND outcome IS NOT NULL
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
                   FROM shots WHERE player = ? AND attributed = 1 AND mulligan = 0 AND hole IS NOT NULL
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
                FROM shots WHERE player = ? AND attributed = 1 AND mulligan = 0 AND is_putt = 0
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
                FROM shots WHERE player = ? AND attributed = 1 AND mulligan = 0 AND is_putt = 1
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
                  WHERE player = ? AND attributed = 1 AND mulligan = 0 AND is_putt = 0
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
        """GSPro's total length for these shots, where the HUD showed it."""
        ids = [i for i in shot_ids if i is not None]
        if not ids:
            return {}
        rows = self.db.execute(
            f"SELECT id, total_yards FROM shots WHERE total_yards IS NOT NULL AND id IN ({','.join('?' * len(ids))})", ids)
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
               FROM shots WHERE player = ? AND attributed = 1 AND mulligan = 0 AND is_putt = 0 AND club = 'DR'
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
                FROM shots WHERE player = ? AND attributed = 1 AND mulligan = 0 AND is_putt = 0 AND finish_yards IS NOT NULL
                      AND lie_before IN ({placeholders}){clause}{_ctx_sql(context)}""",
            params,
        ).fetchone()
        return {k: r[k] for k in r.keys()}

    def record_round(self, *, course, fmt, side, team, holes, stats, strokes, net_to_par, hole_number,
                     hole_pars=None, is_league: bool = True, complete: bool = True) -> None:
        """One side's round. A round quit early is recorded too (complete=0, holes_played short): its
        per-hole and per-try stats count, but it only counts as a round in the season totals once a
        full nine was played (see _COUNTS_AS_ROUND), and those totals are scored per nine holes."""
        # Key per-hole maps by real hole number for cross-round per-hole stats; drop holes whose number
        # is unknown rather than falling back to the round sequence (which would collide with hole 1).
        scores_json = json.dumps({str(h): v for k, v in stats.scores.items() if (h := hole_number(k)) is not None})
        # None (not "{}") when no hole numbers resolved - an unconfirmed start hole makes hole_number()
        # return None for every seq - so team_hole_stats' `hole_pars IS NOT NULL` skips the empty row.
        pars = {str(h): p for k, p in (hole_pars or {}).items() if (h := hole_number(k)) is not None and p}
        pars_json = json.dumps(pars) if pars else None
        self.db.execute(
            """INSERT INTO rounds (session_id, ended_at, date, course, format, side, team, holes, gross, par, strokes,
                   net_to_par, fir_hit, fir_tries, gir_hit, gir_tries, scramble_hit, scramble_tries, sand_hit,
                   sand_tries, putts, putt_holes, penalties, longest_drive, scores, hole_pars, source, is_league,
                   complete, holes_played)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.session_id, time.time(), time.strftime("%Y-%m-%d"), course, fmt, side, team, holes,
             stats.gross if stats.holes_scored else None, stats.par or None, strokes, net_to_par,
             *stats.fir, *stats.gir, *stats.scramble, *stats.sand,
             stats.putts if stats.putt_holes else None, stats.putt_holes, stats.penalties, stats.longest_drive,
             scores_json, pars_json, stats.source, int(is_league), int(complete), stats.holes_scored),
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
                            hole_number, hole_pars=None, is_league: bool = True, complete: bool = True) -> None:
        """One individual player's round for per-player season stats: FIR/GIR/scrambling/sand-save
        counts, putts, and the per-hole scores and pars (keyed by real hole number, like record_round)
        for the scores-vs-par stats. Scrambling/sand and the hole-score stats are only meaningful in an
        individual-ball format; they're stored as computed and gated out for the shared-ball formats at
        query time by `fmt`."""
        scores_json = json.dumps({str(h): v for k, v in stats.scores.items() if (h := hole_number(k)) is not None})
        pars = {str(h): p for k, p in (hole_pars or {}).items() if (h := hole_number(k)) is not None and p}
        pars_json = json.dumps(pars) if pars else None
        self.db.execute(
            """INSERT INTO player_rounds (session_id, ended_at, player, course, fmt, fir_hit, fir_tries,
                   gir_hit, gir_tries, scramble_hit, scramble_tries, sand_hit, sand_tries,
                   putts, putt_holes, scores, hole_pars, is_league, complete, holes_played)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.session_id, time.time(), player, course, fmt,
             *stats.fir, *stats.gir, *stats.scramble, *stats.sand,
             stats.putts if stats.putt_holes else None, stats.putt_holes, scores_json, pars_json, int(is_league),
             int(complete), stats.holes_scored),
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
        from one quit early. The net scores are per nine holes, so a 9 and an 18 compare."""
        r = self.db.execute(
            f"""SELECT COUNT(*) AS rounds, AVG({_NET_PER_NINE}) AS avg_net_to_par, MIN({_NET_PER_NINE}) AS best_net_to_par,
                      AVG(1.0 * putts / NULLIF(putt_holes, 0)) AS putts_per_hole,
                      AVG(1.0 * fir_hit / NULLIF(fir_tries, 0)) AS fir_rate,
                      AVG(1.0 * gir_hit / NULLIF(gir_tries, 0)) AS gir_rate
               FROM rounds WHERE side = ? AND session_id NOT IN (SELECT id FROM sessions WHERE date = (SELECT date FROM sessions WHERE id = ?)) AND net_to_par IS NOT NULL
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
               FROM rounds WHERE side = ? AND course = ? AND session_id NOT IN (SELECT id FROM sessions WHERE date = (SELECT date FROM sessions WHERE id = ?)) AND net_to_par IS NOT NULL
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

