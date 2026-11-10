import json
import sqlite3

from commentator import highlights as H
from test_record_clips import FakeS3, _stream

ROOT = "live/"
_id = iter(range(1, 10_000))


def shot(player, hole, n, ts, par=4, putt=False, lie="fairway", to_pin=None, finish=None, outcome=None,
         total=None, offline=None, hazard=None, fmt=None, picked=None, mulligan=0, wet=False):
    return {"id": next(_id), "player": player, "hole": f"Hole {hole}, par {par}, 400 yards", "shot_number": n,
            "ts": ts, "is_putt": int(putt), "lie_before": "green" if putt else lie, "to_pin_yards": to_pin,
            "finish_yards": finish, "outcome": outcome, "total_yards": total, "offline_yards": offline,
            "hazard_x": hazard, "fmt": fmt, "picked": picked, "mulligan": mulligan, "left_out": 0,
            "gimme_yards": 1.0, "shared_ball": 0, "tags": '["in_water"]' if wet else "[]"}


def test_picks_the_big_moments_and_one_bad_shot_a_player_in_order():
    shots = [
        shot("Ann", 1, 1, 10, lie="tee", total=250),
        shot("Ann", 1, 2, 20, to_pin=150, finish=1.5),                       # stuffed to 4.5 ft: 8
        shot("Ann", 1, 3, 30, putt=True, to_pin=1.5, outcome="holed"),
        shot("Bob", 1, 1, 40, lie="tee", total=240, hazard=1.0, wet=True),   # penalty: bad
        shot("Bob", 2, 1, 50, lie="tee", total=230, hazard=1.0, wet=True),   # another: only one a player
        shot("Bob", 2, 2, 60, putt=True, to_pin=12, outcome="holed"),       # 36 ft: 8
        shot("Ann", 3, 1, 70, lie="tee", total=260),
        shot("Ann", 3, 2, 80, putt=True, to_pin=1, outcome="missed"),       # missed from 3 ft: bad
        shot("Ann", 3, 3, 90, putt=True, to_pin=0.1, outcome="holed"),
        shot("Ann", 4, 1, 100, lie="tee", total=250, mulligan=1, hazard=1.0),   # a mulligan: never
    ]
    got = H.pick(shots)
    assert [(m["shot"]["ts"], m["bad"]) for m in got] == [(20, False), (40, True), (60, False), (80, True)]
    assert got[0]["what"] == "Ann stuffs it to 4 ft from 150 yd on 1 to set up birdie"
    assert got[2]["what"] == "Bob holes a 36 ft putt for eagle on 2"
    assert got[3]["what"] == "Ann misses from 3 ft on 3"


def test_three_putts_misses_off_line_and_long_penalty_drives_are_bad_ones():
    shots = [shot("Ann", 1, 1, 10, lie="tee", total=340, hazard=1.0),            # long, but wet: not a bomb
             shot("Ann", 1, 3, 15, to_pin=100),                                   # (the penalty: no stroke 2)
             shot("Ann", 2, 1, 20, lie="tee", total=260, hazard=1.0, wet=True),
             shot("Bob", 3, 2, 30, offline=60),
             shot("Cal", 4, 2, 40, putt=True, to_pin=10, outcome="conceded"),
             shot("Cal", 4, 3, 50, putt=True, to_pin=3, outcome="conceded"),
             shot("Cal", 4, 4, 55, putt=True, outcome="gimme")]
    assert H.pick(shots) == []   # no good ones to go with them: no more bad ones than good
    shots += [shot(p, h, 3, ts, putt=True, to_pin=10, outcome="holed") for p, h, ts in
              (("Ann", 5, 60), ("Bob", 6, 70), ("Cal", 7, 80))]   # 30 ft for par: big ones
    got = H.pick(shots)
    assert [(m["shot"]["ts"], m["bad"]) for m in got if m["bad"]] == [(10, True), (30, True), (40, True)]
    assert [m["what"] for m in got if m["bad"]][1:] == ["Bob sprays one 60 yd off line on 3", "Cal three-putts on 4"]
    assert [m["post"] for m in got if m["shot"]["ts"] == 40] == [H.POST_PUTT + 10]   # from the first putt


