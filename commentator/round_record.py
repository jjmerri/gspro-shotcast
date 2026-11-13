"""The stored round record (docs/dev/local-data-design.md): which round, or practice stretch, every
shot, line and round row belongs to, kept in season.db instead of worked out by time each read.

`membership` is the rule rounds were always read by (the time rules below, formerly in stats_web),
over every row, excluded or deleted ones included. `reconcile` stores what it says for the rows that
don't have a record yet: all of them the first time (the schema 6 migration), and afterwards only
what an older version of the app wrote. A record the booth wrote as it played (`assigned = live`,
`Live`) is never regrouped. The readers (round_groups, shot_rounds, ...) read the stored record
once every row has one, and the time rules till then."""
from __future__ import annotations

import bisect
import json
import sqlite3
import uuid
from collections import Counter
from typing import Any

from .store import CLOSER_SECONDS

ROUND_GAP_SECONDS = 30   # a round's sides are stored a moment apart; separate rounds are minutes apart
ROUND_SPAN_SECONDS = 8 * 3600   # no round takes longer: an older shot was in a round never recorded
PRACTICE_IDLE_SECONDS = 30 * 60   # a practice stretch ends after the bay sits idle this long (to be tuned)

# Tables, columns and triggers of schema 6. Older versions of the app never write the new columns;
# the triggers fill in a row's uid and keep each record's change counter for them too.
TABLES = """
CREATE TABLE IF NOT EXISTS meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    dataset_id TEXT NOT NULL,                    -- made once per season.db: a copy is recognisable
    quiet TEXT NOT NULL DEFAULT '0'              -- who is writing, for the triggers: 0 / applying / bookkeeping
);
CREATE TABLE IF NOT EXISTS round_records (
    uuid TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                          -- round / practice (a practice stretch)
    session_id INTEGER,
    course TEXT,
    started_at REAL,
    ended_at REAL,
    state TEXT NOT NULL,                         -- in_progress / finished / abandoned
    gen INTEGER NOT NULL DEFAULT 0,              -- moves whenever what the record shows changes
    assigned TEXT NOT NULL                       -- live (the booth, as it played) / inferred (by time)
);
CREATE TABLE IF NOT EXISTS round_slots (           -- a player in a round
    slot_id TEXT PRIMARY KEY,
    round_uuid TEXT NOT NULL,
    seat INTEGER,                                -- GSPro's setup-screen index, when known
    name TEXT,                                   -- as GSPro shows it (a rename changes it)
    side TEXT,                                   -- the rounds.side it plays on
    player TEXT,                                 -- the league roster name it was matched to
    matched_by TEXT NOT NULL                     -- booth (a player as it played) / shot (a name on a shot, no player of it) / legacy (the migration)
);
"""

COLUMNS = {
    "shots": {"uid": "TEXT", "record_uuid": "TEXT", "slot_id": "TEXT", "derived_from": "TEXT"},
    "lines": {"uid": "TEXT", "record_uuid": "TEXT"},
    "rounds": {"round_uuid": "TEXT"},
    "player_rounds": {"round_uuid": "TEXT", "slot_id": "TEXT"},
}

INDEXES = {
    "shots_record": "shots(record_uuid)",
    "lines_record": "lines(record_uuid)",
    "rounds_record": "rounds(round_uuid)",
    "player_rounds_record": "player_rounds(round_uuid)",
    "round_slots_record": "round_slots(round_uuid)",
    "round_records_state": "round_records(state, ended_at)",
}
UNIQUE_INDEXES = {"shots_uid": "shots(uid)", "lines_uid": "lines(uid)"}

_NEW_UID = "lower(hex(randomblob(16)))"
_COUNTS = "(SELECT quiet FROM meta) IS NOT 'bookkeeping'"
# table -> the column naming its record
_RECORD_OF = {"shots": "record_uuid", "lines": "record_uuid", "rounds": "round_uuid",
              "player_rounds": "round_uuid", "round_slots": "round_uuid"}


def _bump(uuid_sql: str) -> str:
    return f"UPDATE round_records SET gen = gen + 1 WHERE uuid = {uuid_sql};"


def _triggers() -> str:
    out = []
    for table in ("shots", "lines"):
        out.append(f"""CREATE TRIGGER IF NOT EXISTS {table}_uid AFTER INSERT ON {table} WHEN NEW.uid IS NULL
BEGIN UPDATE {table} SET uid = {_NEW_UID} WHERE id = NEW.id; END;""")
    for table, col in _RECORD_OF.items():
        out.append(f"""CREATE TRIGGER IF NOT EXISTS {table}_gen_insert AFTER INSERT ON {table}
WHEN NEW.{col} IS NOT NULL AND {_COUNTS} BEGIN {_bump(f"NEW.{col}")} END;""")
        out.append(f"""CREATE TRIGGER IF NOT EXISTS {table}_gen_update AFTER UPDATE ON {table}
WHEN {_COUNTS} BEGIN UPDATE round_records SET gen = gen + 1 WHERE uuid IN (NEW.{col}, OLD.{col}); END;""")
        out.append(f"""CREATE TRIGGER IF NOT EXISTS {table}_gen_delete AFTER DELETE ON {table}
WHEN OLD.{col} IS NOT NULL AND {_COUNTS} BEGIN {_bump(f"OLD.{col}")} END;""")
    rec = "(SELECT round_uuid FROM rounds WHERE id = {}.round_id)"
    out.append(f"""CREATE TRIGGER IF NOT EXISTS week_results_gen_insert AFTER INSERT ON week_results
WHEN {_COUNTS} BEGIN {_bump(rec.format("NEW"))} END;""")
    out.append(f"""CREATE TRIGGER IF NOT EXISTS week_results_gen_update AFTER UPDATE ON week_results
WHEN {_COUNTS} BEGIN UPDATE round_records SET gen = gen + 1 WHERE uuid IN ({rec.format("NEW")}, {rec.format("OLD")}); END;""")
    out.append(f"""CREATE TRIGGER IF NOT EXISTS week_results_gen_delete AFTER DELETE ON week_results
WHEN {_COUNTS} BEGIN {_bump(rec.format("OLD"))} END;""")
    out.append(f"""CREATE TRIGGER IF NOT EXISTS round_records_gen AFTER UPDATE OF state, ended_at, course ON round_records
WHEN {_COUNTS} BEGIN {_bump("NEW.uuid")} END;""")
    return "\n".join(out)


