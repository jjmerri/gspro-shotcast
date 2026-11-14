"""Time the stats build, the local stats page, the booth's history lookups and a backup on years of
history, against the targets in docs/dev/stats-longevity.md. Run by hand before a release that touches
stats; too slow for CI. Never touches the database it's given: it works on a copy.

    python tools/scale_bench.py scale logs/season.db /tmp/bench/3y.db --copies 50
    python tools/scale_bench.py run /tmp/bench/3y.db

`scale` makes a bigger file from a real one: the original's sessions, shots, lines, rounds and the rest,
copied again `copies - 1` times, each copy moved back in time by the original's span (ids, sessions
and dates shifted to match). The copies' rows get their records the way an older version's rows do,
when the file is next opened (round_record.migrate). `run` times, on a copy of the file:
a cold build (no stats cache), the same build again (nothing changed), a build after one round is
edited, the local page's first load after that edit, the booth's lookups for the busiest player, and
a backup.
"""

import argparse
import contextlib
import hashlib
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from commentator import backup, stats_cache, stats_site, stats_web  # noqa: E402
from commentator.store import Store  # noqa: E402

# The tables copied, and their columns that hold times (shifted with the copy).
TABLES = {"sessions": ("started_at",), "shots": ("ts", "rest_ts"), "lines": ("ts", "aired_at"), "rounds": ("ended_at",),
          "player_rounds": ("ended_at",), "predictions": ("ts",), "form_mentions": ("ts",), "asides": ("ts",)}
# What ties a row to its record: cleared on a copy, so the copy gets records of its own.
FRESH = ("record_uuid", "round_uuid", "slot_id")
# A shot's or line's uid, and a shot's reference to another shot's uid: a copy gets the same new one.
UIDS = ("uid", "derived_from")
TARGETS = {"build after a round": 30.0, "page first load after a round": 2.0}


def _snapshot(src: str | Path, out: str | Path) -> None:
    """Copy a season file (with a WAL beside it, if any) without opening it: SQLite opening even a
    read-only file can write beside it. Copy it with Shotcast closed, or the copy may be torn."""
    src = Path(src)
    with tempfile.TemporaryDirectory(prefix="scale-bench-") as tmp:
        staged = Path(tmp) / src.name
        shutil.copyfile(src, staged)
        if Path(f"{src}-wal").exists():
            shutil.copyfile(f"{src}-wal", f"{staged}-wal")
        with contextlib.closing(sqlite3.connect(staged)) as s, contextlib.closing(sqlite3.connect(out)) as d:
            s.backup(d)
            d.execute("PRAGMA journal_mode = DELETE")   # an edit changes the file's time (the page's cue)


def _copy_uid(uid: str | None, i: int) -> str | None:
    return None if uid is None else hashlib.sha1(f"{uid}:{i}".encode()).hexdigest()[:32]


def scale(src: str, out: str, copies: int) -> None:
    if any(Path(f"{out}{s}").exists() or Path(f"{out}{s}").is_symlink() for s in ("", "-wal", "-shm", "-journal")):
        raise SystemExit(f"{out} exists: give a new file (this never writes over one, so never over the season)")
    _snapshot(src, out)
    db = sqlite3.connect(out)
    db.create_function("copy_uid", 2, _copy_uid, deterministic=True)
    span = db.execute("SELECT MAX(ts) - MIN(ts) FROM shots").fetchone()[0] + 86400
    top = {t: db.execute(f"SELECT COALESCE(MAX(id), 0) FROM {t}").fetchone()[0] for t in TABLES}
    for i in range(1, copies):
        off = span * i
        for table, times in TABLES.items():
            cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
            sel = []
            for c in cols:
                if c == "id":
                    sel.append(f"id + {top[table] * i}")
                elif c == "session_id":
                    sel.append(f"session_id + {top['sessions'] * i}")
                elif c == "shot_id":
                    sel.append(f"shot_id + {top['shots'] * i}")
                elif c in times:
                    sel.append(f"{c} - {off}")
                elif c in FRESH:
                    sel.append("NULL")
                elif c in UIDS:
                    sel.append(f"copy_uid({c}, {i})")
                else:
                    sel.append(c)
            db.execute(f"INSERT INTO {table} ({', '.join(cols)}) SELECT {', '.join(sel)} FROM {table}"
                       f" WHERE id <= {top[table]}")
            if "date" in cols:
                days = timedelta(seconds=off)
                db.executemany(f"UPDATE {table} SET date = ? WHERE id = ?", [
                    ((date.fromisoformat(d[:10]) - days).isoformat() + d[10:], rid) for rid, d in db.execute(
                        f"SELECT id, date FROM {table} WHERE id > ? AND id <= ? AND date IS NOT NULL",
                        (top[table] * i, top[table] * (i + 1))).fetchall()])
    db.commit()
    db.close()
    t = time.perf_counter()
    Store(out, new_session=False).close()   # the copies' records
    with contextlib.closing(sqlite3.connect(out)) as db:
        n = db.execute("SELECT COUNT(*) FROM shots").fetchone()[0]
    print(f"{out}: {n} shots ({copies} copies); records assigned in {time.perf_counter() - t:.0f} s")


