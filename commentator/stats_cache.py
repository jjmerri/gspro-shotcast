"""Stats worked out per round, kept between site builds (docs/dev/stats-longevity.md, steps 4 and 5).

A build works most of its stats out round by round. A round's result only changes when the round
does, which its record's change counter tells (round_record: `gen`, moved by triggers on every row of
the round), or when something outside it that the result reads does (another round, a hole's layout,
the usual gimme); each kind of result names those in its signature. So a build reuses the results whose
signature is unchanged and works out the rest.

It's only a cache: a file beside the database (`<name>.stats-cache`), never the database itself, and
never backed up. Deleting it (stats_web's Rebuild stats) costs one slower build, and one it can't read
or write is worked around (everything worked out as before). It's thrown away whole when the app's
code, the database's schema or the database itself changes: another file (its dataset_id), or an
older copy put back (backup.restore deletes it. A file copied back by hand is put right by its next
build, which keeps each changed round's result under the counter it's gone back to; changed before
then, the counters can reach the counts kept with other rows, and Rebuild stats is the way back). It's used only on a file whose every row has its
record (round_record.recorded).
"""
from __future__ import annotations

import hashlib
import logging
import marshal
import sqlite3
import time
import zlib
from collections.abc import Callable, Hashable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SUFFIX = ".stats-cache"
_open: dict[int, "Cache"] = {}   # id(connection) -> the cache a build is using
_code: str | None = None


def path_for(db_path: str | Path) -> Path:
    db_path = Path(db_path)
    return db_path.with_name(db_path.name + SUFFIX)


def code_stamp() -> str:
    """A hash of the app's code: results worked out by other code aren't reused."""
    global _code
    if _code is None:
        h = hashlib.sha256()
        package = Path(__file__).parent
        for p in sorted(package.rglob("*.py")):
            h.update(p.relative_to(package).as_posix().encode())
            h.update(p.read_bytes())
        _code = h.hexdigest()
    return _code


def _stamp(db: sqlite3.Connection) -> str:
    version = db.execute("PRAGMA main.user_version").fetchone()[0]
    dataset = db.execute("SELECT dataset_id FROM main.meta").fetchone()[0]
    return f"{code_stamp()}:{version}:{dataset}"


def _key(key: Hashable) -> str:
    return repr(key)