def test_a_dry_hazard_entry_isnt_a_penalty_and_a_scramble_putt_can_miss():
    shots = [shot("Ann", 1, 1, 10, lie="tee", total=340, hazard=1.0),   # logged an entry, played on: dry
             shot("Ann", 1, 2, 20, to_pin=100),
             shot("Bob", 2, 3, 30, putt=True, to_pin=1, outcome="missed", fmt="scramble")]
    assert [m["what"] for m in H.pick(shots)] == ["Ann bombs a 340 yd drive on 1", "Bob misses from 3 ft on 2"]


def test_solid_shots_fill_in_and_personal_bests_are_big():
    shots = [shot("Ann", 1, 1, 10, lie="tee", total=285),                     # in the fairway: solid
             shot("Ann", 1, 2, 20, to_pin=150, finish=7),                    # 21 ft from 150: solid
             shot("Ann", 1, 3, 30, putt=True, to_pin=3, outcome="holed"),   # 9 ft for birdie
             shot("Ann", 2, 5, 40, putt=True, to_pin=3, outcome="holed"),   # 9 ft for bogey: no
             shot("Bob", 3, 1, 50, lie="tee", total=270),                    # Bob's longest yet
             shot("Ann", 4, 4, 60, putt=True, to_pin=3, outcome="holed")]   # 9 ft for par: solid
    shots[0]["lie_after"], shots[1]["lie_after"] = "fairway", "green"
    said = lambda before: [m["what"] for m in H.pick(shots, before=before)]
    assert said({}) == ["Ann finds the green, 21 ft from 150 yd on 1 to set up birdie",
                        "Ann holes a 9 ft putt for birdie on 1", "Ann holes a 9 ft putt for par on 4"]   # two a hole
    pb = {"Bob": {"drive": (265, 40)}}
    assert said(pb)[-1] == "Bob's longest drive yet: 270 yd on 3"
    assert "Ann holes a 9 ft putt for par on 4" not in said(pb)   # Bob gets his turn first
    assert "Bob's longest drive yet: 270 yd on 3" not in said({"Bob": {"drive": (265, 3)}})   # too few to beat
    assert said({"Bob": {"drive": (265, 3)}})[-1] != "Bob's longest drive yet: 270 yd on 3"   # too few to beat
    assert said({"Bob": {"drive": (280, 40)}})[-1] == "Bob hits a 270 yd drive on 3, 10 yd off a personal best"
    assert not any("Bob" in x for x in said({"Bob": {"drive": (300, 40)}}))   # not that close
    shots[1]["lie_after"] = "sand"   # 21 ft away, but not on the green
    assert not any("finds the green" in x for x in said({}))
    del shots[:4]   # no pin distances to go on: a drive in the fairway still counts
    shots[0].update(total_yards=285, lie_after="fairway", finish_yards=None)
    assert "Bob splits the fairway, 285 yd on 3" in said({})
    chip = [shot("Dee", 6, 3, 80, to_pin=30, finish=1.8, wet=True)]
    assert H.pick(chip) == []   # in the water, however close: not a good one
    big = [shot("Eve", 7, 1, 90, lie="tee", total=340, to_pin=340, finish=7),
           shot("Eve", 8, 4, 100, putt=True, to_pin=3, outcome="holed")]
    big[0]["lie_after"] = "green"
    assert H.pick(big, most=1)[0]["what"].startswith("Eve bombs a 340 yd drive")
    ace = [shot("Cal", 5, 1, 70, lie="tee", total=340, outcome="holed")]
    assert H.pick(ace, before={"Cal": {"drive": (330, 40)}})[0]["what"] == "Hole in one! Cal on 5, Cal's longest drive yet"
    assert H.pick(ace, before={"Cal": {"drive": (330, 40)}})[0]["post"] == H.POST_BIG


def test_a_scramble_ball_not_played_isnt_the_longest_drive():
    shots = [shot("Ann", 1, 1, 10, lie="tee", total=340, fmt="scramble", picked=0),
             shot("Bob", 1, 1, 20, lie="tee", total=330, fmt="scramble", picked=1),
             shot("Bob", 2, 2, 30, putt=True, to_pin=12, outcome="holed", fmt="scramble")]
    assert [m["shot"]["ts"] for m in H.pick(shots)] == [20, 30]


