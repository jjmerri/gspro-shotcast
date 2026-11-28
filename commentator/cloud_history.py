"""A paired booth's playing history from Shotcast Cloud (shotcast-cloud's docs/dev/history-pack.md).

A cloud profile's season file holds only what this bay recorded since pairing, so when players are
starting the booth asks the cloud for each one's history (`POST /api/devices/history`, one name a
call): the rows the cloud keeps of every round of theirs it counts, at any of the tenant's bays, as
the PCs sent them. Each answer goes into a file of its own beside the season file (`FILE`), never
into the season file: the uploader, the repairs and the stats page never meet a cloud row, and an
older Shotcast never sees one.

The booth reads its past from both (`attach`): the history file's rows, and the season file's own
whose round the history file doesn't hold for that player (practice stretches, rounds not sent or
not counted yet, rounds the cloud doesn't give back, and everything when a call failed: the season
file drops only rounds the history file already holds, below). Every loaded row is practice (`is_league` 0):
a paired booth has no league, so its opener asks for the practice slice.

Asked at the setup screen's names (`DEBOUNCE_S` after they settle) and again at the tee for a name
the setup didn't have (`Asks`): at most `AT_ONCE` calls at once, a call for a name no longer set up
dropped, each given `CALL_S` (a cold server's answer still lands for the round); the opener waits
for its players' calls `BOUND_S` at most, then talks from what it has. A failed call keeps the last
answer; a name not asked for in `AGE_DAYS` has its answer dropped. Self-hosted (or replaying), none
of this runs: nothing is attached and every read is of the season file.

And the PC stops keeping what the cloud gives back (`forget`, as the booth starts): the shots, cards
and side rows of each round the cloud has from an earlier day, with no rows added since it was sent,
that the history file already holds for every one of its players (so the booth reads nothing
different without them); and of each practice stretch (which the cloud never has) once it's
PRACTICE_DAYS old. The lines (the phrase memory), the sessions, the booth's memory tables, the course
caches (logged pins included: a session's, not a round's) and the round records with their upload
marks stay. Only in a paired profile's own season file, bound to its pairing, while the API accepts
it and the cloud answers for its players; a self-hosted file, a replay, or a refused pairing loses
nothing. Each answer's handicap index is kept with it (`indexes`), the booth's from its start. The
profile's local backups keep what they had, ageing out on their own schedule.

Accepted, as it stands:
- a round the cloud withholds (deleted, hidden, its player erased) is still read from this PC's own
  season file while it's there; once the history file held it and it went, it's gone with the next
  answer that leaves it out, as the cloud means;
- a round the cloud doesn't give back (a venue's walk-ins, a name it resolves to nobody, past its
  bounds) is never dropped: the season file keeps it for good;
- what changed in a sent round's rows without adding one (a stroke's values GSPro's round file
  corrected after the uploader's ceiling, a startup repair of a card's stats) goes with it: the
  cloud's copy is the round. Shot tracks go with their shots (the body sent them). A late closer is
  a line: kept;
- a dropped round's history lives in the history file: a name not asked for in AGE_DAYS has its
  answer dropped, and its rounds come back with the next answer (none while the calls fail);
- an account whose booth add-on lapses keeps its answers for up to AGE_DAYS, then reads only what
  the season file still has: its dropped rounds come back if the add-on does;
- the first drop on a big profile runs as the booth starts, synchronously, in one transaction:
  seconds, once; each start after looks only at the rounds still here;
- a team's row reached by an alias keeps the spelling the answer had for the other teammates;
- a side's row two teammates' answers hold is refreshed when either of them is asked again;
- `nights` counts sessions: a day this PC restarted the booth counts twice, as it does locally,
  while the cloud's rounds of a day are one.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import threading
import sqlite3
import time
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

FILE = "history.db"        # beside the season file, in the cloud profile's logs
SCHEMA = "hist"            # the season file's connection's name for it
PATH = "/api/devices/history"
FORMAT = 1                 # the answer's `v`: another is ignored
BOUND_S = 2.5              # the opener's longest wait for its players' calls
CALL_S = 10.0              # a call's longest, a token refresh included: a slow answer still lands for the round
AGE_DAYS = 30              # a name not asked for this long: its answer is dropped
DEBOUNCE_S = 1.0           # the setup screen's names stand this long before they're asked for
AT_ONCE = 2
PRACTICE_DAYS = 30         # a practice stretch (never sent: the cloud never has it) is kept on this PC this long
PLAYER = "Player"          # a venue's word for a teammate the answer doesn't name (the cloud's erasure.PLAYER)
TABLES = ("sessions", "shots", "player_rounds", "rounds")

# The answer's columns (the cloud's history.py), each the season file's column of that name.
_COUNTS = ("fir_hit", "fir_tries", "gir_hit", "gir_tries", "scramble_hit", "scramble_tries", "sand_hit",
           "sand_tries", "putts", "putt_holes")
ROUND_COLUMNS = ("id", "day", "is_league", "is_tournament")
PLAYER_COLUMNS = ("ended_at", "course", "fmt", *_COUNTS, "scores", "hole_pars", "complete", "holes_played",
                  "nine_to_par")
SIDE_COLUMNS = ("ended_at", "date", "course", "format", "side", "team", "holes", "gross", "par", "strokes",
                "net_to_par", *_COUNTS, "penalties", "longest_drive", "scores", "hole_pars", "source", "complete",
                "holes_played", "nine_to_par", "nine_net_to_par", "green_firmness", "fairway_firmness",
                "course_type", "launch_monitor", "side_id", "players")
SHOT_COLUMNS = ("round", "uid", "ts", "course", "hole", "shot_number", "club", "is_putt", "lie_before", "lie_after",
                "to_pin_yards", "finish_yards", "total_yards", "carry_game_yards", "carry_raw", "ball_speed",
                "club_speed", "launch_angle", "total_spin", "spin_axis", "start_direction", "outcome", "mulligan",
                "attributed")
_HOLES = ("scores", "hole_pars")   # hole number -> a whole number, kept as JSON text as the season file has it

# What the history file has beyond the season file's shapes: which rounds each name's answer holds
# (and on which side), what the views leave the season file's own rows out by; and the keys a
# loaded row is one of (a shot once a player, a side once a round: two teammates' answers hold a
# team round once).
EXTRA = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA}.held (
    player TEXT NOT NULL,                        -- the name asked for, as the booth has it tonight
    round_uuid TEXT NOT NULL,                    -- the round's id, the PC's own (round_records.uuid)
    side_id TEXT NOT NULL,                       -- the player's side in it (cloud_rounds._sides)
    PRIMARY KEY (player, round_uuid)
);
CREATE TABLE IF NOT EXISTS {SCHEMA}.asked (     -- when each name's answer last came (attach drops the old)
    player TEXT PRIMARY KEY,
    at REAL NOT NULL,
    idx REAL                                     -- the answer's handicap index (`indexes`)
);
CREATE UNIQUE INDEX IF NOT EXISTS {SCHEMA}.shots_player_uid ON shots(player, uid);
CREATE INDEX IF NOT EXISTS {SCHEMA}.player_rounds_player ON player_rounds(player, round_uuid);
CREATE UNIQUE INDEX IF NOT EXISTS {SCHEMA}.rounds_side_id ON rounds(round_uuid, side_id);
CREATE INDEX IF NOT EXISTS {SCHEMA}.rounds_side ON rounds(side);
"""