def _timed(label: str, fn) -> float:
    t = time.perf_counter()
    fn()
    dt = time.perf_counter() - t
    target = TARGETS.get(label)
    print(f"  {label:34s} {dt:8.2f} s" + (f"   (target {target:g} s{', MISSED' if dt > target else ''})" if target else ""))
    return dt


def _edit_a_round(db_path: Path) -> None:
    """Leave a shot of the latest finished round out of the stats, as an edit from the page does."""
    with contextlib.closing(sqlite3.connect(db_path)) as db:
        ids = [r[0] for r in db.execute(
            "SELECT s.id FROM shots s JOIN round_records rr ON rr.uuid = s.record_uuid"
            " WHERE rr.kind = 'round' AND rr.state = 'finished' AND s.excluded = 0 AND s.attributed = 1"
            " AND s.mulligan = 0 AND s.left_out = 0"
            " AND EXISTS (SELECT 1 FROM rounds r WHERE r.round_uuid = rr.uuid AND r.excluded = 0)"
            " ORDER BY s.ts DESC LIMIT 2000")]
    for sid in ids:
        try:
            if stats_web.set_left_out(db_path, sid, True):
                return
        except stats_web.RoundInPlay:
            continue
    raise SystemExit("no shot could be left out")


def run(db_in: str) -> None:
    work = Path(tempfile.mkdtemp(prefix="scale-bench-"))
    try:
        db_path = work / Path(db_in).name
        _snapshot(db_in, db_path)
        with contextlib.closing(sqlite3.connect(db_path)) as db:
            n = db.execute("SELECT COUNT(*) FROM shots").fetchone()[0]
        print(f"{db_in}: {n} shots")
        site = work / "site"
        stats_cache.forget(db_path)
        _timed("cold build (no stats cache)", lambda: stats_site.build(db_path, site, local=True))
        _timed("build, nothing changed", lambda: stats_site.build(db_path, site, local=True))
        _edit_a_round(db_path)
        _timed("build after a round", lambda: stats_site.build(db_path, site, local=True))

        server = stats_web.make_server(db_path, port=0, site=work / "page")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            urllib.request.urlopen(base + "/latest.json").read()   # built for the file as it is
            before = db_path.stat().st_mtime
            time.sleep(0.01)
            _edit_a_round(db_path)
            if db_path.stat().st_mtime == before:
                raise SystemExit("the edit didn't change the file's time: the page wouldn't rebuild")
            _timed("page first load after a round", lambda: urllib.request.urlopen(base + "/latest.json").read())
        finally:
            server.shutdown()

        st = Store(str(db_path), new_session=False)
        try:
            player = st.db.execute("SELECT player FROM shots WHERE player IS NOT NULL AND player != 'unknown'"
                                   " GROUP BY 1 ORDER BY COUNT(*) DESC").fetchone()[0]
            lookups = {"season_stats": lambda: st.season_stats(player, None),
                       "player_profile": lambda: st.player_profile(player, None),
                       "fairways_hit": lambda: st.fairways_hit(player, None),
                       "approach_proximity": lambda: st.approach_proximity(player, 150, context=None),
                       "putting_make_rate": lambda: st.putting_make_rate(player, 8, context=None),
                       "season_longest_drive": lambda: st.season_longest_drive(None)}
            for name, fn in lookups.items():
                fn()
                _timed(f"booth: {name}", fn)
        finally:
            st.close()
        _timed("backup", lambda: backup.back_up(db_path, work / "backups", force=True))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scale", help="make a bigger copy of a season file")
    s.add_argument("src")
    s.add_argument("out")
    s.add_argument("--copies", type=int, default=50)
    r = sub.add_parser("run", help="time the build, the page, the booth's lookups and a backup")
    r.add_argument("db")
    a = ap.parse_args()
    if a.cmd == "scale":
        scale(a.src, a.out, a.copies)
    else:
        run(a.db)


if __name__ == "__main__":
    main()
