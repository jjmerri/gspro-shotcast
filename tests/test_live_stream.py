import asyncio
import json

from commentator import live_stream
from commentator.live_stream import Mirror, parse_playlist


class FakeS3:
    def __init__(self):
        self.objects, self.deleted = {}, []

    def put_object(self, Bucket, Key, Body, CacheControl, ContentType):
        self.objects[Key] = (Body, CacheControl, ContentType)

    def delete_objects(self, Bucket, Delete):
        for o in Delete["Objects"]:
            self.deleted.append(o["Key"])
            self.objects.pop(o["Key"], None)

    def get_paginator(self, _):
        objects = self.objects

        class P:
            def paginate(self, Bucket, Prefix):
                return [{"Contents": [{"Key": k} for k in objects if k.startswith(Prefix)]}]
        return P()


def _playlist(folder, seq, names, ended=False, mtime=None):
    body = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:6", f"#EXT-X-MEDIA-SEQUENCE:{seq}"]
    for n in names:
        if not (folder / n).exists():
            (folder / n).write_bytes(n.encode())
        body += ["#EXTINF:6.0,", n]
    body += ["#EXT-X-ENDLIST"] if ended else []
    pl = folder / "stream.m3u8"
    pl.write_text("\n".join(body) + "\n")
    if mtime is not None:
        import os
        os.utime(pl, (mtime, mtime))


def test_parse_playlist_waits_for_a_whole_one():
    assert parse_playlist("#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:3\n#EXTINF:6,\na3.ts\n#EXTINF:6,\na4.ts\n") == (3, ["a3.ts", "a4.ts"], False)
    assert parse_playlist("#EXTM3U\n#EXTINF:6,\na0.ts\n#EXT-X-ENDLIST\n") == (0, ["a0.ts"], True)
    assert parse_playlist("#EXTM3U\n#EXTINF:6,\na0.ts\n#EXTINF:6,\n") is None   # cut off mid-write
    assert parse_playlist("") is None
    assert parse_playlist("#EXTM3U\n#EXTINF:6,\n../secret.ts\n") is None       # only files beside it


def test_mirror_uploads_the_stream_as_it_goes_and_leaves_nothing_behind(tmp_path):
    s3, now = FakeS3(), [1000.0]
    s3.objects["live/s/old/stream3.ts"] = (b"", "", "")   # a crashed stream's leftovers
    status = tmp_path / "live.json"
    m = Mirror(tmp_path / "hls", "bkt", client=s3, status_file=status, clock=lambda: now[0])
    m.folder.mkdir()
    m.clear()
    assert "live/s/old/stream3.ts" not in s3.objects
    assert json.loads(s3.objects["live/status.json"][0]) == {"live": False}

    _playlist(m.folder, 0, ["stream0.ts", "stream1.ts"], mtime=now[0])
    m.step()
    sid = m.session
    on = json.loads(s3.objects["live/status.json"][0])
    assert on["live"] and on["session"] == sid and json.loads(status.read_text())["live"]
    assert s3.objects[f"live/s/{sid}/stream0.ts"][1:] == ("public, max-age=86400, immutable", "video/mp2t")
    assert s3.objects[f"live/s/{sid}/stream.m3u8"][1:] == ("max-age=1", "application/vnd.apple.mpegurl")

    now[0] += 6
    _playlist(m.folder, 1, ["stream1.ts", "stream2.ts"], mtime=now[0])
    m.step()
    assert f"live/s/{sid}/stream0.ts" not in s3.objects            # rolled off the playlist: gone
    assert f"live/s/{sid}/stream2.ts" in s3.objects

    now[0] += 6
    _playlist(m.folder, 1, ["stream1.ts", "stream2.ts"], ended=True, mtime=now[0])
    m.step()                                                         # OBS stopped
    assert json.loads(s3.objects["live/status.json"][0]) == {"live": False}
    assert not [k for k in s3.objects if k.startswith("live/s/")]
    assert not list(m.folder.iterdir())                              # and the PC's copy too