class Bad(ValueError):
    """An answer that isn't whole: dropped, and what the history file had is kept."""


class Throttled(Exception):
    def __init__(self, wait: float) -> None:
        self.wait = wait
        super().__init__(f"Shotcast Cloud: this PC's history allowance is spent; asking again in {int(wait)}s")


def path_for(db_path: str) -> Path:
    return Path(db_path).with_name(FILE)


# ---- the season file's connection: the history file attached, the past read through views ----------

def attach(db: sqlite3.Connection, path: str | Path) -> dict[str, str]:
    """Attach the history file (made, in the season file's shapes, if it isn't there) and make the views
    the booth reads its past through. Returns each table's view."""
    db.execute(f"ATTACH DATABASE ? AS {SCHEMA}", (str(path),))
    try:   # (another connection writes the answers while the booth reads)
        db.execute(f"PRAGMA {SCHEMA}.journal_mode = WAL")
    except sqlite3.OperationalError as e:
        log.warning("history file not in WAL mode: %s", e)
    shapes = {t: [r[1] for r in db.execute(f"PRAGMA main.table_info({t})")] for t in TABLES}
    have = {t: {r[1] for r in db.execute(f"PRAGMA {SCHEMA}.table_info({t})")} for t in TABLES}
    if any(have[t] and not set(shapes[t]) <= have[t] for t in TABLES):
        # Made by an older Shotcast, before a column the season file has now: it holds only what the
        # cloud said, which is asked for again.
        log.info("History file made in an older shape: emptied, and asked for again")
        for t in (*TABLES, "held", "asked"):
            db.execute(f"DROP TABLE IF EXISTS {SCHEMA}.{t}")
    for t in TABLES:
        (sql,) = db.execute("SELECT sql FROM main.sqlite_master WHERE type = 'table' AND name = ?", (t,)).fetchone()
        db.execute(re.sub(r'^CREATE TABLE\s+"?\w+"?', f"CREATE TABLE IF NOT EXISTS {SCHEMA}.{t}", sql))
    if "side_id" not in {r[1] for r in db.execute(f"PRAGMA {SCHEMA}.table_info(rounds)")}:
        db.execute(f"ALTER TABLE {SCHEMA}.rounds ADD COLUMN side_id TEXT")
    db.executescript(EXTRA)
    if "idx" not in {r[1] for r in db.execute(f"PRAGMA {SCHEMA}.table_info(asked)")}:
        db.execute(f"ALTER TABLE {SCHEMA}.asked ADD COLUMN idx REAL")   # (a file made before it was kept)
    _age(db)
    # The season file's own rows the history file has the cloud's of are left out: the cloud's are
    # what counts (a shot it left out, a round it re-marked). A shot of a round the history file holds
    # for that player, or the same shot (moved here into a round not sent yet); a player's card the
    # history file has for that round; a side's card when one of its players' answers holds the
    # round with a card for their side (the cloud's row for that side stands in for it).
    mine = {"shots": f"EXISTS (SELECT 1 FROM {SCHEMA}.held AS h WHERE h.round_uuid = m.record_uuid"
                     f" AND h.player = m.player) OR EXISTS (SELECT 1 FROM {SCHEMA}.shots AS c"
                     " WHERE c.player = m.player AND c.uid = m.uid)",
            "player_rounds": f"EXISTS (SELECT 1 FROM {SCHEMA}.player_rounds AS c WHERE c.player = m.player"
                             " AND c.round_uuid = m.round_uuid)",
            "rounds": f"EXISTS (SELECT 1 FROM {SCHEMA}.held AS h JOIN {SCHEMA}.rounds AS c"
                      " ON c.round_uuid = h.round_uuid AND c.side_id = h.side_id WHERE h.round_uuid = m.round_uuid"
                      " AND (h.player = m.side OR h.player IN (SELECT value FROM json_each(m.players))))"}
    # A night is a session: a cloud round's shots go in this PC's session of that day, if it has one
    # (a practice stretch here before it), so that day is one night, as it is when both are local.
    nights = (f"{SCHEMA}.shots AS c LEFT JOIN {SCHEMA}.sessions AS cs ON cs.id = c.session_id LEFT JOIN"
              " (SELECT date, MIN(id) AS id FROM main.sessions GROUP BY date) AS ms ON ms.date = cs.date")
    views = {}
    for t, theirs_too in mine.items():
        cols = ", ".join(shapes[t])
        theirs = f"SELECT {cols} FROM {SCHEMA}.{t}"
        if t == "shots":
            theirs = "SELECT " + ", ".join("COALESCE(ms.id, c.session_id)" if c == "session_id" else f"c.{c}"
                                           for c in shapes[t]) + f" FROM {nights}"
        views[t] = f"temp.history_{t}"
        db.execute(f"DROP VIEW IF EXISTS {views[t]}")
        db.execute(f"""CREATE TEMP VIEW history_{t} ({cols}) AS {theirs}
                       UNION ALL SELECT {cols} FROM main.{t} AS m
                       WHERE NOT ({theirs_too})""")
    db.commit()
    return views