class Cache:
    """One build's view of the cache file."""

    def __init__(self, path: Path, stamp: str) -> None:
        self.db = sqlite3.connect(path, timeout=30)
        try:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS stamp (id INTEGER PRIMARY KEY CHECK (id = 1), value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS entries (kind TEXT, key TEXT, sig TEXT NOT NULL, value BLOB NOT NULL,
                                                    PRIMARY KEY (kind, key));
            """)
            with self.db:
                row = self.db.execute("SELECT value FROM stamp").fetchone()
                if row is None or row[0] != stamp:
                    self.db.execute("DELETE FROM entries")
                    self.db.execute("INSERT OR REPLACE INTO stamp VALUES (1, ?)", (stamp,))
        except BaseException:
            self.db.close()
            raise
        self.hits = self.misses = 0

    def close(self) -> None:
        self.db.close()

    def get(self, kind: str, sigs: dict[Hashable, str]) -> dict[Hashable, Any]:
        """The stored results for these keys whose signature matches."""
        want = {_key(k): (k, s) for k, s in sigs.items()}
        out = {}
        try:
            rows = self.db.execute("SELECT key, sig, value FROM entries WHERE kind = ?", (kind,)).fetchall()
        except sqlite3.Error as e:
            log.warning("stats cache not read: %s", e)
            return out
        for key, sig, value in rows:
            if (w := want.get(key)) is not None and w[1] == sig:
                try:
                    out[w[0]] = marshal.loads(zlib.decompress(value))
                except (zlib.error, ValueError, EOFError, TypeError):   # (a damaged entry: worked out again)
                    pass
        return out

    def put(self, kind: str, items: dict[Hashable, tuple[str, Any]], keep: set[Hashable]) -> None:
        """Store these (signature, result)s, and drop the kind's results for keys no longer asked for."""
        keep_keys = {_key(k) for k in keep}
        rows = [(kind, _key(k), sig, zlib.compress(marshal.dumps(v), 1)) for k, (sig, v) in items.items()]
        try:
            with self.db:
                self.db.executemany("INSERT OR REPLACE INTO entries VALUES (?, ?, ?, ?)", rows)
                gone = [(kind, k) for (k,) in self.db.execute("SELECT key FROM entries WHERE kind = ?", (kind,))
                        if k not in keep_keys]
                self.db.executemany("DELETE FROM entries WHERE kind = ? AND key = ?", gone)
        except sqlite3.Error as e:   # (worked out again next time)
            log.warning("stats cache not written: %s", e)


@contextmanager
def opened(db: sqlite3.Connection, db_path: str | Path) -> Iterator[Cache | None]:
    """Use the cache beside `db_path` for `db` (a stats_web.connect snapshot of it) while inside. Not on a
    file with rows yet to get their record, and not if the cache file can't be used (it's then
    worked out as before)."""
    from .round_record import recorded

    cache = None
    if id(db) not in _open and recorded(db):
        try:
            cache = Cache(path_for(db_path), _stamp(db))
        except sqlite3.Error as e:
            log.warning("stats cache not used: %s", e)
    if cache is None:
        yield None
        return
    _open[id(db)] = cache
    started = time.perf_counter()
    try:
        yield cache
    finally:
        _open.pop(id(db), None)
        cache.close()
        log.info("stats cache: %d reused, %d worked out (%.1fs)", cache.hits, cache.misses,
                 time.perf_counter() - started)


def of(db: sqlite3.Connection) -> Cache | None:
    return _open.get(id(db))


def forget(db_path: str | Path) -> None:
    """Delete the cache file (backup.restore, once an older copy is put back)."""
    path = path_for(db_path)
    for p in (path, path.with_name(path.name + "-journal")):
        try:
            p.unlink(missing_ok=True)
        except OSError as e:
            log.warning("stats cache not deleted: %s", e)


def clear(db_path: str | Path) -> None:
    """Forget every kept result (stats_web's Rebuild stats): the next build works them all out."""
    path = path_for(db_path)
    if path.exists():
        with closing(sqlite3.connect(path, timeout=30)) as c, c:
            c.execute("DELETE FROM entries")


def sig(*parts: Any) -> str:
    return hashlib.sha256(repr(parts).encode()).hexdigest()[:32]


def per_key(db: sqlite3.Connection, kind: str, sigs: dict[Hashable, str],
            compute: Callable[[list[Hashable]], dict[Hashable, Any]], tally: bool = True) -> dict[Hashable, Any]:
    """Each key's result: the kept one while its signature holds, else `compute(keys)` for the rest
    (which may leave a key out: it has no result, and that's kept too). Without a cache open on `db`,
    `compute` gets every key. The result is in `sigs`' order. `tally`: count the hits and misses."""
    cache = of(db)
    if cache is None:
        got = compute(list(sigs))
        return {k: got[k] for k in sigs if k in got}
    kept = cache.get(kind, sigs)
    missing = [k for k in sigs if k not in kept]
    new = compute(missing) if missing else {}
    if tally:
        cache.hits += len(kept)
        cache.misses += len(missing)
    cache.put(kind, {k: (sigs[k], (k in new, new.get(k))) for k in missing}, set(sigs))
    out = {}
    for k in sigs:
        if k in new:
            out[k] = new[k]
        elif k in kept and kept[k][0]:
            out[k] = kept[k][1]
    return out


def record_sigs(db: sqlite3.Connection) -> dict[str, int]:
    """Each record's change counter."""
    return dict(db.execute("SELECT uuid, gen FROM main.round_records"))


def row_records(db: sqlite3.Connection) -> dict[int, str]:
    """Each rounds row's record (a round, as round_groups numbers it by its first row, is in its
    first row's record)."""
    return dict(db.execute("SELECT id, round_uuid FROM main.rounds"))
