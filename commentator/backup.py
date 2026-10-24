"""Backups of the season database: gzipped copies in a folder (and, optionally, an S3 bucket), taken
when the booth starts and after every round, and only when something changed since the last one.

A copy is taken with SQLite's online backup API, so it's consistent even while the booth is writing,
and checked (PRAGMA integrity_check) before it's kept. Kept: the newest copy of each of the last
`daily` days that have one, and before those, the newest of each of the last `weekly` weeks. Days
with no golf take no copies, so a break never ages the history out.

Point backup.dir at a synced or other drive (OneDrive, Dropbox, a NAS, a USB stick) to keep copies
off the PC without S3.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config

log = logging.getLogger(__name__)

STAMP = "%Y%m%d-%H%M%S"
PREFIX, SUFFIX = "season-", ".db.gz"
STATE = "last-backup.json"


def folder(cfg: Config) -> Path:
    return Path(cfg.backup.dir) if cfg.backup.dir else Path(cfg.log_dir) / "backups"


def run(cfg: Config, force: bool = False) -> Path | None:
    """Back up cfg's database into its backup folder, and upload the newest copy to S3 if that's on and
    it isn't there yet (a failed upload is retried next time). The new copy, or None if unchanged."""
    b, dest = cfg.backup, folder(cfg)
    path = back_up(cfg.db_path, dest, b.daily, b.weekly, force=force)
    state = _state(dest)
    if b.s3 and state.get("file") and state.get("uploaded") != state["file"]:
        if not cfg.publish.bucket:
            raise RuntimeError("backup.s3 is on but publish.bucket isn't set")
        import boto3

        client = boto3.client("s3", region_name=cfg.publish.region) if cfg.publish.region else boto3.client("s3")
        upload(dest / state["file"], cfg.publish.bucket, b.s3_prefix, b.daily, b.weekly, client)
        (dest / STATE).write_text(json.dumps({**state, "uploaded": state["file"]}), encoding="utf-8")
    return path


def _when(name: str) -> datetime | None:
    if not (name.startswith(PREFIX) and name.endswith(SUFFIX)):
        return None
    try:
        return datetime.strptime(name[len(PREFIX):-len(SUFFIX)], STAMP)
    except ValueError:
        return None


def keep(names: list[str], daily: int = 14, weekly: int = 4) -> set[str]:
    """The backup files to keep: the newest of each of the last `daily` days with one, then the newest
    of each of the last `weekly` weeks older than those days. Other names are left alone."""
    dated = sorted(((w, n) for n in names if (w := _when(n))), reverse=True)
    kept, days, weeks = set(), [], []
    for when, name in dated:
        day = when.date()
        if day in days:
            continue
        if len(days) < daily:
            days.append(day)
            kept.add(name)
            continue
        week = when.isocalendar()[:2]
        if week in weeks or len(weeks) >= weekly:
            continue
        if week == days[-1].isocalendar()[:2]:
            continue   # the week the daily copies reach back into is covered by them
        weeks.append(week)
        kept.add(name)
    return kept


def _snapshot(db_path: Path, out: Path) -> None:
    with closing(sqlite3.connect(db_path)) as src, closing(sqlite3.connect(out)) as dst:
        src.backup(dst)
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
    if ok != "ok":
        raise RuntimeError(f"the copy failed its integrity check: {ok}")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _state(dest: Path) -> dict[str, Any]:
    try:
        return json.loads((dest / STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def back_up(db_path: str | Path, dest: str | Path, daily: int = 14, weekly: int = 4, force: bool = False,
            now: datetime | None = None) -> Path | None:
    """Copy the database into `dest` and prune old copies. Returns the new file, or None when nothing
    changed since the last copy (unless `force`)."""
    db_path, dest = Path(db_path), Path(dest)
    if not db_path.exists():
        raise FileNotFoundError(f"no database at {db_path}")
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest) as tmp:
        raw = Path(tmp) / "season.db"
        _snapshot(db_path, raw)
        digest = _sha256(raw)
        state = _state(dest)
        if not force and state.get("sha256") == digest and (dest / state.get("file", "")).is_file():
            return None
        name = PREFIX + (now or datetime.now()).strftime(STAMP) + SUFFIX
        part = Path(tmp) / name
        with open(raw, "rb") as fin, gzip.open(part, "wb", compresslevel=6) as fout:
            shutil.copyfileobj(fin, fout, 1 << 20)
        os.replace(part, dest / name)
    (dest / STATE).write_text(json.dumps({"file": name, "sha256": digest,
                                          "at": (now or datetime.now()).isoformat(timespec="seconds")}),
                              encoding="utf-8")
    names = [p.name for p in dest.iterdir() if _when(p.name)]
    for old in set(names) - keep(names, daily, weekly):
        (dest / old).unlink(missing_ok=True)
    return dest / name


def upload(path: Path, bucket: str, prefix: str = "backups/", daily: int = 14, weekly: int = 4,
           client: Any = None) -> None:
    """Upload a backup to s3://bucket/prefix and prune the copies there the same way."""
    if client is None:
        import boto3

        client = boto3.client("s3")
    prefix = prefix.strip("/") + "/"
    client.upload_file(str(path), bucket, prefix + path.name,
                       ExtraArgs={"ContentType": "application/gzip"})
    names = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        names += [o["Key"][len(prefix):] for o in page.get("Contents", [])]
    stale = [prefix + n for n in set(names) - keep(names, daily, weekly) if _when(n)]
    for chunk in range(0, len(stale), 1000):
        client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in stale[chunk:chunk + 1000]]})


def restore(backup: str | Path, db_path: str | Path, dest: str | Path, daily: int = 14,
            weekly: int = 4) -> Path | None:
    """Put a backup back as the database. The current database is backed up first (returned), and the
    restored copy is checked before it replaces anything. Stop the booth first."""
    backup, db_path = Path(backup), Path(db_path)
    with tempfile.TemporaryDirectory(dir=db_path.parent) as tmp:
        raw = Path(tmp) / "season.db"
        opener = gzip.open if backup.name.endswith(".gz") else open
        with opener(backup, "rb") as fin, open(raw, "wb") as fout:
            shutil.copyfileobj(fin, fout, 1 << 20)
        with closing(sqlite3.connect(raw)) as db:
            ok = db.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise RuntimeError(f"{backup.name} failed its integrity check: {ok}")
        # Read before this copy is taken: its pruning can drop an older copy from the same day.
        saved = back_up(db_path, dest, daily, weekly, force=True) if db_path.exists() else None
        os.replace(raw, db_path)
    return saved