def _age(db: sqlite3.Connection) -> None:
    """Drop the answers of names not asked for in AGE_DAYS (a player gone, an erased person's): the
    cloud's word on them is that old."""
    since = time.time() - AGE_DAYS * 86400
    gone = f"player NOT IN (SELECT player FROM {SCHEMA}.asked WHERE at >= ?)"
    for t in ("held", "shots", "player_rounds", "asked"):
        db.execute(f"DELETE FROM {SCHEMA}.{t} WHERE {gone}", (since,))
    db.execute(f"DELETE FROM {SCHEMA}.rounds WHERE NOT EXISTS (SELECT 1 FROM {SCHEMA}.held AS h"
               f" WHERE h.round_uuid = rounds.round_uuid AND h.side_id = rounds.side_id)")


def indexes(db: sqlite3.Connection) -> dict[str, float]:
    """The handicap index each kept answer gave (attached): the booth's own for those players from its
    start, so a call that fails tonight still leaves a player whose rounds are no longer here theirs."""
    return {p: i for p, i in db.execute(f"SELECT player, idx FROM {SCHEMA}.asked WHERE idx IS NOT NULL")}


# ---- an answer into the history file (a thread's own connection) -------------------------------------

def _table(got: Any, columns: tuple[str, ...]) -> tuple[dict[str, list], int]:
    """A columnar table: each column a list, all of one length."""
    if not isinstance(got, dict):
        raise Bad("a table isn't one")
    cols = {k: got.get(k) for k in columns}
    if not all(isinstance(v, list) for v in cols.values()):
        raise Bad(f"a table has no {', '.join(k for k, v in cols.items() if not isinstance(v, list))}")
    lengths = {len(v) for v in cols.values()}
    if len(lengths) != 1:
        raise Bad("a table's columns aren't of one length")
    return cols, lengths.pop()


