import asyncio
import itertools
import json
import sqlite3
from contextlib import closing

from commentator import season_analytics as A
from commentator import stats_site, stats_web
from commentator.store import Store


def _season(tmp_path):
    path = tmp_path / "season.db"
    Store(str(path)).close()
    db = sqlite3.connect(path)
    db.execute("INSERT INTO sessions (id, started_at, date) VALUES (2, 100, '2026-10-17')")
    ts = 0
    for sid, player, club, lie, league in itertools.product((1, 2), ("Ann", "Bob"), ("DR", "7I", "PT"),
                                                            ("tee", "fairway"), (0, 1)):
        ts += 1
        cur = db.execute("INSERT INTO shots (session_id, ts, player, course, club, lie_before, is_putt, attributed,"
                         " mulligan, is_league, hole, shot_number) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?, 1, 1)",
                         (sid, ts, player, "Pebble" if sid == 1 else "Riviera", club, lie, club == "PT", league))
        db.execute("INSERT INTO lines (session_id, shot_id, ts, kind, speaker, text) VALUES (?, ?, ?, 'shot', 'pbp', ?)",
                   (sid, cur.lastrowid, ts, f"{player} with the {club}"))
    db.commit()
    db.close()
    return path


def test_published_shots_and_transcripts_match_the_live_queries(tmp_path):
    path = _season(tmp_path)
    names = {"pbp": "Nigel"}
    version = stats_site.build(path, tmp_path / "site", names)
    vdir = tmp_path / "site" / "v" / version
    assert json.loads((tmp_path / "site" / "latest.json").read_text())["version"] == version
    for name in ("all", "league", "practice"):
        body = json.loads((vdir / f"summary-{name}.json").read_text())
        assert body["context"] == name and body["db"]["name"] == "season.db"
    with closing(stats_web.connect(path)) as src, closing(sqlite3.connect(vdir / "stats.db")) as pub:
        filters = [{}, {"club": "DR"}, {"player": "Bob", "lie": "tee"}, {"date": "2026-10-17", "course": "Riviera"},
                   {"club": "nope"}]
        for context, f in itertools.product((None, "league", "practice"), filters):
            sql, args = stats_site.shot_query(context, f, limit=5)
            got = [json.loads(r[0]) for r in pub.execute(sql, args)]
            assert got == A.shot_log(src, context, limit=5, **f), (context, f)
        everything = A.shot_log(src, None, limit=-1)
        for lo, hi in (("2026-10-17", None), (None, "2026-10-16"), ("2026-10-17", "2026-10-17"), ("2027-01-01", None)):
            sql, args = stats_site.shot_query(None, {"from": lo, "to": hi}, limit=-1)
            want = [s["id"] for s in everything if (not lo or s["date"] >= lo) and (not hi or s["date"] <= hi)]
            assert [json.loads(r[0])["id"] for r in pub.execute(sql, args)] == want, (lo, hi)
        for sid in (1, 2):
            got = json.loads(pub.execute("SELECT json FROM pub_transcript WHERE session_id = ?", (sid,)).fetchone()[0])
            assert got == A.transcript(src, sid, names) and got[0]["name"] == "Nigel"
        assert pub.execute("PRAGMA page_size").fetchone()[0] == stats_site.PAGE_SIZE


def test_rebuilding_keeps_only_the_newest_versions(tmp_path):
    path = _season(tmp_path)
    site = tmp_path / "site"
    versions = [stats_site.build(path, site) for _ in range(3)]
    assert sorted(p.name for p in (site / "v").iterdir()) == sorted(versions[-stats_site.KEEP_VERSIONS:])
    assert (site / "index.html").read_bytes() == stats_site.PAGE.read_bytes()
    assert list((site / "vendor").glob("*/sqlite.worker.js"))


