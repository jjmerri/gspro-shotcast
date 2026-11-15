"""A small local web page for browsing the season database (`stats-web`).

The stats themselves (summary() and friends) are computed here; stats_site.py builds them into the
static site that this module serves locally and `publish` uploads. The page is stats_web.html. Stats follow the booth's own rules (store.py): mulligans and unattributed
shots are left out, and scoring, GIR, scrambling, sand and putting come from individual-ball rounds
only, since a scramble or alternate-shot score isn't one player's.
"""

import calendar
import json
import logging
import math
import re
import sqlite3
import tempfile
import threading
import time
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import date
from pathlib import Path
from collections.abc import Callable
from typing import Any
from urllib.parse import unquote, urlparse

from . import changelog, handicap_index
from . import night_totals as N
from . import season_analytics as A
from .round_record import (_late_gimmes, in_play, player_round_of, recorded, reuse, round_groups, round_rows,
                           round_shots, shot_in_play, shot_rounds, unsettled)
from .store import DRIVE_SQL

log = logging.getLogger(__name__)

SHARED_BALL = ("scramble", "alternate")
# Putt length bucket edges in feet (to_pin_yards is stored in yards); the first bucket starts at the
# usual gimme circle (putt_buckets).
PUTT_EDGES = (6, 10, 20, 40)
# Approach-by-club only counts shots from this close, so a 3-wood off a par-5 tee isn't an "approach".
APPROACH_MAX_YARDS = 250
_DRIVE = "COALESCE(total_yards, carry_game_yards, carry_raw)"
_TEE_DRIVER = DRIVE_SQL
_HOLE_YARDS = re.compile(r"Hole (\d+), par \d+, (\d+) yards")
_HOLE = re.compile(r"Hole (\d+)\b")


EXCLUDED, DELETED = 1, 2   # the `excluded` column: out of the stats but listed locally / deleted for good


def connect(path: str | Path) -> sqlite3.Connection:
    """A read-only snapshot of the database, for building the page: the file as it was when it was
    opened, however long the build reads it, while the booth goes on writing. In WAL mode (Store sets it)
    that's one read transaction on the file, held till it's closed; otherwise reading the file that long
    would lock out the booth's writes (a booth starting up, or a shot), so it's copied into memory.
    Rounds excluded from
    the stats or deleted (set_round_excluded, delete_round), with their shots and what the booth said,
    are left out: temp views shadow the tables, which also fill in columns an older database is missing
    (a shot left out of the stats or excluded: none are; the player's hand: unknown). The tables
    themselves stay reachable as main.<table>."""
    from contextlib import closing

    # read-write (made query_only below): a read-only connection can't read a WAL file no other
    # connection has open (it makes the -shm beside it). mode=rw doesn't make a missing file.
    db = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=rw", uri=True, timeout=30)
    wal = db.execute("PRAGMA main.journal_mode").fetchone()[0] == "wal"
    if not wal:
        with closing(db) as src:
            db = sqlite3.connect(":memory:")
            src.backup(db)
    db.row_factory = sqlite3.Row
    for table, missing in (("shots", (("left_out", "0"), ("handed", "NULL"), ("excluded", "0"), ("is_tournament", "0"))),
                           ("rounds", (("excluded", "0"), ("is_tournament", "0"))),
                           ("player_rounds", (("excluded", "0"), ("is_tournament", "0"))),
                           ("lines", (("excluded", "0"),))):
        have = {r[1] for r in db.execute(f"PRAGMA main.table_info({table})")}
        if not have:
            continue
        extra = "".join(f", {v} AS {c}" for c, v in missing if c not in have)
        where = " WHERE excluded = 0" if "excluded" in have else ""
        db.execute(f"CREATE TEMP VIEW {table} AS SELECT *{extra} FROM main.{table}{where}")
    if wal:
        # Reads the file through the OS's cache, mapped, not a page at a time: as quick as a copy in memory,
        # and nothing of its own to hold. (Pages past the first GB are read the usual way.)
        db.execute("PRAGMA mmap_size = 1073741824")
        db.execute("BEGIN")
        db.execute("SELECT 1 FROM main.sqlite_master LIMIT 1").fetchall()   # the snapshot starts at a read
    db.execute("PRAGMA query_only = ON")   # a slip can't change the snapshot either
    return db


def file_stamp(path: str | Path) -> tuple:
    """What changes when the database does: the file, and in WAL mode the log beside it, where writes
    go till they're checkpointed into the file (the file itself can go unchanged for a while)."""
    def one(p: Path) -> tuple[int, int] | None:
        try:
            st = p.stat()
        except FileNotFoundError:
            return None
        return st.st_mtime_ns, st.st_size

    path = Path(path)
    return one(path), one(path.with_name(path.name + "-wal"))


def modified(path: str | Path) -> float:
    """When the database last changed (the file or its WAL log), in seconds."""
    return max(ns for ns, _ in filter(None, file_stamp(path))) / 1e9


def set_left_out(db_path: str | Path, shot_id: int, left_out: bool, ts: float | None = None) -> bool:
    """Leave a shot out of the stats, or put it back (the local page's toggle). False when there's no such
    shot, or (with `ts`, its time on the page) it isn't the one the page showed. Raises RoundInPlay for a
    shot of a round not settled yet."""
    from contextlib import closing

    with closing(sqlite3.connect(db_path, timeout=10)) as db:
        if "left_out" not in {r[1] for r in db.execute("PRAGMA table_info(shots)")}:
            db.execute("ALTER TABLE shots ADD COLUMN left_out INTEGER NOT NULL DEFAULT 0")
        db.execute("BEGIN IMMEDIATE")   # the shot checked is the shot changed
        if shot_in_play(db, shot_id, time.time()):
            db.rollback()
            raise RoundInPlay(shot_id)
        cur = db.execute("UPDATE shots SET left_out = ? WHERE id = ? AND (? IS NULL OR ABS(ts - ?) <= 0.001)",
                         (int(left_out), shot_id, ts, ts))
        db.commit()
        return cur.rowcount > 0


_ROUND_TABLES = ("rounds", "player_rounds", "shots", "lines")


class RoundInPlay(Exception):
    """A round the booth may still be adding to (round_record.in_play): not edited till it's settled."""


def _round_rows(db: sqlite3.Connection, round_id: int, ended: float) -> list[list[int]] | None:
    """A round's rows (as round_groups and the page number it: its lowest `rounds` id), per table of
    _ROUND_TABLES: its sides, its players' cards, its shots and what the booth said in it
    (round_record.round_rows'). None when there's no such round, it didn't end at `ended` (its
    `round_end` on the page), or it was deleted. Worked out over every row, excluded or not, so it's
    the same round either way. Raises RoundInPlay for a round not settled yet."""
    db.row_factory = sqlite3.Row
    g = round_rows(db, round_id)
    if g is None or abs(g["end"] - ended) > 0.001:
        return None
    if db.execute(f"SELECT 1 FROM rounds WHERE excluded = {DELETED} AND id IN ({_in(g['sides'])})").fetchone():
        return None
    if in_play(db, g["sides"], g["end"], time.time()):
        raise RoundInPlay(round_id)
    return [g["sides"], g["cards"], g["shots"], g["lines"]]


