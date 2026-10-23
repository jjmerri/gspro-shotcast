"""The stats page as a static site: build it from the season database, and publish it to S3.

The heavy stats (season tables, strokes gained, scorecards, highlights) are computed here, once per
publish, into one JSON file per context. Shots and broadcast transcripts go into a small SQLite file
(stats.db) that the page queries in the browser with sql.js-httpvfs, which reads only the pages a
query needs over HTTP range requests, so the page stays fast however long the season gets.

    index.html                      the page
    vendor/sql.js-httpvfs-<v>/      the browser SQLite library
    v/<version>/summary-<ctx>.json  ctx: all, league, practice
    v/<version>/player/<ctx>/<key>.json   one player's courses, highlights and records
    v/<version>/stats.db            shots and transcripts
    latest.json                     {"version": ...}: which v/ the page reads

Each build is a new version, and latest.json is written last, so a page that's open while a new
round is published keeps reading the old files until it's refreshed.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from . import hole_replay
from . import season_analytics as A
from . import stats_web

PAGE = Path(__file__).with_name("stats_web.html")
VENDOR = Path(__file__).with_name("stats_vendor")
CONTEXTS = {"all": None, "league": "league", "practice": "practice"}
SHOT_FILTERS = ("date", "player", "course", "club", "lie", "round")   # the page's exact-match filters
SHOT_RANGE = {"from": ">=", "to": "<="}                               # and its date range (YYYY-MM-DD)
PAGE_SIZE = 1024   # small pages: a lookup fetches little more than the rows it needs
KEEP_VERSIONS = 2  # the newest, plus the one before for pages opened just before a publish

CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".json": "application/json", ".wasm": "application/wasm",
                 ".db": "application/vnd.sqlite3", ".txt": "text/plain; charset=utf-8"}


def player_key(name: str) -> str:
    """A player's file name: stable, and safe whatever the name."""
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]


def player_file(src: sqlite3.Connection, name: str, context: str | None, holes: list[dict]) -> dict[str, Any]:
    """What the page shows for one picked player beyond the summary: their courses (hole by hole),
    highlights and records."""
    return {"name": name, "courses": A.courses(src, context, name),
            "highlights": A.highlights(src, holes, context, name), "records": stats_web.records(src, context, name)}


def new_version() -> str:
    """Sorts in build order (to the microsecond), so pruning keeps the newest."""
    now = time.time()
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f".{int(now % 1 * 1e6):06d}-" + secrets.token_hex(3)