def check(got: Any) -> tuple[list[dict], list[dict], float | None]:
    """An answer as rows: (rounds, each with its `player` and `side` rows; shots; the index). Raises Bad
    for anything but a whole answer of this format."""
    if not isinstance(got, dict) or got.get("v") != FORMAT:
        raise Bad(f"not an answer of format {FORMAT}")
    rounds = got.get("rounds")
    rcols, n = _table(rounds, ROUND_COLUMNS)
    (pcols, pn), (scols, sn) = _table(rounds.get("player"), PLAYER_COLUMNS), _table(rounds.get("side"), SIDE_COLUMNS)
    if pn != n or sn != n:
        raise Bad("a round's rows aren't all there")
    tcols, _ = _table(got.get("shots"), SHOT_COLUMNS)
    out = []
    for i in range(n):
        r = {k: v[i] for k, v in rcols.items()}
        r["player"] = {k: v[i] for k, v in pcols.items()}
        r["side"] = {k: v[i] for k, v in scols.items()}
        if not isinstance(r["id"], str) or not isinstance(r["day"], str):
            raise Bad("a round has no id or day")
        try:
            r["date"] = date.fromisoformat(r["day"])
        except ValueError:
            raise Bad(f"a round's day isn't a date: {r['day']!r}") from None
        players = r["side"]["players"]
        if not isinstance(players, list) or not all(isinstance(p, str) for p in players):
            raise Bad("a side's players aren't names")
        out.append(r)
    shots = [{k: v[j] for k, v in tcols.items()} for j in range(len(tcols["uid"]))]
    if not all(isinstance(s["round"], int) and not isinstance(s["round"], bool) and 0 <= s["round"] < n
               for s in shots):
        raise Bad("a shot's round isn't one of the answer's")
    index = got.get("index")
    if index is not None and (isinstance(index, bool) or not isinstance(index, (int, float))
                              or not math.isfinite(index)):
        raise Bad("the index isn't a number")
    return out, shots, index


def _value(k: str, v: Any) -> Any:
    """A value as the season file keeps it: a flag as 0/1, a hole map as JSON text."""
    if isinstance(v, bool):
        return int(v)
    if k in _HOLES:
        return json.dumps(v) if isinstance(v, dict) else None
    return v


def _seat(players: list[str], name: str) -> int | None:
    """Where the asked-for player is in an answer's side: by name, ignoring case, else the one player a
    venue's answer names (the others are PLAYER); None when that can't be told."""
    at = [i for i, p in enumerate(players) if p == name] or [i for i, p in enumerate(players)
                                                             if p.casefold() == name.casefold()]
    if not at:
        at = [i for i, p in enumerate(players) if p != PLAYER]
    return at[0] if len(at) == 1 else None


def _renamed(players: list[str], seat: int | None, name: str) -> list[str]:
    return [name if i == seat else p for i, p in enumerate(players)]


def _noon(d: date) -> float:
    """A row's time when the answer has none: midday of the day it was played (local)."""
    return time.mktime((d.year, d.month, d.day, 12, 0, 0, 0, 0, -1))


def load(path: str | Path, name: str, got: Any) -> float | None:
    """One player's answer into the history file, in place of what it held for `name` (the booth's name
    for them tonight, which their rows take), in one transaction: a bad answer (Bad) changes nothing.
    Returns the answer's handicap index."""
    rounds, shots, index = check(got)
    con = sqlite3.connect(str(path), timeout=30)
    try:
        with con:
            con.execute("BEGIN IMMEDIATE")
            _replace(con, name, rounds, shots)
            con.execute("INSERT OR REPLACE INTO asked (player, at, idx) VALUES (?, ?, ?)", (name, time.time(), index))
    finally:
        con.close()
    return index


