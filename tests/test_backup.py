import gzip
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta

from commentator import backup
from commentator.config import Config
from commentator.engine import Engine
from commentator.league import League


def _db(path, value="a"):
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
        db.execute("DELETE FROM t")
        db.execute("INSERT INTO t VALUES (?)", (value,))
        db.commit()


def _value(path):
    with closing(sqlite3.connect(path)) as db:
        return db.execute("SELECT v FROM t").fetchone()[0]


def _name(when):
    return backup.PREFIX + when.strftime(backup.STAMP) + backup.SUFFIX


def test_keep_holds_the_newest_per_day_for_14_days_then_one_per_week_for_4():
    start = datetime(2026, 1, 1, 12)
    names = [_name(start + timedelta(days=d, hours=h)) for d in range(120) for h in (0, 3)]
    kept = sorted(backup.keep(names + ["notes.txt"]), reverse=True)
    newest = start + timedelta(days=119)
    assert kept[:14] == [_name(newest - timedelta(days=d) + timedelta(hours=3)) for d in range(14)]
    weekly = [backup._when(n) for n in kept[14:]]
    assert len(weekly) == 4
    weeks = [w.isocalendar()[:2] for w in weekly]
    assert len(set(weeks)) == 4 and backup._when(kept[13]).isocalendar()[:2] not in weeks
    assert all(w.hour == 15 and w.weekday() == 6 for w in weekly)   # each week's newest copy


def test_keep_counts_days_with_copies_not_calendar_days():
    names = [_name(datetime(2026, 1, 1) + timedelta(days=30 * i)) for i in range(10)]
    assert backup.keep(names) == set(names)


def test_back_up_skips_an_unchanged_database_and_prunes(tmp_path):
    db, dest = tmp_path / "season.db", tmp_path / "backups"
    _db(db)
    first = backup.back_up(db, dest, now=datetime(2026, 3, 1, 9))
    assert first.name == "season-20260301-090000.db.gz"
    assert backup.back_up(db, dest, now=datetime(2026, 3, 1, 10)) is None
    _db(db, "b")
    second = backup.back_up(db, dest, daily=1, weekly=0, now=datetime(2026, 3, 2, 9))
    assert sorted(p.name for p in dest.glob("season-*")) == [second.name]
    with gzip.open(second) as fh:
        (tmp_path / "out.db").write_bytes(fh.read())
    assert _value(tmp_path / "out.db") == "b"


def test_restore_puts_a_backup_back_and_saves_the_file_it_replaces(tmp_path):
    db, dest = tmp_path / "season.db", tmp_path / "backups"
    _db(db, "old")
    old = backup.back_up(db, dest, daily=1, weekly=0)
    _db(db, "new")
    saved = backup.restore(old, db, dest, daily=1, weekly=0)   # same day: the safety copy prunes `old`
    assert _value(db) == "old"
    with gzip.open(saved) as fh:
        (tmp_path / "out.db").write_bytes(fh.read())
    assert _value(tmp_path / "out.db") == "new"


class FakeS3:
    def __init__(self, keys):
        self.keys, self.deleted = set(keys), []

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.keys.add(key)

    def get_paginator(self, name):
        fake = self

        class P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for k in sorted(fake.keys) if k.startswith(Prefix)]}
        return P()

    def delete_objects(self, Bucket, Delete):
        for o in Delete["Objects"]:
            self.deleted.append(o["Key"])
            self.keys.discard(o["Key"])


def test_upload_prunes_the_bucket_the_same_way(tmp_path):
    days = [datetime(2026, 3, d, 9) for d in range(1, 4)]
    s3 = FakeS3(["backups/" + _name(d) for d in days[:2]] + ["site/index.html"])
    new = tmp_path / _name(days[2])
    new.write_bytes(b"x")
    backup.upload(new, "b", "backups/", daily=2, weekly=0, client=s3)
    assert s3.deleted == ["backups/" + _name(days[0])]
    assert "site/index.html" in s3.keys


def test_run_retries_a_failed_upload_on_the_next_backup(tmp_path, monkeypatch):
    cfg = Config(log_dir=str(tmp_path))
    cfg.backup.s3, cfg.publish.bucket = True, "b"
    _db(cfg.db_path)
    calls = []

    def flaky(path, *a, **k):
        calls.append(path.name)
        if len(calls) == 1:
            raise OSError("offline")

    monkeypatch.setattr(backup, "upload", flaky)
    import sys
    import types
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda *a, **k: None))
    try:
        backup.run(cfg)
    except OSError:
        pass
    assert backup.run(cfg) is None   # unchanged, but the upload is still owed
    assert len(calls) == 2 and calls[0] == calls[1]
    assert json.loads((backup.folder(cfg) / backup.STATE).read_text())["uploaded"] == calls[0]
    backup.run(cfg)
    assert len(calls) == 2


async def test_a_failed_backup_raises_a_health_warning_and_a_good_one_clears_it(tmp_path, monkeypatch):
    cfg = Config(log_dir=str(tmp_path))
    engine = Engine(cfg=cfg, league=League.load("data/league.example.json"), source=None,
                    live_llm=None, segment_llm=None, speech=None)

    def broken(cfg, force=False):
        raise OSError("disk full")

    monkeypatch.setattr(backup, "run", broken)
    await engine._backup("round end")
    assert "disk full" in engine.health.get("backup.failed").message
    monkeypatch.setattr(backup, "run", lambda cfg, force=False: None)
    await engine._backup("round end")
    assert engine.health.get("backup.failed") is None