def test_mirror_publishes_a_renamed_playlist_where_the_page_looks(tmp_path):
    s3, now = FakeS3(), [1000.0]
    m = Mirror(tmp_path, "bkt", playlist="obs.m3u8", client=s3, clock=lambda: now[0])
    _playlist(tmp_path, 0, ["obs0.ts"], mtime=now[0])
    (tmp_path / "stream.m3u8").rename(tmp_path / "obs.m3u8")
    m.step()
    assert f"live/s/{m.session}/stream.m3u8" in s3.objects


def test_mirror_goes_off_air_when_obs_stops_writing_and_ignores_an_old_playlist(tmp_path):
    s3, now = FakeS3(), [1000.0]
    m = Mirror(tmp_path, "bkt", prefix="stats", client=s3, clock=lambda: now[0], stale_seconds=20)
    _playlist(tmp_path, 4, ["stream4.ts"], mtime=now[0] - 300)   # left from a stream days ago
    m.step()
    assert m.session is None and not s3.objects

    _playlist(tmp_path, 0, ["stream0.ts"], mtime=now[0])
    m.step()
    assert m.session and "stats/live/status.json" in s3.objects
    now[0] += 1                                                     # its last playlist names a segment
    _playlist(tmp_path, 0, ["stream0.ts", "stream1.ts"], mtime=now[0])   # that never got written:
    (tmp_path / "stream1.ts").unlink()
    m.step()
    assert m.session
    now[0] += 30                                                    # OBS crashed, no ENDLIST
    m.step()
    assert m.session is None and json.loads(s3.objects["stats/live/status.json"][0]) == {"live": False}


def test_mirror_starts_a_new_session_when_obs_restarts_between_passes(tmp_path):
    s3, now = FakeS3(), [1000.0]
    m = Mirror(tmp_path, "bkt", client=s3, clock=lambda: now[0])
    _playlist(tmp_path, 7, ["stream7.ts"], mtime=now[0])
    m.step()
    first = m.session
    now[0] += 1
    _playlist(tmp_path, 0, ["stream0.ts"], mtime=now[0])
    m.step()
    assert m.session != first and f"live/s/{first}/stream7.ts" not in s3.objects
    assert json.loads(s3.objects["live/status.json"][0])["session"] == m.session

    second = m.session                                     # and again, from 0 to 0: the files are new
    now[0] += 1
    import os
    (tmp_path / "stream0.ts").write_bytes(b"new video")
    os.utime(tmp_path / "stream0.ts", ns=(10**18, 10**18))
    _playlist(tmp_path, 0, ["stream0.ts"], mtime=now[0])
    m.step()
    assert m.session != second and s3.objects[f"live/s/{m.session}/stream0.ts"][0] == b"new video"


async def test_the_stream_starts_with_a_round_and_stops_after_the_sign_off(tmp_path, monkeypatch):
    from tests.test_round import _engine as make

    calls = []

    class FakeObs:
        def __init__(self, *a):
            pass

        def start(self):
            calls.append("start")
            return True

        def stop(self):
            calls.append("stop")
            return True
    monkeypatch.setattr(live_stream, "Obs", FakeObs)
    eng = make(tmp_path)
    eng.cfg.source.type = "connector_log"
    eng.cfg.publish.bucket = "bkt"
    eng.cfg.live.stop_after_seconds = 0.05
    eng._live_round_start()
    await asyncio.sleep(0.05)
    assert calls == []                                    # not set up
    eng.cfg.live.enabled = True
    eng._live_round_start()
    await asyncio.sleep(0.05)
    assert calls == ["start"]
    eng._schedule_live_stop()
    eng._live_round_start()                               # the next round starts before it stops
    await asyncio.sleep(0.2)
    assert calls == ["start", "start"]
    eng._schedule_live_stop()
    await asyncio.wait_for(eng._live_stop_task, 2)
    assert calls == ["start", "start", "stop"]
