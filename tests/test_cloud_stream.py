"""The live stream to Shotcast Cloud (cloud_stream.CloudStream) against a fake of the API's stream routes."""
import hashlib
import json

import pytest

from commentator import cloud, cloud_booth
from commentator.cloud_stream import GIVE_UP_S, OFF_S, CloudStream
from tests.test_live_stream import _playlist


class Answer:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._body


class FakeStreams:
    """The API's stream routes: what was asked, what's up, and refusals to give."""

    def __init__(self):
        self.calls, self.streams, self.uploaded, self.stored, self.lost, self.ended = [], {}, {}, [], [], []
        self.refuse: dict[str, list] = {}    # path suffix -> [(status, body)] given before answering
        self.unreachable = 0                 # requests that get no answer at all
        self.put_fails = 0

    def request(self, method, path, body=None, **kw):
        self.calls.append((path, json.loads(json.dumps(body))))
        if self.unreachable:
            self.unreachable -= 1
            raise cloud.Unreachable("couldn't reach Shotcast Cloud: down")
        for suffix, queue in self.refuse.items():
            if path.endswith(suffix) and queue:
                return Answer(*queue.pop(0))
        if path == "/api/streams":
            sid = self.streams.setdefault(body["session"], f"st-{len(self.streams)}")
            return Answer(200, {"id": sid})
        if path.endswith("/segments"):
            return Answer(200, {"links": [{"seq": s["seq"], "url": f"https://bucket.test/{s['seq']}",
                                           "headers": {"x-amz-checksum-sha256": s["sha256"]}, "expires_in": 300}
                                          for s in body["segments"]], "expires_in": 300})
        if path.endswith("/stored"):
            self.stored += body["seqs"]
            self.lost += body["lost"]
            return Answer(200, {"id": path.split("/")[3]})
        if path.endswith("/end"):
            self.ended.append(path.split("/")[3])
            return Answer(200, {"id": path.split("/")[3]})
        raise AssertionError(path)

    def put(self, url, headers, data):
        if self.put_fails:
            self.put_fails -= 1
            return 503
        assert headers["x-amz-checksum-sha256"] == hashlib.sha256(data).hexdigest()
        self.uploaded[int(url.rsplit("/", 1)[1])] = data
        return 200


def _sink(tmp_path, api, now, **kw):
    return CloudStream(tmp_path, lambda: api, clock=lambda: now[0], put=api.put, **kw)


def _asks(api, suffix):
    return [b for p, b in api.calls if p.endswith(suffix)]


def test_finished_segments_go_up_in_order_with_their_seconds_and_the_stream_ends_with_obs(tmp_path):
    api, now = FakeStreams(), [1000.0]
    s = _sink(tmp_path, api, now)
    _playlist(tmp_path, 5, ["stream5.ts", "stream6.ts"], mtime=now[0])
    s.step()
    assert api.calls[0] == ("/api/streams", {"session": s.session})
    assert api.uploaded == {5: b"stream5.ts", 6: b"stream6.ts"}   # (OBS lists a segment once it's whole)
    [ask] = _asks(api, "/segments")
    assert ask["segments"][0] == {"seq": 5, "size": 10, "sha256": hashlib.sha256(b"stream5.ts").hexdigest(),
                                  "seconds": 6.0}
    now[0] += 1
    _playlist(tmp_path, 6, ["stream6.ts", "stream7.ts"], mtime=now[0])   # 5 has left the playlist
    s.step()
    assert set(api.uploaded) == {5, 6, 7} and api.stored == [5, 6, 7]
    now[0] += 1
    _playlist(tmp_path, 6, ["stream6.ts", "stream7.ts"], ended=True, mtime=now[0])
    s.step()
    assert api.stored == [5, 6, 7] and api.ended == ["st-0"] and s.session is None
    assert not list(tmp_path.glob("*.ts")) and not (tmp_path / "stream.m3u8").exists()   # OBS's folder emptied


