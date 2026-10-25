import asyncio
import os
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


def _obs_zip(tmp_path):
    import hashlib
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("bin/64bit/obs64.exe", b"exe")
        z.writestr("bin/64bit/obs64.pdb", b"symbols")
    data = buf.getvalue()

    class Response(io.BytesIO):
        def __enter__(self):
            return self

    return (lambda url, timeout: Response(data)), hashlib.sha256(data).hexdigest()


def test_obs_download_is_checked_and_keeps_an_earlier_setup(tmp_path):
    import pytest

    from commentator import obs_setup

    opener, sha = _obs_zip(tmp_path)
    with pytest.raises(RuntimeError, match="checksum"):
        obs_setup.download(tmp_path, sha256="0" * 64, opener=opener)
    assert list(tmp_path.iterdir()) == []

    obs_setup.download(tmp_path, sha256=sha, opener=opener)
    assert obs_setup.installed(tmp_path) and not (tmp_path / "obs/bin/64bit/obs64.pdb").exists()
    (tmp_path / "obs/config/obs-studio").mkdir(parents=True)
    (tmp_path / "obs/config/obs-studio/user.ini").write_text("[Basic]\nProfile=Shotcast\n")
    obs_setup.download(tmp_path, sha256=sha, opener=opener)          # a newer OBS: same scenes
    assert "Shotcast" in (tmp_path / "obs/config/obs-studio/user.ini").read_text()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["obs"]


def test_obs_settings_make_its_recording_the_stream_and_keep_its_own(tmp_path):
    from commentator import obs_setup

    profile = tmp_path / "obs/config/obs-studio/basic/profiles/Shotcast/basic.ini"
    profile.parent.mkdir(parents=True)
    profile.write_text("\ufeff[Video]\r\nBaseCX=1920\r\n[Hotkeys]\r\nOBSBasic.StartRecording={\"bindings\":[]}\r\n",
                       encoding="utf-8")
    playlist = tmp_path / "live" / "stream.m3u8"
    obs_setup.write_settings(tmp_path, "pw", playlist, 4456, encoder=("h264_nvenc", "preset=p4"))
    text = profile.read_text(encoding="utf-8-sig")
    assert f"FFURL={playlist}\n" in text and "RecType=FFmpeg" in text and "FFFormat=hls" in text
    assert "BaseCX=1920" in text and 'OBSBasic.StartRecording={"bindings":[]}' in text
    ws = json.loads((tmp_path / "obs/config/obs-studio/plugin_config/obs-websocket/config.json").read_text())
    assert ws["server_password"] == "pw" and ws["server_port"] == 4456 and ws["server_enabled"]
    assert (tmp_path / "obs/portable_mode.txt").exists()
    assert "LastVersion=" in (tmp_path / "obs/config/obs-studio/global.ini").read_text(encoding="utf-8-sig")


def test_obs_configure_fits_gspro_to_a_720p_stream_once(tmp_path):
    from commentator import obs_setup

    class FakeObs:
        def __init__(self):
            self.calls, self.inputs = [], ["Desktop Audio", "Mic/Aux"]

        def call(self, request, data=None):
            self.calls.append((request, data))
            return {"GetVideoSettings": {"baseWidth": 3840, "baseHeight": 2160},
                    "GetCurrentProgramScene": {"currentProgramSceneName": "Scene"},
                    "GetInputList": {"inputs": [{"inputName": n} for n in self.inputs]},
                    "CreateInput": {"sceneItemId": 7}, "GetSceneItemId": {"sceneItemId": 7},
                    "GetSpecialInputs": {"mic1": "Mic/Aux"}}.get(request, {})

    obs = FakeObs()
    obs_setup.configure(obs)
    sent = dict(obs.calls)
    assert sent["SetVideoSettings"] == {"fpsNumerator": 30, "fpsDenominator": 1, "baseWidth": 1920,
                                        "baseHeight": 1080, "outputWidth": 1280, "outputHeight": 720}
    assert sent["CreateInput"]["inputKind"] == "game_capture"
    assert sent["SetSceneItemTransform"]["sceneItemTransform"]["boundsWidth"] == 1920
    assert sent["SetInputMute"] == {"inputName": "Mic/Aux", "inputMuted": True}

    obs.inputs.append("GSPro")
    obs.calls.clear()
    obs_setup.configure(obs)                                         # set up again: no second GSPro
    assert "CreateInput" not in dict(obs.calls) and "SetSceneItemTransform" in dict(obs.calls)


def test_launching_obs_clears_its_crash_flag(tmp_path):
    root = tmp_path / "obs"
    exe = root / "bin" / "64bit" / "obs64.exe"
    exe.parent.mkdir(parents=True)
    (root / "portable_mode.txt").touch()
    sentinel = root / "config" / "obs-studio" / ".sentinel"
    sentinel.mkdir(parents=True)
    (sentinel / "run_1234").touch()
    live_stream.clear_crash_flag(exe)
    assert list(sentinel.iterdir()) == []


def test_tray_sets_up_the_live_stream(tmp_path, monkeypatch):
    from commentator import obs_setup, tray
    from commentator.config import load_config

    home, _ = tray.prepare_home(tmp_path / "Shotcast")
    (home / "obs").mkdir()                                           # as downloaded
    monkeypatch.delenv(live_stream.PASSWORD_ENV, raising=False)
    monkeypatch.setattr(live_stream, "obs_running", lambda: False)
    monkeypatch.setattr(obs_setup, "installed", lambda home: True)
    monkeypatch.setattr(obs_setup, "launch", lambda home: None)
    monkeypatch.setattr(obs_setup, "wait_for", lambda obs: None)
    configured, said = [], []
    monkeypatch.setattr(obs_setup, "configure", lambda obs: configured.append(obs.password))
    t = tray.Tray(home, load_config(home / "config.yaml"))
    monkeypatch.setattr(t, "restart_booth", lambda: said.append("restart"))
    monkeypatch.setattr(t, "info", said.append)
    monkeypatch.setattr(t, "error", said.append)
    monkeypatch.setattr(t, "notify", said.append)
    with t._live_setup:
        t.setup_live()                                               # a second click while it runs
    assert said == ["The live stream is already being set up."]
    said.clear()
    t.setup_live()
    with t._live_setup:                                              # held until the setup thread ends
        pass
    password = (home / ".env").read_text().split(f"{live_stream.PASSWORD_ENV}=")[1].split("\n")[0]
    assert password and configured == [password] and os.environ[live_stream.PASSWORD_ENV] == password
    assert t.cfg.live.enabled and (home / "live").is_dir()
    assert said[0] == "restart" and "set up" in said[1]
    assert f"FFURL={home / 'live' / 'stream.m3u8'}" in (
        home / "obs/config/obs-studio/basic/profiles/Shotcast/basic.ini").read_text(encoding="utf-8-sig")