def test_a_scramble_setup_takes_its_own_teams_score():
    shots = [shot(p, 1, 2, ts, to_pin=150, finish=4, fmt="scramble", picked=1) for p, ts in (("Ann", 10), ("Cal", 20))]
    shots += [shot("Ben", 1, 5, 30, putt=True, to_pin=0.3, outcome="holed", fmt="scramble"),     # Ann's side: bogey
              shot("Dan", 1, 3, 40, putt=True, to_pin=0.3, outcome="holed", fmt="scramble")]     # Cal's: birdie
    said = lambda sides: [m["what"] for m in H.pick(shots, sides=sides)]
    assert said({"Ann": 0, "Ben": 0, "Cal": 1, "Dan": 1}) == ["Ann stuffs it to 12 ft from 150 yd on 1",
                                                              "Cal stuffs it to 12 ft from 150 yd on 1 to set up birdie"]
    assert said({}) == ["Ann stuffs it to 12 ft from 150 yd on 1", "Cal stuffs it to 12 ft from 150 yd on 1"]


def test_a_scramble_doesnt_fill_a_hole_or_count_the_balls_not_played():
    shots = [shot(p, 5, 2, 10 + i, lie="rough", to_pin=30, finish=0.8, outcome="conceded", fmt="scramble",
                  picked=int(p == "Cal")) for i, p in enumerate(("Ann", "Bob", "Cal"))]
    shots += [shot("Ann", 6, 2, 50 + i, lie="rough", to_pin=40, finish=0.7, outcome="conceded", fmt="scramble",
                   picked=1) for i in range(3)]
    got = H.pick(shots)
    assert [m["shot"]["ts"] for m in got] == [12, 50, 51]   # the ball they played on 5; on 6 two, not three


def test_most_moments_and_the_cap():
    shots = [shot("Ann", h, 2, h * 10, putt=True, to_pin=15, outcome="holed") for h in range(1, 13)]
    assert len(H.pick(shots, seconds=1e9)) == H.MAX_MOMENTS
    old = [{k: v for k, v in x.items() if k not in ("picked", "hazard_x", "tags", "shared_ball", "gimme_yards")}
           for x in shots]   # an older database's shots
    assert len(H.pick(old, seconds=1e9)) == H.MAX_MOMENTS


def _reel(*ts):
    return {"label": "Pebble · Ann", "date": "2026-11-01", "ended": 1200, "league": False,
            "moments": [{"id": i, "ts": t, "player": "Ann", "hole": i, "what": f"m{i}", "bad": False, "post": 12.0}
                        for i, t in enumerate(ts, 1)]}


def test_a_reel_is_cut_from_the_stream_with_a_break_between_moments():
    s3 = FakeS3(_stream(60))   # 240 s from 1000
    assert H.sync(s3, "b", "", {7: _reel(1020, 1026, 1150)}, clock=lambda: 1300) == {"kept": 1, "added": 1, "removed": 0}
    pl = s3.objects[ROOT + "reels/7/reel.m3u8"].decode()
    # Each shows 5 s on and starts 2 s before that: 20 s and 26 s in share one run (23-38 s: segments
    # 5-9); 150 s in, the next (153-162 s: 38-40).
    assert pl.count("#EXT-X-DISCONTINUITY") == 1 and pl.endswith("#EXT-X-ENDLIST\n")
    assert [ln.split("/")[1] for ln in pl.splitlines() if ln.startswith("s1/")] == \
        [f"stream{i}.ts" for i in [*range(5, 10), *range(38, 41)]]
    assert s3.objects[ROOT + "reels/7/s1/stream38.ts"] == b"stream38.ts"
    reel = json.loads(s3.objects[ROOT + "reels.json"])["reels"]["7"]
    assert [m["at"] for m in reel["moments"]] == [4.0, 10.0, 22.0] and reel["duration"] == 32.0   # a second before each shows
    # Unchanged: nothing to do. Said differently, or made a league round: the same cut, its words redone.
    assert H.sync(s3, "b", "", {7: _reel(1020, 1026, 1150)}, clock=lambda: 1300)["added"] == 0
    renamed = _reel(1020, 1026, 1150)
    renamed["league"], renamed["moments"][0]["what"] = True, "new words"
    assert H.sync(s3, "b", "", {7: renamed}, clock=lambda: 1300)["added"] == 0
    reel = json.loads(s3.objects[ROOT + "reels.json"])["reels"]["7"]
    assert reel["league"] and reel["moments"][0] == {"at": 4.0, "id": 1, "player": "Ann", "hole": 1, "what": "new words", "bad": False}
    longer = _reel(1020, 1026, 1150)
    longer["moments"][2]["post"] = 20.0   # a longer cut: a new playlist
    assert H.sync(s3, "b", "", {7: longer}, clock=lambda: 1300)["added"] == 1
    stopped = _reel(1020, 1026, 1150)   # the booth's next line starts 13 s on: not its segment (160-164 s)
    stopped["moments"][2].update(stop=13.0, must=5.0)
    H.sync(s3, "b", "", {7: stopped}, clock=lambda: 1300)
    pl = s3.objects[ROOT + "reels/7/reel.m3u8"].decode()
    assert "s1/stream39.ts" in pl and "s1/stream40.ts" not in pl
    stopped["moments"][2]["must"] = 10.0 + H.MUST_GRACE + 1   # ...unless its own talk runs well into it
    H.sync(s3, "b", "", {7: stopped}, clock=lambda: 1300)
    assert "s1/stream40.ts" in s3.objects[ROOT + "reels/7/reel.m3u8"].decode()
    # Gone off the page (or past its keep): deleted.
    assert H.sync(s3, "b", "", {}, clock=lambda: 1300) == {"kept": 0, "added": 0, "removed": 1}
    assert not [k for k in s3.objects if k.startswith(ROOT + "reels/")]