def test_a_backlog_goes_a_few_at_a_time(tmp_path):
    api, now = FakeStreams(), [1000.0]
    s = _sink(tmp_path, api, now)
    _playlist(tmp_path, 0, [f"s{i}.ts" for i in range(20)], mtime=now[0])
    s.step()
    assert [len(a["segments"]) for a in _asks(api, "/segments")] == [8]
    s.step()   # (the playlist unchanged: the queue goes on)
    s.step()
    assert [len(a["segments"]) for a in _asks(api, "/segments")] == [8, 8, 4] and api.stored == list(range(20))


def test_a_segment_that_cant_go_up_is_given_up_as_lost_not_held_up_behind(tmp_path):
    api, now = FakeStreams(), [1000.0]
    s = _sink(tmp_path, api, now)
    _playlist(tmp_path, 0, ["a0.ts", "a1.ts", "a2.ts", "a3.ts"], mtime=now[0])
    (tmp_path / "a1.ts").write_bytes(b"x" * (4 * 1024 * 1024 + 1))   # too big for the cloud
    (tmp_path / "a2.ts").unlink()                                     # can't be read when it's sent
    (tmp_path / "a2.ts").mkdir()
    api.put_fails = 1                                                 # a0's upload fails once
    s.step()
    assert api.lost == [1, 2] and api.stored == [3]
    s.step()                     # (storage failed: a wait before asking again)
    assert api.stored == [3] and len(_asks(api, "/segments")) == 1
    now[0] += 3
    s.step()
    assert api.stored == [3, 0]
    # A segment the network keeps from going: given up once it's waited GIVE_UP_S.
    now[0] += 1
    _playlist(tmp_path, 3, ["a3.ts", "a4.ts"], mtime=now[0])
    api.unreachable = 100
    s.step()
    now[0] += GIVE_UP_S + 1
    api.unreachable = 0
    s.step()
    assert 4 in api.lost and 4 not in api.uploaded


def test_seconds_come_from_each_segments_extinf(tmp_path):
    api, now = FakeStreams(), [1000.0]
    s = _sink(tmp_path, api, now)
    for n in ("b0.ts", "b1.ts", "b2.ts"):
        (tmp_path / n).write_bytes(n.encode())
    (tmp_path / "stream.m3u8").write_text("#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:4.004,\nb0.ts\n"
                                          "#EXTINF:3.5,\nb1.ts\n#EXTINF:4,\nb2.ts\n")
    s.step()
    assert [x["seconds"] for x in _asks(api, "/segments")[0]["segments"]] == [4.004, 3.5, 4.0]


def test_segments_the_cloud_asks_again_are_said_again(tmp_path):
    api, now = FakeStreams(), [1000.0]
    s = _sink(tmp_path, api, now)
    api.refuse["/stored"] = [(409, {"error": "not_stored", "message": "not there yet", "action": "retry",
                                    "seqs": [1]})]
    _playlist(tmp_path, 0, ["c0.ts", "c1.ts", "c2.ts"], mtime=now[0])
    s.step()
    assert set(s.stored) == {1} and s.done == {0, 2}
    s.step()
    assert api.stored == [1] and s.stored == {}


def test_a_used_up_allowance_stops_the_stream_and_tells_the_tray(tmp_path):
    api, now, said = FakeStreams(), [1000.0], []
    s = _sink(tmp_path, api, now, issues=said.extend)
    api.refuse["/segments"] = [(403, {"error": "allowance_used", "message": "used up", "action": "fix"})]
    _playlist(tmp_path, 0, ["d0.ts", "d1.ts"], mtime=now[0])
    s.step()
    assert s.halted and said and "used up" in said[0][1]
    now[0] += 1
    _playlist(tmp_path, 0, ["d0.ts", "d1.ts", "d2.ts"], mtime=now[0])
    s.step()
    assert len(_asks(api, "/segments")) == 1 and not api.uploaded   # nothing more asked
    now[0] += 30
    s.step()   # OBS stopped: the stream still ends on the cloud
    assert api.ended == ["st-0"]


