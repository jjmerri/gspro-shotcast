"""Stats frozen per night, merged for any range (docs/dev/stats-longevity.md, step 3).

A night is a session date. Each stat family keeps, per night, the counts behind its figures (a sum and
its count for an average, the top rows with their tie keys for a best), worked out once and kept in
the stats cache (stats_cache.per_key) under the night's signature: its sessions and the change
counters of the records played in them (no record spans two dates). An edit to an old round works
out that one night again; lifetime, a season and the rolling ranges merge the nights in range.

Partials are kept per round context (league / tournament / practice, as _ctx tells them apart); all
contexts is their merge.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from typing import Any

from . import stats_cache
from .round_record import reuse
from shotcast_scoring.analytics import exact_add, exact_merge  # noqa: F401 (the shared copy)

CONTEXTS = ("league", "tournament", "practice")


def context_of(is_league: Any, is_tournament: Any) -> str:
    """A row's round context, as season_analytics._ctx filters them ("" for none of them: such a row
    only counts with all contexts)."""
    if is_league == 1:
        return "league"
    if is_league == 0:
        return "tournament" if is_tournament == 1 else "practice"
    return ""


def wanted(context: str | None) -> Callable[[str], bool]:
    """Whether a partial's context counts for `context` (None: all of them)."""
    if context in CONTEXTS:
        return lambda c: c == context
    return lambda c: True


def nights(db: sqlite3.Connection) -> dict[str, list[int]]:
    """Each session date's sessions, oldest date first."""
    def run() -> dict[str, list[int]]:
        out: dict[str, list[int]] = {}
        for sid, d in db.execute("SELECT id, date FROM main.sessions ORDER BY date, id"):
            out.setdefault(d, []).append(sid)
        return out
    return reuse(db, ("nights",), run)


def night_sigs(db: sqlite3.Connection) -> dict[str, str]:
    """Each night's signature: its sessions, and the change counters of the records with rows in them
    or starting in them (a round resumed after midnight is on two nights). A record that can't be
    placed (no session, or none on file) is in every night's."""
    def run() -> dict[str, str]:
        date_of = {sid: d for d, sids in nights(db).items() for sid in sids}
        per: dict[str, list[tuple[str, int]]] = {d: [] for d in nights(db)}
        loose = []
        rows_in = record_sessions(db)
        for uuid, sess, gen in db.execute("SELECT uuid, session_id, gen FROM main.round_records ORDER BY uuid"):
            dates = {date_of[s] for s in (sess, *rows_in.get(uuid, ())) if s in date_of}
            if not dates:
                loose.append((uuid, gen))
            for d in dates:
                per[d].append((uuid, gen))
        return {d: stats_cache.sig(sids, per[d], loose) for d, sids in nights(db).items()}
    return reuse(db, ("night_sigs",), run)


_RECORD_ROWS = (("shots", "record_uuid"), ("lines", "record_uuid"), ("rounds", "round_uuid"),
                ("player_rounds", "round_uuid"))


def record_sessions(db: sqlite3.Connection) -> dict[str, list[int]]:
    """Each record's sessions: those its rows are in. Kept with the record's change counter, so only a
    changed record is looked up again."""
    def compute(keys: list) -> dict[str, list[int]]:
        out: dict[str, set[int]] = {k: set() for k in keys}
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for table, col in _RECORD_ROWS:
                for uuid, sid in db.execute(
                        f"SELECT DISTINCT {col}, session_id FROM main.{table} WHERE {col} IN ({marks})", chunk):
                    out[uuid].add(sid)
        return {k: sorted(v) for k, v in out.items()}

    def run() -> dict[str, list[int]]:
        gens = {uuid: str(gen) for uuid, gen in db.execute("SELECT uuid, gen FROM main.round_records")}
        return stats_cache.per_key(db, "record_sessions", gens, compute, tally=False)
    return reuse(db, ("record_sessions",), run)


class Nights:
    """The nights a family's `collect` is asked for: `where` limits a query on a table with a
    session_id column to them ("" when it's every night), `date` maps a session to its night."""

    def __init__(self, db: sqlite3.Connection, dates: list[str]) -> None:
        every = nights(db)
        self.dates = dates
        self.date = {sid: d for d in dates for sid in every[d]}
        sids = sorted(self.date)
        self.where = "" if len(dates) == len(every) else f" AND session_id IN ({','.join(map(str, sids)) or 'NULL'})"


BATCH_NIGHTS = 64   # nights worked out per `collect` call, so a cold build holds a batch's rows, not all history's


def _batched(db: sqlite3.Connection, collect: Callable[[Nights], dict[str, Any]], dates: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for i in range(0, len(dates), BATCH_NIGHTS):
        out.update(collect(Nights(db, dates[i:i + BATCH_NIGHTS])))
    return out


def per_night(db: sqlite3.Connection, kind: str, collect: Callable[[Nights], dict[str, Any]],
              extra: Any = (), tally: bool = True, since: str | None = None, memo: bool = True) -> dict[str, Any]:
    """Each night's partial for a family: kept ones while the night's signature (and `extra`, anything
    else the partial reads) holds, `collect(nights)` for the rest, which returns one for each of its
    dates, `BATCH_NIGHTS` at a time. Oldest night first; with `since` (a date), only the nights from then
    on (the others' are dropped from the cache). `tally`: count them in the cache's hits and misses. `memo`: kept for the
    rest of the build (round_record.reuse); False where they're only read once, merged, so a build
    doesn't hold every night's partials to its end."""
    def run() -> dict[str, Any]:
        dates = [d for d in nights(db) if since is None or d >= since]
        if stats_cache.of(db) is None:   # (no cache: nothing kept to check, and maybe no records to sign by)
            got = _batched(db, collect, dates)
            return {d: got[d] for d in dates}
        every = night_sigs(db)
        sigs = {d: stats_cache.sig(every[d], extra) for d in dates}
        return stats_cache.per_key(db, kind, sigs, lambda ds: _batched(db, collect, ds), tally=tally)
    return reuse(db, ("per_night", kind, extra, since), run) if memo else run()


def since(partials: dict[str, Any], start: str | None) -> Iterable[Any]:
    """The partials of the nights from `start` (a date; None: all of them), oldest first."""
    return (p for d, p in partials.items() if start is None or d >= start)


def add(into: list, part: Iterable) -> None:
    """Add a list of counts into another, element by element."""
    for i, v in enumerate(part):
        into[i] += v