def test_a_reel_cut_before_the_streams_delay_was_allowed_for_moves_its_moments_once():
    s3 = FakeS3(_stream(60))
    H.sync(s3, "b", "", {7: _reel(1020, 1026)}, clock=lambda: 1300)
    entry = json.loads(s3.objects[ROOT + "reels.json"])["reels"]["7"]
    del entry["shows"]
    entry["moments"][0]["at"], entry["moments"][1]["at"], entry["finish"] = 3.0, 9.0, {"at": 2.0, "cut": [9, 12.0, None, None]}
    s3.objects[ROOT + "reels.json"] = json.dumps({"reels": {"7": entry}}).encode()
    reel = {**_reel(1020, 1026), "finish": {"id": 9, "ts": 1150, "player": "Ann", "hole": 9, "what": "fin", "post": 12.0}}
    assert H.sync(s3, "b", "", {7: reel}, clock=lambda: 1300)["added"] == 0   # not cut again
    entry = json.loads(s3.objects[ROOT + "reels.json"])["reels"]["7"]
    assert [m["at"] for m in entry["moments"]] == [8.0, 14.0] and entry["finish"]["at"] == 7.0
    H.sync(s3, "b", "", {7: reel}, clock=lambda: 1300)
    assert [m["at"] for m in json.loads(s3.objects[ROOT + "reels.json"])["reels"]["7"]["moments"]] == [8.0, 14.0]


def test_a_reel_waits_for_its_stream_and_keeps():
    s3 = FakeS3(_stream(15, ended=False))   # 60 s so far: the last moment's not in yet
    assert H.sync(s3, "b", "", {7: _reel(1020, 1055)}, clock=lambda: 1100)["added"] == 0
    s3.objects.update(_stream(30, ended=False))
    assert H.sync(s3, "b", "", {7: _reel(1020, 1055)}, clock=lambda: 1200)["added"] == 1
    week = 8 * 86400
    assert not H.keep(_reel(1), 1200 + week, 7, None)
    assert H.keep({**_reel(1), "league": True}, 1200 + week, 7, "2026-03-01")
    assert not H.keep({**_reel(1), "league": True}, 1200, 7, "2026-12-01")


def test_the_build_finds_each_rounds_reel(tmp_path):
    import sqlite3
    from contextlib import closing

    from test_stats_site import _season
    from commentator import stats_site

    path, reels = _season(tmp_path), {}
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes, is_league)"
                   " VALUES (1, 30, '2026-10-17', 'Pebble', 'Ann', 9, 1)")
        db.execute("UPDATE shots SET is_putt = 1, lie_before = 'green', to_pin_yards = 10, outcome = 'holed',"
                   " hole = 'Hole ' || id || ', par 4, 400 yards', shot_number = 3 WHERE session_id = 1")
    stats_site.build(path, tmp_path / "site", reels=reels)
    assert list(reels) and all(r["league"] and len(r["moments"]) >= 2 for r in reels.values())
    assert all(r["finish"]["what"].endswith("to finish") for r in reels.values())   # a league round's, for its recap
    # Its last putt left a gimme, the gimme in after the round was recorded: still its finish.
    with closing(sqlite3.connect(path)) as db, db:
        last = db.execute("SELECT id FROM shots WHERE session_id = 1 ORDER BY ts DESC, id DESC LIMIT 1").fetchone()[0]
        db.execute("UPDATE shots SET outcome = 'conceded' WHERE id = ?", (last,))
        db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, is_putt, lie_before, outcome)"
                   " SELECT session_id, 31, player, course, hole, 4, 1, 'green', 'gimme' FROM shots WHERE id = ?", (last,))
    reels.clear()
    stats_site.build(path, tmp_path / "site", reels=reels)
    assert [(r["finish"]["id"], "gimme" in r["finish"]["what"]) for r in reels.values()] == [(last, True)]