def test_the_tray_hears_of_it_with_how_uploads_stand(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(cloud_booth, "set_health", lambda d, issues: seen.append(issues))
    cloud_booth.set_stream_issues([("warn", "streaming used up")])
    try:
        u = cloud_booth.Uploads.__new__(cloud_booth.Uploads)
        u.cfg = type("C", (), {"log_dir": str(tmp_path)})()
        monkeypatch.setattr(u, "_behind", lambda: [], raising=False)
        u._health()
        assert seen == [[("warn", "streaming used up")]]
    finally:
        cloud_booth.set_stream_issues([])


@pytest.mark.parametrize("error", ["not_in_plan", "league_suspended"])
def test_an_account_without_streaming_is_left_alone_and_asked_again_later(tmp_path, error, caplog):
    caplog.set_level("INFO", logger="commentator.cloud_stream")
    api, now = FakeStreams(), [1000.0]
    s = _sink(tmp_path, api, now)
    api.refuse["/api/streams"] = [(403, {"error": error, "message": "no", "action": "fix"})] * 2
    _playlist(tmp_path, 0, ["e0.ts", "e1.ts"], mtime=now[0])
    s.step()
    now[0] += 1
    _playlist(tmp_path, 0, ["e0.ts", "e1.ts", "e2.ts"], mtime=now[0])
    s.step()
    assert [p for p, _ in api.calls] == ["/api/streams"] and not api.uploaded   # once, then quiet
    assert sum("isn't taking" in r.message for r in caplog.records) == 1
    now[0] += 30
    s.step()   # off air: nothing to end on the cloud
    assert s.session is None and api.ended == []
    now[0] += OFF_S
    _playlist(tmp_path, 0, ["f0.ts", "f1.ts"], mtime=now[0])
    s.step()   # the next stream, after OFF_S: asked again (and refused again)
    s.step()
    assert [p for p, _ in api.calls].count("/api/streams") == 2


def test_a_start_with_no_answer_is_asked_again_with_the_same_session(tmp_path):
    api, now = FakeStreams(), [1000.0]
    s = _sink(tmp_path, api, now)
    api.unreachable = 1
    _playlist(tmp_path, 0, ["g0.ts", "g1.ts"], mtime=now[0])
    s.step()
    assert s.stream_id is None and s.session
    s.step()   # (straight after: it waits first)
    assert [p for p, _ in api.calls] == ["/api/streams"]
    now[0] += 3
    s.step()
    assert [b for p, b in api.calls if p == "/api/streams"] == [{"session": s.session}] * 2
    assert api.stored == [0, 1] and len(api.streams) == 1


def test_an_end_with_no_answer_is_asked_again(tmp_path):
    api, now = FakeStreams(), [1000.0]
    s = _sink(tmp_path, api, now)
    _playlist(tmp_path, 0, ["h0.ts", "h1.ts"], mtime=now[0])
    s.step()
    api.refuse["/end"] = [(502, {"message": "bad gateway"})]
    now[0] += 30
    s.step()
    assert s.session is None and api.ended == [] and s.ending
    s.step()   # (a moment later: it waits before asking again)
    assert api.ended == []
    now[0] += 3
    s.step()
    assert api.ended == ["st-0"] and not s.ending


def test_obs_restarting_between_passes_is_another_stream(tmp_path):
    api, now = FakeStreams(), [1000.0]
    s = _sink(tmp_path, api, now)
    _playlist(tmp_path, 7, ["stream7.ts", "stream8.ts"], mtime=now[0])
    s.step()
    first = s.session
    now[0] += 1
    _playlist(tmp_path, 0, ["stream0.ts", "stream1.ts"], mtime=now[0])
    s.step()
    assert s.session != first and api.ended == ["st-0"] and set(api.streams) == {first, s.session}
    assert [p for p, b in api.calls if p.startswith("/api/streams/st-1/segments")]


def test_the_booths_calls_on_every_copy_are_taken(tmp_path):
    s = _sink(tmp_path, FakeStreams(), [1000.0])
    s.mark("Pebble"), s.shot(), s.league(None), s.week(None)
    assert s.watched_at("x", "y", 1.0) is None and s.on_page() is False


async def test_a_paired_booth_streams_to_shotcast_cloud_and_an_unpaired_one_as_before(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tests.test_round import _engine as make

    started = []
    monkeypatch.setattr(CloudStream, "start", lambda self, *a: started.append(self))
    monkeypatch.setattr("commentator.live_stream.launch_obs", lambda *a: False)
    eng = make(tmp_path)
    eng.cfg.source.type = "connector_log"
    eng.cfg.live.enabled, eng.cfg.live.launch_obs, eng.cfg.live.local_keep_days = True, False, 0
    eng.cfg.publish.bucket = None
    assert not eng._live_on()           # unpaired, with no bucket and nothing kept here: no stream, as before
    eng._start_live()
    assert eng.live_cloud is None and started == []

    prof = SimpleNamespace(problem=None, rec={"api": "https://api.test", "tenant_id": "t", "device_id": "d"},
                           bound=("https://api.test",))
    monkeypatch.setattr(cloud, "startup", prof)
    assert eng._live_on()
    eng._start_live()
    assert started == [eng.live_cloud] and eng.live_cloud in eng._live_mirrors()
    assert eng.live_cloud.folder == tmp_path.__class__(eng.cfg.live.dir)

    monkeypatch.setattr(cloud, "startup", SimpleNamespace(problem="refused", rec={}, bound=()))
    assert not eng._live_on()           # a pairing the cloud no longer accepts: nothing goes


def test_the_paired_booth_keeps_the_profiles_live_stream_for_the_cloud_and_nothing_on_the_pc(tmp_path):
    from tests.test_cloud import FakeAPI, base_cfg, mock_http, paired

    api = FakeAPI()
    import pytest as _p
    mp = _p.MonkeyPatch()
    try:
        mock_http(mp, api)
        prof = paired(api)
        cfg = base_cfg(tmp_path)
        cfg.live.enabled, cfg.live.local_keep_days, cfg.live.keep_days, cfg.live.obs_port = True, 9, 7, 4460
        booth = cloud.booth_config(cfg, prof)
        assert booth.live.enabled and booth.live.obs_port == 4460
        assert booth.live.local_keep_days == 0 and booth.live.keep_days == 0 and booth.publish.bucket is None
        cfg.live.enabled = False
        booth = cloud.booth_config(cfg, prof)
        assert not booth.live.enabled and not booth.live.with_rounds and not booth.live.launch_obs
    finally:
        mp.undo()


def test_a_put_whose_answer_was_lost_is_up_and_confirmed_not_given_up(tmp_path):
    api, now = FakeStreams(), [1000.0]
    s = _sink(tmp_path, api, now)
    put = api.put
    def committed_then_dropped(url, headers, data):   # the bytes went up; the 2xx never came back
        put(url, headers, data)
        raise OSError("connection reset")
    s.put = committed_then_dropped
    _playlist(tmp_path, 0, ["k0.ts"], mtime=now[0])
    s.step()
    assert api.uploaded and not api.stored
    s.put = lambda url, headers, data: 412   # asked again: the link's If-None-Match finds it there
    now[0] += 3
    s.step()
    assert api.stored == [0] and api.lost == []


def test_a_segment_the_cloud_keeps_saying_isnt_there_is_given_up(tmp_path):
    api, now = FakeStreams(), [1000.0]
    s = _sink(tmp_path, api, now)
    api.refuse["/stored"] = [(409, {"error": "not_stored", "message": "no", "action": "retry", "seqs": [0]})] * 3
    _playlist(tmp_path, 0, ["m0.ts"], mtime=now[0])
    s.step()
    s.step()
    assert set(s.stored) == {0} and not s.lost
    now[0] += GIVE_UP_S + 1
    _playlist(tmp_path, 0, ["m0.ts"], mtime=now[0])   # (OBS still on)
    s.step()                     # still not there, past GIVE_UP_S: given up
    assert s.stored == {} and s.lost == {0}
    s.step()
    assert api.lost == [0]


def test_whats_up_but_unconfirmed_is_confirmed_before_the_stream_ends(tmp_path):
    api, now = FakeStreams(), [1000.0]
    s = _sink(tmp_path, api, now)
    api.refuse["/stored"] = [(503, {"message": "busy"})] * 3
    _playlist(tmp_path, 0, ["n0.ts", "n1.ts"], ended=True, mtime=now[0])
    s.step()   # up, but /stored unanswered (in the pass and at the end)
    assert api.ended == [] and s.ending and s.ending[0]["stored"]
    for _ in range(2):           # still 503 when it's asked again after the stream ended: kept, not dropped
        now[0] += 5
        s.step()
        assert api.ended == [] and s.ending[0]["stored"]
    now[0] += 10
    s.step()
    assert api.stored == [0, 1] and api.ended == ["st-0"] and not s.ending
    assert [p.rsplit("/", 1)[1] for p, _ in api.calls][-2:] == ["stored", "end"]


def test_refused_mid_stream_it_ends_on_the_cloud_and_is_asked_again_later_with_obs_still_on(tmp_path):
    api, now = FakeStreams(), [1000.0]
    s = _sink(tmp_path, api, now)
    _playlist(tmp_path, 0, ["p0.ts"], mtime=now[0])
    s.step()
    first = s.session
    api.refuse["/segments"] = [(403, {"error": "league_suspended", "message": "no", "action": "fix"})]
    now[0] += 1
    _playlist(tmp_path, 0, ["p0.ts", "p1.ts"], mtime=now[0])
    s.step()
    assert s.halted == "refused" and api.ended == ["st-0"] and not s.queued
    for i in range(2, 5):        # OBS goes on: nothing queued or asked for meanwhile
        now[0] += 4
        _playlist(tmp_path, 0, [f"p{j}.ts" for j in range(i + 1)], mtime=now[0])
        s.step()
        assert not s.queued
    assert len(_asks(api, "/segments")) == 2
    now[0] = 1001 + OFF_S + 1
    _playlist(tmp_path, 0, [f"p{j}.ts" for j in range(6)], mtime=now[0])
    s.step()                     # asked again: a fresh stream, from the newest segment on
    assert s.session != first and s.stream_id == "st-1" and api.uploaded.keys() >= {5}
    assert 2 not in api.uploaded


def test_a_resumed_start_on_a_stream_the_cloud_ended_starts_a_fresh_one(tmp_path):
    api, now = FakeStreams(), [1000.0]
    s = _sink(tmp_path, api, now)
    api.unreachable = 1          # the start's answer lost (the cloud made it)
    _playlist(tmp_path, 0, ["s0.ts"], mtime=now[0])
    s.step()
    first = s.session
    api.refuse["/api/streams"] = [(409, {"error": "stream_ended", "message": "ended", "action": "fix"})]
    now[0] += 3
    _playlist(tmp_path, 0, ["s0.ts", "s1.ts"], mtime=now[0])
    s.step()                     # resumed: the cloud ended it meanwhile
    assert s.session != first and s.stream_id is None
    now[0] += 1
    _playlist(tmp_path, 0, ["s0.ts", "s1.ts"], mtime=now[0])
    s.step()
    assert s.stream_id is not None and [b["session"] for p, b in api.calls if p == "/api/streams"][-1] == s.session
    assert api.uploaded


def test_uploaded_before_a_refusal_is_still_confirmed_before_the_end(tmp_path):
    api, now = FakeStreams(), [1000.0]
    s = _sink(tmp_path, api, now)
    api.refuse["/stored"] = [(503, {"message": "busy"})]
    _playlist(tmp_path, 0, ["t0.ts"], mtime=now[0])
    s.step()
    assert set(s.stored) == {0}
    api.refuse["/segments"] = [(403, {"error": "allowance_used", "message": "used up", "action": "fix"})]
    now[0] += 3
    _playlist(tmp_path, 0, ["t0.ts", "t1.ts"], mtime=now[0])
    s.step()
    assert api.stored == [0] and api.ended == ["st-0"]
    assert [p.rsplit("/", 1)[1] for p, _ in api.calls][-2:] == ["stored", "end"]


def test_a_segment_obs_rewrote_under_the_same_name_isnt_sent_as_this_streams(tmp_path):
    import os
    api, now = FakeStreams(), [1000.0]
    s = _sink(tmp_path, api, now)
    api.unreachable = 1
    _playlist(tmp_path, 0, ["o0.ts", "o1.ts"], mtime=now[0])
    s.step()                     # queued, the start unanswered
    (tmp_path / "o1.ts").write_bytes(b"the next stream's")
    st = (tmp_path / "o1.ts").stat()
    os.utime(tmp_path / "o1.ts", ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    now[0] += 3
    s.step()
    assert api.uploaded == {0: b"o0.ts"} and api.lost == [1]


def test_live_json_follows_obs_not_the_cloud(tmp_path):
    api, now = FakeStreams(), [1000.0]
    status = tmp_path / "logs" / "live.json"
    status.parent.mkdir()
    (tmp_path / "obs").mkdir()
    status.write_text(json.dumps({"live": True, "session": "old"}))   # a booth killed on air
    s = _sink(tmp_path / "obs", api, now, status_file=status)
    s._stop.set()                # (the thread starts and stops at once)
    s.start()
    s._thread.join(5)
    assert json.loads(status.read_text()) == {"live": False}
    # Refused: on air all the same while OBS's stream is, and off when it stops.
    api.refuse["/api/streams"] = [(403, {"error": "not_in_plan", "message": "no", "action": "fix"})] * 3
    _playlist(tmp_path / "obs", 0, ["l0.ts"], mtime=now[0])
    s.step()
    assert json.loads(status.read_text())["live"] is True
    now[0] += 30
    s.step()
    assert json.loads(status.read_text()) == {"live": False}
    now[0] += 1
    _playlist(tmp_path / "obs", 0, ["l1.ts"], mtime=now[0])   # another, inside the wait: on air too
    s.step()
    assert json.loads(status.read_text())["live"] is True and [p for p, _ in api.calls] == ["/api/streams"]


@pytest.mark.parametrize("err", [cloud.TooOld("9.9"), cloud.NotPaired("unpaired")])
def test_the_thread_stopping_on_the_pairing_or_an_old_app_leaves_live_json_off(tmp_path, err):
    api, now = FakeStreams(), [1000.0]
    status = tmp_path / "live.json"
    def gone(*a, **k):
        raise err
    api.request = gone
    (tmp_path / "obs").mkdir()
    s = _sink(tmp_path / "obs", api, now, status_file=status)
    _playlist(tmp_path / "obs", 0, ["j0.ts"], mtime=now[0])
    s.run(interval=0.01)         # returns once the client raises
    assert json.loads(status.read_text()) == {"live": False}


def test_losing_the_pairing_or_an_old_app_stops_it(tmp_path):
    for err in (cloud.NotPaired("unpaired"), cloud.TooOld("9.9")):
        api, now = FakeStreams(), [1000.0]
        def gone(*a, **k):
            raise err
        api.request = gone
        s = _sink(tmp_path, api, now)
        _playlist(tmp_path, 0, ["q0.ts"], mtime=now[0])
        with pytest.raises(type(err)):
            s.step()


def test_no_answer_waits_longer_each_time_and_says_so_once(tmp_path, caplog):
    caplog.set_level("INFO", logger="commentator.cloud_stream")
    api, now = FakeStreams(), [1000.0]
    s = _sink(tmp_path, api, now)
    api.unreachable = 1000
    _playlist(tmp_path, 0, ["r0.ts"], mtime=now[0])
    for _ in range(200):         # 200 seconds of passes, the network down
        s.step()
        now[0] += 1
        _playlist(tmp_path, 0, ["r0.ts"], mtime=now[0])
    asked = len(api.calls)
    assert asked < 12            # 2, 4, 8, ... 60 s apart, not every second
    assert sum("trying again" in r.message for r in caplog.records if r.levelname == "INFO") == 1


def test_the_used_up_warning_goes_once_a_stream_starts_again(tmp_path):
    api, now, said = FakeStreams(), [1000.0], []
    s = _sink(tmp_path, api, now, issues=said.append)
    api.refuse["/segments"] = [(403, {"error": "allowance_used", "message": "used up", "action": "fix"})]
    _playlist(tmp_path, 0, ["u0.ts"], mtime=now[0])
    s.step()
    assert said[-1] and said[-1][0][0] == "warn" and api.ended == ["st-0"]   # (ended on the cloud too)
    now[0] += 30
    s.step()                     # OBS stopped
    now[0] += 1
    _playlist(tmp_path, 0, ["v0.ts"], mtime=now[0])
    s.step()                     # the next stream starts: the warning's gone
    assert said[-1] == []


def test_live_json_says_on_air_for_the_tray(tmp_path):
    api, now = FakeStreams(), [1000.0]
    status = tmp_path / "logs" / "live.json"
    status.parent.mkdir()
    s = _sink(tmp_path / "obs", api, now, status_file=status)
    (tmp_path / "obs").mkdir()
    _playlist(tmp_path / "obs", 0, ["w0.ts"], mtime=now[0])
    s.step()
    assert json.loads(status.read_text())["live"] is True
    now[0] += 30
    s.step()
    assert json.loads(status.read_text()) == {"live": False}


def test_only_this_streams_files_are_deleted_after_it(tmp_path):
    api, now = FakeStreams(), [1000.0]
    s = _sink(tmp_path, api, now)
    old = tmp_path / "match-night.mp4"
    old.write_bytes(b"a recording")
    import os
    os.utime(old, (now[0] - 5000, now[0] - 5000))
    other = tmp_path / "x9.ts"
    other.write_bytes(b"another stream's")
    os.utime(other, (now[0] - 5000, now[0] - 5000))
    _playlist(tmp_path, 0, ["y0.ts", "y1.ts"], ended=True, mtime=now[0])
    s.step()
    assert api.ended == ["st-0"]
    assert old.exists() and other.exists()
    assert not (tmp_path / "y0.ts").exists() and not (tmp_path / "stream.m3u8").exists()


def test_a_stream_the_cloud_ended_while_the_network_was_down_is_followed_by_a_fresh_one(tmp_path):
    api, now = FakeStreams(), [1000.0]
    s = _sink(tmp_path, api, now)
    _playlist(tmp_path, 0, ["z0.ts"], mtime=now[0])
    s.step()
    first = s.session
    api.refuse["/segments"] = [(409, {"error": "stream_ended", "message": "ended", "action": "fix"})]
    now[0] += 1
    _playlist(tmp_path, 0, ["z0.ts", "z1.ts"], mtime=now[0])
    s.step()
    assert s.stream_id is None and s.session != first and s.queued
    now[0] += 1
    _playlist(tmp_path, 0, ["z0.ts", "z1.ts"], mtime=now[0])
    s.step()
    assert s.stream_id == "st-1" and 1 in api.uploaded