def install(db: sqlite3.Connection) -> None:
    """Schema 6's tables, columns, indexes and triggers, where they're missing (idempotent)."""
    db.executescript(TABLES)
    for table, cols in COLUMNS.items():
        have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols.items():
            if col not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    for name, on in INDEXES.items():
        db.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {on}")
    if db.execute("PRAGMA user_version").fetchone()[0] >= 6:   # (else `migrate` makes them, once the uids are in)
        _unique_indexes(db)
    db.executescript(_triggers())
    db.execute("INSERT OR IGNORE INTO meta (id, dataset_id) VALUES (1, ?)", (str(uuid.uuid4()),))


def _unique_indexes(db: sqlite3.Connection) -> None:
    for name, on in UNIQUE_INDEXES.items():
        db.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {on}")


def set_quiet(db: sqlite3.Connection, quiet: str) -> None:
    """Say who is writing, for the triggers: set and cleared within the writer's own transaction."""
    db.execute("UPDATE meta SET quiet = ?", (quiet,))


# ---- the time rules (what a round was before it was stored) ---------------------------------------------------------------------------------

def _timed_round_groups(db: sqlite3.Connection, table: str = "rounds") -> tuple[list[dict[str, Any]], dict[int, int]]:
    """The rounds as played: one per end of a round, whatever its number of sides (one row each in
    `rounds`, or `table`). Returns them oldest first ({round, end, course}; `round` is the lowest row
    id) and each row's round."""
    groups: list[dict[str, Any]] = []
    of_row: dict[int, int] = {}
    for r in db.execute(f"SELECT id, session_id, ended_at, course FROM {table} ORDER BY ended_at, id"):
        g = groups[-1] if groups else None
        if not (g and g["session"] == r["session_id"] and g["course"] == r["course"]
                and r["ended_at"] - g["end"] <= ROUND_GAP_SECONDS):
            g = {"round": r["id"], "end": r["ended_at"], "course": r["course"], "session": r["session_id"]}
            groups.append(g)
        g["end"] = r["ended_at"]
        of_row[r["id"]] = g["round"]
    return groups, of_row


def _timed_player_round_of(db: sqlite3.Connection):
    """A lookup from a player_rounds row (session, course, end) to its round in `rounds` (round_groups'
    id): both are stored as the round ends, a moment apart. None when no side of it was recorded."""
    _, of_row = _timed_round_groups(db)
    ends: dict[tuple, list[tuple[float, int]]] = {}
    for rid, sess, end, course in db.execute("SELECT id, session_id, ended_at, course FROM rounds"):
        ends.setdefault((sess, (course or "").casefold()), []).append((end, of_row[rid]))

    def find(session_id: int, course: str | None, ended_at: float) -> int | None:
        near = [(abs(end - ended_at), rnd) for end, rnd in ends.get((session_id, (course or "").casefold()), [])]
        best = min(near, default=None)
        return best[1] if best and best[0] <= ROUND_GAP_SECONDS else None
    return find


def _timed_shot_rounds(db: sqlite3.Connection, every: bool = False) -> dict[int, int]:
    """Each shot's round: shots aren't stored with one, so it's the round that ended next after the
    shot (and after the round before it), when that round was on the shot's course and ended within a
    round's length of it (a booth restart mid-round keeps its shots). Shots of a round that was never
    finished and recorded have none. `every` takes in the rounds left out of the stats too (connect's
    views hide them)."""
    groups, _ = _timed_round_groups(db, "main.rounds" if every else "rounds")
    ends = [g["end"] for g in groups]
    out = {}
    for sid, ts, course in db.execute(f"SELECT id, ts, course FROM {'main.shots' if every else 'shots'}"):
        i = bisect.bisect_left(ends, ts)
        # (a course name read off the scorecard is in capitals)
        if i < len(groups) and (groups[i]["course"] or "").casefold() == (course or "").casefold() \
                and groups[i]["end"] - ts <= ROUND_SPAN_SECONDS:
            out[sid] = groups[i]["round"]
    return out


def _timed_late_gimmes(db: sqlite3.Connection, groups: list[dict[str, Any]], owner: dict[int, int],
                 table: str = "shots") -> dict[int, int]:
    """shot_rounds' `owner`, with each putt conceded late (its result came in after the round was
    recorded) given to that round, not the next one."""
    for x in groups:
        for sid, course in db.execute(f"SELECT id, course FROM {table} WHERE session_id = ? AND outcome = 'gimme'"
                                      " AND ts > ? AND ts <= ?", (x["session"], x["end"], x["end"] + CLOSER_SECONDS)):
            if (course or "").casefold() == (x["course"] or "").casefold():
                owner[sid] = x["round"]
    return owner


