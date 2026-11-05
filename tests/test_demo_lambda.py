"""The demo site's live function (tools/demo/demo-site.yaml, Live), run from the template itself: the
looped recording's live playlist and the scripted chat, with a viewer's messages only in their cookie."""
import json
import re
import sys
import textwrap
import types
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / "tools" / "demo" / "demo-site.yaml"
SCRIPT = {"session": "s1", "first": 0, "segments": [4.0, 4.0, 4.0, 3.0],   # a 15 second loop
          "chat": [{"t": 1, "name": "A", "text": "first"}, {"t": 9, "name": "B", "text": "later"}]}


@pytest.fixture
def live(monkeypatch):
    code = re.search(r"\n  Live:\n.*?ZipFile: \|\n(.*?)\n\n  LiveUrl:", TEMPLATE.read_text(), re.S).group(1)
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda name: None))
    mod = types.ModuleType("live_fn")
    exec(compile(textwrap.dedent(code), "live_fn", "exec"), mod.__dict__)
    mod.script = lambda: SCRIPT
    clock = {"now": 0.0}
    mod.time = types.SimpleNamespace(time=lambda: clock["now"])

    def call(now, path="/chat/messages", method="GET", body=None, cookies=None):
        clock["now"] = now
        r = mod.handler({"rawPath": path, "requestContext": {"http": {"method": method}},
                         "body": json.dumps(body) if body is not None else None, "cookies": cookies}, None)
        return r
    return call


def test_the_live_playlist_wraps_to_the_start_with_a_discontinuity(live):
    # 2 loops and 9 s in: segments 0 and 1 of loop 3 are whole, after loop 2's last four.
    body = live(2 * 15 + 9, "/live/s/s1/stream.m3u8")["body"]
    assert "#EXT-X-MEDIA-SEQUENCE:4" in body and "#EXT-X-DISCONTINUITY-SEQUENCE:1" in body
    assert re.findall(r"stream\d+\.ts|#EXT-X-DISCONTINUITY\n", body) == [
        "stream0.ts", "stream1.ts", "stream2.ts", "stream3.ts", "#EXT-X-DISCONTINUITY\n", "stream0.ts", "stream1.ts"]
    status = json.loads(live(2 * 15 + 9, "/live/status.json")["body"])
    assert status["live"] and status["session"] == "s1"


def test_the_loop_can_be_a_stretch_from_the_middle_of_the_recording(live, monkeypatch):
    monkeypatch.setitem(SCRIPT, "first", 350)
    body = live(2 * 15 + 9, "/live/s/s1/stream.m3u8")["body"]
    assert re.findall(r"stream\d+\.ts", body) == [f"stream{i}.ts" for i in (350, 351, 352, 353, 350, 351)]


def test_the_chat_shows_the_script_so_far_and_a_viewers_own_messages_from_their_cookie(live):
    got = json.loads(live(15 + 5)["body"])   # 5 s into the second loop
    assert got["on"] and [(m["name"], m["at"]) for m in got["messages"]] == [("B", 9), ("A", 16)]
    sent = live(15 + 6, method="POST", body={"name": " Me ", "text": "hi  booth"})
    assert sent["statusCode"] == 200 and len(sent["cookies"]) == 1
    cookie = sent["cookies"][0].split(";")[0]
    mine = [m for m in json.loads(live(15 + 7, cookies=[cookie])["body"])["messages"] if m["name"] == "Me"]
    assert [m["text"] for m in mine] == ["hi booth"]
    assert not any(m["name"] == "Me" for m in json.loads(live(15 + 7)["body"])["messages"])   # nobody else's
    assert live(15 + 8, method="POST", body={"name": "", "text": "x"})["statusCode"] == 400


def test_a_viewers_cookie_drops_their_oldest_messages_to_fit(live):
    cookie = None
    for i in range(5):
        r = live(20 + i, method="POST", body={"name": "Me", "text": "字" * 200}, cookies=[cookie] if cookie else None)
        cookie = r["cookies"][0].split(";")[0]
    assert len(cookie) < 3100
    mine = [m for m in json.loads(live(30, cookies=[cookie])["body"])["messages"] if m["name"] == "Me"]
    assert 1 <= len(mine) < 5 and mine[-1]["at"] == 24


def test_redact_renames_josh_and_keeps_his_lines_when_run_again(tmp_path):
    # The demo workflow rebuilds from the already-redacted copy: that must not drop Josh's commentary.
    import sqlite3

    from commentator.store import Store
    from tools.demo import build

    src = tmp_path / "season.db"
    Store(str(src)).db.close()
    db = sqlite3.connect(src)
    (sess,) = db.execute("SELECT id FROM sessions").fetchone()
    db.executemany("INSERT INTO shots (id, session_id, ts, player, is_putt) VALUES (?, ?, ?, ?, 0)",
                   [(1, sess, 10.0, "Josh Merritt"), (2, sess, 20.0, "Dan Binder")])
    db.executemany("INSERT INTO lines (session_id, shot_id, ts, kind, speaker, text) VALUES (?, ?, ?, 'shot', 'A', ?)",
                   [(sess, 1, 10.0, "Josh Merritt finds the fairway."), (sess, 2, 20.0, "Dan Binder pulls it.")])
    db.commit()
    db.close()

    once, twice = tmp_path / "once.db", tmp_path / "twice.db"
    build.redact(src, once)
    build.redact(once, twice)
    for out in (once, twice):
        db = sqlite3.connect(out)
        assert [p for (p,) in db.execute("SELECT player FROM shots ORDER BY id")] == ["Josh Mercer", "Mike Russo"]
        assert [t for (t,) in db.execute("SELECT text FROM lines")] == ["Josh Mercer finds the fairway."]
        db.close()