class FakeS3:
    def __init__(self, keys=()):
        self.keys = set(keys)
        self.calls = []

    def get_paginator(self, _):
        keys = self.keys

        class P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for k in sorted(keys) if k.startswith(Prefix)]}
        return P()

    def upload_file(self, path, bucket, key, ExtraArgs):
        self.calls.append(("upload", key, ExtraArgs))
        self.keys.add(key)

    def put_object(self, Bucket, Key, Body, ContentType, CacheControl):
        self.calls.append(("put", Key, {"ContentType": ContentType, "CacheControl": CacheControl}))
        self.keys.add(Key)

    def delete_objects(self, Bucket, Delete):
        gone = [o["Key"] for o in Delete["Objects"]]
        self.calls.append(("delete", gone))
        self.keys -= set(gone)


def test_upload_points_latest_at_the_new_version_last_and_prunes_old_ones(tmp_path):
    path = _season(tmp_path)
    site = tmp_path / "site"
    version = stats_site.build(path, site)
    vendor = sorted(p.relative_to(site).as_posix() for p in (site / "vendor").rglob("*") if p.is_file())
    old = ["stats/v/20200101-000000-aaaaaa/stats.db", "stats/v/20200102-000000-bbbbbb/stats.db"]
    s3 = FakeS3([*old, f"stats/{vendor[0]}"])
    assert stats_site.upload(site, "bkt", "/stats/", client=s3) == version
    uploads = [c for c in s3.calls if c[0] == "upload"]
    assert s3.calls[-2][:2] == ("put", "stats/latest.json")      # last write, then the cleanup
    assert s3.calls[-1] == ("delete", [old[0]])                  # keeps the newest previous version
    assert uploads[-1][1] == "stats/index.html" and uploads[-1][2]["CacheControl"] == "no-cache"
    assert f"stats/{vendor[0]}" not in [c[1] for c in uploads]   # already there
    db = next(c for c in uploads if c[1] == f"stats/v/{version}/stats.db")
    assert db[2] == {"ContentType": "application/vnd.sqlite3", "CacheControl": "public, max-age=31536000, immutable"}
    wasm = next(c for c in uploads if c[1].endswith(".wasm"))
    assert wasm[2]["ContentType"] == "application/wasm"


def _engine(tmp_path, **publish):
    from tests.test_round import _engine as make

    eng = make(tmp_path)
    eng.cfg.source.type = "connector_log"
    for k, v in publish.items():
        setattr(eng.cfg.publish, k, v)
    ran = []

    async def fake():
        ran.append(1)
    eng._publish = fake
    return eng, ran


async def test_publish_after_a_round_only_when_set_up_and_once_for_rounds_close_together(tmp_path):
    eng, ran = _engine(tmp_path, enabled=True, bucket=None, delay_seconds=0.05)
    eng._schedule_publish()
    assert eng._publish_task is None                       # no bucket
    eng.cfg.publish.bucket = "bkt"
    eng.cfg.source.type = "replay"
    eng._schedule_publish()
    assert eng._publish_task is None                       # replays don't publish
    eng.cfg.source.type = "connector_log"
    eng._schedule_publish()
    first = eng._publish_task
    eng._schedule_publish()                                # another round ends before it ran
    await asyncio.sleep(0)
    assert first.cancelled()
    await eng._publish_task
    assert ran == [1]


async def test_publish_goes_out_once_the_sign_off_has_aired_with_the_timer_as_a_backup(tmp_path):
    eng, ran = _engine(tmp_path, enabled=True, bucket="bkt", delay_seconds=60)
    talking = [True]
    eng.speech.is_idle = lambda: not talking[0]
    eng._schedule_publish()                                # round over: the backup timer
    backup = eng._publish_task
    eng._schedule_publish(after_speech=True)               # sign-off queued
    await asyncio.sleep(0.3)
    assert backup.cancelled() and ran == []                # still voicing the sign-off
    talking[0] = False
    await asyncio.wait_for(eng._publish_task, 2)
    assert ran == [1]


