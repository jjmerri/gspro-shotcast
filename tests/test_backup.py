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
        db.execute("CREATE TABLE IF NOT EXISTS shots (v TEXT)")
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


def test_restore_keeps_the_chosen_copy_and_saves_even_a_corrupted_current_file(tmp_path):
    db, dest = tmp_path / "season.db", tmp_path / "backups"
    _db(db, "old")
    old = backup.back_up(db, dest, daily=1, weekly=0)
    db.write_bytes(b"not a database")
    (tmp_path / "season.db-journal").write_bytes(b"stale")
    saved = backup.restore(old, db, dest)
    assert _value(db) == "old" and old.exists()
    assert not (tmp_path / "season.db-journal").exists()
    with gzip.open(saved) as fh:
        assert fh.read() == b"not a database"
    assert backup.keep([p.name for p in dest.iterdir()]) == {old.name}   # pruning never sees it


def test_restore_saves_the_current_file_with_whats_still_in_its_log(tmp_path):
    import shutil

    db, dest, crashed = tmp_path / "season.db", tmp_path / "backups", tmp_path / "crashed"
    _db(db, "old")
    old = backup.back_up(db, dest, daily=1, weekly=0)
    with closing(sqlite3.connect(db)) as booth:
        booth.execute("PRAGMA journal_mode = WAL")
        booth.execute("PRAGMA wal_autocheckpoint = 0")
        booth.execute("UPDATE t SET v = 'last shot'")
        booth.commit()
        crashed.mkdir()   # the files as a crash leaves them: the last shot only in the log
        for name in ("season.db", "season.db-wal", "season.db-shm"):
            shutil.copy(tmp_path / name, crashed / name)
    saved = backup.restore(old, crashed / "season.db", dest)
    assert _value(crashed / "season.db") == "old"
    with gzip.open(saved) as fh:
        (tmp_path / "saved.db").write_bytes(fh.read())
    assert _value(tmp_path / "saved.db") == "last shot"
    with gzip.open(backup.back_up(db, dest, force=True)) as fh:
        assert fh.read(20)[18:20] == b"\x01\x01"   # a copy is one file, not in WAL mode


def test_restore_refuses_an_empty_or_foreign_file_and_puts_the_journal_back_on_failure(tmp_path, monkeypatch):
    import pytest

    db, dest = tmp_path / "season.db", tmp_path / "backups"
    _db(db, "live")
    empty = tmp_path / "empty.db.gz"
    with gzip.open(empty, "wb"):
        pass
    other = tmp_path / "other.db"
    with closing(sqlite3.connect(other)) as o:
        o.execute("CREATE TABLE x (v)")
        o.commit()
    for bad in (empty, other):
        with pytest.raises(RuntimeError):
            backup.restore(bad, db, dest)
    assert _value(db) == "live"

    good = backup.back_up(db, dest)
    journal = tmp_path / "season.db-journal"
    journal.write_bytes(b"hot")
    real = backup.os.replace

    def locked(src, dst):
        if str(dst) == str(db):
            raise PermissionError("in use")
        real(src, dst)

    monkeypatch.setattr(backup.os, "replace", locked)
    with pytest.raises(PermissionError):
        backup.restore(good, db, dest)
    assert journal.read_bytes() == b"hot"


def test_keep_with_no_daily_copies_keeps_weekly_ones():
    names = [_name(datetime(2026, 3, 2) + timedelta(days=d)) for d in range(21)]
    assert len(backup.keep(names, daily=0, weekly=2)) == 2


def test_s3_prefix_must_stay_under_backups():
    import pytest
    from pydantic import ValidationError
    from commentator.config import BackupConfig

    assert BackupConfig(s3_prefix="backups/simpc/").s3_prefix == "backups/simpc/"
    for bad in ("archive/", "backupsx/", "backups/../v/"):
        with pytest.raises(ValidationError):
            BackupConfig(s3_prefix=bad)


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
    cfg = Config(log_dir=str(tmp_path), db_path=str(tmp_path / "season.db"))
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


