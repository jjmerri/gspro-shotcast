"""Build and publish the public Shotcast demo site (tools/demo/demo-site.yaml, the shotcast-demo stack).

Maintainer-only: the app never runs this, and nothing here changes what a user installs or publishes. It
uses the production code (stats_site, highlights, the Engine's recap) on a copy of a real season
database, made safe to show:

  * Josh (who built Shotcast, and agreed to be shown) keeps his first name, his rounds and the booth's
    commentary on them, as "Josh Mercer". Everyone else gets a made-up name everywhere it appears (stats,
    teams, the shots' raw data, anything the booth said), and the booth's words about anyone else's shots
    are dropped. Where the recordings' booth says his real surname, it's muted (MUTES, --mute).
  * A made-up league: six teams of those players, whose week 1 is Josh's 71 at Premonition (the only
    league round with a recording) and five other real rounds at made-up handicaps, so the Home tab has
    results and standings and the week has a recap video.
  * Videos come only from Josh's solo streams (DEMO_KEEP, copied from the stats bucket where they won't
    expire), since the GSPro screen shows each player's name. Premonition is also looped as the "live"
    broadcast by the stack's function: its hole 8 on a loop, with a scripted chat (CHAT below).

    python tools/demo/build.py --db season.db [--no-recap] [--upload]
    python tools/demo/build.py --stored-db --no-recap --upload   # what .github/workflows/demo.yml runs
    python tools/demo/build.py --live      # just the live loop and its chat
    python tools/demo/build.py --mute      # mute MUTES in the kept streams (once, after they're copied)

A build from --db keeps its redacted copy of the season, and --upload stores it in the demo bucket
(demo/season.db, which CloudFront won't serve), so the workflow can rebuild the site whenever the stats
site's code changes without the real database. It skips the recap (which costs model and TTS calls);
the last one uploaded stays.

The stack, once (and again with DomainName/CertificateArn once demo.thegolfshed.app's certificate is
issued; DNS is on Cloudflare: a CNAME from demo to the DistributionDomain output):

    aws cloudformation deploy --stack-name shotcast-demo --template-file tools/demo/demo-site.yaml \\
        --capabilities CAPABILITY_NAMED_IAM [--parameter-overrides DomainName=... CertificateArn=...]

Needs AWS credentials for the stats bucket (read) and the demo stack (write), and with the recap, the
keys in .env (the closer's model and TTS).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

WORK = Path(os.environ.get("DEMO_WORK", "/tmp/shotcast-demo"))
SITE = WORK / "site"
STACK = "shotcast-demo"
REGION = "us-east-1"
STATS_BUCKET = "shotcast-stats-bucket-9qzb6obvqvt3"
DEMO_KEEP = "demo-keep/"          # in the stats bucket: the streams shown, and their recordings.json
ME = "Josh Merritt"                 # in the season database
SHOWN = "Josh Mercer"              # on the demo
LIVE_SESSION = "20261104-133911-67149a"   # Premonition, Nov 4: the looped "live" round
ROUND_LABELS = {LIVE_SESSION: "Premonition · Josh Mercer",
                "20261030-225803-70cea1": "Paynes Valley · Josh Mercer · practice",
                "20261029-101254-d9d0ae": "Abacoa Golf Club · Josh Mercer · practice"}

# Real name -> made-up name. Whole names first, then surnames, then first names, so "Kathy Merritt"
# gets her own and Josh keeps his first name. Corey keeps his: the booth says it on Josh's recordings.
NAMES = {
    "Kathy Merritt": "Linda Hayes", "Corey Martin": "Corey Lane", "Dylan Martin": "Ethan Lane",
    "Paul Martin": "Greg Lane", "Kyle Martin": "Ben Lane", "Dan Binder": "Mike Russo",
    "Derek Binder": "Tony Russo", "Drew Binder": "Sam Russo", "Kyle Curley": "Ben Carter",
    "Jay Piwnicki": "Jake Morrow", "Brian Piehler": "Chris Lyons", "Bob Dunn": "Al Fisher",
    "Matt Ferri": "Nate Brooks", "Garrett Norris": "Luke Barrett",
    "Merritt": "Mercer", "Binder": "Russo", "Martin": "Lane", "Curley": "Carter", "Piwnicki": "Morrow", "Piehler": "Lyons",
    "Dunn": "Fisher", "Ferri": "Brooks", "Norris": "Barrett",
    "Kathy": "Linda", "Dylan": "Ethan", "Paul": "Greg", "Kyle": "Ben", "Dan": "Mike", "Derek": "Tony",
    "Drew": "Sam", "Jay": "Jake", "Brian": "Chris", "Bob": "Al", "Matt": "Nate", "Garrett": "Luke",
    "Frankie": "Vinnie", "David": "Marcus", "Travis": "Dean",
}
_NAME_RE = re.compile(r"\b(" + "|".join(sorted(map(re.escape, NAMES), key=len, reverse=True)) + r")\b")

# The made-up league's week 1: team -> (its rounds, handicap strokes). Team names are the renamed
# rounds' own, so the stats page and the league agree.
WEEK1 = {"Josh and Corey": ([79], 2), "Mike and Tony": ([52], 14), "Ben C and Al": ([56, 57], 15),
         "Jake and Chris": ([80, 81], 12), "Sam and Nate": ([51], 26), "Greg and Dean": ([34], 17)}
TEAMS = {"Josh and Corey": [SHOWN, "Corey Lane"], "Mike and Tony": ["Mike Russo", "Tony Russo"],
         "Ben C and Al": ["Ben Carter", "Al Fisher"], "Jake and Chris": ["Jake Morrow", "Chris Lyons"],
         "Sam and Nate": ["Sam Russo", "Nate Brooks"], "Greg and Dean": ["Greg Lane", "Dean Price"]}
WEEK1_END = "2026-11-04"
POINTS = [70, 60, 50, 45, 40, 35]

# The "live" loop: hole 8 of that round, segments FIRST..LAST of its stream (tee to the birdie putt and
# the booth's look ahead to 9), 2 minutes 8 seconds.
LIVE_SEGMENTS = (350, 381)

# Where the recordings' booth says Josh's real surname: stream -> (from, to) in seconds of the recording
# (its segments' timestamps), from a word-timed transcript and the gaps either side of the word. --mute silences them.
MUTES = {LIVE_SESSION: [(65.48, 65.88), (84.18, 84.57), (395.40, 395.80)],
         "20261029-101254-d9d0ae": [(18.43, 18.80), (32.10, 32.47)]}

# The live chat's script for the loop: seconds into it (a little after each moment shows), who, what.
# Made up, about what's on screen.
CHAT = [
    (6, "Sarah", "hi from Ohio 👋"), (14, "Dave K", "is this live?"),
    (24, "mike_r", "fairway finder"), (40, "Tom H", "the commentary is wild, it's all AI?"),
    (58, "golfnut22", "how does the booth know all these stats?"), (80, "Sarah", "nice wedge"),
    (92, "Dave K", "27 feet for birdie, c'mon"), (110, "Tom H", "BIRDIE 🐦"), (113, "mike_r", "🔥🔥"),
    (122, "golfnut22", "back to even, let's go"),
]


# In GitHub Actions the role's credentials are in the environment; on the maintainer's machine, keys
# there are a .env's (for other things), and the AWS CLI and boto3 should use the default profile.
IN_CI = os.environ.get("GITHUB_ACTIONS") == "true"


def aws_env() -> dict:
    if IN_CI:
        return dict(os.environ)
    return {k: v for k, v in os.environ.items() if k not in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")}


def stack_outputs() -> dict:
    out = subprocess.run(["aws", "--region", REGION, "cloudformation", "describe-stacks", "--stack-name", STACK,
                          "--query", "Stacks[0].Outputs", "--output", "json"],
                         env=aws_env(), check=True, capture_output=True, text=True).stdout
    return {o["OutputKey"]: o["OutputValue"] for o in json.loads(out)}


# ---- the database ---------------------------------------------------------------------------------

def rename(text):
    return _NAME_RE.sub(lambda m: NAMES[m.group(1)], text) if isinstance(text, str) else text


def mine_at(db: sqlite3.Connection) -> "callable":
    """ts -> whether Josh is the one playing then: the shots either side of it, in its session, are his."""
    shots: dict = {}
    for sess, ts, player in db.execute("SELECT session_id, ts, player FROM shots ORDER BY ts"):
        shots.setdefault(sess, []).append((ts, player))

    def check(sess, ts):
        got = shots.get(sess) or []
        before = [p for t, p in got if t <= ts][-1:]
        after = [p for t, p in got if t > ts][:1]
        return bool(before or after) and all(p == ME for p in before + after)
    return check


def redact(src: Path, out: Path) -> None:
    """The demo's copy of the season: everyone renamed, and the booth's words kept only about Josh. A copy
    that's already the demo's (the stored one) is only checked."""
    out.unlink(missing_ok=True)
    shutil.copy(src, out)
    db = sqlite3.connect(out)
    if not db.execute("SELECT 1 FROM shots WHERE player = ? LIMIT 1", (ME,)).fetchone():
        demo = db.execute("SELECT 1 FROM shots WHERE player = ? LIMIT 1", (SHOWN,)).fetchone()
        db.close()
        if not demo:
            raise SystemExit(f"{src} isn't a season database with {ME}'s rounds, or the demo's copy of one")
        return _check(out)
    player_of = dict(db.execute("SELECT id, player FROM shots"))
    is_mine = mine_at(db)
    drop = [i for i, sess, shot, ts in db.execute("SELECT id, session_id, shot_id, ts FROM lines")
            if not (player_of.get(shot) == ME if shot is not None else is_mine(sess, ts))]
    db.executemany("DELETE FROM lines WHERE id = ?", [(i,) for i in drop])
    for table in ("asides", "predictions"):
        gone = [(i,) for i, sess, ts in db.execute(f"SELECT id, session_id, ts FROM {table}") if not is_mine(sess, ts)]
        db.executemany(f"DELETE FROM {table} WHERE id = ?", gone)
    for table in ("news_heard", "form_mentions"):
        db.execute(f"DELETE FROM {table} WHERE player IS NOT ?", (ME,))
    for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        cols = [c[1] for c in db.execute(f"PRAGMA table_info({table})") if c[2].upper() in ("TEXT", "")]
        keys = [c[1] for c in db.execute(f"PRAGMA table_info({table})") if c[5]] or ["rowid"]
        for row in db.execute(f"SELECT {', '.join(keys + cols)} FROM {table}").fetchall():
            key, vals = row[:len(keys)], row[len(keys):]
            new = [rename(v) for v in vals]
            if new != list(vals):
                db.execute(f"UPDATE {table} SET {', '.join(f'{c} = ?' for c in cols)} WHERE "
                           + " AND ".join(f"{k} = ?" for k in keys), (*new, *key))
    ids = [i for ids, _ in WEEK1.values() for i in ids]
    marks = ",".join("?" * len(ids))
    db.execute(f"UPDATE rounds SET is_league = 1 WHERE id IN ({marks})", ids)
    for sess, ended in db.execute(f"SELECT session_id, ended_at FROM rounds WHERE id IN ({marks})", ids).fetchall():
        db.execute("UPDATE player_rounds SET is_league = 1 WHERE session_id = ? AND ended_at = ?", (sess, ended))
    db.commit()
    db.execute("VACUUM")
    db.close()
    _check(out)


def _check(out: Path) -> None:
    leaks = sorted({w for w in NAMES if " " not in w and _leaks(out, w)})
    if leaks:
        raise SystemExit(f"Real names still in the demo database: {leaks}")


def _leaks(path: Path, word: str) -> bool:
    db = sqlite3.connect(path)
    try:
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
            cols = [c[1] for c in db.execute(f"PRAGMA table_info({table})") if c[2].upper() in ("TEXT", "")]
            for c in cols:
                for (v,) in db.execute(f"SELECT {c} FROM {table} WHERE {c} LIKE ?", (f"%{word}%",)):
                    if re.search(rf"\b{word}\b", v or ""):
                        return True
        return False
    finally:
        db.close()


def league_data() -> dict:
    schedule = [{"week": 1, "course": "Premonition", "format": "Best Ball", "holes": "18 Holes", "hole_count": 18,
                 "start_hole": 1, "tees": "Blue", "start_date": None, "end_date": WEEK1_END, "ctp_holes": [4, 11]}]
    for w, course in enumerate(["Bethpage Black", "Riviera", "The Ancient Course", "Pine Valley",
                                "Kiawah Ocean Course", "Sand Valley", "Cabot Cliffs"], start=2):
        schedule.append({"week": w, "course": course, "format": "Best Ball", "holes": "18 Holes", "hole_count": 18,
                         "start_hole": 1, "tees": "Blue", "start_date": None, "end_date": f"2026-12-{w * 3:02d}",
                         "ctp_holes": [3, 12]})
    return {
        "league_name": "the Demo Sim League", "season": 2026, "total_weeks": len(schedule),
        "format": "Six two-person teams. Every team plays each week's round on its own schedule, best ball, "
                  "net of handicap, and earns points for where it places: 1st 70, 2nd 60, 3rd 50, 4th 45, "
                  "5th 40, 6th 35. Ties split the points of the places they cover.",
        "prizes": [], "rules": [], "gspro_settings": [], "storylines": [], "pronunciations": {},
        "teams": [{"name": t, "number": f"Team {i}", "aliases": [], "players": [
            {"name": p, "aliases": [p.split()[0]], "handicap": None, "pronouns": "he/him", "notes":
             "Built this booth. An occasional light ribbing about that is fine, but don't lean on it."
             if p == SHOWN else ""} for p in players]} for i, (t, players) in enumerate(TEAMS.items(), 1)],
        "schedule": schedule,
    }


def week_holes(db, ids, strokes):
    """A team's net to par on each hole: the best of its rounds' scores, less its strokes, hardest first."""
    rows = [db.execute("SELECT scores, hole_pars FROM rounds WHERE id = ?", (i,)).fetchone() for i in ids]
    rows = [(json.loads(a), json.loads(b)) for a, b in rows]
    holes = sorted(int(h) for h in rows[0][1])
    hardest = sorted(holes, key=lambda h: (h * 7) % len(holes))
    given = {h: 0 for h in holes}
    for i in range(strokes):
        given[hardest[i % len(holes)]] += 1
    return {h: min(sc[str(h)] for sc, _ in rows) - rows[0][1][str(h)] - given[h] for h in holes}


# ---- S3 for the build: writes to SITE, the streams read from the stats bucket's DEMO_KEEP ---------

class Overlay:
    class exceptions:
        class NoSuchKey(Exception):
            pass

    def __init__(self):
        import boto3
        self.s3 = boto3.Session().client("s3", region_name=REGION)

    def _bytes(self, key):
        p = SITE / key
        if p.exists():
            return p.read_bytes()
        if not key.startswith("live/s/"):
            raise self.exceptions.NoSuchKey(key)
        try:
            data = self.s3.get_object(Bucket=STATS_BUCKET, Key=DEMO_KEEP + key[len("live/"):])["Body"].read()
        except self.s3.exceptions.NoSuchKey:
            raise self.exceptions.NoSuchKey(key)
        if not key.endswith(".m3u8"):   # a segment, cut into a reel or recap: kept for the next one
            CACHE.joinpath(key).parent.mkdir(parents=True, exist_ok=True)
            CACHE.joinpath(key).write_bytes(data)
        return data

    def get_object(self, Bucket, Key):
        cached = CACHE / Key
        data = cached.read_bytes() if Key.startswith("live/s/") and cached.exists() else self._bytes(Key)

        class Body:
            def read(_):
                return data
        return {"Body": Body()}

    def put_object(self, Bucket, Key, Body, **_):
        p = SITE / Key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(Body if isinstance(Body, bytes) else Body.encode())

    def copy_object(self, Bucket, Key, CopySource, **_):
        self.put_object(Bucket, Key, self.get_object(Bucket, CopySource["Key"])["Body"].read())

    def delete_objects(self, Bucket, Delete):
        for o in Delete["Objects"]:
            (SITE / o["Key"]).unlink(missing_ok=True)

    def get_paginator(self, _):
        class P:
            def paginate(self, Bucket, Prefix):
                d = SITE / Prefix.rsplit("/", 1)[0]
                keys = [f.relative_to(SITE).as_posix() for f in d.rglob("*") if f.is_file()] if d.exists() else []
                return [{"Contents": [{"Key": k} for k in keys if k.startswith(Prefix)]}]
        return P()


CACHE = WORK / "segments"
SOURCE = WORK / "demo-source.db"      # the redacted season, before the build adds the league's results
SOURCE_KEY = "demo/season.db"         # where --upload keeps it, for --stored-db
TOUR = Path(__file__).with_name("tour.js")


# ---- the build ------------------------------------------------------------------------------------

def load_env() -> None:
    env = Path(os.environ.get("SHOTCAST_ENV", Path.home() / "workspace/gspro-commentator/.env"))
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def build(db_src: Path, recap: bool) -> None:
    from commentator import highlights, stats_site
    from commentator.board import Board
    from commentator.config import load_config
    from commentator.engine import Engine
    from commentator.league import League
    from commentator.llm import build_llm
    from commentator.sources.replay import ReplaySource
    from commentator.speech import SpeechQueue
    from commentator.stats_web import round_groups
    from commentator.store import Store
    from commentator.tts import build_tts
    from commentator.tts.console_tts import ConsoleTTS
    from tests.test_core import FakeLLM

    WORK.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(SITE, ignore_errors=True)
    (SITE / "live").mkdir(parents=True)
    db_path = WORK / "season.db"
    redact(db_src, db_path)
    if db_src.resolve() != SOURCE.resolve():
        shutil.copy(db_path, SOURCE)

    client = Overlay()
    recs = json.loads(client.s3.get_object(Bucket=STATS_BUCKET, Key=DEMO_KEEP + "recordings.json")["Body"].read())
    for r in recs["recordings"]:
        for x in r["rounds"]:
            x["label"] = ROUND_LABELS.get(r["session"], rename(x["label"]))
        r.pop("keep_until", None)
    (SITE / "live/recordings.json").write_text(json.dumps(recs))

    league = League(league_data())
    store = Store(str(db_path))
    for team, (ids, strokes) in WEEK1.items():
        holes = week_holes(store.db, ids, strokes)
        print(f"week 1: {team} {sum(holes.values()):+d}")
        store.record_week_result(1, WEEK1_END, team, sum(holes.values()), ids[0], holes)

    reels: dict = {}
    stats_site.build(db_path, SITE, reels=reels, site_name="Shotcast demo", news=False)
    add_tour()
    _, of_row = round_groups(store.db)
    t = time.time()
    shown = {of_row[i] for i in WEEK1["Josh and Corey"][0]}
    print("reels:", highlights.sync(client, "demo", "", {k: v for k, v in reels.items() if k in shown}),
          f"{time.time() - t:.0f}s")

    cfg = load_config(REPO / "config.example.yaml")
    cfg.log_dir = str(WORK / "logs")
    cfg.publish.bucket, cfg.publish.prefix = "demo", ""
    llm = FakeLLM(cfg.llm.live)
    e = Engine(cfg=cfg, league=league, source=ReplaySource(str(REPO / "data/sample_shots.jsonl")), live_llm=llm,
               segment_llm=llm, speech=SpeechQueue(cfg, build_tts(cfg.tts) if recap else ConsoleTTS()),
               closer_llm=build_llm(cfg.llm.closer) if recap else llm, store=store, rng=random.Random(0))
    e.board = Board({}, league, POINTS)
    e._s3 = lambda: client
    board = e._league_board()
    page = board.page(time.strftime("%Y-%m-%d"))
    for r in (page.get("week") or {}).get("rows") or []:
        r.pop("sim", None)   # the made-up league has no Squabbit board for the sim's counts to stand in for
    (page.get("standings") or {}).pop("provisional", None)
    (SITE / "league.json").write_text(json.dumps({**page, "updated": round(time.time())}))
    if recap:
        t = time.time()
        asyncio.run(e._build_recap())
        print(f"recap: {time.time() - t:.0f}s")

    write_live(client)


def add_tour() -> None:
    """The demo's walkthrough (tour.js): its own file, and a script tag in the demo's copy of the page."""
    import hashlib
    js = TOUR.read_bytes()
    (SITE / "tour.js").write_bytes(js)
    page = SITE / "index.html"
    tag = f'<script src="tour.js?v={hashlib.sha256(js).hexdigest()[:10]}"></script>\n</body>'
    html = page.read_text(encoding="utf-8")
    if html.count("</body>") != 1:
        raise SystemExit("The page has no single </body> for the tour's script")
    page.write_text(html.replace("</body>", tag), encoding="utf-8")


def stored_db() -> Path:
    """The redacted season the last --db build uploaded (demo/season.db), for a build without the real one."""
    import boto3
    WORK.mkdir(parents=True, exist_ok=True)
    boto3.Session().client("s3", region_name=REGION).download_file(stack_outputs()["BucketName"], SOURCE_KEY, str(SOURCE))
    return SOURCE


def write_live(client) -> None:
    """The live function's script (demo/live.json): the loop's segments and the chat."""
    full = client.get_object("", f"live/s/{LIVE_SESSION}/full.m3u8")["Body"].read().decode()
    segs = [float(x) for x in re.findall(r"#EXTINF:([\d.]+)", full)][LIVE_SEGMENTS[0]:LIVE_SEGMENTS[1] + 1]
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "live.json").write_text(json.dumps({"session": LIVE_SESSION, "first": LIVE_SEGMENTS[0], "segments": segs,
                                                "chat": [{"t": t, "name": n, "text": x} for t, n, x in CHAT]}))


def upload_live() -> None:
    """Only the live loop and its chat: the function reads it within 5 minutes."""
    import boto3
    s3 = boto3.Session().client("s3", region_name=REGION)
    s3.upload_file(str(WORK / "live.json"), stack_outputs()["BucketName"], "demo/live.json",
                   ExtraArgs={"ContentType": "application/json"})
    print("live loop up")


def mute() -> None:
    """Silence MUTES in the kept streams' segments, in the stats bucket's DEMO_KEEP (what builds read) and
    the demo's copy. The video is copied untouched and the timestamps kept, so the playlists still fit."""
    import tempfile

    import boto3
    from commentator.burn import ffmpeg
    from commentator.live_stream import TYPES

    s3 = boto3.Session().client("s3", region_name=REGION)
    demo = stack_outputs()["BucketName"]
    for sess, spans in MUTES.items():
        full = s3.get_object(Bucket=STATS_BUCKET, Key=f"{DEMO_KEEP}s/{sess}/full.m3u8")["Body"].read().decode()
        t, segs = 0.0, []
        for d, name in zip(re.findall(r"#EXTINF:([\d.]+)", full), re.findall(r"^(stream\d+\.ts)$", full, re.M)):
            segs.append((t, t + float(d), name))
            t += float(d)
        for name in sorted({n for a, b, n in segs for x, y in spans if x < b and y > a}):
            with tempfile.TemporaryDirectory() as tmp:
                src, out = Path(tmp, "in.ts"), Path(tmp, "out.ts")
                s3.download_file(STATS_BUCKET, f"{DEMO_KEEP}s/{sess}/{name}", str(src))
                quiet = "+".join(f"between(t,{x:.2f},{y:.2f})" for x, y in spans)
                subprocess.run([ffmpeg(), "-loglevel", "error", "-y", "-copyts", "-i", str(src), "-map", "0",
                                "-c:v", "copy", "-af", f"volume=0:enable='{quiet}'", "-c:a", "aac", "-b:a", "128k",
                                "-muxdelay", "0", "-muxpreload", "0", "-f", "mpegts", str(out)], check=True)
                for bucket, key in ((STATS_BUCKET, f"{DEMO_KEEP}s/{sess}/{name}"), (demo, f"live/s/{sess}/{name}")):
                    s3.upload_file(str(out), bucket, key, ExtraArgs={"ContentType": TYPES[".ts"],
                                                                     "CacheControl": "public, max-age=86400"})
            CACHE.joinpath(f"live/s/{sess}/{name}").unlink(missing_ok=True)
            print("muted", sess, name)