def test_shots_are_put_in_the_round_that_ended_next_on_their_course(tmp_path):
    path = tmp_path / "season.db"
    Store(str(path)).close()
    db = sqlite3.connect(path)
    db.execute("INSERT INTO sessions (id, started_at, date) VALUES (2, 50, '2026-10-14')")

    def shot(sid, ts, course):
        return db.execute("INSERT INTO shots (session_id, ts, player, course, is_putt, attributed, mulligan, is_league)"
                          " VALUES (?, ?, 'Ann', ?, 0, 1, 0, 0)", (sid, ts, course)).lastrowid

    def side(sid, ended, course, who):
        return db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes, gross, is_league)"
                          " VALUES (?, ?, '2026-10-14', ?, ?, 9, 40, 0)", (sid, ended, course, who)).lastrowid

    before_restart, after_restart = shot(1, 10, "Pebble"), shot(2, 60, "Pebble")   # the booth restarted mid-round
    ann, bob = side(2, 100, "Pebble", "Ann"), side(2, 100.01, "Pebble", "Bob")       # one round, two sides
    next_round = shot(2, 150, "Riviera")
    shot(2, 400, "Riviera")                                                       # after the last round: none
    riviera = side(2, 200, "Riviera", "Ann")
    db.commit()
    with closing(stats_web.connect(path)) as ro:
        assert stats_web.shot_rounds(ro) == {before_restart: ann, after_restart: ann, next_round: riviera}
        assert {r["id"]: r["round"] for r in stats_web.rounds(ro)} == {ann: ann, bob: ann, riviera: riviera}
        # Bob's side counts as his round even with no tracked shots; Ann's shots put her in both sides' rounds
        assert {r["id"]: r["players"] for r in stats_web.rounds(ro)} == {ann: ["Ann"], bob: ["Ann", "Bob"], riviera: ["Ann"]}
    db.close()
    site = tmp_path / "site"
    vdir = site / "v" / stats_site.build(path, site)
    with closing(sqlite3.connect(vdir / "stats.db")) as pub:
        sql, args = stats_site.shot_query(None, {"round": str(ann)})
        assert sorted(json.loads(r[0])["id"] for r in pub.execute(sql, args)) == [before_restart, after_restart]


def test_each_player_gets_a_file_of_just_their_courses_highlights_and_records(tmp_path):
    path = _season(tmp_path)
    db = sqlite3.connect(path)
    for who, strokes in (("Ann", 4), ("Bob", 5)):
        db.execute("INSERT INTO player_rounds (session_id, ended_at, player, course, scores, hole_pars, is_league)"
                   " VALUES (1, 50, ?, 'Pebble', ?, ?, 0)",
                   (who, json.dumps({h: strokes for h in range(1, 10)}), json.dumps({h: 4 for h in range(1, 10)})))
    db.execute("UPDATE shots SET total_yards = 200 + ts WHERE club = 'DR'")
    db.commit()
    db.close()
    site = tmp_path / "site"
    vdir = site / "v" / stats_site.build(path, site)
    body = json.loads((vdir / "summary-all.json").read_text())
    files = {p["name"]: p["file"] for p in body["players"]}
    assert set(files) == {"Ann", "Bob"} and files["Ann"] == stats_site.player_key("Ann") != files["Bob"]
    ann = json.loads((vdir / "player" / "all" / f"{files['Ann']}.json").read_text())
    with closing(stats_web.connect(path)) as src:
        assert ann == stats_site.player_file(src, "Ann", None, A.holes_played(src, None))
        everyone = stats_web.records(src, None)
    assert ann["name"] == "Ann" and [c["players"] for c in ann["courses"]] == [["Ann"]]
    assert {h["avg"] for h in ann["courses"][0]["holes"]} == {4}
    assert all(x["player"] == "Ann" for x in ann["highlights"]["drives"]) and ann["highlights"]["drives"]
    assert ann["records"]["longest_drive"]["player"] == "Ann"
    assert everyone["longest_drive"]["player"] == "Bob"   # the last shots are Bob's: the longest drives
    assert (vdir / "player" / "league").is_dir() and (vdir / "player" / "practice").is_dir()