def test_a_copy_before_an_update_outlives_the_new_versions_first_copy(tmp_path):
    cfg = Config(log_dir=str(tmp_path), db_path=str(tmp_path / "season.db"))
    assert backup.pre_update(cfg, "0.1.0") is None              # no season yet
    _db(cfg.db_path, "before")
    start = datetime(2026, 1, 1, 12)
    pres = [backup.pre_update(cfg, f"0.1.{i}", now=start + timedelta(minutes=i)) for i in range(5)]
    _db(cfg.db_path, "migrated")
    backup.back_up(cfg.db_path, backup.folder(cfg), daily=1, weekly=0, now=start + timedelta(hours=1))
    backup.pre_update(cfg, "0.1.9", now=start + timedelta(hours=2))
    left = sorted(p.name for p in backup.folder(cfg).glob(backup.PRE_UPDATE + "*"))
    assert len(left) == backup.PRE_UPDATE_KEPT and pres[0].name not in left and pres[4].name in left
    with gzip.open(pres[4]) as fin:
        (tmp_path / "restored.db").write_bytes(fin.read())
    assert _value(tmp_path / "restored.db") == "before"


def _older_season(path):
    """A season file as a release before store.FORMAT left it: an older format, no WAL, one shot."""
    from commentator.store import Store

    Store(str(path)).db.close()
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA journal_mode = DELETE")
        db.execute("INSERT INTO shots (session_id, ts, player, is_putt, raw) VALUES (1, 1, 'Ann', 0, '{}')")
        db.execute("PRAGMA user_version = 5")
        db.commit()


def test_a_season_file_in_an_older_format_is_copied_as_it_was_before_its_upgraded(tmp_path, caplog):
    from commentator.store import FORMAT, Store

    path = tmp_path / "season.db"
    Store(str(path)).db.close()   # a new season: nothing to keep
    assert not (tmp_path / "backups").exists()
    _older_season(path)
    Store(str(path)).db.close()
    made = list((tmp_path / "backups").glob(backup.PRE_UPDATE + "*"))
    assert len(made) == 1 and "-from-format-5" in made[0].name
    with gzip.open(made[0]) as fin:
        (tmp_path / "restored.db").write_bytes(fin.read())
    with closing(sqlite3.connect(tmp_path / "restored.db")) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert db.execute("SELECT player FROM shots").fetchall() == [("Ann",)]
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == FORMAT
    with closing(sqlite3.connect(path)) as db:   # in WAL mode, as a closed booth leaves it: no -shm to read by
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for side in ("-wal", "-shm"):
        (tmp_path / f"season.db{side}").unlink(missing_ok=True)
    Store(str(path)).db.close()   # upgraded already
    assert len(list((tmp_path / "backups").glob(backup.PRE_UPDATE + "*"))) == 1
    assert "not copied" not in caplog.text


def test_an_older_season_file_is_copied_even_beside_a_newer_copy_of_another(tmp_path):
    from commentator.store import Store

    cfg = Config(log_dir=str(tmp_path / "logs"), db_path=str(tmp_path / "season.db"))
    _older_season(cfg.db_path)
    _older_season(tmp_path / "other.db")
    backup.pre_update(cfg, "0.1.134")   # (the other season's copy, made since)
    Store(str(tmp_path / "other.db"), backups=backup.folder(cfg)).db.close()
    assert len(list(backup.folder(cfg).glob("*-from-format-5*"))) == 1


def test_the_booth_copies_an_older_season_file_into_the_backup_folder_set(tmp_path):
    from commentator.__main__ import _build_engine

    cfg = Config(log_dir=str(tmp_path), db_path=str(tmp_path / "season.db"))
    cfg.backup.dir = str(tmp_path / "kept")
    cfg.league_file = "data/league.example.json"
    cfg.screen.ocr = cfg.screen.send_images = False
    cfg.commentary.enabled = False
    _older_season(cfg.db_path)
    _build_engine(cfg).store.db.close()
    assert len(list((tmp_path / "kept").glob(backup.PRE_UPDATE + "*"))) == 1