def test_a_clip_runs_till_the_booth_is_done_talking_about_it_and_stops_before_whatever_follows():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE lines (id INTEGER PRIMARY KEY, session_id, shot_id, ts, kind, text)")
    words = lambda n: " ".join(["word"] * n)   # 27 words: 10 s said
    chip = {**shot("Ann", 1, 3, 100, to_pin=30, finish=1.5), "session_id": 1}
    nxt = {**shot("Bob", 1, 3, 140), "session_id": 1}
    db.executemany("INSERT INTO lines (session_id, shot_id, ts, kind, text) VALUES (?,?,?,?,?)",
                   [(1, chip["id"], 108.0, "shot", words(27)),
                    (1, None, 103.0, "reaction", words(27)),    # (no shot logged with it: the chip's)
                    (1, None, 133.0, "segment", words(27))])
    post = lambda at, last=None: H._post(H.POST_SHOT, at, last or at, H._talk(db, [chip, nxt]))
    heard = lambda t, at=100: round(t + H.TALK_LAG - at, 1)
    done = 123 + H.TALK_TAIL   # the call waits for the reaction: 113-123
    assert post(chip) == {"post": heard(done), "stop": heard(133 - H.STOP_MARGIN), "must": heard(done)}
    db.execute("UPDATE lines SET ts = 124 WHERE kind = 'segment'")   # straight after: never waited for
    assert post(chip) == {"post": heard(done), "stop": heard(done), "must": heard(done)}
    db.execute("UPDATE lines SET ts = 128 WHERE kind = 'segment'")
    assert post(chip)["stop"] == heard(128 - H.STOP_MARGIN)
    db.execute("UPDATE lines SET ts = 133 WHERE kind = 'segment'")
    nxt["ts"] = 130   # ...nor the next swing
    assert post(chip)["stop"] == heard(130 + H.STREAM_LAG - H.TALK_LAG - H.STOP_MARGIN)
    nxt["ts"] = 140
    first = {**chip, "ts": 40.0}   # a three-putt played from its first putt: not capped short of the last
    assert post(first, chip)["post"] == heard(done, 40)
    long_talk = ({1: [(100.0, 200.0, 100.0)]}, {1: [100.0]})   # on and on: capped
    assert H._post(H.POST_SHOT, chip, chip, long_talk)["post"] == H.TALK_MAX
    assert H._post(H.POST_SHOT, chip, chip, H._talk(sqlite3.connect(":memory:"), [chip]))["post"] == H.POST_SHOT
    # Not called, only its flight: on past that, but not into the banter after it.
    db.execute("DELETE FROM lines")
    db.executemany("INSERT INTO lines (session_id, shot_id, ts, kind, text) VALUES (?,?,?,?,?)",
                   [(1, None, 100.0, "flight", words(20)), (1, None, 112.0, "banter", words(40))])
    assert post(chip) == {"post": heard(112 - H.STOP_MARGIN), "stop": heard(112 - H.STOP_MARGIN),
                          "must": heard(100 + 20 / H.TALK_WORDS_PER_SECOND + H.TALK_TAIL)}