def build_data_db(src: sqlite3.Connection, out: Path, names: dict[str, str] | None = None) -> None:
    """Shots (newest first, as the page lists them) and each session's transcript."""
    tmp = out.with_name(out.name + ".tmp")
    tmp.unlink(missing_ok=True)
    shots = A.shot_log(src, None, limit=-1)
    league = {r[0]: r[1] for r in src.execute("SELECT id, is_league FROM shots")}
    round_of = stats_web.shot_rounds(src)
    sessions = [r[0] for r in src.execute("SELECT DISTINCT session_id FROM lines WHERE session_id IS NOT NULL")]
    with closing(sqlite3.connect(tmp)) as db:
        db.execute(f"PRAGMA page_size = {PAGE_SIZE}")
        db.execute("PRAGMA journal_mode = DELETE")
        db.executescript("""
            CREATE TABLE pub_shot (ord INTEGER PRIMARY KEY, id INTEGER, is_league INTEGER, date TEXT,
                                   player TEXT, course TEXT, club TEXT, lie TEXT, round INTEGER);
            CREATE TABLE pub_shot_json (ord INTEGER PRIMARY KEY, json TEXT NOT NULL);
            CREATE TABLE pub_transcript (session_id INTEGER PRIMARY KEY, json TEXT NOT NULL);
            CREATE TABLE pub_replay (round INTEGER, hole INTEGER, json TEXT NOT NULL, PRIMARY KEY (round, hole));
            CREATE TABLE pub_hole_image (id INTEGER PRIMARY KEY, src TEXT NOT NULL);
        """)
        db.executemany("INSERT INTO pub_shot VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       [(i, s["id"], league.get(s["id"]), s["date"], s["player"], s["course"], s["club"],
                         s["lie_before"], round_of.get(s["id"])) for i, s in enumerate(shots)])
        db.executemany("INSERT INTO pub_shot_json VALUES (?, ?)",
                       [(i, json.dumps(s, separators=(",", ":"))) for i, s in enumerate(shots)])
        db.executemany("INSERT INTO pub_transcript VALUES (?, ?)",
                       [(sid, json.dumps(A.transcript(src, sid, names), separators=(",", ":")))
                        for sid in sessions])
        replays = hole_replay.hole_replays(src)
        db.executemany("INSERT INTO pub_replay VALUES (?, ?, ?)",
                       [(rnd, hole, json.dumps(rp, separators=(",", ":"))) for (rnd, hole), rp in replays.items()])
        # Each hole's minimap once, however many rounds' replays draw on it.
        db.executemany("INSERT INTO pub_hole_image VALUES (?, ?)", hole_replay.hole_image_sources(
            src, {rp["map"]["id"] for rp in replays.values() if "map" in rp}).items())
        # Each filter walks its own index in the page's order, so the newest matches come first.
        for col in (*SHOT_FILTERS, "is_league"):
            db.execute(f"CREATE INDEX pub_shot_{col} ON pub_shot ({col}, ord)")
        db.execute("ANALYZE")   # lets SQLite pick the most selective filter's index
        db.commit()
        db.execute("VACUUM")
    tmp.replace(out)


def shot_query(context: str | None, filters: dict[str, str], limit: int = 500) -> tuple[str, list]:
    """The page's shot query (stats_web.html builds the same SQL): newest first, up to `limit`."""
    where, args = [], []
    if context in ("league", "practice"):
        where.append("s.is_league = ?")
        args.append(1 if context == "league" else 0)
    for key in SHOT_FILTERS:
        if filters.get(key):
            where.append(f"s.{key} = ?")
            args.append(int(filters[key]) if key == "round" else filters[key])
    for key, op in SHOT_RANGE.items():
        if filters.get(key):
            where.append(f"s.date {op} ?")
            args.append(filters[key])
    sql = ("SELECT j.json FROM pub_shot s JOIN pub_shot_json j ON j.ord = s.ord"
           + (" WHERE " + " AND ".join(where) if where else "") + f" ORDER BY s.ord LIMIT {int(limit)}")
    return sql, args


def build(db_path: str | Path, site: str | Path, names: dict[str, str] | None = None) -> str:
    """Build (or refresh) the site in `site` from the season database; returns the new version."""
    db_path, site = Path(db_path), Path(site)
    version = new_version()
    vdir = site / "v" / version
    vdir.mkdir(parents=True, exist_ok=True)
    with closing(stats_web.connect(db_path)) as src:
        for name, context in CONTEXTS.items():
            body: dict[str, Any] = stats_web.summary(src, context, names)
            body["db"] = {"name": db_path.name, "modified": db_path.stat().st_mtime, "published": time.time()}
            pdir = vdir / "player" / name
            pdir.mkdir(parents=True, exist_ok=True)
            holes = A.holes_played(src, context)
            for p in body["players"]:
                p["file"] = player_key(p["name"])
                (pdir / f"{p['file']}.json").write_text(
                    json.dumps(player_file(src, p["name"], context, holes), separators=(",", ":")), encoding="utf-8")
            (vdir / f"summary-{name}.json").write_text(json.dumps(body, separators=(",", ":")), encoding="utf-8")
        build_data_db(src, vdir / "stats.db", names)
    shutil.copyfile(PAGE, site / "index.html")
    for lib in VENDOR.iterdir():
        if lib.is_dir():
            shutil.copytree(lib, site / "vendor" / lib.name, dirs_exist_ok=True)
    tmp = site / "latest.json.tmp"
    tmp.write_text(json.dumps({"version": version}), encoding="utf-8")
    tmp.replace(site / "latest.json")
    for old in sorted((site / "v").iterdir(), key=lambda p: p.name)[:-KEEP_VERSIONS]:
        if old.name != version:
            shutil.rmtree(old, ignore_errors=True)
    return version