# ---- the upload -----------------------------------------------------------------------------------

def upload() -> None:
    import boto3
    from commentator import stats_site
    from commentator.live_stream import TYPES

    out = stack_outputs()
    bucket = out["BucketName"]
    s3 = boto3.Session().client("s3", region_name=REGION)
    version = stats_site.upload(SITE, bucket, client=s3)
    print("site:", version)
    extra = [p for p in SITE.rglob("*") if p.is_file() and p.relative_to(SITE).parts[0] not in ("v", "vendor")
             and p.name not in ("index.html", "latest.json")]
    for p in extra:
        key = p.relative_to(SITE).as_posix()
        s3.upload_file(str(p), bucket, key, ExtraArgs={"ContentType": TYPES.get(p.suffix) or stats_site.CONTENT_TYPES.get(p.suffix, "application/octet-stream"),
                                                        "CacheControl": "no-cache" if p.suffix in (".json", ".m3u8") else "public, max-age=86400"})
    print("files:", len(extra))
    s3.upload_file(str(WORK / "live.json"), bucket, "demo/live.json", ExtraArgs={"ContentType": "application/json"})
    if SOURCE.exists():
        s3.upload_file(str(SOURCE), bucket, SOURCE_KEY)
    there = {o["Key"] for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="live/s/")
             for o in page.get("Contents", [])}
    copied = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=STATS_BUCKET, Prefix=DEMO_KEEP + "s/"):
        for o in page.get("Contents", []):
            key = "live/" + o["Key"][len(DEMO_KEEP):]
            if key in there:
                continue
            s3.copy_object(Bucket=bucket, Key=key, CopySource={"Bucket": STATS_BUCKET, "Key": o["Key"]},
                           MetadataDirective="REPLACE", ContentType=TYPES.get(Path(key).suffix, "application/octet-stream"),
                           CacheControl="no-cache" if key.endswith(".m3u8") else "public, max-age=86400")
            copied += 1
    print("streams: copied", copied)
    subprocess.run(["aws", "cloudfront", "create-invalidation", "--distribution-id", out["DistributionId"],
                    "--paths", "/*"], env=aws_env(), check=True, capture_output=True)
    print("up:", out["SiteUrl"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, help="a copy of the season database (the PC's logs/backups/season-*.db.gz, unzipped)")
    ap.add_argument("--no-recap", action="store_true", help="skip the week recap (it calls the closer's model and TTS)")
    ap.add_argument("--upload", action="store_true", help="publish to the shotcast-demo stack")
    ap.add_argument("--stored-db", action="store_true", help="build from the redacted season the last --db build uploaded")
    ap.add_argument("--live", action="store_true", help="only rewrite and upload the live loop and its chat")
    ap.add_argument("--mute", action="store_true", help="mute MUTES in the kept streams, the stats bucket's and the demo's")
    a = ap.parse_args()
    if not IN_CI:
        load_env()
        for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
            os.environ.pop(k, None)
    os.chdir(REPO)
    if a.mute:
        mute()
        return
    if a.live:
        write_live(Overlay())
        upload_live()
        return
    if a.stored_db and a.db:
        ap.error("--db or --stored-db, not both")
    if a.db or a.stored_db:
        build(a.db or stored_db(), not a.no_recap)
    if a.upload:
        upload()


if __name__ == "__main__":
    main()