def test_a_clip_with_nothing_said_runs_till_the_ball_is_at_rest_and_a_gimme_is_no_next_swing():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE lines (id INTEGER PRIMARY KEY, session_id, shot_id, ts, kind, text)")   # no booth
    chip = {**shot("Ann", 1, 3, 100, to_pin=30, finish=1.0), "session_id": 1}
    gimme = {**shot("Ann", 1, 4, 108.6, putt=True, outcome="gimme"), "session_id": 1}   # as the ball stops
    nxt = {**shot("Bob", 1, 3, 140), "session_id": 1}
    got = H._post(H.POST_SHOT, chip, chip, H._talk(db, [chip, gimme, nxt]))
    settled = round(8.6 + H.SHOT_SHOWS + H.REST_TAIL, 1)   # over once the ball's stopped
    assert got == {"post": settled, "stop": round(40 + H.STREAM_LAG - H.STOP_MARGIN, 1), "must": settled}
    gimme["ts"] = 103   # (a quick chip's over as quick)
    assert H._post(H.POST_SHOT, chip, chip, H._talk(db, [chip, gimme, nxt]))["post"] == 3 + H.SHOT_SHOWS + H.REST_TAIL
    nxt["ts"], gimme["ts"] = 103, 120   # a gimme after the next swing is that one's; and a next swing
    # that soon still can't cut it before the ball's at rest
    assert H._post(H.POST_SHOT, chip, chip, H._talk(db, [chip, nxt, gimme]))["post"] == H.SHOT_SHOWS + H.MIN_AFTER


def test_the_booths_logged_air_times_place_its_lines_and_one_never_aired_is_skipped():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE lines (id INTEGER PRIMARY KEY, session_id, shot_id, ts, kind, text, aired_at, aired_seconds)")
    chip = {**shot("Ann", 1, 3, 100, to_pin=30, finish=1.5), "session_id": 1}
    db.executemany("INSERT INTO lines (session_id, shot_id, ts, kind, text, aired_at, aired_seconds)"
                   " VALUES (?,?,?,?,?,?,?)",
                   [(1, chip["id"], 108.0, "shot", "a few words", 109.0, 12.0),   # longer than its words
                    (1, chip["id"], 108.0, "shot", " ".join(["word"] * 50), None, None)])   # cut off
    got = H._post(H.POST_SHOT, chip, chip, H._talk(db, [chip]))
    assert got["post"] == got["must"] == round(121.0 + H.TALK_TAIL + H.STREAM_LAG - 100, 1)
    db.execute("ALTER TABLE lines DROP COLUMN aired_at")   # an older database: by its words
    assert H._post(H.POST_SHOT, chip, chip, H._talk(db, [chip]))["post"] > got["post"]


def test_a_reel_keeps_to_its_length():
    shots = [shot("Ann", h, 2, h * 100, putt=True, to_pin=15, outcome="holed") for h in range(1, 13)]
    clip = H.PRE + H.POST_PUTT + H.CLIP_SLACK
    assert len(H.pick(shots, seconds=3 * clip + 1)) == 3


def test_a_league_rounds_finish_is_cut_on_its_own_for_the_recap():
    # Its last hole: Bo's putt left a gimme, Ann holed hers after: Ann's finished it.
    shots = [shot("Ann", 8, 1, 10, lie="tee"), shot("Ann", 9, 1, 20, lie="tee"), shot("Bo", 9, 1, 25, lie="tee"),
             shot("Bo", 9, 2, 40, putt=True, to_pin=4.0, outcome="gimme"),
             shot("Ann", 9, 3, 50, putt=True, to_pin=4.0, outcome="holed"), shot("Bo", 9, 3, 41, putt=True, outcome="gimme")]
    f = H.finish(shots)
    assert f["shot"] is not None and f["shot"]["ts"] == 50 and f["what"] == "Ann holes a 12 ft putt on 9 for birdie to finish"
    assert H.finish(shots[:3]) is None   # not finished
    s3 = FakeS3(_stream(60))
    reel = {**_reel(1020, 1026), "league": True,
            "finish": {"id": 99, "ts": 1150, "player": "Ann", "hole": 9, "what": "fin", "bad": False, "level": 5, "post": 12.0}}
    odd = FakeS3({"golf-reels/" + k: v for k, v in _stream(60).items()})   # (a prefix with "reels" in it)
    H.sync(odd, "b", "golf-reels", {7: reel}, clock=lambda: 1300)
    assert "golf-reels/" + ROOT + "reels/7/finish/s1/stream38.ts" in odd.objects
    H.sync(s3, "b", "", {7: reel}, clock=lambda: 1300)
    # Not in the reel: its own playlist, under finish/.
    assert "stream38.ts" not in s3.objects[ROOT + "reels/7/reel.m3u8"].decode()
    assert "s1/stream38.ts" in s3.objects[ROOT + "reels/7/finish/finish.m3u8"].decode()
    assert s3.objects[ROOT + "reels/7/finish/s1/stream38.ts"] == b"stream38.ts"
    entry = json.loads(s3.objects[ROOT + "reels.json"])["reels"]["7"]
    assert entry["finish"]["at"] == 2.0 and entry["finish"]["cut"] == [99, 12.0, None, None] and len(entry["moments"]) == 2
    # The reel cut again: the finish stays. Gone from the round: deleted.
    longer = {**reel, "moments": [{**m, "post": 20.0} for m in reel["moments"]]}
    H.sync(s3, "b", "", {7: longer}, clock=lambda: 1300)
    assert ROOT + "reels/7/finish/s1/stream38.ts" in s3.objects
    assert json.loads(s3.objects[ROOT + "reels.json"])["reels"]["7"]["finish"]["what"] == "fin"
    H.sync(s3, "b", "", {7: {k: v for k, v in longer.items() if k != "finish"}}, clock=lambda: 1300)
    assert not [k for k in s3.objects if "/finish/" in k]
    assert "finish" not in json.loads(s3.objects[ROOT + "reels.json"])["reels"]["7"]