# ---- S3 ---------------------------------------------------------------------------------------

def _cache_control(key: str) -> str:
    if key.startswith(("v/", "vendor/")):
        return "public, max-age=31536000, immutable"   # a version's files never change
    return "no-cache"                                   # index.html, latest.json: always revalidate


def upload(site: str | Path, bucket: str, prefix: str = "", client: Any = None) -> str:
    """Upload the built site to s3://bucket/prefix: the new version's files and the page first,
    latest.json last (so nobody is pointed at files that aren't there yet), then drop all but the
    newest KEEP_VERSIONS versions. Returns the published version."""
    site = Path(site)
    if client is None:
        import boto3

        client = boto3.client("s3")
    prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
    version = json.loads((site / "latest.json").read_text(encoding="utf-8"))["version"]
    files = [p for p in site.rglob("*") if p.is_file() and not p.name.endswith(".tmp")]
    rel = lambda p: p.relative_to(site).as_posix()   # noqa: E731
    keep = {f"v/{version}/"}
    order = ([p for p in files if rel(p).startswith(f"v/{version}/")]
             + [p for p in files if rel(p).startswith("vendor/")]
             + [site / "index.html"])
    existing = _list(client, bucket, prefix)
    for path in order:
        key = rel(path)
        if key.startswith("vendor/") and prefix + key in existing:
            continue   # vendored files are versioned by their folder name
        client.upload_file(str(path), bucket, prefix + key, ExtraArgs={
            "ContentType": CONTENT_TYPES.get(path.suffix, "application/octet-stream"),
            "CacheControl": _cache_control(key)})
    client.put_object(Bucket=bucket, Key=prefix + "latest.json", Body=json.dumps({"version": version}).encode(),
                      ContentType=CONTENT_TYPES[".json"], CacheControl=_cache_control("latest.json"))
    versions = sorted({k[len(prefix) + 2:].split("/", 1)[0] for k in existing if k.startswith(prefix + "v/")}
                      | {version})
    keep |= {f"v/{v}/" for v in versions[-KEEP_VERSIONS:]}   # `keep` already has this one
    stale = [k for k in existing if k.startswith(prefix + "v/") and not any(k.startswith(prefix + d) for d in keep)]
    for chunk in range(0, len(stale), 1000):
        client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in stale[chunk:chunk + 1000]]})
    return version


def _list(client: Any, bucket: str, prefix: str) -> set[str]:
    keys: set[str] = set()
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        keys |= {o["Key"] for o in page.get("Contents", [])}
    return keys


def publish(db_path: str | Path, site: str | Path, bucket: str, prefix: str = "", region: str | None = None,
            names: dict[str, str] | None = None) -> str:
    """Build the site from the database and upload it. Returns the published version."""
    import boto3

    build(db_path, site, names)
    return upload(site, bucket, prefix, boto3.client("s3", region_name=region) if region else boto3.client("s3"))


def main(argv: list[str] | None = None) -> None:
    """`python -m commentator.stats_site`: what the booth runs, in its own process, after a round."""
    import argparse

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", required=True)
    p.add_argument("--site", required=True)
    p.add_argument("--bucket", required=True)
    p.add_argument("--prefix", default="")
    p.add_argument("--region")
    p.add_argument("--names", default="{}", help="persona keys to on-air names, as JSON")
    a = p.parse_args(argv)
    version = publish(a.db, a.site, a.bucket, a.prefix, a.region, json.loads(a.names))
    print(f"Published {version} to s3://{a.bucket}/{a.prefix}")


if __name__ == "__main__":
    main()