def _insert(con: sqlite3.Connection, table: str, row: dict[str, Any], verb: str = "INSERT") -> None:
    con.execute(f"{verb} INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))


def _replace(con: sqlite3.Connection, name: str, rounds: list[dict], shots: list[dict]) -> None:
    con.execute("DELETE FROM held WHERE player = ?", (name,))
    con.execute("DELETE FROM shots WHERE player = ?", (name,))
    con.execute("DELETE FROM player_rounds WHERE player = ?", (name,))
    con.execute("DELETE FROM rounds WHERE NOT EXISTS (SELECT 1 FROM held h WHERE h.round_uuid = rounds.round_uuid"
                " AND h.side_id = rounds.side_id)")
    # (ids below every one there, negative: never one of the season file's in the views)
    ids = {t: con.execute(f"SELECT MIN(0, COALESCE(MIN(id), 0)) FROM {t}").fetchone()[0] for t in TABLES[1:]}

    def next_id(table: str) -> int:
        ids[table] -= 1
        return ids[table]

    kept = []   # each round's session and flags, for its shots
    for r in rounds:
        d, own, side = r["date"], r["player"], r["side"]
        # One session a day, dated as played (its id from the day: every answer's rounds of a day share it).
        session = -d.toordinal()
        con.execute("INSERT OR IGNORE INTO sessions (id, started_at, date) VALUES (?, ?, ?)",
                    (session, time.mktime(d.timetuple()), d.isoformat()))
        ended = own["ended_at"] if own["ended_at"] is not None else side["ended_at"]
        ended = ended if ended is not None else _noon(d)
        flags = {"is_league": 0, "is_tournament": int(r["is_tournament"] == 1), "excluded": 0}
        side_id = side["side_id"] or ""
        con.execute("INSERT OR IGNORE INTO held (player, round_uuid, side_id) VALUES (?, ?, ?)", (name, r["id"], side_id))
        kept.append((session, flags))
        if any(v is not None for v in own.values()):   # (all null: the slot had no card)
            _insert(con, "player_rounds", {k: _value(k, v) for k, v in own.items()} | flags | {
                "id": next_id("player_rounds"), "session_id": session, "ended_at": ended, "player": name,
                "complete": int(own["complete"] is not False), "round_uuid": r["id"]})
        seat = _seat(side["players"], name)
        players = _renamed(side["players"], seat, name)
        have = con.execute("SELECT players FROM rounds WHERE round_uuid = ? AND side_id = ?", (r["id"], side_id)).fetchone()
        if have is not None:   # (a teammate's answer has the side already: the same card, with them named)
            held = json.loads(have[0]) if have[0] else players
            if len(held) == len(players):   # (and a side of one is theirs, under tonight's name)
                con.execute("UPDATE rounds SET players = ?, side = CASE WHEN ? = 1 THEN ? ELSE side END"
                            " WHERE round_uuid = ? AND side_id = ?",
                            (json.dumps(_renamed(held, seat, name)), len(held), name, r["id"], side_id))
            continue
        label = name if len(players) == 1 else side["side"]
        holes = next((h for h in (side["holes"], side["holes_played"]) if isinstance(h, int)), None)
        if label is None or holes is None:   # (the season file has no row without them)
            continue
        row = {k: _value(k, v) for k, v in side.items() if k not in ("players", "side_id")}
        _insert(con, "rounds", row | flags | {
            "id": next_id("rounds"), "session_id": session, "ended_at": side["ended_at"] if side["ended_at"] is not None
            else ended, "date": side["date"] or d.isoformat(), "side": label, "holes": holes,
            "complete": int(side["complete"] is not False), "players": json.dumps(players), "round_uuid": r["id"],
            "side_id": side_id})
    for s in shots:
        if s["ts"] is None or s["is_putt"] is None or not s["uid"]:   # (the season file has no shot without them)
            continue
        session, flags = kept[s["round"]]
        _insert(con, "shots", {k: _value(k, v) for k, v in s.items() if k != "round"} | flags | {
            "id": next_id("shots"), "session_id": session, "player": name, "record_uuid": rounds[s["round"]]["id"],
            "mulligan": int(bool(s["mulligan"])), "attributed": int(s["attributed"] is not False), "left_out": 0},
            "INSERT OR IGNORE")   # (each uid once a player)


# ---- the season file keeps only what the cloud doesn't give back ---------------------------------------

# The days a round record touched: its own session's and those of its rows (a round a restarted booth
# went on with past midnight is played on both). NULL when it has none: never taken as over.
_DAYS = """(SELECT MAX(s.date) FROM main.sessions AS s WHERE s.id = r.session_id
            OR s.id IN (SELECT session_id FROM main.shots WHERE record_uuid = r.uuid)
            OR s.id IN (SELECT session_id FROM main.lines WHERE record_uuid = r.uuid)
            OR s.id IN (SELECT session_id FROM main.rounds WHERE round_uuid = r.uuid)
            OR s.id IN (SELECT session_id FROM main.player_rounds WHERE round_uuid = r.uuid))"""
# What goes: a round the cloud has (the uploader's own "sent", round_final included) whose days are
# over and that the booth already reads from the history file alone (none of its rows is read through
# the views: every one of its players' answers holds it, with their cards and their sides'), so dropping
# it changes nothing the booth says; and a practice stretch (never sent) older than PRACTICE_DAYS.
# Neither in progress, and only one that still has rows here. (A round also needs its rows as sent:
# `forget` compares them.)
_GONE = f"""SELECT r.uuid, r.kind, u.sent_rows FROM main.round_records AS r
            LEFT JOIN main.cloud_uploads AS u ON u.uuid = r.uuid
            WHERE r.state != 'in_progress'
            AND ((r.kind = 'round' AND u.revision = 1 AND {_DAYS} < :today
                  AND EXISTS (SELECT 1 FROM {SCHEMA}.held AS h WHERE h.round_uuid = r.uuid)
                  AND NOT EXISTS (SELECT 1 FROM temp.history_shots WHERE record_uuid = r.uuid AND id > 0)
                  AND NOT EXISTS (SELECT 1 FROM temp.history_rounds WHERE round_uuid = r.uuid AND id > 0)
                  AND NOT EXISTS (SELECT 1 FROM temp.history_player_rounds WHERE round_uuid = r.uuid AND id > 0))
                 OR (r.kind = 'practice' AND {_DAYS} < :practice))
            AND (EXISTS (SELECT 1 FROM main.shots WHERE record_uuid = r.uuid)
                 OR EXISTS (SELECT 1 FROM main.rounds WHERE round_uuid = r.uuid)
                 OR EXISTS (SELECT 1 FROM main.player_rounds WHERE round_uuid = r.uuid))
            ORDER BY r.started_at, r.uuid"""
# A record's rows that go, in order. A kept line on a shot that goes is on none (shot ids are used again:
# it would be on a new one); a week's result on a side that goes goes with it (as would its id, else).
_DROP = ("UPDATE main.lines SET shot_id = NULL WHERE shot_id IN (SELECT id FROM main.shots WHERE record_uuid = ?)",
         "DELETE FROM main.shot_tracks WHERE shot_id IN (SELECT id FROM main.shots WHERE record_uuid = ?)",
         "DELETE FROM main.shots WHERE record_uuid = ?",
         "DELETE FROM main.week_results WHERE round_id IN (SELECT id FROM main.rounds WHERE round_uuid = ?)",
         "DELETE FROM main.rounds WHERE round_uuid = ?",
         "DELETE FROM main.player_rounds WHERE round_uuid = ?",
         "DELETE FROM main.round_stats_due WHERE uuid = ?",
         "DELETE FROM main.round_folders WHERE record_uuid = ?")


def forget(db: sqlite3.Connection, prof: Any, today: str) -> tuple[int, int]:
    """At a paired booth's start, with the history file attached (`attach`): the season file's rows of each
    round the cloud has whose day is over (before `today`, the booth's session date), whose rows are as
    they were sent, and that the history file holds for each of its players (_GONE), and of each practice
    stretch older than PRACTICE_DAYS, deleted: their shots and shot tracks, cards and side rows, and the
    round's GSPro course folder. Kept: the record itself, its slots and its cloud_uploads row (so a round gone from here is never
    thought unsent, and the lines still have their record), the lines (the phrase memory), every session,
    the booth's memory tables, the course caches and every other record's rows. Nothing at all unless the
    file is this pairing's profile's own season file, bound to it, and the cloud has answered for a
    player in AGE_DAYS (the account has the booth's history: else the season file is the booth's only
    past). One transaction. Returns (rounds, practice stretches) dropped."""
    from . import cloud_rounds

    main = next(r[2] for r in db.execute("PRAGMA database_list") if r[1] == "main")
    tables = {r[0] for r in db.execute("SELECT name FROM main.sqlite_master WHERE type = 'table'")}
    if (not main or Path(main).resolve() != Path(prof.db).resolve() or "cloud_uploads" not in tables
            or "cloud_pairing" not in tables or cloud_rounds.bound(db) != tuple(prof.bound)
            or "sent_rows" not in {r[1] for r in db.execute("PRAGMA main.table_info(cloud_uploads)")}
            or not db.execute(f"SELECT 1 FROM {SCHEMA}.asked LIMIT 1").fetchone()):
        return 0, 0
    practice = (date.fromisoformat(today) - timedelta(days=PRACTICE_DAYS)).isoformat()
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    try:
        # (a round with rows written since it was sent, taken up again and played on, has holes the
        # cloud never got: kept; so is one whose rows as the cloud has them can't be told: sent before
        # they were noted, or a round_final, the cloud keeping an earlier build)
        gone = [(uuid, kind) for uuid, kind, sent in db.execute(_GONE, {"today": today, "practice": practice})
                if kind == "practice" or sent == cloud_rounds.rows_of(db, uuid)]
        # (a deleted shot notes its session for the finishes to be worked out again, and its round for
        # its stats: neither is, as no kept row changed; a kept shot's next stroke would now be another's)
        noted = [s for (s,) in db.execute("SELECT session_id FROM main.finish_recheck")]
        for uuid, _ in gone:
            for sql in _DROP:
                db.execute(sql, (uuid,))
        db.execute("DELETE FROM main.finish_recheck WHERE session_id NOT IN (SELECT value FROM json_each(?))",
                   (json.dumps(noted),))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    rounds = sum(kind == "round" for _, kind in gone)
    if gone:
        log.info("Shotcast Cloud: %d of this PC's earlier rounds no longer kept here (the cloud has them), "
                 "and %d practice stretches over %d days old", rounds, len(gone) - rounds, PRACTICE_DAYS)
    return rounds, len(gone) - rounds


# ---- the calls -------------------------------------------------------------------------------------

def fetch(cl: Any, name: str, before: str) -> Any:
    """One player's answer (its JSON), asked for with the PC's pass (cloud.Cloud). Raises Throttled on a
    429, cloud.CloudError for any other refusal, and cloud.Unreachable for the network."""
    from .cloud import CloudError, _message

    r = cl.request("POST", PATH, {"name": name, "before": before}, timeout=CALL_S)
    if r.status_code == 429:
        try:
            wait = float(r.headers.get("Retry-After", ""))
        except (TypeError, ValueError):
            wait = 300.0
        raise Throttled(min(max(wait, 1.0), 86400.0) if wait == wait else 300.0)
    if r.status_code != 200:
        raise CloudError(f"Shotcast Cloud didn't give the history (HTTP {r.status_code}: {_message(r)})")
    try:
        return r.json()
    except ValueError:
        raise CloudError("Shotcast Cloud's history answer isn't JSON") from None


def _daemon(work: Callable[[], Any]) -> asyncio.Future:
    """`work` in a daemon thread (as the engine's own handicaps are worked out), so a booth quitting
    doesn't wait on a call under way. A future of its result on the running loop."""
    loop = asyncio.get_running_loop()
    done: asyncio.Future = loop.create_future()

    def settle(ok: bool, value: Any) -> None:
        if not done.done():
            done.set_result(value) if ok else done.set_exception(value)

    def run() -> None:
        try:
            out = (True, work())
        except BaseException as e:   # noqa: BLE001 - handed to the waiting call
            out = (False, e)
        try:
            loop.call_soon_threadsafe(settle, *out)
        except RuntimeError:   # the loop has closed: the booth has quit
            pass

    threading.Thread(target=run, name="cloud-history", daemon=True).start()
    return done


class Asks:
    """The booth's calls for its players' histories, on its event loop: the calls and the loads run in
    threads. `cloud`: the PC's pass, still the booth's pairing (else cloud.PairingChanged); `day`: the
    booth's session date (the calls' `before`); `on_index(name, index)`: an answer's handicap index."""

    def __init__(self, path: str | Path, cloud: Callable[[], Any], day: Callable[[], str],
                 on_index: Callable[[str, float | None], None]) -> None:
        self.path, self.cloud, self.day, self.on_index = path, cloud, day, on_index
        self.wanted: tuple[str, ...] = ()
        self.answered: dict[str, str] = {}          # name -> the `before` it was answered for
        self.calls: dict[str, asyncio.Task] = {}    # name -> its call, waiting its turn or under way
        self.flying: set[str] = set()               # those under way (a thread can't be stopped)
        self.timer: asyncio.Task | None = None
        self.turns = asyncio.Semaphore(AT_ONCE)
        self.hold = 0.0       # (monotonic) a 429's wait: nothing is asked till then
        self.stopped = False  # the pairing isn't the booth's any more: nothing more is asked
        self.said: set[str] = set()   # what went wrong, said once till a call goes through

    def want(self, names: list[str], now: bool = False) -> None:
        """The players set up now (`now`: at the tee, asked at once; else once they've stood DEBOUNCE_S).
        A call for anyone else that hasn't started is dropped, and one under way isn't loaded."""
        names = tuple(dict.fromkeys(n for n in names if n))
        if names == self.wanted and not now:
            return
        self.wanted = names
        for n, task in list(self.calls.items()):
            if n not in names and n not in self.flying:
                task.cancel()
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        if now:
            self._ask()
        else:
            self.timer = asyncio.get_running_loop().create_task(self._later(), name="history-debounce")

    async def _later(self) -> None:
        await asyncio.sleep(DEBOUNCE_S)
        self.timer = None
        try:
            self._ask()
        except Exception as e:   # noqa: BLE001 - the season file busy (the session's date), say
            self._say(type(e).__name__, f"Shotcast Cloud: histories not asked for: {e}")

    def _ask(self) -> None:
        if self.stopped or time.monotonic() < self.hold:
            return
        before = self.day()
        for n in self.wanted:
            if self.answered.get(n) != before and (n not in self.calls or self.calls[n].done()):
                self.calls[n] = asyncio.get_running_loop().create_task(self._call(n, before), name="history-call")

    async def ready(self, names: list[str]) -> None:
        """Wait, BOUND_S at most, for these players' calls: those waiting to be asked too."""
        end = time.monotonic() + BOUND_S
        if self.timer is not None and set(names) & set(self.wanted):
            await asyncio.wait({self.timer}, timeout=BOUND_S)
        waits = {t for n in names if (t := self.calls.get(n)) is not None}
        if waits and (left := end - time.monotonic()) > 0:
            await asyncio.wait(waits, timeout=left)

    def _say(self, key: str, text: str) -> None:
        if key not in self.said:
            self.said.add(key)
            log.warning("%s", text)

    async def _call(self, name: str, before: str) -> None:
        from . import cloud

        try:
            async with self.turns:
                if name not in self.wanted or self.stopped or time.monotonic() < self.hold:
                    return
                self.flying.add(name)
                try:
                    try:
                        got = await asyncio.wait_for(_daemon(lambda: fetch(self.cloud(), name, before)), CALL_S)
                    except asyncio.TimeoutError:
                        raise cloud.Unreachable(f"no answer in {CALL_S:.0f}s") from None
                    if name not in self.wanted:   # (set up no more: its answer is dropped)
                        return
                    index = await _daemon(lambda: load(self.path, name, got))
                finally:
                    self.flying.discard(name)
            self.answered[name] = before
            self.said.clear()
            self.on_index(name, index)
            log.info("Shotcast Cloud: %s's history loaded", name)
        except asyncio.CancelledError:
            raise
        except Throttled as e:
            self.hold = time.monotonic() + e.wait
            self._say("throttled", str(e))
        except (cloud.NotPaired, cloud.PairingChanged, cloud.TooOld) as e:
            self.stopped = True
            log.warning("Shotcast Cloud: no more histories asked for: %s", e)
        except Bad as e:
            self._say("bad", f"Shotcast Cloud's history answer was dropped: {e}")
        except cloud.Unreachable as e:
            self._say("offline", f"Shotcast Cloud: history not asked for (the booth goes on with what it has): {e}")
        except Exception as e:   # noqa: BLE001 - a refusal, a busy file: the last answer is kept
            self._say(type(e).__name__, f"Shotcast Cloud: history not loaded (the last one is kept): {e}")
        finally:
            if self.calls.get(name) is asyncio.current_task():
                del self.calls[name]


def start(cfg: Any, store: Any, on_index: Callable[[str, float | None], None]) -> Asks | None:
    """As a paired booth starts (cloud.startup; not a replay): the history file attached to the season
    file's connection, the season file's rows the cloud gives back dropped (`forget`), and the calls for
    it; None when the API no longer accepts the pairing (the last answers are still read, and nothing is
    dropped). Self-hosted: nothing, and None."""
    from . import cloud

    prof = cloud.startup
    if prof is None or cfg.source.type == "replay":
        return None
    path = path_for(cfg.db_path)
    store.attach_history(str(path))
    if not prof.connected:
        return None
    identity, bound = cloud.identity(prof.rec), prof.bound

    def pass_() -> Any:
        cl = cloud.Cloud(cloud.store(), expect=bound)
        if cl.identity != identity:
            raise cloud.PairingChanged("this PC's Shotcast Cloud pairing changed; restart Shotcast to use it")
        return cl

    def day() -> str:
        return store.db.execute("SELECT date FROM sessions WHERE id = ?", (store.session_id,)).fetchone()[0]

    try:
        forget(store.db, prof, day())
    except Exception as e:   # noqa: BLE001 - the file busy, say: all of it is kept, and looked at next start
        log.warning("Shotcast Cloud: this PC's earlier rounds kept for now (%s)", e)
    return Asks(path, pass_, day, on_index)