def membership(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every round by the time rules, over every row (excluded and deleted ones too: call it on the
    file's own tables, not stats_web.connect's views), oldest first: {round, end, course, session,
    sides, cards, shots, lines}, each a sorted list of row ids. A round's lines are those on its shots,
    and the lines on no shot (the calls before a swing, the reactions, the closer) of every session it
    was played in (a round can span a booth restart), each from the closer of that session's round
    before to its own."""
    groups, of_row = _timed_round_groups(db)
    find = _timed_player_round_of(db)
    owner = _timed_late_gimmes(db, groups, _timed_shot_rounds(db))
    by_id = {g["round"]: g for g in groups}
    for g in groups:
        g.update(sides=[], cards=[], shots=[], lines=[])
    for rid, rnd in of_row.items():
        by_id[rnd]["sides"].append(rid)
    for r in db.execute("SELECT id, session_id, course, ended_at FROM player_rounds"):
        if (rnd := find(r["session_id"], r["course"], r["ended_at"])) is not None:
            by_id[rnd]["cards"].append(r["id"])
    for sid, rnd in owner.items():
        by_id[rnd]["shots"].append(sid)
    session_of = dict(db.execute("SELECT id, session_id FROM shots").fetchall())
    on_shot: dict[int, list[int]] = {}
    for lid, sid in db.execute("SELECT id, shot_id FROM lines WHERE shot_id IS NOT NULL"):
        on_shot.setdefault(sid, []).append(lid)
    loose: dict[Any, tuple[list[float], list[int]]] = {}
    for lid, sess, ts in db.execute("SELECT id, session_id, ts FROM lines WHERE shot_id IS NULL ORDER BY ts, id"):
        at, ids = loose.setdefault(sess, ([], []))
        at.append(ts)
        ids.append(lid)
    claimed: set[int] = set()
    last_end: dict[Any, float] = {}
    for g in groups:
        lines = {lid for sid in g["shots"] for lid in on_shot.get(sid, ())}
        for sess in {g["session"]} | {session_of[sid] for sid in g["shots"]}:
            at, ids = loose.get(sess, ([], []))
            since = last_end.get(sess, float("-inf"))
            lo, hi = bisect.bisect_right(at, since + CLOSER_SECONDS), bisect.bisect_right(at, g["end"] + CLOSER_SECONDS)
            lines.update(ids[lo:hi])
        g["lines"] = sorted(lines - claimed)
        claimed |= lines
        last_end[g["session"]] = max(last_end.get(g["session"], float("-inf")), g["end"])
        for k in ("sides", "cards", "shots"):
            g[k].sort()
    return groups


# ---- the readers --------------------------------------------------------------------------------------
# What the stats read rounds by: the stored record, once every row has one; until then (a file not yet
# migrated, or rows an older version of the app wrote since the last start) the time rules. Each keeps
# the time rules' shapes: a round is numbered by its first `rounds` row.

def recorded(db: sqlite3.Connection) -> bool:
    """Whether every row of the file has its record (main's tables: stats_web.connect's views hide some)."""
    try:
        if db.execute("PRAGMA main.user_version").fetchone()[0] < 6:
            return False
        return db.execute("""SELECT NOT (EXISTS (SELECT 1 FROM main.shots WHERE record_uuid IS NULL)
                             OR EXISTS (SELECT 1 FROM main.lines WHERE record_uuid IS NULL)
                             OR EXISTS (SELECT 1 FROM main.rounds WHERE round_uuid IS NULL)
                             OR EXISTS (SELECT 1 FROM main.player_rounds WHERE round_uuid IS NULL))""").fetchone()[0] == 1
    except sqlite3.OperationalError:
        return False


def round_groups(db: sqlite3.Connection, table: str = "rounds") -> tuple[list[dict[str, Any]], dict[int, int]]:
    """The rounds as played: one per end of a round, whatever its number of sides (one row each in
    `rounds`, or `table`). Returns them oldest first ({round, end, course, session}; `round` is its
    first row's id) and each row's round."""
    if not recorded(db):
        return _timed_round_groups(db, table)
    groups: list[dict[str, Any]] = []
    of_row: dict[int, int] = {}
    by_uuid: dict[str, dict[str, Any]] = {}
    for r in db.execute(f"SELECT id, session_id, ended_at, course, round_uuid FROM {table} ORDER BY ended_at, id"):
        if (g := by_uuid.get(r["round_uuid"])) is None:
            g = by_uuid[r["round_uuid"]] = {"round": r["id"], "end": r["ended_at"], "course": r["course"],
                                            "session": r["session_id"]}
            groups.append(g)
        g["end"] = r["ended_at"]
        of_row[r["id"]] = g["round"]
    return groups, of_row


def _round_of_record(db: sqlite3.Connection, table: str, of_row: dict[int, int]) -> dict[str, int]:
    return {u: of_row[rid] for rid, u in db.execute(f"SELECT id, round_uuid FROM {table}") if rid in of_row}


def player_round_of(db: sqlite3.Connection):
    """A lookup from a player_rounds row (its session, course and end) to its round in `rounds`
    (round_groups' id). None when no side of it was recorded."""
    if not recorded(db):
        return _timed_player_round_of(db)
    _, of_row = round_groups(db)
    of_record = _round_of_record(db, "rounds", of_row)
    card = {(sess, (course or "").casefold(), end): of_record.get(u) for sess, course, end, u in db.execute(
        "SELECT session_id, course, ended_at, round_uuid FROM player_rounds")}

    def find(session_id: int, course: str | None, ended_at: float) -> int | None:
        return card.get((session_id, (course or "").casefold(), ended_at))
    return find


def shot_rounds(db: sqlite3.Connection, every: bool = False) -> dict[int, int]:
    """Each shot's round (round_groups' id): none for practice, or a round never finished and recorded,
    or a putt conceded after its round was recorded (_late_gimmes'). `every` takes in the rounds left
    out of the stats too (connect's views hide them)."""
    if not recorded(db):
        return _timed_shot_rounds(db, every)
    rounds_table, shots_table = ("main.rounds", "main.shots") if every else ("rounds", "shots")
    groups, of_row = round_groups(db, rounds_table)
    of_record = _round_of_record(db, rounds_table, of_row)
    end = {g["round"]: g["end"] for g in groups}
    out = {}
    for sid, ts, outcome, u in db.execute(f"SELECT id, ts, outcome, record_uuid FROM {shots_table}"):
        if (rnd := of_record.get(u)) is not None and not (outcome == "gimme" and ts > end[rnd]):
            out[sid] = rnd
    return out


def _late_gimmes(db: sqlite3.Connection, groups: list[dict[str, Any]], owner: dict[int, int],
                 table: str = "shots") -> dict[int, int]:
    """shot_rounds' `owner`, with each putt conceded late (its result came in after the round was
    recorded) given to that round, not the next one."""
    if not recorded(db):
        return _timed_late_gimmes(db, groups, owner, table)
    of_record = {u: g for g in groups for (u,) in db.execute("SELECT round_uuid FROM main.rounds WHERE id = ?",
                                                             (g["round"],))}
    for sid, ts, u in db.execute(f"SELECT id, ts, record_uuid FROM {table} WHERE outcome = 'gimme'"):
        if (g := of_record.get(u)) is not None and ts > g["end"]:
            owner[sid] = g["round"]
    return owner


def round_rows(db: sqlite3.Connection, round_id: int) -> dict[str, Any] | None:
    """A round by round_groups' id, over every row (excluded or deleted ones too: call it on the file's
    own tables): {round, end, sides, cards, shots, lines} as `membership` gives it. None when there's
    no such round."""
    if not recorded(db):
        return next((g for g in membership(db) if g["round"] == round_id), None)
    row = db.execute("SELECT round_uuid FROM rounds WHERE id = ?", (round_id,)).fetchone()
    if row is None:
        return None
    u = row[0]
    sides = db.execute("SELECT id, ended_at FROM rounds WHERE round_uuid = ? ORDER BY ended_at, id", (u,)).fetchall()
    if sides[0][0] != round_id:
        return None

    def ids(table: str, col: str) -> list[int]:
        return [i for (i,) in db.execute(f"SELECT id FROM {table} WHERE {col} = ? ORDER BY id", (u,))]
    return {"round": round_id, "end": sides[-1][1], "sides": sorted(i for i, _ in sides),
            "cards": ids("player_rounds", "round_uuid"), "shots": ids("shots", "record_uuid"),
            "lines": ids("lines", "record_uuid")}


# ---- storing it -------------------------------------------------------------------------------------

def _new_id() -> str:
    return str(uuid.uuid4())


def unassigned(db: sqlite3.Connection) -> bool:
    """Whether any row has no record yet (all of them, before the migration; afterwards, an older
    version's)."""
    return db.execute("""SELECT EXISTS (SELECT 1 FROM shots WHERE record_uuid IS NULL)
                         OR EXISTS (SELECT 1 FROM lines WHERE record_uuid IS NULL)
                         OR EXISTS (SELECT 1 FROM rounds WHERE round_uuid IS NULL)
                         OR EXISTS (SELECT 1 FROM player_rounds WHERE round_uuid IS NULL)""").fetchone()[0] == 1


def _side_of(name: str, sides: list[sqlite3.Row]) -> str | None:
    for r in sides:
        try:
            players = json.loads(r["players"] or "null")
        except (TypeError, ValueError):
            players = None
        if (isinstance(players, list) and name in players) or r["side"] == name:
            return r["side"]
    return None


def reconcile(db: sqlite3.Connection, quiet: str = "0") -> None:
    """Store `membership`'s grouping for every row that isn't in a live record, and a practice
    stretch for the shots and lines in no round. Records it already made keep their UUIDs where the
    grouping still has them (the one holding most of a group's rows); ones left with no rows are
    removed. Runs inside the caller's transaction, with meta.quiet `quiet` (`bookkeeping` for the
    migration, so no record's counter moves; else every record that gains or loses a row moves)."""
    db.row_factory = sqlite3.Row
    set_quiet(db, quiet)
    records = {r["uuid"]: r for r in db.execute("SELECT * FROM round_records")}
    live = {u for u, r in records.items() if r["assigned"] == "live"}
    shots = {r["id"]: r for r in db.execute("SELECT id, ts, session_id, course, player, record_uuid, slot_id FROM shots")}
    lines = {r["id"]: r for r in db.execute("SELECT id, ts, session_id, shot_id, record_uuid FROM lines")}
    sides = {r["id"]: r for r in db.execute(
        "SELECT id, session_id, ended_at, course, side, players, complete, round_uuid FROM rounds")}
    cards = {r["id"]: r for r in db.execute(
        "SELECT id, session_id, course, ended_at, complete, player, round_uuid, slot_id FROM player_rounds")}
    tables = {"shots": (shots, "record_uuid"), "lines": (lines, "record_uuid"),
              "rounds": (sides, "round_uuid"), "player_rounds": (cards, "round_uuid")}

    def pinned(table: str, rid: int) -> bool:
        rows, col = tables[table]
        return rows[rid][col] in live

    target: dict[tuple[str, int], str] = {}
    meta: dict[str, dict[str, Any]] = {}
    claimed: set[str] = set()

    def claim(rows: list[tuple[str, int]], kind: str) -> str:
        """The record for these rows: the live one its sides or cards were recorded in (a late row of a
        round the booth recorded), else the existing one (of `kind`) holding most of them, else a new
        one."""
        now = [tables[t][0][i][tables[t][1]] for t, i in rows]
        if found := next((tables[t][0][i][tables[t][1]] for t, i in rows
                          if t in ("rounds", "player_rounds") and tables[t][0][i][tables[t][1]] in live), None):
            return found
        for u, _ in Counter(u for u in now if u and u not in live and u in records
                            and records[u]["kind"] == kind).most_common():
            if u not in claimed:
                claimed.add(u)
                return u
        u = _new_id()
        claimed.add(u)
        return u

    def follows_shot(i: int) -> bool:
        return lines[i]["shot_id"] in shots

    in_round: set[int] = set()
    matched_cards: set[int] = set()
    round_ends: list[float] = []   # (a round with no shots still breaks a practice stretch)
    for g in membership(db):
        matched_cards.update(g["cards"])
        round_ends.append(g["end"])
        rows = ([("rounds", i) for i in g["sides"]] + [("player_rounds", i) for i in g["cards"]]
                + [("shots", i) for i in g["shots"]] + [("lines", i) for i in g["lines"]])
        # (a line on a shot goes where the shot goes, below: the shot may be pinned elsewhere)
        free = [(t, i) for t, i in rows if not pinned(t, i) and not (t == "lines" and follows_shot(i))]
        if not free:
            in_round.update(g["shots"])
            continue
        u = claim(rows, "round")
        in_round.update(g["shots"])
        for row in free:
            target[row] = u
        if u not in live:
            at = [shots[i]["ts"] for i in g["shots"]] + [lines[i]["ts"] for i in g["lines"]]
            done = [sides[i]["complete"] for i in g["sides"]]
            meta[u] = {"kind": "round", "session_id": g["session"], "course": g["course"],
                       "started_at": min(at, default=g["end"]), "ended_at": g["end"],
                       "state": "abandoned" if done and min(done) == 0 else "finished"}
    # Cards whose round no side was recorded for: a round of their own (those of one ending together).
    orphans = sorted((c["ended_at"], i) for i, c in cards.items()
                     if i not in matched_cards and not pinned("player_rounds", i))
    run: list[int] = []

    def close_cards() -> None:
        if not run:
            return
        u = claim([("player_rounds", i) for i in run], "round")
        for i in run:
            target[("player_rounds", i)] = u
        if u not in live:
            c = cards[run[0]]
            meta[u] = {"kind": "round", "session_id": c["session_id"], "course": c["course"],
                       "started_at": cards[run[0]]["ended_at"], "ended_at": cards[run[-1]]["ended_at"],
                       "state": "abandoned" if min(cards[i]["complete"] for i in run) == 0 else "finished"}
        round_ends.append(cards[run[-1]]["ended_at"])
        run.clear()

    for end, i in orphans:
        c, last = cards[i], cards[run[-1]] if run else None
        if last and (c["session_id"] != last["session_id"] or (c["course"] or "").casefold() != (last["course"] or "").casefold()
                     or end - last["ended_at"] > ROUND_GAP_SECONDS):
            close_cards()
        run.append(i)
    close_cards()
    # Shots in no round, and lines on none of a round's shots or in its stretch of the session: practice,
    # in stretches split where a round's shot came between, the session changed or the bay sat idle.
    round_at = sorted([shots[i]["ts"] for i in in_round | {i for i, s in shots.items()
                                                            if s["record_uuid"] in live
                                                            and records[s["record_uuid"]]["kind"] == "round"}]
                      + round_ends)
    events = sorted([(s["ts"], 0, i) for i, s in shots.items() if ("shots", i) not in target
                     and i not in in_round and not pinned("shots", i)]
                    + [(ln["ts"], 1, i) for i, ln in lines.items() if ("lines", i) not in target
                       and not pinned("lines", i) and ln["shot_id"] is None])
    stretch: list[tuple[str, int]] = []
    prev: tuple[float, Any] | None = None

    def close() -> None:
        if not stretch:
            return
        u = claim(stretch, "practice")
        for row in stretch:
            target[row] = u
        if u not in live:
            first = next((shots[i] for t, i in stretch if t == "shots"), None)
            at = [tables[t][0][i]["ts"] for t, i in stretch]
            meta[u] = {"kind": "practice", "session_id": tables[stretch[0][0]][0][stretch[0][1]]["session_id"],
                       "course": first["course"] if first else None, "started_at": min(at), "ended_at": max(at),
                       "state": "finished"}
        stretch.clear()

    for ts, is_line, i in events:
        row = ("lines" if is_line else "shots", i)
        sess = tables[row[0]][0][i]["session_id"]
        if prev and (sess != prev[1] or ts - prev[0] > PRACTICE_IDLE_SECONDS
                     or bisect.bisect_right(round_at, prev[0]) < bisect.bisect_left(round_at, ts)):
            close()
        stretch.append(row)
        prev = (ts, sess)
    close()
    # A line on a shot goes with the shot, wherever that went.
    for i, ln in lines.items():
        if ln["shot_id"] is not None and ("lines", i) not in target and not pinned("lines", i):
            sid = ln["shot_id"]
            if (u := target.get(("shots", sid)) or (shots[sid]["record_uuid"] if follows_shot(i) else None)):
                target[("lines", i)] = u
            else:
                target[("lines", i)] = claim([("lines", i)], "practice")
                meta[target[("lines", i)]] = {"kind": "practice", "session_id": ln["session_id"], "course": None,
                                              "started_at": ln["ts"], "ended_at": ln["ts"], "state": "finished"}
    _store(db, records, meta, target, tables, sides, shots, cards)
    set_quiet(db, "0")


def _store(db, records, meta, target, tables, sides, shots, cards) -> None:
    db.executemany("UPDATE round_records SET kind = :kind, session_id = :session_id, course = :course,"
                   " started_at = :started_at, ended_at = :ended_at, state = :state WHERE uuid = :uuid",
                   [{**m, "uuid": u} for u, m in meta.items() if u in records])
    db.executemany("INSERT INTO round_records (uuid, kind, session_id, course, started_at, ended_at, state, assigned)"
                   " VALUES (:uuid, :kind, :session_id, :course, :started_at, :ended_at, :state, 'inferred')",
                   [{**m, "uuid": u} for u, m in meta.items() if u not in records])
    # Slots: one per player name in the record (its shots, cards and sides' players), kept where the
    # record has one by that name already.
    names: dict[str, dict[str, None]] = {}
    for (t, i), u in target.items():
        if t == "rounds":
            try:
                who = json.loads(sides[i]["players"] or "null")
            except (TypeError, ValueError):
                who = None
            who = [n for n in who if isinstance(n, str)] if isinstance(who, list) else []
        else:
            who = [shots[i]["player"] if t == "shots" else cards[i]["player"] if t == "player_rounds" else None]
        for name in who:
            if name and name != "unknown":
                names.setdefault(u, {})[name] = None
    record_sides: dict[str, list] = {}
    for i, u in ((i, u) for (t, i), u in target.items() if t == "rounds"):
        record_sides.setdefault(u, []).append(sides[i])
    slot, side_now = {}, {}
    for r in db.execute("SELECT round_uuid, name, slot_id, side FROM round_slots"):
        slot[(r["round_uuid"], r["name"])] = r["slot_id"]
        side_now[r["slot_id"]] = r["side"]
    new_slots, sided = [], []
    for u, who in names.items():
        for name in who:
            side = _side_of(name, record_sides.get(u, []))
            if (u, name) not in slot:
                slot[(u, name)] = _new_id()
                new_slots.append((slot[(u, name)], u, name, side))
            elif u in meta and side_now[slot[(u, name)]] != side:   # (a side recorded since)
                sided.append((side, slot[(u, name)]))
    db.executemany("INSERT INTO round_slots (slot_id, round_uuid, name, side, matched_by) VALUES (?, ?, ?, ?, 'legacy')",
                   new_slots)
    db.executemany("UPDATE round_slots SET side = ? WHERE slot_id = ?", sided)
    # (every row of a record that isn't live is in `target`, so `names` is all of its players)
    db.executemany("DELETE FROM round_slots WHERE slot_id = ?",
                   [(s,) for (u, name), s in slot.items()
                    if (u in meta or (u in records and records[u]["assigned"] != "live")) and name not in names.get(u, {})])
    changed: dict[str, list[tuple]] = {t: [] for t in tables}
    for (t, i), u in target.items():
        rows, col = tables[t]
        r = rows[i]
        if t in ("shots", "player_rounds"):
            s = slot.get((u, r["player"]))
            if r[col] != u or r["slot_id"] != s:
                changed[t].append((u, s, i))
        elif r[col] != u:
            changed[t].append((u, i))
    for t, args in changed.items():
        col = tables[t][1]
        sets = f"{col} = ?, slot_id = ?" if t in ("shots", "player_rounds") else f"{col} = ?"
        if t in ("shots", "lines"):   # (the migration's uids, in the same pass over the rows)
            sets += f", uid = COALESCE(uid, {_NEW_UID})"
        db.executemany(f"UPDATE {t} SET {sets} WHERE id = ?", args)
    # Records no row is in any more (their rows regrouped elsewhere), unless the booth wrote them.
    db.execute("""DELETE FROM round_records WHERE assigned != 'live'
                  AND NOT EXISTS (SELECT 1 FROM shots WHERE record_uuid = round_records.uuid)
                  AND NOT EXISTS (SELECT 1 FROM lines WHERE record_uuid = round_records.uuid)
                  AND NOT EXISTS (SELECT 1 FROM rounds WHERE round_uuid = round_records.uuid)
                  AND NOT EXISTS (SELECT 1 FROM player_rounds WHERE round_uuid = round_records.uuid)""")
    db.execute("DELETE FROM round_slots WHERE round_uuid NOT IN (SELECT uuid FROM round_records)")


def migrate(db: sqlite3.Connection) -> None:
    """Schema 5 to 6, once: every row gets its record (`reconcile`, without moving any counter:
    nothing a record shows has changed) and every shot and line a uid, then the uids are made unique.
    Afterwards, at each start, the
    rows an older version of the app wrote since (no record yet) are reconciled, moving the counters
    of the records they join or leave. One transaction either way."""
    if db.execute("PRAGMA user_version").fetchone()[0] >= 6 and not unassigned(db):
        return
    db.commit()
    db.execute("BEGIN IMMEDIATE")   # (another booth or a backfill opening the file at once waits, then sees 6)
    try:
        if db.execute("PRAGMA user_version").fetchone()[0] < 6:
            reconcile(db, "bookkeeping")
            set_quiet(db, "bookkeeping")
            for table in ("shots", "lines"):
                db.execute(f"UPDATE {table} SET uid = {_NEW_UID} WHERE uid IS NULL")
                if db.execute(f"SELECT 1 FROM {table} WHERE uid IS NULL LIMIT 1").fetchone():
                    raise RuntimeError(f"{table}: uids missing")
            _unique_indexes(db)   # (fails, rolling it all back, if a uid repeated)
            set_quiet(db, "0")
            db.execute("PRAGMA user_version = 6")
        elif unassigned(db):
            reconcile(db)
        db.commit()
    except BaseException:
        db.rollback()
        raise


# ---- the booth, as it plays ---------------------------------------------------------------------------

def _same(a: str | None, b: str | None) -> bool:
    return (a or "").casefold() == (b or "").casefold()


class Live:
    """The records the booth writes as it plays (`assigned = live`): the round being played, from its
    start (`begin`) to its end (`end`), else a practice stretch. Each write method asks `place` (a
    shot) or `place_line` where its row goes. Writes join the caller's transaction; the caller commits."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self.round: str | None = None            # the round being played
        self.closing: tuple[str, float] | None = None   # the round just ended, and when: its closer's lines
        self.practice: tuple[str, int | None, float] | None = None   # (uuid, session, last row at)
        self.mine: set[str] = set()              # rounds begun by this booth (never a restart's to continue)

    def _last_at(self, u: str) -> float | None:
        return self.db.execute("SELECT MAX(at) FROM (SELECT MAX(ts) AS at FROM shots WHERE record_uuid = ?1"
                               " UNION ALL SELECT MAX(ts) FROM lines WHERE record_uuid = ?1"
                               " UNION ALL SELECT started_at FROM round_records WHERE uuid = ?1)", (u,)).fetchone()[0]

    def _close(self, u: str, state: str) -> None:
        """End an in-progress record at its last row; a practice stretch left with none is dropped."""
        practice = self.db.execute("SELECT 1 FROM round_records WHERE uuid = ? AND kind = 'practice'", (u,)).fetchone()
        if practice and not self.db.execute("SELECT 1 FROM shots WHERE record_uuid = ?1 UNION ALL"
                                            " SELECT 1 FROM lines WHERE record_uuid = ?1 LIMIT 1", (u,)).fetchone():
            self.db.execute("DELETE FROM round_slots WHERE round_uuid = ?", (u,))
            self.db.execute("DELETE FROM round_records WHERE uuid = ?", (u,))
            return
        self.db.execute("UPDATE round_records SET state = ?, ended_at = ? WHERE uuid = ? AND state = 'in_progress'",
                        (state, self._last_at(u), u))

    def start(self, now: float) -> None:
        """A booth starting: the practice stretch the last one left open is over, and so is a round
        nothing can continue any more (`ROUND_SPAN_SECONDS` since its last row). A more recent one waits
        for `begin` to continue it."""
        for u, kind in self.db.execute("SELECT uuid, kind FROM round_records WHERE state = 'in_progress'"
                                       " AND assigned = 'live'").fetchall():
            if kind == "practice":
                self._close(u, "finished")
            elif now - (self._last_at(u) or 0) > ROUND_SPAN_SECONDS:
                self._close(u, "abandoned")

    def _find(self, u: str, name: str, player: str | None = None) -> str | None:
        """The slot in `u` that's `name` (case aside), or holds it or `player` as its roster name."""
        best = None
        for s, n, p in self.db.execute("SELECT slot_id, name, player FROM round_slots WHERE round_uuid = ?", (u,)):
            rank = 0 if n == name else 1 if _same(n, name) else 2 if p and _same(p, name) else \
                3 if player and (_same(n, player) or _same(p, player)) else None
            if rank is not None and (best is None or rank < best[0]):
                best = (rank, s)
        return best[1] if best else None

    def slot(self, u: str | None, name: str | None, *, player: str | None = None, seat: int | None = None,
             how: str = "shot") -> str | None:
        """`name`'s slot in record `u`, made if it has none (no slot for a shot nobody's named). A shot
        names a league player by their roster name, which their slot (made with GSPro's) holds as `player`.
        One made for a round's player is `booth`'s; one made for a name on a shot, no player of it, `shot`'s."""
        if not u or not name or name == "unknown":
            return None
        if s := self._find(u, name, player):
            return s
        s = _new_id()
        self.db.execute("INSERT INTO round_slots (slot_id, round_uuid, seat, name, player, matched_by)"
                        " VALUES (?, ?, ?, ?, ?, ?)", (s, u, seat, name, player, how))
        return s

    def sync(self, u: str, players: dict[str, str | None], seats: list[str] | None) -> None:
        """Round `u`'s slots as its players are now (one added or dropped mid-round): each player's
        slot takes their GSPro name, roster name and seat; a slot nobody has any more, with no shot or
        card on it, goes."""
        if not self.known(u):
            return
        keep = set()
        for name, player in players.items():
            if not name or name == "unknown":
                continue
            seat = seats.index(name) if seats and name in seats else None
            if s := self._find(u, name, player):
                self.db.execute("UPDATE round_slots SET name = ?, player = ?, seat = ?, matched_by = 'booth'"
                                " WHERE slot_id = ?", (name, player, seat, s))
            else:
                s = self.slot(u, name, player=player, seat=seat, how="booth")
            keep.add(s)
        for (s,) in self.db.execute("SELECT slot_id FROM round_slots WHERE round_uuid = ?", (u,)).fetchall():
            if s not in keep and not self.db.execute(
                    "SELECT 1 FROM shots WHERE slot_id = ?1 UNION ALL SELECT 1 FROM player_rounds WHERE slot_id = ?1"
                    " LIMIT 1", (s,)).fetchone():
                self.db.execute("DELETE FROM round_slots WHERE slot_id = ?", (s,))

    def _practice(self, session_id: int | None, at: float, course: str | None) -> str:
        if p := self.practice:
            if p[1] == session_id and at - p[2] <= PRACTICE_IDLE_SECONDS and self.db.execute(
                    "SELECT 1 FROM round_records WHERE uuid = ? AND state = 'in_progress'", (p[0],)).fetchone():
                self.practice = (p[0], session_id, max(at, p[2]))
                return p[0]
            self._close(p[0], "finished")
        u = _new_id()
        self.db.execute("INSERT INTO round_records (uuid, kind, session_id, course, started_at, state, assigned)"
                        " VALUES (?, 'practice', ?, ?, ?, 'in_progress', 'live')", (u, session_id, course, at))
        self.practice = (u, session_id, at)
        return u

    def place(self, session_id: int | None, player: str | None, at: float, course: str | None,
              record: str | None = None) -> tuple[str, str | None]:
        """A new shot's (record, slot): the round it was struck in (`record`, when it has one: a shot
        stored late is still its), else the round being played, else the practice stretch."""
        u = (record if self.known(record) else None) or self.round or self._practice(session_id, at, course)
        return u, self.slot(u, player)

    def place_line(self, session_id: int | None, at: float) -> str:
        """A new line on no shot: the round being played, the one just ended (its closer), else practice."""
        if self.round:
            return self.round
        if self.closing and at - self.closing[1] <= CLOSER_SECONDS:
            return self.closing[0]
        return self._practice(session_id, at, None)

    def known(self, u: str | None) -> bool:
        return bool(u) and self.db.execute("SELECT 1 FROM round_records WHERE uuid = ?", (u,)).fetchone() is not None

    def begin(self, u: str, *, session_id: int | None, course: str | None, players: dict[str, str | None],
              seats: list[str] | None, since_id: int | None, now: float) -> str:
        """A round starting: its record and a slot per player, the shots since `since_id` (its opening
        drives, struck before it started) moved in from practice, and the practice stretch ended.
        A round left in progress by a booth that stopped is continued (its UUID returned) when this one
        is on the same course with the same players; any other is abandoned."""
        names = {n.casefold() for n in players if n and n != "unknown"}
        go_on = None
        for v, c in self.db.execute("SELECT uuid, course FROM round_records WHERE kind = 'round' AND state = 'in_progress'"
                                    " AND assigned = 'live' ORDER BY started_at DESC").fetchall():
            had = {r[0].casefold() for r in self.db.execute(
                "SELECT name FROM round_slots WHERE round_uuid = ? AND name IS NOT NULL AND matched_by = 'booth'", (v,))}
            if go_on is None and v not in self.mine and names and _same(c, course) and had == names \
                    and now - (self._last_at(v) or 0) <= ROUND_SPAN_SECONDS:
                go_on = v
            else:
                self._close(v, "abandoned")
        if go_on:
            u = go_on
        else:
            self.db.execute("INSERT INTO round_records (uuid, kind, session_id, course, started_at, state, assigned)"
                            " VALUES (?, 'round', ?, ?, ?, 'in_progress', 'live')", (u, session_id, course, now))
        self.sync(u, players, seats)
        if since_id is not None:
            moved = self.db.execute(
                "SELECT id, player FROM shots WHERE id > ? AND (record_uuid IS NULL OR record_uuid IN"
                " (SELECT uuid FROM round_records WHERE kind = 'practice'))", (since_id,)).fetchall()
            for sid, player in moved:
                self.db.execute("UPDATE shots SET record_uuid = ?, slot_id = ? WHERE id = ?", (u, self.slot(u, player), sid))
                self.db.execute("UPDATE lines SET record_uuid = ? WHERE shot_id = ?", (u, sid))
        if self.practice:
            # What the booth said this session since practice's last shot (the opener, aired over the
            # course load) is the round's: as the time rules had it.
            p = self.practice[0]
            if self.practice[1] == session_id:
                self.db.execute("UPDATE lines SET record_uuid = ?1 WHERE record_uuid = ?2 AND shot_id IS NULL AND ts >"
                                " COALESCE((SELECT MAX(ts) FROM shots WHERE record_uuid = ?2), -1e18)", (u, p))
            self._close(p, "finished")
            self.practice = None
        self.round, self.closing = u, None
        self.mine.add(u)
        return u

    def end(self, u: str, complete: bool, now: float) -> None:
        """A round over: `finished`, or `abandoned` when quit early."""
        self.db.execute("UPDATE round_records SET state = ?, ended_at = ? WHERE uuid = ? AND state = 'in_progress'",
                        ("finished" if complete else "abandoned", now, u))
        if self.round == u:
            self.round, self.closing = None, (u, now)

    def take(self, shot_id: int, u: str | None) -> None:
        """A shot recorded before its round started, taken into it since (the round's record must exist)."""
        if self.known(u):
            player = self.db.execute("SELECT player FROM shots WHERE id = ?", (shot_id,)).fetchone()
            if player is not None:
                self.db.execute("UPDATE shots SET record_uuid = ?, slot_id = ? WHERE id = ?",
                                (u, self.slot(u, player[0]), shot_id))
                self.db.execute("UPDATE lines SET record_uuid = ? WHERE shot_id = ?", (u, shot_id))

    def reslot(self, shot_ids: list[int]) -> None:
        """Shots whose player changed: each to its player's slot in its own record."""
        for sid in shot_ids:
            if row := self.db.execute("SELECT record_uuid, player, slot_id FROM shots WHERE id = ?", (sid,)).fetchone():
                self.db.execute("UPDATE shots SET slot_id = ? WHERE id = ?", (self.slot(row[0], row[1]), sid))
                # A name only a shot had (a misread), left with none: gone.
                self.db.execute("DELETE FROM round_slots WHERE slot_id = ?1 AND matched_by = 'shot' AND NOT EXISTS"
                                " (SELECT 1 FROM shots WHERE slot_id = ?1) AND NOT EXISTS"
                                " (SELECT 1 FROM player_rounds WHERE slot_id = ?1)", (row[2],))

    def rename(self, u: str, old: str, new: str) -> None:
        """A player's name in round `u` read right at last: the slot keeps its ID and takes the name.
        Where the round has a slot by that name already, the two are one: the round's player's
        (`booth`) is kept, and the other's shots join it."""
        slots = self.db.execute("SELECT slot_id, matched_by, name FROM round_slots WHERE round_uuid = ?"
                                " ORDER BY name = ? DESC", (u, old)).fetchall()
        mine = next((s for s in slots if _same(s[2], old)), None)
        if not mine:
            return
        have = next((s for s in slots if _same(s[2], new) and s[0] != mine[0]), None)
        keep, drop = (mine, have) if have and mine[1] == "booth" and have[1] != "booth" else (have or mine, have and mine)
        if drop:
            for table in ("shots", "player_rounds"):
                self.db.execute(f"UPDATE {table} SET slot_id = ? WHERE slot_id = ?", (keep[0], drop[0]))
            self.db.execute("DELETE FROM round_slots WHERE slot_id = ?", (drop[0],))
        self.db.execute("UPDATE round_slots SET name = ? WHERE slot_id = ?", (new, keep[0]))