def test_the_shot_a_league_lead_turned_on_is_cut_from_the_rounds_stream():
    bogey = [shot("Ann", 6, 1, 1010, lie="tee"), shot("Ann", 6, 2, 1030, to_pin=150, finish=12),
             shot("Ann", 6, 3, 1050, putt=True, to_pin=12, outcome="missed"),
             shot("Ann", 6, 4, 1070, putt=True, to_pin=2, outcome="missed"),
             shot("Ann", 6, 5, 1090, putt=True, to_pin=1, outcome="holed")]
    # The bogey: its last missed putt, not the tap-in.
    m = H.turn_shot(bogey, 6, "miss")
    assert m["ts"] == 1070 and m["what"] == "Ann misses from 6 ft on 6" and m["bad"] and m["post"] == H.TURN_POST
    wet = [shot("Bo", 6, 1, 1010, lie="tee", wet=True, outcome="water"), *bogey[1:]]
    assert H.turn_shot(wet, 6, "miss")["what"] == "Bo finds a penalty area on 6"
    left = [shot("Bo", 6, 1, 1005, lie="tee", wet=True, outcome="water", fmt="scramble", picked=0), *bogey]
    assert H.turn_shot(left, 6, "miss")["ts"] == 1070   # a scramble ball not played didn't cost them
    # A birdie from a long putt shows the putt; a tap-in birdie doesn't, but the approach that set it up.
    long = [shot("Ann", 7, 1, 1010, lie="tee"), shot("Ann", 7, 2, 1030, to_pin=150, finish=6),
            shot("Ann", 7, 3, 1050, putt=True, to_pin=6, outcome="holed")]
    assert H.turn_shot(long, 7, "made")["what"] == "Ann holes a 18 ft putt on 7"
    tap = [*long[:1], shot("Ann", 7, 2, 1030, to_pin=150, finish=1), shot("Ann", 7, 3, 1050, putt=True, to_pin=1, outcome="holed")]
    assert H.turn_shot(tap, 7, "made") is None
    assert H.turn_shot(tap, 7, "setup")["what"] == "Ann hits it to 3 ft from 150 yd on 7"
    assert H.turn_shot(long, 8, "miss") is None   # nothing on that hole
    # Best ball: Ann's birdie putt counted, not Bo's tap-in for bogey after it, nor his approach.
    both = [*long, shot("Bo", 7, 1, 1012, lie="tee"), shot("Bo", 7, 2, 1032, to_pin=160, finish=2),
            shot("Bo", 7, 3, 1060, putt=True, to_pin=2, outcome="missed"), shot("Bo", 7, 4, 1070, putt=True, to_pin=1, outcome="holed")]
    assert H.turn_shot(sorted(both, key=lambda s: s["ts"]), 7, "made")["ts"] == 1050
    assert H.turn_shot(sorted(both, key=lambda s: s["ts"]), 7, "setup")["player"] == "Ann"   # Bo's closer one 2-putted
    # Cut on its own from the round's stream, where it's kept.
    s3 = FakeS3(_stream(60))
    cut = H.cut_alone(s3, "b", "", [H.turn_shot(long, 7, "made")], clock=lambda: 1300)
    ((parts, at),) = cut.values()
    assert [u for p in parts for u, _ in p][0].startswith("s1/stream") and 0 < at < 12
    assert H.cut_alone(FakeS3({}), "b", "", [H.turn_shot(long, 7, "made")]) == {}
