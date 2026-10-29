import json
import sqlite3

from commentator import record_clips

ROOT = "live/"


class FakeS3:
    class exceptions:
        class NoSuchKey(Exception):
            pass

    def __init__(self, objects=None):
        self.objects = dict(objects or {})

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise self.exceptions.NoSuchKey(Key)

        class Body:
            def read(_, data=self.objects[Key]):
                return data
        return {"Body": Body()}

    def put_object(self, Bucket, Key, Body, CacheControl, ContentType):
        self.objects[Key] = Body

    def copy_object(self, Bucket, Key, CopySource, **_):
        self.objects[Key] = self.objects[CopySource["Key"]]

    def delete_objects(self, Bucket, Delete):
        for o in Delete["Objects"]:
            self.objects.pop(o["Key"], None)

    def get_paginator(self, _):
        objects = self.objects

        class P:
            def paginate(self, Bucket, Prefix):
                return [{"Contents": [{"Key": k} for k in list(objects) if k.startswith(Prefix)]}]
        return P()


def _stream(n, ended=True, since=1000):
    """A kept stream of n 4-second segments, from `since`."""
    names = [f"stream{i}.ts" for i in range(n)]
    pl = "\n".join(["#EXTM3U"] + [f"#EXTINF:4.000,\n{x}" for x in names] + (["#EXT-X-ENDLIST"] if ended else []))
    objs = {f"{ROOT}s/s1/{x}": x.encode() for x in names}
    objs[f"{ROOT}s/s1/full.m3u8"] = pl.encode()
    objs[f"{ROOT}recordings.json"] = json.dumps({"recordings": [
        {"session": "s1", "since": since, "ended": since + 4 * n if ended else None, "duration": 4 * n}]}).encode()
    return objs


def _index(s3):
    return json.loads(s3.objects[ROOT + "clips.json"])["clips"]


def test_a_record_shot_is_copied_out_of_its_stream_and_dropped_once_beaten():
    s3 = FakeS3(_stream(30))
    shot = {"ts": 1050, "player": "Ann", "course": "Pebble", "hole": "Hole 7, par 3, 106 yards"}
    assert record_clips.sync(s3, "b", "", {7: shot, 8: {"ts": 5000, "player": "Bob"}}) == {"kept": 1, "added": 1, "removed": 0}
    # 50 s in: the segments from 44 s (3 before, 47 s) to 72 s (20 after, 70 s), and the shot a second before 50 s.
    assert sorted(k.rsplit("/", 1)[1] for k in s3.objects if k.startswith(ROOT + "clips/7/")) == \
        ["clip.m3u8"] + sorted(f"stream{i}.ts" for i in range(11, 18))
    assert s3.objects[ROOT + "clips/7/stream11.ts"] == b"stream11.ts"
    assert _index(s3) == {"7": {"at": 5.0, "duration": 28.0, "player": "Ann", "course": "Pebble",
                                "hole": "Hole 7, par 3, 106 yards"}}
    # Its stream goes after a week: the clip stays, as long as the shot holds a record.
    for k in [k for k in s3.objects if k.startswith(ROOT + "s/")]:
        del s3.objects[k]
    assert record_clips.sync(s3, "b", "", {7: shot})["kept"] == 1
    assert record_clips.sync(s3, "b", "", {9: {"ts": 1100}}) == {"kept": 0, "added": 0, "removed": 1}
    assert _index(s3) == {} and not [k for k in s3.objects if k.startswith(ROOT + "clips/")]


def test_a_shot_near_the_end_of_a_stream_still_uploading_waits_for_the_rest():
    s3 = FakeS3(_stream(15, ended=False))   # 60 s so far
    shot = {"ts": 1050, "player": "Ann"}
    assert record_clips.sync(s3, "b", "", {7: shot})["added"] == 0
    s3.objects.update(_stream(30, ended=False))
    assert record_clips.sync(s3, "b", "", {7: shot})["added"] == 1


def test_the_build_finds_the_seasons_records_and_each_players(tmp_path):
    from contextlib import closing

    from test_stats_site import _season
    from commentator import stats_site, stats_web

    path, found = _season(tmp_path), {}
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE shots SET total_yards = 200 + ts WHERE club = 'DR'")   # Bob's are the longest
    stats_site.build(path, tmp_path / "site", records=found)
    with closing(stats_web.connect(path)) as src:
        want = {s["id"] for who in (None, "Ann", "Bob") for s in stats_web.record_shots(stats_web.records(src, None, who))}
    assert len(want) == 2 and want <= set(found) and all(found[i]["ts"] for i in want)