def _ensure_excluded(db: sqlite3.Connection) -> None:
    for table in _ROUND_TABLES:
        if "excluded" not in {r[1] for r in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0")


def _in(ids: list[int]) -> str:
    return ",".join(str(int(i)) for i in ids)


def _mark_round(db_path: str | Path, round_id: int, ended: float, value: int) -> bool:
    from contextlib import closing

    with closing(sqlite3.connect(db_path, timeout=10)) as db:
        _ensure_excluded(db)
        db.execute("BEGIN IMMEDIATE")   # the round checked is the round changed
        rows = _round_rows(db, round_id, ended)
        if rows is None:
            db.rollback()
            return False
        for table, ids in zip(_ROUND_TABLES, rows):
            if ids:   # (a shot left out by hand, or a line, already deleted with another round stays so)
                db.execute(f"UPDATE {table} SET excluded = ? WHERE excluded != {DELETED} AND id IN ({_in(ids)})", (value,))
        db.commit()
        return True


def set_round_league(db_path: str | Path, round_id: int, ended: float, league: bool, tournament: bool = False) -> bool:
    """Make a round a league, tournament or practice one (every side, card and shot of it): which of
    the League/Tournament/Practice stats it counts in. A league round is never a tournament one.
    False when there's no such round (or it was deleted)."""
    from contextlib import closing

    with closing(sqlite3.connect(db_path, timeout=10)) as db:
        _ensure_excluded(db)
        for table in _ROUND_TABLES[:3]:
            if "is_tournament" not in {r[1] for r in db.execute(f"PRAGMA table_info({table})")}:
                db.execute(f"ALTER TABLE {table} ADD COLUMN is_tournament INTEGER NOT NULL DEFAULT 0")
        db.execute("BEGIN IMMEDIATE")   # the round checked is the round changed
        rows = _round_rows(db, round_id, ended)
        if rows is None:
            db.rollback()
            return False
        for table, ids in zip(_ROUND_TABLES[:3], rows):   # lines carry no is_league
            if ids:
                db.execute(f"UPDATE {table} SET is_league = ?, is_tournament = ? WHERE id IN ({_in(ids)})",
                           (int(league), int(tournament and not league)))
        db.commit()
        return True


def set_round_excluded(db_path: str | Path, round_id: int, ended: float, excluded: bool) -> bool:
    """Exclude a round (every side, card, shot and line of it) from the stats, or put it back: it's
    still listed on the local page, so it can be. False when there's no such round."""
    return _mark_round(db_path, round_id, ended, EXCLUDED if excluded else 0)


def delete_round(db_path: str | Path, round_id: int, ended: float) -> bool:
    """Delete a round for good: gone from every stat, page and published site, and no longer listed.
    The rows stay in the file, marked: a freed id would be reused for the next shot or round, which
    a page left open (or a published record clip, keyed by shot id) would then take for the old one.
    False when there's no such round."""
    return _mark_round(db_path, round_id, ended, DELETED)


VOD_OVERLAP_SECONDS = 10   # a stream running a few buffered seconds into a round isn't its replay


def round_spans(db: sqlite3.Connection) -> list[list[float]]:
    """Every round as played, deleted ones too, oldest first: [first shot, end, state], state 0 when
    it counts, else EXCLUDED or DELETED. The Watch tab hides a replay whose rounds are all out, and
    deleting the last of them deletes it (videos_of_deleted). Read from the tables under connect()'s views."""
    if not {r[1] for r in db.execute("PRAGMA main.table_info(rounds)")}:
        return []
    groups, of_row = round_groups(db, "main.rounds")
    has_excluded = "excluded" in {r[1] for r in db.execute("PRAGMA main.table_info(rounds)")}
    state: dict[int, int] = {}
    for rid, ex in db.execute(f"SELECT id, {'excluded' if has_excluded else '0'} FROM main.rounds"):
        g = of_row[rid]
        state[g] = min(state.get(g, ex), ex)   # one side still counting keeps the round
    start = {g["round"]: g["end"] for g in groups}
    if recorded(db):
        # (a putt conceded late is the round's too, and after its end: it never moves the start)
        for rnd, d in round_shots(db, "main.shots").items():
            if rnd in start and d["first"] is not None:
                start[rnd] = min(start[rnd], d["first"])
    else:
        owner = _late_gimmes(db, groups, shot_rounds(db, every=True), "main.shots")
        for sid, ts in db.execute("SELECT id, ts FROM main.shots"):
            if (rnd := owner.get(sid)) in start:
                start[rnd] = min(start[rnd], ts)
    return [[start[g["round"]], g["end"], state[g["round"]]] for g in groups]


def _vod_rounds(spans: list[list[float]], since: float, ended: float) -> list[int]:
    """The states of the rounds played on a stream from `since` to `ended`."""
    return [st for start, end, st in spans
            if min(ended, end) - max(since, start) >= min(VOD_OVERLAP_SECONDS, end - start)]


def videos_of_deleted(spans: list[list[float]], recs: list[dict]) -> list[dict]:
    """The finished recordings (local_video's or the bucket's) of rounds that were all deleted."""
    out = []
    for rec in recs:
        if not isinstance(rec.get("since"), (int, float)) or not isinstance(rec.get("ended"), (int, float)):
            continue
        states = _vod_rounds(spans, rec["since"], rec["ended"])
        if states and all(st == DELETED for st in states):
            out.append(rec)
    return out


def excluded_rounds(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """The rounds excluded from the stats, newest first, one per round (its sides' names joined): the
    local page lists them so they can be put back. Read from the tables under connect()'s views."""
    if "excluded" not in {r[1] for r in db.execute("PRAGMA main.table_info(rounds)")}:
        return []
    groups, of_row = round_groups(db, "main.rounds")
    end_of = {g["round"]: g["end"] for g in groups}
    out: dict[int, dict[str, Any]] = {}
    have = {r[1] for r in db.execute("PRAGMA main.table_info(rounds)")}
    tour = "is_tournament" if "is_tournament" in have else "0 AS is_tournament"
    for r in db.execute("SELECT id, ended_at, date, course, format, side, holes, holes_played, gross, is_league,"
                        f" {tour} FROM main.rounds WHERE excluded = {EXCLUDED} ORDER BY ended_at DESC, id DESC"):
        rnd = of_row.get(r["id"], r["id"])
        g = out.setdefault(rnd, {"round": rnd, "round_end": end_of[rnd], "date": r["date"], "course": r["course"],
                                 "format": r["format"] or "individual", "holes": r["holes"],
                                 "played": r["holes_played"] if r["holes_played"] is not None else r["holes"],
                                 "league": bool(r["is_league"]), "tournament": bool(r["is_tournament"]),
                                 "sides": []})
        g["sides"].append({"side": r["side"], "gross": r["gross"]})
    return list(out.values())


def _editing(db: sqlite3.Connection) -> dict[str, Any]:
    """What the local page needs to offer edits: the excluded rounds, and the rounds (by page number)
    and shots not settled yet, each with when it will be, null while its record is open
    (round_record.unsettled)."""
    rounds, shots = unsettled(db, time.time())
    return {"excluded_rounds": excluded_rounds(db), "unsettled": rounds, "unsettled_shots": shots}


def _ctx(context: str | None) -> str:
    return A._ctx(context)


def _json(text: str | None) -> dict[int, int]:
    try:
        return {int(k): v for k, v in json.loads(text or "{}").items() if isinstance(v, int)}
    except (TypeError, ValueError, AttributeError):
        return {}


def _names(text: str | None) -> list[str] | None:
    try:
        v = json.loads(text or "null")
    except (TypeError, ValueError):
        return None
    return [n for n in v if isinstance(n, str)] if isinstance(v, list) else None


def _pct(hit, tries) -> float | None:
    return round(100.0 * hit / tries, 1) if tries else None


def _card(scores: dict[int, int], pars: dict[int, int]) -> dict[str, Any]:
    """Per-hole score vs par over the holes actually scored."""
    holes = sorted(h for h in scores if h in pars)
    return {
        "holes": [{"hole": h, "par": pars[h], "score": scores[h]} for h in holes],
        "gross": sum(scores[h] for h in holes),
        "par": sum(pars[h] for h in holes),
        "to_par": sum(scores[h] - pars[h] for h in holes),
        "played": len(holes),
    }


def _counts(played) -> bool:
    """As in the booth: nine holes or more, finished or quit after a full nine (a player's card has no
    round length, and a one-hole 19th scored per nine would read as a -9 round)."""
    return (played or 0) >= 9


def round_gimmes(db: sqlite3.Connection) -> dict[int, float]:
    """Each round's gimme radius in feet: the one most of its shots logged (GSPro writes it at every
    shot's setup; the odd shot without one, like an opening tee shot, is outvoted)."""
    per: dict[int, Counter] = {}
    if recorded(db):
        # (in order of each round's first shot to log one, as usual_gimme's ties go)
        logged = sorted((d["gimme_at"], rnd, d["gimme_ft"]) for rnd, d in round_shots(db).items() if d["gimme_ft"])
        per = {rnd: Counter(ft) for _, rnd, ft in logged}
    else:
        of_shot = shot_rounds(db)
        for sid, g in db.execute("SELECT id, gimme_yards FROM shots WHERE gimme_yards IS NOT NULL"):
            if (rnd := of_shot.get(sid)) is not None:
                per.setdefault(rnd, Counter())[round(g * 3, 1)] += 1
    return {rnd: c.most_common(1)[0][0] for rnd, c in per.items()}


def usual_gimme(db: sqlite3.Connection, gimmes: dict[int, float]) -> float | None:
    """The usual gimme radius in feet: the one most league rounds were played with, each round counted
    once (most rounds of any kind before there are league ones). None with no radius on record."""
    _, of_row = round_groups(db)
    league = {of_row.get(i, i) for (i,) in db.execute("SELECT id FROM rounds WHERE is_league = 1")}
    pool = [g for rnd, g in gimmes.items() if rnd in league] or list(gimmes.values())
    return Counter(pool).most_common(1)[0][0] if pool else None


def putt_buckets(gimme_ft: float | None) -> list[tuple[float, float | None]]:
    """Putt length buckets in feet, the first from the usual gimme circle (a putt inside it is conceded,
    never struck): that bucket also takes the rare putt struck from closer, in a round with a smaller
    circle or none, rather than show a bucket that's always empty. From 0 with no radius on record."""
    lo = gimme_ft or 0
    edges = [e for e in PUTT_EDGES if e > lo]
    return list(zip([lo, *edges], [*edges, None]))


def _tee_yards(players: list[dict[int, Counter]], card: set[int]) -> int | None:
    """A side's length from its tees over its card's holes: one total its players' hole labels all
    give (a team on different tees has none), or None when any hole's length is missing."""
    totals = set()
    for yards in players:
        if not card <= yards.keys():
            return None
        totals.add(sum(yards[h].most_common(1)[0][0] for h in card))
    return totals.pop() if len(totals) == 1 else None


def _course_lengths(db: sqlite3.Connection) -> dict[tuple[str, str], tuple[int, int]]:
    """Each course's tee boxes' whole-course lengths from GSPro's course files (store.tee_lengths):
    (course, tee box) -> (yards, holes)."""
    try:
        rows = db.execute("SELECT course, tee, yards, holes FROM tee_lengths").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {((course or "").casefold(), tee): (yards, holes) for course, tee, yards, holes in rows}


def _round_facts(db: sqlite3.Connection) -> dict[str, Any]:
    """What rounds() reads from the shots, the same whatever the context (worked out once a build)."""
    groups, of_row = round_groups(db)
    end_of = {g["round"]: g["end"] for g in groups}
    from . import hole_replay
    tees = hole_replay.round_tees(db)
    who: dict[int, set] = {}
    lengths: dict[tuple[int, str], dict[int, Counter]] = {}
    started: dict[int, float] = {}
    kept: dict[tuple, float | None] = {}
    left: set[tuple] = set()
    mulligans: dict[int, Counter] = {}
    hole_putts: Counter = Counter()
    for night in N.per_night(db, "round_facts", lambda ns: _collect_round_facts(db, ns), memo=False).values():
        for rnd, names in night["who"]:
            who.setdefault(rnd, set()).update(names)
        for rnd, player, hole, yards, n in night["lengths"]:
            lengths.setdefault((rnd, player), {}).setdefault(hole, Counter())[yards] += n
        for rnd, ts in night["started"]:
            if rnd not in started or ts < started[rnd]:
                started[rnd] = ts
        for rnd, player, d in night["kept"]:
            kept[(rnd, player)] = max(kept.get((rnd, player)) or 0, d)
        left.update((rnd, player) for rnd, player in night["left"])
        for rnd, player, n in night["mulligans"]:
            mulligans.setdefault(rnd, Counter())[player] += n
        for rnd, player, hole, n in night["hole_putts"]:
            hole_putts[(rnd, player, hole)] += n
    return {"of_row": of_row, "end_of": end_of, "who": who, "tees": tees, "course_lengths": _course_lengths(db),
            "lengths": lengths, "started": started, "kept": kept, "left": left, "mulligans": mulligans,
            "hole_putts": hole_putts, "sides": Counter(of_row.values())}


def _collect_round_facts(db: sqlite3.Connection, ns: "N.Nights") -> dict[str, dict[str, list]]:
    """Per night, what _round_facts reads from its shots, per round (a round resumed after midnight
    has some on each night): who played, hole lengths read, the first shot, longest drives, mulligans,
    putts per hole."""
    of_shot = shot_rounds(db)
    parts: dict[str, dict[str, dict]] = {d: {k: {} for k in ("who", "lengths", "started", "kept", "mulligans",
                                                             "hole_putts")} | {"left": set()} for d in ns.dates}

    def night(ses: int) -> dict[str, Any]:
        return parts[ns.date[ses]]
    for sid, ses, player in db.execute("SELECT id, session_id, player FROM shots WHERE player IS NOT NULL"
                                       " AND player != 'unknown'" + ns.where):
        if (rnd := of_shot.get(sid)) is not None:
            night(ses)["who"].setdefault(rnd, set()).add(player)
    # Each player's hole lengths from their tees, as GSPro's hole label read with their shots gave them
    # (the most read, past a misread): a side's distance when every hole on its card has one.
    for sid, ses, player, hole in db.execute("SELECT id, session_id, player, hole FROM shots WHERE hole LIKE"
                                             " 'Hole %yards' AND player IS NOT NULL" + ns.where):
        if (rnd := of_shot.get(sid)) is not None and (m := _HOLE_YARDS.match(hole)):
            key = (rnd, player, int(m.group(1)), int(m.group(2)))
            got = night(ses)["lengths"]
            got[key] = got.get(key, 0) + 1
    for sid, ses, ts in db.execute("SELECT id, session_id, ts FROM shots WHERE 1 = 1" + ns.where):
        # each round's first shot, for the page's video links
        if (rnd := of_shot.get(sid)) is not None:
            got = night(ses)["started"]
            if rnd not in got or ts < got[rnd]:
                got[rnd] = ts
    # Each round's longest drive, from its drives in the stats: the one stored at the round's end misses
    # a drive left out since, a total read after the swing, and drives a later rule counts (no club read).
    for sid, ses, player, d, out_ in db.execute(
            f"SELECT id, session_id, player, {_DRIVE}, left_out FROM shots WHERE {_TEE_DRIVER}"
            f" AND mulligan = 0 AND {_DRIVE} IS NOT NULL" + ns.where):
        if (rnd := of_shot.get(sid)) is None:
            continue
        if out_:
            night(ses)["left"].add((rnd, player))
        else:
            got = night(ses)["kept"]
            got[(rnd, player)] = max(got.get((rnd, player)) or 0, d)
    # Each player's mulligans in each round, but a misread putt (handicap_index.COUNTED_MULLIGAN).
    for sid, ses, player in db.execute(f"SELECT id, session_id, player FROM shots WHERE {handicap_index.COUNTED_MULLIGAN}"
                                       " AND player IS NOT NULL AND player != 'unknown'" + ns.where):
        if (rnd := of_shot.get(sid)) is not None:
            got = night(ses)["mulligans"]
            got[(rnd, player)] = got.get((rnd, player), 0) + 1
    # Each player's putts on each hole of each round, counted as three_putts does (their own ball only).
    for sid, ses, player, hole in db.execute(
            "SELECT id, session_id, player, hole FROM shots WHERE attributed = 1 AND mulligan = 0 AND left_out = 0"
            " AND shared_ball = 0 AND COALESCE(fmt, '') NOT IN ('scramble', 'alternate') AND player IS NOT NULL"
            " AND (lie_before = 'green' OR (is_putt = 1 AND lie_before IS NULL))" + ns.where):
        if (rnd := of_shot.get(sid)) is not None and (m := _HOLE.match(hole or "")):
            key = (rnd, player, int(m.group(1)))
            got = night(ses)["hole_putts"]
            got[key] = got.get(key, 0) + 1
    return {d: {"who": [(rnd, sorted(names)) for rnd, names in p["who"].items()],
                "lengths": [(*k, n) for k, n in p["lengths"].items()],
                "started": list(p["started"].items()),
                "kept": [(*k, d_) for k, d_ in p["kept"].items()],
                "left": list(p["left"]),
                "mulligans": [(*k, n) for k, n in p["mulligans"].items()],
                "hole_putts": [(*k, n) for k, n in p["hole_putts"].items()]}
            for d, p in parts.items()}


def rounds(db: sqlite3.Connection, context: str | None = None,
           gimmes: dict[int, float] | None = None) -> list[dict[str, Any]]:
    out = []
    gimmes = round_gimmes(db) if gimmes is None else gimmes
    f = reuse(db, ("round_facts",), lambda: _round_facts(db))
    of_row, end_of, who, tees, course_lengths = f["of_row"], f["end_of"], f["who"], f["tees"], f["course_lengths"]
    lengths, started, kept, left, mulligans = f["lengths"], f["started"], f["kept"], f["left"], f["mulligans"]
    hole_putts, sides = f["hole_putts"], f["sides"]
    for r in db.execute("SELECT * FROM rounds WHERE 1=1" + _ctx(context) + " ORDER BY ended_at DESC, id DESC"):
        card = _card(_json(r["scores"]), _json(r["hole_pars"]))
        rnd = of_row.get(r["id"], r["id"])
        # Who played this side: on record since rounds kept it; before, the side itself when it's a
        # player, or everyone with a shot when the round had just the one side.
        played_by = _names(r["players"]) if "players" in r.keys() else None
        if not played_by:
            here = who.get(rnd, set())
            played_by = [r["side"]] if r["side"] in here else sorted(here) if sides[rnd] == 1 and here else None
        holes = r["holes"]
        played = r["holes_played"] if r["holes_played"] is not None else holes
        complete = bool(r["complete"])
        if not complete and holes == 18 and played == 9:
            # Stopped right at the turn: a nine with the back left switched on in GSPro, not a quit.
            holes, complete = 9, True
        tee_yards = _tee_yards([lengths[(rnd, p)] for p in played_by or [] if (rnd, p) in lengths],
                               {h["hole"] for h in card["holes"]}) if complete and len(card["holes"]) == holes else None
        # The tee's full length on the course's scorecard, however much of it this round played.
        course_yards, course_holes = course_lengths.get(((r["course"] or "").casefold(), tees.get(rnd)),
                                                        (None, None))
        longest = r["longest_drive"]
        # (a team side's players aren't on record per round, so its stored drive stands)
        if (rnd, r["side"]) in kept or (rnd, r["side"]) in left:
            longest = kept.get((rnd, r["side"]))
        # A one-player side's one-, two- and three-or-more-putt holes, when its shots add up to its putts.
        putt_split = None
        if played_by and len(played_by) == 1 and r["putts"]:
            per_hole = [hole_putts[(rnd, played_by[0], h["hole"])] for h in card["holes"]]
            if sum(per_hole) == r["putts"]:
                putt_split = [sum(n == 1 for n in per_hole), sum(n == 2 for n in per_hole),
                              sum(n >= 3 for n in per_hole)]
        out.append({
            "id": r["id"], "round": of_row.get(r["id"], r["id"]), "session": r["session_id"], "ended": r["ended_at"], "date": r["date"],
            "started": started.get(of_row.get(r["id"], r["id"])), "round_end": end_of.get(rnd),
            "course": r["course"],
            # who played in it (a team side's players too), so the page can show one player's rounds
            "players": sorted(who.get(rnd, set()) | ({r["side"]} if r["side"] else set()) | set(played_by or [])), "format": r["format"] or "individual",
            "side": r["side"], "side_players": played_by, "team": r["team"], "holes": holes,
            "played": played, "complete": complete, "league": bool(r["is_league"]),
            "tournament": bool(r["is_tournament"]) and not r["is_league"],
            "gross": card["gross"] if card["played"] else r["gross"],
            "to_par": card["to_par"] if card["played"] else None,
            "net_to_par": _net_to_par(r, card) if r["is_league"] else None,
            "fir": [r["fir_hit"], r["fir_tries"]], "gir": [r["gir_hit"], r["gir_tries"]],
            "scramble": [r["scramble_hit"], r["scramble_tries"]], "sand": [r["sand_hit"], r["sand_tries"]],
            "putts": r["putts"], "putt_split": putt_split, "penalties": r["penalties"], "longest_drive": longest,
            "card": card["holes"], "gimme_ft": gimmes.get(rnd), "tee": tees.get(rnd),
            "tee_yards": tee_yards, "course_yards": course_yards, "course_holes": course_holes,
            # this side's players' mulligans (a team side's players aren't always on record: its own)
            "mulligans": {p: mulligans.get(rnd, Counter())[p] for p in played_by or [r["side"]] if p},
        })
    return out


def _net_to_par(r, card: dict) -> int | None:
    """A league round's net to par: its scorecard's to par less the handicap strokes it was given
    (what was recorded: gross to par less net), so a corrected card moves the net with it."""
    if r["net_to_par"] is None:
        return None
    if not card["played"] or r["gross"] is None or not r["par"]:
        return r["net_to_par"]
    return card["to_par"] - (r["gross"] - r["par"] - r["net_to_par"])


def _shot_brief(st, h: dict, sg: dict) -> dict[str, Any]:
    return {"id": st["id"], "ts": st["ts"], "hole": h["hole"], "shot_number": st["shot_number"], "club": st["club"],
            "lie": st["lie_before"], "to_pin": st["to_pin_yards"], "putt": bool(st["is_putt"]),
            "outcome": st["outcome"], "finish": st["finish_yards"], "category": sg["category"], "sg": round(sg["sg"], 2)}


def round_sg_totals(holes: list[dict], sg: list[dict], of_shot: dict[int, int]) -> dict[tuple, dict[str, Any]]:
    """Per (round, player), from `holes` and their strokes gained (A.strokes_gained): what the round
    summary's strokes gained and best and worst shots come of (round_sg_from). Totals of some holes
    add up (add_round_sg) to theirs together, in the order they were played."""
    sg_of = {s["shot_id"]: s for s in sg}
    out: dict[tuple, dict[str, Any]] = {}
    for h in holes:
        rnd = of_shot.get(h["strokes"][0]["id"])
        if rnd is None:
            continue
        add_round_sg(out, {(rnd, h["player"]): {"cats": {}, "shots": 0, "best": None, "worst": None}})
        for st in h["strokes"]:
            if st["id"] in sg_of:
                got = sg_of[st["id"]]
                brief = _shot_brief(st, h, got)
                add_round_sg(out, {(rnd, h["player"]): {"cats": {got["category"]: [got["sg"]]}, "shots": 1,
                                                        "best": brief, "worst": brief}})
    return out


def add_round_sg(into: dict[tuple, dict[str, Any]], part: dict[tuple, dict[str, Any]]) -> None:
    for key, got in part.items():
        m = into.get(key)
        if m is None:
            into[key] = m = {"cats": {}, "shots": 0, "best": None, "worst": None}
        for cat, partials in got["cats"].items():
            N.exact_merge(m["cats"].setdefault(cat, []), partials)
        m["shots"] += got["shots"]
        # the first of the best, and the last of the worst (as a stable sort ranks them)
        if got["best"] is not None and (m["best"] is None or got["best"]["sg"] > m["best"]["sg"]):
            m["best"] = got["best"]
        if got["worst"] is not None and (m["worst"] is None or got["worst"]["sg"] <= m["worst"]["sg"]):
            m["worst"] = got["worst"]


def round_sg_from(m: dict[str, Any]) -> dict[str, Any]:
    """A round summary's strokes gained by category and its best and worst shots by strokes gained."""
    return {
        "sg": {**{c: round(math.fsum(m["cats"].get(c, ())), 2) for c in A.SG_CATEGORIES},
               "total": round(math.fsum(x for p in m["cats"].values() for x in p), 2)} if m["shots"] else None,
        "best_shot": m["best"],
        "worst_shot": m["worst"] if m["shots"] > 1 and m["worst"]["sg"] < 0 else None,
    }


def add_round_summaries(db: sqlite3.Connection, rounds_: list[dict[str, Any]], round_sg: dict[tuple, dict[str, Any]],
                        people: list[dict[str, Any]]) -> None:
    """A one-player side's round of nine holes or more gets a `summary`: the round against the player's season (score per
    nine, fairways, greens, putts per hole), its strokes gained by category, its best and worst shots
    by strokes gained (`round_sg`: round_sg_totals', merged), and its best and worst holes against par."""
    season = {p["name"]: p for p in people}
    for r in rounds_:
        got = round_sg.get((r["round"], r["side"]))
        got = got and round_sg_from(got)
        p = season.get(r["side"])
        if not got or not p or r["format"] in SHARED_BALL or not _counts(r["played"]):
            continue
        played = r["played"] or len(r["card"]) or 0
        row = db.execute("SELECT * FROM rounds WHERE id = ?", (r["id"],)).fetchone()
        putt_holes = row["putt_holes"]
        # As the season average counts it: a round quit part-way through by the nine it finished.
        nine = row["nine_to_par"] if "nine_to_par" in row.keys() else None
        per_nine = nine if nine is not None else (
            round(r["to_par"] * 9 / played, 1) if r["to_par"] is not None and played else None)
        vs = [
            {"label": "Score / 9", "round": per_nine,
             "season": p["avg_to_par_9"], "better": -1, "fmt": "to_par"},
            {"label": "Fairways", "round": _pct(*r["fir"]), "season": p["fir_pct"], "better": 1, "fmt": "pct"},
            {"label": "Greens", "round": _pct(*r["gir"]), "season": p["gir_pct"], "better": 1, "fmt": "pct"},
            {"label": "Putts / hole", "round": round(r["putts"] / putt_holes, 2) if r["putts"] is not None and putt_holes else None,
             "season": p["putts_per_hole"], "better": -1, "fmt": "num"},
        ]
        card = sorted(r["card"], key=lambda c: c["score"] - c["par"])
        spread = len(card) > 1 and card[0]["score"] - card[0]["par"] != card[-1]["score"] - card[-1]["par"]
        r["summary"] = {
            "vs": [v for v in vs if v["round"] is not None],
            "sg": got["sg"], "best_shot": got["best_shot"], "worst_shot": got["worst_shot"],
            "best_hole": card[0] if spread else None,
            "worst_hole": card[-1] if spread else None,
        }


def players(db: sqlite3.Connection, context: str | None = None,
            gimme_ft: float | None = None) -> list[dict[str, Any]]:
    """One row per player: season scoring, ball-striking, driving and putting. A player is someone in a
    recorded round: with a card of their own, or shots in one (an alternate-shot or shared-ball round
    files no card per player). Shots outside any round (a session whose round was never recorded)
    don't make a player on their own, so deleting someone's rounds takes them off the list."""
    merged = _players_merged(db, context)
    return [_player(name, merged.get(name) or _new_player(), gimme_ft)
            for name in sorted(n for n, m in merged.items() if m["listed"])]


def player(db: sqlite3.Connection, name: str, context: str | None = None,
           gimme_ft: float | None = None) -> dict[str, Any]:
    """`gimme_ft` is the usual gimme radius (usual_gimme), where the putting buckets start."""
    return _player(name, _players_merged(db, context).get(name) or _new_player(), gimme_ft)


# First-putt length bucket edges in feet for the three-putt chart.
THREE_PUTT_EDGES = (10, 20, 30, 40, 60)
_RATES = ("fir", "gir", "scramble", "sand", "putts")


def _new_player() -> dict[str, Any]:
    """A player's counts over some nights (_collect_players), everything players() shows worked out
    from them."""
    return {"listed": False, **{k: [0, 0] for k in _RATES}, "outcomes": [0, 0, 0, 0, 0],
            "by_par": {3: [0, 0], 4: [0, 0], 5: [0, 0]}, "rounds": [], "drive": [0, [], None], "shots": 0,
            "clubs": {}, "putts_by": [[0, 0] for _ in range(len(PUTT_EDGES) + 1)],
            # three-putts by holes in its context, and (the window taking in every context) in any
            "three": [0, 0], "three_by": [[0, 0] for _ in range(len(THREE_PUTT_EDGES) + 1)],
            "three_any": [0, 0], "three_any_by": [[0, 0] for _ in range(len(THREE_PUTT_EDGES) + 1)]}


def _players_merged(db: sqlite3.Connection, context: str | None) -> dict[str, dict[str, Any]]:
    """Each player's counts over every night in `context`. Every context's at once, from one read of
    the nights' partials, which aren't kept after."""
    def run() -> dict[str | None, dict[str, dict[str, Any]]]:
        nights = N.per_night(db, "players", lambda ns: _collect_players(db, ns), memo=False)
        return {c: _players_merge(nights, c) for c in (None, *N.CONTEXTS)}
    return reuse(db, ("players_merged",), run)[context if context in N.CONTEXTS else None]


def _players_merge(nights: dict[str, Any], context: str | None) -> dict[str, dict[str, Any]]:
    keep = N.wanted(context)
    out: dict[str, dict[str, Any]] = {}
    for night in nights.values():
        for ctx, people in night.items():
            if not keep(ctx):
                continue
            for name, part in people.items():
                m = out.setdefault(name, _new_player())
                m["listed"] = m["listed"] or part["listed"]
                for k in (*_RATES, "outcomes", "three", "three_any"):
                    N.add(m[k], part[k])
                for k, v in part["by_par"].items():
                    N.add(m["by_par"][k], v)
                m["rounds"] += part["rounds"]
                n, total, best = part["drive"]
                m["drive"][0] += n
                N.exact_merge(m["drive"][1], total)
                if best is not None and (m["drive"][2] is None or best > m["drive"][2]):
                    m["drive"][2] = best
                m["shots"] += part["shots"]
                for club, (cn, fin, low, pin) in part["clubs"].items():
                    c = m["clubs"].setdefault(club, [0, [], None, []])
                    c[0] += cn
                    N.exact_merge(c[1], fin)
                    c[2] = low if c[2] is None or low < c[2] else c[2]
                    N.exact_merge(c[3], pin)
                for into, got in ((m["putts_by"], part["putts_by"]), (m["three_by"], part["three_by"]),
                                  (m["three_any_by"], part["three_any_by"])):
                    for a, b in zip(into, got):
                        N.add(a, b)
    for m in out.values():
        m["rounds"].sort(key=lambda r: (r[0], r[1]))
        if context not in N.CONTEXTS:
            m["three"], m["three_by"] = m["three_any"], m["three_any_by"]
    return out


def _collect_players(db: sqlite3.Connection, ns: "N.Nights") -> dict[str, dict[str, dict[str, Any]]]:
    """Per night, per context, per player: the counts behind players()."""
    out: dict[str, dict[str, dict[str, Any]]] = {d: {} for d in ns.dates}

    def of(session_id: int, r: Any, name: str) -> dict[str, Any]:
        ctx = N.context_of(r["is_league"], r["is_tournament"])
        return out[ns.date[session_id]].setdefault(ctx, {}).setdefault(name, _new_player())

    round_of = player_round_of(db)
    for r in db.execute("SELECT * FROM player_rounds WHERE player IS NOT NULL" + ns.where + " ORDER BY ended_at, id"):
        p = of(r["session_id"], r, r["player"])
        p["listed"] = True
        p["fir"][0] += r["fir_hit"] or 0
        p["fir"][1] += r["fir_tries"] or 0
        if (r["fmt"] or "") in SHARED_BALL:
            continue
        for key, hit, tries in (("gir", "gir_hit", "gir_tries"), ("scramble", "scramble_hit", "scramble_tries"),
                                ("sand", "sand_hit", "sand_tries"), ("putts", "putts", "putt_holes")):
            p[key][0] += r[hit] or 0
            p[key][1] += r[tries] or 0
        card = _card(_json(r["scores"]), _json(r["hole_pars"]))
        for h in card["holes"]:
            d = h["score"] - h["par"]
            p["outcomes"][0] += 1
            p["outcomes"][1] += d <= -1
            p["outcomes"][2] += d == 0
            p["outcomes"][3] += d == 1
            p["outcomes"][4] += d >= 2
            if h["par"] in p["by_par"]:
                p["by_par"][h["par"]][0] += d
                p["by_par"][h["par"]][1] += 1
        if card["played"] and _counts(r["holes_played"] or card["played"]):
            # A round quit part-way through contributes the nine it finished; prorating the odd holes
            # onto a nine would rank it by a score nobody shot. Older rows have no stored nine.
            per_nine = r["nine_to_par"] if "nine_to_par" in r.keys() and r["nine_to_par"] is not None \
                else card["to_par"] * 9.0 / card["played"]
            p["rounds"].append((r["ended_at"], r["id"], per_nine, ns.date[r["session_id"]], r["course"],
                                card["played"], card["gross"], card["to_par"],
                                round_of(r["session_id"], r["course"], r["ended_at"])))
    in_round = None
    putt_lo = [0.0, *(e / 3.0 for e in PUTT_EDGES)]
    for r in db.execute(
            f"SELECT id, session_id, player, is_league, is_tournament, ({_TEE_DRIVER}) AS tee_driver,"
            f" {_DRIVE} AS drive, is_putt, outcome, to_pin_yards, finish_yards, club FROM shots"
            " WHERE player IS NOT NULL AND attributed = 1 AND mulligan = 0 AND left_out = 0" + ns.where):
        p = of(r["session_id"], r, r["player"])
        if not p["listed"] and r["player"] != "unknown":
            if in_round is None:
                in_round = shot_rounds(db)
            p["listed"] = r["id"] in in_round
        p["shots"] += 1
        if r["tee_driver"] and r["drive"] is not None:
            p["drive"][0] += 1
            N.exact_add(p["drive"][1], r["drive"])
            if p["drive"][2] is None or r["drive"] > p["drive"][2]:
                p["drive"][2] = r["drive"]
        pin = r["to_pin_yards"]
        if (r["is_putt"] == 0 and r["finish_yards"] is not None and r["club"] is not None
                and r["club"] not in ("DR", "PT") and pin is not None and pin <= APPROACH_MAX_YARDS):
            c = p["clubs"].setdefault(r["club"], [0, [], None, []])
            c[0] += 1
            N.exact_add(c[1], r["finish_yards"])
            c[2] = r["finish_yards"] if c[2] is None or r["finish_yards"] < c[2] else c[2]
            N.exact_add(c[3], pin)
        if (r["is_putt"] == 1 and r["outcome"] is not None and r["outcome"] != "gimme" and pin is not None
                and 0 <= pin < 10_000 / 3.0):
            i = max(i for i, lo in enumerate(putt_lo) if pin >= lo)
            p["putts_by"][i][0] += 1
            p["putts_by"][i][1] += r["outcome"] == "holed"
    # As Store.three_putt_rate: a hole's putts are its strokes from the green (a conceded tap-in's
    # 'gimme' row counts, as on the card), grouped by session + course + hole, the first by time giving
    # the length. Shared-ball holes are left out (the team's putts aren't one player's).
    # A hole in one context is apart from the same hole in another; with every context, it's one hole
    # (filed under its first putt's context, and only counted when they're all taken in).
    edges = [0, *THREE_PUTT_EDGES]
    ctx_sql = "CASE WHEN is_league = 1 THEN 1 WHEN is_league = 0 AND is_tournament = 1 THEN 2 WHEN is_league = 0 THEN 3 END"
    for key, apart in (("three", f", {ctx_sql}"), ("three_any", "")):
        for r in db.execute(
                f"""WITH hole_putts AS (
                       SELECT session_id, player, is_league, is_tournament, to_pin_yards,
                              ROW_NUMBER() OVER (PARTITION BY player, session_id, course, hole{apart} ORDER BY ts) AS putt_no,
                              COUNT(*) OVER (PARTITION BY player, session_id, course, hole{apart}) AS putts
                       FROM shots WHERE player IS NOT NULL AND attributed = 1 AND mulligan = 0 AND left_out = 0
                             AND shared_ball = 0 AND COALESCE(fmt, '') NOT IN ('scramble', 'alternate') AND hole IS NOT NULL
                             AND (lie_before = 'green' OR (is_putt = 1 AND lie_before IS NULL)){ns.where}
                   )
                   SELECT session_id, player, is_league, is_tournament, to_pin_yards * 3 AS feet, putts
                   FROM hole_putts WHERE putt_no = 1"""):
            p = of(r["session_id"], r, r["player"])
            three = r["putts"] >= 3
            p[key][0] += three
            p[key][1] += 1
            if (feet := r["feet"]) is not None:
                for i, (lo, hi) in enumerate(zip(edges, [*THREE_PUTT_EDGES, None])):
                    if feet >= lo and (hi is None or feet < hi):
                        p[key + "_by"][i][0] += 1
                        p[key + "_by"][i][1] += three
    return out


def _player(name: str, m: dict[str, Any], gimme_ft: float | None) -> dict[str, Any]:
    """players()' row for a player, from their counts (_players_merged)."""
    nines = [r[2] for r in m["rounds"]]
    tot = {k: m[k] for k in _RATES}
    fir = m["fir"]
    holes, birdies, pars_made, bogeys, doubles = m["outcomes"]
    by_par = m["by_par"]
    p: dict[str, Any] = {"name": name}
    p.update({
        "rounds": len(nines),
        "avg_to_par_9": round(sum(nines) / len(nines), 1) if nines else None,
        "best_to_par_9": round(min(nines), 1) if nines else None,
        "fir_pct": _pct(*fir), "fir": list(fir),
        "gir_pct": _pct(*tot["gir"]), "gir": list(tot["gir"]),
        "scramble_pct": _pct(*tot["scramble"]), "scramble": list(tot["scramble"]),
        "sand_pct": _pct(*tot["sand"]), "sand": list(tot["sand"]),
        "putts_per_hole": round(tot["putts"][0] / tot["putts"][1], 2) if tot["putts"][1] else None,
        "holes": holes,
        "birdie_pct": _pct(birdies, holes), "par_pct": _pct(pars_made, holes),
        "bogey_pct": _pct(bogeys, holes), "double_pct": _pct(doubles, holes),
        "par_avg": {str(k): round(v[0] / v[1], 2) if v[1] else None for k, v in by_par.items()},
        "history": [{"date": d, "course": course, "played": played, "gross": gross, "to_par": to_par, "round": rnd}
                    for _, _, _, d, course, played, gross, to_par, rnd in m["rounds"]],
        # The counts behind the rates, so the page can pool any group of players (the league average).
        "totals": {"nines": sum(nines), "putts": list(tot["putts"]),
                   "outcomes": [birdies, pars_made, bogeys, doubles],
                   "by_par": {str(k): list(v) for k, v in by_par.items()}},
    })
    n, total, best = m["drive"][0], math.fsum(m["drive"][1]), m["drive"][2]
    p["drives"] = n
    p["avg_drive"] = round(total / n) if n else None
    p["longest_drive"] = round(best) if best is not None else None
    p["totals"]["drive_yards"] = total if n else 0
    p["shots"] = m["shots"]
    p["clubs"] = sorted(({"club": club, "n": cn, "avg_finish": _sql_round(math.fsum(fin) / cn, 1),
                          "best_finish": _sql_round(low, 1), "avg_from": _sql_round(math.fsum(pin) / cn, 0)}
                         for club, (cn, fin, low, pin) in m["clubs"].items()),
                        key=lambda c: (-c["avg_from"], c["club"]))
    # The first bucket starts at the usual gimme circle, and takes in the rare putt struck from closer.
    p["putting"] = []
    buckets = putt_buckets(gimme_ft)
    merged = [0, 0]
    first = len(PUTT_EDGES) + 1 - len(buckets)   # the intervals the first bucket takes in, less one
    for i, got in enumerate(m["putts_by"]):
        N.add(merged, got)
        if i < first:
            continue
        lo, hi = buckets[i - first]
        att, made = merged
        p["putting"].append({"range": f"{lo:g}-{hi} ft" if hi else f"{lo:g}+ ft", "attempts": att,
                             "made": made, "pct": _pct(made, att)})
        merged = [0, 0]
    total3 = [m["three"][0], m["three"][1]]
    p.update({"three_putt_pct": _pct(*total3), "three_putts": total3, "three_putting": [
        {"range": f"{lo}-{hi} ft" if hi else f"{lo}+ ft", "holes": h, "three_putts": n3, "pct": _pct(n3, h)}
        for (lo, hi), (h, n3) in zip(zip([0, *THREE_PUTT_EDGES], [*THREE_PUTT_EDGES, None]), m["three_by"])]})
    return p


_ROUNDER = sqlite3.connect(":memory:", check_same_thread=False)
_ROUNDER_LOCK = threading.Lock()


def _sql_round(x: float, digits: int) -> float:
    """SQLite's ROUND, which the page has always shown (half away from zero, a float)."""
    with _ROUNDER_LOCK:
        return _ROUNDER.execute("SELECT ROUND(?, ?)", (x, digits)).fetchone()[0]


def three_putts(db: sqlite3.Connection, name: str, context: str | None = None) -> dict[str, Any]:
    """Holes three-putted (or worse), overall and by the first putt's length (player()'s)."""
    p = player(db, name, context)
    return {k: p[k] for k in ("three_putt_pct", "three_putts", "three_putting")}


def _date(db: sqlite3.Connection, session_id: int) -> str | None:
    r = db.execute("SELECT date FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return r[0] if r else None


ROLES = {"pbp": "play-by-play", "color": "color analyst"}


def predictions(db: sqlite3.Connection, names: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Each commentator's make/miss call record, by on-air name (the DB keeps the persona key)."""
    names = names or {}
    return [{"commentator": names.get(r["commentator"], r["commentator"]),
             "role": ROLES.get(r["commentator"], r["commentator"]), "calls": r["n"], "right": r["hits"] or 0, "pct": _pct(r["hits"] or 0, r["n"])}
            for r in db.execute("SELECT commentator, COUNT(*) n, SUM(correct) hits FROM predictions "
                                "WHERE correct IS NOT NULL GROUP BY commentator ORDER BY commentator")]


# The closest-approach records' bands, by the distance the shot was hit from (yards, over lo up to hi);
# together they cover every approach (season_analytics.APPROACH_SQL).
APPROACH_BANDS = ((A.AROUND_GREEN_YARDS, 100), (100, 150), (150, 200), (200, A.APPROACH_MAX_YARDS))


def records(db: sqlite3.Connection, context: str | None = None, player: str | None = None,
            since: str | None = None) -> dict[str, Any]:
    return records_table(db, context, {"": since})(player, "")


def _record_queries() -> list[tuple[str, tuple, Callable[[Any], tuple]]]:
    """Each record's shots (the longest drive, the closest approach from each band, the longest putt
    holed), with its ranking key: lowest first, a tie going to the earlier shot."""
    base = " AND player IS NOT NULL AND player != 'unknown' AND attributed = 1 AND mulligan = 0 AND left_out = 0"
    cols = "session_id, is_league, is_tournament"
    out: list[tuple[str, tuple, Callable[[Any], tuple]]] = [
        (f"SELECT id, ts, player, {_DRIVE} AS v, course, hole, {cols} FROM shots WHERE {_TEE_DRIVER}"
         f" AND {_DRIVE} IS NOT NULL" + base, (), lambda r: (-r["v"], r["ts"], r["id"]))]
    for lo, hi in APPROACH_BANDS:   # 1 ft from 80 yd isn't the shot 2 ft from 200 is: a record for each
        out.append((f"SELECT id, ts, player, finish_yards AS v, club, outcome, ROUND(to_pin_yards) AS from_yd, course,"
                    f" hole, {cols} FROM shots WHERE {A.APPROACH_SQL} AND to_pin_yards > ? AND to_pin_yards <= ?"
                    " AND finish_yards IS NOT NULL" + base, (lo, hi), lambda r: (r["v"], r["ts"], r["id"])))
    out.append((f"SELECT id, ts, player, to_pin_yards AS v, course, hole, {cols} FROM shots WHERE is_putt = 1"
                " AND outcome = 'holed' AND to_pin_yards IS NOT NULL" + base, (), lambda r: (-r["v"], r["ts"], r["id"])))
    return out


def _collect_records(db: sqlite3.Connection, ns: "N.Nights") -> dict[str, dict[str, list[dict]]]:
    """Per night, per context, each record's best shot by each player that night (as [key, row]): all a
    range's record can take from the night."""
    out: dict[str, dict[str, list[dict]]] = {d: {} for d in ns.dates}
    queries = _record_queries()
    for i, (sql, args, rank) in enumerate(queries):
        for r in db.execute(sql + ns.where, args):
            ctx = N.context_of(r["is_league"], r["is_tournament"])
            got = out[ns.date[r["session_id"]]].setdefault(ctx, [{} for _ in queries])[i]
            key = rank(r)
            if r["player"] not in got or key < tuple(got[r["player"]][0]):
                row = {c: r[c] for c in r.keys() if c not in ("session_id", "is_league", "is_tournament")}
                got[r["player"]] = [list(key), row]
    return out


def records_table(db: sqlite3.Connection, context: str | None, starts: dict[str, str | None],
                  nights: dict[str, Any] | None = None) -> Callable[[str | None, str], dict[str, Any]]:
    """records() for every player (and everyone, None) and each of the date ranges `starts` (key: first
    night, None for all time), from each night's bests: returns pick(player, key). A tie goes to the
    earlier shot. `nights`: their partials, read already."""
    keep = N.wanted(context)
    n = len(_record_queries())
    if nights is None:
        nights = N.per_night(db, "records", lambda ns: _collect_records(db, ns))
    # The ranges are the nights from their start on: going back from the newest night, each range's
    # bests are the running bests once every night from its start has been taken in.
    bests: list[dict[str, tuple]] = [{} for _ in range(n)]
    got: dict[str, list[dict[str, tuple]]] = {}
    order = sorted(starts.items(), key=lambda kv: kv[1] or "", reverse=True)

    def snapshot(key: str) -> None:
        got[key] = [dict(b) for b in bests]

    i = 0
    for d in reversed(list(nights)):
        while i < len(order) and order[i][1] is not None and d < order[i][1]:
            snapshot(order[i][0])
            i += 1
        for ctx, parts in nights[d].items():
            if not keep(ctx):
                continue
            for b, part in zip(bests, parts):
                for player, (key, row) in part.items():
                    key = tuple(key)
                    if player not in b or key < b[player][0]:
                        b[player] = (key, row)
    for key, _ in order[i:]:
        snapshot(key)
    allbest = {k: [min(b.values(), key=lambda kr: kr[0]) if b else None for b in got[k]] for k in got}

    def best(key: str, j: int, player: str | None) -> dict | None:
        kr = allbest[key][j] if player is None else got[key][j].get(player)
        return dict(kr[1]) if kr else None

    def pick(player: str | None, key: str) -> dict[str, Any]:
        return {"longest_drive": best(key, 0, player),
                "approaches": [{"from": lo, "to": hi, "best": best(key, j + 1, player)}
                               for j, (lo, hi) in enumerate(APPROACH_BANDS)],
                "longest_putt": best(key, n - 1, player)}
    return pick


def record_shots(records_: dict[str, Any]) -> list[dict]:
    """The shots holding one records() result's records."""
    shots = [records_.get("longest_drive"), records_.get("longest_putt")]
    shots += [b["best"] for b in records_.get("approaches") or []]
    return [s for s in shots if s]


# The Records tab's date ranges, in months back from the build (None: all time).
PERIODS = {"3m": 3, "6m": 6, "9m": 9, "1y": 12, "all": None}


def season_start(start: str | None, today: date | None = None) -> str | None:
    """The first day (YYYY-MM-DD) of the season under way, for a league whose season starts every
    year on `start` (MM-DD, publish.season_start): this year's start once it's come, else last year's."""
    if not start:
        return None
    today = today or date.today()
    m, d = (int(x) for x in start.split("-"))

    def on(y: int) -> date:
        return date(y, m, min(d, calendar.monthrange(y, m)[1]))   # 02-29 in other years: the 28th

    return (on(today.year) if on(today.year) <= today else on(today.year - 1)).isoformat()


def period_starts(today: date | None = None, season: str | None = None) -> dict[str, str | None]:
    """Each range's first night (YYYY-MM-DD), the same day of the month that many months back, and
    with a season start (MM-DD), "season": since the season under way began."""
    today = today or date.today()
    out: dict[str, str | None] = {"season": season_start(season, today)} if season else {}
    for key, months in PERIODS.items():
        if months is None:
            out[key] = None
            continue
        y, m = divmod(today.year * 12 + today.month - 1 - months, 12)
        m += 1
        out[key] = date(y, m, min(today.day, calendar.monthrange(y, m)[1])).isoformat()
    return out


def _usual(db: sqlite3.Connection) -> float | None:
    return usual_gimme(db, reuse(db, ("round_gimmes",), lambda: round_gimmes(db)))


def _filters(db: sqlite3.Connection) -> tuple[dict[str, list], bool, bool]:
    """The page's filters (the nights with shots, newest first, and the courses, clubs and lies played)
    and whether there are any league rounds, and any tournament ones. With no league rounds at all (no
    league file, or the season hasn't started) the page drops the League filter, and with no tournament
    rounds either, the filter and tags, since every round would be practice."""
    def run() -> tuple[dict[str, list], bool, bool]:
        dates, seen, league, tournament = [], {"courses": set(), "clubs": set(), "lies": set()}, False, False
        for d, night in N.per_night(db, "filters", lambda ns: _collect_filters(db, ns), memo=False).items():
            if night["shots"]:
                dates.append(d)
            for k in seen:
                seen[k].update(night[k])
            league = league or night["league"]
            tournament = tournament or night["tournament"]
        return {"dates": dates[::-1], **{k: sorted(v, key=_sql_order) for k, v in seen.items()}}, league, tournament
    filters, league, tournament = reuse(db, ("filters",), run)
    return {k: list(v) for k, v in filters.items()}, league, tournament


def _sql_order(v: Any) -> tuple:
    """SQLite's ORDER BY for a column's values: numbers before text, text by its bytes (code points)."""
    return (isinstance(v, str), v)


def _collect_filters(db: sqlite3.Connection, ns: "N.Nights") -> dict[str, dict[str, Any]]:
    out = {d: {"shots": False, "courses": [], "clubs": [], "lies": [], "league": False, "tournament": False}
           for d in ns.dates}
    for sid, course, club, lie, league, tournament in db.execute(
            "SELECT DISTINCT session_id, course, club, lie_before, is_league = 1, is_league = 0 AND is_tournament = 1"
            " FROM shots WHERE 1=1" + ns.where):
        night = out[ns.date[sid]]
        night["shots"] = True
        for k, v in (("courses", course), ("clubs", club), ("lies", lie)):
            if v is not None and v not in night[k]:
                night[k].append(v)
        night["league"] = night["league"] or league == 1
        night["tournament"] = night["tournament"] or tournament == 1
    for sid, league, tournament in db.execute(
            "SELECT DISTINCT session_id, is_league = 1, is_league = 0 AND is_tournament = 1 FROM rounds WHERE 1=1"
            + ns.where):
        night = out[ns.date[sid]]
        night["league"] = night["league"] or league == 1
        night["tournament"] = night["tournament"] or tournament == 1
    return out


def _booth(db: sqlite3.Connection, names: dict[str, str] | None) -> dict[str, Any]:
    """The booth tab; its signature phrases go by the last year (the 1y range's nights), so working
    them out never grows with the years kept."""
    since = period_starts()["1y"]
    return reuse(db, ("booth", tuple(sorted((names or {}).items())), since), lambda: A.booth(db, names, since))


def _collect_holes(db: sqlite3.Connection, ns: "N.Nights", usual: float | None) -> dict[str, dict[str, Any]]:
    """Per night, per context ("" for all of them: a hole is grouped from its strokes in the context),
    what comes of the holes played (A.holes_played): strokes gained, recovery, the trend point, each
    round's strokes gained, and the highlights (those that rank kept to each player's best of the night,
    which is all a range's best can take from it)."""
    rows = db.execute(A.HOLE_ROWS_SQL.format(A.HOLE_ROWS_WHERE + ns.where)).fetchall()
    of_shot = shot_rounds(db) if rows else {}
    out: dict[str, dict[str, Any]] = {d: {} for d in ns.dates}
    for key in ("", *N.CONTEXTS):
        ctx = key or None
        holes = A.holes_of(rows if not key else [r for r in rows if A._IN_CONTEXT[key](r)])
        sg = A.strokes_gained(holes, usual)
        per = {d: {"sg": {}, "rec": {}, "trend": {}, "rounds": {}, "hl": {
            "moments": [], "streaks": [], "bounce": [], "blowups": [], "tops": {n: [] for n in A.TOPS}}}
            for d in ns.dates}
        sg_by: dict[str, list[dict]] = {}
        for s in sg:
            sg_by.setdefault(s["date"], []).append(s)
        holes_by: dict[str, list[dict]] = {}
        for h in holes:
            holes_by.setdefault(h["date"], []).append(h)
        for d, hs in holes_by.items():
            per[d]["sg"] = A.sg_totals(hs, sg_by.get(d, []))
            per[d]["rec"] = A.recovery_totals(hs)
            per[d]["rounds"] = round_sg_totals(hs, sg_by.get(d, []), of_shot)
        for name, points in A.trends(db, holes, ctx, usual, sg, where=ns.where).items():
            for pt in points:
                per[pt["date"]]["trend"][name] = pt
        moments, streaks, bounce, blowups, tops = A.highlight_rows(db, holes, ctx, ns.where)
        for m in moments:
            per[m["date"]]["hl"]["moments"].append(m)
        for name, xs, rank, k in (("streaks", streaks, lambda s: -s["length"], 10), ("bounce", bounce, None, 10),
                                  ("blowups", blowups, lambda b: -b["over"], 5)):
            mine: dict[tuple, list[dict]] = {}
            for x in xs:
                mine.setdefault((x["date"], x["player"]), []).append(x)
            keep = {id(x) for got in mine.values() for x in (sorted(got, key=rank) if rank else got)[:k]}
            for x in xs:
                if id(x) in keep:
                    per[x["date"]]["hl"][name].append(x)
        for name, rs in tops.items():
            seen: Counter = Counter()
            for r in rs:
                if seen[(r["date"], r["player"])] < 5:
                    seen[(r["date"], r["player"])] += 1
                    per[r["date"]]["hl"]["tops"][name].append(r)
        for d in ns.dates:
            out[d][key] = per[d]
    return out


def _holes_merged(db: sqlite3.Connection, context: str | None) -> dict[str, Any]:
    """_collect_holes' nights merged, in `context`: sg_summary, recovery and trends as A's give them,
    round_sg_totals' rounds, and highlight_rows' lists. Every context's at once, from one read of the
    nights' partials, which aren't kept after."""
    usual = _usual(db)

    def run() -> dict[str, dict[str, Any]]:
        nights = N.per_night(db, "holes", lambda ns: _collect_holes(db, ns, usual), extra=(usual,), memo=False)
        return {k: _holes_merge(nights, k) for k in ("", *N.CONTEXTS)}
    return reuse(db, ("holes_merged", usual), run)[context if context in N.CONTEXTS else ""]


def _holes_merge(nights: dict[str, Any], key: str) -> dict[str, Any]:
    sg: dict[str, dict[str, Any]] = {}
    rec: dict[tuple, list] = {}
    trend: dict[str, list[dict]] = {}
    rounds_: dict[tuple, dict] = {}
    hl: dict[str, Any] = {"moments": [], "streaks": [], "bounce": [], "blowups": [], "tops": {n: [] for n in A.TOPS}}
    for night in nights.values():
        part = night[key]
        A.add_sg(sg, part["sg"])
        A.add_recovery(rec, part["rec"])
        for name, pt in part["trend"].items():
            trend.setdefault(name, []).append(pt)
        add_round_sg(rounds_, part["rounds"])
        for k in ("moments", "streaks", "bounce", "blowups"):
            hl[k] += part["hl"][k]
        for n in A.TOPS:
            hl["tops"][n] += part["hl"]["tops"][n]
    return {"sg": A.sg_from(sg), "recovery": A.recovery_from(rec), "trend": trend, "rounds": rounds_, "hl": hl}


def highlights_by_period(db: sqlite3.Connection, context: str | None = None,
                         player: str | None = None, today: date | None = None,
                         season: str | None = None) -> dict[str, Any]:
    """Records and highlights for each of the tab's date ranges: a top five can't be cut down to a
    shorter range on the page, so each range is worked out on its own. `season`: publish.season_start."""
    starts = period_starts(today, season)
    key = tuple(starts.items())
    def tables() -> dict[str | None, Callable[[str | None, str], dict[str, Any]]]:
        # every context's from one read of the nights' partials, which aren't kept after
        nights = N.per_night(db, "records", lambda ns: _collect_records(db, ns), memo=False)
        return {c: records_table(db, c, starts, nights) for c in (None, *N.CONTEXTS)}
    recs = reuse(db, ("records_table", key), tables)[context if context in N.CONTEXTS else None]
    hl = _holes_merged(db, context)["hl"]
    highs = reuse(db, ("highlights_table", context, key),
                  lambda: A.highlights_pick(hl["moments"], hl["streaks"], hl["bounce"], hl["blowups"], hl["tops"], starts))
    return {"records": {k: recs(player, k) for k in starts},
            "highlights": {k: highs(player, k) for k in starts}}


def summary(db: sqlite3.Connection, context: str | None = None,
            names: dict[str, str] | None = None, local: bool = False, season: str | None = None) -> dict[str, Any]:
    """`local`: for the page served on the PC, which also lists the rounds excluded from the stats (a
    published site leaves them out altogether). `season`: publish.season_start (MM-DD), for the
    records' This season range."""
    last = db.execute("SELECT MAX(date) FROM sessions").fetchone()[0]
    gimmes = reuse(db, ("round_gimmes",), lambda: round_gimmes(db))
    usual = usual_gimme(db, gimmes)
    fam = _holes_merged(db, context)
    sg, rec, trend = fam["sg"], fam["recovery"], fam["trend"]
    people = players(db, context, usual)
    # Our own handicap index (handicap_index) goes by every counting round, whatever the context:
    # every such round, and without the rounds with a mulligan.
    own = reuse(db, ("indexes",), lambda: handicap_index.indexes(db))
    year = period_starts()["1y"]   # gapping: the bag as it's been this last year (the 1y range's nights)
    from .hole_replay import finish_laterals   # hole_replay imports this module
    finish = finish_laterals(db, year)
    for p in people:
        mine = own.get(p["name"]) or {}
        clean = mine.get("without_mulligans") or {}
        p["index"], p["index_history"] = mine.get("index"), mine.get("history", [])
        # without the rounds a mulligan was taken in, for the page's toggle
        p["index_clean"], p["index_history_clean"] = clean.get("index"), clean.get("history", [])
        p["sg"] = sg.get(p["name"])
        p["gapping"] = A.gapping(db, p["name"], context, finish, year)
        p["recovery"] = rec.get(p["name"], [])
        p["trend"] = trend.get(p["name"], [])
    filters, has_league, has_tournament = _filters(db)
    rounds_ = rounds(db, context, gimmes)
    add_round_summaries(db, rounds_, fam["rounds"], people)
    return {"context": context or "all", "last_session": last, "has_league": has_league,
            "has_tournament": has_tournament, "gimme_ft": usual,
            "players": people,
            "rounds": rounds_, **(_editing(db) if local else {}), **highlights_by_period(db, context, season=season),
            "season_from": season_start(season), "predictions": predictions(db, names),
            "courses": A.courses(db, context),
            "booth": _booth(db, names), "broadcasts": A.broadcasts(db), "filters": filters,
            "vod_rounds": [[s, e, int(st > 0)] for s, e, st in round_spans(db)],
            "news": changelog.site()}


def make_server(db_path: str | Path, port: int = 8765, names: dict[str, str] | None = None,
                site: str | Path | None = None,
                brand: dict[str, Any] | Callable[[], dict[str, Any]] | None = None,
                videos: Callable[[], tuple[Path | None, int]] | None = None,
                on_change: Callable[[], None] | None = None,
                publish_status: Callable[[], dict[str, Any]] | None = None,
                video_deleted: Callable[[dict], None] | None = None,
                bucket_videos: Callable[[], list[dict]] | None = None,
                build_in_background: bool = False) -> ThreadingHTTPServer:
    """Serve the stats page on localhost only (port 0 picks a free one), the same static site that
    `publish` uploads (stats_site.py), built into `site` (a temp folder by default) and rebuilt when
    the database changes. `names` maps persona keys to on-air names; `brand` is build()'s site_name,
    more_tab and season_start, or a function returning them, asked on each request so a change in
    Settings rebuilds the page. `videos()`: where the round videos kept on the PC are (local_video.py; None if they
    aren't) and for how many days, asked on each request so a change in Settings counts at once. They're
    served as the bucket's live/ (recordings.json, s/<session>/full.m3u8), so the Watch buttons play them.
    `on_change()` is called after an edit from the page is saved (the tray publishes the site a while
    later); `publish_status()` is what GET api/publish-status answers, so the page can say when it's up.
    POST api/recordings/<session>/delete deletes a round video kept on the PC, then calls
    `video_deleted(record)` with its sidecar's record (the tray asks the booth to delete the bucket's copy too).
    Deleting a round deletes the videos it was the last round kept on: the PC's, and (`bucket_videos()`,
    the bucket's kept streams, each passed to `video_deleted`) the bucket's. `build_in_background`: the
    first build runs on its own thread (after an update it works every round out again, minutes on
    years of rounds), and the page waits for it."""
    from . import local_video, stats_site

    db_path = Path(db_path)
    site = Path(site) if site else Path(tempfile.mkdtemp(prefix="shotcast-stats-"))
    lock = threading.Lock()
    built: dict[str, Any] = {"key": None}

    def refresh() -> None:
        with lock:
            options = (brand() if callable(brand) else brand) or {}
            key = (file_stamp(db_path), sorted(options.items()))
            if built["key"] != key:
                stats_site.build(db_path, site, names, local=True, **options)
                built["key"] = key

    ready = threading.Event()

    def first_build() -> None:
        try:
            refresh()
        except Exception:   # (latest.json tries again, and says why)
            log.exception("Building the stats page failed")
        finally:
            ready.set()

    if not build_in_background:
        refresh()
        ready.set()

    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):  # noqa: N802
            self._file(head=True)

        def do_GET(self):  # noqa: N802
            if urlparse(self.path).path == "/api/publish-status":
                status = publish_status() if publish_status else {"enabled": False}
                return self._send(200, json.dumps(status).encode(), "application/json")
            self._file(head=False)

        def do_POST(self):  # noqa: N802
            # Only this page may write: same host, a JSON body (another site's form can't send one
            # without a CORS preflight, which this server never answers).
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            if host.split(":")[0] not in ("127.0.0.1", "localhost") or (origin and urlparse(origin).netloc != host) \
                    or not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._send(403, b"forbidden", "text/plain")
            path = urlparse(self.path).path
            if m := re.fullmatch(r"/api/recordings/([^/]+)/delete", path):
                return self._delete_video(m.group(1))
            routes = ((r"/api/shots/(\d+)/left-out", "left_out"), (r"/api/rounds/(\d+)/excluded", "excluded"),
                      (r"/api/rounds/(\d+)/delete", "delete"), (r"/api/rounds/(\d+)/league", "league"))
            m, key = next(((m, key) for pat, key in routes if (m := re.fullmatch(pat, path))), (None, None))
            if not m:
                return self._send(404, b"not found", "text/plain")
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 1024)) or b"{}")
                ident = int(m.group(1))
                if key == "left_out":
                    value = bool(body["left_out"])
                    found = set_left_out(db_path, ident, value, float(body["ts"]))
                elif key == "league":
                    value = bool(body["league"])
                    found = set_round_league(db_path, ident, float(body["ended"]), value,
                                             bool(body.get("tournament")))
                elif key == "excluded":
                    value = bool(body["excluded"])
                    found = set_round_excluded(db_path, ident, float(body["ended"]), value)
                else:
                    if body.get("confirm") is not True:   # a delete can't be undone: the page asks first
                        raise ValueError("delete needs confirm: true")
                    value = True
                    found = delete_round(db_path, ident, float(body["ended"]))
                    if found:
                        self._drop_videos()
                if not found:
                    return self._send(404, b"no such shot" if key == "left_out" else b"no such round", "text/plain")
                refresh()
            except RoundInPlay:
                return self._send(409, json.dumps({"error": "the round just ended: try again in a few minutes"}).encode(),
                                  "application/json")
            except (ValueError, KeyError, TypeError) as e:
                return self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
            except (OSError, sqlite3.Error) as e:
                return self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
            if on_change is not None:
                try:
                    on_change()
                except Exception:   # the edit is saved either way
                    log.exception("After a stats page edit")
            self._send(200, json.dumps({"id": ident, {"delete": "deleted"}.get(key, key): value}).encode(),
                       "application/json")

        def _drop_videos(self) -> None:
            """After a round is deleted: the videos no round kept is on any more. The round is gone
            either way; a video that can't go yet (open in a player) stays, hidden on the page."""
            from contextlib import closing

            try:
                with closing(connect(db_path)) as db:
                    spans = round_spans(db)
                folder = videos()[0] if videos is not None else None
                for rec in videos_of_deleted(spans, local_video.recordings(folder) if folder else []):
                    try:
                        local_video.delete(folder, rec["session"])
                    except (OSError, ValueError) as e:
                        log.info("Kept the round video %s for now: %s", rec["session"], e)
                        continue
                    if video_deleted is not None and bucket_videos is None:
                        video_deleted(rec)
                if video_deleted is not None and bucket_videos is not None:
                    for rec in videos_of_deleted(spans, bucket_videos()):
                        video_deleted(rec)
            except Exception:
                log.exception("Deleting the videos of a deleted round")

        def _delete_video(self, session: str) -> None:
            folder = videos()[0] if videos is not None else None
            if folder is None:
                return self._send(404, b"round videos aren't kept on this PC", "text/plain")
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 1024)) or b"{}")
                if body.get("confirm") is not True:   # it can't be undone: the page asks first
                    raise ValueError("delete needs confirm: true")
                rec = local_video.delete(folder, session)
            except (ValueError, TypeError, AttributeError) as e:
                return self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
            except OSError as e:
                return self._send(409, json.dumps({"error": str(e)}).encode(), "application/json")
            if rec is None:
                return self._send(404, b"no such recording", "text/plain")
            if video_deleted is not None:
                try:
                    video_deleted(rec)
                except Exception:   # the PC's copy is gone either way
                    log.exception("Asking for the bucket's copy of %s to go", session)
            self._send(200, json.dumps({"session": session, "deleted": True}).encode(), "application/json")

        def _video(self, rel: str) -> tuple[bytes, str] | None:
            folder, keep_days = videos()
            if folder is None:
                return None
            if rel == "live/recordings.json":
                recs = [{k: v for k, v in r.items() if k != "segments"} for r in local_video.recordings(folder)]
                return json.dumps({"recordings": recs, "keep_days": keep_days}).encode(), "application/json"
            m = re.fullmatch(r"live/s/([^/]+)/(full\.m3u8|seg(\d+)\.ts)", rel)
            rec = m and next((r for r in local_video.recordings(folder) if r["session"] == m.group(1)), None)
            if not rec:
                return None
            if m.group(3) is None:
                return local_video.playlist(rec), "application/vnd.apple.mpegurl"
            data = local_video.segment(folder, rec, int(m.group(3)))
            return None if data is None else (data, "video/mp2t")

        def _file(self, head: bool) -> None:
            rel = unquote(urlparse(self.path).path).lstrip("/") or "index.html"
            if videos is not None and rel.startswith("live/"):
                try:
                    got = self._video(rel)
                except OSError:
                    got = None
                if got is None:
                    return self._send(404, b"not found", "text/plain", head)
                return self._range(*got, head)
            ready.wait()   # (not a half-built page, nor the last version's)
            if rel == "latest.json" or built["key"] is None:   # (a first build that failed: again)
                try:
                    refresh()
                except (OSError, sqlite3.Error) as e:
                    return self._send(500, json.dumps({"error": f"{db_path}: {e}"}).encode(), "application/json", head)
            path = (site / rel).resolve()
            if not path.is_file() or site.resolve() not in path.parents:
                return self._send(404, b"not found", "text/plain", head)
            data = path.read_bytes()
            if rel == "latest.json":   # served from here, the page may leave shots out of the stats
                data = json.dumps({**json.loads(data), "local": True}).encode()
            self._range(data, stats_site.CONTENT_TYPES.get(path.suffix, "application/octet-stream"), head)

        def _range(self, data: bytes, kind: str, head: bool) -> None:
            m = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
            if m and int(m.group(1)) < len(data):
                start = int(m.group(1))
                end = min(int(m.group(2)) if m.group(2) else len(data) - 1, len(data) - 1)
                return self._send(206, data[start:end + 1], kind, head,
                                  {"Content-Range": f"bytes {start}-{end}/{len(data)}"})
            self._send(200, data, kind, head)

        def _send(self, code: int, body: bytes, kind: str, head: bool = False,
                  extra: dict[str, str] | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def log_message(self, *args):  # quiet: the worker makes many small range requests
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    if build_in_background:   # (once the port's taken: a server that couldn't be made builds nothing)
        threading.Thread(target=first_build, name="stats-build", daemon=True).start()
    return server


def serve(db_path: str | Path, port: int = 8765, open_browser: bool = True,
          names: dict[str, str] | None = None, site: str | Path | None = None,
          brand: dict[str, Any] | None = None, videos: Callable[[], tuple[Path | None, int]] | None = None) -> None:
    server = make_server(db_path, port, names, site, brand, videos)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Season stats from {db_path} at {url} (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
