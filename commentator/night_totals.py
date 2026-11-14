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
    """Each night's signature: its sessions, and its records' change counters. A record with no session
    (or one no longer on file) can't be placed, so it's in every night's."""
    def run() -> dict[str, str]:
        date_of = {sid: d for d, sids in nights(db).items() for sid in sids}
        per: dict[str, list[tuple[str, int]]] = {d: [] for d in nights(db)}
        loose = []
        for uuid, sess, gen in db.execute("SELECT uuid, session_id, gen FROM main.round_records ORDER BY uuid"):
            if (d := date_of.get(sess)) is None:
                loose.append((uuid, gen))
            else:
                per[d].append((uuid, gen))
        return {d: stats_cache.sig(sids, per[d], loose) for d, sids in nights(db).items()}
    return reuse(db, ("night_sigs",), run)


class Nights:
    """The nights a family's `collect` is asked for: `where` limits a query on a table with a
    session_id column to them ("" when it's every night), `date` maps a session to its night."""

    def __init__(self, db: sqlite3.Connection, dates: list[str]) -> None:
        every = nights(db)
        self.dates = dates
        self.date = {sid: d for d in dates for sid in every[d]}
        sids = sorted(self.date)
        self.where = "" if len(dates) == len(every) else f" AND session_id IN ({','.join(map(str, sids)) or 'NULL'})"


def per_night(db: sqlite3.Connection, kind: str, collect: Callable[[Nights], dict[str, Any]],
              extra: Any = ()) -> dict[str, Any]:
    """Each night's partial for a family: kept ones while the night's signature (and `extra`, anything
    else the partial reads) holds, `collect(nights)` for the rest, which returns one for each of its
    dates. Oldest night first."""
    def run() -> dict[str, Any]:
        if stats_cache.of(db) is None:   # (no cache: nothing kept to check, and maybe no records to sign by)
            got = collect(Nights(db, list(nights(db))))
            return {d: got[d] for d in nights(db)}
        sigs = {d: stats_cache.sig(s, extra) for d, s in night_sigs(db).items()}
        return stats_cache.per_key(db, kind, sigs, lambda dates: collect(Nights(db, dates)))
    return reuse(db, ("per_night", kind, extra), run)


def since(partials: dict[str, Any], start: str | None) -> Iterable[Any]:
    """The partials of the nights from `start` (a date; None: all of them), oldest first."""
    return (p for d, p in partials.items() if start is None or d >= start)


def add(into: list, part: Iterable) -> None:
    """Add a list of counts into another, element by element."""
    for i, v in enumerate(part):
        into[i] += v
